# Contributing

## Branch model

- `main` — deployable known-good state
- `feature/*` — new functionality
- `fix/*` — bug fixes

## Required flow

1. Create branch
2. Make changes
3. Run local validation
4. Open PR
5. CI must pass
6. Review diff
7. Merge
8. Controlled deployment separately

Never edit production directly for normal development work.
