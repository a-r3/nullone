# MorningWorkflow / AnalyticsWorkflow deployment mapping (#59)

Status: **PROPOSED / NOT APPLIED**

This document records the repository-level mapping only. No production
OpenClaw job, Claude invocation, Zernio connector, Telegram delivery,
scheduler, secret, or publication state was changed while implementing #59.
Current live Morning Editorial and Daily Analytics OpenClaw automations
remain the legacy prompt-only `agentTurn` jobs described below; neither was
read-edited-or-mutated by this change.

## Application flow

```text
OpenClaw scheduler edge (nullone_openclaw_scheduler_adapter.py)
  -> nullone.scheduler-invocation.v1
  -> MorningWorkflow / AnalyticsWorkflow (nullone_morning_workflow.py /
     nullone_analytics_workflow.py)
  -> existing #28 / #29 domain runtime
  -> exact persisted #27 result (reloaded from disk and validated)
  -> #30 notify_if_required() domain-notification decision
  -> typed MorningWorkflowResult / AnalyticsWorkflowResult
```

`MorningWorkflow`/`AnalyticsWorkflow` accept only `workflow_id ==
"morning-editorial"` / `"daily-analytics"` respectively (via
`nullone_scheduler_invocation.accept_workflow_trigger`), never invoke
`openclaw`, never know Telegram/Zernio transport details, never know cron
syntax or OpenClaw job UUIDs, and never publish, approve, schedule, or
create Zernio drafts. The Claude CLI invocation and the Telegram/OpenClaw
notification transport are both supplied as injected dependencies
(`invoke_provider`, `notifier`); see
`tests/test_scheduled_workflows_capability_negative.py` for the executable
proof.

## OpenClaw scheduler edge: confirmed read-only evidence

`nullone_openclaw_scheduler_adapter.py` owns 100% of the OpenClaw-specific
vocabulary at the trigger edge. It was built from real, read-only
inspection of the installed OpenClaw 2026.8.2 CLI and the live Gateway on
2026-09-07 -- `openclaw cron list/get/runs --json`. No job was created,
edited, enabled, disabled, removed, or run.

Confirmed facts are separated below into what was directly VERIFIED versus
this adapter's own INFERENCE/design choice built on top of that evidence --
the two must not be conflated.

VERIFIED:

- Both live jobs are legacy prompt-only `agentTurn` automations:
  `texbrif-morning-editorial` (id `0666d47b-aceb-4a4d-960a-b4888f2066ed`,
  `schedule.expr="30 8 * * *"`, `schedule.tz="Asia/Baku"`) and
  `texbrif-daily-analytics` (id `8e94064c-7e52-4ac5-a167-f3526f9f20c7`,
  `schedule.expr="20 3 * * *"`, `schedule.tz="Asia/Baku"`). Neither invokes
  this repository's application workflows today; this matches the #37
  preflight's confirmed durable fact "Morning Editorial and Daily
  Analytics: legacy job payloads still active."
- A job's stable identity is its own UUID `id` field. This adapter uses
  only that field, never a job's display name or prompt text, as the
  stable "source job identity" input.
- Each individual cron execution recorded by `openclaw cron runs --id <id>
  --json` carries its own per-execution `sessionKey`
  (`agent:<agentId>:cron:<jobId>:run:<uuid>`) and its own actual start
  instant (`runAtMs`/`runAtIso`), which can lag the job's own cron target:
  the real 2026-09-05 `texbrif-morning-editorial` execution (cron target
  08:30) actually started at `09:07:13` and failed with
  `ENOTFOUND`/timeout; the real 2026-09-06 execution started on-target at
  08:30 and succeeded. These are two separate run records with two
  different `run:<uuid>` `sessionKey` suffixes. This proves only that
  distinct cron executions get distinct per-run identifiers and that
  actual start time can lag the cron target -- it does **not** prove that
  a retry of one specific logical occurrence gets a new UUID: the
  2026-09-06 run is the *next day's separate scheduled tick*, not a replay
  of the failed 2026-09-05 one, so no same-occurrence-retry UUID behavior
  was directly observed here.

