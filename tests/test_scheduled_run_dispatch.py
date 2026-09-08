#!/usr/bin/env python3
"""Tests for the shared `nullone_scheduled_run_dispatch.py` wiring (#59
remaining scope, section 16).

Proves `nullone-scheduled-run.py` (exact-trigger-file path) and
`nullone-scheduled-wakeup.py` (wake-up path) go through the exact same
`run_morning_trigger`/`run_analytics_trigger` production wiring, without
this test itself ever reaching the real Claude CLI / Zernio / Telegram
adapters: an invalid trigger is rejected by
`nullone_scheduler_invocation.accept_workflow_trigger` before either the
Claude CLI provider or the AnalyticsProvider factory would ever be
invoked, so exercising the real `run_morning_trigger`/`run_analytics_trigger`
functions with a deliberately-rejected trigger stays fully offline.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_scheduled_run_dispatch import (  # noqa: E402
    run_analytics_trigger,
    run_morning_trigger,
    run_story_trigger,
)


class RejectedTriggerNeverReachesProductionAdaptersTests(unittest.TestCase):
    def test_morning_invalid_trigger_is_rejected_offline(self):
        result = run_morning_trigger({"workflow_id": "morning-editorial"})
        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "TRIGGER_REJECTED")

    def test_analytics_invalid_trigger_is_rejected_offline(self):
        result = run_analytics_trigger({"workflow_id": "daily-analytics"})
        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "TRIGGER_REJECTED")

    def test_story_invalid_trigger_is_rejected_offline(self):
        result = run_story_trigger({"workflow_id": "story"})
        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "TRIGGER_REJECTED")

    def test_morning_wrong_workflow_id_boundary_is_rejected(self):
        from nullone_scheduler_invocation import compute_occurrence_id

        trigger = {
            "schema": "nullone.scheduler-invocation.v1",
            "contract_version": "1.0.0",
            "workflow_id": "daily-analytics",
            "source": "openclaw",
            "external_occurrence_id": "dispatch-test-wrong-workflow",
            "scheduled_for": "2026-09-08T04:30:00Z",
            "triggered_at": "2026-09-08T04:30:02Z",
        }
        trigger["occurrence_id"] = compute_occurrence_id(
            trigger["workflow_id"], trigger["source"], trigger["external_occurrence_id"], trigger["scheduled_for"]
        )
        result = run_morning_trigger(trigger)
        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "TRIGGER_REJECTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
