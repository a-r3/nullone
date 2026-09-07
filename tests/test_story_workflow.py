#!/usr/bin/env python3
"""Behavioral tests for the #62 StoryWorkflow application orchestrator.

Exercises `nullone_story_workflow.run_story_workflow` against temp-fixture
state roots only. No network, no real Zernio/Telegram/Claude calls --
review-draft creation and review delivery always use injected fakes, and
the trigger is always a locally-constructed `nullone.scheduler-invocation.v1`
value (never a real OpenClaw receipt).
"""
from __future__ import annotations

import json
import shutil
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
from nullone_bridge_common import atomic_write_json, load_manifest, now_iso  # noqa: E402
from nullone_review_delivery import FakeReviewDelivery  # noqa: E402
from nullone_scheduler_invocation import compute_occurrence_id  # noqa: E402
from nullone_story_candidate_provider import StaticStoryCandidateProvider  # noqa: E402
from nullone_story_pipeline import make_fake_verifier  # noqa: E402
from nullone_story_workflow import run_story_workflow  # noqa: E402

REAL_STORY_RENDERER = ROOT / "workspace/social/tools/render_story_v2.py"
BAKU = ZoneInfo("Asia/Baku")
NOW = datetime(2026, 9, 8, 14, 30, 0, tzinfo=BAKU)

# Isolates Story's own gap/pending accounting from Main's independent gap:
# main_target_min default (2) would otherwise always be gapped too (this
# suite never records a main publication), and the cadence contract's
# deterministic reason-selection order checks MAIN before STORY -- without
# this override every NO_ACTION reason_code assertion below would observe
# MAIN's blocker instead of the Story-specific one under test.
STORY_ONLY_CONFIG = {"main_target_min": 0}

BOTH_UNAVAILABLE = {
    "main_quality_candidate_available": False,
    "story_quality_candidate_available": False,
}
STORY_AVAILABLE = {
    "main_quality_candidate_available": False,
    "story_quality_candidate_available": True,
}
MAIN_AVAILABLE = {
    "main_quality_candidate_available": True,
    "story_quality_candidate_available": True,
}


def make_trigger(**overrides) -> dict:
    base = {
        "schema": "nullone.scheduler-invocation.v1",
        "contract_version": "1.0.0",
        "workflow_id": "story",
        "source": "openclaw",
        "external_occurrence_id": "openclaw-occ-story-0001",
        "scheduled_for": "2026-09-08T10:30:00Z",
        "triggered_at": "2026-09-08T10:30:02Z",
    }
    base.update(overrides)
    if "occurrence_id" not in overrides:
        base["occurrence_id"] = compute_occurrence_id(
            base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
        )
    return base


def make_candidate(**overrides) -> dict:
    base = {
        "candidate_id": "cand-1",
        "topic": "Test topic",
        "topic_cluster": "test",
        "content_type": "NEWS",
        "verification": "PASS",
        "evidence_refs": ["Test evidence."],
        "source_attribution": "Test source",
        "factual_inputs": {},
    }
    base.update(overrides)
    return base


DEFAULT_SPEC = {
    "layout": "big-stat",
    "headline": "Test headline",
    "body": "Body.",
    "stat": "1",
    "source_name": "Test",
    "use_source_image": False,
    "cta": "@nullone.az",
}


def writer(_context):
    return dict(DEFAULT_SPEC)


class ExplodingCandidateProvider:
    """Fails the test loudly if ever called -- used to prove a code path
    that must be entirely side-effect free at the candidate-provider layer."""

    def get_candidates(self):
        raise AssertionError("candidate provider must not be called on this path")


class RecordingDraftConnector:
    def __init__(self, delay: float = 0.0):
        self.calls = 0
        self._delay = delay

    def create_review_draft(self, manifest_path: Path) -> None:
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        _, manifest = load_manifest(manifest_path)
        manifest["review"]["create_attempts"] = 1
        manifest["review"]["state"] = "DRAFT_CREATED"
        manifest["review"]["zernio_draft_id"] = f"review-{self.calls}"
        manifest["review"]["created_at"] = now_iso()
        atomic_write_json(manifest_path, manifest)


class StoryWorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self._patcher = patch.object(bridge_common, "WORKSPACE", self.tmp_path)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._tmpdir_ctx.cleanup)

        self.state_root = self.tmp_path / "social"
        self.state_root.mkdir(parents=True, exist_ok=True)

        tools_dir = self.tmp_path / "social/tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(REAL_STORY_RENDERER, tools_dir / "render_story_v2.py")

    def run_workflow(
        self,
        trigger=None,
        *,
        candidate_availability=STORY_AVAILABLE,
        candidate_provider=None,
        draft_connector=None,
        review_delivery=None,
        cadence_config=STORY_ONLY_CONFIG,
        verifier=None,
    ):
        trigger = trigger if trigger is not None else make_trigger()
        candidate_provider = candidate_provider or StaticStoryCandidateProvider([make_candidate()])
        draft_connector = draft_connector or RecordingDraftConnector()
        review_delivery = review_delivery if review_delivery is not None else FakeReviewDelivery(status="SENT")
        return run_story_workflow(
            trigger,
            state_root=self.state_root,
            now=NOW,
            candidate_availability=candidate_availability,
            candidate_provider=candidate_provider,
            writer=writer,
            verifier=verifier or make_fake_verifier("PASS"),
            draft_connector=draft_connector,
            review_delivery=review_delivery,
            cadence_config=cadence_config,
        )


# ---------------------------------------------------------------------------
# Trigger boundary
# ---------------------------------------------------------------------------


class TriggerBoundaryTests(StoryWorkflowTestCase):
    def test_malformed_trigger_is_rejected_zero_side_effects(self):
        result = self.run_workflow(
            trigger={"workflow_id": "story"},
            candidate_provider=ExplodingCandidateProvider(),
        )
        self.assertEqual(result.outcome, "TRIGGER_REJECTED")

    def test_wrong_workflow_id_is_rejected(self):
        result = self.run_workflow(
            trigger=make_trigger(workflow_id="breaking"),
            candidate_provider=ExplodingCandidateProvider(),
        )
        self.assertEqual(result.outcome, "TRIGGER_REJECTED")

    def test_unknown_field_is_rejected(self):
        trigger = make_trigger()
        trigger["extra_field"] = "nope"
        result = self.run_workflow(trigger=trigger, candidate_provider=ExplodingCandidateProvider())
        self.assertEqual(result.outcome, "TRIGGER_REJECTED")

    def test_occurrence_id_mismatch_is_rejected(self):
        trigger = make_trigger()
        trigger["occurrence_id"] = "occ_" + "0" * 24
        result = self.run_workflow(trigger=trigger, candidate_provider=ExplodingCandidateProvider())
        self.assertEqual(result.outcome, "TRIGGER_REJECTED")

    def test_non_canonical_timestamp_is_rejected(self):
        trigger = make_trigger(scheduled_for="2026-09-08 10:30:00")
        result = self.run_workflow(trigger=trigger, candidate_provider=ExplodingCandidateProvider())
        self.assertEqual(result.outcome, "TRIGGER_REJECTED")

    def test_replay_with_different_triggered_at_keeps_same_occurrence_and_is_accepted(self):
        first = make_trigger()
        second = make_trigger(triggered_at="2026-09-08T10:35:00Z")
        self.assertEqual(first["occurrence_id"], second["occurrence_id"])
        # Both are individually acceptable triggers (occurrence identity is
        # unaffected by triggered_at) -- accept the second on its own state
        # root to avoid entangling this with cadence pending-suppression.
        result = self.run_workflow(trigger=second)
        self.assertEqual(result.outcome, "DRAFT_CREATED")


# ---------------------------------------------------------------------------
# Section 21: no-action / blocked paths must be entirely side-effect free
# ---------------------------------------------------------------------------


