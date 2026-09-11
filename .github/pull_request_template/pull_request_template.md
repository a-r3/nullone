## What changed

Describe the change.

## Why

Explain why this change is needed.

## Issue / intent

Linked issue (e.g. `Closes #<n>`) or change intent. If there is no issue,
state the intent here in one paragraph.

## Change type

- [ ] Repository governance / docs only
- [ ] Runtime/scripts/prompts/agents/plugins/policy change
- [ ] Tooling/CI change

## Production impact

- [ ] None (governance/docs/tests only)
- [ ] Production-relevant (scripts, prompts, agents, plugins, policy, contracts)

## Deployment required? YES/NO

- [ ] NO — merge only, no deployment follows
- [ ] YES — separate controlled deployment follows (MERGED != DEPLOYED)

## Exact head SHA

Full 40-hex SHA reviewed: `<sha>`

## CI evidence

NullOne CI run/conclusion for the exact head SHA.

## Risk / rollback notes

Residual risks and rollback pointer (backup ID, revert plan, or N/A).

## Human review receipt

```text
REVIEW_RECEIPT
reviewer: <operator>
reviewed_head_sha: <full 40-hex sha>
reviewed_at: <UTC timestamp>
decision: APPROVE
scope: <files/areas reviewed; non-goals>
ci_evidence: NullOne CI <run/url> <conclusion> for <sha>
risk_notes: <residual risks>
deployment_required: YES|NO
```

One-person rule: native self-approval may be unavailable; an explicit
operator sign-off here binds to the exact head SHA. A new head SHA after
review invalidates the receipt.

## Post-merge deployment status

- [ ] Not required (`deployment_required: NO`)
- [ ] Pending (record PR/SHA, deployment not yet performed)
- [ ] Deployed (link deployment change record)
- [ ] Rolled back / blocked (link record)

## Safety impact

- [ ] No secrets/runtime state added
- [ ] Two-stage publication approval preserved
- [ ] No new blind publication path
- [ ] Public branding remains NullOne
- [ ] Production scripts compile
- [ ] Relevant offline tests pass

## Deployment

- [ ] No automatic production deployment
- [ ] Controlled deploy required after merge
