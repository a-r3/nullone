#!/usr/bin/env python3
"""Offline tests for the deterministic Zernio publisher connector (#90).

All tests use fake transports only. Network calls = 0. No real
credential is ever used; the fake secret marker must never appear in
argv, environment, logs, manifests, ledgers, or test output.

Covers the #90 acceptance surface:

- secret absent -> fail before publication (attempts 0, zero transport);
- plain environment string alone never satisfies the publish credential;
- mocked protected resolution delivers the secret memory-only;
- exact GET /posts/<24-hex> and PUT /posts/<24-hex> paths;
- exact minimal PUT payload {"isDraft": False, "publishNow": True};
- no redirects, no arbitrary host/path;
- remote-draft mismatches (content/order/account/target/Story) ->
  attempts 0, zero PUT;
- exact match -> exactly one PUT, attempts=1 persisted BEFORE the PUT;
- second invocation -> zero second PUT;
- timeout/malformed/5xx -> UNKNOWN, no retry;
- documented 200/207/4xx behavior incl. 207 trap and rejection terminality;
- readback clarifies truth without ever authorizing another PUT;
- no fabricated platform ids/permalinks/metadata;
- no Claude/MCP/run_structured anywhere in the publication transport.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import inspect
import io
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
import nullone_state as nullone_state  # noqa: E402
import nullone_zernio_publish_adapter as adapter  # noqa: E402
from nullone_bridge_common import (  # noqa: E402
    CANONICAL_ACCOUNT_ID,
    BridgeError,
    atomic_write_json,
)
from nullone_secret_provider import (  # noqa: E402
    ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN,
    ENV_VAR_ZERNIO_DRAFT_API_TOKEN,
    ENV_VAR_ZERNIO_PUBLISH_API_TOKEN,
    SECRET_ID_ZERNIO_ANALYTICS_BEARER,
    SECRET_ID_ZERNIO_DRAFTS_BEARER,
    SECRET_ID_ZERNIO_PUBLISH_BEARER,
    EnvironmentSecretProvider,
    SecretNotConfiguredError,
    SecretValue,
)

POST_ID = "0123456789abcdef01234567"
OTHER_ID = "fedcba987654321001234567"
SECRET_MARKER = "FAKE_ZERNIO_PUBLISH_SECRET_DO_NOT_LOG_456"
CAPTION_TEXT = "NullOne publish fixture caption.\n"
MEDIA_URL_1 = "https://example.invalid/p-0.png"
MEDIA_URL_2 = "https://example.invalid/p-1.png"


def load_bridge():
    spec = importlib.util.spec_from_file_location(
        "nullone_publish_bridge_under_test",
        SCRIPTS / "nullone-publish-bridge.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_factory():
    import nullone_publish_provider_factory as factory

    return factory


# ---------------------------------------------------------------------------
# Isolated workspace (mirrors the #89 controller tests)
# ---------------------------------------------------------------------------


class IsolatedWorkspace:
    """Isolated WORKSPACE + ledger/queue bindings for one test."""

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


def make_manifest(root: Path, fmt="FEED", media_urls=(MEDIA_URL_1,)):
    caption_path = root / "social/drafts/pub-caption.txt"
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_path.write_text(CAPTION_TEXT, encoding="utf-8")
    media = [
        {
            "public_url": url,
            "content_type": "image/png",
            "sha256": "0" * 64,
        }
        for url in media_urls
    ]
    manifest = {
        "schema": bridge_common.SCHEMA,
        "manifest_id": "pub-manifest",
        "created_at": bridge_common.now_iso(),
        "candidate_id": "candidate-pub",
        "topic": "Publisher safety",
        "topic_cluster": "pub-safety",
        "content_type": "NEWS",
        "format": fmt,
        "verification": "PASS",
        "account_id": CANONICAL_ACCOUNT_ID,
        "caption": {
            "file": "social/drafts/pub-caption.txt",
            "sha256": bridge_common.sha256_file(caption_path),
        },
        "media": media,
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
    path = root / "social/ops/manifests/pub-manifest.json"
    atomic_write_json(path, manifest)
    return path, manifest


def remote_draft_doc(
    *,
    post_id=POST_ID,
    status="draft",
    caption=CAPTION_TEXT,
    media_urls=(MEDIA_URL_1,),
    account_id=CANONICAL_ACCOUNT_ID,
    platform="instagram",
    extra_platforms=0,
    psd=None,
    include_content=True,
    include_media=True,
):
    platforms = [
        {"platform": platform, "accountId": account_id}
    ]
    if psd is not None:
        platforms[0]["platformSpecificData"] = psd
    for _ in range(extra_platforms):
        platforms.append(
            {"platform": "instagram", "accountId": account_id}
        )
    post: dict = {
        "_id": post_id,
        "status": status,
        "platforms": platforms,
    }
    if include_content:
        post["content"] = caption
    if include_media:
        post["mediaItems"] = [
            {"type": "image", "url": url} for url in media_urls
        ]
    return {"post": post}


def readback_doc(
    *,
    live_status="published",
    platform_status="published",
    post_id=POST_ID,
    platform_post_url=None,
):
    doc = remote_draft_doc(post_id=post_id, status=live_status)
    doc["post"]["platforms"][0]["status"] = platform_status
    if platform_post_url is not None:
        doc["post"]["platformPostUrl"] = platform_post_url
    return doc


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class FakePublishTransport:
    """In-memory transport recording every call, scripted responses only."""

    def __init__(self, get_responses=(), put_responses=()):
        self.get_calls: list = []
        self.put_calls: list = []
        self._get = list(get_responses)
        self._put = list(put_responses)
        self.on_get = None
        self.on_put = None

    def get(self, path):
        self.get_calls.append(path)
        if self.on_get is not None:
            self.on_get(path)
        if not self._get:
            raise AssertionError(f"unexpected GET {path!r}")
        item = self._get.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def put(self, path, *, json_body):
        self.put_calls.append((path, json_body))
        if self.on_put is not None:
            self.on_put(path)
        if not self._put:
            raise AssertionError(f"unexpected PUT {path!r}")
        item = self._put.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run_bridge(manifest_path, provider):
    """Run the real bridge core with a fake provider factory."""
    bridge = load_bridge()
    with patch.object(
        bridge, "provider_factory", return_value=provider
    ):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                code = bridge.execute_loaded(
                    manifest_path,
                    json.loads(manifest_path.read_text(encoding="utf-8")),
                )
            except BridgeError as exc:
                return bridge, "raised", str(exc), buf.getvalue()
        return bridge, code, None, buf.getvalue()


def read_manifest(manifest_path):
    return json.loads(manifest_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Secret boundary
# ---------------------------------------------------------------------------


class SecretBoundaryTests(unittest.TestCase):
    def test_secret_absent_fails_before_publication(self):
        from nullone_publish_provider_factory import (
            build_production_publish_provider,
        )

        class MissingProvider:
            def get_required(self, secret_id):
                raise SecretNotConfiguredError("not configured")

        def factory():
            return build_production_publish_provider(
                secret_provider=MissingProvider()
            )

        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(root)
            bridge = load_bridge()
            with patch.object(bridge, "provider_factory", factory):
                with self.assertRaises(BridgeError) as ctx:
                    bridge.execute_loaded(
                        manifest_path, read_manifest(manifest_path)
                    )
            self.assertIn("credential", str(ctx.exception).lower())
            current = read_manifest(manifest_path)
            self.assertEqual(current["publication"]["attempts"], 0)
            self.assertEqual(
                current["publication"]["state"], "NOT_REQUESTED"
            )

    def test_plain_env_alone_does_not_satisfy_publish_credential(self):
        with patch.dict(
            os.environ,
            {ENV_VAR_ZERNIO_PUBLISH_API_TOKEN: SECRET_MARKER},
            clear=False,
        ):
            # A raw environment string is rejected as a programming
            # defect: only a SecretValue is accepted.
            with self.assertRaises(TypeError):
                adapter.build_authenticated_transport(
                    token=os.environ[ENV_VAR_ZERNIO_PUBLISH_API_TOKEN]
                )
        # Neither the adapter nor the bridge ever reads the environment.
        for module in (
            adapter,
            load_bridge(),
            load_factory(),
        ):
            source = inspect.getsource(module)
            self.assertNotIn("os.environ", source)
            self.assertNotIn("getenv", source)

    def test_mocked_protected_resolution_reaches_adapter_memory_only(self):
        from nullone_publish_provider_factory import (
            build_production_publish_provider,
        )

        class ProtectedStoreProvider:
            """Stand-in for a protected store SecretRef resolution."""

            def get_required(self, secret_id):
                assert secret_id == SECRET_ID_ZERNIO_PUBLISH_BEARER
                return SecretValue(SECRET_MARKER)

        provider = build_production_publish_provider(
            secret_provider=ProtectedStoreProvider()
        )
        self.assertIsInstance(provider, adapter.ZernioPublishProvider)
        self.assertIsInstance(
            provider._transport.token, SecretValue
        )
        self.assertEqual(
            provider._transport.token.reveal(), SECRET_MARKER
        )
        self.assertNotIn(SECRET_MARKER, repr(provider))
        self.assertNotIn(SECRET_MARKER, repr(provider._transport))

    def test_token_absent_from_argv_env_log_receipt_output(self):
        self.assertNotIn(SECRET_MARKER, " ".join(sys.argv))
        self.assertNotIn(SECRET_MARKER, list(os.environ.values()))

        from nullone_publish_provider_factory import (
            build_production_publish_provider,
        )

        class ProtectedStoreProvider:
            def get_required(self, secret_id):
                return SecretValue(SECRET_MARKER)

        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(root)
            provider = adapter.ZernioPublishProvider(
                FakePublishTransport(
                    get_responses=[
                        (200, remote_draft_doc()),
                        (200, readback_doc()),
                    ],
                    put_responses=[
                        (200, {"post": {"_id": POST_ID}}),
                    ],
                )
            )
            # Memory-only injection through the real factory path shape.
            self.assertIsInstance(
                build_production_publish_provider(
                    secret_provider=ProtectedStoreProvider()
                )._transport.token,
                SecretValue,
            )
            _bridge, code, _err, output = run_bridge(
                manifest_path, provider
            )
            self.assertEqual(code, 0)
            self.assertNotIn(SECRET_MARKER, output)
            self.assertNotIn(
                SECRET_MARKER,
                manifest_path.read_text(encoding="utf-8"),
            )
            for ledger in (
                nullone_state.PUBLISH_LEDGER,
                nullone_state.TOPIC_LEDGER,
            ):
                if ledger.exists():
                    self.assertNotIn(
                        SECRET_MARKER,
                        ledger.read_text(encoding="utf-8"),
                    )

    def test_capability_separation_publish_distinct(self):
        self.assertNotEqual(
            SECRET_ID_ZERNIO_PUBLISH_BEARER,
            SECRET_ID_ZERNIO_ANALYTICS_BEARER,
        )
        self.assertNotEqual(
            SECRET_ID_ZERNIO_PUBLISH_BEARER,
            SECRET_ID_ZERNIO_DRAFTS_BEARER,
        )
        self.assertNotEqual(
            ENV_VAR_ZERNIO_PUBLISH_API_TOKEN,
            ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN,
        )
        self.assertNotEqual(
            ENV_VAR_ZERNIO_PUBLISH_API_TOKEN,
            ENV_VAR_ZERNIO_DRAFT_API_TOKEN,
        )
        self.assertEqual(
            EnvironmentSecretProvider.bound_env_var(
                SECRET_ID_ZERNIO_PUBLISH_BEARER
            ),
            "ZERNIO_PUBLISH_API_TOKEN",
        )
        self.assertEqual(
            len(EnvironmentSecretProvider.ENV_VAR_BY_SECRET_ID), 3
        )


# ---------------------------------------------------------------------------
# Transport allowlist: exact paths, no redirects, no arbitrary host/path
# ---------------------------------------------------------------------------


class TransportAllowlistTests(unittest.TestCase):
    def test_exact_get_and_put_paths(self):
        transport = FakePublishTransport(
            get_responses=[(200, remote_draft_doc())],
            put_responses=[(200, {"post": {"_id": POST_ID}})],
        )
        provider = adapter.ZernioPublishProvider(transport)
        expected = {
            "post_id": POST_ID,
            "caption": CAPTION_TEXT,
            "media_items": [{"type": "image", "url": MEDIA_URL_1}],
            "account_id": CANONICAL_ACCOUNT_ID,
            "format": "FEED",
        }
        provider.preflight(POST_ID, expected)
        self.assertEqual(transport.get_calls, [f"/posts/{POST_ID}"])

    def test_exact_minimal_put_payload(self):
        transport = FakePublishTransport(
            put_responses=[(200, {"post": {"_id": POST_ID}})]
        )
        provider = adapter.ZernioPublishProvider(transport)
        disposition, _status, _body = provider.promote_once(POST_ID)
        self.assertEqual(disposition, "NEEDS_READBACK")
        self.assertEqual(len(transport.put_calls), 1)
        path, payload = transport.put_calls[0]
        self.assertEqual(path, f"/posts/{POST_ID}")
        self.assertEqual(payload, {"isDraft": False, "publishNow": True})
        self.assertEqual(
            set(payload.keys()), {"isDraft", "publishNow"}
        )

    def test_no_idempotency_header_claim_in_source(self):
        code = _code_without_docstrings(adapter)
        self.assertNotIn("x-request-id", code.lower())
        self.assertNotIn("idempotency", code.lower())

    def test_real_transport_rejects_arbitrary_paths_without_network(self):
        transport = adapter.build_authenticated_transport(
            token=SecretValue(SECRET_MARKER)
        )
        for bad_path in (
            "/posts",
            "/posts/",
            "/posts/xyz",
            "/posts/0123456789abcdef0123456",
            "/posts/0123456789abcdef012345678",
            "/posts/0123456789abcdef01234567/retry",
            "/posts/0123456789abcdef0123456g",
            "/media/presign",
            "/tools/validate/post",
            "/analytics",
            "https://evil.example/posts/0123456789abcdef01234567",
        ):
            with self.assertRaises(
                adapter.PublishAdapterError, msg=bad_path
            ):
                transport.get(bad_path)
            with self.assertRaises(
                adapter.PublishAdapterError, msg=bad_path
            ):
                transport.put(
                    bad_path,
                    json_body=adapter.build_promote_payload(),
                )

    def test_real_transport_rejects_non_exact_hosts(self):
        for bad_base in (
            "http://zernio.com/api/v1",
            "https://evil.example/api/v1",
            "https://zernio.com.evil.example/api/v1",
            "https://api.zernio.com/api/v1",
        ):
            with self.assertRaises(
                adapter.PublishAdapterError, msg=bad_base
            ):
                adapter.build_authenticated_transport(
                    token=SecretValue(SECRET_MARKER),
                    base_url=bad_base,
                )
        self.assertEqual(
            adapter.DEFAULT_BASE_URL, "https://zernio.com/api/v1"
        )

    def test_redirects_refused_never_followed(self):
        handler = adapter._NoRedirectHandler()
        with self.assertRaises(adapter.PublishRedirectRefusedError):
            handler.redirect_request(
                object(),
                object(),
                302,
                "Found",
                {},
                "https://evil.example/",
            )
        source = inspect.getsource(adapter.UrllibPublishTransport)
        self.assertIn("_NoRedirectHandler", source)
        self.assertNotIn("urllib_request.urlopen", source)

    def test_transport_has_no_post_delete_capability(self):
        for name in ("post", "delete", "patch", "upload"):
            self.assertFalse(
                hasattr(adapter.UrllibPublishTransport, name)
            )
        provider_methods = [
            name
            for name in dir(adapter.ZernioPublishProvider)
            if not name.startswith("_")
        ]
        self.assertEqual(
            provider_methods, ["preflight", "promote_once", "readback"]
        )


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
            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            add_doc(node)
    return "".join(
        line
        for i, line in enumerate(lines, start=1)
        if not any(start <= i <= end for start, end in removals)
    )


# ---------------------------------------------------------------------------
# Remote-draft preflight mismatches -> attempts 0, zero PUT
# ---------------------------------------------------------------------------


class RemoteDraftPreflightTests(unittest.TestCase):
    def _run_mismatch(self, remote_body, fmt="FEED",
                      media_urls=(MEDIA_URL_1,)):
        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(
                root, fmt=fmt, media_urls=media_urls
            )
            transport = FakePublishTransport(
                get_responses=[(200, remote_body)]
            )
            provider = adapter.ZernioPublishProvider(transport)
            _bridge, result, _err, _out = run_bridge(
                manifest_path, provider
            )
            current = read_manifest(manifest_path)
            return result, current, transport

    def _assert_blocked(self, result, current, transport):
        self.assertEqual(result, "raised")
        self.assertEqual(current["publication"]["attempts"], 0)
        self.assertEqual(
            current["publication"]["state"], "NOT_REQUESTED"
        )
        self.assertEqual(transport.put_calls, [])
        self.assertEqual(len(transport.get_calls), 1)

    def test_remote_content_mismatch_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(caption="Different caption.\n")
        )
        self._assert_blocked(result, current, transport)

    def test_media_order_mismatch_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(media_urls=(MEDIA_URL_2, MEDIA_URL_1)),
            media_urls=(MEDIA_URL_1, MEDIA_URL_2),
        )
        self._assert_blocked(result, current, transport)

    def test_media_url_mismatch_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(media_urls=("https://example.invalid/x.png",))
        )
        self._assert_blocked(result, current, transport)

    def test_account_mismatch_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(account_id="0" * 24)
        )
        self._assert_blocked(result, current, transport)

    def test_extra_publication_target_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(extra_platforms=1)
        )
        self._assert_blocked(result, current, transport)

    def test_wrong_platform_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(platform="tiktok")
        )
        self._assert_blocked(result, current, transport)

    def test_non_draft_remote_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(status="published")
        )
        self._assert_blocked(result, current, transport)

    def test_remote_id_mismatch_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(post_id=OTHER_ID)
        )
        self._assert_blocked(result, current, transport)

    def test_story_content_type_mismatch_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(psd={"contentType": "post"}),
            fmt="STORY",
        )
        self._assert_blocked(result, current, transport)

    def test_feed_with_story_remote_blocks(self):
        result, current, transport = self._run_mismatch(
            remote_draft_doc(psd={"contentType": "story"}),
            fmt="FEED",
        )
        self._assert_blocked(result, current, transport)

    def test_malformed_remote_body_blocks(self):
        for bad in (None, "nope", [], {"nope": True}, {"post": []}):
            result, current, transport = self._run_mismatch(bad)
            self._assert_blocked(result, current, transport)

    def test_transport_redirect_during_preflight_blocks(self):
        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(root)
            transport = FakePublishTransport(
                get_responses=[
                    adapter.PublishRedirectRefusedError("refused")
                ]
            )
            provider = adapter.ZernioPublishProvider(transport)
            _bridge, result, _err, _out = run_bridge(
                manifest_path, provider
            )
            current = read_manifest(manifest_path)
            self.assertEqual(result, "raised")
            self.assertEqual(current["publication"]["attempts"], 0)
            self.assertEqual(
                current["publication"]["state"], "NOT_REQUESTED"
            )
            self.assertEqual(transport.put_calls, [])

    def test_story_match_with_exposed_settings_passes_preflight(self):
        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(root, fmt="STORY")
            transport = FakePublishTransport(
                get_responses=[
                    (
                        200,
                        remote_draft_doc(
                            psd={"contentType": "story"}
                        ),
                    ),
                    (200, readback_doc()),
                ],
                put_responses=[(200, {"post": {"_id": POST_ID}})],
            )
            provider = adapter.ZernioPublishProvider(transport)
            _bridge, code, _err, _out = run_bridge(
                manifest_path, provider
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(transport.put_calls), 1)


# ---------------------------------------------------------------------------
# Exact match -> one PUT, attempts=1 persisted BEFORE the PUT
# ---------------------------------------------------------------------------


class ExactMatchPublishTests(unittest.TestCase):
    def test_exact_match_issues_one_put(self):
        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(root)
            transport = FakePublishTransport(
                get_responses=[
                    (200, remote_draft_doc()),
                    (200, readback_doc()),
                ],
                put_responses=[(200, {"post": {"_id": POST_ID}})],
            )
            provider = adapter.ZernioPublishProvider(transport)
            _bridge, code, _err, _out = run_bridge(
                manifest_path, provider
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(transport.put_calls), 1)
            path, payload = transport.put_calls[0]
            self.assertEqual(path, f"/posts/{POST_ID}")
            self.assertEqual(
                payload, {"isDraft": False, "publishNow": True}
            )
            current = read_manifest(manifest_path)
            self.assertEqual(current["publication"]["attempts"], 1)
            self.assertEqual(
                current["publication"]["state"], "PUBLISHED"
            )
            self.assertEqual(
                current["publication"]["live_zernio_post_id"], POST_ID
            )

    def test_attempts_persisted_before_put_and_get_before_attempts(self):
        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(root)
            transport = FakePublishTransport(
                get_responses=[
                    (200, remote_draft_doc()),
                    (200, readback_doc()),
                ],
                put_responses=[(200, {"post": {"_id": POST_ID}})],
            )
            observed = {}

            def on_get(_path):
                # Only the preflight GET proves attempts==0: the later
                # readback GET runs after the attempt is consumed.
                if "at_get" not in observed:
                    observed["at_get"] = (
                        read_manifest(manifest_path)["publication"][
                            "attempts"
                        ]
                    )

            def on_put(_path):
                snap = read_manifest(manifest_path)["publication"]
                observed["at_put"] = (snap["attempts"], snap["state"])

            transport.on_get = on_get
            transport.on_put = on_put
            provider = adapter.ZernioPublishProvider(transport)
            _bridge, code, _err, _out = run_bridge(
                manifest_path, provider
            )
            self.assertEqual(code, 0)
            self.assertEqual(observed["at_get"], 0)
            self.assertEqual(
                observed["at_put"], (1, "PUBLISH_IN_FLIGHT")
            )

    def test_second_invocation_issues_zero_second_put(self):
        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(root)
            transport = FakePublishTransport(
                get_responses=[
                    (200, remote_draft_doc()),
                    (200, readback_doc()),
                ],
                put_responses=[(200, {"post": {"_id": POST_ID}})],
            )
            provider = adapter.ZernioPublishProvider(transport)
            bridge = load_bridge()
            with patch.object(
                bridge, "provider_factory", return_value=provider
            ):
                code = bridge.execute_loaded(
                    manifest_path, read_manifest(manifest_path)
                )
                self.assertEqual(code, 0)
                with self.assertRaises(BridgeError):
                    bridge.execute_loaded(
                        manifest_path, read_manifest(manifest_path)
                    )
            self.assertEqual(len(transport.put_calls), 1)
            current = read_manifest(manifest_path)
            self.assertEqual(current["publication"]["attempts"], 1)


# ---------------------------------------------------------------------------
# PUT response matrix: 200 / 207 / 4xx / 5xx / timeout / malformed
# ---------------------------------------------------------------------------


class PutResponseMatrixTests(unittest.TestCase):
    def _run(
        self,
        put_responses,
        readback,
        fmt="FEED",
        media_urls=(MEDIA_URL_1,),
    ):
        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(
                root, fmt=fmt, media_urls=media_urls
            )
            get_responses = [(200, remote_draft_doc())]
            if readback is not None:
                get_responses.append(readback)
            transport = FakePublishTransport(
                get_responses=get_responses,
                put_responses=put_responses,
            )
            provider = adapter.ZernioPublishProvider(transport)
            _bridge, result, _err, _out = run_bridge(
                manifest_path, provider
            )
            return result, read_manifest(manifest_path), transport

    def test_documented_200_with_published_readback(self):
        result, current, transport = self._run(
            [(200, {"post": {"_id": POST_ID, "status": "draft"}})],
            (200, readback_doc()),
        )
        self.assertEqual(result, 0)
        self.assertEqual(current["publication"]["state"], "PUBLISHED")
        self.assertEqual(len(transport.put_calls), 1)

    def test_200_but_readback_still_draft_is_check_required(self):
        result, current, transport = self._run(
            [(200, {"post": {"_id": POST_ID}})],
            (200, remote_draft_doc()),
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            current["publication"]["state"], "CHECK_REQUIRED"
        )
        self.assertEqual(len(transport.put_calls), 1)

    def test_documented_207_partial_never_direct_published(self):
        result, current, transport = self._run(
            [
                (
                    207,
                    {
                        "post": {
                            "_id": POST_ID,
                            "status": "partial",
                            "platforms": [
                                {
                                    "platform": "instagram",
                                    "accountId": CANONICAL_ACCOUNT_ID,
                                }
                            ],
                        }
                    },
                )
            ],
            (200, readback_doc(live_status="failed")),
        )
        self.assertEqual(result, 0)
        # 207 alone proves nothing; the failed readback decides.
        self.assertEqual(current["publication"]["state"], "FAILED")
        self.assertEqual(len(transport.put_calls), 1)

    def test_documented_207_scheduled_maps_publishing_family(self):
        result, current, transport = self._run(
            [
                (
                    207,
                    {
                        "post": {
                            "_id": POST_ID,
                            "status": "scheduled",
                            "platforms": [
                                {
                                    "platform": "instagram",
                                    "accountId": CANONICAL_ACCOUNT_ID,
                                }
                            ],
                        }
                    },
                )
            ],
            (200, readback_doc(live_status="scheduled",
                               platform_status="")),
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            current["publication"]["state"], "PUBLISHING"
        )
        self.assertEqual(len(transport.put_calls), 1)

    def test_documented_4xx_is_terminal_failed(self):
        for code in (400, 401, 403, 404, 409):
            result, current, transport = self._run(
                [(code, {"error": "rejected", "code": code})],
                (200, remote_draft_doc()),
            )
            self.assertEqual(result, 0, f"status {code}")
            self.assertEqual(
                current["publication"]["state"], "FAILED", f"status {code}"
            )
            self.assertEqual(current["publication"]["attempts"], 1)
            self.assertEqual(len(transport.put_calls), 1)

    def test_rejection_stands_when_readback_confirms_draft(self):
        # A definite rejection is not softened by a readback that merely
        # confirms the un-published draft.
        result, current, transport = self._run(
            [(400, {"error": "queue_slot_conflict"})],
            (200, remote_draft_doc()),
        )
        self.assertEqual(result, 0)
        self.assertEqual(current["publication"]["state"], "FAILED")

    def test_4xx_malformed_envelope_is_unknown(self):
        result, current, transport = self._run(
            [(400, None)],
            (503, None),
        )
        self.assertEqual(result, "raised")
        self.assertEqual(current["publication"]["state"], "UNKNOWN")
        self.assertEqual(len(transport.put_calls), 1)

    def test_5xx_is_unknown_no_retry(self):
        result, current, transport = self._run(
            [(503, {"error": "upstream"})],
            (503, None),
        )
        self.assertEqual(result, "raised")
        self.assertEqual(current["publication"]["state"], "UNKNOWN")
        self.assertEqual(current["publication"]["attempts"], 1)
        self.assertEqual(len(transport.put_calls), 1)

    def test_timeout_is_unknown_no_retry(self):
        result, current, transport = self._run(
            [TimeoutError("timed out")],
            TimeoutError("timed out"),
        )
        self.assertEqual(result, "raised")
        self.assertEqual(current["publication"]["state"], "UNKNOWN")
        self.assertEqual(len(transport.put_calls), 1)
        # Preflight GET + readback GET, nothing else.
        self.assertEqual(len(transport.get_calls), 2)

    def test_malformed_put_response_is_unknown_no_retry(self):
        result, current, transport = self._run(
            [(200, "not-a-dict")],
            (200, None),
        )
        self.assertEqual(result, "raised")
        self.assertEqual(current["publication"]["state"], "UNKNOWN")
        self.assertEqual(len(transport.put_calls), 1)

    def test_unexpected_2xx_is_unknown_no_retry(self):
        result, current, transport = self._run(
            [(204, None)],
            (200, None),
        )
        self.assertEqual(result, "raised")
        self.assertEqual(current["publication"]["state"], "UNKNOWN")
        self.assertEqual(len(transport.put_calls), 1)

    def test_readback_clarifies_without_retry(self):
        # Ambiguous PUT (timeout) that actually published: readback
        # proves PUBLISHED with still exactly one PUT.
        result, current, transport = self._run(
            [TimeoutError("timed out")],
            (200, readback_doc()),
        )
        self.assertEqual(result, 0)
        self.assertEqual(current["publication"]["state"], "PUBLISHED")
        self.assertEqual(len(transport.put_calls), 1)

    def test_readback_failure_after_accept_is_readback_failed(self):
        result, current, transport = self._run(
            [(200, {"post": {"_id": POST_ID}})],
            (503, None),
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            current["publication"]["state"], "READBACK_FAILED"
        )
        self.assertEqual(len(transport.put_calls), 1)

    def test_provider_level_dispositions(self):
        transport = FakePublishTransport(
            put_responses=[
                (200, {"post": {}}),
                (207, {"post": {"status": "partial"}}),
                (400, {"error": "nope"}),
            ]
        )
        provider = adapter.ZernioPublishProvider(transport)
        self.assertEqual(
            provider.promote_once(POST_ID)[0], "NEEDS_READBACK"
        )
        self.assertEqual(
            provider.promote_once(POST_ID)[0], "NEEDS_READBACK"
        )
        self.assertEqual(
            provider.promote_once(POST_ID)[0], "REJECTED"
        )
        transport2 = FakePublishTransport(
            put_responses=[(500, None)]
        )
        provider2 = adapter.ZernioPublishProvider(transport2)
        with self.assertRaises(adapter.PublishAmbiguousError):
            provider2.promote_once(POST_ID)


# ---------------------------------------------------------------------------
# No fabricated metadata
# ---------------------------------------------------------------------------


class NoFabricatedMetadataTests(unittest.TestCase):
    def test_absent_ids_stay_empty(self):
        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(root)
            transport = FakePublishTransport(
                get_responses=[
                    (200, remote_draft_doc()),
                    (200, readback_doc()),
                ],
                put_responses=[(200, {"post": {"_id": POST_ID}})],
            )
            provider = adapter.ZernioPublishProvider(transport)
            _bridge, code, _err, _out = run_bridge(
                manifest_path, provider
            )
            self.assertEqual(code, 0)
            current = read_manifest(manifest_path)
            self.assertEqual(
                current["publication"]["state"], "PUBLISHED"
            )
            self.assertIsNone(
                current["publication"]["platform_post_id"]
            )
            self.assertIsNone(current["publication"]["permalink"])
            self.assertIsNone(current["publication"]["error"])

    def test_documented_platform_post_url_is_copied_only_when_present(self):
        with IsolatedWorkspace() as root:
            manifest_path, _m = make_manifest(root)
            transport = FakePublishTransport(
                get_responses=[
                    (200, remote_draft_doc()),
                    (
                        200,
                        readback_doc(
                            platform_post_url=(
                                "https://example.invalid/p/123"
                            )
                        ),
                    ),
                ],
                put_responses=[(200, {"post": {"_id": POST_ID}})],
            )
            provider = adapter.ZernioPublishProvider(transport)
            _bridge, code, _err, _out = run_bridge(
                manifest_path, provider
            )
            self.assertEqual(code, 0)
            current = read_manifest(manifest_path)
            self.assertEqual(
                current["publication"]["permalink"],
                "https://example.invalid/p/123",
            )
            # Still never fabricated: no documented platform id field.
            self.assertIsNone(
                current["publication"]["platform_post_id"]
            )


# ---------------------------------------------------------------------------
# Capability-negative: no Claude/MCP in the publication transport
# ---------------------------------------------------------------------------


class PublicationTransportCapabilityNegativeTests(unittest.TestCase):
    MODULES = (
        adapter,
        load_bridge(),
        load_factory(),
    )

    def test_no_model_transport_tokens(self):
        for module in self.MODULES:
            code = _code_without_docstrings(module)
            for token in (
                "mcp__zernio",
                "nullone_claude",
                "run_structured",
                "claude -p",
                "Claude",
                "posts_publish_now",
            ):
                self.assertNotIn(
                    token, code, f"{module.__name__}: {token}"
                )

    def test_bridge_does_not_import_model_transport(self):
        bridge = load_bridge()
        source = inspect.getsource(bridge)
        self.assertNotIn("nullone_claude", source)
        self.assertNotIn("run_structured", source)

    def test_controller_still_delegates_to_deterministic_core(self):
        import nullone_final_publish_controller as controller

        self.assertIsNotNone(controller.bridge_core)
        self.assertTrue(
            callable(controller.bridge_core),
            "controller must keep an injectable deterministic core",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
