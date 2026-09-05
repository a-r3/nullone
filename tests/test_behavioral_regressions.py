#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
TESTS = ROOT / "tests"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import nullone_bridge_common as common  # noqa: E402
import nullone_claude as claude_runtime  # noqa: E402
import nullone_state as state  # noqa: E402
from support.domain_completion_contract import (  # noqa: E402
    CompletionContractError,
    validate_domain_completion,
)


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


draft = load_script("nullone_draft_bridge_test", "nullone-draft-bridge.py")
publish = load_script("nullone_publish_bridge_test", "nullone-publish-bridge.py")
notifier = load_script("nullone_publish_notify_test", "nullone-publish-notify.py")


@contextmanager
def isolated_workspace():
    old_workspace = common.WORKSPACE
    old_manifest_dir = common.MANIFEST_DIR

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        common.WORKSPACE = root
        common.MANIFEST_DIR = root / "social/ops/manifests"

        try:
            yield root
        finally:
            common.WORKSPACE = old_workspace
            common.MANIFEST_DIR = old_manifest_dir


def make_manifest(root: Path) -> tuple[Path, dict]:
    caption = root / "social/publisher/caption.txt"
    media = root / "social/media/card.png"
    manifest_path = root / "social/ops/manifests/test.json"

    caption.parent.mkdir(parents=True, exist_ok=True)
    media.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    caption.write_text("NullOne synthetic behavioral test.", encoding="utf-8")
    Image.new("RGB", (1080, 1350), (14, 14, 15)).save(media)

    m = {
        "schema": common.SCHEMA,
        "manifest_id": "behavioral-test",
        "created_at": common.now_iso(),
        "candidate_id": "candidate-test",
        "topic": "Synthetic test",
        "topic_cluster": "synthetic-test",
        "content_type": "EVERGREEN",
        "format": "FEED",
        "verification": "PASS",
        "account_id": common.CANONICAL_ACCOUNT_ID,
        "caption": {
            "file": str(caption.relative_to(root)),
            "sha256": common.sha256_file(caption),
        },
        "media": [
            {
                "local_path": str(media.relative_to(root)),
                "sha256": common.sha256_file(media),
                "content_type": "image/png",
                "width": 1080,
                "height": 1350,
                "image_format": "PNG",
                "public_url": "https://example.invalid/media.png",
            }
        ],
        "review": {
            "create_attempts": 0,
            "state": "NOT_CREATED",
            "zernio_draft_id": None,
            "created_at": None,
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

    return manifest_path, m


def authorize_for_publish(m: dict) -> None:
    m["review"].update(
        {
            "create_attempts": 1,
            "state": "DRAFT_CREATED",
            "zernio_draft_id": "000000000000000000000001",
            "created_at": common.now_iso(),
        }
    )
    m["approval"].update(
        {
            "first_stage": True,
            "first_stage_at": common.now_iso(),
            "final_publish": True,
            "final_publish_at": common.now_iso(),
            "source": "texbrif-approval",
            "operator": "Rauf",
            "human_confirmation": "two_step",
        }
    )


class ScriptedProvider:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.steps:
            raise AssertionError("unexpected provider call")

        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return copy.deepcopy(step)


class BehavioralRegressionTests(unittest.TestCase):
    def test_verification_not_pass_blocks(self):
        with isolated_workspace() as root:
            _, m = make_manifest(root)
            m["verification"] = "BLOCKED"
            with self.assertRaisesRegex(
                common.BridgeError,
                "verification is not PASS",
            ):
                common.validate_manifest(m)

    def test_caption_and_media_tampering_blocks(self):
        with isolated_workspace() as root:
            _, m = make_manifest(root)
            common.validate_manifest(m)

            caption = root / m["caption"]["file"]
            original_caption = caption.read_text(encoding="utf-8")
            caption.write_text(original_caption + " changed", encoding="utf-8")

            with self.assertRaisesRegex(
                common.BridgeError,
                "Caption hash mismatch",
            ):
                common.validate_manifest(m)

            caption.write_text(original_caption, encoding="utf-8")
            common.validate_manifest(m)

            media = root / m["media"][0]["local_path"]
            Image.new("RGB", (1080, 1350), (15, 15, 16)).save(media)

            with self.assertRaisesRegex(
                common.BridgeError,
                "Media hash mismatch",
            ):
                common.validate_manifest(m)

    def test_missing_approval_and_consumed_attempt_block(self):
        with isolated_workspace() as root:
            _, m = make_manifest(root)
            authorize_for_publish(m)

            missing = copy.deepcopy(m)
            missing["approval"]["final_publish"] = False
            with self.assertRaisesRegex(
                common.BridgeError,
                "Final human publish authorization missing",
            ):
                publish.require_final_authorization(missing)

            consumed = copy.deepcopy(m)
            consumed["publication"]["attempts"] = 1
            with self.assertRaisesRegex(
                common.BridgeError,
                "Publication attempt already consumed",
            ):
                publish.require_final_authorization(consumed)

    def test_ambiguous_publish_consumes_attempt_and_never_readbacks(self):
        with isolated_workspace() as root:
            manifest_path, m = make_manifest(root)
            authorize_for_publish(m)

            provider = ScriptedProvider(
                {
                    "status": "READY",
                    "review_draft_ok": True,
                    "account_ok": True,
                    "media_ok": True,
                    "error": "",
                },
                common.BridgeError("synthetic ambiguous provider result"),
            )

            events: list[str] = []

            with (
                patch.object(
                    publish,
                    "load_manifest",
                    return_value=(manifest_path, m),
                ),
                patch.object(
                    publish,
                    "run_structured",
                    side_effect=provider,
                ),
                patch.object(
                    publish,
                    "record_publication_event",
                    side_effect=lambda _m, event: events.append(event),
                ),
                patch.object(
                    publish,
                    "mark_queue_published_exact",
                    side_effect=AssertionError(
                        "queue update must not run for ambiguous publish"
                    ),
                ),
            ):
                with self.assertRaises(common.BridgeError):
                    publish.execute("synthetic")

            self.assertEqual(m["publication"]["attempts"], 1)
            self.assertEqual(m["publication"]["state"], "UNKNOWN")
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(events, [])

    def test_readback_failure_never_republishes(self):
        with isolated_workspace() as root:
            manifest_path, m = make_manifest(root)
            authorize_for_publish(m)

            provider = ScriptedProvider(
                {
                    "status": "READY",
                    "review_draft_ok": True,
                    "account_ok": True,
                    "media_ok": True,
                    "error": "",
                },
                {
                    "status": "PUBLISH_CALLED",
                    "live_zernio_post_id": "000000000000000000000002",
                    "returned_status": "publishing",
                    "error": "",
                },
                common.BridgeError("synthetic readback failure"),
            )

            events: list[str] = []

            with (
                patch.object(
                    publish,
                    "load_manifest",
                    return_value=(manifest_path, m),
                ),
                patch.object(
                    publish,
                    "run_structured",
                    side_effect=provider,
                ),
                patch.object(
                    publish,
                    "record_publication_event",
                    side_effect=lambda _m, event: events.append(event),
                ),
                patch.object(
                    publish,
                    "mark_queue_published_exact",
                    side_effect=AssertionError(
                        "queue update must not run on readback failure"
                    ),
                ),
            ):
                with self.assertRaises(common.BridgeError):
                    publish.execute("synthetic")

            self.assertEqual(m["publication"]["attempts"], 1)
            self.assertEqual(m["publication"]["state"], "READBACK_FAILED")
            self.assertEqual(len(provider.calls), 3)
            self.assertEqual(events, ["PUBLISH_ACCEPTED"])

    def test_draft_ambiguous_create_is_unsafe_to_repeat(self):
        with isolated_workspace() as root:
            manifest_path, m = make_manifest(root)

            provider = ScriptedProvider(
                {
                    "status": "READY",
                    "account_ok": True,
                    "media_ok": True,
                    "post_ok": True,
                    "error": "",
                },
                common.BridgeError("synthetic create ambiguity"),
            )

            with (
                patch.object(
                    draft,
                    "load_manifest",
                    return_value=(manifest_path, m),
                ),
                patch.object(
                    draft,
                    "run_structured",
                    side_effect=provider,
                ),
                patch.object(
                    draft,
                    "urlopen",
                    side_effect=AssertionError(
                        "network upload must not occur in isolated test"
                    ),
                ),
            ):
                with self.assertRaises(common.BridgeError):
                    draft.execute("synthetic")

            self.assertEqual(m["review"]["create_attempts"], 1)
            self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")
            self.assertEqual(len(provider.calls), 2)

            with self.assertRaisesRegex(
                common.BridgeError,
                "create attempt already consumed",
            ):
                draft.require_not_created(m)

    def test_notifier_failure_does_not_change_publication_attempt(self):
        with isolated_workspace() as root:
            manifest_path, m = make_manifest(root)
            authorize_for_publish(m)
            m["publication"]["attempts"] = 1
            m["publication"]["state"] = "UNKNOWN"

            owner = root / "private/owner"
            owner.parent.mkdir(parents=True, exist_ok=True)
            owner.write_text("100000001", encoding="utf-8")

            send_calls: list[list[str]] = []

            def fake_send(cmd, **_kwargs):
                send_calls.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 1, "", "synthetic")

            with (
                patch.object(
                    notifier,
                    "find_manifest_by_review_post_id",
                    return_value=(manifest_path, m),
                ),
                patch.object(notifier, "OWNER_ID_FILE", owner),
                patch.object(
                    notifier.subprocess,
                    "run",
                    side_effect=fake_send,
                ),
            ):
                with self.assertRaisesRegex(
                    common.BridgeError,
                    "Telegram notification failed",
                ):
                    notifier.notify("000000000000000000000001")

                self.assertEqual(m["publication"]["attempts"], 1)
                self.assertEqual(
                    m["publication"]["telegram_notification_attempts"],
                    1,
                )
                self.assertEqual(
                    m["publication"]["telegram_notification_state"],
                    "FAILED",
                )

                with self.assertRaisesRegex(
                    common.BridgeError,
                    "notification attempt already consumed",
                ):
                    notifier.notify("000000000000000000000001")

            self.assertEqual(len(send_calls), 1)
            self.assertEqual(m["publication"]["attempts"], 1)

    def test_serial_duplicate_ledger_append_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "publish-ledger.jsonl"
            record = {
                "event": "PUBLISH_ACCEPTED",
                "live_zernio_post_id": "000000000000000000000002",
            }

            first = state.append_jsonl_once(
                path,
                record,
                ("event", "live_zernio_post_id"),
            )
            second = state.append_jsonl_once(
                path,
                record,
                ("event", "live_zernio_post_id"),
            )

            self.assertTrue(first)
            self.assertFalse(second)
            rows = state.read_jsonl(path)
            self.assertEqual(rows, [record])

    def test_analytics_scheduler_success_cannot_hide_blocked_domain(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = {
                "run_id": "run-test-001",
                "scheduler_status": "succeeded",
                "domain_outcome": "BLOCKED",
                "reason_code": "ZERNIO_ANALYTICS_UNAVAILABLE",
                "reason_text": "Zernio analytics capability is unavailable.",
            }

            validate_domain_completion(
                result,
                artifact_root=root,
                required_artifacts=("analytics/daily.json",),
            )
            self.assertNotEqual(result["domain_outcome"], "SUCCEEDED")

    def test_analytics_success_requires_artifact_or_explicit_empty_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            missing = {
                "run_id": "run-test-002",
                "scheduler_status": "succeeded",
                "domain_outcome": "SUCCEEDED",
            }
            with self.assertRaisesRegex(
                CompletionContractError,
                "required artifacts missing",
            ):
                validate_domain_completion(
                    missing,
                    artifact_root=root,
                    required_artifacts=("analytics/daily.json",),
                )

            artifact = root / "analytics/daily.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text('{"status":"ok"}\n', encoding="utf-8")

            validate_domain_completion(
                missing,
                artifact_root=root,
                required_artifacts=("analytics/daily.json",),
            )

            explicit_no_data = {
                "run_id": "run-test-003",
                "scheduler_status": "succeeded",
                "domain_outcome": "SUCCEEDED",
                "empty_success": "NO_DATA",
            }
            validate_domain_completion(
                explicit_no_data,
                artifact_root=root,
                required_artifacts=("analytics/other.json",),
            )

    def test_harness_fails_closed_on_real_subprocess_or_network_attempt(self):
        schema = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

        with patch.object(
            claude_runtime.subprocess,
            "run",
            side_effect=AssertionError(
                "real subprocess is forbidden in behavioral tests"
            ),
        ):
            with self.assertRaisesRegex(
                AssertionError,
                "real subprocess is forbidden",
            ):
                claude_runtime.run_structured(
                    prompt="synthetic",
                    allowed_tools=[],
                    schema=schema,
                )

        with isolated_workspace() as root:
            media = root / "media.png"
            Image.new("RGB", (1080, 1350), (14, 14, 15)).save(media)

            item = {
                "local_path": "media.png",
                "content_type": "image/png",
            }

            with patch.object(
                draft,
                "urlopen",
                side_effect=AssertionError(
                    "real network is forbidden in behavioral tests"
                ),
            ):
                with self.assertRaisesRegex(
                    AssertionError,
                    "real network is forbidden",
                ):
                    draft.upload_one(
                        upload_url="https://example.invalid/upload",
                        item=item,
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
