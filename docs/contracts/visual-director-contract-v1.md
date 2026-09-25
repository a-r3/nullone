# NullOne Visual Director Contract V1

Status: PROPOSED
Scope: deterministic visual-STYLE decision only — SOURCE_PHOTO vs
BRANDED_GRAPHIC vs DATA_VISUALIZATION vs EDITORIAL_TYPOGRAPHY, for one
already-verified, already-`POST`-decided candidate. Does not decide
CONTENT_SHAPE, FORMAT_DECISION (SINGLE_POST/CAROUSEL/STORY/SKIP), or
carousel eligibility — those remain `editorial-packaging-contract-v1.md`
/ `nullone_packaging_policy.py`'s unchanged authority.
Production impact: repo-only. This PR wires the decision step directly
into `workspace/social/ops/prompts/draft-factory.md`'s existing agent
turn (see "Insertion point" below) so it is exercised the next time that
prompt file is deployed and run — but no scheduler, OpenClaw job, cron
entry, or live automation is touched by merging this PR, and the
prompt-file deploy itself is a separate, explicit step this PR does not
perform.

## Purpose

Real production proof (2026-09-25,
`amazon-seller-assistant-claude-2026-09-25`) showed the failure mode
this contract exists to close: a `SINGLE_FACT` candidate with no
discovered source asset fell through the packaging contract's existing
evidence ladder straight to `EDITORIAL_TYPOGRAPHY` — the only "give up"
default that branch ever produced — and rendered as a headline, one
stat, and roughly 58% empty black canvas
(`content_coverage_ratio` ≈ 0.34 against the 0.45 floor this contract
introduces). Nothing in the pipeline stopped it: the packaging contract
never claimed to choose *good* typography, only *a* format, and PR
#164's brand gate only ever blocked a stat-bearing render, never an
under-filled one.

The real `@nullone.az` feed mixes official/source screenshots,
product/UI visuals, source-grounded photos, branded abstract graphics,
data/stat graphics, and typography-led layouts. Typography-only must
never be the automatic default. This contract makes NullOne's runtime
itself choose the visual style for every new post, from richer context
(recent published visual rhythm, the verified story, already-discovered
source assets) than the packaging contract's raw asset booleans alone
provide — and makes an under-filled EDITORIAL_TYPOGRAPHY card
mechanically unable to reach Zernio, not just editorially discouraged.

Claude Code is not part of this runtime decision. The decision is made
by NullOne's own `visual_director` role (a Sonnet-class model, invoked
through the existing provider-routing architecture) and is validated,
bound, and enforced entirely by deterministic code
(`nullone_visual_director.py`, `nullone_packaging_policy.py`,
`nullone_brand_gate.py`) before anything reaches Zernio or a human
reviewer.

## Current-system audit (before this contract)

- `CURRENT_VISUAL_DECISION_OWNER`: the packaging contract's
  `_resolve_visual_style` ladder in `nullone_packaging_policy.py`, fed
  by raw `assets.*` booleans the Draft Factory model assessed. When a
  real photo/evidence is required, the ladder is genuinely good (real
  asset → REAL_PHOTO/SOURCE_SCREENSHOT; else data-viz; else SKIP —
  never synthetic illustration standing in for missing grounding). The
  gap is entirely the "neither required nor available" branch: it
  always silently returned `EDITORIAL_TYPOGRAPHY` (or an unusable
  `GENERATED_ILLUSTRATION_ALLOWED` stub the renderer refuses outright),
  with zero awareness of recent feed rhythm and zero notion of an
  abstract branded-graphic alternative.
- `CURRENT_VISUAL_STYLE_INPUT`: five deterministic booleans
  (`has_official_or_source_image`, `has_usable_screenshot`,
  `image_on_topic`, `image_quality_ok`, `data_visualization_possible`)
  the Draft Factory model self-reports before packaging evaluation —
  not model-driven judgment about which style best serves the story,
  only "does evidence exist".
