# 90 — Deterministic Zernio publisher connector (implementation record)

Status: **IMPLEMENTED IN PR — NOT DEPLOYED**. Repository implementation only:
no secret provisioned, no live Zernio call, no publication, no deployment,
no Gateway/plugin change, no Telegram send, no production manifest/ledger
touched. #37 not rerun. #89 remains closed.

## What changed

The #89 in-process publication core (`nullone-publish-bridge.execute_loaded`,
invoked under `review_post_lock` by the deterministic controller) no longer
issues its consequential write through `nullone_claude.run_structured` /
`claude -p` / Zernio MCP `posts_publish_now`. The consequential path is now:

human callback → #89 OpenClaw plugin → authenticated controller daemon →
publication bridge/core → `ZernioPublishProvider`
(`nullone_zernio_publish_adapter.py`) → exactly one HTTPS PUT
`/v1/posts/{reviewPostId}` `{isDraft: false, publishNow: true}` →
read-only readback GET → deterministic notifier.

ZERO model decisions after the final human click. `nullone_claude` is NOT
removed globally (unrelated workflows still use it); it is only removed
from publication.

## Transport contract (current Zernio OpenAPI 3.1.0 / 1.0.4)

- Base `https://zernio.com/api/v1`; exact host allowlist; HTTPS only.
- Allowlisted paths only: `GET /posts/<24-hex>` (preflight + readback),
  `PUT /posts/<24-hex>` (single promotion). Nothing else; no POST, no
  DELETE, no arbitrary path.
- Redirects are refused, never followed (Authorization can never leak).
- Finite timeout, bounded response body, bearer header only to the exact
  host, no automatic retry.
- Minimal PUT payload is exactly `{isDraft: false, publishNow: true}`;
  no caption/media resend, no scheduling fields, no `x-request-id` claim
  (documented for POST create only; PUT 409 = queue_slot_conflict).
- Read-only remote-draft preflight BEFORE `attempts=1`: same id, still
  draft, exact caption, exact ordered media (url/type), exactly one
  Instagram target, canonical account, STORY settings where exposed.
  Any contradiction → BLOCKED, attempts 0, zero PUT, fresh human
  confirmation required. The remote draft is never modified to match.
- `attempts=1` + `PUBLISH_IN_FLIGHT` persisted BEFORE the network write;
  exactly ONE PUT; second invocation issues zero PUT.
- PUT 200 → verify + readback; 207 → branch on code, post.status
  partial/failed/scheduled (scheduled = provider auto-retry,
  PUBLISHING-family), never PUBLISHED directly; 4xx + valid envelope
  (400/401/403/404/409) → terminal FAILED; timeout/network/5xx/
  malformed/unexpected → UNKNOWN, no retry.
- Readback clarifies truth (PUBLISHED/PUBLISHING/FAILED/CHECK_REQUIRED/
  READBACK_FAILED/UNKNOWN) but can never authorize another PUT. A
  definite rejection stands unless readback proves delivery.
- Metadata honesty: permalink copied ONLY from a documented
  `platformPostUrl` string when present; platform post id never
  fabricated.

## Secret boundary (protected store SecretRef + private pipe)

New logical credential `zernio.publish.bearer`, protected-store entry
`ZERNIO_PUBLISH_API_TOKEN`. The publication credential NEVER comes from
inherited environment state: `zernio.publish.bearer` is intentionally NOT
bound in `EnvironmentSecretProvider`, so a plain inherited variable alone
cannot satisfy publication auth (typed missing-secret, fail-closed before
any attempt).

Production flow:

1. The plugin manifest declares the managed SecretInput
   `plugins.entries.nullone-final-publish.config.publishToken`
   (expected string); production binds
   `{source:"store", provider:"default", id:"ZERNIO_PUBLISH_API_TOKEN"}`
   there (a #37 deployment action, never a Git merge, nothing provisioned
   here).
2. At activation the plugin reads the configured input from
   `api.pluginConfig.publishToken`: a host-materialized string is used
   directly; a `{source:"store",...}` ref is resolved through the
   supported SDK SecretInput surface only (never a manual store read,
   never the environment). Missing/blank/non-store input fails closed
   with zero spawn and zero publication.
3. The resolved token crosses to the controller daemon ONLY inside one
   bounded HMAC-authenticated startup frame on the existing private
   spawn pipe (after the per-boot key K; argv/env carry nothing).
4. The daemon refuses READY until a valid channel AND a valid non-empty
   credential are both established, wraps the credential as a
   memory-only SecretValue, and injects it into the provider factory
   through an InMemorySecretProvider -- the single secret source. The
   publisher adapter, bridge, factory, IPC, and controller never read
   the environment for publication auth.

Analytics/draft secret behavior is untouched.

## #89 preservation (all green offline)

Two-stage human approval, plugin-authenticated ingress, per-human
authorization instance, HMAC/private pipe, raw IDs absent from
argv/log/receipt, legacy raw execute fail-closed, one review_post_lock
owner, attempts max 1, no blind retry, Story supersession, truthful
Telegram result, pre/post-dispatch ambiguity wording, notifier failure
never retries publication. The controller, IPC, receipt, and plugin
modules are behaviorally unchanged.

## What this change does NOT do

No production activation (MERGED != DEPLOYED). No credential
provisioning. No live calls. #37 owns any deployment and is NOT rerun
here.
