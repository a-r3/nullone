#!/usr/bin/env python3
"""Tests for the static wake-up edge (`nullone-scheduled-wakeup.py`, #59
remaining scope).

Exercises the CLI module's own `wake_up()` end to end against the REAL
`nullone_scheduled_occurrence_authority.resolve_scheduled_occurrence` and
the REAL `run_morning_workflow`/`run_analytics_workflow`, but always with
injected fakes standing in for the production Claude CLI provider,
AnalyticsProvider factory, and OpenClaw/Telegram notifier -- exactly the
same test-double discipline `tests/test_morning_workflow.py` and
`tests/test_scheduled_run_cli.py` already use, never the real production
adapters `nullone_scheduled_run_dispatch.py` wires in.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_analytics_workflow import run_analytics_workflow  # noqa: E402
from nullone_morning_workflow import run_morning_workflow  # noqa: E402
from support.morning_artifacts import write_morning_artifacts  # noqa: E402


def _load_wakeup_cli():
    spec = importlib.util.spec_from_file_location(
        "nullone_scheduled_wakeup", SCRIPTS / "nullone-scheduled-wakeup.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load nullone-scheduled-wakeup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WAKEUP = _load_wakeup_cli()


def _fixed_clock(dt: datetime):
    def _now() -> datetime:
        return dt
    return _now


def _capture_wake_up(*args: Any, **kwargs: Any) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = WAKEUP.wake_up(*args, **kwargs)
    return code, buf.getvalue()


class EarlyWakeIsNoOpTests(unittest.TestCase):
    def test_before_slot_makes_zero_provider_notifier_or_result_calls(self):
        provider_calls: list[int] = []
        notifier_calls: list[int] = []

        def exploding_provider() -> None:
            provider_calls.append(1)
            raise AssertionError("provider must not be called before today's slot")

        def exploding_notifier(_r: dict[str, Any]) -> dict[str, Any]:
            notifier_calls.append(1)
            raise AssertionError("notifier must not be called before today's slot")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def dispatch_morning(trigger: dict[str, Any]):
                return run_morning_workflow(
                    trigger,
                    invoke_provider=exploding_provider,
                    notifier=exploding_notifier,
                    artifact_root=root / "artifacts",
                    output_root=root / "run-outcomes",
                    sleep=lambda _s: None,
                )

            exit_code = WAKEUP.wake_up(
                "morning",
                source="openclaw",
                now=_fixed_clock(datetime(2026, 9, 8, 3, 0, 0, tzinfo=timezone.utc)),
                dispatch_by_workflow={"morning-editorial": dispatch_morning, "daily-analytics": dispatch_morning},
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(provider_calls, [])
        self.assertEqual(notifier_calls, [])
        self.assertFalse((root / "run-outcomes").exists())


class DueWakeInvokesRealWorkflowTests(unittest.TestCase):
    def test_due_wake_produces_persisted_result_via_real_morning_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact_root = root / "artifacts"
            output_root = root / "run-outcomes"

            provider_calls: list[int] = []

            def succeed_provider() -> None:
                provider_calls.append(1)
                write_morning_artifacts(artifact_root, "2026-09-08")

            notifier_calls: list[dict[str, Any]] = []

            def notifier(persisted: dict[str, Any]) -> dict[str, Any]:
                notifier_calls.append(persisted)
                return {"status": "NOT_REQUIRED"}

            def dispatch_morning(trigger: dict[str, Any]):
                return run_morning_workflow(
                    trigger,
                    invoke_provider=succeed_provider,
                    notifier=notifier,
                    artifact_root=artifact_root,
                    output_root=output_root,
                    sleep=lambda _s: None,
                )

            exit_code = WAKEUP.wake_up(
                "morning",
                source="openclaw",
                now=_fixed_clock(datetime(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc)),
                dispatch_by_workflow={"morning-editorial": dispatch_morning, "daily-analytics": dispatch_morning},
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(len(notifier_calls), 1)


class RepeatedWakeReplaysNotRerunsTests(unittest.TestCase):
    def test_08_30_then_09_07_then_10_15_call_provider_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact_root = root / "artifacts"
            output_root = root / "run-outcomes"

            provider_calls: list[int] = []

            def succeed_provider() -> None:
                provider_calls.append(1)
                write_morning_artifacts(artifact_root, "2026-09-08")

            notifier_calls: list[str] = []

            def notifier(_r: dict[str, Any]) -> dict[str, Any]:
                status = "SENT" if not notifier_calls else "ALREADY_SENT"
                notifier_calls.append(status)
                return {"status": status}

            def dispatch_morning(trigger: dict[str, Any]):
                return run_morning_workflow(
                    trigger,
                    invoke_provider=succeed_provider,
                    notifier=notifier,
                    artifact_root=artifact_root,
                    output_root=output_root,
                    sleep=lambda _s: None,
                )

            occurrence_ids = set()
            for hour, minute in ((4, 30), (5, 7), (6, 15)):
                exit_code = WAKEUP.wake_up(
                    "morning",
                    source="openclaw",
                    now=_fixed_clock(datetime(2026, 9, 8, hour, minute, 0, tzinfo=timezone.utc)),
                    dispatch_by_workflow={"morning-editorial": dispatch_morning, "daily-analytics": dispatch_morning},
                )
                self.assertEqual(exit_code, 0)

            self.assertEqual(len(provider_calls), 1)
            self.assertEqual(notifier_calls, ["SENT", "ALREADY_SENT", "ALREADY_SENT"])


class ConcurrentWakeUpsRunOneLogicalCycleTests(unittest.TestCase):
    def test_four_concurrent_wakeups_same_slot_one_provider_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact_root = root / "artifacts"
            output_root = root / "run-outcomes"

            call_lock = threading.Lock()
            provider_calls: list[int] = []

            def slow_provider() -> None:
                with call_lock:
                    provider_calls.append(1)
                write_morning_artifacts(artifact_root, "2026-09-08")

            notify_lock = threading.Lock()
            notifier_calls: list[int] = []

            def notifier(_r: dict[str, Any]) -> dict[str, Any]:
                with notify_lock:
                    notifier_calls.append(1)
                return {"status": "NOT_REQUIRED"}

            def dispatch_morning(trigger: dict[str, Any]):
                return run_morning_workflow(
                    trigger,
                    invoke_provider=slow_provider,
                    notifier=notifier,
                    artifact_root=artifact_root,
                    output_root=output_root,
                    sleep=lambda _s: None,
                )

            exit_codes: list[int] = []
            results_lock = threading.Lock()

            def worker() -> None:
                code = WAKEUP.wake_up(
                    "morning",
                    source="openclaw",
                    now=_fixed_clock(datetime(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc)),
                    dispatch_by_workflow={"morning-editorial": dispatch_morning, "daily-analytics": dispatch_morning},
                )
                with results_lock:
                    exit_codes.append(code)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(provider_calls), 1)
            self.assertTrue(all(code == 0 for code in exit_codes))


class AnalyticsProviderSecretPendingIsNotFakedTests(unittest.TestCase):
    """#61 boundary: a DUE analytics wake reaching the real
    AnalyticsWorkflow with an unauthorized provider factory still yields a
    BLOCKED domain outcome and application_execution=COMPLETED -- this
    module never fakes Analytics success."""

    def test_analytics_due_wake_blocked_without_secret(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def unauthorized_factory() -> Any:
                from nullone_zernio_analytics_adapter import ConnectorUnauthorizedError

                raise ConnectorUnauthorizedError("missing token")

            def dispatch_analytics(trigger: dict[str, Any]):
                return run_analytics_workflow(
                    trigger,
                    provider_factory=unauthorized_factory,
                    notifier=lambda _r: {"status": "NOT_REQUIRED"},
                    artifact_root=root / "artifacts",
                    output_root=root / "run-outcomes",
                )

            exit_code = WAKEUP.wake_up(
                "analytics",
                source="openclaw",
                now=_fixed_clock(datetime(2026, 9, 8, 23, 20, 0, tzinfo=timezone.utc)),
                dispatch_by_workflow={"morning-editorial": dispatch_analytics, "daily-analytics": dispatch_analytics},
            )

        self.assertEqual(exit_code, 0)


class ManualRunCannotMintNovelOccurrenceTests(unittest.TestCase):
    def test_manual_run_before_slot_is_no_due(self):
        exit_code = WAKEUP.wake_up(
            "morning",
            source="openclaw",
            now=_fixed_clock(datetime(2026, 9, 8, 3, 0, 0, tzinfo=timezone.utc)),
            dispatch_by_workflow={
                "morning-editorial": lambda _t: (_ for _ in ()).throw(AssertionError("must not dispatch")),
                "daily-analytics": lambda _t: (_ for _ in ()).throw(AssertionError("must not dispatch")),
            },
        )
        self.assertEqual(exit_code, 0)

    def test_manual_run_after_slot_matches_scheduled_occurrence(self):
        seen_triggers: list[dict[str, Any]] = []

        class _FakeResult:
            application_execution = "COMPLETED"
            domain_outcome = "SUCCEEDED"
            run_id = "run_fake"
            occurrence_id = "occ_fake"
            result_file = None
            notification_status = "NOT_REQUIRED"
            reason_code = "OK"
            reason_text = "fake"

        def dispatch(trigger: dict[str, Any]):
            seen_triggers.append(trigger)
            return _FakeResult()

        scheduled_exit = WAKEUP.wake_up(
            "morning",
            source="openclaw",
            now=_fixed_clock(datetime(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc)),
            dispatch_by_workflow={"morning-editorial": dispatch, "daily-analytics": dispatch},
        )
        manual_exit = WAKEUP.wake_up(
            "morning",
            source="openclaw",
            now=_fixed_clock(datetime(2026, 9, 8, 18, 0, 0, tzinfo=timezone.utc)),
            dispatch_by_workflow={"morning-editorial": dispatch, "daily-analytics": dispatch},
        )

        self.assertEqual(scheduled_exit, 0)
        self.assertEqual(manual_exit, 0)
        self.assertEqual(len(seen_triggers), 2)
        self.assertEqual(seen_triggers[0]["occurrence_id"], seen_triggers[1]["occurrence_id"])
        self.assertEqual(seen_triggers[0]["scheduled_for"], seen_triggers[1]["scheduled_for"])


class M0SourceAllowlistTests(unittest.TestCase):
    """Current M0 executable pins source to reviewed `openclaw` only.

    Generic authority still supports alternate namespaces; only this edge
    rejects them.
    """

    def test_allowlist_constant_is_exactly_openclaw(self):
        self.assertEqual(WAKEUP.M0_WAKEUP_SOURCES, frozenset({"openclaw"}))

    def test_reviewed_openclaw_source_proceeds(self):
        seen: list[dict[str, Any]] = []

        class _FakeResult:
            application_execution = "COMPLETED"
            domain_outcome = "SUCCEEDED"
            run_id = "run_fake"
            occurrence_id = "occ_fake"
            result_file = None
            notification_status = "NOT_REQUIRED"
            reason_code = "OK"
            reason_text = "fake"

        def dispatch(trigger: dict[str, Any]):
            seen.append(trigger)
            return _FakeResult()

        code, out = _capture_wake_up(
            "morning",
            source="openclaw",
            now=_fixed_clock(datetime(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc)),
            dispatch_by_workflow={"morning-editorial": dispatch, "daily-analytics": dispatch},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        self.assertIn("SOURCE=openclaw", out)
        self.assertIn("STATUS=DUE", out)

    def _assert_source_rejected(self, source: object) -> str:
        provider_calls: list[int] = []
        notifier_calls: list[int] = []

        def exploding_provider() -> None:
            provider_calls.append(1)
            raise AssertionError("provider must not run for unreviewed source")

        def exploding_notifier(_r: dict[str, Any]) -> dict[str, Any]:
            notifier_calls.append(1)
            raise AssertionError("notifier must not run for unreviewed source")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def dispatch_morning(trigger: dict[str, Any]):
                return run_morning_workflow(
                    trigger,
                    invoke_provider=exploding_provider,
                    notifier=exploding_notifier,
                    artifact_root=root / "artifacts",
                    output_root=root / "run-outcomes",
                    sleep=lambda _s: None,
                )

            code, out = _capture_wake_up(
                "morning",
                source=source,  # type: ignore[arg-type]
                now=_fixed_clock(datetime(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc)),
                dispatch_by_workflow={
                    "morning-editorial": dispatch_morning,
                    "daily-analytics": dispatch_morning,
                },
            )

            self.assertNotEqual(code, 0)
            self.assertIn("STATUS=WAKEUP_REJECTED", out)
            self.assertIn(f"REASON_CODE={WAKEUP.REASON_SOURCE_UNSUPPORTED}", out)
            self.assertNotIn("STATUS=DUE", out)
            # Never echo the arbitrary/malformed source back to the operator.
            if isinstance(source, str) and source not in ("",):
                self.assertNotIn(source, out)
            self.assertEqual(provider_calls, [])
            self.assertEqual(notifier_calls, [])
            self.assertFalse((root / "run-outcomes").exists())
            artifact_root = root / "artifacts"
            if artifact_root.exists():
                self.assertEqual(list(artifact_root.rglob("*")), [])
        return out

    def test_systemd_timer_source_rejected(self):
        self._assert_source_rejected("systemd-timer")

    def test_typo_openclaww_source_rejected(self):
        self._assert_source_rejected("openclaww")

    def test_unknown_source_rejected(self):
        self._assert_source_rejected("unknown")

    def test_padded_openclaw_source_rejected_no_strip(self):
        self._assert_source_rejected(" openclaw ")

    def test_empty_source_rejected(self):
        self._assert_source_rejected("")

    def test_newline_and_control_source_rejected_without_echo(self):
        for bad in ("openclaw\n", "openclaw\x00", "\x1bopenclaw", "OPENCLAW"):
            with self.subTest(source=repr(bad)):
                out = self._assert_source_rejected(bad)
                # Stable reason lines only; never re-emit the rejected token.
                self.assertEqual(
                    [line for line in out.splitlines() if line.startswith("REASON_CODE=")],
                    [f"REASON_CODE={WAKEUP.REASON_SOURCE_UNSUPPORTED}"],
                )
                if "\x00" in bad:
                    self.assertNotIn("\x00", out)
                if "\x1b" in bad:
                    self.assertNotIn("\x1b", out)


class StrictClockContractTests(unittest.TestCase):
    def test_aware_utc_accepted(self):
        seen: list[dict[str, Any]] = []

        class _FakeResult:
            application_execution = "COMPLETED"
            domain_outcome = "SUCCEEDED"
            run_id = "run_fake"
            occurrence_id = "occ_fake"
            result_file = None
            notification_status = "NOT_REQUIRED"
            reason_code = "OK"
            reason_text = "fake"

        def dispatch(trigger: dict[str, Any]):
            seen.append(trigger)
            return _FakeResult()

        code, out = _capture_wake_up(
            "morning",
            source="openclaw",
            now=_fixed_clock(datetime(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc)),
            dispatch_by_workflow={"morning-editorial": dispatch, "daily-analytics": dispatch},
        )
        self.assertEqual(code, 0)
        self.assertEqual(seen[0]["triggered_at"], "2026-09-08T04:30:00Z")
        self.assertIn("TRIGGERED_AT=2026-09-08T04:30:00Z", out)

    def test_aware_non_utc_normalized_to_utc(self):
        seen: list[dict[str, Any]] = []

        class _FakeResult:
            application_execution = "COMPLETED"
            domain_outcome = "SUCCEEDED"
            run_id = "run_fake"
            occurrence_id = "occ_fake"
            result_file = None
            notification_status = "NOT_REQUIRED"
            reason_code = "OK"
            reason_text = "fake"

        def dispatch(trigger: dict[str, Any]):
            seen.append(trigger)
            return _FakeResult()

        baku = timezone(timedelta(hours=4))
        code, _out = _capture_wake_up(
            "morning",
            source="openclaw",
            now=_fixed_clock(datetime(2026, 9, 8, 8, 30, 0, tzinfo=baku)),
            dispatch_by_workflow={"morning-editorial": dispatch, "daily-analytics": dispatch},
        )
        self.assertEqual(code, 0)
        self.assertEqual(seen[0]["triggered_at"], "2026-09-08T04:30:00Z")
        self.assertEqual(seen[0]["scheduled_for"], "2026-09-08T04:30:00Z")

    def _assert_clock_rejected(self, clock_value: object) -> str:
        provider_calls: list[int] = []
        notifier_calls: list[int] = []

        def exploding_provider() -> None:
            provider_calls.append(1)
            raise AssertionError("provider must not run for invalid clock")

        def exploding_notifier(_r: dict[str, Any]) -> dict[str, Any]:
            notifier_calls.append(1)
            raise AssertionError("notifier must not run for invalid clock")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def dispatch_morning(trigger: dict[str, Any]):
                return run_morning_workflow(
                    trigger,
                    invoke_provider=exploding_provider,
                    notifier=exploding_notifier,
                    artifact_root=root / "artifacts",
                    output_root=root / "run-outcomes",
                    sleep=lambda _s: None,
                )

            code, out = _capture_wake_up(
                "morning",
                source="openclaw",
                now=lambda: clock_value,  # type: ignore[return-value,arg-type]
                dispatch_by_workflow={
                    "morning-editorial": dispatch_morning,
                    "daily-analytics": dispatch_morning,
                },
            )
            self.assertNotEqual(code, 0)
            self.assertIn("STATUS=WAKEUP_REJECTED", out)
            self.assertIn(f"REASON_CODE={WAKEUP.REASON_CLOCK_INVALID}", out)
            self.assertNotIn("STATUS=DUE", out)
            self.assertEqual(provider_calls, [])
            self.assertEqual(notifier_calls, [])
            self.assertFalse((root / "run-outcomes").exists())
        return out

    def test_naive_datetime_rejected(self):
        out = self._assert_clock_rejected(datetime(2026, 9, 8, 4, 30, 0))
        self.assertNotIn("2026-09-08", out)

    def test_string_none_object_clock_rejected(self):
        for bad in ("2026-09-08T04:30:00Z", None, object(), 12345):
            with self.subTest(clock=repr(bad)):
                out = self._assert_clock_rejected(bad)
                self.assertNotIn(repr(bad), out)

    def test_naive_clock_rejection_has_zero_workflow_side_effects(self):
        self._assert_clock_rejected(datetime(2026, 9, 8, 4, 30, 0))

    def test_host_timezone_cannot_affect_aware_normalization(self):
        """Aware instants normalize identically regardless of ZoneInfo labels.

        Proves host-local interpretation is not used: two distinct aware
        offsets that encode the same absolute instant yield one triggered_at.
        """
        seen: list[str] = []

        class _FakeResult:
            application_execution = "COMPLETED"
            domain_outcome = "SUCCEEDED"
            run_id = "run_fake"
            occurrence_id = "occ_fake"
            result_file = None
            notification_status = "NOT_REQUIRED"
            reason_code = "OK"
            reason_text = "fake"

        def dispatch(trigger: dict[str, Any]):
            seen.append(trigger["triggered_at"])
            return _FakeResult()

        fixed = {"morning-editorial": dispatch, "daily-analytics": dispatch}
        utc_code, _ = _capture_wake_up(
            "morning",
            source="openclaw",
            now=_fixed_clock(datetime(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc)),
            dispatch_by_workflow=fixed,
        )
        tokyo = ZoneInfo("Asia/Tokyo")
        tokyo_code, _ = _capture_wake_up(
            "morning",
            source="openclaw",
            # 13:30 JST == 04:30 UTC
            now=_fixed_clock(datetime(2026, 9, 8, 13, 30, 0, tzinfo=tokyo)),
            dispatch_by_workflow=fixed,
        )
        self.assertEqual(utc_code, 0)
        self.assertEqual(tokyo_code, 0)
        self.assertEqual(seen, ["2026-09-08T04:30:00Z", "2026-09-08T04:30:00Z"])

    def test_operator_output_does_not_echo_clock_object(self):
        class WeirdClock:
            def __repr__(self) -> str:
                return "LEAKED_CLOCK_REPR\x00"

        out = self._assert_clock_rejected(WeirdClock())
        self.assertNotIn("LEAKED_CLOCK_REPR", out)
        self.assertNotIn("\x00", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
