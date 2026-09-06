#!/usr/bin/env python3
"""Offline publication-safety tests for Story supersession."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as common  # noqa: E402
import nullone_story_pipeline as pipeline  # noqa: E402
import nullone_story_supersession as supersession  # noqa: E402


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publisher = load_script("nullone_publisher_run_supersession_test", "nullone-publisher-run.py")

REVIEW_POST_ID = "0123456789abcdef01234567"

STORY_SPEC = {
    "layout": "big-stat",
    "headline": "Yeni düzəliş",
    "body": "Dəqiqləşdirilmiş Story mətni.",
    "stat": "",
    "source_name": "Official source",
    "use_source_image": False,
    "cta": "@nullone.az",
}


@contextmanager
def isolated_workspace():
    old_workspace = common.WORKSPACE
    old_manifest_dir = common.MANIFEST_DIR
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory).resolve()
        common.WORKSPACE = root
        common.MANIFEST_DIR = root / "social/ops/manifests"
        try:
            yield root
        finally:
            common.WORKSPACE = old_workspace
            common.MANIFEST_DIR = old_manifest_dir


def make_candidate() -> dict:
    return {
        "candidate_id": "candidate-story-supersession",
        "topic": "Supersession safety",
        "topic_cluster": "story-safety",
        "content_type": "NEWS",
        "verification": "PASS",
        "evidence_refs": ["Official evidence"],
        "source_attribution": "Official source",
        "factual_inputs": {},
    }


def make_manifest(root: Path, fmt: str = "STORY") -> tuple[Path, dict]:
    manifest_id = f"supersession-{fmt.lower()}"
    caption_path = root / f"social/drafts/{manifest_id}-caption.txt"
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_path.write_text("NullOne safety fixture.\n", encoding="utf-8")

    media_count = 2 if fmt == "CAROUSEL" else 1
    dimensions = (1080, 1920) if fmt == "STORY" else (1080, 1350)
    media = []
    for index in range(media_count):
        media_path = root / f"social/drafts/{manifest_id}-{index}.png"
        Image.new("RGB", dimensions, (10 + index, 20, 30)).save(media_path, "PNG")
        media.append(common.inspect_media(media_path, fmt))

    manifest = {
        "schema": common.SCHEMA,
        "manifest_id": manifest_id,
        "created_at": common.now_iso(),
        "candidate_id": make_candidate()["candidate_id"],
        "topic": "Supersession safety",
        "topic_cluster": "story-safety",
        "content_type": "NEWS",
        "format": fmt,
        "verification": "PASS",
        "account_id": common.CANONICAL_ACCOUNT_ID,
        "caption": {
            "file": common.workspace_relative(caption_path),
            "sha256": common.sha256_file(caption_path),
        },
        "media": media,
        "review": {
            "create_attempts": 1,
            "state": "DRAFT_CREATED",
            "zernio_draft_id": REVIEW_POST_ID,
            "created_at": common.now_iso(),
        },
        "approval": {
            "first_stage": False,
            "first_stage_at": None,
            "final_publish": False,
            "final_publish_at": None,
            "source": None,
            "operator": None,
            "human_confirmation": None,
        },
        "publication": {
            "attempts": 0,
            "state": "NOT_REQUESTED",
            "live_zernio_post_id": None,
            "platform_post_id": None,
            "permalink": None,
            "last_checked_at": None,
            "error": None,
        },
    }
    path = root / f"social/ops/manifests/{manifest_id}.json"
    common.validate_manifest(manifest)
    common.atomic_write_json(path, manifest)
    return path, manifest


def mark_superseded(manifest: dict) -> dict:
    with supersession.review_post_lock(REVIEW_POST_ID):
        record, _created = supersession.mark_story_superseded(
            parent_manifest_id=manifest["manifest_id"],
            parent_review_post_id=REVIEW_POST_ID,
            candidate_id=manifest["candidate_id"],
            superseded_by_story_request_id="story-request-test-revision",
            operator_instruction="Başlığı qısalt.",
        )
    return record


def successful_publish_run(manifest_path: Path):
    def _run(*args, **kwargs):
        _path, manifest = common.load_manifest(manifest_path)
        manifest["publication"].update(
            {
                "attempts": 1,
                "state": "PUBLISHED",
                "live_zernio_post_id": "fedcba987654321001234567",
                "platform_post_id": "platform-post",
                "permalink": "https://example.invalid/live",
                "last_checked_at": common.now_iso(),
            }
        )
        common.atomic_write_json(manifest_path, manifest)
        return subprocess.CompletedProcess(args[0], 0, stdout="PUBLISH_BRIDGE=PASS", stderr="")

    return _run


class CreatingDraftConnector:
    def create_review_draft(self, manifest_path: Path) -> None:
        _path, manifest = common.load_manifest(manifest_path)
        manifest["review"].update(
            {
                "create_attempts": 1,
                "state": "DRAFT_CREATED",
                "zernio_draft_id": "new-story-review",
                "created_at": common.now_iso(),
            }
        )
        common.atomic_write_json(manifest_path, manifest)


class PublisherSupersessionTests(unittest.TestCase):
    def test_stale_story_callback_blocks_before_authorization_or_subprocess(self):
        with isolated_workspace() as root:
            manifest_path, manifest = make_manifest(root)
            mark_superseded(manifest)

            with patch.object(
                publisher.subprocess,
                "run",
                side_effect=AssertionError("publish bridge must not be invoked"),
            ) as run:
                with self.assertRaisesRegex(common.BridgeError, "STORY_VERSION_SUPERSEDED"):
                    publisher.execute(REVIEW_POST_ID)

            run.assert_not_called()
            _path, current = common.load_manifest(manifest_path)
            self.assertFalse(current["approval"]["final_publish"])
            self.assertEqual(current["publication"]["attempts"], 0)
            self.assertEqual(current["publication"]["state"], "NOT_REQUESTED")

    def test_non_superseded_story_reaches_existing_publisher_flow(self):
        with isolated_workspace() as root:
            manifest_path, _manifest = make_manifest(root)
            with patch.object(
                publisher.subprocess,
                "run",
                side_effect=successful_publish_run(manifest_path),
            ) as run:
                self.assertEqual(publisher.execute(REVIEW_POST_ID), 0)

            self.assertEqual(run.call_count, 1)
            _path, current = common.load_manifest(manifest_path)
            self.assertTrue(current["approval"]["final_publish"])
            self.assertEqual(current["publication"]["attempts"], 1)

    def test_failed_revision_still_blocks_old_callback(self):
        with isolated_workspace() as root:
            manifest_path, manifest = make_manifest(root)
            revision = pipeline.build_revision_candidate(
                make_candidate(),
                operator_instruction="Başlığı qısalt.",
                parent_manifest_id=manifest["manifest_id"],
                parent_review_post_id=REVIEW_POST_ID,
            )
            result = pipeline.run_story_pipeline(
                revision,
                writer=lambda _context: (_ for _ in ()).throw(
                    RuntimeError("writer unavailable")
                ),
                verifier=pipeline.make_fake_verifier("PASS"),
                draft_connector=object(),
            )
            self.assertEqual(result.outcome, "WRITER_FAILED")

            with patch.object(
                publisher.subprocess,
                "run",
                side_effect=AssertionError("publish bridge must not be invoked"),
            ) as run:
                with self.assertRaisesRegex(common.BridgeError, "STORY_VERSION_SUPERSEDED"):
                    publisher.execute(REVIEW_POST_ID)
            run.assert_not_called()
            _path, current = common.load_manifest(manifest_path)
            self.assertFalse(current["approval"]["final_publish"])
            self.assertEqual(current["publication"]["attempts"], 0)

    def test_successful_revision_still_blocks_old_callback(self):
        with isolated_workspace() as root:
            manifest_path, manifest = make_manifest(root)
            tools_dir = root / "social/tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(
                ROOT / "workspace/social/tools/render_story_v2.py",
                tools_dir / "render_story_v2.py",
            )
            revision = pipeline.build_revision_candidate(
                make_candidate(),
                operator_instruction="Başlığı qısalt.",
                parent_manifest_id=manifest["manifest_id"],
                parent_review_post_id=REVIEW_POST_ID,
            )
            result = pipeline.run_story_pipeline(
                revision,
                writer=lambda _context: dict(STORY_SPEC),
                verifier=pipeline.make_fake_verifier("PASS"),
                draft_connector=CreatingDraftConnector(),
            )
            self.assertEqual(result.outcome, "DRAFT_CREATED")

            with patch.object(
                publisher.subprocess,
                "run",
                side_effect=AssertionError("publish bridge must not be invoked"),
            ) as run:
                with self.assertRaisesRegex(common.BridgeError, "STORY_VERSION_SUPERSEDED"):
                    publisher.execute(REVIEW_POST_ID)
            run.assert_not_called()
            _path, current = common.load_manifest(manifest_path)
            self.assertFalse(current["approval"]["final_publish"])
            self.assertEqual(current["publication"]["attempts"], 0)

    def test_feed_and_carousel_publisher_flow_is_unchanged(self):
        for fmt in ("FEED", "CAROUSEL"):
            with self.subTest(fmt=fmt), isolated_workspace() as root:
                manifest_path, _manifest = make_manifest(root, fmt=fmt)
                with patch.object(
                    publisher.subprocess,
                    "run",
                    side_effect=successful_publish_run(manifest_path),
                ) as run:
                    self.assertEqual(publisher.execute(REVIEW_POST_ID), 0)
                self.assertEqual(run.call_count, 1)

    def test_revision_wins_lock_then_stale_callback_blocks(self):
        with isolated_workspace() as root:
            _manifest_path, manifest = make_manifest(root)
            candidate = make_candidate()
            revision = pipeline.build_revision_candidate(
                candidate,
                operator_instruction="Başlığı qısalt.",
                parent_manifest_id=manifest["manifest_id"],
                parent_review_post_id=REVIEW_POST_ID,
            )
            marker_created = threading.Event()
            release_revision = threading.Event()
            real_mark = pipeline.mark_story_superseded

            def slow_mark(**kwargs):
                result = real_mark(**kwargs)
                marker_created.set()
                self.assertTrue(release_revision.wait(timeout=5))
                return result

            revision_result = []
            publish_error = []

            def run_revision():
                revision_result.append(
                    pipeline.run_story_pipeline(
                        revision,
                        writer=lambda _context: (_ for _ in ()).throw(
                            RuntimeError("stop after supersession")
                        ),
                        verifier=pipeline.make_fake_verifier("PASS"),
                        draft_connector=object(),
                    )
                )

            def run_publish():
                try:
                    publisher.execute(REVIEW_POST_ID)
                except Exception as error:
                    publish_error.append(error)

            with (
                patch.object(pipeline, "mark_story_superseded", side_effect=slow_mark),
                patch.object(
                    publisher.subprocess,
                    "run",
                    side_effect=AssertionError("publish bridge must not be invoked"),
                ) as publish_run,
            ):
                revision_thread = threading.Thread(target=run_revision)
                revision_thread.start()
                self.assertTrue(marker_created.wait(timeout=5))
                publish_thread = threading.Thread(target=run_publish)
                publish_thread.start()
                time.sleep(0.1)
                self.assertTrue(publish_thread.is_alive())
                release_revision.set()
                revision_thread.join(timeout=10)
                publish_thread.join(timeout=10)

            self.assertEqual(revision_result[0].outcome, "WRITER_FAILED")
            self.assertEqual(len(publish_error), 1)
            self.assertRegex(str(publish_error[0]), "STORY_VERSION_SUPERSEDED")
            publish_run.assert_not_called()

    def test_publisher_wins_lock_then_revision_is_invalid(self):
        with isolated_workspace() as root:
            manifest_path, manifest = make_manifest(root)
            candidate = make_candidate()
            revision = pipeline.build_revision_candidate(
                candidate,
                operator_instruction="Başlığı qısalt.",
                parent_manifest_id=manifest["manifest_id"],
                parent_review_post_id=REVIEW_POST_ID,
            )
            publisher_entered = threading.Event()
            release_publisher = threading.Event()

            def consequential_publish(*args, **kwargs):
                publisher_entered.set()
                self.assertTrue(release_publisher.wait(timeout=5))
                _path, current = common.load_manifest(manifest_path)
                current["publication"].update(
                    {
                        "attempts": 1,
                        "state": "UNKNOWN",
                        "error": "synthetic ambiguous result",
                    }
                )
                common.atomic_write_json(manifest_path, current)
                return subprocess.CompletedProcess(args[0], 3, stdout="", stderr="")

            publisher_result = []
            revision_result = []
            writer_calls = 0

            def run_publish():
                publisher_result.append(publisher.execute(REVIEW_POST_ID))

            def writer(_context):
                nonlocal writer_calls
                writer_calls += 1
                return {}

            def run_revision():
                revision_result.append(
                    pipeline.run_story_pipeline(
                        revision,
                        writer=writer,
                        verifier=pipeline.make_fake_verifier("PASS"),
                        draft_connector=object(),
                    )
                )

            with patch.object(
                publisher.subprocess, "run", side_effect=consequential_publish
            ):
                publish_thread = threading.Thread(target=run_publish)
                publish_thread.start()
                self.assertTrue(publisher_entered.wait(timeout=5))
                revision_thread = threading.Thread(target=run_revision)
                revision_thread.start()
                time.sleep(0.1)
                self.assertTrue(revision_thread.is_alive())
                release_publisher.set()
                publish_thread.join(timeout=10)
                revision_thread.join(timeout=10)

            self.assertEqual(publisher_result, [3])
            self.assertEqual(revision_result[0].outcome, "REVISION_PARENT_INVALID")
            self.assertEqual(writer_calls, 0)
            self.assertFalse(
                supersession.supersession_path(manifest["manifest_id"]).exists()
            )
            _path, current = common.load_manifest(manifest_path)
            self.assertEqual(current["publication"]["attempts"], 1)
            self.assertTrue(current["approval"]["final_publish"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
