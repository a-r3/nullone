#!/usr/bin/env python3
"""Behavioral tests for the #36 deterministic breaking draft router.

Exercises nullone_breaking_router.evaluate_routing() against synthetic,
in-process inputs only. No network, no filesystem, no real #35/#33 calls --
this module is a pure function and is tested as one.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_breaking_router as router  # noqa: E402


def identity(
    *,
    event_id="event-1",
    development_id="dev-1",
    decision="DISTINCT_EVENT",
    reason_code="DISTINCT_DEVELOPMENT",
    reason_text="test",
    reconciliation_required=False,
    candidate_id="cand-1",
    parent_development_id=None,
    follow_up_reason=None,
):
    return {
        "schema": router.IDENTITY_SCHEMA,
        "candidate_id": candidate_id,
        "event": {
            "event_id": event_id,
            "development_id": development_id,
            "topic_id": "topic-1",
            "identity_basis": "EXACT_IDENTIFIER" if event_id else "UNRESOLVED",
            "identity_refs": ["ref-1"] if event_id else [],
        },
        "dedup": {
            "decision": decision,
            "matched_refs": [],
            "parent_development_id": parent_development_id,
            "follow_up_reason": follow_up_reason,
        },
        "reason_code": reason_code,
        "reason_text": reason_text,
        "reconciliation_required": reconciliation_required,
    }


def verification(state="PASS", evidence_refs=("ev-1",)):
    return router.VerificationInput(state=state, evidence_refs=tuple(evidence_refs))


def quiet_coverage():
    return router.RecentCoverageInput(related_coverage_exists=False, incremental_value_present=False)


def safe_story():
    return router.StorySafetyInput(quality_pass=True, load_pass=True, dependencies_available=True)


def main_findings(**overrides):
    base = dict(
        feed_score=20,
        carousel_score=20,
        single_visual_value=False,
        concise_announcement_value=False,
        meaningful_multi_slide_value=False,
        comparison_value=False,
        sequence_value=False,
        multi_fact_value=False,
        material_context_value=False,
        available_source_media=True,
        capacity_available=True,
    )
    base.update(overrides)
    return router.MainFormatFindings(**base)


def make_input(**overrides):
    base = dict(
        candidate_id="cand-1",
        assessment_ref="assess-1",
        state_snapshot_ref="state-1",
        severity="NORMAL",
        verification=verification(),
        identity=identity(),
        recent_coverage=quiet_coverage(),
        story_safety=safe_story(),
    )
    base.update(overrides)
    return router.RoutingInput(**base)


class NormalTests(unittest.TestCase):
    def test_normal_yields_normal_queue_no_targets(self):
        result = router.evaluate_routing(make_input(severity="NORMAL"))
        self.assertEqual(result.routing_decision, "NORMAL_QUEUE")
        self.assertEqual(result.reason_code, "NORMAL_CADENCE")
        self.assertEqual(result.draft_targets, ())
        self.assertIsNone(result.main_draft_justification)
        self.assertFalse(result.reconciliation_required)

    def test_normal_never_mutates_to_ready(self):
        result = router.evaluate_routing(make_input(severity="NORMAL"))
        # No such field/state exists on the output at all -- draft_targets
        # stays empty, which is the only side-effect surface.
        self.assertEqual(result.to_dict()["draft_targets"], [])


class MaterialTests(unittest.TestCase):
    def test_material_yields_story_only(self):
        result = router.evaluate_routing(
            make_input(severity="MATERIAL_BREAKING", severity_reason_text="time value")
        )
        self.assertEqual(result.routing_decision, "IMMEDIATE_STORY_DRAFT")
        self.assertEqual(result.reason_code, "MATERIAL_TIME_VALUE")
        self.assertEqual(result.draft_targets, ("STORY",))

    def test_material_can_never_produce_main(self):
        with self.assertRaises(router.PolicyInputError):
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="time value",
                main_justification="not allowed",
                main_format=main_findings(feed_score=50, available_source_media=True),
            )


class ExceptionalFormatTests(unittest.TestCase):
    def _exceptional(self, findings, justification="standalone value"):
        return router.evaluate_routing(
            make_input(
                severity="EXCEPTIONAL_BREAKING",
                severity_reason_text="exceptional value",
                main_justification=justification,
                main_format=findings,
            )
        )

    def test_feed_only_eligibility(self):
        result = self._exceptional(
            main_findings(feed_score=40, single_visual_value=True, available_source_media=True)
        )
        self.assertEqual(result.routing_decision, "IMMEDIATE_STORY_AND_MAIN_DRAFT")
        self.assertEqual(result.draft_targets, ("STORY", "FEED"))
        self.assertEqual(result.main_draft_justification, "standalone value")

    def test_carousel_only_eligibility(self):
        result = self._exceptional(
            main_findings(carousel_score=45, meaningful_multi_slide_value=True, comparison_value=True)
        )
        self.assertEqual(result.draft_targets, ("STORY", "CAROUSEL"))

    def test_both_eligible_single_concise_visual_prefers_feed(self):
        result = self._exceptional(
            main_findings(
                feed_score=40,
                carousel_score=45,
                meaningful_multi_slide_value=True,
                single_visual_value=True,
                available_source_media=True,
            )
        )
        self.assertEqual(result.draft_targets, ("STORY", "FEED"))

    def test_both_eligible_meaningful_multi_slide_prefers_carousel(self):
        result = self._exceptional(
            main_findings(
                feed_score=40,
                carousel_score=45,
                meaningful_multi_slide_value=True,
                comparison_value=True,
                available_source_media=True,
            )
        )
        self.assertEqual(result.draft_targets, ("STORY", "CAROUSEL"))

    def test_both_eligible_no_deterministic_winner_is_story_only(self):
        result = self._exceptional(
            main_findings(
                feed_score=40,
                carousel_score=45,
                meaningful_multi_slide_value=True,
                available_source_media=True,
                # Neither single-signal nor multi-slide-signal set.
            )
        )
        self.assertEqual(result.routing_decision, "IMMEDIATE_STORY_DRAFT")
        self.assertEqual(result.reason_code, "EXCEPTIONAL_STORY_ONLY")
        self.assertEqual(result.draft_targets, ("STORY",))
        self.assertIsNone(result.main_draft_justification)

    def test_carousel_below_threshold_is_not_carousel(self):
        result = self._exceptional(
            main_findings(carousel_score=41, meaningful_multi_slide_value=True, comparison_value=True)
        )
        self.assertEqual(result.draft_targets, ("STORY",))

    def test_feed_below_threshold_is_not_feed(self):
        result = self._exceptional(
            main_findings(feed_score=37, single_visual_value=True, available_source_media=True)
        )
        self.assertEqual(result.draft_targets, ("STORY",))

    def test_carousel_without_multi_slide_value_is_not_carousel(self):
        result = self._exceptional(
            main_findings(carousel_score=50, meaningful_multi_slide_value=False)
        )
        self.assertEqual(result.draft_targets, ("STORY",))

    def test_main_omitted_when_not_requested(self):
        result = router.evaluate_routing(
            make_input(severity="EXCEPTIONAL_BREAKING", severity_reason_text="exceptional value")
        )
        self.assertEqual(result.routing_decision, "IMMEDIATE_STORY_DRAFT")
        self.assertEqual(result.reason_code, "EXCEPTIONAL_STORY_ONLY")

    def test_main_capacity_exhausted_is_story_only(self):
        result = self._exceptional(
            main_findings(
                feed_score=40, single_visual_value=True, available_source_media=True,
                capacity_available=False,
            )
        )
        self.assertEqual(result.routing_decision, "IMMEDIATE_STORY_DRAFT")
        self.assertEqual(result.reason_code, "EXCEPTIONAL_STORY_ONLY")

    def test_main_justification_required_when_main_format_given(self):
        with self.assertRaises(router.PolicyInputError):
            make_input(
                severity="EXCEPTIONAL_BREAKING",
                severity_reason_text="exceptional value",
                main_justification="",
                main_format=main_findings(feed_score=50, available_source_media=True),
            )

    def test_draft_targets_never_produces_disallowed_combination(self):
        result = self._exceptional(
            main_findings(feed_score=40, single_visual_value=True, available_source_media=True)
        )
        self.assertIn(tuple(result.to_dict()["draft_targets"]), (("STORY", "FEED"),))


class DedupSuppressionTests(unittest.TestCase):
    def test_existing_consequential_state_suppresses(self):
        result = router.evaluate_routing(
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="x",
                identity=identity(
                    decision="EXACT_DUPLICATE",
                    reason_code="EXISTING_CONSEQUENTIAL_STATE",
                    reconciliation_required=True,
                ),
            )
        )
        self.assertEqual(result.routing_decision, "SUPPRESS_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")
        self.assertEqual(result.draft_targets, ())
        self.assertTrue(result.reconciliation_required)
        # Assessed severity is retained on a valid suppression.
        self.assertEqual(result.severity, "MATERIAL_BREAKING")

    def test_unresolved_event_history_suppresses(self):
        result = router.evaluate_routing(
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="x",
                identity=identity(
                    decision="MATERIAL_FOLLOW_UP",
                    reason_code="UNRESOLVED_EVENT_HISTORY",
                    reconciliation_required=True,
                    parent_development_id="dev-0",
                    follow_up_reason="OFFICIAL_NUMBER_CHANGED",
                ),
            )
        )
        self.assertEqual(result.routing_decision, "SUPPRESS_DUPLICATE")
        self.assertEqual(result.reason_code, "UNRESOLVED_EVENT_HISTORY")

    def test_existing_draft_request_suppresses(self):
        result = router.evaluate_routing(
            make_input(
                identity=identity(
                    decision="EXACT_DUPLICATE",
                    reason_code="EXISTING_DRAFT_REQUEST",
                ),
            )
        )
        self.assertEqual(result.routing_decision, "SUPPRESS_DUPLICATE")
        self.assertEqual(result.reason_code, "EXISTING_DRAFT_REQUEST")

    def test_candidate_excluded_suppresses(self):
        result = router.evaluate_routing(
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="x",
                identity=identity(decision="EXACT_DUPLICATE", reason_code="CANDIDATE_EXCLUDED"),
            )
        )
        self.assertEqual(result.reason_code, "CANDIDATE_EXCLUDED")

    def test_exact_event_duplicate_suppresses(self):
        result = router.evaluate_routing(
            make_input(identity=identity(decision="EXACT_DUPLICATE", reason_code="EXACT_EVENT_DUPLICATE"))
        )
        self.assertEqual(result.routing_decision, "SUPPRESS_DUPLICATE")
        self.assertEqual(result.reason_code, "EXACT_EVENT_DUPLICATE")

    def test_same_event_different_source_suppresses(self):
        result = router.evaluate_routing(
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="x",
                identity=identity(decision="SAME_EVENT", reason_code="SAME_EVENT_DIFFERENT_SOURCE"),
            )
        )
        self.assertEqual(result.reason_code, "SAME_EVENT_DIFFERENT_SOURCE")

    def test_ambiguous_identity_blocks(self):
        result = router.evaluate_routing(
            make_input(
                severity=None,
                identity=identity(
                    decision="AMBIGUOUS_IDENTITY",
                    reason_code="IDENTITY_UNRESOLVED",
                    reconciliation_required=True,
                ),
            )
        )
        self.assertEqual(result.routing_decision, "BLOCKED_AMBIGUOUS_IDENTITY")
        self.assertEqual(result.reason_code, "IDENTITY_UNRESOLVED")
        self.assertTrue(result.reconciliation_required)

    def test_ambiguous_state_unavailable_blocks(self):
        result = router.evaluate_routing(
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="x",
                identity=identity(
                    decision="AMBIGUOUS_IDENTITY",
                    reason_code="STATE_UNAVAILABLE_OR_CONFLICTING",
                    reconciliation_required=True,
                ),
            )
        )
        self.assertEqual(result.routing_decision, "BLOCKED_AMBIGUOUS_IDENTITY")
        self.assertEqual(result.reason_code, "STATE_UNAVAILABLE_OR_CONFLICTING")

    def test_material_follow_up_linked_accelerates(self):
        result = router.evaluate_routing(
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="x",
                identity=identity(
                    decision="MATERIAL_FOLLOW_UP",
                    reason_code="MATERIAL_FOLLOW_UP_LINKED",
                    reconciliation_required=False,
                    parent_development_id="dev-0",
                    follow_up_reason="OFFICIAL_NUMBER_CHANGED",
                ),
            )
        )
        self.assertEqual(result.routing_decision, "IMMEDIATE_STORY_DRAFT")
        self.assertEqual(result.dedup["parent_development_id"], "dev-0")


class VerificationTests(unittest.TestCase):
    def test_non_pass_verification_blocks(self):
        result = router.evaluate_routing(
            make_input(severity=None, verification=verification(state="UNVERIFIED", evidence_refs=()))
        )
        self.assertEqual(result.routing_decision, "BLOCKED_UNVERIFIED")
        self.assertEqual(result.reason_code, "EVIDENCE_INSUFFICIENT")
        self.assertIsNone(result.severity)
        self.assertEqual(result.draft_targets, ())

    def test_severity_requires_pass_verification(self):
        with self.assertRaises(router.PolicyInputError):
            make_input(severity="NORMAL", verification=verification(state="PARTIAL", evidence_refs=()))


class RecentCoverageTests(unittest.TestCase):
    def test_related_coverage_without_incremental_value_suppresses(self):
        result = router.evaluate_routing(
            make_input(
                recent_coverage=router.RecentCoverageInput(
                    related_coverage_exists=True, incremental_value_present=False
                )
            )
        )
        self.assertEqual(result.routing_decision, "SUPPRESS_RECENT_COVERAGE")
        self.assertEqual(result.reason_code, "NO_INCREMENTAL_AUDIENCE_VALUE")

    def test_related_coverage_with_incremental_value_proceeds(self):
        result = router.evaluate_routing(
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="x",
                recent_coverage=router.RecentCoverageInput(
                    related_coverage_exists=True, incremental_value_present=True
                ),
            )
        )
        self.assertEqual(result.routing_decision, "IMMEDIATE_STORY_DRAFT")


class StorySafetyTests(unittest.TestCase):
    def test_story_blocked_before_main_never_creates_main(self):
        result = router.evaluate_routing(
            make_input(
                severity="EXCEPTIONAL_BREAKING",
                severity_reason_text="x",
                story_safety=router.StorySafetyInput(
                    quality_pass=False, load_pass=True, dependencies_available=True
                ),
                main_justification="value",
                main_format=main_findings(feed_score=50, available_source_media=True),
            )
        )
        self.assertEqual(result.routing_decision, "BLOCKED_DRAFT_SAFETY")
        self.assertEqual(result.reason_code, "STORY_QUALITY_BLOCK")
        self.assertEqual(result.draft_targets, ())

    def test_story_load_block_priority_over_dependency(self):
        result = router.evaluate_routing(
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="x",
                story_safety=router.StorySafetyInput(
                    quality_pass=True, load_pass=False, dependencies_available=False
                ),
            )
        )
        self.assertEqual(result.reason_code, "STORY_LOAD_BLOCK")

    def test_dependency_unavailable_blocks(self):
        result = router.evaluate_routing(
            make_input(
                severity="MATERIAL_BREAKING",
                severity_reason_text="x",
                story_safety=router.StorySafetyInput(
                    quality_pass=True, load_pass=True, dependencies_available=False
                ),
            )
        )
        self.assertEqual(result.reason_code, "DRAFT_DEPENDENCY_UNAVAILABLE")


class DeterminismTests(unittest.TestCase):
    def test_identical_input_yields_identical_output(self):
        ri = make_input(severity="MATERIAL_BREAKING", severity_reason_text="x")
        a = router.evaluate_routing(ri)
        b = router.evaluate_routing(ri)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_output_schema_fields_exact(self):
        result = router.evaluate_routing(make_input(severity="NORMAL"))
        expected_fields = {
            "schema", "candidate_id", "assessment_ref", "state_snapshot_ref", "severity",
            "event", "verification", "dedup", "routing_decision", "reason_code",
            "reason_text", "draft_targets", "main_draft_justification",
            "reconciliation_required",
        }
        self.assertEqual(set(result.to_dict()), expected_fields)


if __name__ == "__main__":
    unittest.main()
