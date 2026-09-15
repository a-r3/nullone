# Acceptance Gateway action — controlled deployment notes (#129, stacked on #128)

No deployment happens by Git merge. No restart, force-run, schedule
change, or production mutation happens in the implementing PRs. This
document is the checklist for the LATER authorized #37 deployment window.

## What is deployed

1. PR #128: `workspace/social/ops/scripts/nullone_acceptance_run.py`
   (guarded acceptance operation) + `tests/test_acceptance_entrypoint.py`.
2. Follow-up (#129): `workspace/social/ops/scripts/nullone_acceptance_action.py`
   (narrow action core, `nullone.acceptance.run`) +
   `plugins/nullone-acceptance/` (Telegram `texbrif:accept` handler) +
   tests + this note.

Neither PR merges, installs, enables, or configures anything in
production by itself.

## Deployment sequence (authorized window only)

1. Deploy the reviewed file set; verify hashes/paths of:
   - `workspace/social/ops/scripts/nullone_acceptance_run.py`
   - `workspace/social/ops/scripts/nullone_acceptance_action.py`
   - `plugins/nullone-acceptance/{index,route}.js`
   - `plugins/nullone-acceptance/openclaw.plugin.json`
2. Enable the `nullone-acceptance` plugin in Gateway config. No
   `secretInputs`: this action introduces no new credential (it inherits
   the Gateway process environment, the only runtime holding the Zernio +
   Telegram credentials). Leave `admin-http-rpc` disabled.
3. Perform the controlled Gateway restart (same service/user/config:
   `openclaw-gateway.service`, user `oem`). New plugin registration
   loads only at boot (`onStartup:true`, synchronous `register()`).
4. Verify post-restart:
   - `openclaw plugins list` shows `nullone-acceptance` enabled;
   - the `texbrif` interactive handler is registered;
   - cron definitions are byte-for-byte unchanged (no schedule mutation);
   - approval/reject/revise/back/publish flows behave as before.
5. Execute the preserved acceptance ID exactly once via the authorized
   operator Telegram path (`texbrif:accept:<acceptance-id>`):
   - exactly one Zernio draft maximum;
   - exactly one Telegram preview maximum;
   - path terminates at human approval pending (no publish).
6. Confirm audit line in `social/ops/acceptance-action-audit.jsonl`
   (non-secret acceptance ID + result only) and the Telegram delivery
   receipt. A repeated invocation must replay with zero external calls.

## Rollback

Disable the `nullone-acceptance` plugin and restart. The acceptance
operation files are inert without the handler; cron jobs, publisher,
and approval paths are untouched by this change.
