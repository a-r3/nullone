# Zernio Publishing

Platform: Instagram
Username: @nullone.az
Account ID: 6a982bbf77555aae01c28f21
Account type: BUSINESS
Status: active

## Publishing states

1. PREFLIGHT
   Zernio validation only.
   Must not create or publish anything.

2. DRAFT
   May create a Zernio draft only.
   No publishNow and no scheduledFor.

3. PUBLISH
   Disabled until explicitly enabled in CONTENT_RULES.md.

## Rules

Every Instagram publication must pass:
- editorial verification
- factual verification
- media validation
- Zernio post validation

Never expose ZERNIO_API_KEY.

For Instagram:
- Feed: image/video required
- Carousel: up to 10 media items
- Story: platformSpecificData.contentType = "story"
- Reel: one video

NullOne feed media MUST be exactly 1080x1350 (4:5). Stories and Reels MUST be exactly 1080x1920 (9:16). Carousel slides MUST all be exactly 1080x1350.

## Canonical integration path

CURRENT PRODUCTION INTEGRATION:

OpenClaw
→ official Zernio MCP
→ OAuth
→ Instagram

MCP server:
https://mcp.zernio.com/mcp

The legacy raw REST integration using ZERNIO_API_KEY is DEPRECATED and must
not be used for normal NullOne operations.

Agents must prefer Zernio MCP tools for:
- accounts
- drafts/posts
- validation
- analytics
- media preparation/upload discovery

Do not fall back to ZERNIO_API_KEY or direct authenticated curl calls if an
MCP operation fails.

If MCP authentication fails:
STOP and report BLOCKED.

Publishing rules remain unchanged:
DRAFT_FIRST.
Never publish or schedule without explicit operator authorization for the exact post.

## Canonical integration — 2026-09-02

All NullOne authenticated Zernio operations use:

OpenClaw -> official Zernio MCP -> OAuth -> Instagram

Legacy ZERNIO_API_KEY / raw authenticated REST is deprecated and must not be used.

Use MCP for:
- accounts
- post drafts
- post reads
- validation
- analytics
- media presign/discovery

For local media:
local file -> MCP presign -> direct unauthenticated PUT -> MCP validate -> MCP posts_create.

Current publishing mode:
DRAFT_FIRST.

Never publish or schedule without explicit operator approval for the exact post.

## FINAL MCP SAFETY CONTRACT

Canonical authenticated path:

OpenClaw -> official Zernio MCP -> OAuth -> Instagram

Legacy API-key / authenticated REST / secret-egress Zernio integration is forbidden.

### Static MCP surface

Only the explicitly filtered NullOne Zernio MCP tools may be used.

### Dynamic call_tool allowlist

zernio call_tool may invoke ONLY:

1. media_get_media_presigned_url
2. posts_create_post

All other dynamic Zernio targets are unauthorized.

Explicitly forbidden:
- publish / publish_now
- delete
- unpublish
- publication retry
- comments or replies
- mentions or replies
- messaging
- ads
- account/profile mutation
- queue mutation
- scheduling
- cross-posting

If another dynamic capability is required:
STOP as BLOCKED and require an explicit operator rule change.

### Publishing authority

CURRENT MODE = DRAFT_FIRST

Autonomous system MAY:
- monitor/research
- verify
- render
- upload media
- validate
- create drafts
- read analytics
- learn from analytics

Autonomous system MUST NOT:
- publish
- schedule
- delete
- unpublish

A draft being technically publishable is NOT publishing authorization.

### Human approval gate

Publishing requires explicit operator authorization for the exact Zernio post ID.

Generic approval such as:
- looks good
- okay
- continue

does NOT authorize publication.

### scheduledFor caveat

Zernio may auto-populate scheduledFor with a creation-time value for drafts.

Do not infer scheduling from scheduledFor alone.

A NullOne post is considered safely unpublished when:
- status = draft
- no explicit schedule action occurred
- no explicit publish action occurred

## HUMAN-AUTHORIZED PUBLISH EXCEPTION

Autonomous Draft Factory remains forbidden from publishing.

The ONLY publication-capable path is:

Rauf
-> Telegram NullOne Control
-> first approval callback
-> second explicit publish callback
-> texbrif-approval agent
-> posts_update_post

Authorized dynamic write:

posts_update_post

ONLY with:

{
  "post_id": "<EXACT APPROVED POST ID>",
  "is_draft": false,
  "publish_now": true
}

Only agent:
texbrif-approval

may use this publication exception.

It may occur only after:

callback_data: texbrif:publish:<POST_ID>

Main/Draft Factory may NOT use posts_update_post to publish autonomously.
