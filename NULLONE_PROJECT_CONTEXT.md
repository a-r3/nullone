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
- canonical planning issues #3–#14 exist; #4 and #5 are now CLOSED, while #3 and the remaining applicable planning issues stay open
- accidental duplicates #15–#26 closed
- operational issue #27 is CLOSED; #28–#37 remain open
- native dependencies created
- Project #5 exists
- GitHub Project field ordering for some M0 items may remain UI-housekeeping due transient GraphQL secondary rate limiting; this is not an engineering blocker

Relevant issues:
- #3 reliability proof/anomaly investigation — OPEN; proof verdict remains separate from repo engineering
- #4 executable completion/publication contracts — CLOSED; PR #38 squash-merged as `417cf400157dea95e19d8eb0a860c7bcb974e6e7`
- #5 offline behavioral regression tests — CLOSED; PR #39 squash-merged as `72e5c31e5bb3db922f30a2f8ea91c5b2d7ef8b41`
- #27 explicit domain run outcomes/health — CLOSED; PR #40 squash-merged as `c70047a9e3123d19b46968715c3fc294a51d69d4`
- #28 Morning Editorial network failure behavior — repo-level implementation complete on branch `feature/n28-morning-editorial-network-failures`; PR to `main` pending review
- #29 Daily Analytics Zernio access/runtime bootstrap
- #30 Telegram failure alerts
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


### #28 repo-level implementation — 2026-09-06
Issue #28 `Harden Morning Editorial API/network failure handling` is implemented at the repository level on branch `feature/n28-morning-editorial-network-failures`. Production deployment and real scheduled validation are NOT performed.

Added:
- `workspace/social/ops/scripts/nullone_editorial_runtime.py`: `run_morning_editorial()` classifies provider failures via `nullone_run_outcome`, retries only the confirmed transient `PROVIDER_UNREACHABLE` (ENOTFOUND/timeout/reachability) pattern up to 3 bounded attempts with deterministic backoff, and checks the required editorial-board artifact before every provider call so a retry or re-entry for the same occurrence can never repeat the board write or any state mutation. Every attempt for one scheduled occurrence shares the same `run_id`; once that run_id has a persisted terminal result the provider is never called again for it. Non-reachability errors fail immediately as `EDITORIAL_PROVIDER_ERROR` without retry.
- `workspace/social/ops/scripts/nullone-morning-editorial-run.py`: thin CLI wrapper (`execute`, `self-test`) exposing this as the wiring point a scheduler would call. Its default provider invocation is untested/unwired to production; all tests inject a fake provider.
- `tests/test_morning_editorial.py`: offline coverage for bounded retry-to-failure, transient-failure-then-success without duplicate mutation, a later distinct occurrence recovering normally, run/occurrence identity preservation across retries and re-entry, non-retryable errors, and a static guard that this module never references publication/Zernio.

This implementation does not touch `nullone-publish-bridge.py`, `nullone-publisher-run.py`, or `nullone-publish-notify.py`; publication `UNKNOWN`/no-auto-retry invariants are unchanged, and no retry behavior from this issue applies to publication.

Pre-merge correction: the retry policy's worst-case wall-clock cost is now an explicit, tested invariant. `OCCURRENCE_FAILURE_BUDGET_SECONDS = 480` (8 minutes), computed as `MAX_ATTEMPTS(3) * PROVIDER_CALL_TIMEOUT_SECONDS(120) + sum(RETRY_BACKOFF_SECONDS[:2])(30+90) = 480s`, chosen with a 120s margin under the confirmed ~10-minute (600s) minimum spacing between the 2026-09-05 failed occurrences, so a persistent reachability failure cannot still be running when the next scheduled occurrence starts. `nullone_editorial_runtime.worst_case_occurrence_seconds()` computes this and is asserted against the budget at import time; `tests/test_morning_editorial.py` covers it offline (no sleeping, no real provider calls). The provider-call timeout was reduced from an earlier unbounded-worst-case value of 900s (which allowed a ~47-minute worst case) to 120s for this reason.

Validation:
- `python3 tests/run_offline.py` → `OFFLINE_REGRESSION_SUITE=PASS` (23 run-outcome tests, 11 existing behavioral tests, 8 Morning Editorial tests, acceptance-contract validation, and all script self-tests, including the new one, PASS)
- no network or subprocess calls occur in the new tests; the real provider invocation path is never exercised

## Reliability proof
Baseline: 2026-09-04 03:47 Asia/Baku
Nominal end: 2026-09-06 03:47 Asia/Baku
Proof remains `INCOMPLETE` until the time window closes and exercised evidence is evaluated.

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
Daily Analytics on Sep 4 and Sep 5 reported scheduler/runtime:
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
#4, #5 and #27 are complete in Git. #28 is implemented in Git on a feature branch pending PR review. No production deployment has been performed for #27 or #28.

Current order:
1. keep #3 proof/evidence evaluation separate; do not mutate or synthetically contaminate proof state
2. review/merge #28 Morning Editorial network/runtime failure handling PR
3. #29 Daily Analytics Zernio scheduled-session bootstrap/access path
4. #30 concise Telegram failure alerts consuming truthful domain health
5. #31/#34 decision work may proceed in parallel as planned
6. later Story/breaking implementation
7. #37 remains the separate controlled production activation/validation gate

GitHub development may continue while proof evaluation is pending, but production deployment, synthetic production cycles and healthy-path production patching remain forbidden unless recovering a concrete production failure.

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
