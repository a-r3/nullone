#!/usr/bin/env python3
"""NullOne Scheduled Occurrence Authority (#59 remaining scope).

Answers exactly one deterministic question, and nothing else:

    Given this workflow, this trigger-adapter source, and an observed
    wake-up instant, which exact NullOne-owned daily schedule slot (if
    any) is due right now?

This module is the repository's own scheduled-occurrence authority --
NullOne, not the external scheduler, owns `scheduled_for`. See
`docs/deployment/59-scheduled-workflows-deployment.md`'s "OpenClaw trigger
edge: CONFIRMED BLOCKED" section for why: OpenClaw 2026.8.2 command-payload
jobs cannot supply the intended scheduled occurrence to the process they
invoke, so a wake-up-only external trigger (`nullone-scheduled-wakeup.py`)
must resolve its own due slot instead of trusting anything the trigger
supplies beyond "wake up now."

Explicit non-goals (deliberately, not an oversight):

- no scheduler database, job queue, cron engine, or occurrence-claim
  ledger -- #28/#29's existing per-occurrence lock and persisted #27
  result already make replay/concurrency safe once a `scheduled_for` is
  known; this module only needs to be a pure function computing that value;
- no historical backfill/catch-up of a missed prior local-date occurrence
  -- see `resolve_scheduled_occurrence`'s docstring;
- no nearest-cron heuristic, no "probably today's occurrence" guessing;
- no I/O, subprocess, or network of any kind -- this is a pure function
  over its own arguments and the immutable `nullone_schedule_registry`.

`triggered_at` here is the same canonical-UTC-RFC3339 "observational only"
value the `nullone.scheduler-invocation.v1` contract already defines --
this module is the one caller allowed to treat it as *also* the wake-up
observation used to decide whether today's slot is due, but it is never
minted into `scheduled_for`, `external_occurrence_id`, or `occurrence_id`
directly; those are always derived from the resolved schedule slot.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from nullone_schedule_registry import ScheduleRegistryError, ScheduleSpec, get_schedule
from nullone_scheduler_invocation import (
    CONTRACT_VERSION,
    SCHEMA,
    TIMESTAMP_RE,
    SchedulerInvocationError,
    compute_occurrence_id,
    validate_payload,
)

_CANONICAL_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

STATUSES = frozenset({"DUE", "NO_DUE_OCCURRENCE"})


class ScheduledOccurrenceAuthorityError(ValueError):
    """Malformed authority input (workflow_id/source/triggered_at).

    Raised instead of silently substituting a default -- this module's own
    fail-closed requirement, matching
    `nullone_scheduler_invocation.SchedulerInvocationError`'s convention.
    """


@dataclass(frozen=True)
class ScheduledOccurrenceResolution:
    """The authority's typed pure result.

    `status == "DUE"`: `scheduler_invocation` is a validated
    `nullone.scheduler-invocation.v1` payload ready to hand to
    `MorningWorkflow`/`AnalyticsWorkflow`; `lateness_seconds` is diagnostic
    only (this module invents no automatic "too late" blocker -- see
    module docstring).

    `status == "NO_DUE_OCCURRENCE"`: `scheduler_invocation` is `None`;
    `next_local_scheduled_instant` names today's still-future slot, purely
    for operator/diagnostic visibility -- it is never treated as a
    fallback due occurrence by this module or any caller.
    """

    status: str
    workflow_id: str
    schedule_id: str
    source: str
    triggered_at: str
    reason_code: str
    local_scheduled_date: str
    local_scheduled_time: str
    scheduler_invocation: dict[str, Any] | None = None
    scheduled_for: str | None = None
    lateness_seconds: float | None = None
    next_local_scheduled_instant: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ScheduledOccurrenceAuthorityError(f"unknown status: {self.status!r}")
        if self.status == "DUE" and (self.scheduler_invocation is None or self.scheduled_for is None):
            raise ScheduledOccurrenceAuthorityError("DUE resolution must carry scheduler_invocation/scheduled_for")
        if self.status == "NO_DUE_OCCURRENCE" and self.scheduler_invocation is not None:
            raise ScheduledOccurrenceAuthorityError("NO_DUE_OCCURRENCE resolution must not carry scheduler_invocation")


def _parse_triggered_at(triggered_at: str) -> datetime:
    if not isinstance(triggered_at, str) or not TIMESTAMP_RE.fullmatch(triggered_at):
        raise ScheduledOccurrenceAuthorityError(
            f"triggered_at must be canonical UTC RFC3339: {triggered_at!r}"
        )
    return datetime.strptime(triggered_at, _CANONICAL_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def _format_canonical(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).strftime(_CANONICAL_TIMESTAMP_FORMAT)


def _todays_scheduled_instant(spec: ScheduleSpec, observed_local: datetime) -> datetime:
    """The exact configured schedule time, on `observed_local`'s own calendar date."""

    local_tz = ZoneInfo(spec.timezone_name)
    local_time = spec.local_time_of_day()
    return datetime(
        year=observed_local.year,
        month=observed_local.month,
        day=observed_local.day,
        hour=local_time.hour,
        minute=local_time.minute,
        second=local_time.second,
        tzinfo=local_tz,
    )


