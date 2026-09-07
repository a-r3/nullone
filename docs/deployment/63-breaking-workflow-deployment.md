# BreakingWorkflow deployment mapping (#63)

Status: **PROPOSED / NOT APPLIED**

This document records the repository-level mapping only. No production
OpenClaw job, Radar prompt, Zernio connector, Telegram delivery, scheduler,
secret, ledger, or publication state was changed while implementing #63.

## Application flow

The dependency direction is:

```text
OpenClaw/Radar edge
  -> nullone.scheduler-invocation.v1 + nullone.breaking-workflow-input.v1
  -> BreakingWorkflow
  -> #35 load_repository_state() + evaluate()
  -> #36 evaluate_routing() + strict result validation
  -> #36 dispatch_draft_set()
  -> #33 Story core, then optional #36 main core
  -> existing DraftProvider + #62 ReviewDelivery
```

BreakingWorkflow accepts only `workflow_id == "breaking"`. The edge maps the
external occurrence through the merged scheduler-invocation contract;
`triggered_at` is excluded from occurrence identity, so replay of one logical
Radar occurrence cannot mint a new NullOne occurrence.

Breaking is an accelerated trigger into `run_story_pipeline()`. It does not
manufacture `PREPARE_STORY`, override target-min cadence, disable quiet hours,
or create a synthetic Story scheduler event. The ordinary `StoryWorkflow`
remains unchanged.

## Strict Radar handoff

Schema: `nullone.breaking-workflow-input.v1`, contract version `1.0.0`.

The machine-readable assessment carries exact candidate, assessment and state
references; structured #35 evidence; optional explicit `FollowUpDelta`;
verification and its exact ordered evidence refs; a verified severity and
reason; auditable recent-coverage/freshness findings; Story quality and draft
dependency findings; and optional exceptional-main structural findings.
Unknown fields are rejected. Identity and capacity results are deliberately
absent: NullOne recomputes #35 identity from fresh state and derives Story/main
breaking capacity from authoritative repository state.

The separate edge envelope is
`nullone.breaking-radar-handoff.v1`:

```json
{
  "schema": "nullone.breaking-radar-handoff.v1",
  "contract_version": "1.0.0",
  "occurrence": {
    "external_occurrence_id": "<stable external occurrence>",
    "scheduled_for": "<canonical UTC RFC3339>",
    "triggered_at": "<canonical UTC RFC3339>"
  },
  "assessment": {"schema": "nullone.breaking-workflow-input.v1"}
}
```

The abbreviated assessment above is illustrative only; the executable
validator requires its complete exact field set. No OpenClaw job UUID is an
application/domain requirement.

## Identity, routing, and capacity

The workflow constructs the existing #35 `CandidateInput`, `EvidenceItem`, and
optional `FollowUpDelta`, then calls:

```text
load_repository_state(workspace)
-> evaluate(candidate_input, fresh_state)
```

It never trusts a caller-supplied identity result. It then constructs the
existing #36 inputs, calls `evaluate_routing()`, and runs
`validate_routing_result_dict()` before durable dispatch.

Breaking bypasses regular timing permission only. Capacity is:

```text
story_effective_load = story_load.published_today + story_load.pending
main_effective_load  = main_load.published_today  + main_load.pending
```

Eligibility requires strict `<` comparison with the existing
`story_target_max_breaking` / `main_target_max_breaking` values. Equality is
exhausted. Caller main findings contain no `capacity_available`; the workflow
supplies that field from authoritative state.

The mandatory Story dispatcher recheck first reuses #35's
`make_state_authoritative_recheck()`, then reloads Story load and dependency
availability. The mandatory main-capacity callback reloads current main load
immediately before main. Any missing, malformed, unreadable, exhausted, or
decision-relevant-unknown state blocks before that target's draft attempt.

## Story-first and replay guarantees

`compute_draft_set_id(event_id, development_id)` binds both Story and optional
main candidate `request_lineage`. The existing dispatcher owns the durable
record and per-set `fcntl.flock`:

```text
Story DRAFT_CREATED + preview_delivery.status == SENT
  -> optional selected FEED/CAROUSEL
```

No main target runs first or as compensation. Exact replay reuses the same set
and does not repeat completed targets. A different routing decision for the
same development raises the existing `DraftSetConflict`. Recovered
`DISPATCH_IN_FLIGHT`, runner exceptions, and unrecognized consequential
results are `UNKNOWN` with reconciliation required and no automatic retry.

Story/main pipelines share the merged #62 `ReviewDelivery`; therefore the
OpenClaw v2026.8.2 media ordering, final `--presentation`, camelCase
`messageId`, exact `SENT`, and no-retry proof contract remains centralized in
that adapter. BreakingWorkflow contains none of those transport details.

## Outcome boundary

The result exposes occurrence, assessment/candidate refs, #35 identity, #36
routing, optional draft-set/dispatch proof, reconciliation state, and the
existing #27 domain classification. `SUCCEEDED` on an accelerated route means
only that every requested review draft exists and every required human-review
preview reports exact `SENT`; it never means Instagram publication.

`run_outcome_mapping()` provides deterministic #27 inputs. Persisting the
exact `nullone.run-outcome.v1` artifact and scheduler health is reserved for
the future production edge; #63 does not create a competing persistence
system.

## Current live limitations

The repository Breaking Radar prompt remains `DELTA_MONITORING_ONLY` and its
current operational output is
`social/research/daily/YYYY-MM-DD-breaking-HHMM.md`. It does not prove that a
production job emits the strict machine-readable handoff above. That handoff
is **DESIRED / NOT DEPLOYED**; arbitrary Radar Markdown must never be scraped
heuristically into safety-critical #35/#36 fields.

No reviewed structured authoritative production Breaking candidate source is
currently proven. No Story-specific live OpenClaw job is proven or deployed.
The scheduled-session Zernio DraftProvider bootstrap remains
`LIVE_SCHEDULED_PATH_UNPROVEN`. Natural live Breaking proof remains
`UNPROVEN_LIVE / DEFERRED_TO_#37`; no synthetic production event may be used
to manufacture it.
