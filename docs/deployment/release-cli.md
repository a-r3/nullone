# NullOne deterministic release CLI (repository engineering only)

Status: **REPOSITORY TOOLING ONLY — NOT ACTIVATED**. This document describes
the intended future operator UX. No production deployment, Gateway restart,
or background automation is enabled by this change.

## Principle

```text
MERGE != DEPLOY
```

Merging to `main` never deploys. Production deployment happens only through
an explicit human-triggered `nullone update` run that resolves an exact
reviewed `origin/main` SHA, proves CI success for that SHA, and installs
only the allowlisted files from that exact commit.

There are **no automatic background updates**. `--yes` exists for future
controlled automation only; unattended deployment is never the default.

## Intended future UX

All commands run from the engineering checkout. Tests and this PR use
temporary fixture production roots only — never `~/.openclaw`.

```bash
./ops/nullone version
./ops/nullone status --production-root <path>
./ops/nullone preflight --production-root <path>
./ops/nullone update --production-root <path> [--target <full-sha>] [--yes]
./ops/nullone rollback --production-root <path> [--backup <id>]
```

- `version` — reports the release-tool version (safe, read-only).
- `status` — reports one of `UP_TO_DATE`, `UPDATE_AVAILABLE`,
  `DRIFT_DETECTED`, `DEPLOY_STATE_MISSING`, `CHECK_REQUIRED`, `UNKNOWN`,
  with `DEPLOYED_SHA` and `AVAILABLE_SHA`. Hash equality alone is never
  reported as operational health.
- `preflight` — read-only: resolves the target SHA, checks the CI gate,
  reports drift and a plan summary (`FILES_ADD/UPDATE/REMOVE`,
  `RESTART_REQUIRED`). Exits non-zero when CI is unproven, drift exists,
  or the plan contains forbidden entries.
- `update` — the normal deployment path: inspect → deterministic plan →
  show exact target SHA and files → require explicit confirmation (`--yes`)
  → drift check → backup → staged validation → atomic install with hash
  proof → deploy-state update → history append. Any drift, CI failure,
  forbidden destination, traversal/symlink escape, backup gap, or hash
  mismatch aborts before or during mutation (with restore).
- `rollback` — restores exactly one known previous successful release
  backup (explicit `--backup` selection when ambiguous), verifies backup
  integrity first, updates deploy-state truthfully, appends history.
  History is never erased.

## What is deployed (and what is not)

Source of truth: exact Git commit on `origin/main` in `a-r3/nullone`
(full SHA recorded; short SHAs rejected; non-main commits rejected;
production is never a `git pull` working tree).

`ops/release-policy.json` (versioned) is the only allowlist:
repository-source → production-destination mappings plus immutable
exclusions. `docs/`, `tests/`, `NULLONE_PROJECT_CONTEXT.md`, and GitHub
workflow files are never deployed. Mutable production state
(`social/state/**`, `social/ops/manifests/**`, run outcomes, ledgers,
candidate queues, secrets, OAuth/session/auth, Telegram owner data,
presigned URLs, caches, backups, `deploy-state/`) is never overwritten.

Deploy metadata lives production-locally outside editorial state
(`deploy-state/current.json`, `history.jsonl`, `backups/`, lock and
transaction files). It stores SHAs, timestamps, tool/policy versions,
and file hashes only — never secrets.

V1 declares `restart_required: false` and performs **no Gateway restart**.
Validation hooks are offline only (`py_compile`, JSON parse,
`node --check` where available). A deployment lock prevents concurrent
update/rollback, and an interrupted transaction surfaces as
`CHECK_REQUIRED`, never silent success.