- `CURRENT_PHOTO_DISCOVERY_PATH`: none, formally. Draft Factory's prompt
  says "prefer an official/source asset already exposed by the primary
  source" and "do not search through many alternative images", but no
  deterministic tooling in this repository discovers, downloads, or
  validates a candidate source asset — the model either already has one
  from its own research or reports `false`, as it did on 2026-09-25 for
  a story (Amazon Seller Central × Claude) that plausibly had an
  available official screenshot.
- `CURRENT_PUBLISHED_HISTORY_SOURCE`: none consulted by the packaging
  contract at all. `social/state/publish-ledger.jsonl` and
  `social/ops/manifests/*.json` exist and already carry everything
  needed (see "Visual memory" below) but nothing in the packaging path
  reads them.
- `INSERTION_POINT_FOR_VISUAL_DIRECTOR`: inside `draft-factory.md`,
  between the existing "Apply VERIFICATION" step and the existing
  "Format selection — Editorial Packaging Contract" step, as a new
  "Visual Director" step (see "Insertion point" below).

## Schema: `nullone.visual-decision.v1`

Raw, model-authored document (written to
`social/drafts/production/<CANDIDATE_ID>-visual-decision-request.json`,
never edited by hand afterward) and its validated/persisted form (the
same fields plus `decision_hash`, written only by
`nullone-visual-director.py evaluate` to the canonical
`<CANDIDATE_ID>-visual-decision.json`). See
`nullone_visual_director.py` for the authoritative implementation;
this section is the human-readable mirror.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | literal | `nullone.visual-decision.v1` |
| `contract_version` | literal | `1.0.0` |
| `candidate_id` | string | Path-safe candidate identity |
| `visual_style` | enum | `SOURCE_PHOTO` \| `BRANDED_GRAPHIC` \| `DATA_VISUALIZATION` \| `EDITORIAL_TYPOGRAPHY` |
| `source_asset_required` | bool | `true` iff `visual_style` is evidence-backed |
| `source_asset_url` | string\|null | Exact primary-source URL (evidence styles only) |
| `source_asset_type` | enum\|null | `official_photo` \| `official_screenshot` \| `product_ui` \| `official_graphic` (SOURCE_PHOTO only) |
| `source_provenance` | string\|null | Human-readable provenance note (evidence styles only) |
| `local_path` | string\|null | Already-downloaded, workspace-contained file (evidence styles only — never a bare remote URL) |
| `sha256` | string\|null | Declared hash, cross-checked against the actual file |
| `headline` | string | Verified headline echoed for auditability (≤ 200 chars) |
| `deck` | string\|null | Optional supporting line (≤ 320 chars) |
| `stat` | string\|null | Optional verified stat (≤ 200 chars) |
| `visual_motif` | string\|null | Short motif label (BRANDED_GRAPHIC only; renderer draws one fixed deterministic treatment regardless of the exact label in this v1) |
| `decision_reason_code` | enum | One of the reviewed codes below — never free text |
| `recent_feed_context_used` | bool | Whether visual memory actually influenced this decision |

No chain-of-thought/rationale field exists in this schema on purpose:
`reasoning`, `chain_of_thought`, `rationale`, `notes`, and `explanation`
are explicitly forbidden keys — a request carrying any of them is
rejected outright (`VISUAL_DECISION_INVALID: forbidden free-text
field(s)`), not silently stripped.

### Reason codes

`SOURCE_PHOTO_STRONG_PRIMARY_MATCH`,
`BRANDED_GRAPHIC_ABSTRACT_NO_SOURCE`,
`BRANDED_GRAPHIC_AVOIDS_TYPOGRAPHY_REPETITION`,
`DATA_VISUALIZATION_CENTRAL_METRIC`,
`EDITORIAL_TYPOGRAPHY_DELIBERATE_FALLBACK`,
`EDITORIAL_TYPOGRAPHY_SUFFICIENT_CONTENT`,
`FEED_RHYTHM_AVOIDS_REPETITION`,
`TOPIC_SUITABILITY_OVERRIDES_RHYTHM`.

