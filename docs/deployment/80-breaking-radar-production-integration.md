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
  -> staged assessment JSON under social/ops/breaking-staging
  -> deterministic commit edge (fcntl-serialized, receipted)
  -> committed handoff spool (receipt = COMMIT AUTHORITY)
  -> static consumer job (receipt-authoritative sweep)
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

Stable lowercase slug, 2–8 hyphen segments, ≤80 chars. Stable-anchor
derivation (primary announcement/source identity + topic slug) is
required by the Radar prompt contract. The deterministic edge enforces
canonical slug **shape** at commit and re-checks shape before workflow;
commit immutability prevents same candidate occurrence content overwrite.
Semantic provenance of the slug itself is **not** independently
recomputed at the commit edge. Rank/title/timestamp derivation remains
forbidden by the prompt contract.

## 5. Strict handoff and commit

Schemas reused verbatim: `nullone.breaking-radar-handoff.v1` envelope +
`nullone.breaking-workflow-input.v1` assessment (exact vocabularies:
verification UNVERIFIED/PARTIAL/PASS/BLOCKED, severity
NORMAL/MATERIAL_BREAKING/EXCEPTIONAL_BREAKING, NEWS/BREAKING only; no
FAIL reintroduced; non-PASS fail-closed before drafts).

Commit edge (`nullone-breaking-scan.py`):

- Canonical staging-root and spool-root containment:
  `social/ops/breaking-staging` and `social/ops/breaking-handoffs` must
  each resolve as a real workspace-contained directory and must not be
  symlink-mediated escape paths. A symlinked canonical root fails the
  commit before any mutation.
- Staging-root containment: staged assessments must resolve inside
  `social/ops/breaking-staging` as regular non-symlink `.json` files;
  outside paths and symlinks are rejected.
- Per-scan `fcntl` lock serializes all commit-edge mutations for one
  scan (candidate commit and empty record cannot both win).
- Strict receipt validation (`validate_scan_receipt`): exact field set,
  schema/contract_version, identity, supported source, status,
  duplicate-free candidates with external-ID shape, and
  status↔candidates invariants. Malformed receipts fail closed (never
  reinterpreted as empty).
- `NO_MATERIAL_DEVELOPMENT` is checked **before** any handoff write, so
  a rejected candidate is never left durably committed.
- Concurrent A/B candidate commits both land; the receipt lists both
  once.
- Identical recommit is idempotent and repairs a missing receipt
  listing. If an orphan already occupies the deterministic
  external-occurrence path with the same scan identity, same
  `candidate_id`, same exact assessment, and same external occurrence
  ID, a later retry may differ only in `occurrence.triggered_at`: the
  **original committed handoff bytes are preserved** (triggered_at is
  never rewritten) and the receipt listing is repaired under the scan
  lock. Assessment mutation still → `COMMIT_CONFLICT`.

Canonical path:
`social/ops/breaking-handoffs/<source_occurrence_id>/<candidate-external-id>.json`
plus `scan-receipt.json`
(`nullone.breaking-radar-scan-receipt.v1`). The receipt is the
**authoritative commit record**. Zero candidates is valid truth.

## 6. Receipt-authoritative consumer

`nullone-breaking-consume.py` never infers authority from directory
contents and never falls back to `source="openclaw"` on a
missing/corrupt receipt.

Canonical spool-root containment: `social/ops/breaking-handoffs` must
resolve as a real workspace-contained directory and must not be a
symlink. A symlinked spool root fails the sweep with
`SWEEP_AUTHORITY_CORRUPT` / non-zero CLI. Scan-directory entries that
are symlinks are never silently ignored; they fail the sweep as
`SCAN_DIRECTORY_SYMLINK` under the same authority-corruption umbrella.

For every scan directory:

1. Require exactly one valid `scan-receipt.json`.
2. Strict-validate schema, contract_version, source_occurrence_id,
   scheduled_for, source, status, candidates, created_at.
3. Receipt source must be supported; directory name must match
   `source_occurrence_id`.
4. Registry-backed identity:
   `validate_committed_scan_identity` calls
   `resolve_radar_scan(source=…, triggered_at=receipt.scheduled_for)`
   and requires a DUE resolution whose `scheduled_for` /
   `source_occurrence_id` exactly match the receipt (and directory).
   Fabricated names (e.g. `breaking-radar.fake-slot.v1@…`) fail even
   when the receipt mirrors them. Historical legitimate scans validate
   deterministically (no wall-clock dependency).

Handoff-to-receipt binding: before any workflow is invoked, every listed
handoff is bound to the exact authoritative receipt scan. The handoff's
`occurrence.source_occurrence_id` and `occurrence.scheduled_for` must
match the receipt exactly, and the handoff's computed external occurrence
ID must equal the receipt-listed external ID. Any mismatch is
authoritative spool corruption → `HANDOFF_SCAN_IDENTITY_MISMATCH` →
sweep FAILED / non-zero CLI. Mismatches are never inferred or repaired.

`NO_MATERIAL_DEVELOPMENT`: candidates must be `[]`; execute zero
handoffs; any handoff JSON present is an authoritative inconsistency.

`CANDIDATES_EMITTED`: consume **only** exact external occurrence IDs
listed in `receipt.candidates`; each listed ID must have exactly one
matching regular non-symlink JSON file; unlisted handoffs are not
executable (non-authoritative skip); missing listed files and listed
file integrity failures are authoritative corruption.

