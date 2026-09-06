# Breaking draft routing policy V1

Version: `nullone.breaking-routing.v1`

Status: PROPOSED / decision for [#34](https://github.com/a-r3/nullone/issues/34)

Enforcement: NOT IMPLEMENTED by this decision. Fixture validation is not runtime proof.

Baseline inspected: `f9924123de4677047e231a5eec0437c8ad76acb1`

Production impact: none; no activation, scheduling, draft creation or publication.

## Authority and compatibility

Breaking may override **normal draft timing only**. It never authorizes
publication. Verification, exact claim scope, quality, deduplication, safe load
limits, human approval and the second final publication confirmation remain
mandatory. No output is `PUBLISH`; no result may set approval flags, schedule,
call a publisher, retry publication or resurrect an unsafe candidate.

This is the policy for future [#35 identity/dedup](https://github.com/a-r3/nullone/issues/35)
and [#36 routing](https://github.com/a-r3/nullone/issues/36). It does not change
the current Radar's `DELTA_MONITORING_ONLY` prompt. #33 owns Story worker work;
#31/#32 own cadence policy/controller. #37 remains the controlled deployment
boundary. The canonical context is intentionally unchanged pending a separate
context-sync PR after the parallel #31 and #34 decisions merge.

Inspected sources (paths relative to repository root):

| Source | Binding convention |
|---|---|
| `NULLONE_PROJECT_CONTEXT.md` | Two-stage approval; quality and publication safety; dependency order. |
| `workspace/social/ops/prompts/breaking-radar.md` | Delta monitoring; NEWS/BREAKING candidates; no current production delegation. |
| `workspace/social/ops/prompts/morning-editorial.md`, `workspace/social/ops/prompts/draft-factory.md` | Ordinary queue selection; latest Production Bridge overrides govern older prompt sections. |
| `workspace/social/SCORING.md`, `workspace/social/CONTENT_STRATEGY.md` | Content-neutral scoring, hard quality blocks, candidate metadata, derivative limits and load. |
| `workspace/social/SOURCES.md`, `workspace/social/CONTENT_RULES.md`, `workspace/AGENTS.md` | Primary evidence, exact final wording, rejection/revision and approval rules. |
| `workspace/social/STATE_RULES.md`, `workspace/social/OPERATING_SYSTEM.md` | Manifest > publish ledger > queue > editorial board; durable history and active-load distinction. |
| `workspace/social/ops/scripts/nullone_state.py` | Markdown queue; append-only JSONL publish/topic ledgers; existing identifiers and reconciliation records. |
| `workspace/social/ops/scripts/nullone-manifest.py`, `workspace/social/ops/scripts/nullone_bridge_common.py` | `nullone.production.v1`; candidate/topic cluster/content type/format; review/approval/publication sections. |
| `workspace/social/ops/scripts/nullone-draft-bridge.py`, `workspace/social/ops/scripts/nullone-publish-bridge.py`, `workspace/social/ops/PRODUCTION_BRIDGE.md` | Consumed attempts, actual bridge states, immutable content and separate review/live objects. |
| `workspace/social/ops/scripts/nullone_run_outcome.py`, `tests/fixtures/run_outcome_cases.json` | #27 run identity and domain outcomes, separate from publication state. |
| `docs/contracts/acceptance.md`, `tests/fixtures/acceptance_contracts.json`, `tests/test_behavioral_regressions.py` | Existing enforced/missing guarantees and offline publication/review safety cases. |

Live `workspace/social/state/` queues/ledgers and
`workspace/social/ops/manifests/` are ignored and absent from the development
checkout. This decision inspects their contracts, writers and synthetic tests,
not production records. An absent development fixture is not evidence of empty
production coverage. No existing guarantee is upgraded by this document.

Severity is a new routing assessment, not a queue status, `content_type`,
`freshness_class`, manifest `format` or publication state. Existing formats
remain `STORY`, `FEED`, `CAROUSEL`; main means **one FEED or one CAROUSEL**.
`BREAKING` content type/freshness alone cannot establish severity. Existing
candidate statuses and manifests are not migrated by this policy.

## Severity: audience value supported by evidence

| Class | Required finding | Maximum immediate request |
|---|---|---|
| `NORMAL` | Verified ordinary development; waiting for normal cadence does not materially reduce audience usefulness. Examples: incremental feature, routine announcement, minor availability or a non-urgent follow-up. | None; ordinary queue/cadence. |
| `MATERIAL_BREAKING` | Verified development changes what the audience can use, decide, understand or act on now; the assessment explains the concrete usefulness lost by waiting for the next normal window. A concise Story has immediate value. | One Story draft. Never automatically main. |
| `EXCEPTIONAL_BREAKING` | All material conditions, plus rare high-impact consequences and substantial standalone audience value for a main treatment beyond repeating the Story. The assessment explains affected users, changed consequence and why main deserves attention now. | One Story plus optionally one justified FEED or CAROUSEL candidate. |

Default to NORMAL; assign MATERIAL_BREAKING only when its timing/value conditions
are evidenced, and EXCEPTIONAL_BREAKING only when its additional conditions are
also evidenced. If uncertain, retain the lower supported class. Exceptional is
deliberately rare, never a routine label for a famous company, impressive number,
high score, viral post or busy news day. No emotional language, model confidence
or universal numeric severity threshold substitutes for these findings. Record
the verified consequence, audience value and timing-loss explanation in the
assessment. If only material conditions are evidenced, use MATERIAL_BREAKING.
If verification is not PASS, severity is `null` (unassessed), not NORMAL.

Classification alone does not establish format eligibility. Current scoring
still applies: STORY >= 26, FEED >= 38, CAROUSEL >= 42 with meaningful multi-slide
value, plus every hard block in `SCORING.md`. These are existing production
thresholds, not invented severity cutoffs; future scoring revisions are pinned
in the assessment. Quality always beats speed or quota.

## Evidence gate

Every assessed development requires an auditable evidence record containing:

1. Original/canonical URL or direct-evidence reference, publisher/authority,
   announcement/release identifier where available, and observation time plus
   event/effective time where known. Unknown dates remain unknown.
2. The exact supported claim, product/version, number/unit/comparison basis,
   audience/plan/region, availability stage and relevant limitations. Preserve
   distinctions such as plans, preview, testing and available.
3. A retrievable snapshot or excerpt reference sufficient to establish what
   was verified, and explicit claim-to-evidence links. A URL alone is insufficient.
4. Verification state and rationale, including resolution of source conflicts.
   Severity explanations must be supported by those same scoped claims.

Minimum for acceleration: every central claim has direct, attributable evidence
and no unresolved material contradiction. Prefer a primary source, first-party
announcement, official release or authoritative documentation. Credible direct
evidence can substitute only with recorded provenance, reproducible support for
the exact claim, and why the primary source is unavailable/inapplicable. Seek
corroboration for uncertainty; unresolved uncertainty blocks the affected claim.
A first-party assertion of performance is evidence of that scoped assertion,
not independent proof of universal superiority.

Secondary reporting may discover/support/contextualize; it may not silently
broaden claims. Virality, social chatter, a lone unverified post and model
confidence never meet this gate. Primary evidence wins over conflicting
secondary wording; unresolved central conflicts remain blocked.

Map existing candidate `verification_status` verbatim:
`UNVERIFIED | PARTIAL | PASS | BLOCKED`. A missing historical verification field
maps to UNVERIFIED pending re-verification, never implied PASS. Non-PASS gives
`BLOCKED_UNVERIFIED` unless an earlier identity/state suppression applies.
Research may continue in NEW/RESEARCHING/DEFERRED; no READY or publication-ready
draft may be accelerated. BLOCKED needs new evidence and re-verification.

Candidate PASS is only the admission gate. After drafting, compare **every final
Azerbaijani factual phrase** in Story/image/slides/caption with the evidence.
`VERIFICATION: PASS` and the immutable manifest's `verification == PASS` remain
mandatory before a publication-ready review draft. Changes require re-verification
and fresh approval of the exact version. Each target receives Telegram review,
human approval and a second final publication confirmation through existing
boundaries. Neither candidate PASS nor an immediate route is approval.

## Identity and dedup requirements for #35

Maintain a stable `event_id` for the underlying event and `development_id` for
the exact scoped development being considered. Initial coverage has its own
development ID; a verified material follow-up receives a different development
ID linked to the parent. `topic_id` corresponds to the existing `topic_cluster`
and groups related developments; it is not itself proof of event equivalence.
Existing `candidate_id`/`manifest_id` remain references, not replacement IDs.

#35 owns deterministic encoding and normalization, and must version and test
them. Identical evidence/claims must resolve to the same development across
sources, scans, runs and formats. Persist identity basis, input references,
matched candidate/manifest/ledger references and decision reason. Do not include
scan time, source popularity or target format in development identity. No vector
database or embeddings are required for V1.

| Relation | Deterministic requirement |
|---|---|
| `EXACT_DUPLICATE` | Same stable development/announcement identity and scoped claims, or the same canonical source evidence with unchanged claims. Suppress a new draft set. |
| `SAME_EVENT` | Different source/article/headline covers the same development without an evidenced material delta. Link it as supporting evidence; suppress a new draft set. |
| `MATERIAL_FOLLOW_UP` | Same event, but a verified substantive delta with distinct audience value; new development ID, parent link, evidence and stable follow-up reason required. |
| `DISTINCT_EVENT` | Different evidenced development/occurrence, e.g. a separate product/version release or action; same topic alone does not merge it. Explicitly explain incremental value when recent related coverage exists. |

URL normalization must remove known tracking-only variation while preserving
identity-bearing path/query/version identifiers; unproven aliases or redirects
must not be guessed. A different article URL is not a new event. A reused official
release/status URL can contain a new development only with evidence of changed
claims (snapshot/revision/announcement ID); it does not become a duplicate solely
because its URL matches. Cosmetic rewrites are still duplicates. Conflicting exact
identifiers or an unproven delta are ambiguous, not a license to mint a fresh ID.

### Precedence and state authority

Matching precedence and state authority are separate. Both must be applied:

1. Resolve exact source/announcement/event/development/candidate identifiers.
2. Inspect matching manifests and consequential publication history, including
   the publish ledger. Authority is manifest > publish ledger > queue > board.
   A stale READY row cannot override a published/unsafe manifest or ledger.
3. Inspect queue/review state and topic-ledger history, including drafts, rejection,
   supersession and already reserved requests. An existing viable queue entry is
   reused for its first request, not copied; merely being READY is not a draft.
4. Compare normalized deterministic claim/topic/product/version/occurrence
   identifiers to resolve different-source reports and related coverage.
5. Optional AI assistance may flag or explain ambiguous semantic comparisons.
   It cannot overrule exact identity, evidence scope, persisted requests or known
   state. Unresolved comparison returns `BLOCKED_AMBIGUOUS_IDENTITY`.

Do not stop at the first apparent absence or downgrade an unsafe historical fact
because a later row says draft/failed/READY. Any positive equivalent consequential
record suppresses even if another store is stale. Missing/unreadable/malformed
required state, inconsistent identity, or unexplained state conflict must block
as ambiguous when a definite suppression cannot already be established. A
proven initialized-empty store can count as empty; a missing store cannot silently
do so. Record all matching history, not just a recent slice.

### Consequential and review suppression

Equivalent developments that **have or had** any of the following normally yield
`SUPPRESS_DUPLICATE`; automatic replacement/regeneration is prohibited:

- approval (`approval.first_stage == true` or explicit approved queue/history),
  scheduled state, `PUBLISH_IN_FLIGHT`, `PUBLISH_ACCEPTED`, `PUBLISHING`, `PUBLISHED`;
- publication `UNKNOWN`, `READBACK_FAILED`, `CHECK_REQUIRED`, or any consumed
  `publication.attempts`, including a later FAILED/draft readback;
- review `CREATE_IN_FLIGHT`, `REVIEW_UNKNOWN`, `DRAFT_CREATED`, any consumed
  `review.create_attempts`, or an existing reserved draft request;
- DRAFTED, REJECTED, LEGACY_DRAFT or SUPERSEDED_DRAFT candidate/history. A manual
  revision request is a separate versioned review operation, never Radar replay.

No universal enum named APPROVED is added to manifests: use actual approval
fields and adapter-normalized queue/provider state. Scheduled evidence is a
safety observation, never permission to schedule.

`UNKNOWN` never means "nothing happened, regenerate". Ambiguous review/publication
outcomes require manual reconciliation using read-only evidence. The same applies
to unexplained consumed attempts and unsafe state conflicts. Retain history;
reconciliation may clarify outcome but cannot erase consumed attempts or authorize
automatic equivalent regeneration. A notifier failure cannot create a new draft.

A follow-up is permitted after confirmed parent coverage only for its distinct
delta. An unresolved unsafe parent outcome blocks automatic follow-up within that
event until manual reconciliation establishes coverage scope; use
`SUPPRESS_DUPLICATE / UNRESOLVED_EVENT_HISTORY`. Clearly distinct events in the
same topic are not suppressed merely for sharing that topic.

Load accounting is different from suppression: LEGACY_DRAFT/SUPERSEDED_DRAFT and
historical queue-only DRAFTED do not count as active main drafts, but remain
ineligible for automatic resurrection. Active main drafts follow STATE_RULES:
valid current manifest, DRAFT_CREATED and a review ID, without terminal live state.

## Follow-up and recent coverage

A follow-up records the parent development, exact before/after facts, delta
evidence, incremental audience value and one stable reason:

`AVAILABILITY_CHANGED`, `OFFICIAL_NUMBER_CHANGED`, `AFFECTED_REGION_CHANGED`,
`MATERIAL_CORRECTION`, `PRODUCT_VERSION_CHANGED`, `USER_CONSEQUENCE_CHANGED`.

Multiple changes choose the reason for the central production claim and record
the others in evidence. A new number requires source-matched units, population
and period; a cosmetic precision change is not material by itself. A separately
announced product/version can instead be DISTINCT_EVENT if its identity supports
that relation. Restatement, new headline, another source, a format switch, elapsed
time, or merely greater popularity do not qualify. A distinct event may still be
NORMAL; a follow-up does not inherit the parent's severity.

Recent coverage includes prepared/reviewed/published pieces, not just successful
posts. Equivalent coverage never expires into a fresh draft. Related coverage
with no explicit incremental audience value yields `SUPPRESS_RECENT_COVERAGE`.
Verified distinct value can pass this check even soon after related coverage,
subject to all state, quality and load gates.

Do not introduce a universal magic cooldown. Recent-coverage windows must be
configurable by channel/topic and recorded with the evaluated time, window and
configuration revision in `state_snapshot_ref`. Existing main policy at this
baseline is at most two main pieces per topic cluster in seven days unless a
material new development exists; this is a derivative constraint, not an event
identity TTL. #31/#32 own effective cadence/load settings. No new Story numeric
cooldown or daily quota is established here. Elapsed time alone never proves
novelty, clears suppression or resolves UNKNOWN.

## Routing order and cadence handoff for #36

Given the same validated assessment and state snapshot, use this order; the first
applicable terminal rule wins. Invalid input/schema is rejected before any request
as `POLICY_INPUT_INVALID` (a consumer/domain error, not a ninth route).

| Order | Condition | Route / reason code |
|---|---|---|
| 1 | Established equivalent consequential history; unsafe unresolved parent | SUPPRESS_DUPLICATE / EXISTING_CONSEQUENTIAL_STATE; use UNRESOLVED_EVENT_HISTORY for the unsafe parent case. |
| 2 | Existing review/reservation/draft; excluded rejected/legacy/superseded candidate | SUPPRESS_DUPLICATE / EXISTING_DRAFT_REQUEST; use CANDIDATE_EXCLUDED for excluded candidate history. |
| 3 | Proven repeat of unchanged exact source/development; equivalent different-source report | SUPPRESS_DUPLICATE / EXACT_EVENT_DUPLICATE or SAME_EVENT_DIFFERENT_SOURCE, respectively. |
| 4 | Identity or required state cannot be resolved safely | BLOCKED_AMBIGUOUS_IDENTITY / IDENTITY_UNRESOLVED or STATE_UNAVAILABLE_OR_CONFLICTING. |
| 5 | Candidate verification is not PASS | BLOCKED_UNVERIFIED / EVIDENCE_INSUFFICIENT. |
| 6 | Recent related coverage lacks explicit incremental audience value | SUPPRESS_RECENT_COVERAGE / NO_INCREMENTAL_AUDIENCE_VALUE. |
| 7 | NORMAL | NORMAL_QUEUE / NORMAL_CADENCE. Queue eligibility remains governed by existing scoring/status rules; this does not set READY. |
| 8 | Breaking Story fails quality/format, safe load, or required dependency gate | BLOCKED_DRAFT_SAFETY / STORY_QUALITY_BLOCK, STORY_LOAD_BLOCK or DRAFT_DEPENDENCY_UNAVAILABLE (in that priority). No main-only fallback. |
| 9 | MATERIAL_BREAKING and Story eligible | IMMEDIATE_STORY_DRAFT / MATERIAL_TIME_VALUE. |
| 10 | EXCEPTIONAL_BREAKING, Story eligible and main requested, justified and eligible | IMMEDIATE_STORY_AND_MAIN_DRAFT / EXCEPTIONAL_MAIN_VALUE. |
| 11 | EXCEPTIONAL_BREAKING, Story eligible but main not selected/eligible | IMMEDIATE_STORY_DRAFT / EXCEPTIONAL_STORY_ONLY. Record main omission in assessment. |

For orders 1–4 the dedup relation still describes the actual comparison; a known
equivalent state wins even when fresh incoming verification is insufficient.
Within grouped orders, evaluate subconditions left to right: equivalent
consequential history before unsafe parent history, existing review/request before
excluded-candidate history, and exact duplicate before different-source equivalence.
Record `severity: null` for any non-PASS candidate in these results too. Within
order 4, unavailable/conflicting required state takes precedence over remaining
semantic uncertainty. Reason text explains facts; codes determine behavior.

Immediate means prepare the permitted draft request without waiting for the next
normal Draft Factory slot, once safety gates permit. This is neither a publication
deadline nor permission to relax standards. Main eligibility requires a recorded
standalone justification, one explicit FEED/CAROUSEL choice, its scoring/quality
gates and available main capacity. If only main is blocked, preserve the eligible
Story with EXCEPTIONAL_STORY_ONLY. An absent standalone value finding means the
severity is MATERIAL_BREAKING, rather than exceptional by assertion.

Coordinate with the accepted #31 cadence contract rather than invent new cadence
outputs here. #36 must distinguish timing recommendations from quality, active
load, format capacity and duplication safety. Only timing can be overridden.
Until superseded by accepted cadence policy, existing normal two-main target,
exceptional three-main ceiling and topic derivative rules still apply. This
policy grants no unlimited extra Feed slots. Missing required cadence/Story
dependencies block dispatch, not fall back to main or silently declare success.

One development may initiate at most one allowed draft set: STORY, or STORY plus
one main. Ordinary cadence and breaking share that reservation; a second scan,
different source, severity escalation or another format cannot initiate a new
set. #36 must recheck authoritative state before reserving/dispatching, serialize
competing consumers and bind target progress to stable development/target IDs.
The initial exceptional two-target set is intentional complementary treatment,
not permission to repeat already covered material in a new set.

Track each target independently. Continuation of an already reserved exceptional
set can service only a target proven never attempted, within the same set and
after safety rechecks; never recreate a completed/possibly attempted sibling.
Any ambiguous create/publication or changed approval/coverage state pauses the
set for reconciliation/re-evaluation. Main failure must not repeat Story; Story
failure must not repeat main. Policy V1 grants no automatic later main upgrade
to a Story-only set; a distinct development or separately authorized editorial
revision is required. No existing bridge attempt limit or one-draft-per-worker
invocation rule changes; the two requests are not one multi-create bridge call.

## Versioned machine-readable output

The following is a **future data contract**, separate from `nullone.production.v1`
and `nullone.run-outcome.v1`. Fields below are required; nullable means JSON null,
not omission. V1 consumers reject unknown versions, fields, enum values or invalid
combinations without side effects. Changed meanings require a new version.

| Field | Type / values / requirement |
|---|---|
| `schema` | Literal `nullone.breaking-routing.v1`. |
| `candidate_id` | Nonempty existing candidate reference; no replacement queue status. |
| `assessment_ref` | Nonempty durable reference to scoped evidence, audience value, timing loss, scoring/config revision, eligibility findings and main-selection reasoning. |
| `state_snapshot_ref` | Nonempty durable reference to state reads, matched history, completeness, evaluated time and recent-coverage configuration. Record unavailable reads too. |
| `severity` | NORMAL, MATERIAL_BREAKING, EXCEPTIONAL_BREAKING; null only if verification is not PASS or classification is unresolved on a blocked/suppressed path. |
| `event` | Object: `event_id`, `development_id` (nonempty strings or null if unresolved), `topic_id` (nonempty string), `identity_basis` (EXACT_IDENTIFIER, CANONICAL_SOURCE, NORMALIZED_CLAIM, UNRESOLVED), `identity_refs` (array of nonempty evidence/identity references). |
| `verification` | Object: `state` (UNVERIFIED, PARTIAL, PASS, BLOCKED), `evidence_refs` (array of nonempty strings; nonempty for PASS). |
| `dedup` | Object: `decision` (EXACT_DUPLICATE, SAME_EVENT, MATERIAL_FOLLOW_UP, DISTINCT_EVENT, AMBIGUOUS_IDENTITY), `matched_refs` (array of state references), `parent_development_id` and `follow_up_reason` (nullable strings; required/non-null only for MATERIAL_FOLLOW_UP, using the stable reasons above). |
| `routing_decision` | Exactly one of the eight routes in the routing table. |
| `reason_code`, `reason_text` | A code allowed for that route above and a concise nonempty operator explanation. Codes match #27's uppercase underscore convention. |
| `draft_targets` | Ordered array: [] for normal/suppressed/blocked; [STORY] for IMMEDIATE_STORY_DRAFT; [STORY, FEED] or [STORY, CAROUSEL] for IMMEDIATE_STORY_AND_MAIN_DRAFT. |
| `main_draft_justification` | Nonempty standalone audience-value explanation for Story+main; null for all other routes. |
| `reconciliation_required` | Boolean; true for identity/state ambiguity, UNKNOWN/REVIEW_UNKNOWN, unresolved parent or unexplained consumed attempts/conflicts. False only when no ambiguity requires operator reconciliation. |

Every accelerated output requires non-null identity and severity, PASS, nonempty
identity/evidence references, DISTINCT_EVENT or MATERIAL_FOLLOW_UP, complete
authoritative state and `reconciliation_required: false`. Follow-ups always link
a different parent development and evidence-backed delta. Exact duplicates and
same-event reports require matching state references. A valid suppression can
retain an assessed severity without becoming acceleration. There is no output
field for publishing, scheduling, credentials or approval.

Example (synthetic; full examples in the fixture):

```json
{
  "schema": "nullone.breaking-routing.v1",
  "candidate_id": "candidate-b34-003",
  "assessment_ref": "fixture:B34-003:assessment",
  "state_snapshot_ref": "fixture:B34-003:state",
  "severity": "EXCEPTIONAL_BREAKING",
  "event": {
    "event_id": "event-platform-retirement",
    "development_id": "development-retirement-announced",
    "topic_id": "platform-lifecycle",
    "identity_basis": "EXACT_IDENTIFIER",
    "identity_refs": ["https://example.invalid/releases/retirement-1"]
  },
  "verification": {
    "state": "PASS",
    "evidence_refs": ["fixture:B34-003:evidence"]
  },
  "dedup": {
    "decision": "DISTINCT_EVENT",
    "matched_refs": [],
    "parent_development_id": null,
    "follow_up_reason": null
  },
  "routing_decision": "IMMEDIATE_STORY_AND_MAIN_DRAFT",
  "reason_code": "EXCEPTIONAL_MAIN_VALUE",
  "reason_text": "Confirmed retirement needs an immediate alert and a standalone migration explanation.",
  "draft_targets": ["STORY", "CAROUSEL"],
  "main_draft_justification": "Affected developers need the documented deadlines, supported alternatives and migration limits explained together.",
  "reconciliation_required": false
}
```

#27 integration: routing is a decision artifact, not a domain outcome or proof
that drafts exist. A validated persisted decision can complete a policy-evaluation
run; suppression may be an explicit valid NO_ACTION. BLOCKED_* maps to domain
BLOCKED for the attempted routing workflow with its reason. Any suppressed result with
`reconciliation_required: true` also maps to domain BLOCKED with its suppression
reason, never healthy NO_ACTION. Missing expected
decision/draft artifacts cannot become SUCCEEDED from scheduler `ok`; execution
errors use FAILED and ambiguous side effects use UNKNOWN. Keep run/occurrence IDs
separate from event/development identity. Publication UNKNOWN remains publication
state, not a healthy no-op, and is never retried.

## Deterministic acceptance examples

All events, URLs and references below and in
`tests/fixtures/breaking_routing_policy_v1.json` are synthetic. PASS rows assume
the exact evidence gate; accelerated rows also assume all relevant quality/load/
dependency gates pass unless the row explicitly says otherwise. The fixture
holds resolved input findings and full expected outputs. It does not implement
classification, matching, dispatch or any future worker.

| ID | Given | Severity | Expected route | Stable reason |
|---|---|---|---|---|
| B34-001 | Routine verified minor feature; new development | NORMAL | NORMAL_QUEUE | NORMAL_CADENCE |
| B34-002 | Official action needed before next cadence window; Story useful | MATERIAL_BREAKING | IMMEDIATE_STORY_DRAFT | MATERIAL_TIME_VALUE |
| B34-003 | Official major platform retirement; immediate alert plus standalone migration value | EXCEPTIONAL_BREAKING | IMMEDIATE_STORY_AND_MAIN_DRAFT | EXCEPTIONAL_MAIN_VALUE |
| B34-004 | Urgent viral rumor, no verified evidence | null | BLOCKED_UNVERIFIED | EVIDENCE_INSUFFICIENT |
| B34-005 | Repeat same canonical source and unchanged claims | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | EXACT_EVENT_DUPLICATE |
| B34-006 | Another article repeats the same event | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | SAME_EVENT_DIFFERENT_SOURCE |
| B34-007 | Equivalent PUBLISHED manifest, stale READY queue | EXCEPTIONAL_BREAKING | SUPPRESS_DUPLICATE | EXISTING_CONSEQUENTIAL_STATE |
| B34-008 | Equivalent publication UNKNOWN; later provider draft readback | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | EXISTING_CONSEQUENTIAL_STATE |
| B34-009 | Same event, official affected-user count changes with material practical consequence | MATERIAL_BREAKING | IMMEDIATE_STORY_DRAFT | MATERIAL_TIME_VALUE |
| B34-010 | Same topic, separate verified product version with non-urgent new value | NORMAL | NORMAL_QUEUE | NORMAL_CADENCE |
| B34-011 | Recent related coverage; proposed angle has no explicit incremental value | NORMAL | SUPPRESS_RECENT_COVERAGE | NO_INCREMENTAL_AUDIENCE_VALUE |
| B34-012 | Recent confirmed coverage, new major official user consequence and standalone main value | EXCEPTIONAL_BREAKING | IMMEDIATE_STORY_AND_MAIN_DRAFT | EXCEPTIONAL_MAIN_VALUE |
| B34-013 | Conflicting release identifiers; equivalence unresolved | null | BLOCKED_AMBIGUOUS_IDENTITY | IDENTITY_UNRESOLVED |
| B34-014 | Equivalent first human approval already recorded | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | EXISTING_CONSEQUENTIAL_STATE |
| B34-015 | Equivalent scheduled record | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | EXISTING_CONSEQUENTIAL_STATE |
| B34-016 | Equivalent PUBLISH_IN_FLIGHT / PUBLISHING / PUBLISH_ACCEPTED | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | EXISTING_CONSEQUENTIAL_STATE |
| B34-017 | Equivalent READBACK_FAILED / CHECK_REQUIRED / FAILED with consumed attempt | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | EXISTING_CONSEQUENTIAL_STATE |
| B34-018 | Equivalent REVIEW_UNKNOWN / CREATE_IN_FLIGHT or unexplained consumed create | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | EXISTING_DRAFT_REQUEST |
| B34-019 | Equivalent DRAFT_CREATED or reserved request; incoming rumor unverified | null | SUPPRESS_DUPLICATE | EXISTING_DRAFT_REQUEST |
| B34-020 | Rejected/legacy/superseded equivalent; not active load | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | CANDIDATE_EXCLUDED |
| B34-021 | Required publish ledger unreadable, no definite match | MATERIAL_BREAKING | BLOCKED_AMBIGUOUS_IDENTITY | STATE_UNAVAILABLE_OR_CONFLICTING |
| B34-022 | Exceptional value, Story eligible, main capacity exhausted | EXCEPTIONAL_BREAKING | IMMEDIATE_STORY_DRAFT | EXCEPTIONAL_STORY_ONLY |
| B34-023 | Verified material Story fails required quality gate | MATERIAL_BREAKING | BLOCKED_DRAFT_SAFETY | STORY_QUALITY_BLOCK |
| B34-024 | Verified material Story exceeds safe load | MATERIAL_BREAKING | BLOCKED_DRAFT_SAFETY | STORY_LOAD_BLOCK |
| B34-025 | Verified material, required Story/cadence dependency unavailable | MATERIAL_BREAKING | BLOCKED_DRAFT_SAFETY | DRAFT_DEPENDENCY_UNAVAILABLE |
| B34-026 | Proven new official delta but parent publication UNKNOWN | MATERIAL_BREAKING | SUPPRESS_DUPLICATE | UNRESOLVED_EVENT_HISTORY |
| B34-027 | Reused official URL now explicitly confirms availability in a new scope | MATERIAL_BREAKING | IMMEDIATE_STORY_DRAFT | MATERIAL_TIME_VALUE |
| B34-028 | Same URL with changed prose but unchanged factual claims after a long delay | NORMAL | SUPPRESS_DUPLICATE | EXACT_EVENT_DUPLICATE |

Grouped state rows have identical outputs for each named variant; the fixture
enumerates those states. #35/#36 must exercise each variant behaviorally when
implemented, as well as races, replay, aliases, true deltas and partial target
failure. Present validation checks contract structure, table alignment, reference
integrity and safety combinations only; it cannot prove future runtime behavior.

Validate offline from the repository root:

```bash
python3 tests/test_breaking_routing_contract.py
python3 tests/run_offline.py
git diff --check
```
