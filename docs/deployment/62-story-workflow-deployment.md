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

Notes on what is deliberately NOT decided here:

- **No job UUID is invented.** The preflight confirmed no dedicated Story
  job exists; this document does not assign one. A future deployment PR
  under #37 creates and records the actual job configuration and its
  identity at that time.
- **Reuse vs. replace Draft Factory scheduling** is an open deployment
  choice, not a fact this document asserts. Whether the eventual Story
  job reuses or replaces existing Draft Factory scheduling infrastructure
  is a decision for that future deployment PR; nothing here should be read
  as claiming either option is already chosen.
- **No cron/systemd timer syntax is authored here.** The OpenClaw edge
  adapter box above is descriptive of the mapping responsibility, not an
  implementation.
- **The candidate-availability signal** (`candidate_availability` passed
  into `run_story_workflow`) is deliberately out of this document's scope
  too: today's repository has no reviewed source for it beyond the
  existing upstream editorial process the #31 cadence contract already
  assumes computes it. #62 does not add a new one.

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
