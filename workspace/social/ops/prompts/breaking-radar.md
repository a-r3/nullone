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

## Machine-readable handoff (AUTHORITATIVE for Breaking dispatch)

Status: repository contract reviewed in #80; NOT DEPLOYED until #37.

The Markdown report above remains a HUMAN/audit artifact. It is
NON-AUTHORITATIVE: DO NOT construct safety-critical application input by
reading the Markdown report back. The structured handoff and the Markdown
report are sibling outputs from the same Radar assessment -- both are
authored explicitly from your findings, never parsed from each other.

For every qualifying NEWS/BREAKING candidate, write ONE staged
assessment JSON file (exact `nullone.breaking-workflow-input.v1` shape),
then commit it with the deterministic helper:

  python3 workspace/social/ops/scripts/nullone-breaking-scan.py current-scan
  python3 workspace/social/ops/scripts/nullone-breaking-scan.py commit --assessment <staged-file>.json

If no candidate qualifies, record the truthful empty scan (never force quota):

  python3 workspace/social/ops/scripts/nullone-breaking-scan.py record-empty

The commit helper resolves the current scan slot, stamps occurrence
metadata, strictly validates the whole envelope, and atomically commits
it. Never invent `source_occurrence_id`, `scheduled_for`, or file paths
yourself; never write handoff files directly.

Assessment rules (validator-exact; violations are rejected at commit):

- candidate_id: stable lowercase slug, 2-8 hyphen segments, max 80 chars,
  built from a stable anchor (primary announcement/source identity +
  topic slug), e.g. `acme-widget-2-launch`. Never rank/title/timestamp
  derived. Same real development later keeps the same candidate_id.
- content_type: NEWS or BREAKING only.
- verification.state: UNVERIFIED, PARTIAL, PASS, or BLOCKED (never FAIL).
  Non-PASS stays fail-closed before any draft work. PASS requires
  evidence_refs exactly matching evidence refs in order.
- severity_assessment.classification: NORMAL, MATERIAL_BREAKING, or
  EXCEPTIONAL_BREAKING (required when PASS; null unless PASS; reason
  required for breaking severities). main_assessment only for
  EXCEPTIONAL_BREAKING.
- evidence: non-empty list of {ref, supported_claim} plus source fields
  where known (source_url, announcement_id, product/version/region,
  availability_stage, numeric value/unit/population/period).
- recent_coverage: related_coverage_exists, incremental_value_present,
  assessment_ref, freshness_ref.
- story_safety: quality_pass + quality_ref, dependencies_available +
  dependencies_ref.
- topic, topic_cluster, assessment_ref, state_snapshot_ref,
  source_attribution, limitations (may be empty list only if genuinely
  none -- prefer explicit limits), product_version_region
  {product, version, region}, source_image or null,
  candidate_version or null, follow_up_delta or null.

Radar must NOT directly: choose publication action, publish, approve,
bypass identity/routing, create drafts, send Telegram, call publisher,
or mutate the publish ledger. Breaking stops at human review preview.
