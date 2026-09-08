#!/usr/bin/env python3
"""Tests for the M0 NullOne-owned schedule registry (#59 remaining scope).

Proves exactly the two reviewed/live schedule slots exist with the exact
documented values, that `ScheduleSpec` validates its own fields (supported
workflow, unique schedule_id, valid IANA timezone, exact HH:MM:SS), and
that this is config, not a mutable/generic scheduler.
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import time
from pathlib import Path

_TRIPLE_QUOTED_RE = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)


def _code_only(source: str) -> str:
    """Strip docstrings so a capability check scans executable code only,
    not this module's own prose describing the boundary it upholds."""

    return _TRIPLE_QUOTED_RE.sub("", source)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_schedule_registry import (  # noqa: E402
    ScheduleRegistryError,
    ScheduleSpec,
    get_schedule,
)


class ExactM0RegistryValuesTests(unittest.TestCase):
    def test_morning_editorial_exact_values(self):
        spec = get_schedule("morning-editorial")
        self.assertEqual(spec.workflow_id, "morning-editorial")
        self.assertEqual(spec.schedule_id, "morning-editorial.daily.v1")
        self.assertEqual(spec.timezone_name, "Asia/Baku")
        self.assertEqual(spec.local_time, "08:30:00")
        self.assertEqual(spec.local_time_of_day(), time(8, 30, 0))

    def test_daily_analytics_exact_values(self):
        spec = get_schedule("daily-analytics")
        self.assertEqual(spec.workflow_id, "daily-analytics")
        self.assertEqual(spec.schedule_id, "daily-analytics.daily.v1")
        self.assertEqual(spec.timezone_name, "Asia/Baku")
        self.assertEqual(spec.local_time, "03:20:00")

    def test_unsupported_workflow_is_rejected(self):
        for workflow_id in ("story", "breaking", "does-not-exist", ""):
            with self.assertRaises(ScheduleRegistryError):
                get_schedule(workflow_id)


class ScheduleSpecValidationTests(unittest.TestCase):
    def _valid_kwargs(self, **overrides):
        base = dict(
            workflow_id="morning-editorial",
            schedule_id="morning-editorial.daily.v1",
            timezone_name="Asia/Baku",
            local_time="08:30:00",
        )
        base.update(overrides)
        return base

    def test_unsupported_workflow_rejected_at_construction(self):
        with self.assertRaises(ScheduleRegistryError):
            ScheduleSpec(**self._valid_kwargs(workflow_id="breaking"))

    def test_story_workflow_accepted_at_construction(self):
        spec = ScheduleSpec(**self._valid_kwargs(workflow_id="story"))
        self.assertEqual(spec.workflow_id, "story")

    def test_invalid_iana_timezone_rejected(self):
        with self.assertRaises(ScheduleRegistryError):
            ScheduleSpec(**self._valid_kwargs(timezone_name="Definitely/NotAZone"))

    def test_malformed_local_time_rejected(self):
        for bad in ("8:30:00", "08:30", "08:30:00Z", "25:00:00", "08:60:00", "08:30:60", ""):
            with self.assertRaises(ScheduleRegistryError):
                ScheduleSpec(**self._valid_kwargs(local_time=bad))

    def test_malformed_schedule_id_rejected(self):
        for bad in ("", "no dots here", "morning-editorial.daily", "morning-editorial.daily.1"):
            with self.assertRaises(ScheduleRegistryError):
                ScheduleSpec(**self._valid_kwargs(schedule_id=bad))

    def test_valid_v2_revision_id_is_accepted(self):
        spec = ScheduleSpec(**self._valid_kwargs(schedule_id="morning-editorial.daily.v2"))
        self.assertEqual(spec.schedule_id, "morning-editorial.daily.v2")


class NoGenericSchedulingCapabilityTests(unittest.TestCase):
    """Static proof this is a narrow, reviewed registry -- not a cron
    engine or a user-configurable schedule store."""

    def test_no_cron_expression_parsing_library(self):
        source = _code_only((SCRIPTS / "nullone_schedule_registry.py").read_text(encoding="utf-8"))
        for forbidden in ("croniter", "crontab", "APScheduler", "import sched"):
            self.assertNotIn(forbidden, source)

    def test_no_persistence_or_io(self):
        source = _code_only((SCRIPTS / "nullone_schedule_registry.py").read_text(encoding="utf-8"))
        for forbidden in ("sqlite3", "psycopg2", "open(", "subprocess", "requests.", "urllib"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
