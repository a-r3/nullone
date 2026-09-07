#!/usr/bin/env python3
"""OpenClaw scheduler edge adapter (#59/#65).

Maps one reviewed OpenClaw automation ("cron job") occurrence into
`nullone.scheduler-invocation.v1` and nothing else. See
`docs/deployment/59-scheduled-workflows-deployment.md` for the exact
read-only OpenClaw 2026.8.2 CLI evidence (`openclaw cron list/get/runs
--json` against the live Gateway, 2026-09-07) this mapping is built from,
and the confirmed gap it deliberately does not guess around.

This module owns 100% of the OpenClaw-specific vocabulary (job `id`,
`declarationKey`, `cron`, Gateway) at the trigger edge; nothing past
`map_openclaw_occurrence`'s return value carries any of it --
`nullone_morning_workflow.py`/`nullone_analytics_workflow.py` never import
this module. It never shells out to `openclaw` itself and never mutates a
job: it is a pure mapping function over already-observed, already-read
values, with no I/O, subprocess, or network capability of its own.

Confirmed OpenClaw 2026.8.2 read-only evidence (2026-09-07,
`openclaw cron list/get/runs --json` against the live Gateway; read-only,
no job created/edited/enabled/disabled/removed/run):

- A cron/automation job's stable identity is its own UUID `id` field
  (e.g. `0666d47b-aceb-4a4d-960a-b4888f2066ed` for the live
  `texbrif-morning-editorial` job, `8e94064c-7e52-4ac5-a167-f3526f9f20c7`
  for the live `texbrif-daily-analytics` job). `declarationKey` is present
  on some other jobs as an alternate stable identity, but not on these two;
  `id` is always present and is the identity this adapter uses.
- `openclaw cron runs --id <id> --json` shows each attempt's own
  `sessionKey` embeds a per-ATTEMPT UUID (`...:run:<uuid>`) that differs
  between a failed attempt and its later successful retry for the *same*
  logical scheduled occurrence -- confirmed directly: the real
  2026-09-05 09:07 `ENOTFOUND` failure and the real 2026-09-06 08:30
  success for `texbrif-morning-editorial` are two separate run records
  with two different `run:<uuid>` suffixes. That per-attempt id is
  therefore NOT a safe stable occurrence identity, and this adapter never
  uses it -- exactly why `docs/contracts/scheduler-invocation-v1.md`
  derives `occurrence_id` from `(workflow_id, source,
  external_occurrence_id, scheduled_for)` rather than trusting a
  source-supplied per-run id.
- Each run record also carries `runAtMs`/`runAtIso` (that attempt's own
  actual start instant, which can lag the job's cron target when a prior
  attempt failed or the Gateway was delayed -- confirmed: the failed
  2026-09-05 attempt's `runAtIso` was `09:07:13`, not the job's `08:30`
  cron target) and the job's own `schedule.expr`/`schedule.tz` (5-field
  cron + IANA timezone, e.g. `"30 8 * * *"` / `"Asia/Baku"` for both live
  jobs).

Confirmed gap (documented, not guessed around): `openclaw cron add/edit
--help`'s documented `--command*` flags do not show an environment variable
that reliably carries the *intended* cron-tick instant to a command-payload
job, as distinct from wall-clock execution time. Until #37 activation
confirms exactly what a `--command` job's environment receives from a live
Gateway invocation, the caller of this adapter (the future #37 job wiring,
not this module) is responsible for supplying the exact intended scheduled
instant as `scheduled_for`. This adapter never substitutes the current time
for it: `map_openclaw_occurrence` fails closed if `scheduled_for` is
missing/blank, matching the scheduler-invocation contract's "no
current-time substitution" rule.
"""
from __future__ import annotations

from typing import Any

from nullone_scheduler_invocation import (
    CONTRACT_VERSION,
    SCHEMA,
    SchedulerInvocationError,
    compute_occurrence_id,
)

SOURCE = "openclaw"


