# NullOne Executable Acceptance Contracts V1

Status: PROPOSED / issue #4  
Scope: publication safety + workflow completion semantics  
Production impact: none. This document and its fixtures do not upgrade a safety claim by themselves.

## Contract vocabulary

Each contract has a stable ID.

**Enforcement**
- `ENFORCED_TODAY` — a deterministic code path currently rejects/guards the negative case.
- `MISSING` — the required guarantee is not deterministically enforced by the inspected code path.

**Proof**
- `EXERCISED` — real or offline evidence has exercised the relevant behavior.
- `NOT_EXERCISED` — implementation/contract may exist, but this exact scenario has not yet been demonstrated.

`ENFORCED_TODAY` does **not** mean provider-side exactly-once delivery. NullOne may claim only the local guarantee actually enforced: a consequential publication authorization consumes at most one local publication attempt, and ambiguous outcomes are not retried automatically.

The machine-readable source for this matrix is:

`tests/fixtures/acceptance_contracts.json`

CI validates the schema, positive/negative scenarios, traceability anchors, and fixture hygiene.

## Publication contracts

| ID | Invariant | Enforcement | Current proof | Observable result | Owner |
|---|---|---|---|---|---|
| `PUB-VERIFY-001` | Publication manifest requires `verification == PASS`. | ENFORCED_TODAY | EXERCISED | non-PASS manifest is blocked before bridge use | Production Bridge |
| `PUB-BIND-001` | Caption/media approved by the manifest remain hash-bound and dimension-bound. | ENFORCED_TODAY | EXERCISED | modified caption/media fails manifest validation | Production Bridge |
| `PUB-AUTH-001` | Publish Bridge requires first-stage + final-publish flags and expected authorization metadata. | ENFORCED_TODAY | EXERCISED | missing/incorrect fields block final publication | Publish Bridge |
| `PUB-AUTH-PROVENANCE-001` | Authorization metadata must be derived from authenticated, exact callback identity/stage rather than asserted by the publisher wrapper. | MISSING | NOT_EXERCISED | forged/local wrapper metadata cannot create publish authority | Approval/Core authorization |
| `PUB-CALLBACK-001` | stale/replayed/wrong-sender/wrong-chat/wrong-message/wrong-stage callbacks must be rejected deterministically. | MISSING | NOT_EXERCISED | invalid callback produces no authorization transition | Approval/Core authorization |
| `PUB-IDEMP-001` | A live publication attempt counter is consumed durably before the one consequential provider call. | ENFORCED_TODAY | EXERCISED | second local attempt is blocked when `attempts != 0` | Publish Bridge |
| `PUB-MODEL-REPEAT-001` | One authorized wrapper invocation must deterministically prevent a second live publish tool call inside the same model session. | MISSING | NOT_EXERCISED | second `posts_publish_now` call in the same session is rejected | Publish Bridge |
| `PUB-UNKNOWN-001` | timeout/malformed/ambiguous publish result becomes unsafe-to-repeat and is never auto-retried. | ENFORCED_TODAY | EXERCISED | state is `UNKNOWN`/equivalent and retry is forbidden | Publish Bridge |
| `PUB-READBACK-001` | accepted publish + inconclusive readback cannot cause another live publish attempt. | ENFORCED_TODAY | EXERCISED | readback failure remains unsafe-to-repeat | Publish Bridge |
| `PUB-NOTIFY-001` | publication-result notification is a separate side effect and its failure cannot trigger publication retry. | ENFORCED_TODAY | EXERCISED | notification consumes its own attempt; publication attempt remains unchanged | Publish Notifier |
| `PUB-DRAFT-001` | review transport is draft-only and a possibly-consumed create attempt cannot be blindly repeated. | ENFORCED_TODAY | EXERCISED | ambiguous review create becomes `REVIEW_UNKNOWN`; no second automatic create | Draft Bridge |

## Workflow completion contracts

| ID | Invariant | Enforcement | Current proof | Observable result | Owner |
|---|---|---|---|---|---|
| `RUN-OUTCOME-001` | scheduler/process success is distinct from domain/business success. | MISSING | EXERCISED | `ok/succeeded` cannot represent a business-level `BLOCKED` result | Workflow Health |
| `RUN-ARTIFACT-001` | a job is domain-successful only when required artifacts exist, or it returns an explicit valid no-op/no-data outcome. | MISSING | EXERCISED | missing required artifact makes domain result non-success | Workflow Health |
| `RUN-REASON-001` | every non-success domain outcome has an explicit machine-readable reason. | MISSING | EXERCISED | `BLOCKED/FAILED/UNKNOWN` includes a stable reason/code | Workflow Health |
| `RUN-ID-001` | each scheduled execution exposes a stable run ID that can bind scheduler receipt, artifacts and domain outcome. | MISSING | NOT_EXERCISED | one run can be traced without relying on free-form model summary | Workflow Health |

