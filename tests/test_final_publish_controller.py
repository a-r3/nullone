#!/usr/bin/env python3
"""Offline tests for the #89 deterministic final-publish controller.

All fake/tempdir: manifests in an isolated workspace, fake bridge core
(mutating the manifest exactly like the real core would), fake notifier,
in-memory HMAC pipe. No OpenClaw, Telegram, Zernio, or network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as common  # noqa: E402
import nullone_final_publish_controller as controller  # noqa: E402
import nullone_publish_ipc as ipc  # noqa: E402
import nullone_publish_receipt as receipts  # noqa: E402
from nullone_bridge_common import BridgeError  # noqa: E402

POST_ID = "0123456789abcdef01234567"

IDS = {
    "account_id": "test-account",
    "chat_id": "test-chat",
    "message_id": "test-message-1",
    "sender_id": "test-sender",
}


def make_envelope(message_id="test-message-1", post_id=POST_ID, nonce="n" * 16):
    return {
        "schema": "nullone.publish-callback.v1",
        "post_id": post_id,
        "account_id": IDS["account_id"],
        "chat_id": IDS["chat_id"],
        "message_id": message_id,
        "sender_id": IDS["sender_id"],
        "nonce": nonce,
        "request_id": "0" * 32,
    }


def instance_for(message_id="test-message-1", post_id=POST_ID):
    return receipts.derive_authorization_instance_id(
        IDS["account_id"], IDS["chat_id"], message_id, post_id
    )


class IsolatedWorkspace:
    """Isolated WORKSPACE + MANIFEST_DIR + NULLONE_WORKSPACE for one test."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._old_workspace = common.WORKSPACE
        self._old_manifest_dir = common.MANIFEST_DIR
        self._old_env = os.environ.get("NULLONE_WORKSPACE")

    def __enter__(self):
        common.WORKSPACE = self.root
        common.MANIFEST_DIR = self.root / "social/ops/manifests"
        os.environ["NULLONE_WORKSPACE"] = str(self.root)
        return self.root

    def __exit__(self, *exc):
        common.WORKSPACE = self._old_workspace
        common.MANIFEST_DIR = self._old_manifest_dir
        if self._old_env is None:
            os.environ.pop("NULLONE_WORKSPACE", None)
        else:
            os.environ["NULLONE_WORKSPACE"] = self._old_env
        self._tmp.cleanup()


