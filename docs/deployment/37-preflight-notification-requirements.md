# #37 preflight requirement: scheduler-native execution-failure alerts

Status: repo-level requirement only. No production change has been made
by writing this document. This is deliberately scoped narrower than a
full #37 preflight checklist — it records only the scheduler-native
`failureAlert` activation requirement that issue #30 identified and
could not implement here.

## Why this is a deployment-time requirement, not repo config

Issue #30 (`Send concise Telegram alerts for meaningful workflow
failures`) covers two distinct failure surfaces:

1. **Domain/business failures** — a workflow's own structured #27 run
   outcome is `BLOCKED`, `FAILED`, or actionable `UNKNOWN` even though
   the scheduler reports success. This is implemented in this
   repository: `workspace/social/ops/scripts/nullone_failure_notify.py`.
2. **Scheduler/execution failures** — the OpenClaw scheduler itself
   marks a run `error`/`failed` (e.g. the confirmed 2026-09-05 Morning
   Editorial `ENOTFOUND`/timeout occurrences). OpenClaw already has a
   native `failureAlert` mechanism for this surface, and #30's direction
   is to reuse it rather than build a second, redundant cron-error
   notifier.

This repository was inspected for a Git-tracked, declarative
OpenClaw automation/job configuration file (the kind of thing that
would let `failureAlert` be authored here and reviewed like any other
change). None exists: no automation/cron config of any kind is tracked
in this repository as of 2026-09-06. OpenClaw's `failureAlert` schema
and activation are confirmed only to exist as **live runtime job
configuration**, reachable through `openclaw cron get/set` against a
running Gateway — not through a file this repository owns.

Per the #30 scope boundary: **do not mutate production to activate
this**, and **do not guess the exact `failureAlert` field shape** from
memory. Both would violate the repo/production separation this project
maintains (see `NULLONE_PROJECT_CONTEXT.md`). This requirement is
therefore recorded here for #37 to execute and validate as a controlled
production change, using OpenClaw's own current documentation/CLI help
at that time to get the exact schema right.

## Confirmed current state (read-only, 2026-09-06)

From `docs/reliability/2026-09-proof-verdict.md`:

- `openclaw cron get` for both `morning-editorial` and `daily-analytics`
  shows **no `failureAlert` field at all**.
- `delivery.mode=none` for both jobs.
- `lastFailureNotificationDeliveryStatus=not-requested` for both jobs,
  for the full 2026-09-04 → 2026-09-06 proof window.
- `openclaw config get cron` shows no authored global `cron` config
  (runtime default applies).

## Exact requirement for #37

When #37 performs controlled production activation, it must, as part
of that same controlled change:

1. Query the live OpenClaw Gateway's current `failureAlert`
   configuration schema for a cron/automation job (e.g. via
   `openclaw cron --help` / `openclaw automations --help` against the
   actual running version at deployment time — do not assume the shape
   from this document or from general OpenClaw familiarity, since it is
   not declared anywhere in this repository).
2. Configure a `failureAlert` (or equivalent native delivery
   configuration) for both the `morning-editorial` and `daily-analytics`
   automations, delivering to the same Telegram owner target already
   used by publication-result notifications
   (`workspace/social/ops/private/telegram-owner-id`, not committed).
3. Choose a dedupe/threshold/cooldown consistent with whatever OpenClaw
   actually supports at that time — do not invent a cadence not backed
   by the real schema. At minimum, avoid alert storms for a single
   sustained outage (e.g. the confirmed 2026-09-05 Morning Editorial
   case: 4 scheduler-level attempts, all `ENOTFOUND`/timeout, in roughly
   40 minutes).
4. Verify with a read-only `openclaw cron get` (or equivalent) that
   `failureAlert`/delivery is actually present after activation — the
   same class of query already used to *detect* the current gap must be
   reused to *confirm* the fix, rather than trusting the change was
   applied.
5. Confirm this does **not** duplicate the domain/business alert path.
   This is not merely a documentation claim: `nullone_failure_notify.py`
   itself enforces the routing (see "Enforced ownership routing" below),
   so activating this requirement at #37 cannot cause a double alert for
   one incident — the domain notifier already, unconditionally, defers
   to this native alert whenever `scheduler_status` indicates a
   scheduler-level execution failure. #37 only needs to confirm the
   native alert actually fires for that same case; it does not need to
   (and should not) modify `nullone_failure_notify.py` to avoid overlap.

## Enforced ownership routing

The overlap between these two surfaces is not just described here — it
is enforced in code, in `workspace/social/ops/scripts/nullone_failure_notify.py`:

- `SCHEDULER_NATIVE_FAILURE_STATUSES = frozenset({"error", "failed"})`
  names the `scheduler_status` values (case-insensitive) that mean
  OpenClaw's own scheduler already recorded this occurrence as an
  execution failure.
- `_is_scheduler_native_execution_failure(result)` checks a result's
  `scheduler_status` against that set. This is an ownership/routing
  check only — it never substitutes for `domain_outcome` as the source
  of business-health truth, and `is_actionable()` (which decides
  actionability from `domain_outcome` alone) is unaffected by it.
- `notify_if_required()` calls this check immediately after determining
  actionability. When it is true, the function returns
  `{"status": "NOT_REQUIRED", "policy": "SCHEDULER_NATIVE_FAILURE_ALERT"}`
  and creates **no notification state at all** — no lock file, no
  record, no outbound call — deferring entirely to the native alert
  this document requires #37 to activate.

Concretely, today's confirmed Morning Editorial execution-failure shape
(`nullone_editorial_runtime.py`'s retry-exhausted path: `scheduler_status="error"`,
`domain_outcome="FAILED"`) is quiet in the domain notifier for exactly
this reason. `tests/test_failure_notify.py` covers this with:
`scheduler_status="error"` + `FAILED`, and `scheduler_status="failed"` +
`FAILED`, both asserting zero sends and no notification-state file
created; and, as a contrast case, `scheduler_status="succeeded"` +
`FAILED` (the Daily Analytics shape) still sends exactly once, proving
the routing is genuinely conditional on `scheduler_status` and not a
blanket suppression of `FAILED`.

## What #30 already delivers without this

Domain/business failure alerting (`nullone_failure_notify.py`) does not
depend on this requirement at all: it consumes the #27 run-outcome
record directly and is independent of whatever the scheduler's own
execution-failure delivery configuration is. The 2026-09-05/06 Daily
Analytics symptom this proof identified — scheduler `ok/succeeded`
masking a domain `BLOCKED` result — is exactly the case a
scheduler-native `failureAlert` cannot catch on its own, since the
scheduler itself saw no failure.

This document exists only for the other half: a true scheduler-level
execution failure (like the Morning Editorial `ENOTFOUND` incident)
producing no operator-visible signal today beyond manual
`openclaw automations list` inspection.
