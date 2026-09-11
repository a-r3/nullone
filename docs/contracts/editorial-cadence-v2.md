# NullOne Editorial Cadence V2 — Reviewed Repository Contract ONLY

Status: PROPOSED / REVIEWED CONTRACT ONLY — NOT IMPLEMENTED, NOT DEPLOYED.
Scope: deterministic editorial-cadence policy contract only.
Production impact: none. This document changes no scheduler, controller,
prompt, automation, ledger, draft, publication, or notification behavior.

Baseline inspected: `10ca76f50962a1f125bd2f1222e02aab2ec8fae8`
(reconciled with `workspace/social/CONTENT_STRATEGY.md`,
`docs/contracts/cadence-contract-v1.md`,
`docs/contracts/breaking-routing-policy-v1.md`,
`docs/deployment/79-story-production-integration.md`,
`docs/deployment/80-breaking-radar-production-integration.md`).

## 0. Authority and non-goals

V2 is a policy successor to the V1 cadence contract
(`docs/contracts/cadence-contract-v1.md`), not a silent replacement of
existing safety rules. The following are preserved unchanged and are
non-negotiable by any V2 arithmetic:

- quality overrides quota (`quality > quota`);
- `VERIFICATION: PASS` required for every publication-ready piece;
- two-stage human approval (Zernio draft → Telegram preview →
  approve/revise/reject → second final publication confirmation);
- no blind publication; `PREPARE_*` is never `PUBLISH`;
- pending/backpressure accounting (consequential pending suppresses
  repeated preparation; `UNKNOWN`/`CHECK_REQUIRED`/in-flight states
  count as pending, never as empty slots);
- `UNKNOWN` fail-closed behavior until read-only reconciliation;
- duplicate/topic-saturation rules, including the derivative-content
  rule (default maximum 2 main pieces from the same `topic_cluster`
  within 7 days unless a materially new development exists);
- human approval/revision/reject boundaries (the `texbrif-approval`
  agent owns the human publication boundary).

V2 does NOT, by itself:

- implement or modify any production cadence/controller runtime;
- change OpenClaw automations, schedules, prompts, or secrets;
- deploy anything, force-run Morning/Story/Radar, or publish;
- send Telegram, call Zernio, or create synthetic production state;
- invent a new database, a second news-monitoring system, or new
  numeric quotas disguised as analytics.

V2 reuses reviewed semantics where they exist: V1 format accounting
and state-source precedence (manifest > publish ledger > queue >
board), the breaking-routing severity vocabulary (`NORMAL` /
`MATERIAL_BREAKING` / `EXCEPTIONAL_BREAKING`), the Morning structured
handoff (`nullone.editorial-candidate-handoff.v1`), and the Radar
scan/commit/consume authority. Where V2 deliberately diverges from V1
— independent surface evaluation replacing the single combined
recommendation with main-before-Story precedence — §4 states the
divergence explicitly.

## 1. Daily activity model (guidance, never blind quota)

All targets below are editorial guidance for gap computation, never
forced publication quotas. Not reaching a target never forces weak
content; exceeding a minimum never forbids a genuinely stronger
verified candidate. `QUIET_DAY_WEAK_FILLER=FORBIDDEN`.

Reviewed min/max baseline (both modeled; max is a hard ceiling, never
a quota obligation):

| Day profile | MAIN min | MAIN max | STORY min | STORY max |
|---|---|---|---|---|
| NORMAL | 2 | 2 | 3 | 5 |
| STRONG_NEWS | 2 | 2 | 4 | 6 |
| EXCEPTIONAL | 2 | 3 | 4 | 6 |
| QUIET | 1 | 2 | 2 | 4 |

Semantics per surface:

- `published < min` → `AUDIENCE_GAP`;
- `min <= published < max` → `TARGET_BAND_REACHED` /
  optional capacity (a high-quality verified ordinary candidate MAY
  be prepared while inside the band when no pending backpressure,
  spacing allows, the candidate is non-duplicate, and the surface
  max is not reached — optional quality capacity, never a quota
  obligation);
- `published >= max` → `TARGET_MAX_REACHED` (hard stop; neither
  MATERIAL nor EXCEPTIONAL may exceed the configured hard maximum
  without a future explicit policy revision).

For MAIN, ordinary NORMAL/STRONG_NEWS evaluation normally stops at
max=2. The third MAIN is permitted only when the day profile or
signal establishes EXCEPTIONAL, `opportunity ==
EXCEPTIONAL_BREAKING`, `published MAIN < 3`, and all normal
quality/safety gates pass.

