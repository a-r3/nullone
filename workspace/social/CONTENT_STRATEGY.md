# NullOne Content Strategy V1

Version: 1.0
Status: ACTIVE

## Mission

NullOne is not a news-only account.

The editorial system must answer:

"What is the most useful, relevant and timely technology content
NullOne should give its audience today?"

not merely:

"What new technology news happened today?"

Quality overrides quota.

---

## 1. Two independent editorial dimensions

### Topic bucket

Initial rolling direction:

- AI: ~60%
- TECH: ~25%
- AZ_TECH: ~10%
- EXPERIMENT: ~5%

This is a starting hypothesis, not a permanent quota.

NullOne analytics should gradually override this mix.

### Content type

Main Feed/Carousel content should use:

- NEWS
- BREAKING
- EXPLAINER
- PRACTICAL
- COMPARISON
- AZ_CONTEXT
- EVERGREEN

Rolling 14-main-post direction:

- NEWS + BREAKING: ~30–35%
- EXPLAINER: ~20–25%
- PRACTICAL: ~15–20%
- COMPARISON: ~10%
- AZ_CONTEXT: ~10%
- EVERGREEN / editorial education: ~10%

These are balancing signals, not mathematical quotas.

Never manufacture weak content to satisfy a percentage.

Stories are more tactical and are NOT required to follow this exact
14-main-post mix.

---

## 2. Content type definitions

### NEWS

A materially new verified development.

Examples:
- model or product launch
- acquisition
- important platform change
- major capability
- policy/company announcement

### BREAKING

A NEWS development where timing materially changes editorial value.

Do not use BREAKING as a synonym for "recent".

### EXPLAINER

A source-driven piece whose main value is understanding.

It should answer questions such as:

- What is it?
- How does it work?
- Why does it matter?
- What actually changed?
- What is misunderstood?

An explainer may originate from recent news but must add distinct value.

### PRACTICAL

Useful, actionable technology content.

Examples:
- how a new verified feature works
- real workflows
- product capabilities
- practical AI use cases
- useful settings/functions

Availability and platform/account scope must be verified.

### COMPARISON

A meaningful comparison between genuinely comparable products,
features, models or approaches.

Never compare incompatible metrics merely because the numbers look good.

### AZ_CONTEXT

Content whose main editorial value is direct relevance to Azerbaijan.

Examples:
- Azerbaijan technology/startup ecosystem
- regional availability
- local digital products
- meaningful local impact of a global development

Do not manufacture an Azerbaijan angle where none exists.

### EVERGREEN

Durable, source-driven educational/editorial content.

Evergreen is not filler.

It must provide real audience value and pass the same factuality standards.

---

## 3. Candidate metadata

Every NEW candidate created after Content Strategy V1 should record:

- discovered_at
- topic
- topic_bucket
- content_type
- topic_cluster
- primary_source
- supporting_source when useful
- score
- status
- proposed_format
- freshness_class
- freshness_deadline when relevant
- why_now
- audience_value
- duplicate_check
- verification_status
- rationale

Allowed topic_bucket:

AI
TECH
AZ_TECH
EXPERIMENT

Allowed freshness_class:

BREAKING
TODAY
THIS_WEEK
DURABLE

Allowed verification_status:

UNVERIFIED
PARTIAL
PASS
BLOCKED

Old queue entries do not need automatic migration.

---

## 4. Editorial Board

Morning Editorial creates:

social/research/daily/YYYY-MM-DD-editorial-board.md

The board should normally contain 4–6 serious candidates, not a huge
undifferentiated news list.

A healthy board may contain:

- strongest current NEWS/BREAKING candidate
- another NEWS candidate only when genuinely strong
- at least one strong EXPLAINER opportunity
- at least one PRACTICAL or EVERGREEN opportunity
- AZ_CONTEXT when credible
- COMPARISON when scope is valid

Do not force a category when there is no good candidate.

The editorial board is a decision pool, not a publication quota.

---

## 5. How non-news ideas are discovered

NullOne must remain source-driven.

EXPLAINER / PRACTICAL / EVERGREEN ideas should preferably emerge from:

1. important recent developments
2. official product documentation/release notes
3. recurring concepts currently relevant to the tech cycle
4. a useful follow-up angle to a recently covered topic
5. verified product capabilities people can actually use
6. meaningful audience questions or knowledge gaps
7. Azerbaijan-relevant technology context

Do not generate generic AI-tip filler detached from credible sources.

---

## 6. Derivative content rule

One strong event can produce more than one main piece only when each piece
creates distinct audience value.

Allowed:

NEWS:
"Feature X launched"

later:

EXPLAINER:
"What Feature X actually changes"

or:

PRACTICAL:
"How Feature X can be used"

Not allowed:

- same facts with a different headline
- repeated feed posts with no new insight
- mechanical content multiplication

Default maximum:

2 main Feed/Carousel pieces from the same topic_cluster within 7 days.

Exception:
a materially new development.

---

## 7. Main content cadence

Normal target:

2 main Feed/Carousel pieces per day.

The existing Draft Factory slots are opportunities, not quotas:

09:45
- primary opportunity for main piece #1

15:45
- primary opportunity for main piece #2

20:45
- recovery slot if fewer than 2 strong main pieces were prepared
- OR exceptional materially new BREAKING development

If 2 suitable main pieces have already been prepared/published:

20:45 must normally return NO_REPLY.

A third ordinary main post must NOT be created simply because a Factory
slot exists.

Exceptional breaking day:
maximum 3 main pieces.

Quality always overrides cadence.

---

## 8. Draft selection

Draft Factory selects by EDITORIAL VALUE, not freshness alone.

Consider together:

- editorial score
- audience usefulness
- consequence
- source confidence
- timeliness appropriate to content type
- distinctiveness
- topic-cluster saturation
- current rolling content mix
- format suitability
- today's existing production count

A strong EXPLAINER or PRACTICAL piece may outrank mediocre NEWS.

Do not artificially boost a weak candidate solely because the rolling
mix is imbalanced.

---

## 9. Format guidance

NEWS:
Feed or Story; Carousel only when explanation adds material value.

BREAKING:
Story first when appropriate; Feed for major developments.

EXPLAINER:
Carousel preferred when multiple concepts genuinely help understanding.

PRACTICAL:
Feed or Carousel.

COMPARISON:
Carousel preferred.

AZ_CONTEXT:
Feed or Carousel based on depth.

EVERGREEN:
Carousel when teaching benefits from structure.

Do not choose Carousel simply to produce more slides.

---

## 10. Story cadence — approved direction, not active yet

Target after the dedicated Story pipeline is implemented:

Normal:
3–5 Stories/day

Strong-news day:
4–6 Stories/day

Initial production-check windows:

10:30
13:30
18:30
21:30

A Story slot may produce zero Stories.

Story pipeline must be lightweight and should normally use Haiku +
deterministic rendering.

Do not activate Story automation until the main production bridge is stable.

---

## 11. Verification

Every publication-ready piece requires:

VERIFICATION: PASS

This applies equally to:

NEWS
BREAKING
EXPLAINER
PRACTICAL
COMPARISON
AZ_CONTEXT
EVERGREEN

Verify:

- exact numbers
- exact availability
- platform/geography/account scope
- company claims
- benchmark scope
- dates
- price
- rollout state
- planned vs available distinctions

If uncertain:
BLOCK instead of guessing.

---

## 12. NullOne editorial character

NullOne should feel:

fast
accurate
explained
useful
independent
source-driven

NullOne should not feel like:

an RSS feed
a press-release mirror
a generic AI tips page
a quota-driven content farm
