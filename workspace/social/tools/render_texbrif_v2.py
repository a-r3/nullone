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


def render(args):
    source = load_image(args.source)

    # Decorative full-bleed background derived from source.
    bg = ImageOps.fit(
        source,
        (W, HERO_H),
        method=Image.Resampling.LANCZOS,
    )
    bg = bg.filter(ImageFilter.GaussianBlur(20))

    canvas = Image.new("RGB", (W, H), (14, 14, 15))
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

    draw = ImageDraw.Draw(canvas)

    # Brand header.
    draw.text((70, 55), "NULLONE", font=fnt(32, True), fill=(242, 234, 225))

    section = "AI • TEXNOLOGİYA"
    box = draw.textbbox((0, 0), section, font=fnt(22, True))
    draw.text(
        (W - 70 - (box[2] - box[0]), 64),
        section,
        font=fnt(22, True),
        fill=(225, 225, 225),
    )

    # Editorial lower band.
    draw.rectangle((0, HERO_H, W, H), fill=(15, 15, 15, 255))

    kicker_y = HERO_H + 62
    draw.text(
        (70, kicker_y),
        args.kicker.upper(),
        font=fnt(24, True),
        fill=(185, 185, 185),
    )

    hf, lines, line_h = fit_headline(draw, args.headline)

    y = kicker_y + 62
    for line in lines:
        draw.text((70, y), line, font=hf, fill=(242, 234, 225))
        y += line_h

    if args.stat:
        y += 24
        draw.text(
            (70, y),
            args.stat,
            font=fnt(31, True),
            fill=(220, 220, 220),
        )

    # Bottom metadata.
    bottom_y = H - 105

    draw.text(
        (70, bottom_y),
        f"Mənbə: {args.source_name}",
        font=fnt(23),
        fill=(150, 150, 150),
    )

    handle = "@nullone.az"
    box = draw.textbbox((0, 0), handle, font=fnt(27, True))
    draw.text(
        (W - 70 - (box[2] - box[0]), bottom_y - 2),
        handle,
        font=fnt(27, True),
        fill=(242, 234, 225),
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

    p.add_argument("--source", required=True)
    p.add_argument("--kicker", required=True)
    p.add_argument("--headline", required=True)
    p.add_argument("--stat", default="")
    p.add_argument("--source-name", required=True)
    p.add_argument("--output", required=True)

    render(p.parse_args())


if __name__ == "__main__":
    main()
