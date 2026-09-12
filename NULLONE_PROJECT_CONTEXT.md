# NULLONE_PROJECT_CONTEXT

Last updated: 2026-09-12 Asia/Baku
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
No production activation occurred.

**#59 is CLOSED/COMPLETED.** PR #75 squash-merged as
`03603698291b2f6e5c0775067f15abb33f87fa63` (closes via `Closes #59`).
NullOne-owned scheduled occurrence authority is merged on `main`. The
Morning/Daily OpenClaw production path is now repository-complete as:

```text
static wake-up → NullOne schedule authority → SchedulerInvocation → workflow
```

Current M0 executable wake-up edge pins `--source` to reviewed `openclaw`
(`M0_WAKEUP_SOURCES`); generic authority remains multi-adapter. **No
production activation yet** — desired OpenClaw wake-up jobs remain
`DESIRED / NOT DEPLOYED`; live production jobs remain legacy until #37.

**#61 is CLOSED/COMPLETED** — secure scheduled injection for
`ZERNIO_ANALYTICS_API_TOKEN`. PR #77 squash-merged on `main` as
`b49bf36983b8db1042371d9809410a3d1881593c` (`Closes #61`). The reviewed
secret boundary (`nullone_secret_provider.py`) and real production factory
(`nullone_analytics_provider_factory.py`) are merged; the direct GET-only
Zernio API adapter (not MCP) receives its token as a `SecretValue`.
Component-level repository engineering for #27–#36/#59/#61/#62/#63/#65/#60/#66
is complete; that claim is unchanged. It no longer implies zero M0
engineering blockers: the 2026-09-08 NEW #37 read-only preflight returned
`BLOCKED` with newly-discovered deployment engineering blockers (recorded
below). The implementation is **NOT DEPLOYED** — no real secret provisioned,
no systemd/OpenClaw production activation. #37 remains OPEN / NOT READY YET.

### 2026-09-08 NEW #37 read-only preflight — VERDICT = BLOCKED

Strictly read-only against `main`
`d8822a2518f2a49670966b008db448b1efcb184a`; no production file, job,
service, credential, ledger, Zernio, Telegram, approval, or publication
mutation occurred. Morning Editorial and Daily Analytics repository paths
are deployable (production still legacy; exact deltas known; NOT blockers).
Stale production approval/publisher files are an expected deployment delta
(exact replacement/rollback known; no activation before replacement).
Historical ledger missing-format rows were rechecked current and are safely
handled by #60 (NOT a blocker). OpenClaw 2026.8.2 contracts remain
compatible.

Newly-discovered deployment engineering blockers:
- Story production trigger + authoritative candidate source (no reviewed
  Story job identity/schedule; no real reviewed `StoryCandidateProvider`/
  source; `candidate_availability` unresolved; repository tests use fakes
  only).
- Breaking production structured candidate source/handoff emitter (Radar
  still emits Markdown research output;
  `nullone.breaking-radar-handoff.v1` DESIRED / NOT DEPLOYED; Breaking also
  depends on the Story path).

Safety-relevant unresolved deployment unknown:
- Story/main scheduled-session Zernio DraftProvider live path
  (`LIVE_SCHEDULED_PATH_UNPROVEN`; host-level MCP health does not resolve
  it).

These are distinct from the completed #27–#36/#59/#61 repository components.
The historical 2026-09-07 preflight record below is unchanged.

### #79 Story production integration — CLOSED / COMPLETED

#82 merged at `7a8aea4419ebdb86a0b4825870a179255eaa2f3d`. #79 is
CLOSED/COMPLETED: PR #83 squash-merged on `main` as
`3d88d142c0757f4cdb70ae3e7c5861e1efe115bf` (`Closes #79`). Production
Story repository integration is complete:

- Story windows: 10:30 / 13:30 / 18:30 / 21:30 Asia/Baku (dedicated
  static wake-up `nullone-scheduled-wakeup.py story --source openclaw`,
  latest-due-slot coalescing, replay-stable identity);
- structured Morning handoff `nullone.editorial-candidate-handoff.v1` is
  the authoritative normal Story source (Morning writes board + handoff;
  partial output never re-invokes the provider; exact date/board
  binding; `story_eligible` requires PASS + READY);
- Story replay/provenance safety: persisted Story #27 replay authority
  first (identity-checked, corrupt fails closed), Morning #27 provenance
  before first execution only (source follows the invocation namespace);
- consumed-state reads fail closed; provider is rank-ordered, READY-only,
  Markdown-free;
- Draft Factory normal Story ownership superseded by StoryWorkflow in
  desired state (main FEED/CAROUSEL preserved; Breaking Story-first
  unaffected);
