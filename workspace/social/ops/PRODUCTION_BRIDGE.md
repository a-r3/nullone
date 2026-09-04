# NullOne Production Bridge V1

Status: BUILDING

## Purpose

Production Bridge separates editorial reasoning from external publication
transport.

Target flow:

Draft Factory
-> immutable production manifest
-> Draft Bridge
-> Zernio review draft
-> Telegram preview
-> first human approval
-> second human publish confirmation
-> Publish Bridge
-> new live Zernio post
-> readback
-> Telegram result

## Canonical destination

Instagram:
@nullone.az

Zernio account ID:
6a982bbf77555aae01c28f21

The account ID is authoritative.
A stale cached username such as texbrif must not block a valid account.

## Review draft vs live post

They are different objects.

review.zernio_draft_id
is the review/audit draft.

publication.live_zernio_post_id
is the post created by the supported live publication path.

Never assume they are the same ID.

## Human boundary

No live publication may occur from:
- ordinary text
- first approval
- editorial completion
- successful draft creation

Live publication requires the second Telegram callback:

texbrif:publish:<REVIEW_POST_ID>

The publisher path must record:

source = texbrif-approval
operator = Rauf
human_confirmation = two_step

## Idempotency

Review draft:
- maximum one create attempt once an external create call may have occurred
- ambiguous create result -> REVIEW_UNKNOWN
- never automatically create another review draft

Live publication:
- publication.attempts starts at 0
- immediately before the live external publish call it becomes 1
- maximum allowed value is 1
- timeout / malformed / ambiguous response -> UNKNOWN
- UNKNOWN must never auto-retry publication

Only read-only reconciliation is allowed after UNKNOWN.

## Manifest binding

Human approval binds to the exact manifest content.

Before draft creation and before publication verify:
- verification == PASS
- canonical account ID
- caption SHA-256 unchanged
- every local media SHA-256 unchanged
- required dimensions unchanged

If approved content changes:
the previous authorization is invalid.

## Sensitive data

Never persist:
- presigned uploadUrl
- OAuth tokens
- API keys

Public media URLs may be persisted because they are publication assets.

## Bridge models

Until OpenClaw MCP projection is fixed:

OpenClaw editorial runtime
-> local deterministic bridge
-> direct Claude Code Zernio MCP
-> Zernio

Use Haiku for transport where possible.

Claude Code publication permission must be granted only for the specific
final publish invocation. Do not permanently allow posts_publish_now.
