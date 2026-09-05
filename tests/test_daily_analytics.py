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

# Obviously-fake test value; never a real credential. Used to prove it
# cannot leak into a persisted result even when the transport layer
# holds it internally.
FAKE_SECRET = "fake-test-secret-do-not-leak-abc123"  # noqa: S105


def _paths(account_id: str = ACCOUNT_ID) -> dict[str, str]:
    return {
        "account": f"/v2/accounts/{account_id}",
        "follower_history": f"/v2/accounts/{account_id}/follower-history",
        "insights": f"/v2/accounts/{account_id}/insights",
        "posts": f"/v2/accounts/{account_id}/posts/analytics",
    }


def _valid_account() -> tuple[int, dict]:
    return 200, {"account_id": ACCOUNT_ID, "username": "nullone.az", "status": "active"}


def _valid_follower_history() -> tuple[int, dict]:
    return 200, {
        "account_id": ACCOUNT_ID,
        "history": [{"date": "2026-09-05", "followers": 1000}],
    }


def _valid_insights(total_interactions: int = 42) -> tuple[int, dict]:
    return 200, {
        "account_id": ACCOUNT_ID,
        "reach": 500,
        "views": 900,
        "accounts_engaged": 80,
        "total_interactions": total_interactions,
        "comments": 3,
        "likes": 30,
        "saves": 5,
        "shares": 4,
        "profile_links_taps": 2,
    }


def _valid_post_analytics(posts: list | None = None) -> tuple[int, dict]:
    return 200, {
        "account_id": ACCOUNT_ID,
        "posts": (
            posts
            if posts is not None
            else [
                {
                    "post_id": "p1",
                    "reach": 400,
                    "likes": 20,
                    "comments": 2,
                    "saves": 3,
                    "shares": 1,
                }
            ]
        ),
    }


def _full_success_responses(
    total_interactions: int = 42,
    posts: list | None = None,
) -> dict[str, tuple[int, dict]]:
    p = _paths()
    return {
        p["account"]: _valid_account(),
        p["follower_history"]: _valid_follower_history(),
        p["insights"]: _valid_insights(total_interactions),
        p["posts"]: _valid_post_analytics(posts),
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


class DailyAnalyticsRuntimeTests(unittest.TestCase):
    def test_success_produces_raw_and_report_artifacts(self):
        transport = RecordingTransport(_full_success_responses())
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
            self.assertEqual(result["health"], "HEALTHY")
            self.assertIsNone(result["empty_success"])
            self.assertEqual(transport.calls.__len__(), 4)

            raw_path = root / raw_relative_path("2026-09-05")
            report_path = root / report_relative_path("2026-09-05")

            self.assertTrue(raw_path.is_file())
            self.assertTrue(report_path.is_file())
            self.assertIn("nullone.az", raw_path.read_text(encoding="utf-8"))
            self.assertIn("reach", report_path.read_text(encoding="utf-8"))

    def test_valid_no_data_semantics(self):
        transport = RecordingTransport(
            _full_success_responses(total_interactions=-1, posts=[])
        )
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
            self.assertEqual(result["health"], "HEALTHY")
            self.assertEqual(result["empty_success"], "NO_DATA")
            self.assertFalse((root / "social/analytics").exists())

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
        p = _paths()
        responses = {
            p["account"]: _valid_account(),
            p["follower_history"]: _valid_follower_history(),
            # Missing required insight fields.
            p["insights"]: (200, {"account_id": ACCOUNT_ID, "reach": 500}),
            p["posts"]: _valid_post_analytics(),
        }
        transport = RecordingTransport(responses)
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

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["health"], "UNHEALTHY")
            self.assertEqual(result["reason_code"], "ANALYTICS_RESPONSE_INVALID")
            self.assertFalse((root / "social/analytics").exists())

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

            healthy_transport = RecordingTransport(_full_success_responses())
            healthy_connector = ZernioReadOnlyAnalyticsConnector(
                healthy_transport, account_id=ACCOUNT_ID
            )

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

            # This is the exact confirmed Sep 4-5 shape: scheduler-level
            # receipt reads "succeeded" while the domain result is
            # BLOCKED. RUN-OUTCOME-001 requires these stay distinct.
            self.assertEqual(result["scheduler_status"], "succeeded")
            self.assertEqual(result["domain_outcome"], "BLOCKED")
            self.assertEqual(result["health"], "UNHEALTHY")

            with mock.patch.object(
                runner,
                "run_daily_analytics",
                lambda **kwargs: result,
            ):
                exit_code = runner.execute("2026-09-05T03:20:00+04:00", "2026-09-05")

            # The wrapper's own process-level exit code must not paper
            # over the blocked domain outcome either.
            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
