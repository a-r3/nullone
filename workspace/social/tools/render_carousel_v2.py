#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350

BG = (14, 14, 14)
PANEL = (24, 24, 24)
WHITE = (242, 234, 225)
GRAY = (164, 161, 157)
MUTED = (105, 105, 105)
CYAN = (253, 69, 3)  # NullOne Signal Orange

BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else REGULAR, size)


def wrap(draw, text, fnt, max_width):
    words = text.split()
    lines, current = [], ""

    for word in words:
        candidate = word if not current else f"{current} {word}"
        box = draw.textbbox((0, 0), candidate, font=fnt)

        if box[2] - box[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def fit(draw, text, width, max_lines, start, minimum, bold=True):
    for size in range(start, minimum - 1, -2):
        f = font(size, bold)
        lines = wrap(draw, text, f, width)
        if len(lines) <= max_lines:
            return f, lines, int(size * 1.14)

    f = font(minimum, bold)
    return f, wrap(draw, text, f, width), int(minimum * 1.14)


def base(number, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.text((72, 55), "NULLONE", font=font(30, True), fill=WHITE)

    section = "AI • TEXNOLOGİYA"
    bb = d.textbbox((0, 0), section, font=font(20, True))
    d.text(
        (W - 72 - (bb[2] - bb[0]), 63),
        section,
        font=font(20, True),
        fill=GRAY,
    )

    d.line((72, 113, W - 72, 113), fill=(52, 52, 52), width=2)

    pagination = f"{number}/{total}"
    bb = d.textbbox((0, 0), pagination, font=font(21, True))
    d.text(
        (W - 72 - (bb[2] - bb[0]), H - 86),
        pagination,
        font=font(21, True),
        fill=MUTED,
    )

    return img, d


def accent_label(d, text, y=185):
    d.rounded_rectangle((72, y, 72 + 250, y + 48), 20, fill=CYAN)
    d.text((91, y + 9), text.upper(), font=font(20, True), fill=BG)


def footer(d, source):
    if source:
        d.text(
            (72, H - 86),
            f"Mənbə: {source}",
            font=font(20),
            fill=MUTED,
        )


def draw_lines(d, lines, f, y, line_h, fill=WHITE, x=72):
    for line in lines:
        d.text((x, y), line, font=f, fill=fill)
        y += line_h
    return y


def cover(slide, n, total, out):
    img, d = base(n, total)

    # Cyan accent frame / visual anchor.
    d.rectangle((0, 0, 18, H), fill=CYAN)

    accent_label(d, slide.get("kicker", "İZAH"))

    hf, lines, lh = fit(
        d,
        slide["headline"],
        910,
        4,
        78,
        50,
    )

    y = 310
    y = draw_lines(d, lines, hf, y, lh)

    stat = slide.get("stat", "")
    if stat:
        y += 50
        sf, slines, slh = fit(d, stat, 900, 3, 43, 30)
        draw_lines(d, slines, sf, y, slh, CYAN)

    # Decorative composition — deterministic, no generated art.
    d.arc((610, 690, 1060, 1140), 190, 350, fill=CYAN, width=12)
    d.arc((700, 780, 990, 1070), 190, 350, fill=(55, 105, 110), width=5)
    d.ellipse((850, 920, 885, 955), fill=CYAN)

    d.text((72, H - 185), "sürüşdür →", font=font(29, True), fill=WHITE)

    footer(d, slide.get("source"))
    img.save(out, "PNG", optimize=True)


def stat_slide(slide, n, total, out):
    img, d = base(n, total)

    accent_label(d, slide.get("label", "FAKT"))

    stat = slide["stat"]

    sf, stat_lines, stat_h = fit(
        d,
        stat,
        920,
        3,
        150,
        82,
    )

    y = 335
    y = draw_lines(d, stat_lines, sf, y, stat_h, CYAN)

    title = slide.get("title", "")
    if title:
        y += 50
        tf, title_lines, tlh = fit(d, title, 900, 3, 49, 36)
        y = draw_lines(d, title_lines, tf, y, tlh)

    body = slide.get("body", "")
    if body:
        y += 42
        bf = font(31)
        body_lines = wrap(d, body, bf, 900)
        draw_lines(d, body_lines[:4], bf, y, 47, GRAY)

    footer(d, slide.get("source"))
    img.save(out, "PNG", optimize=True)


def explainer(slide, n, total, out):
    img, d = base(n, total)

    accent_label(d, slide.get("label", "İZAH"))

    tf, title_lines, tlh = fit(
        d,
        slide["title"],
        900,
        3,
        62,
        42,
    )

    y = 315
    y = draw_lines(d, title_lines, tf, y, tlh)

    # visual divider / information panel
    y += 60
    d.rounded_rectangle(
        (72, y, 1008, y + 360),
        30,
        fill=PANEL,
        outline=(42, 42, 42),
        width=2,
    )

    body = slide.get("body", "")
    bf = font(32)
    body_lines = wrap(d, body, bf, 820)

    by = y + 66
    draw_lines(d, body_lines[:5], bf, by, 49, (205, 205, 205), x=120)

    stat = slide.get("stat", "")
    if stat:
        d.text((120, y + 270), stat, font=font(34, True), fill=CYAN)

    footer(d, slide.get("source"))
    img.save(out, "PNG", optimize=True)


def comparison(slide, n, total, out):
    img, d = base(n, total)

    accent_label(d, slide.get("label", "MÜQAYİSƏ"))

    tf, title_lines, tlh = fit(d, slide["title"], 900, 3, 57, 40)

    y = 295
    y = draw_lines(d, title_lines, tf, y, tlh)

    left = slide.get("left", {})
    right = slide.get("right", {})

    top = y + 70
    card_h = 400

    d.rounded_rectangle(
        (72, top, 515, top + card_h),
        28,
        fill=PANEL,
        outline=(50, 50, 50),
        width=2,
    )
    d.rounded_rectangle(
        (565, top, 1008, top + card_h),
        28,
        fill=PANEL,
        outline=CYAN,
        width=3,
    )

    for x, item, accent in [
        (110, left, False),
        (603, right, True),
    ]:
        label = item.get("label", "")
        value = item.get("value", "")
        note = item.get("note", "")

        d.text((x, top + 55), label, font=font(24, True), fill=GRAY)

        vf, vlines, vlh = fit(d, value, 360, 2, 72, 52)
        draw_lines(
            d,
            vlines,
            vf,
            top + 125,
            vlh,
            CYAN if accent else WHITE,
            x=x,
        )

        if note:
            nf = font(25)
            nlines = wrap(d, note, nf, 350)
            draw_lines(d, nlines[:3], nf, top + 265, 38, GRAY, x=x)

    body = slide.get("body", "")
    if body:
        bf = font(29)
        blines = wrap(d, body, bf, 900)
        draw_lines(d, blines[:3], bf, top + card_h + 65, 44, GRAY)

    footer(d, slide.get("source"))
    img.save(out, "PNG", optimize=True)


def limitation(slide, n, total, out):
    img, d = base(n, total)

    accent_label(d, slide.get("label", "MƏHDUDİYYƏT"))

    # large bordered warning panel
    d.rounded_rectangle(
        (72, 305, 1008, 970),
        34,
        fill=PANEL,
        outline=CYAN,
        width=4,
    )

    tf, lines, lh = fit(d, slide["title"], 810, 4, 60, 42)

    y = 385
    y = draw_lines(d, lines, tf, y, lh, WHITE, x=125)

    body = slide.get("body", "")
    if body:
        y += 55
        bf = font(31)
        blines = wrap(d, body, bf, 800)
        draw_lines(d, blines[:5], bf, y, 47, GRAY, x=125)

    footer(d, slide.get("source"))
    img.save(out, "PNG", optimize=True)


def final_slide(slide, n, total, out):
    img, d = base(n, total)

    d.rectangle((0, 0, 18, H), fill=CYAN)
    accent_label(d, "NİYƏ VACİBDİR?")

    tf, lines, lh = fit(d, slide["title"], 900, 5, 67, 44)

    y = 325
    y = draw_lines(d, lines, tf, y, lh)

    body = slide.get("body", "")
    if body:
        y += 60
        bf = font(33)
        blines = wrap(d, body, bf, 890)
        draw_lines(d, blines[:5], bf, y, 50, GRAY)

    d.line((72, H - 235, 1008, H - 235), fill=(55, 55, 55), width=2)
    d.text((72, H - 185), "@nullone.az", font=font(35, True), fill=CYAN)

    footer(d, slide.get("source"))
    img.save(out, "PNG", optimize=True)


RENDERERS = {
    "cover": cover,
    "stat": stat_slide,
    "explainer": explainer,
    "comparison": comparison,
    "limitation": limitation,
    "final": final_slide,
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--spec", required=True)
    p.add_argument("--output-dir", required=True)
    args = p.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    slides = spec["slides"]

    if not 2 <= len(slides) <= 10:
        raise RuntimeError("Carousel must contain 2–10 slides")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = len(slides)

    for i, slide in enumerate(slides, start=1):
        kind = slide.get("type")

        if kind not in RENDERERS:
            raise RuntimeError(f"Unsupported slide type: {kind}")

        out = out_dir / f"{i:02d}.png"

        RENDERERS[kind](slide, i, total, out)

        check = Image.open(out)

        if check.size != (1080, 1350):
            raise RuntimeError(f"{out}: BAD SIZE {check.size}")

        print(f"{out}: 1080x1350 PASS [{kind}]")

    print(f"CAROUSEL_V2_VALID=true")
    print(f"SLIDES={total}")


if __name__ == "__main__":
    main()
