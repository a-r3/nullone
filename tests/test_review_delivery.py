#!/usr/bin/env python3
"""Offline tests for shared ReviewDelivery and its OpenClaw adapter."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_main_draft_pipeline as main_pipeline  # noqa: E402
import nullone_story_pipeline as story_pipeline  # noqa: E402
from nullone_review_delivery import (  # noqa: E402
    MAIN_PREVIEW_SCHEMA,
    STORY_PREVIEW_SCHEMA,
    FakeReviewDelivery,
    ReviewDeliveryError,
    validate_preview_payload,
)
from nullone_telegram_review_delivery_adapter import (  # noqa: E402
    OPENCLAW_COMMIT,
    OPENCLAW_VERSION,
    TelegramReviewDeliveryAdapter,
)

CONTRACT_FIXTURE = ROOT / "tests/fixtures/openclaw-message-send-v2026.8.2.json"


def make_media(
    local_path: str = "social/drafts/production/story/story-manifest-1.png",
    sha256: str = "a" * 64,
    *,
    width: int = 1080,
    height: int = 1920,
) -> dict:
    return {
        "local_path": local_path,
        "sha256": sha256,
        "width": width,
        "height": height,
        "content_type": "image/png",
    }


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
        "media": make_media(),
        "caption_excerpt": "Test headline",
        "text": "NullOne Story draft\nMövzu: Test topic\nPost ID: review-1",
        "presentation": {
            "blocks": [
                {
                    "type": "buttons",
                    "buttons": [
                        {
                            "label": "✅ Təsdiq et",
                            "value": "texbrif:approve:review-1",
                            "style": "success",
                        },
                        {
                            "label": "❌ İmtina et",
                            "value": "texbrif:reject:review-1",
                            "style": "danger",
                        },
                        {
                            "label": "📝 Dəyişiklik istə",
                            "value": "texbrif:revise:review-1",
                        },
                    ],
                }
            ],
            "future_field": {"preserve": [1, 2, 3]},
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
    payload["media"] = [copy.deepcopy(payload["media"])]
    payload.update(overrides)
    return payload


def openclaw_json(message_id: str | None = None, **extra) -> str:
    response = {
        "action": "send",
        "channel": "telegram",
        "dryRun": False,
        "handledBy": "plugin",
        "payload": {},
        **extra,
    }
    if message_id is not None:
        response["messageId"] = message_id
    return json.dumps(response)


class SharedPayloadValidationTests(unittest.TestCase):
    def test_story_and_main_media_shapes_both_validate(self):
        validate_preview_payload(make_story_preview())
        validate_preview_payload(make_main_preview())

    def test_story_requires_media_object(self):
        for media in (None, [], {}):
            with self.subTest(media=media), self.assertRaises(ReviewDeliveryError):
                validate_preview_payload(make_story_preview(media=media))

    def test_main_requires_non_empty_media_list(self):
        for media in (None, {}, []):
            with self.subTest(media=media), self.assertRaises(ReviewDeliveryError):
                validate_preview_payload(make_main_preview(media=media))

    def test_each_media_entry_requires_transport_fields(self):
        for field in ("local_path", "sha256", "width", "height", "content_type"):
            media = make_media()
            del media[field]
            with self.subTest(field=field), self.assertRaises(ReviewDeliveryError):
                validate_preview_payload(make_story_preview(media=media))

    def test_sha256_shape_and_positive_dimensions_are_required(self):
        invalid = [
            make_media(sha256="not-a-sha"),
            {**make_media(), "width": 0},
            {**make_media(), "height": True},
        ]
        for media in invalid:
            with self.subTest(media=media), self.assertRaises(ReviewDeliveryError):
                validate_preview_payload(make_story_preview(media=media))

    def test_exact_callback_set_is_required(self):
        invalid_values = (
            "texbrif:approve:other-review-1-junk",
            "texbrif:approve:review-1:extra",
            "custom:approve:review-1",
        )
        for value in invalid_values:
            payload = make_story_preview()
            payload["presentation"]["blocks"][0]["buttons"][0]["value"] = value
            with self.subTest(value=value), self.assertRaises(ReviewDeliveryError):
                validate_preview_payload(payload)

    def test_duplicate_or_missing_callback_is_rejected(self):
        payload = make_story_preview()
        buttons = payload["presentation"]["blocks"][0]["buttons"]
        buttons[2]["value"] = buttons[0]["value"]
        with self.assertRaises(ReviewDeliveryError):
            validate_preview_payload(payload)

    def test_unsupported_schema_brand_and_empty_review_id_are_rejected(self):
        invalid = (
            make_story_preview(schema="nullone.other.v1"),
            make_story_preview(brand="Texbrif"),
            make_story_preview(review_post_id=""),
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ReviewDeliveryError):
                validate_preview_payload(payload)

    def test_public_wording_remains_nullone(self):
        payload = make_story_preview()
        user_facing = " ".join([payload["brand"], payload["text"], payload["topic"]])
        self.assertNotIn("texbrif", user_facing.lower())


class FakeReviewDeliveryTests(unittest.TestCase):
    def test_same_port_accepts_story_and_main(self):
        sender = FakeReviewDelivery(status="SENT")
        self.assertEqual(sender.send(make_story_preview()), {"status": "SENT"})
        self.assertEqual(sender.send(make_main_preview()), {"status": "SENT"})
        self.assertEqual(len(sender.sent), 2)

    def test_explicit_non_sent_status_is_not_success(self):
        result = FakeReviewDelivery(status="REJECTED", error="busy").send(
            make_story_preview()
        )
        self.assertNotEqual(result["status"], "SENT")


class StoryAndMainPipelineAcceptSharedDeliveryTests(unittest.TestCase):
    def setUp(self):
        import shutil

        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)
        original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = self.tmp_path
        self.addCleanup(setattr, bridge_common, "WORKSPACE", original_workspace)

        tools_dir = self.tmp_path / "social/tools"
        tools_dir.mkdir(parents=True)
        real_workspace = ROOT / "workspace"
        shutil.copy(real_workspace / "social/tools/render_story_v2.py", tools_dir)
        shutil.copy(real_workspace / "social/tools/render_texbrif_v2.py", tools_dir)

    def test_one_shared_delivery_instance_consumes_both_pipeline_previews(self):
        from PIL import Image

        delivery = FakeReviewDelivery(status="SENT")

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
            count = 0

            def create_review_draft(self, manifest_path: Path) -> None:
                FakeDraftConnector.count += 1
                _, manifest = story_pipeline.load_manifest(manifest_path)
                manifest["review"].update(
                    {
                        "create_attempts": 1,
                        "state": "DRAFT_CREATED",
                        "zernio_draft_id": f"shared-{FakeDraftConnector.count}",
                        "created_at": story_pipeline.now_iso(),
                    }
                )
                story_pipeline.atomic_write_json(manifest_path, manifest)

        story_result = story_pipeline.run_story_pipeline(
            story_candidate,
            writer=story_writer,
            verifier=story_pipeline.make_fake_verifier("PASS"),
            draft_connector=FakeDraftConnector(),
            telegram_sender=delivery,
        )
        self.assertEqual(story_result.outcome, "DRAFT_CREATED")
        self.assertEqual(story_result.preview_delivery, {"status": "SENT"})

        source = self.tmp_path / "social/source-assets/shared-source.png"
        source.parent.mkdir(parents=True)
        Image.new("RGB", (1600, 900), (5, 6, 7)).save(source, "PNG")
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
                "source_image": main_pipeline.workspace_relative(source),
                "kicker": "TEST",
                "headline": "Shared delivery main headline",
                "source_name": "Self-test",
            },
        }
        main_result = main_pipeline.run_main_pipeline(
            main_candidate,
            final_verifier=main_pipeline.make_fake_main_verifier("PASS"),
            draft_connector=FakeDraftConnector(),
            telegram_sender=delivery,
        )
        self.assertEqual(main_result.outcome, "DRAFT_CREATED")
        self.assertEqual(main_result.preview_delivery, {"status": "SENT"})
        self.assertEqual(
            [payload["schema"] for payload in delivery.sent],
            [STORY_PREVIEW_SCHEMA, MAIN_PREVIEW_SCHEMA],
        )


class TelegramAdapterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)
        original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = self.tmp_path
        self.addCleanup(setattr, bridge_common, "WORKSPACE", original_workspace)
        self.owner_file = self.tmp_path / "telegram-owner-id"
        self.owner_file.write_text("fake-owner-id", encoding="utf-8")

    def media(self, name: str, content: bytes | None = None) -> dict:
        content = content if content is not None else name.encode("utf-8")
        path = self.tmp_path / "social/drafts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return make_media(
            str(path.relative_to(self.tmp_path)), hashlib.sha256(content).hexdigest()
        )

    def story(self, name: str = "story.png") -> dict:
        return make_story_preview(media=self.media(name))

    def main(self, *names: str, fmt: str = "FEED") -> dict:
        return make_main_preview(
            format=fmt,
            media=[self.media(name, f"content:{name}".encode()) for name in names],
        )

    def adapter(self, runner) -> TelegramReviewDeliveryAdapter:
        return TelegramReviewDeliveryAdapter(owner_id_file=self.owner_file, runner=runner)

    @staticmethod
    def success_runner(calls: list[list[str]]):
        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=openclaw_json(f"message-{len(calls)}"), stderr=""
            )

        return runner


class OpenClawContractTests(TelegramAdapterTestCase):
    def test_pinned_offline_contract_fixture(self):
        fixture = json.loads(CONTRACT_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(fixture["source"]["version"], OPENCLAW_VERSION)
        self.assertEqual(fixture["source"]["commit"], OPENCLAW_COMMIT)
        self.assertIn("--presentation", fixture["send"]["supported_flags"])
        self.assertIn("--media", fixture["send"]["supported_flags"])
        self.assertIn("--buttons", fixture["send"]["unsupported_flags"])
        self.assertEqual(fixture["send"]["proof_field"], "messageId")

    def test_story_media_then_exact_presentation_and_never_buttons(self):
        calls: list[list[str]] = []
        payload = self.story()
        result = self.adapter(self.success_runner(calls)).send(payload)

        resolved = str(
            bridge_common.resolve_workspace_path(payload["media"]["local_path"])
        )
        base = [
            "openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--account",
            "texbrif",
            "--target",
            "fake-owner-id",
        ]
        self.assertEqual(calls[0], [*base, "--media", resolved, "--json"])
        self.assertEqual(
            calls[1][:-3], [*base, "--message", payload["text"]]
        )
        self.assertEqual(calls[1][-3], "--presentation")
        self.assertEqual(json.loads(calls[1][-2]), payload["presentation"])
        self.assertEqual(calls[1][-1], "--json")
        self.assertTrue(all("--buttons" not in argv for argv in calls))
        self.assertEqual(
            result,
            {
                "status": "SENT",
                "media_message_ids": ["message-1"],
                "approval_message_id": "message-2",
            },
        )

    def test_feed_media_then_one_approval_card(self):
        calls: list[list[str]] = []
        payload = self.main("feed.png")
        result = self.adapter(self.success_runner(calls)).send(payload)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0][calls[0].index("--media") + 1],
            str(
                bridge_common.resolve_workspace_path(
                    payload["media"][0]["local_path"]
                )
            ),
        )
        self.assertIn("--presentation", calls[1])
        self.assertEqual(result["media_message_ids"], ["message-1"])
        self.assertEqual(result["approval_message_id"], "message-2")

    def test_carousel_preserves_three_media_paths_then_one_card(self):
        calls: list[list[str]] = []
        payload = self.main("slide-1.png", "slide-2.png", "slide-3.png", fmt="CAROUSEL")
        result = self.adapter(self.success_runner(calls)).send(payload)
        media_paths = [argv[argv.index("--media") + 1] for argv in calls[:-1]]
        expected = [
            str(bridge_common.resolve_workspace_path(item["local_path"]))
            for item in payload["media"]
        ]
        self.assertEqual(media_paths, expected)
        self.assertEqual(len(calls), 4)
        self.assertNotIn("--media", calls[-1])
        self.assertIn("--presentation", calls[-1])
        self.assertEqual(result["media_message_ids"], ["message-1", "message-2", "message-3"])
        self.assertEqual(result["approval_message_id"], "message-4")


class JsonProofTests(TelegramAdapterTestCase):
    def run_first_send_response(self, stdout: str, returncode: int = 0) -> tuple[dict, int]:
        calls = 0

        def runner(argv, **_kwargs):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

        return self.adapter(runner).send(self.story()), calls

    def test_valid_top_level_message_id_is_eligible_proof(self):
        calls: list[list[str]] = []
        result = self.adapter(self.success_runner(calls)).send(self.story())
        self.assertEqual(result["status"], "SENT")

    def test_missing_blank_and_snake_case_only_message_id_are_rejected(self):
        responses = (
            openclaw_json(),
            openclaw_json(""),
            openclaw_json("   "),
            json.dumps(
                {
                    "action": "send",
                    "channel": "telegram",
                    "dryRun": False,
                    "handledBy": "plugin",
                    "message_id": "fake",
                    "payload": {},
                }
            ),
        )
        for stdout in responses:
            with self.subTest(stdout=stdout):
                result, calls = self.run_first_send_response(stdout)
                self.assertEqual(result["status"], "MALFORMED_RESPONSE")
                self.assertEqual(calls, 1)

    def test_invalid_json_and_non_object_are_rejected(self):
        for stdout in ("not-json{", "[]", "null"):
            with self.subTest(stdout=stdout):
                result, calls = self.run_first_send_response(stdout)
                self.assertEqual(result["status"], "MALFORMED_RESPONSE")
                self.assertEqual(calls, 1)

    def test_nonzero_exit_is_non_sent_and_not_retried(self):
        result, calls = self.run_first_send_response("", returncode=7)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(calls, 1)

    def test_timeout_is_non_sent_and_not_retried(self):
        calls = 0

        def runner(argv, **kwargs):
            nonlocal calls
            calls += 1
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        result = self.adapter(runner).send(self.story())
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertEqual(result["failed_step"], "MEDIA")
        self.assertEqual(calls, 1)


class MediaIntegrityTests(TelegramAdapterTestCase):
    def assert_rejected_without_call(self, payload: dict, expected_status: str):
        calls = 0

        def runner(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("transport must not run")

        result = self.adapter(runner).send(payload)
        self.assertEqual(result["status"], expected_status)
        self.assertEqual(calls, 0)

    def test_missing_and_hash_mismatched_media_are_not_sent(self):
        missing = make_story_preview(media=make_media("social/drafts/missing.png"))
        changed = self.story("changed.png")
        changed["media"]["sha256"] = "0" * 64
        self.assert_rejected_without_call(missing, "MEDIA_INVALID")
        self.assert_rejected_without_call(changed, "MEDIA_INVALID")

    def test_path_escape_is_not_sent(self):
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "outside.png"
            outside.write_bytes(b"outside")
            payload = make_story_preview(
                media=make_media(
                    str(outside), hashlib.sha256(b"outside").hexdigest()
                )
            )
            self.assert_rejected_without_call(payload, "MEDIA_INVALID")

    def test_malformed_media_shape_is_invalid_payload_without_call(self):
        payload = make_story_preview(media={"local_path": "story.png"})
        self.assert_rejected_without_call(payload, "INVALID_PAYLOAD")

    def test_non_json_presentation_is_invalid_before_media_delivery(self):
        payload = self.story()
        payload["presentation"]["future_field"] = {"not-json-serializable"}
        self.assert_rejected_without_call(payload, "INVALID_PAYLOAD")

    def test_all_carousel_media_are_verified_before_first_call(self):
        first = self.media("valid-slide.png")
        bad = make_media("social/drafts/missing-slide.png")
        payload = make_main_preview(format="CAROUSEL", media=[first, bad])
        self.assert_rejected_without_call(payload, "MEDIA_INVALID")


class PartialDeliveryTests(TelegramAdapterTestCase):
    def test_first_media_failure_stops_before_approval(self):
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="failure")

        result = self.adapter(runner).send(self.story())
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["media_message_ids"], [])
        self.assertEqual(len(calls), 1)

    def test_carousel_second_media_failure_stops_without_retry_or_card(self):
        calls: list[list[str]] = []
        payload = self.main("one.png", "two.png", "three.png", fmt="CAROUSEL")

        def runner(argv, **_kwargs):
            calls.append(argv)
            if len(calls) == 1:
                return subprocess.CompletedProcess(argv, 0, stdout=openclaw_json("m1"), stderr="")
            return subprocess.CompletedProcess(argv, 3, stdout="", stderr="failure")

        result = self.adapter(runner).send(payload)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["media_message_ids"], ["m1"])
        self.assertEqual(result["failed_media_index"], 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("--presentation" not in argv for argv in calls))

    def test_approval_failure_retains_media_proof_and_is_non_sent(self):
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            if len(calls) == 1:
                return subprocess.CompletedProcess(argv, 0, stdout=openclaw_json("media-1"), stderr="")
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="failure")

        result = self.adapter(runner).send(self.story())
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["failed_step"], "APPROVAL")
        self.assertEqual(result["media_message_ids"], ["media-1"])
        self.assertEqual(len(calls), 2)


class OwnerTargetSecrecyTests(TelegramAdapterTestCase):
    def test_missing_or_blank_owner_fails_before_transport(self):
        for owner_state in ("missing", "blank"):
            with self.subTest(owner_state=owner_state):
                if owner_state == "missing":
                    self.owner_file.unlink(missing_ok=True)
                else:
                    self.owner_file.write_text("  \n", encoding="utf-8")
                calls = 0

                def runner(*_args, **_kwargs):
                    nonlocal calls
                    calls += 1

                result = self.adapter(runner).send(self.story())
                self.assertEqual(result["status"], "OWNER_TARGET_MISSING")
                self.assertEqual(calls, 0)

    def test_owner_never_appears_in_result_or_failure_error(self):
        secret = "obvious-fake-owner-secret"
        self.owner_file.write_text(secret, encoding="utf-8")

        def runner(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=secret)

        result = self.adapter(runner).send(self.story())
        self.assertNotIn(secret, json.dumps(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
