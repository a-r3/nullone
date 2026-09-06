#!/usr/bin/env python3
"""Behavioral tests for the #32 deterministic cadence controller.

Unlike tests/test_cadence_contract_fixture.py (shape/hygiene only), this
executes the real decision function, nullone_cadence_controller.
evaluate_cadence(), against the accepted #31 fixture and additional
targeted scenarios for time handling, accounting, backpressure, quality
override, anti-burst spacing, downtime coalescing, idempotence, and the
controller's lack of side-effect capability.
"""
from __future__ import annotations

import copy
import inspect
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_cadence_controller as controller  # noqa: E402
from nullone_cadence_controller import (  # noqa: E402
    CadenceContractError,
    DEFAULT_CONFIG,
    evaluate_cadence,
)

FIXTURE_PATH = ROOT / "tests/fixtures/cadence_contract_v1_examples.json"


def base_request(**overrides):
    request = {
        "schema": "nullone.cadence-contract.v1",
        "now": "2026-09-06T11:15:00+04:00",
        "timezone": "Asia/Baku",
        "config": {},
        "main_load": {
            "published_today": 0,
            "pending": 0,
            "last_published_at": None,
        },
        "story_load": {
            "published_today": 0,
            "pending": 0,
            "last_published_at": None,
        },
        "candidate_availability": {
            "main_quality_candidate_available": True,
            "story_quality_candidate_available": True,
        },
        "signal": {"breaking_day": False, "downtime_marker": None},
    }
    request.update(overrides)
    return request


