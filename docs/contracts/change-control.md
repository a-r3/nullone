# NullOne Change-Control Contract

Status: authoritative repository governance. Repository-only; no production
effect. Complements (never duplicates) the deterministic workspace
deployment implementation in `docs/deployment/release-cli.md` (PR #102,
NOT activated).

## Authoritative lifecycle

```text
ISSUE / CHANGE INTENT
→ FEATURE/FIX BRANCH
→ PR
→ EXACT-HEAD CI
→ HUMAN REVIEW RECEIPT (exact head SHA)
→ MERGE
→ SEPARATE DEPLOYMENT DECISION
→ PREFLIGHT
→ CONTROLLED DEPLOYMENT
→ POST-DEPLOY VALIDATION
→ COMPLETE / ROLLED_BACK / CHECK_REQUIRED
```

Every arrow is a separate, auditable step. Skipping a step is a contract
violation, not a shortcut.

## Load-bearing distinctions

- **MERGED != DEPLOYED.** A GitHub merge changes the repository only. It
  never deploys, activates, restarts, or otherwise touches production.
- **DEPLOYED != HEALTHY.** A completed file sync proves delivery, not
  operational health. Health is proven only by post-deploy validation and
  natural live behavior.
- **SCHEDULER_SUCCESS != DOMAIN_SUCCESS.** A scheduler reporting `ok` /
  `succeeded` says nothing about the business outcome; domain truth comes
  from validated run-outcome records.
- **HUMAN PR REVIEW != CONTENT PUBLICATION APPROVAL.** Reviewing code is
  engineering governance. Authorizing a publication is the separate
  two-stage Telegram/editorial approval flow. One is never a substitute
  for the other.

There is no automatic background deployment. No merge, timer, bot, or
background job may deploy production.

## Human review receipt

Every PR that changes production-relevant code, prompts, agents, plugins,
policy, or contracts requires an exact-head human review receipt recorded
in the PR conversation or PR body:

| Field | Meaning |
|---|---|
| `reviewer` | Human reviewer identity (operator). |
| `reviewed_head_sha` | Exact full 40-hex commit SHA reviewed. |
| `reviewed_at` | UTC timestamp of the review. |
| `decision` | `APPROVE` or `REQUEST_CHANGES`. |
| `scope` | What was reviewed (files/areas, non-goals). |
| `ci_evidence` | Exact-head CI reference (workflow, run, conclusion). |
| `risk_notes` | Residual risks and why they are acceptable. |
| `deployment_required` | `YES` or `NO`, explicit. Always explicit. |

Rules:

- The receipt binds to the EXACT head SHA. If the PR head changes after
  review, the previous approval is stale and a new exact-head receipt is
  required before merge.
- This is a one-person repository: native GitHub self-approval may be
  unavailable, and this contract does NOT pretend it exists. An explicit
  operator/manual sign-off in the PR conversation or PR body is the valid
  receipt — but it must still bind to the exact head SHA.
- Agent-generated review is advisory only. Rauf/operator approval remains
  the human authority. An agent must never approve on behalf of the human.

Example receipt block (copy into the PR body or a review comment):

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

## Deployment change record

Every merge with `deployment_required: YES` gets a deployment change
record; merges with `deployment_required: NO` record that explicitly and
stop there. Safe metadata only:

| Field | Meaning |
|---|---|
| `change_id` | Stable identifier (e.g. date + short subject). |
| `source_pr` | PR number that produced the change. |
| `reviewed_sha` | Exact head SHA the human review receipt bound to. |
| `target_sha` | Exact commit SHA deployed (full SHA, never short). |
| `deployment_required` | `YES` or `NO`, explicit. |
| `deployment_surface` | What is deployed (V1: production workspace only). |
| `preflight_result` | Preflight verdict and exact deltas. |
| `deployed_at` | UTC timestamp of the deployment action. |
| `deployed_by` | Human operator identity. |
| `backup_or_rollback_reference` | Backup ID / rollback pointer. |
| `post_deploy_validation` | Validation performed and its result. |
| `result` | `SUCCESS` \| `ROLLED_BACK` \| `BLOCKED` \| `CHECK_REQUIRED`. |

For releases involving external controlled components (e.g. Gateway
plugins, agent runtimes), the record must state that they require their
own separate controlled deployment — the workspace tool never handles them.

Never include in any record: secrets, tokens, credentials, mutable
production content, Telegram owner IDs, presigned URLs, or session data.

## What PR #102 already solves (not duplicated here)

- Deterministic workspace file sync from an exact reviewed commit.
- Exact-SHA `NullOne CI` proof via the release gate.
- Deploy-state/history records for workspace deployments.

What PR #102 does NOT solve (owned by this contract):

- Human review receipts and stale-review invalidation.
- The merge → separate-deployment decision and its record.
- Preflight/validation governance and post-deploy health proof.
- Issue/change-intent linkage and acceptance evidence.

## Current limitation (explicit, not enforced)

Native branch protection and required-external-reviewer enforcement may
not be available or appropriate for the current one-person repository.
This contract documents manual control; it does NOT claim technical
enforcement where only documented/manual control exists. Repository
settings and rulesets are out of scope for this change.
