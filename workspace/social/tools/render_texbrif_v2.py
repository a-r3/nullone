#!/usr/bin/env python3

import argparse
import hashlib
import io
import json
import math
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageStat


W, H = 1080, 1350
HERO_H = 735

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# NullOne brand palette (social/references/visual-rules.md). Named here so
# every draw call traces back to one canonical source instead of repeating
# magic RGB tuples.
BG = (14, 14, 15)  # #0E0E0F
TEXT_PRIMARY = (242, 234, 225)  # #F2EAE1
ACCENT = (253, 69, 3)  # #FD4503 -- Signal Orange, key numbers/labels only
METADATA = (164, 161, 157)  # #A4A19D -- approved neutral metadata treatment

# visual-rules.md: "90 px outer margin on all sides for text and branding."
MARGIN = 90

# Visual V2 brand-compliance gate (issue: Visual V2 brand compliance):
# the renderer is the only place that truly knows what it drew, so it
# emits this semantic metadata sidecar rather than making a downstream
# gate re-derive the same facts by scanning pixels.
BRAND_METADATA_SCHEMA = "nullone.render-brand-metadata.v1"
BRAND_MARK_OPACITY = 170  # < 255 -- "low opacity" bottom-right handle only


def fnt(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size)


def load_image(src):
    if src.startswith("http://") or src.startswith("https://"):
        req = urllib.request.Request(
            src,
            headers={"User-Agent": "Mozilla/5.0 NullOneRenderer/2.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGB")

    return Image.open(src).convert("RGB")


def wrap(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""

    for word in words:
        candidate = word if not current else f"{current} {word}"
        box = draw.textbbox((0, 0), candidate, font=font)

        if box[2] - box[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def fit_headline(draw, text):
    max_width = W - 2 * MARGIN
    for size in range(72, 47, -2):
        font = fnt(size, True)
        lines = wrap(draw, text, font, max_width)

        if len(lines) <= 3:
            return font, lines, int(size * 1.12)

    font = fnt(48, True)
    return font, wrap(draw, text, font, max_width), 55


def fit_stat(draw, text, max_width, start=31, minimum=20, max_lines=3):
    for size in range(start, minimum - 1, -1):
        font = fnt(size, True)
        lines = wrap(draw, text, font, max_width)

        if len(lines) <= max_lines:
            return font, lines, int(size * 1.3)

    font = fnt(minimum, True)
    return font, wrap(draw, text, font, max_width), int(minimum * 1.3)


class RenderBoundsError(RuntimeError):
    """Raised when drawn content would fall outside the output canvas."""


VISUAL_STYLES = (
    "REAL_PHOTO",
    "SOURCE_SCREENSHOT",
    "DATA_VISUALIZATION",
    "EDITORIAL_TYPOGRAPHY",
    "BRANDED_GRAPHIC",
)

# The only styles this renderer draws a no-photo layout for; the other
# three arrive with --source already set by the render dispatcher (the
# receipt's VISUAL_STYLE, passed through verbatim for observability/
# brand-gate reporting -- this renderer does not re-decide it).
NO_PHOTO_STYLES = ("EDITORIAL_TYPOGRAPHY", "BRANDED_GRAPHIC")

# Below this fraction of the available typography band, a no-photo card
# reads as an empty canvas rather than a deliberate composition (the
# exact failure mode of the 2026-09-25 production incident: a bare
# headline + one stat left roughly a quarter of the band filled). The
# template-aware brand gate (nullone_brand_gate.py) enforces this via
# CONTENT_COVERAGE_SUFFICIENT; the renderer only measures and reports it.
MIN_TYPOGRAPHY_COVERAGE = 0.45

# Below this combined-channel pixel stddev, a "photo" region reads as a
# near-uniform placeholder (blank/solid/near-black "image failed to
# load" panel) rather than real photographic/screenshot content -- the
# exact SOURCE_PHOTO false-pass shape: `has_photo=True` (a --source was
# given and loaded without error) while the actual pixels carry no real
# visual information. Calibrated well below genuine photo/screenshot
# variance (typically 30-70) but well above a flat-color fill (~0-3) or
# gentle gradient (~5-10). The template-aware brand gate
# (nullone_brand_gate.py) enforces this via PHOTO_REGION_MEANINGFUL; the
# renderer only measures and reports it.
MIN_PHOTO_REGION_STDDEV = 14.0


# Approved NullOne BRANDED_GRAPHIC motif/template library (visual-rules.md
# "BRANDED_GRAPHIC" + the real @nullone.az feed language: strong black
# base, Signal Orange focal accent, an established radar/"C"-arc motif,
# meaningful canvas occupation). Exactly one deterministic, code-drawn
# treatment -- never an arbitrary per-render shape choice, never
# AI-generated. Reviewed/extended here only by editing this function, the
# same governance as every other fixed layout constant in this file.
RADAR_ARC_SWEEP_DEGREES = 270  # "C" opening, not a full closed ring
RADAR_ARC_STROKE = 26
RADAR_ARC_MIN_DIAM = 140


def _draw_branded_graphic_motif(canvas, *, empty_top, empty_bottom):
    """Deterministic NullOne radar-arc motif filling typography negative space.

    Not AI-generated, not photographic: a fixed accent rule + a bold,
    partially-open ("C"/radar-sweep) arc built entirely from the
    documented brand palette (visual-rules.md Signal Orange accent),
    anchored bottom-right with a bright leading node -- the established
    NullOne radar/ring motif, not a generic thin decorative ellipse.
    Gives BRANDED_GRAPHIC a genuinely distinct, always-present visual
    treatment that meaningfully occupies the canvas instead of acting as
    filler.
    """

    empty_height = empty_bottom - empty_top
    if empty_height < 80:
        return empty_top  # not enough room for a motif; caller keeps plain background

    draw = ImageDraw.Draw(canvas)
    rule_y = empty_top
    draw.rectangle((MARGIN, rule_y, MARGIN + 160, rule_y + 6), fill=(*ACCENT, 255))

    arc_top = rule_y + 48
    arc_bottom = empty_bottom
    arc_diam = min(arc_bottom - arc_top, W - 2 * MARGIN, 760)
    if arc_diam <= RADAR_ARC_MIN_DIAM:
        return arc_top

    arc_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    arc_draw = ImageDraw.Draw(arc_layer)
    cx = W - MARGIN - int(arc_diam * 0.32)
    cy = arc_top + (arc_bottom - arc_top) // 2
    bbox = (cx - arc_diam // 2, cy - arc_diam // 2, cx + arc_diam // 2, cy + arc_diam // 2)

    # A faint full backing ring first (depth), then the bold, brighter
    # "C" sweep on top -- the established NullOne radar treatment rather
    # than one flat thin outline.
    arc_draw.ellipse(bbox, outline=(*ACCENT, 40), width=4)
    start_angle = -90 - (RADAR_ARC_SWEEP_DEGREES / 2)
    end_angle = -90 + (RADAR_ARC_SWEEP_DEGREES / 2)
    arc_draw.arc(bbox, start_angle, end_angle, fill=(*ACCENT, 190), width=RADAR_ARC_STROKE)

    # Bright leading node at the sweep's open end -- the "radar signal"
    # focal point, echoing the accent rule at the top of the band.
    node_angle = math.radians(end_angle)
    node_r = arc_diam / 2
    node_cx = cx + node_r * math.cos(node_angle)
    node_cy = cy + node_r * math.sin(node_angle)
    node_radius = RADAR_ARC_STROKE * 0.7
    arc_draw.ellipse(
        (node_cx - node_radius, node_cy - node_radius, node_cx + node_radius, node_cy + node_radius),
        fill=(*ACCENT, 255),
    )

    canvas.alpha_composite(arc_layer)

    return arc_bottom


def _draw_kicker_chip(draw, canvas, *, xy, text_str, font):
    """Top micro-brand/category chip: a subtle low-opacity Signal Orange
    outline pill behind the kicker label. BRANDED_GRAPHIC-only top-band
    treatment giving the category label more visual weight than plain
    metadata-colored text, without competing with the headline below."""

    box = draw.textbbox(xy, text_str, font=font)
    pad_x, pad_y = 18, 10
    chip_box = (box[0] - pad_x, box[1] - pad_y, box[2] + pad_x, box[3] + pad_y)
    chip_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    chip_draw = ImageDraw.Draw(chip_layer)
    chip_draw.rounded_rectangle(chip_box, radius=chip_box[3] - chip_box[1], outline=(*ACCENT, 130), width=2)
    canvas.alpha_composite(chip_layer)


def render(args):
    has_photo = bool(args.source)
    visual_style = getattr(args, "visual_style", None) or "EDITORIAL_TYPOGRAPHY"
    if visual_style not in VISUAL_STYLES:
        raise ValueError(f"Unsupported --visual-style: {visual_style!r}")

    canvas = Image.new("RGB", (W, H), BG)
    photo_region_stddev = None
    photo_source_sha256 = None

    if has_photo:
        source = load_image(args.source)

        # Renderer-reported provenance/content facts for the brand gate
        # (PHOTO_REGION_MEANINGFUL / provenance-binding): computed from
        # the exact foreground region actually composited below, never
        # re-derived downstream by scanning the final flattened canvas.
        if not (args.source.startswith("http://") or args.source.startswith("https://")):
            photo_source_sha256 = hashlib.sha256(Path(args.source).read_bytes()).hexdigest()

        # Decorative full-bleed background derived from source.
        bg = ImageOps.fit(
            source,
            (W, HERO_H),
            method=Image.Resampling.LANCZOS,
        )
        bg = bg.filter(ImageFilter.GaussianBlur(20))
        canvas.paste(bg, (0, 0))

        # Darken decorative hero background.
        overlay = Image.new("RGBA", (W, HERO_H), (0, 0, 0, 95))
        canvas = canvas.convert("RGBA")
        canvas.alpha_composite(overlay, (0, 0))

        # Preserve full official/source asset in foreground.
        foreground = ImageOps.contain(
            source,
            (900, 535),
            Image.Resampling.LANCZOS,
        )

        # A near-uniform (blank/solid/placeholder) source reads as a
        # near-zero stddev region regardless of dimensions -- the exact
        # SOURCE_PHOTO false-pass shape: has_photo=True while the actual
        # pixels carry no real visual information. Measured on the exact
        # foreground region composited into the final canvas below.
        region_stat = ImageStat.Stat(foreground)
        photo_region_stddev = round(sum(region_stat.stddev) / len(region_stat.stddev), 2)

        fx = (W - foreground.width) // 2
        fy = 125 + (535 - foreground.height) // 2

        # Soft panel behind source visual.
        panel = Image.new(
            "RGBA",
            (foreground.width + 28, foreground.height + 28),
            (255, 255, 255, 28),
        )
        canvas.alpha_composite(panel, (fx - 14, fy - 14))
        canvas.paste(foreground, (fx, fy))
        band_top = HERO_H
    else:
        # Typography-only: no source evidence to frame. Reclaim the hero
        # area rather than drawing an empty placeholder panel/frame.
        canvas = canvas.convert("RGBA")
        band_top = 0

    draw = ImageDraw.Draw(canvas)
    text_boxes = []
    stat_color = None

    def text(xy, s, font, fill):
        draw.text(xy, s, font=font, fill=fill)
        text_boxes.append(draw.textbbox(xy, s, font=font))

    # Editorial band (full canvas when typography-only, lower band otherwise).
    draw.rectangle((0, band_top, W, H), fill=(*BG, 255))

    kicker_y = (HERO_H + 62) if has_photo else 170
    kicker_font = fnt(24, True)
    kicker_text = args.kicker.upper()
    if visual_style == "BRANDED_GRAPHIC":
        # Top micro-brand/category treatment: the established chip
        # around the category kicker, in addition to the bottom-right
        # handle -- BRANDED_GRAPHIC-only per the approved motif library.
        _draw_kicker_chip(draw, canvas, xy=(MARGIN, kicker_y), text_str=kicker_text, font=kicker_font)
        draw = ImageDraw.Draw(canvas)  # chip alpha-composited a new layer
    text((MARGIN, kicker_y), kicker_text, kicker_font, METADATA)

    hf, lines, line_h = fit_headline(draw, args.headline)

    y = kicker_y + 62
    for line in lines:
        text((MARGIN, y), line, hf, TEXT_PRIMARY)
        y += line_h

    deck_present = bool(args.deck)
    if deck_present:
        y += 14
        deck_font, deck_lines, deck_line_h = fit_stat(
            draw, args.deck, W - 2 * MARGIN, start=32, minimum=22, max_lines=2
        )
        for line in deck_lines:
            text((MARGIN, y), line, deck_font, METADATA)
            y += deck_line_h

    if args.stat:
        y += 24
        sf, stat_lines, stat_line_h = fit_stat(draw, args.stat, W - 2 * MARGIN)
        stat_color = ACCENT
        for line in stat_lines:
            text((MARGIN, y), line, sf, ACCENT)
            y += stat_line_h

    content_bottom_y = y

    # Bottom metadata.
    bottom_y = H - 105

    content_coverage_ratio = None
    if not has_photo:
        available_span = bottom_y - kicker_y
        content_span = content_bottom_y - kicker_y
        if visual_style == "BRANDED_GRAPHIC":
            motif_bottom = _draw_branded_graphic_motif(
                canvas, empty_top=content_bottom_y + 40, empty_bottom=bottom_y - 40
            )
            content_span = max(content_span, motif_bottom - kicker_y)
            draw = ImageDraw.Draw(canvas)  # motif may have alpha-composited a new layer
        content_coverage_ratio = (
            round(min(1.0, content_span / available_span), 4) if available_span > 0 else 1.0
        )

    text(
        (MARGIN, bottom_y),
        f"Mənbə: {args.source_name}",
        fnt(23),
        METADATA,
    )

    # Brand mark: exactly ONE, bottom-right, low opacity (visual-rules.md:
    # "Small NULLONE wordmark or @nullone.az handle, bottom-right, low
    # opacity, inside the safe margin"). Drawn on a separate transparent
    # layer and alpha-composited so the reduced opacity survives the
    # final flatten to RGB (a fill alpha drawn directly on the canvas
    # would otherwise just be discarded by convert("RGB")).
    handle = "@nullone.az"
    handle_font = fnt(27, True)
    handle_box = draw.textbbox((0, 0), handle, font=handle_font)
    handle_xy = (
        W - MARGIN - (handle_box[2] - handle_box[0]),
        bottom_y - 2,
    )
    handle_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(handle_layer).text(
        handle_xy, handle, font=handle_font, fill=(*TEXT_PRIMARY, BRAND_MARK_OPACITY)
    )
    canvas.alpha_composite(handle_layer)
    text_boxes.append(draw.textbbox(handle_xy, handle, font=handle_font))
    brand_mark_count = 1
    brand_mark_position = "bottom_right"

    text_bounds_valid = True
    for tb in text_boxes:
        x0, y0, x1, y1 = tb
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
            text_bounds_valid = False
            break

    if not text_bounds_valid:
        raise RenderBoundsError(
            f"TEXT_OUTSIDE_CANVAS: bbox exceeds canvas {(W, H)}"
        )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    canvas.convert("RGB").save(out, "PNG", optimize=True)

    check = Image.open(out)
    assert check.size == (1080, 1350)

    brand_metadata = {
        "schema": BRAND_METADATA_SCHEMA,
        "visual_style": visual_style,
        "has_photo": has_photo,
        "stat_present": bool(args.stat),
        "stat_color": list(stat_color) if stat_color is not None else None,
        "margin_px": MARGIN,
        "brand_mark_count": brand_mark_count,
        "brand_mark_position": brand_mark_position,
        "brand_mark_opacity": BRAND_MARK_OPACITY,
        "text_bounds_valid": text_bounds_valid,
        "content_coverage_ratio": content_coverage_ratio,
        "deck_present": deck_present,
        "photo_region_stddev": photo_region_stddev,
        "photo_source_sha256": photo_source_sha256,
    }
    metadata_path = out.with_suffix(out.suffix + ".brand.json")
    metadata_path.write_text(
        json.dumps(brand_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"OUTPUT={out}")
    print("SIZE=1080x1350")
    print("VALID=true")
    print(f"BRAND_METADATA={metadata_path}")


def main():
    p = argparse.ArgumentParser()

    p.add_argument("--source", default=None)
    p.add_argument("--kicker", required=True)
    p.add_argument("--headline", required=True)
    p.add_argument("--stat", default="")
    p.add_argument("--deck", default="")
    p.add_argument("--source-name", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--visual-style", choices=VISUAL_STYLES, default="EDITORIAL_TYPOGRAPHY")

    render(p.parse_args())


if __name__ == "__main__":
    main()