INFERENCE / DESIGN CHOICE (not directly proven by the evidence above):
this adapter deliberately never uses `sessionKey`'s per-run UUID,
`triggered_at`, or the actual execution instant as occurrence identity.
That UUID is generated internally by OpenClaw only once an execution is
already underway, so it is not a value a future command-job wiring could
know or supply *before* invocation, and OpenClaw does not document it as a
stable occurrence identity anywhere this adapter found. Current wall-clock
time is likewise never used as occurrence identity. Instead, the
scheduler-invocation contract's requirement (the same logical occurrence,
including any genuine retry, must yield the same `occurrence_id`) is
satisfied by deriving identity from the job's own stable `id` plus the
exact intended `scheduled_for` instant -- values a caller can in principle
supply deterministically ahead of any particular execution attempt. This
is a reviewed design choice standing in for evidence this repository does
not have (no same-occurrence retry was observed in the 2026-09-07
inspection window), not a claim of direct proof.

## OpenClaw trigger edge: CONFIRMED BLOCKED

This section supersedes an earlier, weaker "confirmed gap... Status:
`UNPROVEN_LIVE / DEFERRED_TO_#37`" note that was based only on `openclaw
cron add/edit --help`'s flag listing. On 2026-09-07 this was upgraded to
direct, read-only source inspection of the exact installed OpenClaw
**2026.8.2** npm package (`~/.nvm/versions/node/v22.23.2/lib/node_modules/
openclaw`), specifically its command-payload execution and CLI flag-parsing
code and the matching prose in `docs/automation/cron-jobs.md`. No job was
created, edited, enabled, disabled, removed, or run; no file in that
package was modified.

**Question asked**: does a scheduled command-payload job's process (argv,
environment, or stdin) receive any scheduler-owned metadata identifying
the *intended* logical scheduled occurrence (a due/scheduled tick, as
distinct from actual execution start time)?

**Answer: NO**, confirmed directly in source, not inferred:

- `dist/server-cron-DtqkVgKM.js`, function `runCronCommandJob` (defined at
  line 209): the actual process-spawn options built at lines 223-231 are

  ```js
  const result = await runCommandWithTimeout(payload.argv, {
      timeoutMs: secondsToMs(payload.timeoutSeconds) ?? DEFAULT_COMMAND_TIMEOUT_MS,
      ...payload.cwd ? { cwd: payload.cwd } : {},
      ...payload.input !== void 0 ? { input: payload.input } : {},
      ...payload.env ? { env: payload.env } : {},
      ...
  });
  ```

  `payload.argv`/`payload.input`/`payload.env` are passed through
  completely unmodified from the stored job definition. Nothing here merges
  in `runAtMs`, a due/scheduled timestamp, or any other scheduler-owned
  occurrence value.
- The call site (`runCommandJob: async ({ job, abortSignal }) => { const
  result = await runCronCommandJob({ job, abortSignal, nowMs: Date.now });
  ...`, line 3212) passes the stored `job` object as-is; the scheduler's own
  `runAtMs` for that execution is tracked separately (used only for
  `job.state.runningAtMs`, failure-alert text, and delivery-message
  timestamps -- confirmed at lines 2410-2419 and 2548 of the same file --
  never merged into `payload.env`/`payload.input`/`payload.argv`).
- `payload.env`/`payload.input` themselves originate exclusively from the
  CLI layer: `dist/cron-cli-DqFGSvBK.js`'s `parseCronCommandEnv` (line 57)
  is a literal `KEY=VALUE` string parser with no templating or
  interpolation syntax of any kind, fed by the static `--command-env`/
  `--command-input` flags captured once at `automations create`/`edit` time
  (`env: parseCronCommandEnv(opts.commandEnv)`, `input: typeof
  opts.commandInput === "string" ? opts.commandInput : void 0`, lines
  748-749). `docs/automation/cron-jobs.md`'s "Command payloads" section
  documents the identical contract in prose: "Optional `--command-env
  KEY=VALUE` (repeatable), `--command-input`, ... control the process
  environment, stdin, and output bounds" -- author-supplied, static,
  captured at job-authoring time, not scheduler-populated per run.
- Manual runs use the identical code path: `docs/cli/cron.md`'s "Manual
  runs" section confirms `openclaw automations run <job-id>` force-runs
  through the same `runCronCommandJob`/stored-`payload` mechanism, so a
  manual run and a genuinely scheduled run are indistinguishable from the
  command process's own point of view -- neither ever receives an intended-
  occurrence value distinct from whatever static `--command-env`/
  `--command-input` the job was authored with.

**Conclusion**: no OpenClaw 2026.8.2 command-payload mechanism exists for a
future `--command nullone-scheduled-run.py morning --trigger-file <path>`
job to learn its own intended `scheduled_for` from the Gateway at
invocation time. The only values available to such a process are: (a)
whatever static text was baked into `--command-env`/`--command-input` when
the job was created/edited (which cannot itself be a *future* scheduled
tick, since job authoring happens once, long before any particular
occurrence fires), and (b) ordinary process/OS state (wall clock, etc.).
Per this document's and `nullone_openclaw_scheduler_adapter.py`'s explicit
design rule, none of those are acceptable substitutes for the exact
intended scheduled occurrence -- inventing one (current wall clock,
`runAtIso`, nearest cron tick, file mtime, session UUID, "probably this
day's 08:30") would silently weaken the `nullone.scheduler-invocation.v1`
contract's identity/replay guarantees, so this repository does not do that.

```text
#59_OPENCLAW_TRIGGER_EDGE = BLOCKED
reason = EXACT_INTENDED_SCHEDULED_OCCURRENCE_NOT_EXPOSED_TO_COMMAND_PAYLOAD
```

This is a distinct, narrower problem than #37 (live job-payload migration/
activation, which presupposes a working command-payload wiring) and must
not be silently folded into #37 or into #61. Resolving it requires an
explicit, separately reviewed architecture decision -- for example another
OpenClaw payload surface (a script payload, which runs headlessly with
access to the owning agent's tools and could in principle read job/run
state through the `automations`/`cron` API itself, rather than through
static command env/stdin -- not evaluated here, out of this PR's scope), a
non-OpenClaw scheduler adapter (e.g. a systemd timer that itself knows and
supplies the exact intended tick), a reviewed wrapper with a newly
evaluated deterministic occurrence source, or a narrower amendment to
issue #59's own acceptance criteria. None of those is implemented by this
PR.

`nullone_openclaw_scheduler_adapter.map_openclaw_occurrence(workflow_id,
openclaw_job_id, scheduled_for, triggered_at)` is a pure function (no I/O,
no subprocess, no network) deriving `external_occurrence_id =
f"openclaw-job-{openclaw_job_id}-at-{scheduled_for}"` and computing
`occurrence_id` via the existing contract rule. It fails closed
(`SchedulerInvocationError`) if `scheduled_for` is missing/blank rather
than substituting the current time -- this remains correct and unchanged;
the blocker above is about what supplies `scheduled_for` to this function
in a live OpenClaw command-payload deployment, which is not yet possible.

## MorningWorkflow

`nullone_morning_workflow.run_morning_workflow(trigger, *,
invoke_provider, notifier=None, run_editorial=run_morning_editorial,
artifact_root=WORKSPACE, output_root=<#28 run-outcome root>,
sleep=time.sleep, timezone_name="Asia/Baku") -> MorningWorkflowResult`.

1. Validates the trigger (`accept_workflow_trigger(..., workflow_id=
   "morning-editorial")`); a rejected trigger never reaches the runtime.
2. Derives `board_date` from `scheduled_for` converted to Asia/Baku
   (`nullone_scheduled_workflow_support.derive_local_date`) -- never by
   slicing the opaque `occ_<hex>` `occurrence_id`.
3. Calls the existing #28 `run_morning_editorial` exactly once logically
   per normalized occurrence (its own `fcntl.flock` + persisted-result
   check already make re-entry/replay/concurrency safe; MorningWorkflow
   adds no competing lock or retry layer).
4. Computes `expected_run_id = make_run_id("morning-editorial",
   occurrence_id)` and reloads the exact persisted file from
   `result_path(output_root, expected_run_id)` -- never trusting the
   in-memory return, stdout, or exit code alone.
5. Validates full #27 structure (`validate_result_structure`) and proves
   `workflow_id`/`occurrence_id`/`run_id` match exactly; any mismatch or
   unreadable/malformed file fails closed
   (`RESULT_MISSING_OR_CORRUPT`/`RESULT_INVALID`/`RESULT_IDENTITY_MISMATCH`)
   without repairing the file.
6. Reconciles the in-memory return against the persisted record on
   `run_id`/`workflow_id`/`occurrence_id`/`domain_outcome`; disagreement is
   `RESULT_RECONCILIATION_REQUIRED`.
7. Only then calls the injected `notifier(persisted_result)` at most once
   (never `notify_if_required` or `OpenClawTelegramTransport` directly --
   see "Notification composition" below); a notifier exception (unsafe/
   corrupt on-disk notification state) is `NOTIFICATION_STATE_UNSAFE`, and
   a notifier return that is not one of #30's known statuses is
   `NOTIFICATION_RESULT_INVALID` -- neither reruns Morning or rewrites the
   #27 result.

The Claude CLI invocation itself was extracted, behavior-identical, from
`nullone-morning-editorial-run.py`'s previous in-file
`_default_invoke_provider` into `nullone_claude_editorial_provider
.default_invoke_provider` -- same model, timeout, tool allowlist, and
provider-failure classification -- so the legacy CLI wrapper and
`nullone-scheduled-run.py` share exactly one implementation.
`MorningWorkflow` never imports it; only the two CLI/runner layers do.

## AnalyticsWorkflow

`nullone_analytics_workflow.run_analytics_workflow(trigger, *,
provider_factory, notifier=None, run_analytics=run_daily_analytics,
artifact_root=WORKSPACE, output_root=<#29 run-outcome root>,
timezone_name="Asia/Baku") -> AnalyticsWorkflowResult`.

Mirrors MorningWorkflow's validate -> derive-date -> execute -> reload ->
prove -> reconcile -> notify sequence, with `analytics_date` in place of
`board_date` and the existing #29 `run_daily_analytics` in place of #28.

### AnalyticsProvider boundary (#61 seam)

`provider_factory: Callable[[], AnalyticsProvider]` is passed through
unchanged as `run_daily_analytics`'s existing `build_connector` parameter
-- the exact narrow read-only surface `nullone_zernio_analytics_adapter`
already defines (account, follower history, account insights, post
analytics). `AnalyticsWorkflow` never reads the production analytics
credential environment variable, never touches systemd, and never
constructs a Zernio connector itself (enforced by
`tests/test_scheduled_workflows_capability_negative.py`'s
`test_no_analytics_secret_env_var_name`).

The production factory boundary lives in
`nullone_analytics_provider_factory.py` (infrastructure, not application
layer), wired in only by `nullone-scheduled-run.py`. Per #59's scope
boundary against #61, `build_production_analytics_provider()` is a
fail-closed placeholder:

```text
PROVIDER_SECRET_WIRING_PENDING_61
```

It never reads any secret, environment variable, or credential file, and
never calls Zernio -- verified by
`test_factory_placeholder_raises_without_reading_environment`, which
injects the real secret env var name into the process environment and
asserts it is never read and never leaked into the raised error text.
Because this placeholder's exception is not one of #29's typed connector
errors, `run_daily_analytics` does not catch it: it propagates through
`AnalyticsWorkflow` uncaught and is reported as
`application_execution=RUNTIME_CRASHED` (never faked into a domain
`BLOCKED` #27 result, and never a real Zernio bootstrap attempt).
Completing `build_production_analytics_provider` to construct a real
`ZernioReadOnlyAnalyticsConnector` behind a securely-injected credential is
issue #61's job; `AnalyticsWorkflow` and its tests do not need to change
when it does.

## Exact persisted #27 result is authoritative

Both workflows compute `expected_run_id` themselves and reload
`result_path(output_root, expected_run_id)` from disk after runtime
execution/re-entry -- never trusting stdout, CLI exit code, or the
in-memory return alone. `validate_result_structure` (the existing #27
validator) is reused unmodified. Required fail-closed mismatch cases,
never silently repaired:

| Condition | `reason_code` |
| --- | --- |
| Missing file / OS error / not valid JSON / not a JSON object | `RESULT_MISSING_OR_CORRUPT` |
| Valid JSON but fails #27 structural validation (wrong schema, unexpected fields, invalid health/outcome relationship, etc.) | `RESULT_INVALID` |
| Structurally valid but `workflow_id`/`occurrence_id`/`run_id` does not match this occurrence | `RESULT_IDENTITY_MISMATCH` |
| Persisted record valid and matching, but the in-memory runtime return disagrees with it on `run_id`/`workflow_id`/`occurrence_id`/`domain_outcome` | `RESULT_RECONCILIATION_REQUIRED` (`reconciliation_required=True`) |

## Critical scheduler-vs-domain separation

Per `docs/architecture/nullone-application-runtime.md`'s "Critical
scheduler-vs-domain rule": `application_execution` (`"COMPLETED"` /
`"FAILED"`) reflects only whether orchestration itself safely established/
validated the occurrence, result, and notification state -- **never** the
domain outcome. A `domain_outcome` of `BLOCKED`, `FAILED`, or actionable
`UNKNOWN` is still `application_execution="COMPLETED"`, because the
application successfully executed/re-entered the domain runtime, found and
validated the exact persisted #27 result, and evaluated the #30
notification decision.

`application_execution="FAILED"` is reserved for: an invalid normalized
trigger (`TRIGGER_REJECTED`), an unparseable `scheduled_for`
(`SCHEDULED_FOR_INVALID`), the domain runtime raising before establishing
any result (`RUNTIME_CRASHED` -- this is also how the #61 provider-secret
placeholder surfaces; the raising exception's own message text is never
included in `reason_text` or `context`, only its stable
`type(exc).__name__`, since a future real credential/provider failure
could otherwise leak sensitive text into operator-facing output), a
missing/corrupt/mismatched persisted result (the table above), an
unreconciled in-memory/persisted disagreement, the injected `notifier`
raising (`NOTIFICATION_STATE_UNSAFE` -- a #30-level unsafe/corrupt on-disk
notification-record state; likewise never echoes the raised exception's
message, only its type name), or the injected `notifier` returning
something that is not one of #30's own known statuses
(`NOTIFICATION_RESULT_INVALID` -- see "Notification composition" below).
None of these rerun the domain workflow, rewrite the persisted #27 result,
or attempt a second notification send.

#30's own normal typed `FAILED`/`UNKNOWN` transport outcomes (and their
`ALREADY_*` replay forms) are reported truthfully via `notification_status`
with `application_execution` staying `"COMPLETED"`: #30 already durably
records those without this layer's help, and a transport ambiguity is not
an orchestration-establishment failure.

This module deliberately does NOT use the legacy CLI convention
(`domain_outcome != SUCCEEDED -> process exit 1`) that
`nullone-morning-editorial-run.py`/`nullone-daily-analytics-run.py` still
use for their own unrelated CLI contract; those wrappers are unchanged by
#59.

## `nullone-scheduled-run.py` CLI exit semantics

```text
nullone-scheduled-run.py morning --trigger-file <path>
nullone-scheduled-run.py analytics --trigger-file <path>
```

- **Exit 0**: `application_execution == "COMPLETED"` -- a valid occurrence
  was fully orchestrated and a valid authoritative #27 result exists,
  regardless of whether `domain_outcome` is `SUCCEEDED`, `BLOCKED`,
  `FAILED`, or actionable `UNKNOWN`.
- **Non-zero**: `application_execution == "FAILED"` -- orchestration itself
  could not safely establish/validate the occurrence/result/notification
  state.

`tests/test_scheduled_run_cli.py` carries the mandatory regression: a
persisted Daily Analytics `domain_outcome=BLOCKED` /
`scheduler_status=succeeded` result yields exit 0 with the #30 notifier
evaluated, while a corrupt/missing persisted result yields non-zero.
Production wiring uses `nullone_claude_editorial_provider
.default_invoke_provider` and `nullone_analytics_provider_factory
.build_production_analytics_provider` as the two provider defaults, and
`notify_if_required(..., transport=OpenClawTelegramTransport())` as the
notifier -- the only place this CLI (not `MorningWorkflow`/
`AnalyticsWorkflow`) knows OpenClaw/Telegram is the transport.

## Notification composition (#30)

Both workflows call an injected `notifier: Callable[[dict], dict] | None`
with the exact persisted #27 record, at most once per call, and never
reimplement actionability, failure identity, the sanitizer, the Telegram
message, or notification-attempt state -- all of that remains
`nullone_failure_notify.notify_if_required`'s existing job. Production
wiring in `nullone-scheduled-run.py` (the only place either workflow's
`notifier` is bound to a real transport; neither workflow module imports
`OpenClawTelegramTransport` itself, enforced by
`test_no_openclaw_import_or_cli_invocation`) binds it to:

```python
def _production_notifier(result):
    return notify_if_required(
        result,
        transport=OpenClawTelegramTransport(),
        scheduler_native_failure_owned=False,
    )
```

### Fixed: the scheduler-native-alert ownership gap

An earlier version of this PR left a genuine no-alert gap. #28's
bounded-retry exhaustion persists a Morning Editorial provider failure as
`scheduler_status="error"` / `domain_outcome="FAILED"` -- a legacy field
value that predates #59 and originally meant "the scheduler itself failed;
let OpenClaw's own native `failureAlert` own it." `notify_if_required()`,
unmodified, read that field and deferred (`NOT_REQUIRED`,
`policy=SCHEDULER_NATIVE_FAILURE_ALERT`). But under the new #59 contract,
`nullone-scheduled-run.py morning` correctly reports
`application_execution=COMPLETED` (exit 0) for this exact case -- the
application safely established and validated the result. A process that
exits 0 never triggers OpenClaw's native `failureAlert`. The combination
was silent: no native alert (process exited 0) and no domain alert
(deferred to the native alert that would never fire).

The fix is a narrow, explicit, backward-compatible ownership override on
`notify_if_required` itself
(`nullone_failure_notify.py::notify_if_required`,
`scheduler_native_failure_owned: bool | None = None`):

- `None` (the default; every pre-existing caller, including
  `nullone-failure-notify-run.py` and all of `tests/test_failure_notify.py`
  that predate this parameter): behavior is **exactly unchanged** --
  ownership is still derived from the persisted record's own
  `scheduler_status`.
- `True`: the caller explicitly asserts native ownership regardless of
  `scheduler_status`'s content (this override is available for a future
  caller that has independently confirmed scheduler-level failure some
  other way; #59 does not currently pass `True` from anywhere).
- `False`: the caller explicitly asserts that *this* application
  invocation has itself completed (and will exit 0), so a legacy
  `scheduler_status="error"`/`"failed"` value must NOT suppress the domain
  alert. `_production_notifier` above always passes `False`, because it is
  only ever reached by `run_morning_workflow`/`run_analytics_workflow`
  after they have already established, validated, and reconciled a #27
  result -- i.e. exactly the condition under which the CLI will exit 0.

No copy of the persisted result with a rewritten `scheduler_status` is
ever constructed, and the on-disk #27 record is never mutated; the
override only steers which routing branch `notify_if_required` takes.

Effect: a real #28 Morning provider-failure occurrence
(`scheduler_status="error"`, `domain_outcome="FAILED"`) now reaches the
domain Telegram alert exactly once through the #59 CLI path
(`tests/test_scheduled_run_cli.py
::MorningNativeAlertOwnershipHardeningTests`), while a direct legacy call
to `notify_if_required` with no override (e.g. any future scheduler-level
integration that has not adopted #59) is completely unaffected
(`tests/test_failure_notify.py::ExplicitSchedulerOwnershipOverrideTests
::test_omitted_parameter_preserves_legacy_deferral_exactly`). Daily
Analytics, whose #29 BLOCKED/FAILED results already always carry
`scheduler_status="succeeded"`, is unaffected either way -- the override
changes nothing for a record that was never scheduler-native to begin
with.

### Other notification composition properties

- **Healthy results stay quiet**: `domain_outcome=SUCCEEDED` (including
  `empty_success=NO_DATA`/`NO_ACTION`) reaches `notify_if_required`, which
  returns `NOT_REQUIRED` and sends nothing -- the workflow still reports
  this truthfully via `notification_status`.
- **Actionable domain results notify once**: `BLOCKED`/`FAILED`/actionable
  `UNKNOWN` reach `SENT` on first delivery, `ALREADY_SENT`/
  `ALREADY_FAILED`/`ALREADY_UNKNOWN` on exact replay -- #30's own durable,
  locked idempotence is authoritative; no second attempt counter is added
  here.
- **Notifier failure/timeout is never auto-retried**: a `notify_if_
  required` transport timeout (`UNKNOWN`) or definite failure (`FAILED`)
  is a normal, non-exception, durably-recorded #30 return -- reported
  truthfully via `notification_status` with `application_execution`
  staying `"COMPLETED"`, since #30 itself already fails closed against a
  second automatic send attempt on re-entry.
- **Malformed notifier returns fail closed**: `run_morning_workflow`/
  `run_analytics_workflow` validate every non-exception notifier return
  through `nullone_scheduled_workflow_support.validate_notification_outcome`
  against the exact, small allowlist of statuses #30 is documented to
  return (`NOT_REQUIRED`, `SENT`, `FAILED`, `UNKNOWN`, `ALREADY_PENDING`,
  `ALREADY_SENT`, `ALREADY_FAILED`, `ALREADY_UNKNOWN`). `None`, a
  non-mapping, `{}`, a blank/`None` `status`, or any unrecognized status
  string (e.g. an invented `"SUCCESS"`) is `NOTIFICATION_RESULT_INVALID`
  -- `application_execution=FAILED`, no rerun, no #27 rewrite, no second
  attempt. Every one of #30's own legitimate outcomes above (including its
  fail-closed `FAILED`/`UNKNOWN`/`ALREADY_*` forms) passes through
  unaffected; this validation never invents a status #30 does not already
  document.
- Only a genuinely unsafe/corrupt on-disk notification state
  (`notify_if_required` raising `NotifierError`, or any other exception)
  is treated as an application-level orchestration failure
  (`NOTIFICATION_STATE_UNSAFE`) -- see "Critical scheduler-vs-domain
  separation" above for the reasoning and the exception-text-scrubbing
  guarantee.

As required by #37's own preflight notification requirement
(`docs/deployment/37-preflight-notification-requirements.md`), the
OpenClaw native `failureAlert` must be configured for both automations as
part of controlled #37 activation -- **not activated by #59**. This
document does not repeat or supersede that requirement; it only confirms
#59's notifier composition already assumes and preserves it, and closes
the specific gap the two would otherwise have interacted to create.

## OpenClaw job payloads: DESIRED / NOT DEPLOYED

The following describes the intended #37 activation shape only. No job
below has been created, edited, enabled, or disabled by #59.

### Morning Editorial (DESIRED / NOT DEPLOYED)

```text
normalized OpenClaw occurrence (job id 0666d47b-..., scheduled_for)
  -> nullone_openclaw_scheduler_adapter.map_openclaw_occurrence(workflow_id="morning-editorial", ...)
  -> nullone-scheduled-run.py morning --trigger-file <normalized trigger>
  -> MorningWorkflow -> #28 -> #27 -> #30
```

Would replace the current `agentTurn` payload
(`"Read social/ops/prompts/morning-editorial.md and execute it exactly."`)
with a `--command`/`--command-argv` job invoking this repository's CLI.
**This exact shape cannot be activated today**: per "OpenClaw trigger edge:
CONFIRMED BLOCKED" above, no OpenClaw 2026.8.2 command-payload mechanism
supplies `scheduled_for` to the invoked process, and this repository
deliberately does not substitute a heuristic for it. This is a distinct
architecture gap from #37's own scope (live job-payload migration/
activation presupposes a working wiring already exists) and is not
resolved by #37 alone.

### Daily Analytics (DESIRED / NOT DEPLOYED)

```text
normalized OpenClaw occurrence (job id 8e94064c-..., scheduled_for)
  -> nullone_openclaw_scheduler_adapter.map_openclaw_occurrence(workflow_id="daily-analytics", ...)
  -> nullone-scheduled-run.py analytics --trigger-file <normalized trigger>
  -> AnalyticsWorkflow -> #61 AnalyticsProvider wiring -> #29 -> #27 -> #30
```

Requires #61 to complete `build_production_analytics_provider` before
activation can produce anything other than
`PROVIDER_SECRET_WIRING_PENDING_61`.

Both desired jobs: exit 0 means orchestration completed regardless of
domain outcome (never inferred as domain success); exit non-zero is
reserved for a genuine orchestration-establishment failure; native
`failureAlert` (once configured under #37, not here) owns the
scheduler-execution-failure surface, never duplicated by #30.

## Current live limitations

Both live OpenClaw automations remain the legacy prompt-only `agentTurn`
jobs confirmed above; neither invokes `MorningWorkflow`/
`AnalyticsWorkflow`/`nullone-scheduled-run.py`. No Story-specific live job
exists either (unrelated to #59). `ZERNIO_ANALYTICS_API_TOKEN` remains
absent from the production environment (issue #61, unimplemented by
design here). Daily production activation of Daily Analytics requires
**#59 + #61**; Morning Editorial's production activation additionally
requires the #37 job-payload migration above. Natural live scheduled proof
of either workflow remains `UNPROVEN_LIVE / DEFERRED_TO_#37`; no synthetic
production event may be used to manufacture it.