class NoActionSideEffectFreeTests(StoryWorkflowTestCase):
    def test_missing_state_root_is_state_blocked(self):
        missing_root = self.tmp_path / "no-such-social"
        result = run_story_workflow(
            make_trigger(),
            state_root=missing_root,
            now=NOW,
            candidate_availability=STORY_AVAILABLE,
            candidate_provider=ExplodingCandidateProvider(),
            writer=writer,
            verifier=make_fake_verifier("PASS"),
            draft_connector=RecordingDraftConnector(),
            review_delivery=FakeReviewDelivery(status="SENT"),
            cadence_config=STORY_ONLY_CONFIG,
        )
        self.assertEqual(result.outcome, "STATE_BLOCKED")

    def test_cadence_no_quality_candidate_is_no_action_zero_calls(self):
        connector = RecordingDraftConnector()
        sender = FakeReviewDelivery(status="SENT")
        result = self.run_workflow(
            candidate_availability=BOTH_UNAVAILABLE,
            candidate_provider=ExplodingCandidateProvider(),
            draft_connector=connector,
            review_delivery=sender,
        )
        self.assertEqual(result.outcome, "NO_ACTION")
        self.assertEqual(result.cadence["reason_code"], "NO_QUALITY_CANDIDATE")
        self.assertEqual(connector.calls, 0)
        self.assertEqual(len(sender.sent), 0)

    def test_prepare_main_candidate_is_no_action_zero_story_side_effects(self):
        connector = RecordingDraftConnector()
        sender = FakeReviewDelivery(status="SENT")
        result = self.run_workflow(
            candidate_availability=MAIN_AVAILABLE,
            candidate_provider=ExplodingCandidateProvider(),
            draft_connector=connector,
            review_delivery=sender,
            cadence_config=None,  # allow Main's own default gap (target_min=2)
        )
        self.assertEqual(result.outcome, "NO_ACTION")
        self.assertEqual(result.cadence["recommendation"], "PREPARE_MAIN_CANDIDATE")
        self.assertEqual(connector.calls, 0)
        self.assertEqual(len(sender.sent), 0)

    def test_malformed_manifest_state_fails_closed_zero_calls(self):
        manifests_dir = self.state_root / "ops/manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        (manifests_dir / "corrupt.json").write_text("{not json", encoding="utf-8")

        connector = RecordingDraftConnector()
        sender = FakeReviewDelivery(status="SENT")
        result = self.run_workflow(
            candidate_provider=ExplodingCandidateProvider(),
            draft_connector=connector,
            review_delivery=sender,
        )
        self.assertEqual(result.outcome, "STATE_BLOCKED")
        self.assertEqual(connector.calls, 0)
        self.assertEqual(len(sender.sent), 0)

    def test_decision_relevant_unresolved_ledger_row_fails_closed(self):
        ledger_path = self.state_root / "state/publish-ledger.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        # PUBLISHED today, no format, no manifest linkage -> unresolved AND
        # decision-relevant (issue #60 semantics) -> CadenceStateError.
        row = {
            "result": "PUBLISHED",
            "timestamp": NOW.isoformat(),
        }
        ledger_path.write_text(json.dumps(row) + "\n", encoding="utf-8")

        connector = RecordingDraftConnector()
        result = self.run_workflow(candidate_provider=ExplodingCandidateProvider(), draft_connector=connector)
        self.assertEqual(result.outcome, "STATE_BLOCKED")
        self.assertEqual(connector.calls, 0)


# ---------------------------------------------------------------------------
# Section 22: candidate boundary
# ---------------------------------------------------------------------------


class CandidateBoundaryTests(StoryWorkflowTestCase):
    def test_zero_candidates_is_candidate_unavailable(self):
        result = self.run_workflow(candidate_provider=StaticStoryCandidateProvider([]))
        self.assertEqual(result.outcome, "CANDIDATE_UNAVAILABLE")

    def test_exactly_one_valid_pass_candidate_proceeds(self):
        result = self.run_workflow(candidate_provider=StaticStoryCandidateProvider([make_candidate()]))
        self.assertEqual(result.outcome, "DRAFT_CREATED")

    def test_multiple_candidates_no_ordering_is_ambiguous(self):
        result = self.run_workflow(
            candidate_provider=StaticStoryCandidateProvider(
                [make_candidate(candidate_id="a"), make_candidate(candidate_id="b")]
            )
        )
        self.assertEqual(result.outcome, "CANDIDATE_AMBIGUOUS")

    def test_verification_not_pass_is_candidate_invalid(self):
        result = self.run_workflow(
            candidate_provider=StaticStoryCandidateProvider([make_candidate(verification="PARTIAL")])
        )
        self.assertEqual(result.outcome, "CANDIDATE_INVALID")

    def test_missing_evidence_refs_is_candidate_invalid(self):
        result = self.run_workflow(
            candidate_provider=StaticStoryCandidateProvider([make_candidate(evidence_refs=[])])
        )
        self.assertEqual(result.outcome, "CANDIDATE_INVALID")

    def test_malformed_candidate_is_candidate_invalid(self):
        result = self.run_workflow(candidate_provider=StaticStoryCandidateProvider([{"not": "a candidate"}]))
        self.assertEqual(result.outcome, "CANDIDATE_INVALID")

    def test_candidate_provider_failure_is_typed_not_crashed(self):
        class FailingProvider:
            def get_candidates(self):
                raise RuntimeError("upstream unavailable")

        result = self.run_workflow(candidate_provider=FailingProvider())
        self.assertEqual(result.outcome, "CANDIDATE_PROVIDER_FAILED")
        self.assertEqual(result.context["error_type"], "RuntimeError")

    def test_cadence_says_available_but_provider_returns_none_is_truthful_block(self):
        # Section 9: candidate-availability consistency. Cadence recommends
        # PREPARE_STORY (candidate_availability said True), but the
        # provider itself returns zero -- never fabricate content.
        result = self.run_workflow(
            candidate_availability=STORY_AVAILABLE,
            candidate_provider=StaticStoryCandidateProvider([]),
        )
        self.assertEqual(result.outcome, "CANDIDATE_UNAVAILABLE")
        self.assertEqual(result.cadence["recommendation"], "PREPARE_STORY")

    def test_one_invalid_one_valid_still_selects_the_valid_one(self):
        result = self.run_workflow(
            candidate_provider=StaticStoryCandidateProvider(
                [
                    make_candidate(candidate_id="bad", verification="PARTIAL"),
                    make_candidate(candidate_id="good"),
                ]
            )
        )
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        self.assertEqual(result.candidate_id, "good")


# ---------------------------------------------------------------------------
# Section 24: replay / concurrency
# ---------------------------------------------------------------------------


class ReplayAndConcurrencyTests(StoryWorkflowTestCase):
    def test_same_trigger_replay_after_draft_created_is_no_action_pending(self):
        sender = FakeReviewDelivery(status="SENT")
        connector = RecordingDraftConnector()
        first = self.run_workflow(draft_connector=connector, review_delivery=sender)
        self.assertEqual(first.outcome, "DRAFT_CREATED")

        second = self.run_workflow(
            candidate_provider=ExplodingCandidateProvider(),
            draft_connector=connector,
            review_delivery=sender,
        )
        self.assertEqual(second.outcome, "NO_ACTION")
        self.assertEqual(second.cadence["reason_code"], "PENDING_STORY_EXISTS")
        self.assertEqual(connector.calls, 1)
        self.assertEqual(len(sender.sent), 1)

    def test_delivery_failure_never_recreates_draft(self):
        connector = RecordingDraftConnector()
        failing_sender = FakeReviewDelivery(status="FAILED", error="fake failure")
        first = self.run_workflow(draft_connector=connector, review_delivery=failing_sender)
        self.assertEqual(first.outcome, "PREVIEW_DELIVERY_FAILED")
        self.assertEqual(connector.calls, 1)

        second = self.run_workflow(
            candidate_provider=ExplodingCandidateProvider(),
            draft_connector=connector,
            review_delivery=failing_sender,
        )
        # Same real-state suppression applies: a pending draft (delivery
        # failure does not un-pend it) blocks a repeat PREPARE_STORY.
        self.assertEqual(second.outcome, "NO_ACTION")
        self.assertEqual(connector.calls, 1)

    def test_ambiguous_draft_outcome_never_auto_retries(self):
        class AmbiguousConnector:
            def __init__(self):
                self.calls = 0

            def create_review_draft(self, manifest_path: Path) -> None:
                self.calls += 1
                _, manifest = load_manifest(manifest_path)
                manifest["review"]["create_attempts"] = 1
                manifest["review"]["state"] = "REVIEW_UNKNOWN"
                atomic_write_json(manifest_path, manifest)

        connector = AmbiguousConnector()
        first = self.run_workflow(draft_connector=connector)
        self.assertEqual(first.outcome, "REVIEW_DRAFT_AMBIGUOUS")

        second = self.run_workflow(draft_connector=connector)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 1)

    def test_concurrent_invocations_for_same_candidate_call_connector_once(self):
        # Both threads read cadence state before either has written a
        # manifest, so both may see PREPARE_STORY -- #33's own per-request
        # fcntl lock (reused unchanged) still ensures only one draft.
        connector = RecordingDraftConnector(delay=0.1)
        sender = FakeReviewDelivery(status="SENT")
        results = []

        def invoke():
            results.append(
                self.run_workflow(draft_connector=connector, review_delivery=sender)
            )

        t1 = threading.Thread(target=invoke)
        t2 = threading.Thread(target=invoke)
        t1.start()
        time.sleep(0.02)
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())

        self.assertEqual(connector.calls, 1)
        outcomes = {r.outcome for r in results}
        self.assertTrue(outcomes.issubset({"DRAFT_CREATED", "REVIEW_DRAFT_ALREADY_CONSUMED", "NO_ACTION"}))
        self.assertIn("DRAFT_CREATED", outcomes)
        self.assertEqual(len(sender.sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
