#!/usr/bin/env python3
"""End-to-end V2 carousel source-image and packaging authority checks."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "workspace"
SCRIPTS = WORKSPACE / "social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))
from nullone_bridge_common import BridgeError  # noqa: E402
from nullone_packaging_receipt import ASSET_DESCRIPTOR_SCHEMA, evaluate_request  # noqa: E402

spec = importlib.util.spec_from_file_location("carousel_packaging_dispatcher", SCRIPTS / "nullone-packaging-render.py")
dispatcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatcher)

SLIDES = {"slides": [
    {"type": "cover", "headline": "Rəsmi mənbə görüntüsü", "source": "Rəsmi mənbə"},
    {"type": "explainer", "title": "Birinci addım", "body": "İzah."},
    {"type": "explainer", "title": "İkinci addım", "body": "İzah."},
    {"type": "explainer", "title": "Üçüncü addım", "body": "İzah."},
    {"type": "final", "title": "Niyə vacibdir?"},
]}


def request(kind):
    has_photo = kind == "REAL_PHOTO"
    has_shot = kind == "SOURCE_SCREENSHOT"
    return {
        "candidate": {"content_type": "EXPLAINER", "content_shape": "MULTI_STEP_EXPLAINER",
                      "timeliness": "THIS_WEEK", "verification_status": "PASS",
                      "source_grounding": "STRONG_PRIMARY", "audience_value": "HIGH",
                      "distinct_beat_count": 3, "depicts_real_world_subject": has_photo,
                      "still_developing": False,
                      "visual_requirement": "SOURCE_GROUNDED" if kind != "NONE" else "NONE"},
        "assets": {"has_official_or_source_image": has_photo,
                   "has_usable_screenshot": has_shot, "image_on_topic": has_photo or has_shot,
                   "image_quality_ok": has_photo,
                   "data_visualization_possible": kind == "DATA_VISUALIZATION"},
    }


class CarouselSourceImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="carousel-source-", dir=WORKSPACE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec_path = self.root / "spec.json"
        self.spec_path.write_text(json.dumps(SLIDES, ensure_ascii=False), encoding="utf-8")

    def render(self, kind, *, asset_path=None, declared_kind=None, source=None):
        receipt = evaluate_request("source-probe", request(kind))
        self.assertEqual(receipt["FORMAT_DECISION"], "CAROUSEL")
        self.assertEqual(receipt["VISUAL_STYLE"], kind if kind != "NONE" else "EDITORIAL_TYPOGRAPHY")
        receipt_path = self.root / "social/drafts/production/source-probe-packaging-decision.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        descriptor = {"schema": ASSET_DESCRIPTOR_SCHEMA, "candidate_id": "source-probe",
                      "asset_kind": declared_kind or kind, "local_path": str(asset_path) if asset_path else None,
                      "provenance": "Official source asset" if kind != "NONE" else None}
        asset_file = self.root / "asset.json"
        asset_file.write_text(json.dumps(descriptor), encoding="utf-8")
        output = self.root / "slides"
        args = argparse.Namespace(receipt=str(receipt_path), asset_file=str(asset_file),
                                  output=str(output), spec=str(self.spec_path), source=source)
        return dispatcher.render_command(args, root=self.root), output

    def image(self, name="source.png", color=(37, 143, 211)):
        path = self.root / name
        Image.new("RGB", (1200, 800), color).save(path)
        return path

    def assert_source_cover(self, output, color):
        slides = sorted(output.glob("*.png"))
        self.assertEqual(len(slides), 5)
        for slide in slides:
            with Image.open(slide) as image:
                self.assertEqual(image.size, (1080, 1350))
        with Image.open(slides[0]) as cover:
            self.assertEqual(cover.getpixel((540, 400)), color)
            self.assertNotEqual(cover.getpixel((540, 900)), color)  # headline scrim

    def test_real_photo_pixels_reach_cover(self):
        _, output = self.render("REAL_PHOTO", asset_path=self.image())
        self.assert_source_cover(output, (37, 143, 211))

    def test_screenshot_is_contained_without_fabrication(self):
        _, output = self.render("SOURCE_SCREENSHOT", asset_path=self.image(color=(31, 190, 93)))
        self.assert_source_cover(output, (31, 190, 93))

    def test_existing_data_visualization_file_is_displayed(self):
        _, output = self.render("DATA_VISUALIZATION", asset_path=self.image(color=(219, 120, 44)))
        self.assert_source_cover(output, (219, 120, 44))

    def test_typography_carousel_still_renders(self):
        _, output = self.render("NONE")
        self.assertEqual(len(list(output.glob("*.png"))), 5)

    def test_missing_outside_symlink_and_mismatch_fail_closed(self):
        outside = Path(tempfile.gettempdir()) / "carousel-outside-source.png"
        for path in (self.root / "missing.png", outside):
            with self.subTest(path=path), self.assertRaises(BridgeError):
                self.render("REAL_PHOTO", asset_path=path)
            self.assertFalse((self.root / "slides").exists())
        image = self.image()
        link = self.root / "link.png"
        link.symlink_to(image)
        with self.assertRaises(BridgeError):
            self.render("REAL_PHOTO", asset_path=link)
        with self.assertRaises(BridgeError) as mismatch:
            self.render("REAL_PHOTO", asset_path=image, declared_kind="SOURCE_SCREENSHOT")
        self.assertIn("PACKAGING_ASSET_MISMATCH", str(mismatch.exception))
        self.assertFalse((self.root / "slides").exists())

    def test_unreadable_image_and_freeform_source_fail_closed(self):
        fake = self.root / "image.png"
        fake.write_text("not an image", encoding="utf-8")
        with self.assertRaises(BridgeError):
            self.render("REAL_PHOTO", asset_path=fake)
        self.assertFalse((self.root / "slides").exists())
        with self.assertRaises(BridgeError):
            self.render("REAL_PHOTO", asset_path=self.image(), source="rogue.png")


if __name__ == "__main__":
    unittest.main()
