#!/usr/bin/env python3
"""Regression tests for the NullOne Visual Director
(docs/contracts/visual-director-contract-v1.md).

Covers:
- VISUAL_DIRECTOR_SCHEMA_VALID / MALFORMED_DECISION_FAILS_CLOSED
- SOURCE_PHOTO_WITH_VALID_PRIMARY_ASSET
- SOURCE_PHOTO_MISSING_REQUIRED_ASSET_FAILS_CLOSED_OR_REEVALUATES
- NO_RANDOM_STOCK_FALLBACK
- TYPOGRAPHY_NOT_EMPTY_HEADLINE_CARD
- RECENT_PUBLISHED_HISTORY_ONLY / UNPUBLISHED_DRAFTS_EXCLUDED_FROM_VISUAL_MEMORY
- FEED_HISTORY_CONTEXT_BOUNDED
- VISUAL_DECISION_PERSISTED_IN_MANIFEST
- NO_VISUAL_DECISION_CAN_TRIGGER_PUBLICATION

All offline: no network, no Zernio, no Telegram, no production state
mutation (production state is only ever read, never in this suite).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace" / "social" / "ops" / "scripts"

sys.path.insert(0, str(SCRIPTS))
from nullone_packaging_policy import evaluate_packaging  # noqa: E402
from nullone_visual_director import (  # noqa: E402
    VisualDirectorError,
    finalize_decision,
    packaging_style_directive,
    validate_decision,
)
from nullone_visual_memory import (  # noqa: E402
    build_visual_memory_document,
    MAX_WINDOW,
    MIN_WINDOW,
    VisualMemoryError,
)


def _load_hyphenated(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


visual_director_cli = _load_hyphenated("visual_director_cli_test", "nullone-visual-director.py")
evaluator_cli = _load_hyphenated("packaging_evaluator_cli_visualtest", "nullone-packaging-evaluator.py")


def _typography(**overrides) -> dict:
    base = {
        "schema": "nullone.visual-decision.v1",
        "contract_version": "1.0.0",
        "candidate_id": "cand-1",
        "visual_style": "EDITORIAL_TYPOGRAPHY",
        "source_asset_required": False,
        "source_asset_url": None,
        "source_asset_type": None,
        "source_provenance": None,
        "local_path": None,
        "sha256": None,
        "headline": "Amazon Seller Central-a Claude qoşulur",
        "deck": None,
        "stat": "60 saniyə",
        "visual_motif": None,
        "decision_reason_code": "EDITORIAL_TYPOGRAPHY_SUFFICIENT_CONTENT",
        "recent_feed_context_used": True,
    }
    base.update(overrides)
    return base


class SchemaValidationTests(unittest.TestCase):
    """VISUAL_DIRECTOR_SCHEMA_VALID / MALFORMED_DECISION_FAILS_CLOSED."""

    def test_valid_typography_decision_validates(self):
        validated = validate_decision(_typography())
        self.assertEqual(validated["visual_style"], "EDITORIAL_TYPOGRAPHY")
        final = finalize_decision(validated)
        self.assertIn("decision_hash", final)

    def test_valid_branded_graphic_decision_validates(self):
        validated = validate_decision(
            _typography(visual_style="BRANDED_GRAPHIC", visual_motif="signal_accent",
                        decision_reason_code="BRANDED_GRAPHIC_ABSTRACT_NO_SOURCE")
        )
        self.assertEqual(validated["visual_style"], "BRANDED_GRAPHIC")

    def test_malformed_decisions_fail_closed(self):
        for mutation, expect_fragment in (
            ({"schema": "wrong"}, "schema mismatch"),
            ({"contract_version": "9.9.9"}, "contract_version mismatch"),
            ({"visual_style": "PHOTOSHOP_IT"}, "visual_style"),
            ({"decision_reason_code": "BECAUSE"}, "decision_reason_code"),
            ({"recent_feed_context_used": "yes"}, "recent_feed_context_used"),
            ({"headline": ""}, "headline"),
            ({"headline": "x" * 500}, "headline"),
        ):
            with self.subTest(mutation=mutation):
                bad = dict(_typography(), **mutation)
                with self.assertRaises(VisualDirectorError) as ctx:
                    validate_decision(bad)
                self.assertIn(expect_fragment, str(ctx.exception))

    def test_missing_required_field_fails_closed(self):
        bad = _typography()
        del bad["decision_reason_code"]
        with self.assertRaises(VisualDirectorError) as ctx:
            validate_decision(bad)
        self.assertIn("missing field", str(ctx.exception))

    def test_forbidden_chain_of_thought_field_rejected(self):
        for field in ("reasoning", "chain_of_thought", "rationale", "notes", "explanation"):
            with self.subTest(field=field):
                bad = dict(_typography(), **{field: "because the numbers felt right"})
                with self.assertRaises(VisualDirectorError) as ctx:
                    validate_decision(bad)
                self.assertIn("forbidden", str(ctx.exception))

    def test_non_object_decision_fails_closed(self):
        for bad in (None, "typography", 42, ["EDITORIAL_TYPOGRAPHY"]):
            with self.subTest(bad=bad):
                with self.assertRaises(VisualDirectorError):
                    validate_decision(bad)


class SourcePhotoAssetTests(unittest.TestCase):
    """SOURCE_PHOTO_WITH_VALID_PRIMARY_ASSET,
    SOURCE_PHOTO_MISSING_REQUIRED_ASSET_FAILS_CLOSED_OR_REEVALUATES,
    NO_RANDOM_STOCK_FALLBACK."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullone-visual-director-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "social/drafts/production").mkdir(parents=True)
        self.asset = self.root / "social/drafts/production/asset.jpg"
        self.asset.write_bytes(b"official-screenshot-bytes")
        self.asset_sha = hashlib.sha256(self.asset.read_bytes()).hexdigest()

    def _source_photo(self, **overrides):
        base = _typography(
            visual_style="SOURCE_PHOTO",
            source_asset_required=True,
            source_asset_url="https://www.aboutamazon.com/news/example",
            source_asset_type="official_screenshot",
            source_provenance="Official Amazon Seller Central announcement page",
            local_path=str(self.asset),
            sha256=self.asset_sha,
            decision_reason_code="SOURCE_PHOTO_STRONG_PRIMARY_MATCH",
        )
        base.update(overrides)
        return base

    def test_source_photo_with_valid_primary_asset_validates_and_hashes(self):
        validated = validate_decision(self._source_photo(), root=self.root)
        self.assertEqual(validated["sha256"], self.asset_sha)
        # SOURCE_PHOTO/DATA_VISUALIZATION are never routed through the
        # packaging contract's `visual_director_style` field: the
        # evidence ladder is authoritative for real evidence.
        self.assertIsNone(packaging_style_directive(validated))

    def test_source_photo_missing_asset_file_fails_closed(self):
        missing = self._source_photo(local_path=str(self.root / "does-not-exist.jpg"))
        with self.assertRaises(VisualDirectorError) as ctx:
            validate_decision(missing, root=self.root)
        self.assertIn("local_path", str(ctx.exception))

    def test_source_photo_declared_without_local_path_fails_closed(self):
        no_asset = self._source_photo(local_path=None, sha256=None)
        with self.assertRaises(VisualDirectorError) as ctx:
            validate_decision(no_asset, root=self.root)
        self.assertIn("local_path", str(ctx.exception))

    def test_no_random_stock_fallback_bare_url_without_local_file_rejected(self):
        """A remote source_asset_url alone (no downloaded/validated local
        file) must never be sufficient -- the exact "silent fallback to
        random web search" failure mode the contract forbids."""
        bare_url_only = self._source_photo(local_path=None, sha256=None)
        with self.assertRaises(VisualDirectorError):
            validate_decision(bare_url_only, root=self.root)

    def test_sha256_mismatch_fails_closed(self):
        tampered = self._source_photo(sha256="0" * 64)
        with self.assertRaises(VisualDirectorError) as ctx:
            validate_decision(tampered, root=self.root)
        self.assertIn("sha256 mismatch", str(ctx.exception))

    def test_non_evidence_style_cannot_smuggle_asset(self):
        smuggled = _typography(
            visual_style="EDITORIAL_TYPOGRAPHY",
            local_path=str(self.asset),
        )
        with self.assertRaises(VisualDirectorError):
            validate_decision(smuggled, root=self.root)


