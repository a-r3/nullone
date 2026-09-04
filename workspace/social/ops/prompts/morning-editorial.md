Run the NullOne MORNING EDITORIAL PLANNING cycle.

Read:
- AGENTS.md
- social/CONTENT_STRATEGY.md
- social/STATE_RULES.md
- social/OPERATING_SYSTEM.md
- social/SCORING.md
- social/ACCOUNT.md
- social/CONTENT_RULES.md
- social/SOURCES.md
- social/references/style-profile.md
- social/references/visual-rules.md
- social/state/candidate-queue.md
- social/state/topic-ledger.jsonl
- social/state/publish-ledger.jsonl

MODE:
EDITORIAL_PLANNING_ONLY.

EXECUTION CONTRACT:

Complete this editorial cycle inside THIS automation run.

Do NOT:
- spawn a subagent
- delegate to another session
- use sessions_spawn
- use sessions_send
- use subagents
- announce that work will continue in the background

The automation is not complete until today's editorial-board file has
actually been written.

Before returning success, verify that this exact file exists:

social/research/daily/YYYY-MM-DD-editorial-board.md

If the cycle cannot be completed in this run, return BLOCKED with the
specific reason instead of delegating or claiming background progress.

DO NOT:
- publish
- schedule
- create a Zernio post
- create production media
- like/comment/follow/message on Instagram

MISSION:

Build the strongest diversified editorial board for NullOne today.

Do NOT treat this as a news-counting exercise.

TASK:

1. Review the latest AI and technology cycle independently.

Use:
- primary company/lab sources
- official documentation and release notes
- reputable technology journalism
- useful discovery/community signals
- meaningful Azerbaijan technology/startup sources

Reference Instagram accounts are editorial/style signals only.
Never use them as the sole factual source.

2. Target roughly 8–15 serious discovery signals.

Do not deeply research every signal.

3. Remove:
- duplicates
- stale items
- minor noise
- weakly sourced claims
- topic clusters recently over-covered
- substantially identical angles already in queue/published history

4. Build candidate opportunities across BOTH:

CURRENT:
- NEWS
- BREAKING

and when source-supported:

EDITORIAL:
- EXPLAINER
- PRACTICAL
- COMPARISON
- AZ_CONTEXT
- EVERGREEN

A quiet news day is not editorial failure.

5. Score viable candidates using social/SCORING.md.

6. Create today's canonical editorial board:

social/research/daily/YYYY-MM-DD-editorial-board.md

Normally include 4–6 serious candidates.

For every board candidate record:

- RANK
- TOPIC
- TOPIC_BUCKET
- CONTENT_TYPE
- TOPIC_CLUSTER
- ANGLE
- WHY_NOW
- AUDIENCE_VALUE
- PRIMARY_SOURCE
- SUPPORTING_SOURCE if needed
- SCORE
- RECOMMENDED_FORMAT
- FRESHNESS_CLASS
- FRESHNESS_DEADLINE if applicable
- DUPLICATE_CHECK
- VERIFICATION_STATUS
- STATUS

Allowed CONTENT_TYPE:

NEWS
BREAKING
EXPLAINER
PRACTICAL
COMPARISON
AZ_CONTEXT
EVERGREEN

7. Add only genuinely useful candidates to:

social/state/candidate-queue.md

New queue entries must include the Content Strategy V1 metadata fields.

8. Candidates may become READY only when:
- their score passes the relevant threshold
- the central claim is sufficiently verified
- the proposed angle is distinct
- the item remains editorially useful

Otherwise use:
NEW
RESEARCHING
DEFERRED
REJECTED

9. For EXPLAINER/PRACTICAL/EVERGREEN ideas:

Remain source-driven.

Prefer ideas derived from:
- current relevant developments
- official product docs
- verified product capabilities
- recent topic clusters needing explanation
- useful durable concepts with current relevance

Do not create generic AI-tip filler.

10. Respect the derivative-content rule in social/CONTENT_STRATEGY.md.

Default:
maximum 2 main pieces from one topic cluster in 7 days unless a material
new development exists.

11. Append considered/rejected material to topic-ledger.jsonl only when it
will materially improve duplicate prevention.

No publication or Zernio creation.

Quality overrides quota.

If today's strongest board contains only 3 worthwhile candidates:
record 3.

If nothing is strong enough:
record that truthfully.

## Resource efficiency

Discovery:
- use web_search first
- use concise search result evidence
- fetch full pages only for serious candidates

Deep verification:
- focus on candidates likely to enter the board
- prefer primary sources
- do not copy long source text into reports

Context:
- reuse concise queue/ledger findings
- avoid repeatedly rereading already summarized material

Morning Editorial owns broad editorial discovery.
Breaking Radar does not duplicate this job.
