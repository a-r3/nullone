#!/usr/bin/env python3
"""Semantic validation for carousel card-slot blocks (issue #135).

Forensic source: the 2026-09-15 production run rendered slide 4
("V4-Pro təqaüdə çıxır", type=comparison, left={} / right={}) as two
empty cards while post-render validation still reported PASS, because
validation checked only slide-count, known slide type, and output
dimensions.

These tests execute the real renderer (workspace/social/tools/
render_carousel_v2.py) and require fail-closed behavior: an invalid
spec exits nonzero, renders no production-ready slide set, and never
reports PASS. Valid comparison and typography-only layouts must keep
working.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "workspace/social/tools/render_carousel_v2.py"

REASON = "EMPTY_COMPARISON_CONTENT"

FORENSIC_SLIDE = {
    "type": "comparison",
    "label": "KEÇİD",
    "title": "V4-Pro təqaüdə çıxır",
    "body": "14 sentyabr 04:00 UTC-dən bütün V4-Pro sorğuları V4.1-Flash-a yönlənir.",
    "source": "DeepSeek, 10–14 sentyabr 2026",
    "left": {},
    "right": {},
}

VALID_SIDE = {"label": "ƏVVƏL", "value": "V4-Pro", "note": "köhnə flaqman"}
VALID_SIDE_2 = {"label": "İNDİ", "value": "V4.1-Flash", "note": "1M kontekst"}

COVER = {
    "type": "cover",
    "kicker": "AÇIQ MODEL",
    "headline": "DeepSeek-V4.1-Flash açıq çəkidə",
    "stat": "552B MoE",
    "source": "DeepSeek",
}
FINAL = {"type": "final", "title": "Niyə vacibdir?", "body": "Açıq çəki büdcə maneəsini azaldır."}
STAT = {
    "type": "stat",
    "label": "SƏMƏRƏLİLİK",
    "stat": "1/4 HBM",
    "title": "Yaddaş xərci azaldı",
    "body": "KV-cache 4 dəfə az yaddaş tutur.",
    "source": "DeepSeek",
}
EXPLAINER = {
    "type": "explainer",
    "label": "ARXİTEKTURA",
    "title": "Az parametr, çox iş",
    "body": "Giriş üçün cəmi 8B aktiv parametr.",
    "stat": "8B / 16B aktiv",
    "source": "DeepSeek",
}
LIMITATION = {
    "type": "limitation",
    "label": "MƏHDUDİYYƏT",
    "title": "Diqqətli olun",
    "body": "Qiymətlər pik/off-pik rejimindədir.",
    "source": "DeepSeek",
}


def valid_comparison(**overrides):
    slide = {
        "type": "comparison",
        "label": "KEÇİD",
        "title": "V4-Pro təqaüdə çıxır",
        "body": "Bütün sorğular yeni modelə yönlənir.",
        "source": "DeepSeek",
        "left": dict(VALID_SIDE),
        "right": dict(VALID_SIDE_2),
    }
    slide.update(overrides)
    return slide


def run_render(spec: dict) -> tuple[subprocess.CompletedProcess, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="carousel-sem-"))
    spec_path = tmp / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False))
    out_dir = tmp / "out"
    return subprocess.run(
        [sys.executable, str(RENDERER), "--spec", str(spec_path),
         "--output-dir", str(out_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    ), out_dir


class CarouselSemanticValidationTest(unittest.TestCase):
    def assert_rejected(self, spec: dict) -> None:
        cp, out_dir = run_render(spec)
        self.assertNotEqual(cp.returncode, 0, "empty semantic block must fail")
        self.assertIn(REASON, cp.stderr,
                      f"failure must carry reason {REASON}: {cp.stderr[-500:]}")
        self.assertNotIn("CAROUSEL_V2_VALID=true", cp.stdout,
                         "failed spec must never report PASS")
        self.assertEqual(list(out_dir.glob("*.png")), [],
                         "failed spec must render no slide set")

    def test_forensic_slide4_repro_rejected(self):
        self.assert_rejected({"slides": [COVER, dict(FORENSIC_SLIDE), FINAL]})

    def test_missing_left_rejected(self):
        slide = valid_comparison()
        del slide["left"]
        self.assert_rejected({"slides": [COVER, slide, FINAL]})

    def test_missing_right_rejected(self):
        slide = valid_comparison()
        del slide["right"]
        self.assert_rejected({"slides": [COVER, slide, FINAL]})

    def test_empty_left_valid_right_rejected(self):
        self.assert_rejected(
            {"slides": [COVER, valid_comparison(left={}), FINAL]})

    def test_valid_left_empty_right_rejected(self):
        self.assert_rejected(
            {"slides": [COVER, valid_comparison(right={}), FINAL]})

    def test_whitespace_only_values_rejected(self):
        slide = valid_comparison(
            left={"label": "   ", "value": "  \t "},
            right={"label": "İNDİ", "value": "\n "},
        )
        self.assert_rejected({"slides": [COVER, slide, FINAL]})

    def test_valid_comparison_renders(self):
        cp, out_dir = run_render(
            {"slides": [COVER, valid_comparison(), FINAL]})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("CAROUSEL_V2_VALID=true", cp.stdout)
        self.assertEqual(len(list(out_dir.glob("*.png"))), 3)

    def test_typography_only_layouts_regress(self):
        cp, out_dir = run_render(
            {"slides": [COVER, STAT, EXPLAINER, LIMITATION, FINAL]})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("CAROUSEL_V2_VALID=true", cp.stdout)
        self.assertEqual(len(list(out_dir.glob("*.png"))), 5)


if __name__ == "__main__":
    unittest.main()
