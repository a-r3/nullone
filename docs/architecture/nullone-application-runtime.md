# NullOne Application Runtime and replaceable infrastructure adapters

Status: proposed architecture contract under issue #65 and PR #64.

## Decision

> NullOne owns workflow/application orchestration. OpenClaw is one
> replaceable infrastructure adapter that triggers NullOne; it is not the
> owner of NullOne business workflow semantics.

This is an M0 deployability requirement. It is not optional future cleanup.

OpenClaw independence was already a product and architecture intent. The
accepted #27–#36 components reflect it through deterministic cores,
fail-closed state machines and injected dependencies. The missing first-class
contract was the NullOne-owned application/runtime layer that composes those
components into real workflows. The 2026-09-07 #37 read-only preflight exposed
that omission.

## Layers and dependency direction

Dependencies point inward:

```text
Infrastructure adapters → NullOne Application Runtime → Domain/Core
```

### Domain/Core

The existing authoritative components remain unchanged:

- #27 run outcomes and health;
- #31/#32 cadence contract and controller;
- #33 Story core and supersession safety;
- #34/#35 breaking policy and identity;
- #36 routing, durable draft-set dispatch and main review pipeline;
- verification and publication-safety contracts.

Domain/Core must not import OpenClaw internals, depend on an OpenClaw data
model, shell out to `openclaw`, or embed cron/job syntax.

### NullOne Application Runtime

This layer owns what one workflow run means and in what order accepted domain
components and provider ports are called:

- `MorningWorkflow` — normalized invocation → #28 runtime → exact #27 result
  → #30 domain-notification decision;
- `AnalyticsWorkflow` — normalized invocation → #29 runtime/provider → exact
  #27 result → #30 domain-notification decision;
- `StoryWorkflow` — normalized invocation/editorial trigger → authoritative
  state → #32 cadence → candidate selection → #33 Story core → draft and
  review delivery;
- `BreakingWorkflow` — normalized invocation + verified Radar candidate →
  #35 identity → #36 router → #36 dispatcher → `StoryWorkflow` / main review
  pipeline → draft and review delivery.

Application workflows depend on ports and domain types. They do not import
adapter implementations.

### Infrastructure adapters

Current replaceable adapters are:

- OpenClaw scheduler/trigger adapter;
- Zernio draft adapter;
- Telegram review-delivery adapter;
- Zernio analytics adapter;
- filesystem state adapter;
- systemd/environment secret adapter.

A provider adapter may use provider-specific APIs, schemas, environment names
or CLI commands. Those details stop at the adapter boundary. For example, a
Telegram adapter may internally invoke `openclaw message send`; neither
`StoryWorkflow` nor `BreakingWorkflow` may know that.

## Scheduler invocation contract

The exact, fixture-validated version of this section is
`docs/contracts/scheduler-invocation-v1.md`, including the unambiguous
canonical-serialization rule for `occurrence_id` and worked adapter
examples. This section summarizes the same decision; the contract
document is authoritative on exact serialization/derivation detail.

Schema: `nullone.scheduler-invocation.v1`

Contract version: `1.0.0`

Required fields:

| Field | Meaning |
| --- | --- |
| `schema` | Literal `nullone.scheduler-invocation.v1`. |
| `contract_version` | Literal `1.0.0`. |
| `workflow_id` | Stable NullOne workflow identity. Current values are `morning-editorial`, `daily-analytics`, `story`, and `breaking`. |
| `source` | Stable scheduler-adapter namespace such as `openclaw`; not a business workflow identity. |
| `external_occurrence_id` | Opaque, non-empty occurrence identity supplied or deterministically mapped by the source adapter. No OpenClaw UUID shape is required by NullOne. |
| `scheduled_for` | Canonical UTC RFC 3339 time (`YYYY-MM-DDTHH:MM:SSZ`) for the logical scheduled occurrence. |
| `triggered_at` | Canonical UTC RFC 3339 time (`YYYY-MM-DDTHH:MM:SSZ`) at which this adapter invocation was observed. |
| `occurrence_id` | Deterministic NullOne-owned identity derived from the stable occurrence fields below. |

The adapter validates and normalizes the value before application logic. It
must fail closed when it cannot establish an exact stable occurrence; it must
not substitute the current time or an unscoped provider UUID.

### Stable occurrence identity

The proposed canonical identity input is the JSON array:

```json
[
  "nullone.scheduler-invocation.v1",
  "<workflow_id>",
  "<source>",
  "<external_occurrence_id>",
  "<canonical scheduled_for instant>"
]
```

`occurrence_id` is `occ_` plus the first 24 lowercase hexadecimal characters
of SHA-256 over the canonical UTF-8 JSON representation. `triggered_at` is
excluded because retries or delayed delivery of one scheduler occurrence must
not mint another logical NullOne occurrence. #27 continues to derive `run_id`
from the accepted `workflow_id` and normalized `occurrence_id`.

An adapter must verify a supplied `occurrence_id` by recomputing it. A mismatch
is invalid input, not a new run.

Example with placeholders only:

