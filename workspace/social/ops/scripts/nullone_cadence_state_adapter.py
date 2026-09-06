#!/usr/bin/env python3
"""Read-only state adapter for the #32 cadence controller.

Collects the `main_load` / `story_load` counters that
nullone_cadence_controller.evaluate_cadence() consumes, from the existing
authoritative repository state formats:

- workspace/social/ops/manifests/*.json (Production Bridge manifests) --
  the only source for "pending"/"consequential" per-candidate state, per
  docs/contracts/cadence-contract-v1.md "State-source precedence".
- workspace/social/state/publish-ledger.jsonl -- the only source for
  audience-facing publication counts and last-audience-facing-publication
  time, per the same contract section.

`social/state/candidate-queue.md` and `social/state/topic-ledger.jsonl`
are deliberately NOT read here: the accepted contract places them at the
lowest precedence and states explicitly that they are "never a source for
pending-load counts" -- they exist for candidate-search dedup context
(issue #33), not for this controller's accounting.

This module never writes anything. It has no capability to create
drafts, mutate manifests/ledgers, publish, or call any connector.

Safety principle (contract section 14): missing/corrupt required state
must not silently read as "zero posts, zero pending". An explicitly
initialized empty store (an existing, empty manifest directory; an
absent-but-otherwise-valid ledger file with no publications yet) may
legitimately be empty. A missing state root, or a present-but-malformed
file, raises CadenceStateError instead of returning a default.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MANIFEST_SUBPATH = Path("ops/manifests")
PUBLISH_LEDGER_SUBPATH = Path("state/publish-ledger.jsonl")

MAIN_FORMATS = frozenset({"FEED", "CAROUSEL"})
STORY_FORMATS = frozenset({"STORY"})
KNOWN_FORMATS = MAIN_FORMATS | STORY_FORMATS

# Per the accepted format-accounting table: consequential/unsafe-to-repeat
# publication states that count as "pending" and must never be treated as
# an empty slot, UNKNOWN included.
PENDING_PUBLICATION_STATES = frozenset(
    {
        "PUBLISH_IN_FLIGHT",
        "PUBLISH_ACCEPTED",
        "PUBLISHING",
        "CHECK_REQUIRED",
        "UNKNOWN",
        "READBACK_FAILED",
    }
)


class CadenceStateError(RuntimeError):
    """Required repository state is missing, unreadable, or malformed."""


def _fail(message: str) -> None:
    raise CadenceStateError(message)


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        _fail("now must be a timezone-aware datetime")


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        _fail(f"Unknown timezone: {timezone_name!r}: {exc}")
        raise  # unreachable


def _parse_event_timestamp(value: Any, context: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{context}: timestamp is missing or empty")

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        _fail(f"{context}: unparsable timestamp {value!r}: {exc}")
        raise  # unreachable

    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        _fail(f"{context}: timestamp must be timezone-aware: {value!r}")

    return parsed


def _load_manifests(manifest_dir: Path) -> list[dict[str, Any]]:
    if not manifest_dir.exists():
        # No manifest directory yet is a legitimate "no candidates have
        # ever entered review" initial state, not a corruption signal.
        return []

    if not manifest_dir.is_dir():
        _fail(f"Manifest path exists but is not a directory: {manifest_dir}")

    manifests: list[dict[str, Any]] = []

    for path in sorted(manifest_dir.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            _fail(f"Cannot read manifest file: {path}: {exc}")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _fail(f"Malformed manifest JSON: {path}: {exc}")

        if not isinstance(data, dict):
            _fail(f"Manifest is not a JSON object: {path}")

        _require_manifest_shape(data, path)
        manifests.append(data)

    return manifests


def _require_manifest_shape(data: dict[str, Any], path: Path) -> None:
    if not isinstance(data.get("manifest_id"), str) or not data["manifest_id"].strip():
        _fail(f"Manifest missing manifest_id: {path}")

    if data.get("format") not in KNOWN_FORMATS:
        _fail(f"Manifest missing/invalid format: {path}")

    review = data.get("review")
    if not isinstance(review, dict) or "state" not in review:
        _fail(f"Manifest missing review.state: {path}")

    approval = data.get("approval")
    if (
        not isinstance(approval, dict)
        or "first_stage" not in approval
        or "final_publish" not in approval
    ):
        _fail(f"Manifest missing approval.first_stage/final_publish: {path}")

    publication = data.get("publication")
    if not isinstance(publication, dict) or "state" not in publication:
        _fail(f"Manifest missing publication.state: {path}")


def _load_ledger_rows(ledger_path: Path) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        # No publications recorded yet is legitimate for a fresh state
        # substrate; the parent directory presence is checked separately.
        return []

    if not ledger_path.is_file():
        _fail(f"Publish ledger path exists but is not a file: {ledger_path}")

    try:
        text = ledger_path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"Cannot read publish ledger: {ledger_path}: {exc}")

    rows: list[dict[str, Any]] = []

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        if not line:
            continue

        context = f"{ledger_path}:{lineno}"

        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            _fail(f"Malformed publish ledger line at {context}: {exc}")

        if not isinstance(obj, dict):
            _fail(f"Publish ledger row is not a JSON object at {context}")

        for field in ("result", "format", "timestamp"):
            if field not in obj:
                _fail(f"Publish ledger row missing {field!r} at {context}")

        rows.append(obj)

    return rows


def _index_published_ids(ledger_rows: list[dict[str, Any]]) -> frozenset[str]:
    ids: set[str] = set()

    for row in ledger_rows:
        if row.get("result") != "PUBLISHED":
            continue

        for key in ("manifest_id", "live_zernio_post_id"):
            value = row.get(key)
            if value:
                ids.add(str(value))

    return frozenset(ids)


def _is_manifest_pending(manifest: dict[str, Any]) -> bool:
    pub_state = manifest["publication"]["state"]

    if pub_state == "FAILED":
        # Definitive, non-ambiguous failure: never reached the audience
        # and never occupied a slot. Counts as neither published nor
        # pending.
        return False

    review_state = manifest["review"]["state"]
    first_stage = bool(manifest["approval"]["first_stage"])
    final_publish = bool(manifest["approval"]["final_publish"])

    return (
        (review_state == "DRAFT_CREATED" and pub_state == "NOT_REQUESTED")
        or (first_stage and not final_publish)
        or pub_state in PENDING_PUBLICATION_STATES
    )


def _format_load(
    *,
    manifests: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
    published_ids: frozenset[str],
    formats: frozenset[str],
    today_local: date,
    zone: ZoneInfo,
) -> dict[str, Any]:
    published_today = 0
    last_published_at: datetime | None = None

    for row in ledger_rows:
        if row.get("format") not in formats:
            continue

        if row.get("result") != "PUBLISHED":
            continue

        ts = _parse_event_timestamp(row["timestamp"], "publish ledger row")

        if last_published_at is None or ts > last_published_at:
            last_published_at = ts

        if ts.astimezone(zone).date() == today_local:
            published_today += 1

    pending = 0

    for manifest in manifests:
        if manifest["format"] not in formats:
            continue

        manifest_id = manifest["manifest_id"]
        live_id = manifest["publication"].get("live_zernio_post_id")

        # Precedence: the append-only publish ledger is authoritative for
        # audience-facing outcome. A manifest that is locally stale (e.g.
        # still showing PUBLISH_ACCEPTED) but already has a confirmed
        # PUBLISHED ledger row for the same candidate must not also be
        # counted as pending -- that would double-count one piece of
        # content across both counters.
        if manifest_id in published_ids or (live_id and live_id in published_ids):
            continue

        if _is_manifest_pending(manifest):
            pending += 1

    return {
        "published_today": published_today,
        "pending": pending,
        "last_published_at": (
            last_published_at.isoformat() if last_published_at is not None else None
        ),
    }


def collect_format_loads(
    *,
    state_root: Path,
    now: datetime,
    timezone_name: str = "Asia/Baku",
) -> dict[str, dict[str, Any]]:
    """Read authoritative repository state and return contract-shaped loads.

    `state_root` is the directory that contains `ops/manifests/` and
    `state/publish-ledger.jsonl` (i.e. the repository's
    `workspace/social` directory, or an equivalent temp fixture root in
    tests). Returns
    `{"main_load": {...}, "story_load": {...}}` matching the
    `nullone.cadence-contract.v1` input shape for those two fields.

    Raises CadenceStateError if the state root itself is missing (not
    merely empty) or if any manifest/ledger content present is malformed.
    Never returns a silently-defaulted zero for genuinely missing
    required state.
    """

    _require_aware(now)

    if not state_root.exists():
        _fail(f"State root is missing: {state_root}")

    if not state_root.is_dir():
        _fail(f"State root is not a directory: {state_root}")

    zone = _zone(timezone_name)
    today_local = now.astimezone(zone).date()

    manifests = _load_manifests(state_root / MANIFEST_SUBPATH)
    ledger_rows = _load_ledger_rows(state_root / PUBLISH_LEDGER_SUBPATH)
    published_ids = _index_published_ids(ledger_rows)

    main_load = _format_load(
        manifests=manifests,
        ledger_rows=ledger_rows,
        published_ids=published_ids,
        formats=MAIN_FORMATS,
        today_local=today_local,
        zone=zone,
    )
    story_load = _format_load(
        manifests=manifests,
        ledger_rows=ledger_rows,
        published_ids=published_ids,
        formats=STORY_FORMATS,
        today_local=today_local,
        zone=zone,
    )

    return {"main_load": main_load, "story_load": story_load}


def assemble_cadence_request(
    *,
    state_root: Path,
    now: datetime,
    candidate_availability: dict[str, bool],
    config: dict[str, Any] | None = None,
    signal: dict[str, Any] | None = None,
    timezone_name: str = "Asia/Baku",
) -> dict[str, Any]:
    """Assemble a full nullone.cadence-contract.v1 request dict.

    Convenience glue for a future caller (issue #37) that wants to go
    straight from repository state to a controller request without
    hand-assembling the load counters. Performs no evaluation itself --
    pass the result to nullone_cadence_controller.evaluate_cadence().
    """

    loads = collect_format_loads(
        state_root=state_root, now=now, timezone_name=timezone_name
    )

    resolved_signal = {"breaking_day": False, "downtime_marker": None}
    if signal:
        resolved_signal.update(signal)

    return {
        "schema": "nullone.cadence-contract.v1",
        "now": now.isoformat(),
        "timezone": timezone_name,
        "config": config or {},
        "main_load": loads["main_load"],
        "story_load": loads["story_load"],
        "candidate_availability": dict(candidate_availability),
        "signal": resolved_signal,
    }