- DraftProvider live path still UNPROVEN (#81 untouched).

NOT DEPLOYED — no Story job exists, no live handoff, no Story production
run proof, no Zernio draft created, no Telegram preview sent, no OpenClaw
changes.

#81 is CLOSED/COMPLETED (PR #87 squash-merged as `8d5e6dfc1dc0fad48d0a1419945e31e134e1a6e8`; repository implementation is MERGED; NOT DEPLOYED).
NEXT_ENGINEERING_TASK = NEW read-only #37 preflight.
#37 is OPEN / BLOCKED pending a NEW read-only preflight; do not rerun yet. Do NOT mark #37 READY.

### #80 Breaking Radar production integration — CLOSED / COMPLETED

#84 merged at `8b1245b62b2bdd0aa7e93df6a88ad9989d886c3c`. #79 is CLOSED.
#80 is CLOSED/COMPLETED: PR #85 squash-merged on `main` as
`879e04b2db5adda9e0370d10771f626ef6c951ed` (`Closes #80`). Repository
implementation is MERGED. Implementation remains **NOT DEPLOYED** — no
production activation occurred, no synthetic Breaking event was created,
and live OpenClaw jobs/schedules/secrets remain untouched.

Finalized repository architecture:
- Live Radar verified read-only (`texbrif-breaking-radar`,
  `30 11,14,17,20,23 * * * Asia/Baku`, prompt-only agent job — preserved
  slots 11:30/14:30/17:30/20:30/23:30);
- Markdown remains non-authoritative;
- Authoritative schema: `nullone.breaking-radar-handoff.v1`;
- Deterministic scan-slot authority with latest-due coalescing (no backfill);
- Candidate-ID **shape** enforced deterministically at commit (stable-anchor
  semantic derivation remains Radar prompt contract; semantic slug provenance
  is not independently recomputed);
- Exact `nullone.breaking-radar-handoff.v1` envelopes committed atomically by
  the deterministic edge under canonical staging-root and spool-root containment
  (`social/ops/breaking-staging` and `social/ops/breaking-handoffs`); direct and
  parent-component symlink-mediated escapes fail closed before any mutation;
- Scan-directory symlinks fail closed;
- Scan receipt is the authoritative commit record — orphan handoff alone is not
  executable; crash-before-receipt recommit preserves original orphan handoff
  bytes across different retry `triggered_at` and repairs receipt listing, while
  assessment mutation conflicts (`COMMIT_CONFLICT`);
- Registry-backed scan identity (`validate_committed_scan_identity` /
  `resolve_radar_scan`): fabricated schedule IDs fail even when a receipt
  mirrors them; historical scans validate without wall clock;
- Handoff-to-receipt scan binding: every listed handoff is bound to the exact
  authoritative receipt scan before any workflow call — `source_occurrence_id`
  and `scheduled_for` must match the receipt exactly, and the computed candidate
  external ID must match the receipt-listed ID and filename;
- Authoritative mismatch, missing receipt, corrupt spool state, or scan-directory
  symlink fails the consumer sweep closed as `FAILED` / non-zero CLI
  (`SWEEP_AUTHORITY_CORRUPT`, `HANDOFF_SCAN_IDENTITY_MISMATCH`) with no source
  fallback;
- Static spool consumer (desired `45 11,14,17,20,23 * * * Asia/Baku`, NOT
  DEPLOYED) consumes only receipt-listed candidates, fails closed on
  unexpected runner crashes (non-zero CLI), and never treats establishment
  failures as ordinary processed work;
- Story-first preserved, no main provider (`BLOCKED_BEFORE_ATTEMPT`), no
  publication capability;
- Production #80 supplies no fake `dependency_recheck` (Radar/LLM dependency
  attestation is editorial evidence only, not dispatch-time recheck); #81 still
  owns real DraftProvider/live dependency readiness (`LIVE_SCHEDULED_PATH_UNPROVEN`);
- Fresh/replay #27 reason and notification-error classification semantics are
  identical.

#80 is CLOSED. Status of #81 below supersedes the #81-OPEN line recorded here at #80-merge time.
See "### #81 DraftProvider resolution/proof — CLOSED / COMPLETED" for current truth: #81 is CLOSED/COMPLETED (PR #87). #37 remains OPEN /
BLOCKED pending a NEW read-only preflight. No production activation occurred; no synthetic Breaking event.
**NOT DEPLOYED (MERGED != DEPLOYED).** Current live production remains the
old/current baseline.

### #81 DraftProvider resolution/proof — CLOSED / COMPLETED

#81 DraftProvider resolution/proof — CLOSED / COMPLETED. PR #87
squash-merged on `main` as
`8d5e6dfc1dc0fad48d0a1419945e31e134e1a6e8`.

Read-only investigation verdict was REPLACEMENT_REQUIRED:
- old review-draft transport: DraftConnector → nullone-draft-bridge.py →
  `claude -p` → Zernio MCP;
- merged desired review-draft transport: DraftConnector → existing
  nullone-draft-bridge.py stable CLI edge → deterministic direct Zernio
  REST DraftProvider.
- application/domain DraftConnector boundary remains provider-independent;
- review-draft path no longer depends on Claude/MCP bootstrap.

Dedicated draft/write secret logical id: `zernio.drafts.bearer`; env
binding NAME: `ZERNIO_DRAFT_API_TOKEN`. This credential is distinct from
the Analytics credential. No real credential provisioned yet. No Zernio
draft created during implementation. No production activation occurred.
MERGED != DEPLOYED.

Final #81 safety semantics (repository desired state; no live production
proof claimed):
- current Zernio REST/OpenAPI contract is used (base
  `https://zernio.com/api/v1`);
- presign → PUT → persist public_url; presigned upload URL remains
  memory-only; bearer token never sent to upload host;
- existing valid public_url values are reused;
- Sep-8 same-run partial progress is covered: first 3 media can persist
  successfully, 4th presign can fail, create_attempts remains 0, no draft
  create occurs, recovery reuses first 3;
- exact media URL/type/contentType binding;
- complete post validation uses the same canonical payload as create;
- payload uses mediaItems + platforms + isDraft=true;
- STORY contentType is nested in Instagram platformSpecificData;
- publishNow absent; scheduledFor absent;
- deterministic UUIDv5 x-request-id is defense-in-depth only;
- max exactly one POST /posts; ambiguity after create request =>
  REVIEW_UNKNOWN; no automatic retry;
- exact one-target readback required;
- malformed presign provider response => sanitized BLOCKED before attempt;
- authenticated POST capability is exact-path allowlisted to:
  `/media/presign`, `/tools/validate/media`, `/tools/validate/post`,
  `/posts`;
- no publisher/publication capability added.

NOT DEPLOYED — no live secret readiness, no actual production factory
construction, no exact deployment delta, no scheduler mapping, no live
Zernio draft creation, no Telegram preview, and no end-to-end production
health proven by this merge. Those belong to a NEW #37 preflight /
controlled deployment.

Current required order:

```text
NEW read-only #37 preflight
→ READY_FOR_CONTROLLED_DEPLOYMENT?
→ controlled deployment only if READY
```

