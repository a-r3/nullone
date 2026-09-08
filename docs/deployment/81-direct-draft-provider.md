# 81 — Direct Zernio DraftProvider

Status: **IMPLEMENTED IN PR — NOT DEPLOYED**. This document describes the
reviewed replacement for the MCP-backed review-draft transport path. Nothing
here has been applied to production: no credential has been provisioned, no
Zernio draft has been created, and no deployment action has been taken.

## 1. Read-only investigation verdict

**REPLACEMENT_REQUIRED**.

Prior read-only investigation (Sep 7-8, 2026) confirmed that the production
review-draft path depended on:

    DraftConnector → nullone-draft-bridge.py → claude -p → Zernio MCP

This path is non-deterministic, depends on interactive OAuth/consent state,
and couples application code to MCP tool surfaces. It must be replaced with a
deterministic, testable direct HTTPS path.

## 2. Desired replacement

    DraftConnector → nullone-draft-bridge.py → direct deterministic Zernio REST

The new path is:

- `NulloneDraftBridgeConnector` (in `nullone_story_pipeline.py`) — unchanged,
  continues to shell to `nullone-draft-bridge.py` via the existing CLI contract.
- `nullone-draft-bridge.py` — the stable CLI edge. After #81 it no longer
  imports or invokes `nullone_claude`, `run_structured`, `claude -p`, or any
  `mcp__zernio__*` tool. It delegates to the new direct adapter.
- `nullone_zernio_draft_adapter.py` — the direct deterministic REST DraftProvider.
  This is the ONLY module that knows about Zernio-specific HTTPS paths,
  response envelopes, and the draft/write credential.
- `nullone_draft_provider_factory.py` — the production factory boundary. Owns
  the only mapping from the logical secret id to a runtime source.

## 3. Dedicated draft/write credential

| Logical secret id | Runtime source | Syntax in source |
| --- | --- | --- |
| `zernio.drafts.bearer` | inherited process environment variable | `ZERNIO_DRAFT_API_TOKEN` |

This is deliberately separate from `zernio.analytics.bearer`
(`ZERNIO_ANALYTICS_API_TOKEN`). The analytics credential is read-only and must
never be reused for draft creation, media presign, or any write surface.

The environment-variable binding exists only inside
`nullone_secret_provider.py`. Neither the adapter nor the factory references
it (enforced by `tests/test_draft_provider_factory.py` and
`tests/test_zernio_draft_adapter.py`).

The credential value is:
- never committed
- never printed or logged
- never hashed/prefixed/suffixed/length-leaked
- wrapped in `SecretValue` so `repr()`/`str()`/`format()` render the fixed
  token `<redacted>`
- constructed through the `SecretProvider` boundary at production time

No real credential has been provisioned in this task.

## 4. REST endpoint contract

Contract source: Zernio official OpenAPI `docs.zernio.com/api/openapi`
(`openapi: 3.1.0`, `info.version: "1.0.4"`), inspected 2026-09-08.

