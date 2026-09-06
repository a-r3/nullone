#!/usr/bin/env python3
"""Read-only Zernio analytics adapter (issue #29).

This module is the ONLY place that knows about Zernio-specific HTTPS
paths, response envelopes and credentials. `nullone_analytics_runtime`
depends only on the small connector this module exposes, so a future
connector replacement (see issue #13) does not require changing domain
outcome semantics.

Endpoint contract confirmed 2026-09-06 against Zernio's official
OpenAPI specification (docs.zernio.com/api/openapi, `openapi: 3.1.0`,
`info.version: "1.0.4"`):

- base URL: `https://zernio.com/api/v1`
- `GET /accounts` (operationId `listAccounts`) -> `AccountsListResponse`
  `{accounts: [{_id, platform, profileId, username, displayName,
  profileUrl, isActive}], hasAnalyticsAccess}`. There is no documented
  `GET /accounts/{id}`; the configured account is selected by matching
  `_id` inside this list.
- `GET /analytics/instagram/account-insights` (operationId
  `getInstagramAccountInsights`), required query `accountId`, optional
  `metrics` (comma-separated; we pass the full documented Instagram
  metric set) -> `InstagramAccountInsightsResponse`.
- `GET /analytics/instagram/follower-history` (operationId
  `getInstagramFollowerHistory`), required query `accountId`, same
  `InstagramAccountInsightsResponse` envelope ("Response envelope
  matches /v1/analytics/instagram/account-insights").
- `GET /analytics` (operationId `getAnalytics`), no `postId` ->
  `AnalyticsListResponse` `{overview, posts: [...], pagination,
  accounts, hasAnalyticsAccess}`, scoped here with `accountId` and
  `platform=instagram`.

`InstagramAccountInsightsResponse.metrics` is documented as: "A metric
that could not be served is absent from this object and listed in
`unavailableMetrics` instead, so an unavailable metric is never
reported as a zero." This adapter preserves that: a requested metric
absent from the response is surfaced as `None` (unavailable), never
coerced to `0`.

Analytics-add-on/capability unavailability is also documented and
distinct from a malformed request: `GET /accounts` carries a top-level
`hasAnalyticsAccess` flag, and an analytics endpoint can return HTTP
`402` with `code: analytics_addon_required`. Both are surfaced here as
`AnalyticsCapabilityUnavailableError` (a `ConnectorUnavailableError`
subclass), so they become domain BLOCKED like any other capability
unavailability — never FAILED, and never SUCCEEDED. A malformed
request (400) or an unknown account (404) is unaffected and remains an
`AnalyticsResponseError` (domain FAILED).

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

# Confirmed base URL (see module docstring). Overridable via
# ZERNIO_ANALYTICS_BASE_URL; this adapter never uses it for anything
# but GET requests.
DEFAULT_BASE_URL = "https://zernio.com/api/v1"

# Documented Instagram account-insights metric names (valid list from
# the 400 response's validMetrics example), minus the ones this report
# does not use (replies, reposts, follows_and_unfollows).
INSTAGRAM_INSIGHT_METRICS: tuple[str, ...] = (
    "reach",
    "views",
    "accounts_engaged",
    "total_interactions",
    "comments",
    "likes",
    "saves",
    "shares",
    "profile_links_taps",
)

# Documented follower-history metric names.
FOLLOWER_HISTORY_METRICS: tuple[str, ...] = (
    "follower_count",
    "followers_gained",
    "followers_lost",
)

# Page size for the post-analytics list call; well within the
# documented 1-100 `limit` range.
POST_ANALYTICS_PAGE_SIZE = 25


class AnalyticsAdapterError(RuntimeError):
    """Base error for the read-only Zernio analytics adapter."""


class ConnectorUnavailableError(AnalyticsAdapterError):
    """Bootstrap/dependency/runtime unavailability.

    Reproduces the confirmed #29 symptom class (e.g. a `bundle-mcp`
    style startup failure, unreachable host, or 5xx). Must always
    surface as domain BLOCKED, never SUCCEEDED.
    """


class AnalyticsCapabilityUnavailableError(ConnectorUnavailableError):
    """The Zernio Analytics add-on is not available for this account/plan.

    Documented signals: the `GET /accounts` response's top-level
    `hasAnalyticsAccess: false`, or an analytics endpoint's documented
    HTTP 402 (`code: analytics_addon_required`). This is a capability
    unavailability, not a malformed request, so it is still a
    `ConnectorUnavailableError` subclass and must surface as domain
    BLOCKED, never FAILED or SUCCEEDED. The reason text is always a
    fixed, generic string — never the response body or credential.
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