Historical 2026-09-07 preflight and 2026-09-08 BLOCKED preflight evidence
are preserved unchanged. The blockers discovered by the prior 2026-09-08
preflight (#79/#80/#81) are now resolved in Git, but this does NOT
retroactively turn the historical preflight into READY. A NEW read-only
#37 preflight is required against the exact current main. Do NOT rerun
#37 in this task. Do NOT provision `ZERNIO_DRAFT_API_TOKEN` or any other
secret. Do NOT deploy.

1. Repository engineering for #61 is merged (PR #77). Do not start
   production secret provisioning from this context record alone.
2. 2026-09-08 preflight complete: verdict `BLOCKED` (see above); narrow
   follow-up production-integration issues own the blockers.
3. Only if a NEW read-only #37 preflight returns
   `READY_FOR_CONTROLLED_DEPLOYMENT`, perform the separately controlled
   deployment under #37; otherwise report exact blockers, no deploy.
4. Observe natural production behavior; do not manufacture live proof.

Required order:

```text
NEW read-only #37 preflight
→ READY_FOR_CONTROLLED_DEPLOYMENT?
→ only then controlled deployment
```

Only READY permits deployment. No ad-hoc deployment is permitted.

GitHub desired state != production actual state. Current live production
remains the old/current baseline until a controlled deployment under #37.
PR #87 merge alone does not prove: live secret readiness, actual
production factory construction, exact deployment delta, scheduler
mapping, live Zernio draft creation, Telegram preview, or end-to-end
production health.

Existing M1/M2/M3 remain intact.

New operational milestone:
`M0 — Production operations are healthy and timely`

### #89 Deterministic final publish handoff — CLOSED / COMPLETED

#89 CLOSED / COMPLETED. PR #91 squash-merged on `main` as
`f1315f74ed08ab235f524c3777e86c7d5bb703ee`.

#89 desired architecture (repository desired state; implementation MERGED /
NOT DEPLOYED):

```text
final Telegram publish callback
→ deterministic OpenClaw interactive plugin
→ authenticated private pipe
→ deterministic controller
→ per-human authorization receipt
→ in-process publication core
→ deterministic notifier
```

- approval LLM no longer owns the final sessions_send handoff;
- publisher LLM no longer owns consequential wrapper execution;
- legacy raw execute path fail-closed;
- max-one publication attempt preserved;
- ambiguity truthful / no retry.

### #90 Deterministic Zernio publisher connector — CLOSED / COMPLETED

#90 CLOSED / COMPLETED. PR #92 squash-merged on `main` as
`d790aa78564c9e8889c94e9d3b5a2c0f40db774c`.

#90 desired architecture (repository desired state; implementation MERGED /
NOT DEPLOYED):

```text
publication core
→ deterministic direct Zernio REST adapter
→ read-only remote draft preflight
→ attempts=1 persisted
→ exactly one PUT promotion
→ read-only readback
```

- Claude/MCP removed from the consequential publication transport;
- ZERO consequential LLM decisions after the final human click in desired
  Git state;
- publication credential uses the OpenClaw protected SecretRef
  (`zernio.publish.bearer` / store entry `ZERNIO_PUBLISH_API_TOKEN`);
- plugin SecretInput materialized at activation;
- credential crosses only the authenticated private plugin→controller
  channel;
- no inherited-env publication credential;
- no real credential provisioned;
- no live Zernio calls during implementation.

All known repository M0 engineering blockers #79/#80/#81/#89/#90 are CLOSED
in Git. This does NOT mean production is ready or deployed.

NEXT_ENGINEERING_TASK = NONE
NEXT_OPERATIONAL_TASK = NEW read-only #37 preflight

#37 remains OPEN and must determine: `READY_FOR_CONTROLLED_DEPLOYMENT` or
`BLOCKED`. Historical 2026-09-07 and 2026-09-08 preflight records are
preserved as historical evidence below; old incidents are not rewritten as
if they occurred on the new code.

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

**Implemented and merged (2026-09-07):** this decision is on `main` via
PR #75 squash `03603698291b2f6e5c0775067f15abb33f87fa63` (#59
CLOSED/COMPLETED). Repository path only — **not** production-activated:

- `nullone_schedule_registry.py` — the exact two-entry M0 `ScheduleSpec`
  registry (`morning-editorial.daily.v1` at `08:30:00 Asia/Baku`;
  `daily-analytics.daily.v1` at `03:20:00 Asia/Baku`), immutable/reviewed
  code, not a mutable/generic schedule store.
