# #62 StoryWorkflow deployment mapping (proposed, not applied)

Status: repository documentation only. No production file, OpenClaw
config/job, cron entry, Zernio call, or Telegram send has been made or
changed by #62. This document describes the eventual Story trigger/job
integration so that a future, separately-authorized deployment (under
#37) has a reviewed mapping to follow — it does not perform that
deployment.

## Current production fact (2026-09-07 preflight)

Per `docs/deployment/37-preflight-2026-09-07.md`:

- there is **no Story-specific live OpenClaw job today**;
- host-level Zernio MCP/OAuth is healthy;
- the scheduled-session Zernio bootstrap used by the MCP-backed
  Story/main draft bridge (`nullone-draft-bridge.py`) still shows errors
  in that preflight.

#62 does not change either fact. It makes the `StoryWorkflow`
composition and the `DraftProvider`/`ReviewDelivery` adapters
deployable in the sense that they are implemented, tested offline, and
wired correctly to #33's existing production bridge path — it does not
and cannot prove the scheduled-session bootstrap works live. That
remains:

`LIVE_SCHEDULED_PATH_UNPROVEN` — deferred to #37.

## Reviewed OpenClaw transport contract (pinned, not executed)

The ReviewDelivery infrastructure adapter is pinned narrowly to OpenClaw
`v2026.8.2`, commit
`0965053fe6b9341776df147a6934b7485c60b5ca`. The reviewed upstream sources
are `src/cli/program/message/register.send.ts` and
`src/commands/message.ts` at that commit. They establish that `message send`
supports `--media` and `--presentation`, has no `--buttons` option, and emits
a top-level camelCase `messageId` when it has usable send identity.

One logical preview delivery therefore uses this exact proof sequence:

```text
ordered media send(s), one --media path per call
→ exact non-empty messageId proof for every media call
→ one --message plus exact producer-owned --presentation approval card
→ exact non-empty messageId proof for the approval card
→ SENT
```

Story and Feed each require one media proof followed by one approval-card
proof. Carousel sends every `payload.media[]` entry sequentially in its exact
payload order, then sends exactly one approval card. Any failure, timeout, or
missing/blank/wrong-case proof stops immediately and makes the logical result
non-SENT; there is no retry or cleanup of already-sent messages.

Before reading the owner target or invoking OpenClaw, the adapter validates the
whole transport-bound media bundle: schema-specific shape, workspace-contained
resolved regular files, and exact SHA-256 equality. It never uses presigned
URLs or regenerates media. Approval callbacks must be exactly one each of
`texbrif:approve:<REVIEW_POST_ID>`, `texbrif:reject:<REVIEW_POST_ID>`, and
`texbrif:revise:<REVIEW_POST_ID>`.

This pin is an offline implementation contract, not live Telegram proof. The
deployment status remains **PROPOSED / NOT APPLIED** and
`LIVE_SCHEDULED_PATH_UNPROVEN` remains unchanged.

## Proposed (desired, not current) deployment shape

```text
OpenClaw Story occurrence
        |  (a reviewed Story-specific job's own schedule/config --
        |   does not exist yet; #62 does not create it)
        v
OpenClaw scheduler/trigger edge adapter
        |  maps: this job's identity          -> workflow_id = "story"
        |        "openclaw"                    -> source
        |        this job's own occurrence marker -> external_occurrence_id
        |        this job's scheduled instant  -> scheduled_for (UTC RFC3339)
        |        wall-clock observation time   -> triggered_at
        |        computes occurrence_id per docs/contracts/scheduler-invocation-v1.md
        v
nullone.scheduler-invocation.v1
        v
nullone_story_workflow.run_story_workflow()
        |  reads authoritative state (nullone_cadence_state_adapter)
        |  evaluates cadence (nullone_cadence_controller, unchanged)
        |  on PREPARE_STORY/STORY_GAP only: candidate provider -> #33 core
        v
DraftProvider (nullone_story_pipeline.NulloneDraftBridgeConnector,
               existing, unchanged -- shells to nullone-draft-bridge.py)
        +
ReviewDelivery (nullone_telegram_review_delivery_adapter.TelegramReviewDeliveryAdapter)
        v
Telegram review preview -> human approve/revise/reject (unchanged boundary)
```

Notes on what was deliberately NOT decided here (resolved by #79
where noted; this document's historical record is otherwise unchanged):

- **No job UUID is invented.** The preflight confirmed no dedicated Story
  job exists; this document does not assign one. RESOLVED by #79 as a
  repository decision (dedicated `nullone-story-wakeup` job,
  `30 10,13,18,21 * * * Asia/Baku`, static wake-up command) —
  DESIRED / NOT DEPLOYED until #37.
- **Reuse vs. replace Draft Factory scheduling** is an open deployment
  choice, not a fact this document asserts. RESOLVED by #79: dedicated
  Story job (no reuse); Draft Factory keeps normal main work and must
  delegate Story opportunity once #79 is deployed.
- **No cron/systemd timer syntax is authored here.** The OpenClaw edge
  adapter box above is descriptive of the mapping responsibility, not an
  implementation. RESOLVED by #79 (exact desired command documented in
  `docs/deployment/79-story-production-integration.md`).
- **The candidate-availability signal** (`candidate_availability` passed
  into `run_story_workflow`) is deliberately out of this document's scope
  too: today's repository has no reviewed source for it beyond the
  existing upstream editorial process the #31 cadence contract already
  assumes computes it. RESOLVED by #79: derived from the same validated
  structured handoff snapshot the production provider reads
  (`snapshot_story_availability`). #62 does not add a new one.

## Publisher-wrapper deployment delta (restated, not changed)

The already-known #37 preflight fact is restated, not modified, here: the
production publisher wrapper (`nullone-publisher-run.py`) needs the
already-reviewed Story-supersession-aware code path during its own
eventual deployment. That remains a known file-deployment delta for a
future controlled deployment to apply, not a new engineering-design
question, and #62 does not touch publisher authorization, deploy it, or
modify the approval flow.

## Exit condition for this document

This document is superseded by an actual deployment record once #37
performs a controlled deployment; until then it remains a proposed
mapping only, and its existence does not authorize any of the actions it
describes.