def map_openclaw_occurrence(
    *,
    workflow_id: str,
    openclaw_job_id: str,
    scheduled_for: str,
    triggered_at: str,
) -> dict[str, Any]:
    """Pure mapping: one reviewed OpenClaw job occurrence -> `nullone.scheduler-invocation.v1`.

    `openclaw_job_id` must be OpenClaw's own stable per-job UUID
    (`cron get`'s `.id`), never a per-attempt/per-run id (see module
    docstring). `scheduled_for` must be the exact intended scheduled
    instant already normalized to canonical UTC RFC3339 by the caller --
    this function performs no I/O, no subprocess call, and no Gateway
    access; the caller has already read `openclaw_job_id`/`scheduled_for`
    from a prior read-only observation.

    `external_occurrence_id` is deterministically derived from the stable
    `(openclaw_job_id, scheduled_for)` pair only, so a retried/delayed
    trigger for the same logical scheduled event reuses the same value,
    while two distinct jobs or two distinct scheduled instants never
    collide.
    """

    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise SchedulerInvocationError("workflow_id must be non-empty")

    if not isinstance(openclaw_job_id, str) or not openclaw_job_id.strip():
        raise SchedulerInvocationError("openclaw_job_id must be non-empty")

    if not isinstance(scheduled_for, str) or not scheduled_for.strip():
        raise SchedulerInvocationError(
            "scheduled_for must be supplied explicitly as the intended "
            "scheduled instant; OpenClaw wall-clock invocation time must "
            "never substitute for it"
        )

    if not isinstance(triggered_at, str) or not triggered_at.strip():
        raise SchedulerInvocationError("triggered_at must be non-empty")

    external_occurrence_id = f"openclaw-job-{openclaw_job_id}-at-{scheduled_for}"

    occurrence_id = compute_occurrence_id(
        workflow_id, SOURCE, external_occurrence_id, scheduled_for
    )

    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "workflow_id": workflow_id,
        "source": SOURCE,
        "external_occurrence_id": external_occurrence_id,
        "scheduled_for": scheduled_for,
        "triggered_at": triggered_at,
        "occurrence_id": occurrence_id,
    }


def self_test() -> int:
    from nullone_scheduler_invocation import accept_workflow_trigger, validate_payload

    morning_job_id = "0666d47b-aceb-4a4d-960a-b4888f2066ed"
    analytics_job_id = "8e94064c-7e52-4ac5-a167-f3526f9f20c7"

    payload = map_openclaw_occurrence(
        workflow_id="morning-editorial",
        openclaw_job_id=morning_job_id,
        scheduled_for="2026-09-08T04:30:00Z",
        triggered_at="2026-09-08T04:30:02Z",
    )
    validate_payload(dict(payload))
    accept_workflow_trigger(dict(payload), workflow_id="morning-editorial")

    # Replay: later triggered_at, same job + same scheduled_for -> same occurrence_id.
    replay = map_openclaw_occurrence(
        workflow_id="morning-editorial",
        openclaw_job_id=morning_job_id,
        scheduled_for="2026-09-08T04:30:00Z",
        triggered_at="2026-09-08T04:31:59Z",
    )
    assert replay["occurrence_id"] == payload["occurrence_id"], "replay must keep occurrence_id stable"

    # Different job, same instant -> different occurrence_id.
    other_job = map_openclaw_occurrence(
        workflow_id="daily-analytics",
        openclaw_job_id=analytics_job_id,
        scheduled_for="2026-09-08T04:30:00Z",
        triggered_at="2026-09-08T04:30:02Z",
    )
    assert other_job["occurrence_id"] != payload["occurrence_id"]

    # Same job, different scheduled instant -> different occurrence_id.
    next_day = map_openclaw_occurrence(
        workflow_id="morning-editorial",
        openclaw_job_id=morning_job_id,
        scheduled_for="2026-09-09T04:30:00Z",
        triggered_at="2026-09-09T04:30:01Z",
    )
    assert next_day["occurrence_id"] != payload["occurrence_id"]

    # Missing scheduled_for -> fail closed, never wall-clock substitution.
    try:
        map_openclaw_occurrence(
            workflow_id="morning-editorial",
            openclaw_job_id=morning_job_id,
            scheduled_for="",
            triggered_at="2026-09-08T04:30:02Z",
        )
        raise AssertionError("missing scheduled_for was not rejected")
    except SchedulerInvocationError:
        pass

    # Missing job id -> fail closed.
    try:
        map_openclaw_occurrence(
            workflow_id="morning-editorial",
            openclaw_job_id="",
            scheduled_for="2026-09-08T04:30:00Z",
            triggered_at="2026-09-08T04:30:02Z",
        )
        raise AssertionError("missing openclaw_job_id was not rejected")
    except SchedulerInvocationError:
        pass

    print("OPENCLAW_SCHEDULER_ADAPTER_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_JOB_MUTATION=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne OpenClaw scheduler edge adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