For STORY, NORMAL may continue from 3 up to 5 when strong verified
candidates exist; STRONG_NEWS / EXCEPTIONAL may continue up to 6;
QUIET may continue up to 4 only when useful verified Story
candidates exist.

### NORMAL DAY

- main target: 2
- Story target band: 3–5

### STRONG-NEWS DAY

- main target: 2
- Story target band: 4–6

### EXCEPTIONAL BREAKING DAY

- maximum main: 3
- Story maximum: 6 unless a future explicit policy revision changes it

### QUIET DAY

- 1 strong main is acceptable when no second candidate passes quality;
- 2–4 useful Stories are acceptable;
- never manufacture a weak second main or weak Story merely for
  activity.

### Normal 2-main editorial preference

Prefer one fast/current main piece plus one deeper/useful main piece.
This often means `FEED + CAROUSEL`, but MUST NOT become "exactly one
Feed and exactly one Carousel every day." Format is chosen by
editorial fit:

| Content type | Format guidance |
|---|---|
| NEWS | usually FEED |
| EXPLAINER | often CAROUSEL |
| PRACTICAL | FEED or CAROUSEL |
| COMPARISON | usually CAROUSEL |
| AZ_CONTEXT | based on depth |
| EVERGREEN | based on teaching value |

Do not choose Carousel simply to produce more slides.

## 2. Opportunity windows (preparation vs publication)

V2 separates PREPARATION opportunities from PUBLICATION authorization.
Publication still requires Rauf's second explicit confirmation; no
deterministic system may auto-publish merely because a window opened.

Current reviewed preparation rhythm (Asia/Baku):

| Kind | Times |
|---|---|
| Morning Editorial | 08:30 |
| Main preparation | 09:45 primary #1 · 15:45 primary #2 · 20:45 recovery / exceptional breaking only |
| Story checks | 10:30 · 13:30 · 18:30 · 21:30 |
| Intraday Radar | 11:30 · 14:30 · 17:30 · 20:30 · 23:30 |

Initial audience-facing publish WINDOWS (editorial guidance only):

- MAIN WINDOW #1: approximately 10:30–12:30 Asia/Baku;
- MAIN WINDOW #2: approximately 17:00–19:30 Asia/Baku;
- RECOVERY: approximately 20:45–22:30, only if the normal main target
  is not already satisfied with quality content, or an exceptional
  development justifies it.

Windows are guidance for later analytics, not triggers. A window
opening with no quality candidate yields `NO_ACTION`. A missed window
is not an obligation (coalescing, as in V1).

## 3. Independent surface evaluation (no cross-format blocking)

V1 ends in one recommendation with main-before-Story precedence. V2
replaces that combined rule with INDEPENDENT SURFACE evaluation.

Deterministic input: `surface = MAIN | STORY` (or equivalent separate
evaluators — one evaluation per surface, never one blended verdict).

Each surface independently evaluates:

- audience-facing count (`published_today`);
- pending/review load (`pending`);
- last publication time (spacing);
- candidate availability for that surface;
- spacing since last audience-facing publication of that surface;
- duplicate/topic saturation;
- quality (`VERIFICATION: PASS` + surface scoring gates);
- requested opportunity type (normal / breaking severity / recovery).

Rules:

- A Story opportunity must not be suppressed merely because
  `main_gap=true`.
- A main opportunity must not be suppressed merely because
  `story_gap=true`.
- No cross-format substitution: 2 FEED posts do not satisfy Story
  activity; 5 Stories do not satisfy main activity.

`MAIN_STORY_EVALUATION=INDEPENDENT`. This is the one deliberate V2
divergence from V1 §"Deterministic evaluation order" step 3–4; all
accounting inputs (counters, pending definition, spacing, quiet
hours) are inherited unchanged.

## 4. Audience activity vs backpressure (truthful distinction)

`published_today` (audience-facing) and `pending` (drafts awaiting
review) must never be collapsed into a single "activity satisfied"
claim. Pending drafts count for OPERATOR BACKPRESSURE (do not flood
Rauf with previews) but do not satisfy the public activity target.

Observability distinguishes exactly four audience states per
surface (reaching the minimum does not mean the target band is
exhausted; never claim the channel is fully saturated merely because
min is reached):

