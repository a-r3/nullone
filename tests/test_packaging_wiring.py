#!/usr/bin/env python3
"""Offline authority tests for deterministic packaging wiring.

Proves the packaging decision receipt is authoritative at runtime
(no LLM override possible through the reviewed commands):

A. SINGLE_FACT input -> SINGLE_POST; carousel manifest blocked
B. 4-beat explainer -> CAROUSEL; feed-path mismatch blocked;
   slide count exactly the evaluator recommendation
C. breaking/developing real event -> STORY; normal factory Story
   creation unreachable; delegation result only
D. required real photo missing + screenshot -> SOURCE_SCREENSHOT path
E. required real photo missing + data viz only -> DATA_VISUALIZATION
F. required real photo missing + no fallback -> SKIP with zero
   render/manifest/draft/Telegram calls
G. verification BLOCKED -> SKIP with zero consequential work
H. malformed packaging request fails closed
I. model-authored decision fields rejected
J. receipt tamper/fingerprint mismatch blocked before draft creation
K. candidate ID / path traversal blocked
L. receipt output symlink blocked
M. wrong-renderer bypass blocked via deterministic mismatch
N. no final publication capability introduced
O. OpenCode agent boundary equal-or-narrower (evaluator+dispatcher
   only; direct renderers removed)

All offline: fake subprocesses, temp files, no network/model calls.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
WORKSPACE = ROOT / "workspace"
DRAFTS_PROD = WORKSPACE / "social/drafts/production"
WS = "/home/oem/.openclaw/workspace"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_packaging_receipt as receipt_mod  # noqa: E402
import nullone_packaging_policy as policy  # noqa: E402


def _load_hyphenated(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evaluator_cli = _load_hyphenated("packaging_evaluator_cli", "nullone-packaging-evaluator.py")
render_dispatcher = _load_hyphenated("packaging_render_dispatcher", "nullone-packaging-render.py")
dispatcher = render_dispatcher


def _request(**overrides):
    candidate = {
        "content_type": "NEWS",
        "content_shape": "SINGLE_FACT",
        "timeliness": "TODAY",
        "verification_status": "PASS",
        "source_grounding": "STRONG_PRIMARY",
        "audience_value": "HIGH",
        "distinct_beat_count": 1,
        "depicts_real_world_subject": False,
        "still_developing": False,
        "visual_requirement": "NONE",
    }
    assets = {
        "has_official_or_source_image": False,
        "has_usable_screenshot": False,
        "image_on_topic": False,
        "image_quality_ok": False,
        "data_visualization_possible": False,
    }
    for key, value in overrides.items():
        section, field = key.split(".", 1)
        if section == "candidate":
            candidate[field] = value
        else:
            assets[field] = value
    return {"candidate": candidate, "assets": assets}


def _receipt_for(request, candidate_id="probe-candidate"):
    return receipt_mod.evaluate_request(candidate_id, request)


def _asset_descriptor(candidate_id="probe-candidate", asset_kind="NONE", local_path=None,
                      provenance="probe provenance", sha256=None):
    descriptor = {
        "schema": receipt_mod.ASSET_DESCRIPTOR_SCHEMA,
        "candidate_id": candidate_id,
        "asset_kind": asset_kind,
        "local_path": local_path,
        "source_url": None,
        "provenance": provenance,
        "sha256": sha256,
    }
    return descriptor


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class ReceiptAuthorityTests(unittest.TestCase):
    def test_receipt_hash_detects_tamper(self):
        receipt = _receipt_for(_request())
        tampered = dict(receipt)
        tampered["FORMAT_DECISION"] = "CAROUSEL"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / "good.json"
            good.write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual(receipt_mod.load_receipt(good, root=root)["FORMAT_DECISION"], "SINGLE_POST")
            bad = root / "bad.json"
            bad.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(BridgeError) as ctx:
                receipt_mod.load_receipt(bad, root=root)
            self.assertIn("TAMPERED", str(ctx.exception))

    def test_receipt_rejects_schema_mismatch(self):
        receipt = _receipt_for(_request())
        receipt["contract_version"] = "9.9.9"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "r.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaises(BridgeError):
                receipt_mod.load_receipt(path, root=root)

    def test_path_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(BridgeError):
                receipt_mod.contained_path(Path("/etc/passwd"), root)
            with self.assertRaises(BridgeError):
                receipt_mod.contained_path(root / ".." / "escape.json", root)
            with self.assertRaises(BridgeError):
                receipt_mod.check_candidate_id("../../evil")

    def test_symlink_receipt_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real.json"
            real.write_text(json.dumps(_receipt_for(_request())), encoding="utf-8")
            link = root / "link.json"
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaises(BridgeError):
                receipt_mod.load_receipt(link, root=root)


class EvaluatorInputTests(unittest.TestCase):
    def test_model_authored_decision_fields_rejected(self):
        request = _request()
        request["candidate"]["FORMAT_DECISION"] = "CAROUSEL"
        request["candidate"]["VISUAL_STYLE"] = "REAL_PHOTO"
        request["candidate"]["slide_count_recommendation"] = 8
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "req.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            with self.assertRaises(BridgeError) as ctx:
                evaluator_cli.load_validated_request(path, root=root)
            self.assertIn("must not submit", str(ctx.exception))

    def test_verification_mapping_exact(self):
        self.assertEqual(
            evaluator_cli._map_verification({"verification": "PASS"})["verification_status"], "PASS"
        )
        for literal in ("UNVERIFIED", "PARTIAL", "BLOCKED"):
            self.assertEqual(
                evaluator_cli._map_verification({"verification": literal})["verification_status"],
                "BLOCKED",
            )
        with self.assertRaises(BridgeError):
            evaluator_cli._map_verification({"verification": "MAYBE"})
        with self.assertRaises(BridgeError):
            evaluator_cli._map_verification(
                {"verification": "PASS", "verification_status": "BLOCKED"}
            )

    def test_malformed_request_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = root / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(BridgeError):
                evaluator_cli.load_validated_request(bad, root=root)
            outside = Path(tempfile.mkdtemp()) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaises(BridgeError):
                evaluator_cli.load_validated_request(outside, root=root)


class ManifestGateTests(unittest.TestCase):
    def _manifest_args(self, **overrides):
        args = argparse.Namespace(
            candidate_id="probe-candidate",
            topic="Probe",
            topic_cluster="probe",
            content_type="NEWS",
            format="FEED",
            caption_file="x.txt",
            media=[],
            manifest_id=None,
            output=None,
            force=False,
            packaging_receipt="receipt.json",
            render_record="record.json",
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _write_receipt_in_workspace(self, receipt, candidate_id="probe-candidate"):
        path = DRAFTS_PROD / f"{candidate_id}-packaging-decision.json"
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt), encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def _write_render_record_in_workspace(self, receipt, rel_paths, candidate_id="probe-candidate"):
        import hashlib

        record = receipt_mod.build_render_record(
            candidate_id=candidate_id,
            receipt_hash=receipt["receipt_hash"],
            format_decision=receipt["FORMAT_DECISION"],
            asset_kind="NONE",
            outputs=[
                {"path": rel, "sha256": hashlib.sha256((DRAFTS_PROD / Path(rel).name).read_bytes()).hexdigest()}
                for rel in rel_paths
            ],
        )
        path = DRAFTS_PROD / f"{candidate_id}-render-record.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_single_fact_carousel_manifest_blocked(self):
        manifest = _load_hyphenated("nullone_manifest_cli", "nullone-manifest.py")

        receipt = _receipt_for(_request())
        self.assertEqual(receipt["FORMAT_DECISION"], "SINGLE_POST")
        path = self._write_receipt_in_workspace(receipt)
        args = self._manifest_args(format="CAROUSEL", packaging_receipt=str(path))
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("PACKAGING_DECISION_MISMATCH", str(ctx.exception))

    def test_skip_receipt_builds_nothing(self):
        manifest = _load_hyphenated("nullone_manifest_cli", "nullone-manifest.py")

        request = _request(**{"candidate.verification_status": "BLOCKED"})
        receipt = _receipt_for(request)
        self.assertEqual(receipt["POST_DECISION"], "SKIP")
        path = self._write_receipt_in_workspace(receipt)
        args = self._manifest_args(format="FEED", packaging_receipt=str(path))
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("PACKAGING_SKIPPED", str(ctx.exception))

    def test_story_receipt_delegated_not_built(self):
        manifest = _load_hyphenated("nullone_manifest_cli", "nullone-manifest.py")

        request = _request(
            **{
                "candidate.content_shape": "BREAKING_DEVELOPING",
                "candidate.timeliness": "BREAKING",
                "candidate.still_developing": True,
                "candidate.audience_value": "HIGH",
                "assets.has_official_or_source_image": True,
                "assets.image_on_topic": True,
                "assets.image_quality_ok": True,
            }
        )
        receipt = _receipt_for(request)
        self.assertEqual(receipt["FORMAT_DECISION"], "STORY")
        path = self._write_receipt_in_workspace(receipt)
        args = self._manifest_args(format="STORY", packaging_receipt=str(path))
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("PACKAGING_STORY_DELEGATED", str(ctx.exception))

    def test_candidate_mismatch_blocked(self):
        manifest = _load_hyphenated("nullone_manifest_cli", "nullone-manifest.py")

        receipt = _receipt_for(_request(), candidate_id="other-candidate")
        path = self._write_receipt_in_workspace(receipt, candidate_id="other-candidate")
        args = self._manifest_args(format="FEED", packaging_receipt=str(path))
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("candidate", str(ctx.exception))

    def test_arbitrary_media_substitution_blocked(self):
        manifest = _load_hyphenated("nullone_manifest_cli", "nullone-manifest.py")
        from PIL import Image

        tag = uuid.uuid4().hex
        real = DRAFTS_PROD / f"test-{tag}-real.png"
        rogue = DRAFTS_PROD / f"test-{tag}-rogue.png"
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        for p in (real, rogue):
            self.addCleanup(lambda p=p: p.unlink(missing_ok=True))
            Image.new("RGB", (1080, 1350), (14, 14, 15)).save(p)
        receipt = _receipt_for(_request())
        receipt_path = self._write_receipt_in_workspace(receipt)
        record_path = self._write_render_record_in_workspace(
            receipt, [f"social/drafts/production/{real.name}"]
        )
        args = self._manifest_args(
            format="FEED",
            packaging_receipt=str(receipt_path),
            render_record=str(record_path),
            media=[f"social/drafts/production/{rogue.name}"],
        )
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("PACKAGING_DECISION_MISMATCH", str(ctx.exception))

    def test_matching_receipt_builds_manifest(self):
        manifest = _load_hyphenated("nullone_manifest_cli", "nullone-manifest.py")
        from PIL import Image

        tag = uuid.uuid4().hex
        caption = DRAFTS_PROD / f"test-{tag}-caption.txt"
        image = DRAFTS_PROD / f"test-{tag}.png"
        from nullone_bridge_common import MANIFEST_DIR

        out = MANIFEST_DIR / f"test-{tag}.json"
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        for p in (caption, image, out):
            self.addCleanup(lambda p=p: p.unlink(missing_ok=True))
        caption.write_text("Probe caption.", encoding="utf-8")
        Image.new("RGB", (1080, 1350), (14, 14, 15)).save(image)
        receipt = _receipt_for(_request())
        receipt_path = self._write_receipt_in_workspace(receipt)
        record_path = self._write_render_record_in_workspace(
            receipt, [f"social/drafts/production/{image.name}"]
        )
        args = self._manifest_args(
            candidate_id="probe-candidate",
            format="FEED",
            caption_file=str(image.parent / caption.name),
            media=[str(image)],
            output=str(out),
            manifest_id=f"test-{tag}",
            packaging_receipt=str(receipt_path),
            render_record=str(record_path),
        )
        # caption/media must resolve inside the workspace: use relative paths.
        args.caption_file = f"social/drafts/production/{caption.name}"
        args.media = [f"social/drafts/production/{image.name}"]
        self.assertEqual(manifest.build(args), 0)
        built = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(built["format"], "FEED")
        self.assertEqual(built["candidate_id"], "probe-candidate")
        self.assertEqual(built["packaging"]["receipt_hash"], receipt["receipt_hash"])

    def test_manifest_output_outside_manifests_dir_blocked(self):
        manifest = _load_hyphenated("nullone_manifest_cli4", "nullone-manifest.py")
        from PIL import Image

        tag = uuid.uuid4().hex
        caption = DRAFTS_PROD / f"test-{tag}-caption.txt"
        image = DRAFTS_PROD / f"test-{tag}.png"
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        for p in (caption, image):
            self.addCleanup(lambda p=p: p.unlink(missing_ok=True))
        caption.write_text("Probe caption.", encoding="utf-8")
        Image.new("RGB", (1080, 1350), (14, 14, 15)).save(image)
        receipt = _receipt_for(_request())
        receipt_path = self._write_receipt_in_workspace(receipt)
        record_path = self._write_render_record_in_workspace(
            receipt, [f"social/drafts/production/{image.name}"]
        )
        args = self._manifest_args(
            format="FEED",
            caption_file=f"social/drafts/production/{caption.name}",
            media=[f"social/drafts/production/{image.name}"],
            output=f"social/drafts/production/{tag}-bypass.json",
            manifest_id=f"test-{tag}",
            packaging_receipt=str(receipt_path),
            render_record=str(record_path),
        )
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("manifests directory", str(ctx.exception))
        self.assertFalse((DRAFTS_PROD / f"{tag}-bypass.json").exists())

    def test_manifest_id_traversal_blocked(self):
        manifest = _load_hyphenated("nullone_manifest_cli5", "nullone-manifest.py")
        receipt = _receipt_for(_request())
        receipt_path = self._write_receipt_in_workspace(receipt)
        args = self._manifest_args(
            manifest_id="../evil",
            packaging_receipt=str(receipt_path),
            render_record="unused.json",
        )
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("manifest_id", str(ctx.exception))


class RenderDispatcherTests(unittest.TestCase):
    def _args(self, receipt_path, asset_file=None, output=None, **overrides):
        args = argparse.Namespace(
            receipt=str(receipt_path),
            asset_file=str(asset_file) if asset_file is not None else "",
            output=output or "",
            spec=None,
            source=None,
            kicker=None,
            headline=None,
            stat=None,
            source_name=None,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _receipt_file(self, root, receipt, candidate_id="probe-candidate"):
        subdir = root / "social/drafts/production"
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / f"{candidate_id}-packaging-decision.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        return path

    def _asset_file(self, root, receipt, **overrides):
        descriptor = _asset_descriptor(
            candidate_id=receipt["candidate_id"],
            asset_kind={
                "REAL_PHOTO": "REAL_PHOTO",
                "SOURCE_SCREENSHOT": "SOURCE_SCREENSHOT",
                "DATA_VISUALIZATION": "DATA_VISUALIZATION",
            }.get(receipt["VISUAL_STYLE"], "NONE"),
            **overrides,
        )
        path = root / "asset.json"
        path.write_text(json.dumps(descriptor), encoding="utf-8")
        return path

    def test_skip_renders_nothing(self):
        calls: list = []
        request = _request(**{"candidate.verification_status": "BLOCKED"})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = self._receipt_file(root, _receipt_for(request))
            with mock.patch.object(
                dispatcher.subprocess, "run", side_effect=lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a[0], 0, "", "")
            ):
                with self.assertRaises(BridgeError) as ctx:
                    dispatcher.render_command(self._args(receipt), root=root)
        self.assertIn("PACKAGING_SKIPPED", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_carousel_slide_mismatch_blocked_without_subprocess(self):
        calls: list = []
        request = _request(
            **{
                "candidate.content_shape": "MULTI_STEP_EXPLAINER",
                "candidate.distinct_beat_count": 4,
                "candidate.content_type": "EXPLAINER",
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(request)
            self.assertEqual(receipt["FORMAT_DECISION"], "CAROUSEL")
            self.assertEqual(receipt["slide_count_recommendation"], 6)
            receipt_path = self._receipt_file(root, receipt)
            asset_path = self._asset_file(root, receipt)
            spec = root / "spec.json"
            spec.write_text(json.dumps({"slides": [{}, {}]}), encoding="utf-8")
            with mock.patch.object(
                dispatcher.subprocess, "run", side_effect=lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a[0], 0, "", "")
            ):
                with self.assertRaises(BridgeError) as ctx:
                    dispatcher.render_command(self._args(receipt_path, asset_file=str(asset_path), spec=str(spec), output=str(root / "o")), root=root)
        self.assertIn("PACKAGING_DECISION_MISMATCH", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_carousel_exact_count_renders(self):
        calls: list = []
        request = _request(
            **{
                "candidate.content_shape": "MULTI_STEP_EXPLAINER",
                "candidate.distinct_beat_count": 4,
                "candidate.content_type": "EXPLAINER",
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(request)
            receipt_path = self._receipt_file(root, receipt)
            asset_path = self._asset_file(root, receipt)
            spec = root / "spec.json"
            spec.write_text(json.dumps({"slides": [{}, {}, {}, {}, {}, {}]}), encoding="utf-8")
            out_dir = root / "slides"

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out_dir.mkdir(parents=True, exist_ok=True)
                for i in range(1, 7):
                    (out_dir / f"{i:02d}.png").write_text("png", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "SLIDES=6", "")

            with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
                self.assertEqual(
                    dispatcher.render_command(
                        self._args(receipt_path, asset_file=str(asset_path), spec=str(spec), output=str(out_dir)), root=root
                    ),
                    0,
                )
        self.assertEqual(len(calls), 1)
        self.assertIn("render_carousel_v2.py", calls[0][1])

    def test_feed_path_ignores_carousel_spec(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(_request())
            receipt_path = self._receipt_file(root, receipt)
            asset_path = self._asset_file(root, receipt)
            out = root / "feed.png"

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out.write_text("png", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "VALID=true", "")

            with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
                self.assertEqual(
                    dispatcher.render_command(
                        self._args(
                            receipt_path,
                            asset_file=str(asset_path),
                            output=str(out),
                            kicker="k",
                            headline="h",
                            source_name="n",
                        ),
                        root=root,
                    ),
                    0,
                )
        self.assertEqual(len(calls), 1)
        self.assertIn("render_texbrif_v2.py", calls[0][1])
        self.assertNotIn("--source", calls[0])

    def test_typography_freeform_source_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(_request())
            receipt_path = self._receipt_file(root, receipt)
            asset_path = self._asset_file(root, receipt)
            rogue = root / "rogue.jpg"
            rogue.write_bytes(b"rogue")
            with mock.patch.object(
                dispatcher.subprocess, "run", side_effect=AssertionError("must not render")
            ):
                with self.assertRaises(BridgeError) as ctx:
                    dispatcher.render_command(
                        self._args(
                            receipt_path, asset_file=str(asset_path), output=str(root / "o"),
                            source=str(rogue), kicker="k", headline="h", source_name="n",
                        ),
                        root=root,
                    )
        self.assertIn("smuggling blocked", str(ctx.exception))

    def test_generated_illustration_style_fails_closed(self):
        calls: list = []
        request = _request(
            **{
                "assets.has_usable_screenshot": True,
                "assets.image_on_topic": False,
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(request)
            self.assertEqual(receipt["VISUAL_STYLE"], "GENERATED_ILLUSTRATION_ALLOWED")
            receipt_path = self._receipt_file(root, receipt)
            asset_path = self._asset_file(root, receipt)
            with mock.patch.object(
                dispatcher.subprocess, "run", side_effect=lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a[0], 0, "", "")
            ):
                with self.assertRaises(BridgeError) as ctx:
                    dispatcher.render_command(
                        self._args(
                            receipt_path, asset_file=str(asset_path), output=str(root / "o"),
                            kicker="k", headline="h", source_name="n",
                        ),
                        root=root,
                    )
        self.assertIn("PACKAGING_UNSUPPORTED_STYLE", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_carousel_real_photo_fails_closed(self):
        calls: list = []
        request = _request(
            **{
                "candidate.content_shape": "MULTI_STEP_EXPLAINER",
                "candidate.distinct_beat_count": 4,
                "candidate.content_type": "EXPLAINER",
                "candidate.depicts_real_world_subject": True,
                "assets.has_official_or_source_image": True,
                "assets.image_on_topic": True,
                "assets.image_quality_ok": True,
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(request)
            self.assertEqual(receipt["FORMAT_DECISION"], "CAROUSEL")
            self.assertEqual(receipt["VISUAL_STYLE"], "REAL_PHOTO")
            receipt_path = self._receipt_file(root, receipt)
            photo = root / "photo.jpg"
            photo.write_bytes(b"photo")
            asset = _asset_descriptor(
                asset_kind="REAL_PHOTO", local_path=str(photo), provenance="Official",
            )
            asset_path = root / "asset.json"
            asset_path.write_text(json.dumps(asset), encoding="utf-8")
            spec = root / "spec.json"
            spec.write_text(json.dumps({"slides": [{}, {}, {}, {}, {}, {}]}), encoding="utf-8")
            with mock.patch.object(
                dispatcher.subprocess, "run", side_effect=lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a[0], 0, "", "")
            ):
                with self.assertRaises(BridgeError) as ctx:
                    dispatcher.render_command(
                        self._args(receipt_path, asset_file=str(asset_path), spec=str(spec), output=str(root / "o")),
                        root=root,
                    )
        self.assertIn("PACKAGING_UNSUPPORTED_STYLE", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_story_never_renders_in_factory(self):
        calls: list = []
        request = _request(
            **{
                "candidate.content_shape": "BREAKING_DEVELOPING",
                "candidate.timeliness": "BREAKING",
                "candidate.still_developing": True,
                "candidate.audience_value": "HIGH",
                "assets.has_official_or_source_image": True,
                "assets.image_on_topic": True,
                "assets.image_quality_ok": True,
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt_path = self._receipt_file(root, _receipt_for(request))
            with mock.patch.object(
                dispatcher.subprocess, "run", side_effect=lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a[0], 0, "", "")
            ):
                with self.assertRaises(BridgeError) as ctx:
                    dispatcher.render_command(self._args(receipt_path, output=str(root / "o")), root=root)
        self.assertIn("PACKAGING_STORY_DELEGATED", str(ctx.exception))
        self.assertEqual(calls, [])


class CanonicalReceiptTests(unittest.TestCase):
    def test_alternate_filename_for_same_candidate_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(_request())
            alt = root / "some-other-name.json"
            alt.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaises(BridgeError) as ctx:
                receipt_mod.require_canonical_receipt(alt, "probe-candidate", root=root)
            self.assertIn("canonical", str(ctx.exception))

    def test_subdirectory_receipt_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sub = root / "sub"
            sub.mkdir()
            receipt = _receipt_for(_request())
            nested = sub / "probe-candidate-packaging-decision.json"
            nested.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaises(BridgeError):
                receipt_mod.require_canonical_receipt(nested, "probe-candidate", root=root)

    def test_canonical_path_derived_from_candidate_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = receipt_mod.canonical_receipt_path("my-candidate-1", root=root)
            self.assertEqual(path.name, "my-candidate-1-packaging-decision.json")
            self.assertEqual(
                path, (root / "social/drafts/production/my-candidate-1-packaging-decision.json").resolve()
            )

    def test_candidate_traversal_blocked(self):
        for bad in ("../evil", "a/b", "a\\b", "", "  "):
            with self.assertRaises(BridgeError):
                receipt_mod.check_candidate_id(bad)

    def test_evaluator_writes_canonical_path_only(self):
        import argparse as _argparse

        candidate_id = f"test-{uuid.uuid4().hex}"
        request = _request()
        request_path = DRAFTS_PROD / f"{candidate_id}-request.json"
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(request), encoding="utf-8")
        canonical = DRAFTS_PROD / f"{candidate_id}-packaging-decision.json"
        self.addCleanup(lambda: request_path.unlink(missing_ok=True))
        self.addCleanup(lambda: canonical.unlink(missing_ok=True))
        args = _argparse.Namespace(candidate_id=candidate_id, request_file=str(request_path))
        self.assertEqual(evaluator_cli.evaluate_command(args), 0)
        self.assertTrue(canonical.is_file())
        receipt = json.loads(canonical.read_text(encoding="utf-8"))
        self.assertEqual(receipt["candidate_id"], candidate_id)
        # Idempotent re-evaluation succeeds.
        self.assertEqual(evaluator_cli.evaluate_command(args), 0)
        # Changed request for the same candidate conflicts.
        request["candidate"] = {**request["candidate"], "distinct_beat_count": 2}
        request_path.write_text(json.dumps(request), encoding="utf-8")
        with self.assertRaises(BridgeError) as ctx:
            evaluator_cli.evaluate_command(args)
        self.assertIn("CONFLICT", str(ctx.exception))


class AssetDescriptorTests(unittest.TestCase):
    def _photo_receipt(self, root, candidate_id="photo-candidate"):
        request = _request(
            **{
                "candidate.content_shape": "ANNOUNCEMENT",
                "candidate.distinct_beat_count": 1,
                "candidate.depicts_real_world_subject": True,
                "assets.has_official_or_source_image": True,
                "assets.image_on_topic": True,
                "assets.image_quality_ok": True,
            }
        )
        receipt = _receipt_for(request, candidate_id=candidate_id)
        self.assertEqual(receipt["VISUAL_STYLE"], "REAL_PHOTO")
        return receipt

    def test_real_photo_descriptor_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            photo = root / "photo.jpg"
            photo.write_bytes(b"fake-image-bytes")
            receipt = self._photo_receipt(root)
            descriptor = _asset_descriptor(
                candidate_id="photo-candidate", asset_kind="REAL_PHOTO",
                local_path=str(photo), provenance="Official product page",
            )
            validated = receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)
            self.assertEqual(validated["asset_kind"], "REAL_PHOTO")

    def test_real_photo_receipt_rejects_screenshot_kind(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shot = root / "shot.png"
            shot.write_bytes(b"x")
            receipt = self._photo_receipt(root)
            descriptor = _asset_descriptor(
                candidate_id="photo-candidate", asset_kind="SOURCE_SCREENSHOT",
                local_path=str(shot), provenance="screenshot",
            )
            with self.assertRaises(BridgeError) as ctx:
                receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)
            self.assertIn("MISMATCH", str(ctx.exception))

    def test_real_photo_receipt_rejects_generated_kind(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = self._photo_receipt(root)
            descriptor = _asset_descriptor(candidate_id="photo-candidate", asset_kind="NONE")
            with self.assertRaises(BridgeError):
                receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)

    def test_screenshot_receipt_rejects_real_photo_descriptor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            request = _request(
                **{
                    "candidate.content_shape": "ANNOUNCEMENT",
                    "candidate.distinct_beat_count": 1,
                    "assets.has_usable_screenshot": True,
                    "assets.image_on_topic": True,
                }
            )
            receipt = _receipt_for(request, candidate_id="shot-candidate")
            self.assertEqual(receipt["VISUAL_STYLE"], "SOURCE_SCREENSHOT")
            photo = root / "photo.jpg"
            photo.write_bytes(b"x")
            descriptor = _asset_descriptor(
                candidate_id="shot-candidate", asset_kind="REAL_PHOTO",
                local_path=str(photo), provenance="official",
            )
            with self.assertRaises(BridgeError) as ctx:
                receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)
            self.assertIn("MISMATCH", str(ctx.exception))

    def test_dataviz_receipt_rejects_arbitrary_image(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            request = _request(
                **{
                    "candidate.content_shape": "ANNOUNCEMENT",
                    "candidate.distinct_beat_count": 1,
                    "assets.data_visualization_possible": True,
                }
            )
            receipt = _receipt_for(request, candidate_id="viz-candidate")
            self.assertEqual(receipt["VISUAL_STYLE"], "DATA_VISUALIZATION")
            photo = root / "random.jpg"
            photo.write_bytes(b"x")
            descriptor = _asset_descriptor(
                candidate_id="viz-candidate", asset_kind="REAL_PHOTO",
                local_path=str(photo), provenance="random",
            )
            with self.assertRaises(BridgeError):
                receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)

    def test_typography_claim_with_file_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(_request())
            descriptor = _asset_descriptor(
                asset_kind="NONE", local_path=str(root / "sneaky.png"),
            )
            with self.assertRaises(BridgeError):
                receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)

    def test_missing_provenance_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            photo = root / "photo.jpg"
            photo.write_bytes(b"x")
            receipt = self._photo_receipt(root)
            descriptor = _asset_descriptor(
                candidate_id="photo-candidate", asset_kind="REAL_PHOTO",
                local_path=str(photo), provenance="  ",
            )
            with self.assertRaises(BridgeError):
                receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)


class AgentReceiptDenyTests(unittest.TestCase):
    def test_model_can_write_request_but_not_receipt(self):
        import fnmatch as _fnmatch

        text = (ROOT / "workspace/.opencode/agents/nullone-draft-factory.md").read_text(encoding="utf-8")
        head = text.split("---", 2)[1]
        rules: dict = {}
        current = None
        in_permission = False
        for raw in head.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if indent == 0:
                in_permission = stripped == "permission:"
                current = None
                continue
            if not in_permission:
                continue
            if indent == 2:
                if stripped.endswith(":"):
                    current = stripped[:-1]
                    rules[current] = []
                else:
                    key, value = stripped.split(":", 1)
                    rules[key.strip()] = value.strip()
            elif indent == 4 and current is not None:
                pattern, action = stripped.rsplit(":", 1)
                rules[current].append((pattern.strip().strip('"'), action.strip()))

        def resolve(value: str):
            result = None
            for pattern, action in rules["edit"]:
                if _fnmatch.fnmatch(value, pattern):
                    result = action
            return result

        self.assertEqual(resolve(f"{WS}/social/drafts/production/candidate-1-packaging-request.json"), "allow")
        self.assertEqual(resolve(f"{WS}/social/drafts/production/candidate-1-packaging-asset.json"), "allow")
        self.assertEqual(resolve(f"{WS}/social/drafts/production/candidate-1-packaging-decision.json"), "deny")
        self.assertEqual(resolve(f"{WS}/social/drafts/production/candidate-1-render-record.json"), "deny")


class SourcePropagationTests(unittest.TestCase):
    def _photo_setup(self, root, candidate_id="src-candidate"):
        request = _request(
            **{
                "candidate.content_shape": "SINGLE_FACT",
                "candidate.distinct_beat_count": 1,
                "candidate.depicts_real_world_subject": True,
                "assets.has_official_or_source_image": True,
                "assets.image_on_topic": True,
                "assets.image_quality_ok": True,
            }
        )
        receipt = _receipt_for(request, candidate_id=candidate_id)
        self.assertEqual(receipt["VISUAL_STYLE"], "REAL_PHOTO")
        subdir = root / "social/drafts/production"
        subdir.mkdir(parents=True, exist_ok=True)
        receipt_path = subdir / f"{candidate_id}-packaging-decision.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        photo = root / "evidence.jpg"
        photo.write_bytes(b"real-photo-bytes")
        asset = _asset_descriptor(
            candidate_id=candidate_id, asset_kind="REAL_PHOTO",
            local_path=str(photo), provenance="Official product page",
        )
        asset_path = root / "asset.json"
        asset_path.write_text(json.dumps(asset), encoding="utf-8")
        return receipt_path, asset_path, photo

    def test_descriptor_source_reaches_renderer_without_freeform(self):
        import argparse as _argparse

        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt_path, asset_path, photo = self._photo_setup(root)
            out = root / "feed.png"

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out.write_text("png", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "VALID=true", "")

            args = _argparse.Namespace(
                receipt=str(receipt_path), asset_file=str(asset_path), output=str(out),
                spec=None, source=None, kicker="k", headline="h", stat=None, source_name="n",
            )
            with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
                self.assertEqual(dispatcher.render_command(args, root=root), 0)
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertIn(str(photo), argv)
        self.assertNotIn(None, argv)

    def test_freeform_source_cannot_override_descriptor(self):
        import argparse as _argparse

        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt_path, asset_path, photo = self._photo_setup(root)
            rogue = root / "rogue.jpg"
            rogue.write_bytes(b"rogue")
            out = root / "feed.png"

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out.write_text("png", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "VALID=true", "")

            args = _argparse.Namespace(
                receipt=str(receipt_path), asset_file=str(asset_path), output=str(out),
                spec=None, source=str(rogue), kicker="k", headline="h", stat=None, source_name="n",
            )
            with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
                self.assertEqual(dispatcher.render_command(args, root=root), 0)
        sent_source = calls[0][calls[0].index("--source") + 1]
        self.assertEqual(sent_source, str(photo))
        self.assertNotEqual(sent_source, str(rogue))


class DataVisualizationBoundTests(unittest.TestCase):
    def _viz_receipt(self, root, candidate_id="viz-candidate"):
        request = _request(
            **{
                "candidate.content_shape": "ANNOUNCEMENT",
                "candidate.distinct_beat_count": 1,
                "assets.data_visualization_possible": True,
            }
        )
        receipt = _receipt_for(request, candidate_id=candidate_id)
        self.assertEqual(receipt["VISUAL_STYLE"], "DATA_VISUALIZATION")
        return receipt

    def test_dataviz_without_local_path_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = self._viz_receipt(root)
            descriptor = _asset_descriptor(
                candidate_id="viz-candidate", asset_kind="DATA_VISUALIZATION",
                provenance="Verified dataset",
            )
            with self.assertRaises(BridgeError):
                receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)

    def test_dataviz_outside_workspace_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = self._viz_receipt(root)
            descriptor = _asset_descriptor(
                candidate_id="viz-candidate", asset_kind="DATA_VISUALIZATION",
                local_path="/etc/hostname", provenance="Verified dataset",
            )
            with self.assertRaises(BridgeError):
                receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)

    def test_dataviz_wrong_hash_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = self._viz_receipt(root)
            data = root / "data.json"
            data.write_text('{"n": 1}', encoding="utf-8")
            descriptor = _asset_descriptor(
                candidate_id="viz-candidate", asset_kind="DATA_VISUALIZATION",
                local_path=str(data), provenance="Verified dataset", sha256="0" * 64,
            )
            with self.assertRaises(BridgeError) as ctx:
                receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)
            self.assertIn("sha256", str(ctx.exception))

    def test_dataviz_valid_artifact_accepted_with_computed_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = self._viz_receipt(root)
            data = root / "data.json"
            data.write_text('{"n": 1}', encoding="utf-8")
            descriptor = _asset_descriptor(
                candidate_id="viz-candidate", asset_kind="DATA_VISUALIZATION",
                local_path=str(data), provenance="Verified dataset",
            )
            validated = receipt_mod.validate_asset_descriptor(descriptor, receipt, root=root)
            import hashlib as _hashlib

            self.assertEqual(validated["sha256"], _hashlib.sha256(b'{"n": 1}').hexdigest())
            self.assertEqual(validated["asset_kind"], "DATA_VISUALIZATION")


class CanonicalRenderRecordTests(unittest.TestCase):
    def test_alternate_render_record_filename_rejected(self):
        import argparse as _argparse

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record = receipt_mod.build_render_record(
                candidate_id="probe-candidate", receipt_hash="h", format_decision="SINGLE_POST",
                asset_kind="NONE", outputs=[{"path": "x.png", "sha256": "y"}],
            )
            alt = root / "alt-record.json"
            alt.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(BridgeError):
                receipt_mod.require_canonical_render_record(alt, "probe-candidate", root=root)

    def test_wrong_candidate_canonical_record_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subdir = root / "social/drafts/production"
            subdir.mkdir(parents=True, exist_ok=True)
            other = subdir / "other-candidate-render-record.json"
            other.write_text("{}", encoding="utf-8")
            with self.assertRaises(BridgeError):
                receipt_mod.require_canonical_render_record(other, "probe-candidate", root=root)


class ManifestIntegrityTests(unittest.TestCase):
    def test_mutated_bytes_blocked(self):
        manifest = _load_hyphenated("nullone_manifest_cli2", "nullone-manifest.py")
        from PIL import Image

        tag = uuid.uuid4().hex
        caption = DRAFTS_PROD / f"test-{tag}-caption.txt"
        image = DRAFTS_PROD / f"test-{tag}.png"
        out = DRAFTS_PROD / f"test-{tag}-manifest.json"
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        for p in (caption, image, out):
            self.addCleanup(lambda p=p: p.unlink(missing_ok=True))
        caption.write_text("Probe caption.", encoding="utf-8")
        Image.new("RGB", (1080, 1350), (14, 14, 15)).save(image)
        receipt = _receipt_for(_request())
        receipt_path = DRAFTS_PROD / "probe-candidate-packaging-decision.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.addCleanup(lambda: receipt_path.unlink(missing_ok=True))
        record = receipt_mod.build_render_record(
            candidate_id="probe-candidate", receipt_hash=receipt["receipt_hash"],
            format_decision="SINGLE_POST", asset_kind="NONE",
            outputs=[{"path": f"social/drafts/production/{image.name}", "sha256": "0" * 64}],
        )
        record_path = DRAFTS_PROD / "probe-candidate-render-record.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.addCleanup(lambda: record_path.unlink(missing_ok=True))
        args = argparse.Namespace(
            candidate_id="probe-candidate", topic="Probe", topic_cluster="probe",
            content_type="NEWS", format="FEED",
            caption_file=f"social/drafts/production/{caption.name}",
            media=[f"social/drafts/production/{image.name}"],
            manifest_id=None, output=f"social/drafts/production/{out.name}", force=False,
            packaging_receipt=str(receipt_path), render_record=str(record_path),
        )
        # Same path, mutated bytes (valid PNG, different pixels) -> blocked.
        Image.new("RGB", (1080, 1350), (200, 10, 10)).save(image)
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("INTEGRITY", str(ctx.exception))

    def test_carousel_one_slide_mutation_blocks_manifest(self):
        manifest = _load_hyphenated("nullone_manifest_cli3", "nullone-manifest.py")
        from PIL import Image

        tag = uuid.uuid4().hex
        slides = []
        for i in range(6):
            slide = DRAFTS_PROD / f"test-{tag}-s{i:02d}.png"
            Image.new("RGB", (1080, 1350), (14, 14, 15)).save(slide)
            self.addCleanup(lambda p=slide: p.unlink(missing_ok=True))
            slides.append(f"social/drafts/production/{slide.name}")
        caption = DRAFTS_PROD / f"test-{tag}-caption.txt"
        caption.write_text("Probe caption.", encoding="utf-8")
        self.addCleanup(lambda: caption.unlink(missing_ok=True))
        out = DRAFTS_PROD / f"test-{tag}-manifest.json"
        self.addCleanup(lambda: out.unlink(missing_ok=True))
        request = _request(
            **{
                "candidate.content_shape": "MULTI_STEP_EXPLAINER",
                "candidate.distinct_beat_count": 4,
                "candidate.content_type": "EXPLAINER",
            }
        )
        receipt = _receipt_for(request)
        receipt_path = DRAFTS_PROD / "probe-candidate-packaging-decision.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.addCleanup(lambda: receipt_path.unlink(missing_ok=True))
        import hashlib as _hashlib

        record = receipt_mod.build_render_record(
            candidate_id="probe-candidate", receipt_hash=receipt["receipt_hash"],
            format_decision="CAROUSEL", asset_kind="NONE",
            outputs=[
                {"path": rel, "sha256": _hashlib.sha256((DRAFTS_PROD / Path(rel).name).read_bytes()).hexdigest()}
                for rel in slides
            ],
        )
        record_path = DRAFTS_PROD / "probe-candidate-render-record.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.addCleanup(lambda: record_path.unlink(missing_ok=True))
        # Mutate exactly one slide after the record was made.
        Image.new("RGB", (1080, 1350), (1, 2, 3)).save(DRAFTS_PROD / Path(slides[3]).name)
        args = argparse.Namespace(
            candidate_id="probe-candidate", topic="Probe", topic_cluster="probe",
            content_type="EXPLAINER", format="CAROUSEL",
            caption_file=f"social/drafts/production/{caption.name}",
            media=slides,
            manifest_id=None, output=f"social/drafts/production/{out.name}", force=False,
            packaging_receipt=str(receipt_path), render_record=str(record_path),
        )
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("INTEGRITY", str(ctx.exception))


class AssetContainmentOrderTests(unittest.TestCase):
    def test_external_asset_path_rejected_without_read(self):
        import argparse as _argparse

        reads: list = []
        real_read_text = Path.read_text

        def recording_read_text(self, *args, **kwargs):
            if "outside" in str(self):
                reads.append(str(self))
            return real_read_text(self, *args, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subdir = root / "social/drafts/production"
            subdir.mkdir(parents=True, exist_ok=True)
            receipt = _receipt_for(_request())
            receipt_path = subdir / "probe-candidate-packaging-decision.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            outside = Path(tempfile.mkdtemp()) / "outside-asset.json"
            outside.write_text(json.dumps({"asset_kind": "NONE"}), encoding="utf-8")
            args = _argparse.Namespace(
                receipt=str(receipt_path), asset_file=str(outside), output=str(root / "o"),
                spec=None, source=None, kicker=None, headline=None, stat=None, source_name=None,
            )
            with mock.patch.object(Path, "read_text", autospec=True, side_effect=recording_read_text):
                with self.assertRaises(BridgeError):
                    dispatcher.render_command(args, root=root)
        self.assertEqual(reads, [])


class AgentRenderRecordDenyTests(unittest.TestCase):
    def test_model_cannot_write_render_record(self):
        import fnmatch as _fnmatch

        text = (ROOT / "workspace/.opencode/agents/nullone-draft-factory.md").read_text(encoding="utf-8")
        head = text.split("---", 2)[1]
        rules: list = []
        for raw in head.splitlines():
            stripped = raw.strip()
            if stripped.startswith('"**/social/drafts/production/'):
                pattern, action = stripped.rsplit(":", 1)
                rules.append((pattern.strip().strip('"'), action.strip()))

        def resolve(value: str):
            result = None
            for pattern, action in rules:
                if _fnmatch.fnmatch(value, pattern):
                    result = action
            return result

        self.assertEqual(resolve(f"{WS}/social/drafts/production/candidate-1-render-record.json"), "deny")


class BridgeGateTests(unittest.TestCase):
    def _valid_factory_manifest(self, tag=None):

        manifest = _load_hyphenated("nullone_manifest_cli6", "nullone-manifest.py")
        from PIL import Image

        tag = tag or uuid.uuid4().hex
        caption = DRAFTS_PROD / f"test-{tag}-caption.txt"
        image = DRAFTS_PROD / f"test-{tag}.png"
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        for p in (caption, image):
            self.addCleanup(lambda p=p: p.unlink(missing_ok=True))
        caption.write_text("Probe caption.", encoding="utf-8")
        Image.new("RGB", (1080, 1350), (14, 14, 15)).save(image)
        receipt = _receipt_for(_request())
        receipt_path = DRAFTS_PROD / "probe-candidate-packaging-decision.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.addCleanup(lambda: receipt_path.unlink(missing_ok=True))
        record = receipt_mod.build_render_record(
            candidate_id="probe-candidate", receipt_hash=receipt["receipt_hash"],
            format_decision="SINGLE_POST", asset_kind="NONE",
            outputs=[{"path": f"social/drafts/production/{image.name}",
                      "sha256": __import__("hashlib").sha256(image.read_bytes()).hexdigest()}],
        )
        record_path = DRAFTS_PROD / "probe-candidate-render-record.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.addCleanup(lambda: record_path.unlink(missing_ok=True))
        args = argparse.Namespace(
            candidate_id="probe-candidate", topic="Probe", topic_cluster="probe",
            content_type="NEWS", format="FEED",
            caption_file=f"social/drafts/production/{caption.name}",
            media=[f"social/drafts/production/{image.name}"],
            manifest_id=f"test-{tag}", output=None, force=False,
            packaging_receipt=str(receipt_path), render_record=str(record_path),
        )
        self.assertEqual(manifest.build(args), 0)
        from nullone_bridge_common import MANIFEST_DIR

        manifest_path = MANIFEST_DIR / f"test-{tag}.json"
        self.addCleanup(lambda: manifest_path.unlink(missing_ok=True))
        return manifest_path

    def _execute_blocked(self, manifest_path):
        import contextlib
        import io

        bridge = _load_hyphenated("nullone_draft_bridge_cli", "nullone-draft-bridge.py")

        with mock.patch.object(
            bridge, "build_production_draft_provider",
            side_effect=AssertionError("provider must not be reached"),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = bridge.execute(
                    str(manifest_path.relative_to(WORKSPACE))
                    if manifest_path.is_absolute() else str(manifest_path)
                )
        return code, buf.getvalue()

    def test_handwritten_manifest_without_authority_blocked(self):
        manifest_path = self._valid_factory_manifest()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        del data["packaging"]
        hand = manifest_path.with_name(manifest_path.stem + "-hand.json")
        hand.write_text(json.dumps(data), encoding="utf-8")
        self.addCleanup(lambda: hand.unlink(missing_ok=True))
        code, out = self._execute_blocked(hand)
        self.assertEqual(code, 2)
        self.assertIn("PACKAGING_DECISION_MISMATCH", out)

    def test_tampered_receipt_hash_blocked(self):
        manifest_path = self._valid_factory_manifest()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["packaging"]["receipt_hash"] = "0" * 64
        tampered = manifest_path.with_name(manifest_path.stem + "-tamper.json")
        tampered.write_text(json.dumps(data), encoding="utf-8")
        self.addCleanup(lambda: tampered.unlink(missing_ok=True))
        code, out = self._execute_blocked(tampered)
        self.assertEqual(code, 2)
        self.assertIn("PACKAGING", out)

    def test_substituted_media_blocked(self):
        from PIL import Image

        manifest_path = self._valid_factory_manifest()
        tag = uuid.uuid4().hex
        rogue = DRAFTS_PROD / f"test-{tag}-rogue.png"
        Image.new("RGB", (1080, 1350), (99, 99, 99)).save(rogue)
        self.addCleanup(lambda: rogue.unlink(missing_ok=True))
        import hashlib as _hashlib

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["media"] = [{
            "local_path": f"social/drafts/production/{rogue.name}",
            "sha256": _hashlib.sha256(rogue.read_bytes()).hexdigest(),
            "content_type": "image/png",
            "width": 1080,
            "height": 1350,
            "image_format": "PNG",
        }]
        swapped = manifest_path.with_name(manifest_path.stem + "-swap.json")
        swapped.write_text(json.dumps(data), encoding="utf-8")
        self.addCleanup(lambda: swapped.unlink(missing_ok=True))
        code, out = self._execute_blocked(swapped)
        self.assertEqual(code, 2)
        self.assertIn("PACKAGING", out)

    def test_story_manifest_skips_packaging_gate(self):
        bridge = _load_hyphenated("nullone_draft_bridge_cli", "nullone-draft-bridge.py")

        self.assertIsNone(
            bridge.require_packaging_authority(Path("/tmp/x.json"), {"format": "STORY"})
        )


class BridgeCanonicalManifestTests(unittest.TestCase):
    """Blocker A regressions: canonical MANIFEST_DIR + exact ordered media binding."""

    def _build_feed_manifest(self, candidate_id=None, tag=None):
        manifest = _load_hyphenated("nullone_manifest_cli_canonical", "nullone-manifest.py")
        from PIL import Image

        tag = tag or uuid.uuid4().hex
        candidate_id = candidate_id or f"probe-{tag}"
        caption = DRAFTS_PROD / f"test-{tag}-caption.txt"
        image = DRAFTS_PROD / f"test-{tag}.png"
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        for p in (caption, image):
            self.addCleanup(lambda p=p: p.unlink(missing_ok=True))
        caption.write_text("Probe caption.", encoding="utf-8")
        Image.new("RGB", (1080, 1350), (14, 14, 15)).save(image)
        receipt = _receipt_for(_request(), candidate_id=candidate_id)
        receipt_path = DRAFTS_PROD / f"{candidate_id}-packaging-decision.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.addCleanup(lambda: receipt_path.unlink(missing_ok=True))
        record = receipt_mod.build_render_record(
            candidate_id=candidate_id, receipt_hash=receipt["receipt_hash"],
            format_decision="SINGLE_POST", asset_kind="NONE",
            outputs=[{"path": f"social/drafts/production/{image.name}",
                      "sha256": __import__("hashlib").sha256(image.read_bytes()).hexdigest()}],
        )
        record_path = DRAFTS_PROD / f"{candidate_id}-render-record.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.addCleanup(lambda: record_path.unlink(missing_ok=True))
        from nullone_bridge_common import MANIFEST_DIR

        args = argparse.Namespace(
            candidate_id=candidate_id, topic="Probe", topic_cluster="probe",
            content_type="NEWS", format="FEED",
            caption_file=f"social/drafts/production/{caption.name}",
            media=[f"social/drafts/production/{image.name}"],
            manifest_id=f"test-{tag}", output=None, force=False,
            packaging_receipt=str(receipt_path), render_record=str(record_path),
        )
        self.assertEqual(manifest.build(args), 0)
        manifest_path = MANIFEST_DIR / f"test-{tag}.json"
        self.addCleanup(lambda: manifest_path.unlink(missing_ok=True))
        return manifest_path

    def _build_carousel_manifest(self, tag=None):
        manifest = _load_hyphenated("nullone_manifest_cli_carousel", "nullone-manifest.py")
        from PIL import Image

        tag = tag or uuid.uuid4().hex
        candidate_id = f"carousel-{tag}"
        slides = []
        for i in range(6):
            slide = DRAFTS_PROD / f"test-{tag}-s{i:02d}.png"
            Image.new("RGB", (1080, 1350), (14, 14, 15)).save(slide)
            self.addCleanup(lambda p=slide: p.unlink(missing_ok=True))
            slides.append(f"social/drafts/production/{slide.name}")
        caption = DRAFTS_PROD / f"test-{tag}-caption.txt"
        caption.write_text("Probe caption.", encoding="utf-8")
        self.addCleanup(lambda: caption.unlink(missing_ok=True))
        request = _request(
            **{
                "candidate.content_shape": "MULTI_STEP_EXPLAINER",
                "candidate.distinct_beat_count": 4,
                "candidate.content_type": "EXPLAINER",
            }
        )
        receipt = _receipt_for(request, candidate_id=candidate_id)
        self.assertEqual(receipt["FORMAT_DECISION"], "CAROUSEL")
        receipt_path = DRAFTS_PROD / f"{candidate_id}-packaging-decision.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.addCleanup(lambda: receipt_path.unlink(missing_ok=True))
        import hashlib as _hashlib

        record = receipt_mod.build_render_record(
            candidate_id=candidate_id, receipt_hash=receipt["receipt_hash"],
            format_decision="CAROUSEL", asset_kind="NONE",
            outputs=[
                {"path": rel, "sha256": _hashlib.sha256((DRAFTS_PROD / Path(rel).name).read_bytes()).hexdigest()}
                for rel in slides
            ],
        )
        record_path = DRAFTS_PROD / f"{candidate_id}-render-record.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.addCleanup(lambda: record_path.unlink(missing_ok=True))
        from nullone_bridge_common import MANIFEST_DIR

        args = argparse.Namespace(
            candidate_id=candidate_id, topic="Probe", topic_cluster="probe",
            content_type="EXPLAINER", format="CAROUSEL",
            caption_file=f"social/drafts/production/{caption.name}",
            media=list(slides),
            manifest_id=f"test-{tag}", output=None, force=False,
            packaging_receipt=str(receipt_path), render_record=str(record_path),
        )
        self.assertEqual(manifest.build(args), 0)
        manifest_path = MANIFEST_DIR / f"test-{tag}.json"
        self.addCleanup(lambda: manifest_path.unlink(missing_ok=True))
        return manifest_path

    def _execute_blocked(self, manifest_path):
        import contextlib
        import io

        bridge = _load_hyphenated("nullone_draft_bridge_canonical", "nullone-draft-bridge.py")
        calls: list = []

        def _forbidden():
            calls.append(1)
            raise AssertionError("provider must not be reached")

        with mock.patch.object(bridge, "build_production_draft_provider", side_effect=lambda: _forbidden()):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = bridge.execute(
                    str(manifest_path.relative_to(WORKSPACE))
                    if manifest_path.is_absolute() else str(manifest_path)
                )
        return code, buf.getvalue(), calls

    def test_canonical_manifest_passes_offline_gate(self):
        bridge = _load_hyphenated("nullone_draft_bridge_canonical2", "nullone-draft-bridge.py")
        manifest_path = self._build_feed_manifest()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIsNone(bridge.require_packaging_authority(manifest_path, data))

    def test_manual_valid_packaging_manifest_in_production_dir_blocked(self):
        manifest_path = self._build_feed_manifest()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        tag = uuid.uuid4().hex
        manual = DRAFTS_PROD / f"test-{tag}-manual.json"
        manual.write_text(json.dumps(data), encoding="utf-8")
        self.addCleanup(lambda: manual.unlink(missing_ok=True))
        code, out, calls = self._execute_blocked(manual)
        self.assertEqual(code, 2)
        self.assertIn("PACKAGING", out)
        self.assertEqual(calls, [])

    def test_carousel_subset_blocked(self):
        from nullone_bridge_common import MANIFEST_DIR

        manifest_path = self._build_carousel_manifest()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["media"]), 6)
        data["media"] = data["media"][:2]
        tag = uuid.uuid4().hex
        subset = MANIFEST_DIR / f"test-{tag}-subset.json"
        subset.write_text(json.dumps(data), encoding="utf-8")
        self.addCleanup(lambda: subset.unlink(missing_ok=True))
        code, out, calls = self._execute_blocked(subset)
        self.assertEqual(code, 2)
        self.assertIn("PACKAGING", out)
        self.assertEqual(calls, [])

    def test_carousel_reorder_blocked(self):
        from nullone_bridge_common import MANIFEST_DIR

        manifest_path = self._build_carousel_manifest()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        media = list(data["media"])
        media[0], media[1] = media[1], media[0]
        data["media"] = media
        tag = uuid.uuid4().hex
        reordered = MANIFEST_DIR / f"test-{tag}-reordered.json"
        reordered.write_text(json.dumps(data), encoding="utf-8")
        self.addCleanup(lambda: reordered.unlink(missing_ok=True))
        code, out, calls = self._execute_blocked(reordered)
        self.assertEqual(code, 2)
        self.assertIn("PACKAGING", out)
        self.assertEqual(calls, [])

    def test_duplicate_manifest_media_blocked(self):
        from nullone_bridge_common import MANIFEST_DIR

        manifest_path = self._build_carousel_manifest()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        media = list(data["media"])
        media[-1] = dict(media[0])
        data["media"] = media
        tag = uuid.uuid4().hex
        dup = MANIFEST_DIR / f"test-{tag}-dup.json"
        dup.write_text(json.dumps(data), encoding="utf-8")
        self.addCleanup(lambda: dup.unlink(missing_ok=True))
        code, out, calls = self._execute_blocked(dup)
        self.assertEqual(code, 2)
        self.assertIn("PACKAGING", out)
        self.assertEqual(calls, [])

    def test_symlink_manifest_blocked(self):
        from nullone_bridge_common import MANIFEST_DIR

        manifest_path = self._build_feed_manifest()
        tag = uuid.uuid4().hex
        link = MANIFEST_DIR / f"test-{tag}-link.json"
        try:
            link.symlink_to(manifest_path)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        code, out, calls = self._execute_blocked(link)
        self.assertEqual(code, 2)
        self.assertIn("PACKAGING", out)
        self.assertEqual(calls, [])


class CarouselDatavizFailClosedTests(unittest.TestCase):
    """Blocker B regressions: carousel DATA_VISUALIZATION fails closed pre-subprocess."""

    def _args(self, receipt_path, asset_file=None, output=None, **overrides):
        args = argparse.Namespace(
            receipt=str(receipt_path),
            asset_file=str(asset_file) if asset_file is not None else "",
            output=output or "",
            spec=None,
            source=None,
            kicker=None,
            headline=None,
            stat=None,
            source_name=None,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _receipt_file(self, root, receipt, candidate_id="probe-candidate"):
        subdir = root / "social/drafts/production"
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / f"{candidate_id}-packaging-decision.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        return path

    def test_carousel_dataviz_fails_closed_before_subprocess(self):
        calls: list = []
        request = _request(
            **{
                "candidate.content_shape": "MULTI_STEP_EXPLAINER",
                "candidate.distinct_beat_count": 4,
                "candidate.content_type": "EXPLAINER",
                "assets.data_visualization_possible": True,
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(request)
            self.assertEqual(receipt["FORMAT_DECISION"], "CAROUSEL")
            self.assertEqual(receipt["VISUAL_STYLE"], "DATA_VISUALIZATION")
            receipt_path = self._receipt_file(root, receipt)
            data = root / "data.json"
            data.write_text('{"n": 1}', encoding="utf-8")
            descriptor = _asset_descriptor(
                asset_kind="DATA_VISUALIZATION", local_path=str(data),
                provenance="Verified dataset",
            )
            asset_path = root / "asset.json"
            asset_path.write_text(json.dumps(descriptor), encoding="utf-8")
            spec = root / "spec.json"
            spec.write_text(json.dumps({"slides": [{}, {}, {}, {}, {}, {}]}), encoding="utf-8")
            with mock.patch.object(
                dispatcher.subprocess, "run", side_effect=lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a[0], 0, "", "")
            ):
                with self.assertRaises(BridgeError) as ctx:
                    dispatcher.render_command(
                        self._args(receipt_path, asset_file=str(asset_path), spec=str(spec), output=str(root / "o")),
                        root=root,
                    )
        self.assertIn("PACKAGING_UNSUPPORTED_STYLE", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_typography_carousel_still_renders(self):
        calls: list = []
        request = _request(
            **{
                "candidate.content_shape": "MULTI_STEP_EXPLAINER",
                "candidate.distinct_beat_count": 4,
                "candidate.content_type": "EXPLAINER",
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = _receipt_for(request)
            self.assertEqual(receipt["VISUAL_STYLE"], "EDITORIAL_TYPOGRAPHY")
            receipt_path = self._receipt_file(root, receipt)
            descriptor = _asset_descriptor(asset_kind="NONE")
            asset_path = root / "asset.json"
            asset_path.write_text(json.dumps(descriptor), encoding="utf-8")
            spec = root / "spec.json"
            spec.write_text(json.dumps({"slides": [{}, {}, {}, {}, {}, {}]}), encoding="utf-8")
            out_dir = root / "slides"

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out_dir.mkdir(parents=True, exist_ok=True)
                for i in range(1, 7):
                    (out_dir / f"{i:02d}.png").write_text("png", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "SLIDES=6", "")

            with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
                self.assertEqual(
                    dispatcher.render_command(
                        self._args(receipt_path, asset_file=str(asset_path), spec=str(spec), output=str(out_dir)), root=root
                    ),
                    0,
                )
        self.assertEqual(len(calls), 1)


class NoPublicationCapabilityTests(unittest.TestCase):
    NEW_FILES = (        SCRIPTS / "nullone_packaging_receipt.py",
        SCRIPTS / "nullone-packaging-evaluator.py",
        SCRIPTS / "nullone-packaging-render.py",
    )

    def test_no_publication_or_transport_tokens(self):
        for path in self.NEW_FILES:
            source = path.read_text(encoding="utf-8").lower()
            for token in (
                "publish",
                "zernio",
                "telegram",
                "mcp",
                "sessions_send",
                "subprocess",
                "requests.",
                "urllib",
                "http.client",
                "openclaw",
            ):
                if path.name == "nullone-packaging-render.py" and token == "subprocess":
                    continue
                self.assertNotIn(token, source, msg=f"{path.name} must not reference {token!r}")

    def test_manifest_gate_adds_no_publish_path(self):
        source = (SCRIPTS / "nullone-manifest.py").read_text(encoding="utf-8")
        self.assertIn("--packaging-receipt", source)
        self.assertIn("PACKAGING_DECISION_MISMATCH", source)

    def test_agent_boundary_equal_or_narrower(self):
        agent = (ROOT / "workspace/.opencode/agents/nullone-draft-factory.md").read_text(encoding="utf-8")
        head = agent.split("---", 2)[1]
        self.assertNotIn("render_texbrif_v2.py *", head)
        self.assertNotIn("render_carousel_v2.py *", head)
        self.assertNotIn("render_story_v2.py *", head)
        self.assertIn("nullone-packaging-evaluator.py evaluate *", head)
        self.assertIn("nullone-packaging-render.py render *", head)
        self.assertIn("nullone-manifest.py build *", head)


class RendererSemanticIntegrationTests(unittest.TestCase):
    """Issue #135 x packaging authority (P0 forensic gap).

    A CAROUSEL receipt plus a spec carrying the exact forensic defect
    (type=comparison, left={} / right={}) must fail through the REAL
    renderer subprocess: no render record, no manifest, no draft
    bridge call, no Telegram/Zernio call. A fully valid spec on the
    same receipt path must still render and record successfully.
    """

    RENDERER = ROOT / "workspace/social/tools/render_carousel_v2.py"

    def _slides(self, comparison):
        return [
            {"type": "cover", "kicker": "AÇIQ MODEL", "headline": "Başlıq",
             "stat": "552B", "source": "DeepSeek"},
            {"type": "explainer", "label": "ARXİTEKTURA", "title": "Az parametr",
             "body": "8B aktiv parametr.", "stat": "8B", "source": "DeepSeek"},
            {"type": "stat", "label": "SƏMƏRƏLİLİK", "stat": "1/4 HBM",
             "title": "Yaddaş azaldı", "body": "KV-cache azaldı.", "source": "DeepSeek"},
            comparison,
            {"type": "limitation", "label": "MƏHDUDİYYƏT", "title": "Diqqət",
             "body": "Qiymətlər dəyişir.", "source": "DeepSeek"},
            {"type": "final", "title": "Niyə vacibdir?", "body": "Büdcə azalır."},
        ]

    def _forensic_comparison(self):
        return {
            "type": "comparison", "label": "KEÇİD", "title": "V4-Pro təqaüdə çıxır",
            "body": "Sorğular yeni modelə yönlənir.",
            "source": "DeepSeek, 10–14 sentyabr 2026",
            "left": {}, "right": {},
        }

    def _valid_comparison(self):
        slide = self._forensic_comparison()
        slide["left"] = {"label": "ƏVVƏL", "value": "V4-Pro"}
        slide["right"] = {"label": "İNDİ", "value": "V4.1-Flash"}
        return slide

    def _carousel_receipt(self, root, candidate_id="semi-candidate"):
        request = _request(
            **{
                "candidate.content_shape": "MULTI_STEP_EXPLAINER",
                "candidate.distinct_beat_count": 4,
                "candidate.content_type": "EXPLAINER",
            }
        )
        receipt = receipt_mod.evaluate_request(candidate_id, request)
        self.assertEqual(receipt["FORMAT_DECISION"], "CAROUSEL")
        self.assertEqual(receipt["slide_count_recommendation"], 6)
        subdir = root / "social/drafts/production"
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / f"{candidate_id}-packaging-decision.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        asset = _asset_descriptor(candidate_id=candidate_id, asset_kind="NONE")
        asset_path = root / "asset.json"
        asset_path.write_text(json.dumps(asset), encoding="utf-8")
        return receipt, path, asset_path

    def _args(self, receipt_path, asset_path, output, spec):
        return argparse.Namespace(
            receipt=str(receipt_path), asset_file=str(asset_path),
            output=str(output), spec=str(spec), source=None, kicker=None,
            headline=None, stat=None, source_name=None,
        )

    def test_forensic_empty_comparison_fails_through_dispatcher(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt, receipt_path, asset_path = self._carousel_receipt(root)
            spec_path = root / "spec.json"
            spec_path.write_text(
                json.dumps({"slides": self._slides(self._forensic_comparison())}),
                encoding="utf-8",
            )
            out_dir = root / "slides"
            with self.assertRaises(BridgeError) as ctx:
                dispatcher.render_command(
                    self._args(receipt_path, asset_path, out_dir, spec_path), root=root
                )
            self.assertIn("PACKAGING_RENDER_FAILED", str(ctx.exception))
            # No render record marked successful.
            self.assertFalse(
                (root / "social/drafts/production/semi-candidate-render-record.json").exists()
            )
            # No production-ready slide set.
            self.assertEqual(list(out_dir.glob("*.png")) if out_dir.is_dir() else [], [])
            # The failure is the #136 semantic validator, not anything else.
            cp = subprocess.run(
                [sys.executable, str(self.RENDERER), "--spec", str(spec_path),
                 "--output-dir", str(root / "direct")],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("EMPTY_COMPARISON_CONTENT", cp.stderr)
            # No manifest can be built without a render record.
            manifest = _load_hyphenated("nullone_manifest_cli_semi", "nullone-manifest.py")
            margs = argparse.Namespace(
                candidate_id="semi-candidate", topic="Probe", topic_cluster="probe",
                content_type="EXPLAINER", format="CAROUSEL",
                caption_file="x.txt", media=[], manifest_id="semi-probe",
                output=str(root / "m.json"), force=False,
                packaging_receipt=str(receipt_path), render_record=str(root / "missing.json"),
            )
            with self.assertRaises(BridgeError):
                manifest.build(margs)
            self.assertFalse((root / "m.json").exists())
            # No draft bridge call is reachable without a manifest.
            bridge = _load_hyphenated("nullone_draft_bridge_semi", "nullone-draft-bridge.py")
            with self.assertRaises(BridgeError):
                bridge.require_packaging_authority(
                    root / "m.json", {"format": "CAROUSEL", "candidate_id": "semi-candidate"}
                )

    def test_valid_comparison_renders_and_records(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt, receipt_path, asset_path = self._carousel_receipt(root)
            spec_path = root / "spec.json"
            spec_path.write_text(
                json.dumps({"slides": self._slides(self._valid_comparison())}),
                encoding="utf-8",
            )
            out_dir = root / "slides"
            self.assertEqual(
                dispatcher.render_command(
                    self._args(receipt_path, asset_path, out_dir, spec_path), root=root
                ),
                0,
            )
            slides = sorted(out_dir.glob("*.png"))
            self.assertEqual(len(slides), 6)
            record_path = root / "social/drafts/production/semi-candidate-render-record.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["receipt_hash"], receipt["receipt_hash"])
            self.assertEqual(len(record["outputs"]), 6)


class VisualGroundingTests(unittest.TestCase):
    """Issue #138 (E2E e2e-20260915-1925-controlled).

    The V2 feed renderer always emits a hero visual region, so a
    SINGLE_POST backed by no evidence renders an empty decorative
    frame. A declared SOURCE_GROUNDED requirement with no usable
    evidence must fail closed before receipt/render/manifest/draft/
    Telegram; explicit NONE (typography-only) stays valid.
    """

    def _grounded_request(self, **asset_overrides):
        request = _request()
        request["candidate"]["visual_requirement"] = "SOURCE_GROUNDED"
        for key, value in asset_overrides.items():
            request["assets"][key] = value
        return request

    def _receipt_file(self, root, receipt, candidate_id="ground-candidate"):
        subdir = root / "social/drafts/production"
        subdir.mkdir(parents=True, exist_ok=True)
        path = subdir / f"{candidate_id}-packaging-decision.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        return path

    def _args(self, receipt_path, asset_file, output, **overrides):
        args = argparse.Namespace(
            receipt=str(receipt_path), asset_file=str(asset_file), output=str(output),
            spec=None, source=None, kicker="k", headline="h", stat=None,
            source_name="n",
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_undeclared_requirement_rejected(self):
        request = _request()
        del request["candidate"]["visual_requirement"]
        with self.assertRaises(BridgeError) as ctx:
            receipt_mod.evaluate_request("ground-candidate", request)
        self.assertIn("visual_requirement", str(ctx.exception))

    def test_invalid_requirement_rejected(self):
        request = _request()
        request["candidate"]["visual_requirement"] = "MAYBE"
        with self.assertRaises(BridgeError) as ctx:
            receipt_mod.evaluate_request("ground-candidate", request)
        self.assertIn("visual_requirement", str(ctx.exception))

    def test_grounded_without_evidence_fails_before_receipt(self):
        with self.assertRaises(BridgeError) as ctx:
            receipt_mod.evaluate_request("ground-candidate", self._grounded_request())
        self.assertIn("PACKAGING_VISUAL_GROUNDING_UNMET", str(ctx.exception))

    def test_grounded_requirement_bound_into_receipt_hash(self):
        request = self._grounded_request(
            has_usable_screenshot=True, image_on_topic=True,
        )
        receipt = receipt_mod.evaluate_request("ground-candidate", request)
        self.assertEqual(receipt["POST_DECISION"], "POST")
        self.assertEqual(receipt["FORMAT_DECISION"], "SINGLE_POST")
        self.assertEqual(receipt["VISUAL_STYLE"], "SOURCE_SCREENSHOT")
        self.assertEqual(receipt["visual_requirement"], "SOURCE_GROUNDED")
        tampered = dict(receipt)
        tampered["visual_requirement"] = "NONE"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "r.json"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(BridgeError) as ctx:
                receipt_mod.load_receipt(path, root=root)
            self.assertIn("TAMPERED", str(ctx.exception))

    def test_dispatcher_refuses_grounded_none_asset_without_subprocess(self):
        calls: list = []
        decision = {
            "POST_DECISION": "POST", "CONTENT_SHAPE": "SINGLE_FACT",
            "REAL_PHOTO_AVAILABLE": "NO", "REAL_PHOTO_REQUIRED": "NO",
            "VISUAL_EVIDENCE_REQUIRED": "NO", "ASSET_STRENGTH": "NONE",
            "TEXT_DENSITY": "LOW", "TIMELINESS": "TODAY",
            "FORMAT_DECISION": "SINGLE_POST", "FORMAT_REASON": "SINGLE_FACT_FITS_SINGLE_POST",
            "VISUAL_STYLE": "EDITORIAL_TYPOGRAPHY", "slide_count_recommendation": None,
            "source_grounding": "STRONG_PRIMARY", "distinct_beat_count": 1,
            "content_type": "NEWS",
        }
        validated = {"candidate": {"visual_requirement": "SOURCE_GROUNDED"}, "assets": {}}
        receipt = receipt_mod.build_receipt_body(
            candidate_id="ground-candidate", validated_request=validated, decision=decision,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt_path = self._receipt_file(root, receipt)
            asset_path = root / "asset.json"
            asset_path.write_text(json.dumps(_asset_descriptor(
                candidate_id="ground-candidate", asset_kind="NONE",
            )), encoding="utf-8")
            with mock.patch.object(
                dispatcher.subprocess, "run", side_effect=AssertionError("must not render")
            ):
                with self.assertRaises(BridgeError) as ctx:
                    dispatcher.render_command(
                        self._args(receipt_path, asset_path, root / "o.png"), root=root
                    )
        self.assertIn("PACKAGING_VISUAL_GROUNDING_UNMET", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_manifest_refuses_grounded_none_record(self):
        manifest = _load_hyphenated("nullone_manifest_cli_ground", "nullone-manifest.py")
        tag = uuid.uuid4().hex
        candidate_id = f"ground-{tag}"
        request = self._grounded_request(
            has_usable_screenshot=True, image_on_topic=True,
        )
        receipt = receipt_mod.evaluate_request(candidate_id, request)
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        receipt_path = DRAFTS_PROD / f"{candidate_id}-packaging-decision.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.addCleanup(lambda: receipt_path.unlink(missing_ok=True))
        record = receipt_mod.build_render_record(
            candidate_id=candidate_id, receipt_hash=receipt["receipt_hash"],
            format_decision="SINGLE_POST", asset_kind="NONE",
            outputs=[{"path": "social/drafts/production/x.png", "sha256": "y"}],
        )
        record_path = DRAFTS_PROD / f"{candidate_id}-render-record.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.addCleanup(lambda: record_path.unlink(missing_ok=True))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            args = argparse.Namespace(
                candidate_id=candidate_id, topic="Probe", topic_cluster="probe",
                content_type="NEWS", format="FEED", caption_file="x.txt", media=[],
                manifest_id=f"ground-{tag}", output=str(root / "m.json"), force=False,
                packaging_receipt=str(receipt_path), render_record=str(record_path),
            )
            with self.assertRaises(BridgeError) as ctx:
                manifest.build(args)
            self.assertIn("PACKAGING_VISUAL_GROUNDING_UNMET", str(ctx.exception))
            self.assertFalse((root / "m.json").exists())

    def test_grounded_screenshot_single_post_passes(self):
        calls: list = []
        request = self._grounded_request(
            has_usable_screenshot=True, image_on_topic=True,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = receipt_mod.evaluate_request("ground-candidate", request)
            self.assertEqual(receipt["VISUAL_STYLE"], "SOURCE_SCREENSHOT")
            receipt_path = self._receipt_file(root, receipt)
            shot = root / "shot.png"
            shot.write_bytes(b"shot-bytes")
            asset_path = root / "asset.json"
            asset_path.write_text(json.dumps(_asset_descriptor(
                candidate_id="ground-candidate", asset_kind="SOURCE_SCREENSHOT",
                local_path=str(shot), provenance="Primary source screenshot",
            )), encoding="utf-8")
            out = root / "feed.png"

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out.write_text("png", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "VALID=true", "")

            with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
                self.assertEqual(
                    dispatcher.render_command(
                        self._args(receipt_path, asset_path, out), root=root
                    ),
                    0,
                )
        self.assertEqual(len(calls), 1)
        self.assertIn(str(shot), calls[0])

    def test_grounded_dataviz_single_post_passes(self):
        calls: list = []
        request = self._grounded_request(data_visualization_possible=True)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = receipt_mod.evaluate_request("ground-candidate", request)
            self.assertEqual(receipt["VISUAL_STYLE"], "DATA_VISUALIZATION")
            receipt_path = self._receipt_file(root, receipt)
            data = root / "data.json"
            data.write_text('{"n": 1}', encoding="utf-8")
            asset_path = root / "asset.json"
            asset_path.write_text(json.dumps(_asset_descriptor(
                candidate_id="ground-candidate", asset_kind="DATA_VISUALIZATION",
                local_path=str(data), provenance="Verified dataset",
            )), encoding="utf-8")
            out = root / "feed.png"

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out.write_text("png", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "VALID=true", "")

            with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
                self.assertEqual(
                    dispatcher.render_command(
                        self._args(receipt_path, asset_path, out), root=root
                    ),
                    0,
                )
        self.assertEqual(len(calls), 1)

    def test_typography_none_single_post_passes(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt = receipt_mod.evaluate_request("ground-candidate", _request())
            self.assertEqual(receipt["POST_DECISION"], "POST")
            self.assertEqual(receipt["visual_requirement"], "NONE")
            receipt_path = self._receipt_file(root, receipt)
            asset_path = root / "asset.json"
            asset_path.write_text(json.dumps(_asset_descriptor(
                candidate_id="ground-candidate", asset_kind="NONE",
            )), encoding="utf-8")
            out = root / "feed.png"

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out.write_text("png", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "VALID=true", "")

            with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
                self.assertEqual(
                    dispatcher.render_command(
                        self._args(receipt_path, asset_path, out), root=root
                    ),
                    0,
                )
        self.assertEqual(len(calls), 1)

    def test_grandfathered_fieldless_receipt_loads_as_none(self):
        receipt = receipt_mod.evaluate_request("ground-candidate", _request())
        body = {k: v for k, v in receipt.items() if k not in ("receipt_hash", "visual_requirement")}
        import hashlib as _hashlib

        fieldless = dict(body)
        fieldless["receipt_hash"] = _hashlib.sha256(
            receipt_mod.canonical_json_bytes(body)
        ).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "old.json"
            path.write_text(json.dumps(fieldless), encoding="utf-8")
            loaded = receipt_mod.load_receipt(path, root=root)
            self.assertEqual(loaded["visual_requirement"], "NONE")

    def test_zero_side_effects_on_grounding_rejection(self):
        import contextlib
        import io

        from nullone_bridge_common import MANIFEST_DIR

        bridge = _load_hyphenated("nullone_draft_bridge_ground", "nullone-draft-bridge.py")
        from PIL import Image

        tag = uuid.uuid4().hex
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        caption = DRAFTS_PROD / f"test-{tag}-caption.txt"
        image = DRAFTS_PROD / f"test-{tag}.png"
        caption.write_text("Probe caption.", encoding="utf-8")
        Image.new("RGB", (1080, 1350), (14, 14, 15)).save(image)
        for p in (caption, image):
            self.addCleanup(lambda p=p: p.unlink(missing_ok=True))
        hand = MANIFEST_DIR / f"test-{tag}-hand.json"
        hand.write_text(json.dumps({
            "schema": "nullone.production.v1",
            "manifest_id": f"test-{tag}",
            "candidate_id": "ground-candidate",
            "topic": "Probe",
            "topic_cluster": "probe",
            "content_type": "NEWS",
            "format": "FEED",
            "verification": "PASS",
            "account_id": "6a982bbf77555aae01c28f21",
            "caption": {
                "file": f"social/drafts/production/{caption.name}",
                "sha256": __import__("hashlib").sha256(caption.read_bytes()).hexdigest(),
            },
            "media": [{
                "local_path": f"social/drafts/production/{image.name}",
                "sha256": __import__("hashlib").sha256(image.read_bytes()).hexdigest(),
                "content_type": "image/png",
                "width": 1080,
                "height": 1350,
                "image_format": "PNG",
            }],
            "review": {"create_attempts": 0, "state": "NOT_CREATED", "zernio_draft_id": None},
            "publication": {"state": "NOT_REQUESTED", "attempts": 0},
        }), encoding="utf-8")
        self.addCleanup(lambda: hand.unlink(missing_ok=True))
        with mock.patch.object(
            bridge, "build_production_draft_provider",
            side_effect=AssertionError("provider must not be reached"),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = bridge.execute(f"social/ops/manifests/{hand.name}")
        self.assertEqual(code, 2)
        self.assertIn("PACKAGING", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
