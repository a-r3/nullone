# #80 Breaking Radar production integration (repository decision, not applied)

Status: **IMPLEMENTED & REVIEWED — NOT DEPLOYED**. No production file,
OpenClaw job, prompt, Zernio call, Telegram send, ledger/state change, or
synthetic Breaking event resulted from #80. Natural live proof remains
deferred to #37 (`NOT_EXERCISED` permitted).

## 1. Current live Radar (read-only, 2026-09-08)

- Job `texbrif-breaking-radar`, id `096c5e8c-0ac0-46d1-948a-06727ce329bf`,
  enabled, agent `main`, isolated target.
- Schedule `30 11,14,17,20,23 * * *`, timezone `Asia/Baku` — i.e.
  11:30 / 14:30 / 17:30 / 20:30 / 23:30. Last status `ok`.
- Payload `agentTurn`: `Read social/ops/prompts/breaking-radar.md and
  execute it exactly.` Model `anthropic/claude-haiku-4-5`,
  `toolsAllow: ["*"]`, delivery `none`, no `failureAlert` configured.

The repository target preserves these exact slots (no supersession: the
confirmed live cadence already matches the approved monitoring rhythm).

## 2. Chosen production topology

```text
existing Radar agent job (discovery/verification LLM surface,
  structured-handoff-aware prompt, unchanged schedule)
  -> staged assessment JSON + deterministic commit edge
  -> committed handoff spool (atomic, validated, receipted)
  -> static consumer job (sweep, replay-safe, per-file isolated)
  -> Breaking candidate runner (#27 persistence, #30 notify)
  -> #63 BreakingWorkflow (Story-first, unchanged)
```

The Radar agent never executes workflow logic; deterministic code never
parses Markdown. A separate static consumer (rather than agent-invoked
execution) was chosen so a crash between artifact commit and dispatch is
recoverable on the next sweep without LLM reconstruction, and so the
agent gains no execution/publication capability.

Desired consumer (DESIRED / NOT DEPLOYED):

```bash
openclaw automations create "45 11,14,17,20,23 * * *" \
  --name "nullone-breaking-consumer" \
  --command "python3 <repo>/workspace/social/ops/scripts/nullone-breaking-consume.py sweep" \
  --command-cwd "<repo>" \
  --tz "Asia/Baku"
```

Fifteen minutes after each scan bounds latency while leaving the ~1min
agent run ample room. No arguments, no dynamic interpolation, no secret.

## 3. Radar slots and scan identity

Immutable slots (`nullone_schedule_registry.py`, `breaking-radar`
namespace): 11:30 / 14:30 / 17:30 / 20:30 / 23:30 Asia/Baku
(`breaking-radar.scan-1130.v1` … `.scan-2330.v1`). Before the first slot
→ `NO_DUE_SCAN`; at/after → latest same-date due scan; missed slots
coalesce; no previous-day backfill; no cron parser/DB/queue.

`source_occurrence_id = <schedule_id>@<scheduled_for>` — stable per
slot, independent of retry time, UUIDs, candidate text, or model prose.
Candidate occurrence: `SHA256([source_occurrence_id, candidate_id])[:24]`
(existing edge helper). Same scan + same candidate → stable; same scan +
different candidates → distinct; corrected assessment (same
`candidate_id`) → stable; `triggered_at` excluded. Morning/Daily/Story
resolution is byte-for-behavior unchanged.

## 4. Candidate-ID rule

Stable lowercase slug, 2–8 hyphen segments, ≤80 chars, built from a
stable anchor (primary announcement/source identity + topic slug).
Enforced at commit and re-checked before workflow; rank/title/timestamp
derivation forbidden by prompt contract.

## 5. Strict handoff and commit

Schemas reused verbatim: `nullone.breaking-radar-handoff.v1` envelope +
`nullone.breaking-workflow-input.v1` assessment (exact vocabularies:
verification UNVERIFIED/PARTIAL/PASS/BLOCKED, severity
NORMAL/MATERIAL_BREAKING/EXCEPTIONAL_BREAKING, NEWS/BREAKING only; no
FAIL reintroduced; non-PASS fail-closed before drafts).

Commit edge (`nullone-breaking-scan.py`): `current-scan` (identity, no
side effects) → agent stages assessment JSON → `commit` (candidate-ID
shape, DUE-slot resolution, envelope build, edge strict-validation,
atomic write, receipt update) / `record-empty` (truthful
NO_MATERIAL_DEVELOPMENT). Identical recommit idempotent; conflicting
content → COMMIT_CONFLICT; out-of-slot, malformed, symlink, or
contradictory-empty commits rejected. M0 source allowlist (`openclaw`)
mirrors the wake-up edge; alternate namespaces mint distinct identities.

Canonical path:
`social/ops/breaking-handoffs/<source_occurrence_id>/<candidate-external-id>.json`
plus `scan-receipt.json`
(`nullone.breaking-radar-scan-receipt.v1`: scan identity, status
NO_MATERIAL_DEVELOPMENT | CANDIDATES_EMITTED, emitted refs). Zero
candidates is valid truth; receipts never bypass validation.

## 6. Production runner

`run_breaking_candidate` (`nullone_breaking_candidate_runner.py`):
normalize (malformed → FAILED, no workflow) → candidate-ID shape →
run_id → per-run lock → persisted #27 FIRST (replay returns it; corrupt
or identity-mismatched fails closed, never a replacement run) →
`run_breaking_workflow` (Haiku writer, numeric-scope verifier,
MCP-backed DraftConnector with live path still UNPROVEN per #81,
Telegram ReviewDelivery, assessment-attested dependency recheck, NO main
provider/verifier) → existing `run_outcome_mapping()` → assess + emit
under `social/ops/run-outcomes/breaking` → reload/validate → #30 notify
once. COMPLETED + exit 0 for every established outcome (including
actionable UNKNOWN); FAILED + non-zero only for establishment failures
(native alert owns those; no duplication, no retries around
draft/delivery/ambiguity). No publication capability.

## 7. Story-first and source split

#36/#63 semantics untouched: STORY first; main never prepares before
Story SUCCEEDED (`DRAFT_CREATED` + SENT); Story failure → main never
attempted; optional-main failure preserves Story success; no main-first
fallback. With no reviewed real main provider, optional main stays
`BLOCKED_BEFORE_ATTEMPT`. Normal scheduled Story keeps the Morning
handoff; Breaking Story uses only the Radar handoff; both converge after
their own admission into the shared #33 core.

## 8. Relation to #79 / #81

- #79: reused as-is (writer, verifier, DraftConnector, ReviewDelivery,
  #27/#30 patterns, source-independence).
- #81: the DraftConnector live path is wired as the desired dependency
  but remains UNPROVEN; #81 owns its proof/replacement. #81 stays OPEN.

## 9. Failure and recovery

- Handoff persisted, crash before dispatch → next consumer sweep
  processes it exactly once (#27 result existence = idempotence).
- #27 persisted, crash before CLI return → replay returns the
  authoritative result.
- Malformed/unreadable/misnamed spool files → SKIPPED_INVALID, never
  blocking; unknown non-JSON ignored; receipts never consumed.
- Deployment delta for #37: install new/changed scripts + prompts,
  create consumer job, preserve ledgers/manifests/state/jobs; rollback
  restores the Markdown-only prompt/job, removes the consumer edge,
  never rewinds ledgers or deletes review drafts.

## 10. Live truth

No Radar prompt change deployed, no handoff committed in production, no
consumer job, no Breaking production run, no Telegram/Zernio proof. All
`UNPROVEN_LIVE / DEFERRED_TO_#37`.
