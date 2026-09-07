#!/usr/bin/env python3
"""Mandatory #59 regressions for the scheduler-facing CLI (`nullone-scheduled-run.py`).

Section references are to the #59 implementation goal this repository was
asked to satisfy:

- The critical scheduler/domain exit-code regression: a valid persisted
  Daily Analytics `domain_outcome=BLOCKED` / `scheduler_status=succeeded`
  result must still yield `application_execution=COMPLETED` (CLI exit 0),
  and the #30 domain notifier must be evaluated.
- Corrupt/missing exact #27 result -> application non-zero.
- The notifier at-most-once regression: exact replay of an actionable
  domain result sends exactly once (`SENT` then `ALREADY_SENT`), with
  exactly one underlying transport call.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_analytics_workflow import run_analytics_workflow  # noqa: E402
from nullone_failure_notify import notify_if_required  # noqa: E402
from nullone_run_outcome import assess_run  # noqa: E402
from nullone_scheduler_invocation import compute_occurrence_id  # noqa: E402
from nullone_zernio_analytics_adapter import ConnectorUnauthorizedError  # noqa: E402


def _load_cli():
    spec = importlib.util.spec_from_file_location("nullone_scheduled_run", SCRIPTS / "nullone-scheduled-run.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load nullone-scheduled-run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


def make_trigger(**overrides):
    base = {
        "schema": "nullone.scheduler-invocation.v1",
        "contract_version": "1.0.0",
        "workflow_id": "daily-analytics",
        "source": "openclaw",
        "external_occurrence_id": "cli-test-occ",
        "scheduled_for": "2026-09-08T23:20:00Z",
        "triggered_at": "2026-09-08T23:20:02Z",
    }
    base.update(overrides)
    base["occurrence_id"] = compute_occurrence_id(
        base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
    )
    return base


class SchedulerDomainSeparationExitCodeTests(unittest.TestCase):
    def test_blocked_domain_outcome_exits_zero_and_evaluates_notifier(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def unauthorized_factory():
                raise ConnectorUnauthorizedError("missing token")

            notifier_calls = []

            def notifier(persisted):
                notifier_calls.append(persisted)
                return {"status": "SENT"}

            result = run_analytics_workflow(
                make_trigger(external_occurrence_id="cli-blocked-exit0"),
                provider_factory=unauthorized_factory,
                notifier=notifier,
                artifact_root=root / "artifacts",
                output_root=root / "run-outcomes",
            )
            self.assertEqual(result.application_execution, "COMPLETED")
            self.assertEqual(result.domain_outcome, "BLOCKED")
            self.assertEqual(cli._report(result), 0)
            self.assertEqual(len(notifier_calls), 1)

    def test_corrupt_or_missing_result_exits_nonzero(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def fake_run_analytics_missing(**kwargs):
                return assess_run(
                    workflow_id="daily-analytics",
                    occurrence_id=kwargs["occurrence_id"],
                    scheduler_status="succeeded",
                    domain_outcome="SUCCEEDED",
                    empty_success="NO_DATA",
                )

            def exploding_factory():
                raise AssertionError("must not be called")

            result = run_analytics_workflow(
                make_trigger(external_occurrence_id="cli-corrupt-nonzero"),
                provider_factory=exploding_factory,
                run_analytics=fake_run_analytics_missing,
                artifact_root=root / "artifacts",
                output_root=root / "run-outcomes",
            )
            self.assertEqual(result.application_execution, "FAILED")
            self.assertNotEqual(cli._report(result), 0)

    def test_domain_health_is_never_used_as_cli_process_health(self):
        """The legacy convention (domain_outcome != SUCCEEDED -> exit 1)
        must NOT govern the new CLI: FAILED domain outcome with a valid,
        reconciled, notified #27 result is still exit 0."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            class MalformedTransport:
                def get(self, path, *, params=None):
                    if path == "/accounts":
                        return 200, {
                            "accounts": [{"_id": "acct", "platform": "instagram", "username": "x", "isActive": True}],
                            "hasAnalyticsAccess": True,
                        }
                    if path == "/analytics/instagram/follower-history":
                        return 200, {"success": True}  # malformed: missing required keys
                    raise AssertionError(path)

            from nullone_zernio_analytics_adapter import ZernioReadOnlyAnalyticsConnector

            def malformed_factory():
                return ZernioReadOnlyAnalyticsConnector(MalformedTransport(), account_id="acct")

            result = run_analytics_workflow(
                make_trigger(external_occurrence_id="cli-failed-domain-exit0"),
                provider_factory=malformed_factory,
                notifier=lambda _r: {"status": "SENT"},
                artifact_root=root / "artifacts",
                output_root=root / "run-outcomes",
            )
            self.assertEqual(result.domain_outcome, "FAILED")
            self.assertEqual(result.application_execution, "COMPLETED")
            self.assertEqual(cli._report(result), 0)


class NotifierAtMostOnceTests(unittest.TestCase):
    def test_exact_replay_sends_exactly_once(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transport_calls = []

            class FakeTransport:
                def send(self, message: str) -> None:
                    transport_calls.append(message)

            result = assess_run(
                workflow_id="daily-analytics",
                occurrence_id="2026-01-01T03:20:00+04:00",
                scheduler_status="succeeded",
                domain_outcome="BLOCKED",
                reason_code="EXAMPLE_DEPENDENCY_UNAVAILABLE",
                reason_text="Analytics access could not be established.",
            )

            first = notify_if_required(
                result, transport=FakeTransport(), output_root=root / "notifications"
            )
            self.assertEqual(first["status"], "SENT")
            self.assertEqual(len(transport_calls), 1)

            second = notify_if_required(
                result, transport=FakeTransport(), output_root=root / "notifications"
            )
            self.assertEqual(second["status"], "ALREADY_SENT")
            self.assertEqual(len(transport_calls), 1)

    def test_workflow_level_replay_of_actionable_result_notifies_once(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def unauthorized_factory():
                raise ConnectorUnauthorizedError("missing token")

            transport_calls = []

            class FakeTransport:
                def send(self, message: str) -> None:
                    transport_calls.append(message)

            def real_notifier(persisted):
                return notify_if_required(persisted, transport=FakeTransport(), output_root=root / "notifications")

            trigger = make_trigger(external_occurrence_id="cli-notify-once")
            first = run_analytics_workflow(
                trigger, provider_factory=unauthorized_factory, notifier=real_notifier,
                artifact_root=root / "artifacts", output_root=root / "run-outcomes",
            )
            second = run_analytics_workflow(
                trigger, provider_factory=lambda: (_ for _ in ()).throw(AssertionError("must not run again")),
                notifier=real_notifier,
                artifact_root=root / "artifacts", output_root=root / "run-outcomes",
            )
            self.assertEqual(first.notification_status, "SENT")
            self.assertEqual(second.notification_status, "ALREADY_SENT")
            self.assertEqual(len(transport_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
