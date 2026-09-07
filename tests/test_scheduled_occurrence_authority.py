#!/usr/bin/env python3
"""Tests for the NullOne Scheduled Occurrence Authority (#59 remaining scope).

Covers the full acceptance matrix from the #59 remaining-scope goal
(section 32): boundary DUE/NO_DUE, same-day replay identity, no-backfill
across a local-date boundary, the Daily Analytics UTC/Baku boundary,
cross-source identity divergence, and fail-closed malformed input.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_scheduled_occurrence_authority import (  # noqa: E402
    ScheduledOccurrenceAuthorityError,
    resolve_scheduled_occurrence,
)
from nullone_scheduler_invocation import validate_payload  # noqa: E402


class MorningBoundaryTests(unittest.TestCase):
    def test_08_29_59_is_no_due(self):
        r = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:29:59Z"
        )
        self.assertEqual(r.status, "NO_DUE_OCCURRENCE")
        self.assertIsNone(r.scheduler_invocation)
        self.assertEqual(r.next_local_scheduled_instant, "2026-09-08T04:30:00Z")

    def test_08_30_00_is_due(self):
        r = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:30:00Z"
        )
        self.assertEqual(r.status, "DUE")
        self.assertEqual(r.scheduled_for, "2026-09-08T04:30:00Z")
        validate_payload(dict(r.scheduler_invocation))

    def test_09_07_matches_08_30_occurrence(self):
        due = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:30:00Z"
        )
        delayed = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T05:07:00Z"
        )
        self.assertEqual(delayed.status, "DUE")
        self.assertEqual(delayed.scheduled_for, due.scheduled_for)
        self.assertEqual(
            delayed.scheduler_invocation["occurrence_id"], due.scheduler_invocation["occurrence_id"]
        )
        self.assertEqual(
            delayed.scheduler_invocation["external_occurrence_id"],
            due.scheduler_invocation["external_occurrence_id"],
        )
        self.assertNotEqual(delayed.triggered_at, due.triggered_at)

    def test_manual_same_day_18_00_matches_occurrence(self):
        due = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:30:00Z"
        )
        manual = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T14:00:00Z"
        )
        self.assertEqual(manual.status, "DUE")
        self.assertEqual(
            manual.scheduler_invocation["occurrence_id"], due.scheduler_invocation["occurrence_id"]
        )


class NoBackfillAcrossLocalDateTests(unittest.TestCase):
    def test_next_day_07_00_is_no_due_not_yesterday_backfill(self):
        r = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-09T03:00:00Z"
        )
        self.assertEqual(r.status, "NO_DUE_OCCURRENCE")

    def test_next_day_08_30_is_a_new_distinct_occurrence(self):
        day1 = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:30:00Z"
        )
        day2 = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-09T04:30:00Z"
        )
        self.assertEqual(day2.status, "DUE")
        self.assertNotEqual(
            day2.scheduler_invocation["occurrence_id"], day1.scheduler_invocation["occurrence_id"]
        )
        self.assertNotEqual(day2.scheduled_for, day1.scheduled_for)

    def test_repeated_early_wake_remains_no_op(self):
        for triggered_at in ("2026-09-09T00:00:00Z", "2026-09-09T02:00:00Z", "2026-09-09T04:29:59Z"):
            r = resolve_scheduled_occurrence(
                workflow_id="morning-editorial", source="openclaw", triggered_at=triggered_at
            )
            self.assertEqual(r.status, "NO_DUE_OCCURRENCE", triggered_at)


class DailyAnalyticsUtcBakuBoundaryTests(unittest.TestCase):
    """Asia/Baku is UTC+4 year-round (no DST); 03:20 local crosses the UTC
    calendar date backwards."""

    def test_boundary_instant_is_due_on_previous_utc_date(self):
        r = resolve_scheduled_occurrence(
            workflow_id="daily-analytics", source="openclaw", triggered_at="2026-09-08T23:20:00Z"
        )
        self.assertEqual(r.status, "DUE")
        self.assertEqual(r.scheduled_for, "2026-09-08T23:20:00Z")
        self.assertEqual(r.local_scheduled_date, "2026-09-09")

    def test_one_second_before_boundary_is_no_due(self):
        r = resolve_scheduled_occurrence(
            workflow_id="daily-analytics", source="openclaw", triggered_at="2026-09-08T23:19:59Z"
        )
        self.assertEqual(r.status, "NO_DUE_OCCURRENCE")


class IdentityDerivationTests(unittest.TestCase):
    def test_same_slot_different_triggered_at_same_occurrence(self):
        a = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:30:00Z"
        )
        b = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T09:00:00Z"
        )
        self.assertEqual(a.scheduler_invocation["occurrence_id"], b.scheduler_invocation["occurrence_id"])

    def test_different_schedule_date_different_occurrence(self):
        a = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:30:00Z"
        )
        b = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-09T04:30:00Z"
        )
        self.assertNotEqual(a.scheduler_invocation["occurrence_id"], b.scheduler_invocation["occurrence_id"])

    def test_openclaw_vs_alternate_source_same_scheduled_for_different_occurrence_id(self):
        openclaw = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T04:30:00Z"
        )
        systemd = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="systemd-timer", triggered_at="2026-09-08T04:30:00Z"
        )
        self.assertEqual(openclaw.status, "DUE")
        self.assertEqual(systemd.status, "DUE")
        self.assertEqual(openclaw.scheduled_for, systemd.scheduled_for)
        self.assertNotEqual(
            openclaw.scheduler_invocation["occurrence_id"], systemd.scheduler_invocation["occurrence_id"]
        )

    def test_external_occurrence_id_never_contains_triggered_at(self):
        due = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T05:07:00Z"
        )
        self.assertNotIn(due.triggered_at, due.scheduler_invocation["external_occurrence_id"])
        self.assertIn("morning-editorial.daily.v1", due.scheduler_invocation["external_occurrence_id"])


class ConcurrentReplayIsPurelyDeterministicTests(unittest.TestCase):
    """This module has no lock/state of its own -- proves repeated calls
    (simulating concurrent wake-ups) are simply pure-function-deterministic,
    with no ledger required at this layer."""

    def test_many_calls_same_slot_all_agree(self):
        results = [
            resolve_scheduled_occurrence(
                workflow_id="daily-analytics", source="openclaw", triggered_at="2026-09-08T23:20:00Z"
            )
            for _ in range(10)
        ]
        occurrence_ids = {r.scheduler_invocation["occurrence_id"] for r in results}
        self.assertEqual(len(occurrence_ids), 1)


class FailClosedTests(unittest.TestCase):
    def test_malformed_inputs_raise(self):
        bad_cases = [
            {"workflow_id": "", "source": "openclaw", "triggered_at": "2026-09-08T04:30:00Z"},
            {"workflow_id": "morning-editorial", "source": "", "triggered_at": "2026-09-08T04:30:00Z"},
            {"workflow_id": "morning-editorial", "source": "openclaw", "triggered_at": ""},
            {"workflow_id": "morning-editorial", "source": "openclaw", "triggered_at": "not-a-timestamp"},
            {"workflow_id": "morning-editorial", "source": "openclaw", "triggered_at": "2026-09-08 04:30:00"},
            {"workflow_id": "story", "source": "openclaw", "triggered_at": "2026-09-08T04:30:00Z"},
            {"workflow_id": "breaking", "source": "openclaw", "triggered_at": "2026-09-08T04:30:00Z"},
            {"workflow_id": "unknown-workflow", "source": "openclaw", "triggered_at": "2026-09-08T04:30:00Z"},
        ]
        for kwargs in bad_cases:
            with self.assertRaises(ScheduledOccurrenceAuthorityError, msg=kwargs):
                resolve_scheduled_occurrence(**kwargs)

    def test_clock_value_never_becomes_scheduled_for_when_late(self):
        r = resolve_scheduled_occurrence(
            workflow_id="morning-editorial", source="openclaw", triggered_at="2026-09-08T05:07:00Z"
        )
        self.assertNotEqual(r.triggered_at, r.scheduled_for)


if __name__ == "__main__":
    unittest.main(verbosity=2)
