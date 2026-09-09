#!/usr/bin/env python3
"""Offline tests for the #90 publication-secret pipe boundary.

The publication credential must NEVER come from inherited environment
state. In production it arrives only from the OpenClaw protected store
(SecretRef), resolved by the plugin at activation, and crosses to the
controller daemon exclusively through the authenticated private spawn
pipe as one bounded startup frame. The daemon refuses READY until a
valid channel AND a valid non-empty credential are both established,
wraps the credential as a memory-only SecretValue, and injects it into
the provider factory through an InMemorySecretProvider -- the single
secret source. No network, no real credential, no live calls.
"""
from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_final_publish_controller as controller  # noqa: E402
import nullone_publish_ipc as ipc  # noqa: E402
import nullone_state as nullone_state  # noqa: E402
import nullone_zernio_publish_adapter as adapter  # noqa: E402
from nullone_bridge_common import BridgeError, atomic_write_json  # noqa: E402
from nullone_secret_provider import (  # noqa: E402
    PUBLISH_SECRET_STORE_ID,
    SECRET_ID_ZERNIO_PUBLISH_BEARER,
    EnvironmentSecretProvider,
    InMemorySecretProvider,
    SecretNotConfiguredError,
    SecretValue,
)

POST_ID = "0123456789abcdef01234567"
PIPE_MARKER = "FAKE_PIPE_PUBLISH_SECRET_DO_NOT_LOG_789"
CAPTION_TEXT = "NullOne pipe fixture caption.\n"
MEDIA_URL = "https://example.invalid/pipe-0.png"


