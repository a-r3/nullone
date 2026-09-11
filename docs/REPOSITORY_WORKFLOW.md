# NullOne Repository Workflow

Authoritative lifecycle: `docs/contracts/change-control.md`.
Deterministic workspace deployment implementation (NOT activated):
`docs/deployment/release-cli.md`.

## Branches

- `main` — deployable known-good baseline
- `feature/*` — new development
- `fix/*` — bug fixes

## Change flow

1. Create a feature/fix branch.
2. Make changes outside production.
3. Run local validation.
4. Push the branch.
5. Open a pull request to `main` (see `.github/pull_request_template/`).
6. Require exact-head CI (`NullOne CI`) to pass.
7. Record an exact-head human review receipt (see change-control contract).
8. Merge only after approval. A new head SHA after review invalidates it.
9. Production deployment remains a separate controlled action with its own
   preflight, deployment change record, and post-deploy validation.

## Safety boundary

A GitHub merge does not automatically deploy to:

`~/.openclaw/workspace`

MERGED != DEPLOYED. DEPLOYED != HEALTHY.
SCHEDULER_SUCCESS != DOMAIN_SUCCESS.
HUMAN PR REVIEW != CONTENT PUBLICATION APPROVAL (the two-stage
Telegram/editorial flow stays separate).

## Current limitation

Native branch protection / required external reviewer enforcement may not
be available or appropriate for the current one-person repository. This
workflow documents manual control and does NOT claim technical enforcement
where only documented/manual control exists.

During the active reliability proof, repository work must not modify
OpenClaw production state, automations, Zernio, Telegram approval flow,
publication ledgers, manifests, or proof state.
