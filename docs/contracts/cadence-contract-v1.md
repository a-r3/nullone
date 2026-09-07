# NullOne Cadence Contract V1

Status: PROPOSED / issue #31
Scope: deterministic cadence and per-format load recommendation only.
Production impact: none. This document defines a decision contract for
issue #32 to implement. Writing this document does not change any
scheduler, controller, or production behavior.

## Purpose

Answer one deterministic question:

> Given authoritative NullOne workflow/publication state and the current
> Asia/Baku time, should the system recommend preparing a Story
> candidate, a main-post candidate, or no new work right now?

This contract defines the **inputs**, the **deterministic evaluation
order**, the **outputs**, and a **table of worked examples**. It does not
implement the evaluation in production code — that is issue #32.

An LLM may generate editorial content downstream of this contract's
output, but nothing in this contract's evaluation may be delegated to an
LLM. In particular an LLM must never decide: time arithmetic; counters;
whether Story or Feed/Carousel load is already sufficient; whether
pending work already exists; whether a missed cadence slot should be
replayed; or publication authorization.

`PREPARE_STORY` and `PREPARE_MAIN_CANDIDATE` are permission to search for
and prepare a candidate. They are never `PUBLISH`. Every resulting draft
still passes through the existing human-approval boundary unchanged:
Zernio draft → Telegram preview → human approve/revise/reject → second
publication confirmation → publication. This contract has no authority
to shorten, skip, or imply that boundary.

## Non-goals