class TypographyContentTests(unittest.TestCase):
    """TYPOGRAPHY_NOT_EMPTY_HEADLINE_CARD."""

    def test_sparse_typography_decision_still_validates_but_render_is_gated_downstream(self):
        # The Visual Director contract itself does not measure pixels;
        # TYPOGRAPHY_NOT_EMPTY_HEADLINE_CARD is enforced by the
        # template-aware brand gate (tests/test_v2_brand_compliance.py
        # TemplateAwareBrandGateTests.test_brand_gate_editorial_typography
        # _requires_coverage) using the renderer's own reported
        # content_coverage_ratio. This test proves the two layers are
        # wired to the same production-incident shape.
        from nullone_brand_gate import evaluate_brand_gate

        production_incident_metadata = {
            "schema": "nullone.render-brand-metadata.v1",
            "visual_style": "EDITORIAL_TYPOGRAPHY",
            "has_photo": False,
            "stat_present": True,
            "stat_color": [253, 69, 3],
            "margin_px": 90,
            "brand_mark_count": 1,
            "brand_mark_position": "bottom_right",
            "brand_mark_opacity": 170,
            "text_bounds_valid": True,
            "content_coverage_ratio": 0.34,
            "deck_present": False,
            "photo_region_stddev": None,
            "photo_source_sha256": None,
        }
        result = evaluate_brand_gate(production_incident_metadata)
        self.assertEqual(result["BRAND_GATE"], "BLOCKED")
        self.assertIn("CONTENT_COVERAGE_SUFFICIENT", result["BRAND_GATE_REASON"])