def resolve_scheduled_occurrence(
    *,
    workflow_id: str,
    source: str,
    triggered_at: str,
) -> ScheduledOccurrenceResolution:
    """Pure resolver: given a wake-up observation, which exact NullOne-owned
    daily schedule slot (if any) is due right now?

    Algorithm (see module docstring for the non-goals this deliberately
    excludes):

    1. Load the exact `ScheduleSpec` for `workflow_id` (fails closed for an
       unsupported workflow).
    2. Convert `triggered_at` (canonical UTC) to the schedule's own local
       timezone.
    3. Take that observation's local calendar date.
    4. Construct *that same local date's* configured schedule instant.
    5. If the observation is strictly before that instant: `NO_DUE_OCCURRENCE`.
       This never falls back to a previous local date's slot -- a wake-up
       that arrives before today's slot is simply early, not a signal to
       backfill yesterday's occurrence.
    6. Otherwise: today's slot is due. Convert it to canonical UTC as
       `scheduled_for`, derive a deterministic `external_occurrence_id`
       from `(schedule_id, scheduled_for)` only, compute `occurrence_id`
       via the existing #65 contract rule, and return a
       `nullone.scheduler-invocation.v1` payload already run through
       `validate_payload()`.

    Same-day replays (09:07, 10:15, a manual 18:00 run) all recompute the
    identical `scheduled_for`/`external_occurrence_id`/`occurrence_id`
    because those are derived only from the schedule slot, never from
    `triggered_at` -- only `triggered_at` (and the diagnostic
    `lateness_seconds`) changes between calls.
    """

    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise ScheduledOccurrenceAuthorityError("workflow_id must be non-empty")
    if not isinstance(source, str) or not source.strip():
        raise ScheduledOccurrenceAuthorityError("source must be non-empty")

    triggered_at_instant = _parse_triggered_at(triggered_at)

    try:
        spec = get_schedule(workflow_id)
    except ScheduleRegistryError as exc:
        raise ScheduledOccurrenceAuthorityError(str(exc)) from exc

    local_tz = ZoneInfo(spec.timezone_name)
    observed_local = triggered_at_instant.astimezone(local_tz)
    todays_scheduled_local = _todays_scheduled_instant(spec, observed_local)

    local_scheduled_date = todays_scheduled_local.strftime("%Y-%m-%d")
    local_scheduled_time = spec.local_time

    if observed_local < todays_scheduled_local:
        return ScheduledOccurrenceResolution(
            status="NO_DUE_OCCURRENCE",
            workflow_id=workflow_id,
            schedule_id=spec.schedule_id,
            source=source,
            triggered_at=triggered_at,
            reason_code="BEFORE_TODAYS_SCHEDULED_SLOT",
            local_scheduled_date=local_scheduled_date,
            local_scheduled_time=local_scheduled_time,
            next_local_scheduled_instant=_format_canonical(todays_scheduled_local),
        )

    scheduled_for = _format_canonical(todays_scheduled_local)
    external_occurrence_id = f"{spec.schedule_id}@{scheduled_for}"
    occurrence_id = compute_occurrence_id(workflow_id, source, external_occurrence_id, scheduled_for)

    scheduler_invocation = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "workflow_id": workflow_id,
        "source": source,
        "external_occurrence_id": external_occurrence_id,
        "scheduled_for": scheduled_for,
        "triggered_at": triggered_at,
        "occurrence_id": occurrence_id,
    }

    try:
        validate_payload(scheduler_invocation)
    except SchedulerInvocationError as exc:  # pragma: no cover - defensive, should be unreachable
        raise ScheduledOccurrenceAuthorityError(
            f"resolved scheduler_invocation failed contract validation: {exc}"
        ) from exc

    lateness_seconds = (triggered_at_instant - todays_scheduled_local).total_seconds()

    return ScheduledOccurrenceResolution(
        status="DUE",
        workflow_id=workflow_id,
        schedule_id=spec.schedule_id,
        source=source,
        triggered_at=triggered_at,
        reason_code="OK",
        local_scheduled_date=local_scheduled_date,
        local_scheduled_time=local_scheduled_time,
        scheduler_invocation=scheduler_invocation,
        scheduled_for=scheduled_for,
        lateness_seconds=lateness_seconds,
    )