| Audience status | Meaning |
|---|---|
| `AUDIENCE_GAP` | `published_today` below `target_min` and no pending work blocks a new preparation. |
| `AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW` | `published_today` below `target_min`, but pending review work suppresses new preparation. The public target is truthfully UNMET. |
| `TARGET_BAND_REACHED` | `target_min <= published_today < target_max`. The public minimum is reached; the maximum is not. Further preparation is optional capacity, not obligation. |
| `TARGET_MAX_REACHED` | `published_today >= target_max`. The surface is full for today. |

Backpressure remains a separate outcome (`BLOCKED_PENDING_REVIEW`),
not part of the audience status. Example: `published=3`,
`pending=1`, NORMAL Story target 3–5 → `audience_status=
TARGET_BAND_REACHED` with outcome `BLOCKED_PENDING_REVIEW`:
minimum reached, maximum not reached, no new draft because pending
review exists.

Example: `story_published_today=1`, `story_pending=3`,
`story_target_min=3` → the system must NOT create more drafts
(backpressure), but must report `AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW`,
never `TARGET_MET`.

`AUDIENCE_GAP_VS_PENDING_DISTINGUISHED=YES`.

## 5. Unified Story candidate sources (same-day candidate view)

Scheduled Story must NOT permanently remain Morning-snapshot-only.
V2 defines a SAME-DAY candidate view that may contain:

- A. Morning Editorial handoff (`nullone.editorial-candidate-handoff.v1`,
  `story_eligible` flags);
- B. verified committed Intraday Radar developments (committed
  `nullone.breaking-radar-handoff.v1` envelopes consumed through the
  receipt-authoritative path — never Markdown parsing);
- C. existing viable READY/DURABLE queue candidates.

No second news-monitoring system is created. Reused Radar semantics:

| Severity | Routing into the same-day pool |
|---|---|
| NORMAL | ordinary cadence pool; eligible for the NEXT normal Story/main opportunity, not discarded for happening after 08:30. |
| MATERIAL_BREAKING | immediate Story opportunity, still human-reviewed. |
| EXCEPTIONAL_BREAKING | immediate Story opportunity plus an optional justified main opportunity. |

`INTRADAY_RADAR_NORMAL_TO_CADENCE=YES`.
`MATERIAL_BREAKING_STORY_IMMEDIATE=YES`.

Every candidate retains exact provenance/source identity
(`source_class` + evidence refs); a Morning candidate, a Radar
development, and a durable reserve candidate are never merged
indistinguishably. No Markdown inference is performed when a
validated structured handoff exists.

## 6. Durable reserve / quiet-day behavior (no new database)

V2 invents no new database; it uses the existing candidate
queue/state model. A viable reserve candidate is an existing
candidate that is:

- READY;
- `VERIFICATION: PASS`;
- not expired (freshness respected; DURABLE freshness class
  preferred for reserve use);
- not consumed/reviewed/published/rejected/superseded;
- appropriate freshness — especially DURABLE / EXPLAINER /
  PRACTICAL / EVERGREEN / valid AZ_CONTEXT;
- not blocked by topic saturation.

Candidate priority ladder for an ordinary opportunity:

1. material fresh development when timing matters;
2. strongest current same-day candidate;
3. useful recent explainer/practical opportunity;
4. strongest verified durable READY candidate;
5. `NO_ACTION`.

Never descend to weak content merely to fill activity.
`DURABLE_QUEUE_FALLBACK=YES`.

## 7. Story content roles

Stories are not restricted to headline news. Allowed verified Story
roles:

- CURRENT_UPDATE
- MAIN_COMPANION
- MINI_EXPLAINER
- PRACTICAL_POINT
- KEY_NUMBER
- AZ_CONTEXT
- BREAKING_UPDATE

No generic filler. Cross-format reuse of one event is allowed only
when the audience role differs:

- allowed: Story = immediate concise update; Carousel = deeper
  explanation (distinct incremental value, subject to
  derivative/topic limits);
- not allowed: the same exact facts repeated in Story and Feed with
  no incremental value (duplicate suppression).

## 8. Intraday Radar rule

Morning is the day's initial plan, NOT a frozen day-long candidate
universe. After Morning, 11:30 / 14:30 / 17:30 / 20:30 / 23:30 Radar
observations may introduce new verified developments:

- NORMAL → joins the ordinary same-day candidate pool;
- MATERIAL_BREAKING → may bypass normal timing for Story
  preparation;
- EXCEPTIONAL_BREAKING → may bypass normal timing for Story and
  optionally one justified main.

None bypass verification, dedup, load safety, human approval, or the
second final publish confirmation. Late 23:30 NORMAL developments
normally roll into next-day consideration; late MATERIAL/EXCEPTIONAL
may produce a review opportunity, but never blind publication.

