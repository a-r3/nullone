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

## Superseded design note: normalized-occurrence OpenClaw job payload

An earlier revision of this document proposed a `--command`/`--command-argv`
job that would receive an already-normalized occurrence (job id +
`scheduled_for`) and hand it straight to
`nullone_openclaw_scheduler_adapter.map_openclaw_occurrence` /
`nullone-scheduled-run.py --trigger-file`. That design is **rejected**: it
assumed OpenClaw could supply the intended `scheduled_for` to the
command-payload process, which "OpenClaw trigger edge: CONFIRMED BLOCKED"
above directly disproves for the installed OpenClaw 2026.8.2 package. It is
recorded here only so a reader does not rediscover and re-propose the same
disproven shape; the "NullOne Scheduled Occurrence Authority" section below
is the accepted replacement. `map_openclaw_occurrence` and
`nullone-scheduled-run.py --trigger-file` are not deleted -- see "Status of
`map_openclaw_occurrence` and the exact-trigger-file CLI" below for why both
remain valid, non-production-path code.

## NullOne Scheduled Occurrence Authority (#59 remaining scope)

Per the architecture decision recorded in `NULLONE_PROJECT_CONTEXT.md`
("OpenClaw trigger-edge architecture decision"): OpenClaw (or any future
external scheduler) is a **wake-up adapter only**. NullOne itself owns the
exact scheduled slot and computes `scheduled_for` deterministically from its
own repo-owned schedule config, not from anything the external trigger
supplies beyond "wake up now."

```text
OpenClaw --command job (or any other future scheduler adapter)
  -> WAKE-UP ONLY (nullone-scheduled-wakeup.py <workflow> --source <adapter>)
  -> nullone_scheduled_occurrence_authority.resolve_scheduled_occurrence
       (workflow_id, source, triggered_at=<observed UTC wake instant>)
  -> DUE: exact nullone.scheduler-invocation.v1, validated
     | NO_DUE_OCCURRENCE: no payload, no side effect
  -> nullone_scheduled_run_dispatch (shared with the exact-trigger-file CLI)
  -> MorningWorkflow / AnalyticsWorkflow -> #28/#29 -> #27 -> #30
```

### M0 schedule registry (`nullone_schedule_registry.py`)

Exactly two immutable, reviewed `ScheduleSpec` entries -- not a
cron-parsing engine, not a user-configurable store:

| `workflow_id` | `schedule_id` | `timezone_name` | `local_time` |
| --- | --- | --- | --- |
| `morning-editorial` | `morning-editorial.daily.v1` | `Asia/Baku` | `08:30:00` |
| `daily-analytics` | `daily-analytics.daily.v1` | `Asia/Baku` | `03:20:00` |

These match the two live jobs' own `schedule.expr`/`schedule.tz`
(`"30 8 * * *"` / `"20 3 * * *"`, both `Asia/Baku`) recorded above under
"OpenClaw scheduler edge: confirmed read-only evidence" -- NullOne's own
registry intentionally mirrors the currently reviewed/live cadence, it does
not invent a different one. A future schedule semantic change requires a
new `schedule_id` revision (e.g. `.v2`), never silent mutation of `.v1`'s
meaning, because `schedule_id` is part of `occurrence_id`'s stable identity
input (see below).

### Due-slot resolution algorithm (`nullone_scheduled_occurrence_authority.py`)

`resolve_scheduled_occurrence(*, workflow_id, source, triggered_at)` is a
pure function (no I/O, no persistence, no ledger):

1. Load the exact `ScheduleSpec` for `workflow_id` (fails closed if
   unsupported).
2. Convert `triggered_at` (canonical UTC) to the schedule's own
   `timezone_name`.
3. Take that observation's local calendar date.
4. Construct *that same local date's* configured `local_time` as the
   candidate scheduled instant.
