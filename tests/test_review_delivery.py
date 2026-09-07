#!/usr/bin/env python3
"""Behavioral tests for the shared #62 ReviewDelivery port and its
Telegram/OpenClaw infrastructure adapter.

No real `openclaw` invocation anywhere in this file: every transport call
is a fake `subprocess.run`-shaped callable injected into
`TelegramReviewDeliveryAdapter`. No network.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_review_delivery import (  # noqa: E402
    MAIN_PREVIEW_SCHEMA,
    STORY_PREVIEW_SCHEMA,
    FakeReviewDelivery,
    ReviewDeliveryError,
    validate_preview_payload,
)
from nullone_telegram_review_delivery_adapter import TelegramReviewDeliveryAdapter  # noqa: E402
import nullone_story_pipeline as story_pipeline  # noqa: E402
import nullone_main_draft_pipeline as main_pipeline  # noqa: E402


def make_story_preview(**overrides) -> dict:
    payload = {
        "schema": STORY_PREVIEW_SCHEMA,
        "brand": "NullOne",
        "format": "STORY",
        "topic": "Test topic",
        "candidate_id": "cand-1",
        "story_request_id": "story-request-1",
        "story_version_id": "story-version-1",
        "manifest_id": "story-manifest-1",
        "review_post_id": "review-1",
        "media": {
            "local_path": "social/drafts/production/story/story-manifest-1.png",
            "sha256": "a" * 64,
            "width": 1080,
            "height": 1920,
            "content_type": "image/png",
        },
        "caption_excerpt": "Test headline",
        "text": "NullOne Story draft\nMövzu: Test topic\nPost ID: review-1",
        "presentation": {
            "blocks": [
                {
                    "type": "buttons",
                    "buttons": [
                        {"label": "✅ Təsdiq et", "value": "texbrif:approve:review-1", "style": "success"},
                        {"label": "❌ İmtina et", "value": "texbrif:reject:review-1", "style": "danger"},
                        {"label": "📝 Dəyişiklik istə", "value": "texbrif:revise:review-1"},
                    ],
                }
            ]
        },
    }
    payload.update(overrides)
    return payload


def make_main_preview(**overrides) -> dict:
    payload = make_story_preview(schema=MAIN_PREVIEW_SCHEMA, format="FEED")
    payload["main_request_id"] = "main-request-1"
    payload["main_version_id"] = "main-version-1"
    del payload["story_request_id"]
    del payload["story_version_id"]
    payload["media"] = [payload["media"]]
    payload.update(overrides)
    return payload


class SharedPayloadValidationTests(unittest.TestCase):
    def test_story_and_main_payloads_both_validate(self):
        validate_preview_payload(make_story_preview())
        validate_preview_payload(make_main_preview())

    def test_unsupported_schema_is_rejected(self):
        with self.assertRaises(ReviewDeliveryError):
            validate_preview_payload(make_story_preview(schema="nullone.other.v1"))

    def test_non_nullone_brand_is_rejected(self):
        with self.assertRaises(ReviewDeliveryError):
            validate_preview_payload(make_story_preview(brand="Texbrif"))

    def test_public_wording_never_says_texbrif(self):
        payload = make_story_preview()
        user_facing = " ".join([payload["brand"], payload["text"], payload["topic"]])
        self.assertNotIn("texbrif", user_facing.lower())

    def test_callback_values_preserved_exactly(self):
        payload = make_story_preview()
        validate_preview_payload(payload)
        values = {b["value"] for b in payload["presentation"]["blocks"][0]["buttons"]}
        self.assertEqual(
            values,
            {
                "texbrif:approve:review-1",
                "texbrif:reject:review-1",
                "texbrif:revise:review-1",
            },
        )

    def test_non_texbrif_callback_namespace_is_rejected(self):
        bad = make_story_preview()
        bad["presentation"]["blocks"][0]["buttons"][0]["value"] = "custom:approve:review-1"
        with self.assertRaises(ReviewDeliveryError):
            validate_preview_payload(bad)

    def test_missing_buttons_block_is_rejected(self):
        bad = make_story_preview()
        bad["presentation"] = {"blocks": []}
        with self.assertRaises(ReviewDeliveryError):
            validate_preview_payload(bad)

    def test_empty_review_post_id_is_rejected(self):
        bad = make_story_preview(review_post_id="")
        with self.assertRaises(ReviewDeliveryError):
            validate_preview_payload(bad)


class FakeReviewDeliveryTests(unittest.TestCase):
    def test_story_payload_sent(self):
        sender = FakeReviewDelivery(status="SENT")
        result = sender.send(make_story_preview())
        self.assertEqual(result, {"status": "SENT"})

    def test_main_payload_sent_by_same_implementation(self):
        sender = FakeReviewDelivery(status="SENT")
        story_result = sender.send(make_story_preview())
        main_result = sender.send(make_main_preview())
        self.assertEqual(story_result, {"status": "SENT"})
        self.assertEqual(main_result, {"status": "SENT"})
        self.assertEqual(len(sender.sent), 2)

    def test_explicit_non_sent_status_is_not_success(self):
        sender = FakeReviewDelivery(status="REJECTED", error="operator busy")
        result = sender.send(make_story_preview())
        self.assertNotEqual(result["status"], "SENT")


class StoryAndMainPipelineAcceptSharedDeliveryTests(unittest.TestCase):
    """Proves the same `FakeReviewDelivery` instance, run end to end through
    both #33's `run_story_pipeline` and #36's `run_main_pipeline`, is
    accepted as their `telegram_sender` without any change to either
    pipeline module -- not merely a structural note."""

    def setUp(self):
        import shutil

        import nullone_bridge_common as bridge_common

        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)
        self._patcher_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = self.tmp_path
        self.addCleanup(setattr, bridge_common, "WORKSPACE", self._patcher_workspace)

        tools_dir = self.tmp_path / "social/tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        real_workspace_root = ROOT / "workspace"
        shutil.copy(
            real_workspace_root / "social/tools/render_story_v2.py",
            tools_dir / "render_story_v2.py",
        )
        shutil.copy(
            real_workspace_root / "social/tools/render_texbrif_v2.py",
            tools_dir / "render_texbrif_v2.py",
        )

    def test_one_shared_delivery_instance_consumes_both_story_and_main_previews(self):
        from PIL import Image

        shared_delivery = FakeReviewDelivery(status="SENT")

        story_candidate = {
            "candidate_id": "shared-story-1",
            "topic": "Shared delivery Story",
            "topic_cluster": "shared",
            "content_type": "NEWS",
            "verification": "PASS",
            "evidence_refs": ["Shared delivery evidence."],
            "source_attribution": "Self-test source",
            "factual_inputs": {},
        }

        def story_writer(_context):
            return {
                "layout": "big-stat",
                "headline": "Shared delivery headline",
                "body": "Body.",
                "stat": "1",
                "source_name": "Self-test",
                "use_source_image": False,
                "cta": "@nullone.az",
            }

        class FakeDraftConnector:
            _n = 0

            def create_review_draft(self, manifest_path: Path) -> None:
                FakeDraftConnector._n += 1
                _, manifest = story_pipeline.load_manifest(manifest_path)
                manifest["review"]["create_attempts"] = 1
                manifest["review"]["state"] = "DRAFT_CREATED"
                manifest["review"]["zernio_draft_id"] = f"shared-review-{FakeDraftConnector._n}"
                manifest["review"]["created_at"] = story_pipeline.now_iso()
                story_pipeline.atomic_write_json(manifest_path, manifest)

        story_result = story_pipeline.run_story_pipeline(
            story_candidate,
            writer=story_writer,
            verifier=story_pipeline.make_fake_verifier("PASS"),
            draft_connector=FakeDraftConnector(),
            telegram_sender=shared_delivery,
        )
        self.assertEqual(story_result.outcome, "DRAFT_CREATED")
        self.assertEqual(story_result.preview_delivery, {"status": "SENT"})

        source_image = self.tmp_path / "social/source-assets/shared-source.png"
        source_image.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1600, 900), (5, 6, 7)).save(source_image, "PNG")

        main_candidate = {
            "candidate_id": "shared-main-1",
            "topic": "Shared delivery Feed",
            "topic_cluster": "shared",
            "content_type": "NEWS",
            "format": "FEED",
            "verification": "PASS",
            "evidence_refs": ["Shared delivery evidence."],
            "source_attribution": "Self-test source",
            "caption_text": "Shared delivery caption.",
            "feed": {
                "source_image": main_pipeline.workspace_relative(source_image),
                "kicker": "TEST",
                "headline": "Shared delivery main headline",
                "source_name": "Self-test",
            },
        }

        main_result = main_pipeline.run_main_pipeline(
            main_candidate,
            final_verifier=main_pipeline.make_fake_main_verifier("PASS"),
            draft_connector=FakeDraftConnector(),
            telegram_sender=shared_delivery,
        )
        self.assertEqual(main_result.outcome, "DRAFT_CREATED")
        self.assertEqual(main_result.preview_delivery, {"status": "SENT"})

        self.assertEqual(len(shared_delivery.sent), 2)
        schemas_sent = {payload["schema"] for payload in shared_delivery.sent}
        self.assertEqual(schemas_sent, {STORY_PREVIEW_SCHEMA, MAIN_PREVIEW_SCHEMA})


class TelegramAdapterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)
        self.owner_file = self.tmp_path / "telegram-owner-id"

    def make_adapter(self, runner=None) -> TelegramReviewDeliveryAdapter:
        return TelegramReviewDeliveryAdapter(owner_id_file=self.owner_file, runner=runner)


class OwnerTargetTests(TelegramAdapterTestCase):
    def test_missing_owner_target_fails_closed_no_subprocess(self):
        def unreachable(*args, **kwargs):
            raise AssertionError("subprocess must not be invoked without an owner target")

        adapter = self.make_adapter(runner=unreachable)
        result = adapter.send(make_story_preview())
        self.assertEqual(result["status"], "OWNER_TARGET_MISSING")

    def test_blank_owner_target_fails_closed_no_subprocess(self):
        self.owner_file.write_text("   \n", encoding="utf-8")

        def unreachable(*args, **kwargs):
            raise AssertionError("subprocess must not be invoked with a blank owner target")

        adapter = self.make_adapter(runner=unreachable)
        result = adapter.send(make_story_preview())
        self.assertEqual(result["status"], "OWNER_TARGET_MISSING")

    def test_owner_target_never_appears_in_result(self):
        self.owner_file.write_text("owner-secret-id", encoding="utf-8")

        def fake_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"message_id": "m1"}), stderr="")

        adapter = self.make_adapter(runner=fake_runner)
        result = adapter.send(make_story_preview())
        self.assertNotIn("owner-secret-id", json.dumps(result))

    def test_owner_target_never_appears_in_exception_text(self):
        # Missing file -> the only possible "error" surface is the returned
        # status, never an exception carrying the file path/value.
        adapter = self.make_adapter(runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
        try:
            result = adapter.send(make_story_preview())
        except Exception as exc:  # pragma: no cover - defensive
            self.fail(f"owner-target handling must not raise: {exc}")
        self.assertEqual(result["status"], "OWNER_TARGET_MISSING")


class TransportOutcomeTests(TelegramAdapterTestCase):
    def setUp(self):
        super().setUp()
        self.owner_file.write_text("123456789", encoding="utf-8")

    def test_story_payload_sent(self):
        def fake_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"message_id": "m-story"}), stderr="")

        adapter = self.make_adapter(runner=fake_runner)
        result = adapter.send(make_story_preview())
        self.assertEqual(result, {"status": "SENT", "message_id": "m-story"})

    def test_main_payload_sent(self):
        def fake_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"message_id": "m-main"}), stderr="")

        adapter = self.make_adapter(runner=fake_runner)
        result = adapter.send(make_main_preview())
        self.assertEqual(result, {"status": "SENT", "message_id": "m-main"})

    def test_transport_process_failure(self):
        def fake_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

        adapter = self.make_adapter(runner=fake_runner)
        result = adapter.send(make_story_preview())
        self.assertEqual(result["status"], "FAILED")

    def test_transport_timeout_is_never_sent(self):
        def fake_runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 60))

        adapter = self.make_adapter(runner=fake_runner)
        result = adapter.send(make_story_preview())
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertNotEqual(result["status"], "SENT")

    def test_malformed_json_response_is_not_success(self):
        def fake_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="not-json{", stderr="")

        adapter = self.make_adapter(runner=fake_runner)
        result = adapter.send(make_story_preview())
        self.assertEqual(result["status"], "MALFORMED_RESPONSE")

    def test_zero_exit_missing_message_id_is_not_success(self):
        def fake_runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True}), stderr="")

        adapter = self.make_adapter(runner=fake_runner)
        result = adapter.send(make_story_preview())
        self.assertEqual(result["status"], "MALFORMED_RESPONSE")

    def test_explicit_non_sent_style_response_is_not_success(self):
        def fake_runner(argv, **kwargs):
            # CLI ran fine (exit 0) but reported no usable proof -- treated
            # identically to a malformed response, never SENT.
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"message_id": ""}), stderr="")

        adapter = self.make_adapter(runner=fake_runner)
        result = adapter.send(make_story_preview())
        self.assertNotEqual(result["status"], "SENT")

    def test_payload_schema_mismatch_raises_before_any_transport_call(self):
        def unreachable(*args, **kwargs):
            raise AssertionError("subprocess must not be invoked for an invalid payload")

        adapter = self.make_adapter(runner=unreachable)
        bad_payload = make_story_preview(schema="nullone.unsupported.v1")
        with self.assertRaises(ReviewDeliveryError):
            adapter.send(bad_payload)

    def test_buttons_forwarded_to_transport_unchanged(self):
        captured = {}

        def fake_runner(argv, **kwargs):
            captured["buttons"] = json.loads(argv[argv.index("--buttons") + 1])
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"message_id": "m1"}), stderr="")

        adapter = self.make_adapter(runner=fake_runner)
        adapter.send(make_story_preview())
        values = {b["value"] for b in captured["buttons"]}
        self.assertEqual(
            values,
            {"texbrif:approve:review-1", "texbrif:reject:review-1", "texbrif:revise:review-1"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
