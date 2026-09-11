# NullOne Recovery Contract

Status: authoritative recovery semantics for issue #9 (decides), per
ADR-01. Repository only — no recovery infrastructure is deployed.
Empirical proof belongs to issue #10 (restore drill):
**#9 = RECOVERY GUARANTEE / ADR, #10 = RESTORE DRILL / EMPIRICAL PROOF.**

Guarantee vocabulary: PROVEN | DESIGN_TARGET | CURRENT_LIMITATION |
EXTERNAL_AUTHORITY. Future targets are never presented as guarantees.

## State classes

### 1. repository_source

- Examples: all Git-tracked code, prompts, contracts, policy, fixtures.
- Authoritative copy: exact reviewed Git commit (origin/main).
- Secondary: local checkouts (untrusted until verified against commit).
- RPO/RTO: n/a (immutable history) — PROVEN via Git object integrity.
- Retention: Git history itself. Restore order: 1. Owner: repository.
- Replay risk: none. Publisher activation: n/a. Status: PROVEN.

### 2. release_deploy_metadata

- Examples: `deploy-state/current.json`, `history.jsonl`, backups.
- Authoritative copy: production-local deploy-state.
- Secondary: backup metadata + Git history (releases are re-derivable).
- RPO/RTO: same as ordinary state (proposed 900s/14400s, DESIGN_TARGET,
  OPERATOR_ACCEPTANCE pending).
- Restore order: 2 (with safe config references). Owner: release CLI.
- Replay risk: none from metadata alone (it authorizes nothing by itself).
- Publisher activation: metadata never enables publication. Status:
  DESIGN_TARGET (tool merged, not activated).
- Repository merge and production deployment remain separate
  (MERGE != DEPLOY): deploy-state is rewritten only by controlled
  release operations, never by Git merge.

### 3. candidate_queue (ordinary)

- Examples: `social/state/candidate-queue.md`, `topic-ledger.jsonl`.
- Authoritative copy: production files. Secondary: future encrypted copy
  (not provisioned).
- RPO/RTO: proposed 900s/14400s — DESIGN_TARGET, OPERATOR_ACCEPTANCE
  PENDING_RAUF_ALIZADA. Current: CURRENT_LIMITATION (no snapshot job).
- Retention: operator-defined (undecided — pending). Restore order: 3.
  Owner: TBD operator runbook (pending).
- Replay risk: stale queue entries do NOT imply unpublished content; queue
  status is never authorization evidence.
- Publisher activation: not required from this class. Status:
  CURRENT_LIMITATION.

### 4. run_outcomes_and_editorial_artifacts (ordinary)

- Examples: `run-outcomes/`, handoffs, boards, analytics raw/reports.
- Same RPO/RTO/retention posture as class 3 (proposed, pending).
- Restore order: 3. Replay risk: none (read-only evidence).
- Publisher activation: not required. Status: CURRENT_LIMITATION.

### 5. publication_attempt_history (CRITICAL)

- Examples: manifest `attempts` / `create_attempts`, controller
  in-flight claim flags, known result/reconciliation evidence.
- Authoritative copy: production manifests + controller receipts.
- Secondary: future independent durable record (DESIGN_TARGET only).
- RPO/RTO: NO SLA CLAIMED. Safety guarantee instead (see §6 of task
  scope / below): missing, ambiguous, stale, or incomplete history →
  `PUBLISHER_STATE=DISABLED`, `RECOVERY_STATE=CHECK_REQUIRED`,
  `AUTOMATIC_RETRY=FORBIDDEN`.
- Retention: indefinite for terminal attempt records (audit).
- Restore order: 4, validated in step 5. Owner: publication controller.
- Replay risk: THE central risk — restored `attempts=0` does NOT prove no
  historical attempt happened. Status: CURRENT_LIMITATION with
  fail-closed safety rule (rule itself is PROVEN in code paths).

### 6. final_authorization_evidence (CRITICAL)

- Examples: per-human-authorization receipts
  (`publish-callback-receipts/<POST_ID>/<uuid>.json`), first-stage
  evidence where workflow continuity needs it, provider review/live IDs.
- Same no-SLA + disabled-until-proven posture as class 5. Authorization
  can NEVER be inferred from topic text, queue status, Telegram messages,
  or provider draft existence — explicit durable evidence required.
- Restore order: 4. Status: CURRENT_LIMITATION with fail-closed rule.

### 7. notifier_state (ordinary, reservation semantics)

- Examples: `notifications/<workflow>/` PENDING records, Telegram
  delivery state.
- RPO/RTO: proposed ordinary targets (pending). Restore order: 8.
- Replay risk: none by design (any existing record blocks automatic
  re-send, mirroring publication-timeout semantics).
- Publisher activation: not required. Status: CURRENT_LIMITATION.

### 8. rendered_and_source_media

- Metadata/reference (paths, URLs, hashes, dimensions): ordinary,
  proposed RPO/RTO pending.