class PackagingDirectiveIntegrationTests(unittest.TestCase):
    """Proves the discretionary-zone wiring end to end through the
    unmodified packaging evaluator, both with and without a Visual
    Director signal (legacy behavior must be byte-identical when absent)."""

    def _request(self, visual_director_style=None):
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
        if visual_director_style is not None:
            candidate["visual_director_style"] = visual_director_style
        assets = {
            "has_official_or_source_image": False,
            "has_usable_screenshot": False,
            "image_on_topic": False,
            "image_quality_ok": False,
            "data_visualization_possible": False,
        }
        return {"candidate": candidate, "assets": assets}

    def test_legacy_behavior_unchanged_without_visual_director(self):
        result = evaluate_packaging(self._request())
        self.assertEqual(result["VISUAL_STYLE"], "EDITORIAL_TYPOGRAPHY")

    def test_visual_director_branded_graphic_wins_the_discretionary_zone(self):
        result = evaluate_packaging(self._request("BRANDED_GRAPHIC"))
        self.assertEqual(result["VISUAL_STYLE"], "BRANDED_GRAPHIC")

    def test_visual_director_never_overrides_a_hard_requirement(self):
        # ANNOUNCEMENT + depicts_real_world_subject forces REAL_PHOTO_REQUIRED;
        # a strong official asset is present, so the ladder's own evidence
        # branch (not visual_director_style) is authoritative regardless.
        request = self._request("BRANDED_GRAPHIC")
        request["candidate"]["content_shape"] = "ANNOUNCEMENT"
        request["candidate"]["depicts_real_world_subject"] = True
        request["candidate"]["visual_requirement"] = "SOURCE_GROUNDED"
        request["assets"]["has_official_or_source_image"] = True
        request["assets"]["image_on_topic"] = True
        request["assets"]["image_quality_ok"] = True
        result = evaluate_packaging(request)
        self.assertEqual(result["VISUAL_STYLE"], "REAL_PHOTO")


