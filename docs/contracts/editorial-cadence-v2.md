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

Observability distinguishes exactly three audience states per
surface:

| Audience status | Meaning |
|---|---|
| `AUDIENCE_GAP` | `published_today` below target and no pending work blocks a new preparation. |
| `AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW` | `published_today` below target, but pending review work suppresses new preparation. The public target is truthfully UNMET. |
| `TARGET_MET` | `published_today` meets today's guidance. |

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
- `TARGET_MET` (no gap; valid quiet `NO_ACTION`)

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
  "main_load": {"published_today": 1, "pending": 0},
  "story_load": {"published_today": 4, "pending": 0},
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

Enums: `surface` ∈ {`MAIN`, `STORY`}; `opportunity` ∈ {`NORMAL`,
`MATERIAL_BREAKING`, `EXCEPTIONAL_BREAKING`, `RECOVERY`};
`day_profile` ∈ {`NORMAL`, `STRONG_NEWS`, `EXCEPTIONAL`, `QUIET`};
`verification` ∈ {`UNVERIFIED`, `PARTIAL`, `PASS`, `BLOCKED`};
`source_class` ∈ {`MORNING_HANDOFF`, `RADAR_COMMITTED`,
`DURABLE_QUEUE`, `NONE`}.

Targets resolved from `day_profile` (§1): NORMAL → main 2, story
3–5; STRONG_NEWS → main 2, story 4–6; EXCEPTIONAL → main max 3,
story max 6; QUIET → main 1 acceptable, story 2–4 acceptable. The
minimum used for gap computation: main {NORMAL:2, STRONG_NEWS:2,
EXCEPTIONAL:2, QUIET:1}; story {NORMAL:3, STRONG_NEWS:4,
EXCEPTIONAL:4, QUIET:2}.

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
`SOURCE_UNAVAILABLE`, `TARGET_MET`}; `audience_status` ∈
{`AUDIENCE_GAP`, `AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW`,
`TARGET_MET`}; `permitted_action` ∈ {`NONE`,
`CANDIDATE_SEARCH_AND_PREPARE`} and is never `PUBLISH`.

### Deterministic per-surface evaluation order

For the requested `surface` only (the other surface's load is
reported context, never a blocking input):

1. Resolve `target_min` from `day_profile`; compute `audience_met =
   published_today(surface) >= target_min`.
2. Compute `audience_status`: `TARGET_MET` if met; else
   `AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW` if `pending(surface) >
   0`; else `AUDIENCE_GAP`.
3. If `source_class == NONE` (or no candidate record): `NO_ACTION` /
   `SOURCE_UNAVAILABLE`.
4. If `verification != PASS` or `quality_available == false`:
   `NO_ACTION` / `NO_QUALITY_CANDIDATE` — regardless of gap,
   opportunity, or restart state (`quality > quota` is absolute).
5. If `incremental_value == false` (same facts, no distinct audience
   role): `NO_ACTION` / `DUPLICATE_SUPPRESSED`.
6. If `opportunity == RECOVERY` and `audience_met` and no
   `exceptional_development`: `NO_ACTION` / `TARGET_MET`.
7. If `pending(surface) > 0`: `NO_ACTION` /
   `BLOCKED_PENDING_REVIEW` (audience gap stays truthful per step 2).
8. If `audience_met`: `NO_ACTION` / `TARGET_MET` (or
   `RECENT_ACTIVITY` when anti-burst spacing holds).
9. Else `PREPARE_MAIN` (surface MAIN) or `PREPARE_STORY` (surface
   STORY) / `PREPARED`.

Cross-surface loads never appear in steps 3–9. `PREPARE_*` remains
permission to search for and prepare a candidate subject to scoring,
`VERIFICATION: PASS`, dedup, load safety, human approval, and the
second final confirmation. `PREPARE_* != PUBLISH`, unconditionally.

## 11. Worked examples (deterministic)

Machine-readable fixtures:
`tests/fixtures/editorial_cadence_v2_examples.json` (10 cases,
one per item below). Validated offline by
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
   `TARGET_MET`.
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
