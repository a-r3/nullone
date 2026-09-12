# NullOne Editorial Packaging Contract V1

Status: PROPOSED
Scope: deterministic post/format decision only — SINGLE_POST vs CAROUSEL vs
STORY vs SKIP, plus real-photo/visual-evidence requirements.
Production impact: none. This is a repository decision contract plus a
reference pure evaluator (`nullone_packaging_policy.py`) and its tests.
Writing/merging it does not change production, the scheduler, the
Gateway, or any live automation. Wiring it into the live Draft
Factory/Morning/Story production pipeline is a separate, later,
explicitly-reviewed deployment step (not performed here).

## Purpose

Answer three deterministic questions for one already-scored, already
`READY` candidate:

1. Should this candidate be posted at all right now, or `SKIP`?
2. If posted, which packaging format — `SINGLE_POST`, `CAROUSEL`, or
   `STORY`?
3. What visual evidence does that packaging require — a real photo, a
   source screenshot, a faithful data visualization, editorial
   typography, or (rarely, and never as a substitute for required
   grounding) generated illustration?

This contract exists because current production output is biased toward
`CAROUSEL` by default rather than by editorial justification, visual
quality is inconsistent, and real photos are sometimes skipped in favor
of generic synthetic illustration even when the topic needed real
grounding. The fix is a deterministic evaluator with named inputs, named
outputs, and a fixed reason-code vocabulary — not vaguer prose guidance
layered on top of the existing prompt.

