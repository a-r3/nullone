# NULLONE_PROJECT_CONTEXT

Last updated: 2026-09-06 Asia/Baku
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
- GitHub merge ≠ production deployment
- production actual state and GitHub desired state must be kept separate

## Current execution priority
Operational workstreams remain ahead of large migration/product work:

1. Operational Reliability Defects
2. Story Pipeline + Cadence Controller
3. Breaking Behavior

Existing M1/M2/M3 remain intact.

New operational milestone:
`M0 — Production operations are healthy and timely`

## GitHub planning / engineering state
Repository: `a-r3/nullone`
Current visibility: **public** as of 2026-09-05 18:34 so the ChatGPT GitHub connector can inspect it. This was described by Rauf as being made public for connector access; do **not** infer that the long-term private-repo policy was permanently abandoned unless Rauf explicitly confirms that.
Local clone: `~/nullone-repo-staging`

Verified planning:
- M1/M2/M3 exist
- M0 exists as milestone #4
- canonical planning issues #3–#14 exist; #3, #4 and #5 are now CLOSED (PR #46, #38, #39 respectively), while the remaining applicable planning issues stay open
- accidental duplicates #15–#26 closed
- operational issues #27, #28 and #29 are CLOSED; #30–#37 remain open
- native dependencies created
- Project #5 exists
- GitHub Project field ordering for some M0 items may remain UI-housekeeping due transient GraphQL secondary rate limiting; this is not an engineering blocker

Relevant issues:
- #3 reliability proof/anomaly investigation — CLOSED/COMPLETED (PR #46 squash-merged as `875eeb715cac3c933b29694fec3c07fba094a39e`, verified via `gh issue view 3`/`gh pr view 46` on 2026-09-06); the recorded FAIL verdict and its criterion counts (PASS 4 / FAIL 4 / NOT_EXERCISED 7) are unchanged by this closure and still gate #37
- #4 executable completion/publication contracts — CLOSED; PR #38 squash-merged as `417cf400157dea95e19d8eb0a860c7bcb974e6e7`
- #5 offline behavioral regression tests — CLOSED; PR #39 squash-merged as `72e5c31e5bb3db922f30a2f8ea91c5b2d7ef8b41`
- #27 explicit domain run outcomes/health — CLOSED; PR #40 squash-merged as `c70047a9e3123d19b46968715c3fc294a51d69d4`
- #28 Morning Editorial network failure behavior — CLOSED; PR #42 squash-merged as `dee4ce1b3fc2ee9285454ea71d23b5eb63a76728`
- #29 Daily Analytics Zernio access/runtime bootstrap — CLOSED; PR #44 squash-merged as `d5db8ff0b907c0ea43b58da27f08c2d47eb94151`; production NOT deployed; #30 is now the next main engineering issue
- #30 Telegram failure alerts — OPEN; repo-level implementation exists on branch `feature/n30-telegram-failure-alerts` (not merged, no PR opened yet); see "Repo-level #30 implementation" below
- #31–#33 cadence/Story
- #34–#36 breaking
- #37 controlled production activation/validation

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

### Repo-level #30 implementation — 2026-09-06 (branch, not merged)

Issue #30 `Send concise Telegram alerts for meaningful workflow failures`
has a repo-level implementation on branch
`feature/n30-telegram-failure-alerts` (based cleanly on `main` at
`875eeb7`, the #3 verdict-record merge). **Not merged. No PR opened.
Issue #30 is not closed.** Do not upgrade this status until a PR
actually merges.

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
- issue #30: **NOT closed**; no PR opened

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

Unresolved risks and release restrictions (owners/next actions in full in the report): #27 (domain outcomes/health), #28 (Morning Editorial runtime), #29 (Daily Analytics runtime/adapter), and #30 (failure alerts) are the **proof-derived blocking operational bundle** this verdict identified — #27/#28/#29 are merged in Git but not deployed, and #30 remains the next main engineering implementation item. This bundle is not the complete set of everything #37 requires; remaining required M0 engineering continues per the existing roadmap, including applicable #31–#36 Story/breaking work. #37 remains the final, explicit controlled production deployment/preflight/live-validation boundary once the required reviewed M0 changes (including this bundle) are ready; production deployment of the bundle occurs within #37 itself, not as a separate pre-#37 stage. Do not provision `ZERNIO_API_KEY` based on the automation's own Sep-6 text. The Astra content's `UNKNOWN`/`draft` state is a distinct operator publish-or-discard decision, not a release blocker.

Immediate next engineering order: #30 is the next main engineering implementation item; #31/#34 decision work and later Story/breaking implementation may proceed in parallel per existing roadmap; #37 is the controlled production deployment/preflight/live-validation boundary that deploys the reviewed #27/#28/#29/#30 bundle and observes genuine scheduled behavior before controlled production activation is considered complete. No separate, uncontrolled production deployment stage occurs before #37 except genuine emergency recovery of a concrete production failure under existing hotfix rules.

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
#4, #5, #27, #28 and #29 are complete in Git and merged (#28 via PR #42, squash merge commit `dee4ce1b3fc2ee9285454ea71d23b5eb63a76728`; #29 via PR #44, squash merge commit `d5db8ff0b907c0ea43b58da27f08c2d47eb94151`). No production deployment has been performed for #27, #28, or #29.

Current order:
1. the #3 read-only reliability proof evaluation is complete at repo/report level with a final verdict of FAIL — see `docs/reliability/2026-09-proof-verdict.md`; #3 itself is now CLOSED/COMPLETED (PR #46 merged as `875eeb715cac3c933b29694fec3c07fba094a39e`) — this closure records the verdict, it does not itself authorize production deployment or #37
2. #30 concise Telegram failure alerts consuming truthful domain health is the next main engineering implementation item — a repo-level implementation exists on branch `feature/n30-telegram-failure-alerts` (see "Repo-level #30 implementation" above) but is not merged and #30 is not closed
3. #31/#34 decision work may proceed in parallel as planned, then relevant Story/breaking implementation
4. once the reviewed prerequisite changes are ready, #37 performs the controlled production deployment/preflight/live-validation boundary — it deploys the exact reviewed SHAs/components required, including #27/#28/#29/#30 as applicable, and then observes genuine scheduled behavior before controlled production activation is considered complete; #37 also performs the scheduler-native `failureAlert` activation recorded in `docs/deployment/37-preflight-notification-requirements.md`

The #3 evaluation is complete at report level and #3 is now closed (PR #46 merged); GitHub development continues per this order. No uncontrolled production changes should occur; production deployment happens only through the explicit #37 controlled-deployment/validation gate, except genuine emergency recovery of a concrete production failure under existing hotfix rules. No synthetic production cycles.

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