### Malformed input fails closed

`validate_decision` (pure function, no I/O beyond the one local-file
containment/hash check evidence styles require) raises
`VisualDirectorError` — never defaults — on: unknown schema/contract
version, an unrecognized `visual_style` or `decision_reason_code`, a
forbidden field, a non-boolean `recent_feed_context_used`/
`source_asset_required`, a blank/oversized text field, an evidence style
missing its required fields, a non-evidence style naming any evidence
field at all (smuggling blocked both directions), a `local_path` that is
missing, not a regular file, a symlink, or outside the workspace, and a
declared `sha256` that does not match the actual file.

## Format-selection policy

**SOURCE_PHOTO** — a relevant, verified primary-source visual (official
product/UI screenshot, official press image, official event/person
image, primary-source product visual) materially helps explain the
story. Never random stock imagery; never an unrelated image used only to
fill space. Choosing SOURCE_PHOTO is a *directive to acquire and
validate* a real asset, reusing existing source-asset discovery/
validation conventions (prefer the primary-source domain, preserve the
exact URL, validate the downloaded file) — never a claim that evidence
already exists in hand.

**BRANDED_GRAPHIC** — the story is abstract and no strong source image
exists, but a NullOne motif can meaningfully support it. Deterministic,
code-drawn (an accent rule + a fixed low-opacity ring motif in Signal
Orange, `render_texbrif_v2.py._draw_branded_graphic_motif`) — never
AI-generated imagery.

**DATA_VISUALIZATION** — only when structured numeric/data content is
central and can be represented faithfully; requires an already-produced,
verified local chart file. Unchanged from the packaging contract's
existing DATA_VISUALIZATION treatment.

**EDITORIAL_TYPOGRAPHY** — the fallback / intentional editorial choice,
never the silent default. It must carry enough content to read as a
complete post: `nullone_brand_gate.evaluate_brand_gate`'s
`CONTENT_COVERAGE_SUFFICIENT` check requires
`content_coverage_ratio >= 0.45` (the renderer's own measured fraction
of the typography band actually filled with text/motif — the
2026-09-25 incident measured ≈ 0.34). It must NOT produce headline + one
stat + mostly empty canvas; a render that would is refused by the brand
gate, not silently published.

## Feed-rhythm awareness

The Visual Director reads a compact recent-published-history summary
(see "Visual memory" below) and may weigh recent repetition toward
variety (`FEED_RHYTHM_AVOIDS_REPETITION`) — but topic suitability always
outranks rhythm (`TOPIC_SUITABILITY_OVERRIDES_RHYTHM`); nothing in this
contract enforces a hard quota. `recent_feed_context_used` records
whether the history actually mattered to the decision, for later audit.

## Visual memory

`nullone_visual_memory.py` (pure) + `nullone-visual-memory.py` (CLI)
build the bounded, PUBLISHED-only history from existing durable local
production records — never Instagram scraping:

- Source: `social/state/publish-ledger.jsonl`, filtered to
  `result == "PUBLISHED"` — the exact predicate
  `nullone_cadence_state_adapter.py` already uses for audience-facing
  counting — joined to `social/ops/manifests/<manifest_id>.json` for
  `format`/`visual_style`/`asset_kind`/media path.
- Excluded by construction: `social/state/candidate-queue.md` and
  `social/state/topic-ledger.jsonl` are never read for this purpose — a
  rejected draft, an abandoned manifest, or an unpublished experimental
  render can never enter visual memory, only a row whose ledger `result`
  is the confirmed terminal `PUBLISHED`.
- Bounded window: 12–20 records (default 16), newest first, deduplicated
  by post id. A present-but-malformed ledger/manifest fails closed
  (`VisualMemoryError`); an absent-but-otherwise-valid state root is a
  legitimate empty history, not an error.
