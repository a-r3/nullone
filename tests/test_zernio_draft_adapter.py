#!/usr/bin/env python3
"""Offline tests for the direct Zernio REST DraftProvider adapter (#81).

All tests use fake transports only. Network calls = 0.

Fixtures mirror the CURRENT official Zernio OpenAPI (openapi 3.1.0,
info.version 1.0.4, base https://zernio.com/api/v1):

- POST /v1/media/presign: request {filename, contentType};
  response {uploadUrl, publicUrl, key, expiresIn}
- POST /v1/tools/validate/media: request {url};
  response {valid, url, error, contentType, size, type, platformLimits}
- POST /v1/tools/validate/post: SAME BODY as POST /v1/posts;
  response {valid, message, warnings} / {valid, errors, warnings}
- POST /v1/posts: request {content, mediaItems[{type,url}],
  platforms[{platform, accountId, platformSpecificData}], isDraft};
  success is 201 {message, post: {_id, ...}, warnings}
- GET /v1/posts/{postId}: response {post: {_id, status, platforms, ...}}
- x-request-id header: UUID per logical request
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import uuid
from io import StringIO
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

CAPTION_TEXT = "Test caption. @nullone.az\n"
PUBLIC_URL_1 = "https://cdn.example.com/1.png"
UPLOAD_URL_1 = "https://upload.example.com/put"


def load_bridge():
    spec = importlib.util.spec_from_file_location(
        "nullone_draft_bridge_under_test", SCRIPTS / "nullone-draft-bridge.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        self.default_post = (
            200,
            {"valid": True, "message": "No validation issues found.", "warnings": []},
        )
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


class SameRunTransport(FakeTransport):
    """Scripted fake: presign succeeds with indexed public URLs except at
    the configured presign-attempt index, which fails. Media validation
    echoes each requested URL so persisted URLs validate. All calls are
    recorded exactly like the base fake."""

    def __init__(self, url_prefix, fail_at_presign_index):
        super().__init__()
        self.url_prefix = url_prefix
        self.fail_at_presign_index = fail_at_presign_index
        self.presign_attempts = 0

    def post(self, path, *, json_body=None, headers=None, idempotency_key=None):
        self.post_calls.append((path, json_body, headers, idempotency_key))
        if path == "/media/presign":
            idx = self.presign_attempts
            self.presign_attempts += 1
            if (
                self.fail_at_presign_index is not None
                and idx == self.fail_at_presign_index
            ):
                return (400, {"error": "synthetic presign failure"})
            return presign_success(
                public_url=f"https://cdn.example.com/{self.url_prefix}-{idx}.png",
                upload_url=f"https://upload.example.com/{self.url_prefix}-{idx}",
            )
        if path == "/tools/validate/media":
            return media_validation_success(url=(json_body or {}).get("url"))
        return self.post_responses.get(path, self.default_post)


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
    caption_path.write_text(CAPTION_TEXT, encoding="utf-8")

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


def presign_success(public_url=PUBLIC_URL_1, upload_url=UPLOAD_URL_1):
    return (
        200,
        {
            "uploadUrl": upload_url,
            "publicUrl": public_url,
            "key": "temp/abc123_1.png",
            "expiresIn": 3600,
        },
    )


def media_validation_success(url=PUBLIC_URL_1):
    return (
        200,
        {
            "valid": True,
            "url": url,
            "contentType": "image/png",
            "size": 12345,
            "sizeFormatted": "12 KB",
            "type": "image",
            "platformLimits": {
                "instagram": {
                    "limit": 8388608,
                    "limitFormatted": "8.0 MB",
                    "withinLimit": True,
                }
            },
        },
    )


def post_validation_success():
    return (
        200,
        {"valid": True, "message": "No validation issues found.", "warnings": []},
    )


def create_success(post_id="draft-123"):
    return (
        201,
        {
            "message": "Post created successfully",
            "post": {
                "_id": post_id,
                "status": "draft",
                "content": CAPTION_TEXT,
                "platforms": [
                    {
                        "platform": "instagram",
                        "accountId": {
                            "_id": CANONICAL_ACCOUNT_ID,
                            "platform": "instagram",
                            "username": "@nullone.az",
                            "displayName": "NullOne",
                            "isActive": True,
                        },
                        "status": "pending",
                    }
                ],
            },
            "warnings": [],
        },
    )


def readback_success(post_id="draft-123", fmt="FEED"):
    platforms = [
        {
            "platform": "instagram",
            "accountId": {
                "_id": CANONICAL_ACCOUNT_ID,
                "platform": "instagram",
                "username": "@nullone.az",
                "displayName": "NullOne",
                "isActive": True,
            },
            "status": "pending",
        }
    ]
    if fmt == "STORY":
        platforms[0]["platformSpecificData"] = {"contentType": "story"}
    return (
        200,
        {
            "post": {
                "_id": post_id,
                "status": "draft",
                "content": CAPTION_TEXT,
                "platforms": platforms,
            }
        },
    )


def setup_valid_transport(transport, post_id="draft-123", fmt="FEED"):
    """Configure a transport with valid CURRENT-contract responses."""
    transport.default_get = (
        200,
        {
            "accounts": [
                {
                    "_id": CANONICAL_ACCOUNT_ID,
                    "platform": "instagram",
                    "profileId": "profile-1",
                    "username": "@nullone.az",
                    "displayName": "NullOne",
                    "isActive": True,
                }
            ],
            "hasAnalyticsAccess": False,
        },
    )
    transport.post_responses["/media/presign"] = presign_success()
    transport.post_responses["/tools/validate/media"] = media_validation_success()
    transport.post_responses["/tools/validate/post"] = post_validation_success()
    transport.default_put = 204
    transport.post_responses["/posts"] = create_success(post_id)
    transport.get_responses[f"/posts/{post_id}"] = readback_success(post_id, fmt)
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
        self.assertEqual(m["media"][0]["public_url"], PUBLIC_URL_1)

        presign_calls = [c for c in transport.post_calls if c[0] == "/media/presign"]
        self.assertEqual(len(presign_calls), 1)
        # Current request is exactly {filename, contentType}: no size.
        self.assertEqual(
            set(presign_calls[0][1].keys()), {"filename", "contentType"}
        )
        self.assertNotIn("size", presign_calls[0][1])
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
            {"error": "presign failed"},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPresignBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")
        self.assertEqual(len([c for c in transport.post_calls if c[0] == "/posts"]), 0)
        for i in range(3):
            self.assertEqual(
                m["media"][i]["public_url"],
                f"https://cdn.example.com/pre-{i}.png",
            )

        transport2 = FakeTransport()
        setup_valid_transport(transport2, fmt="CAROUSEL")
        # Unique public URLs per presign for the carousel recovery.
        presign_count = [0]
        fallback = dict(transport2.post_responses)
        default_post = transport2.default_post

        def chained(path, *, json_body=None, headers=None, idempotency_key=None):
            transport2.post_calls.append((path, json_body, headers, idempotency_key))
            if path == "/media/presign":
                idx = presign_count[0]
                presign_count[0] += 1
                return presign_success(
                    public_url=f"https://cdn.example.com/carousel-{idx}.png",
                    upload_url=f"https://upload.example.com/put-{idx}",
                )
            if path == "/tools/validate/media":
                return media_validation_success(url=(json_body or {}).get("url"))
            return fallback.get(path, default_post)

        transport2.post = chained
        # Readback without mediaItems exposure is accepted.
        transport2.get_responses["/posts/draft-123"] = readback_success(
            "draft-123", "CAROUSEL"
        )
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
        # Accept the pre-existing URL in media validation.
        fallback = dict(transport.post_responses)
        default_post = transport.default_post

        def validating_post(path, *, json_body=None, headers=None, idempotency_key=None):
            transport.post_calls.append((path, json_body, headers, idempotency_key))
            if path == "/tools/validate/media":
                return media_validation_success(url=(json_body or {}).get("url"))
            if path == "/media/presign":
                return presign_success(
                    public_url="https://cdn.example.com/recovered.png",
                    upload_url="https://upload.example.com/put-recovered",
                )
            return fallback.get(path, default_post)

        transport.post = validating_post
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
            {"error": "presign failed"},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPresignBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")
        self.assertIsNone(m["review"]["zernio_draft_id"])

    def test_partial_presign_same_run_three_success_fourth_fails_and_recovery_reuses(self):
        """Real Sep-8 partial-progress shape: 8 CAROUSEL items start with NO
        public_url; items 1-3 presign+upload in ONE invocation, item 4
        presign fails; a second recovery run reuses the first three."""
        manifest_path, _ = make_manifest(self.tmp_path, fmt="CAROUSEL", media_count=8)
        _, m = load_manifest(manifest_path)
        self.assertTrue(all(item.get("public_url") is None for item in m["media"]))

        first = SameRunTransport(url_prefix="samerun", fail_at_presign_index=3)
        setup_valid_transport(first, fmt="CAROUSEL")
        provider = make_provider(first)
        with self.assertRaises(adapter.DraftPresignBlockedError):
            provider.create_review_draft(manifest_path)

        # Immediate post-failure proof: exactly 4 presigns, 3 PUTs, 0 creates.
        presign_attempts = [c for c in first.post_calls if c[0] == "/media/presign"]
        self.assertEqual(len(presign_attempts), 4)
        self.assertEqual(len(first.put_calls), 3)
        self.assertEqual(
            len([c for c in first.post_calls if c[0] == "/posts"]), 0
        )

        _, m = load_manifest(manifest_path)
        expected_first_urls = [
            f"https://cdn.example.com/samerun-{i}.png" for i in range(3)
        ]
        for i in range(3):
            self.assertEqual(m["media"][i]["public_url"], expected_first_urls[i])
        for i in range(3, 8):
            self.assertIsNone(m["media"][i].get("public_url"))
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")
        self.assertFalse(m["review"]["zernio_draft_id"])
        raw = manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("upload.example.com", raw)

        # Second recovery run with a fresh transport and distinct URLs.
        second = SameRunTransport(url_prefix="recovery", fail_at_presign_index=None)
        setup_valid_transport(second, fmt="CAROUSEL")
        second.get_responses["/posts/draft-123"] = readback_success(
            "draft-123", "CAROUSEL"
        )
        provider2 = make_provider(second)
        provider2.create_review_draft(manifest_path)

        # Only the remaining five items presigned/uploaded in recovery.
        second_presigns = [c for c in second.post_calls if c[0] == "/media/presign"]
        self.assertEqual(len(second_presigns), 5)
        self.assertEqual(len(second.put_calls), 5)
        # First three were NOT re-presigned: recovery presigned exactly
        # items 4-8 by filename, in exact manifest order.
        self.assertEqual(
            [c[1]["filename"] for c in second_presigns],
            [f"source-{i}.png" for i in range(3, 8)],
        )
        # First three were NOT re-uploaded: no recovery PUT touched a
        # first-run upload URL.
        first_upload_urls = {url for url, _, _ in first.put_calls}
        for url, _data, _headers in second.put_calls:
            self.assertNotIn(url, first_upload_urls)
            self.assertTrue(
                url.startswith("https://upload.example.com/recovery-")
            )

        _, m = load_manifest(manifest_path)
        expected_all = expected_first_urls + [
            f"https://cdn.example.com/recovery-{i}.png" for i in range(5)
        ]
        self.assertEqual(
            [item["public_url"] for item in m["media"]], expected_all
        )
        # Exactly one eventual draft create, first-three work never repeated.
        self.assertEqual(
            len([c for c in second.post_calls if c[0] == "/posts"]), 1
        )
        create_payload = [
            c for c in second.post_calls if c[0] == "/posts"
        ][0][1]
        self.assertEqual(
            [item["url"] for item in create_payload["mediaItems"]], expected_all
        )
        self.assertEqual(m["review"]["create_attempts"], 1)
        self.assertEqual(m["review"]["state"], "DRAFT_CREATED")
        self.assertEqual(m["review"]["zernio_draft_id"], "draft-123")


# ---------------------------------------------------------------------------
# Malformed presign normalization tests (malformed provider body -> clean
# presign BLOCKED at the presign adapter boundary, attempts untouched)
# ---------------------------------------------------------------------------


class PresignMalformedTests(unittest.TestCase):
    MALFORMED_MARKER = "SYNTHETIC_MALFORMED_BODY_MARKER_9f8c"

    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)
        self.bridge = load_bridge()

    def _assert_untouched(self, manifest_path, transport):
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")
        self.assertFalse(m["review"]["zernio_draft_id"])
        self.assertEqual(
            len([c for c in transport.post_calls if c[0] == "/posts"]), 0
        )
        return m

    def test_malformed_json_from_presign_transport_is_clean_blocked(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        marker = self.MALFORMED_MARKER
        original_post = transport.post

        def raising_post(path, *, json_body=None, headers=None, idempotency_key=None):
            if path == "/media/presign":
                raise adapter.DraftAdapterError(
                    f"Zernio drafts response was not valid JSON: {marker}"
                )
            return original_post(
                path, json_body=json_body, headers=headers,
                idempotency_key=idempotency_key,
            )

        transport.post = raising_post
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPresignBlockedError) as ctx:
            provider.create_review_draft(manifest_path)
        self.assertIs(type(ctx.exception), adapter.DraftPresignBlockedError)
        self.assertNotIn(marker, str(ctx.exception))
        self._assert_untouched(manifest_path, transport)

    def test_non_object_json_presign_body_is_clean_blocked(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/media/presign"] = (
            200,
            ["not", "an", "object"],
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPresignBlockedError) as ctx:
            provider.create_review_draft(manifest_path)
        self.assertIs(type(ctx.exception), adapter.DraftPresignBlockedError)
        self._assert_untouched(manifest_path, transport)

    def test_unusable_presign_envelope_is_clean_blocked(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/media/presign"] = (
            200,
            {"unexpected": "shape"},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPresignBlockedError):
            provider.create_review_draft(manifest_path)
        self._assert_untouched(manifest_path, transport)

    def test_presign_programming_defect_still_surfaces(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        original_post = transport.post

        def broken_post(path, *, json_body=None, headers=None, idempotency_key=None):
            if path == "/media/presign":
                raise TypeError("synthetic internal misuse")
            return original_post(
                path, json_body=json_body, headers=headers,
                idempotency_key=idempotency_key,
            )

        transport.post = broken_post
        provider = make_provider(transport)
        with self.assertRaises(TypeError):
            provider.create_review_draft(manifest_path)

    def test_bridge_reports_malformed_presign_as_blocked_without_traceback(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        marker = self.MALFORMED_MARKER
        transport = FakeTransport()
        setup_valid_transport(transport)
        original_post = transport.post

        def raising_post(path, *, json_body=None, headers=None, idempotency_key=None):
            if path == "/media/presign":
                raise adapter.DraftAdapterError(
                    f"Zernio drafts response was not valid JSON: {marker}"
                )
            return original_post(
                path, json_body=json_body, headers=headers,
                idempotency_key=idempotency_key,
            )

        transport.post = raising_post

        class MalformedProvider:
            def create_review_draft(self, _path):
                make_provider(transport).create_review_draft(_path)

        with patch.object(
            self.bridge,
            "build_production_draft_provider",
            return_value=MalformedProvider(),
        ):
            out = StringIO()
            with patch("sys.stdout", out):
                code = self.bridge.execute(str(manifest_path))
        self.assertEqual(code, 2)
        output = out.getvalue()
        self.assertIn("BLOCKED=", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn(marker, output)
        self._assert_untouched(manifest_path, transport)


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
            {"valid": False, "error": "invalid media"},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)

    def test_media_validation_instagram_limit_blocks_before_attempt(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/tools/validate/media"] = (
            200,
            {
                "valid": True,
                "url": PUBLIC_URL_1,
                "contentType": "image/png",
                "size": 99999999,
                "type": "image",
                "platformLimits": {
                    "instagram": {
                        "limit": 8388608,
                        "limitFormatted": "8.0 MB",
                        "withinLimit": False,
                    }
                },
            },
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(
            len([c for c in transport.post_calls if c[0] == "/posts"]), 0
        )

    def test_media_validation_request_is_url_only(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        media_calls = [
            c for c in transport.post_calls if c[0] == "/tools/validate/media"
        ]
        self.assertEqual(len(media_calls), 1)
        self.assertEqual(set(media_calls[0][1].keys()), {"url"})
        self.assertEqual(media_calls[0][1]["url"], PUBLIC_URL_1)

    def test_post_validation_fail_blocks_before_attempt(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/tools/validate/post"] = (
            200,
            {"valid": False, "errors": [{"platform": "instagram", "error": "bad"}]},
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
# Media identity binding tests (§1: response bound to exact manifest item)
# ---------------------------------------------------------------------------


class MediaBindingTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def _blocked_run(self, media_body):
        """Run a full draft flow with one media-validation body; return manifest + transport."""
        manifest_path, _ = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/tools/validate/media"] = (200, media_body)
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        return m, transport

    def _assert_blocked_before_create(self, m, transport):
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")
        self.assertIsNone(m["review"]["zernio_draft_id"])
        self.assertEqual(
            len([c for c in transport.post_calls if c[0] == "/posts"]), 0
        )

    def test_exact_valid_media_succeeds(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "DRAFT_CREATED")

    def test_response_url_mismatch_blocks_before_create(self):
        body = media_validation_success(url="https://cdn.example.com/other.png")[1]
        m, transport = self._blocked_run(body)
        self._assert_blocked_before_create(m, transport)

    def test_response_media_type_mismatch_blocks_before_create(self):
        body = dict(media_validation_success()[1])
        body["type"] = "video"
        m, transport = self._blocked_run(body)
        self._assert_blocked_before_create(m, transport)

    def test_response_content_type_mismatch_blocks_before_create(self):
        body = dict(media_validation_success()[1])
        body["contentType"] = "image/jpeg"
        m, transport = self._blocked_run(body)
        self._assert_blocked_before_create(m, transport)

    def test_instagram_within_limit_false_blocks_before_create(self):
        body = dict(media_validation_success()[1])
        body["platformLimits"] = {
            "instagram": {
                "limit": 8388608,
                "limitFormatted": "8.0 MB",
                "withinLimit": False,
            }
        }
        m, transport = self._blocked_run(body)
        self._assert_blocked_before_create(m, transport)

    def test_jpg_alias_matches_jpeg_manifest(self):
        # The only documented alias: image/jpg ≡ image/jpeg.
        self.assertEqual(
            adapter._normalize_content_type("image/jpg"), "image/jpeg"
        )
        manifest_path, m = make_manifest(self.tmp_path)
        m["media"][0]["content_type"] = "image/jpeg"
        atomic_write_json(manifest_path, m)
        transport = FakeTransport()
        setup_valid_transport(transport)
        body = dict(media_validation_success()[1])
        body["contentType"] = "image/jpg"
        transport.post_responses["/tools/validate/media"] = (200, body)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "DRAFT_CREATED")


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
        self.assertEqual(
            payload["platforms"][0]["platformSpecificData"]["contentType"], "story"
        )
        self.assertTrue(payload["isDraft"])

        create_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        self.assertEqual(len(create_calls), 1)
        create_payload = create_calls[0][1]
        self.assertEqual(
            create_payload["platforms"][0]["platformSpecificData"]["contentType"],
            "story",
        )
        self.assertTrue(create_payload["isDraft"])
        self.assertNotIn("publishNow", create_payload)
        self.assertNotIn("scheduledFor", create_payload)
        self.assertNotIn("queuedFromProfile", create_payload)
        self.assertNotIn("queueId", create_payload)

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
        self.assertNotIn("platformSpecificData", payload["platforms"][0])

        create_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        create_payload = create_calls[0][1]
        self.assertNotIn("platformSpecificData", create_payload["platforms"][0])
        self.assertTrue(create_payload["isDraft"])
        self.assertNotIn("publishNow", create_payload)
        self.assertNotIn("scheduledFor", create_payload)

    def test_carousel_media_order_exact(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="CAROUSEL", media_count=3)
        transport = FakeTransport()
        setup_valid_transport(transport, fmt="CAROUSEL")

        presign_count = [0]
        original_post = transport.post

        def dynamic_post(path, *, json_body=None, headers=None, idempotency_key=None):
            if path == "/media/presign":
                idx = presign_count[0]
                presign_count[0] += 1
                return presign_success(
                    public_url=f"https://cdn.example.com/carousel-{idx}.png",
                    upload_url=f"https://upload.example.com/put-{idx}",
                )
            if path == "/tools/validate/media":
                return media_validation_success(url=(json_body or {}).get("url"))
            return original_post(
                path, json_body=json_body, headers=headers,
                idempotency_key=idempotency_key,
            )

        transport.post = dynamic_post
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        create_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        create_payload = create_calls[0][1]
        media_urls = [item["url"] for item in create_payload["mediaItems"]]
        expected = [
            f"https://cdn.example.com/carousel-{i}.png"
            for i in range(3)
        ]
        self.assertEqual(media_urls, expected)
        self.assertEqual(
            [item["type"] for item in create_payload["mediaItems"]],
            ["image", "image", "image"],
        )

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
        self.assertNotIn("queuedFromProfile", create_payload)
        self.assertNotIn("queueId", create_payload)
        self.assertNotIn("publish_now", create_payload)
        self.assertNotIn("schedule", create_payload)
        # Obsolete root shape is gone.
        self.assertNotIn("platform", create_payload)
        self.assertNotIn("accountId", create_payload)
        self.assertNotIn("media", create_payload)
        self.assertNotIn("platformSpecificData", create_payload)
        # Current shape keys only.
        self.assertEqual(
            set(create_payload.keys()),
            {"content", "mediaItems", "platforms", "isDraft"},
        )
        self.assertEqual(create_payload["content"], CAPTION_TEXT)
        self.assertEqual(
            create_payload["platforms"],
            [{"platform": "instagram", "accountId": CANONICAL_ACCOUNT_ID}],
        )

    def test_validate_post_body_is_identical_to_create_body(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, fmt="FEED")
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        validate_payload = [
            c for c in transport.post_calls if c[0] == "/tools/validate/post"
        ][0][1]
        create_payload = [
            c for c in transport.post_calls if c[0] == "/posts"
        ][0][1]
        self.assertEqual(validate_payload, create_payload)

    def test_unsupported_media_type_fails_closed_before_create(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        m["media"][0]["content_type"] = "application/octet-stream"
        atomic_write_json(manifest_path, m)
        # Presign still succeeds; payload derivation must fail closed.
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftPreflightBlockedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(
            len([c for c in transport.post_calls if c[0] == "/posts"]), 0
        )

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

    def test_review_draft_transport_has_no_mcp_claude(self):
        import ast
        import inspect

        def code_without_docstrings(module):
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
            stripped = [
                line
                for i, line in enumerate(lines, start=1)
                if not any(start <= i <= end for start, end in removals)
            ]
            return "".join(stripped)

        for module in (adapter, load_bridge()):
            code = code_without_docstrings(module)
            self.assertNotIn("mcp__zernio", code)
            self.assertNotIn("nullone_claude", code)
            self.assertNotIn("claude -p", code)
            self.assertNotIn("run_structured", code)


# ---------------------------------------------------------------------------
# Contract-shape regression tests (A-H)
# ---------------------------------------------------------------------------


class ContractShapeTests(unittest.TestCase):
    """Fixtures mirror CURRENT official Zernio examples/OpenAPI."""

    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def test_a_actual_presign_success_envelope(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        presign_calls = [c for c in transport.post_calls if c[0] == "/media/presign"]
        self.assertEqual(len(presign_calls), 1)
        body = transport.post_responses["/media/presign"][1]
        self.assertIn("uploadUrl", body)
        self.assertIn("publicUrl", body)
        self.assertIn("key", body)
        self.assertIn("expiresIn", body)
        self.assertNotIn("status", body)
        self.assertNotIn("error", body)

    def test_b_actual_media_validation_success_envelope(self):
        body = media_validation_success()[1]
        for key in ("valid", "url", "contentType", "size", "type", "platformLimits"):
            self.assertIn(key, body)
        self.assertNotIn("status", body)
        self.assertTrue(
            adapter._validate_media_response(
                body,
                what="media validation",
                expected_url=PUBLIC_URL_1,
                expected_content_type="image/png",
            )
        )

    def test_c_actual_post_validation_success_envelope(self):
        body = post_validation_success()[1]
        for key in ("valid", "message", "warnings"):
            self.assertIn(key, body)
        self.assertNotIn("status", body)
        self.assertNotIn("error", body)
        self.assertTrue(adapter._validate_post_response(body, what="post validation"))

    def test_d_exact_create_request_body(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        create_payload = [
            c for c in transport.post_calls if c[0] == "/posts"
        ][0][1]
        self.assertEqual(create_payload["content"], CAPTION_TEXT)
        self.assertEqual(
            create_payload["mediaItems"],
            [{"type": "image", "url": PUBLIC_URL_1}],
        )
        self.assertEqual(len(create_payload["platforms"]), 1)
        self.assertEqual(create_payload["platforms"][0]["platform"], "instagram")
        self.assertEqual(create_payload["platforms"][0]["accountId"], CANONICAL_ACCOUNT_ID)
        self.assertTrue(create_payload["isDraft"])

    def test_e_story_platform_specific_data_nested(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="STORY")
        transport = FakeTransport()
        setup_valid_transport(transport, fmt="STORY")
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        create_payload = [
            c for c in transport.post_calls if c[0] == "/posts"
        ][0][1]
        self.assertEqual(
            create_payload["platforms"][0]["platformSpecificData"],
            {"contentType": "story"},
        )
        self.assertNotIn("platformSpecificData", create_payload)

    def test_f_actual_create_success_envelope(self):
        body = create_success("draft-123")[1]
        self.assertIn("post", body)
        self.assertNotIn("_id", body)
        self.assertEqual(adapter._validate_create_response(body), "draft-123")

    def test_g_actual_get_envelope(self):
        body = readback_success("draft-123", "FEED")[1]
        self.assertIn("post", body)
        self.assertNotIn("platform", body)
        self.assertNotIn("accountId", body)
        self.assertNotIn("platformSpecificData", body)
        adapter._validate_readback_response(
            body,
            post_id="draft-123",
            account_id=CANONICAL_ACCOUNT_ID,
            fmt="FEED",
            expected_content=CAPTION_TEXT,
            expected_media_items=[{"type": "image", "url": PUBLIC_URL_1}],
        )

    def test_h_malformed_legacy_shapes_fail_closed(self):
        # Legacy presign envelope without usable HTTPS URLs.
        with self.assertRaises(adapter.DraftPresignBlockedError):
            adapter._validate_presign_response({"status": "OK"})
        # Legacy media validation envelope with invented status.
        with self.assertRaises(adapter.DraftAdapterError):
            adapter._validate_media_response(
                {"status": "READY", "valid": True, "error": ""},
                what="media validation",
                expected_url=PUBLIC_URL_1,
                expected_content_type="image/png",
            )
        # Legacy post validation envelope with invented status.
        with self.assertRaises(adapter.DraftAdapterError):
            adapter._validate_post_response(
                {"status": "READY", "valid": True, "error": ""},
                what="post validation",
            )
        # Legacy top-level _id create shape (no post envelope).
        with self.assertRaises(adapter.DraftCreateAmbiguousError):
            adapter._validate_create_response({"_id": "draft-123"})
        # Legacy top-level readback shape (no post envelope).
        with self.assertRaises(adapter.DraftReadbackFailedError):
            adapter._validate_readback_response(
                {
                    "_id": "draft-123",
                    "status": "draft",
                    "platform": "instagram",
                    "accountId": CANONICAL_ACCOUNT_ID,
                },
                post_id="draft-123",
                account_id=CANONICAL_ACCOUNT_ID,
                fmt="FEED",
            )


# ---------------------------------------------------------------------------
# x-request-id tests
# ---------------------------------------------------------------------------


class RequestIdTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def _manifest_dict(self, manifest_path):
        _, m = load_manifest(manifest_path)
        return m

    def test_valid_uuid_syntax(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        m = self._manifest_dict(manifest_path)
        value = adapter.ZernioDraftProvider._request_id(manifest_path, m)
        parsed = uuid.UUID(value)
        self.assertEqual(str(parsed), value)
        self.assertEqual(parsed.version, 5)

    def test_stable_for_same_manifest(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        m = self._manifest_dict(manifest_path)
        first = adapter.ZernioDraftProvider._request_id(manifest_path, m)
        second = adapter.ZernioDraftProvider._request_id(manifest_path, m)
        self.assertEqual(first, second)

    def test_different_manifest_different_uuid(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        m = self._manifest_dict(manifest_path)
        first = adapter.ZernioDraftProvider._request_id(manifest_path, m)
        m2 = dict(m)
        m2["manifest_id"] = "test-feed-002"
        second = adapter.ZernioDraftProvider._request_id(manifest_path, m2)
        self.assertNotEqual(first, second)

    def test_still_exactly_one_post_with_uuid(self):
        manifest_path, _ = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        provider = make_provider(transport)
        provider.create_review_draft(manifest_path)

        post_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        self.assertEqual(len(post_calls), 1)
        key = post_calls[0][3]
        self.assertIsNotNone(key)
        uuid.UUID(key)


# ---------------------------------------------------------------------------
# Bridge normalization tests
# ---------------------------------------------------------------------------


class BridgeNormalizationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)
        self.bridge = load_bridge()

    def test_unauthorized_before_create_is_clean_blocked(self):
        manifest_path, _ = make_manifest(self.tmp_path)

        class UnauthorizedProvider:
            def create_review_draft(self, _path):
                raise adapter.DraftConnectorUnauthorizedError("missing")

        with patch.object(
            self.bridge, "build_production_draft_provider",
            return_value=UnauthorizedProvider(),
        ):
            out = StringIO()
            with patch("sys.stdout", out):
                code = self.bridge.execute(str(manifest_path))
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED=", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)
        self.assertEqual(m["review"]["state"], "NOT_CREATED")

    def test_unavailable_before_create_is_clean_blocked(self):
        manifest_path, _ = make_manifest(self.tmp_path)

        class UnavailableProvider:
            def create_review_draft(self, _path):
                raise adapter.DraftConnectorUnavailableError("down")

        with patch.object(
            self.bridge, "build_production_draft_provider",
            return_value=UnavailableProvider(),
        ):
            out = StringIO()
            with patch("sys.stdout", out):
                code = self.bridge.execute(str(manifest_path))
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED=", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 0)

    def test_programming_defect_still_surfaces(self):
        manifest_path, _ = make_manifest(self.tmp_path)

        class BrokenProvider:
            def create_review_draft(self, _path):
                raise RuntimeError("synthetic programming defect")

        with patch.object(
            self.bridge, "build_production_draft_provider",
            return_value=BrokenProvider(),
        ):
            with self.assertRaises(RuntimeError):
                self.bridge.execute(str(manifest_path))


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
        transport.post_responses["/posts"] = (201, {"not_a_post": True})
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftCreateAmbiguousError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_legacy_top_level_id_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/posts"] = (201, {"_id": "draft-123"})
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftCreateAmbiguousError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 1)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_unexpected_200_variant_yields_review_unknown_no_retry(self):
        # Same x-request-id idempotency hint / dryRun-style 200 can never
        # prove a created post.
        for body in (
            {"existingPost": {"_id": "draft-123"}},
            {"dryRun": True, "canPublish": True, "tiktok": []},
        ):
            manifest_path, m = make_manifest(self.tmp_path)
            transport = FakeTransport()
            setup_valid_transport(transport)
            transport.post_responses["/posts"] = (200, body)
            provider = make_provider(transport)
            with self.assertRaises(adapter.DraftCreateAmbiguousError):
                provider.create_review_draft(manifest_path)

            _, m = load_manifest(manifest_path)
            self.assertEqual(m["review"]["create_attempts"], 1)
            self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")
            post_calls = [c for c in transport.post_calls if c[0] == "/posts"]
            self.assertEqual(len(post_calls), 1)

    def test_missing_post_id_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/posts"] = (
            201,
            {"message": "created", "post": {"_id": "", "status": "draft"}},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftCreateAmbiguousError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_no_automatic_retry(self):
        manifest_path, m = make_manifest(self.tmp_path)
        transport = FakeTransport()
        setup_valid_transport(transport)
        transport.post_responses["/posts"] = (
            201,
            {"message": "created", "post": {"_id": "", "status": "draft"}},
        )
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
                "post": {
                    "_id": "draft-123",
                    "status": "draft",
                    "platforms": [
                        {
                            "platform": "instagram",
                            "accountId": "wrong-account",
                        }
                    ],
                }
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
                "post": {
                    "_id": "draft-123",
                    "status": "draft",
                    "platforms": [
                        {
                            "platform": "twitter",
                            "accountId": CANONICAL_ACCOUNT_ID,
                        }
                    ],
                }
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
                "post": {
                    "_id": "draft-123",
                    "status": "published",
                    "platforms": [
                        {
                            "platform": "instagram",
                            "accountId": CANONICAL_ACCOUNT_ID,
                        }
                    ],
                }
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
                "post": {
                    "_id": "different-id",
                    "status": "draft",
                    "platforms": [
                        {
                            "platform": "instagram",
                            "accountId": CANONICAL_ACCOUNT_ID,
                        }
                    ],
                }
            },
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_contradictory_content_yields_review_unknown(self):
        manifest_path, m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, post_id="draft-123", fmt="FEED")
        transport.get_responses["/posts/draft-123"] = (
            200,
            {
                "post": {
                    "_id": "draft-123",
                    "status": "draft",
                    "content": "tampered caption",
                    "platforms": [
                        {
                            "platform": "instagram",
                            "accountId": {"_id": CANONICAL_ACCOUNT_ID},
                        }
                    ],
                }
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
                transport.post_responses["/posts"] = (201, {"not_a_post": True})
            elif scenario == "missing_id":
                transport.post_responses["/posts"] = (
                    201,
                    {"message": "created", "post": {"_id": "", "status": "draft"}},
                )
            elif scenario == "readback_fail":
                transport.get_responses["/posts/draft-123"] = (
                    200,
                    {
                        "post": {
                            "_id": "wrong",
                            "status": "draft",
                            "platforms": [
                                {
                                    "platform": "instagram",
                                    "accountId": CANONICAL_ACCOUNT_ID,
                                }
                            ],
                        }
                    },
                )

            provider = make_provider(transport)
            try:
                provider.create_review_draft(manifest_path)
            except Exception:
                pass

            post_calls = [c for c in transport.post_calls if c[0] == "/posts"]
            self.assertEqual(len(post_calls), 1, f"scenario {scenario} had extra POSTs")


# ---------------------------------------------------------------------------
# Readback exact target-set tests (§2: platforms must be exactly the one
# intended target)
# ---------------------------------------------------------------------------


class ReadbackTargetSetTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def _run_with_platforms(self, platforms):
        manifest_path, _ = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, post_id="draft-123", fmt="FEED")
        transport.get_responses["/posts/draft-123"] = (
            200,
            {"post": {"_id": "draft-123", "status": "draft", "platforms": platforms}},
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)

        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["create_attempts"], 1)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")
        post_calls = [c for c in transport.post_calls if c[0] == "/posts"]
        self.assertEqual(len(post_calls), 1)
        return m

    def _instagram_entry(self, account_id=CANONICAL_ACCOUNT_ID):
        return {"platform": "instagram", "accountId": account_id}

    def test_extra_twitter_target_rejected(self):
        self._run_with_platforms(
            [self._instagram_entry(), {"platform": "twitter", "accountId": "other"}]
        )

    def test_second_instagram_account_rejected(self):
        self._run_with_platforms(
            [self._instagram_entry(), self._instagram_entry(account_id="other")]
        )

    def test_duplicate_instagram_targets_rejected(self):
        self._run_with_platforms([self._instagram_entry(), self._instagram_entry()])

    def test_correct_platform_wrong_account_rejected(self):
        self._run_with_platforms([self._instagram_entry(account_id="wrong-account")])

    def test_empty_platforms_rejected(self):
        self._run_with_platforms([])


# ---------------------------------------------------------------------------
# Transport POST allowlist tests (§3: exact-path capability boundary)
# ---------------------------------------------------------------------------


class TransportCapabilityTests(unittest.TestCase):
    def _real_transport(self):
        return adapter.build_authenticated_transport(
            token=SecretValue(MARKER + "-transport")
        )

    def _canned_urlopen(self, calls):
        class CannedResponse:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            return CannedResponse()

        return fake_urlopen

    def test_exact_post_allowlist(self):
        self.assertEqual(
            adapter.ALLOWED_POST_PATHS,
            frozenset(
                {
                    "/media/presign",
                    "/tools/validate/media",
                    "/tools/validate/post",
                    "/posts",
                }
            ),
        )

    def test_all_four_exact_paths_reach_request_handling(self):
        transport = self._real_transport()
        for path in sorted(adapter.ALLOWED_POST_PATHS):
            calls = []
            with patch.object(
                adapter.urllib_request, "urlopen", side_effect=self._canned_urlopen(calls)
            ):
                status, body = transport.post(path, json_body={"probe": True})
            self.assertEqual(status, 200)
            self.assertEqual(body, {})
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].full_url.endswith(path))

    def test_posts_subpath_rejected_before_network(self):
        transport = self._real_transport()
        for forbidden in ("/posts/anything", "/posts/anything/retry", "/publish", "/v1/posts"):
            calls = []
            with patch.object(
                adapter.urllib_request,
                "urlopen",
                side_effect=AssertionError("network must not be attempted"),
            ):
                with self.assertRaises(adapter.DraftAdapterError):
                    transport.post(forbidden, json_body={"probe": True})
            self.assertEqual(calls, [])

    def test_rejection_exposes_no_credential(self):
        transport = self._real_transport()
        try:
            transport.post("/posts/anything/retry", json_body={})
        except adapter.DraftAdapterError as exc:
            self.assertNotIn(MARKER, str(exc))
        else:
            self.fail("forbidden POST path did not fail closed")

    def test_transport_exposes_no_publish_retry_method(self):
        public_callable = sorted(
            name
            for name in dir(adapter.UrllibDraftTransport)
            if not name.startswith("_")
            and callable(getattr(adapter.UrllibDraftTransport, name))
        )
        self.assertEqual(public_callable, ["get", "post", "put"])


# ---------------------------------------------------------------------------
# OpenAPI required-field discipline tests (§4)
# ---------------------------------------------------------------------------


class RequiredFieldDisciplineTests(unittest.TestCase):
    """Current OpenAPI (3.1.0 / 1.0.4) declares NO `required[]` entry on the
    presign, validate/media, or validate/post success schemas. key,
    expiresIn, message, and warnings are therefore OPTIONAL and must not be
    invented as requirements. This class pins that decision: responses
    omitting those fields still validate."""

    def test_presign_without_key_and_expires_in_passes(self):
        upload_url, public_url = adapter._validate_presign_response(
            {"uploadUrl": UPLOAD_URL_1, "publicUrl": PUBLIC_URL_1}
        )
        self.assertEqual(upload_url, UPLOAD_URL_1)
        self.assertEqual(public_url, PUBLIC_URL_1)

    def test_post_validation_without_message_warnings_passes(self):
        self.assertTrue(
            adapter._validate_post_response({"valid": True}, what="post validation")
        )

    def test_media_validation_without_optional_metadata_passes(self):
        self.assertTrue(
            adapter._validate_media_response(
                {"valid": True, "type": "image", "contentType": "image/png"},
                what="media validation",
                expected_url=PUBLIC_URL_1,
                expected_content_type="image/png",
            )
        )


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


# ---------------------------------------------------------------------------
# Shared media-URL identity rule tests (draft readback safety boundary).
#
# nullone_bridge_common.media_url_identity_matches is the ONE shared rule
# used by BOTH this adapter's readback validator and the publish adapter's
# remote-draft preflight/readback validator (tests/test_zernio_publish_
# adapter.py :: MediaUrlIdentityPreflightTests). Fixes the confirmed
# defect from the 2026-09-10 production readback: Zernio returned the
# same HTTPS host and exact filename but rewrote the storage path, so an
# exact full-URL equality check falsely forced REVIEW_UNKNOWN on a
# legitimate, fully-identified draft.
# ---------------------------------------------------------------------------


class MediaUrlIdentityHelperTests(unittest.TestCase):
    """Direct unit tests of the shared identity rule itself."""

    def test_exact_equality_passes(self):
        self.assertTrue(
            bridge_common.media_url_identity_matches(
                "https://cdn.example.com/a/b/img.png",
                "https://cdn.example.com/a/b/img.png",
            )
        )

    def test_zernio_same_basename_different_directory_passes(self):
        self.assertTrue(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/uploads/2026/09/10/img-abc123.png",
                "https://media.zernio.com/storage/rewritten/path/img-abc123.png",
            )
        )

    def test_sep10_production_readback_case_passes(self):
        """Named regression: 2026-09-10 production incident. Sanitized
        shape observed live: same https scheme, same media.zernio.com
        host, identical filename, minimally rewritten directory path."""
        self.assertTrue(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/temp/abc123_1/"
                "2026-09-10-mistral-samsung-series-d.png",
                "https://media.zernio.com/posts/6aa298cd08781ff66238fc89/"
                "2026-09-10-mistral-samsung-series-d.png",
            )
        )

    def test_same_filename_different_host_fails(self):
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/a/img.png",
                "https://cdn.other.example/a/img.png",
            )
        )

    def test_same_zernio_host_different_filename_fails(self):
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/a/img.png",
                "https://media.zernio.com/a/other.png",
            )
        )

    def test_http_scheme_never_passes(self):
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "http://media.zernio.com/a/img.png",
                "https://media.zernio.com/a/img.png",
            )
        )
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/a/img.png",
                "http://media.zernio.com/a/img.png",
            )
        )

    def test_external_cdn_same_filename_different_path_fails(self):
        # Rule C: non-Zernio hosts are never relaxed to filename identity.
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://cdn.example.com/a/img.png",
                "https://cdn.example.com/b/img.png",
            )
        )

    def test_query_string_never_manufactures_a_match(self):
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/a/img.png",
                "https://media.zernio.com/b/img.png?sig=abc",
            )
        )
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/a/img.png?sig=abc",
                "https://media.zernio.com/b/img.png",
            )
        )

    def test_fragment_never_manufactures_a_match(self):
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/a/img.png",
                "https://media.zernio.com/b/img.png#frag",
            )
        )

    def test_userinfo_credentials_rejected(self):
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://user:pass@media.zernio.com/a/img.png",
                "https://media.zernio.com/b/img.png",
            )
        )
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/a/img.png",
                "https://user:pass@media.zernio.com/b/img.png",
            )
        )

    def test_empty_basename_rejected(self):
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/a/",
                "https://media.zernio.com/b/",
            )
        )

    def test_no_fuzzy_similarity_no_length_authority(self):
        # A near-identical but non-identical filename must fail even
        # though the strings are very similar / same length.
        self.assertFalse(
            bridge_common.media_url_identity_matches(
                "https://media.zernio.com/a/img-1.png",
                "https://media.zernio.com/a/img-2.png",
            )
        )


class MediaIdentityReadbackTests(unittest.TestCase):
    """Integration tests through the real draft-readback safety boundary
    (ZernioDraftProvider._readback -> _validate_readback_response)."""

    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def _run_single_media_case(self, expected_url, returned_url):
        manifest_path, _m = make_manifest(self.tmp_path, fmt="FEED")
        transport = FakeTransport()
        setup_valid_transport(transport, post_id="draft-123", fmt="FEED")
        transport.post_responses["/media/presign"] = (
            200,
            {
                "uploadUrl": UPLOAD_URL_1,
                "publicUrl": expected_url,
                "key": "temp/abc123_1.png",
                "expiresIn": 3600,
            },
        )
        transport.post_responses["/tools/validate/media"] = (
            media_validation_success(url=expected_url)
        )
        transport.get_responses["/posts/draft-123"] = (
            200,
            {
                "post": {
                    "_id": "draft-123",
                    "status": "draft",
                    "content": CAPTION_TEXT,
                    "platforms": [
                        {
                            "platform": "instagram",
                            "accountId": CANONICAL_ACCOUNT_ID,
                        }
                    ],
                    "mediaItems": [{"type": "image", "url": returned_url}],
                }
            },
        )
        provider = make_provider(transport)
        return provider, manifest_path

    # -- PASS -----------------------------------------------------------

    def test_exact_same_url_passes_readback(self):
        provider, manifest_path = self._run_single_media_case(
            PUBLIC_URL_1, PUBLIC_URL_1
        )
        provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "DRAFT_CREATED")

    def test_zernio_rewritten_path_same_basename_passes_readback(self):
        expected = "https://media.zernio.com/temp/abc123_1/mistral-samsung.png"
        returned = "https://media.zernio.com/posts/xyz789/mistral-samsung.png"
        provider, manifest_path = self._run_single_media_case(
            expected, returned
        )
        provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "DRAFT_CREATED")

    def test_sep10_production_readback_regression_passes(self):
        """Named regression for the 2026-09-10 production incident: exact
        caption/account/platform/media-count/type/order already matched;
        only the media URL's storage path differed by Zernio's own
        rewrite while the filename stayed identical. Before this fix the
        deployed validator's exact full-URL equality falsely forced
        REVIEW_UNKNOWN on this legitimate draft."""
        expected = (
            "https://media.zernio.com/temp/abc123_1/"
            "2026-09-10-mistral-samsung-series-d.png"
        )
        returned = (
            "https://media.zernio.com/posts/6aa298cd08781ff66238fc89/"
            "2026-09-10-mistral-samsung-series-d.png"
        )
        provider, manifest_path = self._run_single_media_case(
            expected, returned
        )
        provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "DRAFT_CREATED")
        self.assertEqual(m["review"]["zernio_draft_id"], "draft-123")

    # -- FAIL -------------------------------------------------------------

    def test_same_filename_different_host_fails_readback(self):
        provider, manifest_path = self._run_single_media_case(
            "https://media.zernio.com/a/img.png",
            "https://cdn.other.example/a/img.png",
        )
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_same_host_different_filename_fails_readback(self):
        provider, manifest_path = self._run_single_media_case(
            "https://media.zernio.com/a/img.png",
            "https://media.zernio.com/a/other.png",
        )
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_http_url_fails_readback(self):
        provider, manifest_path = self._run_single_media_case(
            "https://media.zernio.com/a/img.png",
            "http://media.zernio.com/a/img.png",
        )
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_external_cdn_same_filename_different_path_fails_readback(self):
        provider, manifest_path = self._run_single_media_case(
            "https://cdn.example.com/a/img.png",
            "https://cdn.example.com/b/img.png",
        )
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_query_fragment_manufactured_match_fails_readback(self):
        provider, manifest_path = self._run_single_media_case(
            "https://media.zernio.com/a/img.png",
            "https://media.zernio.com/b/img.png?sig=xyz",
        )
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")


class MediaIdentityCarouselStructuralTests(unittest.TestCase):
    """Order/type/count must still fail closed under the new shared rule
    (CAROUSEL, 2 Zernio-hosted media items each individually eligible for
    the relaxed filename match)."""

    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def _carousel_setup(self):
        manifest_path, _m = make_manifest(
            self.tmp_path, fmt="CAROUSEL", media_count=2
        )
        transport = FakeTransport()
        setup_valid_transport(transport, fmt="CAROUSEL")
        expected_urls = [
            "https://media.zernio.com/temp/a/img-0.png",
            "https://media.zernio.com/temp/a/img-1.png",
        ]
        presign_count = [0]

        def dynamic_post(path, *, json_body=None, headers=None, idempotency_key=None):
            transport.post_calls.append((path, json_body, headers, idempotency_key))
            if path == "/media/presign":
                idx = presign_count[0]
                presign_count[0] += 1
                return (
                    200,
                    {
                        "uploadUrl": f"https://upload.example.com/put-{idx}",
                        "publicUrl": expected_urls[idx],
                        "key": f"temp/a/img-{idx}.png",
                        "expiresIn": 3600,
                    },
                )
            if path == "/tools/validate/media":
                return media_validation_success(url=(json_body or {}).get("url"))
            return transport.post_responses.get(path, transport.default_post)

        transport.post = dynamic_post
        return transport, manifest_path, expected_urls

    def _readback_body(self, media_items):
        return (
            200,
            {
                "post": {
                    "_id": "draft-123",
                    "status": "draft",
                    "content": CAPTION_TEXT,
                    "platforms": [
                        {
                            "platform": "instagram",
                            "accountId": CANONICAL_ACCOUNT_ID,
                        }
                    ],
                    "mediaItems": media_items,
                }
            },
        )

    def test_changed_media_order_fails_readback(self):
        transport, manifest_path, urls = self._carousel_setup()
        # Same two Zernio-rewritten filenames, but positions swapped.
        returned = [
            "https://media.zernio.com/final/x/img-1.png",
            "https://media.zernio.com/final/x/img-0.png",
        ]
        transport.get_responses["/posts/draft-123"] = self._readback_body(
            [{"type": "image", "url": u} for u in returned]
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_changed_media_type_fails_readback(self):
        transport, manifest_path, urls = self._carousel_setup()
        transport.get_responses["/posts/draft-123"] = self._readback_body(
            [
                {"type": "video", "url": urls[0]},
                {"type": "image", "url": urls[1]},
            ]
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_missing_media_item_fails_readback(self):
        transport, manifest_path, urls = self._carousel_setup()
        transport.get_responses["/posts/draft-123"] = self._readback_body(
            [{"type": "image", "url": urls[0]}]
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")

    def test_extra_media_item_fails_readback(self):
        transport, manifest_path, urls = self._carousel_setup()
        extra = urls + ["https://media.zernio.com/temp/a/img-2.png"]
        transport.get_responses["/posts/draft-123"] = self._readback_body(
            [{"type": "image", "url": u} for u in extra]
        )
        provider = make_provider(transport)
        with self.assertRaises(adapter.DraftReadbackFailedError):
            provider.create_review_draft(manifest_path)
        _, m = load_manifest(manifest_path)
        self.assertEqual(m["review"]["state"], "REVIEW_UNKNOWN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
