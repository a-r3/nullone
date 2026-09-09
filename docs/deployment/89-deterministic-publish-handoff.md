# 89 — Deterministic final publish handoff (implementation record)

Status: **IMPLEMENTED IN PR — NOT DEPLOYED**. Repository implementation only:
no plugin installed, no Gateway restart, no OpenClaw config/job change, no
Telegram send, no live publisher invocation, no Zernio call, no secret
provisioned, no production manifest/ledger touched. #37 not rerun.

## Architecture (V4, as implemented)

Human final callback (`texbrif:publish:<POST_ID>`, legacy `value` transport)
→ OpenClaw plugin handler (`plugins/nullone-final-publish`, channel
telegram, namespace `texbrif`; non-publish `texbrif:*` falls through with
`handled:false`) → HMAC-authenticated private pipe (per-boot memory-only key
K; argv carries nothing; raw Telegram ids never in argv/title/logs/receipts)
→ singleton controller daemon (`nullone_final_publish_controller.py`) →
per-human-authorization-instance receipt (`social/ops/publish-callback-receipts/
<POST_ID>/<uuid5>.json`, atomic, symlink-safe, digests only) →
`review_post_lock` held exactly once across claim → final authorization →
in-process bridge core (`nullone-publish-bridge.execute_loaded`; no
parent/child boundary before attempts=1) → receipt settlement from re-read
manifest truth (attempts>=1 always wins; never re-invoke) → deterministic
notifier best-effort (failure never retries publication).

Legacy `nullone-publisher-run.py execute <POST_ID>` is fail-closed (exit 2).
Approval agent keeps first-stage/button/reject/revise/back behavior and no
longer owns any `sessions_send` handoff (protocol retired). Publisher agent
is removed from the consequential path (legacy IDs retained; exec removal is
a #37 deployment hardening step).

## Crash semantics (implemented, offline-tested)

Absent→claim+invoke; RECEIVED/attempts-0→adopt; EXECUTING+flag-set+attempts-0
→adopt (in-process boundary proves no external call); EXECUTING+flag-cleared
+attempts-0→SETTLED_BLOCKED terminal (revoke is its sole clearer);
attempts>=1→settle-from-manifest, never invoke; CHECK_REQUIRED/UNKNOWN/
READBACK_FAILED→terminal, no retry; notifier failure→result-send only.
Boot reconciliation abandons stale pre-attempt instances (fresh message
required) and settles consumed ones from manifest truth.

## What this PR does NOT do (#90 owns it)

The bridge core's provider transport is still Claude/MCP
(`run_structured` → `posts_publish_now`). ZERO-LLM-after-click holds only
after #89 + #90 are both merged. #90 (OPEN) owns the deterministic REST
publisher connector (PUT promotion, remote-draft preflight, publish secret).

## Deployment (NOT performed; #37 design input)

1. Sync reviewed scripts to the production workspace; provision NOTHING new
   secret-wise for #89 (no new credential; K is per-boot memory-only).
2. Install plugin package to the Gateway plugin dir, enable it, restart the
   Gateway; verify handler READY (daemon handshake) before any callback can
   route; confirm non-publish callbacks still reach agents.
3. Cut over prompts (already in Git): approval/publisher de-escalated files
   replace live ones as part of the file sync.
4. Deployment hardening: remove publisher-agent `exec` grant.
5. Health: natural second-stage callbacks observed; receipts inspected;
   scheduler≠domain truth re-verified.

## Rollback (design only)

- SAFE FAIL-CLOSED: disable the plugin/path → final publish unavailable;
  nothing can publish accidentally. Preferred default.
- FULL FUNCTIONAL: restore the exact reviewed pre-#89 bundle (plugin state,
  controller/wrapper version, prompts, tool grants) as ONE controlled action.
  Never a partial old/new mix: with raw execute fail-closed in the new code,
  a half-rolled-back state would wedge publication shut (safe) — still
  forbidden as a planned state; do it atomically.

## Offline proof

New suites: `test_publish_ipc` (19), `test_publish_receipt` (14),
`test_final_publish_controller` (27), `test_plugin_routing` (1 harness → 9
Node assertions); updated `test_story_supersession` (7, now on the controller
path) and `test_approval_publication_instruction_safety` (20, new #89
invariants). Full `python3 tests/run_offline.py` → OFFLINE_REGRESSION_SUITE=PASS.
