#!/usr/bin/env python3
"""Offline render reproducibility proofs (issue #8).

FEED / CAROUSEL / STORY rendered from repository fixtures only:
no network, no Zernio, no Telegram, no model calls, no production state.

Determinism level claimed: STRUCTURAL_DETERMINISM (dimensions, counts,
format, glyph coverage, input hashes) plus same-host pipeline byte
stability. Cross-host BIT_IDENTICAL output is explicitly NOT claimed.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "workspace" / "social" / "tools"
FIXTURES = ROOT / "tests" / "fixtures"

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

AZ_CHARS = "ƏəĞğİıÖöÜüŞşÇç"
AZ_HEADLINE = "Şəhərdə süni intellekt: Əsas yeniliklər"
AZ_BODY = "Qiymətlər 199 ₼-dən başlayır, artım 42% oldu."
AZ_MIXED = "Rauf Alizada Bakıda görüşdü"


def make_source_png(path: Path, w: int = 1200, h: int = 900) -> str:
    from PIL import Image

    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
    img.save(path)
    return sha256_file(path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def run_renderer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True, cwd=str(ROOT))


def glyph_bitmap(font, ch: str) -> bytes:
    from PIL import Image

    mask = font.getmask(ch)
    canvas = Image.new("L", mask.size)
    canvas.putdata(list(mask))
    return canvas.tobytes()


class OfflineRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullone-render-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.source = self.work / "source.png"
        make_source_png(self.source)

    def test_az_glyphs_present_no_tofu(self):
        from PIL import ImageFont

        for path in (FONT_REGULAR, FONT_BOLD):
            with self.subTest(font=path):
                try:
                    font = ImageFont.truetype(path, 100)
                except OSError as e:
                    self.fail(f"required font missing: {path}: {e}")
                missing_reference = glyph_bitmap(font, "\U0010FFFF")
                tofu = [c for c in AZ_CHARS
                        if glyph_bitmap(font, c) == missing_reference]
                self.assertEqual(tofu, [], f"tofu glyphs in {path}")

    def test_az_strings_render_with_ink(self):
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(FONT_BOLD, 48)
        for text in (AZ_HEADLINE, AZ_BODY, AZ_MIXED):
            with self.subTest(text=text):
                box = font.getbbox(text)
                self.assertIsNotNone(box)
                self.assertGreater(box[2] - box[0], 0)
                canvas = Image.new("RGB", (1000, 120), (255, 255, 255))
                d = ImageDraw.Draw(canvas)
                d.text((10, 10), text, font=font, fill=(0, 0, 0))
                ink = sum(1 for px in canvas.getdata() if px != (255, 255, 255))
                self.assertGreater(ink, 100, f"no ink rendered for {text!r}")

    def test_feed_offline_render(self):
        spec = json.loads((FIXTURES / "offline-render-feed.json").read_text())
        out = self.work / "feed.png"
        cp = run_renderer(
            str(TOOLS / "render_texbrif_v2.py"),
            "--source", str(self.source),
            "--kicker", spec["kicker"],
            "--headline", spec["headline"],
            "--stat", spec["stat"],
            "--source-name", spec["source-name"],
            "--output", str(out))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertTrue(out.is_file())
        from PIL import Image

        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 1350))
            self.assertEqual(img.format, "PNG")
        first = sha256_file(out)
        # Same-host pipeline byte stability (NOT a cross-host claim).
        out2 = self.work / "feed2.png"
        cp = run_renderer(
            str(TOOLS / "render_texbrif_v2.py"),
            "--source", str(self.source),
            "--kicker", spec["kicker"],
            "--headline", spec["headline"],
            "--stat", spec["stat"],
            "--source-name", spec["source-name"],
            "--output", str(out2))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(sha256_file(out2), first)

    def test_carousel_offline_render(self):
        out_dir = self.work / "carousel"
        cp = run_renderer(
            str(TOOLS / "render_carousel_v2.py"),
            "--spec", str(FIXTURES / "offline-render-carousel.json"),
            "--output-dir", str(out_dir))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        slides = sorted(out_dir.glob("*.png"))
        spec = json.loads((FIXTURES / "offline-render-carousel.json").read_text())
        self.assertEqual(len(slides), len(spec["slides"]))
        self.assertGreaterEqual(len(slides), 2)
        from PIL import Image

        for slide in slides:
            with Image.open(slide) as img:
                self.assertEqual(img.size, (1080, 1350))
                self.assertEqual(img.format, "PNG")

    def test_story_offline_render(self):
        spec = json.loads((FIXTURES / "offline-render-story.json").read_text())
        out = self.work / "story.png"
        cp = run_renderer(
            str(TOOLS / "render_story_v2.py"),
            "--layout", spec["layout"],
            "--headline", spec["headline"],
            "--body", spec["body"],
            "--label", spec.get("label", "NULLONE"),
            "--source-image", str(self.source),
            "--cta", spec.get("cta", "@nullone.az"),
            "--output", str(out))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertTrue(out.is_file())
        from PIL import Image

        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 1920))
            self.assertEqual(img.format, "PNG")

    def test_missing_font_fails_clearly(self):
        from PIL import ImageFont

        with self.assertRaises(OSError):
            ImageFont.truetype("/nonexistent/fonts/Nope.ttf", 20)

    def test_no_byte_identical_claim(self):
        doc = (ROOT / "docs" / "operations" / "runtime-inventory.md").read_text()
        self.assertIn("STRUCTURAL_DETERMINISM", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
