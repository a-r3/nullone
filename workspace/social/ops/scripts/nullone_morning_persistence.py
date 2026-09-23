#!/usr/bin/env python3
"""Deterministic Morning queue/ledger persistence (issue #153).

Proven live evidence (2026-09-23 Claude/Sonnet production-parity
benchmark): the model completed genuine editorial work (valid board +
valid handoff, 6 candidates, ~7.5 min) but then stayed in the tool loop
until the outer 600s deadline because prompt steps 7/11 told it to
mutate `social/state/candidate-queue.md` and
`social/state/topic-ledger.jsonl` itself -- and it reached for
`Bash`/`sed`, which the reviewed Claude transport allowlist denies.

Architecture (reviewed fix): the model owns research, reasoning, and
the structured board/handoff artifacts, then STOPS. Queue/ledger
persistence is deterministic code in `run_morning_editorial`, derived
ONLY from the validated handoff snapshot:

    validated handoff
    -> deterministic candidate queue update
    -> deterministic topic ledger updates
    -> persistence result

Safety properties:

- Mutation happens only AFTER the board exists non-empty AND the
  handoff loads through the strict validator (`load_handoff_snapshot`).
  Invalid/failed model output raises `MorningPersistenceError` with
  zero state mutated.
- Idempotent: a date section already present in the queue is never
  re-appended; a `candidate_id` already queued is never duplicated;
  ledger rows are appended once per (`editorial_date`, `candidate_id`)
  via the existing `nullone_state.append_jsonl_once` writer.
- Single-writer: the only caller is `run_morning_editorial`, which
  already serializes each occurrence behind its `fcntl` occurrence
  lock and never re-invokes the provider once artifacts exist -- a
  replay therefore replays persistence against already-persisted state
  and adds nothing.
- Transport-neutral: applies equally to the Claude and OpenCode
  transports. No model route, timeout, capability, publication,
  Telegram, or Zernio behavior changes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from nullone_bridge_common import BridgeError, now_iso
from nullone_editorial_candidate_handoff import (
    EditorialHandoffError,
    board_relative_path,
    load_handoff_snapshot,
)
from nullone_state import append_jsonl_once

QUEUE_RELATIVE = "social/state/candidate-queue.md"
TOPIC_LEDGER_RELATIVE = "social/state/topic-ledger.jsonl"

# Handoff editorial statuses the deterministic writer admits to the
# queue. DEFERRED/REJECTED candidates are recorded in the topic ledger
# only (as NOT_QUEUED), never queued.
QUEUED_STATUSES = frozenset({"READY", "NEW", "RESEARCHING"})

EVENT_QUEUED = "MORNING_EDITORIAL_SCAN"
EVENT_NOT_QUEUED = "MORNING_EDITORIAL_SCAN_NOT_QUEUED"


class MorningPersistenceError(BridgeError):
    """Deterministic Morning queue/ledger persistence failed.

    Raised BEFORE any state mutation when the board/handoff cannot be
    trusted, and surfaced by the runtime as a domain failure
    (MORNING_PERSISTENCE_ERROR) -- never as false success.
    """


def _workspace_path(workspace_root: Any, relative: str) -> Path:
    """Resolve a state path, contained in the workspace, never a symlink."""

    if not isinstance(workspace_root, Path):
        raise MorningPersistenceError("workspace_root must be a Path")
    root = workspace_root.resolve()
    unresolved = root / relative
    if unresolved.is_symlink():
        raise MorningPersistenceError(f"state path must not be a symlink: {relative}")
    path = unresolved.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MorningPersistenceError(
            f"state path escapes workspace: {relative}"
        ) from exc
    return path


def _section_header(editorial_date: str) -> str:
    return f"## {editorial_date} morning-editorial scan"


def _render_queue_entry(
    *, editorial_date: str, board_rel: str, candidate: dict[str, Any]
) -> str:
    """Render one deterministic queue entry from a validated candidate.

    Every value is carried over from the validated handoff snapshot --
    nothing is invented. Narrative detail (why_now, audience_value,
    duplicate analysis) stays in the board Markdown the entry points
    at via `report`; the entry records the machine-actionable core
    plus the model's own `angle` as rationale.
    """

    source_urls = candidate.get("source_urls") or []
    primary_source = candidate["source_attribution"]
    if source_urls:
        primary_source = f"{primary_source} ({', '.join(source_urls)})"
    score = candidate.get("score")
    score_text = str(score) if isinstance(score, (int, float)) else "unscored"
    lines = [
        f"- **discovered_at:** {editorial_date}",
        f"- **candidate_id:** {candidate['candidate_id']}",
        f"- **topic:** {candidate['topic']}",
        f"- **topic_bucket:** {candidate.get('topic_bucket') or 'AI'}",
        f"- **content_type:** {candidate['content_type']}",
        f"- **topic_cluster:** {candidate['topic_cluster']}",
        f"- **primary_source:** {primary_source}",
        f"- **score:** {score_text}",
        f"- **status:** {candidate['editorial_status']}",
        f"- **verification_status:** {candidate['verification']}",
        f"- **story_eligible:** {str(bool(candidate['story_eligible'])).lower()}",
        f"- **angle:** {candidate['angle']}",
        f"- **evidence_refs:** {'; '.join(candidate['evidence_refs'])}",
        f"- **report:** {board_rel}",
    ]
    freshness_class = candidate.get("freshness_class")
    if isinstance(freshness_class, str) and freshness_class.strip():
        lines.append(f"- **freshness_class:** {freshness_class.strip()}")
    freshness_deadline = candidate.get("freshness_deadline")
    if isinstance(freshness_deadline, str) and freshness_deadline.strip():
        lines.append(f"- **freshness_deadline:** {freshness_deadline.strip()}")
    return "\n".join(lines)


def _ledger_row(
    *,
    timestamp: str,
    editorial_date: str,
    board_rel: str,
    candidate: dict[str, Any],
    queued: bool,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "event": EVENT_QUEUED if queued else EVENT_NOT_QUEUED,
        "editorial_date": editorial_date,
        "candidate_id": candidate["candidate_id"],
        "rank": candidate["rank"],
        "topic": candidate["topic"],
        "topic_cluster": candidate["topic_cluster"],
        "content_type": candidate["content_type"],
        "verification": candidate["verification"],
        "status": candidate["editorial_status"],
        "story_eligible": bool(candidate["story_eligible"]),
        "report": board_rel,
    }


def persist_morning_state(
    *,
    workspace_root: Path,
    editorial_date: str,
    now: Callable[[], str] = now_iso,
) -> dict[str, Any]:
    """Persist one Morning date's validated handoff into queue + ledger.

    Returns `{"queue_added": int, "ledger_added": int,
    "ledger_skipped_duplicate": int, "candidates": int}`. Raises
    `MorningPersistenceError` without mutating anything when the board
    is missing/empty or the handoff is invalid.
    """

    board_rel = board_relative_path(editorial_date)
    if not isinstance(workspace_root, Path):
        raise MorningPersistenceError("workspace_root must be a Path")
    root = workspace_root.resolve()
    board_path = _workspace_path(root, board_rel)
    if not board_path.is_file() or board_path.stat().st_size == 0:
        raise MorningPersistenceError(
            "Morning board artifact missing or empty; no state mutated."
        )

    try:
        snapshot = load_handoff_snapshot(
            workspace_root=root, editorial_date=editorial_date
        )
    except (OSError, json.JSONDecodeError, EditorialHandoffError) as exc:
        raise MorningPersistenceError(
            "Morning handoff invalid; no state mutated."
        ) from exc

    candidates = snapshot["candidates"]
    queued = [c for c in candidates if c["editorial_status"] in QUEUED_STATUSES]
    skipped = [c for c in candidates if c["editorial_status"] not in QUEUED_STATUSES]

    queue_added = 0
    if queued:
        queue_path = _workspace_path(root, QUEUE_RELATIVE)
        existing = (
            queue_path.read_text(encoding="utf-8")
            if queue_path.is_file()
            else ""
        )
        header = _section_header(editorial_date)
        if header not in existing:
            fresh = [
                c
                for c in queued
                if f"- **candidate_id:** {c['candidate_id']}" not in existing
            ]
            by_status: dict[str, list[dict[str, Any]]] = {}
            for c in sorted(fresh, key=lambda c: c["rank"]):
                by_status.setdefault(c["editorial_status"], []).append(c)
            parts = [header, "", f"See: {board_rel}", ""]
            for status, group in by_status.items():
                ranks = ",".join(str(c["rank"]) for c in group)
                parts.append(f"### {status} (board RANK {ranks})")
                parts.append("")
                parts.append(
                    "\n\n".join(
                        _render_queue_entry(
                            editorial_date=editorial_date,
                            board_rel=board_rel,
                            candidate=c,
                        )
                        for c in group
                    )
                )
                parts.append("")
            block = "\n".join(parts) + "\n"
            if not queue_path.is_file():
                block = (
                    "# NullOne Candidate Queue\n\n"
                    "Current active editorial candidates.\n\n" + block
                )
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            with queue_path.open("a", encoding="utf-8") as f:
                f.write(block)
            queue_added = len(fresh)

    ledger_path = _workspace_path(root, TOPIC_LEDGER_RELATIVE)
    ledger_added = 0
    ledger_skipped_duplicate = 0
    timestamp = now()
    for candidate in queued:
        added = append_jsonl_once(
            ledger_path,
            _ledger_row(
                timestamp=timestamp,
                editorial_date=editorial_date,
                board_rel=board_rel,
                candidate=candidate,
                queued=True,
            ),
            unique=("editorial_date", "candidate_id"),
        )
        ledger_added += 1 if added else 0
        ledger_skipped_duplicate += 0 if added else 1
    for candidate in skipped:
        added = append_jsonl_once(
            ledger_path,
            _ledger_row(
                timestamp=timestamp,
                editorial_date=editorial_date,
                board_rel=board_rel,
                candidate=candidate,
                queued=False,
            ),
            unique=("editorial_date", "candidate_id"),
        )
        ledger_added += 1 if added else 0
        ledger_skipped_duplicate += 0 if added else 1

    return {
        "queue_added": queue_added,
        "ledger_added": ledger_added,
        "ledger_skipped_duplicate": ledger_skipped_duplicate,
        "candidates": len(candidates),
    }


def self_test() -> int:
    import tempfile

    date = "2026-09-08"
    board_rel = f"social/research/daily/{date}-editorial-board.md"
    handoff_rel = f"social/research/daily/{date}-editorial-candidates.json"

    def candidate(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "candidate_id": "cand-1",
            "rank": 1,
            "topic": "Topic",
            "topic_cluster": "cluster",
            "content_type": "NEWS",
            "angle": "Angle",
            "verification": "PASS",
            "evidence_refs": ["evidence"],
            "source_attribution": "Source",
            "source_urls": ["https://example.com/a"],
            "editorial_status": "READY",
            "story_eligible": True,
        }
        base.update(overrides)
        return base

    def handoff(*cands: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "nullone.editorial-candidate-handoff.v1",
            "contract_version": "1.0.0",
            "editorial_date": date,
            "board_path": board_rel,
            "candidates": list(cands),
        }

    def setup(root: Path, doc: dict[str, Any], board: str = "# Board\n") -> None:
        (root / board_rel).parent.mkdir(parents=True, exist_ok=True)
        (root / board_rel).write_text(board, encoding="utf-8")
        (root / handoff_rel).write_text(json.dumps(doc), encoding="utf-8")

    # 1. Valid output persists queue + ledger.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        setup(root, handoff(candidate()))
        out = persist_morning_state(
            workspace_root=root, editorial_date=date, now=lambda: "2026-09-08T00:00:00Z"
        )
        assert out == {
            "queue_added": 1,
            "ledger_added": 1,
            "ledger_skipped_duplicate": 0,
            "candidates": 1,
        }, out
        queue = (root / QUEUE_RELATIVE).read_text(encoding="utf-8")
        assert f"## {date} morning-editorial scan" in queue
        assert "- **candidate_id:** cand-1" in queue
        rows = (root / TOPIC_LEDGER_RELATIVE).read_text(encoding="utf-8").splitlines()
        assert len(rows) == 1
        row = json.loads(rows[0])
        assert row["event"] == EVENT_QUEUED
        assert row["editorial_date"] == date
        assert row["candidate_id"] == "cand-1"

    # 2. Invalid handoff mutates nothing.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        bad = handoff(candidate())
        bad["candidates"][0]["verification"] = "MAYBE"
        setup(root, bad)
        try:
            persist_morning_state(workspace_root=root, editorial_date=date)
            raise AssertionError("invalid handoff did not fail closed")
        except MorningPersistenceError:
            pass
        assert not (root / QUEUE_RELATIVE).exists()
        assert not (root / TOPIC_LEDGER_RELATIVE).exists()

    # 3. Replay adds nothing.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        setup(
            root,
            handoff(
                candidate(),
                candidate(candidate_id="cand-2", rank=2, editorial_status="REJECTED",
                          story_eligible=False, verification="PARTIAL"),
            ),
        )
        first = persist_morning_state(workspace_root=root, editorial_date=date)
        assert (first["queue_added"], first["ledger_added"]) == (1, 2), first
        second = persist_morning_state(workspace_root=root, editorial_date=date)
        assert second["queue_added"] == 0, second
        assert second["ledger_added"] == 0, second
        assert second["ledger_skipped_duplicate"] == 2, second
        queue = (root / QUEUE_RELATIVE).read_text(encoding="utf-8")
        assert queue.count(f"## {date} morning-editorial scan") == 1
        assert queue.count("- **candidate_id:** cand-1") == 1

    print("MORNING_PERSISTENCE_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_MODEL_CALLS=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="NullOne deterministic Morning queue/ledger persistence"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
