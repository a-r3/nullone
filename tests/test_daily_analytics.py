#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_analytics_runtime as analytics_runtime  # noqa: E402
from nullone_analytics_runtime import (  # noqa: E402
    raw_relative_path,
    report_relative_path,
    run_daily_analytics,
)
import nullone_zernio_analytics_adapter as analytics_adapter  # noqa: E402
from nullone_zernio_analytics_adapter import (  # noqa: E402
    ConnectorUnauthorizedError,
    ConnectorUnavailableError,
    ZernioReadOnlyAnalyticsConnector,
)


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_script(
    "nullone_daily_analytics_run_test",
    "nullone-daily-analytics-run.py",
)

ACCOUNT_ID = "6a982bbf77555aae01c28f21"
OTHER_ACCOUNT_ID = "0000000000000000000000aa"

# Obviously-fake test value; never a real credential. Used to prove it
# cannot leak into a persisted result even when the transport layer
# holds it internally.
FAKE_SECRET = "fake-test-secret-do-not-leak-abc123"  # noqa: S105

# Confirmed 2026-09-06 documented paths (docs.zernio.com/api/openapi,
# info.version "1.0.4"). See nullone_zernio_analytics_adapter's module
# docstring for the full citation.
ACCOUNTS_PATH = "/accounts"
FOLLOWER_HISTORY_PATH = "/analytics/instagram/follower-history"
INSIGHTS_PATH = "/analytics/instagram/account-insights"
POST_ANALYTICS_PATH = "/analytics"


def _insights_envelope(metric_type: str = "total_value", **metrics) -> tuple[int, dict]:
    return 200, {
        "success": True,
        "accountId": ACCOUNT_ID,
        "platform": "instagram",
        "metricType": metric_type,
        "metrics": {
            name: {"total": value}
            for name, value in metrics.items()
            if value is not None
        },
    }


def _valid_accounts(account_id: str = ACCOUNT_ID) -> tuple[int, dict]:
    return 200, {
        "accounts": [
            {
                "_id": account_id,
                "platform": "instagram",
                "username": "nullone.az",
                "isActive": True,
            }
        ],
        "hasAnalyticsAccess": True,
    }


def _valid_follower_history() -> tuple[int, dict]:
    return _insights_envelope(
        follower_count=1000, followers_gained=12, followers_lost=3
    )


def _valid_insights(total_interactions: int | None = 42) -> tuple[int, dict]:
    return _insights_envelope(
        reach=500,
        views=900,
        accounts_engaged=80,
        total_interactions=total_interactions,
        comments=3,
        likes=30,
        saves=5,
        shares=4,
        profile_links_taps=2,
    )


def _valid_post_analytics(posts: list | None = None) -> tuple[int, dict]:
    return 200, {
        "posts": (
            posts
            if posts is not None
            else [
                {
                    "_id": "p1",
                    "analytics": {
                        "reach": 400,
                        "likes": 20,
                        "comments": 2,
                        "saves": 3,
                        "shares": 1,
                    },
                }
            ]
        ),
        "pagination": {"page": 1, "limit": 25, "total": 1, "pages": 1},
    }


def _full_success_responses(
    total_interactions: int | None = 42,
    posts: list | None = None,
    account_id: str = ACCOUNT_ID,
) -> dict[str, tuple[int, dict]]:
    return {
        ACCOUNTS_PATH: _valid_accounts(account_id),
        FOLLOWER_HISTORY_PATH: _valid_follower_history(),
        INSIGHTS_PATH: _valid_insights(total_interactions),
        POST_ANALYTICS_PATH: _valid_post_analytics(posts),
    }