- No visual workflow builder.
- No AI deciding authorization.
- No fixed rule that `today_count == 2` (or any other count) means stop.
- No production code, scheduler, or OpenClaw automation/cron change.
- No Story production pipeline (#33), changes to the accepted breaking policy
  (#34), or breaking identity/dedup/routing implementation (#35/#36).

## Relationship to existing repository material

This contract does not invent a new source of truth. It reuses and
extends what already exists:

- `workspace/social/STATE_RULES.md` already defines the publication
  state-source precedence (production manifest → `publish-ledger.jsonl`
  → candidate queue → editorial board) and the terminal/unsafe-to-repeat
  publication states. This contract adopts that precedence unchanged
  (see "State-source precedence" below) and only clarifies where the
  review/approval fields and `topic-ledger.jsonl` fit into it.
- `workspace/social/CONTENT_STRATEGY.md` §7 and §10 already document the
  current approved cadence *guidance* — 2 main Feed/Carousel pieces/day
  (up to 3 on an exceptional breaking day) and 3–5 Stories/day (4–6 on a
  strong-news day), plus the existing Draft Factory/Story check-window
  times. This contract treats those numbers as the current default value
  of a configurable guidance input (`main_target_min`, etc.), not as a
  new invented policy, and does not raise, lower, or hard-code them as a
  publication quota.
- `workspace/social/ops/scripts/nullone-manifest.py` already defines the
  concrete `review.state` / `approval.*` / `publication.state` fields
  this contract's pending-work accounting is built from.

If a later engineering decision changes any of these existing documents,
this contract's *inputs* change value; the contract's *evaluation logic*
does not need to change.

## Vocabulary

- **Audience-facing**: has actually reached the public Instagram account.
  Only `publication.state == PUBLISHED` (cross-checked against
  `publish-ledger.jsonl`, the append-only audience-facing log) qualifies.
- **Consequential pending**: not yet audience-facing, but already
  committed enough that producing an equivalent new candidate would
  create duplicate pressure or duplicate publication risk. See "Pending
  accounting" below for the exact state list.
- **Review/draft state**: a Zernio draft has been created for human
  review (`review.state == DRAFT_CREATED`) but has not reached a
  consequential publication state yet. This is a subset of "consequential
  pending", tracked separately for observability.
- **Generated candidate not yet in review**: a queue candidate with
  `status` such as `NEW`, `RESEARCHING`, `READY`, or `DEFERRED` and no
  Production Bridge manifest yet. This never counts as load (see below)
  and is exactly the category #32/#33 candidate search is allowed to
  keep producing against.
- **Rejected / stale / expired candidate**: a `topic-ledger.jsonl` entry
  marked `rejected`, or a queue candidate whose `freshness_deadline` has
  passed, or a manifest with `publication.state == FAILED` (a definitive,
  non-ambiguous failure). None of these count as load, and none of them
  may be silently resurrected as "still pending" — they are absent from
  both `effective_load` and `candidate_availability`.

## State-source precedence

Unchanged from `STATE_RULES.md`, extended only to say explicitly where
each concept used by this contract lives:

1. **Production manifest** (`workspace/social/ops/manifests/*.json`) —
   authoritative for one exact candidate's `review.state`,
   `approval.first_stage` / `approval.final_publish`, and
   `publication.state`. This is the only source for "consequential
   pending" and "review/draft state".
2. **`social/state/publish-ledger.jsonl`** — authoritative,
   append-only audience-facing publication history. This is the only
   source for "audience-facing" counts and for "last audience-facing
   publication time" per format.
3. **`social/state/candidate-queue.md`** — current viable/queued
   candidates, including `LEGACY_DRAFT` / `SUPERSEDED_DRAFT` bookkeeping.
   Used only to confirm a candidate is not being double-counted and to
   identify candidates that have not yet entered review.
4. **`social/state/topic-ledger.jsonl`** and the daily editorial board —
   lowest precedence. Used for `rejected` suppression and dedup context
   only; never a source for pending-load counts, and never allowed to
   override a confirmed publish-ledger or manifest entry.

A stale queue or board entry must never override a confirmed
publish-ledger or manifest entry. `UNKNOWN` publication state is never
treated as an empty slot — it stays in "consequential pending" until a
read-only reconciliation resolves it.

## Format accounting

Two independent counters. Neither counter's Story a substitute for the
other's gap.

### Main-post load (`FEED` + `CAROUSEL`)

Counts a candidate toward main load only when its manifest `format` is
`FEED` or `CAROUSEL`. `STORY` never contributes to main-post load.

| State | Counts as | Why |
|---|---|---|
| `publication.state == PUBLISHED`, published-date (Baku) == today | `published_today` | Confirmed audience-facing, per `publish-ledger.jsonl`. |
| `review.state == DRAFT_CREATED` and `publication.state == NOT_REQUESTED` | `pending` | A live Zernio draft exists and is awaiting human decision; producing another equivalent draft would flood the operator, matching the existing "existing active drafts... count toward today's production load" rule in `draft-factory.md`. |
| `approval.first_stage == true` and `approval.final_publish == false` | `pending` | Approved but not yet finally confirmed; still consequential, still blocks an equivalent duplicate. |
| `publication.state` in `{PUBLISH_IN_FLIGHT, PUBLISH_ACCEPTED, PUBLISHING, CHECK_REQUIRED, UNKNOWN, READBACK_FAILED}` | `pending` | Per `STATE_RULES.md`, all of these are unsafe-to-repeat/consequential; treating any of them as an empty slot risks a duplicate live publication. |
| `publication.state == FAILED` (definitive) | *(neither)* | A clean, non-ambiguous failure never reached the audience and is not ambiguous; it does not block a fresh attempt, but it also never occupied a slot, so it must not inflate `published_today`. |
| `review.state == NOT_CREATED`, no manifest, queue `status` in `{NEW, RESEARCHING, READY, DEFERRED}` | *(neither)* | Pre-review candidate; this is exactly the pool #32/#33 candidate search may keep drawing from. |
| Queue `status` in `{LEGACY_DRAFT, SUPERSEDED_DRAFT}` | *(neither)* | `STATE_RULES.md` already excludes these from active draft load explicitly. |
| `topic-ledger` `rejected`, or expired `freshness_deadline` | *(neither)* | Must not create duplicate pressure, but also must never be silently treated as still-open work. |

```
main_effective_load_today = main_published_today + main_pending
main_gap = main_effective_load_today < main_target_min
```

### Story load (`STORY` only)

Same table, restricted to manifests with `format == STORY`. Two Feed
posts never satisfy `story_gap`, and Story volume never suppresses a
genuinely open `main_gap` — the two counters are evaluated independently
before the single-recommendation precedence rule below is applied.

```
story_effective_load_today = story_published_today + story_pending
story_gap = story_effective_load_today < story_target_min
```

`main_target_min` and `story_target_min` are configurable guidance
inputs (see "Targets" below), never a hard-coded universal constant.

## Time contract

Timezone: `Asia/Baku`, using IANA timezone semantics (`ZoneInfo("Asia/Baku")`
or equivalent), not a hard-coded `UTC+4` offset — Baku does not currently
observe DST, but a hard-coded offset would silently break if that ever
changes, and IANA data is the only representation that survives such a
change without a code edit.

**Calendar day boundary**: the audience-facing "today" for both counters
is the Asia/Baku local calendar date derived from each event's
timestamp, not the day boundary of wherever the evaluating process
happens to run.

**Dayparts** are a coarse, configurable V1 signal — not fixed publishing
slots. Default boundaries (Asia/Baku local time-of-day), chosen to align
with, but not hard-code, the existing approved windows in
`CONTENT_STRATEGY.md` §7/§10:

| Daypart | Default local hours | Notes |
|---|---|---|
| `QUIET` | 00:00–06:59 | Before normal activity. No Story check window or main opportunity exists this early today. |
| `MORNING` | 07:00–12:59 | Covers the 09:45 main opportunity and the 10:30 Story check window. |
| `AFTERNOON` | 13:00–18:59 | Covers the 13:30 Story check window and the 15:45 main opportunity. |
| `EVENING` | 19:00–23:59 | Covers the 18:30/21:30 Story check windows and the 20:45 main recovery opportunity. |

Boundaries are configuration (`config.daypart_boundaries`), not a
constant baked into the algorithm, so a future editorial change to
`CONTENT_STRATEGY.md` does not require re-deriving this contract.

`QUIET` suppresses `PREPARE_*` by default (`quiet_hours_enabled: true`
default) because no human approver is expected to be actively reviewing
Telegram previews at that hour, and preparing a candidate that will sit
unreviewed for hours provides no value. This is operational pacing, not
editorial strategy, and remains configurable.

**Downtime / restart**: the contract is stateless and only ever reads
*current* authoritative state (state-source precedence above) — it has
no memory of which theoretical cadence opportunities were missed while
the machine was down. This is deliberate and is the entire coalescing
mechanism: a restart after any amount of downtime produces exactly one
evaluation of current state, never a queue of N replayed historical
recommendations, because no such queue is ever constructed. The caller
may optionally pass `signal.downtime_marker` purely for observability
(see "Downtime / catch-up" below); it never changes counters, gap
arithmetic, eligibility, or a more specific present-time reason. Only
when neither format has a gap does it replace `TARGETS_MET` with
`COALESCED_AFTER_DOWNTIME`.

## Quality overrides quota

Hard rule, non-negotiable by any cadence arithmetic:

`quality > quota`

If `candidate_availability.main_quality_candidate_available` (or the
Story equivalent) is `false`, the recommendation for that format is
`NO_ACTION` / `NO_QUALITY_CANDIDATE` — **regardless of**:
- Story count being zero;
- main-post count being below `main_target_min`;
- the system having been silent for hours;
- any daypart or downtime signal.

No deterministic cadence rule may manufacture weak content merely to hit
a number. This contract's `PREPARE_*` output is permission to *search
for and prepare* a candidate that still must independently pass:

- editorial selection (score, audience usefulness, consequence, source
  confidence, distinctiveness, topic-cluster saturation — per
  `CONTENT_STRATEGY.md` §8);
- `VERIFICATION: PASS` before it becomes publication-ready (per
  `docs/contracts/acceptance.md` `PUB-VERIFY-001`).

`candidate_availability.*_quality_candidate_available` is computed
upstream (Morning Editorial / Draft Factory / future #33 Story pipeline)
and is consumed here as an opaque boolean input — this contract does not
score or verify content itself.

## Pending work / backpressure

Already fully specified by the format-accounting tables above via
`effective_load = published_today + pending`. Concretely:

- A pending Story review draft (`PENDING_STORY_EXISTS`) suppresses a
  repeated `PREPARE_STORY` recommendation even if `story_gap` is still
  numerically true, because the gap is already being addressed.
- A pending/approved/scheduled main candidate (`PENDING_MAIN_EXISTS`)
  suppresses a repeated `PREPARE_MAIN_CANDIDATE` recommendation the same
  way.
- `UNKNOWN` publication state counts as pending (never as an empty slot)
  until read-only reconciliation resolves it — this prevents the
  controller from regenerating equivalent content for a candidate whose
  provider-side outcome is merely unconfirmed, not absent.
- Rejected/expired candidates are excluded outright (see the accounting
  tables), so they neither block nor motivate a new recommendation.

## Anti-burst spacing (recent audience activity)

Configurable guidance, not confirmed immutable editorial policy:
`main_min_spacing_minutes` (default 120) and `story_min_spacing_minutes`
(default 45). If `now - last_published_at(format) < min_spacing_minutes`,
that format is held even if its gap is numerically true, with reason
`RECENT_AUDIENCE_ACTIVITY`. This directly uses the "last audience-facing
publication time" input the issue requires, and only smooths pacing — it
never raises or lowers a daily target.

## Downtime / catch-up

Coalescing principle: **a stale missed slot is not an obligation.**
Because the contract is stateless and evaluates only current state (see
"Time contract" above), three theoretical missed opportunities during a
downtime window collapse, by construction, into exactly one evaluation
of current state on restart — never three historical draft requests.

`signal.downtime_marker` (optional) is an observability echo only:

```json
{"restart_after_downtime": true, "time_since_last_evaluation_seconds": 14400}
```

The deterministic evaluation order below is authoritative. The marker
must not mask a more specific present-time reason:

- A gap held by pending work preserves `PENDING_MAIN_EXISTS` /
  `PENDING_STORY_EXISTS`.
- A gap with no quality candidate preserves `NO_QUALITY_CANDIDATE`.
- A gap held by recent activity preserves `RECENT_AUDIENCE_ACTIVITY`.
- A gap held by quiet hours preserves `QUIET_HOURS`.
- A genuinely eligible gap preserves `MAIN_GAP` / `STORY_GAP`.
- No gap in either format, with a marker: `NO_ACTION` /
  `COALESCED_AFTER_DOWNTIME`.
- No gap in either format, without a marker: `NO_ACTION` / `TARGETS_MET`.

These rules preserve the existing single-recommendation, main-before-Story
evaluation order; the marker changes neither eligibility nor blocker precedence.
In particular, downtime plus a gap with no quality candidate remains
`NO_ACTION` / `NO_QUALITY_CANDIDATE`, never `COALESCED_AFTER_DOWNTIME`.
`context.downtime_marker` may echo the input for auditability independently
of the selected reason. A coalescing audit flag is not a reason override
and does not change counters, gap arithmetic, or eligibility.

## Targets

Configurable guidance inputs, defaulted to the values already approved
in `CONTENT_STRATEGY.md` §7/§10 — preserved, not invented:

| Input | Default | Source |
|---|---|---|
| `main_target_min` | `2` | `CONTENT_STRATEGY.md` §7 "Normal target: 2 main Feed/Carousel pieces per day." |
| `main_target_max_breaking` | `3` | §7 "Exceptional breaking day: maximum 3 main pieces." |
| `story_target_min` | `3` | §10 "Normal: 3–5 Stories/day" (lower bound). |
| `story_target_max_breaking` | `6` | §10 "Strong-news day: 4–6 Stories/day" (upper bound). |

These are guidance for the `*_gap` computation, never a forced
publication quota: reaching `main_target_min` does not forbid a
genuinely stronger breaking candidate, and *not* reaching it never
forces a weak one (quality-over-quota above is absolute). If a future
decision changes these numbers, only the config default changes — the
evaluation logic above is unaffected.

## V1 input/output shape

Machine-implementable shape for issue #32. This contract does not
implement the function; it defines its signature and behavior.

### Input: `nullone.cadence-contract.v1` (request)

```json
{
  "schema": "nullone.cadence-contract.v1",
  "now": "2026-09-06T11:15:00+04:00",
  "timezone": "Asia/Baku",
  "config": {
    "main_target_min": 2,
    "main_target_max_breaking": 3,
    "story_target_min": 3,
    "story_target_max_breaking": 6,
    "main_min_spacing_minutes": 120,
    "story_min_spacing_minutes": 45,
    "quiet_hours_enabled": true,
    "daypart_boundaries": {
      "quiet_end": "07:00",
      "morning_end": "13:00",
      "afternoon_end": "19:00"
    }
  },
  "main_load": {
    "published_today": 0,
    "pending": 0,
    "last_published_at": null
  },
  "story_load": {
    "published_today": 0,
    "pending": 0,
    "last_published_at": null
  },
  "candidate_availability": {
    "main_quality_candidate_available": true,
    "story_quality_candidate_available": true
  },
  "signal": {
    "breaking_day": false,
    "downtime_marker": null
  }
}
```

### Output: `nullone.cadence-contract.v1` (response)

```json
{
  "schema": "nullone.cadence-contract.v1",
  "contract_version": "1.0.0",
  "recommendation": "NO_ACTION",
  "reason_code": "TARGETS_MET",
  "reason_text": "Main and Story load already meet today's guidance.",
  "permitted_action": "NONE",
  "daypart": "MORNING",
  "counters": {
    "main": {"published_today": 2, "pending": 0, "effective_load": 2, "gap": false},
    "story": {"published_today": 3, "pending": 0, "effective_load": 3, "gap": false}
  },
  "context": {
    "evaluated_at": "2026-09-06T11:15:00+04:00",
    "downtime_coalesced": false
  }
}
```

`permitted_action` is `"NONE"` or `"CANDIDATE_SEARCH_AND_PREPARE"` and is
never `"PUBLISH"` — publication authorization is out of scope for this
contract entirely (see `docs/contracts/acceptance.md`
`PUB-AUTH-001`/`PUB-AUTH-PROVENANCE-001` for the actual authorization
contract).

### Deterministic evaluation order

1. Compute `main_effective_load`, `story_effective_load`,
   `main_gap`, `story_gap` from the accounting tables above.
2. For each format independently, compute `eligible(format)`:

   ```
   eligible(format) =
       gap(format)
       AND pending(format) == 0
       AND candidate_availability.<format>_quality_candidate_available
       AND NOT recently_active(format)
       AND NOT (quiet_hours_enabled AND daypart == QUIET)
   ```

   `pending(format) == 0` is deliberately a separate condition from
   `gap(format)`, not folded into it: a single pending item does not
   always close the numeric gap (e.g. one pending Story against
   `story_target_min = 3`), but it must still suppress a *repeated*
   recommendation for that format — otherwise the controller would keep
   asking for more Story candidates while one is already awaiting human
   review, which is exactly the duplicate-pressure case this contract
   must prevent.
3. If `eligible(main)`: `recommendation = PREPARE_MAIN_CANDIDATE`,
   `reason_code = MAIN_GAP`. **Main is checked before Story** — see
   justification below.
4. Else if `eligible(story)`: `recommendation = PREPARE_STORY`,
   `reason_code = STORY_GAP`.
5. Else: `recommendation = NO_ACTION`. Choose `reason_code` by walking
   `[MAIN, STORY]` in that fixed order and, for the first format with
   `gap(format) == true`, taking the first matching sub-rule:

   ```
   for format in [MAIN, STORY]:
       if not gap(format): continue          # nothing to complain about
       if pending(format) > 0: reason = PENDING_MAIN_EXISTS / PENDING_STORY_EXISTS; stop
       if not candidate_availability[format]: reason = NO_QUALITY_CANDIDATE; stop
       if recently_active(format): reason = RECENT_AUDIENCE_ACTIVITY; stop
       if quiet_hours_enabled and daypart == QUIET: reason = QUIET_HOURS; stop
   # if the loop found no gapped format at all:
   if no gap in either format and signal.downtime_marker is present:
       reason = COALESCED_AFTER_DOWNTIME
   elif no gap in either format:
       reason = TARGETS_MET
   ```

   This keeps a single, fully deterministic reason per response — never
   two independent boolean flags — while still reflecting whichever
   format actually has the open gap.

**Why Main is checked first**: main pieces have a slower production
cycle (research, verification, Feed/Carousel rendering) and a small
daily capacity (`main_target_max_breaking = 3`), so a genuinely open
main opportunity with a verified candidate available is time-sensitive —
deferring it risks losing the day's opportunity entirely. Story
production is designed to be lightweight and re-checked at multiple
windows per day (§10), so deferring a Story recommendation by one
evaluation cycle costs comparatively little: the next evaluation (run
moments later, or at the next check window) re-derives `story_gap` from
current state with no memory loss, so nothing is dropped, only delayed.
This is the one combined-recommendation rule this contract needs; it
does not expose two independent booleans, per the issue's explicit
guidance against ambiguous multi-boolean output.

### Reason codes (minimal coherent set for V1)

| Reason code | Recommendation it pairs with | Meaning |
|---|---|---|
| `MAIN_GAP` | `PREPARE_MAIN_CANDIDATE` | Main load below target and a verified candidate is available. |
| `STORY_GAP` | `PREPARE_STORY` | Story load below target and a verified candidate is available. |
| `NO_QUALITY_CANDIDATE` | `NO_ACTION` | A gap exists but no candidate currently passes quality/verification for that format. |
| `PENDING_MAIN_EXISTS` | `NO_ACTION` | Main gap is already being addressed by pending/approved/in-flight work. |
| `PENDING_STORY_EXISTS` | `NO_ACTION` | Story gap is already being addressed by pending/approved/in-flight work. |
| `RECENT_AUDIENCE_ACTIVITY` | `NO_ACTION` | Anti-burst spacing not yet elapsed since the last publication of that format. |
| `COALESCED_AFTER_DOWNTIME` | `NO_ACTION` | Neither format has a gap and a downtime marker is present; replaces only `TARGETS_MET`, with no historical slot replay. |
| `QUIET_HOURS` | `NO_ACTION` | Current daypart is `QUIET` and quiet-hours suppression is enabled. |
| `TARGETS_MET` | `NO_ACTION` | Neither counter has a gap. |

## Table-driven examples

Machine-readable fixtures for these scenarios live in
`tests/fixtures/cadence_contract_v1_examples.json`. Summary:

| # | Scenario | Expected recommendation | Expected reason_code |
|---|---|---|---|
| 1 | Early morning, nothing published, quiet hours | `NO_ACTION` | `QUIET_HOURS` |
| 2 | Midday silence, no Story today, candidate available | `PREPARE_STORY` | `STORY_GAP` |
| 3 | Story already present today (target met) | `NO_ACTION` | `TARGETS_MET` |
| 4 | Two main posts, zero Story, Story candidate available | `PREPARE_STORY` | `STORY_GAP` |
| 5 | Strong-news day, healthy main and Story load | `NO_ACTION` | `TARGETS_MET` |
| 6 | Gap exists but no quality/verified candidate | `NO_ACTION` | `NO_QUALITY_CANDIDATE` |
| 7 | Pending Story draft already exists | `NO_ACTION` | `PENDING_STORY_EXISTS` |
| 8 | Pending main draft already exists | `NO_ACTION` | `PENDING_MAIN_EXISTS` |
| 9 | Publication `UNKNOWN` for equivalent main content | `NO_ACTION` | `PENDING_MAIN_EXISTS` |
| 10 | Downtime then restart, current state already met | `NO_ACTION` | `COALESCED_AFTER_DOWNTIME` |
| 11 | Multiple missed opportunities, restart shows real gap | `PREPARE_MAIN_CANDIDATE` | `MAIN_GAP` |
| 12 | Asia/Baku midnight/day-boundary rollover | `NO_ACTION` | `QUIET_HOURS` |
| 13 | Downtime restart, real main gap, no quality candidate | `NO_ACTION` | `NO_QUALITY_CANDIDATE` |

Row 9 resolves to `PENDING_MAIN_EXISTS` rather than manufacturing a
duplicate: an `UNKNOWN` publication state for one candidate already
counts as `pending` for that format (see accounting table), so the gap
is already covered pending reconciliation — this is exactly the
"`UNKNOWN` must not be treated as an empty slot" requirement.

Row 11 shows that coalescing does not mean "always NO_ACTION after
downtime" — it means "evaluate current state once." If that current
state genuinely shows a gap with an available candidate, the
recommendation is the normal gap-based one; only the `reason_code`
choice in row 10 changes because neither format has a gap and the
ordinary result would be `TARGETS_MET`. Row 13 retains the more specific
`NO_QUALITY_CANDIDATE` reason, with the downtime marker echoed separately
in context. Missed opportunities are not replayed in any of these cases.

## Human approval

Restated for this contract explicitly: `PREPARE_STORY` and
`PREPARE_MAIN_CANDIDATE` mean "search for and prepare a candidate." They
are not publication authorization. Every resulting draft still requires,
unchanged:

1. Zernio draft creation (review transport only, per
   `docs/contracts/acceptance.md`'s draft-only transport rule);
2. Telegram preview;
3. human approve/revise/reject (first-stage approval);
4. second, final publication confirmation;
5. publication, subject to the existing one-attempt/`UNKNOWN`-is-unsafe
   guarantees already documented in `docs/contracts/acceptance.md`.

`PREPARE_* != PUBLISH`, unconditionally.

## Non-goals for issue #32 (explicit boundary)

This contract intentionally leaves to #32:
- reading the real manifest/queue/ledger files and computing the actual
  counter values fed into this contract;
- wiring this contract's output into Draft Factory / a future Story
  worker;
- any scheduler or OpenClaw automation change.

The separate #34 decision contract is accepted and merged in
`33bd7c9114ecaeda675f1565a80268541c95dd68`; see
`docs/contracts/breaking-routing-policy-v1.md`. #31 remains independent
cadence policy and does not implement or modify #34/#35/#36.

This contract intentionally leaves to #33/#35/#36:
- the Story draft production pipeline itself (#33);
- breaking-news event identity/dedup implementation (#35);
- breaking draft routing (#36).

After #31 merges, a separate canonical-context sync PR will record both
accepted decisions before #32/#35 implementation. This task does not
modify `NULLONE_PROJECT_CONTEXT.md`.

## Validation

- `tests/fixtures/cadence_contract_v1_examples.json` — the 13 worked
  examples above, machine-readable.
- `tests/test_cadence_contract_fixture.py` — validates fixture shape and
  hygiene only (schema fields present, `recommendation` and
  `reason_code` are drawn from the sets documented above, no duplicate
  scenario names, no production identifiers/credentials), plus the fixed
  downtime/no-quality regression expectation and its audit context. It does not
  execute a decision function, because no decision function exists yet
  in this repository — implementing one is #32's job, not this contract
  document's.
- `python3 tests/run_offline.py` must remain green.

## Historical publish-ledger compatibility note (issue #60)

This is a compatibility-reader note, not a change to #31's accepted
policy above: the format-accounting tables, targets, spacing, and
evaluation order are all unchanged. It documents how
`nullone_cadence_state_adapter.py` reads `publish-ledger.jsonl` rows
written before this repository required every row to carry `format`.

- **Native format**: a row whose own `format` field is already one of
  the known formats (`FEED`/`CAROUSEL`/`STORY`) is read exactly as
  before this note; nothing about its behavior changed.
- **Deterministic recovered format**: a row missing (or carrying an
  unrecognized) `format` may be recovered read-time from authoritative
  linkage already present in loaded manifest state, in this evidence
  hierarchy: (1) exact `manifest_id` match to one loaded manifest with a
  known format, then (2) exact `live_zernio_post_id` match to one loaded
  manifest with a known format. Recovery succeeds only when the evidence
  resolves to exactly one format; format is never inferred from title,
  topic, time, or row order. The on-disk ledger row is never rewritten —
  the recovered format is a read-time value the adapter derives and
  reports alongside its source, never merged indistinguishably into the
  row as if it had always been native.
- **Unresolved format**: missing, conflicting, or ambiguous evidence
  (including one identifier — a `manifest_id` or a
  `live_zernio_post_id` — matching manifests of more than one format,
  whether across distinct manifests or a duplicated `manifest_id`)
  leaves a row's format `UNKNOWN`. An `UNKNOWN`-format row is excluded
  from both the main and Story buckets — it is never guessed into
  either — and never participates in published-ID reconciliation
  either: only a native or deterministically recovered known-format
  `PUBLISHED` row can contribute a `manifest_id`/`live_zernio_post_id`
  toward suppressing a pending manifest counted elsewhere. An
  unresolved row therefore never suppresses manifest pending state and
  cannot manufacture a false gap that way, regardless of whether the
  row itself is decision-relevant.
- **Decision-relevant vs. decision-irrelevant uncertainty**: an
  `UNKNOWN`-format row only matters if it could still change today's
  counters or the current anti-burst spacing decision, i.e. its
  `result == PUBLISHED` and either its Asia/Baku calendar date is today,
  or it may still fall inside the configured main/Story spacing window
  measured from `now`. A row that is provably older than both is
  decision-irrelevant: it cannot change today's count or the current
  spacing decision, so it does not block a normal read and remains
  visible only as a compatibility diagnostic. A non-`PUBLISHED` row
  never participates in this accounting at all, so its missing format is
  always diagnostic-only.
- **Fail-safe request assembly**: when an unresolved row is
  decision-relevant, `collect_format_loads()` (and therefore
  `assemble_cadence_request()`) raises `CadenceStateError` before the
  pure controller ever evaluates a request — it never hands the
  controller an apparently-complete state that could manufacture a false
  `PREPARE_MAIN_CANDIDATE` or `PREPARE_STORY`, and it never silently
  treats the ambiguous row as zero either.
- **No historical ledger mutation**: this is a read-only compatibility
  layer. `analyze_ledger_compatibility()` provides a non-mutating,
  non-raising-for-format-uncertainty audit (native/recovered/unknown row
  counts and per-row references) for operators; no production ledger
  migration is performed or required (`migration_required: False`).

## Exit rule for issue #31

Issue #31 can close when:

1. versioned cadence inputs/outputs are documented (above);
2. Feed/Carousel and Story counters are demonstrably separate (above);
3. IANA `Asia/Baku` timezone is explicit, not a hard-coded offset;
4. stale silence/daypart can recommend work but cannot manufacture
   low-quality content (quality-over-quota section is unconditional);
5. `NO_ACTION` is a valid, well-reasoned output with a stable reason
   code;
6. pending drafts/approvals/`UNKNOWN` states are counted to prevent
   duplicate production pressure;
7. Story and main-post targets remain configurable guidance, never a
   forced publication quota;
8. the human-approval boundary is explicitly restated as unchanged;
9. table-driven examples exist and are machine-readable;
10. no production/runtime/controller code was added by this decision.

Closing #31 does not close #32, #33, #35, #36, or #37; #34 is already merged.
