# Spec: dedicated scheduled publish reconciliation job (PROPOSAL — not deployed)

Status: **specification for human review.** No scheduler job exists, none is
created by this PR, and no code changes.

## Problem (verified 2026-10-19)

Nine manifests are local `publication.state=PUBLISHING` (attempts=1, no
`platform_post_id`, two-step approval recorded, receipts `SETTLED_PUBLISHING`)
while read-only Zernio `posts_get` reports `published` for all nine. This is
the by-design outcome of `nullone-publish-bridge.py` (one PUT, one readback,
settle) combined with asynchronous Zernio delivery; the GET-only reconciler
`nullone-publish-reconcile.py` (PR #170/#181) is deployed but only ever run by
hand. It is an operational gap, not a publish-retry bug.

## Audit of `nullone-publish-reconcile.py scan` (origin/main)

**Remote operations.** Only `ZernioPublishReadOnlyReconciler.readback` — exactly
one `GET /v1/posts/{postId}` per eligible manifest. The class has no PUT/POST
method; it uses the distinct `zernio.publish.reconcile.bearer` secret.

**Eligibility.** `publication.attempts == 1` and `publication.state ==
"PUBLISHING"` only (`RECONCILABLE_STATES`); `last_checked_at` at least
`--min-stale-seconds` (default 900) old; `review.zernio_draft_id` present.
Oldest first, bounded by `--max-items` (default 5). `PUBLISHED` short-circuits
to `NOOP_ALREADY_PUBLISHED` before any call; `NOT_REQUESTED`,
`PUBLISH_IN_FLIGHT`, `FAILED`, `UNKNOWN`, `READBACK_FAILED`, `CHECK_REQUIRED`
are never reconciled.

**Outcomes.** `RECONCILED_PUBLISHED`, `RECONCILED_FAILED`, `STILL_PENDING`
(remote still publishing; no change), `CHECK_REQUIRED` (credential/readback
ambiguous or audience time unproven; no change, never fabricated), `BLOCKED`
(ineligible), `NOOP_ALREADY_PUBLISHED`.

**Local mutations, only after proven remote truth** (under `review_post_lock`,
manifest re-read inside the lock): manifest `publication` state/permalink/
`platform_post_id`/audience `published_at` via the bridge's `_persist_state`;
one `record_publication_event` ledger row (deduplicated); on PUBLISHED,
`mark_queue_published_exact` (queue entry matched by exact topic line);
best-effort convergence of `SETTLED_PUBLISHING` receipts to
`SETTLED_PUBLISHED`/`SETTLED_FAILED`. `attempts` is never changed.

**Duplicate-publish impossibility.** No code path in the module can issue a
PUT/POST; attempts is required to be 1 and is never incremented; second
confirmation is untouched (the reconciler neither reads nor writes `approval`).
**Idempotent:** after settlement the state is `PUBLISHED`/`FAILED`, so reruns
are `NOOP`/`BLOCKED` with no external call.

## Findings that block a naive `cron` wiring

1. **Secret loading.** The provider factory uses `EnvironmentSecretProvider`;
   the credential lives in `~/.config/nullone/secrets/zernio-publish-reconcile.env`
   and is intentionally NOT in the Gateway environment. A scheduled job needs a
   reviewed wrapper that loads that file for that one process only
   (no secret in job config, argv or logs).
2. **Always-green exit code.** `scan` prints results and returns 0 even when
   items are `BLOCKED`/`CHECK_REQUIRED`/`STILL_PENDING`; there is no
   run-outcome/summary contract, so a scheduled job could not distinguish
   "nothing to do" from "stuck". A deterministic summary line
   (`SCANNED/RECONCILED/PENDING/CHECK_REQUIRED/BLOCKED` counts) and a distinct
   non-zero exit for infrastructure failure are required first.
3. **Bound.** Default `--max-items 5`; the current backlog (9) takes two runs.
4. **Not covered.** `CHECK_REQUIRED`/`READBACK_FAILED`/`UNKNOWN` manifests (older
   September items) remain manual by design.

## Proposed architecture

A **dedicated** OpenClaw command job — not part of Story, Morning, Daily or
any wake-up (those must stay Zernio-agnostic, see
`tests/test_scheduled_workflows_capability_negative.py`):

- name: `nullone-publish-reconcile`, `agentId: main`, isolated session,
  `delivery.mode: none`
- command: a small reviewed wrapper (follow-up PR) that sources the reconcile
  secret file into the child process only and runs
  `nullone-publish-reconcile.py scan --max-items 5 --min-stale-seconds 900`
- cadence: `20,50 * * * *` Asia/Baku (every 30 min, offset from the
  `:30/:45` Radar/Consumer/Story/Draft slots). Quiet when nothing is
  `PUBLISHING` (zero remote calls). A newly published post is normally
  reconciled within ~30–60 min.
- never initiates publication, never calls PUT, never resets attempts, never
  bypasses second confirmation (all enforced by the module above).

## Required before enabling (follow-up work, each its own reviewed PR)

1. Wrapper + deterministic summary/exit contract + offline tests.
2. Capability-negative test proving the job path contains no publish client.
3. Controlled deployment record (backup, hashes) and a dry first manual
   `scan --max-items 1` observed by the owner.
4. Only then create the OpenClaw job (owner-authorised scheduler change).

## Safe to schedule?

**Not yet.** The reconciler itself is safe to run repeatedly; the missing
pieces are items 1–2 above (secret loading and an honest outcome contract).
