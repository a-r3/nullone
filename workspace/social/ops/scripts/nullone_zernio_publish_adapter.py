#!/usr/bin/env python3
"""Direct deterministic Zernio REST publisher adapter (#90).

This module is the ONLY place that knows about the Zernio publication
HTTPS paths, the promotion payload, publication response envelopes, and
the publication credential. It replaces the previous model-backed path:

    bridge -> run_structured -> claude -p -> mcp__zernio__posts_publish_now

with a deterministic, testable direct HTTPS path:

    bridge -> nullone_publish_provider_factory -> ZernioPublishProvider
    (this module) -> HTTPS PUT

Endpoint contract (Zernio OpenAPI 3.1.0 / 1.0.4, base
https://zernio.com/api/v1):

- GET /posts/{postId} (getPost) -> {post: {_id, status, platforms:
  [{platform, accountId, platformSpecificData, ...}], content,
  mediaItems, ...}}. Read-only; used for the remote-draft preflight
  BEFORE any attempt is consumed and for the readback AFTER the one PUT.
- PUT /posts/{postId} (updatePost, publishing group) with
  {isDraft: false, publishNow: true} promotes the existing review draft
  to immediate publication. Documented TRAP: scheduledFor without
  isDraft:false returns 200 but the post REMAINS a draft -- the adapter
  always sends explicit isDraft:false + publishNow:true and never sends
  scheduling fields.
- IDEMPOTENCY: x-request-id (~5-min window) and 409 content-dedup are
  documented ONLY for POST create. PUT params = {postId} only; PUT 409 =
  queue_slot_conflict. The adapter sends NO idempotency header: safety is
  attempts=1 persisted BEFORE the PUT (owned by the bridge) + exactly ONE
  PUT + no transport retry + ambiguity -> UNKNOWN + readback is
  clarification-only.
- PUT statuses (documented only): 200 -> verify non-draft + readback;
  207 (explicit 2xx trap -- branched on code, never treated as
  PUBLISHED) -> post.status partial/failed/scheduled (scheduled =
  provider-side auto-retry, PUBLISHING-family, never a second PUT;
  platformResults may be absent, platforms[] always present);
  4xx + valid envelope (400/401/403/404/409) -> definitely rejected ->
  FAILED terminal; 5xx / timeout / network / malformed / unexpected ->
  AMBIGUOUS -> UNKNOWN. Never a second PUT.
- Immediate publish returns platformPostUrl; truth comes from post.status
  + GET readback. The adapter never fabricates platform post ids,
  permalinks, or provider metadata: permalink is copied ONLY from a
  documented platformPostUrl string when actually present, and
  platform_post_id stays empty unless a documented field provides it.

Transport capability is narrowly allowlisted by construction:

- exact host https://zernio.com, HTTPS only;
- exact paths GET /posts/<24-hex-id> and PUT /posts/<24-hex-id>;
- redirects are NEVER followed (a redirect response is a typed error,
  never a followed request -- Authorization can never leak to another
  host);
- finite connect/read timeout, bounded response body;
- bearer header only to the exact Zernio host;
- no automatic retry, no POST, no DELETE, no arbitrary path.

Credentials never enter this module as an environment variable,
credential file, or literal. The transport receives a SecretValue token
at construction time via build_authenticated_transport, which is the
only function in this adapter that accepts one. The SecretValue wrapper
makes an accidental print impossible: repr()/str()/format() render the
fixed token <redacted> and the value is exposed only through the narrow
reveal() used by the transport to build the Authorization header.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from nullone_bridge_common import (
    CANONICAL_ACCOUNT_ID,
    resolve_workspace_path,
)
from nullone_secret_provider import (
    SECRET_ID_ZERNIO_PUBLISH_BEARER,
    SECRET_REDACTED_RENDER,
    EnvironmentSecretProvider,
    SecretNotConfiguredError,
    SecretProvider,
    SecretUnavailableError,
    SecretValue,
)

# Confirmed base URL. This adapter never reads it from any environment
# variable; build_authenticated_transport uses it as the default.
DEFAULT_BASE_URL = "https://zernio.com/api/v1"

# Exact host allowlist: a single entry. The transport refuses any base
# URL whose scheme is not https or whose hostname is not exactly this.
EXACT_HOST = "zernio.com"

# Exact-path allowlist for the publication transport. Both the read-only
# preflight/readback GET and the single promotion PUT must match exactly
# one review-post path. Any other path fails locally before any request
# is constructed or sent: no HTTP attempt, no credential use, no provider
# call. Exact full-match only.
POST_PATH_RE = re.compile(r"^/posts/[0-9a-fA-F]{24}$")

# Canonical Instagram platform value used by Zernio REST.
INSTAGRAM_PLATFORM = "instagram"

# Bounded response body: review/post envelopes are small JSON. Anything
# larger is untrustworthy and fails closed as malformed.
MAX_BODY_BYTES = 262144

# Fixed, generic reason texts. Never derived from a provider exception
# and never carrying response bodies (which could contain anything).
PREFLIGHT_MISMATCH_REASON = (
    "Zernio remote draft does not match the approved manifest."
)
PREFLIGHT_UNAVAILABLE_REASON = (
    "Zernio remote draft preflight endpoint was not reachable."
)
PROMOTE_AMBIGUOUS_REASON = (
    "Zernio publication result could not be proven; retry forbidden."
)
PROMOTE_REJECTED_REASON = (
    "Zernio publication request was definitely rejected."
)
READBACK_FAILED_REASON = (
    "Publish accepted but readback failed; retry forbidden."
)
SECRET_MISSING_REASON = (
    "Zernio publish credential is missing or was rejected."
)
SECRET_UNAVAILABLE_REASON = (
    "Zernio publish secret could not be read from its runtime source."
)


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class PublishAdapterError(RuntimeError):
    """Base error for the direct Zernio publisher adapter."""


class PublishConnectorUnauthorizedError(PublishAdapterError):
    """Missing or rejected credential. Never carries the credential value."""


class PublishConnectorUnavailableError(PublishAdapterError):
    """Bootstrap/dependency/runtime unavailability.

    Must always surface as domain BLOCKED before any attempt, never
    SUCCEEDED.
    """


class PublishPreflightBlockedError(PublishAdapterError):
    """Remote-draft preflight contradiction before any attempt.

    Attempts remain 0, zero PUT issued.
    """


class PublishRedirectRefusedError(PublishAdapterError):
    """A redirect response was received and deliberately not followed.

    Authorization material must never travel to another host. Treated as
    ambiguity (UNKNOWN), never as success or rejection.
    """


class PublishAmbiguousError(PublishAdapterError):
    """The promotion request may have reached Zernio but the result cannot
    be proven (timeout, network failure, 5xx, malformed or unexpected
    response). Attempts are consumed; retry is forbidden."""


class PublishRejectedError(PublishAdapterError):
    """The promotion request was definitely rejected (documented 4xx with
    a valid envelope). Terminal FAILED; retry is forbidden."""


class PublishReadbackFailedError(PublishAdapterError):
    """Readback could not prove the publication result."""


# ---------------------------------------------------------------------------
# Minimal promotion payload
# ---------------------------------------------------------------------------


def build_promote_payload() -> dict[str, Any]:
    """The ONE minimal promotion payload required by the PUT contract.

    Exactly {"isDraft": False, "publishNow": True} -- no caption, no
    media, no scheduling fields, no idempotency header claim. A fresh
    dict per call so callers can never alias shared state.
    """
    return {"isDraft": False, "publishNow": True}


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


class PublishTransport(Protocol):
    """The only capability the publisher is allowed to use.

    Deliberately limited to GET (read-only preflight/readback) and PUT
    (the single promotion). There is no post/delete/patch/schedule
    method anywhere in this protocol, and the provider below never
    references one. A transport test double that implements only
    get/put is therefore a working proof that the executor cannot issue
    any other write call.
    """

    def get(self, path: str) -> tuple[int, Any]:
        """Return (status_code, decoded_json_body_or_None)."""
        ...

    def put(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
    ) -> tuple[int, Any]:
        """Return (status_code, decoded_json_body_or_None)."""
        ...


# ---------------------------------------------------------------------------
# Real HTTPS transport
# ---------------------------------------------------------------------------


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it.

    urllib's default redirect handling would re-issue the request to the
    Location URL; Authorization must never travel there. Raising here
    turns any 301/302/303/307/308 into a typed local error before any
    second request exists.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise PublishRedirectRefusedError(
            "Zernio publication transport refused a redirect response"
        )


def _check_base_url(base_url: str) -> str:
    """Validate the exact HTTPS host. Returns the normalized base URL."""
    try:
        parts = urllib_parse.urlsplit(base_url)
    except Exception as exc:
        raise PublishAdapterError(
            "Zernio publication base URL is invalid"
        ) from exc
    if parts.scheme != "https" or parts.hostname != EXACT_HOST:
        raise PublishAdapterError(
            "Zernio publication transport allows only the exact host"
        )
    return base_url.rstrip("/")


def _check_post_path(path: str) -> str:
    """Validate the exact review-post path. Returns the path unchanged."""
    if not isinstance(path, str) or POST_PATH_RE.match(path) is None:
        raise PublishAdapterError(
            "Publication transport path is outside the allowlist"
        )
    return path


@dataclass(frozen=True)
class UrllibPublishTransport:
    """Real HTTPS transport for the deterministic Zernio publisher.

    Not exercised by any offline test in this repository (tests never
    perform network calls); wiring this into a scheduled production
    run is a separate, later step gated behind issue #37. Kept
    intentionally small: authenticated GET plus exactly one shape of
    authenticated PUT.

    The `token` is a SecretValue and its field is excluded from the
    dataclass's autogenerated repr; the explicit __repr__ below also
    never renders it.
    """

    base_url: str
    token: SecretValue = field(repr=False)
    timeout_seconds: float = 15.0
    max_body_bytes: int = MAX_BODY_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _check_base_url(self.base_url))
        if not isinstance(self.timeout_seconds, (int, float)) or not (
            self.timeout_seconds > 0
        ):
            raise PublishAdapterError(
                "Zernio publication transport requires a finite timeout"
            )

    def __repr__(self) -> str:
        return (
            "UrllibPublishTransport("
            f"base_url={self.base_url!r}, "
            f"token={SECRET_REDACTED_RENDER!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )

    def _opener(self) -> Any:
        # A private opener with redirect-following disabled. The shared
        # global opener is never used here.
        return urllib_request.build_opener(_NoRedirectHandler())

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        _check_post_path(path)
        url = f"{self.base_url}{path}"
        data = (
            json.dumps(json_body).encode("utf-8")
            if json_body is not None
            else None
        )
        req = urllib_request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token.reveal()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener().open(
                req, timeout=self.timeout_seconds
            ) as resp:
                status = resp.status
                raw = resp.read(self.max_body_bytes + 1)
        except urllib_error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(self.max_body_bytes + 1)
            except Exception:
                raw = b""
        except PublishRedirectRefusedError:
            raise
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise PublishConnectorUnavailableError(
                "Zernio publication HTTPS endpoint was not reachable"
            ) from exc

        if len(raw) > self.max_body_bytes:
            raise PublishAdapterError(
                "Zernio publication response exceeded the body bound"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PublishAdapterError(
                "Zernio publication response was not valid UTF-8"
            ) from exc
        if not text:
            return status, None
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PublishAdapterError(
                "Zernio publication response was not valid JSON"
            ) from exc
        return status, decoded

    def get(self, path: str) -> tuple[int, Any]:
        return self._request("GET", path)

    def put(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
    ) -> tuple[int, Any]:
        if not isinstance(json_body, dict):
            raise PublishAdapterError(
                "Zernio publication PUT body must be a JSON object"
            )
        return self._request("PUT", path, json_body=json_body)


def build_authenticated_transport(
    *,
    token: SecretValue,
    base_url: str = DEFAULT_BASE_URL,
) -> UrllibPublishTransport:
    """Construct the real publisher transport from an injected SecretValue.

    This is the only entry point in the adapter that accepts a
    credential. It never reads an environment variable, a secret file,
    or any proxy of the value, and it never logs anything.

    Rejects non-SecretValue tokens as a programming defect (TypeError,
    never a domain result) so a plain environment string alone can never
    satisfy the publish credential. Rejects blank/whitespace-only tokens
    with PublishConnectorUnauthorizedError so an empty inherited
    variable surfaces as the same domain BLOCKED result as a missing
    one.
    """

    if not isinstance(token, SecretValue):
        raise TypeError(
            "token must be a nullone_secret_provider.SecretValue"
        )

    if token.reveal() == "" or token.reveal().isspace():
        raise PublishConnectorUnauthorizedError(SECRET_MISSING_REASON)

    return UrllibPublishTransport(base_url=base_url, token=token)


# ---------------------------------------------------------------------------
# Manifest-derived expectations
# ---------------------------------------------------------------------------


def _media_item_type(content_type: Any) -> str:
    """Derive the Zernio MediaItem.type from a manifest media MIME type.

    Same deterministic authority as the draft adapter: image MIME types
    map to image/gif, video/* to video, application/pdf to document.
    Anything else fails closed (preflight BLOCKED, attempts untouched).
    """
    if not isinstance(content_type, str):
        raise PublishPreflightBlockedError(PREFLIGHT_MISMATCH_REASON)
    normalized = content_type.strip().lower()
    if normalized in ("image/jpeg", "image/jpg", "image/png", "image/webp"):
        return "image"
    if normalized in ("image/gif",):
        return "gif"
    if normalized.startswith("video/"):
        return "video"
    if normalized in ("application/pdf",):
        return "document"
    raise PublishPreflightBlockedError(PREFLIGHT_MISMATCH_REASON)


def build_expected_snapshot(manifest: dict[str, Any]) -> dict[str, Any]:
    """Derive the exact remote-draft expectations from the manifest.

    Returns {"post_id", "caption", "media_items": [{"type", "url"}],
    "account_id", "format"}. Raises PublishPreflightBlockedError when
    the manifest cannot supply exact expectations (missing caption file,
    missing public URLs, malformed review id). Never performs network
    calls. Never modifies the remote draft.
    """
    try:
        review = manifest["review"]
        post_id = review.get("zernio_draft_id")
    except (KeyError, TypeError, AttributeError):
        raise PublishPreflightBlockedError(
            PREFLIGHT_MISMATCH_REASON
        ) from None
    if (
        not isinstance(post_id, str)
        or len(post_id) != 24
        or any(c not in "0123456789abcdefABCDEF" for c in post_id)
    ):
        raise PublishPreflightBlockedError(PREFLIGHT_MISMATCH_REASON)

    try:
        caption_path = resolve_workspace_path(manifest["caption"]["file"])
        caption = caption_path.read_text(encoding="utf-8")
    except (KeyError, TypeError, AttributeError, OSError):
        raise PublishPreflightBlockedError(
            PREFLIGHT_MISMATCH_REASON
        ) from None

    try:
        media = manifest["media"]
    except (KeyError, TypeError):
        raise PublishPreflightBlockedError(
            PREFLIGHT_MISMATCH_REASON
        ) from None
    if not isinstance(media, list) or not media:
        raise PublishPreflightBlockedError(PREFLIGHT_MISMATCH_REASON)
    media_items: list[dict[str, Any]] = []
    for item in media:
        if not isinstance(item, dict):
            raise PublishPreflightBlockedError(PREFLIGHT_MISMATCH_REASON)
        public_url = item.get("public_url")
        if not isinstance(public_url, str) or not public_url:
            raise PublishPreflightBlockedError(PREFLIGHT_MISMATCH_REASON)
        media_items.append(
            {
                "type": _media_item_type(item.get("content_type")),
                "url": public_url,
            }
        )

    fmt = manifest.get("format")
    if fmt not in ("FEED", "CAROUSEL", "STORY", "REEL"):
        raise PublishPreflightBlockedError(PREFLIGHT_MISMATCH_REASON)

    return {
        "post_id": post_id,
        "caption": caption,
        "media_items": media_items,
        "account_id": CANONICAL_ACCOUNT_ID,
        "format": fmt,
    }


# ---------------------------------------------------------------------------
# Remote-draft validation (preflight + readback share one checker)
# ---------------------------------------------------------------------------


def _resolve_account_id(value: Any) -> str | None:
    """Resolve the canonical account id from the documented representation.

    GET /posts/{id} echoes accountId as an expanded SocialAccount object
    ({_id, ...}) or a plain string. Returns the resolved string id, or
    None when unresolvable.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        resolved = value.get("_id")
        if isinstance(resolved, str):
            return resolved
    return None


def _check_remote_draft(
    body: Any,
    *,
    expected: dict[str, Any],
    error_cls: type[PublishAdapterError],
    reason: str,
    require_draft: bool,
) -> dict[str, Any]:
    """Validate one GET /posts/{postId} envelope against expectations.

    Requires where the current API exposes the field: same review post
    id, draft status (preflight only), exact caption/content, exact
    ordered media items (url + type where exposed), exactly one
    Instagram target resolving to the canonical account, STORY
    settings/contentType when STORY (and never story-typed for
    non-STORY), and no extra publication target. Fields the API does not
    expose are not invented as proof; any present-but-contradictory
    field fails closed.

    Returns a summary {"live_status", "platform_status",
    "platform_post_url"} for truth classification. Never fabricates:
    absent fields become "" / None.
    """
    if not isinstance(body, dict):
        raise error_cls(reason)
    post = body.get("post")
    if not isinstance(post, dict):
        raise error_cls(reason)

    if post.get("_id") != expected["post_id"]:
        raise error_cls(reason)

    status = post.get("status")
    if not isinstance(status, str) or not status:
        raise error_cls(reason)
    live_status = status.lower()
    if require_draft and live_status != "draft":
        raise error_cls(reason)

    platforms = post.get("platforms")
    if not isinstance(platforms, list) or len(platforms) != 1:
        # Exactly one intended target: any extra, duplicate, or missing
        # target contradicts the approved manifest.
        raise error_cls(reason)
    entry = platforms[0]
    if not isinstance(entry, dict):
        raise error_cls(reason)
    platform = entry.get("platform")
    if not isinstance(platform, str) or platform.lower() != INSTAGRAM_PLATFORM:
        raise error_cls(reason)
    if _resolve_account_id(entry.get("accountId")) != expected["account_id"]:
        raise error_cls(reason)

    psd = entry.get("platformSpecificData")
    if expected["format"] == "STORY":
        if psd is not None:
            if not isinstance(psd, dict):
                raise error_cls(reason)
            if "contentType" in psd and psd.get("contentType") != "story":
                raise error_cls(reason)
    else:
        if isinstance(psd, dict) and psd.get("contentType") == "story":
            raise error_cls(reason)

    if "content" in post and post.get("content") != expected["caption"]:
        raise error_cls(reason)

    if "mediaItems" in post:
        exposed = post.get("mediaItems")
        if not isinstance(exposed, list):
            raise error_cls(reason)
        want = expected["media_items"]
        if len(exposed) != len(want):
            raise error_cls(reason)
        for got, want_item in zip(exposed, want):
            if not isinstance(got, dict):
                raise error_cls(reason)
            if got.get("url") != want_item.get("url"):
                raise error_cls(reason)
            if "type" in got and got.get("type") != want_item.get("type"):
                raise error_cls(reason)

    platform_status = ""
    raw_platform_status = entry.get("status")
    if isinstance(raw_platform_status, str):
        platform_status = raw_platform_status.lower()

    platform_post_url = post.get("platformPostUrl")
    if not isinstance(platform_post_url, str) or not platform_post_url:
        platform_post_url = None

    return {
        "live_status": live_status,
        "platform_status": platform_status,
        "platform_post_url": platform_post_url,
    }


def validate_preflight_remote_draft(
    body: Any, *, expected: dict[str, Any]
) -> None:
    """Preflight gate: remote draft must still be the approved draft."""
    _check_remote_draft(
        body,
        expected=expected,
        error_cls=PublishPreflightBlockedError,
        reason=PREFLIGHT_MISMATCH_REASON,
        require_draft=True,
    )


# ---------------------------------------------------------------------------
# ZernioPublishProvider
# ---------------------------------------------------------------------------


class ZernioPublishProvider:
    """Direct deterministic Zernio REST publisher.

    The single consequential-write surface for final publication. It:

    - performs a read-only remote-draft preflight BEFORE the caller may
      consume the publication attempt;
    - performs exactly ONE PUT /posts/{postId} with the minimal
      promotion payload (no idempotency-header claim);
    - performs exactly one read-only readback GET /posts/{postId} that
      clarifies truth but can never authorize another PUT;
    - maps ambiguity (timeout, network failure, 5xx, malformed or
      unexpected response) to UNKNOWN with no retry;
    - maps documented definite rejection (4xx + valid envelope) to
      terminal FAILED;
    - never fabricates platform post ids, permalinks, or metadata.

    The provider never touches the manifest file: attempt ownership and
    state persistence belong to the bridge caller, which persists
    attempts=1 BEFORE calling promote_once.
    """

    def __init__(
        self,
        transport: PublishTransport,
        *,
        account_id: str = CANONICAL_ACCOUNT_ID,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._transport = transport
        self._account_id = account_id
        self._base_url = base_url

    # -- Phase 1: read-only remote draft preflight ---------------------------

    def preflight(
        self, post_id: str, expected: dict[str, Any]
    ) -> None:
        """GET /v1/posts/{postId} and verify it equals the approved manifest.

        Any contradiction raises PublishPreflightBlockedError (attempts
        remain 0, zero PUT). Transport unreachability raises
        PublishConnectorUnavailableError; missing/rejected credential
        raises PublishConnectorUnauthorizedError. Never modifies the
        remote draft to make it match.
        """
        path = f"/posts/{post_id}"
        try:
            status, body = self._transport.get(path)
        except PublishAdapterError:
            raise
        except Exception as exc:
            raise PublishConnectorUnavailableError(
                PREFLIGHT_UNAVAILABLE_REASON
            ) from exc

        if status in (401, 403):
            raise PublishConnectorUnauthorizedError(SECRET_MISSING_REASON)
        if status >= 500 or status == 0:
            raise PublishConnectorUnavailableError(
                PREFLIGHT_UNAVAILABLE_REASON
            )
        if status != 200:
            raise PublishPreflightBlockedError(PREFLIGHT_MISMATCH_REASON)
        try:
            validate_preflight_remote_draft(body, expected=expected)
        except PublishPreflightBlockedError:
            raise
        except Exception as exc:
            raise PublishPreflightBlockedError(
                PREFLIGHT_MISMATCH_REASON
            ) from exc

    # -- Phase 2: exactly one consequential PUT ------------------------------

    def promote_once(self, post_id: str) -> tuple[str, int, Any]:
        """Issue exactly ONE PUT /v1/posts/{postId}.

        Returns (disposition, put_status, put_body) where disposition is
        one of "REJECTED" (documented definite 4xx rejection),
        "NEEDS_READBACK" (200/207 accepted shape -- readback decides
        truth, never PUBLISHED directly), or "AMBIGUOUS" (timeout,
        network failure, 5xx, malformed or unexpected response).

        No application retry loop. No HTTP retry loop. No second PUT
        under any circumstance. No idempotency-header claim: the PUT
        contract documents none.
        """
        path = f"/posts/{post_id}"
        try:
            status, body = self._transport.put(
                path, json_body=build_promote_payload()
            )
        except PublishRedirectRefusedError as exc:
            raise PublishAmbiguousError(
                PROMOTE_AMBIGUOUS_REASON
            ) from exc
        except PublishConnectorUnavailableError as exc:
            raise PublishAmbiguousError(
                PROMOTE_AMBIGUOUS_REASON
            ) from exc
        except PublishAdapterError as exc:
            # Malformed-response and body-bound violations detected
            # inside the real transport are ambiguity, not rejection.
            raise PublishAmbiguousError(
                PROMOTE_AMBIGUOUS_REASON
            ) from exc
        except Exception as exc:
            raise PublishAmbiguousError(
                PROMOTE_AMBIGUOUS_REASON
            ) from exc

        if status in (400, 401, 403, 404, 409):
            # Documented definite request rejection requires a VALID
            # envelope; a malformed 4xx body proves nothing.
            if isinstance(body, dict):
                return ("REJECTED", status, body)
            raise PublishAmbiguousError(PROMOTE_AMBIGUOUS_REASON)

        if status == 200:
            if isinstance(body, dict):
                return ("NEEDS_READBACK", status, body)
            raise PublishAmbiguousError(PROMOTE_AMBIGUOUS_REASON)

        if status == 207:
            # Explicit 2xx trap: branch on code, handle per documented
            # response/status fields, never treat as PUBLISHED. The
            # readback decides truth.
            if isinstance(body, dict):
                return ("NEEDS_READBACK", status, body)
            raise PublishAmbiguousError(PROMOTE_AMBIGUOUS_REASON)

        # 5xx, 3xx (should be unreachable -- redirects are refused),
        # 429, and any other undocumented status: ambiguous.
        raise PublishAmbiguousError(PROMOTE_AMBIGUOUS_REASON)

    # -- Phase 3: read-only readback (clarification only) ---------------------

    def readback(
        self, post_id: str, expected: dict[str, Any]
    ) -> dict[str, Any]:
        """Exactly one GET /v1/posts/{postId} to clarify truth.

        Returns the truth summary {"live_status", "platform_status",
        "platform_post_url"}. Any failure raises
        PublishReadbackFailedError. Readback NEVER authorizes another
        PUT -- the provider has no method that could issue one.
        """
        path = f"/posts/{post_id}"
        try:
            status, body = self._transport.get(path)
        except PublishAdapterError as exc:
            raise PublishReadbackFailedError(
                READBACK_FAILED_REASON
            ) from exc
        except Exception as exc:
            raise PublishReadbackFailedError(
                READBACK_FAILED_REASON
            ) from exc

        if status in (401, 403):
            raise PublishConnectorUnauthorizedError(SECRET_MISSING_REASON)
        if status != 200:
            raise PublishReadbackFailedError(READBACK_FAILED_REASON)
        try:
            return _check_remote_draft(
                body,
                expected=expected,
                error_cls=PublishReadbackFailedError,
                reason=READBACK_FAILED_REASON,
                require_draft=False,
            )
        except PublishConnectorUnauthorizedError:
            raise
        except PublishReadbackFailedError:
            raise
        except Exception as exc:
            raise PublishReadbackFailedError(
                READBACK_FAILED_REASON
            ) from exc


# ---------------------------------------------------------------------------
# Truth classification (readback decides; PUT never does)
# ---------------------------------------------------------------------------


def classify_readback_truth(summary: dict[str, Any]) -> str:
    """Map a readback truth summary to a terminal publication state.

    PUBLISHED requires proof of delivery on both levels (post published
    AND platform published). A provider-side scheduled/auto-retry status
    is PUBLISHING-family (the provider may still deliver; never a
    second PUT). Anything unprovable is CHECK_REQUIRED -- never
    fabricated success.
    """
    live = summary.get("live_status", "")
    platform = summary.get("platform_status", "")
    if live == "published" and platform == "published":
        return "PUBLISHED"
    if live in {"publishing", "processing", "queued", "scheduled"}:
        return "PUBLISHING"
    if live in {"failed", "error"}:
        return "FAILED"
    return "CHECK_REQUIRED"


# ---------------------------------------------------------------------------
# Factory (secret boundary seam shared with the production factory module)
# ---------------------------------------------------------------------------


def build_direct_publish_provider(
    *,
    secret_provider: SecretProvider | None = None,
) -> ZernioPublishProvider:
    """Construct the production ZernioPublishProvider behind the reviewed
    secret boundary.

    Construction performs no network calls and writes nothing to disk.
    Converts typed missing/unavailable secret conditions into sanitized
    publish-provider failures. Never exposes credential material. The
    logical secret id requested here is `zernio.publish.bearer`,
    distinct from both the analytics and drafts credentials; the
    environment-variable mapping is owned by
    `nullone_secret_provider.py`.
    """
    provider: SecretProvider = (
        secret_provider if secret_provider is not None
        else EnvironmentSecretProvider()
    )

    try:
        token = provider.get_required(SECRET_ID_ZERNIO_PUBLISH_BEARER)
    except SecretNotConfiguredError:
        raise PublishConnectorUnauthorizedError(
            SECRET_MISSING_REASON
        ) from None
    except SecretUnavailableError:
        raise PublishConnectorUnavailableError(
            SECRET_UNAVAILABLE_REASON
        ) from None

    transport = build_authenticated_transport(token=token)

    return ZernioPublishProvider(transport)


# ---------------------------------------------------------------------------
# Self-test (offline, no fixtures, no network)
# ---------------------------------------------------------------------------


def self_test() -> int:
    """Offline self-test: no network, no real credential."""
    marker = "FAKE_ZERNIO_PUBLISH_SECRET_DO_NOT_LOG_123"

    # SecretValue never renders.
    secret = SecretValue(marker)
    assert repr(secret) == f"SecretValue({SECRET_REDACTED_RENDER!r})"
    assert str(secret) == SECRET_REDACTED_RENDER
    assert f"{secret}" == SECRET_REDACTED_RENDER
    assert marker not in repr(secret)
    assert marker not in str(secret)
    assert secret.reveal() == marker

    # Plain strings alone can never satisfy the publish credential.
    try:
        build_authenticated_transport(token=marker)  # type: ignore[arg-type]
        raise AssertionError("plain string token was accepted")
    except TypeError:
        pass

    # Missing secret -> PublishConnectorUnauthorizedError (BLOCKED).
    class MissingProvider:
        def get_required(self, secret_id):
            raise SecretNotConfiguredError("not configured")

    try:
        build_direct_publish_provider(secret_provider=MissingProvider())
        raise AssertionError("missing publish secret did not fail closed")
    except PublishConnectorUnauthorizedError as exc:
        assert str(exc) == SECRET_MISSING_REASON

    # Unknown secret id -> missing-secret (BLOCKED).
    class UnknownIdProvider:
        def get_required(self, secret_id):
            raise SecretNotConfiguredError("unknown secret id")

    try:
        build_direct_publish_provider(secret_provider=UnknownIdProvider())
        raise AssertionError("unknown publish secret id did not fail closed")
    except PublishConnectorUnauthorizedError:
        pass

    # Typed SecretUnavailableError -> PublishConnectorUnavailableError,
    # sanitized.
    class UnavailableProvider:
        def get_required(self, secret_id):
            raise SecretUnavailableError(
                f"credential store unreachable with value {marker}"
            )

    try:
        build_direct_publish_provider(secret_provider=UnavailableProvider())
        raise AssertionError("unavailable publish secret did not fail closed")
    except PublishConnectorUnavailableError as exc:
        assert str(exc) == SECRET_UNAVAILABLE_REASON
        assert marker not in str(exc)

    # Unexpected programming defect (RuntimeError) -> propagates.
    class BrokenProvider:
        def get_required(self, secret_id):
            raise RuntimeError("secret daemon died")

    try:
        build_direct_publish_provider(secret_provider=BrokenProvider())
        raise AssertionError("factory swallowed RuntimeError")
    except RuntimeError as exc:
        assert "secret daemon died" in str(exc)

    # Present secret -> provider constructed; value never leaks.
    class PresentEnv(EnvironmentSecretProvider):
        def __init__(self):
            self._environ = {
                EnvironmentSecretProvider.bound_env_var(
                    SECRET_ID_ZERNIO_PUBLISH_BEARER
                ): marker,
            }

    provider = build_direct_publish_provider(secret_provider=PresentEnv())
    assert isinstance(provider, ZernioPublishProvider)
    assert marker not in repr(provider)
    assert marker not in repr(provider._transport)
    assert "redacted" in repr(provider._transport)
    assert type(provider._transport.token) is SecretValue

    # Publish credential separate from analytics AND drafts.
    from nullone_secret_provider import (
        ENV_VAR_ZERNIO_PUBLISH_API_TOKEN,
        SECRET_ID_ZERNIO_ANALYTICS_BEARER,
        SECRET_ID_ZERNIO_DRAFTS_BEARER,
    )

    assert SECRET_ID_ZERNIO_PUBLISH_BEARER != SECRET_ID_ZERNIO_ANALYTICS_BEARER
    assert SECRET_ID_ZERNIO_PUBLISH_BEARER != SECRET_ID_ZERNIO_DRAFTS_BEARER
    assert (
        EnvironmentSecretProvider.bound_env_var(
            SECRET_ID_ZERNIO_PUBLISH_BEARER
        )
        == ENV_VAR_ZERNIO_PUBLISH_API_TOKEN
        == "ZERNIO_PUBLISH_API_TOKEN"
    )

    # Exact-path allowlist enforced before any network use (real
    # transport object, fake credential, invalid paths only).
    transport = build_authenticated_transport(token=SecretValue(marker))
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
        "https://evil.example/posts/0123456789abcdef01234567",
    ):
        try:
            transport.get(bad_path)
            raise AssertionError(f"transport allowed path {bad_path!r}")
        except PublishAdapterError:
            pass
        try:
            transport.put(bad_path, json_body=build_promote_payload())
            raise AssertionError(f"transport allowed path {bad_path!r}")
        except PublishAdapterError:
            pass

    # Non-exact base URLs are refused at construction.
    for bad_base in (
        "http://zernio.com/api/v1",
        "https://evil.example/api/v1",
        "https://zernio.com.evil.example/api/v1",
        "https://api.zernio.com/api/v1",
    ):
        try:
            build_authenticated_transport(
                token=SecretValue(marker), base_url=bad_base
            )
            raise AssertionError(f"transport allowed base {bad_base!r}")
        except PublishAdapterError:
            pass

    # Minimal promotion payload is exact.
    assert build_promote_payload() == {
        "isDraft": False,
        "publishNow": True,
    }

    # Redirects are refused, never followed.
    try:
        _NoRedirectHandler().redirect_request(
            object(), object(), 302, "Found", {}, "https://evil.example/"
        )
        raise AssertionError("redirect was not refused")
    except PublishRedirectRefusedError:
        pass

    # Blank token -> unauthorized, never a transport.
    try:
        build_authenticated_transport(token=SecretValue("   "))
        raise AssertionError("blank publish token was accepted")
    except PublishConnectorUnauthorizedError:
        pass

    print("ZERNIO_PUBLISH_ADAPTER_SELF_TEST=PASS")
    print("SECRET_REDACTED=TRUE")
    print("NO_NETWORK=TRUE")
    print("NO_MODEL_TRANSPORT=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Direct deterministic Zernio REST publisher (#90)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
