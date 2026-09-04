# NullOne Visual Rules v1

Operational design spec for @nullone.az. Dimensions are platform-fixed; the rest are NullOne's own rules (derived from observed reference patterns + NullOne's AI-first explainer positioning). This is a spec to follow, not a record of reference-account design.

---

## FEED (single image) — 1080 × 1350

### Canvas
- **Exactly 1080×1350** (4:5). No other feed size for now.

### Headline / line guidance
- Cover headline: **1 line preferred, 2 lines max.**
- **≤ 38 characters** on the card (shorter = more legible at feed thumbnail size).
- Azerbaijani headline carries the number + outcome (e.g., "AI xərclər 32 trilyon $ ola bilər").
- If a second line is needed, it must be a subordinate clause, not a second fact.

### Text density
- **One headline + one supporting number** on a card. Never a paragraph on a feed image.
- Max 2 blocks of text: (1) headline, (2) a single stat/label. Leave ~60% of the card for visual/negative space.

### Source image placement
- Official/source image = hero, occupying the **upper ~60%** of the card; text sits in the **lower band** on a solid/dark scrim.
- If no source image: full-bleed flat/color field with centered typography.
- Screenshot policy: crop to the relevant region; do not show browser chrome unless it adds context.

### Logo / branding
- Small **"NULLONE"** wordmark or `@nullone.az` handle, bottom-right, low opacity, inside the safe margin.
- No watermark over the headline. Branding must not compete with the hook.

### Margins / safe areas
- **90 px outer margin** on all sides for text and branding.
- Keep the headline within the central **1080 × 990** box (top/bottom 135 px padding reserved).
- Critical elements ≥ 60 px from any edge.

### Hierarchy
1. Headline (largest, bold)
2. Number/stat (accent color, second-largest)
3. Source credit line (small, muted) if quoting a primary source
4. Brand mark (smallest, corner)

---

## CAROUSEL — 1080 × 1350 every slide

### Slide spec
- **Every slide exactly 1080×1350.**
- Consistent template across all slides (same margins, font sizes, color accents) — one visual family, unlike references' mixed look.

### Cover (slide 1)
- Headline (≤ 38 chars, 1–2 lines) + one hook number.
- Optional small label: "Karusel · izah" or "Necə işləyir" as a category tag.
- End with a **"sürüşdür →" (swipe) cue** near the bottom (proven in AZ market by Trilogy).

### Inner slides (2 … N-1)
- **One idea per slide.**
- Max 2 text blocks: a short heading (≤ 24 chars) + up to 2 lines of body (≤ 90 chars total).
- One number or one visual element per slide; do not stack two facts on one slide.

### Final slide
- Takeaway / "why it matters" (1 line) + handle/brand + optional CTA ("Arxivə keç →").

### Maximum practical text density
- **≤ 90 characters per inner slide**, ≤ 38 chars headline on cover. Beyond this, split into another slide.

---

## STORY — 1080 × 1920

### Canvas
- **Exactly 1080×1920** (9:16).

### Safe zones
- **Top 220 px** = UI zone (username, close) — keep clear.
- **Bottom 220 px** = UI zone (reply bar, swipe) — keep clear.
- Safe content band ≈ **y 220 → y 1700**. Headline in the **upper-middle** of this band; CTA near the bottom edge of the safe band.

### Hierarchy
1. Headline (largest, upper-middle)
2. Number/stat (accent)
3. Source line (small)
4. Optional CTA ("Link bioda" / "Sürüşdür")

### Image placement
- Source image full-bleed with a dark scrim under the text; or split layout (image top 50%, text bottom 50%).

### Frames by content type
- **Breaking / one fact:** 1 frame — headline + number.
- **Context/teaser:** 1–2 frames — hook, then "why you should care" / link to feed post.
- **Mini explainer:** 2–3 frames — claim → mechanism → takeaway.

---

## Reusable templates

### 3 Feed templates
1. **Stat-led news brief** — source image top 60% + dark lower band; headline (number + outcome) + one accent stat + brand mark.
2. **Product/update card** — official product image as hero + one-line "what's new" headline + version/model label.
3. **Quote/source card** — large quote (≤ 40 chars) + source attribution line + brand mark; muted background.

