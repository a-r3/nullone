#!/usr/bin/env python3
"""Offline behavioral tests for NullOne BreakingWorkflow (#63)."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_breaking_dispatch as breaking_dispatch  # noqa: E402
import nullone_breaking_identity as breaking_identity  # noqa: E402
import nullone_breaking_workflow as workflow  # noqa: E402
from nullone_breaking_radar_edge import (  # noqa: E402
    BreakingRadarEdgeError,
    normalize_breaking_radar_handoff,
)
from nullone_breaking_workflow_input import BreakingWorkflowInputError  # noqa: E402
from nullone_run_outcome import assess_run, make_run_id  # noqa: E402
from nullone_scheduler_invocation import compute_occurrence_id  # noqa: E402

NOW = datetime(2026, 9, 8, 15, 0, tzinfo=ZoneInfo("Asia/Baku"))
_DEFAULT_MAIN_FINAL_VERIFIER = object()


def make_trigger(**overrides):
    trigger = {
        "schema": "nullone.scheduler-invocation.v1",
        "contract_version": "1.0.0",
        "workflow_id": "breaking",
        "source": "openclaw",
        "external_occurrence_id": "radar-cycle-20260908T110000Z",
        "scheduled_for": "2026-09-08T11:00:00Z",
        "triggered_at": "2026-09-08T11:00:03Z",
    }
    trigger.update(overrides)
    if "occurrence_id" not in overrides:
        trigger["occurrence_id"] = compute_occurrence_id(
            trigger["workflow_id"],
            trigger["source"],
            trigger["external_occurrence_id"],
            trigger["scheduled_for"],
        )
    return trigger


def main_findings(**overrides):
    result = {
        "feed_score": 40,
        "carousel_score": 20,
        "single_visual_value": True,
        "concise_announcement_value": False,
        "meaningful_multi_slide_value": False,
        "comparison_value": False,
        "sequence_value": False,
        "multi_fact_value": False,
        "material_context_value": False,
        "available_source_media": True,
    }
    result.update(overrides)
    return result


def make_assessment(*, severity="MATERIAL_BREAKING", **overrides):
    reason = None if severity in (None, "NORMAL") else "Synthetic time value is material."
    assessment = {
        "schema": "nullone.breaking-workflow-input.v1",
        "contract_version": "1.0.0",
        "candidate_id": "candidate-breaking-1",
        "candidate_version": "v1",
        "assessment_ref": "assessment:synthetic:1",
        "state_snapshot_ref": "state:synthetic:1",
        "topic": "Synthetic product release",
        "topic_cluster": "synthetic-product",
        "content_type": "BREAKING",
        "evidence": [
            {
                "ref": "evidence:official:1",
                "supported_claim": "Synthetic Product 2 is available in Region A.",
                "source_url": "https://example.invalid/releases/product-2",
                "announcement_id": "release-product-2-region-a",
                "product": "Synthetic Product",
                "version": "2",
                "region": "Region A",
                "availability_stage": "GENERAL_AVAILABILITY",
            }
        ],
        "follow_up_delta": None,
        "source_attribution": "Synthetic official source",
        "limitations": ["Region A only."],
        "product_version_region": {
            "product": "Synthetic Product",
            "version": "2",
            "region": "Region A",
        },
        "source_image": None,
        "verification": {"state": "PASS", "evidence_refs": ["evidence:official:1"]},
        "severity_assessment": {"classification": severity, "reason_text": reason},
        "recent_coverage": {
            "related_coverage_exists": False,
            "incremental_value_present": True,
            "assessment_ref": "coverage:synthetic:1",
            "freshness_ref": "freshness:synthetic:1",
        },
        "story_safety": {
            "quality_pass": True,
            "quality_ref": "quality:synthetic:1",
            "dependencies_available": True,
            "dependencies_ref": "dependencies:synthetic:1",
        },
        "main_assessment": None,
    }
    assessment.update(overrides)
    return assessment


def exceptional_assessment(**finding_overrides):
    assessment = make_assessment(severity="EXCEPTIONAL_BREAKING")
    assessment["main_assessment"] = {
        "standalone_justification": "Distinct standalone audience value.",
        "findings": main_findings(**finding_overrides),
    }
    return assessment


class FakePipelineResult:
    def __init__(
        self,
        outcome="DRAFT_CREATED",
        *,
        manifest_id="manifest-story",
        review_post_id="review-story",
        preview_delivery=None,
    ):
        self.outcome = outcome
        self.reason_code = outcome
        self.reason_text = outcome
        self.manifest_id = manifest_id
        self.review_post_id = review_post_id
        self.preview_delivery = (
            {"status": "SENT"} if preview_delivery is None else preview_delivery
        )


class BoundMainProvider:
    def __init__(self, *, fmt="FEED", count=1, mutate=None):
        self.fmt = fmt
        self.count = count
        self.mutate = mutate
        self.calls = 0

    def get_candidates(self, *, candidate_id, selected_format, request_lineage):
        self.calls += 1
        candidates = []
        for _ in range(self.count):
            candidate = {
                "candidate_id": candidate_id,
                "candidate_version": "v1",
                "request_lineage": request_lineage,
                "topic": "Synthetic product release",
                "topic_cluster": "synthetic-product",
                "content_type": "BREAKING",
                "format": self.fmt,
                "verification": "PASS",
                "evidence_refs": ["evidence:official:1"],
                "source_attribution": "Synthetic official source",
                "caption_text": "Synthetic verified caption.",
                "feed": {
                    "source_image": "social/research/synthetic.png",
                    "kicker": "BREAKING",
                    "headline": "Synthetic Product 2",
                    "source_name": "Synthetic official source",
                },
            }
            if self.fmt == "CAROUSEL":
                candidate.pop("feed")
                candidate["carousel"] = {
                    "meaningful_multi_slide_value": True,
                    "slides": [
                        {"type": "cover", "headline": "Synthetic Product 2"},
                        {"type": "final", "headline": "Scope: Region A"},
                    ],
                }
            if self.mutate:
                self.mutate(candidate)
            candidates.append(candidate)
        return candidates


class FailingMainProvider:
    def __init__(self):
        self.calls = 0

    def get_candidates(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("synthetic provider failure")


class CountingDraftConnector:
    def __init__(self):
        self.calls = 0

    def create_review_draft(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("draft connector must not be called")


class CountingReviewDelivery:
    def __init__(self):
        self.calls = 0

    def send(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("review delivery must not be called")


class BreakingWorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.state_root = self.workspace / "social"
        (self.workspace / "social/ops/manifests").mkdir(parents=True)
        state = self.workspace / "social/state"
        state.mkdir(parents=True)
        for name in ("publish-ledger.jsonl", "topic-ledger.jsonl", "candidate-queue.md"):
            (state / name).touch()
        self.workspace_patch = patch.object(bridge_common, "WORKSPACE", self.workspace)
        self.workspace_patch.start()
        self.addCleanup(self.workspace_patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.story_calls = 0
        self.main_calls = []

    def story_result(self, *_args, **_kwargs):
        self.story_calls += 1
        return FakePipelineResult()

    def main_result(self, candidate, **_kwargs):
        self.main_calls.append(candidate["format"])
        return FakePipelineResult(
            manifest_id=f"manifest-{candidate['format'].lower()}",
            review_post_id=f"review-{candidate['format'].lower()}",
        )

    def run_workflow(
        self,
        assessment=None,
        *,
        trigger=None,
        main_provider=None,
        main_final_verifier=_DEFAULT_MAIN_FINAL_VERIFIER,
        draft_connector=None,
        review_delivery=None,
        **kwargs,
    ):
        if main_final_verifier is _DEFAULT_MAIN_FINAL_VERIFIER:
            main_final_verifier = object() if main_provider else None
        with patch.object(workflow, "run_story_pipeline", side_effect=self.story_result), patch.object(
            workflow, "run_main_pipeline", side_effect=self.main_result
        ):
            return workflow.run_breaking_workflow(
                trigger or make_trigger(),
                assessment or make_assessment(),
                workspace=self.workspace,
                state_root=self.state_root,
                now=NOW,
                story_writer=object(),
                story_verifier=object(),
                draft_connector=draft_connector or object(),
                review_delivery=review_delivery or object(),
                dependency_recheck=lambda _stage: True,
                main_candidate_provider=main_provider,
                main_final_verifier=main_final_verifier,
                **kwargs,
            )

    def assert_run_outcome_accepted(self, result):
        mapped = result.run_outcome_mapping()
        for relative_path in mapped["required_artifacts"]:
            path = self.workspace / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        assessed = assess_run(
            workflow_id="breaking",
            occurrence_id=result.occurrence_id,
            scheduler_status="ok",
            domain_outcome=mapped["domain_outcome"],
            artifact_root=self.workspace,
            required_artifacts=tuple(mapped["required_artifacts"]),
            reason_code=mapped["reason_code"],
            reason_text=mapped["reason_text"],
            empty_success=mapped["empty_success"],
        )
        self.assertEqual(assessed["domain_outcome"], mapped["domain_outcome"])
        return assessed

    def write_pending(self, fmt, index):
        manifest = {
            "schema": "nullone.production.v1",
            "manifest_id": f"load-{fmt.lower()}-{index}",
            "candidate_id": f"other-{fmt.lower()}-{index}",
            "format": fmt,
            "review": {"state": "DRAFT_CREATED", "zernio_draft_id": f"draft-{index}"},
            "approval": {"first_stage": False, "final_publish": False},
            "publication": {"state": "NOT_REQUESTED", "live_zernio_post_id": None},
        }
        path = self.workspace / f"social/ops/manifests/{manifest['manifest_id']}.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")


class InputAndTriggerTests(BreakingWorkflowTestCase):
    def assert_rejected(self, assessment):
        result = self.run_workflow(assessment)
        self.assertEqual(result.domain_outcome, "BLOCKED")
        self.assertEqual(self.story_calls, 0)
        self.assertEqual(self.main_calls, [])

    def test_wrong_workflow_id_rejected(self):
        result = self.run_workflow(trigger=make_trigger(workflow_id="story"))
        self.assertEqual(result.reason_code, "TRIGGER_REJECTED")
        self.assertEqual(self.story_calls, 0)

    def test_malformed_scheduler_invocation_rejected(self):
        result = self.run_workflow(trigger={"workflow_id": "breaking"})
        self.assertEqual(result.reason_code, "TRIGGER_REJECTED")

    def test_unknown_input_field_rejected(self):
        value = make_assessment()
        value["invented"] = True
        self.assert_rejected(value)

    def test_evidence_ref_mismatch_rejected(self):
        value = make_assessment()
        value["verification"]["evidence_refs"] = ["other"]
        self.assert_rejected(value)

    def test_pass_without_evidence_rejected(self):
        value = make_assessment()
        value["evidence"] = []
        value["verification"]["evidence_refs"] = []
        self.assert_rejected(value)

    def test_fail_verification_state_is_rejected(self):
        value = make_assessment()
        value["verification"]["state"] = "FAIL"
        value["severity_assessment"] = {"classification": None, "reason_text": None}
        with self.assertRaises(BreakingWorkflowInputError):
            workflow.validate_breaking_workflow_input(value)
        self.assert_rejected(value)

    def test_non_pass_with_non_null_severity_is_rejected(self):
        for state in ("UNVERIFIED", "PARTIAL", "BLOCKED"):
            with self.subTest(state=state):
                value = make_assessment()
                value["verification"]["state"] = state
                self.assert_rejected(value)

    def test_malformed_structured_evidence_rejected(self):
        value = make_assessment()
        value["evidence"][0]["unknown"] = "no"
        self.assert_rejected(value)

    def test_split_identity_and_cadence_state_roots_are_rejected(self):
        with patch.object(workflow, "run_story_pipeline", side_effect=self.story_result):
            result = workflow.run_breaking_workflow(
                make_trigger(), make_assessment(), workspace=self.workspace,
                state_root=self.workspace / "other-social", now=NOW,
                story_writer=object(), story_verifier=object(), draft_connector=object(),
                review_delivery=object(), dependency_recheck=lambda _stage: True,
            )
        self.assertEqual(result.reason_code, "STATE_ROOT_MISMATCH")
        self.assertEqual(self.story_calls, 0)

    def test_invalid_follow_up_delta_rejected(self):
        value = make_assessment()
        value["follow_up_delta"] = {
            "delta_kind": "HEADLINE_CHANGED",
            "parent_claim": "old",
            "new_claim": "new",
            "evidence_ref": "evidence:official:1",
        }
        self.assert_rejected(value)

    def test_malformed_scores_and_booleans_rejected(self):
        value = exceptional_assessment(feed_score=True)
        self.assert_rejected(value)
        value = exceptional_assessment(single_visual_value="yes")
        self.assert_rejected(value)


class EdgeNormalizationTests(unittest.TestCase):
    def handoff(self):
        return {
            "schema": "nullone.breaking-radar-handoff.v1",
            "contract_version": "1.0.0",
            "occurrence": {
                "source_occurrence_id": "radar-cycle-20260908T110000Z",
                "scheduled_for": "2026-09-08T11:00:00Z",
                "triggered_at": "2026-09-08T11:00:03Z",
            },
            "assessment": make_assessment(),
        }

    def test_edge_emits_valid_breaking_invocation_and_validated_input(self):
        normalized = normalize_breaking_radar_handoff(self.handoff())
        self.assertEqual(normalized.trigger["workflow_id"], "breaking")
        self.assertEqual(normalized.trigger["source"], "openclaw")
        self.assertEqual(normalized.assessment["candidate_id"], "candidate-breaking-1")

    def test_replay_triggered_at_does_not_change_occurrence_id(self):
        first = normalize_breaking_radar_handoff(self.handoff())
        replay = self.handoff()
        replay["occurrence"]["triggered_at"] = "2026-09-08T11:01:59Z"
        second = normalize_breaking_radar_handoff(replay)
        self.assertEqual(
            first.trigger["external_occurrence_id"],
            second.trigger["external_occurrence_id"],
        )
        self.assertEqual(first.trigger["occurrence_id"], second.trigger["occurrence_id"])

    def test_same_scan_two_candidates_have_distinct_occurrences_and_run_ids(self):
        first = normalize_breaking_radar_handoff(self.handoff())
        second_handoff = self.handoff()
        second_handoff["assessment"]["candidate_id"] = "candidate-breaking-2"
        second = normalize_breaking_radar_handoff(second_handoff)
        self.assertNotEqual(
            first.trigger["external_occurrence_id"],
            second.trigger["external_occurrence_id"],
        )
        self.assertNotEqual(first.trigger["occurrence_id"], second.trigger["occurrence_id"])
        self.assertNotEqual(
            make_run_id(workflow_id="breaking", occurrence_id=first.trigger["occurrence_id"]),
            make_run_id(workflow_id="breaking", occurrence_id=second.trigger["occurrence_id"]),
        )

    def test_same_candidate_different_scan_has_distinct_occurrence(self):
        first = normalize_breaking_radar_handoff(self.handoff())
        later = self.handoff()
        later["occurrence"]["source_occurrence_id"] = "radar-cycle-20260908T113000Z"
        later["occurrence"]["scheduled_for"] = "2026-09-08T11:30:00Z"
        second = normalize_breaking_radar_handoff(later)
        self.assertNotEqual(first.trigger["occurrence_id"], second.trigger["occurrence_id"])

    def test_corrected_assessment_keeps_candidate_occurrence_stable(self):
        first = normalize_breaking_radar_handoff(self.handoff())
        corrected = self.handoff()
        corrected["assessment"]["severity_assessment"]["reason_text"] = (
            "Corrected but still verified material assessment."
        )
        corrected["assessment"]["evidence"][0]["supported_claim"] = (
            "Corrected supported wording for Synthetic Product 2 in Region A."
        )
        second = normalize_breaking_radar_handoff(corrected)
        self.assertEqual(first.trigger["occurrence_id"], second.trigger["occurrence_id"])

    def test_edge_rejects_unknown_occurrence_field(self):
        value = self.handoff()
        value["occurrence"]["job_uuid"] = "not-an-application-identity"
        with self.assertRaises(BreakingRadarEdgeError):
            normalize_breaking_radar_handoff(value)

    def test_normalized_edge_value_is_directly_accepted_by_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "social/ops/manifests").mkdir(parents=True)
            state = workspace / "social/state"
            state.mkdir(parents=True)
            for name in ("publish-ledger.jsonl", "topic-ledger.jsonl", "candidate-queue.md"):
                (state / name).touch()
            normalized = normalize_breaking_radar_handoff(self.handoff())
            with patch.object(bridge_common, "WORKSPACE", workspace), patch.object(
                workflow, "run_story_pipeline", return_value=FakePipelineResult()
            ):
                result = workflow.run_breaking_workflow(
                    normalized.trigger,
                    normalized.assessment,
                    workspace=workspace,
                    state_root=workspace / "social",
                    now=NOW,
                    story_writer=object(),
                    story_verifier=object(),
                    draft_connector=object(),
                    review_delivery=object(),
                    dependency_recheck=lambda _stage: True,
                )
            self.assertEqual(result.domain_outcome, "SUCCEEDED")


class ClassificationAndDispatchTests(BreakingWorkflowTestCase):
    def test_normal_routes_to_normal_queue_without_draft(self):
        result = self.run_workflow(make_assessment(severity="NORMAL", content_type="NEWS"))
        self.assertEqual(result.routing_result["routing_decision"], "NORMAL_QUEUE")
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(self.story_calls, 0)
        self.assertEqual(result.run_outcome_mapping()["empty_success"], "NO_ACTION")
        assessed = self.assert_run_outcome_accepted(result)
        self.assertEqual(assessed["empty_success"], "NO_ACTION")

    def test_material_routes_story_only(self):
        result = self.run_workflow()
        self.assertEqual(result.routing_result["draft_targets"], ["STORY"])
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(self.story_calls, 1)
        self.assertEqual(self.main_calls, [])

    def test_recent_coverage_without_incremental_value_is_no_action(self):
        assessment = make_assessment()
        assessment["recent_coverage"]["related_coverage_exists"] = True
        assessment["recent_coverage"]["incremental_value_present"] = False
        result = self.run_workflow(assessment)
        self.assertEqual(result.routing_result["routing_decision"], "SUPPRESS_RECENT_COVERAGE")
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(self.story_calls, 0)

    def test_non_pass_verification_states_reach_existing_router_gate(self):
        for state in ("UNVERIFIED", "PARTIAL", "BLOCKED"):
            with self.subTest(state=state):
                assessment = make_assessment(severity=None)
                assessment["verification"]["state"] = state
                draft_connector = CountingDraftConnector()
                review_delivery = CountingReviewDelivery()
                result = self.run_workflow(
                    assessment,
                    draft_connector=draft_connector,
                    review_delivery=review_delivery,
                )
                self.assertEqual(
                    result.routing_result["routing_decision"], "BLOCKED_UNVERIFIED"
                )
                self.assertEqual(result.routing_result["reason_code"], "EVIDENCE_INSUFFICIENT")
                self.assertEqual(result.domain_outcome, "BLOCKED")
                self.assertEqual(self.story_calls, 0)
                self.assertEqual(self.main_calls, [])
                self.assertEqual(draft_connector.calls, 0)
                self.assertEqual(review_delivery.calls, 0)
                self.assert_run_outcome_accepted(result)

    def test_pass_verification_behavior_is_unchanged(self):
        result = self.run_workflow()
        self.assertEqual(result.routing_result["routing_decision"], "IMMEDIATE_STORY_DRAFT")
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(self.story_calls, 1)

    def test_story_quality_failure_is_blocked_without_dispatch(self):
        assessment = make_assessment()
        assessment["story_safety"]["quality_pass"] = False
        result = self.run_workflow(assessment)
        self.assertEqual(result.routing_result["reason_code"], "STORY_QUALITY_BLOCK")
        self.assertEqual(self.story_calls, 0)
        self.assert_run_outcome_accepted(result)

    def test_exceptional_without_main_assessment_is_story_only(self):
        result = self.run_workflow(make_assessment(severity="EXCEPTIONAL_BREAKING"))
        self.assertEqual(result.routing_result["draft_targets"], ["STORY"])

    def test_exceptional_feed_is_story_first_then_feed(self):
        order = []

        def story(*_args, **_kwargs):
            order.append("STORY")
            return FakePipelineResult()

        def main(candidate, **_kwargs):
            order.append(candidate["format"])
            return FakePipelineResult(manifest_id="manifest-feed")

        class OrderedProvider(BoundMainProvider):
            def get_candidates(provider_self, **kwargs):
                order.append("PROVIDER")
                return super().get_candidates(**kwargs)

        with patch.object(workflow, "run_story_pipeline", side_effect=story), patch.object(
            workflow, "run_main_pipeline", side_effect=main
        ):
            result = workflow.run_breaking_workflow(
                make_trigger(), exceptional_assessment(), workspace=self.workspace,
                state_root=self.state_root, now=NOW, story_writer=object(),
                story_verifier=object(), draft_connector=object(), review_delivery=object(),
                dependency_recheck=lambda _stage: True,
                main_candidate_provider=OrderedProvider(fmt="FEED"),
                main_final_verifier=object(),
            )
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(order, ["STORY", "PROVIDER", "FEED"])
        self.assert_run_outcome_accepted(result)

    def test_exceptional_carousel_is_story_first_then_carousel(self):
        result = self.run_workflow(
            exceptional_assessment(
                feed_score=20,
                carousel_score=45,
                single_visual_value=False,
                meaningful_multi_slide_value=True,
                comparison_value=True,
                available_source_media=False,
            ),
            main_provider=BoundMainProvider(fmt="CAROUSEL"),
        )
        self.assertEqual(result.routing_result["draft_targets"], ["STORY", "CAROUSEL"])
        self.assertEqual(self.main_calls, ["CAROUSEL"])

    def test_both_qualify_single_signal_selects_feed(self):
        assessment = exceptional_assessment(
            carousel_score=45, meaningful_multi_slide_value=True
        )
        result = self.run_workflow(assessment, main_provider=BoundMainProvider(fmt="FEED"))
        self.assertEqual(result.routing_result["draft_targets"], ["STORY", "FEED"])

    def test_both_qualify_multi_signal_selects_carousel(self):
        assessment = exceptional_assessment(
            carousel_score=45,
            meaningful_multi_slide_value=True,
            single_visual_value=False,
            comparison_value=True,
        )
        result = self.run_workflow(assessment, main_provider=BoundMainProvider(fmt="CAROUSEL"))
        self.assertEqual(result.routing_result["draft_targets"], ["STORY", "CAROUSEL"])

    def test_both_qualify_ambiguous_structure_omits_main(self):
        assessment = exceptional_assessment(
            carousel_score=45,
            meaningful_multi_slide_value=True,
            single_visual_value=False,
        )
        result = self.run_workflow(assessment)
        self.assertEqual(result.routing_result["draft_targets"], ["STORY"])

    def test_review_delivery_failure_is_failed_and_stops_main(self):
        def failed_story(*_args, **_kwargs):
            self.story_calls += 1
            return FakePipelineResult(preview_delivery={"status": "FAILED"})

        provider = BoundMainProvider()
        with patch.object(workflow, "run_story_pipeline", side_effect=failed_story), patch.object(
            workflow, "run_main_pipeline", side_effect=self.main_result
        ):
            result = workflow.run_breaking_workflow(
                make_trigger(), exceptional_assessment(), workspace=self.workspace,
                state_root=self.state_root, now=NOW, story_writer=object(),
                story_verifier=object(), draft_connector=object(), review_delivery=object(),
                dependency_recheck=lambda _stage: True,
                main_candidate_provider=provider, main_final_verifier=object(),
            )
        self.assertEqual(result.domain_outcome, "FAILED")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.main_calls, [])
        self.assert_run_outcome_accepted(result)

    def test_runner_exception_is_unknown_and_replay_does_not_retry(self):
        calls = []

        def explode(*_args, **_kwargs):
            calls.append(1)
            raise RuntimeError("synthetic ambiguity")

        kwargs = dict(
            workspace=self.workspace, state_root=self.state_root, now=NOW,
            story_writer=object(), story_verifier=object(), draft_connector=object(),
            review_delivery=object(), dependency_recheck=lambda _stage: True,
        )
        with patch.object(workflow, "run_story_pipeline", side_effect=explode):
            first = workflow.run_breaking_workflow(make_trigger(), make_assessment(), **kwargs)
            second = workflow.run_breaking_workflow(make_trigger(), make_assessment(), **kwargs)
        self.assertEqual(first.domain_outcome, "UNKNOWN")
        self.assertTrue(first.reconciliation_required)
        self.assertEqual(second.domain_outcome, "UNKNOWN")
        self.assertEqual(calls, [1])
        self.assert_run_outcome_accepted(first)

    def test_story_runner_exception_never_invokes_main_provider(self):
        provider = BoundMainProvider()
        with patch.object(
            workflow, "run_story_pipeline", side_effect=RuntimeError("ambiguous story")
        ), patch.object(workflow, "run_main_pipeline", side_effect=self.main_result):
            result = workflow.run_breaking_workflow(
                make_trigger(), exceptional_assessment(), workspace=self.workspace,
                state_root=self.state_root, now=NOW, story_writer=object(),
                story_verifier=object(), draft_connector=object(), review_delivery=object(),
                dependency_recheck=lambda _stage: True,
                main_candidate_provider=provider, main_final_verifier=object(),
            )
        self.assertEqual(result.domain_outcome, "UNKNOWN")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.main_calls, [])


class CapacityAndRecheckTests(BreakingWorkflowTestCase):
    def test_story_max_minus_one_is_eligible_including_pending(self):
        self.write_pending("STORY", 1)
        result = self.run_workflow(cadence_config={"story_target_max_breaking": 2})
        self.assertEqual(result.domain_outcome, "SUCCEEDED")

    def test_regular_targets_spacing_and_quiet_hours_are_not_breaking_permission_gates(self):
        result = self.run_workflow(
            cadence_config={
                "main_target_min": 999,
                "story_target_min": 999,
                "main_min_spacing_minutes": 999,
                "story_min_spacing_minutes": 999,
                "quiet_hours_enabled": True,
            }
        )
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(self.story_calls, 1)

    def test_story_at_max_is_blocked_without_attempt(self):
        self.write_pending("STORY", 1)
        result = self.run_workflow(cadence_config={"story_target_max_breaking": 1})
        self.assertEqual(result.routing_result["reason_code"], "STORY_LOAD_BLOCK")
        self.assertEqual(self.story_calls, 0)

    def test_story_load_drift_blocks_at_dispatch(self):
        empty = {
            "story_load": {"published_today": 0, "pending": 0, "last_published_at": None},
            "main_load": {"published_today": 0, "pending": 0, "last_published_at": None},
        }
        exhausted = copy.deepcopy(empty)
        exhausted["story_load"]["pending"] = 1
        with patch.object(workflow, "collect_format_loads", side_effect=[empty, exhausted]):
            result = self.run_workflow(cadence_config={"story_target_max_breaking": 1})
        self.assertEqual(result.domain_outcome, "BLOCKED")
        self.assertEqual(result.dispatch_result.record["targets"]["STORY"]["reason_code"], "STORY_LOAD_BLOCK")
        self.assertEqual(self.story_calls, 0)
        self.assert_run_outcome_accepted(result)

    def test_story_load_drift_never_invokes_optional_main_provider(self):
        empty = {
            "story_load": {"published_today": 0, "pending": 0, "last_published_at": None},
            "main_load": {"published_today": 0, "pending": 0, "last_published_at": None},
        }
        exhausted = copy.deepcopy(empty)
        exhausted["story_load"]["pending"] = 1
        provider = BoundMainProvider()
        with patch.object(workflow, "collect_format_loads", side_effect=[empty, exhausted]):
            result = self.run_workflow(
                exceptional_assessment(),
                main_provider=provider,
                cadence_config={"story_target_max_breaking": 1},
            )
        self.assertEqual(result.domain_outcome, "BLOCKED")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.main_calls, [])

    def test_main_at_max_is_omitted_and_provider_not_called(self):
        self.write_pending("FEED", 1)
        provider = BoundMainProvider()
        result = self.run_workflow(
            exceptional_assessment(),
            main_provider=provider,
            cadence_config={"main_target_max_breaking": 1},
        )
        self.assertEqual(result.routing_result["draft_targets"], ["STORY"])
        self.assertEqual(provider.calls, 0)

    def test_main_max_minus_one_is_eligible(self):
        self.write_pending("FEED", 1)
        result = self.run_workflow(
            exceptional_assessment(),
            main_provider=BoundMainProvider(),
            cadence_config={"main_target_max_breaking": 2},
        )
        self.assertEqual(result.domain_outcome, "SUCCEEDED")

    def test_main_capacity_drift_blocks_without_main_attempt(self):
        empty = {
            "story_load": {"published_today": 0, "pending": 0, "last_published_at": None},
            "main_load": {"published_today": 0, "pending": 0, "last_published_at": None},
        }
        main_exhausted = copy.deepcopy(empty)
        main_exhausted["main_load"]["pending"] = 1
        provider = BoundMainProvider()
        with patch.object(
            workflow, "collect_format_loads", side_effect=[empty, empty, main_exhausted]
        ):
            result = self.run_workflow(
                exceptional_assessment(),
                main_provider=provider,
                cadence_config={"main_target_max_breaking": 1},
            )
        self.assertEqual(result.domain_outcome, "BLOCKED")
        self.assertEqual(self.story_calls, 1)
        self.assertEqual(self.main_calls, [])
        self.assertEqual(provider.calls, 0)

    def test_missing_dependency_recheck_blocks_before_story(self):
        with patch.object(workflow, "run_story_pipeline", side_effect=self.story_result):
            result = workflow.run_breaking_workflow(
                make_trigger(), make_assessment(), workspace=self.workspace,
                state_root=self.state_root, now=NOW, story_writer=object(),
                story_verifier=object(), draft_connector=object(), review_delivery=object(),
                dependency_recheck=None,
            )
        self.assertEqual(result.domain_outcome, "BLOCKED")
        self.assertEqual(self.story_calls, 0)

    def test_dependency_drift_blocks_before_story(self):
        with patch.object(workflow, "run_story_pipeline", side_effect=self.story_result):
            result = workflow.run_breaking_workflow(
                make_trigger(), make_assessment(), workspace=self.workspace,
                state_root=self.state_root, now=NOW, story_writer=object(),
                story_verifier=object(), draft_connector=object(), review_delivery=object(),
                dependency_recheck=lambda _stage: False,
            )
        self.assertEqual(result.domain_outcome, "BLOCKED")
        self.assertEqual(
            result.dispatch_result.record["targets"]["STORY"]["reason_code"],
            "DRAFT_DEPENDENCY_UNAVAILABLE",
        )
        self.assertEqual(self.story_calls, 0)


class IdentityAndProviderBindingTests(BreakingWorkflowTestCase):
    def prior_for(self, assessment, **overrides):
        parsed = workflow.validate_breaking_workflow_input(assessment)
        computed = breaking_identity.compute_identity(parsed.identity_candidate())
        prior = {
            "ref": "prior:synthetic",
            "event_id": computed.event_id,
            "development_id": computed.development_id,
            "identity_basis": computed.identity_basis,
            "candidate_id": "prior-candidate",
        }
        prior.update(overrides)
        return prior

    def test_distinct_event_flows_from_35_into_36(self):
        result = self.run_workflow()
        self.assertEqual(result.identity_result["dedup"]["decision"], "DISTINCT_EVENT")
        self.assertEqual(result.routing_result["routing_decision"], "IMMEDIATE_STORY_DRAFT")

    def test_different_url_same_development_is_same_event(self):
        original = make_assessment()
        prior = self.prior_for(original)
        candidate = make_assessment()
        candidate["candidate_id"] = "candidate-reprint"
        candidate["evidence"][0]["ref"] = "evidence:reprint:1"
        candidate["evidence"][0]["source_url"] = "https://other.invalid/report/product-2"
        candidate["verification"]["evidence_refs"] = ["evidence:reprint:1"]
        result = self.run_workflow(candidate, prior_developments=[prior])
        self.assertEqual(result.identity_result["dedup"]["decision"], "SAME_EVENT")
        self.assertEqual(result.routing_result["routing_decision"], "SUPPRESS_DUPLICATE")

    def test_material_follow_up_with_proven_parent_accelerates(self):
        parent = make_assessment()
        parent["evidence"][0]["supported_claim"] = "Synthetic Product 1 is available."
        parent["evidence"][0]["version"] = "1"
        prior = self.prior_for(parent)
        follow_up = make_assessment()
        follow_up["follow_up_delta"] = {
            "delta_kind": "PRODUCT_VERSION_CHANGED",
            "parent_claim": "Synthetic Product 1 is available.",
            "new_claim": "Synthetic Product 2 is available in Region A.",
            "evidence_ref": "evidence:official:1",
        }
        result = self.run_workflow(follow_up, prior_developments=[prior])
        self.assertEqual(result.identity_result["dedup"]["decision"], "MATERIAL_FOLLOW_UP")
        self.assertEqual(result.routing_result["routing_decision"], "IMMEDIATE_STORY_DRAFT")

    def test_conflicting_exact_identifiers_are_ambiguous(self):
        assessment = make_assessment()
        second = dict(assessment["evidence"][0])
        second["ref"] = "evidence:official:2"
        second["announcement_id"] = "conflicting-release-id"
        assessment["evidence"].append(second)
        assessment["verification"]["evidence_refs"].append("evidence:official:2")
        result = self.run_workflow(assessment)
        self.assertEqual(result.identity_result["dedup"]["decision"], "AMBIGUOUS_IDENTITY")
        self.assertEqual(result.domain_outcome, "BLOCKED")

    def test_same_topic_distinct_version_remains_distinct(self):
        earlier = make_assessment()
        earlier["evidence"][0]["announcement_id"] = None
        earlier["evidence"][0]["version"] = "1"
        prior = self.prior_for(earlier)
        current = make_assessment()
        current["evidence"][0]["announcement_id"] = None
        result = self.run_workflow(current, prior_developments=[prior])
        self.assertEqual(result.identity_result["dedup"]["decision"], "DISTINCT_EVENT")

    def test_duplicate_candidate_suppressed_without_dispatch(self):
        manifest = {
            "schema": "nullone.production.v1",
            "manifest_id": "existing-candidate",
            "candidate_id": "candidate-breaking-1",
            "format": "STORY",
            "review": {"create_attempts": 1, "state": "DRAFT_CREATED", "zernio_draft_id": "existing"},
            "approval": {"first_stage": False, "final_publish": False},
            "publication": {"attempts": 0, "state": "NOT_REQUESTED", "live_zernio_post_id": None},
        }
        (self.workspace / "social/ops/manifests/existing.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        result = self.run_workflow()
        self.assertEqual(result.routing_result["routing_decision"], "SUPPRESS_DUPLICATE")
        self.assertEqual(self.story_calls, 0)
        self.assert_run_outcome_accepted(result)

    def test_explicit_follow_up_without_parent_is_ambiguous(self):
        assessment = make_assessment()
        assessment["follow_up_delta"] = {
            "delta_kind": "PRODUCT_VERSION_CHANGED",
            "parent_claim": "version 1",
            "new_claim": "version 2",
            "evidence_ref": "evidence:official:1",
        }
        result = self.run_workflow(assessment)
        self.assertEqual(result.routing_result["routing_decision"], "BLOCKED_AMBIGUOUS_IDENTITY")
        self.assertEqual(self.story_calls, 0)

    def test_malformed_authoritative_state_blocks(self):
        (self.workspace / "social/state/topic-ledger.jsonl").write_text("{bad\n", encoding="utf-8")
        result = self.run_workflow()
        self.assertEqual(result.domain_outcome, "BLOCKED")
        self.assertEqual(self.story_calls, 0)

    def test_malformed_optional_identity_state_is_typed_block(self):
        result = self.run_workflow(prior_developments=object())
        self.assertEqual(result.reason_code, "IDENTITY_STATE_BLOCKED")
        self.assertEqual(result.domain_outcome, "BLOCKED")
        self.assertTrue(result.reconciliation_required)
        self.assert_run_outcome_accepted(result)

    def test_missing_main_provider_preserves_successful_story(self):
        result = self.run_workflow(exceptional_assessment())
        targets = result.dispatch_result.record["targets"]
        self.assertEqual(targets["STORY"]["status"], "SUCCEEDED")
        self.assertEqual(targets["FEED"]["status"], "BLOCKED_BEFORE_ATTEMPT")
        self.assertEqual(targets["FEED"]["reason_code"], "MAIN_DRAFT_DEPENDENCY_UNAVAILABLE")
        self.assertEqual(self.story_calls, 1)
        self.assertEqual(self.main_calls, [])

    def test_missing_main_verifier_preserves_successful_story(self):
        provider = BoundMainProvider()
        result = self.run_workflow(
            exceptional_assessment(),
            main_provider=provider,
            main_final_verifier=None,
        )
        targets = result.dispatch_result.record["targets"]
        self.assertEqual(targets["STORY"]["status"], "SUCCEEDED")
        self.assertEqual(targets["FEED"]["status"], "BLOCKED_BEFORE_ATTEMPT")
        self.assertEqual(targets["FEED"]["reason_code"], "MAIN_DRAFT_DEPENDENCY_UNAVAILABLE")
        self.assertEqual(provider.calls, 0)

    def assert_main_binding_block(self, provider, reason_code):
        result = self.run_workflow(exceptional_assessment(), main_provider=provider)
        targets = result.dispatch_result.record["targets"]
        self.assertEqual(targets["STORY"]["status"], "SUCCEEDED")
        self.assertEqual(targets["FEED"]["status"], "BLOCKED_BEFORE_ATTEMPT")
        self.assertEqual(targets["FEED"]["reason_code"], reason_code)
        self.assertEqual(result.reason_code, reason_code)
        self.assertEqual(self.story_calls, 1)
        self.assertEqual(self.main_calls, [])
        return result

    def test_wrong_main_candidate_id_blocks_only_main(self):
        provider = BoundMainProvider(mutate=lambda c: c.update(candidate_id="other"))
        self.assert_main_binding_block(provider, "MAIN_CANDIDATE_ID_MISMATCH")

    def test_wrong_main_format_blocks_only_main(self):
        self.assert_main_binding_block(
            BoundMainProvider(fmt="CAROUSEL"), "MAIN_CANDIDATE_FORMAT_MISMATCH"
        )

    def test_zero_main_candidates_blocks_only_main(self):
        self.assert_main_binding_block(BoundMainProvider(count=0), "MAIN_CANDIDATE_UNAVAILABLE")

    def test_multiple_main_candidates_blocks_only_main(self):
        self.assert_main_binding_block(BoundMainProvider(count=2), "MAIN_CANDIDATE_AMBIGUOUS")

    def test_main_candidate_provider_failure_blocks_only_main(self):
        provider = FailingMainProvider()
        self.assert_main_binding_block(provider, "MAIN_CANDIDATE_PROVIDER_FAILED")
        self.assertEqual(provider.calls, 1)

    def test_main_evidence_mismatch_blocks_only_main(self):
        provider = BoundMainProvider(mutate=lambda c: c.update(evidence_refs=["other"]))
        self.assert_main_binding_block(provider, "MAIN_EVIDENCE_BINDING_MISMATCH")

    def test_main_source_mismatch_blocks_only_main(self):
        provider = BoundMainProvider(mutate=lambda c: c.update(source_attribution="other"))
        self.assert_main_binding_block(provider, "MAIN_SOURCE_BINDING_MISMATCH")

    def test_main_lineage_mismatch_blocks_only_main(self):
        provider = BoundMainProvider(mutate=lambda c: c.update(request_lineage="other"))
        self.assert_main_binding_block(provider, "MAIN_LINEAGE_MISMATCH")

    def test_main_invalid_candidate_blocks_only_main(self):
        provider = BoundMainProvider(mutate=lambda c: c.pop("caption_text"))
        self.assert_main_binding_block(provider, "MAIN_CANDIDATE_INVALID")

    def test_story_candidate_lineage_is_draft_set_id(self):
        captured = []

        def story(candidate, **_kwargs):
            captured.append(candidate)
            return FakePipelineResult()

        with patch.object(workflow, "run_story_pipeline", side_effect=story):
            result = workflow.run_breaking_workflow(
                make_trigger(), make_assessment(), workspace=self.workspace,
                state_root=self.state_root, now=NOW, story_writer=object(),
                story_verifier=object(), draft_connector=object(), review_delivery=object(),
                dependency_recheck=lambda _stage: True,
            )
        self.assertEqual(captured[0]["request_lineage"], result.draft_set_id)
        self.assertEqual(captured[0]["candidate_id"], "candidate-breaking-1")
        self.assertEqual(captured[0]["evidence_refs"], ["evidence:official:1"])

    def test_exact_replay_does_not_repeat_completed_target(self):
        first = self.run_workflow()
        second = self.run_workflow()
        self.assertEqual(first.draft_set_id, second.draft_set_id)
        self.assertEqual(self.story_calls, 1)
        self.assertFalse(second.dispatch_result.created)

    def test_replay_after_main_preparation_block_retries_neither_target(self):
        first_provider = BoundMainProvider(count=0)
        first = self.run_workflow(exceptional_assessment(), main_provider=first_provider)
        self.assertEqual(first.dispatch_result.record["targets"]["STORY"]["status"], "SUCCEEDED")
        self.assertEqual(
            first.dispatch_result.record["targets"]["FEED"]["status"],
            "BLOCKED_BEFORE_ATTEMPT",
        )
        self.story_calls = 0
        replay_provider = BoundMainProvider()
        replay = self.run_workflow(exceptional_assessment(), main_provider=replay_provider)
        self.assertEqual(self.story_calls, 0)
        self.assertEqual(replay_provider.calls, 0)
        self.assertEqual(self.main_calls, [])
        self.assertEqual(
            replay.dispatch_result.record["targets"]["FEED"]["status"],
            "BLOCKED_BEFORE_ATTEMPT",
        )

    def test_changed_routing_for_same_development_is_conflict_not_new_set(self):
        first = self.run_workflow()
        self.story_calls = 0
        second = self.run_workflow(
            exceptional_assessment(), main_provider=BoundMainProvider()
        )
        self.assertEqual(second.draft_set_id, first.draft_set_id)
        self.assertEqual(second.reason_code, "DRAFT_SET_CONFLICT")
        self.assertEqual(second.domain_outcome, "UNKNOWN")
        self.assertEqual(self.story_calls, 0)
        self.assertGreater(len(second.reason_text), 240)
        mapped = second.run_outcome_mapping()
        self.assertLessEqual(len(mapped["reason_text"]), 240)
        self.assertNotIn("\n", mapped["reason_text"])
        self.assertNotIn("\r", mapped["reason_text"])
        assessed = self.assert_run_outcome_accepted(second)
        self.assertEqual(assessed["domain_outcome"], "UNKNOWN")

    def test_story_dispatch_in_flight_reentry_requires_reconciliation(self):
        first = self.run_workflow()
        record = breaking_dispatch.load_draft_set(first.draft_set_id)
        breaking_dispatch._update_target(record, "STORY", status="DISPATCH_IN_FLIGHT")
        self.story_calls = 0
        replay = self.run_workflow()
        self.assertEqual(self.story_calls, 0)
        self.assertEqual(replay.domain_outcome, "UNKNOWN")
        self.assertTrue(replay.reconciliation_required)
        self.assert_run_outcome_accepted(replay)

    def test_main_dispatch_in_flight_reentry_does_not_repeat_story_or_main(self):
        provider = BoundMainProvider()
        first = self.run_workflow(exceptional_assessment(), main_provider=provider)
        record = breaking_dispatch.load_draft_set(first.draft_set_id)
        breaking_dispatch._update_target(record, "FEED", status="DISPATCH_IN_FLIGHT")
        self.story_calls = 0
        self.main_calls = []
        replay = self.run_workflow(exceptional_assessment(), main_provider=provider)
        self.assertEqual(self.story_calls, 0)
        self.assertEqual(self.main_calls, [])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(replay.domain_outcome, "UNKNOWN")
        self.assertTrue(replay.reconciliation_required)

    def test_run_outcome_mapping_collapses_newlines_and_bounds_detail(self):
        result = workflow.BreakingWorkflowResult(
            occurrence_id=make_trigger()["occurrence_id"],
            candidate_id="candidate-breaking-1",
            assessment_ref="assessment:synthetic:1",
            identity_result=None,
            routing_result=None,
            draft_set_id=None,
            dispatch_result=None,
            domain_outcome="UNKNOWN",
            reconciliation_required=True,
            reason_code="DRAFT_SET_CONFLICT",
            reason_text=("Detailed conflict\r\nwith reconciliation context " * 20),
        )
        self.assertGreater(len(result.reason_text), 240)
        mapped = result.run_outcome_mapping()
        self.assertLessEqual(len(mapped["reason_text"]), 240)
        self.assertNotIn("\n", mapped["reason_text"])
        self.assertNotIn("\r", mapped["reason_text"])
        self.assert_run_outcome_accepted(result)

    def test_concurrent_replay_creates_at_most_one_story_attempt(self):
        calls = []
        lock = threading.Lock()

        def slow_story(*_args, **_kwargs):
            with lock:
                calls.append(1)
            time.sleep(0.05)
            return FakePipelineResult()

        results = []
        kwargs = dict(
            workspace=self.workspace, state_root=self.state_root, now=NOW,
            story_writer=object(), story_verifier=object(), draft_connector=object(),
            review_delivery=object(), dependency_recheck=lambda _stage: True,
        )

        def invoke():
            results.append(workflow.run_breaking_workflow(make_trigger(), make_assessment(), **kwargs))

        with patch.object(workflow, "run_story_pipeline", side_effect=slow_story):
            threads = [threading.Thread(target=invoke) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(calls, [1])
        self.assertEqual(len({result.draft_set_id for result in results}), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