class FixtureExecutionTests(unittest.TestCase):
    """Run the actual evaluator against all 13 accepted #31 examples."""

    @classmethod
    def setUpClass(cls):
        data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.cases = data["cases"]
        cls.default_config = data["default_config"]

    def test_default_config_matches_accepted_fixture(self):
        self.assertEqual(DEFAULT_CONFIG, self.default_config)

    def test_all_fixture_cases_produce_expected_recommendation(self):
        self.assertGreaterEqual(len(self.cases), 13)

        for case in self.cases:
            with self.subTest(case=case["name"]):
                result = evaluate_cadence(case["input"])
                expected = case["expected_output"]

                self.assertEqual(result["recommendation"], expected["recommendation"])
                self.assertEqual(result["reason_code"], expected["reason_code"])
                self.assertEqual(result["permitted_action"], expected["permitted_action"])
                self.assertEqual(result["daypart"], expected["daypart"])

    def test_downtime_no_quality_case_echoes_marker_in_context(self):
        by_name = {case["name"]: case for case in self.cases}
        case = by_name["downtime_restart_gap_no_quality_candidate"]

        result = evaluate_cadence(case["input"])

        self.assertEqual(result["recommendation"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "NO_QUALITY_CANDIDATE")
        self.assertEqual(
            result["context"]["downtime_marker"],
            case["input"]["signal"]["downtime_marker"],
        )
        self.assertFalse(result["context"]["downtime_coalesced"])


class TimeHandlingTests(unittest.TestCase):
    def test_rejects_naive_now(self):
        request = base_request(now="2026-09-06T11:15:00")
        with self.assertRaises(CadenceContractError):
            evaluate_cadence(request)

    def test_rejects_unparsable_now(self):
        request = base_request(now="not-a-timestamp")
        with self.assertRaises(CadenceContractError):
            evaluate_cadence(request)

    def test_rejects_wrong_timezone_field(self):
        request = base_request(timezone="UTC")
        with self.assertRaises(CadenceContractError):
            evaluate_cadence(request)

    def test_rejects_naive_last_published_at(self):
        request = base_request(
            main_load={
                "published_today": 0,
                "pending": 0,
                "last_published_at": "2026-09-06T09:00:00",
            }
        )
        with self.assertRaises(CadenceContractError):
            evaluate_cadence(request)

    def test_utc_now_is_converted_to_baku_local_daypart(self):
        # 03:00 UTC == 07:00 Asia/Baku (+04:00, no DST) -> MORNING, not QUIET.
        request = base_request(now="2026-09-06T03:00:00+00:00")
        result = evaluate_cadence(request)
        self.assertEqual(result["daypart"], "MORNING")

    def test_daypart_boundaries_are_exclusive_on_the_low_side(self):
        just_before_morning = base_request(now="2026-09-06T06:59:59+04:00")
        at_morning = base_request(now="2026-09-06T07:00:00+04:00")
        just_before_afternoon = base_request(now="2026-09-06T12:59:59+04:00")
        at_afternoon = base_request(now="2026-09-06T13:00:00+04:00")
        just_before_evening = base_request(now="2026-09-06T18:59:59+04:00")
        at_evening = base_request(now="2026-09-06T19:00:00+04:00")

        self.assertEqual(evaluate_cadence(just_before_morning)["daypart"], "QUIET")
        self.assertEqual(evaluate_cadence(at_morning)["daypart"], "MORNING")
        self.assertEqual(evaluate_cadence(just_before_afternoon)["daypart"], "MORNING")
        self.assertEqual(evaluate_cadence(at_afternoon)["daypart"], "AFTERNOON")
        self.assertEqual(evaluate_cadence(just_before_evening)["daypart"], "AFTERNOON")
        self.assertEqual(evaluate_cadence(at_evening)["daypart"], "EVENING")

    def test_asia_baku_midnight_rollover_resets_daypart(self):
        # Just after local midnight: previous day's story publish must not
        # leak into today's daypart/quiet-hours determination.
        request = base_request(
            now="2026-09-07T00:05:00+04:00",
            story_load={
                "published_today": 0,
                "pending": 0,
                "last_published_at": "2026-09-06T23:50:00+04:00",
            },
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["daypart"], "QUIET")
        self.assertEqual(result["reason_code"], "QUIET_HOURS")


class AccountingTests(unittest.TestCase):
    def test_feed_and_carousel_share_main_counter_independent_of_story(self):
        request = base_request(
            now="2026-09-06T20:00:00+04:00",
            main_load={"published_today": 2, "pending": 0, "last_published_at": None},
            story_load={"published_today": 0, "pending": 0, "last_published_at": None},
        )
        result = evaluate_cadence(request)
        self.assertFalse(result["counters"]["main"]["gap"])
        self.assertTrue(result["counters"]["story"]["gap"])
        self.assertEqual(result["recommendation"], "PREPARE_STORY")

    def test_story_only_gap_never_satisfied_by_main_volume(self):
        request = base_request(
            now="2026-09-06T20:00:00+04:00",
            main_load={"published_today": 3, "pending": 0, "last_published_at": None},
            story_load={"published_today": 0, "pending": 0, "last_published_at": None},
        )
        result = evaluate_cadence(request)
        self.assertTrue(result["counters"]["story"]["gap"])
        self.assertEqual(result["recommendation"], "PREPARE_STORY")

    def test_effective_load_is_published_plus_pending_no_double_count(self):
        request = base_request(
            main_load={"published_today": 1, "pending": 1, "last_published_at": None},
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["counters"]["main"]["effective_load"], 2)

    def test_negative_counter_is_rejected(self):
        request = base_request(
            main_load={"published_today": -1, "pending": 0, "last_published_at": None}
        )
        with self.assertRaises(CadenceContractError):
            evaluate_cadence(request)


class BackpressureTests(unittest.TestCase):
    def test_pending_story_blocks_recommendation_even_with_large_gap(self):
        request = base_request(
            now="2026-09-06T14:00:00+04:00",
            config={"story_target_min": 5},
            main_load={"published_today": 2, "pending": 0, "last_published_at": None},
            story_load={"published_today": 0, "pending": 1, "last_published_at": None},
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "PENDING_STORY_EXISTS")

    def test_pending_main_blocks_recommendation(self):
        request = base_request(
            now="2026-09-06T10:15:00+04:00",
            main_load={"published_today": 0, "pending": 1, "last_published_at": None},
            story_load={"published_today": 3, "pending": 0, "last_published_at": None},
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "PENDING_MAIN_EXISTS")

    def test_one_pending_item_suppresses_repeat_even_if_numeric_gap_remains(self):
        # pending=1 against target=3 still leaves a numeric gap of 2, but
        # a single pending item must still suppress a repeated
        # recommendation for that format.
        request = base_request(
            now="2026-09-06T14:00:00+04:00",
            config={"story_target_min": 3},
            main_load={"published_today": 2, "pending": 0, "last_published_at": None},
            story_load={"published_today": 0, "pending": 1, "last_published_at": None},
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "PENDING_STORY_EXISTS")


class QualityOverrideTests(unittest.TestCase):
    def test_gap_with_quality_candidate_recommends_preparation(self):
        request = base_request(now="2026-09-06T13:15:00+04:00")
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "PREPARE_MAIN_CANDIDATE")

    def test_gap_without_quality_candidate_never_recommends(self):
        request = base_request(
            now="2026-09-06T13:15:00+04:00",
            candidate_availability={
                "main_quality_candidate_available": False,
                "story_quality_candidate_available": False,
            },
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "NO_QUALITY_CANDIDATE")

    def test_huge_gap_does_not_override_missing_quality_candidate(self):
        request = base_request(
            now="2026-09-06T13:15:00+04:00",
            config={"main_target_min": 50, "story_target_min": 50},
            candidate_availability={
                "main_quality_candidate_available": False,
                "story_quality_candidate_available": False,
            },
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "NO_QUALITY_CANDIDATE")