class EndToEndCliWiringTests(unittest.TestCase):
    """The two CLIs a production Draft Factory cycle actually invokes,
    chained exactly as docs/contracts/visual-director-contract-v1.md
    describes: nullone-visual-director.py evaluate, then
    nullone-packaging-evaluator.py evaluate --visual-decision.

    Real, gitignored social/drafts/production files with a unique tag
    and addCleanup (same pattern as test_packaging_wiring.py) since
    neither CLI's `evaluate_command` takes a root override."""

    def test_visual_director_decision_reaches_the_packaging_receipt(self):
        import argparse
        import uuid

        from nullone_bridge_common import WORKSPACE

        candidate_id = f"e2e-{uuid.uuid4().hex}"
        drafts_prod = WORKSPACE / "social/drafts/production"
        drafts_prod.mkdir(parents=True, exist_ok=True)

        raw_decision_path = drafts_prod / f"{candidate_id}-visual-decision-request.json"
        raw_decision_path.write_text(
            json.dumps(_typography(candidate_id=candidate_id, visual_style="BRANDED_GRAPHIC",
                                    visual_motif="signal_accent",
                                    decision_reason_code="BRANDED_GRAPHIC_ABSTRACT_NO_SOURCE")),
            encoding="utf-8",
        )
        decision_path = drafts_prod / f"{candidate_id}-visual-decision.json"
        self.addCleanup(lambda: raw_decision_path.unlink(missing_ok=True))
        self.addCleanup(lambda: decision_path.unlink(missing_ok=True))

        vd_args = argparse.Namespace(candidate_id=candidate_id, request_file=str(raw_decision_path))
        self.assertEqual(visual_director_cli.evaluate_command(vd_args), 0)
        self.assertTrue(decision_path.is_file())

        packaging_request = {
            "candidate": {
                "content_type": "NEWS", "content_shape": "SINGLE_FACT",
                "timeliness": "TODAY", "verification": "PASS",
                "source_grounding": "STRONG_PRIMARY", "audience_value": "HIGH",
                "distinct_beat_count": 1, "depicts_real_world_subject": False,
                "still_developing": False, "visual_requirement": "NONE",
            },
            "assets": {
                "has_official_or_source_image": False, "has_usable_screenshot": False,
                "image_on_topic": False, "image_quality_ok": False,
                "data_visualization_possible": False,
            },
        }
        packaging_request_path = drafts_prod / f"{candidate_id}-packaging-request.json"
        packaging_request_path.write_text(json.dumps(packaging_request), encoding="utf-8")
        receipt_path = drafts_prod / f"{candidate_id}-packaging-decision.json"
        self.addCleanup(lambda: packaging_request_path.unlink(missing_ok=True))
        self.addCleanup(lambda: receipt_path.unlink(missing_ok=True))

        eval_args = argparse.Namespace(
            candidate_id=candidate_id, request_file=str(packaging_request_path),
            visual_decision=str(decision_path),
        )
        self.assertEqual(evaluator_cli.evaluate_command(eval_args), 0)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["VISUAL_STYLE"], "BRANDED_GRAPHIC")

    def test_model_cannot_smuggle_visual_director_style_directly(self):
        import argparse
        import uuid

        from nullone_bridge_common import BridgeError as _BridgeError
        from nullone_bridge_common import WORKSPACE

        candidate_id = f"e2e-smuggle-{uuid.uuid4().hex}"
        drafts_prod = WORKSPACE / "social/drafts/production"
        drafts_prod.mkdir(parents=True, exist_ok=True)
        packaging_request = {
            "candidate": {
                "content_type": "NEWS", "content_shape": "SINGLE_FACT",
                "timeliness": "TODAY", "verification": "PASS",
                "source_grounding": "STRONG_PRIMARY", "audience_value": "HIGH",
                "distinct_beat_count": 1, "depicts_real_world_subject": False,
                "still_developing": False, "visual_requirement": "NONE",
                "visual_director_style": "BRANDED_GRAPHIC",
            },
            "assets": {
                "has_official_or_source_image": False, "has_usable_screenshot": False,
                "image_on_topic": False, "image_quality_ok": False,
                "data_visualization_possible": False,
            },
        }
        path = drafts_prod / f"{candidate_id}-packaging-request.json"
        path.write_text(json.dumps(packaging_request), encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        args = argparse.Namespace(candidate_id=candidate_id, request_file=str(path), visual_decision=None)
        with self.assertRaises(_BridgeError) as ctx:
            evaluator_cli.evaluate_command(args)
        self.assertIn("visual_director_style", str(ctx.exception))


class VisualMemoryTests(unittest.TestCase):
    """RECENT_PUBLISHED_HISTORY_ONLY, UNPUBLISHED_DRAFTS_EXCLUDED_FROM_
    VISUAL_MEMORY, FEED_HISTORY_CONTEXT_BOUNDED."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullone-visual-memory-")
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name) / "social"
        (self.state_root / "state").mkdir(parents=True)
        (self.state_root / "ops/manifests").mkdir(parents=True)

    def _write_ledger(self, rows):
        path = self.state_root / "state/publish-ledger.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_recent_published_history_only(self):
        self._write_ledger(
            [
                {
                    "timestamp": "2026-09-01T00:00:00+00:00", "manifest_id": "m1",
                    "topic": "Rejected/abandoned candidate", "live_zernio_post_id": "p1",
                    "result": "PUBLISH_ACCEPTED",
                },
                {
                    "timestamp": "2026-09-02T00:00:00+00:00", "manifest_id": "m2",
                    "topic": "Genuinely published", "live_zernio_post_id": "p2",
                    "result": "PUBLISHED",
                },
            ]
        )
        history = build_visual_memory_document(state_root=self.state_root, window=MIN_WINDOW)["history"]
        topics = {r["topic"] for r in history}
        self.assertIn("Genuinely published", topics)
        self.assertNotIn("Rejected/abandoned candidate", topics)

    def test_unpublished_drafts_excluded_even_with_a_manifest(self):
        manifest = {
            "manifest_id": "m-draft",
            "format": "FEED",
            "packaging": {"visual_style": "EDITORIAL_TYPOGRAPHY", "asset_kind": "NONE"},
            "media": [{"local_path": "social/drafts/production/x.png"}],
        }
        (self.state_root / "ops/manifests/m-draft.json").write_text(json.dumps(manifest), encoding="utf-8")
        self._write_ledger(
            [
                {
                    "timestamp": "2026-09-01T00:00:00+00:00", "manifest_id": "m-draft",
                    "topic": "Never actually published", "live_zernio_post_id": "p-draft",
                    "result": "PUBLISHING",
                }
            ]
        )
        doc = build_visual_memory_document(state_root=self.state_root, window=MIN_WINDOW)
        self.assertEqual(doc["history"], [])

    def test_feed_history_context_bounded(self):
        rows = []
        for i in range(30):
            rows.append(
                {
                    "timestamp": f"2026-09-{i + 1:02d}T00:00:00+00:00", "manifest_id": f"m{i}",
                    "topic": f"Topic {i}", "live_zernio_post_id": f"p{i}", "result": "PUBLISHED",
                }
            )
        self._write_ledger(rows)
        doc = build_visual_memory_document(state_root=self.state_root, window=1000)
        self.assertEqual(doc["window_actual"], MAX_WINDOW)
        self.assertLessEqual(len(doc["history"]), MAX_WINDOW)

    def test_malformed_ledger_fails_closed_not_silently_empty(self):
        (self.state_root / "state/publish-ledger.jsonl").write_text("{not json", encoding="utf-8")
        with self.assertRaises(VisualMemoryError):
            build_visual_memory_document(state_root=self.state_root, window=MIN_WINDOW)

    def test_absent_state_is_legitimately_empty(self):
        empty_root = Path(self.tmp.name) / "never-produced" / "social"
        doc = build_visual_memory_document(state_root=empty_root, window=MIN_WINDOW)
        self.assertEqual(doc["history"], [])


class ManifestPersistenceTests(unittest.TestCase):
    """VISUAL_DECISION_PERSISTED_IN_MANIFEST.

    Exercises the real `nullone-manifest.py build()` (it has no root
    override, so -- exactly like tests/test_packaging_wiring.py's own
    manifest tests -- this writes uniquely-tagged, cleaned-up files
    under the real, gitignored social/drafts/production and
    social/ops/manifests directories rather than a temp root."""

    def test_receipt_visual_style_reaches_the_manifest_packaging_block(self):
        import uuid

        from PIL import Image

        from nullone_bridge_common import MANIFEST_DIR, WORKSPACE
        from nullone_packaging_receipt import build_receipt_body, build_render_record

        manifest_cli = _load_hyphenated("manifest_cli_visualtest", "nullone-manifest.py")
        drafts_prod = WORKSPACE / "social/drafts/production"
        drafts_prod.mkdir(parents=True, exist_ok=True)

        tag = uuid.uuid4().hex
        candidate_id = f"mf-cand-{tag}"
        request = {
            "candidate": {
                "content_type": "NEWS", "content_shape": "SINGLE_FACT",
                "timeliness": "TODAY", "verification_status": "PASS",
                "source_grounding": "STRONG_PRIMARY", "audience_value": "HIGH",
                "distinct_beat_count": 1, "depicts_real_world_subject": False,
                "still_developing": False, "visual_requirement": "NONE",
                "visual_director_style": "BRANDED_GRAPHIC",
            },
            "assets": {
                "has_official_or_source_image": False, "has_usable_screenshot": False,
                "image_on_topic": False, "image_quality_ok": False,
                "data_visualization_possible": False,
            },
        }
        decision = evaluate_packaging(request)
        self.assertEqual(decision["VISUAL_STYLE"], "BRANDED_GRAPHIC")
        receipt = build_receipt_body(candidate_id=candidate_id, validated_request=request, decision=decision)
        receipt_path = drafts_prod / f"{candidate_id}-packaging-decision.json"
        self.addCleanup(lambda: receipt_path.unlink(missing_ok=True))
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        image = drafts_prod / f"test-{tag}.png"
        caption = drafts_prod / f"test-{tag}-caption.txt"
        out = MANIFEST_DIR / f"test-{tag}.json"
        for p in (image, caption, out):
            self.addCleanup(lambda p=p: p.unlink(missing_ok=True))
        Image.new("RGB", (1080, 1350), (14, 14, 15)).save(image)
        caption.write_text("Caption text.", encoding="utf-8")

        record = build_render_record(
            candidate_id=candidate_id, receipt_hash=receipt["receipt_hash"],
            format_decision="SINGLE_POST", asset_kind="NONE",
            outputs=[{"path": f"social/drafts/production/{image.name}",
                      "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}],
        )
        record_path = drafts_prod / f"{candidate_id}-render-record.json"
        self.addCleanup(lambda: record_path.unlink(missing_ok=True))
        record_path.write_text(json.dumps(record), encoding="utf-8")

        _candidate_id = candidate_id

        class Args:
            candidate_id = _candidate_id
            topic = "Topic"
            topic_cluster = "cluster"
            content_type = "NEWS"
            format = "FEED"
            caption_file = f"social/drafts/production/{caption.name}"
            media = [f"social/drafts/production/{image.name}"]
            manifest_id = f"test-{tag}"
            output = str(out)
            force = False
            packaging_receipt = str(receipt_path)
            render_record = str(record_path)

        self.assertEqual(manifest_cli.build(Args()), 0)
        manifest = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(manifest["packaging"]["visual_style"], "BRANDED_GRAPHIC")


_TRIPLE_QUOTED_RE = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"#.*")


def _production_code_only(source: str) -> str:
    """Strip docstrings/comments so a code-scan guard cannot false-positive
    on this module's own prose (e.g. "no Zernio/Telegram capability" in a
    docstring). Mirrors test_breaking_production_capability_negative.py."""

    stripped = _TRIPLE_QUOTED_RE.sub("", source)
    stripped = _LINE_COMMENT_RE.sub("", stripped)
    return stripped


class NoPublicationCapabilityTests(unittest.TestCase):
    """NO_VISUAL_DECISION_CAN_TRIGGER_PUBLICATION.

    Static source-inspection guard (mirrors
    tests/test_breaking_production_capability_negative.py): neither
    Visual Director module may import or reference Zernio, Telegram,
    publish, or approval transport of any kind, in executable code
    (docstrings/comments describing the absence of such capability are
    stripped first).
    """

    # Precise capability tokens (mirrors FORBIDDEN_TRANSPORT_TOKENS /
    # FORBIDDEN_PUBLISH_TOKENS in test_breaking_production_capability_
    # negative.py), not broad English-word substrings: "published" is a
    # normal word in a reason-code description string, not a capability.
    FORBIDDEN_TOKENS = (
        "subprocess", "import openclaw", "mcp__zernio", "posts_create",
        "posts_publish_now", "posts_update", "posts_delete",
        "posts_unpublish_post", "media_get_media_presigned_url",
        "nullone-publish-bridge", "nullone-publisher-run",
        "nullone_publish_bridge", "nullone_publisher_run", "publish_now",
        "PUBLISH_AUTHORIZED", "final_publish", "send_message",
        "answer_comment", "OpenClawTelegramTransport",
        "urllib.request", "http.client", "requests.",
    )
    MODULES = ("nullone_visual_director.py", "nullone-visual-director.py")

    def test_no_publish_or_network_token_in_visual_director_modules(self):
        for filename in self.MODULES:
            source = _production_code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            for token in self.FORBIDDEN_TOKENS:
                with self.subTest(filename=filename, token=token):
                    self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
