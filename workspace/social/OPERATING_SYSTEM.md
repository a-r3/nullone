# NullOne Media Operating System

## Mission

Build NullOne into one of Azerbaijan's most recognized AI/technology media brands.

Core promise:

FAST + ACCURATE + EXPLAINED

NullOne should not merely repeat technology news.
It should discover important developments early, verify them, explain why they
matter to an Azerbaijani audience, package them well for Instagram, and learn
continuously from real audience behavior.

---

## Core operating loop

DISCOVER
→ VERIFY
→ SCORE
→ SELECT
→ FORMAT
→ WRITE
→ RENDER
→ VERIFY FINAL ASSET
→ ZERNIO PREFLIGHT
→ DRAFT
→ APPROVAL
→ PUBLISH
→ MEASURE
→ LEARN
→ UPDATE STRATEGY

Every published item must eventually return performance data into the learning loop.

---

## Current autonomy

Mode: DRAFT_FIRST

Allowed autonomously:
- monitor sources
- discover topics
- research
- verify
- score/rank
- choose format
- write Azerbaijani copy
- create exact-size NullOne assets
- validate media
- Zernio preflight
- create/update Zernio drafts
- collect analytics
- analyze performance

NOT allowed autonomously yet:
- publish
- schedule publication
- comment
- DM
- follow/unfollow
- like
- interact with users

Publishing requires explicit operator approval for the exact draft/post.

---

## Editorial memory

Do not rely on conversation memory for operational state.

Persistent state lives in files:

social/state/candidate-queue.md
    Current viable stories.

social/state/topic-ledger.jsonl
    Stories already considered, rejected, drafted or published.

social/state/publish-ledger.jsonl
    Every publication and its metadata.

social/state/experiments.jsonl
    Controlled growth/content experiments.

social/analytics/
    Raw and interpreted performance data.

social/references/style-profile.md
    Reference intelligence.

social/references/visual-rules.md
    Current NullOne visual specification.

MEMORY.md
    Durable high-level lessons only.

---

## Duplicate prevention

Before preparing a story, check:

1. candidate queue
2. current Zernio drafts
3. published ledger
4. recent published content

Do not create another feed post on substantially the same development unless:
- there is a material new development;
- it is explicitly a follow-up;
- or the format serves a genuinely different editorial purpose.

---

## Initial content targets

These are targets, not quotas.

Feed:
- baseline 2 strong posts/day
- optional 3rd only when justified

Stories:
- approximately 3–6/day

Carousel:
- approximately 3–5/week when explanation adds value

Reels:
- introduced progressively after the repeatable video workflow is stable

Never lower the quality threshold merely to hit volume.

---

## Growth objective

Do not optimize primarily for likes.

Prioritize:

1. shares
2. saves
3. follows generated
4. follows per reach
5. profile visits
6. reach
7. meaningful comments
8. Story completion/navigation quality
9. carousel usefulness
10. likes

A post with fewer likes but substantially more shares/saves/follows may be a
better growth asset.

---

## Learning dimensions

For every published feed item track when possible:

- topic cluster
- specific topic
- source class
- format
- visual template
- hook type
- headline
- headline length
- number-led hook yes/no
- explanation depth
- CTA type
- publish weekday
- publish time
- freshness at publication
- reach
- views
- likes
- comments
- shares
- saves
- follows
- profile visits
- impressions if available

For Reels additionally:
- views
- watch-related metrics where available

For Stories additionally:
- reach
- views
- replies
- shares
- profile visits
- follows
- taps forward
- taps back
- exits

---

## Experiment discipline

Do not change everything at once.

Prefer controlled experiments such as:

- headline style A vs B
- short vs explanatory caption
- source-image template vs text-led card
- single post vs carousel
- publish window A vs B
- CTA vs no CTA

Change one major variable where practical.

Record the hypothesis before evaluating the result.

Never manufacture engagement.

---

## Strategy evolution

Strategy must be based increasingly on NullOne's own audience data.

Reference accounts are useful for:
- market context
- format awareness
- editorial patterns
- local expectations

They are NOT the optimization target.

As NullOne accumulates sufficient data, its own performance history overrides
reference-account assumptions.

---

## Editorial differentiation

NullOne's target position:

"Azərbaycanda AI və texnologiyada nə baş verdiyini tez tapıb,
dəqiq yoxlayan və niyə vacib olduğunu aydın izah edən media."

Default editorial question:

WHY SHOULD THE READER CARE?

If the content cannot answer that convincingly, it may belong in a Story or
may not deserve publication at all.

## Story visual-learning loop

Story templates are not a fixed brand identity.

The system should learn which combinations of:
- layout
- topic
- hook
- source visual
- information density
- statistic prominence

perform best.

Current Story renderer baseline:
V2.

Do not optimize for visual consistency at the expense of information value.
NullOne should be recognizable, but individual Stories should not all look
identical.