## 9. Observability (metrics, not quotas)

Per Baku day, for later analytics (windows may later change on
evidence; initial times are not permanent growth truth):

- `main_published`, `feed_published`, `carousel_published`,
  `story_published`;
- `main_pending`, `story_pending`;
- `morning_source_count`, `radar_source_count`,
  `durable_source_count`.

Per opportunity, report the outcome (Story and main equivalents):

- `PREPARED`
- `NO_QUALITY_CANDIDATE`
- `BLOCKED_PENDING_REVIEW`
- `RECENT_ACTIVITY`
- `DUPLICATE_SUPPRESSED`
- `SOURCE_UNAVAILABLE`
- `TARGET_BAND_REACHED` (no gap pressure; valid quiet `NO_ACTION`)
- `TARGET_MAX_REACHED` (surface full; valid quiet `NO_ACTION`)

Also track per publication: local time, format, content_type,
topic_cluster, source class, and where available reach, shares,
saves, and profile actions.

## 10. Machine-readable shape (contract only, no runtime)

Schema: `nullone.cadence-contract.v2` (request) /
`nullone.cadence-contract-examples.v2` (fixtures). No decision
function exists in this repository; a future implementation issue
owns it. This contract defines signature and behavior only.

### Request (per-surface evaluation)

```json
{
  "schema": "nullone.cadence-contract.v2",
  "surface": "MAIN",
  "opportunity": "NORMAL",
  "day_profile": "NORMAL",
  "now": "2026-09-06T11:00:00+04:00",
  "timezone": "Asia/Baku",
  "config": {"min_spacing_minutes": 120},
  "main_load": {
    "published_today": 1,
    "pending": 0,
    "last_published_at": "2026-09-06T09:50:00+04:00"
  },
  "story_load": {
    "published_today": 4,
    "pending": 0,
    "last_published_at": "2026-09-06T10:35:00+04:00"
  },
  "candidate": {
    "quality_available": true,
    "verification": "PASS",
    "source_class": "MORNING_HANDOFF",
    "provenance": "morning:2026-09-06/candidate-001",
    "incremental_value": true,
    "content_type": "NEWS",
    "format": "FEED"
  },
  "signal": {"severity": "NORMAL", "exceptional_development": false}
}
```

`config.min_spacing_minutes` is the versioned anti-burst spacing for
the requested surface (defaults live in fixture `default_config`:
MAIN 120, STORY 45 — guidance, not immutable policy).
`load.last_published_at` is the offset-aware ISO8601 timestamp of the
last audience-facing publication of that surface, or `null` when
there is none today. `request.now` remains authoritative; the
evaluator never invents the current time.

Spacing rule: if `last_published_at` is not null and `now -
last_published_at < min_spacing_minutes`, the surface is held with
`NO_ACTION` / `RECENT_ACTIVITY` — except that MATERIAL_BREAKING and
EXCEPTIONAL_BREAKING opportunities bypass the spacing hold for the
requested surface (breaking timing policy governs instead). A null
`last_published_at` never holds.

Enums: `surface` ∈ {`MAIN`, `STORY`}; `opportunity` ∈ {`NORMAL`,
`MATERIAL_BREAKING`, `EXCEPTIONAL_BREAKING`, `RECOVERY`};
`day_profile` ∈ {`NORMAL`, `STRONG_NEWS`, `EXCEPTIONAL`, `QUIET`};
`verification` ∈ {`UNVERIFIED`, `PARTIAL`, `PASS`, `BLOCKED`};
`source_class` ∈ {`MORNING_HANDOFF`, `RADAR_COMMITTED`,
`DURABLE_QUEUE`, `NONE`}.

Targets resolved from `day_profile` (§1 table): NORMAL → main
2/2, story 3/5; STRONG_NEWS → main 2/2, story 4/6; EXCEPTIONAL →
main 2/3, story 4/6; QUIET → main 1/2, story 2/4.

### `day_profile` authority (boundary only, no resolver)

- The cadence evaluator does NOT let a writer/model choose
  `day_profile`.
- NORMAL is the fail-closed default profile.
- STRONG_NEWS / QUIET / EXCEPTIONAL require a versioned
  authoritative upstream deterministic/profile signal (resolver
  owned by a future implementation issue — not this contract).
- EXCEPTIONAL_BREAKING assessment may establish exceptional
  treatment only through the existing reviewed breaking severity
  contract (`docs/contracts/breaking-routing-policy-v1.md`).
