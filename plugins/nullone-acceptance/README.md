# nullone-acceptance — OpenClaw plugin source (#129)

Repository source ONLY. Git merge does NOT install, enable, or configure
this plugin. Install/enable/Gateway-restart happens exclusively at the
controlled #37 deployment (see `docs/deployment/129-acceptance-gateway-action.md`).

## What it does

Registers ONE OpenClaw 2026.8.2 interactive handler:

- channel: `telegram`
- namespace: `texbrif`

`texbrif:accept:<ACCEPTANCE_ID>` callbacks take the deterministic route:
sender check via ingress `ctx.auth.isAuthorizedSender`, then a one-shot
spawn of the reviewed Python action core
(`workspace/social/ops/scripts/nullone_acceptance_action.py handle
--acceptance-id <id>`) with a fixed argv array (no shell) and the
inherited Gateway process environment. Every other `texbrif:*` callback
(including approve/reject/revise/back/publish) returns `handled:false`,
preserving existing flows byte-for-byte.

Malformed accept-shaped callbacks are consumed safely (`handled:true`,
zero side effects, zero agent turn).

## Why a one-shot child, not a daemon + HMAC pipe

The `nullone-final-publish` HMAC pipe exists to deliver the publish
SECRET without touching argv/env. This action carries NO secret:
credentials stay in the inherited Gateway environment (the only runtime
that holds them) and never enter argv/files/logs. The Python action core
enforces the exact `{"acceptance_id": ...}` schema, derives the manifest
path internally, holds the single-flight lock, appends the non-secret
audit line, and returns only non-secret fields.

## Authorization boundary

Invokers are exactly the already-authorized sender(s) of the existing
Telegram channel binding, as computed by Gateway ingress
(`auth.isAuthorizedSender`) and re-checked in the handler before
anything else. No new auth layer is invented. The action is unreachable
from any other channel, namespace, or remote surface; `admin-http-rpc`
stays disabled and plays no role.

## Trust boundary recap

- The handler never touches Zernio, credentials, or manifests directly;
  it forwards one edge-validated acceptance ID to the reviewed core.
- The Python core never imports approval/reject/revise/scheduler/
  publisher/Instagram modules; the path terminates at Zernio draft ->
  Telegram preview -> human approval pending.
- Same-UID memory scraping, reviewed-code modification, process
  signaling, and root are out of scope (they defeat every host control
  equally).

## Layout

- `openclaw.plugin.json` — native plugin manifest. No `secretInputs`:
  this action introduces no new credential.
- `package.json` — package metadata (private, unlicensed reuse scope).
- `route.js` — dependency-free routing. Unit-tested offline
  (`node --test tests/js/test_acceptance_route.js` via
  `tests/test_acceptance_plugin_routing.py`).
- `index.js` — plugin entry: deterministic controller-path resolution,
  fixed-argv one-shot spawn with inherited Gateway env, authorized-sender
  check, fixed user replies, no `submitText` (zero LLM involvement).