### 3 Carousel structures
1. **Explainer (6–8 slides)** — cover(hook+number) → context → mechanism → numbers → limitation → takeaway → brand/CTA.
2. **List/digest (5–7 slides)** — cover("günün xəbərləri" + date + swipe) → one item per slide → closing summary.
3. **Comparison (4–6 slides)** — cover(question) → item A → item B → verdict → takeaway.

### 3 Story structures
1. **Breaking (1 frame)** — headline + number, source line.
2. **Teaser→feed (2 frames)** — hook → "ətraflı feed postunda" + CTA.
3. **Mini explainer (3 frames)** — claim → how → why it matters.

---

## Standing visual constraints
- No AI-generated imagery when a source/official visual or screenshot exists.
- One consistent typeface + 2 accent colors across all formats.
- Dark scrims for text legibility over images.
- Brand mark always bottom-right, subtle, inside safe margin.

## Carousel visual system V2

Carousel slides must not become a sequence of identical text cards.

Use visual rhythm.

### Slide roles

COVER
- Prefer an official/source visual where available.
- Strong headline is the dominant editorial element.
- One hook/stat maximum.
- Must work at Instagram grid thumbnail size.

STAT
- The number must be the largest element on the slide.
- Examples: 100%, 91.5%, 2 ZERO-DAY.
- Supporting explanation stays short.

EXPLAINER
- Short title + one concise explanation.
- Never fill the slide with paragraph text.
- Use an icon, source visual, diagram-like device or spatial composition when useful.

LIMITATION / SAFETY
- Visually distinguish restrictions, uncertainty, safeguards and caveats.
- Do not bury limitations in small body copy.

FINAL
- Large "Niyə vacibdir?" takeaway.
- One memorable conclusion.
- NullOne brand visibly closes the sequence.

### Visual rhythm

Do not use the exact same composition for every slide.

A 6–8 slide carousel should normally contain at least:
- 1 hero/cover composition
- 2 strong stat compositions
- 2–3 explainer compositions
- 1 takeaway/final composition

### Typography

Body copy must be comfortably readable on a phone.
If explanation requires small text, shorten it or split it into another slide.

### NullOne accent

Base:
- near-black
- white
- neutral gray

Primary accent:
- Signal Orange (#FD4503)

Use accent selectively for:
- key numbers
- labels
- lines
- progress elements
- important words

Never turn the whole slide into a bright color field merely for decoration.

## Instagram Story V2 — default

Renderer:
social/tools/render_story_v2.py

Legacy:
social/tools/render_story.py

Canvas:
1080x1920 exactly.

Story V2 is the default renderer.

### Layout selection

BIG-STAT
Use when one verified number is the strongest hook:
- benchmark result
- revenue
- price
- users
- percentage
- funding
- performance delta

The number must be source-verified and meaningful without misleading context.

COMPARISON
Use when two directly comparable verified values are central to the story.
Preferred for:
- model A vs model B
- before vs after
- price changes
- performance changes
- market/share comparisons

Both values must refer to the same metric or genuinely comparable measurements.

BREAKING
Use only for genuinely time-sensitive developments.
Do not use "BREAKING" merely to make ordinary news look urgent.
Prefer a source/product visual when available because text-only breaking cards
are visually weak.

EXPLAINER
Use when the main value is understanding a concept rather than reacting to
a headline or number.

### Composition

Prefer:
source visual + editorial hierarchy
over:
plain text on an empty background.

A Story must contain a visual reason for its layout.

Large empty areas are acceptable only when they create deliberate visual
tension. Do not leave empty space simply because the template lacks content.

### Source visuals

When a suitable official/source image exists:
- use it especially for breaking and product stories
- preserve important parts of the image
- use dark scrim/gradient for readability
- never stretch
- never destructively crop the subject

For statistical/comparison Stories, source imagery is optional if the data
visual itself is sufficiently strong.

### Fact wording

A visual statistic must preserve the exact scope of the source.

Example:
Prefer:
"ExploitBench qiymətləndirməsində 100% nəticə göstərib"

Do not broaden that automatically into:
"bütün tapşırıqları keçib"

unless the primary source explicitly supports that wording.
