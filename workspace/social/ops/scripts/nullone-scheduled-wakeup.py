#!/usr/bin/env python3
"""NullOne static wake-up edge (#59 remaining scope).

    nullone-scheduled-wakeup.py morning --source openclaw
    nullone-scheduled-wakeup.py analytics --source openclaw

Replaces the disproven "OpenClaw directly supplies the scheduled
occurrence" design (`docs/deployment/59-scheduled-workflows-deployment.md`'s
"OpenClaw trigger edge: CONFIRMED BLOCKED"; OpenClaw 2026.8.2 command-payload
jobs cannot receive an intended scheduled instant through argv/env/stdin).
This process needs no per-occurrence trigger file, no `scheduled_for`
argument, no OpenClaw per-run UUID, and no dynamic env interpolation -- the
point of this replacement architecture:

    external timer (e.g. OpenClaw --command job, or any other future
    scheduler adapter)
        -> WAKE-UP ONLY
        -> nullone_scheduled_occurrence_authority.resolve_scheduled_occurrence
        -> exact NullOne-owned scheduled_for (or NO_DUE_OCCURRENCE)
        -> nullone.scheduler-invocation.v1
        -> MorningWorkflow / AnalyticsWorkflow (via
           nullone_scheduled_run_dispatch, shared with
           nullone-scheduled-run.py's exact-trigger-file path)

Architectural split (intentional, not accidental):

- Generic occurrence authority (`resolve_scheduled_occurrence`) remains
  scheduler-independent and multi-adapter: `source` is part of #65
  occurrence identity, so alternate namespaces such as `systemd-timer`
  are still valid at the pure-authority layer.
- This **current M0 production wake-up executable** pins `--source` to the
  reviewed allowlist `M0_WAKEUP_SOURCES` (exactly `openclaw`). An unreviewed
  / typo / alternate CLI source fails closed before occurrence resolution
  and before any workflow/provider/notifier/#27 side effect. A future
  adapter namespace requires an explicit reviewed code/config change, not
  an arbitrary CLI string.

The only things this process may know: the reviewed adapter `--source`
string and an injected/testable timezone-aware clock (see `now` below).
It never invokes OpenClaw, reads OpenClaw config, knows Telegram/Zernio
transport, reads secret environment variables, publishes, approves, or
processes an approval callback -- see
`tests/test_scheduled_workflows_capability_negative.py`.

Exit-code contract (distinct from, but consistent with,
`nullone-scheduled-run.py`'s scheduler-vs-domain rule):

- Infrastructure rejection (`WAKEUP_SOURCE_UNSUPPORTED` /
  `WAKEUP_CLOCK_INVALID` / authority reject): non-zero. No occurrence
  resolution side effect beyond the pure call itself, no dispatch, no
  provider, no notifier, no #27 artifact.
- `NO_DUE_OCCURRENCE`: exit 0. The wake-up was valid; there was simply no
  current NullOne schedule slot to execute. No provider call, no notifier
  call, no #27 result fabricated.
- `DUE` + `application_execution == "COMPLETED"`: exit 0, regardless of
  domain health (`SUCCEEDED`/`BLOCKED`/`FAILED`/actionable `UNKNOWN`),
  preserving the merged #59 scheduler-vs-domain semantics unchanged.
- `DUE` + `application_execution == "FAILED"`: non-zero. No retry layer is
  added here; #28/#29's own bounded retry already ran before this point.

Manual/repeated wake-up semantics: because `scheduled_for`/
`external_occurrence_id`/`occurrence_id` are derived only from the
resolved schedule slot (never from the observed wake instant), a manual
run through this exact same static command after today's slot replays the
same occurrence as a natural scheduled wake would; a manual run before
today's slot is `NO_DUE_OCCURRENCE`, identically to a natural early wake.
This process cannot mint a novel arbitrary occurrence, and cannot mint a
second namespace merely by typing a different `--source` on the M0 edge.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from nullone_scheduled_occurrence_authority import (
    ScheduledOccurrenceAuthorityError,
    ScheduledOccurrenceResolution,
    resolve_scheduled_occurrence,
)
from nullone_scheduled_run_dispatch import run_analytics_trigger, run_morning_trigger

WORKFLOW_BY_COMMAND = {
    "morning": "morning-editorial",
    "analytics": "daily-analytics",
}

# Current M0 production wake-up edge allowlist only. Generic
# `resolve_scheduled_occurrence(..., source=...)` remains multi-adapter;
# expanding this set requires an explicit reviewed change.
M0_WAKEUP_SOURCES = frozenset({"openclaw"})

REASON_SOURCE_UNSUPPORTED = "WAKEUP_SOURCE_UNSUPPORTED"
REASON_CLOCK_INVALID = "WAKEUP_CLOCK_INVALID"
REASON_AUTHORITY_REJECTED = "WAKEUP_AUTHORITY_REJECTED"

_DISPATCH_BY_WORKFLOW: dict[str, Callable[[dict[str, Any]], Any]] = {
    "morning-editorial": run_morning_trigger,
    "daily-analytics": run_analytics_trigger,
}


class WakeupInfrastructureError(ValueError):
    """Stable infrastructure rejection on the M0 wake-up edge.

    `args[0]` is always a stable `REASON_*` code -- never an arbitrary
    operator-supplied string or clock/object repr.
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_m0_wakeup_source(source: object) -> str:
    """Fail closed unless `source` is an exact reviewed M0 allowlist member.

    No `.strip()` / case-fold / control-character normalization: malformed
    input must not collapse into a valid identity namespace.
    """

    if not isinstance(source, str) or source not in M0_WAKEUP_SOURCES:
        raise WakeupInfrastructureError(REASON_SOURCE_UNSUPPORTED)
    return source


