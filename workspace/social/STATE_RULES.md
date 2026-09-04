# NullOne Publication State Rules V1

Status: ACTIVE

## Source of truth

For publication/duplicate state:

1. production manifest publication state
2. social/state/publish-ledger.jsonl
3. candidate queue
4. editorial board

A stale queue or board MUST NOT override a confirmed publication ledger entry.

## Terminal/unsafe-to-repeat states

A candidate must NOT be drafted again when an existing publication record
for the same exact candidate shows any consequential live state such as:

- PUBLISH_ACCEPTED
- PUBLISHING
- PUBLISHED
- UNKNOWN after a publication attempt
- READBACK_FAILED after a publication attempt

These states all mean:

DO NOT CREATE A DUPLICATE LIVE PUBLICATION.

## Queue status

PUBLISHED is a valid terminal candidate status.

A PUBLISHED candidate must never return to READY because of a later
Morning Editorial or Breaking Radar scan.

A materially new development may become a FOLLOW-UP candidate only when
the angle and audience value are genuinely distinct.

## Verification state

For Content Strategy V1 candidates:

VERIFICATION_STATUS: PASS
is required for READY.

PARTIAL must remain:
- NEW
- RESEARCHING
- DEFERRED

until the exact production claim can be verified.

BLOCKED can never become READY without new evidence and re-verification.

Historical pre-V1 candidates without verification_status may be reverified
by Draft Factory, but publication-ledger duplicate checks remain mandatory.

## Editorial consistency

WHY_NOW, source status and VERIFICATION_STATUS must agree.

Examples:

- if a primary earnings release is already public, do not say the numbers
  are "not yet published";
- if legislation is only prepared/submitted, do not describe it as enacted;
- if a product is early access, preview or beta, preserve that scope.

## Mandatory pre-production check

Before Draft Factory selects any candidate:

1. check publish-ledger;
2. check manifests when present;
3. check queue;
4. reject exact already-published candidates;
5. apply topic-cluster derivative limits.

Publication state beats freshness and score.


## Legacy draft accounting — V1

Historical Zernio drafts created before Production Bridge V1 use explicit
non-active states.

LEGACY_DRAFT:
- real Zernio object exists
- status is still draft
- retained for audit/history
- must not be selected again automatically
- must not count toward today's active draft load

SUPERSEDED_DRAFT:
- historical draft replaced by a newer preferred draft
- retained for audit/history
- never counts toward active draft load
- never becomes READY automatically

Only a current Production Bridge manifest may represent an active approval
draft.

For main-post load accounting, count a draft as active only when:
- a manifest exists under social/ops/manifests/
- manifest validation passes
- review.state == DRAFT_CREATED
- review.zernio_draft_id is present
- publication has not reached a terminal live state

A queue-only historical DRAFTED/LEGACY_DRAFT/SUPERSEDED_DRAFT record without
a current Production Bridge manifest does not count as an active draft.

For append-only ledgers, a later reconciliation event may supersede the
descriptive current state of an older event, but historical rows must not be
deleted.

Any historical consequential publish attempt remains unsafe to repeat even
when a later readback clarifies its final state.

PUBLISHED is terminal for the exact candidate.
