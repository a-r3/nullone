#!/usr/bin/env python3
"""Offline contract tests for the safe credentialed acceptance entrypoint.

All external connectors are fakes (no network, no model calls, no
production state). Fixtures live in disposable temporary workspaces;
caption/media use absolute workspace-contained paths so the REAL
manifest validator exercises its exact production code path.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
AGENT = ROOT / "workspace/.opencode/agents/nullone-editorial.md"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import atomic_write_json, sha256_bytes, sha256_file  # noqa: E402
import nullone_acceptance_run as acc  # noqa: E402

AID = "nullone-acceptance-20260101-000000"
MANIFEST_REL = f"social/ops/manifests/{AID}.json"


def make_fixture(root: Path, *, aid: str = AID) -> Path:
    """Build a fully valid TEST acceptance fixture; returns manifest path."""

    drafts = root / "social/drafts/production"
    drafts.mkdir(parents=True, exist_ok=True)
    stem = aid
    caption = drafts / f"{stem}-caption.txt"
    caption.write_text("TEST — probe run. YAYIM ÜÇÜN DEYİL.\n", encoding="utf-8")
    media_path = drafts / f"{stem}.png"
    Image.new("RGB", (1080, 1350), (10, 10, 20)).save(media_path)
    manifest = {
        "schema": "nullone.production.v1",
        "account_id": "6a982bbf77555aae01c28f21",
        "verification": "PASS",
        "format": "FEED",
        "content_type": "NEWS",
        "topic_cluster": "probe",
        "caption": {"file": str(caption.resolve()), "sha256": sha256_bytes(caption.read_bytes())},
        "media": [
            {
                "local_path": str(media_path.resolve()),
                "sha256": sha256_file(media_path),
                "content_type": "image/png",
                "width": 1080,
                "height": 1350,
                "image_format": "PNG",
                "public_url": None,
            }
        ],
        "review": {"create_attempts": 0, "state": "NOT_CREATED", "zernio_draft_id": None, "created_at": None},
        "approval": {"first_stage": False, "first_stage_at": None, "final_publish": False, "final_publish_at": None, "source": None, "operator": None, "human_confirmation": None},
        "publication": {"attempts": 0, "state": "NOT_REQUESTED", "live_zernio_post_id": None, "platform_post_id": None, "permalink": None, "last_checked_at": None, "error": None},
    }
    manifest_path = root / "social/ops/manifests" / f"{stem}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(manifest_path, manifest)
    return manifest_path


class FakeDraft:
    """Fake Zernio bridge: records calls, mutates review state like the
    real provider, returns a configured exit code."""

    def __init__(self, *, exit_code: int = 0, draft_id: str | None = "zdr_fake_1") -> None:
        self.exit_code = exit_code
        self.draft_id = draft_id
        self.calls: list[str] = []

    def __call__(self, manifest_path: str) -> int:
        self.calls.append(manifest_path)
        if self.exit_code == 0 and self.draft_id is not None:
            path = Path(manifest_path)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["review"]["create_attempts"] = 1
            data["review"]["state"] = "DRAFT_CREATED"
            data["review"]["zernio_draft_id"] = self.draft_id
            atomic_write_json(path, data)
        return self.exit_code


class FakeDelivery:
    """Fake Telegram transport: records payloads, returns a configured
    delivery result."""

    def __init__(self, result: dict | None = None) -> None:
        self.result = result if result is not None else {
            "status": "SENT",
            "approval_message_id": "msg_fake_1",
            "media_message_ids": ["msg_fake_media_1"],
        }
        self.sent: list[dict] = []

    def __call__(self, payload: dict) -> dict:
        self.sent.append(payload)
        return dict(self.result)


def run(root: Path, *, aid: str = AID, manifest_rel: str = MANIFEST_REL,
        mode: str = acc.ACCEPTANCE_MODE, draft=None, delivery=None) -> dict:
    return acc.run_acceptance(
        aid, manifest_rel, mode=mode, workspace_root=root,
        draft_execute=draft or FakeDraft(), delivery_send=delivery or FakeDelivery(),
    )


class HappyPathTests(unittest.TestCase):
    def test_1_happy_path_one_draft_one_preview(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            draft, delivery = FakeDraft(), FakeDelivery()
            result = run(root, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "COMPLETED", result)
            self.assertEqual(result["zernio"]["draft_id"], "zdr_fake_1")
            self.assertTrue(result["zernio"]["created"])
            self.assertEqual(result["telegram"]["approval_message_id"], "msg_fake_1")
            self.assertTrue(result["telegram"]["sent"])
            self.assertEqual(len(draft.calls), 1)
            self.assertEqual(len(delivery.sent), 1)
            receipt = json.loads((root / "social/ops/delivery-receipts" / f"{AID}.telegram.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "SENT")
            self.assertEqual(receipt["review_post_id"], "zdr_fake_1")

    def test_2_second_invocation_creates_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            draft, delivery = FakeDraft(), FakeDelivery()
            first = run(root, draft=draft, delivery=delivery)
            self.assertEqual(first["status"], "COMPLETED")
            second = run(root, draft=draft, delivery=delivery)
            self.assertEqual(second["status"], "COMPLETED", second)
            self.assertEqual(len(draft.calls), 1)
            self.assertEqual(len(delivery.sent), 1)
            self.assertTrue(second["zernio"].get("replayed"))
            self.assertTrue(second["telegram"].get("replayed"))

    def test_3_zernio_ok_telegram_failed_no_second_draft(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            draft = FakeDraft()
            delivery = FakeDelivery(result={"status": "FAILED", "error": "boom"})
            result = run(root, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "BLOCKED", result)
            self.assertEqual(result["reason_code"], acc.REASON_TELEGRAM_FAILED)
            retry = run(root, draft=draft, delivery=delivery)
            self.assertEqual(len(draft.calls), 1, "retry must not create a second draft")
            self.assertEqual(retry["zernio"]["draft_id"], "zdr_fake_1")

    def test_4_ambiguous_zernio_lookup_before_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path = make_fixture(root)
            # Exit 0 but no persisted remote ID: ambiguous, never retried blindly.
            draft = FakeDraft(exit_code=0, draft_id=None)
            delivery = FakeDelivery()
            result = run(root, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "BLOCKED", result)
            self.assertEqual(result["reason_code"], acc.REASON_DRAFT_AMBIGUOUS)
            self.assertEqual(len(delivery.sent), 0)
            # Manifest lookup now shows a remote ID (resolved externally):
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["review"]["create_attempts"] = 1
            data["review"]["state"] = "DRAFT_CREATED"
            data["review"]["zernio_draft_id"] = "zdr_lookup_9"
            atomic_write_json(manifest_path, data)
            retry = run(root, draft=draft, delivery=delivery)
            self.assertEqual(retry["status"], "COMPLETED", retry)
            self.assertEqual(len(draft.calls), 1, "lookup short-circuits a second create")
            self.assertEqual(retry["zernio"]["draft_id"], "zdr_lookup_9")

    def test_5_telegram_ambiguous_blocks_resend(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            draft = FakeDraft()
            delivery = FakeDelivery(result={"status": "TIMEOUT", "error": "timed out"})
            result = run(root, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "BLOCKED", result)
            self.assertEqual(result["reason_code"], acc.REASON_TELEGRAM_AMBIGUOUS)
            retry = run(root, draft=draft, delivery=FakeDelivery())
            self.assertEqual(retry["status"], "BLOCKED", retry)
            self.assertEqual(retry["reason_code"], acc.REASON_TELEGRAM_ALREADY_ATTEMPTED)
            self.assertEqual(len(delivery.sent), 1, "ambiguous send must never auto-retry")


class GuardTests(unittest.TestCase):
    def test_6_invalid_acceptance_id_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            for bad in ("", "acceptance-1", "../escape", "nullone-acceptance-2026-1-1", AID + "-x"):
                result = run(root, aid=bad)
                self.assertEqual(result["status"], "BLOCKED", bad)
                self.assertEqual(result["reason_code"], acc.REASON_BAD_ID, bad)

    def test_7_non_test_payload_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            caption = root / "social/drafts/production" / f"{AID}-caption.txt"
            caption.write_text("Ordinary publishable news headline.\n", encoding="utf-8")
            manifest_path = root / "social/ops/manifests" / f"{AID}.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            from nullone_bridge_common import sha256_bytes as _sha

            data["caption"]["sha256"] = _sha(caption.read_bytes())
            atomic_write_json(manifest_path, data)
            result = run(root)
            self.assertEqual(result["status"], "BLOCKED", result)
            self.assertEqual(result["reason_code"], acc.REASON_NOT_TEST_MARKED)

    def test_8_publish_requested_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_path = make_fixture(root)
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["publication"]["attempts"] = 1
            atomic_write_json(manifest_path, data)
            result = run(root)
            self.assertEqual(result["status"], "BLOCKED", result)
            self.assertEqual(result["reason_code"], acc.REASON_PUBLISH_REQUESTED)

    def test_unknown_mode_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            result = run(root, mode="publish_anything")
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason_code"], acc.REASON_BAD_MODE)

    def test_arbitrary_manifest_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            for bad in (
                "/etc/passwd",
                "social/ops/manifests/../../.env",
                "social/research/daily/x.json",
                f"social/ops/manifests/other-id.json",
                "",
            ):
                result = run(root, manifest_rel=bad)
                self.assertEqual(result["status"], "BLOCKED", bad)
                self.assertIn(
                    result["reason_code"],
                    (acc.REASON_BAD_MANIFEST_PATH, acc.REASON_BAD_ID, acc.REASON_MANIFEST_UNREADABLE),
                    bad,
                )


class SecurityBoundaryTests(unittest.TestCase):
    FORBIDDEN_SOURCE_TOKENS = (
        "subprocess",
        "os.system",
        "os.popen",
        "eval(",
        "exec(",
        "__import__",
        "importlib",
        "openclaw message",
        "nullone-publish-bridge",
        "publisher-run",
        "PUBLISH_AUTHORIZED",
        "second-confirm",
        "approve:",
        "callback",
    )

    def test_9_approval_action_unreachable(self):
        source = (SCRIPTS / "nullone_acceptance_run.py").read_text(encoding="utf-8")
        self.assertNotIn("nullone-publish-bridge", source)
        self.assertNotIn("publisher-run", source)
        self.assertNotIn("PUBLISH_AUTHORIZED", source)

    def test_10_publish_action_unreachable(self):
        source = (SCRIPTS / "nullone_acceptance_run.py").read_text(encoding="utf-8")
        lowered = source.lower()
        self.assertNotIn("final_publish(", lowered)
        self.assertNotIn("def publish", lowered)
        self.assertNotIn("second-confirm", lowered)
        self.assertNotIn("second_confirm", lowered)

    def test_11_arbitrary_path_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            outside = Path(td) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            for bad in (str(outside), "/tmp/x.json", "social/ops/manifests/sub/x.json"):
                result = run(root, manifest_rel=bad)
                self.assertEqual(result["status"], "BLOCKED", bad)

    def test_12_arbitrary_command_impossible(self):
        source = (SCRIPTS / "nullone_acceptance_run.py").read_text(encoding="utf-8")
        for token in ("subprocess", "os.system", "os.popen", "eval(", "exec(", "__import__", "importlib", "openclaw message"):
            self.assertNotIn(token, source, msg=f"forbidden: {token}")

    def test_13_secrets_never_in_result_or_logs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            result = run(root)
            self.assertEqual(result["status"], "COMPLETED")
            dumped = json.dumps(result)
            for token in ("TOKEN", "token", "SECRET", "secret", "BEARER", "Authorization", "password"):
                self.assertNotIn(token, dumped)
            receipt = (root / "social/ops/delivery-receipts" / f"{AID}.telegram.json").read_text(encoding="utf-8")
            for token in ("TOKEN", "SECRET", "BEARER", "Authorization", "password"):
                self.assertNotIn(token, receipt)


if __name__ == "__main__":
    unittest.main()