```json
{
  "schema": "nullone.scheduler-invocation.v1",
  "contract_version": "1.0.0",
  "workflow_id": "morning-editorial",
  "source": "openclaw",
  "external_occurrence_id": "<opaque-source-occurrence>",
  "scheduled_for": "2026-09-08T04:30:00Z",
  "triggered_at": "2026-09-08T04:30:02Z",
  "occurrence_id": "occ_<24-lowercase-hex>"
}
```

OpenClaw maps its exact scheduler receipt into this contract. A future systemd,
Temporal or cloud adapter maps its own exact occurrence into the same fields.
Changing adapters must not change the workflow ordering, #27 outcome meaning,
or the component safety contracts.

## Ports selected for M0

Only boundaries required by current integration work are selected:

- `SchedulerInvocation` — a validated value contract at the trigger edge; it
  is not an abstract scheduler framework.
- `ReviewDelivery` — delivers the already-built Story/main review payload and
  returns explicit `SENT` or fail-closed non-success. The current Telegram
  implementation may satisfy the existing `TelegramPreviewSender` protocols.
- `DraftProvider` — creates/reads a review draft under the existing one-attempt
  and ambiguity rules. The current implementation is Zernio-backed.
- `AnalyticsProvider` — exposes the read-only analytics capability consumed by
  `AnalyticsWorkflow`. The current implementation is the #29 Zernio adapter.
- `SecretProvider` — supplies secrets to infrastructure adapter construction.
  The current production implementation is systemd/environment-backed; the
  application workflow never knows `ZERNIO_ANALYTICS_API_TOKEN` or how it is
  installed.
- `StateStore`/filesystem boundary — used where an application workflow must
  obtain authoritative state. Existing deterministic filesystem adapters are
  retained; this contract does not abstract every file operation.
- `Clock` — injected only where application orchestration reads current time
  and deterministic tests require control.

Existing narrow writer and verifier protocols in #33/#36 remain in force.
This contract does not add duplicate global abstractions for them.

## Critical dependency rule

Application/runtime and Domain/Core modules must not:

- import OpenClaw internals;
- shell out to `openclaw` as part of workflow logic;
- derive business identity directly from an OpenClaw-specific model;
- embed cron/job syntax;
- embed Telegram or Zernio details except behind a provider port;
- require an OpenClaw-specific environment or filesystem layout as their core
  contract.

OpenClaw-specific parsing, CLI use, job payloads and native `failureAlert`
configuration belong in the OpenClaw adapter/deployment edge. Provider-specific
secret environment names belong in the secret/provider adapter factory.

## Testability and substitution

Every application workflow must run in offline tests with fake providers,
temporary state and an injected clock where needed. No OpenClaw process,
Gateway, cron job, Zernio service or Telegram connection is required for an
application workflow test.

Adapter tests prove mapping and transport behavior separately. At minimum:

- OpenClaw receipt → `SchedulerInvocation` mapping is deterministic and
  rejects missing/ambiguous identity;
- a hypothetical alternate scheduler receipt can map to the same invocation
  contract without changes to application/domain code;
- provider failures remain typed at the port boundary;
- application/domain source scans reject OpenClaw imports, `openclaw` shell
  calls and embedded cron/provider details.

## Safety and authority preserved

- #27–#36 contracts remain authoritative and are not rewritten.
- Scheduler success never substitutes for domain success.
- Exact occurrence/run identity, consumed-attempt rules, locks, ambiguity and
  no-auto-retry behavior remain unchanged.
- `ReviewDelivery` success requires the existing exact `SENT` proof.
- Draft creation remains review-only and human approval remains mandatory.
- Defining or invoking an application workflow grants no publication,
  approval, scheduling or callback capability.

## Relationship to future capability architecture (#13)

#65 is the current M0 minimum: only the Domain/Core, NullOne Application
Runtime, and the adapters/ports #59/#61/#62/#63 actually need today. #13 is a
separate, later M3 contract that may generalize model/runtime capability
taxonomy, connector versioning, scopes/permissions, idempotency evidence, and
provider reconciliation across future connectors.

#13 extends/generalizes the accepted #65 runtime/adapter boundary; it does not
redefine, contradict, or block it. Any change to #65's boundary that #13 later
requires needs an explicit reviewed migration/ADR, and provider-specific
semantics must not leak back into NullOne Application Runtime or Domain/Core.
#13 is not a dependency of #65 or of #59/#61/#62/#63, and no universal
provider/plugin abstraction is introduced in #65 merely to anticipate it.

## M0 dependency application

- #65 owns review of this architecture contract.
- #60 can proceed independently because it is deterministic state
  compatibility work.
- #59 implements `MorningWorkflow`, `AnalyticsWorkflow`, and the OpenClaw
  scheduler adapter after #65.
- #61 implements the analytics secret/runtime adapter boundary after #65.
- #62 implements `StoryWorkflow`, `DraftProvider`, and shared
  `ReviewDelivery` after #65 + #60.
- #63 implements `BreakingWorkflow` after #65 + #62 and indirectly #60.
- #66 removes the reachable legacy direct-Zernio-call instructions in
  `agents/approval/AGENTS.md`/`agents/publisher/AGENTS.md` (the narrow #6
  subset that remains reachable after this integration); it blocks #37 and is
  independent of #59/#60/#61/#62/#63.
- #37 repeats its read-only preflight only after #65, #59, #61, #62, #63, and
  #66 are accepted through normal review.
