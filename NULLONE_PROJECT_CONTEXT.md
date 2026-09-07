# NULLONE_PROJECT_CONTEXT

Last updated: 2026-09-07 Asia/Baku
Status: canonical project context for repository/project continuity. Production deployment of this document is NOT PERFORMED.

## Identity
Public brand: NullOne
Instagram: @nullone.az
Bio: Qısa. Dəqiq. Aktual.
Official spelling always NullOne.

Stable internal infrastructure identifiers containing `texbrif` remain intentional until a deliberate migration.

Conflict priority:
1. newest explicit decision from Rauf
2. this context
3. older project chats/files

## Primary objective
First make the media workflow reliable:

`monitor → verify → score → draft → render → Zernio draft → Telegram preview → human approval/revise/reject → second publish confirmation → publication → result`

No blind autonomous publishing.

## Safety invariants
- `VERIFICATION: PASS` required for publication-ready content
- two-stage human approval
- max one publication attempt
- timeout/ambiguity → `UNKNOWN`
- never auto-retry ambiguous publication
- notifier failure never triggers publication retry
- approved content and media are immutable
- GitHub merge ≠ production deployment
- production actual state and GitHub desired state must be kept separate

## Current execution priority
Operational workstreams remain ahead of large migration/product work:

Component-level repo engineering for #27–#36 is complete. Deployable
end-to-end M0 integration is not complete. Repository-level
`BreakingWorkflow` implementation is complete: #63 is CLOSED/COMPLETED and
PR #71 squash-merged as `34b35cba7bea0c366096bfbfd5d7141743c87189`.
No production activation occurred. The remaining M0 repository blockers are
exactly #59 and #61. A repository-level #59 **foundation**
(`MorningWorkflow`/`AnalyticsWorkflow`, the OpenClaw pure occurrence-mapping
function, and the `nullone-scheduled-run.py` CLI) is merged: PR #73
squash-merged as `cec185f9a62c2055175454f06d3d5c54596bba23`. This merge is
**partial and deliberately did not close #59**: #59 remains OPEN because
the OpenClaw exact-occurrence trigger edge is `CONFIRMED BLOCKED` (direct
2026-09-07 source inspection of the installed OpenClaw 2026.8.2 package
proved command-payload jobs never receive the intended scheduled
occurrence through argv/env/stdin — see "OpenClaw trigger-edge
architecture decision" below and
`docs/deployment/59-scheduled-workflows-deployment.md`). #61
remains fully unimplemented (its AnalyticsProvider secret-wiring seam is
established by the merged #59 foundation but deliberately left as a
fail-closed `PROVIDER_SECRET_WIRING_PENDING_61` placeholder). No
production/OpenClaw job/Zernio/Telegram/Claude action occurred while
implementing or merging #59's foundation.

1. #59's repository foundation is merged (PR #73); its remaining scope is
   a separately reviewed OpenClaw-trigger-edge replacement architecture
   (see "OpenClaw trigger-edge architecture decision" below) — not yet
   started. Implement and merge that remaining #59 scope, and implement
   and merge #61, independently, each through normal review.
