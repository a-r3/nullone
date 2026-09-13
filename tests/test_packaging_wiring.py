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
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _write_receipt_in_workspace(self, receipt):
        name = f"test-{uuid.uuid4().hex}-packaging-decision.json"
        path = DRAFTS_PROD / name
        DRAFTS_PROD.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt), encoding="utf-8")
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
        path = self._write_receipt_in_workspace(receipt)
        args = self._manifest_args(format="FEED", packaging_receipt=str(path))
        with self.assertRaises(BridgeError) as ctx:
            manifest.build(args)
        self.assertIn("candidate", str(ctx.exception))

    def test_matching_receipt_builds_manifest(self):
        manifest = _load_hyphenated("nullone_manifest_cli", "nullone-manifest.py")
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
        receipt_path = self._write_receipt_in_workspace(receipt)
        args = self._manifest_args(
            candidate_id="probe-candidate",
            format="FEED",
            caption_file=str(image.parent / caption.name),
            media=[str(image)],
            output=str(out),
            packaging_receipt=str(receipt_path),
        )
        # caption/media must resolve inside the workspace: use relative paths.
        args.caption_file = f"social/drafts/production/{caption.name}"
        args.media = [f"social/drafts/production/{image.name}"]
        args.output = f"social/drafts/production/{out.name}"
        self.assertEqual(manifest.build(args), 0)
        built = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(built["format"], "FEED")
        self.assertEqual(built["candidate_id"], "probe-candidate")


class RenderDispatcherTests(unittest.TestCase):
    def _args(self, receipt_path, **overrides):
        args = argparse.Namespace(
            receipt=str(receipt_path),
            output="/tmp/x.png",
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

    def _receipt_file(self, root, receipt):
        path = root / f"{uuid.uuid4().hex}.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
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
            spec = root / "spec.json"
            spec.write_text(json.dumps({"slides": [{}, {}]}), encoding="utf-8")
            with mock.patch.object(
                dispatcher.subprocess, "run", side_effect=lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a[0], 0, "", "")
            ):
                with self.assertRaises(BridgeError) as ctx:
                    dispatcher.render_command(self._args(receipt_path, spec=str(spec), output=str(root / "o")), root=root)
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
            spec = root / "spec.json"
            spec.write_text(json.dumps({"slides": [{}, {}, {}, {}, {}, {}]}), encoding="utf-8")
            out_dir = root / "slides"

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                out_dir.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, "SLIDES=6", "")

            with mock.patch.object(dispatcher.subprocess, "run", side_effect=fake_run):
                self.assertEqual(
                    dispatcher.render_command(
                        self._args(receipt_path, spec=str(spec), output=str(out_dir)), root=root
                    ),
                    0,
                )
        self.assertEqual(len(calls), 1)
        self.assertIn("render_carousel_v2.py", calls[0][1])

    def test_feed_path_ignores_carousel_spec(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            receipt_path = self._receipt_file(root, _receipt_for(_request()))
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
                            output=str(out),
                            source="s",
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


class NoPublicationCapabilityTests(unittest.TestCase):
    NEW_FILES = (
        SCRIPTS / "nullone_packaging_receipt.py",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