Sweep result contract (explicit, not counts-only):

- `sweep_status`: `COMPLETED` | `FAILED`
- `reason_code` / `reason_text` (stable generic text; raw exception
  messages / file contents never exposed)
- `processed` / `skipped_invalid` / `establishment_failed`

**Authoritative spool corruption → sweep FAILED / CLI non-zero** so
scheduler-native `failureAlert` can own it. This includes at least:
`RECEIPT_MISSING`, `RECEIPT_REJECTED`, `RECEIPT_SOURCE_UNSUPPORTED`,
`RECEIPT_IDENTITY_MISMATCH`, `RECEIPT_INCONSISTENT`,
`CANDIDATE_FILE_MISSING`, `CANDIDATE_PATH_REJECTED`, `SCAN_DIRECTORY_SYMLINK`,
and listed-candidate `UNREADABLE` / `NOT_A_HANDOFF` /
`FILENAME_CONTENT_MISMATCH` / `HANDOFF_REJECTED` /
`CANDIDATE_ID_REJECTED` / `HANDOFF_SCAN_IDENTITY_MISMATCH`. Unlisted
extra junk never executes and does not grant false authority.
Unexpected runner crashes and runner establishment failures likewise
fail the sweep.

## 7. Production runner

`run_breaking_candidate` (`nullone_breaking_candidate_runner.py`):
normalize (malformed → FAILED, no workflow) → candidate-ID shape →
run_id → per-run lock → persisted #27 FIRST (replay returns it; corrupt
or identity-mismatched fails closed, never a replacement run) →
`run_breaking_workflow` (Haiku writer, numeric-scope verifier,
MCP-backed DraftConnector with live path still UNPROVEN per #81,
Telegram ReviewDelivery, **caller-injected** `dependency_recheck` only —
Radar/LLM `story_safety.dependencies_available` is editorial evidence,
never a dispatch-time fallback; production #80 currently passes `None`
so #63 fails closed with `DRAFT_DEPENDENCY_RECHECK_MISSING`; no Story /
main draft work without an authoritative recheck; #81 remains
unresolved and owns live DraftProvider readiness; NO main
provider/verifier) → existing `run_outcome_mapping()` → assess + emit
under `social/ops/run-outcomes/breaking` → reload/validate → #30 notify
once.

Fresh and replay for the exact same persisted #27 result agree on
`domain_outcome`, `reason_code`, and `reason_text`. When
`domain_outcome == SUCCEEDED`, `reason_code = OK`; otherwise both paths
surface the persisted reason. Do not confuse
`application_execution=COMPLETED` with domain success.

COMPLETED + exit 0 for every established outcome (including actionable
UNKNOWN); FAILED + non-zero only for establishment failures (native
alert owns those; no duplication, no retries around
draft/delivery/ambiguity). No publication capability.

## 8. Story-first and source split

#36/#63 semantics untouched: STORY first; main never prepares before
Story SUCCEEDED (`DRAFT_CREATED` + SENT); Story failure → main never
attempted; optional-main failure preserves Story success; no main-first
fallback. With no reviewed real main provider, optional main stays
`BLOCKED_BEFORE_ATTEMPT`. Normal scheduled Story keeps the Morning
handoff; Breaking Story uses only the Radar handoff; both converge after
their own admission into the shared #33 core.

## 9. Relation to #79 / #81

- #79: reused as-is (writer, verifier, DraftConnector, ReviewDelivery,
  #27/#30 patterns, source-independence).
- #81: the DraftConnector live path is wired as the desired dependency
  but remains UNPROVEN; #81 owns its proof/replacement. #81 stays OPEN.
  #80 does not solve #81 and does not invent a fake production recheck.

## 10. Failure and recovery

- Handoff written, crash before receipt listing → orphan is not
  executable. A later recommit of the same scan + same candidate + same
  assessment may use a different `triggered_at`: the original orphan
  handoff bytes are preserved (triggered_at not rewritten), the receipt
  listing is repaired, and the consumer may then process once.
  Assessment mutation still conflicts. Directory contents alone never
  grant authority.
- Handoff + receipt committed, crash before dispatch → next consumer
  sweep processes listed candidates exactly once (#27 result existence =
  idempotence).
- #27 persisted, crash before CLI return → replay returns the
  authoritative result with identical reason and notification-error
  classification semantics (notifier raise → `NOTIFICATION_STATE_UNSAFE`;
  malformed notifier return → `NOTIFICATION_RESULT_INVALID`).
- Authoritative spool corruption (missing/corrupt/contradictory receipt
  or listed-candidate integrity failure) → sweep FAILED / non-zero CLI.
  Unlisted extras are non-authoritative skips. Unexpected runner crashes
  → sweep FAILED / non-zero CLI.
- Deployment delta for #37: install new/changed scripts + prompts,
  create consumer job, preserve ledgers/manifests/state/jobs; rollback
  restores the Markdown-only prompt/job, removes the consumer edge,
  never rewinds ledgers or deletes review drafts.

## 11. Live truth

No Radar prompt change deployed, no handoff committed in production, no
consumer job, no Breaking production run, no Telegram/Zernio proof. All
`UNPROVEN_LIVE / DEFERRED_TO_#37`. **NOT DEPLOYED.**