def _validate_insights_envelope(body: Any, *, what: str) -> dict[str, Any]:
    """Validate one `InstagramAccountInsightsResponse` envelope.

    Deliberately does not require every requested metric to be
    present: a metric Zernio could not serve is documented to be
    OMITTED from `metrics` (and optionally listed in
    `unavailableMetrics`) rather than reported as zero. Callers read
    missing metrics as `None` via `metric_total`.
    """

    body = _require_keys(
        body,
        ("success", "accountId", "platform", "metricType", "metrics"),
        what=what,
    )

    if body["success"] is not True:
        raise AnalyticsResponseError(f"{what} response reported success=false")

    metrics = body["metrics"]

    if not isinstance(metrics, dict):
        raise AnalyticsResponseError(f"{what} response 'metrics' was not an object")

    for name, entry in metrics.items():
        if not isinstance(entry, dict) or "total" not in entry:
            raise AnalyticsResponseError(
                f"{what} response metric '{name}' entry was malformed"
            )

    return body


def metric_total(envelope: dict[str, Any], name: str) -> int | float | None:
    """Read one metric's `total` from an insights envelope.

    Returns `None` when the metric is absent (Zernio's documented
    "unavailable" signal) rather than coercing it to `0`.
    """

    entry = envelope.get("metrics", {}).get(name)

    if not isinstance(entry, dict):
        return None

    total = entry.get("total")
    return total if isinstance(total, (int, float)) else None


class ZernioReadOnlyAnalyticsConnector:
    """GET-only Zernio analytics connector.

    Every public method here maps to one documented read-only endpoint:
    connected-account listing, Instagram account insights, Instagram
    follower history, and post analytics. There is intentionally no
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
    ) -> Any:
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

        if status == 402:
            # Documented: "Analytics access required. Legacy plans need
            # the Analytics add-on ... code: analytics_addon_required".
            # A missing add-on is a capability unavailability, not a
            # malformed request: BLOCKED, never FAILED.
            raise AnalyticsCapabilityUnavailableError(
                "Zernio analytics add-on is required but not enabled "
                "for this account."
            )

        if status >= 500 or status == 0:
            raise ConnectorUnavailableError(
                f"Zernio analytics endpoint returned status {status}"
            )

        if status != 200:
            raise AnalyticsResponseError(
                f"Zernio analytics endpoint returned unexpected status {status}"
            )

        return body

    def get_account(self) -> dict[str, Any]:
        """Select the configured account from the documented `GET /accounts`
        list response. There is no documented `GET /accounts/{id}`."""

        body = self._get("/accounts")

        if not isinstance(body, dict) or not isinstance(body.get("accounts"), list):
            raise AnalyticsResponseError(
                "accounts response was not a valid AccountsListResponse"
            )

        # Documented top-level capability gate: stop before any
        # analytics endpoint is called at all when the plan does not
        # have analytics access, rather than letting each analytics
        # call fail separately.
        if body.get("hasAnalyticsAccess") is False:
            raise AnalyticsCapabilityUnavailableError(
                "Zernio analytics add-on is required but not enabled "
                "for this account."
            )

        for account in body["accounts"]:
            if not isinstance(account, dict):
                raise AnalyticsResponseError(
                    "accounts response contained a non-object account entry"
                )

            if account.get("_id") == self._account_id:
                return _require_keys(
                    account,
                    ("_id", "platform", "username", "isActive"),
                    what="account",
                )

        raise AnalyticsResponseError(
            f"configured account {self._account_id} was not present in the "
            "accounts response"
        )

    def get_follower_history(self) -> dict[str, Any]:
        body = self._get(
            "/analytics/instagram/follower-history",
            params={
                "accountId": self._account_id,
                "metrics": ",".join(FOLLOWER_HISTORY_METRICS),
            },
        )
        return _validate_insights_envelope(body, what="follower history")

    def get_account_insights(self) -> dict[str, Any]:
        body = self._get(
            "/analytics/instagram/account-insights",
            params={
                "accountId": self._account_id,
                "metrics": ",".join(INSTAGRAM_INSIGHT_METRICS),
            },
        )
        return _validate_insights_envelope(body, what="account insights")

    def get_post_analytics(self) -> dict[str, Any]:
        body = self._get(
            "/analytics",
            params={
                "accountId": self._account_id,
                "platform": "instagram",
                "sortBy": "date",
                "order": "desc",
                "limit": POST_ANALYTICS_PAGE_SIZE,
            },
        )

        if not isinstance(body, dict) or not isinstance(body.get("posts"), list):
            raise AnalyticsResponseError(
                "analytics response was not a valid AnalyticsListResponse"
            )

        for post in body["posts"]:
            if not isinstance(post, dict) or not isinstance(
                post.get("analytics"), dict
            ):
                raise AnalyticsResponseError(
                    "analytics response contained a malformed post entry"
                )

        return body