def load_bridge_fresh(name):
    spec = importlib.util.spec_from_file_location(
        name, SCRIPTS / "nullone-publish-bridge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PipeWorkspace:
    """Isolated workspace + ledger/queue bindings for one test."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._old_workspace = bridge_common.WORKSPACE
        self._old_manifest_dir = bridge_common.MANIFEST_DIR
        self._old_env = os.environ.get("NULLONE_WORKSPACE")
        self._old_ledger = nullone_state.PUBLISH_LEDGER
        self._old_topic = nullone_state.TOPIC_LEDGER
        self._old_queue = nullone_state.QUEUE

    def __enter__(self):
        bridge_common.WORKSPACE = self.root
        bridge_common.MANIFEST_DIR = self.root / "social/ops/manifests"
        os.environ["NULLONE_WORKSPACE"] = str(self.root)
        nullone_state.PUBLISH_LEDGER = (
            self.root / "social/state/publish-ledger.jsonl"
        )
        nullone_state.TOPIC_LEDGER = (
            self.root / "social/state/topic-ledger.jsonl"
        )
        nullone_state.QUEUE = self.root / "social/state/candidate-queue.md"
        return self.root

    def __exit__(self, *exc):
        bridge_common.WORKSPACE = self._old_workspace
        bridge_common.MANIFEST_DIR = self._old_manifest_dir
        if self._old_env is None:
            os.environ.pop("NULLONE_WORKSPACE", None)
        else:
            os.environ["NULLONE_WORKSPACE"] = self._old_env
        nullone_state.PUBLISH_LEDGER = self._old_ledger
        nullone_state.TOPIC_LEDGER = self._old_topic
        nullone_state.QUEUE = self._old_queue
        self._tmp.cleanup()


@contextlib.contextmanager
def preserved_controller_state():
    """Save/restore controller + controller-owned bridge secret state."""
    saved_keys = set(controller._INSTALLED_KEYS)
    saved_token = controller._PUBLISH_TOKEN
    bridge_mod = controller._bridge_module()
    saved_provider = bridge_mod._installed_secret_provider
    try:
        yield bridge_mod
    finally:
        controller._INSTALLED_KEYS.clear()
        controller._INSTALLED_KEYS.update(saved_keys)
        controller._PUBLISH_TOKEN = saved_token
        bridge_mod._installed_secret_provider = saved_provider


def make_manifest(root: Path):
    caption_path = root / "social/drafts/pipe-caption.txt"
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_path.write_text(CAPTION_TEXT, encoding="utf-8")
    manifest = {
        "schema": bridge_common.SCHEMA,
        "manifest_id": "pipe-manifest",
        "created_at": bridge_common.now_iso(),
        "candidate_id": "candidate-pipe",
        "topic": "Pipe safety",
        "topic_cluster": "pipe-safety",
        "content_type": "NEWS",
        "format": "FEED",
        "verification": "PASS",
        "account_id": bridge_common.CANONICAL_ACCOUNT_ID,
        "caption": {
            "file": "social/drafts/pipe-caption.txt",
            "sha256": bridge_common.sha256_file(caption_path),
        },
        "media": [
            {
                "public_url": MEDIA_URL,
                "content_type": "image/png",
                "sha256": "0" * 64,
            }
        ],
        "review": {
            "create_attempts": 1,
            "state": "DRAFT_CREATED",
            "zernio_draft_id": POST_ID,
            "created_at": bridge_common.now_iso(),
        },
        "approval": {
            "first_stage": True,
            "first_stage_at": bridge_common.now_iso(),
            "final_publish": True,
            "final_publish_at": bridge_common.now_iso(),
            "source": "texbrif-approval",
            "operator": "Rauf",
            "human_confirmation": "two_step",
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
    path = root / "social/ops/manifests/pipe-manifest.json"
    atomic_write_json(path, manifest)
    return path


def remote_draft_doc():
    return {
        "post": {
            "_id": POST_ID,
            "status": "draft",
            "platforms": [
                {
                    "platform": "instagram",
                    "accountId": bridge_common.CANONICAL_ACCOUNT_ID,
                }
            ],
            "content": CAPTION_TEXT,
            "mediaItems": [{"type": "image", "url": MEDIA_URL}],
        }
    }


def readback_doc():
    doc = remote_draft_doc()
    doc["post"]["status"] = "published"
    doc["post"]["platforms"][0]["status"] = "published"
    return doc


class FakePublishTransport:
    def __init__(self, get_responses=(), put_responses=()):
        self.get_calls: list = []
        self.put_calls: list = []
        self._get = list(get_responses)
        self._put = list(put_responses)

    def get(self, path):
        self.get_calls.append(path)
        item = self._get.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def put(self, path, *, json_body):
        self.put_calls.append((path, json_body))
        item = self._put.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run_daemon(payload: bytes, root: Path):
    """Run daemon_main once against in-memory pipes; EOF ends the loop."""
    stdout = io.BytesIO()
    code = controller.daemon_main(
        stdin=io.BytesIO(payload),
        stdout=stdout,
        workspace=root,
        startup_timeout=5,
    )
    return code, stdout.getvalue()


def parse_first_frame(output: bytes, key: bytes):
    body, _consumed = ipc.verify_frame(output, key)
    return json.loads(body.decode("utf-8"))


class PipeSecretBoundaryTests(unittest.TestCase):
    def test_env_alone_cannot_publish(self):
        # The store entry name in the environment is never consulted for
        # publication auth, and a fresh bridge (nothing installed) fails
        # closed before any attempt.
        with PipeWorkspace() as root:
            manifest_path = make_manifest(root)
            bridge = load_bridge_fresh("pipe_bridge_env_test")
            with patch.dict(
                os.environ,
                {PUBLISH_SECRET_STORE_ID: PIPE_MARKER},
                clear=False,
            ):
                with self.assertRaises(BridgeError):
                    bridge.execute_loaded(
                        manifest_path,
                        json.loads(
                            manifest_path.read_text(encoding="utf-8")
                        ),
                    )
                with self.assertRaises(SecretNotConfiguredError):
                    EnvironmentSecretProvider().get_required(
                        SECRET_ID_ZERNIO_PUBLISH_BEARER
                    )
            current = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(current["publication"]["attempts"], 0)

    def test_pipe_delivers_credential_and_env_is_ignored(self):
        # Mocked resolved plugin SecretRef value crosses ONLY the private
        # pipe: even with a decoy in the environment, the installed
        # credential is exactly the piped value.
        key = ipc.generate_key()
        payload = key + ipc.encode_startup_frame(PIPE_MARKER, key)
        with PipeWorkspace() as root:
            with preserved_controller_state() as bridge_mod:
                with patch.dict(
                    os.environ,
                    {PUBLISH_SECRET_STORE_ID: "DECOY_ENV_VALUE"},
                    clear=False,
                ):
                    code, output = run_daemon(payload, root)
                self.assertEqual(code, 0)
                reply = parse_first_frame(output, key)
                self.assertEqual(reply.get("t"), "ready")
                self.assertIsInstance(
                    controller._PUBLISH_TOKEN, SecretValue
                )
                self.assertEqual(
                    controller._PUBLISH_TOKEN.reveal(), PIPE_MARKER
                )
                installed = bridge_mod._installed_secret_provider
                self.assertIsInstance(
                    installed, InMemorySecretProvider
                )
                self.assertEqual(
                    installed.get_required(
                        SECRET_ID_ZERNIO_PUBLISH_BEARER
                    ).reveal(),
                    PIPE_MARKER,
                )
                # The real factory seam serves the piped credential with
                # zero network (construction only).
                from nullone_publish_provider_factory import (
                    build_production_publish_provider,
                )

                provider = build_production_publish_provider(
                    secret_provider=installed
                )
                self.assertIsInstance(
                    provider, adapter.ZernioPublishProvider
                )
                self.assertEqual(
                    provider._transport.token.reveal(), PIPE_MARKER
                )

    def test_missing_plugin_secret_refuses_ready(self):
        # Key but EOF before any startup frame: fail closed, no READY.
        key = ipc.generate_key()
        with PipeWorkspace() as root:
            with preserved_controller_state():
                code, output = run_daemon(key, root)
                self.assertEqual(code, 2)
                self.assertEqual(output, b"")
                self.assertIsNone(controller._PUBLISH_TOKEN)

    def test_blank_startup_credential_refuses_ready(self):
        key = ipc.generate_key()
        for bad in ("", "   "):
            frame = ipc.canonical_envelope_bytes(
                {"schema": ipc.STARTUP_SCHEMA, "publish_token": bad}
            )
            header = (
                ipc.MAGIC
                + bytes((ipc.VERSION,))
                + ipc.LEN_STRUCT.pack(len(frame))
            )
            import hashlib
            import hmac

            mac = hmac.new(
                key, header + frame, hashlib.sha256
            ).digest()
            with PipeWorkspace() as root:
                with preserved_controller_state():
                    code, output = run_daemon(
                        key + header + frame + mac, root
                    )
                    self.assertEqual(code, 2, repr(bad))
                    self.assertEqual(output, b"")
                    self.assertIsNone(controller._PUBLISH_TOKEN)

    def test_tampered_startup_frame_refuses_ready(self):
        key = ipc.generate_key()
        forged = bytearray(ipc.encode_startup_frame(PIPE_MARKER, key))
        forged[-1] ^= 0x01
        with PipeWorkspace() as root:
            with preserved_controller_state():
                code, output = run_daemon(
                    key + bytes(forged), root
                )
                self.assertEqual(code, 2)
                self.assertEqual(output, b"")
                self.assertIsNone(controller._PUBLISH_TOKEN)

    def test_token_absent_from_argv_log_receipt(self):
        self.assertNotIn(PIPE_MARKER, " ".join(sys.argv))
        self.assertNotIn(PIPE_MARKER, list(os.environ.values()))
        key = ipc.generate_key()
        payload = key + ipc.encode_startup_frame(PIPE_MARKER, key)
        with PipeWorkspace() as root:
            manifest_path = make_manifest(root)
            with preserved_controller_state():
                code, output = run_daemon(payload, root)
                self.assertEqual(code, 0)
                # READY reply carries counts only, never the credential.
                reply = parse_first_frame(output, key)
                self.assertEqual(reply.get("t"), "ready")
                self.assertNotIn(
                    PIPE_MARKER, json.dumps(reply, ensure_ascii=False)
                )
                # Full fake-transport publication: stdout, manifest, and
                # both ledgers stay free of the credential.
                bridge = load_bridge_fresh("pipe_bridge_leak_test")
                transport = FakePublishTransport(
                    get_responses=[
                        (200, remote_draft_doc()),
                        (200, readback_doc()),
                    ],
                    put_responses=[(200, {"post": {"_id": POST_ID}})],
                )
                provider = adapter.ZernioPublishProvider(transport)
                buf = io.StringIO()
                with patch.object(
                    bridge, "provider_factory", return_value=provider
                ):
                    with contextlib.redirect_stdout(buf):
                        result = bridge.execute_loaded(
                            manifest_path,
                            json.loads(
                                manifest_path.read_text(encoding="utf-8")
                            ),
                        )
                self.assertEqual(result, 0)
                self.assertNotIn(PIPE_MARKER, buf.getvalue())
                self.assertNotIn(
                    PIPE_MARKER,
                    manifest_path.read_text(encoding="utf-8"),
                )
                for ledger in (
                    nullone_state.PUBLISH_LEDGER,
                    nullone_state.TOPIC_LEDGER,
                ):
                    if ledger.exists():
                        self.assertNotIn(
                            PIPE_MARKER,
                            ledger.read_text(encoding="utf-8"),
                        )

    def test_no_env_or_model_transport_in_publication_path(self):
        import re

        import nullone_publish_provider_factory as factory_mod

        # Adapter, bridge, factory, and IPC never touch the environment
        # at all (no os import for publication auth anywhere in them).
        for module in (
            adapter,
            load_bridge_fresh("pipe_bridge_scan_test"),
            factory_mod,
            ipc,
        ):
            source = inspect.getsource(module)
            self.assertNotIn("os.environ", source)
            self.assertNotIn("getenv", source)
        # The controller reads exactly one environment value: the
        # workspace root (#89, pre-existing). It never reads any
        # credential from the environment.
        controller_source = inspect.getsource(controller)
        env_gets = set(
            re.findall(r"os\.environ\.get\(\"([^\"]+)\"\)", controller_source)
        )
        self.assertEqual(env_gets, {"NULLONE_WORKSPACE"})
        self.assertNotIn("getenv", controller_source)
        self.assertNotIn("ZERNIO_", controller_source)
        code = _code_without_docstrings(adapter)
        for token in (
            "mcp__zernio",
            "nullone_claude",
            "run_structured",
            "posts_publish_now",
        ):
            self.assertNotIn(token, code)


def _code_without_docstrings(module):
    source = inspect.getsource(module)
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    removals = []

    def add_doc(node):
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            doc = node.body[0]
            removals.append((doc.lineno, doc.end_lineno))

    add_doc(tree)
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            add_doc(node)
    return "".join(
        line
        for i, line in enumerate(lines, start=1)
        if not any(start <= i <= end for start, end in removals)
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)