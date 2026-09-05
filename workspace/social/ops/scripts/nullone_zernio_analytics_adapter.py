#!/usr/bin/env python3
"""Read-only Zernio analytics adapter (issue #29).

This module is the ONLY place that knows about Zernio-specific HTTPS
paths, response shapes and credentials. `nullone_analytics_runtime`
depends only on the small connector this module exposes, so a future
connector replacement (see issue #13) does not require changing domain
outcome semantics.

By construction this module exposes GET-only, read-only analytics
capability:
- there is no method that can create, update, delete, publish, draft,
  schedule, or otherwise mutate any Zernio/Instagram state;
- the connector never calls anything on its transport other than
  `.get(...)`.

This deliberately does not depend on generic MCP tool dispatch: issue
#29's confirmed root cause is a scheduled-session `bundle-mcp`
bootstrap/runtime-availability failure, not a Zernio outage or
allowlist defect. A direct, narrow, read-only HTTPS path removes that
dependency for analytics specifically, without granting any
write-capable surface.

Credentials are read from an environment variable at call time and are
never embedded in code, logs, or reason text.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

CREDENTIAL_ENV_VAR = "ZERNIO_ANALYTICS_API_TOKEN"

# Base host for Zernio's documented read-only analytics HTTPS surface.
# Overridable via ZERNIO_ANALYTICS_BASE_URL; this adapter never uses it
# for anything but GET requests.
DEFAULT_BASE_URL = "https://api.zernio.com"


class AnalyticsAdapterError(RuntimeError):
    """Base error for the read-only Zernio analytics adapter."""


class ConnectorUnavailableError(AnalyticsAdapterError):
    """Bootstrap/dependency/runtime unavailability.

    Reproduces the confirmed #29 symptom class (e.g. a `bundle-mcp`
    style startup failure, unreachable host, or 5xx). Must always
    surface as domain BLOCKED, never SUCCEEDED.
    """


class ConnectorUnauthorizedError(AnalyticsAdapterError):
    """Missing or rejected credential. Never carries the credential value."""


class AnalyticsResponseError(AnalyticsAdapterError):
    """A response was malformed, partial, or otherwise not trustworthy."""


class AnalyticsTransport(Protocol):
    """The only capability this adapter is allowed to use.

    Deliberately GET-only: there is no `post`/`put`/`delete`/`patch` in
    this protocol, and the connector below never references one. A
    transport test double that implements only `get` is therefore a
    working proof that the executor cannot issue a write call.
    """

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        """Return (status_code, decoded_json_body_or_None)."""
        ...


@dataclass(frozen=True)
class UrllibAnalyticsTransport:
    """Real GET-only HTTPS transport.

    Not exercised by any offline test in this repository (tests never
    perform network calls); wiring this into a scheduled production
    run is a separate, later step gated behind issue #37. Kept
    intentionally small: it can only perform authenticated GET
    requests.
    """

    base_url: str
    token: str
    timeout_seconds: float = 15.0

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
                "Authorization": f"Bearer {self.token}",
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
            raise ConnectorUnavailableError(
                "Zernio analytics HTTPS endpoint was not reachable"
            ) from exc

        try:
            decoded = json.loads(body) if body else None
        except json.JSONDecodeError as exc:
            raise AnalyticsResponseError(
                "Zernio analytics response was not valid JSON"
            ) from exc

        return status, decoded


def build_default_transport() -> AnalyticsTransport:
    """Construct the real transport from an externally supplied credential.

    Raises `ConnectorUnauthorizedError` (never leaking the credential
    itself, since it is never present in the message) when the
    required environment variable is absent or blank. Not exercised by
    tests; not wired into any scheduled runner by this change.
    """

    token = os.environ.get(CREDENTIAL_ENV_VAR, "").strip()

    if not token:
        raise ConnectorUnauthorizedError(
            f"{CREDENTIAL_ENV_VAR} is not configured"
        )

    return UrllibAnalyticsTransport(
        base_url=os.environ.get("ZERNIO_ANALYTICS_BASE_URL", DEFAULT_BASE_URL),
        token=token,
    )


def _require_keys(
    body: Any,
    keys: tuple[str, ...],
    *,
    what: str,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AnalyticsResponseError(f"{what} response was not a JSON object")

    missing = [key for key in keys if key not in body]

    if missing:
        raise AnalyticsResponseError(
            f"{what} response missing required fields: {', '.join(missing)}"
        )

    return body


class ZernioReadOnlyAnalyticsConnector:
    """GET-only Zernio analytics connector.

    Every public method here maps to one documented read-only endpoint:
    connected-account metadata, follower history, Instagram account
    insights, and post analytics. There is intentionally no
    create/update/delete/publish/draft/schedule/message/comment method
    on this class, and it never calls anything on its transport other
    than `.get(...)`.
    """

    def __init__(self, transport: AnalyticsTransport, *, account_id: str) -> None:
        self._transport = transport
        self._account_id = account_id

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            status, body = self._transport.get(path, params=params)
        except AnalyticsAdapterError:
            raise
        except Exception as exc:
            raise ConnectorUnavailableError(
                "Zernio analytics transport failed"
            ) from exc

        if status in (401, 403):
            raise ConnectorUnauthorizedError(
                "Zernio analytics credential is missing or was rejected."
            )

        if status >= 500 or status == 0:
            raise ConnectorUnavailableError(
                f"Zernio analytics endpoint returned status {status}"
            )

        if status != 200:
            raise AnalyticsResponseError(
                f"Zernio analytics endpoint returned unexpected status {status}"
            )

        if not isinstance(body, dict):
            raise AnalyticsResponseError(
                f"Zernio analytics response for {path} was not a JSON object"
            )

        return body

    def get_account(self) -> dict[str, Any]:
        body = self._get(f"/v2/accounts/{self._account_id}")
        return _require_keys(
            body, ("account_id", "username", "status"), what="account"
        )

    def get_follower_history(self) -> dict[str, Any]:
        body = self._get(f"/v2/accounts/{self._account_id}/follower-history")
        body = _require_keys(
            body, ("account_id", "history"), what="follower history"
        )

        if not isinstance(body["history"], list):
            raise AnalyticsResponseError(
                "follower history 'history' field was not a list"
            )

        return body

    def get_account_insights(self) -> dict[str, Any]:
        body = self._get(f"/v2/accounts/{self._account_id}/insights")
        return _require_keys(
            body,
            (
                "account_id",
                "reach",
                "views",
                "accounts_engaged",
                "total_interactions",
                "comments",
                "likes",
                "saves",
                "shares",
                "profile_links_taps",
            ),
            what="account insights",
        )

    def get_post_analytics(self) -> dict[str, Any]:
        body = self._get(f"/v2/accounts/{self._account_id}/posts/analytics")
        body = _require_keys(
            body, ("account_id", "posts"), what="post analytics"
        )

        if not isinstance(body["posts"], list):
            raise AnalyticsResponseError(
                "post analytics 'posts' field was not a list"
            )

        return body
