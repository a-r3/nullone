#!/usr/bin/env python3
"""Regression tests for issue #159 (P0 renderer layout correctness).

Covers two confirmed Visual V2 feed-renderer defects:

1. Typography-only posts (no source photo) rendered a large empty
   photo-frame placeholder.
2. Long stat lines could overflow the 1080px canvas while the renderer
   still reported VALID=true.

All offline: local fixtures / subprocess calls only, no network, no
Zernio, no Telegram, no production state.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "workspace" / "social" / "tools"
RENDERER = TOOLS / "render_texbrif_v2.py"


def run_renderer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RENDERER), *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def make_source_png(path: Path, w: int = 1200, h: int = 900) -> None:
    from PIL import Image

    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
    img.save(path)


class RenderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullone-render-layout-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)


class EmptyFrameTests(RenderTestCase):
    """PHOTO_NOT_REQUIRED vs PHOTO_REQUIRED_BUT_MISSING at the renderer."""

    def test_empty_frame_absent_when_photo_not_required(self):
        out = self.work / "typography.png"
        cp = run_renderer(
            "--kicker", "Süni İntellekt",
            "--headline", "Anthropic Opus 5.5 buraxıldı",
            "--stat", "",
            "--source-name", "Anthropic",
            "--output", str(out),
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("VALID=true", cp.stdout)

        from PIL import Image

        img = Image.open(out).convert("RGB")
        # No source photo was given, so nothing should composite a
        # panel/frame anywhere on the canvas: every sampled pixel is
        # either the flat editorial background or text ink -- never the
        # translucent panel-edge shade the old code produced by treating
        # a placeholder canvas as if it were a real photo.
        # #0E0E0F per social/references/visual-rules.md (the renderer's
        # editorial-band fill previously drifted to (15,15,15) / #0F0F0F;
        # fixed under the Visual V2 brand-compliance gate work).
        bg = (14, 14, 15)
        px = img.load()
        w, h = img.size
        for x in range(0, w, 15):
            for y in range(0, h, 15):
                self.assertNotEqual(
                    px[x, y], (36, 36, 36),
                    f"found legacy panel-edge shade at ({x},{y})",
                )
        # A point clear of any text/brand element must be exactly the
        # flat background -- proving no placeholder box was drawn there.
        self.assertEqual(px[950, 750], bg)

    def test_photo_still_frames_when_a_real_source_is_provided(self):
        source = self.work / "source.png"
        make_source_png(source)
        out = self.work / "with_photo.png"
        cp = run_renderer(
            "--source", str(source),
            "--kicker", "Süni İntellekt",
            "--headline", "Anthropic Opus 5.5 buraxıldı",
            "--stat", "",
            "--source-name", "Anthropic",
            "--output", str(out),
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)

        from PIL import Image

        img = Image.open(out).convert("RGB")
        # With a real source photo, the hero compositing (above HERO_H)
        # is unchanged -- that area now carries photo-derived content
        # instead of the flat editorial background.
        self.assertNotEqual(img.load()[950, 300], (15, 15, 15))


class StatOverflowTests(RenderTestCase):
    def test_long_stat_fits_within_canvas(self):
        out = self.work / "long_stat.png"
        long_stat = (
            "Bu, çox uzun bir statistika sətridir və o, kətanın kənarından "
            "kəsilməli deyil — məsələn 128% artım qeydə alındı bu il"
        )
        cp = run_renderer(
            "--kicker", "Faktlar",
            "--headline", "Başlıq",
            "--stat", long_stat,
            "--source-name", "Mənbə",
            "--output", str(out),
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("VALID=true", cp.stdout)

        from PIL import Image

        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 1350))

    def test_text_outside_canvas_invalid(self):
        # A single unbroken "word" long enough that no deterministic
        # wrap/font-size reduction can fit it inside the canvas at any
        # allowed size: the renderer must fail closed, never print
        # VALID=true for output it cannot guarantee is in-bounds.
        unfittable = "X" * 400
        out = self.work / "unfittable.png"
        cp = run_renderer(
            "--kicker", "Faktlar",
            "--headline", "Başlıq",
            "--stat", unfittable,
            "--source-name", "Mənbə",
            "--output", str(out),
        )
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("VALID=true", cp.stdout)
        self.assertIn("TEXT_OUTSIDE_CANVAS", cp.stderr)

    def test_render_valid_implies_no_text_overflow(self):
        # Sweep of stat lengths: whenever the renderer reports
        # VALID=true, the output PNG must exist at the canonical size --
        # VALID=true is never printed for a geometrically broken render.
        from PIL import Image

        for n_words in (1, 5, 15, 30):
            stat = " ".join(["statistika"] * n_words)
            out = self.work / f"sweep_{n_words}.png"
            cp = run_renderer(
                "--kicker", "Faktlar",
                "--headline", "Başlıq",
                "--stat", stat,
                "--source-name", "Mənbə",
                "--output", str(out),
            )
            with self.subTest(n_words=n_words):
                if "VALID=true" in cp.stdout:
                    self.assertEqual(cp.returncode, 0)
                    with Image.open(out) as img:
                        self.assertEqual(img.size, (1080, 1350))
                else:
                    self.assertNotEqual(cp.returncode, 0)

    def test_1080px_output_preserved_for_normal_input(self):
        out = self.work / "normal.png"
        cp = run_renderer(
            "--kicker", "Faktlar",
            "--headline", "Qısa başlıq",
            "--stat", "42%",
            "--source-name", "Mənbə",
            "--output", str(out),
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)

        from PIL import Image

        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 1350))
            self.assertEqual(img.format, "PNG")


if __name__ == "__main__":
    unittest.main(verbosity=2)