def _require_aware_clock_instant(value: object) -> datetime:
    """Injected clock must return a timezone-aware datetime.

    Naive datetimes are rejected (never interpreted via host-local TZ).
    Non-datetime values are rejected. Aware non-UTC values are normalized
    to UTC only after awareness is proven (`tzinfo` present and
    `utcoffset()` not None).
    """

    if not isinstance(value, datetime):
        raise WakeupInfrastructureError(REASON_CLOCK_INVALID)
    if value.tzinfo is None or value.utcoffset() is None:
        raise WakeupInfrastructureError(REASON_CLOCK_INVALID)
    return value.astimezone(timezone.utc)


def _canonical_now(now: Callable[[], datetime]) -> str:
    try:
        instant = now()
    except WakeupInfrastructureError:
        raise
    except Exception:
        raise WakeupInfrastructureError(REASON_CLOCK_INVALID) from None
    aware_utc = _require_aware_clock_instant(instant)
    return aware_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _report_wakeup_rejected(reason_code: str) -> int:
    print("STATUS=WAKEUP_REJECTED")
    print(f"REASON_CODE={reason_code}")
    print("PROVIDER_CALLED=FALSE")
    print("NOTIFIER_CALLED=FALSE")
    return 1


def _report_no_due(resolution: ScheduledOccurrenceResolution) -> int:
    print("STATUS=NO_DUE_OCCURRENCE")
    print(f"WORKFLOW_ID={resolution.workflow_id}")
    print(f"SCHEDULE_ID={resolution.schedule_id}")
    print(f"SOURCE={resolution.source}")
    print(f"TRIGGERED_AT={resolution.triggered_at}")
    print(f"NEXT_LOCAL_SCHEDULED_INSTANT={resolution.next_local_scheduled_instant}")
    print(f"REASON_CODE={resolution.reason_code}")
    print("PROVIDER_CALLED=FALSE")
    print("NOTIFIER_CALLED=FALSE")
    return 0


