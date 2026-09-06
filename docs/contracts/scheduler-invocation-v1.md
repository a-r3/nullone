# NullOne Scheduler Invocation Contract V1

Status: PROPOSED / issue #65
Scope: scheduler/trigger-adapter normalization only.
Production impact: none. This document defines a decision contract for the
dependent integration issues (#59/#61/#62/#63) to implement. Writing this
document does not change any scheduler, adapter, or production behavior.

## Purpose

Answer one deterministic question at the trigger edge, before any
NullOne application workflow logic runs:

> Given one scheduler/trigger adapter's exact receipt of "run this
> workflow now," what is the single, stable, replay-safe NullOne
> occurrence identity for it?

This contract defines the **schema**, the **exact required field set**,
the **field semantics**, the **canonical serialization and
`occurrence_id` derivation rule**, and **worked adapter-mapping
examples**. It does not implement a scheduler adapter, a scheduler, or
any workflow execution — those belong to #59/#61/#62/#63.

## Relationship to the NullOne Application Runtime architecture

This contract is the "Scheduler invocation contract" section of
`docs/architecture/nullone-application-runtime.md`, made exact and
fixture-validated. That document remains the canonical source for
layering, workflow ownership, ports, and adapters; this document does
not redefine or contradict it, and adds no new architectural decision
beyond making the existing `occurrence_id` derivation rule unambiguous.

```text
Infrastructure adapters → NullOne Application Runtime → Domain/Core
```

`SchedulerInvocation` is the value contract at the trigger edge of that
diagram. It is a validated, normalized value — not an abstract scheduler
framework, not a queue, and not a retry policy.

## Non-goals

- No scheduler, cron, job queue, or OpenClaw automation implementation.
- No `MorningWorkflow`/`AnalyticsWorkflow`/`StoryWorkflow`/`BreakingWorkflow`
  implementation (#59/#62/#63).
- No change to #27's `run_id`/run-outcome contract
  (`workspace/social/ops/scripts/nullone_run_outcome.py`). This document
  only specifies what feeds `make_run_id`'s `occurrence_id` argument.
- No OpenClaw-specific UUID/job-ID requirement. NullOne must not require
  an OpenClaw UUID shape, and must not substitute the current time for a
  missing `external_occurrence_id`.
- No publication, approval, or scheduling authority. This contract
  normalizes an invocation; it grants no capability.

## Schema

`nullone.scheduler-invocation.v1`

## Contract version

`1.0.0`

## Exact required fields

No optional or unknown fields. A payload with a missing required field or
an additional unrecognized field is invalid input.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string, literal | Must equal `nullone.scheduler-invocation.v1` exactly. |
| `contract_version` | string, literal | Must equal `1.0.0` exactly. |
| `workflow_id` | string | Stable NullOne-owned workflow identity. See "workflow_id" below. |
| `source` | string | Stable scheduler/trigger-adapter namespace. See "source" below. |
| `external_occurrence_id` | string | Opaque, non-empty occurrence identity from the source adapter. See "external_occurrence_id" below. |
| `scheduled_for` | string | Canonical UTC RFC 3339 instant of the logical scheduled occurrence. |
| `triggered_at` | string | Canonical UTC RFC 3339 instant this adapter invocation was observed. Observational only. |
| `occurrence_id` | string | Deterministic NullOne-owned identity. See "Occurrence identity derivation" below. |

## Field semantics

### `workflow_id`

Current accepted values, exactly:

- `morning-editorial`
- `daily-analytics`
- `story`
- `breaking`

`workflow_id` is NullOne-owned business identity, not an OpenClaw
agent/job ID and not any other adapter's internal identifier. An adapter
maps its own trigger configuration (whatever that adapter calls it) to
exactly one of these four values; NullOne never accepts an adapter's
native job name as a substitute.

### `source`

Stable scheduler/trigger-adapter namespace. The current accepted value
is `openclaw`. An alternate adapter (see "Alternate scheduler example"
below) uses its own stable namespace, e.g. `systemd-timer`. `source` is
part of `occurrence_id`'s stable identity input specifically so that two
different adapters can never collide on the same occurrence identity
even if they happen to reuse the same `external_occurrence_id` string.

### `external_occurrence_id`

Opaque, non-empty identity supplied or deterministically mapped by the
source adapter. NullOne treats it as an opaque string:

- It must not be empty or whitespace-only.
- NullOne must not require any particular shape (no OpenClaw UUID
  requirement).
- It must never be replaced by the current time when missing — a
  missing or empty value is invalid input and must fail closed, not
  silently mint a same-instant identity.

### `scheduled_for`

Canonical UTC RFC 3339 instant, exactly matching
`YYYY-MM-DDTHH:MM:SSZ` (uppercase literal `T` and `Z`, zero-padded
two-digit month/day/hour/minute/second, four-digit year, no fractional
seconds, no numeric UTC offset). Represents the logical scheduled
occurrence — "the 04:30 Morning Editorial run," independent of when any
particular adapter attempt observed it.

### `triggered_at`

Canonical UTC RFC 3339 instant, same exact format as `scheduled_for`.
Represents when this adapter invocation was observed. It is
observational metadata only:

- It must **not** participate in `occurrence_id` derivation.
- It must **not** affect logical occurrence identity, retry
  deduplication, or run ordering.
- A retry, a delayed delivery, or a re-observed invocation of the same
  logical scheduler occurrence produces a different `triggered_at` but
  the same `occurrence_id`.

## Occurrence identity derivation

The existing NullOne Application Runtime architecture states:

> `occurrence_id = "occ_" + first 24 lowercase hex characters of
> SHA-256(...)`

This section makes that serialization rule completely unambiguous,
following the exact canonical-serialization convention the repository
already uses for #27's `run_id` in
`workspace/social/ops/scripts/nullone_run_outcome.py::make_run_id`
(`json.dumps(..., ensure_ascii=True, separators=(",", ":"))` encoded as
UTF-8, then SHA-256, then a lowercase-hex prefix). No new serialization
framework is introduced; this is that same convention applied to a
5-element array instead of a 2-element one.

### Canonical identity input

The canonical identity input is exactly the ordered array:

```json
[
  "nullone.scheduler-invocation.v1",
  "<workflow_id>",
  "<source>",
  "<external_occurrence_id>",
  "<scheduled_for>"
]
```

`triggered_at` and `occurrence_id` itself are never included in this
array. `schema` is included as the fixed literal
`nullone.scheduler-invocation.v1`, not the payload's `contract_version`.

### Canonical UTF-8 JSON serialization rule

1. Build the five-element JSON array above, with all five elements as
   JSON strings in that exact order.
2. Serialize it as compact JSON using `ensure_ascii=True` (every
   non-ASCII code point is escaped as a stable lowercase `\uXXXX`
   sequence — this is the same escaping Python's `json.dumps` produces
   with `ensure_ascii=True`, which is what `make_run_id` already uses).
3. Use `separators=(",", ":")` — no insignificant whitespace: no space
   after `,`, no space after `:`, no indentation, no trailing newline.
4. Encode the resulting JSON string as UTF-8 bytes. This is the exact
   hash input. Do not append a trailing newline or any other byte to it.

Equivalent to, in Python (the reference implementation, matching
`make_run_id`'s existing convention):

```python
import hashlib
import json

def occurrence_id(workflow_id: str, source: str, external_occurrence_id: str, scheduled_for: str) -> str:
    canonical = json.dumps(
        [
            "nullone.scheduler-invocation.v1",
            workflow_id,
            source,
            external_occurrence_id,
            scheduled_for,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return f"occ_{digest}"
```

`hashlib.sha256(...).hexdigest()` already returns lowercase hexadecimal
characters, so "first 24 lowercase hex characters" is exactly
`hexdigest()[:24]` with no case conversion required.

### Determinism and replay rules

- A retry or delayed invocation for the same logical scheduler
  occurrence (same `workflow_id`, `source`, `external_occurrence_id`,
  `scheduled_for`, any `triggered_at`) **must** produce the same
  `occurrence_id`.
- Changing any one of `workflow_id`, `source`, `external_occurrence_id`,
  or `scheduled_for` **must** produce a different `occurrence_id`.
- A supplied `occurrence_id` (e.g. echoed back by an adapter, or
  recorded in a manifest) must be recomputed from the four stable
  fields and compared exactly (byte-for-byte string equality). A
  mismatch is invalid input, full stop.
- A mismatch must never be reinterpreted as "this must be a new
  occurrence" — it is rejected as malformed input, and the adapter must
  fail closed rather than mint a new identity or guess.

## #27 run identity compatibility (confirmed, unchanged)

`workspace/social/ops/scripts/nullone_run_outcome.py::make_run_id`
(accepted, unchanged by this document) computes:

```python
run_id = "run_" + sha256(json.dumps([workflow_id, occurrence_id], ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
```

This contract supplies exactly the two inputs `make_run_id` already
requires, unmodified:

- `workflow_id` — the same stable NullOne workflow identity defined
  above, passed through unchanged.
- `occurrence_id` — the deterministic value this contract derives above,
  passed through unchanged as `make_run_id`'s `occurrence_id` argument.

No #27 semantic change is made or required. This document only makes
precise how a scheduler invocation's stable fields become the
`occurrence_id` that #27 already expects as an opaque, stable input.
Worked example (values reproduced from
`tests/fixtures/scheduler_invocation_v1_examples.json`):

```text
workflow_id    = "morning-editorial"
occurrence_id  = "occ_f91cb754b51f00645f425538"
run_id         = make_run_id(workflow_id, occurrence_id)
               = "run_f05fcfe2e88bd1114fed1625"
```

## Adapter mapping examples

### Current OpenClaw example (placeholders only)

```text
OpenClaw scheduler occurrence
        |  (OpenClaw-internal job id, schedule config -- not shown)
        v
OpenClaw edge adapter
        |  maps: OpenClaw job -> workflow_id
        |        "openclaw"  -> source
        |        OpenClaw's own occurrence marker -> external_occurrence_id (opaque)
        |        OpenClaw's scheduled instant -> scheduled_for (normalized to UTC RFC 3339)
        |        wall-clock observation time -> triggered_at
        |        computes occurrence_id per the rule above
        v
nullone.scheduler-invocation.v1
        v
NullOne Application Runtime
```

Concrete placeholder payload (no private job IDs, no production cron
payload):

```json
{
  "schema": "nullone.scheduler-invocation.v1",
  "contract_version": "1.0.0",
  "workflow_id": "morning-editorial",
  "source": "openclaw",
  "external_occurrence_id": "openclaw-occ-morning-0001",
  "scheduled_for": "2026-09-08T04:30:00Z",
  "triggered_at": "2026-09-08T04:30:02Z",
  "occurrence_id": "occ_f91cb754b51f00645f425538"
}
```

The OpenClaw adapter owns translating its own cron/job syntax and
native `failureAlert`/config semantics into this value; nothing past
this boundary knows OpenClaw exists.

### Alternate scheduler example (hypothetical, not built)

A systemd timer is used here only to prove the contract does not depend
on OpenClaw. This adapter is **not implemented** by this document or by
#65 — it is illustrative only.

```text
systemd timer unit fires (OnCalendar=*-*-* 04:30:00)
        v
hypothetical systemd-timer edge adapter
        |  maps: timer unit name -> workflow_id
        |        "systemd-timer" -> source
        |        deterministic mapping of unit name + calendar
        |          occurrence -> external_occurrence_id (opaque)
        |        unit's calendar instant -> scheduled_for (normalized to UTC RFC 3339)
        |        wall-clock observation time -> triggered_at
        |        computes occurrence_id per the same rule above
        v
nullone.scheduler-invocation.v1   <-- byte-for-byte the same schema
        v
NullOne Application Runtime        <-- unchanged workflow/domain logic
```

```json
{
  "schema": "nullone.scheduler-invocation.v1",
  "contract_version": "1.0.0",
  "workflow_id": "morning-editorial",
  "source": "systemd-timer",
  "external_occurrence_id": "systemd-timer-run-2026-09-08T0430",
  "scheduled_for": "2026-09-08T04:30:00Z",
  "triggered_at": "2026-09-08T04:30:01Z",
  "occurrence_id": "occ_8d312f344d3ced803ee0c7ec"
}
```

Note this produces a **different** `occurrence_id` than the OpenClaw
example above for the same `workflow_id` and `scheduled_for`, because
`source` and `external_occurrence_id` differ — this is intended:
`source` is part of the stable identity precisely so two adapters can
never collide. `MorningWorkflow` and every other application workflow
consume either payload identically; no application/domain code changes
between the two adapters.

## Ports selected for M0 (unchanged, restated)

This document does not add ports beyond those already selected in
`docs/architecture/nullone-application-runtime.md`
("Ports selected for M0"). `SchedulerInvocation` is a validated value
contract at the trigger edge, not an abstract scheduler framework.

## Critical dependency rule (restated for this contract)

Application/domain modules that consume a validated
`SchedulerInvocation` value must not:

- import OpenClaw internals;
- shell out to `openclaw`;
- embed cron/job syntax;
- read provider-specific secrets as application semantics;
- hard-code Zernio/Telegram transport details.

Because #59/#61/#62/#63 have not yet introduced application/domain
modules that consume this contract, this document does not invent an
empty production directory or a fake workflow module to statically
enforce this rule today. When those modules are added, their static
check must assert the five points above against whatever
application/domain package boundary #59/#61/#62/#63 introduces.

## Safety and authority preserved (restated for this contract)

- #27–#36 remain authoritative and unchanged; this contract supplies
  only `make_run_id`'s existing `occurrence_id` input, unmodified.
- Scheduler success is never domain success. A validated, normalized
  `SchedulerInvocation` means only "an adapter observed a trigger for
  this occurrence" — it carries no statement about workflow outcome.
- The NullOne Application Runtime gains no publication authorization
  from this contract.
- `ReviewDelivery` does not imply approval; this contract does not
  touch `ReviewDelivery` semantics.
- Draft creation remains review-only; publication still requires the
  existing human-approval boundary. No blind publish.
- No provider receipt (scheduler or otherwise) is automatically trusted
  as publication authorization.
- `UNKNOWN` and consumed-attempt semantics in #27–#36 remain unchanged.
- Provider/scheduler adapters cannot broaden domain authority merely by
  supplying a validated invocation.

## Validation

- `tests/fixtures/scheduler_invocation_v1_examples.json` — worked
  examples: valid payloads per workflow, the OpenClaw and alternate
  adapter mappings, a same-occurrence replay pair, and distinct-field
  negative-control cases, all with pre-computed `occurrence_id` values.
- `tests/test_scheduler_invocation_contract_fixture.py` — contract
  fixture validation only (schema/version/field-set exactness, allowed
  `workflow_id` values, non-empty `source`/`external_occurrence_id`,
  strict canonical timestamp format, deterministic `occurrence_id`
  recomputation and comparison, `triggered_at` exclusion, malformed
  `occurrence_id` rejection, replay stability, distinct-field
  divergence). It does not implement or invoke an adapter, and performs
  no subprocess/network access.
- `python3 tests/run_offline.py` must remain green.

## Exit rule for this contract's scope in issue #65

This document, together with
`docs/architecture/nullone-application-runtime.md`, satisfies the
"Required scheduler invocation contract" and "Offline validation
requirements" sections of issue #65 when:

1. the exact required field set is fixed and no optional/unknown field
   is permitted;
2. `workflow_id` is confirmed NullOne-owned, not an OpenClaw job ID;
3. `external_occurrence_id` is confirmed opaque, non-empty, and never
   backfilled with the current time;
4. `scheduled_for`/`triggered_at` are both canonical UTC RFC 3339, and
   `triggered_at` is confirmed excluded from identity;
5. the canonical serialization and `occurrence_id` derivation rule is
   fully unambiguous (this document, "Occurrence identity derivation");
6. a supplied `occurrence_id` mismatch is defined as invalid input, not
   a new occurrence;
7. the #27 `run_id` compatibility finding is documented without
   modifying #27;
8. at least one OpenClaw example and one alternate-scheduler example
   exist and produce the same contract shape;
9. fixtures and a fixture-validation test exist and
   `python3 tests/run_offline.py` remains green;
10. no runtime, adapter, job, or production behavior is implemented.

Closing #65 does not close #59, #60, #61, #62, #63, or #66, and does not
implement or extend #13.
