#!/usr/bin/env python3
"""Shared pure helpers for the #59 scheduled application workflows.

Used by both `nullone_morning_workflow.py` and `nullone_analytics_workflow.py`
so the two safety-relevant behaviors every scheduled workflow needs --
deriving a business date from `scheduled_for` in Asia/Baku (never by slicing
the opaque hash-like `occurrence_id`), and proving an in-memory runtime
return agrees with the exact persisted #27 record -- are implemented once,
not duplicated and risk drifting apart.

Pure value module: no I/O, no subprocess, no network, no provider/transport
knowledge of any kind.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

_CANONICAL_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The exact #27 fields an in-memory runtime return must agree with the
# persisted record on. Full object equality is not required: some runtimes
# may legitimately return an equivalent-but-not-byte-identical mapping
# (e.g. key ordering); these four fields are what #27/#59 identity and
# domain-health decisions actually key off.
RECONCILIATION_FIELDS: tuple[str, ...] = (
    "run_id",
    "workflow_id",
    "occurrence_id",
    "domain_outcome",
)


class ScheduledWorkflowSupportError(ValueError):
    """A `scheduled_for` value could not be interpreted as a business date."""


def derive_local_date(scheduled_for: str, *, timezone_name: str = "Asia/Baku") -> str:
    """Return the `YYYY-MM-DD` local business date for a canonical UTC instant.

    `scheduled_for` must already be the canonical
    `nullone.scheduler-invocation.v1` UTC RFC3339 instant
    (`YYYY-MM-DDTHH:MM:SSZ`) -- this function does not itself validate the
    full scheduler-invocation contract (`nullone_scheduler_invocation`
    already does that before this is ever called). This deliberately never
    slices an opaque `occ_<hex>` occurrence_id: the business date is a
    property of *when* the occurrence was scheduled, not of its identity
    string.
    """

    if not isinstance(scheduled_for, str):
        raise ScheduledWorkflowSupportError("scheduled_for must be a string")

    try:
        instant = datetime.strptime(scheduled_for, _CANONICAL_TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ScheduledWorkflowSupportError(
            f"scheduled_for is not a canonical UTC RFC3339 instant: {scheduled_for!r}"
        ) from exc

    local = instant.astimezone(ZoneInfo(timezone_name))
    return local.strftime("%Y-%m-%d")


def results_agree(in_memory: Any, persisted: dict[str, Any]) -> bool:
    """True when an in-memory runtime return agrees with the persisted record.

    The persisted #27 record on disk remains authoritative regardless; this
    only decides whether the two are consistent enough to proceed, or
    whether `RESULT_RECONCILIATION_REQUIRED` must fail closed instead.
    """

    if not isinstance(in_memory, dict):
        return False

    return all(in_memory.get(field) == persisted.get(field) for field in RECONCILIATION_FIELDS)


def safe_trigger_context(trigger: Any) -> dict[str, Any]:
    """Echo only already-public, non-sensitive trigger fields for diagnosis.

    Never assumes `trigger` parsed successfully -- this is called from a
    TRIGGER_REJECTED path, where `trigger` may be malformed or not even a
    dict.
    """

    if not isinstance(trigger, dict):
        return {}

    return {
        key: trigger.get(key)
        for key in ("workflow_id", "source", "scheduled_for", "occurrence_id")
        if key in trigger
    }