- If profile authority is absent or invalid, fall back to NORMAL
  rather than guessing. This contract defines the boundary only;
  it does not implement the profile resolver.

### Response

```json
{
  "schema": "nullone.cadence-contract.v2",
  "recommendation": "PREPARE_MAIN",
  "outcome": "PREPARED",
  "audience_status": "AUDIENCE_GAP",
  "permitted_action": "CANDIDATE_SEARCH_AND_PREPARE"
}
```

`recommendation` ∈ {`PREPARE_MAIN`, `PREPARE_STORY`, `NO_ACTION`};
`outcome` ∈ {`PREPARED`, `NO_QUALITY_CANDIDATE`,
`BLOCKED_PENDING_REVIEW`, `RECENT_ACTIVITY`, `DUPLICATE_SUPPRESSED`,
`SOURCE_UNAVAILABLE`, `TARGET_BAND_REACHED`, `TARGET_MAX_REACHED`};
`audience_status` ∈ {`AUDIENCE_GAP`,
`AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW`, `TARGET_BAND_REACHED`,
`TARGET_MAX_REACHED`}; `permitted_action` ∈ {`NONE`,
`CANDIDATE_SEARCH_AND_PREPARE`} and is never `PUBLISH`.

### Deterministic per-surface evaluation order

For the requested `surface` only (the other surface's load is
reported context, never a blocking input):

1. Resolve `target_min`/`target_max` from `day_profile` (§1 table).
   Compute position: gap (`published < min`), band (`min <=
   published < max`), or max (`published >= max`).
2. Compute `audience_status`: `AUDIENCE_GAP` if gap and
   `pending == 0`; `AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW` if gap
   and `pending > 0`; `TARGET_BAND_REACHED` if band;
   `TARGET_MAX_REACHED` if max.
3. If `source_class == NONE` (or no candidate record): `NO_ACTION` /
   `SOURCE_UNAVAILABLE`.
4. If `verification != PASS` or `quality_available == false`:
   `NO_ACTION` / `NO_QUALITY_CANDIDATE` — regardless of gap, band,
   opportunity, or restart state (`quality > quota` is absolute).
5. If `incremental_value == false` (same facts, no distinct audience
   role): `NO_ACTION` / `DUPLICATE_SUPPRESSED`.
6. If position is max: `NO_ACTION` / `TARGET_MAX_REACHED`. The hard
   maximum is never exceeded — breaking bypasses the minimum, never
   the maximum.
7. If the spacing hold applies (non-null `last_published_at` within
   `min_spacing_minutes` of `now`) and `opportunity` is neither
   MATERIAL_BREAKING nor EXCEPTIONAL_BREAKING: `NO_ACTION` /
   `RECENT_ACTIVITY`.
8. If `opportunity == RECOVERY` and position is not gap and no
   `exceptional_development`: `NO_ACTION` with `TARGET_BAND_REACHED`
   or `TARGET_MAX_REACHED` matching the position.
9. If `pending(surface) > 0`: `NO_ACTION` /
   `BLOCKED_PENDING_REVIEW` (audience status stays truthful per
   step 2).
10. If surface is MAIN and position is band and `opportunity !=
    EXCEPTIONAL_BREAKING` (or exceptional justification absent):
    `NO_ACTION` / `TARGET_BAND_REACHED`. The ordinary second main
    slot stops at the band; only a justified exceptional third main
    proceeds.
11. Else `PREPARE_MAIN` (surface MAIN) or `PREPARE_STORY` (surface
    STORY) / `PREPARED` — covering the gap case, the optional
    in-band Story capacity case, the breaking-bypasses-minimum case,
    and the justified exceptional MAIN #3 case.

Cross-surface loads never appear in steps 3–9. `PREPARE_*` remains
permission to search for and prepare a candidate subject to scoring,
`VERIFICATION: PASS`, dedup, load safety, human approval, and the
second final confirmation. `PREPARE_* != PUBLISH`, unconditionally.

## 11. Worked examples (deterministic)

Machine-readable fixtures:
`tests/fixtures/editorial_cadence_v2_examples.json` (18 cases,
items 1–18 above). Validated offline by
`tests/test_editorial_cadence_v2_contract.py`, which checks fixture
shape/hygiene and replays a test-local reference evaluator over the
fixtures — no production runtime is added or changed.

