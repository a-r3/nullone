# ADR-01: Recovery and Artifact Durability

Status: ACCEPTED (decision record; repository only — no recovery
infrastructure is deployed by accepting this ADR).

Date: 2026-09-11.
Scope: issue #9. Empirical proof belongs to issue #10 (restore drill),
which must test THIS decision, not invent its own.

## Context

NullOne production state is heterogeneous: reviewed Git source, JSON/JSONL
ledgers and manifests, Markdown queues, callback receipts, notifier
reservation records, run outcomes, analytics artifacts, rendered media
bytes, live OpenClaw automation config, secret-store entries, and external
provider truth (Zernio). There is no backup service, no encrypted copy,
and no proven restore path today. Local disk is not a backup.

Prior art in-repo: deterministic manifests with persisted attempt counts
(#89/#90), reserve-before-side-effect notifiers (#30), occurrence locks
and idempotent result emission (#27/#28/#29), atomic install with hash
proof and backups (release CLI, PR #102). None of these constitute a
recovery story on their own.

## Decision

Separate ALL recovery reasoning into two independent questions:

**DATA AVAILABILITY != PUBLICATION SAFETY.**

1. Ordinary operational state (queue, non-consequential outcomes,
   analytics/editorial artifacts, derived state) gets a PROPOSED design
   target of RPO <= 15 minutes / RTO <= 4 hours, with
   OPERATOR_ACCEPTANCE=PENDING_RAUF_ALIZADA. These numbers are discussion
   targets, not accepted truth.

2. Publication-critical state (final authorization, attempt counts,
   in-flight claims, authorization receipts, provider identifiers, known
   results) gets NO loss SLA today. Instead it gets a stronger safety
   guarantee: a restored environment whose critical history cannot be
   independently proven stays publisher-DISABLED (`PUBLISHER_STATE=DISABLED`,
   `RECOVERY_STATE=CHECK_REQUIRED`, `AUTOMATIC_RETRY=FORBIDDEN`) until a
   human operator reconciles against provider truth. Losing ordinary state
   is recoverable; replaying a possibly-dispatched publication is not, so
   the system fails toward silence, never toward duplicate publication.

3. Source-of-truth precedence on conflict: reviewed Git commit for
   repository bytes; proven provider PUBLISHED status may reconcile stale
   local PUBLISHING (never causing a retry); final authorization and
   attempt history require explicit durable evidence (never inferred from
   topic text, queue status, Telegram messages, or draft existence);
   terminal receipts are historical audit records and are never rewritten
   because provider truth later advances; UNKNOWN/conflict fails closed.

4. No SQLite exists in production-critical state today
   (`CURRENT_SQLITE_CRITICAL_STATE=NONE`; `sqlite3` is statically
   forbidden in the schedule registry). Any future SQLite state must use a
   consistent snapshot mechanism (checkpoint + copy, or dump), never raw
   live-file copying.

5. Signed/presigned URLs are metadata, never durable media copies.
   Rendered bytes reproducible from immutable inputs need no independent
   retention; original remote bytes need it, with hash verification.

6. Secrets, OAuth state, sessions, and tokens are NEVER part of recovery
   artifacts. Each has a named source/recovery owner and, where applicable,
   a re-authentication requirement.

7. Future independent recovery copies must be encrypted at rest with
   separated key-recovery ownership, separated failure domain, retention
   classes, and hash verification. Key loss means backup unusable — stated
   explicitly, never hand-waved. Provisioning that storage is OUT OF SCOPE
   for this ADR.

## Alternatives considered

- **Zero-loss SLA for everything.** Rejected: unproven and unfalsifiable
  with current architecture; claiming it would be a fabricated guarantee.
- **Best-effort restore with automatic catch-up publication.** Rejected:
  directly risks blind/duplicate publication, violating the primary safety
  invariant (max one publication attempt, ambiguity → UNKNOWN, no retry).
- **Defer all recovery thinking until a host migration forces it.**
  Rejected: #37 controlled deployment and any future migration need the
  contract first; thinking is cheap, incidents are not.

## Consequences

- Issue #10 (restore drill) has a fixed oracle: it must demonstrate the
  tabletop scenarios in `docs/operations/recovery-contract.md`, including
  key-loss-means-unusable, stale-attempts-never-retry, and
  missing-history-means-disabled.
- #37 controlled deployment inherits the stale-backup rules and the
  publisher-disabled default for any fresh/uncertain environment.
- The proposed ordinary RPO/RTO numbers remain PENDING until Rauf Alizada
  explicitly accepts them; no schedule, snapshot job, or SLA may cite them
  as approved before that.

## Migration / future work

- Possible future RPO=0 independent durable record for critical
  acknowledgements: DESIGN_TARGET only, not a current guarantee.
- Encrypted independent copy provisioning: separate future scope.
- PostgreSQL-backed Core (M3) must re-derive this ADR's precedence and
  safety rules for transactional state; this ADR does not assume them.