2. Rerun the strictly read-only #37 preflight only after #59 and #61 are
   accepted (the earlier #65/#60/#62/#63/#66 dependencies are complete).
3. Only if the verdict is `READY_FOR_CONTROLLED_DEPLOYMENT`, perform the
   controlled deployment under #37.
4. Observe natural production behavior; do not manufacture live proof.

Required order:

`#59 (remaining trigger-edge scope) + #61 → review/merge → repeat strictly read-only #37 preflight → only READY_FOR_CONTROLLED_DEPLOYMENT permits deployment`

No ad-hoc deployment is permitted.

Existing M1/M2/M3 remain intact.

New operational milestone:
`M0 — Production operations are healthy and timely`

## OpenClaw trigger-edge architecture decision (#59, 2026-09-07)

`nullone_openclaw_scheduler_adapter.map_openclaw_occurrence` (merged by PR
#73) is a pure, fail-closed mapping function only; it has no live caller.
Direct, read-only source inspection of the installed OpenClaw **2026.8.2**
package (`dist/server-cron-DtqkVgKM.js`'s `runCronCommandJob`,
`dist/cron-cli-DqFGSvBK.js`'s `parseCronCommandEnv`, and
`docs/automation/cron-jobs.md`) confirmed that a scheduled command-payload
job's process receives only static, job-authored `argv`/`env`/`input`
(captured once at `automations create`/`edit` time); the scheduler's own
`runAtMs` is tracked solely for its own diagnostics and is never merged
into that process. A manual force-run goes through the identical code
path. Full citation trail: `docs/deployment/59-scheduled-workflows-
deployment.md`'s "OpenClaw trigger edge: CONFIRMED BLOCKED" section.

```text
#59_OPENCLAW_TRIGGER_EDGE = BLOCKED
reason = EXACT_INTENDED_SCHEDULED_OCCURRENCE_NOT_EXPOSED_TO_COMMAND_PAYLOAD
```

**Current explicit architecture decision, recorded here for continuity:**

> OpenClaw must not be treated as authoritative source of `scheduled_for`
> for Morning/Daily in OpenClaw 2026.8.2.
>
> The next #59 design must make the external scheduler a wake-up adapter
> while NullOne owns deterministic scheduled-occurrence authority, unless
> another exact reviewed scheduler mechanism is proven first.

This is a major architecture continuity decision, not yet implemented. No
schedule ledger, occurrence-claim state, systemd adapter, cron-inference
mechanism, new OpenClaw payload, or wake-up dispatcher exists in this
repository as of this entry; that replacement architecture is explicitly
deferred to a separate, later, separately reviewed task. Do not infer that
any such implementation exists from this decision record alone.

## GitHub planning / engineering state
Repository: `a-r3/nullone`
Current visibility: **public** as of 2026-09-05 18:34 so the ChatGPT GitHub connector can inspect it. This was described by Rauf as being made public for connector access; do **not** infer that the long-term private-repo policy was permanently abandoned unless Rauf explicitly confirms that.
Local clone: `~/nullone-repo-staging`

Verified planning:
- M1/M2/M3 exist
- M0 exists as milestone #4
- canonical planning issues #3–#14 exist; #3, #4 and #5 are now CLOSED (PR #46, #38, #39 respectively), while the remaining applicable planning issues stay open
- accidental duplicates #15–#26 closed
- operational component issues #27–#36, application-runtime architecture issue #65, cadence compatibility #60, Story workflow/review delivery #62, BreakingWorkflow orchestration #63, and the narrow legacy-publication-instruction blocker #66 are CLOSED/COMPLETED; the remaining M0 repository blockers are exactly #59 and #61, while parent deployment issue #37 remains OPEN; #6 remains OPEN in M1 and #13 remains OPEN in M3
- native dependencies created
- Project #5 exists
- GitHub Project field ordering for some M0 items may remain UI-housekeeping due transient GraphQL secondary rate limiting; this is not an engineering blocker

Relevant issues:
- #3 reliability proof/anomaly investigation — CLOSED/COMPLETED (PR #46 squash-merged as `875eeb715cac3c933b29694fec3c07fba094a39e`, verified via `gh issue view 3`/`gh pr view 46` on 2026-09-06); the recorded FAIL verdict and its criterion counts (PASS 4 / FAIL 4 / NOT_EXERCISED 7) are unchanged by this closure and still gate #37
- #4 executable completion/publication contracts — CLOSED; PR #38 squash-merged as `417cf400157dea95e19d8eb0a860c7bcb974e6e7`
- #5 offline behavioral regression tests — CLOSED; PR #39 squash-merged as `72e5c31e5bb3db922f30a2f8ea91c5b2d7ef8b41`
- #27 explicit domain run outcomes/health — CLOSED; PR #40 squash-merged as `c70047a9e3123d19b46968715c3fc294a51d69d4`
- #28 Morning Editorial network failure behavior — CLOSED; PR #42 squash-merged as `dee4ce1b3fc2ee9285454ea71d23b5eb63a76728`
- #29 Daily Analytics Zernio access/runtime bootstrap — CLOSED; PR #44 squash-merged as `d5db8ff0b907c0ea43b58da27f08c2d47eb94151`; production NOT deployed
- #30 Telegram failure alerts — CLOSED/COMPLETED; PR #47 squash-merged as `31ac4cca9e4255d5ba665ea42989ab9237eb05c2` (was `feature/n30-telegram-failure-alerts`); this is repo-level only — scheduler-native OpenClaw `failureAlert` is NOT activated in production, the domain notifier is NOT wired into any live scheduled job, and no real Telegram/live scheduled validation has occurred; see "Verified #30 completion" below
- #31 cadence contract decision — CLOSED/COMPLETED; PR #49 squash-merged as `a9334e27576c04f37535e05e8b6bd08e45606ffa`; decision/contract document only, no controller implementation (see "Verified #31 completion" below)
- #32 cadence controller — CLOSED/COMPLETED; PR #52 squash-merged as `a5fc6c69f5133baa9a807fe725f79c1a7ae5d96c`; deterministic controller implementation of the #31 contract, repo-level only (see "Verified #32 completion" below)
- #33 Story draft pipeline — CLOSED/COMPLETED; PR #55 squash-merged as `7404501bdafb224f221bf8c79ee67bf7182bb2f7`; trigger-agnostic repo-level Story production core behind human approval, with no production activation or publication capability (see "Verified #33 completion" below)
- #34 breaking policy decision — CLOSED/COMPLETED; PR #50 squash-merged as `33bd7c9114ecaeda675f1565a80268541c95dd68`; decision/contract document only, no identity/dedup or routing implementation (see "Verified #34 completion" below)
- #35 breaking identity/dedup — CLOSED/COMPLETED; PR #53 squash-merged as `0b0679c2d5aac98d777da34e2257526e9d9a09b5`; identity/dedup/follow-up-suppression implementation of the #34 policy, repo-level only (see "Verified #35 completion" below)
- #36 breaking draft routing — CLOSED/COMPLETED; PR #57 squash-merged as `36f358a539fedf90e0c5cffda9b503b87594e3f1`; deterministic router, durable Story-first draft-set dispatcher, review-only Feed/Carousel main pipeline, strict routing-artifact boundary, Telegram SENT proof, and #35 multi-manifest hardening are complete at repo level only (see "Verified #36 completion" below)
- #37 controlled production activation/validation — OPEN; its historical 2026-09-07 read-only preflight returned `BLOCKED`; it is not `READY`, and deployment remains prohibited while #59 and #61 remain open and until a repeated strictly read-only preflight returns `READY_FOR_CONTROLLED_DEPLOYMENT`
- #59 NullOne scheduled workflow orchestration — **OPEN** (repository foundation merged, issue deliberately left open); depends on #65 and binds normalized scheduler invocations to `MorningWorkflow`/`AnalyticsWorkflow`, existing runtimes, persisted #27 outcomes and #30 domain notification; OpenClaw is the first trigger adapter only. Repository-level foundation merged via PR #73, squash SHA `cec185f9a62c2055175454f06d3d5c54596bba23` (see `docs/deployment/59-scheduled-workflows-deployment.md`): `nullone_morning_workflow.py`/`nullone_analytics_workflow.py` reload and validate the exact persisted #27 result before invoking the unmodified #30 `notify_if_required`, and the new `nullone-scheduled-run.py` CLI separates scheduler/application exit status from domain health (exit 0 even for `BLOCKED`/`FAILED`/actionable `UNKNOWN`, provided a valid #27 result was established). Daily Analytics production activation additionally requires #61 (`PROVIDER_SECRET_WIRING_PENDING_61` fail-closed placeholder until then). The OpenClaw trigger edge itself is `CONFIRMED BLOCKED` (2026-09-07, direct source inspection of the installed OpenClaw 2026.8.2 package): no command-payload job mechanism supplies the intended `scheduled_for` occurrence to the invoked process (`payload.env`/`payload.input`/`payload.argv` are static values captured at job-authoring time; `runAtMs` is never merged in), so `map_openclaw_occurrence` correctly has no live caller yet and this repository does not substitute a heuristic — see `docs/deployment/59-scheduled-workflows-deployment.md`'s "OpenClaw trigger edge: CONFIRMED BLOCKED" section and this document's "OpenClaw trigger-edge architecture decision" section above. This is distinct from #37/#61 and is not resolved by either. #59 stays open specifically for the remaining OpenClaw-trigger-edge replacement architecture work (not yet started); no production/OpenClaw job/Zernio/Telegram/Claude action occurred; OpenClaw job payloads remain `DESIRED / NOT DEPLOYED`.
- #60 cadence-state backward compatibility — CLOSED/COMPLETED; its merged read-only compatibility implementation supplies the authoritative load behavior reused by #62/#63; no production ledger migration was performed or required (`migration_required: False`)
- #61 secure scheduled analytics credential injection — OPEN; depends on #65 and separates the application `AnalyticsProvider`/secret boundary from the current systemd/OpenClaw environment adapter that supplies the non-committed `ZERNIO_ANALYTICS_API_TOKEN`
- #62 `StoryWorkflow` and shared review-delivery adapter — CLOSED/COMPLETED; PR #70 squash-merged as `8d6d9844f0c494e3a813180fb7e83e87f713e745`; live scheduled Zernio/Telegram proof remains unproven
- #63 `BreakingWorkflow` orchestration — CLOSED/COMPLETED; PR #71 squash-merged as `34b35cba7bea0c366096bfbfd5d7141743c87189`; repository-level implementation is complete and preserves the exact #34/#36 verification vocabulary, defers optional-main preparation until after Story review delivery succeeds, derives candidate-level Radar occurrences, and emits contract-safe #27 mappings; no production activation occurred, strict Radar handoff remains `DESIRED / NOT DEPLOYED`, natural Breaking live proof remains `UNPROVEN_LIVE / DEFERRED_TO_#37`, and the scheduled Zernio path remains `LIVE_SCHEDULED_PATH_UNPROVEN`
- #65 NullOne Application Runtime architecture — CLOSED/COMPLETED; the accepted contract establishes NullOne workflow ownership and replaceable provider adapters
- #66 remove reachable legacy publication instructions/capabilities — CLOSED/COMPLETED; no production activation was implied by repository completion

### Verified #5 completion — 2026-09-05 18:32–18:34
PR #39 `Add isolated behavioral regression tests to CI` was verified with local authenticated `gh` and, after the repository became public, independently visible through the GitHub connector.

Verified merge state:
- PR #39: MERGED
- head SHA: `ecb4dd3d2f2601f6b54e0cd10d7a5068ccc4bd32`
- CI run `33971670816`: completed / success
- squash merge commit: `72e5c31e5bb3db922f30a2f8ea91c5b2d7ef8b41`
- issue #5: CLOSED
- local `main`: `72e5c31e5bb3db922f30a2f8ea91c5b2d7ef8b41`
- `origin/main`: `72e5c31e5bb3db922f30a2f8ea91c5b2d7ef8b41`
- production deployment: NOT PERFORMED
- reliability proof: UNTOUCHED

#5 added one documented offline command: `python3 tests/run_offline.py`. The suite currently passes 11 behavioral tests plus acceptance and existing bridge self-tests. `PUB-NOTIFY-001` and `PUB-DRAFT-001` are now `EXERCISED`; no missing guarantee was falsely upgraded to `ENFORCED_TODAY`.

### Verified #27 completion — 2026-09-06
Issue #27 `Implement explicit domain run outcomes and health evaluation` is complete in the development repository.

Verified merge state:
- PR #40: MERGED
- branch head before merge: `a6fab46447ed04e544047994157f04919580ef1f`
- squash merge commit: `c70047a9e3123d19b46968715c3fc294a51d69d4`
- issue #27: CLOSED / completed
- local `main`: `c70047a9e3123d19b46968715c3fc294a51d69d4`
- `origin/main`: `c70047a9e3123d19b46968715c3fc294a51d69d4`
- production deployment: NOT PERFORMED
- reliability proof: UNTOUCHED

#27 now provides a deterministic repo-level Workflow Health core:
- scheduler/runtime receipt is distinct from domain/business outcome
- minimum outcomes: `SUCCEEDED`, `BLOCKED`, `FAILED`, `UNKNOWN`
- `SUCCEEDED` requires validated required artifacts or explicit valid `NO_DATA` / `NO_ACTION`
- non-success requires a stable machine-readable `reason_code` plus concise operator-readable `reason_text`
- occurrence-scoped deterministic run IDs bind one logical execution
- deterministic `HEALTHY` / `UNHEALTHY` decision surface is available for downstream notification
- result records are strictly validated
- artifact-backed success is rechecked before persistence
- result emission is idempotent and conflict-safe for the same run ID
- concurrent same-run emission is covered by offline race tests
- publication `UNKNOWN` / no-auto-retry semantics are unchanged

Validation after merge:
- `python3 tests/run_offline.py` → `OFFLINE_REGRESSION_SUITE=PASS`
- 23 run-outcome tests PASS
- 11 existing behavioral regression tests PASS
- acceptance-contract validation PASS
- manifest/draft/publish/publisher self-tests PASS
- no network or consequential provider calls in regression tests

Acceptance-contract state after #27:
- `RUN-OUTCOME-001`: `MISSING / EXERCISED`
- `RUN-ARTIFACT-001`: `MISSING / EXERCISED`
- `RUN-REASON-001`: `MISSING / EXERCISED`
- `RUN-ID-001`: `MISSING / EXERCISED`

These remain operationally `MISSING` because current production Morning Editorial and Daily Analytics runners are not yet wired to the new result surface. Do not upgrade them to `ENFORCED_TODAY` until downstream integration is actually deployed and validated.

Downstream consumers:
- #28 Morning Editorial network/runtime failure handling
- #29 Daily Analytics Zernio scheduled-session bootstrap/access path
- #30 concise Telegram failure alerts


### Verified #28 completion — 2026-09-06
Issue #28 `Harden Morning Editorial API/network failure handling` is complete in the development repository.

Verified merge state:
- PR #42: MERGED
- squash merge commit: `dee4ce1b3fc2ee9285454ea71d23b5eb63a76728`
- issue #28: CLOSED / completed
- production deployment of #28: NOT PERFORMED
- reliability proof #3: UNTOUCHED; nominal end remains 2026-09-06 03:47 Asia/Baku
- #29 Daily Analytics Zernio access/runtime bootstrap was the next main engineering issue at this point in the timeline; see "Verified #29 completion" below for its final merged state

Added:
- `workspace/social/ops/scripts/nullone_editorial_runtime.py`: `run_morning_editorial()` classifies provider failures via `nullone_run_outcome`, retries only the confirmed transient `PROVIDER_UNREACHABLE` (ENOTFOUND/timeout/reachability) pattern up to the configured bounded attempts with deterministic backoff, and checks the required editorial-board artifact before every provider call so a retry or re-entry for the same occurrence can never repeat the board write or any state mutation. Every attempt for one scheduled occurrence shares the same `run_id`; once that run_id has a persisted terminal result the provider is never called again for it. Non-reachability errors fail immediately as `EDITORIAL_PROVIDER_ERROR` without retry.
- `workspace/social/ops/scripts/nullone-morning-editorial-run.py`: thin CLI wrapper (`execute`, `self-test`) exposing this as the wiring point a scheduler would call. Its default provider invocation is untested/unwired to production; all tests inject a fake provider.
- `tests/test_morning_editorial.py`: offline coverage for bounded retry-to-failure, transient-failure-then-success without duplicate mutation, a later distinct occurrence recovering normally, run/occurrence identity preservation across retries and re-entry, non-retryable errors, same-occurrence concurrent execution, and a static guard that this module never references publication/Zernio.

This implementation does not touch `nullone-publish-bridge.py`, `nullone-publisher-run.py`, or `nullone-publish-notify.py`; publication `UNKNOWN`/no-auto-retry invariants are unchanged, and no retry behavior from this issue applies to publication. No publication behavior was changed.

### Verified #29 completion — 2026-09-06

Issue #29 `Restore Daily Analytics through a working Zernio analytics access path` is complete in the development repository.

Verified merge state:
- PR #44: MERGED
- final PR head before merge: `ccf3ddea5373df6313b98547a043aec767bfa285`
- squash merge commit: `d5db8ff0b907c0ea43b58da27f08c2d47eb94151`
- issue #29: CLOSED / completed
- production deployment of #29: NOT PERFORMED
- real authorized scheduled Zernio validation: NOT PERFORMED
- reliability proof #3: UNTOUCHED

Added:
- `workspace/social/ops/scripts/nullone_zernio_analytics_adapter.py`: the only module aware of Zernio-specific HTTPS paths, response envelopes and credentials. `ZernioReadOnlyAnalyticsConnector` exposes exactly four GET-only methods (`get_account`, `get_follower_history`, `get_account_insights`, `get_post_analytics`) mapped to Zernio's documented read-only analytics endpoints, and never calls anything on its transport but `.get(...)` — there is no create/update/delete/publish/draft/schedule/message/comment capability anywhere in this module. This deliberately bypasses generic MCP tool dispatch — the confirmed #29 root cause is a scheduled-session `bundle-mcp` bootstrap/runtime-availability failure, not a Zernio outage or allowlist defect — so analytics no longer depends on that bootstrap path. The credential is read from `ZERNIO_ANALYTICS_API_TOKEN` at call time and is never embedded in code, logs, or reason text; no credential is committed. The real HTTPS transport (`UrllibAnalyticsTransport`/`build_default_transport`) is not exercised by any test and is not wired into a scheduled runner.

  Endpoint contract, verified against Zernio's official OpenAPI spec (`docs.zernio.com/api/openapi`, `info.version: "1.0.4"`): base `https://zernio.com/api/v1`; `GET /accounts` (account selected by matching `_id` in the returned list — there is no documented `GET /accounts/{id}`); `GET /analytics/instagram/account-insights`; `GET /analytics/instagram/follower-history`; `GET /analytics` (post analytics, scoped by `accountId`+`platform`). A metric documented as unavailable is preserved as unavailable (never coerced to zero).

  Domain classification: `hasAnalyticsAccess=false` on `GET /accounts` (checked before any analytics endpoint is called) or a documented HTTP `402` (`analytics_addon_required`) on any analytics endpoint → `BLOCKED` / `ZERNIO_ANALYTICS_ADDON_REQUIRED`; `401`/`403` → `BLOCKED` / `ZERNIO_ANALYTICS_UNAUTHORIZED`; dependency/unreachable/`5xx` → `BLOCKED` / `ZERNIO_ANALYTICS_UNAVAILABLE`; malformed `400`/`404`/invalid response → `FAILED` / `ANALYTICS_RESPONSE_INVALID`; valid no-data → `SUCCEEDED` / `NO_DATA`; artifact commit failure → `FAILED` / `ANALYTICS_ARTIFACT_COMMIT_FAILED`.

- `workspace/social/ops/scripts/nullone_analytics_runtime.py`: connector-agnostic domain runtime (`run_daily_analytics`) reusing the #27 workflow/domain contract with `workflow_id="daily-analytics"` and an occurrence-scoped run_id. `scheduler_status="succeeded"` is reported even on `BLOCKED`, reproducing (and now correctly resolving the domain side of) the exact Sep 4-5 symptom where scheduler `ok/succeeded` masked a blocked business result. The raw+report artifact pair is committed via staging both rendered/validated contents as temp files, then swapping each into place with an ordered `os.replace`; if a later swap in the same call fails, every target already committed in that call is rolled back to its exact pre-call state (restored from an in-memory backup, or removed if it did not exist before). This is call-scoped atomicity via staging + ordered swap + rollback-on-failure — it is **not** a filesystem transaction and does **not** claim crash/power-loss atomicity (it does not protect against the process being killed mid-`os.replace`).
- `workspace/social/ops/scripts/nullone-daily-analytics-run.py`: thin CLI wrapper (`execute`, `self-test`); its default connector-building path is untested/unwired to production, matching the pattern already used for Morning Editorial in #28.
- `tests/test_daily_analytics.py`: 25 offline tests covering the exact documented paths/query parameters, response-envelope parsing (including the omitted-metric-is-unavailable-not-zero case), a static source-scan proving the earlier invented `/v2/...` paths are absent, account selection from the accounts list (and the not-found case), `hasAnalyticsAccess=false` blocking before any analytics call, documented `402` → `BLOCKED` (never `FAILED`/`SUCCEEDED`, no response-body leak), `400`/`404` explicitly remaining `FAILED`, fake success producing both artifacts, valid `NO_DATA` semantics, connector/bootstrap unavailability with no artifacts, unauthorized credential with no secret leakage, a missing-credential guard on the default transport builder, malformed/partial payloads with no partial artifacts, a static+dynamic capability-negative proof that the connector exposes only the four read-only `get_*` methods and never issues a non-GET call, a later healthy occurrence recovering after an earlier blocked one, a scheduler-success/domain-BLOCKED case whose CLI wrapper still exits non-zero, and two artifact-commit-failure/rollback tests (no partial pair left behind; a pre-existing valid pair is preserved) plus a narrow unit-level test of the commit helper itself.

Validation:
- `python3 tests/run_offline.py` → `OFFLINE_REGRESSION_SUITE=PASS`; full offline suite passed, including all 25 Daily Analytics tests and the existing run-outcome, behavioral-regression, Morning Editorial, and self-test suites
- `git diff --check` → clean
- no real Zernio/MCP/network/Telegram/Instagram/publication calls occur in any test; all connector/transport dependencies are fake doubles
- this change does not touch `nullone-publish-bridge.py`, `nullone-publisher-run.py`, or `nullone-publish-notify.py`; publication `UNKNOWN`/no-auto-retry invariants are unchanged
- production deployment of #29 is **NOT performed**; no real authorized scheduled run against live Zernio was executed; controlled production activation remains gated behind issue #37

Final retry policy: the retry policy's worst-case wall-clock cost is an explicit, tested invariant. `MAX_ATTEMPTS = 2`, `PROVIDER_CALL_TIMEOUT_SECONDS = 210`, `RETRY_BACKOFF_SECONDS = (60,)`, giving `OCCURRENCE_FAILURE_BUDGET_SECONDS = 480`, computed as `2 * 210 + 60 = 480s`. Policy safety is enforced by `validate_occurrence_policy()` raising `UnsafeRetryPolicyError` (not a Python `assert`) so a misconfigured policy fails loudly instead of being silently disabled under `-O`. `nullone_editorial_runtime.worst_case_occurrence_seconds()` computes the worst case and `validate_occurrence_policy()` checks it against the budget at import time; `tests/test_morning_editorial.py` covers it offline (no sleeping, no real provider calls).

Same-occurrence concurrent execution is serialized before any provider side effect using an exclusive `fcntl.flock` on `social/ops/run-outcomes/morning-editorial/<run_id>.lock`, so two concurrent invocations for the same run ID cannot both reach the provider or duplicate the board write.

Validation:
- `python3 tests/run_offline.py` → `OFFLINE_REGRESSION_SUITE=PASS`; full offline suite passed
- 15 Morning Editorial tests PASS (final count), alongside the existing run-outcome tests, behavioral regression tests, acceptance-contract validation, and all script self-tests
- no network or subprocess calls occur in the new tests; the real provider invocation path is never exercised

### Verified #30 completion — 2026-09-06

Issue #30 `Send concise Telegram alerts for meaningful workflow failures`
is complete in the development repository. PR #47 ("Add deduplicated
workflow failure alerts (#30)") squash-merged to `main` as
`31ac4cca9e4255d5ba665ea42989ab9237eb05c2` (was branch
`feature/n30-telegram-failure-alerts`, based cleanly on `main` at
`875eeb7`, the #3 verdict-record merge). Issue #30 auto-closed
(CLOSED/COMPLETED) via the PR's `Closes #30` keyword. **This is
repo-level only**: scheduler-native OpenClaw `failureAlert` is still NOT
activated in production, the domain notifier is still NOT wired into any
live scheduled job, and no real Telegram/live scheduled validation has
occurred — #37 remains the controlled production deployment/preflight/
live-validation boundary for all of that. Do not say production alerting
is active.

Architecture — two distinct failure surfaces, kept separate:
- **Scheduler/execution failures** (e.g. the confirmed 2026-09-05
  Morning Editorial `ENOTFOUND`/timeout occurrences): direction remains
  to reuse OpenClaw's own scheduler-native `failureAlert` mechanism, not
  build a second cron-error notifier. No Git-tracked declarative
  OpenClaw automation/cron configuration exists in this repository to
  implement that in, and its schema is confirmed reachable only via live
  `openclaw cron get/set` against a running Gateway. Per #30's scope,
  this was **not implemented or guessed at here**; the exact activation
  requirement is recorded as an explicit #37 deployment-time requirement
  in `docs/deployment/37-preflight-notification-requirements.md`.
- **Domain/business failures** (e.g. the confirmed Sep 5-6 Daily
  Analytics scheduler-`succeeded`/domain-`BLOCKED` symptom): implemented
  as `workspace/social/ops/scripts/nullone_failure_notify.py`, a
  standalone module that consumes an already-validated #27 run-outcome
  record and decides whether an operator alert is required. The
  structured `domain_outcome` remains the sole source of business-health
  truth; `scheduler_status` is consulted only for a narrower, separate
  purpose — routing notification *ownership* — so a true scheduler-level
  execution failure (`scheduler_status` of `error`/`failed`) is left
  quiet here and deferred to OpenClaw's own scheduler-native
  `failureAlert` instead, preventing one incident from producing two
  alerts once that native alert is activated at #37 (see "Enforced
  ownership routing" in `docs/deployment/37-preflight-notification-requirements.md`).
  `workspace/social/ops/scripts/nullone-failure-notify-run.py`
  is its thin CLI wrapper (`notify --result-file <path>`, `self-test`),
  following the same convention as the Morning Editorial/Daily Analytics
  runners. Neither file is wired into any production runner or scheduled
  job by this change — that wiring is deployment, deferred to #37, not
  repo engineering.

Key design points:
- Actionability: alerts on `BLOCKED`, `FAILED`, and `UNKNOWN` (unless a
  reason_code is explicitly listed as non-actionable — the list is
  empty today because no such reason_code currently exists); stays
  quiet on `SUCCEEDED`, including its `NO_DATA`/`NO_ACTION` variants.
  Recovery is quiet by construction — there is no code path that
  compares against a prior failure at all, not a policy flag that could
  be flipped by mistake.
- Stable failure identity: reuses #27's `health_decision()` output
  verbatim — `run_id + ":" + reason_code` — rather than inventing a
  weaker timestamp- or free-text-based identity.
- Dedup/persistence: one JSON record per failure identity under
  `social/ops/notifications/<workflow_id>/` (gitignored, matching the
  existing `run-outcomes/`, `manifests/`, etc. pattern — `run-outcomes/`
  was added to `.gitignore` in this same change, since it was missing
  despite already being runtime-only state from #28/#29). The
  notification attempt is durably reserved (`PENDING`) before the
  outbound call, mirroring `nullone-publish-notify.py`'s
  reserve-before-side-effect pattern; any existing record for an
  identity, in any state, blocks a new automatic send — so a timeout
  (ambiguous delivery) is never auto-resent, exactly like the existing
  publication notifier's own timeout handling. Same-identity concurrency
  is serialized with an exclusive `fcntl.flock` on a per-identity lock
  file, the same primitive already used by the #28/#29 occurrence locks.
- Message rendering is fully deterministic (no LLM in the loop) and
  sanitizes reason text (bearer/API-key/OAuth-token/presigned-query-param
  redaction, newline collapsing, length cap) before it ever reaches the
  transport.
- Capability-negative by construction: the module has no import or
  symbol capable of publishing, drafting, scheduling, approving, or
  retrying anything; a static source-scan test enforces the same
  substring absence (`publish`, `zernio`) already used for the #28
  retry module.
- Telegram transport is injected (`Transport` protocol); the default
  `OpenClawTelegramTransport` reuses the existing
  `social/ops/private/telegram-owner-id` file and `openclaw message send`
  CLI invocation already used by `nullone-publish-notify.py`. No new
  transport/network code was introduced.
- Notification-directory path containment: `#27`'s `workflow_id`
  contract guarantees only a non-empty single-line string, not path
  safety, so this notifier independently resolves and verifies its
  per-workflow notification directory stays inside the configured
  notification root, rejecting an absolute or traversal-style
  `workflow_id` with `NotifierError` before any filesystem write.
- `format_occurrence_time()` never emits an unparseable `occurrence_id`
  verbatim into the Telegram message — it is opaque by #27's contract
  and a future value could carry internal/private context — falling
  back to a neutral `"unavailable"` placeholder instead; traceability
  stays available via the deterministic `run_id`.

Validation performed for this branch (as of the `fix: harden failure
alert routing and state safety` follow-up commit):
- `python3 tests/run_offline.py` → `OFFLINE_REGRESSION_SUITE=PASS`
  (49 tests in `tests/test_failure_notify.py` — including scheduler-
  ownership-routing, path-containment, and opaque-occurrence-ID
  coverage added in the hardening follow-up — plus all existing suites
  unchanged and still passing)
- no network, Zernio, Telegram, or model call in any test; the default
  `OpenClawTelegramTransport`/CLI path is exercised only with fake
  transports and a tempdir-scoped notification root in tests
- `git diff --check` clean; diff inspected for secrets/production
  identifiers/signed URLs — none found (redaction tests use obviously
  synthetic fixture values only)
- publication code (`nullone-publish-bridge.py`, `nullone-publisher-run.py`,
  `nullone-publish-notify.py`) and the Morning Editorial/Daily Analytics
  runtime modules were **not modified** by this change
- production deployment: **NOT performed**
- real Telegram send / live scheduled validation: **NOT performed**
- issue #30: **CLOSED/COMPLETED**; merged via PR #47 as `31ac4cca9e4255d5ba665ea42989ab9237eb05c2`

### Verified #31 completion — 2026-09-06

Issue #31 `Define deterministic cadence and per-format load contract` is
complete in the development repository. PR #49 ("Define deterministic
cadence contract (#31)", branch `docs/n31-cadence-contract`)
squash-merged to `main` as `a9334e27576c04f37535e05e8b6bd08e45606ffa`.
Issue #31 auto-closed CLOSED/COMPLETED via the PR's `Closes #31` keyword.
This is a decision/contract document only — no cadence controller,
load-tracking implementation, or production change.

Contract: `docs/contracts/cadence-contract-v1.md`. Core accepted policy:
deterministic cadence/load contract under the `Asia/Baku` IANA timezone;
Feed/Carousel load and Story load are tracked as independent counters;
pending/approved/consequential/`UNKNOWN` state creates backpressure and
is never treated as empty load; `quality > quota`; `PREPARE_* != PUBLISH`;
human approval and second publication confirmation are unchanged; missed
cadence opportunities coalesce and are never replayed; the downtime
marker is observability-only and never masks a more specific present-time
reason — no-gap + downtime marker gives `COALESCED_AFTER_DOWNTIME`,
no-gap + no marker gives `TARGETS_MET`; current deterministic
recommendation precedence remains main before Story; 13 machine-readable
examples are included. #32 implemented this contract (see "Verified #32
completion" below).

### Verified #32 completion — 2026-09-06

Issue #32 `Implement deterministic cadence controller and per-format load
accounting` is complete in the development repository. PR #52 squash-merged
to `main` as `a5fc6c69f5133baa9a807fe725f79c1a7ae5d96c`. Issue #32
auto-closed CLOSED/COMPLETED via the PR's `Closes #32` keyword. This is the
repo-level implementation of the #31 cadence contract — no production or
scheduler wiring.

Repo-level implementation:
- `workspace/social/ops/scripts/nullone_cadence_controller.py`
- `workspace/social/ops/scripts/nullone_cadence_state_adapter.py`
- behavioral tests: `tests/test_cadence_controller.py`,
  `tests/test_cadence_state_adapter.py`

Accepted operational semantics: deterministic, connector-free cadence
evaluator; explicit `Asia/Baku`/IANA timezone handling; independent
FEED+CAROUSEL vs STORY accounting; main-before-Story single recommendation
precedence; output `NO_ACTION`/`PREPARE_STORY`/`PREPARE_MAIN_CANDIDATE`;
pending work creates backpressure independently from numeric gap; `UNKNOWN`
is consequential pending and never empty capacity; quality overrides quota;
candidate quality is an upstream boolean input, not decided by the
controller; downtime opportunities coalesce and missed slots are never
replayed; the downtime marker is audit-only and never masks a more
specific present-time reason; the read-only state adapter fails closed
rather than silently treating malformed/missing required state as zero
load; no LLM, no network, no Story copy generation, no publication
capability, no scheduler/live wiring.

Validation at merge: `python3 tests/run_offline.py` →
`OFFLINE_REGRESSION_SUITE=PASS`.

Production deployment of #32: NOT PERFORMED. #32 is repo-level only; Story
cadence is not active in production.

### Verified #33 completion — 2026-09-06

Issue #33 `Implement lightweight Story draft pipeline behind human
approval` is CLOSED/COMPLETED. PR #55 `Implement lightweight Story draft
pipeline (#33)` is MERGED; its squash merge SHA is
`7404501bdafb224f221bf8c79ee67bf7182bb2f7`.

Deployment and live-validation state:
- production deployment: NOT PERFORMED;
- real Story cadence activation: NOT PERFORMED;
- real Zernio, Telegram, or live workflow validation: NOT PERFORMED;
- no real Story draft/Telegram approval cycle has occurred through #33.

Repo-level implementation:
- a trigger-agnostic Story production core, plus a separate #32 cadence
  adapter that accepts only `PREPARE_STORY`;
- the candidate must already be `VERIFICATION: PASS`;
- Haiku is used only for minimal Story writing/reasoning; deterministic
  mechanics remain scripts, and a separate final exact-wording verifier
  is required;
- finalized immutable `nullone.story-spec.v1` is persisted before render,
  manifest creation, or review-draft creation, with deterministic
  `story_request_id` and deterministic exact-content `story_version_id`;
- per-request `fcntl.flock`; the existing Visual V2 Story renderer is
  reused with exact 1080×1920 media validation;
- the existing Production Bridge remains the hash/media authority, and
  the existing `nullone.production.v1` manifest is reused;
- at most one review-draft attempt is allowed per logical Story
  request/version; `CREATE_IN_FLIGHT`, `REVIEW_UNKNOWN`, or any consumed
  review-create attempt never auto-retries;
- Telegram preview carries the exact request, version, manifest, review,
  and media identity; preview failure is explicit
  `PREVIEW_DELIVERY_FAILED` and cannot trigger a second review draft;
- the legacy internal `texbrif:` callback namespace is intentionally
  preserved while public wording remains NullOne;
- the Story pipeline contains no publication capability.

Revision semantics:
- a Story revision requires an exact existing same-candidate STORY parent
  and its exact `DRAFT_CREATED` review post; the parent must be unpublished
  and not final-authorized;
- the revision creates a new logical Story request/version and requires
  new exact verification; approvals and publication attempts on the new
  version start fresh;
- the old manifest, media, and caption remain immutable and are not
  mutated by the revision.

Durable supersession uses schema `nullone.story-supersession.v1` at
`social/drafts/production/story/superseded/<PARENT_MANIFEST_ID>.json`:
- operator revision durably supersedes the old Story before writer,
  verifier, render, or new-draft work;
- exact revision replay is idempotent; a conflicting second revision
  fails closed;
- supersession remains durable even if the new revision later fails;
- the old Zernio draft remains audit history, and its manifest, media, and
  caption remain immutable.

Revision/publication race safety uses the shared review-post lock
`social/ops/locks/review/<REVIEW_POST_ID>.lock`. Revision and publication
serialize on that lock: if revision wins first, the stale old publication
callback blocks; if publication wins and becomes final-authorized or
consequential, revision blocks. Supersession and publication attempt
cannot both validly succeed for the same parent version.

`nullone-publisher-run.py` checks Story supersession before final
authorization or provider execution and blocks a superseded Story with
`STORY_VERSION_SUPERSEDED`. Feed/Carousel behavior is unchanged. This
publisher guard does not give the Story pipeline publication capability.

Validation at merge: Story pipeline tests `71 PASS`; Story
supersession/publisher safety `7 PASS`; full merged-main
`python3 tests/run_offline.py` → `OFFLINE_REGRESSION_SUITE=PASS`. Validation
made no live or external calls and is not production validation.

### Current production truth after #36

GitHub implementation is not production deployment. `Git merge !=
production deployment`, and `production actual != Git desired`. Current
production still runs the pre-new-system live configuration until #37
performs controlled activation:
- #27 repo outcome system is not yet fully live-wired;
- #28 Morning Editorial runtime hardening is not yet activated live;
- #29 Daily Analytics runtime is not yet live-authorized or activated;
- #30 failure alert/domain notifier is not live-wired;
- #32 cadence controller is repo-level only;
- #33 Story pipeline is repo-level only;
- #35 breaking identity/dedup is repo-level only;
- #36 breaking router/dispatcher/main pipeline is repo-level only;
- Story cadence is NOT active in production;
- Story draft pipeline is NOT active in production;
- breaking routing/dispatch is NOT active;
- Feed/Carousel breaking main pipeline is NOT active;
- no real #36 Telegram/Zernio breaking cycle has occurred;
- #37 remains the explicit deployment/preflight/live-validation boundary.

### Historical #37 preflight state — 2026-09-07

This section records production evidence at the time of the preflight. It is
not rewritten to imply that #63 was complete then. Current Git desired state
is recorded in the current execution priority, GitHub planning/engineering
state, and verified-completion sections.

Verdict: `BLOCKED`. The preflight was strictly read-only against canonical
desired Git SHA `0d68aebc3d83cc528398ca0e3348b7306db6ffe4`. No production
file, job/config, service, scheduler/model, credential, ledger, Zernio,
Telegram, approval or publication mutation occurred.

Durable production facts at preflight:
- OpenClaw `2026.8.2` was installed and the Gateway was healthy at host
  level;
- 10 scheduler jobs were enabled; Morning Editorial and Daily Analytics
  still used legacy job payloads, and no Story-specific live job existed;
- scheduler-native `failureAlert` was supported but not configured for
  Morning Editorial or Daily Analytics;
- the private Telegram owner target was `PRESENT/READABLE` without its
  value being disclosed;
- host-level Zernio OAuth/MCP was healthy, but scheduled-session bootstrap
  errors were still observed;
- the dedicated #29 `ZERNIO_ANALYTICS_API_TOKEN` was absent; the deprecated
  `ZERNIO_API_KEY` path remains rejected;
- the production publish ledger contained historical rows incompatible
  with the strict #32 cadence adapter because two valid JSON rows lacked
  `format`;
- the existing Astra publication `UNKNOWN` and two `CHECK_REQUIRED`
  records remain operator decisions and must not be touched automatically.

Confirmed deployability blockers:
1. no reviewed scheduler occurrence → Morning/Daily runtime → persisted
   #27 outcome → #30 notifier orchestration;
2. no reviewed compatibility path for historical cadence ledger rows;
3. no reviewed cadence → Story production runner;
4. no production implementation of the injected Story/main Telegram
   preview-sender protocol;
5. no reviewed Radar → #35 identity → #36 router/dispatcher → Story/main
   runner;
6. no reviewed secure injection mechanism for
   `ZERNIO_ANALYTICS_API_TOKEN`;
7. scheduled-session Zernio bootstrap remains unproven for the MCP-backed
   Story/main draft bridge despite healthy host-level service;
8. exact safe cron/job payloads cannot be authored until the missing
   orchestration and occurrence-ID boundaries exist.

Dependencies created from this evidence and architecture review:
- #65 NullOne Application Runtime and replaceable-adapter contract;
- #59 scheduled outcome orchestration;
- #60 cadence-state backward compatibility;
- #61 secure #29 analytics credential/runtime path;
- #62 Story runtime orchestration plus shared Telegram preview sender;
- #63 breaking runtime orchestration;
- #66 removal of the reachable legacy direct-Zernio-call instructions in
  `agents/approval/AGENTS.md`/`agents/publisher/AGENTS.md` (the narrow #6
  subset that stays reachable after this integration).

#65, #60 and #66 can begin independently. #59 and #61 depend on #65. #62
depends on #65 + #60. #63 depends on #65 + #62 and indirectly on #60.
#37 depends on #65, all five integration dependencies, and #66. A separate
generic scheduled-session Zernio issue was not created: #29's direct GET-only
adapter deliberately removes that dependency for Daily Analytics, while #62
must prove or replace the MCP-backed review-draft path used by Story and
inherited by #63.

#6's read-only M0 relevance check (2026-09-07) found `agents/approval/AGENTS.md`
still contains dead `## FIRST APPROVAL`/`## FINAL PUBLISH` instructions
directing the approval agent to call Zernio directly, contradicted only
textually (not structurally) by later override sections in the same file;
none of #65/#59/#61/#62/#63 touch that file, so the risk remains reachable on
the human-approval → publish path after M0 activation. Verdict:
`#6_HAS_M0_BLOCKING_SUBSET`, narrowly scoped to #66; the remainder of #6
(Morning/Radar/Strategy prompt consolidation, the capability matrix, legacy
production-skill source ownership) stays M1 cleanup and is not a #37
dependency. The separately-confirmed legacy Daily Analytics
`toolsAllow=['*']` job-config risk is not part of #66: it is already retired
by #59/#61's planned replacement of that legacy prompt-only job.

The sanitized evidence summary is
`docs/deployment/37-preflight-2026-09-07.md`. #27–#36 remain complete at
component/repository level; this preflight records previously uncaptured
integration prerequisites and does not reopen them.

### Verified #66 completion — 2026-09-07

`agents/approval/AGENTS.md`'s dead `## FIRST APPROVAL`/`## FINAL PUBLISH`
sections (direct `zernio posts_get` / `zernio call_tool` → `posts_update_post`
instructions) and the self-contradictory `## FINAL TOOL ROUTING — OVERRIDES
OLDER PUBLISH INSTRUCTIONS` section have been removed. The file now states one
authoritative flow: `texbrif-approval` validates the callback, sends
`sessions_send` `PUBLISH_AUTHORIZED` to `texbrif-publisher`, and never calls
Zernio itself; `## ABSOLUTE PROHIBITIONS` no longer carries the
`zernio call_tool may be used ONLY for posts_update_post` carve-out.
`agents/publisher/AGENTS.md` already authorized only the deterministic
`nullone-publisher-run.py execute <POST_ID>` wrapper and required no change.
`tests/test_approval_publication_instruction_safety.py` adds a
paragraph-scoped capability-negative guard (fails on a reintroduced
affirmative Zernio-mutation grant, passes legitimate prohibition prose) plus
a positive delegation-invariant check, registered in `tests/run_offline.py`.

Repository evidence does not include the OpenClaw runtime's actual live
`toolsAllow`/equivalent tool-grant configuration for the `texbrif-approval`
or `texbrif-publisher` agents — no tracked job/agent config file in this repo
carries those grants (the only tracked live-grant evidence at all is the
unrelated Daily Analytics `toolsAllow=['*']` finding above, which #66 does not
touch). The actual live capability grant for these two agents is therefore
`UNVERIFIED_FROM_REPOSITORY` and must be checked during the repeated #37
read-only production preflight, not assumed equal to the prompt-level
prohibitions this fix adds.

### NullOne Application Runtime architecture — #65

OpenClaw independence was already a product/architecture intent, and the
#27–#36 component designs reflected it through deterministic cores and
injected dependencies. The missing first-class contract was the
NullOne-owned application/orchestration layer that composes those components
for a real run. The #37 preflight exposed that omission. It is an M0
deployability requirement, not optional future cleanup.

The governing invariant is:

> NullOne owns workflow/application orchestration. OpenClaw is one
> replaceable infrastructure adapter that triggers NullOne; it is not the
> owner of NullOne business workflow semantics.

The architecture has three dependency layers:
- Domain/Core: #27–#36 run outcomes, cadence, Story, breaking identity,
  routing/dispatch, verification and publication safety; no OpenClaw
  dependency;
- NullOne Application Runtime: `MorningWorkflow`, `AnalyticsWorkflow`,
  `StoryWorkflow` and `BreakingWorkflow`; owns ordering and composes domain
  components through ports;
- adapters: OpenClaw scheduler, Zernio draft/analytics, Telegram review,
  filesystem state and systemd/runtime secret implementations.

Only current portability boundaries are introduced: normalized
`SchedulerInvocation`, `ReviewDelivery`, `DraftProvider`,
`AnalyticsProvider`, `SecretProvider`, a state/filesystem boundary where
workflow composition needs it, and an injectable clock only where
deterministic time testing requires it. Existing narrow writer/verifier
protocols remain authoritative; no abstract interface is added merely for
aesthetic layering.

Application/runtime modules must not import OpenClaw internals, shell out to
`openclaw`, derive business identity from an OpenClaw-specific model, embed
cron syntax or provider details, or require OpenClaw-specific environment or
layout as their core contract. Such details belong in edge adapters and job
payloads outside workflow implementation. A Telegram adapter may use the
OpenClaw CLI internally, but `StoryWorkflow`/`BreakingWorkflow` only see
`ReviewDelivery`.

The versioned scheduler-independent contract is
`nullone.scheduler-invocation.v1`: stable `workflow_id`, source namespace,
opaque source `external_occurrence_id`, timezone-aware `scheduled_for` and
`triggered_at`, plus a deterministic NullOne `occurrence_id`. The latter is
derived from stable occurrence fields and excludes `triggered_at`, so retry
observation time cannot mint a new logical run. #27 then derives its stable
run ID from the normalized workflow/occurrence pair. An OpenClaw, systemd,
Temporal or cloud adapter can map into the same contract; application/domain
logic never requires an OpenClaw UUID.

Full proposed contract: `docs/architecture/nullone-application-runtime.md`.
Issue #65 owns its review. #59, #61, #62 and #63 must implement against it;
#60 remains scheduler-independent and can proceed in parallel. Replacing
OpenClaw later must not require rewriting core workflow/domain logic. The
contract adds no publication capability and does not change #27–#36.

**Relationship to #13.** #65 is the current M0 minimum: only the
Domain/Core, NullOne Application Runtime, and the adapters/ports #59/#61/#62/#63
need today. #13 (M3) is a separate, later, generalized Core/product capability
contract — model/runtime capability taxonomy, connector versioning,
scopes/permissions, idempotency evidence, provider reconciliation. #13
extends/generalizes the accepted #65 boundary; it does not redefine,
contradict, or block it, and #59/#61/#62/#63 implement against #65's current
M0 boundary, not #13's speculative M3 scope. Any change to #65's boundary that
a future #13 implementation requires needs an explicit reviewed
migration/ADR. No universal provider/plugin abstraction is introduced in #65
merely to anticipate #13.

### Verified #62 completion — 2026-09-07

PR #70 (`Implement StoryWorkflow and shared review delivery`) squash-merged
to `main` as `8d6d9844f0c494e3a813180fb7e83e87f713e745`, and #62 closed as
`CLOSED/COMPLETED` through that merge. The merged implementation remains
repository state only; it is not production activation or live provider
proof.

What the PR adds, layered per the #65 architecture:

- `nullone_scheduler_invocation.py` — the shared executable
  `nullone.scheduler-invocation.v1` value/validation module the #65
  contract document said to add "if there is no shared executable
  scheduler-invocation validator yet" (there was none); the existing
  fixture test (`tests/test_scheduler_invocation_contract_fixture.py`) now
  imports it instead of duplicating the rule a second time;
- `nullone_story_candidate_provider.py` — the narrow injected
  `StoryCandidateProvider` boundary and deterministic selection rule
  (zero → `CANDIDATE_UNAVAILABLE`, exactly one eligible → proceed,
  multiple with no accepted ordering → `CANDIDATE_AMBIGUOUS`, all invalid
  → `CANDIDATE_INVALID`). No heuristic Markdown parsing of
  `candidate-queue.md`/`topic-ledger.jsonl` was added — repository
  inspection found no existing structured, authoritative candidate source
  to read, and inventing a ranking heuristic to fill that gap was
  explicitly out of scope;
- `nullone_review_delivery.py` — the shared `ReviewDelivery` port
  (`.send(payload) -> dict`), a cross-schema payload validator accepting
  both `nullone.story-preview.v1` and `nullone.main-preview.v1` unchanged,
  and fakes for tests. Its method name/shape is structurally identical to
  the existing local `TelegramPreviewSender` protocols already defined in
  `nullone_story_pipeline.py`/`nullone_main_draft_pipeline.py`, so neither
  #33 nor #36 core file needed to change;
- `nullone_telegram_review_delivery_adapter.py` — the one reusable
  Telegram/OpenClaw infrastructure adapter implementing that port, pinned to
  OpenClaw `v2026.8.2` commit
  `0965053fe6b9341776df147a6934b7485c60b5ca`. It validates every
  transport-bound media path is workspace-contained, regular, and matches its
  preview SHA-256 before any transport; sends Story/Feed media once or every
  Carousel item sequentially in exact payload order; then sends exactly one
  approval card using the producer-owned `presentation` via
  `--presentation` (never the unsupported `--buttons`). Every send requires a
  top-level non-empty camelCase `messageId`; partial delivery, wrong-case
  `message_id`, failure, or timeout is non-SENT with no retry or cleanup.
  Successful aggregate proof retains ordered `media_message_ids` and one
  `approval_message_id`. The adapter reads
  `social/ops/private/telegram-owner-id` at call time only, never logs or
  echoes it, and has no fallback target;
- `nullone_story_workflow.py` — `StoryWorkflow` itself: validates the
  trigger against the shared scheduler-invocation module pinned to
  `workflow_id == "story"`, reads authoritative state through the existing
  #32 cadence state adapter, evaluates the existing #32 controller
  unchanged, and only on an exact `PREPARE_STORY`/`STORY_GAP`/
  `CANDIDATE_SEARCH_AND_PREPARE` result calls the candidate provider and
  then #33's own `run_story_pipeline()` unchanged. Cadence arithmetic,
  Story rendering, request/version identity, supersession, manifest
  construction, verification, and the one-attempt review-draft guard are
  all reused verbatim, never reimplemented.

Candidate-availability consistency (goal-doc section 9): `StoryWorkflow`
never derives `candidate_availability.story_quality_candidate_available`
by speculatively calling the candidate provider — that signal is accepted
as the same externally-computed opaque input the #31 cadence contract
already documents. This keeps every `NO_ACTION`/`PREPARE_MAIN_CANDIDATE`/
malformed-state path genuinely free of any candidate-provider call
(verified by test), and makes the consistency check meaningful: if
cadence says a Story candidate is available but the injected provider
then returns none, `StoryWorkflow` returns a truthful
`CANDIDATE_UNAVAILABLE` rather than fabricating content.

Tests added: `tests/test_story_workflow.py` (trigger boundary, no-action
side-effect-freedom, the full candidate-selection matrix, replay/
concurrency reusing #33's existing per-request lock — no second lock
system), `tests/test_review_delivery.py` (both preview schemas via one
shared delivery instance run through the real #33/#36 pipelines, exact
OpenClaw v2026.8.2 fixture/argv/JSON proof contract, Story/Feed/Carousel media
ordering, media integrity, owner-target secrecy, partial-delivery and every
transport failure mode, exact callback binding, and public-wording
preservation), and `tests/test_story_workflow_capability_negative.py`
(static proof the application layer has zero subprocess/OpenClaw/Zernio-
tool/publisher-import capability, checked against code with docstrings/
comments stripped so the architecture boundary can still be documented in
prose without tripping its own capability check). `python3
tests/run_offline.py` remains green with all of the above registered.

Deployment/integration mapping (OpenClaw occurrence → adapter
normalization → `nullone.scheduler-invocation.v1` → `StoryWorkflow`) is
documented, not applied, in
`docs/deployment/62-story-workflow-deployment.md`. No Story-specific live
OpenClaw job exists in production and none was added by this PR; the
confirmed #37 preflight fact that the scheduled-session Zernio bootstrap
remains unproven for the MCP-backed draft bridge is restated there as
`LIVE_SCHEDULED_PATH_UNPROVEN`, deferred to #37 — this PR does not and
cannot prove live behavior offline. No production file, OpenClaw
config/job, cron entry, Zernio call, or Telegram send occurred while
building or testing #62.

The #62 dependency for #63 is satisfied. Live scheduled-session Zernio and
Telegram behavior remains unproven; `LIVE_SCHEDULED_PATH_UNPROVEN` is not a
PASS and remains deferred to #37.

### Verified #63 completion — 2026-09-07

Issue #63 is CLOSED/COMPLETED. PR #71 `Implement BreakingWorkflow
orchestration` squash-merged to `main` as
`34b35cba7bea0c366096bfbfd5d7141743c87189`. Repository-level
`BreakingWorkflow` implementation is complete. It accepts the shared
normalized scheduler invocation plus strict machine-readable Radar handoff,
recomputes #35 identity from fresh authoritative state, evaluates and strictly
validates #36 routing, derives breaking Story/main maxima from authoritative
published-plus-pending loads, and delegates durable Story-first execution to
the unchanged #36 dispatcher. Story/main review drafts reuse the merged #62
DraftProvider and ReviewDelivery boundaries.

This is repository completion only; no production activation occurred. The
current legacy Radar remains `DELTA_MONITORING_ONLY` and Markdown-only; its
strict machine-readable handoff remains `DESIRED / NOT DEPLOYED`. That desired
edge maps one raw Radar scan plus each `candidate_id` into a distinct stable
Breaking occurrence. The strict input accepts exactly `UNVERIFIED | PARTIAL |
PASS | BLOCKED` and rejects `FAIL`. Optional-main preparation occurs only after
Story reaches exact review-delivery `SENT`, so main dependency/candidate
failures preserve durable Story success. Non-success #27 mapping text is
normalized to one line and at most 240 characters while full diagnostics
remain in application/audit state. Natural Breaking live proof remains
`UNPROVEN_LIVE / DEFERRED_TO_#37`; the scheduled Zernio path remains
`LIVE_SCHEDULED_PATH_UNPROVEN`.

### Verified #34 completion — 2026-09-06

Issue #34 `Define breaking severity, deduplication and immediate-draft
policy` is complete in the development repository. PR #50 ("Define
breaking draft routing policy (#34)", branch
`docs/n34-breaking-routing-policy`) squash-merged to `main` as
`33bd7c9114ecaeda675f1565a80268541c95dd68`. Issue #34 auto-closed
CLOSED/COMPLETED via the PR's `Closes #34` keyword. This is a
decision/contract document only — no identity/dedup engine, routing
implementation, or production change.

Contract: `docs/contracts/breaking-routing-policy-v1.md`. Core accepted
policy: severity classes `NORMAL`/`MATERIAL_BREAKING`/
`EXCEPTIONAL_BREAKING`; breaking overrides timing only, never publication
authorization; verification, quality, dedup, safe load, human approval
and publication safety remain mandatory; `MATERIAL_BREAKING` may request
an immediate Story; `EXCEPTIONAL_BREAKING` may request Story plus an
optional justified Feed/Carousel candidate, with exceptional main not
mandatory; known duplicate/consequential/`UNKNOWN` state suppresses
automatic regeneration; deterministic identity/state authority precedes
optional AI assistance; a follow-up requires verified material delta plus
distinct audience value; no routing output authorizes publication. #35
implemented identity/dedup (see "Verified #35 completion" below); #33
implemented the reusable Story production core (see "Verified #33
completion" above); #36 implemented this accepted policy as deterministic
repo-level draft routing (see "Verified #36 completion" below).

### Verified #35 completion — 2026-09-06

Issue #35 `Implement breaking event identity, deduplication and follow-up
suppression` is complete in the development repository. PR #53
squash-merged to `main` as `0b0679c2d5aac98d777da34e2257526e9d9a09b5`.
Issue #35 auto-closed CLOSED/COMPLETED via the PR's `Closes #35` keyword.
This is the repo-level implementation of the #34 breaking policy — no
production or scheduler wiring.

Repo-level implementation:
- `workspace/social/ops/scripts/nullone_breaking_identity.py`
- behavioral tests: `tests/test_breaking_identity.py`

Output schema: `nullone.breaking-identity.v1`. Relations:
`EXACT_DUPLICATE`, `SAME_EVENT`, `MATERIAL_FOLLOW_UP`, `DISTINCT_EVENT`,
`AMBIGUOUS_IDENTITY`.

Accepted safety behavior: deterministic SHA-256 identity; exact persisted
identifiers have strongest authority; a different article/source URL
alone never proves a distinct event, and URL-only evidence cannot by
itself license `DISTINCT_EVENT`; structured deterministic occurrence
metadata is used before URL fallback; topic/title/`topic_cluster` alone
never proves event equivalence; queue/topic-ledger matching requires
exact persisted linkage; all deterministically matching history is
collected, not just the first match; source/state precedence remains
authoritative; required state distinguishes `MISSING`/`INITIALIZED_EMPTY`/
`PRESENT_WITH_DATA`/`UNREADABLE`/`MALFORMED`; a missing required store is
never silently treated as empty; positive unsafe higher-authority
evidence still suppresses even if lower state is unavailable; `UNKNOWN`,
`CHECK_REQUIRED`, `READBACK_FAILED`, consequential publication states,
and any consumed `publication.attempts >= 1` block automatic equivalent
regeneration irrespective of later state; any consumed
`review.create_attempts >= 1` suppresses irrespective of later review
state; an explicit material follow-up delta requires deterministic parent
linkage, and an asserted delta without a proven parent fails closed as
`AMBIGUOUS_IDENTITY`; no AI/vector DB, no network, no Story/Feed routing,
no publication capability, no scheduler/live wiring.

Narrow #36 state-reader cardinality hardening now represents manifest
state as `candidate_id -> tuple of all linked manifests`, rather than
assuming one manifest per candidate. This permits one accepted
EXCEPTIONAL development to own Story + Feed or Story + Carousel under the
same candidate without turning valid state into `MALFORMED`. Valid sibling
manifests remain `PRESENT_WITH_DATA`; every sibling reference remains
auditable; an unsafe or unresolved sibling cannot be downgraded by a clean
sibling; malformed JSON, missing/invalid candidate identity, or conflicting
use of one `manifest_id` still fails closed. This cardinality change did
NOT broaden or otherwise change #35 event identity, dedup, follow-up, or
suppression policy.

Current merged validation: breaking identity behavioral suite 99/99 PASS,
with all original 89 guarantees still covered; full offline suite
`python3 tests/run_offline.py` →
`OFFLINE_REGRESSION_SUITE=PASS`.

Production deployment of #35: NOT PERFORMED. #35 is repo-level only;
breaking routing is not active in production.

### Verified #36 completion — 2026-09-07

Issue #36 `Route material breaking to Story drafts and exceptional
breaking to optional main drafts` is CLOSED/COMPLETED. PR #57 `Implement
breaking draft routing (#36)` is MERGED; its squash merge SHA is
`36f358a539fedf90e0c5cffda9b503b87594e3f1`.

Deployment and live-validation state:
- production deployment: NOT PERFORMED;
- breaking routing live activation: NOT PERFORMED;
- real Zernio/Telegram breaking cycle: NOT PERFORMED.

#36 completed repo-level engineering only.

Deterministic router:
- implementation: `workspace/social/ops/scripts/nullone_breaking_router.py`;
- output contract: `nullone.breaking-routing.v1`;
- pure deterministic routing with no network, LLM, or publication
  capability;
- consumes upstream verified severity, the #35 identity/dedup result, and
  structured safety/format findings;
- `NORMAL` → `NORMAL_QUEUE` with no immediate draft;
- `MATERIAL_BREAKING` → `[STORY]` only, never automatic Feed/Carousel;
- `EXCEPTIONAL_BREAKING` → `[STORY, FEED]`, `[STORY, CAROUSEL]`, or
  Story-only `[STORY]` when main is ineligible or structurally ambiguous;
  there is no main-only fallback;
- Feed requires score >= 38; Carousel requires score >= 42 plus meaningful
  multi-slide value; deterministic structural fit selects Feed versus
  Carousel, while an ambiguous winner falls back to Story-only;
  `quality > extra format/quota`.

Breaking overrides ordinary cadence timing only. It never bypasses
verification, quality, safe load/capacity, dedup, dependency availability,
or human approval.

Strict routing-artifact boundary:
- public `validate_routing_result_dict()` strictly validates the exact
  accepted 14-field `nullone.breaking-routing.v1` object before durable
  reservation, authoritative recheck, or dispatch;
- missing/unknown fields, invalid semantic combinations, main-only,
  reordered or duplicate targets, incompatible reason/severity pairs,
  acceleration without PASS verification, unresolved identity, an
  ineligible dedup relation, or reconciliation-required acceleration are
  rejected;
- the complete accepted routing decision is SHA-256 bound and semantically
  revalidated on reload; malformed or tampered decisions fail closed.

Durable Story-first dispatch:
- implementation: `workspace/social/ops/scripts/nullone_breaking_dispatch.py`;
- schema: `nullone.breaking-draft-set.v1`;
- path:
  `social/drafts/production/breaking/sets/<DRAFT_SET_ID>.json`;
- `draft_set_id` is deterministic from contract version + `event_id` +
  `development_id`; target format is not part of set identity;
- one development owns at most one durable draft set; a different source,
  candidate, severity escalation, format change, or changed decision cannot
  mint or hijack another set;
- the exact decision is persisted and hash-bound; per-set `fcntl.flock`
  serializes reservation/dispatch;
- Story always dispatches before optional main, with no main-only fallback.

Crash/replay safety uses target states `PENDING`, `DISPATCH_IN_FLIGHT`,
`SUCCEEDED`, `BLOCKED_BEFORE_ATTEMPT`, `PREVIEW_DELIVERY_FAILED`, and
`AMBIGUOUS`. `DISPATCH_IN_FLIGHT` is persisted before a Story/main runner
is invoked. An unexpected exception after reservation becomes
`AMBIGUOUS` with reconciliation required; an in-flight state found after
restart is never automatically replayed. Story failure or ambiguity
prevents main; main failure or ambiguity never repeats Story; completed
targets never automatically repeat. Explicit continuation exists only for a target
proven `BLOCKED_BEFORE_ATTEMPT`; consumed, ambiguous, or in-flight targets
cannot use it.

Mandatory authoritative rechecks:
- fresh #35 authoritative state must still permit dispatch immediately
  before Story;
- state is re-read again after Story and immediately before main;
- the exact Story manifest owned by this draft set may be recognized as
  the expected sibling, while any additional external/unsafe equivalent
  state blocks main;
- a fresh main-capacity recheck is mandatory for a main target; a missing
  recheck is never PASS.

Telegram human-review truth:
- for both Story and main, target `SUCCEEDED` requires pipeline outcome
  `DRAFT_CREATED` and `preview_delivery.status == "SENT"`;
- a Zernio review draft alone is not workflow success;
- missing, malformed, or non-SENT proof becomes
  `PREVIEW_DELIVERY_FAILED`; the draft is not recreated, Telegram is not
  automatically resent, and a Story preview failure prevents optional
  main;
- #27 domain outcome is `FAILED`, never `SUCCEEDED`, for preview-delivery
  failure;
- legacy internal callbacks remain
  `texbrif:approve:<POST_ID>`, `texbrif:reject:<POST_ID>`, and
  `texbrif:revise:<POST_ID>`, while public wording remains NullOne.

Review-only main pipeline:
- implementation:
  `workspace/social/ops/scripts/nullone_main_draft_pipeline.py`;
- closes the confirmed repo gap for programmatic Feed/Carousel review
  drafts, with no publisher capability;
- reuses the existing Visual V2 Feed and Carousel renderers and generic
  Production Bridge manifest/draft infrastructure;
- candidate `VERIFICATION: PASS` is admission only; exact finalized main
  wording separately requires `MainFinalVerifier` PASS before render,
  manifest, or draft;
- verifier exception → `VERIFIER_FAILED`; non-PASS →
  `VERIFICATION_BLOCKED`;
- immutable `nullone.main-draft-spec.v1` is persisted before
  render/manifest/draft at
  `social/drafts/production/main/specs/<MAIN_REQUEST_ID>.json`;
- request/version identity is deterministic; same-request content drift
  → `MAIN_SPEC_CONFLICT`, and retry cannot mint altered wording/version;
- Feed is exactly 1080×1350;
- Carousel has 2–10 ordered 1080×1350 slides, requires meaningful
  multi-slide value, and preserves ordered media;
- no Visual V3 and no Reels.

#36 also supplies an offline-testable mapping into #27's domain outcome
vocabulary: valid NORMAL/suppression `NO_ACTION` → `SUCCEEDED`;
deterministic pre-attempt policy/safety block → `BLOCKED`;
preview-delivery failure → `FAILED`; ambiguous/in-flight/possibly consumed
side effect → `UNKNOWN`. This mapping is repo-level only and is not
scheduler/live-wired; scheduler success remains distinct from domain
success.

Validation at merge:
- router: 54/54 PASS;
- dispatcher: 60/60 PASS;
- #35 identity: 99/99 PASS;
- main pipeline: 34/34 PASS;
- merged-main `python3 tests/run_offline.py` →
  `OFFLINE_REGRESSION_SUITE=PASS`;
- `git diff --check`: PASS;
- no live/external calls.

This is not production validation and adds no final publication
authorization. Every target remains its own review object, first-stage
human approval remains per target, and second final publish confirmation
remains outside #36.

## Parallel engineering safety

#31 and #34 were developed as two concurrent sessions against the same
primary checkout (`~/nullone-repo-staging`) rather than separate
worktrees; one session's `git checkout` changed the other's active branch
mid-task at least once. No work was lost — uncommitted changes survive a
branch switch and untracked files are not branch-scoped — but this is a
process risk, not a pattern to repeat deliberately.

The #32/#35 engineering wave that followed this lesson successfully used
separate `git worktree` directories per concurrent session instead. For
any future parallel engineering in this repo:
- use a separate `git worktree` directory per concurrent session, not the
  shared primary checkout;
- one branch per worktree;
- do not switch the shared primary checkout's branch out from under
  another active session;
- keep the primary checkout on a clean `main` unless it is deliberately
  being used for sync/merge verification.

This is development/change-control guidance only, not production
architecture; worktree paths are session-local scratch and should not be
recorded as permanent infrastructure identifiers.

## Reliability proof
Baseline: 2026-09-04 03:47 Asia/Baku
Nominal end: 2026-09-06 03:47 Asia/Baku
The nominal proof window is closed. The read-only issue #3 evidence evaluation is complete at repo/report level: see `docs/reliability/2026-09-proof-verdict.md` on `feature/n3-reliability-proof-verdict` for the full audit. Issue #3 itself is now **CLOSED/COMPLETED** (PR #46 squash-merged as `875eeb715cac3c933b29694fec3c07fba094a39e`; independently verified via `gh issue view 3` → `state: CLOSED, stateReason: COMPLETED` and `gh pr view 46` → `state: MERGED` on 2026-09-06). This closure records the verdict — it does NOT itself authorize production deployment or #37; the FAIL verdict and its criterion counts below are unchanged.

### Final proof verdict — 2026-09-06
**FAIL.** Criterion counts: PASS 4, FAIL 4, NOT_EXERCISED 7 (of 15 canonical `PUB-`/`RUN-` IDs).

Publication-safety invariants held under real in-window ambiguity: `PUB-UNKNOWN-001` (ambiguous Astra publish correctly stayed `UNKNOWN`, never auto-retried; a read-only post-window Zernio reconciliation shows the provider object is currently `draft`, not `published`, which strengthens the non-publication interpretation but does not resolve the durable `UNKNOWN` state), `PUB-IDEMP-001`, `PUB-AUTH-001`, `PUB-READBACK-001` all PASS on direct in-window evidence. `PUB-UNKNOWN-001`'s PASS rests on "ambiguous result → `UNKNOWN` + no auto-retry" being upheld, not on proving provider-side historical non-publication.

Workflow-reliability invariants FAILED on direct, repeated production evidence, not merely missing coverage:
- `RUN-OUTCOME-001` / `RUN-ARTIFACT-001`: Daily Analytics reported scheduler `succeeded` on BOTH in-window occurrences (2026-09-05 03:20 and 2026-09-06 03:20 Baku) while producing no analytics artifact either time.
- `RUN-REASON-001`: the 2026-09-06 occurrence's own self-generated remediation text told the operator to configure the legacy `ZERNIO_API_KEY` — directly contradicting `workspace/social/ZERNIO.md`, which states that path is deprecated and that an MCP auth failure should STOP/BLOCKED. This is a new, distinct finding beyond the previously known bundle-mcp bootstrap direction; do not act on that automation-generated text.
- Morning Editorial's entire 2026-09-05 occurrence was lost (4 scheduler-level attempts, all `ENOTFOUND`/timeout, terminal 09:17) with no later in-window occurrence to recover naturally (next occurrence 2026-09-06 08:30 falls after window close) — confirmed via `openclaw automations runs`, not merely inferred.
- Confirmed via direct `openclaw cron get` query: neither automation has any `failureAlert` configured; `delivery.mode=none`; `lastFailureNotificationDeliveryStatus=not-requested` for the full window (#30 remains the fix).
- `RUN-ID-001`: the proof window contained 4 real scheduled executions (Morning Editorial ×2, Daily Analytics ×2) with scheduler-side run identity, and for every one of them the required end-to-end binding of that identity to a domain-outcome object was affirmatively absent — an exercised-and-failed requirement, not merely untriggered.

Unresolved risks and release restrictions (owners/next actions in full in the report): #27 (domain outcomes/health), #28 (Morning Editorial runtime), #29 (Daily Analytics runtime/adapter), and #30 (failure alerts) were the **proof-derived blocking operational bundle** this verdict identified — all four are now merged in Git (#30 via PR #47, squash commit `31ac4cca9e4255d5ba665ea42989ab9237eb05c2`) but none are deployed to production. #36 breaking draft routing is also merged/completed, so #27–#36 component-level repo engineering is complete. The later 2026-09-07 #37 read-only preflight identified deployable-integration prerequisites #59–#63; #60 and #62 are now merged/closed, while #59, #61 and #63 remain before a repeated preflight. #65's accepted NullOne-owned application/runtime boundary is also merged. #37 remains open and deployment remains prohibited until the remaining dependencies are reviewed/merged and preflight is rerun. Do not provision `ZERNIO_API_KEY` based on the automation's own Sep-6 text. The Astra content's `UNKNOWN`/`draft` state is a distinct operator publish-or-discard decision, not a release blocker.

Immediate next engineering order (updated after #62 merged): #31–#36, #65, #60, #62 and #66 are accepted and merged. Deployable end-to-end M0 integration remains incomplete. Implement #63 through review now that its dependencies are satisfied; #59 and #61 remain independent open work. Rerun the strictly read-only #37 preflight only after #59/#61/#63 are accepted; only on `READY_FOR_CONTROLLED_DEPLOYMENT`, deploy under #37 and then observe genuine scheduled behavior. #37's acceptance criteria are unchanged. No separate, uncontrolled production deployment stage occurs before #37 except genuine emergency recovery of a concrete production failure under existing hotfix rules.

### Confirmed Morning Editorial defect
On 2026-09-05, Morning Editorial scheduled runs at:
- 08:30
- 08:40
- 08:52
- 09:07

all failed.

Durable run evidence:
- provider path: `claude-cli` / requested `anthropic`
- model: `claude-sonnet-5`
- cause/errorReason: timeout
- terminal error: `API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)`
- model fallback: `next=none`
- calls stalled for roughly ten minutes before terminal failure

A later read-only probe resolved `api.anthropic.com` and completed HTTPS/TLS successfully. Therefore the incident is confirmed as a transient provider/runtime reachability failure pattern; a permanent DNS/configuration fault is NOT confirmed.

### Confirmed scheduler/domain-status defect
Daily Analytics on Sep 5 and Sep 6 (the two in-window scheduled occurrences) reported scheduler/runtime:
- `status=ok`
- `completionStatus=succeeded`

while the business result stated analytics could not be completed.

Required Sep 4–5 analytics artifacts were absent.

Therefore scheduler/process success is not a valid proxy for domain/business completion.

### Confirmed Daily Analytics root cause direction
Current Zernio state at read-only probe time:
- configured
- enabled
- OAuth authorized
- health `ok`
- live MCP capability probe successful
- 21 Zernio tools visible
- `main` agent allows `zernio__*`
- Daily Analytics job has `payload.toolsAllow=['*']`

Sep 5 03:20 journal nevertheless records:
`[bundle-mcp] failed to start server "zernio"`

Therefore #29 is a scheduled-session `bundle-mcp` bootstrap/runtime-availability defect. It is NOT currently supported to describe it as a generic Zernio outage or an allowlist defect.

### Confirmed failure-notification gap
At 2026-09-05 17:31:
- global `cron.failureAlert` not configured
- Morning Editorial per-job `failureAlert` not configured
- Daily Analytics per-job `failureAlert` not configured
- both show `lastFailureNotificationDeliveryStatus=not-requested`
- delivery mode is `none`

Direction:
- reuse OpenClaw scheduler-owned failure alert for true execution failures where suitable
- #27 domain outcomes must additionally drive operator notification for scheduler-success/business-`BLOCKED` cases
- healthy runs remain quiet
- notifier failure stays independent from publication/workflow retry

### Ambiguous publication
`2026-09-05-openai-astra-launch-2026-09-05.json` remains:
- `publication.state=UNKNOWN`
- `attempts=1`

No retry has been performed. Do not auto-retry.

## Immediate engineering order

### #27–#36 component-level repo engineering complete

The accepted component-level engineering sequence is complete in Git:
- #27 domain outcomes — DONE;
- #28 Morning Editorial runtime hardening — DONE;
- #29 Daily Analytics runtime — DONE;
- #30 failure notification architecture — DONE;
- #31 cadence contract — DONE;
- #32 cadence controller — DONE;
- #33 Story pipeline — DONE;
- #34 breaking policy — DONE;
- #35 breaking identity/dedup — DONE;
- #36 breaking draft routing — DONE.

#4 and #5 are also complete. #36 merged via PR #57 as
`36f358a539fedf90e0c5cffda9b503b87594e3f1`; the earlier issue/merge SHAs
remain recorded in their verified-completion sections. No production
deployment has been performed for #27–#36. Component-level repo
engineering complete does not mean deployable end-to-end integration or
production activation is complete.

The #37 preflight discovered integration prerequisites between those
accepted components and the live scheduler/runtime. They are tracked by
#59–#63, with the missing NullOne-owned application/runtime contract tracked
by #65, and the narrow reachable-legacy-instruction subset of #6 tracked by
#66. This is not a resurrection of #27–#36; those issues remain legitimately
CLOSED/COMPLETED.

The #3 read-only reliability proof evaluation is complete at repo/report
level with a final verdict of FAIL — see
`docs/reliability/2026-09-proof-verdict.md`. The proof verdict remains
`PASS 4 / FAIL 4 / NOT_EXERCISED 7`; completing repo engineering does not
rewrite historical production evidence or authorize deployment.

### Current Git desired-state M0 execution order — after PR #71 and PR #73

1. #59's repository foundation is merged: PR #73 squash-merged as
   `cec185f9a62c2055175454f06d3d5c54596bba23` (see
   `docs/deployment/59-scheduled-workflows-deployment.md`). #59 was
   deliberately kept OPEN by that merge (no closing keyword) because its
   OpenClaw trigger edge is `CONFIRMED BLOCKED` — see "OpenClaw
   trigger-edge architecture decision" above. Implement and merge #59's
   remaining trigger-edge replacement architecture, and implement and
   merge #61, independently.
2. Review and merge #59's remaining scope and #61 through normal change
   control.
3. Repeat the strictly read-only #37 preflight.
4. Only a `READY_FOR_CONTROLLED_DEPLOYMENT` verdict permits controlled
   deployment under #37; no ad-hoc deployment is permitted.
5. Observe natural Morning/Daily/Story/breaking/alert behavior under #37.

#37 remains OPEN under its unchanged acceptance criteria. Once its
integration dependencies are merged and preflight permits deployment, it
will separately:
- select exact reviewed commit(s);
- perform preflight;
- deploy deliberately;
- record the exact deployed SHA;
- validate production actual state;
- observe normal Morning Editorial and Daily Analytics;
- observe real Story cadence using eligible real content;
- observe breaking routing only if a natural qualifying event occurs,
  otherwise record the live breaking criterion as `NOT_EXERCISED`;
- preserve two-stage human approval and the no-blind-publish rule;
- record rollback/change evidence;
- perform the scheduler-native `failureAlert` activation recorded in
  `docs/deployment/37-preflight-notification-requirements.md` where the
  reviewed preflight permits it.

No uncontrolled production changes should occur before that explicit
gate except genuine emergency recovery of a concrete production failure
under existing hotfix rules. No synthetic production cycles. Git merge
does not change production/live behavior: Story cadence, Story drafting,
breaking routing/dispatch, and the breaking Feed/Carousel main pipeline
remain inactive; no scheduler-native `failureAlert` activation or domain
notifier live wiring has occurred.

## Model/cost policy
- Haiku: Radar, analytics, heartbeat, approval/publisher/utility and lightweight Story reasoning where useful
- Sonnet: Morning Editorial, Draft Factory, Weekly Strategy
- no Opus default
- deterministic mechanics should continue moving out of LLMs

## Long-term architecture
Still unchanged:
- portable Core owns durable domain state/policy
- OpenClaw becomes optional runtime adapter
- Zernio remains replaceable connector
- model vendors replaceable
- PostgreSQL-centered portable domain
- web-first internal Control before broad SaaS
- no premature Kubernetes/microservices/multi-region
- VPS migration remains deferred and is not a prerequisite for current engineering
