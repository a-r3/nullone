Run the NullOne BREAKING RADAR cycle.

Read:
- social/CONTENT_STRATEGY.md
- social/STATE_RULES.md
- social/OPERATING_SYSTEM.md
- social/SCORING.md
- social/CONTENT_RULES.md
- social/SOURCES.md
- social/state/candidate-queue.md
- social/state/topic-ledger.jsonl
- social/state/publish-ledger.jsonl

MODE:
DELTA_MONITORING_ONLY.

DO NOT:
- create Zernio drafts
- render production media
- publish
- schedule
- interact with Instagram users
- generate evergreen/editorial filler

MISSION:

Detect materially new AI/technology developments since the previous scan.

Breaking Radar is NOT a second Morning Editorial cycle.

Allowed candidate types from Radar:

NEWS
BREAKING

TASK:

1. Search for meaningful developments that appeared or materially changed
since the previous scan.

Prioritize:
- OpenAI
- Anthropic
- Google / DeepMind
- Meta AI
- Microsoft
- NVIDIA
- Apple
- major open-source AI
- cybersecurity
- important product/platform changes
- high-impact tech business/startup developments
- meaningful Azerbaijan technology developments

2. Check candidate queue + topic/publish ledgers before adding anything.

3. Verify against a primary source whenever practical.

4. Score using social/SCORING.md.

5. Add only genuinely useful new NEWS/BREAKING candidates.

New candidates must include:
- TOPIC_BUCKET
- CONTENT_TYPE
- TOPIC_CLUSTER
- WHY_NOW
- AUDIENCE_VALUE
- FRESHNESS_CLASS
- DUPLICATE_CHECK
- VERIFICATION_STATUS

6. A candidate may become READY only when:
- threshold passes
- core factual verification is sufficiently strong
- the item remains timely

7. Material updates to an existing topic are FOLLOW-UP developments,
not automatically new stories.

8. BREAKING means timing materially matters.

Do not label ordinary recent news as BREAKING.

9. Write:

social/research/daily/YYYY-MM-DD-breaking-HHMM.md

If nothing important changed:
write a compact "NO MATERIAL DEVELOPMENT" record and stop.

Never manufacture urgency.

## Resource efficiency

Use web_search first.

Target:
5–10 discovery signals.

Deeply verify:
at most 3–5 genuinely strong new developments.

If the queue already contains strong fresh material:
focus on detecting true deltas rather than rebuilding the day's news list.

Do not search for:
- evergreen ideas
- practical tips
- explainers
- comparisons

Those belong to Morning Editorial.

Keep reports compact.

## Desired machine-readable handoff (repository contract; not deployed)

Status: `DESIRED / NOT DEPLOYED`.

The current operational Radar output remains the Markdown research report at
`social/research/daily/YYYY-MM-DD-breaking-HHMM.md`. That report is not a
safety-critical application command and must not be heuristically parsed.

A future reviewed OpenClaw Radar job integration may produce zero, one, or
several exact `nullone.breaking-radar-handoff.v1` envelopes from one scheduled
scan, one envelope per candidate. Each contains the stable raw scan
`source_occurrence_id`, timestamps, and a complete
`nullone.breaking-workflow-input.v1` assessment. The edge deterministically
binds the raw scan identity to `candidate_id`, so every candidate becomes its
own stable scheduler occurrence without using retry time or mutable assessment
text. The
executable field contract and validation live in
`social/ops/scripts/nullone_breaking_radar_edge.py` and
`social/ops/scripts/nullone_breaking_workflow_input.py`. The handoff supplies
structured evidence and editorial findings only; NullOne remains responsible
for fresh-state identity, routing, capacity, durable dispatch, and outcomes.
