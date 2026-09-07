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

Historical publish-ledger compatibility (issue #60)
----------------------------------------------------
Production `publish-ledger.jsonl` rows written before this repository
tracked `format` on every row lack that field. This module treats a
ledger row missing (or carrying an unrecognized) `format` as a
compatibility case, not automatic malformation:

- `result` and `timestamp` remain required on every row; a row missing
  either still raises CadenceStateError, exactly as before.
- A row's *effective format* is `format` verbatim when it is already one
  of the known formats (`format_source = "LEDGER"`). Otherwise this
  module attempts deterministic, read-only recovery from authoritative
  linkage already present in loaded manifest state, in strict preference
  order: (1) exact `manifest_id` match, (2) exact `live_zernio_post_id`
  match. Recovery only succeeds when exactly one format is proven; any
  missing, conflicting, or ambiguous (one identifier matching manifests
  of more than one format) evidence leaves the row's format `UNKNOWN`.
  Format is never guessed from title/topic/time/order.
- The on-disk ledger is never rewritten; a recovered format is a
  read-time derived value only, kept alongside (not merged
  indistinguishably into) the original row.
- A row whose format stays `UNKNOWN` is excluded from both the main and
  Story format buckets -- it is never invented into either counter.
- If an `UNKNOWN`-format row could still be *decision-relevant* --
  its `result == PUBLISHED` and (a) it falls on today's Asia/Baku
  calendar date, or (b) it may still fall inside the configured main or
  Story anti-burst spacing window -- then its true format could still
  change `published_today` or `last_published_at` for either bucket.
  `collect_format_loads()` (and therefore `assemble_cadence_request()`,
  which calls it) refuses to guess and instead raises
  CadenceStateError before any counters reach the pure controller, so an
  unresolved-but-possibly-current row can never manufacture a false gap
  and can never be silently treated as zero either.
- An `UNKNOWN`-format `PUBLISHED` row that is provably outside today's
  Baku date *and* outside every currently configured spacing window
  cannot change today's counters or the current spacing decision, so it
  does not block a normal cadence read. It remains visible via
  `analyze_ledger_compatibility()` as a compatibility diagnostic only.
- A non-`PUBLISHED` row (it never participates in the audience-facing
  count/index logic in the first place) never blocks a cadence read
  merely because it predates the `format` field.

`analyze_ledger_compatibility()` exposes a deterministic, non-mutating
audit of this: per-row recovery source and a structured summary
(`native_format_rows` / `recovered_format_rows` / `unknown_format_rows`
/ per-row references). No production ledger migration is performed or
required by this module; read compatibility is sufficient
(`migration_required: False` in that report).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nullone_cadence_controller import DEFAULT_CONFIG as _CONTROLLER_DEFAULT_CONFIG

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

# Evidence hierarchy for read-time historical-row format recovery
# (issue #60). Order matters only for the human-readable `format_source`
# label when a single identifier resolves the row; conflict/ambiguity
# detection considers both regardless of order.
FORMAT_SOURCE_LEDGER = "LEDGER"
FORMAT_SOURCE_MANIFEST_ID = "MANIFEST_ID"
FORMAT_SOURCE_LIVE_ZERNIO_POST_ID = "LIVE_ZERNIO_POST_ID"
FORMAT_SOURCE_UNKNOWN = "UNKNOWN"


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


def _load_ledger_rows(ledger_path: Path) -> list[tuple[int, dict[str, Any]]]:
    """Read and shape-validate the publish ledger.

    Returns `(lineno, row)` pairs so callers/diagnostics can cite an exact
    ledger line without re-reading the file. `format` is intentionally
    NOT required here (issue #60): historical rows predate that field.
    `result` and `timestamp` remain required on every row.
    """

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

    rows: list[tuple[int, dict[str, Any]]] = []

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

        for field in ("result", "timestamp"):
            if field not in obj:
                _fail(f"Publish ledger row missing {field!r} at {context}")

        rows.append((lineno, obj))

    return rows


def _build_manifest_indices(
    manifests: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    manifest_by_id: dict[str, dict[str, Any]] = {}
    formats_by_live_id: dict[str, set[str]] = {}

    for manifest in manifests:
        manifest_by_id[manifest["manifest_id"]] = manifest

        live_id = manifest["publication"].get("live_zernio_post_id")
        if isinstance(live_id, str) and live_id:
            formats_by_live_id.setdefault(live_id, set()).add(manifest["format"])

    return manifest_by_id, formats_by_live_id


def _resolve_missing_format(
    row: dict[str, Any],
    manifest_by_id: dict[str, dict[str, Any]],
    formats_by_live_id: dict[str, set[str]],
) -> tuple[str | None, str]:
    """Attempt deterministic read-time format recovery for one ledger row.

    Preferred evidence order: exact `manifest_id` linkage, then exact
    `live_zernio_post_id` linkage (contract section 5). Recovery succeeds
    only when the evidence resolves to exactly one format; any missing,
    conflicting, or internally ambiguous (one identifier matching more
    than one format across manifests) evidence fails safe to unknown --
    never a heuristic guess.
    """

    manifest_id = row.get("manifest_id")
    live_id = row.get("live_zernio_post_id")

    manifest_id_format: str | None = None
    if isinstance(manifest_id, str) and manifest_id in manifest_by_id:
        manifest_id_format = manifest_by_id[manifest_id]["format"]

    live_id_format: str | None = None
    live_id_ambiguous = False
    if isinstance(live_id, str) and live_id in formats_by_live_id:
        candidates = formats_by_live_id[live_id]
        if len(candidates) == 1:
            live_id_format = next(iter(candidates))
        else:
            # The same live_zernio_post_id is recorded against manifests
            # of more than one format: the identifier itself is
            # ambiguous evidence. Fail safe rather than silently
            # preferring the other identifier.
            live_id_ambiguous = True

    if live_id_ambiguous:
        return None, FORMAT_SOURCE_UNKNOWN

    found = {f for f in (manifest_id_format, live_id_format) if f is not None}

    if not found or len(found) > 1:
        return None, FORMAT_SOURCE_UNKNOWN

    fmt = next(iter(found))
    source = (
        FORMAT_SOURCE_MANIFEST_ID
        if manifest_id_format is not None
        else FORMAT_SOURCE_LIVE_ZERNIO_POST_ID
    )
    return fmt, source


def _effective_row_format(
    row: dict[str, Any],
    manifest_by_id: dict[str, dict[str, Any]],
    formats_by_live_id: dict[str, set[str]],
) -> tuple[str | None, str]:
    native = row.get("format")

    if isinstance(native, str) and native in KNOWN_FORMATS:
        return native, FORMAT_SOURCE_LEDGER

    # Missing, null, non-string, or an unrecognized explicit format
    # string are all treated identically: never guessed, only
    # deterministically recovered from authoritative linkage or left
    # UNKNOWN.
    return _resolve_missing_format(row, manifest_by_id, formats_by_live_id)


def _classify_ledger_rows(
    ledger_rows: list[tuple[int, dict[str, Any]]],
    manifest_by_id: dict[str, dict[str, Any]],
    formats_by_live_id: dict[str, set[str]],
) -> list[dict[str, Any]]:
    classified: list[dict[str, Any]] = []

    for lineno, row in ledger_rows:
        effective_format, source = _effective_row_format(
            row, manifest_by_id, formats_by_live_id
        )
        classified.append(
            {
                "lineno": lineno,
                "row": row,
                "effective_format": effective_format,
                "format_source": source,
            }
        )

    return classified


def _spacing_minutes(config: dict[str, Any] | None) -> tuple[int, int]:
    main = _CONTROLLER_DEFAULT_CONFIG["main_min_spacing_minutes"]
    story = _CONTROLLER_DEFAULT_CONFIG["story_min_spacing_minutes"]

    if config:
        main = config.get("main_min_spacing_minutes", main)
        story = config.get("story_min_spacing_minutes", story)

    return main, story


def _row_is_decision_relevant(
    *,
    row: dict[str, Any],
    now: datetime,
    zone: ZoneInfo,
    today_local: date,
    main_spacing_minutes: int,
    story_spacing_minutes: int,
) -> bool:
    """Issue #60 decision-relevance rule for one UNKNOWN-format row.

    Only a `PUBLISHED` row can be decision-relevant: a non-PUBLISHED
    result never participates in audience-facing count/index logic
    (section 12), so its missing format is compatibility-diagnostic
    only regardless of timing.

    A PUBLISHED row is decision-relevant when its unresolved format
    could still change today's counters or the current anti-burst
    spacing decision for either bucket: it falls on today's Asia/Baku
    calendar date, or it may still fall inside the longer of the
    configured main/Story spacing windows measured from `now`. An old
    row that is provably outside both cannot change either, and is
    left as a compatibility diagnostic only.
    """

    if row.get("result") != "PUBLISHED":
        return False

    ts = _parse_event_timestamp(row["timestamp"], "publish ledger row")

    if ts.astimezone(zone).date() == today_local:
        return True

    max_spacing_seconds = max(main_spacing_minutes, story_spacing_minutes) * 60
    elapsed_seconds = (now - ts).total_seconds()

    # No lower bound on elapsed_seconds: a timestamp that is not clearly
    # older than the spacing window (including any clock-skew edge case
    # putting it apparently in the future) is treated as still possibly
    # relevant rather than assumed safe.
    return elapsed_seconds < max_spacing_seconds


def _fail_unresolved_decision_relevant(entries: list[dict[str, Any]]) -> None:
    details = "; ".join(
        f"line {entry['lineno']} (timestamp={entry['row'].get('timestamp')!r}, "
        f"manifest_id={entry['row'].get('manifest_id')!r}, "
        f"live_zernio_post_id={entry['row'].get('live_zernio_post_id')!r})"
        for entry in entries
    )
    _fail(
        "Publish ledger contains decision-relevant PUBLISHED row(s) with "
        "unresolved format that could still change today's published "
        "count or current anti-burst spacing, and cannot be recovered "
        "from manifest_id/live_zernio_post_id linkage: "
        f"{details}. Refusing to assemble cadence state rather than risk "
        "an unsafe recommendation; this is a compatibility-read "
        "limitation, not a production ledger defect."
    )


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
    classified_rows: list[dict[str, Any]],
    manifests: list[dict[str, Any]],
    published_ids: frozenset[str],
    formats: frozenset[str],
    today_local: date,
    zone: ZoneInfo,
) -> dict[str, Any]:
    published_today = 0
    last_published_at: datetime | None = None

    for item in classified_rows:
        if item["effective_format"] not in formats:
            continue

        row = item["row"]

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


def _read_state(
    *,
    state_root: Path,
    now: datetime,
    timezone_name: str,
) -> tuple[ZoneInfo, date, list[dict[str, Any]], list[tuple[int, dict[str, Any]]]]:
    _require_aware(now)

    if not state_root.exists():
        _fail(f"State root is missing: {state_root}")

    if not state_root.is_dir():
        _fail(f"State root is not a directory: {state_root}")

    zone = _zone(timezone_name)
    today_local = now.astimezone(zone).date()

    manifests = _load_manifests(state_root / MANIFEST_SUBPATH)
    ledger_rows = _load_ledger_rows(state_root / PUBLISH_LEDGER_SUBPATH)

    return zone, today_local, manifests, ledger_rows


def collect_format_loads(
    *,
    state_root: Path,
    now: datetime,
    timezone_name: str = "Asia/Baku",
    config: dict[str, Any] | None = None,
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

    `config` is optional and, if supplied, only its
    `main_min_spacing_minutes` / `story_min_spacing_minutes` keys are
    consulted here (for issue #60 decision-relevance below); it need not
    be a full validated cadence-contract config. Omit it to use the same
    defaults `nullone_cadence_controller.DEFAULT_CONFIG` uses.

    Issue #60: a historical ledger row missing `format` is recovered
    read-time from manifest linkage where possible (see module
    docstring). If an unresolved row could still be decision-relevant --
    PUBLISHED, and either dated today (Asia/Baku) or still inside the
    applicable anti-burst spacing window -- this function raises
    CadenceStateError rather than silently excluding it from a counter
    that could then look artificially low.
    """

    zone, today_local, manifests, ledger_rows = _read_state(
        state_root=state_root, now=now, timezone_name=timezone_name
    )

    manifest_by_id, formats_by_live_id = _build_manifest_indices(manifests)
    classified = _classify_ledger_rows(ledger_rows, manifest_by_id, formats_by_live_id)

    main_spacing, story_spacing = _spacing_minutes(config)

    relevant_unresolved = [
        item
        for item in classified
        if item["effective_format"] is None
        and _row_is_decision_relevant(
            row=item["row"],
            now=now,
            zone=zone,
            today_local=today_local,
            main_spacing_minutes=main_spacing,
            story_spacing_minutes=story_spacing,
        )
    ]

    if relevant_unresolved:
        _fail_unresolved_decision_relevant(relevant_unresolved)

    published_ids = _index_published_ids([row for _, row in ledger_rows])

    main_load = _format_load(
        classified_rows=classified,
        manifests=manifests,
        published_ids=published_ids,
        formats=MAIN_FORMATS,
        today_local=today_local,
        zone=zone,
    )
    story_load = _format_load(
        classified_rows=classified,
        manifests=manifests,
        published_ids=published_ids,
        formats=STORY_FORMATS,
        today_local=today_local,
        zone=zone,
    )

    return {"main_load": main_load, "story_load": story_load}


def analyze_ledger_compatibility(
    *,
    state_root: Path,
    now: datetime,
    timezone_name: str = "Asia/Baku",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only historical-ledger compatibility audit (issue #60).

    Unlike `collect_format_loads()`, this never raises solely because a
    row's format is unresolved -- it exists specifically to surface that
    condition, decision-relevant or not, for audit. It still raises
    CadenceStateError for the same non-negotiable cases
    `collect_format_loads()` does: missing state root, malformed JSON, or
    a row/manifest missing an actually-required field. It performs no
    write of any kind.

    Returns a dict shaped like:

        {
          "status": "COMPATIBLE" | "DEGRADED_UNKNOWN_FORMAT",
          "native_format_rows": int,
          "recovered_format_rows": int,
          "unknown_format_rows": int,
          "recovered_rows": [
              {
                "line": int,
                "manifest_id": str | None,
                "live_zernio_post_id": str | None,
                "timestamp": str,
                "result": str,
                "effective_format": str,
                "format_source": "MANIFEST_ID" | "LIVE_ZERNIO_POST_ID",
              },
              ...
          ],
          "unresolved_rows": [
              {
                "line": int,
                "manifest_id": str | None,
                "live_zernio_post_id": str | None,
                "timestamp": str,
                "result": str,
                "decision_relevant": bool,
              },
              ...
          ],
          "migration_required": False,
        }

    Native (`format_source == "LEDGER"`) rows need no diagnostic entry --
    they behave exactly as before #60 -- so only recovered and unresolved
    rows are listed. No secrets, provider credentials, or full production
    ledger rows are included -- only the line number and the
    identifying/timestamp fields already present in the ledger row.
    """

    zone, today_local, manifests, ledger_rows = _read_state(
        state_root=state_root, now=now, timezone_name=timezone_name
    )

    manifest_by_id, formats_by_live_id = _build_manifest_indices(manifests)
    classified = _classify_ledger_rows(ledger_rows, manifest_by_id, formats_by_live_id)

    main_spacing, story_spacing = _spacing_minutes(config)

    native = 0
    recovered_rows: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []

    for item in classified:
        row = item["row"]

        if item["effective_format"] is not None:
            if item["format_source"] == FORMAT_SOURCE_LEDGER:
                native += 1
            else:
                recovered_rows.append(
                    {
                        "line": item["lineno"],
                        "manifest_id": row.get("manifest_id"),
                        "live_zernio_post_id": row.get("live_zernio_post_id"),
                        "timestamp": row.get("timestamp"),
                        "result": row.get("result"),
                        "effective_format": item["effective_format"],
                        "format_source": item["format_source"],
                    }
                )
            continue

        relevant = _row_is_decision_relevant(
            row=row,
            now=now,
            zone=zone,
            today_local=today_local,
            main_spacing_minutes=main_spacing,
            story_spacing_minutes=story_spacing,
        )
        unresolved_rows.append(
            {
                "line": item["lineno"],
                "manifest_id": row.get("manifest_id"),
                "live_zernio_post_id": row.get("live_zernio_post_id"),
                "timestamp": row.get("timestamp"),
                "result": row.get("result"),
                "decision_relevant": relevant,
            }
        )

    unknown = len(unresolved_rows)

    return {
        "status": "DEGRADED_UNKNOWN_FORMAT" if unknown else "COMPATIBLE",
        "native_format_rows": native,
        "recovered_format_rows": len(recovered_rows),
        "unknown_format_rows": unknown,
        "recovered_rows": recovered_rows,
        "unresolved_rows": unresolved_rows,
        "migration_required": False,
    }


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

    Issue #60: if `collect_format_loads()` determines that an unresolved
    historical ledger row could still be decision-relevant, this raises
    CadenceStateError (propagated from `collect_format_loads()`) before
    ever constructing a request, rather than handing the pure controller
    an apparently-complete state that could produce an unsafe `PREPARE_*`
    recommendation.
    """

    loads = collect_format_loads(
        state_root=state_root, now=now, timezone_name=timezone_name, config=config
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
