# NullOne Draft Bridge Action (`nullone-draft-bridge`)

Narrow credentialed Gateway action for issue #142: exactly one fixed
operation, `nullone.draft-bridge.run {"manifest_id": ...}`, which runs
the reviewed `nullone-draft-bridge.py execute` for one authoritative
production manifest inside the Gateway child context that already holds
the Zernio drafts credential.

## What it does

- Claims `texbrif:draft:<MANIFEST_ID>` Telegram callbacks (existing
  ingress authorization re-checked; all other `texbrif:*` fall through
  byte-identical).
- Spawns ONE fixed-argv child: `python3 nullone_draft_bridge_action.py
  handle --manifest-id <id>` (no shell, inherited env only).
- The Python core enforces: exact request schema, internal manifest path
  derivation, preconditions (validates, attempts == 0, NOT_CREATED, no
  draft id), single-flight same-ID lock, allowlist-shaped results, audit
  JSONL. Replay after creation returns the existing draft id with zero
  external calls.

## What it cannot do

No Telegram send, no approval, no second confirmation, no scheduling, no
publication, no model calls, no general exec, no secret return/logging.
Human approval and Confirm Publish remain mandatory downstream.

## Deployment

Repository source only. Install/enable/Gateway-restart happens at a
controlled #37 deployment window, never by Git merge. No restart, no
production mutation, no force-run in the implementing change.
