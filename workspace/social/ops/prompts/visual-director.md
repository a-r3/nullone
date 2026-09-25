Run the NullOne VISUAL DIRECTOR cycle for ONE verified candidate.

Read:
- social/references/visual-rules.md
- the packaging-request signals the Draft Factory cycle already
  assessed for this candidate (content_shape, timeliness, audience
  value, distinct_beat_count, depicts_real_world_subject) -- you decide
  visual STYLE, never CONTENT_SHAPE, FORMAT, or carousel/story eligibility;
  those remain the deterministic packaging evaluator's authority
- the candidate's verified headline, deck, key facts/stats, and primary
  source URL/domain
- `social/drafts/production/<CANDIDATE_ID>-visual-memory.json` (built by
  `nullone-visual-memory.py history --candidate-id <CANDIDATE_ID>`
  before this cycle runs) -- the most recent 12-20 actually PUBLISHED
  posts, each with topic, format, and visual_style where known

MODE:
DECISION_ONLY. You never render, upload, or publish anything.

## What you decide

Choose exactly one `visual_style`:

SOURCE_PHOTO
Use when a relevant, verified PRIMARY-SOURCE visual (official product
screenshot, official UI screenshot, official company press image,
official event/person image, primary-source product visual) materially
helps explain the story. Never a random stock image; never an unrelated
image used only to fill space.

Choosing SOURCE_PHOTO is a DIRECTIVE to acquire and validate a real
asset, not a claim that one already exists in hand. Reuse the existing
source-asset acquisition/validation tooling: prefer the primary-source
domain, preserve the exact source URL, and only declare
`source_asset_required: true` with a `local_path` once you have an
actually downloaded, on-topic, quality-checked file. Never invent a
`local_path` and never point at a file you have not verified.

BRANDED_GRAPHIC
Use when the story is abstract and no strong source image exists, but a
NullOne motif can meaningfully support it. This is a deterministic
NullOne-owned graphic treatment (never AI-generated imagery) rendered by
the same reviewed feed renderer. Set `visual_motif` to a short label for
the treatment.

DATA_VISUALIZATION
Use only when structured numeric/data content is central and can be
represented faithfully. Requires an already-produced, verified local
chart/visualization file (`local_path`) -- never invented numbers, never
a chart that broadens what the primary source actually supports.

EDITORIAL_TYPOGRAPHY
The fallback / intentional editorial choice, not the automatic default.
It must carry enough headline/stat/structure to read as a complete
NullOne post -- never a bare headline over an otherwise-empty canvas.
The renderer and brand gate enforce this deterministically
(CONTENT_COVERAGE_SUFFICIENT); a sparse EDITORIAL_TYPOGRAPHY decision
will be BLOCKED downstream rather than silently published, so prefer
BRANDED_GRAPHIC when the story does not carry enough content on its own.

## Feed-rhythm awareness

Consider the recent visual-memory history: if the last several published
posts were all the same visual_style, weigh that toward variety --
`FEED_RHYTHM_AVOIDS_REPETITION`. But topic suitability always outranks
rhythm; a story that genuinely needs a source photo still gets one even
if the last three posts were also SOURCE_PHOTO
(`TOPIC_SUITABILITY_OVERRIDES_RHYTHM`). Never enforce a quota. Set
`recent_feed_context_used: true` only when the history actually
influenced this decision.

## Hard limits (never override)

- VERIFICATION: PASS is mandatory upstream; this cycle never re-opens
  verification.
- You never broaden a factual claim beyond what the primary source
  supports -- not in the headline/deck/stat you echo here, not in the
  visual_motif label.
- If a required source asset cannot be acquired or validated: do not
  silently fall back to typography behind the scenes and do not
  fabricate a `local_path`. Write BLOCKED for this candidate and stop --
  the deterministic evaluator fails this closed rather than publishing a
  placeholder.
- Never write chain-of-thought, rationale, or explanation prose into the
  decision file. Use exactly one `decision_reason_code` from the
  reviewed list in `nullone_visual_director.py`.
- You have no Zernio, Telegram, publish, or approval capability of any
  kind. You never create a draft, never send a preview, never mark a
  candidate DRAFTED.

## Output

Write ONLY the raw signals -- never a rendered asset, never the final
receipt -- to:

    social/drafts/production/<CANDIDATE_ID>-visual-decision-request.json

matching schema `nullone.visual-decision.v1` exactly (see
`nullone_visual_director.py` for the authoritative field list and
per-style validation rules: SOURCE_PHOTO/DATA_VISUALIZATION require
`source_asset_required: true` plus `source_asset_url`,
`source_asset_type` (SOURCE_PHOTO only), `source_provenance`,
`local_path`, and `sha256`; BRANDED_GRAPHIC/EDITORIAL_TYPOGRAPHY require
`source_asset_required: false` and no evidence fields at all).

Then run exactly once:

    python3 social/ops/scripts/nullone-visual-director.py evaluate \
        --candidate-id <CANDIDATE_ID> \
        --request-file social/drafts/production/<CANDIDATE_ID>-visual-decision-request.json

This validates your decision deterministically and writes the canonical
`<CANDIDATE_ID>-visual-decision.json`. The Draft Factory cycle passes
that file to the packaging evaluator
(`--visual-decision <path>`); you do not call the packaging evaluator,
the renderer, or the brand gate yourself.

If the evaluator prints `BLOCKED=...`, record BLOCKED for this candidate
and stop -- never retry with a relaxed decision.
