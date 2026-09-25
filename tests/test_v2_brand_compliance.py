#!/usr/bin/env python3
"""Regression tests for Visual V2 brand-compliance gate (documented gaps
against social/references/visual-rules.md).

Covers:
1. Stat/key-number rendered in the #FD4503 accent, never plain gray.
2. No 70px legacy margin remains in the active typography path (90px
   enforced).
3. Exactly one brand mark (bottom-right @nullone.az), low opacity.
4. Redundant top-left "NULLONE" wordmark and unsupported top-right
   "AI • TEXNOLOGİYA" label are gone.
5. The deterministic BRAND_VISUAL_GATE blocks a non-compliant render
   before it can reach the render record (and therefore manifest/
   delivery) and passes a compliant one.
6. PR #160 regressions (long-stat wrap/fit, no-photo empty-frame fix)
   still hold under the new margin/color rules.

All offline: local subprocess calls to the real renderer + direct
function calls into the gate module, no network, no production state.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "workspace" / "social" / "tools"
SCRIPTS = ROOT / "workspace" / "social" / "ops" / "scripts"
RENDERER = TOOLS / "render_texbrif_v2.py"

sys.path.insert(0, str(SCRIPTS))
from nullone_bridge_common import BridgeError  # noqa: E402
from nullone_brand_gate import evaluate_brand_gate  # noqa: E402


def _load_hyphenated(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatcher = _load_hyphenated("packaging_render_dispatcher_brandtest", "nullone-packaging-render.py")

BG = (14, 14, 15)  # #0E0E0F
ACCENT = (253, 69, 3)  # #FD4503
LEGACY_STAT_GRAY = (220, 220, 220)
MARGIN = 90


def run_renderer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RENDERER), *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


class RenderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullone-brand-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def render(self, *, stat: str = "42%", headline: str = "Qısa başlıq") -> tuple[Path, dict]:
        out = self.work / "out.png"
        cp = run_renderer(
            "--kicker", "Faktlar",
            "--headline", headline,
            "--stat", stat,
            "--source-name", "Mənbə",
            "--output", str(out),
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("VALID=true", cp.stdout)
        metadata_path = out.with_suffix(out.suffix + ".brand.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        return out, metadata


class StatAccentTests(RenderTestCase):
    def test_stat_metadata_reports_accent_color(self):
        _, metadata = self.render(stat="128% artım")
        self.assertTrue(metadata["stat_present"])
        self.assertEqual(metadata["stat_color"], list(ACCENT))

    def test_stat_pixels_actually_contain_accent_and_never_legacy_gray(self):
        from PIL import Image

        out, _ = self.render(stat="72%")
        img = Image.open(out).convert("RGB")
        colors = {c for _, c in img.getcolors(maxcolors=1_000_000)}
        self.assertIn(ACCENT, colors, "no #FD4503 pixel found anywhere in a stat-bearing render")
        self.assertNotIn(
            LEGACY_STAT_GRAY, colors,
            "legacy plain-gray stat color (220,220,220) must not appear",
        )

    def test_long_stat_from_pr160_still_fits_and_uses_accent(self):
        # Regression: PR #160's fit_stat() wrap/shrink loop must still run
        # (and still use the accent color) after the margin change.
        long_stat = (
            "Bu, çox uzun bir statistika sətridir və o, kətanın kənarından "
            "kəsilməli deyil — məsələn 128% artım qeydə alındı bu il"
        )
        out, metadata = self.render(stat=long_stat)
        self.assertEqual(metadata["stat_color"], list(ACCENT))
        from PIL import Image

        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 1350))


class MarginTests(RenderTestCase):
    def test_margin_metadata_is_90(self):
        _, metadata = self.render()
        self.assertEqual(metadata["margin_px"], MARGIN)

    def test_no_ink_in_legacy_70px_to_90px_strip(self):
        """The old renderer started text at x=70; content must now start
        no earlier than x=90 for every drawn row, so the reclaimed
        70..90px strip is pure background top to bottom."""
        from PIL import Image

        out, _ = self.render()
        img = Image.open(out).convert("RGB")
        px = img.load()
        w, h = img.size
        for y in range(0, h, 3):
            for x in range(70, 90):
                self.assertEqual(
                    px[x, y], BG,
                    f"found non-background ink at ({x},{y}) inside the legacy 70px margin zone",
                )


class BrandMarkTests(RenderTestCase):
    def test_top_band_has_no_brand_mark_or_label(self):
        """visual-rules.md documents exactly one brand mark, bottom-right.
        Neither the redundant top-left NULLONE wordmark nor the
        unsupported top-right 'AI • TEXNOLOGİYA' label may draw ink
        anywhere in the top band."""
        from PIL import Image

        out, _ = self.render()
        img = Image.open(out).convert("RGB")
        px = img.load()
        w, _h = img.size
        for y in range(0, 140, 2):
            for x in range(0, w, 4):
                self.assertEqual(
                    px[x, y], BG,
                    f"found unexpected ink at ({x},{y}) in the top band "
                    "(redundant brand mark or unsupported label)",
                )

    def test_exactly_one_brand_mark_bottom_right_low_opacity(self):
        _, metadata = self.render()
        self.assertEqual(metadata["brand_mark_count"], 1)
        self.assertEqual(metadata["brand_mark_position"], "bottom_right")
        self.assertLess(metadata["brand_mark_opacity"], 255)
        self.assertGreater(metadata["brand_mark_opacity"], 0)

    def test_bottom_right_handle_is_actually_drawn(self):
        from PIL import Image

        out, _ = self.render()
        img = Image.open(out).convert("RGB")
        px = img.load()
        w, h = img.size
        found_ink = any(
            px[x, y] != BG
            for x in range(w - 220, w - 10, 3)
            for y in range(h - 120, h - 55, 3)
        )
        self.assertTrue(found_ink, "expected the @nullone.az handle to draw ink bottom-right")


class BrandGateIntegrationTests(RenderTestCase):
    def test_gate_passes_a_real_compliant_render(self):
        # A short headline+stat typography card no longer counts as
        # "compliant" on its own (see CONTENT_COVERAGE_SUFFICIENT below):
        # exercise the gate through the BRANDED_GRAPHIC style, whose
        # motif deterministically fills the band regardless of copy
        # length.
        out = self.work / "branded.png"
        cp = run_renderer(
            "--kicker", "Faktlar",
            "--headline", "Qısa başlıq",
            "--stat", "55%",
            "--source-name", "Mənbə",
            "--output", str(out),
            "--visual-style", "BRANDED_GRAPHIC",
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        # Must not raise.
        dispatcher._enforce_brand_gate(out, root=ROOT)

    def test_gate_blocks_when_sidecar_reports_noncompliant_stat_color(self):
        out, _ = self.render(stat="55%")
        metadata_path = out.with_suffix(out.suffix + ".brand.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["stat_color"] = list(LEGACY_STAT_GRAY)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with self.assertRaises(BridgeError) as ctx:
            dispatcher._enforce_brand_gate(out, root=ROOT)
        self.assertIn("BRAND_GATE_BLOCKED", str(ctx.exception))
        self.assertIn("STAT_ACCENT_COMPLIANT", str(ctx.exception))

    def test_gate_blocks_when_sidecar_missing(self):
        out = self.work / "no-sidecar.png"
        out.write_bytes(b"not-a-real-png")
        with self.assertRaises(BridgeError) as ctx:
            dispatcher._enforce_brand_gate(out, root=ROOT)
        self.assertIn("BRAND_GATE_BLOCKED", str(ctx.exception))

    def _make_image(self, name: str, *, noisy: bool, seed: int = 7) -> Path:
        from PIL import Image
        import random

        path = self.work / name
        img = Image.new("RGB", (400, 300))
        px = img.load()
        rng = random.Random(seed)
        for x in range(400):
            for y in range(300):
                px[x, y] = (
                    (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
                    if noisy
                    else (18, 18, 19)
                )
        img.save(path)
        return path

    def test_gate_blocks_real_photo_render_with_near_blank_source(self):
        """End-to-end reproduction of the 2026-09-25-shaped SOURCE_PHOTO
        false pass: a real, hash-valid, successfully-loaded source file
        that is nonetheless a near-blank placeholder must BLOCK through
        the actual renderer + dispatcher path, not just the pure gate
        function."""
        blank_source = self._make_image("blank.png", noisy=False)
        out = self.work / "photo.png"
        cp = run_renderer(
            "--source", str(blank_source),
            "--kicker", "Faktlar", "--headline", "Başlıq", "--stat", "55%",
            "--source-name", "Mənbə", "--output", str(out),
            "--visual-style", "REAL_PHOTO",
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        with self.assertRaises(BridgeError) as ctx:
            dispatcher._enforce_brand_gate(out, root=self.work, source=str(blank_source))
        self.assertIn("PHOTO_REGION_MEANINGFUL", str(ctx.exception))

    def test_gate_passes_real_photo_render_with_genuine_source_and_verifies_provenance(self):
        noisy_source = self._make_image("real.png", noisy=True)
        out = self.work / "photo.png"
        cp = run_renderer(
            "--source", str(noisy_source),
            "--kicker", "Faktlar", "--headline", "Başlıq", "--stat", "55%",
            "--source-name", "Mənbə", "--output", str(out),
            "--visual-style", "REAL_PHOTO",
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        dispatcher._enforce_brand_gate(out, root=self.work, source=str(noisy_source))

    def test_gate_blocks_on_photo_provenance_mismatch(self):
        """The renderer's reported source hash must match the exact file
        the dispatcher believes it passed -- otherwise a substituted
        asset could reach production undetected."""
        noisy_source = self._make_image("real.png", noisy=True, seed=7)
        other_source = self._make_image("other.png", noisy=True, seed=99)
        out = self.work / "photo.png"
        cp = run_renderer(
            "--source", str(noisy_source),
            "--kicker", "Faktlar", "--headline", "Başlıq", "--stat", "55%",
            "--source-name", "Mənbə", "--output", str(out),
            "--visual-style", "REAL_PHOTO",
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        with self.assertRaises(BridgeError) as ctx:
            dispatcher._enforce_brand_gate(out, root=self.work, source=str(other_source))
        self.assertIn("PHOTO_PROVENANCE_MISMATCH", str(ctx.exception))

    def test_deck_line_renders_and_reports_in_metadata(self):
        out = self.work / "deck.png"
        cp = run_renderer(
            "--kicker", "Faktlar", "--headline", "Başlıq", "--stat", "55%",
            "--deck", "Dəstəkləyici kontekst sətri",
            "--source-name", "Mənbə", "--output", str(out),
            "--visual-style", "BRANDED_GRAPHIC",
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        metadata_path = out.with_suffix(out.suffix + ".brand.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertTrue(metadata["deck_present"])
        dispatcher._enforce_brand_gate(out, root=ROOT)

    def test_pure_gate_function_matrix(self):
        base = {
            "schema": "nullone.render-brand-metadata.v1",
            "visual_style": "EDITORIAL_TYPOGRAPHY",
            "has_photo": False,
            "stat_present": True,
            "stat_color": list(ACCENT),
            "margin_px": 90,
            "brand_mark_count": 1,
            "brand_mark_position": "bottom_right",
            "brand_mark_opacity": 170,
            "text_bounds_valid": True,
            "content_coverage_ratio": 0.6,
            "deck_present": False,
            "photo_region_stddev": None,
            "photo_source_sha256": None,
        }
        self.assertEqual(evaluate_brand_gate(base)["BRAND_GATE"], "PASS")
        for field, bad_value, reason in (
            ("stat_color", list(LEGACY_STAT_GRAY), "STAT_ACCENT_COMPLIANT"),
            ("margin_px", 70, "MIN_MARGIN_90PX"),
            ("brand_mark_count", 2, "BRAND_MARK_COUNT_EXACTLY_ONE"),
            ("brand_mark_position", "top_left", "BRAND_MARK_BOTTOM_RIGHT"),
            ("brand_mark_opacity", 255, "BRAND_MARK_LOW_OPACITY"),
            ("text_bounds_valid", False, "TEXT_BOUNDS_VALID"),
        ):
            with self.subTest(field=field):
                bad = dict(base, **{field: bad_value})
                result = evaluate_brand_gate(bad)
                self.assertEqual(result["BRAND_GATE"], "BLOCKED")
                self.assertIn(reason, result["BRAND_GATE_REASON"])


class TemplateAwareBrandGateTests(unittest.TestCase):
    """Visual Director contract section 10: one check set per VISUAL_STYLE.

    Named to match docs/contracts/visual-director-contract-v1.md's test
    list (BRAND_GATE_SOURCE_PHOTO / BRAND_GATE_BRANDED_GRAPHIC /
    BRAND_GATE_DATA_VISUALIZATION / BRAND_GATE_EDITORIAL_TYPOGRAPHY).
    """

    def _base(self, **overrides):
        base = {
            "schema": "nullone.render-brand-metadata.v1",
            "visual_style": "EDITORIAL_TYPOGRAPHY",
            "has_photo": False,
            "stat_present": False,
            "stat_color": None,
            "margin_px": 90,
            "brand_mark_count": 1,
            "brand_mark_position": "bottom_right",
            "brand_mark_opacity": 170,
            "text_bounds_valid": True,
            "content_coverage_ratio": 0.7,
            "deck_present": False,
            "photo_region_stddev": None,
            "photo_source_sha256": None,
        }
        base.update(overrides)
        return base

    def test_brand_gate_source_photo_requires_visual_region(self):
        metadata = self._base(
            visual_style="REAL_PHOTO", has_photo=True, content_coverage_ratio=None,
            photo_region_stddev=40.0, photo_source_sha256="a" * 64,
        )
        self.assertEqual(evaluate_brand_gate(metadata)["BRAND_GATE"], "PASS")
        no_region = dict(metadata, has_photo=False)
        result = evaluate_brand_gate(no_region)
        self.assertEqual(result["BRAND_GATE"], "BLOCKED")
        self.assertIn("VISUAL_REGION_PRESENT", result["BRAND_GATE_REASON"])

    def test_brand_gate_source_photo_requires_meaningful_content(self):
        """The 2026-09-25-shaped false pass: has_photo=True (a --source
        loaded without error) but the composited region is a near-blank
        placeholder -- must BLOCK even though VISUAL_REGION_PRESENT
        alone would still pass."""
        metadata = self._base(
            visual_style="REAL_PHOTO", has_photo=True, content_coverage_ratio=None,
            photo_region_stddev=0.3, photo_source_sha256="a" * 64,
        )
        result = evaluate_brand_gate(metadata)
        self.assertEqual(result["BRAND_GATE"], "BLOCKED")
        self.assertIn("PHOTO_REGION_MEANINGFUL", result["BRAND_GATE_REASON"])
        self.assertNotIn("VISUAL_REGION_PRESENT", result["BRAND_GATE_REASON"])

    def test_brand_gate_data_visualization_requires_visual_region(self):
        metadata = self._base(
            visual_style="DATA_VISUALIZATION", has_photo=True, content_coverage_ratio=None,
            photo_region_stddev=25.0, photo_source_sha256="b" * 64,
        )
        self.assertEqual(evaluate_brand_gate(metadata)["BRAND_GATE"], "PASS")
        no_region = dict(metadata, has_photo=False)
        self.assertEqual(evaluate_brand_gate(no_region)["BRAND_GATE"], "BLOCKED")

    def test_brand_gate_branded_graphic_requires_coverage(self):
        filled = self._base(visual_style="BRANDED_GRAPHIC", content_coverage_ratio=0.96, stat_present=True, stat_color=list(ACCENT))
        self.assertEqual(evaluate_brand_gate(filled)["BRAND_GATE"], "PASS")
        empty = dict(filled, content_coverage_ratio=0.1)
        result = evaluate_brand_gate(empty)
        self.assertEqual(result["BRAND_GATE"], "BLOCKED")
        self.assertIn("CONTENT_COVERAGE_SUFFICIENT", result["BRAND_GATE_REASON"])

    def test_brand_gate_branded_graphic_requires_content_density(self):
        """Blocker 3: a branded graphic cannot merely be headline +
        decorative motif + empty space. Requires a stat or a deck line
        beyond the motif's coverage fill."""
        thin = self._base(
            visual_style="BRANDED_GRAPHIC", content_coverage_ratio=0.96,
            stat_present=False, stat_color=None, deck_present=False,
        )
        result = evaluate_brand_gate(thin)
        self.assertEqual(result["BRAND_GATE"], "BLOCKED")
        self.assertIn("CONTENT_DENSITY_SUFFICIENT", result["BRAND_GATE_REASON"])

        with_deck = dict(thin, deck_present=True)
        self.assertEqual(evaluate_brand_gate(with_deck)["BRAND_GATE"], "PASS")

        with_stat = dict(thin, stat_present=True, stat_color=list(ACCENT))
        self.assertEqual(evaluate_brand_gate(with_stat)["BRAND_GATE"], "PASS")

    def test_brand_gate_editorial_typography_requires_coverage(self):
        # The exact 2026-09-25 production incident: short headline + one
        # stat over an otherwise-empty canvas measured coverage ~0.34.
        production_incident = self._base(
            visual_style="EDITORIAL_TYPOGRAPHY",
            stat_present=True,
            stat_color=ACCENT,
            content_coverage_ratio=0.34,
        )
        result = evaluate_brand_gate(production_incident)
        self.assertEqual(result["BRAND_GATE"], "BLOCKED")
        self.assertIn("CONTENT_COVERAGE_SUFFICIENT", result["BRAND_GATE_REASON"])

    def test_structural_failure_blocks_even_without_a_stat(self):
        """Regression: the old gate never blocked a stat-less render no
        matter how many structural checks failed. Structural identity
        checks are style-independent and must always gate."""
        no_stat_bad_mark = self._base(brand_mark_count=2)
        result = evaluate_brand_gate(no_stat_bad_mark)
        self.assertEqual(result["BRAND_GATE"], "BLOCKED")
        self.assertIn("BRAND_MARK_COUNT_EXACTLY_ONE", result["BRAND_GATE_REASON"])


class NoPhotoRegressionTests(RenderTestCase):
    def test_no_photo_empty_frame_fix_holds_under_new_margins(self):
        """PR #160's fix (no placeholder photo frame when --source is
        absent) must still hold now that the editorial band is filled
        with the corrected #0E0E0F background instead of the drifted
        (15,15,15)."""
        from PIL import Image

        out, _ = self.render(stat="")
        img = Image.open(out).convert("RGB")
        # A point clear of any text/brand element must be exactly the
        # canonical brand background.
        self.assertEqual(img.load()[950, 750], BG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