Nothing in this contract is delegated to an LLM. An LLM (Draft Factory)
may still gather the candidate's raw signals (does an official photo
exist? how many genuinely distinct beats does the story have?) — but
never decides the format itself; it calls the deterministic function in
`nullone_packaging_policy.py` (or, until that is wired into a live
runtime, applies this document's rules by hand exactly as written) and
reports the resulting `FORMAT_DECISION`.

This contract governs **packaging** only. It does not change:
- editorial candidate scoring or selection (`SCORING.md`,
  `CONTENT_STRATEGY.md`);
- the cadence/quota decision of *whether a new candidate search should
  happen right now* (`cadence-contract-v1.md`, #31/#32);
- the breaking-routing decision of *whether a candidate is Story-first*
  (`breaking-routing-policy-v1.md`, #34);
- verification (`VERIFICATION: PASS/BLOCKED` is an existing invariant,
  unconditionally required upstream of everything in this document —
  see "Relationship to existing repository material" below);
- publication, approval, or the two-stage human-approval boundary.

## Non-goals

- No new candidate-discovery logic.
- No change to Zernio/Telegram/renderer code paths.
- No AI deciding verification, audience value, or source grounding —
  those remain upstream judgments this contract *consumes* as input, it
  does not produce them.
- No production code, scheduler, or OpenClaw automation/cron change.
- No new external data dependency. Every input field below is either
  already-defined candidate metadata (`CONTENT_STRATEGY.md` §3) or a new
  field this document defines explicitly and requires the producer
  (Morning Editorial / Draft Factory / Breaking Radar) to assess from
  material already in hand (the primary source, the candidate's own
  claims, and whatever image search the existing "Visual hierarchy"
  rules in `visual-rules.md` already perform). Nothing here requires a
  new API, a new provider, or a new paid service.

## Relationship to existing repository material

This does not invent a parallel taxonomy where one already exists — it
reuses:

- **`CONTENT_TYPE`** (`CONTENT_STRATEGY.md` §2: `NEWS`, `BREAKING`,
  `EXPLAINER`, `PRACTICAL`, `COMPARISON`, `AZ_CONTEXT`, `EVERGREEN`) is
  the existing **editorial "why"** taxonomy (why this story matters).
  This contract does not replace it and does not require the candidate
  to drop it.
- **`CONTENT_SHAPE`** (new, defined below) is the **structural "how many
  distinct beats"** taxonomy this contract adds. It answers a different
  question than `CONTENT_TYPE`: a `NEWS` item can be `SINGLE_FACT` (one
  number) or a `MULTI_STEP_EXPLAINER` (mechanism); an `EXPLAINER` can be
  `SINGLE_FACT` (one clarifying number) or genuinely multi-beat. Format
  bias comes from conflating these — treating every `EXPLAINER`
  `CONTENT_TYPE` as automatically carousel-shaped. This contract fixes
  that by deciding format from `CONTENT_SHAPE`, never from `CONTENT_TYPE`
  alone.
- **`freshness_class`** (`CONTENT_STRATEGY.md` §3: `BREAKING`, `TODAY`,
  `THIS_WEEK`, `DURABLE`) is reused verbatim as this contract's
  `TIMELINESS` field — no renamed duplicate vocabulary.
- **`verification_status`** (`CONTENT_STRATEGY.md` §3: `UNVERIFIED`,
  `PARTIAL`, `PASS`, `BLOCKED`) and the existing `VERIFICATION: PASS`/
  `BLOCKED` production gate (`draft-factory.md`) remain the unconditional
  upstream gate. `BLOCKED` content never reaches this contract's
  evaluation at all — see "Evaluation order" below.
- **`audience_value`** (`CONTENT_STRATEGY.md` §3) is reused as this
  contract's low-value `SKIP` input — see "Whether to post at all".
- **`visual-rules.md`**'s existing visual hierarchy ("1. official/source
  visual, 2. useful screenshot, 3. NullOne editorial/data visualization,
  4. reusable owned asset, 5. generated imagery only when materially
  useful") is adopted unchanged as the base preference order for
  `VISUAL_STYLE`. This contract adds the missing piece: exactly *when*
  step 5 is forbidden outright rather than merely deprioritized, and
  exactly when the whole candidate must `SKIP` rather than fall back to
  it.
- **`draft-factory.md`**'s existing carousel guidance ("only when
  multiple slides materially improve understanding", "never
  article-screenshot carousels", "2–10 slides, normally 5–8") is kept
  and made precise, not replaced. See "Carousel eligibility" below for
  the exact, checkable version of "materially improve understanding".

If a later decision changes any of the source documents above, this
contract's *inputs* change value; its *evaluation logic* does not need
to change.

## Vocabulary — structured decision fields

All fields below are the exact names/values produced by
`nullone_packaging_policy.evaluate_packaging()`. Every enum is a
`frozenset` in the module; an unrecognized value fails closed
(`PackagingContractError`), it is never coerced to a default.

### Inputs (assessed by the candidate producer, not invented by this contract)

| Field | Values | Meaning |
|---|---|---|
| `content_type` | existing `CONTENT_TYPE` vocabulary | Editorial "why" (unchanged, passthrough only). |
| `content_shape` | `SINGLE_FACT`, `ANNOUNCEMENT`, `MULTI_STEP_EXPLAINER`, `COMPARISON`, `ROUNDUP`, `BREAKING_DEVELOPING`, `OPINION_ANALYSIS` | Structural "how many distinct beats" shape. See definitions below. |
| `timeliness` | `BREAKING`, `TODAY`, `THIS_WEEK`, `DURABLE` | = existing `freshness_class`. |
| `verification_status` | `PASS`, `BLOCKED` | Existing invariant gate. |
| `source_grounding` | `STRONG_PRIMARY`, `SECONDARY_CORROBORATED`, `WEAK_UNCONFIRMED` | How solid the primary-source evidence for the claim is — independent of image availability. |
| `audience_value` | `LOW`, `MEDIUM`, `HIGH` | Existing candidate metadata field. |
| `distinct_beat_count` | non-negative integer | Count of genuinely separable, source-grounded ideas/steps/items (not padding — see "Beat counting discipline"). |
| `depicts_real_world_subject` | boolean | True when the post is specifically about a named real entity, person, product, place, or event (as opposed to an abstract concept). |
| `still_developing` | boolean | True when facts may still change before the next natural production cycle (relevant to `BREAKING`/`BREAKING_DEVELOPING` only). |
| `has_official_or_source_image` | boolean | An actual photograph (official/press/product/journalistic) of the real subject exists. |
| `has_usable_screenshot` | boolean | A screenshot of the source page/UI/post exists and is legible/on-topic. |
| `image_on_topic` | boolean | The available image (of either kind above) actually depicts this candidate's subject, not a generic/unrelated stand-in. |
| `image_quality_ok` | boolean | The available image meets the technical/editorial bar in `visual-rules.md` (resolution, crop, legibility). |
| `data_visualization_possible` | boolean | The claim can be represented faithfully as a chart/stat card without misrepresenting the source (`visual-rules.md` "Fact wording"). |

### Outputs

| Field | Values | Meaning |
|---|---|---|
| `POST_DECISION` | `POST`, `SKIP` | Whether to post at all. |
| `CONTENT_SHAPE` | passthrough of input | Echoed for the report. |
| `ASSET_STRENGTH` | `STRONG_OFFICIAL`, `MODERATE`, `WEAK`, `NONE` | Derived visual-asset tier — see "Asset strength assessment". |
| `REAL_PHOTO_AVAILABLE` | `YES`, `NO` | A genuine, on-topic, quality-passing photo exists. |
| `REAL_PHOTO_REQUIRED` | `YES`, `NO` | This candidate may not substitute synthetic art for a real photo — see "Real-photo requirement rule". |
| `VISUAL_EVIDENCE_REQUIRED` | `YES`, `NO` | The claim must be visually substantiated (photo, screenshot, or faithful data viz) — typography alone is not enough. |
| `TEXT_DENSITY` | `LOW`, `MEDIUM`, `HIGH` | Derived from `distinct_beat_count` (1→LOW, 2→MEDIUM, ≥3→HIGH) — deterministic, not a style guess. |
| `TIMELINESS` | passthrough of input | Echoed for the report. |
| `FORMAT_DECISION` | `SINGLE_POST`, `CAROUSEL`, `STORY`, `SKIP` | The packaging decision. |
| `FORMAT_REASON` | fixed reason-code vocabulary (below) | Exact rule that fired. |
| `VISUAL_STYLE` | `REAL_PHOTO`, `SOURCE_SCREENSHOT`, `DATA_VISUALIZATION`, `EDITORIAL_TYPOGRAPHY`, `GENERATED_ILLUSTRATION_ALLOWED`, `GENERATED_ILLUSTRATION_FORBIDDEN`, `NONE` | What the render step must use. `NONE` only when `POST_DECISION == SKIP`. |
| `slide_count_recommendation` | integer or `null` | Only set when `FORMAT_DECISION == CAROUSEL`. |

### `CONTENT_SHAPE` definitions

- **`SINGLE_FACT`** — one headline + one number/claim; nothing else
  materially adds understanding. (`distinct_beat_count` should be `1`.)
- **`ANNOUNCEMENT`** — a company/product/policy announcement with one
  primary payload (a launch, a price, a policy change).
- **`MULTI_STEP_EXPLAINER`** — a mechanism or process with ≥2 logically
  separable beats (claim → mechanism → implication → limitation, etc.).
- **`COMPARISON`** — ≥2 genuinely comparable named items/metrics
  evaluated against the same measurement.
- **`ROUNDUP`** — a themed list of distinct items/news bits bundled into
  one post (a digest).
- **`BREAKING_DEVELOPING`** — an unfolding event whose full shape is not
  yet known and may still change before the next natural cycle.
- **`OPINION_ANALYSIS`** — a durable/evergreen analytical take, not tied
  to a specific fact-of-the-moment.

### Beat-counting discipline

`distinct_beat_count` must reflect genuinely separable, independently
source-grounded ideas — never the number of slides someone would like to
produce. Two disciplines apply:

1. **No padding.** If the true count is 2, it is `2`, even if the
   producer would "prefer" a longer carousel. Restating the same fact
   in different words is not a second beat. Producing a cover slide and
   a final/CTA slide are packaging overhead, not beats — do not count
   them.
2. **No inflation via sub-points.** A single claim broken into a
   headline and one supporting stat is still one beat, not two.

## Evaluation order

Evaluated strictly in this order; the first applicable rule decides
`POST_DECISION`/`FORMAT_DECISION` and no later rule is consulted.

### 0. Verification gate (existing invariant, unconditional)

`verification_status != PASS` → `POST_DECISION = SKIP`,
`FORMAT_DECISION = SKIP`, `FORMAT_REASON = VERIFICATION_BLOCKED`. This
contract never overrides `VERIFICATION: BLOCKED`.

### 1. Source-grounding gate

`source_grounding == WEAK_UNCONFIRMED` → `SKIP` /
`WEAK_SOURCE_GROUNDING`. A candidate can technically pass a narrow
`verification_status` check on the claims it does verify while still
resting on weak overall grounding for the story as a whole (e.g. a
single anonymous/unconfirmed report); this field captures that
distinction explicitly rather than silently posting a shaky story.

### 2. Audience-value gate

`audience_value == LOW` and `content_shape != BREAKING_DEVELOPING` →
`SKIP` / `LOW_AUDIENCE_VALUE`. (A genuinely still-developing breaking
item is never suppressed on `audience_value` alone — timeliness can
outrun a same-day value estimate; everything else with low estimated
value should not be posted merely because a slot is open.)

### 3. Asset-strength assessment

Deterministic from the four boolean asset inputs:

```
STRONG_OFFICIAL: has_official_or_source_image AND image_on_topic AND image_quality_ok
MODERATE:        (has_official_or_source_image OR has_usable_screenshot) AND image_on_topic
                 AND NOT STRONG_OFFICIAL
WEAK:            (has_official_or_source_image OR has_usable_screenshot) AND NOT image_on_topic
NONE:            neither image exists
```

`REAL_PHOTO_AVAILABLE = YES` iff `has_official_or_source_image AND
image_on_topic AND image_quality_ok` (a low-quality or off-topic "photo"
does not count as available).

### 4. Real-photo requirement rule

```
REAL_PHOTO_REQUIRED = YES iff
    content_shape in {ANNOUNCEMENT, BREAKING_DEVELOPING}
    OR depicts_real_world_subject == true
    OR timeliness == BREAKING
```

If `REAL_PHOTO_REQUIRED == YES` and `REAL_PHOTO_AVAILABLE == NO`, apply
the fallback ladder, in order:

1. `has_usable_screenshot AND image_on_topic` → proceed, `VISUAL_STYLE =
   SOURCE_SCREENSHOT`.
2. `data_visualization_possible` → proceed, `VISUAL_STYLE =
   DATA_VISUALIZATION`.
3. Neither → `POST_DECISION = SKIP`, `FORMAT_DECISION = SKIP`,
   `FORMAT_REASON = REAL_PHOTO_REQUIRED_NO_FALLBACK`, `VISUAL_STYLE =
   GENERATED_ILLUSTRATION_FORBIDDEN`.

**This is the hard rule the product problem statement asked for:
generated/synthetic illustration is never an acceptable substitute for a
required real photo. There is no step 4.** A weakly-grounded-looking
synthetic post about a real, named subject is exactly the failure mode
this contract exists to close off.

### 5. Visual-evidence requirement

```
VISUAL_EVIDENCE_REQUIRED = YES iff
    REAL_PHOTO_REQUIRED == YES
    OR content_shape in {COMPARISON, ANNOUNCEMENT}
```

When `YES` and neither a real photo, screenshot, nor data visualization
is available (`ASSET_STRENGTH == NONE` and `data_visualization_possible
== false`), the same fallback ladder as step 4 applies and reaches the
same `SKIP` outcome — a comparison or announcement claim never runs on
generated illustration alone.

### 6. Carousel eligibility ("materially improve understanding", made checkable)

```
CAROUSEL is allowed only if ALL of:
  content_shape not in {SINGLE_FACT, BREAKING_DEVELOPING}
  timeliness != BREAKING
  distinct_beat_count >= 3
```

Any one of these failing forbids `CAROUSEL` outright for this candidate,
regardless of how much the producer might prefer a longer post. This is
the direct fix for "the system defaults to carousel even when it isn't
the best format": a carousel is now only reachable through an explicit,
countable, multi-beat justification, never as a default.

### 7. Format decision (given `FORMAT_DECISION` not already forced to `SKIP`)

| `content_shape` | Carousel allowed? | Decision | `FORMAT_REASON` |
|---|---|---|---|
| `SINGLE_FACT` | never | `STORY` if `timeliness==BREAKING and still_developing`, else `SINGLE_POST` | `BREAKING_DEVELOPING_PREFERS_STORY` / `SINGLE_FACT_FITS_SINGLE_POST` |
| `BREAKING_DEVELOPING` | never | `STORY` if `still_developing`, else `SINGLE_POST` | `DEVELOPING_STORY_PREFERS_EPHEMERAL_FORMAT` / `BREAKING_CONFIRMED_FITS_SINGLE_POST` |
| `ANNOUNCEMENT` | if beats≥3 and not breaking | `CAROUSEL` (multi-feature) else `SINGLE_POST` (single payload) else `STORY` (breaking, ≥3 beats but carousel forbidden) | `MULTI_FEATURE_ANNOUNCEMENT_JUSTIFIES_CAROUSEL` / `SINGLE_PAYLOAD_ANNOUNCEMENT` / `BREAKING_TIMELINESS_FORBIDS_CAROUSEL` |
| `COMPARISON` | if beats≥3 and not breaking | `CAROUSEL` else `STORY` (≤2 items, or breaking) | `MULTI_ITEM_COMPARISON_JUSTIFIES_CAROUSEL` / `TWO_ITEM_COMPARISON_FITS_STORY` / `BREAKING_TIMELINESS_FORBIDS_CAROUSEL` |
| `ROUNDUP` | if beats≥3 and not breaking | `CAROUSEL` else `SINGLE_POST` (too few items to be a real roundup) or `STORY` (breaking) | `ROUNDUP_JUSTIFIES_CAROUSEL` / `INSUFFICIENT_DISTINCT_BEATS` / `BREAKING_TIMELINESS_FORBIDS_CAROUSEL` |
| `MULTI_STEP_EXPLAINER` | if beats≥3 and not breaking | `CAROUSEL` else `SINGLE_POST`, or `STORY` if breaking | `MULTI_BEAT_EXPLAINER_JUSTIFIES_CAROUSEL` / `INSUFFICIENT_DISTINCT_BEATS` / `BREAKING_TIMELINESS_FORBIDS_CAROUSEL` |
| `OPINION_ANALYSIS` | if beats≥3 | `CAROUSEL` else `SINGLE_POST` | `MULTI_BEAT_EXPLAINER_JUSTIFIES_CAROUSEL` / `INSUFFICIENT_DISTINCT_BEATS` |

### 8. Slide-count discipline (only when `FORMAT_DECISION == CAROUSEL`)

```
slide_count_recommendation = clamp(distinct_beat_count + 2, 4, 8)
```

The `+2` is the fixed cover + final/takeaway overhead already documented
in `visual-rules.md`'s carousel structures; it is never itself counted
as a "beat". The recommendation is clamped to `[4, 8]` (the existing
"normally 5–8" guidance, with 4 as an absolute floor below which a
carousel is not worth the swipe friction). The pre-existing hard render
ceiling of 10 slides in `draft-factory.md`/`visual-rules.md` is
unchanged and is never exceeded by this formula. If `distinct_beat_count`
exceeds 6, trim to the strongest 6 beats rather than inflating past 8
slides — padding a weak beat to hit a slide count is exactly the
over-synthetic failure mode this contract forbids.

### 9. Visual-style resolution (independent of `FORMAT_DECISION`)

Only reached when `POST_DECISION == POST` and no fallback branch above
already set `VISUAL_STYLE`. In `visual-rules.md`'s existing preference
order:

```
1. STRONG_OFFICIAL & has_official_or_source_image → REAL_PHOTO
2. (STRONG_OFFICIAL or MODERATE) & has_usable_screenshot → SOURCE_SCREENSHOT
3. data_visualization_possible → DATA_VISUALIZATION
4. VISUAL_EVIDENCE_REQUIRED == NO → EDITORIAL_TYPOGRAPHY
5. otherwise → GENERATED_ILLUSTRATION_ALLOWED
```

Step 5 is reachable only when `VISUAL_EVIDENCE_REQUIRED == NO` (i.e. it
can never be reached for a candidate that required a real photo,
screenshot, or data visualization — those candidates were already routed
to the step-4 fallback ladder or to `SKIP`). Generated illustration is
therefore always the last resort for genuinely evidence-light content
(e.g. an abstract `OPINION_ANALYSIS` piece), never a substitute for
missing grounding on a real-world claim.

## Whether to post at all — summary

A candidate reaches `POST_DECISION = SKIP` in exactly these cases (no
others):

1. `verification_status != PASS` (existing invariant).
2. `source_grounding == WEAK_UNCONFIRMED`.
3. `audience_value == LOW` and the story is not a genuinely still-developing
   breaking item.
4. A real photo (or visual evidence) is required, none is available, and
   no source-grounded fallback (screenshot / faithful data visualization)
   exists — generated illustration is never used to paper over this.

"No suitable candidate at all" (nothing to evaluate) is out of this
contract's scope — that is the existing cadence/candidate-availability
concern (`cadence-contract-v1.md`), not a packaging decision.

## Worked examples

Full machine-checked fixtures live in
`tests/fixtures/packaging_contract_v1_examples.json`; this table is the
human-readable summary of that same file (kept in sync by
`tests/test_packaging_contract_fixture.py`).

| # | Scenario | Key inputs | `FORMAT_DECISION` | `FORMAT_REASON` | `VISUAL_STYLE` | `POST_DECISION` |
|---|---|---|---|---|---|---|
| 1 | Breaking news, strong real imagery, confirmed | `SINGLE_FACT`, `BREAKING`, `still_developing=false`, official photo on-topic/quality-ok | `SINGLE_POST` | `SINGLE_FACT_FITS_SINGLE_POST` | `REAL_PHOTO` | `POST` |
| 2 | Multi-step explainer, 5 real beats | `MULTI_STEP_EXPLAINER`, `THIS_WEEK`, `distinct_beat_count=5` | `CAROUSEL` (7 slides) | `MULTI_BEAT_EXPLAINER_JUSTIFIES_CAROUSEL` | per assets | `POST` |
| 3 | Weakly sourced topic | `source_grounding=WEAK_UNCONFIRMED` | `SKIP` | `WEAK_SOURCE_GROUNDING` | `NONE` | `SKIP` |
| 4 | Two-model price comparison, urgent | `COMPARISON`, `BREAKING`, `distinct_beat_count=2` | `STORY` | `BREAKING_TIMELINESS_FORBIDS_CAROUSEL` | per assets | `POST` |
| 5 | Product announcement with official photo | `ANNOUNCEMENT`, official photo available, `depicts_real_world_subject=true` | `SINGLE_POST` | `SINGLE_PAYLOAD_ANNOUNCEMENT` | `REAL_PHOTO` (never generated illustration) | `POST` |
| 6 | Same announcement, no photo/screenshot/data-viz | as #5 but no assets at all | `SKIP` | `REAL_PHOTO_REQUIRED_NO_FALLBACK` | `GENERATED_ILLUSTRATION_FORBIDDEN` | `SKIP` |
| 7 | Explainer with only 2 real beats (old system would carousel this) | `MULTI_STEP_EXPLAINER`, `distinct_beat_count=2` | `SINGLE_POST` | `INSUFFICIENT_DISTINCT_BEATS` | per assets | `POST` |
| 8 | 5-item weekly roundup | `ROUNDUP`, `distinct_beat_count=5`, `TODAY` | `CAROUSEL` (7 slides) | `ROUNDUP_JUSTIFIES_CAROUSEL` | per assets | `POST` |
| 9 | Low audience-value durable filler | `audience_value=LOW`, `OPINION_ANALYSIS` | `SKIP` | `LOW_AUDIENCE_VALUE` | `NONE` | `SKIP` |
| 10 | Still-developing breaking item, no confirmed photo yet, screenshot exists | `BREAKING_DEVELOPING`, `still_developing=true`, no official photo, screenshot on-topic | `STORY` | `DEVELOPING_STORY_PREFERS_EPHEMERAL_FORMAT` | `SOURCE_SCREENSHOT` | `POST` |

## Acceptance

This document is accepted when:
1. `nullone_packaging_policy.py` implements every rule above exactly,
   fails closed on malformed/unrecognized input, and performs no I/O.
2. `tests/test_packaging_policy.py` and
   `tests/test_packaging_contract_fixture.py` pass, covering at minimum
   the 10 worked examples above plus the edge cases in "Regression
   coverage notes" in the test file.
3. `tests/run_offline.py` still passes in full (no existing behavior
   regressed).
4. No production, scheduler, Gateway, or `~/.openclaw` file is touched.

Wiring this evaluator into a live production workflow (Draft Factory,
Morning Editorial, or a future Story production path) is explicitly out
of scope for this document and requires its own separate, reviewed
follow-up — consistent with how #31 (contract) and #32 (controller
wiring) were kept separate for the cadence decision.
