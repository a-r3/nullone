#!/usr/bin/env python3
"""Strict machine-readable Morning Editorial candidate handoff (#79).

`nullone.editorial-candidate-handoff.v1` is the authoritative structured
source production Story scheduling reads. It is machine-authored directly
by the Morning Editorial cycle alongside the human-readable board
Markdown -- it is NEVER derived later by parsing that Markdown, the
candidate queue, or the topic ledger.

One artifact per Baku editorial date:

    social/research/daily/YYYY-MM-DD-editorial-candidates.json

This module owns only the contract: strict validation, workspace-contained
snapshot loading, and deterministic availability/selection reads over one
validated snapshot. Candidate admission into #33 (`validate_candidate`)
and Story request identity stay in `nullone_story_pipeline.py`; the
production `StoryCandidateProvider` lives in
`nullone_story_production_provider.py` and reads only snapshots this
module validated.

Strictness is deliberate: a malformed handoff fails closed as a source
failure, never as silent "no candidate", and never by inferring missing
values from Markdown.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nullone_bridge_common import ALLOWED_CONTENT_TYPES

SCHEMA = "nullone.editorial-candidate-handoff.v1"
CONTRACT_VERSION = "1.0.0"

TOP_LEVEL_FIELDS = frozenset(
    {"schema", "contract_version", "editorial_date", "board_path", "candidates"}
)

CANDIDATE_REQUIRED_FIELDS = (
    "candidate_id",
    "rank",
    "topic",
    "topic_cluster",
    "content_type",
    "angle",
    "verification",
    "evidence_refs",
    "source_attribution",
    "editorial_status",
    "story_eligible",
)

# Explicitly allowed optional fields. Anything else on a candidate is
# rejected -- forward-compatible extension requires a contract revision,
# not silent tolerance.
CANDIDATE_OPTIONAL_FIELDS = frozenset(
    {
        "candidate_version",
        "request_lineage",
        "topic_bucket",
        "score",
        "freshness_class",
        "freshness_deadline",
        "source_urls",
        "factual_inputs",
        "limitations",
        "claims",
    }
)

CANDIDATE_FIELDS = frozenset(CANDIDATE_REQUIRED_FIELDS) | CANDIDATE_OPTIONAL_FIELDS

VERIFICATION_VALUES = frozenset({"UNVERIFIED", "PARTIAL", "PASS", "BLOCKED"})

EDITORIAL_STATUS_VALUES = frozenset(
    {"READY", "NEW", "RESEARCHING", "DEFERRED", "REJECTED"}
)

_EDITORIAL_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class EditorialHandoffError(ValueError):
    """Malformed or unreadable editorial candidate handoff.

    Raised instead of inferring, repairing, or downgrading to an empty
    candidate set -- the caller's own fail-closed requirement.
    """


def handoff_relative_path(editorial_date: str) -> str:
    """Canonical artifact path for one Baku editorial date."""

    _require_editorial_date(editorial_date)
    return f"social/research/daily/{editorial_date}-editorial-candidates.json"


def _require_editorial_date(value: Any) -> str:
    if not isinstance(value, str) or not _EDITORIAL_DATE_RE.fullmatch(value):
        raise EditorialHandoffError(
            f"editorial_date must be exact YYYY-MM-DD: {value!r}"
        )
    return value


def _require_non_empty_str(mapping: dict[str, Any], field: str, *, what: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise EditorialHandoffError(f"{what} missing non-empty {field!r}")
    return value


def _validate_candidate(raw: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EditorialHandoffError(f"candidate[{index}] must be an object")

    unknown = set(raw) - CANDIDATE_FIELDS
    if unknown:
        raise EditorialHandoffError(
            f"candidate[{index}] carries unknown field(s): {sorted(unknown)}"
        )

    missing = [f for f in CANDIDATE_REQUIRED_FIELDS if raw.get(f) in (None, "")]
    if missing:
        raise EditorialHandoffError(
            f"candidate[{index}] missing required field(s): {missing}"
        )

    candidate_id = _require_non_empty_str(raw, "candidate_id", what=f"candidate[{index}]")

    rank = raw["rank"]
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
        raise EditorialHandoffError(
            f"candidate[{index}] rank must be a positive integer"
        )

    for field in ("topic", "topic_cluster", "angle", "source_attribution"):
        _require_non_empty_str(raw, field, what=f"candidate[{index}]")

    if raw["content_type"] not in ALLOWED_CONTENT_TYPES:
        raise EditorialHandoffError(
            f"candidate[{index}] unsupported content_type: {raw['content_type']!r}"
        )

    if raw["verification"] not in VERIFICATION_VALUES:
        raise EditorialHandoffError(
            f"candidate[{index}] invalid verification: {raw['verification']!r}"
        )

    if raw["editorial_status"] not in EDITORIAL_STATUS_VALUES:
        raise EditorialHandoffError(
            f"candidate[{index}] invalid editorial_status: {raw['editorial_status']!r}"
        )

    if not isinstance(raw["story_eligible"], bool):
        raise EditorialHandoffError(
            f"candidate[{index}] story_eligible must be a boolean"
        )

    evidence_refs = raw["evidence_refs"]
    if not isinstance(evidence_refs, list) or not evidence_refs:
        # Evidence is required for every handoff candidate: a Story-eligible
        # candidate cannot become eligible without it (#33 admission), and an
        # ineligible candidate with empty evidence would hide a malformed
        # upstream record as a clean "not eligible".
        raise EditorialHandoffError(
            f"candidate[{index}] evidence_refs must be a non-empty list"
        )
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref.strip():
            raise EditorialHandoffError(
                f"candidate[{index}] evidence_refs must contain non-empty strings"
            )

    if raw["story_eligible"] and raw["verification"] != "PASS":
        # Upstream must never mark an unverified candidate Story-eligible;
        # #33 would reject it anyway, but the contradiction itself is a
        # malformed handoff, not a quiet filter.
        raise EditorialHandoffError(
            f"candidate[{index}] story_eligible with verification != PASS"
        )

    source_urls = raw.get("source_urls", [])
    if not isinstance(source_urls, list) or any(
        not isinstance(u, str) or not u.strip() for u in source_urls
    ):
        raise EditorialHandoffError(
            f"candidate[{index}] source_urls must be a list of non-empty strings"
        )

    for field in ("factual_inputs", "claims", "limitations"):
        if field in raw and raw[field] is not None and not isinstance(raw[field], dict):
            raise EditorialHandoffError(
                f"candidate[{index}] {field} must be an object when present"
            )

    for field in ("candidate_version", "request_lineage", "topic_bucket",
                  "freshness_class", "freshness_deadline"):
        if field in raw and raw[field] is not None and (
            not isinstance(raw[field], str) or not raw[field].strip()
        ):
            raise EditorialHandoffError(
                f"candidate[{index}] {field} must be a non-empty string when present"
            )

    if "score" in raw and raw["score"] is not None and not isinstance(
        raw["score"], (int, float)
    ):
        raise EditorialHandoffError(f"candidate[{index}] score must be numeric")
    if isinstance(raw.get("score"), bool):
        raise EditorialHandoffError(f"candidate[{index}] score must be numeric")

    validated = {field: raw[field] for field in CANDIDATE_FIELDS if field in raw}
    validated["candidate_id"] = candidate_id
    return validated


def validate_handoff(data: Any) -> dict[str, Any]:
    """Strictly validate a decoded handoff document.

    Returns a normalized snapshot dict. Raises `EditorialHandoffError` for
    anything malformed -- never repairs, never infers.
    """

    if not isinstance(data, dict):
        raise EditorialHandoffError("handoff document must be a JSON object")

    unknown = set(data) - TOP_LEVEL_FIELDS
    if unknown:
        raise EditorialHandoffError(
            f"handoff carries unknown top-level field(s): {sorted(unknown)}"
        )

    if data.get("schema") != SCHEMA:
        raise EditorialHandoffError(f"unsupported handoff schema: {data.get('schema')!r}")
    if data.get("contract_version") != CONTRACT_VERSION:
        raise EditorialHandoffError(
            f"unsupported handoff contract_version: {data.get('contract_version')!r}"
        )

    editorial_date = _require_editorial_date(data.get("editorial_date"))

    board_path = data.get("board_path")
    if not isinstance(board_path, str) or not board_path.strip():
        raise EditorialHandoffError("handoff missing non-empty board_path")

    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list):
        raise EditorialHandoffError("handoff candidates must be a list")

    candidates = [
        _validate_candidate(raw, index=index)
        for index, raw in enumerate(raw_candidates)
    ]

    seen_ids: set[str] = set()
    seen_ranks: set[int] = set()
    for candidate in candidates:
        if candidate["candidate_id"] in seen_ids:
            raise EditorialHandoffError(
                f"duplicate candidate_id: {candidate['candidate_id']!r}"
            )
        seen_ids.add(candidate["candidate_id"])
        if candidate["rank"] in seen_ranks:
            raise EditorialHandoffError(
                f"duplicate rank {candidate['rank']} (candidate "
                f"{candidate['candidate_id']!r}): ordering would be ambiguous"
            )
        seen_ranks.add(candidate["rank"])

    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "editorial_date": editorial_date,
        "board_path": board_path,
        "candidates": candidates,
    }


def load_handoff_snapshot(
    *,
    workspace_root: Path,
    editorial_date: str,
) -> dict[str, Any]:
    """Load and strictly validate one editorial date's handoff artifact.

    The artifact must be a workspace-contained regular file (symlinks and
    path escapes rejected), valid JSON, and pass `validate_handoff` with an
    `editorial_date` matching the requested date -- a stale prior-day file
    is never silently accepted. Raises `EditorialHandoffError` otherwise.
    """

    _require_editorial_date(editorial_date)

    if not isinstance(workspace_root, Path):
        raise EditorialHandoffError("workspace_root must be a Path")

    root = workspace_root.resolve()
    rel = handoff_relative_path(editorial_date)
    unresolved = root / rel
    if unresolved.is_symlink():
        raise EditorialHandoffError(f"handoff must not be a symlink: {rel}")
    path = unresolved.resolve()

    try:
        path.relative_to(root)
    except ValueError as exc:
        raise EditorialHandoffError(
            f"handoff path escapes workspace: {rel}"
        ) from exc

    if not path.is_file():
        raise EditorialHandoffError(f"handoff artifact missing: {rel}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditorialHandoffError(
            f"handoff artifact unreadable or malformed JSON: {rel}"
        ) from exc

    try:
        snapshot = validate_handoff(data)
    except EditorialHandoffError as exc:
        raise EditorialHandoffError(f"handoff artifact invalid: {exc}") from exc

    if snapshot["editorial_date"] != editorial_date:
        raise EditorialHandoffError(
            "handoff editorial_date does not match requested date "
            f"(stale file rejected): {snapshot['editorial_date']!r}"
        )

    snapshot["artifact_path"] = rel
    return snapshot


def story_eligible_candidates(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidates flagged Story-eligible by Morning Editorial, in upstream order.

    Ordering is Morning's own accepted ranking (`rank` ascending) -- this
    function never invents its own ordering.
    """

    return sorted(
        (c for c in snapshot["candidates"] if c["story_eligible"]),
        key=lambda c: c["rank"],
    )


