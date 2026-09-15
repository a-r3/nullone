#!/usr/bin/env python3
"""Offline contract tests for the narrow Gateway acceptance action (#129).

All external connectors are fakes (no network, no model calls, no
production state, no Gateway). Fixtures live in disposable temporary
workspaces. Auth is a plugin-layer (JS) concern; these tests prove the
Python action core enforces the exact schema, internal manifest
derivation, single-flight, publication unreachability, and secret-free
results/audit.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import atomic_write_json, sha256_bytes, sha256_file  # noqa: E402
import nullone_acceptance_action as act  # noqa: E402
import nullone_acceptance_run as acc  # noqa: E402

AID = "nullone-acceptance-20260101-000000"


def make_fixture(root: Path, *, aid: str = AID) -> Path:
    """Build a fully valid TEST acceptance fixture; returns manifest path."""

    drafts = root / "social/drafts/production"
    drafts.mkdir(parents=True, exist_ok=True)
    caption = drafts / f"{aid}-caption.txt"
    caption.write_text("TEST — probe run. YAYIM ÜÇÜN DEYİL.\n", encoding="utf-8")
    media_path = drafts / f"{aid}.png"
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
    manifest_path = root / "social/ops/manifests" / f"{aid}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(manifest_path, manifest)
    return manifest_path


class FakeDraft:
    def __init__(self, *, draft_id: str | None = "zdr_action_1", delay: float = 0.0) -> None:
        self.draft_id = draft_id
        self.delay = delay
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, manifest_path: str) -> int:
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.calls.append(manifest_path)
            if self.draft_id is not None:
                path = Path(manifest_path)
                data = json.loads(path.read_text(encoding="utf-8"))
                data["review"]["create_attempts"] = 1
                data["review"]["state"] = "DRAFT_CREATED"
                data["review"]["zernio_draft_id"] = self.draft_id
                atomic_write_json(path, data)
        return 0


class FakeDelivery:
    def __init__(self, result: dict | None = None, delay: float = 0.0) -> None:
        self.result = result if result is not None else {
            "status": "SENT",
            "approval_message_id": "msg_action_1",
            "media_message_ids": ["msg_action_media_1"],
        }
        self.delay = delay
        self.sent: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, payload: dict) -> dict:
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.sent.append(payload)
        return dict(self.result)


def handle(root: Path, request: dict, *, draft=None, delivery=None) -> dict:
    return act.handle_action(
        request, workspace_root=root,
        draft_execute=draft or FakeDraft(), delivery_send=delivery or FakeDelivery(),
    )


class SchemaTests(unittest.TestCase):
    def test_1_happy_path_uses_internally_derived_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            draft, delivery = FakeDraft(), FakeDelivery()
            result = handle(root, {"acceptance_id": AID}, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "COMPLETED", result)
            self.assertEqual(result["reason_code"], "OK")
            self.assertEqual(len(draft.calls), 1)
            self.assertEqual(len(delivery.sent), 1)
            # Manifest path was derived internally, never from the caller.
            self.assertTrue(draft.calls[0].endswith(f"social/ops/manifests/{AID}.json"))

    def test_2_missing_acceptance_id_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            draft, delivery = FakeDraft(), FakeDelivery()
            result = handle(root, {}, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason_code"], act.REASON_MISSING_ID)
            self.assertEqual(draft.calls, [])
            self.assertEqual(delivery.sent, [])

    def test_3_malformed_acceptance_id_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for bad in ("", "acceptance-1", "../escape", "nullone-acceptance-2026-1-1",
                        AID + "-x", "nullone-acceptance-20260101-00000",
                        42, None, ["x"], {"nested": 1}):
                draft, delivery = FakeDraft(), FakeDelivery()
                result = handle(root, {"acceptance_id": bad}, draft=draft, delivery=delivery)
                self.assertEqual(result["status"], "BLOCKED", bad)
                self.assertEqual(result["reason_code"], act.REASON_BAD_ID, bad)
                self.assertEqual(draft.calls, [], bad)
                self.assertEqual(delivery.sent, [], bad)

    def test_4_non_dict_request_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for bad in (None, "x", [("acceptance_id", AID)], 42):
                result = handle(root, bad, draft=FakeDraft(), delivery=FakeDelivery())
                self.assertEqual(result["status"], "BLOCKED", repr(bad))
                self.assertEqual(result["reason_code"], act.REASON_MALFORMED_REQUEST, repr(bad))

    def test_5_forbidden_caller_fields_rejected(self):
        forbidden = (
            "manifest", "manifest_path", "path", "url", "command", "argv",
            "recipient", "target", "chat_id", "destination", "content", "text",
            "media", "mode", "publish", "approve", "reject", "revise",
            "schedule", "token", "secret",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for field in forbidden:
                draft, delivery = FakeDraft(), FakeDelivery()
                result = handle(root, {"acceptance_id": AID, field: "injected"},
                                draft=draft, delivery=delivery)
                self.assertEqual(result["status"], "BLOCKED", field)
                self.assertEqual(result["reason_code"], act.REASON_FORBIDDEN_FIELD, field)
                self.assertEqual(draft.calls, [], field)
                self.assertEqual(delivery.sent, [], field)

    def test_6_unknown_extra_field_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            draft, delivery = FakeDraft(), FakeDelivery()
            result = handle(root, {"acceptance_id": AID, "future_option": True},
                            draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason_code"], act.REASON_UNKNOWN_FIELD)
            self.assertEqual(draft.calls, [])
            self.assertEqual(delivery.sent, [])

    def test_7_id_rule_is_single_sourced_with_entrypoint(self):
        # The action must not drift from the reviewed entrypoint rule.
        self.assertIs(act.ACCEPTANCE_ID_RE, acc.ACCEPTANCE_ID_RE)
        self.assertEqual(act.ACCEPTANCE_MODE, acc.ACCEPTANCE_MODE)


class ReplayTests(unittest.TestCase):
    def test_8_sequential_replay_creates_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            draft, delivery = FakeDraft(), FakeDelivery()
            first = handle(root, {"acceptance_id": AID}, draft=draft, delivery=delivery)
            self.assertEqual(first["status"], "COMPLETED")
            second = handle(root, {"acceptance_id": AID}, draft=draft, delivery=delivery)
            self.assertEqual(second["status"], "COMPLETED", second)
            self.assertEqual(len(draft.calls), 1)
            self.assertEqual(len(delivery.sent), 1)
            self.assertEqual(second["zernio"]["draft_id"], "zdr_action_1")


class SingleFlightTests(unittest.TestCase):
    def test_9_concurrent_same_id_exactly_one_draft_one_send(self):
        aid = "nullone-acceptance-20260202-020202"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root, aid=aid)
            draft, delivery = FakeDraft(delay=0.2), FakeDelivery(delay=0.1)
            barrier = threading.Barrier(8)
            results: list[dict] = []

            def worker():
                barrier.wait()
                results.append(handle(root, {"acceptance_id": aid}, draft=draft, delivery=delivery))

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            self.assertEqual(len(results), 8)
            completed = [r for r in results if r["status"] == "COMPLETED"]
            busy = [r for r in results
                    if r["status"] == "BLOCKED" and r["reason_code"] == act.REASON_BUSY]
            # Exactly one winner; every loser failed closed as BUSY.
            self.assertEqual(len(completed), 1, results)
            self.assertEqual(len(busy), 7, results)
            self.assertEqual(len(draft.calls), 1, "concurrent first calls must not double-create")
            self.assertEqual(len(delivery.sent), 1, "concurrent first calls must not double-send")
            # A later call observes the first result with zero new calls.
            replay = handle(root, {"acceptance_id": aid}, draft=draft, delivery=delivery)
            self.assertEqual(replay["status"], "COMPLETED", replay)
            self.assertEqual(len(draft.calls), 1)
            self.assertEqual(len(delivery.sent), 1)

    def test_10_live_lock_holder_blocks_second_caller(self):
        aid = "nullone-acceptance-20260303-030303"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root, aid=aid)
            lock_path = act._lock_file(root.resolve(), aid)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(json.dumps({
                "schema": act.LOCK_SCHEMA,
                "acceptance_id": aid,
                "pid": os.getpid(),
                "started_at": act.now_iso(),
            }), encoding="utf-8")
            draft, delivery = FakeDraft(), FakeDelivery()
            result = handle(root, {"acceptance_id": aid}, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason_code"], act.REASON_BUSY)
            self.assertEqual(draft.calls, [])
            self.assertEqual(delivery.sent, [])

    def test_11_stale_lock_with_dead_pid_is_reclaimed(self):
        aid = "nullone-acceptance-20260404-040404"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root, aid=aid)
            lock_path = act._lock_file(root.resolve(), aid)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(json.dumps({
                "schema": act.LOCK_SCHEMA,
                "acceptance_id": aid,
                "pid": 2 ** 30,
                "started_at": act.now_iso(),
            }), encoding="utf-8")
            draft, delivery = FakeDraft(), FakeDelivery()
            result = handle(root, {"acceptance_id": aid}, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "COMPLETED", result)
            self.assertEqual(len(draft.calls), 1)

    def test_12_malformed_lock_fails_closed(self):
        aid = "nullone-acceptance-20260505-050505"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root, aid=aid)
            lock_path = act._lock_file(root.resolve(), aid)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text("not-json{{{", encoding="utf-8")
            draft, delivery = FakeDraft(), FakeDelivery()
            result = handle(root, {"acceptance_id": aid}, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason_code"], act.REASON_BUSY)
            self.assertEqual(draft.calls, [])
            self.assertEqual(delivery.sent, [])


class PublicationUnreachableTests(unittest.TestCase):
    # Bare substrings that must not appear ANYWHERE in the action source
    # (not even in prose): each would indicate a real capability.
    FORBIDDEN_SOURCE_TOKENS = (
        "nullone-publish-bridge",
        "publisher-run",
        "PUBLISH_AUTHORIZED",
        "openclaw message",
        "os.system",
        "os.popen",
        "Popen(",
        "__import__",
        "importlib",
        "import subprocess",
        "from subprocess",
        "agent exec",
    )

    # Whole-word call patterns that must never occur (regex, so words like
    # "execute" in parameter names do not false-positive).
    FORBIDDEN_CALL_PATTERNS = (
        r"\beval\s*\(",
        r"\bexec\s*\(",
    )

    # The ONLY NullOne modules the action core may depend on: the reviewed
    # acceptance operation and the shared bridge helpers. Publisher,
    # approval, scheduler, and Instagram modules are structurally excluded.
    ALLOWED_NULLONE_IMPORTS = frozenset(
        {"nullone_acceptance_run", "nullone_bridge_common"}
    )

    def test_13_no_forbidden_capability_in_source(self):
        import re

        source = (SCRIPTS / "nullone_acceptance_action.py").read_text(encoding="utf-8")
        for token in self.FORBIDDEN_SOURCE_TOKENS:
            self.assertNotIn(token, source, msg=f"forbidden: {token}")
        for pattern in self.FORBIDDEN_CALL_PATTERNS:
            self.assertIsNone(
                re.search(pattern, source), msg=f"forbidden pattern: {pattern}"
            )
        imported = set(
            re.findall(r"^\s*(?:from|import)\s+([a-zA-Z0-9_]+)", source, re.M)
        )
        nullone_imports = {m for m in imported if m.startswith("nullone_")}
        self.assertTrue(nullone_imports, "action must import the reviewed operation")
        self.assertLessEqual(nullone_imports, self.ALLOWED_NULLONE_IMPORTS)

    def test_14_publisher_approval_scheduler_modules_never_imported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            before = set(sys.modules)
            handle(root, {"acceptance_id": AID})
            after = set(sys.modules) - before
            for mod in after:
                lowered = mod.lower()
                self.assertNotIn("publish", lowered, mod)
                self.assertNotIn("approv", lowered, mod)
                self.assertNotIn("schedul", lowered, mod)
                self.assertNotIn("instagram", lowered, mod)

    def test_15_auth_lives_in_plugin_layer_not_action_signature(self):
        sig = inspect.signature(act.handle_action)
        params = set(sig.parameters)
        for forbidden_param in ("sender", "sender_id", "token", "auth", "chat_id", "user"):
            self.assertNotIn(forbidden_param, params, forbidden_param)


class SecretSafetyTests(unittest.TestCase):
    SECRET_TOKENS = ("TOKEN", "token", "SECRET", "secret", "BEARER", "Authorization", "password", "SESSION")

    def test_16_results_and_audit_carry_no_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            result = handle(root, {"acceptance_id": AID})
            self.assertEqual(result["status"], "COMPLETED")
            dumped = json.dumps(result)
            for token in self.SECRET_TOKENS:
                self.assertNotIn(token, dumped, token)
            audit_text = (root / "social/ops/acceptance-action-audit.jsonl").read_text(encoding="utf-8")
            for token in ("TOKEN", "SECRET", "BEARER", "Authorization", "password"):
                self.assertNotIn(token, audit_text, token)
            audit = json.loads(audit_text.strip().splitlines()[-1])
            self.assertEqual(audit["schema"], act.AUDIT_SCHEMA)
            self.assertEqual(audit["action"], act.ACTION_NAME)
            self.assertEqual(audit["acceptance_id"], AID)
            self.assertEqual(set(audit), {"schema", "action", "acceptance_id", "status", "reason_code", "at"})

    def test_17_public_result_drops_unexpected_secret_bearing_keys(self):
        raw = {
            "status": "COMPLETED",
            "reason_code": "OK",
            "reason_text": "done",
            "zernio": {"created": True, "draft_id": "zdr_x", "auth_header": "Bearer HUNTER2"},
            "telegram": {"sent": True, "approval_message_id": "m1", "media_message_ids": ["m2"], "bot_token": "HUNTER2"},
            "debug_env": {"ZERNIO_DRAFT_API_TOKEN": "HUNTER2"},
        }
        shaped = act._public_result(raw)
        dumped = json.dumps(shaped)
        self.assertNotIn("HUNTER2", dumped)
        self.assertNotIn("auth_header", dumped)
        self.assertNotIn("bot_token", dumped)
        self.assertNotIn("debug_env", dumped)
        self.assertEqual(shaped["zernio"]["draft_id"], "zdr_x")

    def test_18_audit_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_fixture(root)
            # Plant a directory where the audit file must be appended.
            audit_path = root / "social/ops/acceptance-action-audit.jsonl"
            audit_path.mkdir(parents=True, exist_ok=True)
            draft, delivery = FakeDraft(), FakeDelivery()
            result = handle(root, {"acceptance_id": AID}, draft=draft, delivery=delivery)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason_code"], act.REASON_AUDIT_FAILED)


if __name__ == "__main__":
    unittest.main()