- No image bytes, no analytics dependency: only compact per-post
  metadata (topic, format, visual_style, source_photo_used,
  final_render_path) is ever returned. Analytics may be added later but
  is explicitly not a hard dependency of this first implementation.

## Insertion point

Inside `workspace/social/ops/prompts/draft-factory.md`, as a new
"Visual Director — visual-style decision (runs before packaging)"
section between the existing verification step and the existing
"Format selection — Editorial Packaging Contract" section:

1. `nullone-visual-memory.py history --candidate-id <ID>` (deterministic,
   no model call) writes the bounded history context.
2. The (same, currently — see "Model / cost policy") agent turn follows
   `workspace/social/ops/prompts/visual-director.md` to choose
   `visual_style` and writes the raw decision request.
3. `nullone-visual-director.py evaluate --candidate-id <ID> --request-file
   <request>.json` validates it and writes the canonical decision.
4. The existing packaging-evaluator invocation gains one new optional
   flag, `--visual-decision <path>`, which loads and hash-verifies the
   decision and maps it onto the ONE new optional packaging-contract
   input, `candidate.visual_director_style` — consulted ONLY inside
   `nullone_packaging_policy._resolve_visual_style`'s existing
   `not visual_evidence_required` branch (the exact branch that used to
   default blindly to EDITORIAL_TYPOGRAPHY). Every other branch —
   `REAL_PHOTO_REQUIRED`, `VISUAL_EVIDENCE_REQUIRED`, the SKIP gates, the
   evidence ladder when real evidence genuinely exists — is completely
   unchanged and untouched by this contract; a hard requirement always
   wins and the Visual Director is never consulted for it. Absent
   `--visual-decision`, `nullone-packaging-evaluator.py` behaves
   byte-identically to before this contract existed (proven by
   `tests/test_visual_director_contract.py
   ::PackagingDirectiveIntegrationTests
   ::test_legacy_behavior_unchanged_without_visual_director`).

`BRANDED_GRAPHIC` is additive to `nullone_packaging_policy.VISUAL_STYLES`
and to `nullone_packaging_receipt.STYLE_TO_ASSET_KIND` (mapped to asset
kind `NONE`, exactly like `EDITORIAL_TYPOGRAPHY` — no external evidence
file). No existing enum value was renamed or removed.

## Model / cost policy

Editorial/visual reasoning happens in a `visual_director` role, added to
`nullone_provider_router.py`'s reviewed five roles (now six):
`transport=claude`, `model=sonnet` — the exact reviewed
`claude -p --model sonnet` route Morning Editorial already runs in
production, reused rather than duplicated, per "prefer a model class
that matches the existing role-routing architecture". No Opus, no new
transport, no OpenCode-hosted model change. Capabilities are
`("visual-reasoning", "draft-artifacts")` — explicitly no
`web-research`: the Visual Director must never broaden a factual claim
by going looking for new evidence; it reasons only over already-verified
signals and already-discovered assets.

**v1 wiring**: the decision step above runs inside the Draft Factory
cycle's own agent turn (reusing Draft Factory's `draft_factory` role and
existing file-path capability grant), exactly like the packaging-signal
assessment step it precedes — no new scheduler/orchestration entry is
needed for this to work. The standalone `visual_director` provider-
router role, `nullone_claude_visual_director_provider.py` adapter,
`workspace/.opencode/agents/nullone-visual-director.md` agent, and
`workspace/social/ops/prompts/visual-director.md` prompt all exist and
are offline-tested (`nullone_provider_router.py self-test`,
`nullone_claude_visual_director_provider.py`) so a later, separately
reviewed cutover to a genuinely independent process — matching how Story
Writer already runs separately from Draft Factory — needs no additional
router, capability, or contract work, only an orchestration/deployment
change this PR does not make.

Deterministic code decides: schema validation, asset validation,
hashing, provenance containment, rendering, bounds, the brand gate,
publication safety. The model decides only the editorial visual
direction within that deterministic envelope.