def _report_due(resolution: ScheduledOccurrenceResolution, result: Any) -> int:
    print("STATUS=DUE")
    print(f"WORKFLOW_ID={resolution.workflow_id}")
    print(f"SCHEDULE_ID={resolution.schedule_id}")
    print(f"SOURCE={resolution.source}")
    print(f"SCHEDULED_FOR={resolution.scheduled_for}")
    print(f"TRIGGERED_AT={resolution.triggered_at}")
    print(f"LATENESS_SECONDS={resolution.lateness_seconds}")
    print(f"APPLICATION_EXECUTION={result.application_execution}")
    print(f"DOMAIN_OUTCOME={result.domain_outcome}")
    print(f"RUN_ID={result.run_id}")
    print(f"OCCURRENCE_ID={result.occurrence_id}")
    print(f"RESULT_FILE={result.result_file}")
    print(f"NOTIFICATION_STATUS={result.notification_status}")
    print(f"REASON_CODE={result.reason_code}")
    print(f"REASON_TEXT={result.reason_text}")
    return 0 if result.application_execution == "COMPLETED" else 1


def wake_up(
    command: str,
    *,
    source: str,
    now: Callable[[], datetime] = _utc_now,
    dispatch_by_workflow: dict[str, Callable[[dict[str, Any]], Any]] = _DISPATCH_BY_WORKFLOW,
) -> int:
    """Resolve the wake-up and, only if a slot is due, run the workflow.

    `now`/`dispatch_by_workflow` are injectable purely for testability;
    the CLI's own `main()` always uses the real clock and the real
    production dispatch functions.

    Source allowlist and clock awareness are enforced here on the M0
    executable edge before any occurrence resolution or dispatch.
    """

    try:
        reviewed_source = _require_m0_wakeup_source(source)
        triggered_at = _canonical_now(now)
    except WakeupInfrastructureError as exc:
        return _report_wakeup_rejected(exc.reason_code)

    workflow_id = WORKFLOW_BY_COMMAND[command]

    try:
        resolution = resolve_scheduled_occurrence(
            workflow_id=workflow_id, source=reviewed_source, triggered_at=triggered_at
        )
    except ScheduledOccurrenceAuthorityError:
        return _report_wakeup_rejected(REASON_AUTHORITY_REJECTED)

    if resolution.status == "NO_DUE_OCCURRENCE":
        return _report_no_due(resolution)

    result = dispatch_by_workflow[workflow_id](resolution.scheduler_invocation)
    return _report_due(resolution, result)