- Object bytes reproducible from immutable inputs (renderer + fixtures +
  fonts): NO independent retention required; re-render on demand
  (STRUCTURAL_DETERMINISM per issue #8).
- Original remote bytes NOT reproducible (source photos, uploads):
  independent retention required with SHA256 verification; retention
  owner TBD (pending).
- Signed/presigned URLs are NOT durable copies: expiry kills recovery;
  metadata alone never recovers bytes.
- Restore order: 3 (metadata), originals per retention decision.
- Status: DESIGN_TARGET (rules decided; retention store not provisioned).

### 9. openclaw_automation_config (EXTERNAL_AUTHORITY)

- Examples: live jobs, schedules, plugin enablement, tool grants.
- Authoritative copy: live OpenClaw production config (mutable
  production state, never in Git).
- RPO/RTO: owned by the OpenClaw layer, not this contract.
- Restore order: 2 (safe configuration references only — desired-state
  docs, never live secrets). Publisher activation: cutover owns it (#37).
- Status: EXTERNAL_AUTHORITY.

### 10. credentials_oauth_sessions (EXTERNAL, never bundled)

| Item | Source/recovery owner | Recreation | Re-auth required | Never in Git |
|---|---|---|---|---|
| `zernio.analytics.bearer` (`ZERNIO_ANALYTICS_API_TOKEN`) | operator secret provisioning | yes, re-provision | yes | yes |
| `zernio.drafts.bearer` (`ZERNIO_DRAFT_API_TOKEN`) | operator secret provisioning | yes, re-provision | yes | yes |
| `zernio.publish.bearer` (`ZERNIO_PUBLISH_API_TOKEN` SecretRef) | OpenClaw protected store | yes, re-provision | yes | yes |
| Zernio OAuth state | Zernio provider | re-authorize | yes | yes |
| Claude/OpenClaw local sessions | local runtime | re-login | yes | yes |
| Telegram account/session state | Telegram/OpenClaw | re-login | yes | yes |

No values inspected or recorded to produce this table.

## Source-of-truth precedence (recovery semantics, not new code)

1. Repository bytes: exact reviewed Git commit wins over any checkout.
2. Provider truth: proven remote PUBLISHED may reconcile stale local
   PUBLISHING (settles the record; never triggers a retry).
3. Authorization/attempts: explicit durable evidence only; never inferred.
4. Receipts: terminal callback receipts are historical audit records; a
   later provider advance does not rewrite them.
5. UNKNOWN/conflict: fail closed → CHECK_REQUIRED → no automatic retry.

## Restore order (fail-closed sequence)

1. Recover reviewed repository/runtime version (Git SHA + inventory).
2. Recover safe configuration references (desired-state docs; no secrets).
3. Restore ordinary state snapshot (queue, outcomes, artifacts, media metadata).
4. Restore independent critical authorization/attempt history.
5. Validate manifests/receipts/ledgers consistency (hashes, bindings).
6. Query external provider truth READ-ONLY where required (no side effects).
7. Reconcile stale local publication state (PUBLISHED settles; never retries).
8. Validate Telegram/notifier state (reservations respected).
9. Keep publisher DISABLED.
10. Operator reviews the recovery report.
11. Only then explicitly re-enable publication capability — no automatic step.

No publication side effect may occur during restore validation.

## Stale-backup rules

- Stale queue entries do not imply unpublished content.
- Stale PUBLISHING does not authorize retry.
- Restored `attempts=0` does not prove no historical attempt happened.
- Telegram approval messages are not sufficient authorization evidence.
- Provider DRAFT/PUBLISHED truth must be checked where relevant.
- Missing critical receipt/attempt history → CHECK_REQUIRED.
- Duplicate-publication prevention wins over automatic recovery speed.

## SQLite rule

`CURRENT_SQLITE_CRITICAL_STATE=NONE` — no production-critical state uses
SQLite today (`sqlite3` is statically forbidden in the schedule registry
by negative tests). Any future SQLite state must use a consistent
snapshot mechanism (checkpoint + file copy, or logical dump), never raw
live-file copying.

## Independent encrypted copy (future architecture, not provisioned)

- Encryption at rest: required, algorithm/mechanism TBD at provisioning.
- Key recovery ownership: separated from host operator (TBD — pending;
  must be named before any backup is trusted).
- Separation: backup data in a different failure domain than the primary
  host; retention classes per state class above; SHA256 integrity
  verification on write and on restore.
- Key loss: backup exists but key lost → backup UNUSABLE; recovery is
  NOT claimed (tabletop B). This must be stated in every restore report
  where it applies.

## Tabletop scenarios (oracle for issue #10)

A. PRIMARY DISK LOSS — repo re-cloned at reviewed SHA; ordinary snapshot
   available; critical history available → restore per order above;
   publisher re-enabled only after operator report review.
B. ENCRYPTED BACKUP EXISTS, KEY LOST → backup unusable; do NOT claim
   recovery; rebuild ordinary state from re-derivable sources; critical
   history missing → publisher DISABLED.
C. PARTIAL RESTORE (manifest present, receipt missing) → CHECK_REQUIRED,
   publisher DISABLED until receipt re-proven or reconciled.
D. STALE SNAPSHOT (`attempts=0`, provider reports PUBLISHED) → reconcile
   record to PUBLISHED; NO RETRY, ever.
E. SIGNED URL EXPIRED → metadata alone recovers nothing; original bytes
   required (or re-derivable render); record the gap.
F. REMOTE BACKUP OUTAGE → ordinary availability degrades; critical
   durability requirement is unchanged (disabled until proven).
G. PROVIDER UNREACHABLE DURING RECOVERY → UNKNOWN / CHECK_REQUIRED;
   no retry; report and wait for operator.
