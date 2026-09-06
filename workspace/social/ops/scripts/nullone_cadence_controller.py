#!/usr/bin/env python3
"""Deterministic cadence decision function for issue #32.

Implements the accepted contract in docs/contracts/cadence-contract-v1.md
(issue #31) exactly: given authoritative per-format load counters, an
explicit evaluation time, and upstream candidate-quality signals, return
one typed recommendation (NO_ACTION / PREPARE_STORY /
PREPARE_MAIN_CANDIDATE) with its reason code.

This module is a pure function over its input dict. It performs no I/O,
no network access, no LLM calls, and has no capability to create drafts,
send notifications, or publish anything. `PREPARE_*` is permission to
search for and prepare a candidate; it is never publication authorization
(see the contract's "Human approval" section).

Reading real repository/production state into the shape this module
consumes is the job of nullone_cadence_state_adapter.py, not this module.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "nullone.cadence-contract.v1"
CONTRACT_VERSION = "1.0.0"

# The contract is scoped to Asia/Baku specifically (not a general
# multi-timezone engine); IANA ZoneInfo data is used rather than a
# hard-coded UTC+4 offset so a future DST change cannot silently corrupt
# day-boundary/daypart math.
TIMEZONE_NAME = "Asia/Baku"

MAIN_FORMATS = ("FEED", "CAROUSEL")
STORY_FORMATS = ("STORY",)

RECOMMENDATIONS = frozenset(
    {
        "NO_ACTION",
        "PREPARE_STORY",
        "PREPARE_MAIN_CANDIDATE",
    }
)

REASON_CODES = frozenset(
    {
        "MAIN_GAP",
        "STORY_GAP",
        "NO_QUALITY_CANDIDATE",
        "PENDING_MAIN_EXISTS",
        "PENDING_STORY_EXISTS",
        "RECENT_AUDIENCE_ACTIVITY",
        "COALESCED_AFTER_DOWNTIME",
        "QUIET_HOURS",
        "TARGETS_MET",
    }
)

DAYPARTS = ("QUIET", "MORNING", "AFTERNOON", "EVENING")

REASON_TEXT = {
    "MAIN_GAP": (
        "Main load is below today's guidance and a verified candidate "
        "is available."
    ),
    "STORY_GAP": (
        "Story load is below today's guidance and a verified candidate "
        "is available."
    ),
    "NO_QUALITY_CANDIDATE": (
        "A gap exists but no candidate currently passes quality/"
        "verification for this format."
    ),
    "PENDING_MAIN_EXISTS": (
        "Main gap is already being addressed by pending/approved/"
        "in-flight work."
    ),
    "PENDING_STORY_EXISTS": (
        "Story gap is already being addressed by pending/approved/"
        "in-flight work."
    ),
    "RECENT_AUDIENCE_ACTIVITY": (
        "Anti-burst spacing has not yet elapsed since the last "
        "publication of this format."
    ),
    "COALESCED_AFTER_DOWNTIME": (
        "Neither format has a gap; a downtime marker is present and no "
        "historical slot was replayed."
    ),
    "QUIET_HOURS": (
        "Current daypart is quiet hours and quiet-hour suppression is "
        "enabled."
    ),
    "TARGETS_MET": "Neither counter has a gap against today's guidance targets.",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "main_target_min": 2,
    "main_target_max_breaking": 3,
    "story_target_min": 3,
    "story_target_max_breaking": 6,
    "main_min_spacing_minutes": 120,
    "story_min_spacing_minutes": 45,
    "quiet_hours_enabled": True,
    "daypart_boundaries": {
        "quiet_end": "07:00",
        "morning_end": "13:00",
        "afternoon_end": "19:00",
    },
}

_NON_NEGATIVE_INT_CONFIG_KEYS = (
    "main_target_min",
    "main_target_max_breaking",
    "story_target_min",
    "story_target_max_breaking",
    "main_min_spacing_minutes",
    "story_min_spacing_minutes",
)

_DAYPART_BOUNDARY_KEYS = ("quiet_end", "morning_end", "afternoon_end")


class CadenceContractError(ValueError):
    """Malformed or unsupported cadence-contract.v1 input.

    Raised instead of silently falling back to a default/host-local
    interpretation, per the contract's fail-safe requirement for
    invalid/naive/unsupported inputs.
    """


def _fail(message: str) -> None:
    raise CadenceContractError(message)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if not _is_int_not_bool(value) or value < 0:
        _fail(f"{field} must be a non-negative integer")
    return value


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} is required and must be a non-empty string")

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None

    if parsed is None:
        _fail(f"{field} is not a valid ISO-8601 timestamp: {value!r}")

    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        _fail(
            f"{field} must be timezone-aware (an explicit UTC offset is "
            f"required); got naive timestamp: {value!r}"
        )

    return parsed


def _parse_optional_timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value, field)


def _parse_time_of_day(value: Any, field: str):
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty 'HH:MM' string")

    parts = value.split(":")

    if len(parts) != 2:
        _fail(f"{field} must be in 'HH:MM' form: {value!r}")

    hour_text, minute_text = parts

    if not (hour_text.isdigit() and minute_text.isdigit()):
        _fail(f"{field} must be in 'HH:MM' form: {value!r}")

    hour, minute = int(hour_text), int(minute_text)

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        _fail(f"{field} is out of range: {value!r}")

    from datetime import time as _time

    return _time(hour, minute)


def _merge_config(user_config: Any) -> dict[str, Any]:
    user_config = _require_dict(user_config, "config") if user_config else {}

    merged = copy.deepcopy(DEFAULT_CONFIG)

    for key, value in user_config.items():
        if key not in DEFAULT_CONFIG:
            _fail(f"Unknown config field: {key!r}")

        if key == "daypart_boundaries":
            boundaries = _require_dict(value, "config.daypart_boundaries")

            for bkey, bvalue in boundaries.items():
                if bkey not in DEFAULT_CONFIG["daypart_boundaries"]:
                    _fail(f"Unknown config.daypart_boundaries field: {bkey!r}")
                merged["daypart_boundaries"][bkey] = bvalue
        else:
            merged[key] = value

    _validate_config(merged)
    return merged


def _validate_config(config: dict[str, Any]) -> None:
    for key in _NON_NEGATIVE_INT_CONFIG_KEYS:
        _non_negative_int(config[key], f"config.{key}")

    if not _is_bool(config["quiet_hours_enabled"]):
        _fail("config.quiet_hours_enabled must be a boolean")

    boundaries = _require_dict(
        config["daypart_boundaries"], "config.daypart_boundaries"
    )

    parsed = {
        key: _parse_time_of_day(
            boundaries.get(key), f"config.daypart_boundaries.{key}"
        )
        for key in _DAYPART_BOUNDARY_KEYS
    }

    if not (
        parsed["quiet_end"] < parsed["morning_end"] < parsed["afternoon_end"]
    ):
        _fail(
            "config.daypart_boundaries must be strictly increasing: "
            "quiet_end < morning_end < afternoon_end"
        )


def _zone() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - environment issue
        _fail(f"IANA timezone data unavailable for {TIMEZONE_NAME!r}: {exc}")
        raise  # unreachable, satisfies type checkers


def _compute_daypart(now: datetime, boundaries: dict[str, Any]) -> str:
    local = now.astimezone(_zone())
    time_of_day = local.timetz().replace(tzinfo=None)

    quiet_end = _parse_time_of_day(
        boundaries["quiet_end"], "config.daypart_boundaries.quiet_end"
    )
    morning_end = _parse_time_of_day(
        boundaries["morning_end"], "config.daypart_boundaries.morning_end"
    )
    afternoon_end = _parse_time_of_day(
        boundaries["afternoon_end"], "config.daypart_boundaries.afternoon_end"
    )

    if time_of_day < quiet_end:
        return "QUIET"
    if time_of_day < morning_end:
        return "MORNING"
    if time_of_day < afternoon_end:
        return "AFTERNOON"
    return "EVENING"


def _recently_active(
    now: datetime,
    last_published_at: datetime | None,
    spacing_minutes: int,
) -> bool:
    if last_published_at is None:
        return False

    elapsed_seconds = (now - last_published_at).total_seconds()
    return elapsed_seconds < spacing_minutes * 60


def _format_counters(load: dict[str, Any], target_min: int, field: str) -> dict[str, Any]:
    load = _require_dict(load, field)

    for key in ("published_today", "pending", "last_published_at"):
        if key not in load:
            _fail(f"{field}.{key} is required")

    published_today = _non_negative_int(
        load["published_today"], f"{field}.published_today"
    )
    pending = _non_negative_int(load["pending"], f"{field}.pending")

    effective_load = published_today + pending

    return {
        "published_today": published_today,
        "pending": pending,
        "effective_load": effective_load,
        "gap": effective_load < target_min,
    }


def _validate_top_level(request: Any) -> dict[str, Any]:
    request = _require_dict(request, "request")

    if request.get("schema") != SCHEMA:
        _fail(f"Unsupported schema: {request.get('schema')!r}")

    for field in (
        "now",
        "timezone",
        "main_load",
        "story_load",
        "candidate_availability",
        "signal",
    ):
        if field not in request:
            _fail(f"Missing required field: {field!r}")

    if request["timezone"] != TIMEZONE_NAME:
        _fail(
            "timezone must be "
            f"{TIMEZONE_NAME!r} for this contract, got "
            f"{request['timezone']!r}"
        )

    cav = _require_dict(
        request["candidate_availability"], "candidate_availability"
    )

    for field in (
        "main_quality_candidate_available",
        "story_quality_candidate_available",
    ):
        if not _is_bool(cav.get(field)):
            _fail(f"candidate_availability.{field} must be a boolean")

    signal = _require_dict(request["signal"], "signal")

    if "downtime_marker" not in signal:
        _fail("signal.downtime_marker is required (use null when absent)")

    marker = signal["downtime_marker"]

    if marker is not None and not isinstance(marker, dict):
        _fail("signal.downtime_marker must be an object or null")

    if "breaking_day" in signal and not _is_bool(signal["breaking_day"]):
        _fail("signal.breaking_day must be a boolean")

    return request


def _select_no_action_reason(
    *,
    main_counters: dict[str, Any],
    story_counters: dict[str, Any],
    main_quality_available: bool,
    story_quality_available: bool,
    main_recent: bool,
    story_recent: bool,
    quiet: bool,
    downtime_marker: dict[str, Any] | None,
) -> str:
    formats = (
        (main_counters, main_quality_available, main_recent, "PENDING_MAIN_EXISTS"),
        (story_counters, story_quality_available, story_recent, "PENDING_STORY_EXISTS"),
    )

    for counters, quality_available, recent, pending_reason in formats:
        if not counters["gap"]:
            continue

        if counters["pending"] > 0:
            return pending_reason

        if not quality_available:
            return "NO_QUALITY_CANDIDATE"

        if recent:
            return "RECENT_AUDIENCE_ACTIVITY"

        if quiet:
            return "QUIET_HOURS"

        # Unreachable: any format with a gap, no pending work, an
        # available quality candidate, no recent activity and no quiet
        # hours is eligible and would already have produced a PREPARE_*
        # recommendation before this function is called.
        _fail(
            "internal contract inconsistency: gapped, unblocked format "
            "reached NO_ACTION reason selection"
        )

    if downtime_marker is not None:
        return "COALESCED_AFTER_DOWNTIME"

    return "TARGETS_MET"


def evaluate_cadence(request: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one nullone.cadence-contract.v1 request.

    Pure function: does not mutate `request`, performs no I/O, and has
    no side effects. Returns a nullone.cadence-contract.v1 response dict.

    Raises CadenceContractError on malformed/unsupported input rather
    than silently defaulting (e.g. to host-local time).
    """

    request = _validate_top_level(request)

    config = _merge_config(request.get("config"))

    now = _parse_timestamp(request["now"], "now")
    daypart = _compute_daypart(now, config["daypart_boundaries"])

    main_counters = _format_counters(
        request["main_load"], config["main_target_min"], "main_load"
    )
    story_counters = _format_counters(
        request["story_load"], config["story_target_min"], "story_load"
    )

    cav = request["candidate_availability"]
    main_quality_available = bool(cav["main_quality_candidate_available"])
    story_quality_available = bool(cav["story_quality_candidate_available"])

    main_last_published = _parse_optional_timestamp(
        request["main_load"].get("last_published_at"),
        "main_load.last_published_at",
    )
    story_last_published = _parse_optional_timestamp(
        request["story_load"].get("last_published_at"),
        "story_load.last_published_at",
    )

    main_recent = _recently_active(
        now, main_last_published, config["main_min_spacing_minutes"]
    )
    story_recent = _recently_active(
        now, story_last_published, config["story_min_spacing_minutes"]
    )

    quiet = bool(config["quiet_hours_enabled"]) and daypart == "QUIET"

    main_eligible = (
        main_counters["gap"]
        and main_counters["pending"] == 0
        and main_quality_available
        and not main_recent
        and not quiet
    )
    story_eligible = (
        story_counters["gap"]
        and story_counters["pending"] == 0
        and story_quality_available
        and not story_recent
        and not quiet
    )

    downtime_marker = request["signal"]["downtime_marker"]

    if main_eligible:
        recommendation, reason_code = "PREPARE_MAIN_CANDIDATE", "MAIN_GAP"
    elif story_eligible:
        recommendation, reason_code = "PREPARE_STORY", "STORY_GAP"
    else:
        recommendation = "NO_ACTION"
        reason_code = _select_no_action_reason(
            main_counters=main_counters,
            story_counters=story_counters,
            main_quality_available=main_quality_available,
            story_quality_available=story_quality_available,
            main_recent=main_recent,
            story_recent=story_recent,
            quiet=quiet,
            downtime_marker=downtime_marker,
        )

    permitted_action = (
        "NONE" if recommendation == "NO_ACTION" else "CANDIDATE_SEARCH_AND_PREPARE"
    )

    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "recommendation": recommendation,
        "reason_code": reason_code,
        "reason_text": REASON_TEXT[reason_code],
        "permitted_action": permitted_action,
        "daypart": daypart,
        "counters": {
            "main": main_counters,
            "story": story_counters,
        },
        "context": {
            "evaluated_at": now.isoformat(),
            "downtime_coalesced": reason_code == "COALESCED_AFTER_DOWNTIME",
            "downtime_marker": downtime_marker,
        },
    }