- `nullone_scheduled_occurrence_authority.py` —
  `resolve_scheduled_occurrence(workflow_id, source, triggered_at)`, a pure
  function (no I/O, no persistence, no new ledger) that resolves the exact
  NullOne-owned daily slot due for an observed wake instant, with an
  explicit no-backfill rule (an early wake never falls back to a previous
  local date's slot) and same-day-replay-stable identity derivation.
- `nullone-scheduled-wakeup.py` — the static wake-up-only executable edge
  (`nullone-scheduled-wakeup.py morning --source openclaw` /
  `... analytics --source openclaw`), needing no `scheduled_for` argument,
  no per-occurrence trigger file, and no OpenClaw per-run UUID. The generic
  occurrence authority remains multi-adapter (`source` is part of #65
  identity), but this **current M0 production wake-up edge** pins
  `--source` to reviewed `openclaw` only (`M0_WAKEUP_SOURCES`) and fails
  closed on unreviewed/typo sources and on timezone-naive or non-datetime
  injected clocks — before any dispatch/provider/notifier/#27 side effect.
- `nullone_scheduled_run_dispatch.py` — the shared production dependency
  wiring both `nullone-scheduled-run.py` (exact-trigger-file path) and
  `nullone-scheduled-wakeup.py` (wake-up path) invoke identically, so
  neither duplicates the Claude CLI provider / `AnalyticsProvider` factory
  / OpenClaw-Telegram notifier binding.
- No scheduler database, job queue, cron-parsing engine, or
  occurrence-claim ledger was added — #28/#29's existing per-occurrence
  lock and persisted #27 result remain the sole idempotence/concurrency
  mechanism once a `scheduled_for` is resolved.
- `nullone_openclaw_scheduler_adapter.map_openclaw_occurrence` and
  `nullone-scheduled-run.py --trigger-file` are unchanged and not deleted:
  both remain valid reference/legacy code for a caller that already
  possesses an exact `scheduled_for` from some other reviewed source; they
  are not this repository's OpenClaw 2026.8.2 production path.
- Full detail, worked boundary proofs (Morning 08:30/09:07 identity, Daily
  Analytics UTC/Baku boundary), the exact static desired OpenClaw
  `automations create` command (`DESIRED / NOT DEPLOYED`), and the
  adapter-cutover-safety note are in
  `docs/deployment/59-scheduled-workflows-deployment.md`'s "NullOne
  Scheduled Occurrence Authority" section.

Live truth after merging the #61 work: desired OpenClaw wake-up jobs remain
`NOT DEPLOYED`; existing production jobs remain legacy prompt-only until
#37; the canonical secret file
(`~/.config/nullone/secrets/zernio-analytics.env`) is **NOT CREATED**; the
`EnvironmentFile=` drop-in is **NOT APPLIED**; the Gateway was **NOT
restarted**; no real Zernio credential has been provisioned; live Zernio
auth has **NOT** been tested; no live Daily/Story/Breaking proof is
claimed. The identity/source contract (`zernio.analytics.bearer` via
`EnvironmentSecretProvider`) is documented in
`docs/deployment/61-secure-analytics-secret-injection.md`. No production/
OpenClaw job/Zernio/Telegram/Claude action occurred while implementing or
merging the #61 work.

## GitHub planning / engineering state
Repository: `a-r3/nullone`
Current visibility: **public** as of 2026-09-05 18:34 so the ChatGPT GitHub connector can inspect it. This was described by Rauf as being made public for connector access; do **not** infer that the long-term private-repo policy was permanently abandoned unless Rauf explicitly confirms that.
Local clone: `~/nullone-repo-staging`

Verified planning:
- M1/M2/M3 exist
- M0 exists as milestone #4
- canonical planning issues #3–#14 exist; #3, #4 and #5 are now CLOSED (PR #46, #38, #39 respectively), while the remaining applicable planning issues stay open
- accidental duplicates #15–#26 closed
- operational component issues #27–#36, application-runtime architecture issue #65, cadence compatibility #60, Story workflow/review delivery #62, BreakingWorkflow orchestration #63, scheduled occurrence authority #59, scheduled credential injection #61, the narrow legacy-publication-instruction blocker #66, deterministic final publish handoff #89, and deterministic Zernio publisher connector #90 are CLOSED/COMPLETED; **all known repository M0 engineering blockers #79/#80/#81/#89/#90 are CLOSED in Git** (2026-09-08 preflight discovered separate deployment production-integration blockers — Story trigger/candidate source, Breaking structured source — recorded under Current execution priority; #79/#80/#81 were since closed by PR #83/#85/#87), while parent deployment issue #37 remains OPEN / NOT READY YET; #6 remains OPEN in M1 and #13 remains OPEN in M3
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
- #37 controlled production activation/validation — OPEN / NOT READY YET; its historical 2026-09-07 read-only preflight returned `BLOCKED` and remains preserved as historical evidence; it is not `READY`. Repository engineering is now complete (including #61, PR #77 merged), so #37 is eligible for a NEW strictly read-only preflight — but it is NOT automatically `READY_FOR_CONTROLLED_DEPLOYMENT`. Required sequence: `repository engineering complete → read-only #37 preflight → READY_FOR_CONTROLLED_DEPLOYMENT?` (`NO` → report exact blockers, no deploy; `YES` → separately controlled deployment). Only READY permits deployment. Deployment remains prohibited until that new preflight returns READY.
- #59 NullOne scheduled workflow orchestration — **CLOSED/COMPLETED**; PR #75 squash-merged as `03603698291b2f6e5c0775067f15abb33f87fa63` (foundation earlier via PR #73 `cec185f9a62c2055175454f06d3d5c54596bba23`). Merged NullOne-owned scheduled occurrence authority: static wake-up → NullOne schedule authority → `nullone.scheduler-invocation.v1` → `MorningWorkflow`/`AnalyticsWorkflow`. Current M0 executable source pinned to `openclaw`; generic authority remains multi-adapter. OpenClaw direct `scheduled_for` injection remains impossible in 2026.8.2 (historical `CONFIRMED BLOCKED` fact unchanged). **No production activation**: desired OpenClaw wake-up jobs `DESIRED / NOT DEPLOYED`; existing live jobs still legacy until #37; Daily real analytics secret wiring implemented but NOT DEPLOYED (see #61); no live proof claimed.
- #60 cadence-state backward compatibility — CLOSED/COMPLETED; its merged read-only compatibility implementation supplies the authoritative load behavior reused by #62/#63; no production ledger migration was performed or required (`migration_required: False`)
- #61 secure scheduled analytics credential injection — **CLOSED/COMPLETED**; PR #77 squash-merged as `b49bf36983b8db1042371d9809410a3d1881593c` (`Closes #61`).
    Merged: the reviewed secret boundary
    (`nullone_secret_provider.py`: `SecretValue`, `EnvironmentSecretProvider`,
    presence probe/readback, exact `lstat`-based file/dir mode enforcement
    0600/0700 with parent-symlink rejection) and the real production factory
    (`build_production_analytics_provider` → #29 `ZernioReadOnlyAnalyticsConnector`
    with canonical account id); missing/blank/rejected credential →
    `ConnectorUnauthorizedError` → `BLOCKED`, typed `SecretUnavailableError` →
    `ConnectorUnavailableError` → `BLOCKED`; unexpected programming defects
    (`RuntimeError`, `TypeError`, `AttributeError`, `AssertionError`) from
    the provider now propagate as `RUNTIME_CRASHED`; `SecretValue` is
    deliberately unhashable; missing-secret is a graceful blocked domain
    outcome (scheduled CLI exit 0), never a crash. Direct GET-only Zernio
    API adapter, NOT MCP. **NOT DEPLOYED** — no real credential provisioned,
    no systemd drop-in applied, no OpenClaw job created; production injection
    mechanism (systemd user `EnvironmentFile` → Gateway process env → child
    commands) verified read-only on the host 2026-09-08 and documented in
    `docs/deployment/61-secure-analytics-secret-injection.md`.
    Canonical default secret path fixed to `~/.config/nullone/secrets/zernio-analytics.env`.
    Immediately before the 2026-09-08 NEW preflight, all previously-known M0
    repository engineering blockers had been closed; that preflight then
    discovered the new production-integration blockers #79/#80 and readiness
    gate #81 (see Current execution priority).
- #62 `StoryWorkflow` and shared review-delivery adapter — CLOSED/COMPLETED; PR #70 squash-merged as `8d6d9844f0c494e3a813180fb7e83e87f713e745`; live scheduled Zernio/Telegram proof remains unproven
- #63 `BreakingWorkflow` orchestration — CLOSED/COMPLETED; PR #71 squash-merged as `34b35cba7bea0c366096bfbfd5d7141743c87189`; repository-level implementation is complete and preserves the exact #34/#36 verification vocabulary, defers optional-main preparation until after Story review delivery succeeds, derives candidate-level Radar occurrences, and emits contract-safe #27 mappings; no production activation occurred, strict Radar handoff remains `DESIRED / NOT DEPLOYED`, natural Breaking live proof remains `UNPROVEN_LIVE / DEFERRED_TO_#37`, and the scheduled Zernio path remains `LIVE_SCHEDULED_PATH_UNPROVEN`
- #65 NullOne Application Runtime architecture — CLOSED/COMPLETED; the accepted contract establishes NullOne workflow ownership and replaceable provider adapters
- #66 remove reachable legacy publication instructions/capabilities — CLOSED/COMPLETED; no production activation was implied by repository completion
- #89 deterministic final publish handoff — CLOSED/COMPLETED; PR #91 squash-merged as `f1315f74ed08ab235f524c3777e86c7d5bb703ee` (`Closes #89`); implementation MERGED, NOT DEPLOYED
- #90 deterministic Zernio publisher connector — CLOSED/COMPLETED; PR #92 squash-merged as `d790aa78564c9e8889c94e9d3b5a2c0f40db774c` (`Closes #90`); implementation MERGED, NOT DEPLOYED

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
- `workspace/social/ops/scripts/nullone_zernio_analytics_adapter.py`: the only module aware of Zernio-specific HTTPS paths, response envelopes and credentials. `ZernioReadOnlyAnalyticsConnector` exposes exactly four GET-only methods (`get_account`, `get_follower_history`, `get_account_insights`, `get_post_analytics`) mapped to Zernio's documented read-only analytics endpoints, and never calls anything on its transport but `.get(...)` — there is no create/update/delete/publish/draft/schedule/message/comment capability anywhere in this module. This deliberately bypasses generic MCP tool dispatch — the confirmed #29 root cause is a scheduled-session `bundle-mcp` bootstrap/runtime-availability failure, not a Zernio outage or allowlist defect — so analytics no longer depends on that bootstrap path. The credential never appears in code, logs, or reason text; no credential is committed. With #61, the adapter receives its token as an opaque `SecretValue` (`build_authenticated_transport(token=...)`) whose value is revealed only inside the Authorization header at request time; it never reads environment variables, and its transport `repr` renders only `<redacted>`.

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

Final retry policy (as reviewed 2026-09-06; **superseded 2026-09-11, see "Confirmed Morning Editorial whole-agent timeout defect" below** — the 210s/480s numbers here are historical, not current production policy): the retry policy's worst-case wall-clock cost is an explicit, tested invariant. `MAX_ATTEMPTS = 2`, `PROVIDER_CALL_TIMEOUT_SECONDS = 210`, `RETRY_BACKOFF_SECONDS = (60,)`, giving `OCCURRENCE_FAILURE_BUDGET_SECONDS = 480`, computed as `2 * 210 + 60 = 480s`. Policy safety is enforced by `validate_occurrence_policy()` raising `UnsafeRetryPolicyError` (not a Python `assert`) so a misconfigured policy fails loudly instead of being silently disabled under `-O`. `nullone_editorial_runtime.worst_case_occurrence_seconds()` computes the worst case and `validate_occurrence_policy()` checks it against the budget at import time; `tests/test_morning_editorial.py` covers it offline (no sleeping, no real provider calls).

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

Unresolved risks and release restrictions (owners/next actions in full in the report): #27 (domain outcomes/health), #28 (Morning Editorial runtime), #29 (Daily Analytics runtime/adapter), and #30 (failure alerts) were the **proof-derived blocking operational bundle** this verdict identified — all four are now merged in Git (#30 via PR #47, squash commit `31ac4cca9e4255d5ba665ea42989ab9237eb05c2`) but none are deployed to production. #36 breaking draft routing is also merged/completed, so #27–#36 component-level repo engineering is complete. The 2026-09-07 #37 read-only preflight identified deployable-integration prerequisites #59–#63; #59/#60/#61/#62/#63/#65/#66 are now CLOSED/COMPLETED — immediately before the 2026-09-08 NEW preflight, all previously-known M0 repository engineering blockers had been closed; that preflight then discovered the new production-integration blockers #79/#80 and readiness gate #81 (see Current execution priority). #37 remains OPEN / NOT READY YET; the 2026-09-08 preflight returned BLOCKED, and deployment remains prohibited until a NEW preflight returns READY. Do not provision `ZERNIO_API_KEY` based on the automation's own Sep-6 text. The Astra content's `UNKNOWN`/`draft` state is a distinct operator publish-or-discard decision, not a release blocker.

Immediate next engineering order (updated after 2026-09-08 #37 preflight BLOCKED): #27–#36, #65, #60, #62, #63, #66, #59 and **#61** are accepted and merged at component level, but NEW deployment engineering blockers were discovered — Story production trigger + authoritative candidate source, Breaking production structured candidate source/handoff emitter — plus one safety-relevant unresolved unknown (Story/main scheduled-session DraftProvider live path). Required sequence: `Story production integration → Breaking production integration → resolve DraftProvider production-path proof → NEW read-only #37 preflight → READY_FOR_CONTROLLED_DEPLOYMENT?`. Only READY permits deployment under #37; then observe genuine scheduled behavior. #37's acceptance criteria are unchanged. Desired OpenClaw wake-up jobs remain `NOT DEPLOYED`. No separate, uncontrolled production deployment stage occurs before #37 except genuine emergency recovery of a concrete production failure under existing hotfix rules.

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

### Confirmed Morning Editorial whole-agent timeout defect — 2026-09-11
The 2026-09-11 08:30 Asia/Baku natural Morning Editorial occurrence (`run_28849dc4436e74d25ae99dbf`) persisted `domain_outcome=FAILED`, `reason_code=PROVIDER_UNREACHABLE`, `reason_text="Provider/runtime API was not reachable. (2 attempts)"`. Read-only forensic inspection of both natural Claude Code sessions (local session transcripts under `~/.claude/projects/`) proved this was misclassified:
- Attempt 1 (`2837ea70-3ffb-4c06-8f4f-c93cc75e769d`) showed continuous, successful WebSearch/WebFetch/Bash agent activity for ~208.7s, killed externally at the 210s outer subprocess deadline while still actively working.
- Attempt 2 (`296f7d39-0046-4176-9a9e-37b99d23099a`) showed successful tool activity for ~76s, then a distinct silent tail before being killed at the same outer deadline.
- No artifact (board or structured handoff) was ever written by either attempt.
- Downstream: the same-day Story natural occurrences correctly failed closed with `MORNING_SOURCE_UNPROVEN` (fail-closed behavior working as designed — Story was not the defect).

Root cause: `nullone_claude_editorial_provider.default_invoke_provider` unconditionally converted `subprocess.TimeoutExpired` (the entire `claude -p` agent process exceeding its outer wall-clock deadline) into `ProviderUnreachableError`, the same retryable classification used for genuine DNS/connect reachability failures. A whole-agent-process timeout is not proof of unreachability — proven live evidence shows the child can still be actively succeeding at real work when killed — so this conflation caused an automatic, expensive second full Morning attempt on a run that was simply too slow for its 210s budget, not actually failing.

Fix (branch `fix/morning-agent-timeout-semantics`): introduced a distinct `ProviderExecutionTimeoutError` (NOT a subclass of `ProviderUnreachableError`) for the whole-process-timeout case; `classify_provider_failure()` now maps it to its own non-retryable `reason_code=PROVIDER_EXECUTION_TIMEOUT`; genuine `ENOTFOUND`/`EAI_AGAIN`/`ETIMEDOUT`/`ECONNREFUSED`/"can't reach the api server" reachability signatures remain `ProviderUnreachableError` and remain retryable, unchanged. `PROVIDER_CALL_TIMEOUT_SECONDS` raised from the old single-sample-derived 210s to a reviewed 600s. The stale `_MIN_OBSERVED_OCCURRENCE_SPACING_SECONDS=600` historical-spacing invariant (which never modeled Morning's actual one-slot-per-day schedule) was replaced with a schedule-registry-derived invariant: the occurrence failure budget must stay strictly below `morning_to_first_story_gap_seconds()` (currently 7200s, 08:30→10:30 Asia/Baku). New worst-case retry cost is 1260s (`MAX_ATTEMPTS=2 × 600s + 60s backoff`); `OCCURRENCE_FAILURE_BUDGET_SECONDS=1800` was chosen with headroom above that worst case and comfortably below the 7200s gap. The live OpenClaw Morning automation's own scheduler-level job timeout (`payload.timeoutSeconds=1800`) was read-only confirmed to already accommodate the new 1260s worst case before this change was finalized.

This is a repo-level fix only, matching this doc's existing MERGED ≠ DEPLOYED distinction: it corrects the classification/timeout-sizing code and is not itself a production deployment. Deployment of this change to the live Morning automation remains pending and happens only under #37's controlled-deployment gate, same as every other item in this bundle.

### Confirmed Morning structured-handoff contract defect — 2026-09-12
Natural production proof, read-only audit of the 2026-09-12 00:00–13:43 Asia/Baku window: Morning Editorial's provider **completed successfully** (`run_5fa70f500924382b7e27fcc5`, ~605s, well inside the PR #100 600s/1800s budgets, no timeout, no unreachability) and wrote both `2026-09-12-editorial-board.md` and `2026-09-12-editorial-candidates.json`. The structured handoff nonetheless failed the strict validator (`nullone_editorial_candidate_handoff.py`) and persisted `domain_outcome=FAILED`, `reason_code=HANDOFF_INVALID`, because every candidate object used `"verification_status"` instead of the contracted literal key `"verification"` — reproduced directly by re-running the real `load_handoff_snapshot()` against the actual on-disk file (`EditorialHandoffError: candidate[0] carries unknown field(s): ['verification_status']`). Root cause traced to `social/ops/prompts/morning-editorial.md`, which specified the field in prose as "verification status" (two words) rather than the literal key, next to otherwise-exact snake_case field names in the same sentence — the provider resolved the ambiguity plausibly but non-compliantly.

Both natural Story occurrences (10:30 and 13:30 Asia/Baku) independently failed closed with `reason_code=MORNING_SOURCE_UNPROVEN` at the Morning-provenance gate (`_morning_source_proven`), before ever loading the handoff or evaluating candidates — correct fail-closed behavior, not a Story defect. `Story Zernio create attempts=0`, `Story Telegram sends=0`, `Story publication attempts=0` for both occurrences. Scheduler triggered both slots exactly on time; no Gateway restart or infrastructure anomaly caused or contributed to the failure. This is a **different failure signature** than the 2026-09-11 `PROVIDER_UNREACHABLE` incident above — the provider ran fine this time; the defect is a producer/contract field-name mismatch, not reachability or timeout.

Fix (branch `fix/morning-handoff-verification-contract`): rewrote the structured-handoff section of `morning-editorial.md` to list every candidate field as an exact literal JSON key (including `` `verification` ``), explicitly forbid emitting `verification_status`, and add a worked JSON example that passes the real validator. The strict validator itself is intentionally **unchanged** — it behaved correctly by failing closed on malformed provider output, and no alias/tolerance for `verification_status` was added. This is a repo-level fix only, matching this doc's MERGED ≠ DEPLOYED distinction: deployment of the corrected prompt to the live Morning automation remains pending and requires a controlled deployment step after merge, followed by natural production proof on a subsequent Morning occurrence — do not consider this closed until that proof exists.

Separately confirmed, **not** addressed by this fix: `StructuredHandoffStoryProvider` reads only the single validated same-day Morning handoff snapshot; Breaking Radar/intraday candidates have no code path into Story, so an event discovered mid-day (e.g. an 11:00 Radar scan) cannot reach a 13:30 Story slot regardless of Morning's outcome (`STORY_MORNING_DEPENDENCY=HARD`, `INTRADAY_REFRESH=NO`, `FALLBACK_SOURCE=NONE`). This is a distinct, pre-existing design gap, not caused by and not fixed alongside the schema mismatch above — it requires separate reviewed design work. #37 remains OPEN.

### Editorial provider transport decision — 2026-09-12 (OpenCode primary, Claude fallback)

NEW operational fact: Claude Code is currently inaccessible to Rauf. Therefore the editorial provider architecture migrates from a Claude-Code-specific transport to a provider-neutral architecture, side-by-side, with no live provider switch in the migration PR itself:

- OpenCode is now the intended primary editorial transport (`nullone_opencode_editorial_provider.py`: `opencode run --agent nullone-editorial --model <provider/model> --format json`, fresh isolated session per run, no `--auto`, checked-in narrow agent boundary, externally enforced 600s timeout, failure classification identical to the Claude adapter).
- Claude Code is retained, not removed, as the fallback/rollback transport (`nullone_claude_editorial_provider.py` unchanged; existing Claude adapter tests unchanged).
- Selection is explicit and deterministic (`NULLONE_EDITORIAL_PROVIDER=opencode|claude` via `nullone_editorial_provider_factory.py`); unknown values fail closed; no silent fallback inside a run. Repository default stays `claude` (safe compatibility) — switching the live transport is a separate, explicit deployment decision.
- PR #108's handoff-contract code fix is deployed, but its next natural proof may occur only after provider transport changes, because Claude access became an operational blocker. Future natural proof must distinguish: (a) handoff-contract success (validator accepts the `verification` field) from (b) OpenCode transport success (the new adapter completes a real Morning cycle within budget).
- OpenCode production activation is NOT claimed until it actually happens. Issue #37 remains OPEN.

### OpenCode editorial model policy — 2026-09-12/13 (Muse Spark 1.3, issue #111 owns future routing)

Explicit operational/product decision from Rauf: the current reviewed OpenCode editorial model is **Muse Spark 1.3** (`opencode/muse-spark-1.3-contributor-free`, `DEFAULT_OPENCODE_MODEL` in `nullone_opencode_editorial_provider.py`). This is deliberate, not an accidental fallback. Sonnet is currently unavailable and that is NOT a blocker. `NULLONE_OPENCODE_MODEL` remains as an explicit override mechanism only — it is not required to make the current migration work.

The checked-in `nullone-editorial` agent boundary was hardened on PR #110 before human approval: writes default-denied, allowed only on the four Morning artifact/state paths proven required by the current prompt (`social/research/daily/*-editorial-board.md`, `social/research/daily/*-editorial-candidates.json`, `social/state/candidate-queue.md`, `social/state/topic-ledger.jsonl`); secret-bearing reads (`.env`-family, keys) denied; shell/task/skills/outside-worktree denied; no `--auto`. The `**/` pattern prefix is load-bearing (the edit tool matches absolute paths; bare relative patterns silently block even required writes) and the scoping was proven live against OpenCode 1.18.30 in disposable sandboxes: allowlisted write succeeds, off-scope write blocked, secret read blocked.

Continuity: PR #110 creates only the immediate minimal provider-neutral seam (OpenCode + Muse Spark as the current primary target, Claude retained as installed fallback). Future logical-role → provider → model routing (Morning, Draft Factory, Radar, analytics, image generation, etc. possibly on different providers/models simultaneously) belongs to issue #111 ("Generalize AI provider adapters and role-based multi-provider routing"). #111 must not block urgent OpenCode activation and is NOT implemented in PR #110.

### Story writer provider migration — issue #112 (repo-level, NOT DEPLOYED)

Urgent operational policy (issue #112): all active model-backed NullOne workflows move to OpenCode while Claude Code and current Anthropic Claude/Sonnet/Haiku access are unavailable. Morning already migrated under PR #110. The Story writer (`HaikuStoryWriter` → `nullone_claude.run_structured` → `claude -p --model haiku`) is the next and currently only remaining active Claude Code CLI dependency; its migration removes the last active `claude`-binary execution path while preserving Claude as installed fallback infrastructure.

Repo-level implementation (NOT DEPLOYED, no live Story switch in the PR): `nullone_opencode_story_provider.py` (`OpenCodeStoryWriter`: same shared `_writer_prompt`, JSON-only transport framing, Muse Spark, `--format json` parsing, 300s external timeout matching the previous structured default, empty-string stripping, transport errors → `BridgeError` → `WRITER_FAILED`, unparseable output → `BridgeError` like the Claude non-JSON path, wrong-shape dicts returned for the pipeline's own `WRITER_OUTPUT_INVALID` validation); `nullone_story_provider_factory.py` (`NULLONE_STORY_PROVIDER=opencode|claude`, default `claude`, unknown fails closed, no silent fallback); checked-in `nullone-story-writer` agent denying every tool (mirrors the Claude writer's empty `allowed_tools`; proven live in a disposable sandbox: exact JSON returned, filesystem write refused); dispatch resolves the writer from the factory and stamps `story_provider` in result context. Domain behavior (provenance gate, eligibility, cadence, quiet-day no-op, spec schema, verifier, no delivery capability) is unchanged — transport only. #111 remains the future role-based architecture. #37 remains OPEN.

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

### Current Git desired-state M0 execution order — after #89/#90 merge (PR #91/PR #92)

1. #59 is CLOSED/COMPLETED: PR #75 squash-merged as
   `03603698291b2f6e5c0775067f15abb33f87fa63` (foundation PR #73
   `cec185f9a62c2055175454f06d3d5c54596bba23`). NullOne-owned scheduled
   occurrence authority is on `main`; current M0 wake-up edge pins
   `source=openclaw`; no production/OpenClaw activation occurred.
2. #61 is CLOSED/COMPLETED: PR #77 squash-merged as
   `b49bf36983b8db1042371d9809410a3d1881593c`. Immediately before the
   2026-09-08 NEW preflight, all previously-known M0 repository engineering
   blockers had been closed; that preflight then discovered the new
   production-integration blockers #79/#80 and readiness gate #81. Do not
   provision secrets or mutate production from this context record alone.
3. #79 is CLOSED/COMPLETED: PR #83 squash-merged as
   `3d88d142c0757f4cdb70ae3e7c5861e1efe115bf` (`Closes #79`). Production
   Story repository integration complete; NOT DEPLOYED.
4. #80 is CLOSED/COMPLETED: PR #85 squash-merged on `main` as
   `879e04b2db5adda9e0370d10771f626ef6c951ed` (`Closes #80`). Production
   Breaking Radar structured handoff repository integration complete.
   Implementation is MERGED; remains NOT DEPLOYED; no production activation
   or synthetic Breaking event occurred.
5. #81 is CLOSED/COMPLETED: PR #87 squash-merged on `main` as
   `8d5e6dfc1dc0fad48d0a1419945e31e134e1a6e8`. Direct Zernio REST
   DraftProvider repository implementation complete (REPLACEMENT_REQUIRED;
   `zernio.drafts.bearer` / `ZERNIO_DRAFT_API_TOKEN` distinct from
   Analytics credential; no credential provisioned; no Zernio draft
   created). Implementation is MERGED; remains NOT DEPLOYED.
6. #89 is CLOSED/COMPLETED: PR #91 squash-merged on `main` as
   `f1315f74ed08ab235f524c3777e86c7d5bb703ee` (`Closes #89`).
   Deterministic final publish handoff repository implementation complete
   (plugin-authenticated ingress, in-process publication core, deterministic
   notifier; approval/publisher LLMs out of the consequential path).
   Implementation is MERGED; remains NOT DEPLOYED.
7. #90 is CLOSED/COMPLETED: PR #92 squash-merged on `main` as
   `d790aa78564c9e8889c94e9d3b5a2c0f40db774c` (`Closes #90`).
   Deterministic Zernio REST publisher repository implementation complete
   (remote-draft preflight, attempts=1 before exactly one PUT promotion,
   readback clarification; Claude/MCP out of the publication transport;
   publication credential via protected SecretRef + private pipe, no
   inherited-env credential, nothing provisioned, no live calls).
   Implementation is MERGED; remains NOT DEPLOYED.
8. NEXT_ENGINEERING_TASK = NONE. NEXT_OPERATIONAL_TASK = NEW read-only #37
   preflight. All known repository M0 engineering blockers
   (#79/#80/#81/#89/#90) are CLOSED in Git, but this does NOT
   retroactively turn any historical preflight into READY, and it does NOT
   mean production is ready or deployed. A NEW read-only #37 preflight is
   required against the exact current main (historical 2026-09-07 and
   2026-09-08 preflights remain preserved as historical evidence; DO NOT
   rerun #37 yet; do NOT mark #37 READY; do NOT provision secrets; do NOT
   deploy).
7. Only a `READY_FOR_CONTROLLED_DEPLOYMENT` verdict permits controlled
   deployment under #37; otherwise report the exact blockers with no deploy.
   No ad-hoc deployment is permitted (MERGED != DEPLOYED).
8. Observe natural Morning/Daily/Story/breaking/alert behavior under #37.

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

## Editorial Cadence V2 — repository design only (2026-09-11)

A. PR #100 merged: `10ca76f50962a1f125bd2f1222e02aab2ec8fae8`.

B. PR #100 controlled production deploy SUCCESS on 2026-09-11:
- exact two runtime files deployed
- hash match PASS
- provider timeout 600
- execution-timeout classification separated
- no automation changes
- no force runs
- rollback: `/home/oem/.openclaw/rollback-backups/pr100-20260911T141748Z`

C. Natural Morning/Story proof remains pending for the next normal
occurrences.

D. Editorial Cadence V2 (`docs/contracts/editorial-cadence-v2.md`,
branch `feature/editorial-cadence-v2-contract`) is repository design
only until separately implemented/reviewed/deployed. It changes no
production runtime, automation, schedule, draft, publication, or
notification behavior.

E. PR #101 merged as `f81eb2f81bcf825d5e6201881f8fcb43db09dcf4`:
Editorial Cadence V2 is merged repository policy only, not
runtime/deployed. The deterministic release CLI
(`ops/nullone`, `ops/release-policy.json`, branch
`feature/nullone-release-cli`) is repository engineering only until
separately reviewed and later activated. Current natural Morning/Story
stabilization proof remains untouched.

F. PR #102 merged as `785ad35e7c25306f25a7d42d51ccc7b27458b662`:
deterministic release CLI is on main. V1 scope = production workspace
only (Gateway plugin and agent destinations are externally-controlled
components the tool never deploys). The release CLI remains NOT
ACTIVATED: no deploy-state exists in production yet, no bootstrap or
update was run against production, and current main is NOT deployed.
Current production stabilization proof remains active; issue #37 remains
open. Issue #7 residual governance work
(`docs/contracts/change-control.md`, review receipts, PR/issue
templates) is repository-only.

G. PR #103 merged as `b4ad547fea6ee8f7469cc5fe11b99e52243c68c9`:
auditable change-control evidence is on main; issue #7 is CLOSED.
Reviewer identity convention for future manual receipts: Rauf Alizada
(@a-r3). Issue #37 remains open; production stabilization proof
unchanged. Issue #6 repository hardening only
(`docs/contracts/runtime-permissions.md`, retired Main control-message
protocol, capability matrix, legacy-skill ownership decision), no
production deployment; legacy production automations remain live until a
#37 controlled cutover replaces them.

H. PR #104 merged as `9e3436972f1b209655e91e7b0659b87fbd178fd5`:
runtime instruction authority is on main; issue #6 is CLOSED.
workspace/AGENTS.md changes are merged but NOT deployed. Issue #37
remains open; production stabilization proof unchanged. Issue #8 work
(`docs/operations/runtime-inventory.md`, dependency manifests,
`ops/runtime-inputs.json`, offline render proofs, disposable validator)
is repository-only reproducibility hardening; determinism claimed as
STRUCTURAL_DETERMINISM, never cross-host BIT_IDENTICAL. Reviewer
identity convention remains: Rauf Alizada (@a-r3). Current main is NOT
deployed.

I. PR #105 merged as `90cde7c334af7bc4cb0ad84a2a72828dfadf3ef3`:
runtime reproducibility is on main; issue #8 is CLOSED. Determinism
claimed as STRUCTURAL_DETERMINISM only. Issue #9 recovery decision work
(`docs/adr/ADR-01-recovery-and-artifact-durability.md`,
`docs/operations/recovery-contract.md`, `ops/recovery-policy.json`) is
repository-only. Rauf accepted ordinary recovery targets (RPO=900s,
RTO=14400s) as DESIGN TARGETS only — not deployed/proven recovery
infrastructure; issue #10 will provide empirical restore proof. Issue
#37 remains open; production stabilization state unchanged. Do not claim
recovery guarantees are operational until implemented and proven by the
issue #10 restore drill.

J. PR #106 merged as `1b4d48551457c523d960780a76f84633dc9293ad`:
recovery guarantees are on main; issue #9 is CLOSED with accepted
ordinary DESIGN TARGETS (RPO=900s, RTO=14400s). Issue #10 fixture
restore drill work (`scripts/recovery/restore_drill.py`, sanitized
fixtures, `docs/operations/restore-drill.md`) is repository-only:
publisher-disabled restore proven on fixtures with zero side effects;
private snapshot validation requires separate explicit authorization;
production RPO/RTO remain UNPROVEN. Issue #37 remains open; production
stabilization state unchanged.