class RecentActivitySpacingTests(unittest.TestCase):
    def test_just_inside_spacing_window_is_held(self):
        # main_min_spacing_minutes default is 120; 119 minutes ago is
        # still within the anti-burst window.
        request = base_request(
            now="2026-09-06T12:00:00+04:00",
            main_load={
                "published_today": 0,
                "pending": 0,
                "last_published_at": "2026-09-06T10:01:00+04:00",
            },
            story_load={"published_today": 3, "pending": 0, "last_published_at": None},
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "RECENT_AUDIENCE_ACTIVITY")

    def test_just_outside_spacing_window_is_eligible(self):
        request = base_request(
            now="2026-09-06T12:00:00+04:00",
            main_load={
                "published_today": 0,
                "pending": 0,
                "last_published_at": "2026-09-06T09:59:00+04:00",
            },
            story_load={"published_today": 3, "pending": 0, "last_published_at": None},
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "PREPARE_MAIN_CANDIDATE")
        self.assertEqual(result["reason_code"], "MAIN_GAP")


class DowntimeCoalescingTests(unittest.TestCase):
    def test_targets_met_with_marker_reports_coalesced(self):
        request = base_request(
            now="2026-09-06T12:00:00+04:00",
            main_load={"published_today": 2, "pending": 0, "last_published_at": None},
            story_load={"published_today": 3, "pending": 0, "last_published_at": None},
            signal={
                "breaking_day": False,
                "downtime_marker": {
                    "restart_after_downtime": True,
                    "time_since_last_evaluation_seconds": 14400,
                },
            },
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["reason_code"], "COALESCED_AFTER_DOWNTIME")
        self.assertTrue(result["context"]["downtime_coalesced"])

    def test_real_gap_with_marker_produces_normal_gap_reason(self):
        request = base_request(
            now="2026-09-06T17:00:00+04:00",
            signal={
                "breaking_day": False,
                "downtime_marker": {
                    "restart_after_downtime": True,
                    "time_since_last_evaluation_seconds": 28800,
                },
            },
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "PREPARE_MAIN_CANDIDATE")
        self.assertEqual(result["reason_code"], "MAIN_GAP")
        self.assertFalse(result["context"]["downtime_coalesced"])

    def test_gap_with_no_quality_candidate_preserved_over_coalescing(self):
        request = base_request(
            now="2026-09-06T11:00:00+04:00",
            story_load={"published_today": 3, "pending": 0, "last_published_at": None},
            candidate_availability={
                "main_quality_candidate_available": False,
                "story_quality_candidate_available": True,
            },
            signal={
                "breaking_day": False,
                "downtime_marker": {
                    "restart_after_downtime": True,
                    "time_since_last_evaluation_seconds": 14400,
                },
            },
        )
        result = evaluate_cadence(request)
        self.assertEqual(result["recommendation"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "NO_QUALITY_CANDIDATE")
        self.assertFalse(result["context"]["downtime_coalesced"])

    def test_downtime_magnitude_does_not_change_outcome(self):
        # Three theoretical missed slots vs. one must not produce three
        # (or different) recommendations -- only current state matters.
        short = base_request(
            now="2026-09-06T17:00:00+04:00",
            signal={
                "breaking_day": False,
                "downtime_marker": {
                    "restart_after_downtime": True,
                    "time_since_last_evaluation_seconds": 1800,
                },
            },
        )
        long = base_request(
            now="2026-09-06T17:00:00+04:00",
            signal={
                "breaking_day": False,
                "downtime_marker": {
                    "restart_after_downtime": True,
                    "time_since_last_evaluation_seconds": 999999,
                },
            },
        )
        result_short = evaluate_cadence(short)
        result_long = evaluate_cadence(long)
        self.assertEqual(
            (result_short["recommendation"], result_short["reason_code"]),
            (result_long["recommendation"], result_long["reason_code"]),
        )


class IdempotenceTests(unittest.TestCase):
    def test_repeated_evaluation_is_structurally_identical(self):
        request = base_request(now="2026-09-06T17:00:00+04:00")
        first = evaluate_cadence(request)
        second = evaluate_cadence(request)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )

    def test_evaluate_cadence_does_not_mutate_its_input(self):
        request = base_request(now="2026-09-06T17:00:00+04:00")
        snapshot = copy.deepcopy(request)
        evaluate_cadence(request)
        self.assertEqual(request, snapshot)


class CapabilityNegativeTests(unittest.TestCase):
    """Static checks that the controller has no side-effect capability."""

    FORBIDDEN_TOKENS = (
        "import requests",
        "import urllib",
        "import socket",
        "import subprocess",
        "smtplib",
        "http.client",
        "mcp__zernio",
        "telegram",
        "anthropic",
        "openai",
    )

    def test_module_source_has_no_network_or_connector_imports(self):
        source = inspect.getsource(controller)
        lowered = source.lower()
        for token in self.FORBIDDEN_TOKENS:
            self.assertNotIn(token.lower(), lowered, f"forbidden token found: {token}")

    def test_module_only_imports_standard_library(self):
        module_imports = {
            name
            for name in dir(controller)
            if inspect.ismodule(getattr(controller, name))
        }
        # copy, datetime.*, zoneinfo, typing -- no third-party or
        # repository connector modules.
        self.assertTrue(module_imports.issubset({"copy"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