def make_manifest(root: Path) -> tuple[Path, dict]:
    caption_path = root / "social/drafts/ctrl-caption.txt"
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_path.write_text("NullOne controller fixture.\n", encoding="utf-8")
    media_path = root / "social/drafts/ctrl-0.png"
    Image.new("RGB", (1080, 1350), (40, 50, 60)).save(media_path, "PNG")
    media = common.inspect_media(media_path, "FEED")
    media["public_url"] = "https://example.invalid/ctrl-0.png"
    manifest = {
        "schema": common.SCHEMA,
        "manifest_id": "ctrl-manifest",
        "created_at": common.now_iso(),
        "candidate_id": "candidate-ctrl",
        "topic": "Controller safety",
        "topic_cluster": "ctrl-safety",
        "content_type": "NEWS",
        "format": "FEED",
        "verification": "PASS",
        "account_id": common.CANONICAL_ACCOUNT_ID,
        "caption": {
            "file": common.workspace_relative(caption_path),
            "sha256": common.sha256_file(caption_path),
        },
        "media": [media],
        "review": {
            "create_attempts": 1,
            "state": "DRAFT_CREATED",
            "zernio_draft_id": POST_ID,
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
    path = root / "social/ops/manifests/ctrl-manifest.json"
    common.validate_manifest(manifest)
    common.atomic_write_json(path, manifest)
    return path, manifest


def fake_core_success(manifest_path, manifest):
    _path, current = common.load_manifest(manifest_path)
    current["publication"].update(
        {
            "attempts": 1,
            "state": "PUBLISHED",
            "live_zernio_post_id": "fedcba987654321001234567",
            "platform_post_id": "platform-post",
            "permalink": "https://example.invalid/live",
            "last_checked_at": common.now_iso(),
        }
    )
    common.atomic_write_json(manifest_path, current)
    return 0


def fake_core_blocked(manifest_path, manifest):
    raise BridgeError("Final read-only publication preflight failed")


def fake_core_unknown(manifest_path, manifest):
    _path, current = common.load_manifest(manifest_path)
    current["publication"].update(
        {"attempts": 1, "state": "UNKNOWN", "error": "synthetic ambiguous"}
    )
    common.atomic_write_json(manifest_path, current)
    raise BridgeError("Publication result ambiguous; retry forbidden")


class ProvenanceTests(unittest.TestCase):
    def test_raw_invocation_without_token_refused(self) -> None:
        with IsolatedWorkspace():
            with self.assertRaisesRegex(BridgeError, "provenance"):
                controller.execute_authorized(
                    POST_ID, "0" * 32, make_envelope(), _daemon_key=b"\x00" * 32
                )

    def test_wrong_token_refused(self) -> None:
        with IsolatedWorkspace():
            good = controller.install_test_key()
            wrong = b"\x01" * 32
            self.assertNotEqual(good, wrong)
            with self.assertRaisesRegex(BridgeError, "provenance"):
                controller.execute_authorized(
                    POST_ID, "0" * 32, make_envelope(), _daemon_key=wrong
                )

    def test_malformed_post_refused_before_lock(self) -> None:
        with IsolatedWorkspace():
            key = controller.install_test_key()
            with self.assertRaises(BridgeError):
                controller.execute_authorized(
                    "ZZZ", "0" * 32, make_envelope(), _daemon_key=key
                )


class AuthorizedFlowTests(unittest.TestCase):
    def test_full_pass_flow(self) -> None:
        with IsolatedWorkspace() as root:
            manifest_path, _manifest = make_manifest(root)
            key = controller.install_test_key()
            envelope = make_envelope()
            instance = instance_for()
            notified = []
            with (
                patch.object(controller, "bridge_core", side_effect=fake_core_success),
                patch.object(
                    controller, "notify_fn", side_effect=lambda pid: notified.append(pid) or 0
                ),
            ):
                code = controller.execute_authorized(
                    POST_ID, instance, envelope, _daemon_key=key
                )
            self.assertEqual(code, 0)
            self.assertEqual(notified, [POST_ID])
            _path, current = common.load_manifest(manifest_path)
            self.assertEqual(current["publication"]["attempts"], 1)
            self.assertEqual(current["publication"]["state"], "PUBLISHED")
            self.assertTrue(current["approval"]["final_publish"])
            record = receipts.read_receipt(root, POST_ID, instance)
            self.assertEqual(record["state"], "SETTLED_PUBLISHED")
            self.assertNotIn("test-account", json.dumps(record))

    def test_blocked_flow_terminal_and_fresh_instance_proceeds(self) -> None:
        with IsolatedWorkspace() as root:
            manifest_path, _manifest = make_manifest(root)
            key = controller.install_test_key()
            with patch.object(controller, "bridge_core", side_effect=fake_core_blocked):
                with patch.object(controller, "notify_fn", return_value=0):
                    code = controller.execute_authorized(
                        POST_ID, instance_for(), make_envelope(), _daemon_key=key
                    )
            self.assertEqual(code, 2)
            _path, current = common.load_manifest(manifest_path)
            self.assertEqual(current["publication"]["attempts"], 0)
            self.assertFalse(current["approval"]["final_publish"])
            record = receipts.read_receipt(root, POST_ID, instance_for())
            self.assertEqual(record["state"], "SETTLED_BLOCKED")
            # Replay of the old instance never re-executes.
            with patch.object(
                controller,
                "bridge_core",
                side_effect=AssertionError("must not invoke"),
            ):
                with patch.object(controller, "notify_fn", return_value=0):
                    again = controller.execute_authorized(
                        POST_ID, instance_for(), make_envelope(), _daemon_key=key
                    )
            self.assertEqual(again, 2)
            # Fresh second-stage message => new instance may proceed.
            fresh_envelope = make_envelope(message_id="test-message-2", nonce="m" * 16)
            fresh_instance = instance_for(message_id="test-message-2")
            with patch.object(controller, "bridge_core", side_effect=fake_core_success):
                with patch.object(controller, "notify_fn", return_value=0):
                    fresh_code = controller.execute_authorized(
                        POST_ID, fresh_instance, fresh_envelope, _daemon_key=key
                    )
            self.assertEqual(fresh_code, 0)

    def test_attempts_one_bars_every_later_instance(self) -> None:
        with IsolatedWorkspace() as root:
            manifest_path, _manifest = make_manifest(root)
            key = controller.install_test_key()
            with patch.object(controller, "bridge_core", side_effect=fake_core_unknown):
                with patch.object(controller, "notify_fn", return_value=0):
                    code = controller.execute_authorized(
                        POST_ID, instance_for(), make_envelope(), _daemon_key=key
                    )
            self.assertEqual(code, 3)
            other_envelope = make_envelope(message_id="other-message", nonce="q" * 16)
            other_instance = instance_for(message_id="other-message")
            with patch.object(
                controller,
                "bridge_core",
                side_effect=AssertionError("second attempt forbidden"),
            ):
                with patch.object(controller, "notify_fn", return_value=0):
                    other_code = controller.execute_authorized(
                        POST_ID, other_instance, other_envelope, _daemon_key=key
                    )
            self.assertEqual(other_code, 3)
            _path, current = common.load_manifest(manifest_path)
            self.assertEqual(current["publication"]["attempts"], 1)

    def test_notifier_failure_never_retries(self) -> None:
        with IsolatedWorkspace():
            make_manifest(Path(os.environ["NULLONE_WORKSPACE"]))
            key = controller.install_test_key()

            def _boom(_post_id):
                raise RuntimeError("transport down")

            with patch.object(controller, "bridge_core", side_effect=fake_core_success):
                with patch.object(controller, "notify_fn", side_effect=_boom):
                    code = controller.execute_authorized(
                        POST_ID, instance_for(), make_envelope(), _daemon_key=key
                    )
            self.assertEqual(code, 0)

    def test_unknown_terminal_never_reruns(self) -> None:
        with IsolatedWorkspace():
            make_manifest(Path(os.environ["NULLONE_WORKSPACE"]))
            key = controller.install_test_key()
            with patch.object(controller, "bridge_core", side_effect=fake_core_unknown):
                with patch.object(controller, "notify_fn", return_value=0):
                    controller.execute_authorized(
                        POST_ID, instance_for(), make_envelope(), _daemon_key=key
                    )
            with patch.object(
                controller,
                "bridge_core",
                side_effect=AssertionError("must not invoke"),
            ):
                with patch.object(controller, "notify_fn", return_value=0):
                    code = controller.execute_authorized(
                        POST_ID, instance_for(), make_envelope(), _daemon_key=key
                    )
            self.assertEqual(code, 3)


class CrashRecoveryTests(unittest.TestCase):
    def _claim_only(self, root, instance, envelope):
        manifest_path, _manifest = make_manifest(root)
        record, created = receipts.claim_receipt(
            root,
            POST_ID,
            instance,
            {
                "attempts": 0,
                "publication_state": "NOT_REQUESTED",
                "final_publish": False,
            },
            "b" * 64,
        )
        assert created
        return manifest_path

    def test_crash_after_claim_adopts(self) -> None:
        with IsolatedWorkspace() as root:
            key = controller.install_test_key()
            envelope = make_envelope()
            instance = instance_for()
            self._claim_only(root, instance, envelope)
            with patch.object(controller, "bridge_core", side_effect=fake_core_success):
                with patch.object(controller, "notify_fn", return_value=0):
                    code = controller.execute_authorized(
                        POST_ID, instance, envelope, _daemon_key=key
                    )
            self.assertEqual(code, 0)

    def test_crash_executing_with_flag_adopts(self) -> None:
        with IsolatedWorkspace() as root:
            key = controller.install_test_key()
            envelope = make_envelope()
            instance = instance_for()
            manifest_path, manifest = make_manifest(root)
            apply_final_authorization = (
                controller._publisher_run().apply_final_authorization
            )
            from nullone_story_supersession import review_post_lock

            # Simulate: authorization applied, EXECUTING written, then crash.
            with review_post_lock(POST_ID):
                apply_final_authorization(manifest_path, manifest)
            receipts.claim_receipt(
                root,
                POST_ID,
                instance,
                {"attempts": 0, "publication_state": "NOT_REQUESTED",
                 "final_publish": False},
                "c" * 64,
            )
            receipts.transition_receipt(
                root, POST_ID, instance, "EXECUTING", {"outcome": "started"}
            )
            with patch.object(controller, "bridge_core", side_effect=fake_core_success):
                with patch.object(controller, "notify_fn", return_value=0):
                    code = controller.execute_authorized(
                        POST_ID, instance, envelope, _daemon_key=key
                    )
            self.assertEqual(code, 0)

    def test_crash_after_blocked_revoke_settles_terminal(self) -> None:
        with IsolatedWorkspace() as root:
            key = controller.install_test_key()
            envelope = make_envelope()
            instance = instance_for()
            self._claim_only(root, instance, envelope)
            # Simulate wrapper ran to safe BLOCKED + revoke, then crashed
            # before receipt settlement: flag cleared, attempts 0.
            receipts.transition_receipt(
                root, POST_ID, instance, "EXECUTING", {"outcome": "started"}
            )
            with patch.object(
                controller,
                "bridge_core",
                side_effect=AssertionError("must not invoke"),
            ):
                with patch.object(controller, "notify_fn", return_value=0):
                    code = controller.execute_authorized(
                        POST_ID, instance, envelope, _daemon_key=key
                    )
            self.assertEqual(code, 2)
            record = receipts.read_receipt(root, POST_ID, instance)
            self.assertEqual(record["state"], "SETTLED_BLOCKED")

    def test_core_runs_on_calling_thread_no_survivor(self) -> None:
        # The bridge core runs synchronously on the calling thread under the
        # lock: when execute_authorized returns with attempts==0, NO
        # publication-capable worker can exist anywhere — there is no thread,
        # no timeout-abandonment, no orphan. A thread recording its identity
        # proves the same-thread boundary.
        with IsolatedWorkspace() as root:
            manifest_path, _manifest = make_manifest(root)
            key = controller.install_test_key()
            seen = {}

            def _recording_core(mp, m):
                seen["thread"] = threading.get_ident()
                seen["process"] = os.getpid()
                return 0

            with patch.object(controller, "bridge_core", side_effect=_recording_core):
                with patch.object(controller, "notify_fn", return_value=0):
                    code = controller.execute_authorized(
                        POST_ID, instance_for(), make_envelope(), _daemon_key=key
                    )
            self.assertEqual(code, 0)
            self.assertEqual(seen["thread"], threading.get_ident())
            self.assertEqual(seen["process"], os.getpid())
            _path, current = common.load_manifest(manifest_path)
            self.assertEqual(int(current["publication"]["attempts"]), 0)
            # attempts==0 on return proves nothing was or will be attempted:
            # the only execution site already ran to completion on this thread.
            record = receipts.read_receipt(root, POST_ID, instance_for())
            self.assertEqual(record["state"], "SETTLED_UNKNOWN")

    def test_boot_reconcile_abandons_stale_and_settles_consumed(self) -> None:
        with IsolatedWorkspace() as root:
            manifest_path, _manifest = make_manifest(root)
            stale_instance = instance_for(message_id="stale")
            receipts.claim_receipt(
                root,
                POST_ID,
                stale_instance,
                {"attempts": 0, "publication_state": "NOT_REQUESTED",
                 "final_publish": False},
                "d" * 64,
            )
            summary = controller.reconcile_boot(root)
            self.assertEqual(summary["abandoned"], [stale_instance])
            # Consumed row settles from manifest truth.
            _path, current = common.load_manifest(manifest_path)
            current["publication"].update({"attempts": 1, "state": "PUBLISHED"})
            common.atomic_write_json(manifest_path, current)
            live_instance = instance_for(message_id="live")
            receipts.claim_receipt(
                root,
                POST_ID,
                live_instance,
                {"attempts": 0, "publication_state": "NOT_REQUESTED",
                 "final_publish": False},
                "e" * 64,
            )
            summary = controller.reconcile_boot(root)
            self.assertEqual(len(summary["settled"]), 1)
            record = receipts.read_receipt(root, POST_ID, live_instance)
            self.assertEqual(record["state"], "SETTLED_PUBLISHED")


class ReceiptAuthorityTests(unittest.TestCase):
    def test_symlinked_receipt_root_fails_closed(self) -> None:
        with IsolatedWorkspace() as root:
            manifest_path, _manifest = make_manifest(root)
            key = controller.install_test_key()
            # Swap the canonical receipt root for a symlink: every claim
            # must fail closed before auth, core, or attempt.
            base = receipts.receipts_root(root)
            target = root / "external-receipts"
            target.mkdir(parents=True)
            os.symlink(target, base)
            strict_core = _recording_core()
            with patch.object(controller, "bridge_core", strict_core):
                with patch.object(controller, "notify_fn", return_value=0):
                    with self.assertRaisesRegex(BridgeError, "symlink"):
                        controller.execute_authorized(
                            POST_ID, instance_for(), make_envelope(),
                            _daemon_key=key,
                        )
            self.assertEqual(strict_core.calls, 0)
            _path, current = common.load_manifest(manifest_path)
            self.assertFalse(current["approval"]["final_publish"])
            self.assertEqual(int(current["publication"]["attempts"]), 0)
            self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_post_dir_fails_closed(self) -> None:
        with IsolatedWorkspace() as root:
            manifest_path, _manifest = make_manifest(root)
            key = controller.install_test_key()
            base = receipts.receipts_root(root)
            base.mkdir(parents=True)
            target = root / "external-post"
            target.mkdir()
            os.symlink(target, base / POST_ID.lower())
            strict_core = _recording_core()
            with patch.object(controller, "bridge_core", strict_core):
                with patch.object(controller, "notify_fn", return_value=0):
                    with self.assertRaisesRegex(BridgeError, "symlink"):
                        controller.execute_authorized(
                            POST_ID, instance_for(), make_envelope(),
                            _daemon_key=key,
                        )
            self.assertEqual(strict_core.calls, 0)
            _path, current = common.load_manifest(manifest_path)
            self.assertEqual(int(current["publication"]["attempts"]), 0)


class _recording_core:
    def __init__(self):
        self.calls = 0

    def __call__(self, manifest_path, manifest):
        self.calls += 1
        raise AssertionError("core must not run")


class DaemonProtocolTests(unittest.TestCase):
    # NOTE: pipe IO uses select + raw os.read with hard deadlines.
    # Buffered-reader blocking reads proved hang-prone under host CPU
    # throttling; deadline loops fail loudly instead of hanging the suite.
    def _run_daemon(self, root, key):
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        os.set_blocking(r_out, False)
        result = {}
        stdin_r = os.fdopen(r_in, "rb")
        stdout_w = os.fdopen(w_out, "wb")

        def _target():
            try:
                result["code"] = controller.daemon_main(
                    stdin=stdin_r, stdout=stdout_w, workspace=root,
                    startup_timeout=30,
                )
            except Exception as error:  # pragma: no cover
                result["error"] = error

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        # Handshake: raw key first (private spawn pipe), then framed READY.
        os.write(w_in, key)
        reply = self._read_reply(r_out, key, "READY")
        assert reply.get("t") == "ready", reply
        return thread, result, w_in, r_out

    def _read_reply(self, fd, key, what, deadline_s=30):
        import select

        buffered = b""
        deadline = time.monotonic() + deadline_s
        while True:
            try:
                body, _consumed = ipc.verify_frame(buffered, key)
                return __import__("json").loads(body.decode("utf-8"))
            except ipc.TruncatedFrameError:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{what} reply timeout")
            ready, _, _ = select.select([fd], [], [], min(5.0, remaining))
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise AssertionError(f"{what}: pipe EOF")
            buffered += chunk

    def _request(self, w_in, r_out, key, envelope):
        os.write(w_in, ipc.encode_frame(envelope, key))
        return self._read_reply(r_out, key, "request")

    def test_full_daemon_publish_flow(self) -> None:
        with IsolatedWorkspace() as root:
            make_manifest(root)
            key = ipc.generate_key()
            with (
                patch.object(controller, "bridge_core", side_effect=fake_core_success),
                patch.object(controller, "notify_fn", return_value=0),
            ):
                thread, result, w_in, r_out = self._run_daemon(root, key)
                reply = self._request(w_in, r_out, key, make_envelope())
                self.assertEqual(reply.get("t"), "result")
                # Daemon replies stringify values (bounded sanitized frame).
                self.assertEqual(int(reply.get("code")), 0)
                # Reply correlation: exact request_id echo.
                self.assertEqual(reply.get("request_id"), "0" * 32)
                os.close(w_in)
                thread.join(timeout=10)
            self.assertEqual(result.get("code"), 0)
            _path, current = common.load_manifest(
                root / "social/ops/manifests/ctrl-manifest.json"
            )
            self.assertEqual(current["publication"]["attempts"], 1)

    def test_daemon_rejects_bad_hmac(self) -> None:
        with IsolatedWorkspace() as root:
            make_manifest(root)
            key = ipc.generate_key()
            with (
                patch.object(
                    controller,
                    "bridge_core",
                    side_effect=AssertionError("must not invoke"),
                ),
                patch.object(controller, "notify_fn", return_value=0),
            ):
                thread, result, w_in, r_out = self._run_daemon(root, key)
                forged = bytearray(ipc.encode_frame(make_envelope(), key))
                forged[-1] ^= 0x01
                os.write(w_in, bytes(forged))
                reply = self._read_reply(r_out, key, "bad-hmac")
                self.assertEqual(reply.get("t"), "error")
                os.close(w_in)
                thread.join(timeout=30)

    def test_second_daemon_fails_closed(self) -> None:
        with IsolatedWorkspace() as root:
            r_in, w_in = os.pipe()
            stdin_r = os.fdopen(r_in, "rb")
            first = threading.Thread(
                target=lambda: controller.daemon_main(
                    stdin=stdin_r,
                    stdout=open(os.devnull, "wb"),
                    workspace=root,
                    startup_timeout=5,
                ),
                daemon=True,
            )
            first.start()
            time.sleep(0.5)
            # Second daemon cannot acquire the sentinel: fail-closed, and it
            # must exit WITHOUT consuming stdin (no key read, no service).
            code = controller.daemon_main(
                stdin=open(os.devnull, "rb"),
                stdout=open(os.devnull, "wb"),
                workspace=root,
                startup_timeout=3,
            )
            self.assertEqual(code, 2)
            os.close(w_in)
            first.join(timeout=30)

    def test_daemon_without_key_refused(self) -> None:
        with IsolatedWorkspace() as root:
            code = controller.daemon_main(
                stdin=open(os.devnull, "rb"),
                stdout=open(os.devnull, "wb"),
                workspace=root,
                startup_timeout=5,
            )
            # Fresh workspace: sentinel free, but EOF yields no key.
            self.assertEqual(code, 2)


class LegacyBypassTests(unittest.TestCase):
    def test_legacy_cli_execute_fail_closed(self) -> None:
        with IsolatedWorkspace():
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "nullone-publisher-run.py"),
                    "execute",
                    POST_ID,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("BLOCKED", proc.stdout + proc.stderr)

    def test_legacy_execute_function_fail_closed(self) -> None:
        with IsolatedWorkspace():
            legacy = controller._publisher_run()

            with self.assertRaisesRegex(BridgeError, "disabled"):
                legacy.execute(POST_ID)


class CapabilityNegativeTests(unittest.TestCase):
    SOURCES = [
        "nullone_final_publish_controller.py",
        "nullone_publish_ipc.py",
        "nullone_publish_receipt.py",
    ]

    def _read_sources(self):
        return "\n".join(
            (SCRIPTS / name).read_text(encoding="utf-8") for name in self.SOURCES
        )

    def test_no_sessions_send(self) -> None:
        self.assertNotIn("sessions_send", self._read_sources())

    def test_no_publish_authorized_protocol(self) -> None:
        self.assertNotIn("PUBLISH_AUTHORIZED", self._read_sources())

    def test_no_zernio_capability(self) -> None:
        for token in ("mcp__zernio", "zernio.com", "ZERNIO_"):
            self.assertNotIn(token, self._read_sources())

    def test_no_publisher_agent_dependency(self) -> None:
        self.assertNotIn("texbrif-publisher", self._read_sources())

    def test_no_implementation_of_transport(self) -> None:
        # #90 owns transport: controller must not gain HTTP/post payload code.
        for token in ("urllib", "http.client", "requests.", "publishNow"):
            self.assertNotIn(token, self._read_sources())

    def test_plugin_entry_has_no_llm_handoff(self) -> None:
        entry = (ROOT / "plugins/nullone-final-publish/index.js").read_text(
            encoding="utf-8"
        )
        # Strip comments: documentation may name retired mechanisms, but no
        # executable code path may reference them.
        code = re.sub(r"/\*.*?\*/", "", entry, flags=re.DOTALL)
        code = re.sub(r"//[^\n]*", "", code)
        for token in ("sessions_send", "submitText", "texbrif-publisher"):
            self.assertNotIn(token, code)

    def test_plugin_route_has_no_side_channels(self) -> None:
        route = (ROOT / "plugins/nullone-final-publish/route.js").read_text(
            encoding="utf-8"
        )
        for token in ("sessions_send", "zernio", "openclaw", "exec", "child_process"):
            self.assertNotIn(token, route)


if __name__ == "__main__":
    unittest.main(verbosity=2)
