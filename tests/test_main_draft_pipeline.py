#!/usr/bin/env python3
"""Behavioral tests for the #36 main (Feed/Carousel) draft pipeline.

Exercises nullone_main_draft_pipeline against temp-fixture workspaces
only. No network, no real Zernio/Telegram calls. The real
render_texbrif_v2.py/render_carousel_v2.py renderers ARE exercised (pure
local PNG renderers with no external calls); review-draft creation and
Telegram delivery always use injected fakes.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_main_draft_pipeline as pipeline  # noqa: E402
from nullone_bridge_common import BridgeError, atomic_write_json, load_manifest, now_iso  # noqa: E402

REAL_FEED_RENDERER = ROOT / "workspace/social/tools/render_texbrif_v2.py"
REAL_CAROUSEL_RENDERER = ROOT / "workspace/social/tools/render_carousel_v2.py"


class FakeDraftConnector:
    _next_id = 0

    def __init__(self, scenario: str):
        self.scenario = scenario
        self.calls = 0

    def create_review_draft(self, manifest_path: Path) -> None:
        self.calls += 1
        _, m = load_manifest(manifest_path)

        if self.scenario == "success":
            FakeDraftConnector._next_id += 1
            m["review"]["create_attempts"] = 1
            m["review"]["state"] = "DRAFT_CREATED"
            m["review"]["zernio_draft_id"] = f"fake-main-review-{FakeDraftConnector._next_id}"
            m["review"]["created_at"] = now_iso()
            atomic_write_json(manifest_path, m)
            return

        if self.scenario == "blocked_before_attempt":
            raise BridgeError("fake preflight validation blocked")

        if self.scenario == "review_unknown":
            m["review"]["create_attempts"] = 1
            m["review"]["state"] = "REVIEW_UNKNOWN"
            atomic_write_json(manifest_path, m)
            return

        raise ValueError(f"unknown fake scenario: {self.scenario}")


class FakeTelegramSender:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.sent: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.sent.append(payload)
        if self.should_fail:
            return {"status": "FAILED", "error": "fake delivery failure"}
        return {"status": "SENT"}


class MainPipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self._patcher = patch.object(bridge_common, "WORKSPACE", self.tmp_path)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def install_real_renderers(self):
        tools_dir = self.tmp_path / "social/tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(REAL_FEED_RENDERER, tools_dir / "render_texbrif_v2.py")
        shutil.copy(REAL_CAROUSEL_RENDERER, tools_dir / "render_carousel_v2.py")

    def source_image(self, name="source.png") -> str:
        path = self.tmp_path / "social/source-assets" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1600, 900), (20, 30, 40)).save(path, "PNG")
        return bridge_common.workspace_relative(path)

    def feed_candidate(self, **overrides) -> dict:
        candidate = {
            "candidate_id": "cand-feed-1",
            "topic": "Yeni AI modeli buraxıldı",
            "topic_cluster": "ai-releases",
            "content_type": "NEWS",
            "format": "FEED",
            "verification": "PASS",
            "evidence_refs": ["Rəsmi elan."],
            "source_attribution": "Rəsmi mənbə",
            "caption_text": "Yeni model buraxıldı. @nullone.az",
            "feed": {
                "source_image": self.source_image(),
                "kicker": "AI",
                "headline": "Yeni model buraxıldı",
                "source_name": "Rəsmi mənbə",
                "stat": "",
            },
        }
        candidate.update(overrides)
        return candidate

    def carousel_candidate(self, **overrides) -> dict:
        candidate = {
            "candidate_id": "cand-carousel-1",
            "topic": "Platform təqaüdə çıxır",
            "topic_cluster": "platform-lifecycle",
            "content_type": "EXPLAINER",
            "format": "CAROUSEL",
            "verification": "PASS",
            "evidence_refs": ["Rəsmi elan."],
            "source_attribution": "Rəsmi mənbə",
            "caption_text": "Miqrasiya təfərrüatları. @nullone.az",
            "carousel": {
                "meaningful_multi_slide_value": True,
                "slides": [
                    {"type": "cover", "headline": "Platform təqaüdə çıxır"},
                    {"type": "stat", "stat": "6 ay", "title": "Miqrasiya müddəti"},
                    {"type": "final", "title": "Niyə vacibdir?", "body": "Detallar."},
                ],
            },
        }
        candidate.update(overrides)
        return candidate

    def run_pipeline(self, candidate, connector=None, sender=None):
        connector = connector or FakeDraftConnector("success")
        return pipeline.run_main_pipeline(
            candidate, draft_connector=connector, telegram_sender=sender
        )


class CandidateEligibilityTests(MainPipelineTestCase):
    def test_unverified_candidate_is_blocked(self):
        candidate = self.feed_candidate(verification="PARTIAL")
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_missing_required_field_is_blocked(self):
        candidate = self.feed_candidate()
        del candidate["source_attribution"]
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_unsupported_format_is_blocked(self):
        candidate = self.feed_candidate(format="REEL")
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_feed_without_feed_object_is_blocked(self):
        candidate = self.feed_candidate()
        del candidate["feed"]
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_carousel_one_slide_is_blocked(self):
        candidate = self.carousel_candidate()
        candidate["carousel"]["slides"] = candidate["carousel"]["slides"][:1]
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_carousel_eleven_slides_is_blocked(self):
        candidate = self.carousel_candidate()
        candidate["carousel"]["slides"] = [
            {"type": "explainer", "title": f"Slide {i}", "body": "x"} for i in range(11)
        ]
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_carousel_without_multi_slide_value_is_blocked(self):
        candidate = self.carousel_candidate()
        candidate["carousel"]["meaningful_multi_slide_value"] = False
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_missing_caption_is_blocked(self):
        candidate = self.feed_candidate(caption_text="")
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")


class FeedRenderTests(MainPipelineTestCase):
    def test_feed_draft_created_end_to_end(self):
        self.install_real_renderers()
        candidate = self.feed_candidate()
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        self.assertIsNotNone(result.manifest_id)
        _, manifest = load_manifest(bridge_common.resolve_workspace_path(result.manifest_path))
        self.assertEqual(manifest["format"], "FEED")
        self.assertEqual(len(manifest["media"]), 1)
        self.assertEqual((manifest["media"][0]["width"], manifest["media"][0]["height"]), (1080, 1350))

    def test_feed_missing_source_image_render_fails(self):
        self.install_real_renderers()
        candidate = self.feed_candidate()
        candidate["feed"]["source_image"] = "social/source-assets/does-not-exist.png"
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "RENDER_FAILED")


class CarouselRenderTests(MainPipelineTestCase):
    def test_carousel_draft_created_end_to_end(self):
        self.install_real_renderers()
        candidate = self.carousel_candidate()
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        _, manifest = load_manifest(bridge_common.resolve_workspace_path(result.manifest_path))
        self.assertEqual(manifest["format"], "CAROUSEL")
        self.assertEqual(len(manifest["media"]), 3)
        for item in manifest["media"]:
            self.assertEqual((item["width"], item["height"]), (1080, 1350))

    def test_carousel_media_order_preserved(self):
        self.install_real_renderers()
        candidate = self.carousel_candidate()
        result = self.run_pipeline(candidate)
        _, manifest = load_manifest(bridge_common.resolve_workspace_path(result.manifest_path))
        paths = [item["local_path"] for item in manifest["media"]]
        self.assertEqual(paths, sorted(paths))
        self.assertTrue(paths[0].endswith("01.png"))
        self.assertTrue(paths[1].endswith("02.png"))
        self.assertTrue(paths[2].endswith("03.png"))

    def test_carousel_two_slides_minimum_allowed(self):
        self.install_real_renderers()
        candidate = self.carousel_candidate()
        candidate["carousel"]["slides"] = candidate["carousel"]["slides"][:2]
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "DRAFT_CREATED")

    def test_carousel_ten_slides_maximum_allowed(self):
        self.install_real_renderers()
        candidate = self.carousel_candidate()
        candidate["carousel"]["slides"] = [
            {"type": "explainer", "title": f"Slide {i}", "body": "Body text."} for i in range(10)
        ]
        result = self.run_pipeline(candidate)
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        _, manifest = load_manifest(bridge_common.resolve_workspace_path(result.manifest_path))
        self.assertEqual(len(manifest["media"]), 10)


class ReviewDraftAttemptTests(MainPipelineTestCase):
    def test_review_draft_blocked_before_attempt(self):
        self.install_real_renderers()
        candidate = self.feed_candidate()
        result = self.run_pipeline(candidate, connector=FakeDraftConnector("blocked_before_attempt"))
        self.assertEqual(result.outcome, "REVIEW_DRAFT_BLOCKED_BEFORE_ATTEMPT")

    def test_review_draft_ambiguous_never_retried(self):
        self.install_real_renderers()
        candidate = self.feed_candidate()
        connector = FakeDraftConnector("review_unknown")
        result = self.run_pipeline(candidate, connector=connector)
        self.assertEqual(result.outcome, "REVIEW_DRAFT_AMBIGUOUS")

        # Replay: attempt must never be repeated -- exactly one connector call.
        result2 = self.run_pipeline(candidate, connector=connector)
        self.assertEqual(result2.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 1)

    def test_successful_replay_reuses_manifest_and_does_not_recreate(self):
        self.install_real_renderers()
        candidate = self.feed_candidate()
        connector = FakeDraftConnector("success")
        first = self.run_pipeline(candidate, connector=connector)
        self.assertEqual(first.outcome, "DRAFT_CREATED")

        second = self.run_pipeline(candidate, connector=connector)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(second.manifest_id, first.manifest_id)
        self.assertEqual(connector.calls, 1)


class TelegramPreviewTests(MainPipelineTestCase):
    def test_preview_binds_exact_version_and_review_id(self):
        self.install_real_renderers()
        candidate = self.feed_candidate()
        sender = FakeTelegramSender()
        result = self.run_pipeline(candidate, sender=sender)
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        self.assertEqual(len(sender.sent), 1)
        payload = sender.sent[0]
        self.assertEqual(payload["review_post_id"], result.review_post_id)
        self.assertEqual(payload["manifest_id"], result.manifest_id)
        self.assertEqual(payload["main_version_id"], result.main_version_id)
        self.assertIn(f"texbrif:approve:{result.review_post_id}", str(payload["presentation"]))

    def test_preview_delivery_failure_does_not_create_second_draft(self):
        self.install_real_renderers()
        candidate = self.feed_candidate()
        connector = FakeDraftConnector("success")
        sender = FakeTelegramSender(should_fail=True)
        result = self.run_pipeline(candidate, connector=connector, sender=sender)
        self.assertEqual(result.outcome, "PREVIEW_DELIVERY_FAILED")
        self.assertIsNotNone(result.review_post_id)

        # Replay must not attempt a second review draft.
        result2 = self.run_pipeline(candidate, connector=connector, sender=sender)
        self.assertEqual(result2.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 1)


class DeterministicIdentityTests(MainPipelineTestCase):
    def test_request_id_is_deterministic(self):
        candidate = self.feed_candidate()
        id1 = pipeline.compute_main_request_id(candidate)
        id2 = pipeline.compute_main_request_id(candidate)
        self.assertEqual(id1, id2)

    def test_different_format_yields_different_request_id(self):
        feed = self.feed_candidate()
        carousel = self.carousel_candidate()
        carousel["candidate_id"] = feed["candidate_id"]
        self.assertNotEqual(
            pipeline.compute_main_request_id(feed),
            pipeline.compute_main_request_id(carousel),
        )

    def test_capability_negative_no_publisher_import(self):
        import inspect

        source = inspect.getsource(pipeline)
        self.assertNotIn("nullone-publish-bridge", source)
        self.assertNotIn("nullone-publisher-run", source)
        self.assertNotIn("publish_now", source)
        self.assertNotIn("import nullone_publish", source)


if __name__ == "__main__":
    unittest.main()