1. `main_opportunity_strong_feed_candidate` — 1 main published, 0
   main pending, 4 Stories published, MAIN opportunity, strong
   verified Feed candidate → `PREPARE_MAIN` / `PREPARED`.
2. `story_opportunity_verified_candidate` — 2 main published, 2
   Stories published, STORY opportunity, verified Story candidate
   → `PREPARE_STORY` / `PREPARED` (main count does not block it).
3. `story_backpressure_audience_unmet` — 0 Stories published, 3 Story
   drafts pending → `NO_ACTION` / `BLOCKED_PENDING_REVIEW` with
   `audience_status=AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW`
   (backpressure respected, public target truthfully unmet).
4. `radar_normal_joins_same_day_pool` — Morning has no Story
   candidate; 14:30 Radar commits a verified NORMAL Story-suitable
   development; 18:30 Story check → `PREPARE_STORY` / `PREPARED`
   with `source_class=RADAR_COMMITTED`.
5. `material_breaking_outranks_morning` — Morning candidate exists;
   newer Radar MATERIAL_BREAKING development arrives → `PREPARE_STORY`
   / `PREPARED` for the material development (timing bypass, same
   safety gates).
6. `recovery_no_exceptional_no_action` — 2 main published, 20:45
   RECOVERY opportunity, no exceptional development → `NO_ACTION` /
   `TARGET_MAX_REACHED`.
7. `recovery_durable_carousel_fills_gap` — 1 main published, 20:45
   RECOVERY opportunity, strong verified DURABLE carousel candidate
   → `PREPARE_MAIN` / `PREPARED`.
8. `story_then_distinct_carousel_allowed` — same event already used
   as Story; later Carousel adds genuinely distinct explanation
   (`incremental_value=true`) → `PREPARE_MAIN` / `PREPARED`,
   subject to derivative/topic limits.
9. `story_then_repeat_feed_suppressed` — same event already used as
   Story; Feed repeats the same facts only
   (`incremental_value=false`) → `NO_ACTION` /
   `DUPLICATE_SUPPRESSED`.
10. `no_fresh_no_durable_no_action` — no strong fresh candidate and
    no viable durable reserve → `NO_ACTION` /
    `SOURCE_UNAVAILABLE` despite the activity gap.

11. `story_band_optional_capacity` (A) — NORMAL day,
    `story_published=3`, `story_pending=0`, strong verified Story
    candidate → `PREPARE_STORY` / `PREPARED` with
    `audience_status=TARGET_BAND_REACHED` (within the 3–5 band, not
    a gap; optional capacity exercised).
12. `story_max_reached` (B) — NORMAL day, `story_published=5` →
    `NO_ACTION` / `TARGET_MAX_REACHED` even with a strong
    candidate.
13. `strong_news_band_optional_capacity` (C) — STRONG_NEWS,
    `story_published=4`, strong verified candidate →
    `PREPARE_STORY` / `PREPARED` with
    `audience_status=TARGET_BAND_REACHED` (within the 4–6 band).
14. `exceptional_main_third` (D) — MAIN `published=2`,
    EXCEPTIONAL day, `opportunity=EXCEPTIONAL_BREAKING`, justified
    main candidate → `PREPARE_MAIN` / `PREPARED` for main #3.
15. `ordinary_main_band_stop` (E) — MAIN `published=2`, ordinary
    NORMAL candidate → `NO_ACTION` / `TARGET_BAND_REACHED` (or
    `TARGET_MAX_REACHED` where max is 2).
16. `material_breaking_after_min` (F) — Story minimum already met,
    new MATERIAL_BREAKING candidate, Story count below max →
    `PREPARE_STORY` / `PREPARED` (breaking bypasses the minimum,
    never the maximum).
17. `story_inside_spacing_held` — ordinary NORMAL Story,
    `last_published_at` within `min_spacing_minutes` of `now` →
    `NO_ACTION` / `RECENT_ACTIVITY`.
18. `story_outside_spacing_eligible` — same shape with
    `last_published_at` outside the spacing window → eligible
    (`PREPARE_STORY` / `PREPARED`) when all other gates pass.

## 12. Exit rule

This contract can be accepted when: versioned per-surface
inputs/outputs are documented; MAIN/STORY evaluation is
demonstrably independent; audience vs pending is truthfully
distinguished; same-day sources carry exact provenance; the durable
ladder, Story roles, Radar rule, and observability are defined;
worked examples exist machine-readable; and no production/runtime
code was changed. Acceptance closes no implementation issue and
authorizes no deployment. A future implementation, review, and
controlled deployment remain separately required.
