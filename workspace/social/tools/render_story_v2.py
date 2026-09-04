#!/usr/bin/env python3

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pathlib import Path
import argparse

W, H = 1080, 1920

BG = (7, 9, 11)
WHITE = (242, 234, 225)
MUTED = (157, 166, 174)
CYAN = (253, 69, 3)  # NullOne Signal Orange
CARD = (18, 22, 26)
LINE = (49, 57, 63)

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def f(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)

def wrap(draw, text, font, max_width):
    words = text.split()
    lines, line = [], ""
    for word in words:
        test = word if not line else line + " " + word
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines

def draw_brand(d, label):
    d.rounded_rectangle((72, 270, 270, 326), 28, fill=CYAN)
    d.text((94, 284), label.upper(), font=f(24, True), fill=BG)
    d.text((842, 284), "NULLONE", font=f(26, True), fill=WHITE)

def draw_footer(d, source, cta):
    y = 1610
    d.line((72, y, 1008, y), fill=LINE, width=2)
    if source:
        d.text((72, y + 42), f"Mənbə: {source}", font=f(27), fill=MUTED)
    box = d.textbbox((0, 0), cta, font=f(34, True))
    d.text((1008 - (box[2]-box[0]), y + 38), cta, font=f(34, True), fill=CYAN)

def source_background(im, path):
    if not path:
        return

    src = Image.open(path).convert("RGB")
    target_h = 760
    scale = max(W/src.width, target_h/src.height)
    nw, nh = int(src.width*scale), int(src.height*scale)
    src = src.resize((nw, nh), Image.LANCZOS)

    left = max(0, (nw-W)//2)
    top = max(0, (nh-target_h)//2)
    src = src.crop((left, top, left+W, top+target_h))
    src = src.filter(ImageFilter.GaussianBlur(0.25))

    dark = Image.new("RGBA", src.size, (0,0,0,80))
    src = src.convert("RGBA")
    src.alpha_composite(dark)
    im.paste(src.convert("RGB"), (0, 210))

    fade = Image.new("RGBA", (W, 500), (0,0,0,0))
    fd = ImageDraw.Draw(fade)
    for y in range(500):
        a = int(255 * (y / 499))
        fd.line((0,y,W,y), fill=(7,9,11,a))
    im.paste(fade, (0, 500), fade)

def headline_block(d, headline, y, size=72, max_lines=4):
    font = f(size, True)
    lines = wrap(d, headline, font, 920)

    while len(lines) > max_lines and size > 52:
        size -= 4
        font = f(size, True)
        lines = wrap(d, headline, font, 920)

    for line in lines[:max_lines]:
        d.text((72, y), line, font=font, fill=WHITE)
        y += size + 15
    return y

def render(args):
    im = Image.new("RGB", (W,H), BG)
    source_background(im, args.source_image)
    d = ImageDraw.Draw(im)

    draw_brand(d, args.label)

    if args.layout == "breaking":
        y = 870 if args.source_image else 540

        d.text((72, y), "BREAKING", font=f(31, True), fill=CYAN)
        y += 72
        y = headline_block(d, args.headline, y, 76)

        if args.body:
            y += 38
            body_font = f(38)
            for line in wrap(d, args.body, body_font, 900)[:4]:
                d.text((72,y), line, font=body_font, fill=MUTED)
                y += 54

    elif args.layout == "big-stat":
        y = 700 if args.source_image else 500

        if args.stat:
            sf = f(176, True)
            d.text((72,y), args.stat, font=sf, fill=CYAN)
            y += 220

        y = headline_block(d, args.headline, y, 67)

        if args.body:
            y += 40
            body_font = f(37)
            for line in wrap(d, args.body, body_font, 900)[:4]:
                d.text((72,y), line, font=body_font, fill=MUTED)
                y += 52

    elif args.layout == "explainer":
        y = 620

        d.rounded_rectangle((72,y,1008,y+205), 34, fill=CARD)
        if args.stat:
            d.text((106,y+38), args.stat, font=f(88,True), fill=CYAN)

        y += 265
        y = headline_block(d, args.headline, y, 65)

        if args.body:
            y += 34
            body_font = f(38)
            for line in wrap(d,args.body,body_font,900)[:5]:
                d.text((72,y),line,font=body_font,fill=MUTED)
                y += 54

    elif args.layout == "comparison":
        y = 570

        left = args.left_stat or "—"
        right = args.right_stat or "—"

        d.rounded_rectangle((72,y,515,y+250),32,fill=CARD)
        d.rounded_rectangle((565,y,1008,y+250),32,fill=CARD)

        d.text((110,y+42),left,font=f(82,True),fill=WHITE)
        d.text((603,y+42),right,font=f(82,True),fill=CYAN)

        if args.left_label:
            d.text((110,y+158),args.left_label,font=f(27),fill=MUTED)
        if args.right_label:
            d.text((603,y+158),args.right_label,font=f(27),fill=MUTED)

        y += 330
        y = headline_block(d,args.headline,y,62)

        if args.body:
            y += 30
            body_font=f(36)
            for line in wrap(d,args.body,body_font,900)[:4]:
                d.text((72,y),line,font=body_font,fill=MUTED)
                y += 52

    draw_footer(d,args.source_name,args.cta)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out,"PNG",optimize=True)
    print(f"Saved: {out}")
    print(f"Layout: {args.layout}")
    print(f"Size: {W}x{H}")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--layout",choices=["breaking","big-stat","explainer","comparison"],required=True)
    p.add_argument("--headline",required=True)
    p.add_argument("--stat")
    p.add_argument("--body")
    p.add_argument("--label",default="NULLONE")
    p.add_argument("--source-name")
    p.add_argument("--source-image")
    p.add_argument("--cta",default="@nullone.az")
    p.add_argument("--left-stat")
    p.add_argument("--right-stat")
    p.add_argument("--left-label")
    p.add_argument("--right-label")
    p.add_argument("--output",required=True)
    render(p.parse_args())

if __name__=="__main__":
    main()
