# nullone-final-publish — OpenClaw plugin source (#89)

Repository source ONLY. Git merge does NOT install, enable, or configure
this plugin. Install/enable/Gateway-restart happens exclusively at the
controlled #37 deployment (see `docs/deployment/89-deterministic-publish-handoff.md`).

## What it does

Registers ONE OpenClaw 2026.8.2 interactive handler:

- channel: `telegram`
- namespace: `texbrif`

`texbrif:publish:<24-hex-POST_ID>` callbacks take the deterministic route:
sender check via ingress `ctx.auth.isAuthorizedSender`, canonical envelope +
HMAC-SHA256 over the private daemon pipe (per-boot memory-only key, argv
carries nothing), bounded safe user reply, `handled:true`, no `submitText`
(zero LLM involvement). Every other `texbrif:*` callback returns
`handled:false`, preserving existing approval/reject/revise/back agent flow.

Malformed publish-shaped or unauthorized callbacks are consumed safely
(`handled:true`, zero subprocess, zero canonical receipt).

## Layout

- `openclaw.plugin.json` — native plugin manifest (metadata only).
- `package.json` — package metadata (private, unlicensed reuse scope).
- `route.js` — dependency-free routing + canonical JSON. Unit-tested offline
  (`node --test tests/js/test_plugin_route.js` via `tests/test_plugin_routing.py`).
- `index.js` — plugin entry: daemon lifecycle (spawn at activation, per-boot
  key over the private spawn pipe, READY handshake with timeout, fail-closed
  on daemon loss with NO legacy-LLM fallback) and the handler above.

## Trust boundary recap

The handler never touches Zernio and never handles secrets. The per-boot key
K lives only in Gateway/daemon process memory. Direct binary invocation,
fabricated tuples, and publisher/main LLM direct exec all fail closed in the
Python controller before receipt, authorization, or attempt. Same-UID memory
scraping, reviewed-code modification, process signaling, and root are out of
scope (they defeat every host control equally) — see the V4 design report.
