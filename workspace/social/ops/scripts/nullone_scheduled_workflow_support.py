#!/usr/bin/env python3
"""Shared pure helpers for the #59 scheduled application workflows.

Used by both `nullone_morning_workflow.py` and `nullone_analytics_workflow.py`
so the safety-relevant behaviors every scheduled workflow needs -- deriving
a business date from `scheduled_for` in Asia/Baku (never by slicing the
opaque hash-like `occurrence_id`), proving an in-memory runtime return
agrees with the exact persisted #27 record, and validating that an injected
notifier's return is one of #30's own known statuses rather than trusting
it blindly -- are implemented once, not duplicated and risk drifting apart.

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

# The complete, exact set of statuses `nullone_failure_notify
# .notify_if_required()` is ever documented to return. Kept here (not
# re-derived from that module) because it is a small, stable, explicitly
# reviewed allowlist -- #59 must never invent a new status name of its own,
# and must never accept an unrecognized one on faith. `PENDING` is
# deliberately excluded: it is only ever a transient on-disk record state
# between the reservation write and the outbound transport call inside
# `notify_if_required` itself, never a value that function returns to a
# caller.
VALID_NOTIFICATION_STATUSES: frozenset[str] = frozenset(
    {
        "NOT_REQUIRED",
        "SENT",
        "FAILED",
        "UNKNOWN",
        "ALREADY_PENDING",
        "ALREADY_SENT",
        "ALREADY_FAILED",
        "ALREADY_UNKNOWN",
    }
)


class ScheduledWorkflowSupportError(ValueError):
    """A `scheduled_for` value could not be interpreted as a business date,
    or an injected notifier's return did not match #30's known status
    contract."""


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


def validate_notification_outcome(outcome: Any) -> str:
    """Return the validated `status` string from an injected notifier's return.

    Fails closed (`ScheduledWorkflowSupportError`) rather than trusting an
    injected `notifier` callable's return blindly: a caller returning
    `None`, a non-mapping, a mapping with no/blank/non-string `status`, or
    an unrecognized status name (anything outside
    `VALID_NOTIFICATION_STATUSES`) is not sufficient proof that the #30
    notification decision completed safely, even though no exception was
    raised. This is deliberately narrow: it does not invent new status
    names, and every one of #30's own legitimate fail-closed outcomes
    (`FAILED`, `UNKNOWN`, `ALREADY_FAILED`, `ALREADY_UNKNOWN`, ...) passes
    through here unchanged and is never treated as malformed -- #30 has
    already durably consumed its one automatic attempt for those.
    """

    if not isinstance(outcome, dict):
        raise ScheduledWorkflowSupportError(
            f"notifier result must be an object, got {type(outcome).__name__}"
        )

    status = outcome.get("status")

    if not isinstance(status, str) or status not in VALID_NOTIFICATION_STATUSES:
        raise ScheduledWorkflowSupportError(
            f"notifier result has an unrecognized status: {status!r}"
        )

    return status


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
