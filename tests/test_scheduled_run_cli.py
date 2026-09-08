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

import nullone_scheduled_run_dispatch as dispatch  # noqa: E402
from nullone_analytics_workflow import run_analytics_workflow  # noqa: E402
from nullone_editorial_runtime import ProviderUnreachableError  # noqa: E402
from nullone_failure_notify import notify_if_required  # noqa: E402
from nullone_morning_workflow import run_morning_workflow  # noqa: E402
from support.morning_artifacts import write_morning_artifacts  # noqa: E402
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


class MorningNativeAlertOwnershipHardeningTests(unittest.TestCase):
    """Proves the pre-hardening no-alert gap is fixed end to end through
    the real production notifier binding
    (`nullone_scheduled_run_dispatch.production_notifier`), which is the
    exact wiring both `nullone-scheduled-run.py morning` and
    `nullone-scheduled-wakeup.py morning` use (#59 remaining-scope section
    16 extracted this wiring into a module shared by both entrypoints).

    Before this fix: a persistent real #28 Morning provider failure
    persists `scheduler_status="error"`/`domain_outcome="FAILED"`; the new
    #59 CLI correctly exits 0 for that (valid orchestration completed);
    but the unmodified #30 `notify_if_required()` saw `scheduler_status
    ="error"` and deferred to OpenClaw's native `failureAlert` -- which
    never fires because the process exits 0. Net result: silence. This
    test proves that gap is closed: the production notifier now passes
    `scheduler_native_failure_owned=False` explicitly, so the alert fires
    exactly once, and exact replay does not resend.
    """

    @staticmethod
    def _isolated_notify_if_required(output_root):
        """A stand-in for `nullone_failure_notify.notify_if_required` that
        forwards every call unchanged except pinning `output_root` to an
        isolated temp directory -- `dispatch.production_notifier` itself
        never exposes an `output_root` override (correctly: production
        always uses the real default), so a test exercising it verbatim
        must instead isolate storage by patching the name
        `dispatch.notify_if_required` resolves at call time, not by
        passing extra arguments through `production_notifier`."""

        def _wrapped(result, *, transport, scheduler_native_failure_owned=None, **kwargs):
            return notify_if_required(
                result,
                transport=transport,
                output_root=output_root,
                scheduler_native_failure_owned=scheduler_native_failure_owned,
                **kwargs,
            )

        return _wrapped

    def test_morning_provider_failure_reaches_domain_alert_exactly_once(self):
        import json
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transport_calls = []

            class FakeTransport:
                def send(self, message: str) -> None:
                    transport_calls.append(message)

            trigger = make_trigger(
                workflow_id="morning-editorial",
                source="openclaw",
                external_occurrence_id="cli-morning-native-alert",
                scheduled_for="2026-09-08T04:30:00Z",
                triggered_at="2026-09-08T04:30:02Z",
            )

            provider_calls = []

            def always_unreachable():
                provider_calls.append(1)
                raise ProviderUnreachableError("ENOTFOUND")

            isolated_notify = self._isolated_notify_if_required(root / "notifications")

            with mock.patch.object(dispatch, "OpenClawTelegramTransport", return_value=FakeTransport()), \
                 mock.patch.object(dispatch, "notify_if_required", isolated_notify):
                first = run_morning_workflow(
                    trigger,
                    invoke_provider=always_unreachable,
                    notifier=dispatch.production_notifier,
                    artifact_root=root / "artifacts",
                    output_root=root / "run-outcomes",
                    sleep=lambda _s: None,
                )

            # Confirm #28 really did persist the legacy scheduler-native
            # shape this regression is about -- not a hypothetical one.
            persisted = json.loads(Path(first.result_file).read_text(encoding="utf-8"))
            self.assertEqual(persisted["scheduler_status"], "error")
            self.assertEqual(persisted["domain_outcome"], "FAILED")

            self.assertEqual(first.application_execution, "COMPLETED")
            self.assertEqual(cli._report(first), 0)
            self.assertEqual(first.domain_outcome, "FAILED")
            self.assertEqual(first.notification_status, "SENT")
            self.assertEqual(len(transport_calls), 1)
            self.assertGreaterEqual(len(provider_calls), 1)

            # Exact replay: provider not called again, no second alert,
            # still exit 0.
            with mock.patch.object(dispatch, "OpenClawTelegramTransport", return_value=FakeTransport()), \
                 mock.patch.object(dispatch, "notify_if_required", isolated_notify):
                second = run_morning_workflow(
                    trigger,
                    invoke_provider=lambda: (_ for _ in ()).throw(
                        AssertionError("provider must not run again on replay")
                    ),
                    notifier=dispatch.production_notifier,
                    artifact_root=root / "artifacts",
                    output_root=root / "run-outcomes",
                    sleep=lambda _s: None,
                )

            self.assertEqual(second.application_execution, "COMPLETED")
            self.assertEqual(cli._report(second), 0)
            self.assertEqual(second.notification_status, "ALREADY_SENT")
            self.assertEqual(len(transport_calls), 1)

    def test_daily_blocked_result_unchanged_by_ownership_override(self):
        """The explicit False override must not change Daily Analytics'
        already-correct scheduler_status="succeeded" BLOCKED behavior."""
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transport_calls = []

            class FakeTransport:
                def send(self, message: str) -> None:
                    transport_calls.append(message)

            def unauthorized_factory():
                raise ConnectorUnauthorizedError("missing token")

            trigger = make_trigger(external_occurrence_id="cli-daily-blocked-unchanged")
            isolated_notify = self._isolated_notify_if_required(root / "notifications")

            with mock.patch.object(dispatch, "OpenClawTelegramTransport", return_value=FakeTransport()), \
                 mock.patch.object(dispatch, "notify_if_required", isolated_notify):
                first = run_analytics_workflow(
                    trigger,
                    provider_factory=unauthorized_factory,
                    notifier=dispatch.production_notifier,
                    artifact_root=root / "artifacts",
                    output_root=root / "run-outcomes",
                )
                second = run_analytics_workflow(
                    trigger,
                    provider_factory=lambda: (_ for _ in ()).throw(
                        AssertionError("provider must not run again on replay")
                    ),
                    notifier=dispatch.production_notifier,
                    artifact_root=root / "artifacts",
                    output_root=root / "run-outcomes",
                )

            self.assertEqual(first.application_execution, "COMPLETED")
            self.assertEqual(first.domain_outcome, "BLOCKED")
            self.assertEqual(cli._report(first), 0)
            self.assertEqual(first.notification_status, "SENT")
            self.assertEqual(second.notification_status, "ALREADY_SENT")
            self.assertEqual(len(transport_calls), 1)