Base URL: `https://zernio.com/api/v1`

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/accounts` | Read-only account preflight |
| POST | `/media/presign` | Request a presigned upload URL |
| PUT | `<presigned uploadUrl>` | Upload exact bytes (unauthenticated) |
| POST | `/tools/validate/media` | Validate media |
| POST | `/tools/validate/post` | Validate complete post payload (same body as `POST /posts`) |
| POST | `/posts` | Create exactly one draft |
| GET | `/posts/{postId}` | Readback the created draft |

### 4.1 POST /media/presign

Request (exact — `size` is never sent):

```json
{
  "filename": "source-0.png",
  "contentType": "image/png"
}
```

Success response (`getMediaPresignedUrl`; no `status`/`error` envelope):

```json
{
  "uploadUrl": "<presigned-upload-url>",
  "publicUrl": "https://media.zernio.com/temp/1234567890_abc123_my-video.mp4",
  "key": "temp/1234567890_abc123_my-video.mp4",
  "expiresIn": 3600
}
```

Both URLs must be HTTPS. The upload URL stays memory-only; only the public
URL is persisted, and only after a confirmed `PUT` success.

Required-field discipline (verified against the current schema): the
presign success schema declares no `required[]` set, so `key`/`expiresIn`
are optional and never enforced.

### 4.2 POST /tools/validate/media

Request (exact — `url` only):

```json
{
  "url": "<public URL>"
}
```

Success response (`validateMedia`; no `status` envelope):

```json
{
  "valid": true,
  "url": "https://example.com/image.jpg",
  "contentType": "image/jpeg",
  "size": 250880,
  "type": "image",
  "platformLimits": {
    "instagram": {"limit": 8388608, "limitFormatted": "8.0 MB", "withinLimit": true}
  }
}
```

Requires `valid is True`, a trustworthy structure (`type` of `image`/`video`
with a non-empty `contentType`), and an acceptable Instagram limit result
whenever the documented `platformLimits.instagram` field is present.
Failure stays before create (`create_attempts=0`, `NOT_CREATED`, zero
`POST /posts`).

Identity binding (the response must belong to the exact manifest item):
the response `url` carries no documented canonicalization rule, so a
present `url` must equal the requested public URL exactly; returned `type`
must agree with the expected `MediaItem.type` derived from the manifest
MIME by the same authority used for the create payload (e.g. a
`video/mp4` item answered as `image` fails closed); returned `contentType`
must agree under the narrow rule of lowercase/strip plus the single
documented alias `image/jpg` ≡ `image/jpeg`. Any contradiction fails
closed before create.

Required-field discipline (verified against the current schema): the
validate/media success schema declares no `required[]` set, so absent
optional metadata alone is never the failure — but identity binding
above still applies.

### 4.3 POST /tools/validate/post and POST /posts (one canonical payload)

One canonical exact payload is built and the identical object is used for
both `validate/post` and the create `POST /posts`:

```json
{
  "content": "<exact caption>",
  "mediaItems": [
    {"type": "image", "url": "<public url>"}
  ],
  "platforms": [
    {
      "platform": "instagram",
      "accountId": "<canonical account id>",
      "platformSpecificData": {"contentType": "story"}
    }
  ],
  "isDraft": true
}
```

`MediaItem.type` is derived deterministically from the manifest media MIME
type (`image/*` → `image`, `video/*` → `video`); unsupported media fails
closed. `platformSpecificData.contentType = "story"` is nested inside
`platforms[0]` for STORY only; FEED/CAROUSEL never set it. Media ordering is
preserved exactly. Never included: `publishNow`, `scheduledFor`,
`queuedFromProfile`, `queueId`, any publication/scheduling field, or the
obsolete root `platform`/`accountId`/`media`/`platformSpecificData` shape.

Post-validation success (`validatePost`; no `status`/`error` envelope):

```json
{
  "valid": true,
  "message": "No validation issues found.",
  "warnings": []
}
```

Requires `valid is True`; malformed responses fail closed before create.

Required-field discipline (verified against the current schema): the
validate/post success schema declares no `required[]` set, so `message`/
`warnings` are optional and never enforced.

### 4.4 POST /posts success envelope

Only HTTP `201` with the documented `post` envelope (`PostCreateResponse`)
proves a create:

```json
{
  "message": "Post created successfully",
  "post": {
    "_id": "<created post id>",
    "status": "draft",
    "platforms": [{"platform": "instagram", "accountId": {"_id": "<id>", "...": "..."}}]
  },
  "warnings": []
}
```

Top-level `_id` is never trusted. Unexpected `200` variants (TikTok `dryRun`
preview, same `x-request-id` idempotency hint with `existingPost`), `207`
incomplete publish, `409` content-hash dedup, and any other status are
ambiguity: `create_attempts=1`, `REVIEW_UNKNOWN`, zero retry.

### 4.5 GET /posts/{postId} readback envelope

```json
{
  "post": {
    "_id": "<exact created id>",
    "status": "draft",
    "content": "<exact caption>",
    "platforms": [
      {
        "platform": "instagram",
        "accountId": {"_id": "<canonical account id>", "...": "..."}
      }
    ]
  }
}
```

No top-level `platform`/`accountId`/`platformSpecificData` is required.
Requires `post._id` equal to the created id, `post.status` of `draft`, and
`post.platforms` to be exactly the one intended target (length 1, platform
`instagram`, account resolving to the canonical id as a plain string or an
expanded `{_id, ...}` object — an extra Twitter target, a second account,
or duplicates all fail closed), Story data matching wherever the contract
exposes it, and exact content/media identity wherever GET exposes those
fields. Unexposed fields are never invented as proof; any
insufficient/contradictory readback is `REVIEW_UNKNOWN` with no retry.

### 4.6 Transport POST allowlist

The real transport enforces an exact-path allowlist before any network
request is constructed or sent: authenticated POST is permitted only to
`/media/presign`, `/tools/validate/media`, `/tools/validate/post`, and
`/posts`. Any other POST path (e.g. `/posts/foo`, `/posts/foo/retry`,
`/publish`) fails locally with a capability error — no HTTP attempt, no
credential use. Presigned PUT stays unauthenticated and unchanged.

### 4.7 x-request-id

Sent as `X-Request-ID` on the single `POST /posts`. A valid UUID string
(per current docs, `format: uuid`, one value per logical request),
derived deterministically as UUIDv5 over immutable manifest identity
(`manifest_id`, caption/media fingerprints, format) so the same logical
review-create request carries one stable id. Defense-in-depth only:
NullOne never retries `POST /posts` after ambiguity.

## 5. Partial-presign recovery semantics

Media is iterated IN EXACT ORDER. For an item with a valid existing
`public_url`:

- do not presign again
- do not upload again
- reuse it

For an item without `public_url`:

1. request exactly one presigned URL
2. keep upload URL memory-only
3. upload exact bytes by PUT
4. only after confirmed upload success persist `public_url`
5. atomically persist manifest

Never persist/log: upload URL, signed query string, or credential.

## 6. Exactly-one create semantics

Before the single `POST /posts`:

- persist `review.create_attempts = 1`
- persist `review.state = CREATE_IN_FLIGHT`

Then perform at most ONE `POST /posts`. No application retry loop, no HTTP retry
loop for POST, no second POST after timeout, connection reset, malformed
response, 5xx, or ambiguous result.

## 7. REVIEW_UNKNOWN ambiguity rule

If the draft-create request may have reached Zernio but the result cannot be
proven:

- persist `review.state = REVIEW_UNKNOWN`
- keep `review.create_attempts = 1`
- never auto-retry

Examples: timeout after request submission, connection loss after body send,
malformed success response, response lacks usable post id, uncertain 5xx
semantics, create says success but readback cannot prove exact draft.

Upstream Story/main semantics continue mapping this to `REVIEW_DRAFT_AMBIGUOUS`.

## 8. Readback after create

After a successful create response containing a post ID, perform exactly one
readback: `GET /v1/posts/{postId}`.

Require exact documented evidence that:

- returned post ID is the created ID
- status is draft
- Instagram target is present
- canonical account ID matches
- Story platformSpecificData matches when format = STORY
- payload/media identity is consistent to the extent the API exposes it

Only after successful readback persist:

- `review.state = DRAFT_CREATED`
- `review.zernio_draft_id = exact post ID`
- `review.created_at = now_iso()`

## 9. Deployment status

**DESIRED / NOT DEPLOYED**.

Exact live secret provisioning and read-only/controlled live proof are
deferred to a NEW #37 preflight / controlled deployment boundary. This task is
repository implementation only.

## 10. Sep 7/8 incidents

Sep 7/8 incidents are old-production baseline evidence, not failures of the
undeployed replacement.