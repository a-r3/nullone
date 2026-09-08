#!/usr/bin/env python3
"""Direct deterministic Zernio REST DraftProvider adapter (#81).

This module is the ONLY place that knows about Zernio-specific HTTPS
paths, response envelopes and the draft/write credential. It replaces the
previous MCP-backed path:

    DraftConnector -> nullone-draft-bridge.py -> claude -p -> Zernio MCP

with a deterministic, testable direct HTTPS path:

    DraftConnector -> nullone-draft-bridge.py -> ZernioDraftProvider (this module)

Endpoint contract confirmed 2026-09-08 against Zernio's official
OpenAPI specification (docs.zernio.com/api/openapi, openapi: 3.1.0):

- base URL: https://zernio.com/api/v1
- GET /accounts (listAccounts) -> AccountsListResponse
  {accounts: [{_id, platform, profileId, username, displayName, profileUrl, isActive}], hasAnalyticsAccess}
- POST /media/presign (createMediaPresignedUrl) -> {status, uploadUrl, publicUrl, error}
- PUT <presigned uploadUrl> (unauthenticated, exact bytes)
- POST /tools/validate/media (validateMedia) -> {status, valid, error}
- POST /tools/validate/post (validatePost) -> {status, valid, error}
- POST /posts (createPost) -> {_id, status, platform, accountId, ...}
- GET /posts/{postId} (getPost) -> {_id, status, platform, accountId, ...}

By construction this module exposes ONLY draft-creation capability:
- there is no publish, schedule, delete, update-to-publish, or retry method;
- the transport never issues anything other than GET/POST/PUT, and only the
  POST /posts is a create;
- the adapter contains no publication capability whatsoever.

Credentials never enter this module as an environment variable, credential
file, or literal. The transport receives a SecretValue token at construction
time via build_authenticated_transport, which is the only function in this
adapter that accepts one. The transparent SecretValue wrapper makes an
accidental print impossible: repr()/str()/format() render the fixed token
<redacted> and the value is exposed only through the narrow reveal() used
by the transport to build the Authorization header.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from nullone_bridge_common import (
    CANONICAL_ACCOUNT_ID,
    BridgeError,
    atomic_write_json,
    load_manifest,
    now_iso,
    resolve_workspace_path,
    validate_manifest,
)
from nullone_secret_provider import (
    SECRET_REDACTED_RENDER,
    SecretNotConfiguredError,
    SecretProvider,
    SecretProviderError,
    SecretUnavailableError,
    SecretValue,
)

# Confirmed base URL. This adapter never reads it from any environment
# variable; build_authenticated_transport uses it as the default.
DEFAULT_BASE_URL = "https://zernio.com/api/v1"

# Canonical Instagram platform value used by Zernio REST.
INSTAGRAM_PLATFORM = "instagram"

# Fixed, generic reason texts. Never derived from a provider exception.
PREFLIGHT_ACCOUNT_FAILED_REASON = (
    "Zernio draft preflight failed: canonical Instagram account is missing "
    "or not eligible/connected."
)
PREFLIGHT_MEDIA_FAILED_REASON = (
    "Zernio draft preflight failed: one or more media items failed media "
    "validation."
)
PREFLIGHT_POST_FAILED_REASON = (
    "Zernio draft preflight failed: the complete draft payload failed post "
    "validation."
)
CREATE_FAILED_REASON = "Zernio draft create request failed."
CREATE_AMBIGUOUS_REASON = (
    "Zernio draft create result could not be proven; manifest left in "
    "REVIEW_UNKNOWN. No automatic retry."
)
READBACK_FAILED_REASON = (
    "Zernio draft readback could not prove the created draft; manifest left "
    "in REVIEW_UNKNOWN. No automatic retry."
)
PRESIGN_FAILED_REASON = "Zernio media presign was blocked or unavailable."
UPLOAD_FAILED_REASON = "Zernio media upload failed."
SECRET_MISSING_REASON = (
    "Zernio drafts credential is missing or was rejected."
)
SECRET_UNAVAILABLE_REASON = (
    "Zernio drafts secret could not be read from its runtime source."
)


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class DraftAdapterError(RuntimeError):
    """Base error for the direct Zernio draft adapter."""


class DraftConnectorUnauthorizedError(DraftAdapterError):
    """Missing or rejected credential. Never carries the credential value."""


class DraftConnectorUnavailableError(DraftAdapterError):
    """Bootstrap/dependency/runtime unavailability.

    Must always surface as domain BLOCKED, never SUCCEEDED.
    """


class DraftPreflightBlockedError(DraftAdapterError):
    """Read-only preflight (account/media/post) failed before any create attempt."""


class DraftPresignBlockedError(DraftAdapterError):
    """A media presign request was blocked or returned unusable data."""


class DraftUploadFailedError(DraftAdapterError):
    """A media PUT upload failed or returned an unexpected status."""


class DraftCreateAmbiguousError(DraftAdapterError):
    """The create request may have reached Zernio but the result cannot be proven."""


class DraftReadbackFailedError(DraftAdapterError):
    """Readback could not prove the created draft."""


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


class DraftTransport(Protocol):
    """The only capability this adapter is allowed to use.

    Deliberately limited to GET (read-only) and POST (single create). There
    is no publish/schedule/delete/update method anywhere in this protocol,
    and the connector below never references one. A transport test double
    that implements only get/post/put is therefore a working proof that the
    executor cannot issue a write call other than the single permitted
    draft create.
    """

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        """Return (status_code, decoded_json_body_or_None)."""
        ...

    def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, Any]:
        """Return (status_code, decoded_json_body_or_None)."""
        ...

    def put(
        self,
        url: str,
        *,
        data: bytes,
        headers: dict[str, str] | None = None,
    ) -> int:
        """Return the final HTTP status code. Never authenticated."""
        ...


# ---------------------------------------------------------------------------
# Real HTTPS transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UrllibDraftTransport:
    """Real HTTPS transport for the direct Zernio DraftProvider.

    Not exercised by any offline test in this repository (tests never
    perform network calls); wiring this into a scheduled production
    run is a separate, later step gated behind issue #37. Kept
    intentionally small: it can perform authenticated GET, authenticated
    POST (single create), and unauthenticated PUT (presigned upload).

    The `token` is a SecretValue and its field is excluded from the
    dataclass's autogenerated repr; the explicit __repr__ below also
    never renders it.
    """

    base_url: str
    token: SecretValue = field(repr=False)
    timeout_seconds: float = 15.0

    def __repr__(self) -> str:
        return (
            "UrllibDraftTransport("
            f"base_url={self.base_url!r}, "
            f"token={SECRET_REDACTED_RENDER!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        query = f"?{urllib_parse.urlencode(params)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        req = urllib_request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.token.reveal()}",
                "Accept": "application/json",
            },
        )

        try:
            with urllib_request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                status = resp.status
        except urllib_error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8") if exc.fp else ""
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise DraftConnectorUnavailableError(
                "Zernio drafts HTTPS endpoint was not reachable"
            ) from exc

        try:
            decoded = json.loads(body) if body else None
        except json.JSONDecodeError as exc:
            raise DraftAdapterError(
                "Zernio drafts response was not valid JSON"
            ) from exc

        return status, decoded

    def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = (
            json.dumps(json_body).encode("utf-8") if json_body is not None else None
        )
        req_headers = {
            "Authorization": f"Bearer {self.token.reveal()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if headers:
            req_headers.update(headers)
        if idempotency_key is not None:
            req_headers["X-Request-ID"] = idempotency_key

        req = urllib_request.Request(
            url,
            data=data,
            method="POST",
            headers=req_headers,
        )

        try:
            with urllib_request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                status = resp.status
        except urllib_error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8") if exc.fp else ""
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise DraftConnectorUnavailableError(
                "Zernio drafts HTTPS endpoint was not reachable"
            ) from exc

        try:
            decoded = json.loads(body) if body else None
        except json.JSONDecodeError as exc:
            raise DraftAdapterError(
                "Zernio drafts response was not valid JSON"
            ) from exc

        return status, decoded

    def put(
        self,
        url: str,
        *,
        data: bytes,
        headers: dict[str, str] | None = None,
    ) -> int:
        """PUT exact bytes to a presigned upload URL.

        NEVER authenticated: the presigned URL carries its own signature.
        """
        req_headers = dict(headers or {})
        req = urllib_request.Request(
            url,
            data=data,
            method="PUT",
            headers=req_headers,
        )

        try:
            with urllib_request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return resp.status
        except urllib_error.HTTPError as exc:
            return exc.code
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise DraftConnectorUnavailableError(
                "Zernio media upload endpoint was not reachable"
            ) from exc


def build_authenticated_transport(
    *,
    token: SecretValue,
    base_url: str = DEFAULT_BASE_URL,
) -> UrllibDraftTransport:
    """Construct the real DraftProvider transport from an injected SecretValue.

    This is the only entry point in the adapter that accepts a credential.
    It never reads an environment variable, a secret file, or any proxy of
    the value, and it never logs anything.

    Rejects non-SecretValue tokens as a programming defect (TypeError,
    never a domain result). Rejects blank/whitespace-only tokens with
    DraftConnectorUnauthorizedError so an empty inherited variable surfaces
    as the same domain BLOCKED result as a missing one.
    """

    if not isinstance(token, SecretValue):
        raise TypeError(
            "token must be a nullone_secret_provider.SecretValue"
        )

    if token.reveal() == "" or token.reveal().isspace():
        raise DraftConnectorUnauthorizedError(
            "Zernio drafts credential is missing or was rejected."
        )

    return UrllibDraftTransport(base_url=base_url, token=token)


# ---------------------------------------------------------------------------
# Response envelope validation helpers
# ---------------------------------------------------------------------------


def _require_keys(
    body: Any,
    keys: tuple[str, ...],
    *,
    what: str,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise DraftAdapterError(f"{what} response was not a JSON object")

    missing = [key for key in keys if key not in body]

    if missing:
        raise DraftAdapterError(
            f"{what} response missing required fields: {', '.join(missing)}"
        )

    return body


def _require_bool(body: dict[str, Any], key: str, *, what: str) -> bool:
    value = body.get(key)
    if not isinstance(value, bool):
        raise DraftAdapterError(
            f"{what} response field {key!r} was not a boolean"
        )
    return value


def _require_str(body: dict[str, Any], key: str, *, what: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise DraftAdapterError(
            f"{what} response field {key!r} was not a non-empty string"
        )
    return value


def _validate_account_list(body: Any, *, account_id: str) -> dict[str, Any]:
    """Validate GET /accounts and select the canonical account.

    Requires the canonical NullOne Instagram account to be present and
    eligible/connected according to documented fields. Does not trust
    merely matching platform name.
    """
    body = _require_keys(
        body,
        ("accounts",),
        what="accounts",
    )

    accounts = body["accounts"]
    if not isinstance(accounts, list):
        raise DraftAdapterError(
            "accounts response 'accounts' was not a list"
        )

    for account in accounts:
        if not isinstance(account, dict):
            raise DraftAdapterError(
                "accounts response contained a non-object account entry"
            )

        if account.get("_id") != account_id:
            continue

        platform = account.get("platform")
        if not isinstance(platform, str) or platform.lower() != INSTAGRAM_PLATFORM:
            raise DraftPreflightBlockedError(
                PREFLIGHT_ACCOUNT_FAILED_REASON
            )

        is_active = account.get("isActive")
        if not isinstance(is_active, bool) or not is_active:
            raise DraftPreflightBlockedError(
                PREFLIGHT_ACCOUNT_FAILED_REASON
            )

        return account

    raise DraftPreflightBlockedError(PREFLIGHT_ACCOUNT_FAILED_REASON)


def _validate_media_response(body: Any, *, what: str) -> bool:
    """Validate a media validation response envelope."""
    body = _require_keys(
        body,
        ("status", "valid", "error"),
        what=what,
    )

    status = body["status"]
    if status not in ("READY", "OK", "BLOCKED"):
        raise DraftAdapterError(
            f"{what} response status {status!r} is not recognized"
        )

    valid = _require_bool(body, "valid", what=what)
    return valid


def _validate_post_response(body: Any, *, what: str) -> bool:
    """Validate a post validation response envelope."""
    body = _require_keys(
        body,
        ("status", "valid", "error"),
        what=what,
    )

    status = body["status"]
    if status not in ("READY", "OK", "BLOCKED"):
        raise DraftAdapterError(
            f"{what} response status {status!r} is not recognized"
        )

    valid = _require_bool(body, "valid", what=what)
    return valid


def _validate_presign_response(body: Any) -> tuple[str, str]:
    """Validate a presign response envelope.

    Returns (upload_url, public_url). Both must be http(s) URLs.
    """
    body = _require_keys(
        body,
        ("status", "uploadUrl", "publicUrl", "error"),
        what="presign",
    )

    status = body["status"]
    if status not in ("OK", "BLOCKED"):
        raise DraftAdapterError(
            f"presign response status {status!r} is not recognized"
        )

    if status != "OK":
        raise DraftPresignBlockedError(PRESIGN_FAILED_REASON)

    upload_url = body.get("uploadUrl")
    public_url = body.get("publicUrl")

    if not isinstance(upload_url, str) or not upload_url.startswith("http"):
        raise DraftPresignBlockedError(PRESIGN_FAILED_REASON)

    if not isinstance(public_url, str) or not public_url.startswith("http"):
        raise DraftPresignBlockedError(PRESIGN_FAILED_REASON)

    return upload_url, public_url


def _validate_create_response(body: Any) -> str:
    """Validate a POST /posts create response envelope.

    Returns the post ID. The response must contain a usable post ID.
    """
    body = _require_keys(
        body,
        ("_id",),
        what="create",
    )

    post_id = body.get("_id")
    if not isinstance(post_id, str) or not post_id.strip():
        raise DraftCreateAmbiguousError(CREATE_AMBIGUOUS_REASON)

    return post_id.strip()


def _validate_readback_response(
    body: Any,
    *,
    post_id: str,
    account_id: str,
    fmt: str,
) -> None:
    """Validate the GET /posts/{postId} readback response.

    Requires exact documented evidence that:
    - returned post ID is the created ID
    - status is draft
    - Instagram target is present
    - canonical account ID matches
    - Story platformSpecificData matches when format = STORY
    """
    body = _require_keys(
        body,
        ("_id", "status", "platform", "accountId"),
        what="readback",
    )

    returned_id = body.get("_id")
    if returned_id != post_id:
        raise DraftReadbackFailedError(READBACK_FAILED_REASON)

    status = body.get("status")
    if not isinstance(status, str) or status.lower() != "draft":
        raise DraftReadbackFailedError(READBACK_FAILED_REASON)

    platform = body.get("platform")
    if not isinstance(platform, str) or platform.lower() != INSTAGRAM_PLATFORM:
        raise DraftReadbackFailedError(READBACK_FAILED_REASON)

    returned_account = body.get("accountId")
    if returned_account != account_id:
        raise DraftReadbackFailedError(READBACK_FAILED_REASON)

    if fmt == "STORY":
        psd = body.get("platformSpecificData")
        if not isinstance(psd, dict):
            raise DraftReadbackFailedError(READBACK_FAILED_REASON)
        content_type = psd.get("contentType")
        if content_type != "story":
            raise DraftReadbackFailedError(READBACK_FAILED_REASON)

# ---------------------------------------------------------------------------
# ZernioDraftProvider
# ---------------------------------------------------------------------------


class ZernioDraftProvider:
    """Direct deterministic Zernio REST DraftProvider.

    This is the single write-capable surface for review drafts. It:
    - performs media presign + upload in exact manifest order;
    - performs read-only preflight (account/media/post) BEFORE consuming
      the create attempt;
    - performs exactly ONE POST /posts;
    - performs exactly one readback GET /posts/{postId};
    - never publishes, schedules, retries, or updates-to-publish;
    - maps ambiguity to REVIEW_UNKNOWN with no automatic retry.

    The manifest file is the single source of truth. Every persisted
    mutation is atomic and validator-compatible.
    """

    def __init__(
        self,
        transport: DraftTransport,
        *,
        account_id: str = CANONICAL_ACCOUNT_ID,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._transport = transport
        self._account_id = account_id
        self._base_url = base_url

    # -- public API ----------------------------------------------------------

    def create_review_draft(self, manifest_path) -> None:
        """Create exactly one Zernio review draft for the given manifest.

        Mutates the manifest file atomically to reflect the true resulting
        review state. The caller (pipeline) always reloads the manifest
        afterward and treats it as ground truth, regardless of this
        method's return value or whether it raises.
        """
        manifest_path = resolve_workspace_path(manifest_path)
        m = self._load_manifest(manifest_path)
        self._require_not_created(m)

        # Phase 1: media presign + upload in exact order.
        self._ensure_public_media(manifest_path, m)

        # Phase 2: read-only preflight BEFORE consuming create attempt.
        self._preflight(m)

        # Phase 3: consume the create attempt, then exactly one POST /posts.
        self._persist_attempt(manifest_path, m)
        post_id = self._create_draft(manifest_path, m)

        # Phase 4: exactly one readback.
        self._readback(manifest_path, m, post_id)

    # -- manifest helpers ----------------------------------------------------

    @staticmethod
    def _load_manifest(manifest_path):
        try:
            _, m = load_manifest(manifest_path)
            return m
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(
                f"Could not load manifest: {manifest_path}: {exc}"
            ) from exc

    @staticmethod
    def _require_not_created(m: dict) -> None:
        review = m["review"]

        if review["create_attempts"] != 0:
            raise BridgeError(
                "Review draft create attempt already consumed"
            )

        if review["state"] != "NOT_CREATED":
            raise BridgeError(
                f"Review state is {review['state']}, "
                "expected NOT_CREATED"
            )

        if review.get("zernio_draft_id"):
            raise BridgeError(
                "Manifest already has review draft ID"
            )

    @staticmethod
    def _persist(manifest_path, m: dict) -> None:
        validate_manifest(m)
        atomic_write_json(manifest_path, m)

    def _persist_attempt(self, manifest_path, m: dict) -> None:
        m["review"]["create_attempts"] = 1
        m["review"]["state"] = "CREATE_IN_FLIGHT"
        self._persist(manifest_path, m)

    def _persist_unknown(self, manifest_path, m: dict) -> None:
        m["review"]["state"] = "REVIEW_UNKNOWN"
        self._persist(manifest_path, m)

    def _persist_created(self, manifest_path, m: dict, post_id: str) -> None:
        m["review"]["state"] = "DRAFT_CREATED"
        m["review"]["zernio_draft_id"] = post_id
        m["review"]["created_at"] = now_iso()
        self._persist(manifest_path, m)

    # -- Phase 1: media presign + upload in exact order ----------------------

    def _ensure_public_media(self, manifest_path, m: dict) -> None:
        """Iterate manifest.media IN EXACT ORDER.

        For an item with a valid existing public_url:
        - do not presign again
        - do not upload again
        - reuse it

        For an item without public_url:
        1. request exactly one presigned URL
        2. keep upload URL memory-only
        3. upload exact bytes by PUT
        4. only after confirmed upload success persist public_url
        5. atomically persist manifest

        Never persist/log: upload URL, signed query string, credential.
        """
        changed = False

        for item in m["media"]:
            if item.get("public_url"):
                continue

            upload_url, public_url = self._presign_one(item)
            self._upload_one(upload_url=upload_url, item=item)

            item["public_url"] = public_url
            changed = True

            # Persist only public URL, never signed upload URL.
            self._persist(manifest_path, m)

        if changed:
            validate_manifest(m)

    def _presign_one(self, item: dict) -> tuple[str, str]:
        """Request exactly one presigned URL for a single media item."""
        local_path = resolve_workspace_path(item["local_path"])

        if not local_path.is_file():
            raise BridgeError(f"Media file not found: {local_path}")

        data = local_path.read_bytes()

        try:
            status, body = self._transport.post(
                "/media/presign",
                json_body={
                    "filename": local_path.name,
                    "contentType": item["content_type"],
                    "size": len(data),
                },
            )
        except DraftConnectorUnavailableError:
            raise
        except Exception as exc:
            raise DraftAdapterError(
                "Zernio media presign request failed"
            ) from exc

        if status in (401, 403):
            raise DraftConnectorUnauthorizedError(SECRET_MISSING_REASON)

        if status >= 500 or status == 0:
            raise DraftConnectorUnavailableError(
                "Zernio media presign endpoint was not reachable"
            )

        if status != 200:
            raise DraftPresignBlockedError(PRESIGN_FAILED_REASON)

        try:
            return _validate_presign_response(body)
        except DraftAdapterError:
            raise
        except Exception as exc:
            raise DraftPresignBlockedError(PRESIGN_FAILED_REASON) from exc

    def _upload_one(self, *, upload_url: str, item: dict) -> None:
        """Upload exact bytes by PUT to the presigned URL.

        The upload URL is memory-only. Never persisted or logged.
        Bearer auth is NEVER sent to the presigned upload host.
        """
        path = resolve_workspace_path(item["local_path"])
        data = path.read_bytes()

        try:
            status = self._transport.put(
                upload_url,
                data=data,
                headers={
                    "Content-Type": item["content_type"],
                    "Content-Length": str(len(data)),
                },
            )
        except DraftConnectorUnavailableError:
            raise
        except Exception as exc:
            raise DraftUploadFailedError(UPLOAD_FAILED_REASON) from exc

        if status not in (200, 201, 204):
            raise DraftUploadFailedError(UPLOAD_FAILED_REASON)

    # -- Phase 2: read-only preflight BEFORE create attempt ------------------

    def _preflight(self, m: dict) -> None:
        """Read-only preflight: account, media, and complete post validation.

        If any account/media/post validation fails:
        - NO draft create attempt
        - review.create_attempts remains 0
        - review.state remains NOT_CREATED
        - no POST /posts
        - fail closed
        """
        # A. account check
        self._preflight_account()

        # B. media validation
        self._preflight_media(m)

        # C. complete post validation
        self._preflight_post(m)

    def _preflight_account(self) -> None:
        """GET /accounts and require the canonical NullOne Instagram account
        to be present and eligible/connected.
        """
        try:
            status, body = self._transport.get("/accounts")
        except DraftConnectorUnavailableError:
            raise
        except Exception as exc:
            raise DraftConnectorUnavailableError(
                "Zernio accounts endpoint was not reachable"
            ) from exc

        if status in (401, 403):
            raise DraftConnectorUnauthorizedError(SECRET_MISSING_REASON)

        if status >= 500 or status == 0:
            raise DraftConnectorUnavailableError(
                "Zernio accounts endpoint was not reachable"
            )

        if status != 200:
            raise DraftPreflightBlockedError(PREFLIGHT_ACCOUNT_FAILED_REASON)

        try:
            _validate_account_list(body, account_id=self._account_id)
        except DraftAdapterError:
            raise
        except Exception as exc:
            raise DraftPreflightBlockedError(PREFLIGHT_ACCOUNT_FAILED_REASON) from exc

    def _preflight_media(self, m: dict) -> None:
        """Validate every final public media URL with the documented
        media validation endpoint.
        """
        for item in m["media"]:
            public_url = item.get("public_url")
            if not isinstance(public_url, str) or not public_url:
                raise DraftPreflightBlockedError(PREFLIGHT_MEDIA_FAILED_REASON)

            try:
                status, body = self._transport.post(
                    "/tools/validate/media",
                    json_body={
                        "url": public_url,
                        "platform": INSTAGRAM_PLATFORM,
                    },
                )
            except DraftConnectorUnavailableError:
                raise
            except Exception as exc:
                raise DraftConnectorUnavailableError(
                    "Zernio media validation endpoint was not reachable"
                ) from exc

            if status in (401, 403):
                raise DraftConnectorUnauthorizedError(SECRET_MISSING_REASON)

            if status >= 500 or status == 0:
                raise DraftConnectorUnavailableError(
                    "Zernio media validation endpoint was not reachable"
                )

            if status != 200:
                raise DraftPreflightBlockedError(PREFLIGHT_MEDIA_FAILED_REASON)

            try:
                valid = _validate_media_response(body, what="media validation")
            except DraftAdapterError:
                raise
            except Exception as exc:
                raise DraftPreflightBlockedError(PREFLIGHT_MEDIA_FAILED_REASON) from exc

            if not valid:
                raise DraftPreflightBlockedError(PREFLIGHT_MEDIA_FAILED_REASON)

    def _preflight_post(self, m: dict) -> None:
        """Construct the EXACT eventual draft payload and submit it to the
        documented post validation endpoint.

        Validation body preserves exact:
        - content/caption
        - media order
        - platform
        - canonical account ID
        - platformSpecificData
        - Story content type where applicable
        """
        payload = self._build_draft_payload(m)

        try:
            status, body = self._transport.post(
                "/tools/validate/post",
                json_body=payload,
            )
        except DraftConnectorUnavailableError:
            raise
        except Exception as exc:
            raise DraftConnectorUnavailableError(
                "Zernio post validation endpoint was not reachable"
            ) from exc

        if status in (401, 403):
            raise DraftConnectorUnauthorizedError(SECRET_MISSING_REASON)

        if status >= 500 or status == 0:
            raise DraftConnectorUnavailableError(
                "Zernio post validation endpoint was not reachable"
            )

        if status != 200:
            raise DraftPreflightBlockedError(PREFLIGHT_POST_FAILED_REASON)

        try:
            valid = _validate_post_response(body, what="post validation")
        except DraftAdapterError:
            raise
        except Exception as exc:
            raise DraftPreflightBlockedError(PREFLIGHT_POST_FAILED_REASON) from exc

        if not valid:
            raise DraftPreflightBlockedError(PREFLIGHT_POST_FAILED_REASON)

    # -- Phase 3: exactly one POST /posts ------------------------------------

    def _build_draft_payload(self, m: dict) -> dict[str, Any]:
        """Build the EXACT draft payload for preflight and create.

        For draft creation:
        - isDraft = true
        - Instagram platform
        - canonical account ID
        - exact caption/content
        - exact media order

        For STORY:
        platformSpecificData.contentType = "story"

        For FEED/CAROUSEL:
        do not set Story contentType

        MUST NOT include or invoke:
        - publishNow = true
        - scheduledFor
        - queued scheduling
        - publish endpoint
        - retry endpoint
        - update-to-publish behavior
        """
        caption_path = resolve_workspace_path(m["caption"]["file"])
        caption = caption_path.read_text(encoding="utf-8")

        media_urls = [
            item["public_url"]
            for item in m["media"]
        ]

        payload: dict[str, Any] = {
            "platform": INSTAGRAM_PLATFORM,
            "accountId": self._account_id,
            "isDraft": True,
            "content": caption,
            "media": [
                {"url": url}
                for url in media_urls
            ],
        }

        if m["format"] == "STORY":
            payload["platformSpecificData"] = {
                "contentType": "story",
            }

        return payload

    def _create_draft(self, manifest_path, m: dict) -> str:
        """Perform exactly ONE POST /posts.

        No application retry loop.
        No HTTP retry loop for POST.
        No second POST after timeout, connection reset, malformed response,
        5xx, or ambiguous result.

        If the draft-create request may have reached Zernio but the result
        cannot be proven, persist REVIEW_UNKNOWN and raise
        DraftCreateAmbiguousError.
        """
        payload = self._build_draft_payload(m)
        idempotency_key = self._request_id(manifest_path, m)

        try:
            status, body = self._transport.post(
                "/posts",
                json_body=payload,
                idempotency_key=idempotency_key,
            )
        except DraftConnectorUnavailableError:
            self._persist_unknown(manifest_path, m)
            raise DraftCreateAmbiguousError(CREATE_AMBIGUOUS_REASON) from None
        except Exception:
            # Ambiguity: the request may have reached Zernio.
            self._persist_unknown(manifest_path, m)
            raise DraftCreateAmbiguousError(CREATE_AMBIGUOUS_REASON) from None

        if status in (401, 403):
            self._persist_unknown(manifest_path, m)
            raise DraftConnectorUnauthorizedError(SECRET_MISSING_REASON)

        if status >= 500 or status == 0:
            # Ambiguity: uncertain 5xx semantics.
            self._persist_unknown(manifest_path, m)
            raise DraftCreateAmbiguousError(CREATE_AMBIGUOUS_REASON)

        if status != 201 and status != 200:
            # Ambiguity: unexpected status.
            self._persist_unknown(manifest_path, m)
            raise DraftCreateAmbiguousError(CREATE_AMBIGUOUS_REASON)

        try:
            post_id = _validate_create_response(body)
        except DraftCreateAmbiguousError:
            self._persist_unknown(manifest_path, m)
            raise
        except Exception:
            self._persist_unknown(manifest_path, m)
            raise DraftCreateAmbiguousError(CREATE_AMBIGUOUS_REASON) from None

        return post_id

    @staticmethod
    def _request_id(manifest_path, m: dict) -> str:
        """Stable per-manifest x-request-id as defense-in-depth.

        MUST NOT be used as justification for a NullOne retry.
        NullOne still makes max one create request.
        """
        manifest_id = m.get("manifest_id", "unknown")
        return f"nullone-draft-{manifest_id}-{uuid.uuid4().hex[:16]}"

    # -- Phase 4: exactly one readback ---------------------------------------

    def _readback(self, manifest_path, m: dict, post_id: str) -> None:
        """Perform exactly one GET /posts/{postId}.

        Require exact documented evidence that:
        - returned post ID is the created ID
        - status is draft
        - Instagram target is present
        - canonical account ID matches
        - Story platformSpecificData matches when format = STORY
        - payload/media identity is consistent to the extent the API exposes it

        If readback is missing, contradictory, unavailable or malformed:
        REVIEW_UNKNOWN, no retry.
        """
        path = f"/posts/{post_id}"

        try:
            status, body = self._transport.get(path)
        except DraftConnectorUnavailableError:
            self._persist_unknown(manifest_path, m)
            raise DraftReadbackFailedError(READBACK_FAILED_REASON) from None
        except Exception:
            self._persist_unknown(manifest_path, m)
            raise DraftReadbackFailedError(READBACK_FAILED_REASON) from None

        if status in (401, 403):
            self._persist_unknown(manifest_path, m)
            raise DraftConnectorUnauthorizedError(SECRET_MISSING_REASON)

        if status >= 500 or status == 0:
            self._persist_unknown(manifest_path, m)
            raise DraftReadbackFailedError(READBACK_FAILED_REASON)

        if status != 200:
            self._persist_unknown(manifest_path, m)
            raise DraftReadbackFailedError(READBACK_FAILED_REASON)

        try:
            _validate_readback_response(
                body,
                post_id=post_id,
                account_id=self._account_id,
                fmt=m["format"],
            )
        except DraftReadbackFailedError:
            self._persist_unknown(manifest_path, m)
            raise
        except Exception:
            self._persist_unknown(manifest_path, m)
            raise DraftReadbackFailedError(READBACK_FAILED_REASON) from None

        # Only after successful readback persist DRAFT_CREATED.
        self._persist_created(manifest_path, m, post_id)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_direct_draft_provider(
    *,
    secret_provider: SecretProvider | None = None,
) -> ZernioDraftProvider:
    """Construct the production ZernioDraftProvider behind the reviewed
    secret boundary.

    Construction performs no network calls and writes nothing to disk.
    Converts typed missing/unavailable secret conditions into sanitized
    draft-provider failures. Never exposes credential material.
    """
    provider: SecretProvider = (
        secret_provider if secret_provider is not None
        else EnvironmentSecretProvider()
    )

    try:
        token = provider.get_required(SECRET_ID_ZERNIO_DRAFTS_BEARER)
    except SecretNotConfiguredError:
        raise DraftConnectorUnauthorizedError(SECRET_MISSING_REASON) from None
    except SecretUnavailableError:
        raise DraftConnectorUnavailableError(SECRET_UNAVAILABLE_REASON) from None

    transport = build_authenticated_transport(token=token)

    return ZernioDraftProvider(transport)

from nullone_secret_provider import (  # noqa: E402
    EnvironmentSecretProvider,
    SECRET_ID_ZERNIO_DRAFTS_BEARER,
)


# ---------------------------------------------------------------------------
# Self-test (offline, no fixtures, no network)
# ---------------------------------------------------------------------------


def self_test() -> int:
    """Offline self-test: no network, no real credential."""
    marker = "FAKE_ZERNIO_DRAFT_SECRET_DO_NOT_LOG_123"

    # SecretValue never renders.
    from nullone_secret_provider import SecretValue

    secret = SecretValue(marker)
    assert repr(secret) == f"SecretValue({SECRET_REDACTED_RENDER!r})"
    assert str(secret) == SECRET_REDACTED_RENDER
    assert f"{secret}" == SECRET_REDACTED_RENDER
    assert marker not in repr(secret)
    assert marker not in str(secret)
    assert secret.reveal() == marker

    # Missing secret -> DraftConnectorUnauthorizedError (BLOCKED).
    class MissingProvider:
        def get_required(self, secret_id):
            raise SecretNotConfiguredError("not configured")

    try:
        build_direct_draft_provider(secret_provider=MissingProvider())
        raise AssertionError("missing draft secret did not fail closed")
    except DraftConnectorUnauthorizedError as exc:
        assert str(exc) == SECRET_MISSING_REASON

    # Unknown secret id -> missing-secret (BLOCKED).
    class UnknownIdProvider:
        def get_required(self, secret_id):
            raise SecretNotConfiguredError("unknown secret id")

    try:
        build_direct_draft_provider(secret_provider=UnknownIdProvider())
        raise AssertionError("unknown draft secret id did not fail closed")
    except DraftConnectorUnauthorizedError:
        pass

    # Typed SecretUnavailableError -> DraftConnectorUnavailableError, sanitized.
    class UnavailableProvider:
        def get_required(self, secret_id):
            raise SecretUnavailableError(
                f"credential store unreachable with value {marker}"
            )

    try:
        build_direct_draft_provider(secret_provider=UnavailableProvider())
        raise AssertionError("unavailable draft secret did not fail closed")
    except DraftConnectorUnavailableError as exc:
        assert str(exc) == SECRET_UNAVAILABLE_REASON
        assert marker not in str(exc)

    # Unexpected programming defect (RuntimeError) -> propagates.
    class BrokenProvider:
        def get_required(self, secret_id):
            raise RuntimeError("secret daemon died")

    try:
        build_direct_draft_provider(secret_provider=BrokenProvider())
        raise AssertionError("factory swallowed RuntimeError")
    except RuntimeError as exc:
        assert "secret daemon died" in str(exc)

    # Present secret -> provider constructed; value never leaks.
    class PresentEnv(EnvironmentSecretProvider):
        def __init__(self):
            self._environ = {
                EnvironmentSecretProvider.bound_env_var(
                    SECRET_ID_ZERNIO_DRAFTS_BEARER
                ): marker,
            }

    provider = build_direct_draft_provider(secret_provider=PresentEnv())
    assert isinstance(provider, ZernioDraftProvider)
    assert marker not in repr(provider)
    assert marker not in repr(provider._transport)
    assert "redacted" in repr(provider._transport)
    assert type(provider._transport.token) is SecretValue

    # Draft credential separate from Analytics credential.
    assert SECRET_ID_ZERNIO_DRAFTS_BEARER != "zernio.analytics.bearer"
    assert (
        EnvironmentSecretProvider.bound_env_var(SECRET_ID_ZERNIO_DRAFTS_BEARER)
        == "ZERNIO_DRAFT_API_TOKEN"
    )

    print("ZERNIO_DRAFT_ADAPTER_SELF_TEST=PASS")
    print("SECRET_REDACTED=TRUE")
    print("NO_NETWORK=TRUE")
    print("NO_PUBLISH_CAPABILITY=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Direct deterministic Zernio REST DraftProvider (#81)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