class MalformedNotifierLeakRegressionTests(unittest.TestCase):
    """Proves a malformed notifier result's secret-like content never
    reaches the CLI's own printed output (`cli._report`'s
    `REASON_TEXT=...` line), matching the same guarantee already proven at
    the workflow-result level in `nullone_morning_workflow.py`/
    `nullone_analytics_workflow.py`'s own `self_test()`."""

    SECRET_MARKERS = ("FAKE-SECRET-zat_123456", "token=FAKE_SECRET")
    LEAK_CASES = (
        {"status": "FAKE-SECRET-zat_123456"},
        {"status": "token=FAKE_SECRET"},
        {"status": ["FAKE_SECRET"]},
    )

    def _assert_report_output_is_clean(self, result) -> None:
        import contextlib
        import io

        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "NOTIFICATION_RESULT_INVALID")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = cli._report(result)
        captured = buf.getvalue()

        self.assertNotEqual(exit_code, 0)
        for marker in self.SECRET_MARKERS:
            self.assertNotIn(marker, captured, captured)

    def test_morning_malformed_notifier_leak_absent_from_cli_output(self):
        import tempfile

        for index, bad_outcome in enumerate(self.LEAK_CASES):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                artifact_root = root / "artifacts"

                def succeed():
                    write_morning_artifacts(artifact_root, "2026-09-08")

                trigger = make_trigger(
                    workflow_id="morning-editorial",
                    external_occurrence_id=f"cli-morning-notifier-leak-{index}",
                    scheduled_for="2026-09-08T04:30:00Z",
                    triggered_at="2026-09-08T04:30:02Z",
                )
                result = run_morning_workflow(
                    trigger,
                    invoke_provider=succeed,
                    notifier=lambda _r, _bad=bad_outcome: _bad,
                    artifact_root=artifact_root,
                    output_root=root / "run-outcomes",
                    sleep=lambda _s: None,
                )
                self._assert_report_output_is_clean(result)

    def test_analytics_malformed_notifier_leak_absent_from_cli_output(self):
        import tempfile

        for index, bad_outcome in enumerate(self.LEAK_CASES):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)

                def unauthorized_factory():
                    raise ConnectorUnauthorizedError("missing token")

                trigger = make_trigger(external_occurrence_id=f"cli-analytics-notifier-leak-{index}")
                # A BLOCKED domain outcome (unauthorized provider) still
                # reaches the #30 notifier boundary -- see
                # SchedulerDomainSeparationExitCodeTests above -- so this
                # exercises the same NOTIFICATION_RESULT_INVALID path
                # without needing a real successful connector fixture.
                result = run_analytics_workflow(
                    trigger,
                    provider_factory=unauthorized_factory,
                    notifier=lambda _r, _bad=bad_outcome: _bad,
                    artifact_root=root / "artifacts",
                    output_root=root / "run-outcomes",
                )
                self._assert_report_output_is_clean(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