## Renderer responsibility

`render_texbrif_v2.py` never invents visual strategy; it receives
`--visual-style` (one of the five packaging-contract tokens, passed
through verbatim from `receipt["VISUAL_STYLE"]` by the render
dispatcher) and executes it:

- `REAL_PHOTO` / `SOURCE_SCREENSHOT` / `DATA_VISUALIZATION`: unchanged
  photo-hero layout (a validated local file is always the `--source`).
- `EDITORIAL_TYPOGRAPHY`: unchanged typography-only layout (byte-
  identical output to before this contract when the same arguments are
  given — proven by the unmodified PR #160 regression tests), now also
  reporting `content_coverage_ratio` in its brand-metadata sidecar.
- `BRANDED_GRAPHIC`: the same typography layout plus the deterministic
  motif (`_draw_branded_graphic_motif`) filling the vertical negative
  space an empty typography card would otherwise leave.

PR #160 fixes are preserved unmodified: no empty photo-frame placeholder
when `--source` is absent, and `fit_stat`'s long-stat wrap/shrink loop
(`RenderBoundsError` fail-closed on genuine overflow) is untouched —
verified by the existing `tests/test_v2_renderer_layout_correctness.py`
and `tests/test_v2_brand_compliance.py` suites, unchanged and passing.

## Brand gate: template-aware (PR #164 disposition)

`nullone_brand_gate.py` is rewritten to be template-aware rather than
one-size-fits-all, closing two gaps PR #164 left open:

1. **The vacuous-pass loophole.** PR #164's gate only ever blocked when
   `stat_present` was true (`gate = BLOCKED if (stat_present and failed)
   else PASS`); a bare headline-only render, or any render whose margin/
   brand-mark/text-bounds checks failed, passed unconditionally whenever
   no stat was present. This contract makes the six structural checks
   (`MIN_MARGIN_90PX`, `BRAND_MARK_COUNT_EXACTLY_ONE`,
   `BRAND_MARK_BOTTOM_RIGHT`, `BRAND_MARK_LOW_OPACITY`,
   `TEXT_BOUNDS_VALID`, and the vacuously-true-without-a-stat
   `STAT_ACCENT_COMPLIANT`) gate every render regardless of stat
   presence.
2. **No coverage/region check at all.** Nothing in PR #164 could detect
   an under-filled typography card or a photo-style render that
   accidentally carried no photo. Two new per-style checks close this:
   `VISUAL_REGION_PRESENT` (SOURCE_PHOTO/SOURCE_SCREENSHOT/
   DATA_VISUALIZATION require `has_photo: true`) and
   `CONTENT_COVERAGE_SUFFICIENT` (EDITORIAL_TYPOGRAPHY/BRANDED_GRAPHIC
   require `content_coverage_ratio >= 0.45`).

Per-style required check sets:

- `BRAND_GATE_SOURCE_PHOTO` / `BRAND_GATE_DATA_VISUALIZATION`: base
  checks + `VISUAL_REGION_PRESENT`.
- `BRAND_GATE_BRANDED_GRAPHIC` / `BRAND_GATE_EDITORIAL_TYPOGRAPHY`: base
  checks + `CONTENT_COVERAGE_SUFFICIENT`.

`renderer VALID=true` was never treated as brand `PASS` (the two were
already evaluated as separate gates by PR #164's dispatcher wiring); this
contract preserves that separation and simply makes the brand gate
itself correctly template-aware.

PR #164's genuinely reusable pieces — the brand-metadata sidecar
pattern, the canonical-accent/margin/brand-mark checks, the metadata-
before-pixels design rationale, its `nullone-packaging-render.py` gate-
enforcement wiring, and its test infrastructure
(`tests/test_v2_brand_compliance.py`) — are kept and extended in place,
not replaced. Its one narrowing assumption (blocking scoped to
stat-bearing posts only) is removed as described above.

**Disposition**: this PR is a NEW branch (`feature/nullone-visual-
director`) stacked directly on top of `fix/v2-brand-compliance-gate`
(PR #164's head, `f73569851ef3b16c2b5e949438fa8c41ae6f05cc`) rather than
further commits pushed onto #164 itself. The scope here (a new
provider-routing role, a new contract module, visual memory, template-
aware brand-gate redesign, a new renderer style) is materially larger
than #164's stated scope ("enforce documented NullOne brand compliance
before delivery"). #164's own diff remains valid and is not
contradicted — every check it added still exists, generalized rather
than narrowed. **Do not merge #164 independently of, or after, this PR
without reviewing both together**: this PR's `nullone_brand_gate.py`
supersedes #164's version file-for-file (same file, template-aware
extension), so merging #164 alone first and this PR second is the
correct order if both are accepted; merging them in the other order, or
partially, would silently drop the template-awareness fix.

## Tests

`tests/test_visual_director_contract.py` (25 cases) plus extensions to
`tests/test_v2_brand_compliance.py`, `tests/test_packaging_wiring.py`,
and `tests/test_provider_role_router.py` cover: schema validation and
fail-closed malformed input; a valid SOURCE_PHOTO decision with a
hash-verified local asset; a missing/tampered SOURCE_PHOTO asset failing
closed; no bare-URL stock-photo fallback; the exact 2026-09-25 sparse-
typography shape blocked by the brand gate; PUBLISHED-only visual
memory excluding an in-flight/rejected row even when a manifest exists
for it; the bounded 12–20 window; PR #160's no-empty-photo-frame and
stat-overflow fixes still passing unmodified; all four per-style brand-
gate check sets; the receipt's `VISUAL_STYLE` (including
`BRANDED_GRAPHIC`) persisted into the manifest's `packaging` block; and
a static capability-negative guard proving neither Visual Director
module can reach Zernio/Telegram/publish/approval transport. Run via
`tests/run_offline.py` (registered) or directly:

    python3 -m pytest tests/test_visual_director_contract.py tests/test_v2_brand_compliance.py tests/test_packaging_wiring.py tests/test_packaging_policy.py tests/test_provider_role_router.py -q

