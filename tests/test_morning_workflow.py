#!/usr/bin/env python3
"""Composition tests for MorningWorkflow (#59).

Proves MorningWorkflow's own reload/reconciliation/notification-composition
logic, the Baku date-boundary derivation, and the scheduler-vs-domain
separation. Deliberately does not duplicate #28's own exhaustive retry/
lock/artifact test suite (`tests/test_morning_editorial.py`) -- most cases
here inject a fake `run_editorial` to exercise this module's own logic in
isolation. `nullone_morning_workflow.py`'s own embedded `self_test()`
(invoked via `python3 ... self-test` in `tests/run_offline.py`) already
covers the full end-to-end scenario matrix from #59's acceptance criteria;
this file adds `unittest`-style assertions for a few properties that matter
enough to pin down with explicit, named tests.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_morning_workflow import (  # noqa: E402
    MorningWorkflowResult,
    run_morning_workflow,
)
from support.morning_artifacts import write_morning_artifacts  # noqa: E402
from nullone_run_outcome import assess_run, emit_result_once, make_run_id, result_path  # noqa: E402
from nullone_scheduler_invocation import compute_occurrence_id  # noqa: E402


def make_trigger(**overrides):
    base = {
        "schema": "nullone.scheduler-invocation.v1",
        "contract_version": "1.0.0",
        "workflow_id": "morning-editorial",
        "source": "openclaw",
        "external_occurrence_id": "test-occ-morning",
        "scheduled_for": "2026-09-08T04:30:00Z",
        "triggered_at": "2026-09-08T04:30:02Z",
    }
    base.update(overrides)
    base["occurrence_id"] = compute_occurrence_id(
        base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
    )
    return base


class BakuDateDerivationTests(unittest.TestCase):
    def test_board_date_uses_baku_not_occurrence_id_prefix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trigger = make_trigger(scheduled_for="2026-09-08T21:30:00Z")

            def succeed():
                write_morning_artifacts(root / "artifacts", "2026-09-09")

            result = run_morning_workflow(
                trigger,
                invoke_provider=succeed,
                artifact_root=root / "artifacts",
                output_root=root / "run-outcomes",
                sleep=lambda _s: None,
            )
            self.assertEqual(result.application_execution, "COMPLETED")
            # UTC calendar date is 2026-09-08; Baku (+04:00) is 2026-09-09.
            self.assertEqual(result.board_date, "2026-09-09")
            # occurrence_id is an opaque occ_<hex> string -- confirm the
            # board date was NOT derived by slicing it.
            self.assertFalse(trigger["occurrence_id"].startswith("2026-09-09"))


class ExactPersistedResultTests(unittest.TestCase):
    def test_loads_exact_persisted_file_not_only_in_memory_return(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trigger = make_trigger(external_occurrence_id="test-occ-morning-exact")
            output_root = root / "run-outcomes"
            artifact_root = root / "artifacts"

            def succeed():
                write_morning_artifacts(artifact_root, "2026-09-08")

            result = run_morning_workflow(
                trigger,
                invoke_provider=succeed,
                artifact_root=artifact_root,
                output_root=output_root,
                sleep=lambda _s: None,
            )
            self.assertIsNotNone(result.result_file)
            on_disk = json.loads(Path(result.result_file).read_text(encoding="utf-8"))
            self.assertEqual(on_disk["run_id"], result.run_id)
            self.assertEqual(on_disk["occurrence_id"], trigger["occurrence_id"])
            self.assertEqual(on_disk["workflow_id"], "morning-editorial")

    def test_result_type_is_typed_dataclass(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = run_morning_workflow(
                {"workflow_id": "morning-editorial"},
                invoke_provider=lambda: None,
                artifact_root=root / "artifacts",
                output_root=root / "run-outcomes",
            )
            self.assertIsInstance(result, MorningWorkflowResult)
            self.assertEqual(result.application_execution, "FAILED")


class ReplayNeverRepeatsProviderSideEffectsTests(unittest.TestCase):
    def test_replay_reuses_same_run_id_and_does_not_recall_provider(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact_root = root / "artifacts"
            output_root = root / "run-outcomes"
            trigger = make_trigger(external_occurrence_id="test-occ-morning-replay")
            calls = []

            def succeed():
                calls.append(1)
                write_morning_artifacts(artifact_root, "2026-09-08")

            first = run_morning_workflow(
                trigger, invoke_provider=succeed, artifact_root=artifact_root,
                output_root=output_root, sleep=lambda _s: None,
            )
            second = run_morning_workflow(
                trigger, invoke_provider=succeed, artifact_root=artifact_root,
                output_root=output_root, sleep=lambda _s: None,
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(first.run_id, second.run_id)
            self.assertEqual(first.result_file, second.result_file)


class DifferentOccurrenceIndependenceTests(unittest.TestCase):
    def test_distinct_occurrences_get_distinct_run_ids_and_execute_independently(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact_root = root / "artifacts"
            output_root = root / "run-outcomes"

            trigger_a = make_trigger(external_occurrence_id="test-occ-morning-a")
            trigger_b = make_trigger(external_occurrence_id="test-occ-morning-b")
            self.assertNotEqual(trigger_a["occurrence_id"], trigger_b["occurrence_id"])

            calls = []

            def succeed():
                calls.append(1)
                board, _handoff = write_morning_artifacts(artifact_root, "2026-09-08")
                # Each occurrence's board write is independent; simulate by
                # always (re)writing distinct content per call count.
                board.write_text(f"# board {len(calls)}\n", encoding="utf-8")

            result_a = run_morning_workflow(
                trigger_a, invoke_provider=succeed, artifact_root=artifact_root,
                output_root=output_root, sleep=lambda _s: None,
            )
            result_b = run_morning_workflow(
                trigger_b, invoke_provider=succeed, artifact_root=artifact_root,
                output_root=output_root, sleep=lambda _s: None,
            )
            self.assertNotEqual(result_a.run_id, result_b.run_id)
            self.assertNotEqual(result_a.occurrence_id, result_b.occurrence_id)


class ScheduledForInvalidTests(unittest.TestCase):
    def test_non_canonical_scheduled_for_is_rejected_by_trigger_validation(self):
        # The scheduler-invocation contract itself already rejects a
        # non-canonical scheduled_for at accept_workflow_trigger -- proving
        # this fails closed as TRIGGER_REJECTED before board-date derivation
        # is ever attempted.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trigger = make_trigger()
            trigger["scheduled_for"] = "not-a-timestamp"
            # occurrence_id would now also mismatch the recomputed value,
            # so this fails TRIGGER_REJECTED regardless of which check fires
            # first.
            result = run_morning_workflow(
                trigger,
                invoke_provider=lambda: (_ for _ in ()).throw(AssertionError("must not run")),
                artifact_root=root / "artifacts",
                output_root=root / "run-outcomes",
            )
            self.assertEqual(result.application_execution, "FAILED")
            self.assertEqual(result.reason_code, "TRIGGER_REJECTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
