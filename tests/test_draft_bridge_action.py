#!/usr/bin/env python3
"""Tests for the narrow credentialed draft-bridge Gateway action (#142).

Deterministic offline tests (no network, no Zernio, no Telegram, no
model). A fake bridge module stands in for `nullone-draft-bridge.py`;
the real manifest authority (`load_manifest`/`validate_manifest`)
executes for real against temp-workspace fixtures with a real PNG.

Coverage:
  schema exactness + forbidden/unknown field rejection, zero calls;
  bad/foreign manifest IDs, stem mismatch, missing file;
  preconditions (attempts/state/draft-id) refuse before any call;
  happy path exactly-once + sequential replay with zero new calls;
  8-thread concurrent single-flight (one creation);
  BUSY on live lock, stale-lock reclaim, malformed-lock fail-closed;
  secret-marker never surfaces in results/audit/stdout;
  capability negatives (no Telegram/publish/model/subprocess surface);
  CLI machine output + exit codes.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_draft_bridge_action as action  # noqa: E402
from nullone_draft_bridge_action import ActionError  # noqa: E402

ACCOUNT = "6a982bbf77555aae01c28f21"
MID = "2026-09-16-gates-pledge-2026-09-16"


def make_png(path: Path, w: int = 1080, h: int = 1350) -> None:
    from PIL import Image

    Image.new("RGB", (w, h), (11, 22, 33)).save(path, "PNG")


def write_fixture(root: Path, manifest_id: str = MID, **review_overrides) -> Path:
    caption = root / "caption.txt"
    caption.write_text("NullOne test caption #NullOne", encoding="utf-8")
    caption_sha = hashlib.sha256(caption.read_bytes()).hexdigest()
    png = root / "feed.png"
    make_png(png)
    png_sha = hashlib.sha256(png.read_bytes()).hexdigest()
    review = {
        "create_attempts": 0,
        "state": "NOT_CREATED",
        "zernio_draft_id": None,
        "created_at": None,
    }
    review.update(review_overrides)
    manifest = {
        "schema": "nullone.production.v1",
        "manifest_id": manifest_id,
        "created_at": "2026-09-16T10:44:10+00:00",
        "candidate_id": "test-candidate-2026-09-16",
        "topic": "Test topic",
        "topic_cluster": "test-cluster",
        "content_type": "NEWS",
        "format": "FEED",
        "verification": "PASS",
        "account_id": ACCOUNT,
        "caption": {"file": "caption.txt", "sha256": caption_sha},
        "media": [
            {
                "local_path": "feed.png",
                "sha256": png_sha,
                "content_type": "image/png",
                "width": 1080,
                "height": 1350,
            }
        ],
        "review": review,
        "approval": {"first_stage": False, "final_publish": False},
        "publication": {"state": "NOT_REQUESTED", "attempts": 0},
    }
    manifest_dir = root / "social" / "ops" / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / f"{manifest_id}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class FakeBridge:
    """Stand-in for the reviewed bridge module.

    create: flip the fixture to DRAFT_CREATED (mirrors provider
    persistence). blocked: return 2 without changes. explode: raise
    with a secret-like message (must never surface).
    """

    def __init__(self, mode: str = "create"):
        self.mode = mode
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def execute(self, manifest_arg: str) -> int:
        with self.lock:
            self.calls.append(manifest_arg)
        if self.mode == "explode":
            raise RuntimeError("transport blew up Bearer SECRET_MARKER_XYZ")
        if self.mode != "create":
            return 2
        import time

        time.sleep(0.05)
        root = Path(bridge_common.WORKSPACE)
        path = root / manifest_arg
        data = json.loads(path.read_text(encoding="utf-8"))
        data["review"]["create_attempts"] = 1
        data["review"]["state"] = "DRAFT_CREATED"
        data["review"]["zernio_draft_id"] = "6aa965709ec9a893c0ffee11"
        data["review"]["created_at"] = "2026-09-16T10:45:00+00:00"
        path.write_text(json.dumps(data), encoding="utf-8")
        return 0


@contextmanager
def prod_env(root: Path, fake: FakeBridge):
    """Temp workspace + fake bridge, production code paths otherwise."""
    old = action._BRIDGE_OVERRIDE
    action._BRIDGE_OVERRIDE = fake
    with mock.patch.object(bridge_common, "WORKSPACE", Path(root)):
        try:
            yield
        finally:
            action._BRIDGE_OVERRIDE = old


class RequestSchemaTest(unittest.TestCase):
    def test_exact_schema_accepted(self):
        self.assertEqual(
            action._validate_request({"manifest_id": MID}), MID
        )

    def test_violations_rejected(self):
        cases = [
            ({}, action.REASON_MISSING_ID),
            ({"command": "x"}, action.REASON_MISSING_ID),
            ({"manifest_id": MID, "command": "x"}, action.REASON_FORBIDDEN_FIELD),
            ({"manifest_id": MID, "token": "x"}, action.REASON_FORBIDDEN_FIELD),
            ({"manifest_id": MID, "publish": True}, action.REASON_FORBIDDEN_FIELD),
            ({"manifest_id": MID, "telegram": True}, action.REASON_FORBIDDEN_FIELD),
            ({"manifest_id": MID, "mode": "x"}, action.REASON_FORBIDDEN_FIELD),
            ({"manifest_id": MID, "extra": 1}, action.REASON_UNKNOWN_FIELD),
            ({"manifest_id": "../escape"}, action.REASON_BAD_ID),
            ({"manifest_id": ""}, action.REASON_BAD_ID),
            ({"manifest_id": "has space"}, action.REASON_BAD_ID),
            ({"manifest_id": "x" * 129}, action.REASON_BAD_ID),
            ({"manifest_id": 123}, action.REASON_BAD_ID),
            ("not-a-dict", action.REASON_MALFORMED_REQUEST),
            (None, action.REASON_MALFORMED_REQUEST),
        ]
        for bad, reason in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(ActionError) as ctx:
                    action._validate_request(bad)
                self.assertEqual(str(ctx.exception), reason)

    def test_schema_violations_make_zero_bridge_calls(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            with prod_env(Path(tmp), fake):
                for bad in ({"manifest_id": "../x"}, {"a": 1}):
                    with self.assertRaises(ActionError):
                        action.handle_request(bad, workspace_root=tmp)
        self.assertEqual(fake.calls, [])


class PreconditionTest(unittest.TestCase):
    def run_handle(self, tmp: str, **review_overrides):
        root = Path(tmp)
        with prod_env(root, FakeBridge()):
            write_fixture(root, **review_overrides)
            return action.handle_request(
                {"manifest_id": MID}, workspace_root=root
            )

    def test_consumed_attempts_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = self.run_handle(tmp, create_attempts=1)
            self.assertEqual(res["status"], "BLOCKED")
            self.assertEqual(res["reason_code"], action.REASON_ALREADY_CONSUMED)

    def test_wrong_state_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = self.run_handle(tmp, state="REVIEW_UNKNOWN")
            self.assertEqual(res["reason_code"], action.REASON_ALREADY_CONSUMED)

    def test_existing_draft_id_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = self.run_handle(
                tmp, zernio_draft_id="6aa965709ec9a893c0ffee11"
            )
            self.assertEqual(res["reason_code"], action.REASON_ALREADY_CONSUMED)

    def test_missing_manifest_refused(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "social" / "ops" / "manifests").mkdir(parents=True)
            with prod_env(root, fake):
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
        self.assertEqual(res["reason_code"], action.REASON_MANIFEST_UNREADABLE)
        self.assertEqual(fake.calls, [])

    def test_stem_mismatch_refused(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root, manifest_id="other-stem-2026-09-16")
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
        # No file at the derived path at all.
        self.assertEqual(res["reason_code"], action.REASON_MANIFEST_UNREADABLE)
        self.assertEqual(fake.calls, [])

    def test_foreign_directory_manifest_never_read(self):
        """A manifest outside manifests/ is unreachable: path is derived."""
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                foreign = root / "social" / "drafts" / "production"
                foreign.mkdir(parents=True)
                write_fixture(foreign, manifest_id=MID)
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
        self.assertEqual(res["reason_code"], action.REASON_MANIFEST_UNREADABLE)
        self.assertEqual(fake.calls, [])

    def test_malformed_json_refused(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                manifest_dir = root / "social" / "ops" / "manifests"
                manifest_dir.mkdir(parents=True)
                (manifest_dir / f"{MID}.json").write_text("{broken", encoding="utf-8")
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
        self.assertEqual(res["reason_code"], action.REASON_MANIFEST_UNREADABLE)
        self.assertEqual(fake.calls, [])


class ExactlyOnceTest(unittest.TestCase):
    def test_create_then_replay_zero_new_calls(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                first = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
                self.assertEqual(first["status"], "COMPLETED")
                self.assertEqual(first["reason_code"], action.REASON_OK)
                self.assertEqual(
                    first["zernio"]["draft_id"], "6aa965709ec9a893c0ffee11"
                )
                self.assertEqual(len(fake.calls), 1)
                second = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
                self.assertEqual(second["status"], "COMPLETED")
                self.assertEqual(second["reason_code"], action.REASON_OK_REPLAY)
                self.assertEqual(
                    second["zernio"]["draft_id"], "6aa965709ec9a893c0ffee11"
                )
                self.assertEqual(len(fake.calls), 1)
                audit = (
                    root / "social" / "ops" / "draft-bridge-action-audit.jsonl"
                ).read_text(encoding="utf-8")
                self.assertIn("OK_REPLAY_EXISTING_DRAFT", audit)
                self.assertNotIn("SECRET", audit)

    def test_bridge_refusal_blocks_without_creation(self):
        fake = FakeBridge(mode="blocked")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
        self.assertEqual(res["status"], "BLOCKED")
        self.assertEqual(res["reason_code"], action.REASON_BRIDGE_BLOCKED)
        self.assertFalse(res["zernio"]["created"])
        self.assertIsNone(res["zernio"]["draft_id"])

    def test_concurrent_same_id_single_creation(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                results: list[dict] = []
                errors: list[Exception] = []

                def worker():
                    try:
                        results.append(
                            action.handle_request(
                                {"manifest_id": MID}, workspace_root=root
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)

                threads = [threading.Thread(target=worker) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(fake.calls), 1)
        for res in results:
            self.assertEqual(res["status"], "COMPLETED")
            self.assertEqual(
                res["zernio"]["draft_id"], "6aa965709ec9a893c0ffee11"
            )

    def test_live_lock_returns_busy_zero_calls(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                lock_path = (
                    root / "social" / "ops" / "draft-bridge-action-locks"
                    / f"{MID}.lock"
                )
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_path.write_text(
                    json.dumps(
                        {
                            "schema": action.LOCK_SCHEMA,
                            "manifest_id": MID,
                            "pid": os.getpid(),
                            "started_at": bridge_common.now_iso(),
                        }
                    ),
                    encoding="utf-8",
                )
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
        self.assertEqual(res["reason_code"], action.REASON_BUSY)
        self.assertEqual(fake.calls, [])

    def test_stale_lock_reclaimed(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                lock_path = (
                    root / "social" / "ops" / "draft-bridge-action-locks"
                    / f"{MID}.lock"
                )
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_path.write_text(
                    json.dumps(
                        {
                            "schema": action.LOCK_SCHEMA,
                            "manifest_id": MID,
                            "pid": 999999999,
                            "started_at": "2020-01-01T00:00:00",
                        }
                    ),
                    encoding="utf-8",
                )
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(len(fake.calls), 1)

    def test_malformed_lock_fails_closed(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                lock_path = (
                    root / "social" / "ops" / "draft-bridge-action-locks"
                    / f"{MID}.lock"
                )
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_path.write_text("not-json{{", encoding="utf-8")
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
        self.assertEqual(res["reason_code"], action.REASON_BUSY)
        self.assertEqual(fake.calls, [])


class SecretSafetyTest(unittest.TestCase):
    def test_bridge_exception_secret_never_surfaces(self):
        fake = FakeBridge(mode="explode")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
                audit = (
                    root / "social" / "ops" / "draft-bridge-action-audit.jsonl"
                ).read_text(encoding="utf-8")
        blob = json.dumps(res) + audit
        self.assertNotIn("SECRET_MARKER_XYZ", blob)
        self.assertNotIn("Bearer", blob)
        self.assertEqual(res["status"], "BLOCKED")

    def test_cli_output_carries_no_secrets(self):
        fake = FakeBridge(mode="explode")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = action.main(
                        ["handle", "--manifest-id", MID]
                    )
        out = buf.getvalue()
        self.assertNotIn("SECRET_MARKER_XYZ", out)
        self.assertIn("ACTION_STATUS=BLOCKED", out)
        self.assertIn("REASON_CODE=", out)
        self.assertEqual(code, 1)

    def test_cli_completed_output_and_exit(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = action.main(
                        ["handle", "--manifest-id", MID]
                    )
        out = buf.getvalue()
        self.assertIn("ACTION_STATUS=COMPLETED", out)
        self.assertIn("DRAFT_ID=6aa965709ec9a893c0ffee11", out)
        self.assertEqual(code, 0)

    def test_cli_malformed_exit_two(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = action.main(["handle", "--manifest-id", "../x"])
        self.assertIn("ACTION_STATUS=BLOCKED", buf.getvalue())
        self.assertEqual(code, 2)


class CapabilityNegativeTest(unittest.TestCase):
    def test_no_telegram_publish_model_subprocess_surface(self):
        import re

        source = (
            ROOT
            / "workspace/social/ops/scripts/nullone_draft_bridge_action.py"
        ).read_text(encoding="utf-8")
        # Import surface: every imported module stem must be allowlisted.
        imported: set[str] = set()
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", source, re.M):
            imported.add(m.group(1).split(".")[0])
        allowed = {
            "argparse", "contextlib", "errno", "importlib", "io", "json",
            "os", "re", "threading", "time", "pathlib", "typing",
            "hashlib", "tempfile", "PIL", "datetime", "zoneinfo",
            "nullone_bridge_common", "nullone_review_lifecycle",
            "__future__",
        }
        self.assertEqual(imported - allowed, set(), f"unexpected imports: {imported - allowed}")
        # Dangerous call patterns must not appear anywhere outside prose
        # (docstrings may state their absence; strip them first).
        import re as _re

        code = _re.sub(r'"""[\s\S]*?"""', "", source)
        code = _re.sub(r"'''[\s\S]*?'''", "", code)
        code = _re.sub(r"#[^\n]*", "", code)
        for pattern in (
            "import subprocess",
            "from subprocess",
            "os.system(",
            "os.popen(",
            "os.spawn",
            "os.execv",
            "os.execle",
            "eval(",
            "shell=True",
            "submitText",
            "agentTurn",
            "texbrif:",
            "openclaw",
            "Muse",
            "anthropic",
            "deepseek",
        ):
            self.assertNotIn(pattern, code, f"forbidden pattern: {pattern}")
        # exec( as a call (not a substring of other identifiers).
        self.assertIsNone(
            _re.search(r"(?<![\w.])exec\s*\(", code),
            "forbidden exec() call",
        )

    def test_result_has_no_telegram_or_publish_keys(self):
        fake = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with prod_env(root, fake):
                write_fixture(root)
                res = action.handle_request(
                    {"manifest_id": MID}, workspace_root=root
                )
        self.assertEqual(set(res), {"status", "reason_code", "manifest_id", "zernio"})
        self.assertEqual(set(res["zernio"]), {"created", "draft_id"})


if __name__ == "__main__":
    unittest.main()
