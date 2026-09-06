#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_run_outcome import assess_run  # noqa: E402
from nullone_failure_notify import (  # noqa: E402
    NOTIFICATION_DEFERRED_POLICY,
    NotifierError,
    TransportAmbiguousError,
    TransportFailedError,
    format_occurrence_time,
    is_actionable,
    notify_if_required,
    render_alert_text,
    sanitize_reason_text,
)


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_script(
    "nullone_failure_notify_run_test",
    "nullone-failure-notify-run.py",
)


def _blocked_result(
    *,
    workflow_id: str = "daily-analytics",
    occurrence_id: str = "2026-09-06T03:20:00+04:00",
    reason_code: str = "ZERNIO_ANALYTICS_UNAVAILABLE",
    reason_text: str = "Zernio analytics connector could not be started.",
    domain_outcome: str = "BLOCKED",
    scheduler_status: str = "succeeded",
) -> dict:
    return assess_run(
        workflow_id=workflow_id,
        occurrence_id=occurrence_id,
        scheduler_status=scheduler_status,
        domain_outcome=domain_outcome,
        reason_code=reason_code,
        reason_text=reason_text,
    )


def _succeeded_result(
    *,
    workflow_id: str = "daily-analytics",
    occurrence_id: str = "2026-09-06T03:20:00+04:00",
    empty_success: str | None = "NO_DATA",
) -> dict:
    return assess_run(
        workflow_id=workflow_id,
        occurrence_id=occurrence_id,
        scheduler_status="succeeded",
        domain_outcome="SUCCEEDED",
        empty_success=empty_success,
    )


class RecordingTransport:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


class AmbiguousTransport:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: str) -> None:
        self.calls += 1
        raise TransportAmbiguousError("timed out")


class FailingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, message: str) -> None:
        self.calls += 1
        raise TransportFailedError("exit=1")


class NeverCallTransport:
    def send(self, message: str) -> None:
        raise AssertionError("transport must not be called for this outcome")


class GatedTransport:
    """Blocks inside send() until released, to force two callers to
    genuinely overlap before either finishes."""

    def __init__(self) -> None:
        self.calls = 0
        self._entered = threading.Event()
        self._release = threading.Event()

    def send(self, message: str) -> None:
        self.calls += 1
        self._entered.set()
        self._release.wait(timeout=5)

    def wait_until_entered(self) -> None:
        self._entered.wait(timeout=5)

    def release(self) -> None:
        self._release.set()


