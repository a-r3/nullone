# NullOne Repository Workflow

## Branches

- `main` — deployable known-good baseline
- `feature/*` — new development
- `fix/*` — bug fixes

## Change flow

1. Create a feature/fix branch.
2. Make changes outside production.
3. Run local validation.
4. Push the branch.
5. Open a pull request to `main`.
6. Require CI to pass.
7. Review the diff.
8. Merge only after approval.
9. Production deployment remains a separate controlled action.

## Safety boundary

A GitHub merge does not automatically deploy to:

`~/.openclaw/workspace`

During the active reliability proof, repository work must not modify
OpenClaw production state, automations, Zernio, Telegram approval flow,
publication ledgers, manifests, or proof state.