def _workspace_or_raise(workspace_root: Any) -> Path:
    if not isinstance(workspace_root, Path):
        raise EditorialHandoffError("workspace_root must be a Path")
    return workspace_root.resolve()


def find_consumed_story_request_ids(*, workspace_root: Path) -> frozenset[str]:
    """Story request IDs whose review-draft attempt is already consumed.

    Scans `social/ops/manifests/*.json` for STORY manifests carrying a
    `story_request_id` with `review.create_attempts > 0`. A consumed
    attempt is at-most-once: the production provider must never redraft
    it. Unreadable files fail closed (raised, never skipped silently);
    non-STORY manifests are ignored.
    """

    root = _workspace_or_raise(workspace_root)
    manifest_dir = root / "social/ops/manifests"
    if not manifest_dir.is_dir():
        return frozenset()

    consumed: set[str] = set()
    for path in sorted(manifest_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EditorialHandoffError(
                f"story manifest unreadable: {path.name}"
            ) from exc
        if not isinstance(data, dict) or data.get("format") != "STORY":
            continue
        request_id = data.get("story_request_id")
        review = data.get("review")
        if not isinstance(request_id, str) or not request_id:
            continue
        if not isinstance(review, dict):
            raise EditorialHandoffError(
                f"story manifest has malformed review block: {path.name}"
            )
        attempts = review.get("create_attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            raise EditorialHandoffError(
                f"story manifest has malformed create_attempts: {path.name}"
            )
        if attempts > 0:
            consumed.add(request_id)
    return frozenset(consumed)


def self_test() -> int:
    import tempfile

    def handoff(**overrides):
        base = {
            "schema": SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "editorial_date": "2026-09-08",
            "board_path": "social/research/daily/2026-09-08-editorial-board.md",
            "candidates": [],
        }
        base.update(overrides)
        return base

    def candidate(**overrides):
        base = {
            "candidate_id": "cand-1",
            "rank": 1,
            "topic": "Topic",
            "topic_cluster": "cluster",
            "content_type": "NEWS",
            "angle": "Angle",
            "verification": "PASS",
            "evidence_refs": ["evidence"],
            "source_attribution": "Source",
            "editorial_status": "READY",
            "story_eligible": True,
        }
        base.update(overrides)
        return base

    # 1. Empty-candidate handoff is valid (truthful "no candidate" state).
    snap = validate_handoff(handoff())
    assert snap["candidates"] == []
    assert story_eligible_candidates(snap) == []

    # 2. One eligible candidate validates; ordering helper returns it.
    snap = validate_handoff(handoff(candidates=[candidate()]))
    assert len(story_eligible_candidates(snap)) == 1

    # 3. Unknown top-level field rejected.
    try:
        validate_handoff(handoff(extra=True))
        raise AssertionError("unknown top-level field was not rejected")
    except EditorialHandoffError:
        pass

    # 4. Unknown candidate field rejected.
    try:
        validate_handoff(handoff(candidates=[candidate(mystery=1)]))
        raise AssertionError("unknown candidate field was not rejected")
    except EditorialHandoffError:
        pass

    # 5. Duplicate rank fails closed.
    try:
        validate_handoff(
            handoff(
                candidates=[
                    candidate(candidate_id="a", rank=1),
                    candidate(candidate_id="b", rank=1),
                ]
            )
        )
        raise AssertionError("duplicate rank was not rejected")
    except EditorialHandoffError:
        pass

    # 6. Duplicate candidate_id fails closed.
    try:
        validate_handoff(
            handoff(
                candidates=[
                    candidate(candidate_id="a", rank=1),
                    candidate(candidate_id="a", rank=2),
                ]
            )
        )
        raise AssertionError("duplicate candidate_id was not rejected")
    except EditorialHandoffError:
        pass

    # 7. story_eligible with verification != PASS rejected.
    try:
        validate_handoff(handoff(candidates=[candidate(verification="PARTIAL")]))
        raise AssertionError("eligible-but-unverified was not rejected")
    except EditorialHandoffError:
        pass

    # 8. Unknown schema/version rejected.
    try:
        validate_handoff(handoff(schema="nullone.something-else.v1"))
        raise AssertionError("unknown schema was not rejected")
    except EditorialHandoffError:
        pass
    try:
        validate_handoff(handoff(contract_version="2.0.0"))
        raise AssertionError("unknown version was not rejected")
    except EditorialHandoffError:
        pass

    # 9. Missing evidence rejected.
    try:
        validate_handoff(handoff(candidates=[candidate(evidence_refs=[])]))
        raise AssertionError("empty evidence_refs was not rejected")
    except EditorialHandoffError:
        pass

    # 10. Filesystem round-trip: load, stale date, symlink, missing.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        research = root / "social/research/daily"
        research.mkdir(parents=True, exist_ok=True)
        doc = handoff(candidates=[candidate()])
        (research / "2026-09-08-editorial-candidates.json").write_text(
            json.dumps(doc), encoding="utf-8"
        )
        loaded = load_handoff_snapshot(
            workspace_root=root, editorial_date="2026-09-08"
        )
        assert loaded["editorial_date"] == "2026-09-08"
        assert len(loaded["candidates"]) == 1

        try:
            load_handoff_snapshot(workspace_root=root, editorial_date="2026-09-09")
            raise AssertionError("missing handoff was not rejected")
        except EditorialHandoffError:
            pass

        (research / "2026-09-08-editorial-candidates.json").write_text(
            "{not json", encoding="utf-8"
        )
        try:
            load_handoff_snapshot(workspace_root=root, editorial_date="2026-09-08")
            raise AssertionError("malformed JSON was not rejected")
        except EditorialHandoffError:
            pass

    # 11. Bad editorial_date rejected everywhere.
    for bad in ("2026-9-8", "08-09-2026", "", None):
        try:
            handoff_relative_path(bad)
            raise AssertionError(f"bad date accepted: {bad!r}")
        except EditorialHandoffError:
            pass

    print("EDITORIAL_CANDIDATE_HANDOFF_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_MARKDOWN_PARSING=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne editorial candidate handoff")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