def self_test() -> int:
    # 1. Morning 08:29:59 Baku -> NO_DUE.
    just_before = resolve_scheduled_occurrence(
        workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:29:59Z"
    )
    assert just_before.status == "NO_DUE_OCCURRENCE", just_before
    assert just_before.reason_code == "BEFORE_TODAYS_SCHEDULED_SLOT", just_before
    assert just_before.next_local_scheduled_instant == "2026-09-08T04:30:00Z", just_before

    # 2. Morning 08:30:00 Baku exactly -> DUE.
    exactly_due = resolve_scheduled_occurrence(
        workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:30:00Z"
    )
    assert exactly_due.status == "DUE", exactly_due
    assert exactly_due.scheduled_for == "2026-09-08T04:30:00Z", exactly_due
    assert exactly_due.scheduler_invocation is not None
    assert exactly_due.lateness_seconds == 0, exactly_due

    # 3. Same-day delayed wake (09:07 local = 05:07Z) -> same occurrence identity.
    delayed = resolve_scheduled_occurrence(
        workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T05:07:00Z"
    )
    assert delayed.status == "DUE", delayed
    assert delayed.scheduled_for == exactly_due.scheduled_for
    assert delayed.scheduler_invocation["occurrence_id"] == exactly_due.scheduler_invocation["occurrence_id"]
    assert delayed.scheduler_invocation["external_occurrence_id"] == exactly_due.scheduler_invocation["external_occurrence_id"]
    assert delayed.lateness_seconds == 37 * 60

    # 3b. Manual same-day replay much later (18:00 local) -> same identity too.
    manual_late = resolve_scheduled_occurrence(
        workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T14:00:00Z"
    )
    assert manual_late.status == "DUE", manual_late
    assert manual_late.scheduler_invocation["occurrence_id"] == exactly_due.scheduler_invocation["occurrence_id"]

    # 4. Next day 07:00 Baku (still before 08:30) -> NO_DUE, no previous-day backfill.
    next_day_early = resolve_scheduled_occurrence(
        workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-09T03:00:00Z"
    )
    assert next_day_early.status == "NO_DUE_OCCURRENCE", next_day_early

    # 5. Next day 08:30 Baku -> a new, distinct occurrence.
    next_day_due = resolve_scheduled_occurrence(
        workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-09T04:30:00Z"
    )
    assert next_day_due.status == "DUE", next_day_due
    assert next_day_due.scheduler_invocation["occurrence_id"] != exactly_due.scheduler_invocation["occurrence_id"]

    # 6. Daily Analytics UTC/Baku boundary: 03:20 Asia/Baku on 2026-09-09
    #    is 2026-09-08T23:20:00Z (Asia/Baku is UTC+4, no DST).
    boundary = resolve_scheduled_occurrence(
        workflow_id="daily-analytics", source="openclaw", triggered_at="2026-09-08T23:20:00Z"
    )
    assert boundary.status == "DUE", boundary
    assert boundary.scheduled_for == "2026-09-08T23:20:00Z", boundary
    assert boundary.local_scheduled_date == "2026-09-09", boundary

    boundary_just_before = resolve_scheduled_occurrence(
        workflow_id="daily-analytics", source="openclaw", triggered_at="2026-09-08T23:19:59Z"
    )
    assert boundary_just_before.status == "NO_DUE_OCCURRENCE", boundary_just_before

    # 7. Different source -> same scheduled_for, different occurrence_id
    #    (the accepted #65 contract intentionally namespaces identity by source).
    alt_source_due = resolve_scheduled_occurrence(
        workflow_id="morning-editorial", source="systemd-timer", triggered_at="2026-09-08T04:30:00Z"
    )
    assert alt_source_due.scheduled_for == exactly_due.scheduled_for
    assert alt_source_due.scheduler_invocation["occurrence_id"] != exactly_due.scheduler_invocation["occurrence_id"]

    # 8. Clock value never becomes scheduled_for/external_occurrence_id/occurrence_id directly.
    assert delayed.triggered_at != delayed.scheduled_for
    assert delayed.triggered_at not in delayed.scheduler_invocation["external_occurrence_id"]
    assert delayed.scheduler_invocation["scheduled_for"] == exactly_due.scheduled_for

    # 9. Malformed inputs fail closed.
    for bad_kwargs in (
        {"workflow_id": "", "source": "openclaw", "triggered_at": "2026-09-08T04:30:00Z"},
        {"workflow_id": "morning-editorial", "source": "", "triggered_at": "2026-09-08T04:30:00Z"},
        {"workflow_id": "morning-editorial", "source": "openclaw", "triggered_at": "not-a-timestamp"},
        {"workflow_id": "story", "source": "openclaw", "triggered_at": "2026-09-08T04:30:00Z"},
        {"workflow_id": "unknown-workflow", "source": "openclaw", "triggered_at": "2026-09-08T04:30:00Z"},
    ):
        try:
            resolve_scheduled_occurrence(**bad_kwargs)
            raise AssertionError(f"malformed input was not rejected: {bad_kwargs}")
        except ScheduledOccurrenceAuthorityError:
            pass

    print("SCHEDULED_OCCURRENCE_AUTHORITY_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_PERSISTENCE=TRUE")
    print("NO_BACKFILL=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne scheduled occurrence authority")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
