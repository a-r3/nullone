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
no job created/edited/enabled/disabled/removed/run), split explicitly into
what was directly verified versus what is this adapter's own inference/
design choice built on top of it:

VERIFIED:

- A cron/automation job's stable identity is its own UUID `id` field
  (e.g. `0666d47b-aceb-4a4d-960a-b4888f2066ed` for the live
  `texbrif-morning-editorial` job, `8e94064c-7e52-4ac5-a167-f3526f9f20c7`
  for the live `texbrif-daily-analytics` job). `declarationKey` is present
  on some other jobs as an alternate stable identity, but not on these two;
  `id` is always present and is the identity this adapter uses.
- Each individual cron execution recorded by `openclaw cron runs --id <id>
  --json` carries its own per-execution `sessionKey`
  (`agent:<agentId>:cron:<jobId>:run:<uuid>`) and its own actual start
  instant (`runAtMs`/`runAtIso`). Confirmed directly across two distinct
  scheduled occurrences of `texbrif-morning-editorial`: the real
  2026-09-05 (target 08:30, but this execution actually started at
  09:07:13 after an earlier delay) `ENOTFOUND`/timeout failure and the
  real 2026-09-06 08:30 success are two separate run records with two
  different `run:<uuid>` `sessionKey` suffixes. This proves only that
  distinct cron executions receive distinct per-run identifiers -- it does
  **not** prove that a retry of one specific logical occurrence receives a
  new UUID: those two records are two different scheduled days' ticks (the
  2026-09-06 run is the next day's separate occurrence, not a replay of
  the failed 2026-09-05 one), and no same-occurrence retry was directly
  observed in this evidence.
- Actual execution start time can lag a job's own cron target: the
  2026-09-05 run's `runAtIso` (`09:07:13`) does not match that job's
  `08:30` cron target -- confirmed directly.
- The job's own `schedule.expr`/`schedule.tz` is a plain cron expression
  plus IANA timezone (e.g. `"30 8 * * *"` / `"Asia/Baku"` for both live
  jobs).

DESIGN CHOICE (an inference this adapter makes, not something the evidence
above directly proves): this adapter never uses `sessionKey`'s per-run
UUID, `triggered_at`, or the actual execution instant as occurrence
identity. That UUID is generated internally by OpenClaw only once an
execution is already underway -- it is not a value a future command-job
wiring could know or supply *before* invocation -- and OpenClaw does not
document it as a stable occurrence identity anywhere this adapter found.
The scheduler-invocation contract's own requirement (the same logical
occurrence, including any genuine retry, must yield the same
`occurrence_id`) is instead satisfied by deriving identity from the job's
own stable `id` plus the exact intended `scheduled_for` instant -- values
a caller can in principle supply deterministically ahead of any particular
execution attempt, unlike a UUID OpenClaw only mints during execution.

CONFIRMED BLOCKER (2026-09-07, upgraded from an earlier `--help`-only
inference to direct source verification against the installed OpenClaw
2026.8.2 package -- `docs/deployment/59-scheduled-workflows-deployment.md`
carries the full citation trail): command-payload jobs do NOT receive the
intended scheduled occurrence through any argv, environment, or stdin
channel. `dist/server-cron-DtqkVgKM.js`'s `runCronCommandJob` (function at
line 209; the process-spawn call at lines 223-231) passes `payload.env`/
`payload.input`/`payload.argv` straight through to the spawned process
completely unmodified -- no `runAtMs`, `dueAt`, or any other scheduler-
owned occurrence value is merged in at execution time. Those `payload.env`/
`payload.input` values themselves originate exclusively from
`dist/cron-cli-DqFGSvBK.js`'s `parseCronCommandEnv` (line 57, a literal
`KEY=VALUE` string parser with no templating/interpolation of any kind)
fed by the static `--command-env`/`--command-input` CLI flags captured once
at job authoring/edit time (lines 748-749); `docs/automation/cron-jobs.md`
("Command payloads") documents the same flags as static process-environment/
stdin overrides. `runAtMs` is tracked by the scheduler only for its own
diagnostics (failure-alert text, delivery message timestamps, the
`cron:<jobId>:<startedAt>` run-history key) -- never passed into the
command process. A manual `openclaw automations run <job-id>` force-run
goes through this exact same `runCronCommandJob` path with the exact same
static `payload.env`/`payload.input`, so neither a scheduled nor a manual
invocation ever receives the caller's intended tick.

Because of this, #59's OpenClaw trigger edge cannot be completed today:
this adapter's `map_openclaw_occurrence` correctly requires the caller to
already have the exact intended `scheduled_for` in hand and fails closed
(`SchedulerInvocationError`) if it is missing/blank rather than
substituting the current time -- but no OpenClaw 2026.8.2 command-payload
mechanism exists for a future `--command nullone-scheduled-run.py ...` job
to obtain that value itself. Supplying it would require inventing a
substitute (current wall clock, `runAtMs`, nearest cron tick, or similar),
which this adapter and this repository deliberately refuse to do (see
`docs/deployment/59-scheduled-workflows-deployment.md`'s "OpenClaw trigger
edge: CONFIRMED BLOCKED" section for the full acceptance-gap writeup).
Status: `#59_OPENCLAW_TRIGGER_EDGE = BLOCKED`, reason
`EXACT_INTENDED_SCHEDULED_OCCURRENCE_NOT_EXPOSED_TO_COMMAND_PAYLOAD`. This
is a distinct, narrower problem from #37 (live job-payload migration/
activation) and must not be silently folded into it or into #61.
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