The Sep 4–5 Daily Analytics observation is the canonical current counterexample for `RUN-OUTCOME-001`: scheduler `ok/succeeded` with no required analytics artifacts and a business-level blocked summary is **not** a successful workflow completion.

## Known gap: forged wrapper metadata

`nullone-publisher-run.py` currently writes:

- `approval.first_stage = true`
- `approval.final_publish = true`
- `approval.source = texbrif-approval`
- `approval.operator = Rauf`
- `approval.human_confirmation = two_step`

before calling the Publish Bridge.

The Publish Bridge validates those fields, but the inspected deterministic code does not itself prove that the values originated from the correct Telegram sender/chat/message/callback stage, nor does it enforce replay/expiry identity here.

Therefore:

- `PUB-AUTH-001` is enforced as a **field/value guard**;
- `PUB-AUTH-PROVENANCE-001` and `PUB-CALLBACK-001` remain **MISSING**.

Do not upgrade these missing guarantees based on prompt text or trusted-agent assumptions.

## Known gap: model repeat call inside one wrapper invocation

`nullone-publish-bridge.py` durably sets `publication.attempts = 1` before entering the live `run_structured(...)` call. This correctly prevents a later wrapper invocation from becoming a second local publication attempt.

However, inside that single model session the inspected path currently relies on prompt text:

`Call posts_publish_now MAXIMUM ONE TIME.`

The allowed-tool boundary permits `mcp__zernio__posts_publish_now`, and the inspected code does not show a deterministic counter/hook that rejects a second call to that tool within the same `run_structured(...)` invocation.

Therefore `PUB-MODEL-REPEAT-001` remains **MISSING / NOT_EXERCISED** until a deterministic one-shot tool gate exists and has a negative test.

This gap is distinct from `PUB-IDEMP-001`.

## Exact-preview / immutable-content rule

Human approval is for the exact manifest version.

Positive scenario:
1. verification is PASS;
2. caption/media hashes match;
3. preview corresponds to that manifest;
4. first approval and final confirmation refer to that exact authorization object;
5. publication consumes the same immutable content/media version.

Negative scenario:
- caption or media changes after approval;
- old callback is replayed for a superseded manifest;
- wrong review post ID is supplied;
- authorization metadata is merely reasserted by an untrusted wrapper.

The negative case must block publication rather than silently re-bind approval.

## Draft-only transport rule

Draft creation is review transport, not publication authority.

The draft path must never:
- publish now;
- schedule;
- promote a review draft;
- create a second review object merely because the first create result was ambiguous.

`REVIEW_UNKNOWN` is unsafe to repeat automatically.

## Provider semantics

NullOne must **not** claim provider-side exactly-once publication.

Supported claim:

> For one immutable local authorization, NullOne allows at most one local consequential publication attempt. The attempt is durably consumed before the provider call. Ambiguous results are marked unsafe-to-repeat and require read-only reconciliation.

Unsupported claim:

> Zernio/Instagram will publish exactly once under all network/provider failure modes.

## Completion outcome model required by follow-up implementation

Issue #27 should implement an explicit domain result separate from scheduler status. Minimum semantic states:

- `SUCCEEDED`
- `BLOCKED`
- `FAILED`
- `UNKNOWN`

A successful scheduler process may carry any of these domain outcomes.

`SUCCEEDED` requires one of:
1. all required domain artifacts/effects are present and validated; or
2. an explicit contract-defined `NO_ACTION` / `NO_DATA` result where no artifact is required.

Free-form model text alone is not the authoritative completion signal.

## Fixture safety

Contract fixtures are synthetic. They must not contain:
- production account IDs;
- Telegram owner IDs;
- OAuth/API tokens;
- presigned URLs;
- real platform/post IDs;
- mutable production state.

Allowed URL domain in fixtures: `example.invalid`.

## Code ownership map

This is component ownership, not a GitHub CODEOWNERS file.

- Production Bridge: `workspace/social/ops/scripts/nullone_bridge_common.py`, `nullone-manifest.py`
- Draft Bridge: `workspace/social/ops/scripts/nullone-draft-bridge.py`
- Publish Bridge: `workspace/social/ops/scripts/nullone-publish-bridge.py`
- Publisher Wrapper: `workspace/social/ops/scripts/nullone-publisher-run.py`
- Publish Notifier: `workspace/social/ops/scripts/nullone-publish-notify.py`
- Approval/Core authorization: approval callback handler / future transactional authorization authority
- Workflow Health: follow-up issue #27

## Exit rule for issue #4

Issue #4 can close when:
1. stable contract IDs exist;
2. every invariant has positive and negative scenarios;
3. observable outcome and component owner are explicit;
4. current status is honestly classified as enforced/missing and exercised/not exercised;
5. executable fixture validation is in CI;
6. no production identifier/credential/state leaks into fixtures;
7. this contract does not claim unimplemented guarantees.

Closing #4 does **not** close #5, #27, or the active reliability proof.
