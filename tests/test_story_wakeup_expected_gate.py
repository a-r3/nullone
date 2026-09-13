#!/usr/bin/env python3
"""Regression: a Story expected provenance gate must not poison scheduler health.

Production failure (2026-09-13, job 04043dd3-af94-4a03-b7bd-c75aebce0140):
every Story wakeup with no proven same-day Morning source correctly
refused to consume the handoff (`REASON_CODE=MORNING_SOURCE_UNPROVEN`,
zero provider/draft/notification effects), but the wakeup process exited
1 because `application_execution != "COMPLETED"`. OpenClaw counted each
expected gate as a scheduler failure and auto-disabled the Story job
after 10 consecutive runs (13:30 Asia/Baku), so the 18:30 slot could not
run even if the machine was on.

Contract under test:

- Story + MORNING_SOURCE_UNPROVEN: provider not called, notifier
  behavior unchanged (never invoked), domain outcome truthful
  (FAILED/MORNING_SOURCE_UNPROVEN preserved), scheduler-facing exit 0.
- Repeated expected gates (10x, mirroring the auto-disable window) all
  exit 0 -- they can never become scheduler hard failures.
- Genuine infrastructure failure (rejected source/clock, TRIGGER_REJECTED
  result) still exits nonzero.
- Genuine provider/execution failure (RUNTIME_CRASHED result, Morning
  FAILED result) still exits nonzero -- no blanket FAILED -> 0.
- Story success path and Morning semantics are unchanged (locked once
  end to end here; covered in depth by the existing suites).

All offline: real `run_story_trigger` / real `WAKEUP.wake_up` against the
real occurrence authority, temp roots, exploding/fake doubles only.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
from nullone_editorial_candidate_handoff import (  # noqa: E402
    board_relative_path,
    handoff_relative_path,
)
from nullone_review_delivery import FakeReviewDelivery  # noqa: E402
from nullone_run_outcome import assess_run, emit_result_once, make_run_id  # noqa: E402
from nullone_scheduler_invocation import compute_occurrence_id  # noqa: E402
from nullone_story_pipeline import numeric_scope_verifier  # noqa: E402
from nullone_story_scheduled_workflow import (  # noqa: E402
    EXPECTED_GATE_REASON_CODES,
    StoryScheduledResult,
    is_expected_scheduler_gate,
    run_story_trigger,
)
from support.morning_artifacts import write_morning_artifacts  # noqa: E402

BAKU = ZoneInfo("Asia/Baku")
NOW = datetime(2026, 9, 8, 14, 30, 0, tzinfo=BAKU)  # AFTERNOON, never QUIET

# 18:30 Asia/Baku Story slot (the slot that could not run in production).
STORY_1830_UTC = datetime(2026, 9, 8, 14, 30, 0, tzinfo=timezone.utc)
STORY_SLOTS_UTC = (
    datetime(2026, 9, 8, 6, 30, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 8, 9, 30, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 8, 14, 30, 0, tzinfo=timezone.utc),
)


def _load_wakeup():
    spec = importlib.util.spec_from_file_location(
        "nullone_scheduled_wakeup_gate", SCRIPTS / "nullone-scheduled-wakeup.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load nullone-scheduled-wakeup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WAKEUP = _load_wakeup()


def make_candidate(**overrides):
    base = {
        "candidate_id": "sched-1",
        "rank": 1,
        "topic": "Scheduled topic",
        "topic_cluster": "scheduled",
        "content_type": "NEWS",
        "angle": "Angle",
        "verification": "PASS",
        "evidence_refs": ["Scheduled evidence"],
        "source_attribution": "Scheduled source",
        "editorial_status": "READY",
        "story_eligible": True,
    }
    base.update(overrides)
    return base


def digit_free_writer(_context):
    return {
        "layout": "big-stat",
        "headline": "Scheduled headline",
        "body": "Scheduled body.",
        "stat": "Scheduled stat",
        "source_name": "Scheduled source",
        "use_source_image": False,
        "cta": "@nullone.az",
    }


class FakeDraftConnector:
    def __init__(self) -> None:
        self.calls = 0

    def create_review_draft(self, manifest_path: Path) -> None:
        self.calls += 1
        _, manifest = bridge_common.load_manifest(manifest_path)
        manifest["review"]["create_attempts"] = 1
        manifest["review"]["state"] = "DRAFT_CREATED"
        manifest["review"]["zernio_draft_id"] = "gate-review-1"
        manifest["review"]["created_at"] = bridge_common.now_iso()
        bridge_common.atomic_write_json(manifest_path, manifest)


class Exploding:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"{self.name} must not be called")


def morning_ids_for(date: str, source: str = "openclaw"):
    scheduled_for = f"{date}T04:30:00Z"  # 08:30 Asia/Baku, UTC+4, no DST
    external = f"morning-editorial.daily.v1@{scheduled_for}"
    occurrence_id = compute_occurrence_id(
        "morning-editorial", source, external, scheduled_for
    )
    return scheduled_for, occurrence_id, make_run_id(
        workflow_id="morning-editorial", occurrence_id=occurrence_id
    )


class ExpectedGateSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.out = self.root / "run-outcomes"
        self.morning_out = self.root / "social/ops/run-outcomes/morning-editorial"
        self.original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = self.root
        tools_dir = self.root / "social/tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        real_workspace_root = Path(__file__).resolve().parents[1]
        shutil.copy(
            real_workspace_root / "workspace/social/tools/render_story_v2.py",
            tools_dir / "render_story_v2.py",
        )
        (self.root / "social").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        bridge_common.WORKSPACE = self.original_workspace
        self.td.cleanup()

    def _story_dispatch(self, **overrides):
        provider_calls: list[int] = []
        notifier_calls: list[dict] = []

        def counting_notifier(persisted):
            notifier_calls.append(persisted)
            return {"status": "NOT_REQUIRED"}

        def dispatch(trigger):
            return run_story_trigger(
                trigger,
                writer=Exploding("writer"),
                verifier=Exploding("verifier"),
                draft_connector=Exploding("draft"),
                review_delivery=Exploding("delivery"),
                notifier=counting_notifier,
                workspace_root=self.root,
                output_root=self.out,
                morning_output_root=self.morning_out,
                now=NOW,
                **overrides,
            )

        return dispatch, provider_calls, notifier_calls

    def _wake_story(self, at: datetime, dispatch):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = WAKEUP.wake_up(
                "story",
                source="openclaw",
                now=lambda: at,
                dispatch_by_workflow={"story": dispatch},
            )
        return code, buf.getvalue()

    def test_morning_source_unproven_is_exit_zero_with_zero_effects(self):
        dispatch, _provider_calls, notifier_calls = self._story_dispatch()
        code, out = self._wake_story(STORY_1830_UTC, dispatch)
        # Scheduler health stays green ...
        self.assertEqual(code, 0)
        self.assertIn("STATUS=DUE", out)
        self.assertIn("REASON_CODE=MORNING_SOURCE_UNPROVEN", out)
        # ... with zero production effects: exploding writer/verifier/
        # draft/delivery were never invoked, and the notifier behavior
        # is unchanged (never invoked on this path either).
        self.assertEqual(notifier_calls, [])

    def test_domain_outcome_stays_truthful_failed(self):
        from nullone_scheduler_invocation import compute_occurrence_id as _compute

        trigger = {
            "schema": "nullone.scheduler-invocation.v1",
            "contract_version": "1.0.0",
            "workflow_id": "story",
            "source": "openclaw",
            "external_occurrence_id": "story.check-1830.v1@2026-09-08T14:30:00Z",
            "scheduled_for": "2026-09-08T14:30:00Z",
            "triggered_at": "2026-09-08T14:30:02Z",
        }
        trigger["occurrence_id"] = _compute(
            trigger["workflow_id"],
            trigger["source"],
            trigger["external_occurrence_id"],
            trigger["scheduled_for"],
        )
        result = run_story_trigger(
            trigger,
            writer=Exploding("writer"),
            verifier=Exploding("verifier"),
            draft_connector=Exploding("draft"),
            review_delivery=Exploding("delivery"),
            notifier=Exploding("notifier"),
            workspace_root=self.root,
            output_root=self.out,
            morning_output_root=self.morning_out,
            now=NOW,
        )
        # The domain result itself is unchanged: still a truthful FAILED
        # gate -- only the scheduler-facing exit classification changed.
        self.assertEqual(result.application_execution, "FAILED")
        self.assertIsNone(result.domain_outcome)
        self.assertEqual(result.reason_code, "MORNING_SOURCE_UNPROVEN")
        self.assertIsNone(result.notification_status)
        self.assertIsNone(result.result_file)
        self.assertTrue(is_expected_scheduler_gate(result))

    def test_ten_consecutive_expected_gates_all_exit_zero(self):
        dispatch, _provider_calls, notifier_calls = self._story_dispatch()
        codes = [
            self._wake_story(STORY_SLOTS_UTC[i % len(STORY_SLOTS_UTC)], dispatch)[0]
            for i in range(10)
        ]
        # The exact production auto-disable window: 10 consecutive
        # MORNING_SOURCE_UNPROVEN runs must never read as 10 failures.
        self.assertEqual(codes, [0] * 10)
        self.assertEqual(notifier_calls, [])

    def test_expected_gate_set_is_exactly_morning_source_unproven(self):
        self.assertEqual(EXPECTED_GATE_REASON_CODES, frozenset({"MORNING_SOURCE_UNPROVEN"}))


class HardFailuresStillNonzeroTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.out = self.root / "run-outcomes"
        self.morning_out = self.root / "social/ops/run-outcomes/morning-editorial"
        self.original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = self.root
        (self.root / "social").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        bridge_common.WORKSPACE = self.original_workspace
        self.td.cleanup()

    def _wake(self, *args, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = WAKEUP.wake_up(*args, **kwargs)
        return code, buf.getvalue()

    def test_trigger_rejected_result_stays_nonzero(self):
        result = StoryScheduledResult(
            application_execution="FAILED",
            domain_outcome=None,
            run_id=None,
            occurrence_id="occ",
            result_file=None,
            notification_status=None,
            reason_code="TRIGGER_REJECTED",
            reason_text="Scheduler invocation trigger rejected.",
        )
        self.assertFalse(is_expected_scheduler_gate(result))
        code, _out = self._wake(
            "story",
            source="openclaw",
            now=lambda: STORY_1830_UTC,
            dispatch_by_workflow={"story": lambda _t: result},
        )
        self.assertNotEqual(code, 0)

    def test_runtime_crashed_result_stays_nonzero(self):
        result = StoryScheduledResult(
            application_execution="FAILED",
            domain_outcome=None,
            run_id="run-x",
            occurrence_id="occ-x",
            result_file=None,
            notification_status=None,
            reason_code="RUNTIME_CRASHED",
            reason_text="Story workflow raised before establishing a result.",
        )
        self.assertFalse(is_expected_scheduler_gate(result))
        code, _out = self._wake(
            "story",
            source="openclaw",
            now=lambda: STORY_1830_UTC,
            dispatch_by_workflow={"story": lambda _t: result},
        )
        self.assertNotEqual(code, 0)

    def test_infrastructure_rejections_stay_nonzero(self):
        def exploding_dispatch(_trigger):
            raise AssertionError("dispatch must not run for rejected wakeups")

        fixed = {"story": exploding_dispatch}
        code, out = self._wake("story", source="systemd-timer", now=lambda: STORY_1830_UTC, dispatch_by_workflow=fixed)
        self.assertNotEqual(code, 0)
        self.assertIn("STATUS=WAKEUP_REJECTED", out)
        code, out = self._wake(
            "story",
            source="openclaw",
            now=lambda: datetime(2026, 9, 8, 14, 30, 0),  # naive clock
            dispatch_by_workflow=fixed,
        )
        self.assertNotEqual(code, 0)
        self.assertIn("STATUS=WAKEUP_REJECTED", out)

    def test_morning_failed_result_stays_nonzero(self):
        class _FakeMorningFailed:
            application_execution = "FAILED"
            domain_outcome = None
            run_id = None
            occurrence_id = "occ-m"
            result_file = None
            notification_status = None
            reason_code = "EDITORIAL_PROVIDER_ERROR"
            reason_text = "Simulated Morning failure."

        self.assertFalse(is_expected_scheduler_gate(_FakeMorningFailed()))
        code, _out = self._wake(
            "morning",
            source="openclaw",
            now=lambda: datetime(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc),
            dispatch_by_workflow={"morning-editorial": lambda _t: _FakeMorningFailed()},
        )
        self.assertNotEqual(code, 0)


class SuccessPathsUnchangedTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.out = self.root / "run-outcomes"
        self.morning_out = self.root / "social/ops/run-outcomes/morning-editorial"
        self.original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = self.root
        tools_dir = self.root / "social/tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        real_workspace_root = Path(__file__).resolve().parents[1]
        shutil.copy(
            real_workspace_root / "workspace/social/tools/render_story_v2.py",
            tools_dir / "render_story_v2.py",
        )
        (self.root / "social").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        bridge_common.WORKSPACE = self.original_workspace
        self.td.cleanup()

    def _write_morning_success(self, date, candidates):
        write_morning_artifacts(self.root, date, candidates=tuple(candidates))
        _, occurrence_id, _ = morning_ids_for(date)
        result = assess_run(
            workflow_id="morning-editorial",
            occurrence_id=occurrence_id,
            scheduler_status="succeeded",
            domain_outcome="SUCCEEDED",
            artifact_root=self.root,
            required_artifacts=(
                board_relative_path(date),
                handoff_relative_path(date),
            ),
        )
        self.morning_out.mkdir(parents=True, exist_ok=True)
        emit_result_once(self.morning_out, result, artifact_root=self.root)

    def test_story_success_path_still_completed_and_exit_zero(self):
        self._write_morning_success("2026-09-08", [make_candidate()])
        delivery = FakeReviewDelivery(status="SENT")
        notifier_calls: list[dict] = []

        def notifier(persisted):
            notifier_calls.append(persisted)
            return {"status": "NOT_REQUIRED"}

        connector = FakeDraftConnector()

        def dispatch(trigger):
            return run_story_trigger(
                trigger,
                writer=digit_free_writer,
                verifier=numeric_scope_verifier,
                draft_connector=connector,
                review_delivery=delivery,
                notifier=notifier,
                workspace_root=self.root,
                output_root=self.out,
                morning_output_root=self.morning_out,
                now=NOW,
            )

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = WAKEUP.wake_up(
                "story",
                source="openclaw",
                now=lambda: STORY_1830_UTC,
                dispatch_by_workflow={"story": dispatch},
            )
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("APPLICATION_EXECUTION=COMPLETED", out)
        self.assertIn("DOMAIN_OUTCOME=SUCCEEDED", out)
        self.assertEqual(connector.calls, 1)
        self.assertEqual(len(delivery.sent), 1)
        self.assertEqual(len(notifier_calls), 1)

    def test_morning_success_still_exit_zero(self):
        from nullone_morning_workflow import run_morning_workflow  # noqa: E402
        from support.morning_artifacts import (  # noqa: E402
            write_morning_artifacts as _write,
        )

        artifact_root = self.root / "artifacts"
        output_root = self.root / "morning-outcomes"
        provider_calls: list[int] = []

        def succeed_provider() -> None:
            provider_calls.append(1)
            _write(artifact_root, "2026-09-08")

        def dispatch(trigger):
            return run_morning_workflow(
                trigger,
                invoke_provider=succeed_provider,
                notifier=lambda _r: {"status": "NOT_REQUIRED"},
                artifact_root=artifact_root,
                output_root=output_root,
                sleep=lambda _s: None,
            )

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = WAKEUP.wake_up(
                "morning",
                source="openclaw",
                now=lambda: datetime(2026, 9, 8, 4, 30, 0, tzinfo=timezone.utc),
                dispatch_by_workflow={"morning-editorial": dispatch},
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(provider_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
