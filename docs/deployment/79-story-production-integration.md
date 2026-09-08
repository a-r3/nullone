# #79 Story production integration (repository decision, not applied)

Status: **IMPLEMENTED & REVIEWED — NOT DEPLOYED**. This document describes
the reviewed production Story trigger, authoritative candidate source, and
deployment mapping. No production file, OpenClaw job, prompt, Zernio call,
Telegram send, or ledger/state change was made by #79. Live proof remains
deferred to #37.

## 1. Scheduling decision

Dedicated Story wake-up job (reuse of the legacy Draft Factory job as the
authoritative Story trigger was considered and rejected: after activation
there must not be two independent normal Story producers).

Approved Story check windows (`CONTENT_STRATEGY.md` §10, already referenced
by the cadence contract's daypart table), Asia/Baku:

| Slot | Local | Schedule ID |
| --- | --- | --- |
| A | 10:30 | `story.check-1030.v1` |
| B | 13:30 | `story.check-1330.v1` |
| C | 18:30 | `story.check-1830.v1` |
| D | 21:30 | `story.check-2130.v1` |

Desired OpenClaw shape (OpenClaw 2026.8.2 `--command` syntax, static, no
`scheduled_for` interpolation, no secret):

```bash
openclaw automations create "30 10,13,18,21 * * *" \
  --name "nullone-story-wakeup" \
  --command "python3 <repo>/workspace/social/ops/scripts/nullone-scheduled-wakeup.py story --source openclaw" \
  --command-cwd "<repo>" \
  --tz "Asia/Baku"
```

Status: DESIRED / NOT DEPLOYED. No Story live job exists today; the legacy
Draft Factory remains live until #37.

## 2. Occurrence authority (multi-slot)

`nullone_schedule_registry.py` owns four immutable Story `ScheduleSpec`
entries (Morning/Daily single-slot behavior byte-for-behavior unchanged).
`resolve_scheduled_occurrence(workflow_id="story", ...)` resolves the
latest same-date slot at or before the wake instant:

- before 10:30 → `NO_DUE_OCCURRENCE` (never previous-day backfill);
- 10:30–13:29:59 → A; 13:30–18:29:59 → B; 18:30–21:29:59 → C; ≥21:30 → D;
- missed slots coalesce into one current evaluation, never N replays;
- `triggered_at` excluded from identity; alternate source namespaces mint
  distinct `occurrence_id` for the same slot (never activate two sources
  concurrently).

## 3. Structured candidate source

`nullone.editorial-candidate-handoff.v1` (contract `1.0.0`), one artifact
per Baku editorial date:

```text
social/research/daily/YYYY-MM-DD-editorial-candidates.json
```

Machine-authored directly by the Morning Editorial cycle alongside the
board Markdown — never parsed out of Markdown later. Carries Morning's
accepted ranking (`rank`), stable candidate IDs, verification state,
evidence refs, and the explicit `story_eligible` flag (true only for
VERIFICATION: PASS, Story-suitable candidates). An empty candidate list
is a valid truthful "no candidate" state; quota is never filled.

Strict validator (`nullone_editorial_candidate_handoff.py`): exact
schema/version, real calendar date, no unknown fields, unique IDs,
unambiguous ranks, non-empty evidence, eligible ⟹ verification PASS **and**
editorial_status READY (PASS + DEFERRED/REJECTED/NEW/RESEARCHING with
`story_eligible=true` is malformed, never upgraded), workspace-contained
regular file (no symlink/escape), `editorial_date` matching the requested
date, and `board_path` naming exactly the date's canonical board
(single canonical helpers shared by Morning and Story, so the two sides
cannot disagree).

## 4. Morning Editorial output change

`run_morning_editorial` now requires BOTH artifacts (board + handoff) for
a successful occurrence. Partial-output rule (#28 material-progress
guard): once ANY provider-owned artifact exists for the occurrence, the
editorial provider is never invoked again for it -- bounded retry (max 2,
unreachable-only, same run id) survives solely for a genuinely empty
first attempt. Partial sets fail closed without a second mutation:
board-only → `HANDOFF_INCOMPLETE`; handoff-only →
`PARTIAL_EDITORIAL_ARTIFACT_SET`; present-but-malformed →
`HANDOFF_INVALID`. Morning success additionally proves exact date/board
binding through the same canonical loader Story reads. The desired
prompt (`morning-editorial.md`) requires the agent to write both
artifacts, mark `story_eligible=true` only for PASS + READY + genuinely
Story-suitable candidates, and never overwrite a completed handoff.

## 5. Availability + provider

One immutable snapshot per evaluation: `load_handoff_snapshot` →
`snapshot_story_availability` (cadence boolean) + provider bound to the
same snapshot object. No second scan, no Markdown, no model call.

`StructuredHandoffStoryProvider` (real `StoryCandidateProvider`): keeps
Morning-flagged eligible candidates in rank order (READY-only,
defense-in-depth), drops #33-inadmissible ones, drops
review-attempt-consumed ones (STORY manifest with the same deterministic
`story_request_id` and `create_attempts > 0`), returns at most the first
remaining candidate. Zero remaining is truthful unavailability, never an
error. Consumed-state reads fail closed (symlink/non-regular entries,
malformed JSON, non-object JSON, identity-less STORY manifests, malformed
review blocks all raise) so uncertainty never reads as unconsumed.

## 6. Production entrypoint

```text
nullone-scheduled-wakeup.py story --source openclaw
→ resolve_scheduled_occurrence (story multi-slot)
→ nullone.scheduler-invocation.v1 (workflow_id=story)
→ run_story_trigger (nullone_story_scheduled_workflow.py)
→ Morning #27 provenance proof (MORNING_SOURCE_UNPROVEN if absent)
→ run_story_workflow (cadence → provider → #33 → delivery)
→ #27 assess + emit under social/ops/run-outcomes/story
→ #30 notify once
```

Before any candidate/provider/writer/draft/delivery work, Story proves
the date's Morning occurrence established a valid persisted SUCCEEDED
#27 result with correct deterministic identity declaring both artifacts;
a valid handoff file left behind by a failed Morning cycle is never
consumed (`MORNING_SOURCE_UNPROVEN`). No second health database, no
scheduler stdout.

Production wiring (`run_story_trigger` in
`nullone_scheduled_run_dispatch.py`): `HaikuStoryWriter`,
`numeric_scope_verifier`, `NulloneDraftBridgeConnector` (scheduled-session
live path still `LIVE_SCHEDULED_PATH_UNPROVEN`, owned by #81 — neither
proven nor replaced here), `TelegramReviewDeliveryAdapter` (unchanged #62
contract), production notifier. No publication capability anywhere.

Scheduler-vs-domain: COMPLETED + exit 0 for every established outcome
including truthful non-success (`NO_ACTION`, candidate BLOCKED states,
pipeline FAILED); FAILED + non-zero only for establishment failures
(rejected trigger, unreadable source/state, crash, untrusted notifier).
Domain notifier runs only on the COMPLETED path (no duplication with the
native alert, which owns crashes). `DRAFT_CREATED` counts as success only
with `preview_delivery.status == SENT`, artifact-backed by the persisted
manifest.

## 7. Draft Factory Story exclusivity

Desired prompt change (`draft-factory.md`, highest-priority override, NOT
DEPLOYED): Draft Factory produces normal main FEED/CAROUSEL only and must
delegate Story opportunity to StoryWorkflow. Breaking's Story-first path
is separate and unaffected.

## 8. Relation to #80 / #81

- #80: #79 leaves a clean path — Breaking supplies its own already-verified
  candidate through the reviewed adapter; the normal scheduled Story source
  is never mandatory for Breaking-triggered candidates. #80 not
  implemented here.
- #81: the MCP-backed DraftConnector live path is wired as the desired
  dependency but remains UNPROVEN; #81 owns its proof/replacement. #81 not
  closed here.

## 9. Deployment delta (for #37, not executed)

Install: `nullone_editorial_candidate_handoff.py`,
`nullone_story_production_provider.py`,
`nullone_story_scheduled_workflow.py`, updated registry/authority/
wakeup/dispatch/editorial-runtime, updated Morning + Draft Factory
prompts. Preserve: ledgers, manifests, notifications, run-outcomes,
private owner-id, existing jobs (record legacy definitions for rollback).
Create: `nullone-story-wakeup` job (above). No ledger rewind, no approval
rollback, no duplicate replay (per-run locks + persisted results).

Rollback: remove installed files (restore prior versions), delete the
wake-up job, restore legacy prompts, keep all state files untouched.

## 10. Live truth

No Story live job, no live structured handoff, no live provider, no live
draft, no Telegram proof. All `UNPROVEN_LIVE / DEFERRED_TO_#37`.
