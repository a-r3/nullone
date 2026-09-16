# Deployment note: draft-bridge Gateway action (#142)

Repository source only. Nothing here takes effect by Git merge.

## Triggers (both deterministic, both audited)

1. **Automatic (reachability wiring):** the Draft Factory wrapper
   (`nullone-draft-factory-run.py execute`) runs
   `ensure_pending_bridge(max_creations=1)` after every editorial
   cycle: at most one same-day pending FEED/CAROUSEL manifest is
   completed through the action core (single-flight, audit,
   replay-safe). STORY, prior-day, consumed, and invalid manifests are
   never touched. In the Gateway cron context this carries the drafts
   credential; elsewhere it fails closed with zero calls.
2. **Operator-initiated:** `texbrif:draft:<MANIFEST_ID>` via the
   `plugins/nullone-draft-bridge/` handler (existing ingress
   authorization; enablement under #37).

## What this change adds

- `workspace/social/ops/scripts/nullone_draft_bridge_action.py` — the
  narrow action core (`nullone.draft-bridge.run {"manifest_id": ...}`).
  Deploys with the normal script sync (release-policy
  `workspace/social/ops/scripts/` mapping, no restart required for the
  file itself).
- `plugins/nullone-draft-bridge/` — the Gateway plugin hosting the
  `texbrif:draft:<MANIFEST_ID>` trigger. Takes effect ONLY after a
  controlled #37 deployment window:
  1. copy/sync the plugin directory to the production plugin path,
  2. enable it in Gateway plugin config (no new secrets required --
     the drafts bearer flows via the inherited Gateway process
     environment, the runtime source the bridge CLI already reads),
  3. restart the Gateway so the loader registers the new entry.

## Explicitly not in this change

No production mutation, no Gateway restart, no force-run, no schedule
change, no credential provisioning or rotation. The expired Zernio MCP
OAuth token observed 2026-09-16 is a separate auth path (agent MCP
tools) and is untouched by this change.

## Rollback

Revert the branch; remove the plugin directory from the production
plugin path and restart the Gateway in the next #37 window. The Python
action core without a registered plugin trigger is inert. No state
migration exists to undo (locks/audit are runtime artifacts only).