5. If the observation is strictly before that instant: `NO_DUE_OCCURRENCE`.
   **Never** falls back to a previous local date's slot -- an early wake is
   simply early, not a signal to backfill a missed prior occurrence. This is
   an explicit NullOne schedule policy, not a heuristic; historical
   catch-up/backfill is out of M0 scope (deferred to #37 if ever needed).
6. Otherwise: today's slot is `DUE`. Convert it to canonical UTC
   `scheduled_for`, derive `external_occurrence_id =
   "<schedule_id>@<scheduled_for>"` (opaque, deterministic, contains no
   `triggered_at`/wall-clock/session data), compute `occurrence_id` via the
   existing #65 contract rule
   (`nullone_scheduler_invocation.compute_occurrence_id`, not duplicated),
   and return the resulting `nullone.scheduler-invocation.v1` payload
   already run through `validate_payload()` before any workflow sees it.

Because `scheduled_for`/`external_occurrence_id`/`occurrence_id` are
derived only from the resolved schedule slot -- never from `triggered_at`
-- any number of same-day calls (a natural wake, a delayed wake, a manual
replay) resolve to the identical occurrence identity; only `triggered_at`
and the diagnostic `lateness_seconds` differ between them. `triggered_at`
is used *only* as the observation instant that decides DUE vs. NO_DUE (and
as observational metadata in the emitted payload, per the existing #65
contract) -- it is never substituted into `scheduled_for`,
`external_occurrence_id`, or `occurrence_id` directly.

`lateness_seconds` (`triggered_at - scheduled_for`, on a `DUE` result) is
diagnostic only. M0 invents no automatic "too late, skip it" blocker;
timeliness policy/observation belongs to #37 unless the repository already
has an accepted threshold (it does not, as of this change).

### Worked proofs

Morning Editorial (`Asia/Baku`, UTC+4 year-round, no DST):

| `triggered_at` (UTC) | Result | `scheduled_for` |
| --- | --- | --- |
| `2026-09-08T04:29:59Z` | `NO_DUE_OCCURRENCE` | -- |
| `2026-09-08T04:30:00Z` | `DUE` | `2026-09-08T04:30:00Z` |
| `2026-09-08T05:07:00Z` (09:07 local -- the real observed #28 delayed-start case cited above) | `DUE`, same occurrence as `04:30:00Z` | `2026-09-08T04:30:00Z` |
| `2026-09-08T14:00:00Z` (manual replay) | `DUE`, same occurrence | `2026-09-08T04:30:00Z` |
| `2026-09-09T03:00:00Z` (next day, before slot) | `NO_DUE_OCCURRENCE` -- **not** a backfill of the prior day | -- |
| `2026-09-09T04:30:00Z` | `DUE`, a **new**, distinct occurrence | `2026-09-09T04:30:00Z` |

Daily Analytics UTC/Baku boundary (03:20 `Asia/Baku` crosses the UTC
calendar date backwards):

| `triggered_at` (UTC) | Result | `scheduled_for` | local scheduled date |
| --- | --- | --- | --- |
| `2026-09-08T23:19:59Z` | `NO_DUE_OCCURRENCE` | -- | -- |
| `2026-09-08T23:20:00Z` | `DUE` | `2026-09-08T23:20:00Z` | `2026-09-09` |

Cross-source identity divergence (the accepted #65 contract's own intent,
not weakened here): `source="openclaw"` and a hypothetical
`source="systemd-timer"` resolving the exact same slot produce the same
`scheduled_for` but a **different** `occurrence_id`, because `source`
participates in the existing `occurrence_id` derivation. This repository
does not force cross-adapter ID equality.

## `nullone-scheduled-wakeup.py` -- the static wake-up edge

```text
nullone-scheduled-wakeup.py morning --source openclaw
nullone-scheduled-wakeup.py analytics --source openclaw
```

No `scheduled_for` argument, no per-occurrence trigger file, no OpenClaw
per-run UUID, no job-run metadata, no dynamic env interpolation -- the
process needs only the reviewed adapter `--source` string and a
timezone-aware system clock (`triggered_at` derived from the injected
clock after UTC normalization; never a CLI flag).

### Authority vs current M0 executable (source namespaces)

```text
authority = generic (multi-adapter; source participates in #65 occurrence_id)
current production wake-up CLI = reviewed-source allowlist (exactly openclaw)
```

`resolve_scheduled_occurrence(..., source=...)` remains scheduler-independent:
the pure-authority contract still permits alternate namespaces such as
`systemd-timer` for the same slot (same `scheduled_for`, different
`occurrence_id`). The **current M0 production wake-up executable** does not
expose that genericity as an arbitrary CLI string. It pins
`M0_WAKEUP_SOURCES = frozenset({"openclaw"})` and fails closed with
`STATUS=WAKEUP_REJECTED` / `REASON_CODE=WAKEUP_SOURCE_UNSUPPORTED` before
occurrence resolution and before any workflow/provider/notifier/#27 call
when `--source` is anything else (including typos, padding, empty string,
or a hypothetical future adapter name). Expanding the allowlist requires an
explicit reviewed code/config change. There is no `.strip()` /
case-fold fallback that turns malformed input into `openclaw`.

### Strict clock contract on the wake-up edge

The injected clock must return a timezone-aware `datetime` (`tzinfo`
present and `utcoffset()` not `None`). Aware non-UTC values are normalized
deterministically to UTC. Naive datetimes and non-datetime returns fail
closed with `STATUS=WAKEUP_REJECTED` / `REASON_CODE=WAKEUP_CLOCK_INVALID`
(non-zero exit; zero dispatch/provider/notifier/#27). The edge never
interprets a naive clock via host-local timezone and never assumes UTC for
naive values. Operator rejection output uses stable reason codes only -- it
does not echo arbitrary source strings or clock/object reprs.

Exit-code contract:

- Infrastructure rejection (`WAKEUP_SOURCE_UNSUPPORTED` /
  `WAKEUP_CLOCK_INVALID` / authority reject): non-zero. No workflow side
  effect.
- `NO_DUE_OCCURRENCE`: exit 0. No provider call, no notifier call, no #27
  result fabricated.
- `DUE` + `application_execution=="COMPLETED"`: exit 0, regardless of
  domain health -- identical to `nullone-scheduled-run.py`'s existing
  scheduler-vs-domain rule.
- `DUE` + `application_execution=="FAILED"`: non-zero. No new retry layer.

It shares `nullone_scheduled_run_dispatch.py`'s `run_morning_trigger`/
`run_analytics_trigger` production wiring with `nullone-scheduled-run.py`
(extracted from that CLI's own previous in-file wiring so neither
duplicates it) -- both entrypoints therefore invoke `MorningWorkflow`/
`AnalyticsWorkflow` with the identical production Claude CLI provider,
`AnalyticsProvider` factory, and OpenClaw/Telegram notifier binding.

### Manual OpenClaw runs

Because a manual `openclaw automations run <job-id>` force-run goes through
the identical `runCronCommandJob` path as a scheduled run (see "OpenClaw
scheduler edge" above), a manual wake-up through this exact same static
command cannot mint a novel arbitrary occurrence:

- **Manual run before today's slot** -> `NO_DUE_OCCURRENCE`, identical to a
  natural early wake.
- **Manual run after today's slot** -> resolves to the exact same occurrence
  as today's scheduled/replayed wake (same `scheduled_for`/
  `external_occurrence_id`/`occurrence_id`); #28/#29's own persisted-result
  idempotence prevents any duplicate side effect.

### Repeated and concurrent wake-ups

Repeated same-day wake-ups (08:30, 09:07, 10:15, ...) resolve to the same
occurrence identity and replay through #28/#29's own existing persisted-
result/lock semantics: the provider is not repeated after a completed
result, and the notifier does not resend beyond #30's own at-most-once
guarantee. Concurrent wake-ups resolving the same slot rely on that exact
same existing runtime lock -- this module adds no second lock or replay
cache of its own (`tests/test_scheduled_wakeup_cli.py
::ConcurrentWakeUpsRunOneLogicalCycleTests` proves one logical provider
cycle and one persisted result across four concurrent calls).

## Status of `map_openclaw_occurrence` and the exact-trigger-file CLI

`nullone_openclaw_scheduler_adapter.map_openclaw_occurrence` remains a
valid, tested, pure mapping function -- **reference/legacy exact-receipt
mapper**, not the OpenClaw 2026.8.2 production path. It is not deleted:
it stays correct for any future caller that genuinely already possesses an
exact `scheduled_for` from some other reviewed source (e.g. a one-shot
manual backfill tool, or a future scheduler mechanism that does supply the
instant directly), and its own self-test/fixture coverage is unaffected by
this change. `nullone-scheduled-run.py --trigger-file` similarly remains
valid for any caller that already holds a normalized
`nullone.scheduler-invocation.v1` trigger file; it is not the production
OpenClaw path either, and `nullone-scheduled-wakeup.py` is the one this
document recommends for #37 activation.

## OpenClaw job payloads: DESIRED / NOT DEPLOYED

The following describes the intended #37 activation shape only. No job
below has been created, edited, enabled, or disabled by #59. Exact
OpenClaw 2026.8.2 `automations create` syntax (`docs/automation/cron-jobs.md`
"Command payloads": `--command <shell>` stores `argv: ["sh", "-lc",
<shell>]`; schedule flags `--cron`/`--tz` documented under "Recurring
schedules"), verified against the installed 2026.8.2 package on
2026-09-07, no secrets/private job IDs, no dynamic `scheduled_for`
placeholder of any kind -- both commands below are fully static:

### Morning Editorial (DESIRED / NOT DEPLOYED)

```bash
openclaw automations create "30 8 * * *" \
  --name "nullone-morning-editorial-wakeup" \
  --command "python3 <repo>/workspace/social/ops/scripts/nullone-scheduled-wakeup.py morning --source openclaw" \
  --command-cwd "<repo>" \
  --tz "Asia/Baku"
```

Would replace the current `agentTurn` payload
(`"Read social/ops/prompts/morning-editorial.md and execute it exactly."`).
Unlike the superseded normalized-occurrence design above, this exact
command **can** be activated once #37 reviews it: it requires no
scheduler-supplied `scheduled_for` at all, since
`nullone-scheduled-wakeup.py` resolves the exact due slot itself. It is not
created/edited/enabled by this change. The positional cron argument form
above is the reviewed OpenClaw 2026.8.2 shape retained here.

### Daily Analytics (DESIRED / NOT DEPLOYED)

```bash
openclaw automations create "20 3 * * *" \
  --name "nullone-daily-analytics-wakeup" \
  --command "python3 <repo>/workspace/social/ops/scripts/nullone-scheduled-wakeup.py analytics --source openclaw" \
  --command-cwd "<repo>" \
  --tz "Asia/Baku"
```

Requires #61 to complete `build_production_analytics_provider` before
activation can produce anything other than
`PROVIDER_SECRET_WIRING_PENDING_61`.

Both desired jobs: exit 0 means orchestration completed regardless of
domain outcome (never inferred as domain success), or that there was
simply no due occurrence yet (also exit 0); exit non-zero is reserved for
a genuine orchestration-establishment failure; native `failureAlert` (once
configured under #37, not here) owns the scheduler-execution-failure
surface, never duplicated by #30.

The OpenClaw `--cron`/`--tz` values above (`"30 8 * * *"`/`"20 3 * * *"`,
both `Asia/Baku`) intentionally match `nullone_schedule_registry.py`'s own
`ScheduleSpec` values exactly -- but the external cron exists **only** to
wake the process near that slot; NullOne's own registry remains
authoritative for the exact `scheduled_for` it computes. A mismatch between
the external cron and the NullOne registry (e.g. someone edits one without
the other) would not corrupt occurrence identity -- the authority always
computes the exact configured NullOne slot for whatever calendar date it is
woken on -- but it could cause a very early or very late first wake-up on a
given day; #37's own preflight is the place to detect such a mismatch
before activation, not a runtime check this module adds.

### Cutover safety

Current M0 executable permits only:

```text
source=openclaw
```

A future adapter namespace (for example `systemd-timer`) requires an
explicit reviewed implementation/change to `M0_WAKEUP_SOURCES` (or a
dedicated reviewed adapter edge). The allowlist reduces accidental misuse
of the production wake-up CLI; it does **not** redefine #65 `source`
semantics at the generic authority/contract layer.

Because the accepted #65 contract intentionally namespaces `occurrence_id`
identity by `source` (see "Alternate scheduler example" in
`docs/contracts/scheduler-invocation-v1.md`), **never concurrently activate
two adapter namespaces for one workflow/slot** -- e.g. both an `openclaw`
wake-up job and a hypothetical `systemd-timer` wake-up simultaneously
enabled for Morning Editorial would each independently resolve the same
`scheduled_for` but mint a different, non-colliding `occurrence_id`, so
#28's own per-occurrence lock would not prevent two independent runs. A
controlled adapter cutover (retiring one `source` before/while enabling
another) must occur at a reviewed safe schedule boundary under #37; this
document does not implement a cutover or a systemd adapter here.

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
