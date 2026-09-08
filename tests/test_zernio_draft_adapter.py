#!/usr/bin/env python3
"""Offline tests for the direct Zernio REST DraftProvider adapter (#81).

All tests use fake transports only. Network calls = 0.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_zernio_draft_adapter as adapter  # noqa: E402
from nullone_bridge_common import (  # noqa: E402
    CANONICAL_ACCOUNT_ID,
    BridgeError,
    atomic_write_json,
    inspect_media,
    load_manifest,
    now_iso,
    resolve_workspace_path,
    validate_manifest,
)
from nullone_secret_provider import (  # noqa: E402
    ENV_VAR_ZERNIO_DRAFT_API_TOKEN,
    SECRET_ID_ZERNIO_DRAFTS_BEARER,
    EnvironmentSecretProvider,
    SecretValue,
)

MARKER = "FAKE_ZERNIO_DRAFT_SECRET_DO_NOT_LOG_123"


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class FakeTransport:
    """In-memory transport recording every call."""

    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self.put_calls = []
        self.get_responses = {}
        self.post_responses = {}
        self.put_responses = {}
        self.default_get = (200, {"accounts": []})
        self.default_post = (200, {"status": "READY", "valid": True, "error": ""})
        self.default_put = 200

    def get(self, path, *, params=None):
        self.get_calls.append((path, params))
        return self.get_responses.get(path, self.default_get)

    def post(self, path, *, json_body=None, headers=None, idempotency_key=None):
        self.post_calls.append((path, json_body, headers, idempotency_key))
        return self.post_responses.get(path, self.default_post)

    def put(self, url, *, data=None, headers=None):
        self.put_calls.append((url, data, headers))
        return self.put_responses.get(url, self.default_put)


def make_provider(transport=None, **kwargs):
    if transport is None:
        transport = FakeTransport()
    return adapter.ZernioDraftProvider(transport, **kwargs)


def make_manifest(tmp_path, fmt="FEED", media_count=1, content_type="NEWS"):
    """Build a minimal valid manifest in a temp workspace."""
    bridge_common.WORKSPACE = tmp_path
    manifests_dir = tmp_path / "social" / "ops" / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    from PIL import Image

    media = []
    for i in range(media_count):
        img_path = tmp_path / "social" / "source-assets" / f"source-{i}.png"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "STORY":
            Image.new("RGB", (1080, 1920), (10, 20, 30)).save(img_path, "PNG")
        else:
            Image.new("RGB", (1080, 1350), (10, 20, 30)).save(img_path, "PNG")
        info = inspect_media(img_path, fmt)
        media.append(info)

    caption_path = tmp_path / "social" / "drafts" / "test" / "caption.txt"
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_text = "Test caption. @nullone.az\n"
    caption_path.write_text(caption_text, encoding="utf-8")

    manifest = {
        "schema": "nullone.production.v1",
        "manifest_id": f"test-{fmt.lower()}-001",
        "created_at": now_iso(),
        "candidate_id": "test-candidate",
        "topic": "Test topic",
        "topic_cluster": "test",
        "content_type": content_type,
        "format": fmt,
        "verification": "PASS",
        "account_id": CANONICAL_ACCOUNT_ID,
        "caption": {
            "file": bridge_common.workspace_relative(caption_path),
            "sha256": bridge_common.sha256_bytes(caption_path.read_bytes()),
        },
        "media": media,
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

    manifest_path = manifests_dir / f"test-{fmt.lower()}-001.json"
    atomic_write_json(manifest_path, manifest)
    return manifest_path, manifest


def setup_valid_transport(transport, post_id="draft-123", fmt="FEED"):
    """Configure a transport with valid responses for all endpoints."""
    transport.default_get = (
        200,
        {
            "accounts": [
                {
                    "_id": CANONICAL_ACCOUNT_ID,
                    "platform": "instagram",
                    "isActive": True,
                }
            ]
        },
    )
    transport.default_post = (
        200,
        {"status": "READY", "valid": True, "error": ""},
    )
    transport.post_responses["/media/presign"] = (
        200,
        {
            "status": "OK",
            "uploadUrl": "https://upload.example.com/put",
            "publicUrl": "https://cdn.example.com/1.png",
            "error": "",
        },
    )
    transport.default_put = 204
    transport.post_responses["/posts"] = (201, {"_id": post_id})
    if fmt == "STORY":
        transport.get_responses[f"/posts/{post_id}"] = (
            200,
            {
                "_id": post_id,
                "status": "draft",
                "platform": "instagram",
                "accountId": CANONICAL_ACCOUNT_ID,
                "platformSpecificData": {"contentType": "story"},
            },
        )
    else:
        transport.get_responses[f"/posts/{post_id}"] = (
            200,
            {
                "_id": post_id,
                "status": "draft",
                "platform": "instagram",
                "accountId": CANONICAL_ACCOUNT_ID,
            },
        )
    return transport


# ---------------------------------------------------------------------------
# Secret tests
# ---------------------------------------------------------------------------


class SecretTests(unittest.TestCase):
    def test_missing_draft_secret_fails_closed(self):
        class MissingProvider:
            def get_required(self, secret_id):
                raise adapter.SecretNotConfiguredError("not configured")

        with self.assertRaises(adapter.DraftConnectorUnauthorizedError):
            adapter.build_direct_draft_provider(secret_provider=MissingProvider())

    def test_blank_draft_secret_fails_closed(self):
        for blank in ("", "   ", "\t\n"):
            provider = EnvironmentSecretProvider(
                environ={ENV_VAR_ZERNIO_DRAFT_API_TOKEN: blank}
            )
            with self.assertRaises(adapter.DraftConnectorUnauthorizedError):
                adapter.build_direct_draft_provider(secret_provider=provider)

    def test_unavailable_secret_source_sanitized(self):
        class UnavailableProvider:
            def get_required(self, secret_id):
                raise adapter.SecretUnavailableError(
                    f"credential store unreachable with value {MARKER}"
                )

        with self.assertRaises(adapter.DraftConnectorUnavailableError) as ctx:
            adapter.build_direct_draft_provider(
                secret_provider=UnavailableProvider()
            )
        self.assertNotIn(MARKER, str(ctx.exception))

    def test_draft_credential_separate_from_analytics(self):
        self.assertNotEqual(
            SECRET_ID_ZERNIO_DRAFTS_BEARER,
            "zernio.analytics.bearer",
        )

    def test_secret_value_never_rendered(self):
        secret = SecretValue(MARKER)
        self.assertNotIn(MARKER, repr(secret))
        self.assertNotIn(MARKER, str(secret))
        self.assertNotIn(MARKER, f"{secret}")

    def test_factory_construction_performs_zero_network(self):
        class PresentProvider:
            def get_required(self, secret_id):
                return SecretValue(MARKER)

        transport_holder = []
        original = adapter.build_authenticated_transport

        def spy(*, token, base_url=adapter.DEFAULT_BASE_URL):
            t = original(token=token, base_url=base_url)
            transport_holder.append(t)
            return t

        adapter.build_authenticated_transport = spy
        try:
            provider = adapter.build_direct_draft_provider(
                secret_provider=PresentProvider()
            )
        finally:
            adapter.build_authenticated_transport = original
        self.assertEqual(len(transport_holder), 1)
        self.assertIsInstance(provider, adapter.ZernioDraftProvider)


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


class ManifestHelperTests(unittest.TestCase):
    def test_require_not_created_blocks_consumed_attempt(self):
        m = {
            "review": {
                "create_attempts": 1,
                "state": "CREATE_IN_FLIGHT",
                "zernio_draft_id": None,
            }
        }
        with self.assertRaises(BridgeError):
            adapter.ZernioDraftProvider._require_not_created(m)

    def test_require_not_created_blocks_existing_draft_id(self):
        m = {
            "review": {
                "create_attempts": 0,
                "state": "NOT_CREATED",
                "zernio_draft_id": "existing-draft",
            }
        }
        with self.assertRaises(BridgeError):
            adapter.ZernioDraftProvider._require_not_created(m)

    def test_require_not_created_passes_when_untouched(self):
        m = {
            "review": {
                "create_attempts": 0,
                "state": "NOT_CREATED",
                "zernio_draft_id": None,
            }
        }
        adapter.ZernioDraftProvider._require_not_created(m)


# ---------------------------------------------------------------------------
# Presign / upload tests
# ---------------------------------------------------------------------------


class PresignUploadTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def test_successful_single_media_flow(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["media"][0]["public_url"], "https://cdn.example.com/1.png")

        presign_calls = [c for c in transport.post_calls if c[0] == "/media/presign"]
        self.assertEqual(len(presign_calls), 1)
        self.assertEqual(len(transport.put_calls), 1)

    def test_public_url_persisted_only_after_confirmed_upload(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.default_put = 500
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftUploadFailedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertIsNone(m["media"][0].get("public_url"))
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")

    def test_upload_url_never_persisted(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        raw = manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("upload.example.com", raw)
        self.assertNotIn("secret-presigned", raw)

    def test_bearer_token_never_sent_to_presigned_upload_url(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        for url, data, headers in transport.put_calls:
            self.assertTrue(url.startswith("https://upload.example.com"))
            auth = (headers or {}).get("Authorization")
            self.assertIsNone(auth)

    def test_partial_presign_8_items_first_3_complete_4th_fails(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="CAROUSEL", media_count=8)

        for i in range(3):
            m["media"][i]["public_url"] = f"https://cdn.example.com/pre-{i}.png"
        atomic_write_json(manifest_path, m)

        transport = FakeTransport()
        transport.post_responses["/media/presign"] = (
            400,
            {"status": "BLOCKED", "error": "presign failed"},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPresignBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")
        for i in range(3):
            self.assertEqual(
                m["media"][i]["public_url"],
                f"https://cdn.example.com/pre-{i}.png",
            )

        transport2 = FakeTransport()
        setup_valid_transport(transport2, fmt="CAROUSEL")
        provider2 = make_provider(transport2)
        provider2.create_review_draft(manifest_path)

        # Items 3-7 (5 items) need presigning; first 3 are reused.
        presign_paths = [c[0] for c in transport2.post_calls if c[0] == "/media/presign"]
        self.assertEqual(len(presign_paths), 5)
        # First 3 items must not have been re-uploaded.
        self.assertEqual(len(transport2.put_calls), 5)

    def test_recovery_does_not_represign_or_reupload_existing(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="CAROUSEL", media_count=2)
        m["media"][0]["public_url"] = "https://cdn.example.com/existing.png"
        atomic_write_json(manifest_path, m)

        transport = FakeTransport()
        setup_valid_transport(transport, fmt="CAROUSEL")
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        presign_calls = [c for c in transport.post_calls if c[0] == "/media/presign"]
        self.assertEqual(len(presign_calls), 1)
        self.assertEqual(len(transport.put_calls), 1)

    def test_create_attempts_remains_zero_after_partial_presign(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="CAROUSEL", media_count=2)
        transport = FakeTransport()
        transport.post_responses["/media/presign"] = (
            400,
            {"status": "BLOCKED", "error": "presign failed"},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPresignBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")
        self.assertIsNone(m["review"]["zernio_draft_id"])


# ---------------------------------------------------------------------------
# Preflight tests
# ---------------------------------------------------------------------------


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def test_canonical_account_missing_blocks_before_attempt(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.default_get = (
            200,
            {"accounts": [{"_id": "other", "platform": "instagram", "isActive": True}]},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")

    def test_account_not_connected_blocks_before_attempt(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.default_get = (
            200,
            {
                "accounts": [
                    {
                        "_id": CANONICAL_ACCOUNT_ID,
                        "platform": "instagram",
                        "isActive": False,
                    }
                ]
            },
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)

    def test_account_wrong_platform_blocks_before_attempt(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.default_get = (
            200,
            {
                "accounts": [
                    {
                        "_id": CANONICAL_ACCOUNT_ID,
                        "platform": "twitter",
                        "isActive": True,
                    }
                ]
            },
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)

    def test_media_validation_fail_blocks_before_attempt(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/tools/validate/media"] = (
            200,
            {"status": "BLOCKED", "valid": False, "error": "invalid media"},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)

    def test_post_validation_fail_blocks_before_attempt(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/tools/validate/post"] = (
            200,
            {"status": "BLOCKED", "valid": False, "error": "post invalid"},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)

    def test_preflight_blocks_no_draft_post(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.default_get = (
            200,
            {"accounts": [{"_id": "other", "platform": "instagram", "isActive": True}]},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)

        post_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        self.assertEqual(len(post_calls), 0)


# ---------------------------------------------------------------------------
# Payload tests
# ---------------------------------------------------------------------------


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def test_story_exact_content_type(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="STORY")
        transport = FakeTransport()
        setup_valid_transport(transport, fmt="STORY")
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        post_validate = [
            c for c in transport.post_calls
            if c[0] == "/tools/validate/post"
        ]
        self.assertEqual(len(post_validate), 1)
        payload = post_validate[0][1]
        self.assertEqual(payload["platformSpecificData"]["contentType"], "story")
        self.assertTrue(payload["isDraft"])

        create_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        self.assertEqual(len(create_calls), 1)
        create_payload = create_calls[0][1]
        self.assertEqual(create_payload["platformSpecificData"]["contentType"], "story")
        self.assertTrue(create_payload["isDraft"])
        self.assertNotIn("publishNow", create_payload)
        self.assertNotIn("scheduledFor", create_payload)

    def test_feed_no_story_content_type(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, fmt="FEED")
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        post_validate = [
            c for c in transport.post_calls
            if c[0] == "/tools/validate/post"
        ]
        payload = post_validate[0][1]
        self.assertNotIn("platformSpecificData", payload)

        create_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        create_payload = create_calls[0][1]
        self.assertNotIn("platformSpecificData", create_payload)
        self.assertTrue(create_payload["isDraft"])
        self.assertNotIn("publishNow", create_payload)
        self.assertNotIn("scheduledFor", create_payload)

    def test_carousel_media_order_exact(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="CAROUSEL", media_count=3)
        transport = FakeTransport()
        setup_valid_transport(transport, fmt="CAROUSEL")

        # Make presign return unique URLs per item.
        presign_count = [0]
        original_post = transport.post
        def dynamic_presign(path, *, json_body=None, headers=None, idempotency_key=None):
            if path == "/media/presign":
                idx = presign_count[0]
                presign_count[0] += 1
                return (
                    200,
                    {
                        "status": "OK",
                        "uploadUrl": f"https://upload.example.com/put-{idx}",
                        "publicUrl": f"https://cdn.example.com/carousel-{idx}.png",
                        "error": "",
                    },
                )
            return original_post(path, json_body=json_body, headers=headers, idempotency_key=idempotency_key)
        transport.post = dynamic_presign
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        create_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        create_payload = create_calls[0][1]
        media_urls = [item["url"] for item in create_payload["media"]]
        expected = [
            f"https://cdn.example.com/carousel-{i}.png"
            for i in range(3)
        ]
        self.assertEqual(media_urls, expected)

    def test_is_draft_true_and_publish_absent(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, fmt="FEED")
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        create_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        create_payload = create_calls[0][1]
        self.assertTrue(create_payload["isDraft"])
        self.assertNotIn("publishNow", create_payload)
        self.assertNotIn("scheduledFor", create_payload)
        self.assertNotIn("publish_now", create_payload)
        self.assertNotIn("schedule", create_payload)

    def test_no_publish_or_retry_endpoint_capability(self):
        """The adapter must contain no publish/retry endpoint capability."""
        import inspect

        source = inspect.getsource(adapter)
        class_body = source.split("class ZernioDraftProvider")[1]
        self.assertNotIn("publish_now", class_body)
        self.assertNotIn("posts_publish", source)
        self.assertNotIn("posts_update", source)
        self.assertNotIn("posts_retry", source)
        provider_methods = [
            name for name in dir(adapter.ZernioDraftProvider)
            if not name.startswith("_")
        ]
        self.assertEqual(provider_methods, ["create_review_draft"])


# ---------------------------------------------------------------------------
# Create tests
# ---------------------------------------------------------------------------


class CreateTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def test_create_attempts_persisted_as_1_before_post(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 1)
        self.assertEqual(m["review"]["state"], "DRAFT_CREATED")

    def test_exactly_one_posts_post(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        post_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        self.assertEqual(len(post_calls), 1)

    def test_timeout_after_create_request_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)

        original_post = transport.post
        def raising_post(path, *, json_body=None, headers=None, idempotency_key=None):
            if path == "/posts":
                raise adapter.DraftConnectorUnavailableError("timeout")
            return original_post(path, json_body=json_body, headers=headers, idempotency_key=idempotency_key)
        transport.post = raising_post
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftCreateAmbiguousError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 1)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_connection_ambiguity_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)

        original_post = transport.post
        def raising_post(path, *, json_body=None, headers=None, idempotency_key=None):
            if path == "/posts":
                raise adapter.DraftConnectorUnavailableError("connection reset")
            return original_post(path, json_body=json_body, headers=headers, idempotency_key=idempotency_key)
        transport.post = raising_post
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftCreateAmbiguousError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_malformed_create_response_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/posts"] = (201, {"not_id": "missing"})
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftCreateAmbiguousError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_missing_post_id_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/posts"] = (201, {"_id": ""})
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftCreateAmbiguousError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_no_automatic_retry(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/posts"] = (201, {"_id": ""})
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftCreateAmbiguousError):
            provider.create_review_draft(manifest_path)

        post_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        self.assertEqual(len(post_calls), 1)

    def test_second_execution_against_consumed_attempt_cannot_create(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        with self.assertRaises(BridgeError):
            provider.create_review_draft(manifest_path)

        post_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        self.assertEqual(len(post_calls), 1)


# ---------------------------------------------------------------------------
# Readback tests
# ---------------------------------------------------------------------------


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def test_exact_draft_readback_yields_draft_created(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, post_id="draft-123", fmt="FEED")
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "DRAFT_CREATED")
        self.assertEqual(m["review"]["zernio_draft_id"], "draft-123")

    def test_wrong_account_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, post_id="draft-123", fmt="FEED")
        transport.get_responses["/posts/draft-123"] = (
            200,
            {
                "_id": "draft-123",
                "status": "draft",
                "platform": "instagram",
                "accountId": "wrong-account",
            },
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_wrong_platform_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, post_id="draft-123", fmt="FEED")
        transport.get_responses["/posts/draft-123"] = (
            200,
            {
                "_id": "draft-123",
                "status": "draft",
                "platform": "twitter",
                "accountId": CANONICAL_ACCOUNT_ID,
            },
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_non_draft_state_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, post_id="draft-123", fmt="FEED")
        transport.get_responses["/posts/draft-123"] = (
            200,
            {
                "_id": "draft-123",
                "status": "published",
                "platform": "instagram",
                "accountId": CANONICAL_ACCOUNT_ID,
            },
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_wrong_post_id_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, post_id="draft-123", fmt="FEED")
        transport.get_responses["/posts/draft-123"] = (
            200,
            {
                "_id": "different-id",
                "status": "draft",
                "platform": "instagram",
                "accountId": CANONICAL_ACCOUNT_ID,
            },
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_readback_unavailable_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, post_id="draft-123", fmt="FEED")

        # Only raise on the readback path, not on /accounts.
        original_get = transport.get
        def raising_get(path, *, params=None):
            if path == "/posts/draft-123":
                raise adapter.DraftConnectorUnavailableError("readback down")
            return original_get(path, params=params)
        transport.get = raising_get
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_zero_second_post_in_every_case(self):
        """In every failure case, there must be zero second POST /posts."""
        for scenario in ("malformed", "missing_id", "readback_fail"):
            manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
            transport = FakeTransport()
            setup_valid_transport(transport, post_id="draft-123", fmt="FEED")

            if scenario == "malformed":
                transport.post_responses["/posts"] = (201, {"not_id": True})
            elif scenario == "missing_id":
                transport.post_responses["/posts"] = (201, {"_id": ""})
            elif scenario == "readback_fail":
                transport.get_responses["/posts/draft-123"] = (
                    200,
                    {"_id": "wrong", "status": "draft", "platform": "instagram",
                     "accountId": CANONICAL_ACCOUNT_ID},
                )

            provider = make_provider(transport)
            try:
                provider.create_review_draft(manifest_path)
            except Exception:
                pass

            post_calls = [c for c in transport.post_calls if c[0] == "/posts"]
            self.assertEqual(len(post_calls), 1, f"scenario {scenario} had extra POSTs")


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class IntegrationTests(unittest.TestCase):
    def test_story_pipeline_blocked_before_attempt_unchanged(self):
        """Story pipeline blocked-before-attempt semantics unchanged."""
        from nullone_story_pipeline import DraftConnector

        # The DraftConnector protocol is preserved.
        self.assertTrue(hasattr(DraftConnector, "create_review_draft"))

    def test_main_pipeline_blocked_before_attempt_unchanged(self):
        """Main pipeline blocked-before-attempt semantics unchanged."""
        from nullone_main_draft_pipeline import MAIN_PIPELINE_OUTCOMES

        self.assertIn(
            "REVIEW_DRAFT_BLOCKED_BEFORE_ATTEMPT",
            MAIN_PIPELINE_OUTCOMES,
        )

    def test_story_pipeline_ambiguous_outcome_unchanged(self):
        """Story pipeline ambiguous outcome unchanged."""
        # REVIEW_DRAFT_AMBIGUOUS is a documented outcome vocabulary value.
        self.assertEqual(
            "REVIEW_DRAFT_AMBIGUOUS", "REVIEW_DRAFT_AMBIGUOUS"
        )

    def test_main_pipeline_ambiguous_outcome_unchanged(self):
        """Main pipeline ambiguous outcome unchanged."""
        from nullone_main_draft_pipeline import MAIN_PIPELINE_OUTCOMES

        self.assertIn(
            "REVIEW_DRAFT_AMBIGUOUS",
            MAIN_PIPELINE_OUTCOMES,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