class RecordingTransport:
    """GET-only test double: no other method is defined at all."""

    def __init__(self, responses: dict[str, tuple[int, dict]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, *, params: dict | None = None) -> tuple[int, dict]:
        self.calls.append((path, params))
        return self._responses[path]


class WriteAttemptTransport(RecordingTransport):
    """Proves the connector never issues anything but GET: any write
    verb immediately raises instead of silently succeeding."""

    def post(self, *args, **kwargs):
        raise AssertionError("write attempted: post")

    def put(self, *args, **kwargs):
        raise AssertionError("write attempted: put")

    def delete(self, *args, **kwargs):
        raise AssertionError("write attempted: delete")

    def patch(self, *args, **kwargs):
        raise AssertionError("write attempted: patch")


class SecretLeakGuardTransport:
    """Holds a fake credential the way a real HTTPS transport would.
    The connector never reads this value back; on rejection it must
    only ever see a status code, never the transport's internal state.
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self.calls = 0

    def get(self, path: str, *, params: dict | None = None) -> tuple[int, dict]:
        self.calls += 1
        return 401, {"error": "unauthorized"}


def _connector(responses: dict) -> tuple[ZernioReadOnlyAnalyticsConnector, RecordingTransport]:
    transport = RecordingTransport(responses)
    return ZernioReadOnlyAnalyticsConnector(transport, account_id=ACCOUNT_ID), transport


class DailyAnalyticsRuntimeTests(unittest.TestCase):
    # -- documented Zernio contract --------------------------------------

    def test_exact_documented_paths_and_query_parameters_are_used(self):
        connector, transport = _connector(_full_success_responses())

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

        calls_by_path = dict(transport.calls)

        self.assertIn(ACCOUNTS_PATH, calls_by_path)
        self.assertIsNone(calls_by_path[ACCOUNTS_PATH])

        self.assertEqual(
            calls_by_path[FOLLOWER_HISTORY_PATH],
            {
                "accountId": ACCOUNT_ID,
                "metrics": "follower_count,followers_gained,followers_lost",
            },
        )
        self.assertEqual(
            calls_by_path[INSIGHTS_PATH],
            {
                "accountId": ACCOUNT_ID,
                "metrics": (
                    "reach,views,accounts_engaged,total_interactions,"
                    "comments,likes,saves,shares,profile_links_taps"
                ),
            },
        )
        self.assertEqual(
            calls_by_path[POST_ANALYTICS_PATH],
            {
                "accountId": ACCOUNT_ID,
                "platform": "instagram",
                "sortBy": "date",
                "order": "desc",
                "limit": 25,
            },
        )

    def test_old_invented_v2_paths_are_absent(self):
        adapter_source = inspect.getsource(analytics_adapter)
        self.assertNotIn("/v2/", adapter_source)
        self.assertNotIn("api.zernio.com", adapter_source)
        self.assertEqual(
            analytics_adapter.DEFAULT_BASE_URL, "https://zernio.com/api/v1"
        )

    def test_official_response_envelope_parsing_and_unavailable_metric(self):
        # total_interactions is entirely absent from the envelope, matching
        # the documented "unavailable metric is omitted, never zero" rule.
        responses = _full_success_responses()
        responses[INSIGHTS_PATH] = _insights_envelope(
            reach=500,
            views=900,
            accounts_engaged=80,
            total_interactions=None,
            comments=3,
            likes=30,
            saves=5,
            shares=4,
            profile_links_taps=2,
        )
        # Give it reportable data via a post so this is a SUCCEEDED case,
        # not the NO_DATA path (covered separately).
        connector, _ = _connector(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "SUCCEEDED")

            report = (root / report_relative_path("2026-09-05")).read_text(
                encoding="utf-8"
            )
            self.assertIn("reach: 500", report)
            # Never coerced to 0.
            self.assertIn("total_interactions: unavailable", report)
            self.assertNotIn("total_interactions: 0", report)

    def test_account_lookup_selects_requested_account_from_accounts_list(self):
        responses = _full_success_responses()
        responses[ACCOUNTS_PATH] = (
            200,
            {
                "accounts": [
                    {
                        "_id": OTHER_ACCOUNT_ID,
                        "platform": "instagram",
                        "username": "someone-else",
                        "isActive": True,
                    },
                    {
                        "_id": ACCOUNT_ID,
                        "platform": "instagram",
                        "username": "nullone.az",
                        "isActive": True,
                    },
                ],
                "hasAnalyticsAccess": True,
            },
        )
        connector, _ = _connector(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "SUCCEEDED")

            raw = (root / raw_relative_path("2026-09-05")).read_text(
                encoding="utf-8"
            )
            self.assertIn("nullone.az", raw)
            self.assertNotIn("someone-else", raw)

    def test_account_not_present_in_accounts_response_is_non_success(self):
        responses = _full_success_responses()
        responses[ACCOUNTS_PATH] = (
            200,
            {
                "accounts": [
                    {
                        "_id": OTHER_ACCOUNT_ID,
                        "platform": "instagram",
                        "username": "someone-else",
                        "isActive": True,
                    }
                ],
                "hasAnalyticsAccess": True,
            },
        )
        connector, _ = _connector(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["reason_code"], "ANALYTICS_RESPONSE_INVALID")
            self.assertFalse((root / "social/analytics").exists())

    # -- success / no-data -------------------------------------------------

    def test_success_produces_raw_and_report_artifacts(self):
        connector, transport = _connector(_full_success_responses())

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "SUCCEEDED")
            self.assertEqual(result["health"], "HEALTHY")
            self.assertIsNone(result["empty_success"])
            self.assertEqual(len(transport.calls), 4)

            raw_path = root / raw_relative_path("2026-09-05")
            report_path = root / report_relative_path("2026-09-05")

            self.assertTrue(raw_path.is_file())
            self.assertTrue(report_path.is_file())
            self.assertIn("nullone.az", raw_path.read_text(encoding="utf-8"))
            self.assertIn("reach", report_path.read_text(encoding="utf-8"))

    def test_valid_no_data_semantics(self):
        connector, _ = _connector(
            _full_success_responses(total_interactions=-1, posts=[])
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "SUCCEEDED")
            self.assertEqual(result["health"], "HEALTHY")
            self.assertEqual(result["empty_success"], "NO_DATA")
            self.assertFalse((root / "social/analytics").exists())

    def test_valid_no_data_when_metric_omitted_entirely(self):
        connector, _ = _connector(
            _full_success_responses(total_interactions=None, posts=[])
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "SUCCEEDED")
            self.assertEqual(result["empty_success"], "NO_DATA")

    # -- BLOCKED / non-success ----------------------------------------------

    def test_connector_bootstrap_unavailable_is_blocked_without_artifacts(self):
        def build_connector():
            raise ConnectorUnavailableError(
                '[bundle-mcp] failed to start server "zernio"'
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=build_connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["scheduler_status"], "succeeded")
            self.assertEqual(result["domain_outcome"], "BLOCKED")
            self.assertEqual(result["health"], "UNHEALTHY")
            self.assertEqual(result["reason_code"], "ZERNIO_ANALYTICS_UNAVAILABLE")
            self.assertFalse((root / "social/analytics").exists())

    def test_has_analytics_access_false_is_blocked_before_any_analytics_call(self):
        responses = _full_success_responses()
        responses[ACCOUNTS_PATH] = (
            200,
            {
                "accounts": [
                    {
                        "_id": ACCOUNT_ID,
                        "platform": "instagram",
                        "username": "nullone.az",
                        "isActive": True,
                    }
                ],
                "hasAnalyticsAccess": False,
            },
        )
        connector, transport = _connector(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["scheduler_status"], "succeeded")
            self.assertEqual(result["domain_outcome"], "BLOCKED")
            self.assertEqual(result["health"], "UNHEALTHY")
            self.assertEqual(
                result["reason_code"], "ZERNIO_ANALYTICS_ADDON_REQUIRED"
            )
            self.assertFalse((root / "social/analytics").exists())

            # Stopped before any analytics endpoint was even called.
            called_paths = [path for path, _params in transport.calls]
            self.assertEqual(called_paths, [ACCOUNTS_PATH])

    def test_documented_402_analytics_addon_required_is_blocked(self):
        responses = _full_success_responses()
        responses[INSIGHTS_PATH] = (
            402,
            {"error": "Analytics add-on required", "code": "analytics_addon_required"},
        )
        connector, transport = _connector(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "BLOCKED")
            self.assertEqual(result["health"], "UNHEALTHY")
            self.assertEqual(
                result["reason_code"], "ZERNIO_ANALYTICS_ADDON_REQUIRED"
            )
            self.assertNotEqual(result["domain_outcome"], "FAILED")
            self.assertNotEqual(result["domain_outcome"], "SUCCEEDED")
            self.assertFalse((root / "social/analytics").exists())
            # Never leaks the raw response body into the persisted result.
            serialized = json.dumps(result)
            self.assertNotIn("analytics_addon_required", serialized)

    def test_400_malformed_request_remains_failed_not_blocked(self):
        responses = _full_success_responses()
        responses[INSIGHTS_PATH] = (
            400,
            {"error": "Invalid metrics: bogus_metric"},
        )
        connector, _ = _connector(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["reason_code"], "ANALYTICS_RESPONSE_INVALID")
            self.assertFalse((root / "social/analytics").exists())

    def test_404_account_not_found_remains_failed_not_blocked(self):
        responses = _full_success_responses()
        responses[INSIGHTS_PATH] = (404, {"error": "Account not found"})
        connector, _ = _connector(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["reason_code"], "ANALYTICS_RESPONSE_INVALID")
            self.assertFalse((root / "social/analytics").exists())

    def test_missing_or_unauthorized_credential_blocked_without_secret_leak(self):
        transport = SecretLeakGuardTransport(FAKE_SECRET)
        connector = ZernioReadOnlyAnalyticsConnector(transport, account_id=ACCOUNT_ID)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "BLOCKED")
            self.assertEqual(result["reason_code"], "ZERNIO_ANALYTICS_UNAUTHORIZED")
            serialized = json.dumps(result)
            self.assertNotIn(FAKE_SECRET, serialized)
            self.assertNotIn(FAKE_SECRET, result["reason_text"])
            self.assertFalse((root / "social/analytics").exists())

    def test_build_default_transport_blocks_when_credential_missing(self):
        backup = os.environ.pop(analytics_adapter.CREDENTIAL_ENV_VAR, None)
        try:
            with self.assertRaises(ConnectorUnauthorizedError):
                analytics_adapter.build_default_transport()
        finally:
            if backup is not None:
                os.environ[analytics_adapter.CREDENTIAL_ENV_VAR] = backup

    def test_malformed_payload_is_non_success_without_partial_artifacts(self):
        responses = _full_success_responses()
        # Missing required envelope fields (no "success"/"metrics").
        responses[INSIGHTS_PATH] = (200, {"accountId": ACCOUNT_ID})
        connector, _ = _connector(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["health"], "UNHEALTHY")
            self.assertEqual(result["reason_code"], "ANALYTICS_RESPONSE_INVALID")
            self.assertFalse((root / "social/analytics").exists())

    def test_partial_post_analytics_payload_is_non_success(self):
        responses = _full_success_responses()
        responses[POST_ANALYTICS_PATH] = (
            200,
            {"posts": [{"_id": "p1"}]},  # missing "analytics"
        )
        connector, _ = _connector(responses)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["reason_code"], "ANALYTICS_RESPONSE_INVALID")
            self.assertFalse((root / "social/analytics").exists())

    # -- capability-negative -------------------------------------------------

    def test_connector_exposes_only_read_only_get_methods(self):
        public_methods = {
            name
            for name, _member in inspect.getmembers(
                ZernioReadOnlyAnalyticsConnector, predicate=inspect.isfunction
            )
            if not name.startswith("_")
        }

        self.assertEqual(
            public_methods,
            {
                "get_account",
                "get_follower_history",
                "get_account_insights",
                "get_post_analytics",
            },
        )

        forbidden_terms = (
            "publish",
            "draft",
            "schedule",
            "delete",
            "create",
            "update",
            "message",
            "comment",
            "unpublish",
            "retry",
            "queue",
        )

        for name in public_methods:
            for term in forbidden_terms:
                self.assertNotIn(term, name.lower())

    def test_full_run_never_issues_a_write_call(self):
        transport = WriteAttemptTransport(_full_success_responses())
        connector = ZernioReadOnlyAnalyticsConnector(transport, account_id=ACCOUNT_ID)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "SUCCEEDED")
            self.assertEqual(len(transport.calls), 4)

    # -- occurrence recovery / scheduler-outcome separation ------------------

    def test_later_occurrence_recovers_after_earlier_blocked_one(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_root = root / "run-outcomes"

            def blocked_connector():
                raise ConnectorUnavailableError(
                    '[bundle-mcp] failed to start server "zernio"'
                )

            first = run_daily_analytics(
                occurrence_id="2026-09-04T03:20:00+04:00",
                analytics_date="2026-09-04",
                build_connector=blocked_connector,
                artifact_root=root,
                output_root=output_root,
            )

            healthy_connector, _ = _connector(_full_success_responses())

            second = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=lambda: healthy_connector,
                artifact_root=root,
                output_root=output_root,
            )

            self.assertEqual(first["domain_outcome"], "BLOCKED")
            self.assertEqual(second["domain_outcome"], "SUCCEEDED")
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertTrue((root / raw_relative_path("2026-09-05")).is_file())
            self.assertFalse((root / raw_relative_path("2026-09-04")).exists())

    def test_scheduler_ok_cannot_mask_blocked_domain_outcome(self):
        def build_connector():
            raise ConnectorUnavailableError("boot failure")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_daily_analytics(
                occurrence_id="2026-09-05T03:20:00+04:00",
                analytics_date="2026-09-05",
                build_connector=build_connector,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["scheduler_status"], "succeeded")
            self.assertEqual(result["domain_outcome"], "BLOCKED")
            self.assertEqual(result["health"], "UNHEALTHY")

            with mock.patch.object(
                runner,
                "run_daily_analytics",
                lambda **kwargs: result,
            ):
                exit_code = runner.execute("2026-09-05T03:20:00+04:00", "2026-09-05")

            self.assertEqual(exit_code, 1)

    # -- atomic two-artifact commit -------------------------------------------

    def test_second_artifact_commit_failure_leaves_no_partial_pair(self):
        connector, _ = _connector(_full_success_responses())

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_path = root / raw_relative_path("2026-09-05")
            report_path = root / report_relative_path("2026-09-05")

            real_replace = os.replace

            def flaky_replace(src, dst):
                if Path(dst) == report_path:
                    raise OSError("simulated disk failure committing report artifact")
                return real_replace(src, dst)

            with mock.patch(
                "nullone_analytics_runtime.os.replace", side_effect=flaky_replace
            ):
                result = run_daily_analytics(
                    occurrence_id="2026-09-05T03:20:00+04:00",
                    analytics_date="2026-09-05",
                    build_connector=lambda: connector,
                    artifact_root=root,
                    output_root=root / "run-outcomes",
                )

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(
                result["reason_code"], "ANALYTICS_ARTIFACT_COMMIT_FAILED"
            )
            self.assertFalse(raw_path.exists())
            self.assertFalse(report_path.exists())

    def test_commit_failure_preserves_preexisting_valid_artifacts(self):
        connector, _ = _connector(_full_success_responses())

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_path = root / raw_relative_path("2026-09-05")
            report_path = root / report_relative_path("2026-09-05")

            raw_path.parent.mkdir(parents=True)
            report_path.parent.mkdir(parents=True)
            raw_path.write_text("PRE-EXISTING VALID RAW\n", encoding="utf-8")
            report_path.write_text("PRE-EXISTING VALID REPORT\n", encoding="utf-8")

            real_replace = os.replace

            def flaky_replace(src, dst):
                if Path(dst) == report_path:
                    raise OSError("simulated disk failure committing report artifact")
                return real_replace(src, dst)

            with mock.patch(
                "nullone_analytics_runtime.os.replace", side_effect=flaky_replace
            ):
                result = run_daily_analytics(
                    occurrence_id="2026-09-05T03:20:00+04:00",
                    analytics_date="2026-09-05",
                    build_connector=lambda: connector,
                    artifact_root=root,
                    output_root=root / "run-outcomes",
                )

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(
                result["reason_code"], "ANALYTICS_ARTIFACT_COMMIT_FAILED"
            )
            self.assertEqual(
                raw_path.read_text(encoding="utf-8"), "PRE-EXISTING VALID RAW\n"
            )
            self.assertEqual(
                report_path.read_text(encoding="utf-8"),
                "PRE-EXISTING VALID REPORT\n",
            )

    def test_commit_helper_directly_rolls_back_on_second_failure(self):
        """Narrower unit-level proof of `_commit_artifact_pair` itself,
        independent of the full run_daily_analytics flow above."""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path_a = root / "a.md"
            path_b = root / "b.md"

            real_replace = os.replace

            def flaky_replace(src, dst):
                if Path(dst) == path_b:
                    raise OSError("simulated failure")
                return real_replace(src, dst)

            with mock.patch(
                "nullone_analytics_runtime.os.replace", side_effect=flaky_replace
            ):
                with self.assertRaises(analytics_runtime.ArtifactCommitError):
                    analytics_runtime._commit_artifact_pair(
                        [(path_a, "A content\n"), (path_b, "B content\n")]
                    )

            self.assertFalse(path_a.exists())
            self.assertFalse(path_b.exists())

    def test_commit_helper_succeeds_and_writes_both_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path_a = root / "a.md"
            path_b = root / "b.md"

            analytics_runtime._commit_artifact_pair(
                [(path_a, "A content\n"), (path_b, "B content\n")]
            )

            self.assertEqual(path_a.read_text(encoding="utf-8"), "A content\n")
            self.assertEqual(path_b.read_text(encoding="utf-8"), "B content\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