class ActionabilityTests(unittest.TestCase):
    def test_succeeded_with_artifacts_is_quiet(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "social/analytics/raw/2026-09-06.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("data", encoding="utf-8")

            result = assess_run(
                workflow_id="daily-analytics",
                occurrence_id="2026-09-06T03:20:00+04:00",
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                artifact_root=root,
                required_artifacts=("social/analytics/raw/2026-09-06.md",),
            )

            self.assertFalse(is_actionable(result))

    def test_succeeded_no_data_is_quiet(self):
        result = _succeeded_result(empty_success="NO_DATA")
        self.assertFalse(is_actionable(result))

    def test_succeeded_no_action_is_quiet(self):
        result = _succeeded_result(empty_success="NO_ACTION")
        self.assertFalse(is_actionable(result))

    def test_blocked_is_actionable(self):
        self.assertTrue(is_actionable(_blocked_result(domain_outcome="BLOCKED")))

    def test_failed_is_actionable(self):
        self.assertTrue(is_actionable(_blocked_result(domain_outcome="FAILED")))

    def test_unknown_is_actionable_by_default(self):
        result = _blocked_result(
            domain_outcome="UNKNOWN",
            reason_code="PUBLISH_READBACK_INCONCLUSIVE",
            reason_text="Publish readback was inconclusive.",
        )
        self.assertTrue(is_actionable(result))

    def test_unknown_reason_code_can_be_marked_non_actionable(self):
        result = _blocked_result(
            domain_outcome="UNKNOWN",
            reason_code="EXAMPLE_INFORMATIONAL_UNKNOWN",
            reason_text="Example non-actionable ambiguity.",
        )
        self.assertTrue(is_actionable(result))
        self.assertFalse(
            is_actionable(
                result,
                non_actionable_unknown_reason_codes=frozenset(
                    {"EXAMPLE_INFORMATIONAL_UNKNOWN"}
                ),
            )
        )


class SanitizationTests(unittest.TestCase):
    def test_bearer_token_is_redacted(self):
        text = "Auth failed: Bearer abc123.DEF-456~ghi was rejected"
        sanitized = sanitize_reason_text(text)
        self.assertNotIn("abc123.DEF-456~ghi", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_api_key_is_redacted(self):
        sanitized = sanitize_reason_text("config error api_key=sk_live_abcdef1234")
        self.assertNotIn("sk_live_abcdef1234", sanitized)

    def test_oauth_token_is_redacted(self):
        sanitized = sanitize_reason_text("oauth_token=zzz.yyy.xxx invalid")
        self.assertNotIn("zzz.yyy.xxx", sanitized)

    def test_presigned_style_query_param_is_redacted(self):
        sanitized = sanitize_reason_text(
            "upload failed for https://example.invalid/x?X-Amz-Signature=deadbeef&foo=bar"
        )
        self.assertNotIn("deadbeef", sanitized)

    def test_multiline_text_is_collapsed_to_one_line(self):
        sanitized = sanitize_reason_text("line one\nline two\r\nline three")
        self.assertNotIn("\n", sanitized)
        self.assertNotIn("\r", sanitized)

    def test_long_text_is_truncated(self):
        sanitized = sanitize_reason_text("x" * 500)
        self.assertLessEqual(len(sanitized), 200)

    def test_none_passes_through(self):
        self.assertIsNone(sanitize_reason_text(None))


class RenderingTests(unittest.TestCase):
    def test_alert_contains_expected_fields_no_secrets(self):
        result = _blocked_result(
            reason_text="Zernio analytics connector could not be started "
            "(token=deadbeef1234)"
        )
        message = render_alert_text(result)

        self.assertIn("Daily Analytics", message)
        self.assertIn("Status: BLOCKED", message)
        self.assertIn("Reason: ZERNIO_ANALYTICS_UNAVAILABLE", message)
        self.assertIn(result["run_id"], message)
        self.assertNotIn("deadbeef1234", message)
        self.assertNotIn("\n\n", message)

    def test_alert_has_no_raw_stack_dump(self):
        result = _blocked_result(
            domain_outcome="FAILED",
            reason_code="EDITORIAL_PROVIDER_ERROR",
            reason_text="Editorial provider call failed.",
        )
        message = render_alert_text(result)
        self.assertNotIn("Traceback", message)
        self.assertLess(len(message.splitlines()), 8)

    def test_baku_offset_is_labeled(self):
        formatted = format_occurrence_time("2026-09-06T03:20:00+04:00")
        self.assertEqual(formatted, "2026-09-06 03:20 Baku")

    def test_opaque_occurrence_id_never_rendered_verbatim(self):
        opaque = "internal-session-deadbeef-42"
        formatted = format_occurrence_time(opaque)
        self.assertNotEqual(formatted, opaque)
        self.assertNotIn(opaque, formatted)
        self.assertEqual(formatted, "unavailable")

    def test_opaque_occurrence_id_does_not_reach_alert_text(self):
        opaque = "internal-session-deadbeef-42"
        result = _blocked_result(occurrence_id=opaque)
        message = render_alert_text(result)
        self.assertNotIn(opaque, message)
        self.assertIn("Time: unavailable", message)


class NotifyIfRequiredTests(unittest.TestCase):
    def test_succeeded_produces_no_send(self):
        with tempfile.TemporaryDirectory() as td:
            outcome = notify_if_required(
                _succeeded_result(),
                transport=NeverCallTransport(),
                output_root=Path(td),
            )
            self.assertEqual(outcome["status"], "NOT_REQUIRED")
            self.assertFalse(any(Path(td).iterdir()))

    def test_blocked_sends_one_alert(self):
        with tempfile.TemporaryDirectory() as td:
            transport = RecordingTransport()
            outcome = notify_if_required(
                _blocked_result(domain_outcome="BLOCKED"),
                transport=transport,
                output_root=Path(td),
            )
            self.assertEqual(outcome["status"], "SENT")
            self.assertEqual(len(transport.messages), 1)

    def test_failed_sends_one_alert(self):
        with tempfile.TemporaryDirectory() as td:
            transport = RecordingTransport()
            outcome = notify_if_required(
                _blocked_result(
                    domain_outcome="FAILED",
                    reason_code="EDITORIAL_PROVIDER_ERROR",
                    reason_text="Editorial provider call failed.",
                    workflow_id="morning-editorial",
                ),
                transport=transport,
                output_root=Path(td),
            )
            self.assertEqual(outcome["status"], "SENT")
            self.assertEqual(len(transport.messages), 1)

    def test_actionable_unknown_sends_one_alert(self):
        with tempfile.TemporaryDirectory() as td:
            transport = RecordingTransport()
            result = _blocked_result(
                domain_outcome="UNKNOWN",
                reason_code="PUBLISH_READBACK_INCONCLUSIVE",
                reason_text="Publish readback was inconclusive.",
            )
            outcome = notify_if_required(
                result,
                transport=transport,
                output_root=Path(td),
            )
            self.assertEqual(outcome["status"], "SENT")
            self.assertEqual(len(transport.messages), 1)

    def test_non_actionable_unknown_produces_no_send(self):
        with tempfile.TemporaryDirectory() as td:
            result = _blocked_result(
                domain_outcome="UNKNOWN",
                reason_code="EXAMPLE_INFORMATIONAL_UNKNOWN",
                reason_text="Example non-actionable ambiguity.",
            )
            outcome = notify_if_required(
                result,
                transport=NeverCallTransport(),
                output_root=Path(td),
                non_actionable_unknown_reason_codes=frozenset(
                    {"EXAMPLE_INFORMATIONAL_UNKNOWN"}
                ),
            )
            self.assertEqual(outcome["status"], "NOT_REQUIRED")

    def test_duplicate_invocation_same_failure_sends_once(self):
        with tempfile.TemporaryDirectory() as td:
            transport = RecordingTransport()
            result = _blocked_result()

            first = notify_if_required(
                result, transport=transport, output_root=Path(td)
            )
            second = notify_if_required(
                result, transport=transport, output_root=Path(td)
            )
            third = notify_if_required(
                result, transport=transport, output_root=Path(td)
            )

            self.assertEqual(first["status"], "SENT")
            self.assertEqual(second["status"], "ALREADY_SENT")
            self.assertEqual(third["status"], "ALREADY_SENT")
            self.assertEqual(len(transport.messages), 1)

    def test_distinct_failures_are_independent(self):
        with tempfile.TemporaryDirectory() as td:
            transport = RecordingTransport()

            result_a = _blocked_result(reason_code="ZERNIO_ANALYTICS_UNAVAILABLE")
            result_b = _blocked_result(
                reason_code="ZERNIO_ANALYTICS_ADDON_REQUIRED",
                reason_text="Zernio analytics add-on is required.",
            )

            outcome_a = notify_if_required(
                result_a, transport=transport, output_root=Path(td)
            )
            outcome_b = notify_if_required(
                result_b, transport=transport, output_root=Path(td)
            )

            self.assertEqual(outcome_a["status"], "SENT")
            self.assertEqual(outcome_b["status"], "SENT")
            self.assertEqual(len(transport.messages), 2)
            self.assertNotEqual(
                outcome_a["record"]["failure_identity"],
                outcome_b["record"]["failure_identity"],
            )

    def test_send_failure_leaves_run_outcome_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            result = _blocked_result()
            before = copy.deepcopy(result)

            outcome = notify_if_required(
                result,
                transport=FailingTransport(),
                output_root=Path(td),
            )

            self.assertEqual(outcome["status"], "FAILED")
            self.assertEqual(result, before)

    def test_ambiguous_transport_is_not_automatically_resent(self):
        with tempfile.TemporaryDirectory() as td:
            transport = AmbiguousTransport()
            result = _blocked_result()

            first = notify_if_required(
                result, transport=transport, output_root=Path(td)
            )
            second = notify_if_required(
                result, transport=transport, output_root=Path(td)
            )

            self.assertEqual(first["status"], "UNKNOWN")
            self.assertEqual(second["status"], "ALREADY_UNKNOWN")
            self.assertEqual(transport.calls, 1)

    def test_notification_record_persists_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _blocked_result()

            outcome = notify_if_required(
                result, transport=RecordingTransport(), output_root=root
            )

            record_path = (
                root
                / result["workflow_id"]
                / f"{outcome['record']['failure_identity']}.json".replace(":", "_")
            )
            self.assertTrue(record_path.is_file())

    def test_healthy_run_creates_no_notification_state_and_no_send(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcome = notify_if_required(
                _succeeded_result(),
                transport=NeverCallTransport(),
                output_root=root,
            )
            self.assertEqual(outcome["status"], "NOT_REQUIRED")
            self.assertFalse(list(root.rglob("*.json")))

    def test_recovery_after_prior_failure_is_quiet(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transport = RecordingTransport()

            failed = _blocked_result(occurrence_id="2026-09-05T03:20:00+04:00")
            notify_if_required(failed, transport=transport, output_root=root)
            self.assertEqual(len(transport.messages), 1)

            healthy = _succeeded_result(occurrence_id="2026-09-06T03:20:00+04:00")
            outcome = notify_if_required(
                healthy, transport=transport, output_root=root
            )

            self.assertEqual(outcome["status"], "NOT_REQUIRED")
            self.assertEqual(len(transport.messages), 1)

    def test_concurrent_same_failure_sends_at_most_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _blocked_result()
            transport = GatedTransport()

            first_thread = threading.Thread(
                target=notify_if_required,
                kwargs={
                    "result": result,
                    "transport": transport,
                    "output_root": root,
                },
            )
            first_thread.start()
            transport.wait_until_entered()

            second_result = {"outcome": None}

            def _second_call():
                second_result["outcome"] = notify_if_required(
                    result, transport=transport, output_root=root
                )

            second_thread = threading.Thread(target=_second_call)
            second_thread.start()

            transport.release()
            first_thread.join(timeout=5)
            second_thread.join(timeout=5)

            self.assertEqual(transport.calls, 1)
            self.assertEqual(second_result["outcome"]["status"], "ALREADY_SENT")

    def test_capability_negative_no_publication_mutation_paths(self):
        for filename in (
            "nullone_failure_notify.py",
            "nullone-failure-notify-run.py",
        ):
            source = (SCRIPTS / filename).read_text(encoding="utf-8").lower()
            self.assertNotIn("publish", source)
            self.assertNotIn("zernio", source)
            self.assertNotIn("draft-bridge", source)
            self.assertNotIn("posts_publish_now", source)

    def test_invalid_result_structure_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(Exception):
                notify_if_required(
                    {"not": "a valid result"},
                    transport=NeverCallTransport(),
                    output_root=Path(td),
                )


class SchedulerOwnershipRoutingTests(unittest.TestCase):
    """A true scheduler-level execution failure (scheduler_status
    error/failed) belongs to OpenClaw's own scheduler-native
    failureAlert, not this custom domain notifier — see
    docs/deployment/37-preflight-notification-requirements.md. This
    is a routing rule only: domain_outcome remains the sole source of
    business-health truth, so these tests also prove the routing is
    genuinely conditional on scheduler_status, not a blanket
    suppression of FAILED."""

    def test_scheduler_error_status_defers_to_native_alert(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _blocked_result(
                domain_outcome="FAILED",
                reason_code="EDITORIAL_PROVIDER_ERROR",
                reason_text="Editorial provider call failed.",
                workflow_id="morning-editorial",
                scheduler_status="error",
            )

            outcome = notify_if_required(
                result, transport=NeverCallTransport(), output_root=root
            )

            self.assertEqual(outcome["status"], "NOT_REQUIRED")
            self.assertEqual(outcome.get("policy"), NOTIFICATION_DEFERRED_POLICY)
            self.assertFalse(list(root.rglob("*")))

    def test_scheduler_failed_status_defers_to_native_alert(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _blocked_result(
                domain_outcome="FAILED",
                reason_code="EDITORIAL_PROVIDER_ERROR",
                reason_text="Editorial provider call failed.",
                workflow_id="morning-editorial",
                scheduler_status="failed",
            )

            outcome = notify_if_required(
                result, transport=NeverCallTransport(), output_root=root
            )

            self.assertEqual(outcome["status"], "NOT_REQUIRED")
            self.assertEqual(outcome.get("policy"), NOTIFICATION_DEFERRED_POLICY)
            self.assertFalse(list(root.rglob("*")))

    def test_scheduler_status_check_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for status in ("ERROR", "Error", "FAILED", "Failed"):
                result = _blocked_result(
                    domain_outcome="FAILED",
                    scheduler_status=status,
                )
                outcome = notify_if_required(
                    result, transport=NeverCallTransport(), output_root=root
                )
                self.assertEqual(outcome["status"], "NOT_REQUIRED")
                self.assertEqual(
                    outcome.get("policy"), NOTIFICATION_DEFERRED_POLICY
                )

    def test_scheduler_succeeded_with_failed_domain_still_sends(self):
        # Contrast case: the routing rule is conditional on
        # scheduler_status, not a blanket suppression of FAILED — the
        # confirmed Daily Analytics shape (scheduler ok/succeeded,
        # domain FAILED) must still be alerted exactly once.
        with tempfile.TemporaryDirectory() as td:
            transport = RecordingTransport()
            result = _blocked_result(
                domain_outcome="FAILED",
                reason_code="ANALYTICS_RESPONSE_INVALID",
                reason_text="Zernio analytics response was malformed.",
                scheduler_status="succeeded",
            )

            outcome = notify_if_required(
                result, transport=transport, output_root=Path(td)
            )

            self.assertEqual(outcome["status"], "SENT")
            self.assertEqual(len(transport.messages), 1)

    def test_scheduler_succeeded_blocked_still_sends_exactly_once(self):
        with tempfile.TemporaryDirectory() as td:
            transport = RecordingTransport()
            result = _blocked_result(
                domain_outcome="BLOCKED", scheduler_status="succeeded"
            )

            first = notify_if_required(
                result, transport=transport, output_root=Path(td)
            )
            second = notify_if_required(
                result, transport=transport, output_root=Path(td)
            )

            self.assertEqual(first["status"], "SENT")
            self.assertEqual(second["status"], "ALREADY_SENT")
            self.assertEqual(len(transport.messages), 1)

    def test_native_deferral_creates_no_notification_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _blocked_result(
                domain_outcome="FAILED",
                workflow_id="morning-editorial",
                scheduler_status="error",
            )

            notify_if_required(
                result, transport=NeverCallTransport(), output_root=root
            )

            self.assertFalse(list(root.rglob("*")))

    def test_deferral_does_not_block_a_later_genuine_failure(self):
        # Deferring for one failure identity must not create state that
        # could accidentally suppress a later, different failure that
        # genuinely needs this module's attention.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transport = RecordingTransport()

            deferred = _blocked_result(
                domain_outcome="FAILED",
                workflow_id="morning-editorial",
                scheduler_status="error",
                occurrence_id="2026-09-05T08:30:00+04:00",
            )
            notify_if_required(
                deferred, transport=transport, output_root=root
            )
            self.assertEqual(len(transport.messages), 0)

            genuine = _blocked_result(
                domain_outcome="FAILED",
                workflow_id="morning-editorial",
                scheduler_status="succeeded",
                occurrence_id="2026-09-06T08:30:00+04:00",
                reason_code="EDITORIAL_ARTIFACT_MISSING",
                reason_text="Required editorial board artifact is missing.",
            )
            outcome = notify_if_required(
                genuine, transport=transport, output_root=root
            )

            self.assertEqual(outcome["status"], "SENT")
            self.assertEqual(len(transport.messages), 1)


class PathContainmentTests(unittest.TestCase):
    def test_traversal_workflow_id_is_rejected_before_transport(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _blocked_result(workflow_id="../../outside")

            with self.assertRaises(NotifierError):
                notify_if_required(
                    result, transport=NeverCallTransport(), output_root=root
                )

            # Nothing must exist outside (or even inside) the configured
            # temp notification root as a result of the rejected call.
            self.assertFalse(list(root.rglob("*")))
            escaped = root.parent / "outside"
            self.assertFalse(escaped.exists())

    def test_absolute_path_workflow_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _blocked_result(workflow_id="/etc/passwd-style-id")

            with self.assertRaises(NotifierError):
                notify_if_required(
                    result, transport=NeverCallTransport(), output_root=root
                )

            self.assertFalse(list(root.rglob("*")))

    def test_dotdot_embedded_workflow_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _blocked_result(workflow_id="daily-analytics/../../etc")

            with self.assertRaises(NotifierError):
                notify_if_required(
                    result, transport=NeverCallTransport(), output_root=root
                )

    def test_normal_workflow_ids_still_work(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transport = RecordingTransport()

            for workflow_id in ("morning-editorial", "daily-analytics"):
                result = _blocked_result(
                    workflow_id=workflow_id,
                    reason_code="EXAMPLE_DEPENDENCY_UNAVAILABLE",
                )
                outcome = notify_if_required(
                    result, transport=transport, output_root=root
                )
                self.assertEqual(outcome["status"], "SENT")

            self.assertEqual(len(transport.messages), 2)
            self.assertTrue((root / "morning-editorial").is_dir())
            self.assertTrue((root / "daily-analytics").is_dir())


class RunnerCliTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(runner.self_test(), 0)

    def test_notify_reads_result_file_and_reports_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result_file = root / "result.json"
            result = _blocked_result()

            import json

            result_file.write_text(json.dumps(result), encoding="utf-8")

            outcome = runner.notify(
                str(result_file),
                str(root / "notifications"),
            )
            self.assertEqual(outcome, 0)

    def test_notify_defaults_to_real_notification_root_when_unset(self):
        # The production default path must exist as a constant even
        # though this test never actually writes there (no owner-id
        # file exists in this checkout, so the transport fails closed
        # before any filesystem write to that default happens) — this
        # only proves the wiring, not a live send.
        self.assertTrue(str(runner.NOTIFICATION_ROOT).endswith(
            "social/ops/notifications"
        ))


class PreflightDocumentationTests(unittest.TestCase):
    """No Git-tracked OpenClaw scheduler failureAlert configuration path
    exists in this repository (confirmed by inspection: no automation/
    cron config files are tracked). Per #30's scope, that activation is
    therefore documented as an explicit #37 deployment-time requirement
    rather than guessed at here. This offline check validates that the
    requirement is actually written down, not that a schema exists."""

    def test_scheduler_failure_alert_requirement_is_documented(self):
        doc = (
            ROOT
            / "docs/deployment/37-preflight-notification-requirements.md"
        )
        self.assertTrue(doc.is_file(), f"missing: {doc}")

        text = doc.read_text(encoding="utf-8")
        for phrase in (
            "failureAlert",
            "morning-editorial",
            "daily-analytics",
            "#37",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
