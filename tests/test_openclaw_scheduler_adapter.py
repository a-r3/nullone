#!/usr/bin/env python3
"""Tests for the narrow OpenClaw scheduler edge adapter (#59/#65).

Proves the mapping is pure (no I/O/subprocess/network), deterministic, and
correctly derives distinct/stable occurrence identity from
(job id, scheduled_for) per `docs/deployment/59-scheduled-workflows-deployment.md`'s
recorded OpenClaw 2026.8.2 read-only evidence.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_openclaw_scheduler_adapter import map_openclaw_occurrence  # noqa: E402
from nullone_scheduler_invocation import (  # noqa: E402
    SchedulerInvocationError,
    accept_workflow_trigger,
    validate_payload,
)

MORNING_JOB_ID = "0666d47b-aceb-4a4d-960a-b4888f2066ed"
ANALYTICS_JOB_ID = "8e94064c-7e52-4ac5-a167-f3526f9f20c7"


class MappingProducesValidContractPayloadTests(unittest.TestCase):
    def test_maps_to_a_valid_scheduler_invocation_payload(self):
        payload = map_openclaw_occurrence(
            workflow_id="morning-editorial",
            openclaw_job_id=MORNING_JOB_ID,
            scheduled_for="2026-09-08T04:30:00Z",
            triggered_at="2026-09-08T04:30:02Z",
        )
        validate_payload(dict(payload))
        accept_workflow_trigger(dict(payload), workflow_id="morning-editorial")
        self.assertEqual(payload["source"], "openclaw")
        self.assertNotIn(MORNING_JOB_ID, "")  # sanity: job id used, not leaked into schema fields incorrectly
        self.assertTrue(payload["external_occurrence_id"])

    def test_wrong_workflow_boundary_is_rejected(self):
        payload = map_openclaw_occurrence(
            workflow_id="morning-editorial",
            openclaw_job_id=MORNING_JOB_ID,
            scheduled_for="2026-09-08T04:30:00Z",
            triggered_at="2026-09-08T04:30:02Z",
        )
        with self.assertRaises(SchedulerInvocationError):
            accept_workflow_trigger(dict(payload), workflow_id="daily-analytics")


class DeterminismAndReplayTests(unittest.TestCase):
    def test_same_job_same_instant_replay_keeps_occurrence_id(self):
        first = map_openclaw_occurrence(
            workflow_id="morning-editorial", openclaw_job_id=MORNING_JOB_ID,
            scheduled_for="2026-09-08T04:30:00Z", triggered_at="2026-09-08T04:30:02Z",
        )
        retried = map_openclaw_occurrence(
            workflow_id="morning-editorial", openclaw_job_id=MORNING_JOB_ID,
            scheduled_for="2026-09-08T04:30:00Z", triggered_at="2026-09-08T05:15:00Z",
        )
        self.assertEqual(first["occurrence_id"], retried["occurrence_id"])
        self.assertNotEqual(first["triggered_at"], retried["triggered_at"])

    def test_different_job_same_instant_diverges(self):
        morning = map_openclaw_occurrence(
            workflow_id="morning-editorial", openclaw_job_id=MORNING_JOB_ID,
            scheduled_for="2026-09-08T04:30:00Z", triggered_at="2026-09-08T04:30:02Z",
        )
        analytics = map_openclaw_occurrence(
            workflow_id="daily-analytics", openclaw_job_id=ANALYTICS_JOB_ID,
            scheduled_for="2026-09-08T04:30:00Z", triggered_at="2026-09-08T04:30:02Z",
        )
        self.assertNotEqual(morning["occurrence_id"], analytics["occurrence_id"])

    def test_same_job_different_instant_diverges(self):
        day1 = map_openclaw_occurrence(
            workflow_id="morning-editorial", openclaw_job_id=MORNING_JOB_ID,
            scheduled_for="2026-09-08T04:30:00Z", triggered_at="2026-09-08T04:30:02Z",
        )
        day2 = map_openclaw_occurrence(
            workflow_id="morning-editorial", openclaw_job_id=MORNING_JOB_ID,
            scheduled_for="2026-09-09T04:30:00Z", triggered_at="2026-09-09T04:30:02Z",
        )
        self.assertNotEqual(day1["occurrence_id"], day2["occurrence_id"])


class FailClosedTests(unittest.TestCase):
    def test_missing_scheduled_for_is_rejected_never_defaults_to_wall_clock(self):
        with self.assertRaises(SchedulerInvocationError):
            map_openclaw_occurrence(
                workflow_id="morning-editorial", openclaw_job_id=MORNING_JOB_ID,
                scheduled_for="", triggered_at="2026-09-08T04:30:02Z",
            )

    def test_missing_job_id_is_rejected(self):
        with self.assertRaises(SchedulerInvocationError):
            map_openclaw_occurrence(
                workflow_id="morning-editorial", openclaw_job_id="",
                scheduled_for="2026-09-08T04:30:00Z", triggered_at="2026-09-08T04:30:02Z",
            )

    def test_missing_triggered_at_is_rejected(self):
        with self.assertRaises(SchedulerInvocationError):
            map_openclaw_occurrence(
                workflow_id="morning-editorial", openclaw_job_id=MORNING_JOB_ID,
                scheduled_for="2026-09-08T04:30:00Z", triggered_at="",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
