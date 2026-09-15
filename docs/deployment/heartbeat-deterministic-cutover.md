# Deterministic heartbeat cutover (issue #131) — deployment note

No Gateway change is applied by merging the heartbeat PR. This note is the
reviewable record of the exact later production mutation.

## Current production (read-only evidence, 2026-09-15)

- Job: `heartbeat-main` (`22802b30-0cb2-448c-ac22-942520f57290`, every 2h)
- `lastRunStatus: error`, `consecutiveErrors: 27`
- Failure chain: model-backed agent runner → provider chain dead
  (subscription access disabled → fallback billing/cooldown) →
  `heartbeat failed: agent-runner-failure`

## New repo artifact (this PR, no production effect until cutover)

- `workspace/social/ops/scripts/nullone_heartbeat.py` — stdlib-only,
  zero model calls, no publication, no editorial mutation. Emits
  `nullone.heartbeat.v1` JSON; exit 0 HEALTHY / 1 DEGRADED / 2 FAIL.
- Offline: `python3 workspace/social/ops/scripts/nullone_heartbeat.py
  self-test` and `tests/test_deterministic_heartbeat.py`.

## Exact later deployment mutation (separate authorization required)

1. Snapshot the live job first (read-only, Gateway host):
   `openclaw cron get 22802b30-0cb2-448c-ac22-942520f57290 --json`
2. Replace ONLY the job payload/runner with the deterministic command:
   `python3 social/ops/scripts/nullone_heartbeat.py heartbeat`
   (workspace-relative; no prompt, no model fields, no secret changes).
   Keep the id, schedule (every 2h), and failure-alert wiring unchanged.
3. Verify over two ticks: structured HEALTHY result, `consecutiveErrors`
   reset, no agent-runner-failure, no model-token consumption.
4. Optional hardening later: pass `--required-path` entries for domain
   artifacts and inject the read-only connector probe once approved.

## Rollback

Restore the snapshotted payload. No repo revert is needed for rollback
(the script is inert until the cron payload references it).

PRODUCTION_CHANGED=NO by this PR. GATEWAY_RESTARTED=NO.
