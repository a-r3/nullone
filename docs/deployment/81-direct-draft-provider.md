# 81 — Direct Zernio DraftProvider

Status: **IMPLEMENTED & REVIEWED — NOT DEPLOYED**. This document describes the
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

Base URL: `https://zernio.com/api/v1`

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/accounts` | Read-only account preflight |
| POST | `/media/presign` | Request a presigned upload URL |
| PUT | `<presigned uploadUrl>` | Upload exact bytes (unauthenticated) |
| POST | `/tools/validate/media` | Validate media |
| POST | `/tools/validate/post` | Validate complete post payload |
| POST | `/posts` | Create exactly one draft |
| GET | `/posts/{postId}` | Readback the created draft |

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

(End of file - total 10 lines)