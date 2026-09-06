# NullOne Reliability Proof Verdict — 2026-09

Status: final for issue #3 investigation. Does not itself close #3. Does not itself authorize production deployment.

## Executive verdict

- **Nominal window:** 2026-09-04 03:47:00+04:00 (inclusive) → 2026-09-06 03:47:00+04:00 (inclusive), Asia/Baku. Closed at time of writing (2026-09-06).
- **Final verdict: FAIL**
- **Concise rationale:** The publication-safety invariants that exist to prevent unsafe/duplicate publishing held up under real ambiguity during the window (`PUB-UNKNOWN-001`, `PUB-IDEMP-001`, `PUB-AUTH-001`, `PUB-READBACK-001` all show affirmative in-window evidence with no violation). However, the workflow-reliability side of the proof affirmatively failed: Daily Analytics reported scheduler-level `succeeded` on both of its in-window occurrences (2026-09-05 03:20 and 2026-09-06 03:20) while its business outcome was blocked (no analytics collected) both times, with no recovery before window close, and the automation's own self-generated remediation text on the second occurrence directly contradicts the canonical Zernio integration policy. Morning Editorial lost its entire 2026-09-05 occurrence (4 consecutive scheduler-level attempts, all failed with the same transient-pattern reachability error) with no later in-window occurrence to recover naturally, and there is no configured failure-alert path that would have told an operator either of these things without this manual investigation. The proof window also contained 4 real scheduled executions with scheduler-side run identity but no deployed domain-outcome object for that identity to bind to — an affirmative, not merely untriggered, failure to exercise. `RUN-OUTCOME-001`, `RUN-ARTIFACT-001`, `RUN-REASON-001`, and `RUN-ID-001` are therefore all assessed FAIL at the production level — this is not an evidence gap, it is a repeated, corroborated, still-unresolved defect.
- **Release recommendation:** Do **not** proceed to controlled production activation (#37) yet. This proof identifies a specific **proof-derived blocking operational bundle** — #27 (domain outcomes/health), #28 (Morning Editorial runtime), #29 (Daily Analytics runtime/adapter), and #30 (failure alerts) — as the concrete defects this verdict evidenced; #27/#28/#29 are merged in Git but not deployed, and #30 remains the next main engineering implementation item. This bundle is not necessarily the complete set of everything #37 requires — remaining required M0 engineering continues per the existing roadmap, including applicable #31–#36 Story/breaking work in parallel. #37 remains the final, explicit controlled production deployment/preflight/live-validation boundary after the required reviewed M0 changes (including this bundle) are ready; it deploys the exact reviewed SHAs/components and validates genuine scheduled/eligible behavior, not as a separate pre-#37 stage. See "Release decision" below for exact sequencing.

## Audit method and safety boundary

This investigation was conducted strictly read-only against:
- production workspace `/home/oem/.openclaw/workspace` (files, manifests, ledgers, `/tmp/openclaw/*.log`);
- `openclaw automations list|get|runs` and `openclaw audit` (read-only CLI queries against the local Gateway);
- `openclaw config get cron` (read-only);
- four read-only Zernio MCP calls: `accounts_get`, `posts_get` (×2, one ambiguous post + one known-published post used as a control), `posts_list_failed`. No `posts_create`, `posts_publish_now`, `posts_retry`, `posts_edit_post`, `posts_delete`, `posts_unpublish_post`, or any other mutating tool was called at any point.
- this repository's `NULLONE_PROJECT_CONTEXT.md`, `docs/contracts/acceptance.md`, `tests/fixtures/acceptance_contracts.json`, and GitHub issue #3 (read-only `gh issue view`).

No production file was created, edited, moved, or deleted. No workflow/job was rerun, retried, or manually triggered. No publish/draft/schedule/approval/revise/reject mutation was issued. No OpenClaw config, agent, job, schedule, or model was changed. No service was restarted. This document and the corresponding `NULLONE_PROJECT_CONTEXT.md` update are the only repository writes made for this task, both on `feature/n3-reliability-proof-verdict`.

## Evidence source inventory

| Source | Type | Coverage | Notes |
|---|---|---|---|
| `social/ops/reliability-proof/2026-09-04-0347/*` | baseline snapshot | 2026-09-04 03:47–03:48 | Captured at window open: Telegram bot healthy/polling, Zernio server `ok`/no issues, automation list, script/manifest SHA256s |
| `openclaw automations runs --id <job>` | durable scheduler receipts | full window for both jobs | Authoritative occurrence-level record: `runAtIso`, `status`, `completionStatus`, `error`, `errorReason`, model summary text |
| `openclaw automations get/list` | current job state | queried 2026-09-06 (post-window) | `lastFailureNotificationDeliveryStatus`, `delivery.mode`, current `status` |
| `openclaw audit` | fine-grained tool/agent-run events | 2026-09-04 ~16:30 UTC → 2026-09-05 23:21 UTC (500-event window; older entries have rolled off) | `redaction: metadata_only` — no message bodies; `kind: message` returns 0 events across all retention |
| `/tmp/openclaw/openclaw-2026-09-05.log`, `openclaw-2026-09-06.log` | raw journal | Sep5 log starts 2026-09-05T14:39:50Z (18:39:50 Baku) onward only | Earlier Sep4/early-Sep5 journal is not retained; gap is a genuine evidence gap, not treated as absence-of-event |
| `social/ops/manifests/*.json` | publication manifests | 3 manifests; 1 pre-window (published 2026-09-04 02:47 Baku, before 03:47 start), 2 in-window | Source of truth for approval/publication state machine |
| `social/state/publish-ledger.jsonl`, `analytics/{raw,reports}/*.md` | ledger + analytics artifacts | full window | Confirms which manifests reached the ledger; confirms 2026-09-04 and 2026-09-05 analytics artifacts are absent |
| `workspace/social/ZERNIO.md` (production copy) | canonical policy doc | current | Used to check the Daily Analytics self-generated remediation text against actual policy |
| Zernio MCP read-only reconciliation (`posts_get`, `posts_list_failed`, `accounts_get`) | live provider state | queried 2026-09-06 (post-window) | Used only for the ambiguous-publication section; not a retry |
| `NULLONE_PROJECT_CONTEXT.md` (repo) | prior canonical context | as of 2026-09-06 | Cross-checked, not blindly trusted, against the above |
| GitHub issue #3 | investigation contract | read-only `gh issue view 3` | Confirms scope/acceptance criteria used below |

## Nominal interval vs actual evidence coverage

| Workflow | Expected in-window occurrences | Observed | Missing / gap | Sufficiently exercised for a verdict? |
|---|---|---|---|---|
| Morning Editorial (daily 08:30 Baku) | 2026-09-04 08:30, 2026-09-05 08:30 | Both occurred. 09-04 succeeded; 09-05 failed after 4 scheduler-level attempts (08:30, 08:40, 08:52, 09:07), terminal at 09:17:26 | Next occurrence (2026-09-06 08:30) falls **after** window close — no in-window recovery opportunity existed | Yes — 2 of 2 expected occurrences observed |
| Daily Analytics (daily 03:20 Baku) | 2026-09-05 03:20, 2026-09-06 03:20 | Both occurred; both scheduler-`succeeded`/domain-blocked (no analytics artifacts produced) | 2026-09-04 03:20 occurrence exists but falls **before** window open (03:47) — informative context only, not in-scope evidence | Yes — 2 of 2 expected occurrences observed, both negative |
| Publication pipeline | Opportunistic (approval-gated, not scheduled) | 2 in-window publication attempts: chatgpt-ads-1b (2026-09-05, PUBLISHED, readback-inconclusive) and openai-astra-launch (2026-09-05, UNKNOWN) | No case arose in-window exercising: tampered-manifest rejection, replayed/wrong-stage callback, forged-metadata rejection, second-publish-in-session rejection, ambiguous-draft-create (`REVIEW_UNKNOWN`) | Partially — core happy-path and one ambiguous-outcome case are exercised; several negative/adversarial contract branches were never exercised because no such event occurred |
| Failure notification | N/A (config state) | Config and delivery-status queried directly (not scheduled) | None — this is a config-state check, not an occurrence count | Yes |
| `RUN-OUTCOME` surface (#27) | N/A (repo-level capability) | Not present in production at all | Production has zero `nullone_run_outcome`/run-ID persistence; confirms #27/#28/#29 code is merged in Git but not deployed | Yes — absence itself is the finding |

## Timeline

All times Asia/Baku unless marked UTC. Sanitized: account/post/draft IDs are Zernio-internal object IDs (not customer PII); no tokens, owner IDs, or signed URLs included.

- **2026-09-04 03:47** — Proof window opens. Baseline snapshot recorded: Telegram bot polling/healthy, Zernio MCP server `ok`, automation list captured, script/manifest SHA256 baseline recorded.
- **2026-09-04 08:30–08:33** — Morning Editorial scheduled run: `status=ok`, `completionStatus=succeeded`. Editorial board written with 5 scored candidates, including "OpenAI Astra official launch."
- **2026-09-04 (pre-window, for context only) 02:47/02:58** — `claude-background-computer-use` manifest reaches `publication.state=PUBLISHED` — this event occurred before 03:47 and is background context, not in-window evidence.
- **2026-09-05 03:20–03:21** — Daily Analytics scheduled run #1 in-window: `status=ok`, `completionStatus=succeeded` (scheduler level). Domain outcome: BLOCKED/no-data. Self-reported narrative: "Zernio MCP tools are not available in this execution environment... Gateway-host (node) API access requires a paired node, which is unavailable... Legacy ZERNIO_API_KEY / authenticated REST integration is explicitly deprecated per `social/ZERNIO.md`." No analytics artifact for 2026-09-04 or 2026-09-05 exists in `analytics/raw|reports/`.
- **2026-09-05 08:30:00 → 09:17:26** — Morning Editorial scheduled occurrence: 4 consecutive scheduler-level attempts (08:30:00, 08:40:39, 08:52:01, 09:07:13), each terminating in `status=error`, `completionStatus=failed`, `error="API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"`, `errorReason=timeout`. No further attempt occurs before window close.
- **2026-09-05 09:47:11 (candidate created 05:47:11 UTC)** — Astra manifest created; Zernio draft created 09:48:32 (`zernio_draft_id` recorded).
- **2026-09-05 15:17:54** — Astra manifest: two-stage approval recorded (`first_stage=true`, `final_publish=true`, `human_confirmation=two_step`, operator=Rauf) for the exact approved manifest version.
- **2026-09-05 15:19:23** — Astra manifest: `publication.attempts=1`, `publication.state=UNKNOWN`, `error="Publish result was not unambiguously accepted"`. No telegram-notification fields present in the manifest. No second attempt is ever recorded.
- **2026-09-05 15:46:53** — chatgpt-ads-1b manifest created.
- **2026-09-05 15:49:54** — chatgpt-ads-1b: two-stage approval recorded.
- **2026-09-05 15:52:09** — chatgpt-ads-1b: `publication.attempts=1`, `publication.state=PUBLISHED`, `live_zernio_post_id` recorded; `platform_post_id`/`permalink` = "Not available" (readback-inconclusive, known Zernio API response gap). No telegram-notification fields present in the manifest (in contrast to the pre-window 2026-09-04 manifest, which does have `telegram_notification_state=SENT`).
- **2026-09-05 19:20, 20:30, 21:20, 23:20, 23:30 and 2026-09-06 01:20, 03:20:01, 03:21:25** — Recurring `bundle-mcp` subsystem WARN: `failed to start server "zernio" (https://mcp.zernio.com/mcp): Streamable HTTP error: Error POSTing to endpoint: [redacted response body]`. Recurs at roughly hourly intervals across the evening/night and is not exclusively tied to the 03:20 Daily Analytics cron tick, indicating a broader MCP session/bootstrap availability problem rather than an event scoped to one job invocation.
- **2026-09-06 03:20:00 → 03:21:23** — Daily Analytics scheduled run #2 in-window: `status=ok`, `completionStatus=succeeded` (scheduler level). Domain outcome: BLOCKED/no-data again. Self-reported narrative changes to: "ZERNIO_API_KEY not available in credential store... Job will resume once ZERNIO_API_KEY is configured in the secret store." This directly contradicts `social/ZERNIO.md`, which states the legacy `ZERNIO_API_KEY` path "is DEPRECATED and must not be used" and that an MCP auth failure should "STOP and report BLOCKED" — not wait for a deprecated credential.
- **2026-09-06 03:47** — Proof window closes. Morning Editorial automation status remains `error (4x)`, last run ~2026-09-05 09:07–09:17, no further run before close. Daily Analytics automation status shows `ok` (scheduler-level) with the domain defect unresolved.
- **2026-09-06 (post-window, investigation-time only)** — Read-only Zernio reconciliation performed for this report: `posts_get` on the Astra draft object returns `Status: draft` (a control check on the known-published chatgpt-ads control object correctly returns `Status: published`, validating the tool's semantics); `posts_list_failed` returns no failed posts. This is affirmative post-window evidence that the provider object is currently `draft`, not `published`; it strengthens the non-publication interpretation but does not resolve the durable `UNKNOWN` state. It is documented here, not acted on, and the manifest's durable `UNKNOWN` record from window-time is left unmodified.
- **2026-09-06 (post-window, config query)** — `openclaw cron get` for both Morning Editorial and Daily Analytics jobs shows no `failureAlert` field at all, `delivery.mode=none`, `lastFailureNotificationDeliveryStatus=not-requested` for both. `openclaw config get cron` shows no authored global `cron` config (runtime default applies).

## Criterion matrix

IDs are the canonical IDs from `docs/contracts/acceptance.md` / `tests/fixtures/acceptance_contracts.json`. "Result" below is the **production, in-window** assessment — distinct from that document's own offline/repo-level `Enforcement`/`Proof` columns, which describe the unwired repo code, not production.

| Criterion | Expected guarantee | In-window production evidence | Result | Notes / limitation |
|---|---|---|---|---|
| `PUB-VERIFY-001` | Publish requires `verification==PASS` | All 3 manifests (1 pre-window, 2 in-window) show `verification: PASS` before any review/publish step | PASS | No negative case (non-PASS manifest attempting publish) occurred in-window; positive path only |
| `PUB-BIND-001` | Caption/media hash- and dimension-bound to approval | Hashes recorded and stable in all in-window manifests | NOT_EXERCISED | No tampering/replay attempt occurred in-window to exercise the negative case |
| `PUB-AUTH-001` | First-stage + final-publish flags with authorization metadata required | Both in-window manifests show `first_stage`, `final_publish`, `human_confirmation=two_step`, `operator` populated before publish | PASS | Field/value presence only; see `PUB-AUTH-PROVENANCE-001` for what this does not prove |
| `PUB-AUTH-PROVENANCE-001` | Authorization metadata must be cryptographically/callback-derived, not wrapper-asserted | No change from repo-level `MISSING` — production has no separate mechanism observed | NOT_EXERCISED | Not exercised or falsified in-window; genuinely unresolved gap per `docs/contracts/acceptance.md` |
| `PUB-CALLBACK-001` | Stale/replayed/wrong-stage callbacks rejected deterministically | No such callback occurred in-window | NOT_EXERCISED | No adversarial case arose |
| `PUB-IDEMP-001` | At most one durable local publish attempt per authorization | All 3 manifests show `attempts=1` exactly; ledger shows no duplicate publish entries for either in-window manifest | PASS | Directly exercised twice in-window with no violation |
| `PUB-MODEL-REPEAT-001` | Second `posts_publish_now` call in one session rejected | No evidence either way; this remains a prompt-text-only guard per the acceptance doc | NOT_EXERCISED | Cannot be confirmed or falsified from available audit granularity (`metadata_only` redaction) |
| `PUB-UNKNOWN-001` | Ambiguous outcome → `UNKNOWN`, never auto-retried | Astra manifest: `attempts=1`, `state=UNKNOWN` at 15:19:23 on 2026-09-05; still `UNKNOWN`/`attempts=1` as of this report; post-window read-only reconciliation shows the corresponding provider object is currently `draft`, not `published` — this strengthens the non-publication interpretation without resolving the durable `UNKNOWN` state | PASS | The strongest, most directly-stressed safety result in this proof — real ambiguity occurred and the no-retry invariant held for over 24h to window close and beyond. This PASS rests on "ambiguous result → `UNKNOWN` + no auto-retry" being upheld, not on proving provider-side historical non-publication |
| `PUB-READBACK-001` | Accepted publish + inconclusive readback ≠ second attempt | chatgpt-ads-1b: `state=PUBLISHED`, readback (`platform_post_id`/`permalink`) inconclusive ("Not available"); no second attempt recorded | PASS | Directly exercised in-window |
| `PUB-NOTIFY-001` | Notification is a separate side effect; its failure can't trigger publish retry | Neither in-window manifest has `telegram_notification_*` fields at all (unlike the pre-window manifest, which does); `audit --kind message` returns 0 events across all retention | NOT_EXERCISED | Cannot confirm the notifier ran, succeeded, or failed in-window — this is an evidence gap, not a confirmed pass or fail. No retry was observed either way, so the specific "notify failure ≠ retry" branch was not shown to have been exercised |
| `PUB-DRAFT-001` | Draft-only transport; ambiguous create → `REVIEW_UNKNOWN`, no blind second create | Both in-window manifests show `review.state=DRAFT_CREATED`, `create_attempts=1` — no ambiguous-create case arose | NOT_EXERCISED | Positive path only; the specific ambiguous-create branch never occurred |
| `RUN-OUTCOME-001` | Scheduler success must be distinct from domain/business success | Daily Analytics: `completionStatus=succeeded` reported on both 2026-09-05 and 2026-09-06 occurrences while the domain outcome was BLOCKED (no analytics collected) both times; no structured domain-outcome surface exists in production to make this distinction | **FAIL** | Confirmed twice, not once; production has no deployed fix (#27/#29 are merged in Git but not deployed) |
| `RUN-ARTIFACT-001` | Domain success requires required artifacts or an explicit valid no-op | Required Daily Analytics artifacts for 2026-09-04 and 2026-09-05 are absent; the 2026-09-06 in-window run also produced no analytics artifact (only a "no data" markdown note, which is informal, not a contract-validated `NO_DATA` outcome) | **FAIL** | Same underlying defect as `RUN-OUTCOME-001` |
| `RUN-REASON-001` | Every non-success outcome has a stable, correct, machine-readable reason | Reason text exists but is free-form and self-contradictory between the two in-window Daily Analytics occurrences (2026-09-05: "legacy key path is deprecated, correctly stops"; 2026-09-06: "will resume once legacy ZERNIO_API_KEY is configured" — contradicts `social/ZERNIO.md`) | **FAIL** | Not just "missing structure" — the free-form reason given to the operator on the second occurrence is actively incorrect guidance |
| `RUN-ID-001` | Stable run ID binds scheduler receipt, artifacts, and domain outcome | Scheduler-side `runId`/`sessionId` exist per occurrence (confirmed via `openclaw automations runs` and `openclaw audit`) for real in-window scheduled executions (Morning Editorial ×2, Daily Analytics ×2); there is no domain-outcome object in production for that ID to bind to | **FAIL** | The proof window contained real scheduled executions, so this requirement was exercised, not merely untriggered: the scheduler-receipt half of the binding exists, but the required end-to-end binding (scheduler receipt + artifacts + domain result under one run identity) is affirmatively absent in production for every one of those executions |

**Criterion counts:** PASS: 4 · FAIL: 4 · NOT_EXERCISED: 7 (of 15 total canonical criteria)

## Findings register

**F1 — Daily Analytics: scheduler-success/domain-blocked mismatch, recurring**
- Timestamp: 2026-09-05 03:20–03:21 and 2026-09-06 03:20–03:21 Asia/Baku
- Component: Daily Analytics automation (`8e94064c-…`)
- Source: `openclaw automations runs --id 8e94064c-…` (sanitized run summaries), `social/analytics/{raw,reports}/2026-09-06.md`, absence of `analytics/{raw,reports}/2026-09-0{4,5}.md`
- Observed fact: scheduler `status=ok`/`completionStatus=succeeded` on both in-window occurrences while the domain result was "no data collected" both times; no analytics artifacts produced for either 2026-09-04 or 2026-09-05.
- Classification: DEFECT
- Confidence: CONFIRMED
- Root cause: confirmed direction — Zernio MCP tool/session bootstrap unavailability in the scheduled-session execution environment (corroborated by recurring `bundle-mcp failed to start server "zernio"` journal WARNs). Not confirmed as a generic Zernio-service outage (baseline probe and current `accounts_get`/`posts_get` calls in this same investigation succeeded against the real Zernio API).
- Affected criteria: `RUN-OUTCOME-001` (FAIL), `RUN-ARTIFACT-001` (FAIL)
- Owner: engineering (#29 production deployment owner)
- Next action: ensure #29's `nullone_zernio_analytics_adapter.py` path is included in the #37 controlled-deployment bundle and validated with a real scheduled run as part of that gate before claiming this resolved
- Release restriction: must be included in and validated during #37; controlled production activation should not be considered complete while Daily Analytics can silently self-report `succeeded` on a blocked cycle

**F2 — Daily Analytics: self-generated remediation text contradicts canonical policy**
- Timestamp: 2026-09-06 03:20–03:21 Asia/Baku
- Component: Daily Analytics automation, run-summary text persisted into `social/analytics/reports/2026-09-06.md`
- Source: `openclaw automations runs --id 8e94064c-…` (2026-09-06 entry); `workspace/social/ZERNIO.md` (production copy, "Legacy ZERNIO_API_KEY ... is DEPRECATED and must not be used ... If MCP authentication fails: STOP and report BLOCKED")
- Observed fact: the 2026-09-06 run tells the operator the job "will resume once ZERNIO_API_KEY is configured in the secret store" — the opposite of documented policy. The immediately preceding 2026-09-05 run correctly cited the deprecation and stopped/reported BLOCKED without suggesting the legacy key.
- Classification: DEFECT
- Confidence: CONFIRMED (the contradiction is a direct text comparison, not inference)
- Root cause: UNCONFIRMED — unclear whether this is prompt drift, a model-generated guess presented as fact, or stale cached reasoning from an earlier session; the underlying MCP-unavailability trigger is the same confirmed direction as F1.
- Affected criteria: `RUN-REASON-001` (FAIL)
- Owner: engineering (prompt/runbook owner for `workspace/social/ops/prompts/daily-analytics.md`)
- Next action: a human should not act on the "configure ZERNIO_API_KEY" text; if this recurs after #29 is deployed, treat it as a prompt-hygiene defect requiring a separate fix issue
- Release restriction: do not provision or resurrect `ZERNIO_API_KEY` production credentials based on this automation's own text without independent verification against `ZERNIO.md`

**F3 — Morning Editorial: full 2026-09-05 occurrence lost, no in-window recovery**
- Timestamp: 2026-09-05 08:30:00 → 09:17:26 Asia/Baku (4 attempts: 08:30:00, 08:40:39, 08:52:01, 09:07:13)
- Component: Morning Editorial automation (`0666d47b-…`)
- Source: `openclaw automations runs --id 0666d47b-…`
- Observed fact: 4 consecutive scheduler-level attempts, each `status=error`/`completionStatus=failed`, identical `error="API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"`, `errorReason=timeout`. `openclaw automations list` (queried 2026-09-06, post-window) still shows this job's status as `error (4x)` with no run since. Next scheduled occurrence (2026-09-06 08:30) falls after window close.
- Classification: DEFECT, with RECOVERY_EVIDENCE noted separately (the *preceding* 2026-09-04 08:30 occurrence succeeded cleanly)
- Confidence: CONFIRMED for the failure pattern; STRONG (not CONFIRMED) for "transient" characterization — `NULLONE_PROJECT_CONTEXT.md` records a later read-only DNS/HTTPS probe that succeeded, which is consistent with transient reachability, but no probe was run *during* the exact 08:30–09:17 failure window itself, so a permanent configuration fault is not proven absent, only not currently evidenced.
- Root cause: transient provider/runtime reachability pattern (STRONG); permanent DNS/config fault: UNCONFIRMED (not ruled in or out by in-window evidence alone)
- Affected criteria: not a canonical `PUB-`/`RUN-` ID by itself, but is the concrete evidence underlying why `RUN-REASON-001`/`RUN-OUTCOME-001` matter operationally
- Owner: engineering (#28 production deployment owner)
- Next action: ensure #28's bounded-retry/reachability-classification runtime is included in the #37 controlled-deployment bundle and validated with a real scheduled run as part of that gate; until deployed, this exact failure mode can recur with the same all-or-nothing (4 scheduler retries, then silence until next day) behavior
- Release restriction: must be included in and validated during #37; controlled production activation should not be considered complete while a single-day content-pipeline outage can produce no operator-visible signal beyond `automations list`

**F4 — No failure-alert path exists for either automation**
- Timestamp: queried 2026-09-06 (post-window; reflects state unchanged since before window open per baseline)
- Component: OpenClaw scheduler config, both automations
- Source: `openclaw cron get <id>` (no `failureAlert` field present in either job's schema at all; `delivery.mode=none`; `lastFailureNotificationDeliveryStatus=not-requested`); `openclaw config get cron` (no authored global config)
- Observed fact: neither the Morning Editorial nor the Daily Analytics job has any failure-alert delivery configured; this matches the baseline captured 2026-09-04 03:47 (`Delivery: not requested (not requested)` for every job in `baseline-automations.txt`), so the gap existed for the entire window.
- Classification: EVIDENCE_GAP for detection purposes / DEFECT for the reliability contract (M0's own stated goal is "production operations are healthy and timely" with operator visibility)
- Confidence: CONFIRMED
- Root cause: confirmed — feature not yet implemented/configured (tracked as #30, still open)
- Affected criteria: supports the FAIL determination on `RUN-OUTCOME-001`/`RUN-REASON-001` by removing any compensating detection control
- Owner: engineering (#30 owner)
- Next action: complete #30 (concise Telegram failure alerts consuming truthful domain health) as the next main engineering implementation item; its production deployment occurs within the #37 controlled-deployment gate alongside #27/#28/#29
- Release restriction: must be included in and validated during #37; controlled production activation should not be considered complete until at least execution-failure alerting exists; per repo direction, business-`BLOCKED`-while-scheduler-`ok` alerting is a further, larger requirement layered on top of #27's (undeployed) domain-outcome surface

**F5 — Ambiguous publication (Astra): safety behavior held; provider-side reconciliation available and performed**
- Timestamp: manifest `state=UNKNOWN` set 2026-09-05 15:19:23 Asia/Baku; read-only reconciliation performed 2026-09-06 (post-window)
- Component: Publish Bridge / `2026-09-05-openai-astra-launch-2026-09-05.json`
- Source: manifest file; Zernio MCP `posts_get` (read-only, this investigation) on the Astra draft object (`[ASTRA_DRAFT_ID_REDACTED]`) → `Status: draft`; control check on the known-published control object (`[PUBLISHED_CONTROL_ID_REDACTED]`) → `Status: published` (confirms the tool distinguishes the two states correctly); `posts_list_failed` → no failed posts
- Observed fact: `attempts=1`, `state=UNKNOWN` has not changed since 2026-09-05; as of the post-window reconciliation query, the Zernio-side object is in `draft` status, not `published`, and does not appear in the failed-posts list.
- Classification: EXPECTED_SAFE_BEHAVIOR (the no-retry invariant held) with a documented reconciliation note
- Confidence: CONFIRMED for "no retry occurred" and "local record is unchanged"; STRONG (not CONFIRMED) for the non-publication interpretation — `draft` status is strong affirmative evidence but does not resolve the durable `UNKNOWN` state or prove with historical certainty that publication never occurred at any point, since Zernio's internal state-transition semantics for a failed/ambiguous publish attempt are not documented in the materials available to this investigation; this reconciliation was performed post-window, not at window-close time
- Root cause: N/A — this is a safety-invariant confirmation, not a defect
- Affected criteria: `PUB-UNKNOWN-001` (PASS)
- Owner: operator (Rauf) — a fresh, deliberate publish/discard decision for this specific piece of content is a business decision, not an automatic action, and is out of scope for this proof
- Next action: none required by this proof; if the operator wants to publish this content, that is a new, explicit, single decision — not a retry of the ambiguous attempt
- Release restriction: none from this finding specifically

**F6 — No recorded Telegram publish-result notification for either in-window manifest**
- Timestamp: 2026-09-05 15:19–15:52 Asia/Baku (both in-window manifests)
- Component: Publish Notifier / manifest schema
- Source: both in-window manifest JSON files (no `telegram_notification_*` keys present at all, vs. the pre-window 2026-09-04 manifest which has `telegram_notification_attempts/state/last_attempt_at/notified_at`); `openclaw audit --kind message` (0 events across all retention, so audit cannot independently confirm or deny delivery)
- Observed fact: the field set that would show the notifier ran is simply absent from both in-window manifests.
- Classification: EVIDENCE_GAP
- Confidence: LIMITED — could indicate the notifier was not invoked, was invoked but failed to write back, or reflects a schema/version difference between the pipeline run that produced the pre-window manifest and the ones that produced the in-window manifests; cannot be distinguished from available read-only evidence.
- Root cause: UNCONFIRMED
- Affected criteria: `PUB-NOTIFY-001` (NOT_EXERCISED, not PASS, because the criterion cannot be confirmed as having run at all)
- Owner: engineering (Publish Notifier owner)
- Next action: a future, separate investigation/issue should determine why `telegram_notification_*` fields stopped appearing between the pre-window and in-window manifests
- Release restriction: none directly, but this compounds F4 — if neither execution-failure alerts nor publish-result notifications are confirmed reliable, the operator's only reliability signal is manual inspection (as performed in this proof)

**F7 — Production has no deployed run-outcome/domain-health surface at all**
- Timestamp: observed throughout window (structural, not a point-in-time event)
- Component: production `social/ops/` tree
- Source: `find /home/oem/.openclaw/workspace/social/ops -iname "*run-outcome*"` (no results); absence of `nullone_run_outcome.py`/`nullone_zernio_analytics_adapter.py`/`nullone_editorial_runtime.py` equivalents in the production tree; production Morning Editorial/Daily Analytics run summaries are free-form agent text, not structured outcome objects
- Observed fact: issues #27, #28, #29 are merged in Git (`NULLONE_PROJECT_CONTEXT.md` records this) but none of that code is deployed to the actual scheduled production jobs exercised in this window.
- Classification: EVIDENCE_GAP (structural) for F1/F3's causal explanation; DEFECT for `RUN-ID-001` specifically, since the proof window's 4 real scheduled executions affirmatively exercised the binding requirement and found it absent, not merely untriggered
- Confidence: CONFIRMED
- Root cause: confirmed — deployment has not been performed (this is expected and documented; per instructions, GitHub merge ≠ production deployment)
- Affected criteria: `RUN-OUTCOME-001` (FAIL), `RUN-ARTIFACT-001` (FAIL), `RUN-REASON-001` (FAIL), `RUN-ID-001` (FAIL) — all four affirmatively failed, not merely unexercised
- Owner: engineering / release owner
- Next action: ensure #27/#28/#29 (and #30 once complete) are included in the #37 controlled-deployment gate, then re-validate with real scheduled runs as part of that gate before claiming the underlying defects fixed
- Release restriction: #37 must deploy and validate #27/#28/#29 plus #30 within its own scope before controlled production activation is considered complete

**F8 — Journal coverage gap for 2026-09-04 03:47 → 2026-09-05 14:39**
- Timestamp: the gap itself spans 2026-09-04 03:47–2026-09-05 18:39 Asia/Baku
- Component: `/tmp/openclaw/*.log`
- Source: earliest line in `openclaw-2026-09-05.log` is `2026-09-05T14:39:50.187Z` (18:39:50 Baku); no earlier log file exists
- Observed fact: raw journal-level detail (e.g., independent corroboration of the exact Morning Editorial 08:30–09:07 failure sequence beyond the durable `automations runs` receipt) is unavailable for this span.
- Classification: EVIDENCE_GAP
- Confidence: CONFIRMED (the gap itself is directly observed)
- Root cause: confirmed as log rotation/retention (ephemeral `/tmp` storage), not a data-loss incident specific to this proof
- Affected criteria: none directly (the durable `openclaw automations runs` store independently covers the same occurrences and was the primary source used above), but reduces the number of independent corroborating sources for F3
- Owner: N/A (operational log retention is a platform characteristic, not a NullOne defect)
- Next action: none required; noted for completeness per the instruction to distinguish defects from evidence gaps
- Release restriction: none

## Ambiguous publication / UNKNOWN reconciliation

Manifest: `2026-09-05-openai-astra-launch-2026-09-05.json` (production path: `social/ops/manifests/`).

- Prior state (confirmed from manifest, unchanged since 2026-09-05 15:19:23 Baku): `publication.state=UNKNOWN`, `publication.attempts=1`, `error="Publish result was not unambiguously accepted"`.
- No retry was performed by this investigation or observed to have been performed by production.
- A read-only provider reconciliation query **was** available and was performed as part of this investigation (Zernio MCP `posts_get`, `posts_list_failed`, `accounts_get` — all non-mutating): the Zernio-side object for this content (`[ASTRA_DRAFT_ID_REDACTED]`) currently reports `Status: draft`. It does not appear in the failed-posts list. A control query against the known-published `chatgpt-ads-1b` control object correctly returned `Status: published`, confirming the tool distinguishes these states.
- Interpretation: post-window reconciliation shows the provider object is currently `draft`, not `published`; this strengthens the non-publication interpretation but does not resolve the durable `UNKNOWN` state. This is not proof with absolute historical certainty that the content never went live at any point, because Zernio's internal state-transition semantics for a failed/ambiguous publish attempt (e.g., whether a failed publish reverts to `draft` or moves to a distinct failed state) are not documented in the materials available to this investigation.
- Per the governing rule for this investigation: **the local manifest's `UNKNOWN` state is left unmodified.** This report documents the reconciliation finding; it does not convert `UNKNOWN` to `FAILED` or `PUBLISHED` in production, and no further action (retry, discard, or fresh publish) was taken.
- Any decision to publish, retry, or discard this specific content is a distinct, deliberate operator decision outside the scope of this proof and was not made or implied here.

## Defects vs evidence gaps

**Confirmed defects (production evidence, not merely absence of evidence):**
1. F1 — Daily Analytics scheduler-success/domain-blocked mismatch, recurring twice in-window.
2. F2 — Daily Analytics self-generated remediation text contradicts canonical `ZERNIO.md` policy.
3. F3 — Morning Editorial full-occurrence loss on 2026-09-05 with no in-window recovery opportunity.
4. F4 — No failure-alert delivery path configured for either automation, for the entire window.
5. F7 / `RUN-ID-001` — the proof window contained 4 real scheduled executions (Morning Editorial ×2, Daily Analytics ×2) with scheduler-side run/session identity, and for every one of them the required end-to-end binding of that identity to a domain-outcome object was affirmatively absent, not merely untriggered.

**Evidence gaps (missing/limited proof, not a confirmed failure):**
1. F6 — Cannot confirm whether the Publish Notifier ran for either in-window publication.
2. F8 — Journal coverage gap for roughly the first 39 hours of the window.
3. `PUB-AUTH-PROVENANCE-001`, `PUB-CALLBACK-001`, `PUB-MODEL-REPEAT-001`, `PUB-DRAFT-001`'s ambiguous-create branch, `PUB-BIND-001`'s negative branch — none of these had a triggering event occur in-window, so none could be exercised one way or the other.

**Structural (both a documented gap and the reason the defects above surfaced this way):**
- F7 — no run-outcome/domain-health persistence exists in production; #27/#28/#29 are merged but not deployed.

## Root-cause confidence

- **Morning Editorial (2026-09-05 failures):** transient provider/runtime reachability pattern — **STRONG**, not CONFIRMED. Supporting: identical `ENOTFOUND`/timeout error across all 4 attempts (consistent with an outage rather than a permanent misconfiguration, which would more likely fail differently, e.g. auth error); a later read-only DNS/HTTPS probe (per `NULLONE_PROJECT_CONTEXT.md`) succeeded. Limiting: no probe was run during the exact failure window itself in this investigation's available evidence. A permanent DNS/configuration fault is **UNCONFIRMED**, not ruled out.
- **Daily Analytics (both in-window occurrences):** scheduled-session Zernio MCP bootstrap/runtime-availability failure — **STRONG**, supported by the recurring `bundle-mcp failed to start server "zernio"` journal WARNs spanning the same period and beyond. This is explicitly **not** confirmed as a generic Zernio service outage: this same investigation's own read-only Zernio MCP calls (`accounts_get`, `posts_get` ×2, `posts_list_failed`), made today via the identical MCP endpoint, succeeded without issue, and the window-open baseline (`baseline-zernio.json`) also showed the server `ok`. The defect is scoped to the scheduled-session execution environment, not the Zernio service itself.
- **Daily Analytics self-contradictory remediation text (F2):** root cause of *why* the text is wrong is **UNCONFIRMED** (prompt drift vs. model inference presented as fact vs. stale context) — only the fact of the contradiction is confirmed.
- **Ambiguous publication (Astra):** the reason the publish result was ambiguous (timeout vs. malformed response vs. something else) is **UNCONFIRMED** — the manifest records only `"Publish result was not unambiguously accepted"`, and no additional detail was available in this investigation's read-only sources.

## Unresolved risks

| Risk | Owner | Next action | Release restriction |
|---|---|---|---|
| Daily Analytics can silently report scheduler-success while collecting no data (F1) | Engineering (#29) | Include #29's adapter in the #37 controlled-deployment bundle; validate with one real authorized scheduled run as part of that gate | Must be included in and validated during #37 |
| Daily Analytics may hand the operator incorrect remediation advice (provision a deprecated credential) (F2) | Engineering (prompt/runbook owner) | Do not act on the automation's own "configure ZERNIO_API_KEY" text; investigate prompt/session-state hygiene separately | Advisory — do not provision `ZERNIO_API_KEY` based on this text |
| Morning Editorial can lose a full day's content-pipeline occurrence with no operator-visible signal beyond manual `automations list` inspection (F3) | Engineering (#28) | Include #28's bounded-retry/classification runtime in the #37 controlled-deployment bundle; validate with a real scheduled run as part of that gate | Must be included in and validated during #37 |
| No failure-alert delivery path exists for either automation (F4) | Engineering (#30, open) | Complete #30 as the next main engineering item; deployment occurs within #37 | Must be included in and validated during #37 |
| Cannot confirm Publish Notifier ran for either in-window publication (F6) | Engineering (Publish Notifier owner) | Separate investigation into missing `telegram_notification_*` fields | Advisory — do not assume operators were notified of the 2026-09-05 publications without independent confirmation |
| Astra content (`2026-09-05-openai-astra-launch-2026-09-05.json`) sits in `UNKNOWN`/Zernio `draft` indefinitely | Operator (Rauf) | Make an explicit, deliberate publish-or-discard decision for this exact content; this is not a retry | None (this is a content decision, not a release blocker) |
| No production run-outcome/domain-health surface exists at all (F7) | Engineering / release owner | Include #27/#28/#29 (and #30 once complete) in the #37 controlled-deployment gate; re-validate as part of that gate before treating the associated defects as fixed | Must be included in and validated during #37 |

## Release decision

- **NullOne should NOT proceed to controlled production deployment (#37) at this time.**
- **#27** (domain outcomes/health), **#28** (Morning Editorial runtime), **#29** (Daily Analytics runtime/adapter), and **#30** (failure alerts) are the **proof-derived blocking operational bundle** — the concrete, evidenced defects this reliability verdict identified. #27/#28/#29 are merged in Git but not deployed; #30 remains the next main engineering implementation item. This bundle is not asserted to be the complete set of everything #37 requires — it is what this specific proof window's evidence blocks.
- Correct sequence from here:
  1. this issue #3 verdict is human-reviewed and recorded;
  2. #30 is the next main engineering implementation item;
  3. remaining required M0 engineering continues according to the existing roadmap — #31/#34 decision work may continue in parallel, followed by relevant Story/breaking-behavior implementation applicable across #31–#36;
  4. issue #37 remains the final, explicit controlled production deployment/preflight/live-validation boundary once the required reviewed M0 changes (including the #27/#28/#29/#30 bundle) are ready — it deploys the exact reviewed SHAs/components required and validates genuine scheduled/eligible behavior before controlled production activation is considered complete.
- No separate, uncontrolled production deployment stage should occur before #37, except genuine emergency recovery of a concrete production failure under existing hotfix policy. No synthetic production cycles.
- `#31`–`#36` (Story/cadence/breaking-behavior work) are not blocked by this verdict and may continue in parallel per existing planning.
- This verdict does **not** require redoing the 48h proof from scratch; #37's own scheduled-run observation after deployment is the appropriate follow-up validation for #27/#28/#29/#30, not a repeat of this investigation.
- Publication-safety behavior (`PUB-UNKNOWN-001`, `PUB-IDEMP-001`, `PUB-AUTH-001`, `PUB-READBACK-001`) does not need remediation based on this proof — it performed as designed under real ambiguity.

## What this verdict does NOT prove

- It does not prove Morning Editorial's Sep 5 failure was caused by a permanent DNS/configuration defect (STRONG-but-not-CONFIRMED transient pattern only).
- It does not prove or disprove that the Astra content was rejected by Instagram/Zernio for a specific reason — only that it currently sits in `draft`, not `published`, on the provider side.
- It does not prove the Publish Notifier failed — only that its expected evidence trail is absent for both in-window manifests.
- It does not prove any of the 7 `NOT_EXERCISED` criteria are actually implemented correctly or incorrectly — no triggering event for them occurred in this window, so this proof is silent on them, not reassuring about them.
- It does not prove that #27/#28/#29/#30's repo-level implementations will fix the observed production defects once deployed via #37 — that requires a real scheduled-run validation after deployment, which is explicitly out of scope here.
- It does not constitute closure of issue #3, authorization for #37, or any production mutation, retry, or deployment — none of those actions were taken or are recommended to be taken automatically as a result of this document.
