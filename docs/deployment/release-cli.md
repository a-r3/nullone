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
reviewed `origin/main` SHA, proves `NullOne CI` success for that SHA, and
installs only the allowlisted files from that exact commit. Ordinary
update is forward-only (deployed SHA must be an ancestor of the target;
rollback is the only downgrade path), and `fetch origin/main` must
succeed — stale refs are never used for a real production update.

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
./ops/nullone bootstrap --production-root <path> --baseline <full-sha>
./ops/nullone rollback --production-root <path> [--backup <id>]
```

- `version` — reports the release-tool version (safe, read-only).
- `status` — reports one of `UP_TO_DATE`, `UPDATE_AVAILABLE`,
  `DRIFT_DETECTED`, `DEPLOY_STATE_MISSING`, `CHECK_REQUIRED`, `UNKNOWN`,
  with `DEPLOYED_SHA` and `AVAILABLE_SHA`. Hash equality alone is never
  reported as operational health. Missing deploy state reports
  `BOOTSTRAP_REQUIRED`.
- `preflight` — read-only: resolves the target SHA, loads the deployment
  policy from that exact commit, checks the `NullOne CI` gate, reports
  drift and a plan summary (`FILES_ADD/UPDATE/REMOVE`,
  `RESTART_REQUIRED`). Exits non-zero when CI is unproven, drift exists,
  state is missing, or the plan contains forbidden entries.
- `update` — the normal deployment path, allowed only with existing deploy
  state: inspect → deterministic plan → show exact target SHA, policy
  identity, and files → require explicit confirmation (`--yes`) → drift
  check → backup → staged validation → atomic install with hash proof →
  deploy-state update → history append. Missing state fails closed with
  `BOOTSTRAP_REQUIRED` (no silent first adoption). Any drift, CI failure,
  forbidden destination, traversal/symlink escape, backup gap, hash
  mismatch, or externally-controlled component change aborts before or
  during mutation (with restore).
- `bootstrap` — explicit ONE-TIME adoption of an already-existing
  production tree at an explicit `--baseline <full-sha>` (must exist, be
  on `origin/main`, and have proven `NullOne CI`). The policy comes from
  the baseline commit. Every managed file must match the baseline
  byte-for-byte; any difference, absence, or unsafe symlink blocks with
  `BOOTSTRAP_BLOCKED` and zero mutations. On exact match, only
  deploy-state metadata is written (plus a `bootstrap` history event);
  production files are never rewritten. Unknown files are preserved.
- `rollback` — restores exactly one release step: the selected backup's
  `target_sha` must equal the currently deployed SHA
  (`ROLLBACK_NOT_CURRENT_RELEASE` otherwise, zero mutations), drift must
  be clean, backup integrity is verified first, deploy-state is updated
  truthfully, history is appended. History is never erased.

## What is deployed (and what is not)

Source of truth: exact Git commit on `origin/main` in `a-r3/nullone`
(full SHA recorded; short SHAs rejected; non-main commits rejected;
production is never a `git pull` working tree).

Deployment authority is `ops/release-policy.json` **at the exact target
commit** (versioned; identity recorded as `POLICY_SHA256` in
deploy-state). The working-tree policy is never silent authority — only
an explicit developer `--policy` override. Mapping changes therefore
arrive only through reviewed `main` history. A target commit predating
the policy fails closed (`POLICY_NOT_FOUND_AT_TARGET`).

The policy maps repository-source → production-destination plus immutable
exclusions, and classifies externally-controlled components
(`plugins/nullone-final-publish/**`, `agents/**`) that V1 never deploys.
`docs/`, `tests/`, `NULLONE_PROJECT_CONTEXT.md`, and GitHub workflow files
are never deployed. Mutable production state
(`social/state/**`, `social/ops/manifests/**`, run outcomes, ledgers,
candidate queues, secrets, OAuth/session/auth, Telegram owner data,
presigned URLs, caches, backups, `deploy-state/`) is never overwritten.

Restart semantics are per-mapping: workspace mappings that are read fresh
on next invocation declare no restart; mappings without explicit metadata
default conservative (restart required). `plugins/nullone-final-publish/**`
is an externally-controlled component with restart semantics owned by the
Gateway activation process (the tool never restarts anything). The plan
reports `RESTART_REQUIRED=YES` when a changed workspace file needs it or
when a release range touches a restart-required external component.

Installed files receive their reviewed Git modes (0644/0755); install
failure restores prior bytes and modes; backups record modes and
rollback restores them.

Fail-closed paths are truly zero-mutation: the deployment lock is an
`flock()` on the production-root directory descriptor itself and creates
no state; update/bootstrap/rollback each run a fully read-only
eligibility phase first, and only after the exact-match preconditions
are re-proven under the lock is any deploy-state metadata committed. A
refused update or bootstrap leaves the production tree byte-identical,
including no new `deploy-state/` artifacts.

Repository identity is exact, not substring-based: only canonical
`github.com/a-r3/nullone` remote forms (HTTPS, scp-style SSH,
`ssh://`) are accepted; lookalike owners, repos, hosts, extra path
segments, and ports fail closed, and embedded credentials are stripped
from every error message.

Deploy metadata lives production-locally outside editorial state
(`deploy-state/current.json`, `history.jsonl`, `backups/`, lock and
transaction files). It stores SHAs, timestamps, tool/policy versions
(including `POLICY_SHA256`), file hashes, and modes only — enforced by a
structural safe-metadata validator on every write, never by
filename-substring matching. No file contents, no secrets.

The CI gate proves the **`NullOne CI` workflow itself** (exact repo,
exact head SHA, completed, success) via authenticated local `gh`
read-only API calls (`gh api --method GET .../actions/runs`, since
`gh api -f` would otherwise imply POST). Unrelated successful checks
never count as proof; missing/pending/failed/cancelled/ambiguous/
unreachable all fail closed. The live gate has no environment-variable
bypass: tests inject the CI adapter explicitly in Python only.

V1 performs **no Gateway restart**. Validation hooks are offline only
(`py_compile`, JSON parse, `node --check` where available). A deployment
lock prevents concurrent update/rollback/bootstrap, and an interrupted
transaction surfaces as `CHECK_REQUIRED`, never silent success.

## V1 scope: production workspace only

V1 `nullone update` safely updates the production workspace only — the
single proven filesystem root behind `--production-root`.

`plugins/nullone-final-publish/**` and `agents/**` are classified as
externally-controlled components (repository-only classification in the
policy): their live destinations (Gateway plugin dir, agent runtime) are
separate, unproven filesystem roots, so the tool never copies those files
anywhere and never guesses multi-root paths.

If a reviewed release range touches an external component, the plan
reports `EXTERNAL_COMPONENT_CHANGES` (count and paths) and ordinary
update fails closed with `CONTROLLED_COMPONENT_DEPLOY_REQUIRED` before
any mutation — never a partial workspace deploy. A target with only
workspace-managed changes proceeds normally (`EXTERNAL_COMPONENT_CHANGES=0`).

Future V2 may add reviewed multi-root deployment support after the actual
OpenClaw plugin/agent filesystem roots are explicitly modeled.

## Real-production authority

Against the explicitly unlocked real production root, developer authority
modifiers are forbidden: `--policy`, `--no-fetch`, `NULONE_NO_FETCH`, and
any CI mock/test selector fail closed with
`DEV_OVERRIDE_FORBIDDEN_IN_PRODUCTION`. The real path always fetches
`origin/main`, loads the policy from the exact target commit, and performs
real `NullOne CI` proof. Fixture/synthetic tests remain free to use
explicit test adapters and options.
