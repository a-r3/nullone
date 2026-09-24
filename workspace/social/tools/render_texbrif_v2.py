#!/usr/bin/env python3

import argparse
import io
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps


W, H = 1080, 1350
HERO_H = 735

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


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
    for size in range(72, 47, -2):
        font = fnt(size, True)
        lines = wrap(draw, text, font, 900)

        if len(lines) <= 3:
            return font, lines, int(size * 1.12)

    font = fnt(48, True)
    return font, wrap(draw, text, font, 900), 55


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


def render(args):
    has_photo = bool(args.source)

    canvas = Image.new("RGB", (W, H), (14, 14, 15))

    if has_photo:
        source = load_image(args.source)

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

    def text(xy, s, font, fill):
        draw.text(xy, s, font=font, fill=fill)
        text_boxes.append(draw.textbbox(xy, s, font=font))

    # Brand header.
    text((70, 55), "NULLONE", fnt(32, True), (242, 234, 225))

    section = "AI • TEXNOLOGİYA"
    box = draw.textbbox((0, 0), section, font=fnt(22, True))
    text(
        (W - 70 - (box[2] - box[0]), 64),
        section,
        fnt(22, True),
        (225, 225, 225),
    )

    # Editorial band (full canvas when typography-only, lower band otherwise).
    draw.rectangle((0, band_top, W, H), fill=(15, 15, 15, 255))

    kicker_y = (HERO_H + 62) if has_photo else 170
    text((70, kicker_y), args.kicker.upper(), fnt(24, True), (185, 185, 185))

    hf, lines, line_h = fit_headline(draw, args.headline)

    y = kicker_y + 62
    for line in lines:
        text((70, y), line, hf, (242, 234, 225))
        y += line_h

    if args.stat:
        y += 24
        sf, stat_lines, stat_line_h = fit_stat(draw, args.stat, W - 140)
        for line in stat_lines:
            text((70, y), line, sf, (220, 220, 220))
            y += stat_line_h

    # Bottom metadata.
    bottom_y = H - 105

    text(
        (70, bottom_y),
        f"Mənbə: {args.source_name}",
        fnt(23),
        (150, 150, 150),
    )

    handle = "@nullone.az"
    box = draw.textbbox((0, 0), handle, font=fnt(27, True))
    text(
        (W - 70 - (box[2] - box[0]), bottom_y - 2),
        handle,
        fnt(27, True),
        (242, 234, 225),
    )

    for tb in text_boxes:
        x0, y0, x1, y1 = tb
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
            raise RenderBoundsError(
                f"TEXT_OUTSIDE_CANVAS: bbox {tb} exceeds canvas {(W, H)}"
            )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    canvas.convert("RGB").save(out, "PNG", optimize=True)

    check = Image.open(out)
    assert check.size == (1080, 1350)

    print(f"OUTPUT={out}")
    print("SIZE=1080x1350")
    print("VALID=true")


def main():
    p = argparse.ArgumentParser()

    p.add_argument("--source", default=None)
    p.add_argument("--kicker", required=True)
    p.add_argument("--headline", required=True)
    p.add_argument("--stat", default="")
    p.add_argument("--source-name", required=True)
    p.add_argument("--output", required=True)

    render(p.parse_args())


if __name__ == "__main__":
    main()