## Relationship to existing repository material

This contract governs **visual style** only. It does not change:

- `editorial-packaging-contract-v1.md` / `nullone_packaging_policy.py`'s
  FORMAT_DECISION, carousel eligibility, or SKIP logic — all untouched,
  all existing tests green without modification to their assertions
  (only additive fixture/mock updates for the new metadata fields the
  renderer now emits).
- verification (`VERIFICATION: PASS/BLOCKED` remains the unconditional
  upstream gate);
- publication, approval, or the two-stage human-approval boundary — the
  Visual Director has no path to any of them (proven by
  `NoPublicationCapabilityTests` in `tests/test_visual_director_contract
  .py`);
- Story production (`render_story_v2.py`, `StoryWorkflow`) — out of
  scope for this PR; Story's own "large empty areas ... only when they
  create deliberate visual tension" rule in `visual-rules.md` already
  covers Story, unmodified here.

## Deferred / explicitly out of scope

- Extending BRANDED_GRAPHIC as a re-evaluation option inside the
  packaging contract's `VISUAL_EVIDENCE_REQUIRED`-but-unmet branch
  (today still a hard SKIP for ANNOUNCEMENT/COMPARISON without
  evidence) — would touch reviewed SKIP safety logic and needs its own
  dedicated review; this PR only closes the
  `VISUAL_EVIDENCE_REQUIRED == NO` gap the 2026-09-25 incident actually
  hit.
- A genuinely separate Visual Director process/schedule cutover (see
  "Model / cost policy").
- Basic analytics/engagement as a visual-memory input — explicitly not a
  hard dependency of this first implementation.
- Multiple BRANDED_GRAPHIC motif treatments selected by `visual_motif`'s
  exact value — v1 renders one fixed deterministic motif regardless of
  the label.
