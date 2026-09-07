#!/usr/bin/env python3
"""Composition tests for AnalyticsWorkflow (#59).

Proves AnalyticsWorkflow's own reload/reconciliation/notification-
composition logic, the Baku date-boundary derivation, the AnalyticsProvider
boundary (no secret/env dependency), and the scheduler-vs-domain
separation. Deliberately does not duplicate #29's own exhaustive connector/
artifact-commit test suite (`tests/test_daily_analytics.py`).
`nullone_analytics_workflow.py`'s own embedded `self_test()` (invoked via
`python3 ... self-test` in `tests/run_offline.py`) already covers the full
end-to-end scenario matrix from #59's acceptance criteria; this file adds
`unittest`-style assertions for a few properties that matter enough to pin
down with explicit, named tests.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_analytics_workflow import (  # noqa: E402
    AnalyticsWorkflowResult,
    run_analytics_workflow,
)
from nullone_bridge_common import CANONICAL_ACCOUNT_ID  # noqa: E402
from nullone_scheduler_invocation import compute_occurrence_id  # noqa: E402
from nullone_zernio_analytics_adapter import (  # noqa: E402
    ConnectorUnauthorizedError,
    ZernioReadOnlyAnalyticsConnector,
)


def make_trigger(**overrides):
    base = {
        "schema": "nullone.scheduler-invocation.v1",
        "contract_version": "1.0.0",
        "workflow_id": "daily-analytics",
        "source": "openclaw",
        "external_occurrence_id": "test-occ-analytics",
        "scheduled_for": "2026-09-08T23:20:00Z",
        "triggered_at": "2026-09-08T23:20:02Z",
    }
    base.update(overrides)
    base["occurrence_id"] = compute_occurrence_id(
        base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
    )
    return base


def _no_data_factory():
    class Transport:
        def get(self, path, *, params=None):
            if path == "/accounts":
                return 200, {
                    "accounts": [
                        {"_id": CANONICAL_ACCOUNT_ID, "platform": "instagram", "username": "x", "isActive": True}
                    ],
                    "hasAnalyticsAccess": True,
                }
            if path in ("/analytics/instagram/follower-history", "/analytics/instagram/account-insights"):
                return 200, {
                    "success": True,
                    "accountId": CANONICAL_ACCOUNT_ID,
                    "platform": "instagram",
                    "metricType": "total_value",
                    "metrics": {},
                }
            if path == "/analytics":
                return 200, {"posts": [], "pagination": {"page": 1, "limit": 25, "total": 0, "pages": 0}}
            raise AssertionError(f"unexpected path: {path}")

    return ZernioReadOnlyAnalyticsConnector(Transport(), account_id=CANONICAL_ACCOUNT_ID)


class BakuDateDerivationTests(unittest.TestCase):
    def test_analytics_date_uses_baku_not_occurrence_id_prefix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trigger = make_trigger(scheduled_for="2026-09-08T23:20:00Z")
            result = run_analytics_workflow(
                trigger,
                provider_factory=_no_data_factory,
                artifact_root=root / "artifacts",
                output_root=root / "run-outcomes",
            )
            self.assertEqual(result.application_execution, "COMPLETED")
            # UTC calendar date is 2026-09-08; Baku (+04:00) is 2026-09-09.
            self.assertEqual(result.analytics_date, "2026-09-09")
            self.assertFalse(trigger["occurrence_id"].startswith("2026-09-09"))


class ProviderBoundaryTests(unittest.TestCase):
    def test_provider_factory_called_at_most_once_and_lazily(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls = []

            def counting_factory():
                calls.append(1)
                return _no_data_factory()

            trigger = make_trigger(external_occurrence_id="test-occ-analytics-lazy")
            run_analytics_workflow(
                trigger, provider_factory=counting_factory,
                artifact_root=root / "artifacts", output_root=root / "run-outcomes",
            )
            self.assertEqual(len(calls), 1)

    def test_trigger_rejected_never_calls_provider_factory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def exploding():
                raise AssertionError("must not be called")

            result = run_analytics_workflow(
                {"workflow_id": "daily-analytics"},
                provider_factory=exploding,
                artifact_root=root / "artifacts", output_root=root / "run-outcomes",
            )
            self.assertEqual(result.application_execution, "FAILED")
            self.assertEqual(result.reason_code, "TRIGGER_REJECTED")


class ScheduleVsDomainSeparationTests(unittest.TestCase):
    def test_blocked_domain_outcome_is_still_completed_orchestration(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def unauthorized():
                raise ConnectorUnauthorizedError("missing token")

            result = run_analytics_workflow(
                make_trigger(external_occurrence_id="test-occ-analytics-blocked"),
                provider_factory=unauthorized,
                notifier=lambda _r: {"status": "SENT"},
                artifact_root=root / "artifacts", output_root=root / "run-outcomes",
            )
            self.assertEqual(result.application_execution, "COMPLETED")
            self.assertEqual(result.domain_outcome, "BLOCKED")
            self.assertIsInstance(result, AnalyticsWorkflowResult)

    def test_runtime_crash_before_result_is_application_failure(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def crashes():
                raise RuntimeError("unexpected, not one of #29's typed connector errors")

            result = run_analytics_workflow(
                make_trigger(external_occurrence_id="test-occ-analytics-crash"),
                provider_factory=crashes,
                artifact_root=root / "artifacts", output_root=root / "run-outcomes",
            )
            self.assertEqual(result.application_execution, "FAILED")
            self.assertEqual(result.reason_code, "RUNTIME_CRASHED")
            self.assertIsNone(result.domain_outcome)


class DifferentOccurrenceIndependenceTests(unittest.TestCase):
    def test_distinct_occurrences_get_distinct_run_ids(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trigger_a = make_trigger(external_occurrence_id="test-occ-analytics-a")
            trigger_b = make_trigger(external_occurrence_id="test-occ-analytics-b")
            self.assertNotEqual(trigger_a["occurrence_id"], trigger_b["occurrence_id"])

            result_a = run_analytics_workflow(
                trigger_a, provider_factory=_no_data_factory,
                artifact_root=root / "artifacts", output_root=root / "run-outcomes",
            )
            result_b = run_analytics_workflow(
                trigger_b, provider_factory=_no_data_factory,
                artifact_root=root / "artifacts", output_root=root / "run-outcomes",
            )
            self.assertNotEqual(result_a.run_id, result_b.run_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