def self_test() -> int:
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    class _FakeMorningResult:
        def __init__(self) -> None:
            self.application_execution = "COMPLETED"
            self.domain_outcome = "SUCCEEDED"
            self.run_id = "run_fake"
            self.occurrence_id = "occ_fake"
            self.result_file = "/tmp/fake-result.json"
            self.notification_status = "NOT_REQUIRED"
            self.reason_code = "OK"
            self.reason_text = "fake morning workflow result"

    calls: list[dict[str, Any]] = []

    def fake_dispatch(trigger: dict[str, Any]) -> _FakeMorningResult:
        calls.append(trigger)
        return _FakeMorningResult()

    fixed = {"morning-editorial": fake_dispatch, "daily-analytics": fake_dispatch}

    # 1. Before today's slot -> NO_DUE, exit 0, zero dispatch calls.
    exit_code = wake_up(
        "morning",
        source="openclaw",
        now=lambda: _dt(2026, 9, 8, 4, 0, 0, tzinfo=timezone.utc),
        dispatch_by_workflow=fixed,
    )
    assert exit_code == 0
    assert len(calls) == 0

    # 2. At/after today's slot -> DUE, dispatch called exactly once, exit reflects
    #    application_execution.
    exit_code = wake_up(
        "morning",
        source="openclaw",
        now=lambda: _dt(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc),
        dispatch_by_workflow=fixed,
    )
    assert exit_code == 0
    assert len(calls) == 1
    first_trigger = calls[0]
    assert first_trigger["workflow_id"] == "morning-editorial"
    assert first_trigger["source"] == "openclaw"

    # 3. Same-day later wake -> same occurrence identity as call #2.
    exit_code = wake_up(
        "morning",
        source="openclaw",
        now=lambda: _dt(2026, 9, 8, 5, 7, 0, tzinfo=timezone.utc),
        dispatch_by_workflow=fixed,
    )
    assert exit_code == 0
    assert len(calls) == 2
    assert calls[1]["occurrence_id"] == first_trigger["occurrence_id"]
    assert calls[1]["scheduled_for"] == first_trigger["scheduled_for"]
    assert calls[1]["triggered_at"] != first_trigger["triggered_at"]

    # 4. Next day early wake -> NO_DUE again, no additional dispatch call.
    exit_code = wake_up(
        "morning",
        source="openclaw",
        now=lambda: _dt(2026, 9, 9, 3, 0, 0, tzinfo=timezone.utc),
        dispatch_by_workflow=fixed,
    )
    assert exit_code == 0
    assert len(calls) == 2

    # 5. Analytics: exact UTC/Baku boundary -> DUE.
    analytics_calls: list[dict[str, Any]] = []

    def fake_analytics_dispatch(trigger: dict[str, Any]) -> _FakeMorningResult:
        analytics_calls.append(trigger)
        return _FakeMorningResult()

    analytics_fixed = {"morning-editorial": fake_dispatch, "daily-analytics": fake_analytics_dispatch}
    exit_code = wake_up(
        "analytics",
        source="openclaw",
        now=lambda: _dt(2026, 9, 8, 23, 20, 0, tzinfo=timezone.utc),
        dispatch_by_workflow=analytics_fixed,
    )
    assert exit_code == 0
    assert len(analytics_calls) == 1
    assert analytics_calls[0]["scheduled_for"] == "2026-09-08T23:20:00Z"

    # 6. Application FAILED -> non-zero exit, still no crash/no fabricated retry.
    class _FakeFailedResult(_FakeMorningResult):
        def __init__(self) -> None:
            super().__init__()
            self.application_execution = "FAILED"
            self.domain_outcome = None
            self.reason_code = "RUNTIME_CRASHED"

    def failing_dispatch(_trigger: dict[str, Any]) -> _FakeFailedResult:
        return _FakeFailedResult()

    exit_code = wake_up(
        "morning",
        source="openclaw",
        now=lambda: _dt(2026, 9, 10, 4, 30, 0, tzinfo=timezone.utc),
        dispatch_by_workflow={"morning-editorial": failing_dispatch, "daily-analytics": failing_dispatch},
    )
    assert exit_code != 0

    # 7. Unreviewed source fails closed before dispatch.
    exit_code = wake_up(
        "morning",
        source="systemd-timer",
        now=lambda: _dt(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc),
        dispatch_by_workflow=fixed,
    )
    assert exit_code != 0
    assert len(calls) == 2  # unchanged from before the rejection

    # 8. Aware non-UTC clock normalizes to the same UTC slot as UTC clock.
    baku = timezone(_td(hours=4))
    exit_code = wake_up(
        "morning",
        source="openclaw",
        now=lambda: _dt(2026, 9, 11, 8, 30, 0, tzinfo=baku),
        dispatch_by_workflow=fixed,
    )
    assert exit_code == 0
    assert calls[-1]["triggered_at"] == "2026-09-11T04:30:00Z"

    # 9. Naive clock fails closed; no additional dispatch.
    before = len(calls)
    exit_code = wake_up(
        "morning",
        source="openclaw",
        now=lambda: _dt(2026, 9, 8, 4, 30, 0),
        dispatch_by_workflow=fixed,
    )
    assert exit_code != 0
    assert len(calls) == before

    print("SCHEDULED_WAKEUP_CLI_SELF_TEST=PASS")
    print("NO_OPENCLAW_EXECUTION=TRUE")
    print("NO_ZERNIO_CALL=TRUE")
    print("NO_CLAUDE_CALL=TRUE")
    print("NO_TELEGRAM_SEND=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne static wake-up edge")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")

    m = sub.add_parser("morning")
    m.add_argument("--source", required=True)

    a = sub.add_parser("analytics")
    a.add_argument("--source", required=True)

    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return wake_up(args.command, source=args.source)


if __name__ == "__main__":
    raise SystemExit(main())
