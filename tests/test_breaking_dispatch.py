#!/usr/bin/env python3
"""Behavioral tests for #36 durable draft-set reservation and dispatch.

Exercises nullone_breaking_dispatch against temp-fixture workspaces only.
Story/Main runners are always fakes here -- #33/#36 pipeline behavior is
covered by their own dedicated test files. This file focuses on: draft-set
identity/idempotence, conflict-on-mismatch, concurrency, Story-first
dispatch order, and independent-but-safety-linked target progress.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_breaking_dispatch as dispatch  # noqa: E402
import nullone_breaking_identity as identity  # noqa: E402


class FakeResult:
    def __init__(self, outcome, manifest_id=None, review_post_id=None, reason_code=None):
        self.outcome = outcome
        self.manifest_id = manifest_id
        self.review_post_id = review_post_id
        self.reason_code = reason_code


def always_permit(*, stage, record):
    """A trivial always-PASS AuthoritativeDispatchRecheck fake.

    Most tests here focus on draft-set identity/idempotence, Story-first
    ordering and target-progress bookkeeping -- not on fresh-state
    suppression -- so they use this fake to satisfy the now-mandatory
    `authoritative_recheck` parameter. Fresh-state blocking behavior itself
    is covered by FreshStateAuthoritativeRecheckTests below, including
    against the real `make_state_authoritative_recheck`.
    """

    return dispatch.RecheckResult(permitted=True)


def always_capacity_ok() -> bool:
    return True


def routing_result(
    *,
    decision="IMMEDIATE_STORY_DRAFT",
    targets=("STORY",),
    event_id="event-1",
    development_id="dev-1",
    candidate_id="cand-1",
    reason_code="MATERIAL_TIME_VALUE",
    main_justification=None,
):
    return {
        "schema": "nullone.breaking-routing.v1",
        "candidate_id": candidate_id,
        "assessment_ref": "assess-1",
        "state_snapshot_ref": "state-1",
        "severity": "MATERIAL_BREAKING" if len(targets) == 1 else "EXCEPTIONAL_BREAKING",
        "event": {
            "event_id": event_id,
            "development_id": development_id,
            "topic_id": "topic-1",
            "identity_basis": "EXACT_IDENTIFIER",
            "identity_refs": ["ref-1"],
        },
        "verification": {"state": "PASS", "evidence_refs": ["ev-1"]},
        "dedup": {
            "decision": "DISTINCT_EVENT",
            "matched_refs": [],
            "parent_development_id": None,
            "follow_up_reason": None,
        },
        "routing_decision": decision,
        "reason_code": reason_code,
        "reason_text": "test",
        "draft_targets": list(targets),
        "main_draft_justification": main_justification,
        "reconciliation_required": False,
    }


class DispatchTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self._patcher = patch.object(bridge_common, "WORKSPACE", self.tmp_path)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._tmpdir_ctx.cleanup)


class DraftSetIdentityTests(DispatchTestCase):
    def test_same_development_same_set_id(self):
        a = dispatch.compute_draft_set_id("event-1", "dev-1")
        b = dispatch.compute_draft_set_id("event-1", "dev-1")
        self.assertEqual(a, b)

    def test_different_development_different_set_id(self):
        a = dispatch.compute_draft_set_id("event-1", "dev-1")
        b = dispatch.compute_draft_set_id("event-1", "dev-2")
        self.assertNotEqual(a, b)

    def test_target_format_not_part_of_identity(self):
        # Same development, differently-targeted requests must compute the
        # SAME draft_set_id -- identity excludes target format so an
        # escalated/reformatted request can never mint a second set.
        rr1 = routing_result(targets=("STORY",))
        rr2 = routing_result(
            decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "CAROUSEL")
        )
        id1 = dispatch.compute_draft_set_id(rr1["event"]["event_id"], rr1["event"]["development_id"])
        id2 = dispatch.compute_draft_set_id(rr2["event"]["event_id"], rr2["event"]["development_id"])
        self.assertEqual(id1, id2)


class ReservationIdempotenceTests(DispatchTestCase):
    def test_replay_reuses_existing_set(self):
        rr = routing_result()
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")
        with dispatch.draft_set_lock(set_id):
            record1, created1 = dispatch.reserve_draft_set(rr)
            record2, created2 = dispatch.reserve_draft_set(rr)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(record1["draft_set_id"], record2["draft_set_id"])

    def test_different_source_same_development_conflicts_not_silently_reused(self):
        # #36 hardening section 12 (BLOCKER): exact replay is bound to the
        # exact immutable accepted decision -- same event/development + same
        # target set but a DIFFERENT candidate/decision must fail closed,
        # never silently reuse the existing PENDING set under the new
        # candidate's identity.
        rr1 = routing_result(candidate_id="cand-a")
        rr2 = routing_result(candidate_id="cand-b")  # different source/candidate, same development
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")
        with dispatch.draft_set_lock(set_id):
            record1, created1 = dispatch.reserve_draft_set(rr1)
            self.assertTrue(created1)
            with self.assertRaises(dispatch.DraftSetConflict):
                dispatch.reserve_draft_set(rr2)
        # The set stays bound to whichever candidate reserved it first --
        # never overwritten by the conflicting attempt.
        reloaded = dispatch.load_draft_set(set_id)
        self.assertEqual(reloaded["candidate_id"], "cand-a")

    def test_exact_replay_of_identical_decision_reuses_set(self):
        rr = routing_result(candidate_id="cand-a")
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")
        with dispatch.draft_set_lock(set_id):
            record1, created1 = dispatch.reserve_draft_set(rr)
            record2, created2 = dispatch.reserve_draft_set(rr)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(record1["draft_set_id"], record2["draft_set_id"])
        self.assertEqual(record2["decision_hash"], record1["decision_hash"])

    def test_severity_escalation_same_development_no_added_main_target(self):
        material = routing_result(targets=("STORY",))
        exceptional = routing_result(
            decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "CAROUSEL")
        )
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")
        with dispatch.draft_set_lock(set_id):
            dispatch.reserve_draft_set(material)
            with self.assertRaises(dispatch.DraftSetConflict):
                dispatch.reserve_draft_set(exceptional)

    def test_target_format_change_same_development_conflict(self):
        feed = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        carousel = routing_result(
            decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "CAROUSEL")
        )
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")
        with dispatch.draft_set_lock(set_id):
            dispatch.reserve_draft_set(feed)
            with self.assertRaises(dispatch.DraftSetConflict):
                dispatch.reserve_draft_set(carousel)

    def test_reserve_rejects_non_accelerated_decision(self):
        rr = routing_result(decision="NORMAL_QUEUE", targets=())
        with self.assertRaises(dispatch.DispatchRejected):
            dispatch.reserve_draft_set(rr)

    def test_concurrent_same_development_creates_one_set(self):
        rr = routing_result()
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")
        results = []
        errors = []

        def worker():
            try:
                with dispatch.draft_set_lock(set_id):
                    _, created = dispatch.reserve_draft_set(rr)
                    results.append(created)
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for c in results if c), 1)
        self.assertEqual(sum(1 for c in results if not c), 7)


class StoryFirstDispatchTests(DispatchTestCase):
    def test_story_succeeds_main_may_proceed(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "CAROUSEL"))
        story_calls = []
        main_calls = []

        def story_runner():
            story_calls.append(1)
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):
            main_calls.append(fmt)
            return FakeResult("DRAFT_CREATED", manifest_id="m-main", review_post_id="r-main")

        result = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        self.assertEqual(len(story_calls), 1)
        self.assertEqual(main_calls, ["CAROUSEL"])
        self.assertEqual(result.record["targets"]["STORY"]["status"], "SUCCEEDED")
        self.assertEqual(result.record["targets"]["CAROUSEL"]["status"], "SUCCEEDED")

    def test_story_blocked_before_attempt_main_never_attempted(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        main_calls = []

        def story_runner():
            return FakeResult("CANDIDATE_NOT_ELIGIBLE", reason_code="CANDIDATE_NOT_ELIGIBLE")

        def main_runner(fmt):
            main_calls.append(fmt)
            return FakeResult("DRAFT_CREATED")

        result = dispatch.dispatch_draft_set(
            rr, story_runner=story_runner, main_runner=main_runner, authoritative_recheck=always_permit
        )
        self.assertEqual(main_calls, [])
        self.assertEqual(result.record["targets"]["STORY"]["status"], "BLOCKED_BEFORE_ATTEMPT")
        self.assertIn("FEED", result.record["targets"])
        self.assertEqual(result.record["targets"]["FEED"]["status"], "PENDING")

    def test_story_ambiguous_main_never_attempted(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        main_calls = []

        def story_runner():
            return FakeResult("REVIEW_DRAFT_AMBIGUOUS", reason_code="REVIEW_DRAFT_AMBIGUOUS")

        def main_runner(fmt):
            main_calls.append(fmt)
            return FakeResult("DRAFT_CREATED")

        result = dispatch.dispatch_draft_set(
            rr, story_runner=story_runner, main_runner=main_runner, authoritative_recheck=always_permit
        )
        self.assertEqual(main_calls, [])
        self.assertEqual(result.record["targets"]["STORY"]["status"], "AMBIGUOUS")
        self.assertTrue(result.reconciliation_required)

    def test_story_succeeds_main_blocked_before_attempt_story_not_repeated(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "CAROUSEL"))
        story_calls = []

        def story_runner():
            story_calls.append(1)
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):
            return FakeResult("CANDIDATE_NOT_ELIGIBLE", reason_code="CANDIDATE_NOT_ELIGIBLE")

        result1 = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        self.assertEqual(result1.record["targets"]["STORY"]["status"], "SUCCEEDED")
        self.assertEqual(result1.record["targets"]["CAROUSEL"]["status"], "BLOCKED_BEFORE_ATTEMPT")

        # Replay: Story must never be repeated.
        result2 = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        self.assertEqual(len(story_calls), 1)
        self.assertEqual(result2.record["targets"]["STORY"]["status"], "SUCCEEDED")

    def test_story_succeeds_main_ambiguous_story_not_repeated_main_not_retried(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        story_calls = []
        main_calls = []

        def story_runner():
            story_calls.append(1)
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):
            main_calls.append(fmt)
            return FakeResult("REVIEW_DRAFT_AMBIGUOUS", reason_code="REVIEW_DRAFT_AMBIGUOUS")

        result1 = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        self.assertTrue(result1.reconciliation_required)
        self.assertEqual(result1.record["targets"]["FEED"]["status"], "AMBIGUOUS")

        result2 = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        self.assertEqual(len(story_calls), 1)
        self.assertEqual(len(main_calls), 1)
        self.assertEqual(result2.record["targets"]["FEED"]["status"], "AMBIGUOUS")

    def test_story_only_set_never_attempts_main(self):
        rr = routing_result(targets=("STORY",))

        def story_runner():
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):  # pragma: no cover - must never be called
            raise AssertionError("main_runner must not be called for a Story-only set")

        result = dispatch.dispatch_draft_set(
            rr, story_runner=story_runner, main_runner=main_runner, authoritative_recheck=always_permit
        )
        self.assertIsNone(result.main_target)
        self.assertNotIn("FEED", result.record["targets"])
        self.assertNotIn("CAROUSEL", result.record["targets"])

    def test_replay_after_both_succeeded_repeats_nothing(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "CAROUSEL"))
        story_calls = []
        main_calls = []

        def story_runner():
            story_calls.append(1)
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):
            main_calls.append(fmt)
            return FakeResult("DRAFT_CREATED", manifest_id="m-main", review_post_id="r-main")

        kwargs = dict(
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        dispatch.dispatch_draft_set(rr, **kwargs)
        dispatch.dispatch_draft_set(rr, **kwargs)
        dispatch.dispatch_draft_set(rr, **kwargs)

        self.assertEqual(len(story_calls), 1)
        self.assertEqual(len(main_calls), 1)

    def test_main_capacity_recheck_blocks_before_attempt(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        main_calls = []

        def story_runner():
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):
            main_calls.append(fmt)
            return FakeResult("DRAFT_CREATED")

        result = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=lambda: False,
        )
        self.assertEqual(main_calls, [])
        self.assertEqual(result.record["targets"]["FEED"]["status"], "BLOCKED_BEFORE_ATTEMPT")

    def test_main_capacity_recheck_missing_blocks_never_interpreted_as_pass(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        main_calls = []

        def story_runner():
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):
            main_calls.append(fmt)
            return FakeResult("DRAFT_CREATED")

        result = dispatch.dispatch_draft_set(
            rr, story_runner=story_runner, main_runner=main_runner, authoritative_recheck=always_permit
        )
        self.assertEqual(main_calls, [])
        self.assertEqual(result.record["targets"]["FEED"]["status"], "BLOCKED_BEFORE_ATTEMPT")
        self.assertEqual(result.record["targets"]["FEED"]["reason_code"], "MAIN_CAPACITY_RECHECK_MISSING")


class DomainOutcomeMappingTests(DispatchTestCase):
    def test_normal_queue_is_healthy_no_action(self):
        outcome, code, text = dispatch.dispatch_domain_outcome(
            routing_result(decision="NORMAL_QUEUE", targets=()), None
        )
        self.assertEqual(outcome, "SUCCEEDED")

    def test_suppress_duplicate_without_reconciliation_is_healthy(self):
        rr = routing_result(decision="SUPPRESS_DUPLICATE", targets=())
        rr["reconciliation_required"] = False
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, None)
        self.assertEqual(outcome, "SUCCEEDED")

    def test_suppress_duplicate_with_reconciliation_is_blocked(self):
        rr = routing_result(decision="SUPPRESS_DUPLICATE", targets=())
        rr["reconciliation_required"] = True
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, None)
        self.assertEqual(outcome, "BLOCKED")

    def test_blocked_ambiguous_identity_is_blocked(self):
        rr = routing_result(decision="BLOCKED_AMBIGUOUS_IDENTITY", targets=())
        rr["reconciliation_required"] = True
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, None)
        self.assertEqual(outcome, "BLOCKED")

    def test_accelerated_without_dispatch_is_unknown(self):
        rr = routing_result()
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, None)
        self.assertEqual(outcome, "UNKNOWN")

    def test_accelerated_all_succeeded_is_succeeded(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "CAROUSEL"))

        def story_runner():
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):
            return FakeResult("DRAFT_CREATED", manifest_id="m-main", review_post_id="r-main")

        dr = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, dr)
        self.assertEqual(outcome, "SUCCEEDED")

    def test_accelerated_ambiguous_target_is_unknown(self):
        rr = routing_result(targets=("STORY",))

        def story_runner():
            return FakeResult("REVIEW_DRAFT_AMBIGUOUS", reason_code="REVIEW_DRAFT_AMBIGUOUS")

        dr = dispatch.dispatch_draft_set(rr, story_runner=story_runner, authoritative_recheck=always_permit)
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, dr)
        self.assertEqual(outcome, "UNKNOWN")

    def test_accelerated_blocked_target_is_blocked(self):
        rr = routing_result(targets=("STORY",))

        def story_runner():
            return FakeResult("CANDIDATE_NOT_ELIGIBLE", reason_code="CANDIDATE_NOT_ELIGIBLE")

        dr = dispatch.dispatch_draft_set(rr, story_runner=story_runner, authoritative_recheck=always_permit)
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, dr)
        self.assertEqual(outcome, "BLOCKED")

    def test_story_preview_delivery_failed_is_failed_not_succeeded(self):
        rr = routing_result(targets=("STORY",))

        def story_runner():
            return FakeResult("PREVIEW_DELIVERY_FAILED", manifest_id="m-story", review_post_id="r-story")

        dr = dispatch.dispatch_draft_set(rr, story_runner=story_runner, authoritative_recheck=always_permit)
        self.assertEqual(dr.record["targets"]["STORY"]["status"], "PREVIEW_DELIVERY_FAILED")
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, dr)
        self.assertEqual(outcome, "FAILED")
        self.assertEqual(code, "PREVIEW_DELIVERY_FAILED")

    def test_main_preview_delivery_failed_is_failed_not_succeeded(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))

        def story_runner():
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):
            return FakeResult("PREVIEW_DELIVERY_FAILED", manifest_id="m-main", review_post_id="r-main")

        dr = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        self.assertEqual(dr.record["targets"]["FEED"]["status"], "PREVIEW_DELIVERY_FAILED")
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, dr)
        self.assertEqual(outcome, "FAILED")
        self.assertEqual(code, "PREVIEW_DELIVERY_FAILED")


class InFlightCrashReplayTests(DispatchTestCase):
    def test_in_flight_written_before_runner_invoked(self):
        rr = routing_result(targets=("STORY",))
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")
        seen_status = {}

        def story_runner():
            record = dispatch.load_draft_set(set_id)
            seen_status["status"] = record["targets"]["STORY"]["status"]
            return FakeResult("DRAFT_CREATED", manifest_id="m-1", review_post_id="r-1")

        dispatch.dispatch_draft_set(rr, story_runner=story_runner, authoritative_recheck=always_permit)
        self.assertEqual(seen_status["status"], "DISPATCH_IN_FLIGHT")

    def test_runner_exception_before_side_effect_is_ambiguous_never_replayed(self):
        rr = routing_result(targets=("STORY",))
        calls = []

        def failing_runner():
            calls.append(1)
            raise RuntimeError("boom before any side effect")

        result = dispatch.dispatch_draft_set(
            rr, story_runner=failing_runner, authoritative_recheck=always_permit
        )
        self.assertEqual(result.record["targets"]["STORY"]["status"], "AMBIGUOUS")
        self.assertEqual(result.record["targets"]["STORY"]["reason_code"], "RuntimeError")
        self.assertTrue(result.reconciliation_required)

        # Replay must never call the runner again -- no silent replay.
        def would_succeed():  # pragma: no cover - must never run
            calls.append("replayed")
            return FakeResult("DRAFT_CREATED")

        result2 = dispatch.dispatch_draft_set(
            rr, story_runner=would_succeed, authoritative_recheck=always_permit
        )
        self.assertEqual(calls, [1])
        self.assertEqual(result2.record["targets"]["STORY"]["status"], "AMBIGUOUS")

    def test_runner_exception_after_side_effect_no_sibling_compensation(self):
        # Simulates scenario B (draft creation then raise): the runner's
        # side effect happened, but the exception still means the
        # dispatcher cannot trust the state it returned to it.
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        create_count = {"n": 0}
        main_calls = []

        def story_runner_creates_then_raises():
            create_count["n"] += 1
            raise RuntimeError("crashed after external side effect")

        def main_runner(fmt):  # pragma: no cover - must never run
            main_calls.append(fmt)
            return FakeResult("DRAFT_CREATED")

        result = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner_creates_then_raises,
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        self.assertEqual(result.record["targets"]["STORY"]["status"], "AMBIGUOUS")
        self.assertTrue(result.reconciliation_required)
        self.assertEqual(create_count["n"], 1, "at most one external create action")
        self.assertEqual(main_calls, [], "no sibling compensation for an ambiguous Story")

    def test_malformed_runner_result_is_ambiguous_not_blocked_before_attempt(self):
        rr = routing_result(targets=("STORY",))

        def malformed_runner():
            return {"not": "a recognized result object"}

        result = dispatch.dispatch_draft_set(
            rr, story_runner=malformed_runner, authoritative_recheck=always_permit
        )
        self.assertEqual(result.record["targets"]["STORY"]["status"], "AMBIGUOUS")
        self.assertTrue(result.reconciliation_required)

    def test_existing_in_flight_on_restart_requires_reconciliation_no_auto_invoke(self):
        rr = routing_result(targets=("STORY",))
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")

        with dispatch.draft_set_lock(set_id):
            record, _ = dispatch.reserve_draft_set(rr)
            dispatch._update_target(record, "STORY", status="DISPATCH_IN_FLIGHT")

        calls = []

        def must_not_run():  # pragma: no cover - must never run
            calls.append(1)
            return FakeResult("DRAFT_CREATED")

        result = dispatch.dispatch_draft_set(rr, story_runner=must_not_run, authoritative_recheck=always_permit)
        self.assertEqual(calls, [])
        self.assertTrue(result.reconciliation_required)
        self.assertEqual(result.record["targets"]["STORY"]["status"], "DISPATCH_IN_FLIGHT")


class FreshStateAuthoritativeRecheckTests(DispatchTestCase):
    """#36 hardening section 9: reuses #35 (`nullone_breaking_identity`)
    verbatim via `make_state_authoritative_recheck` -- never forks its
    matching semantics.
    """

    def _init_empty_state(self):
        (self.tmp_path / "social" / "ops" / "manifests").mkdir(parents=True, exist_ok=True)
        state_dir = self.tmp_path / "social" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "publish-ledger.jsonl").touch()
        (state_dir / "topic-ledger.jsonl").touch()
        (state_dir / "candidate-queue.md").touch()

    def _write_manifest(self, candidate_id, **review_overrides):
        manifest_dir = self.tmp_path / "social" / "ops" / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        review = {
            "create_attempts": 0,
            "state": "NOT_CREATED",
            "zernio_draft_id": None,
            "created_at": None,
        }
        review.update(review_overrides)
        manifest = {
            "schema": "nullone.production.v1",
            "manifest_id": f"manifest-{candidate_id}",
            "candidate_id": candidate_id,
            "review": review,
            "publication": {"attempts": 0, "state": "NOT_REQUESTED"},
            "approval": {"first_stage": False},
        }
        (manifest_dir / f"manifest-{candidate_id}.json").write_text(
            __import__("json").dumps(manifest), encoding="utf-8"
        )
        return manifest

    def _candidate_input(self, candidate_id="cand-1"):
        return identity.CandidateInput(
            candidate_id=candidate_id,
            assessment_ref=f"assess:{candidate_id}",
            state_snapshot_ref=f"state:{candidate_id}",
            topic_cluster="topic-cluster-1",
            evidence=(
                identity.EvidenceItem(
                    ref="e1",
                    supported_claim="claim",
                    product="widget",
                    version="1.0",
                ),
            ),
        )

    def test_external_draft_appears_after_routing_blocks_story(self):
        self._init_empty_state()
        candidate_input = self._candidate_input("cand-1")
        # An external draft request appears for this exact candidate AFTER
        # routing decided, but BEFORE Story dispatch actually runs.
        self._write_manifest("cand-1", create_attempts=1, state="DRAFT_CREATED", zernio_draft_id="ext-1")

        recheck = dispatch.make_state_authoritative_recheck(candidate_input, self.tmp_path)
        result = recheck(stage="STORY", record={"targets": {}})
        self.assertFalse(result.permitted)
        self.assertEqual(result.reason_code, "EXISTING_DRAFT_REQUEST")

        rr = routing_result(candidate_id="cand-1", targets=("STORY",))
        calls = []

        def must_not_run():  # pragma: no cover - must never run
            calls.append(1)
            return FakeResult("DRAFT_CREATED")

        dr = dispatch.dispatch_draft_set(rr, story_runner=must_not_run, authoritative_recheck=recheck)
        self.assertEqual(calls, [])
        self.assertEqual(dr.record["targets"]["STORY"]["status"], "BLOCKED_BEFORE_ATTEMPT")
        self.assertEqual(dr.record["targets"]["STORY"]["reason_code"], "EXISTING_DRAFT_REQUEST")

    def test_external_publication_appears_before_story_blocks(self):
        self._init_empty_state()
        candidate_input = self._candidate_input("cand-1")
        self._write_manifest("cand-1")
        # Overwrite with a consequential publication attempt.
        manifest_path = self.tmp_path / "social/ops/manifests/manifest-cand-1.json"
        import json as _json

        manifest = _json.loads(manifest_path.read_text())
        manifest["publication"] = {"attempts": 1, "state": "PUBLISHED"}
        manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")

        recheck = dispatch.make_state_authoritative_recheck(candidate_input, self.tmp_path)
        result = recheck(stage="STORY", record={"targets": {}})
        self.assertFalse(result.permitted)
        self.assertEqual(result.reason_code, "EXISTING_CONSEQUENTIAL_STATE")

    def test_only_this_sets_own_story_manifest_allows_main_to_continue(self):
        self._init_empty_state()
        candidate_input = self._candidate_input("cand-1")
        recheck = dispatch.make_state_authoritative_recheck(candidate_input, self.tmp_path)

        rr = routing_result(
            decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", candidate_id="cand-1", targets=("STORY", "FEED")
        )

        def story_runner():
            # Simulate the real Story pipeline's own side effect: it
            # persists a manifest under this exact candidate_id.
            self._write_manifest("cand-1", create_attempts=1, state="DRAFT_CREATED", zernio_draft_id="own-1")
            return FakeResult("DRAFT_CREATED", manifest_id="manifest-cand-1", review_post_id="own-1")

        def main_runner(fmt):
            return FakeResult("DRAFT_CREATED", manifest_id="m-main", review_post_id="r-main")

        dr = dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=main_runner,
            authoritative_recheck=recheck,
            main_capacity_recheck=always_capacity_ok,
        )
        self.assertEqual(dr.record["targets"]["STORY"]["status"], "SUCCEEDED")
        # The fresh recheck before main dispatch sees this candidate's own
        # just-created Story manifest and correctly treats it as the
        # expected, intentionally-allowed sibling -- not a foreign block.
        self.assertEqual(dr.record["targets"]["FEED"]["status"], "SUCCEEDED")

    def test_fresh_ambiguity_blocks_target(self):
        # No state initialized at all: STATE_MISSING everywhere -> #35
        # fails closed to AMBIGUOUS_IDENTITY / STATE_UNAVAILABLE_OR_CONFLICTING.
        candidate_input = self._candidate_input("cand-1")
        recheck = dispatch.make_state_authoritative_recheck(candidate_input, self.tmp_path)
        result = recheck(stage="STORY", record={"targets": {}})
        self.assertFalse(result.permitted)
        self.assertTrue(result.reconciliation_required)

        rr = routing_result(candidate_id="cand-1", targets=("STORY",))
        dr = dispatch.dispatch_draft_set(
            rr, story_runner=lambda: FakeResult("DRAFT_CREATED"), authoritative_recheck=recheck
        )
        self.assertEqual(dr.record["targets"]["STORY"]["status"], "BLOCKED_BEFORE_ATTEMPT")
        self.assertTrue(dr.reconciliation_required)


class ExplicitContinuationTests(DispatchTestCase):
    def test_story_blocked_before_attempt_can_be_explicitly_continued(self):
        rr = routing_result(targets=("STORY",))
        dispatch.dispatch_draft_set(
            rr,
            story_runner=lambda: FakeResult("CANDIDATE_NOT_ELIGIBLE", reason_code="CANDIDATE_NOT_ELIGIBLE"),
            authoritative_recheck=always_permit,
        )

        result = dispatch.continue_unattempted_target(
            rr,
            "STORY",
            story_runner=lambda: FakeResult("DRAFT_CREATED", manifest_id="m-1", review_post_id="r-1"),
            authoritative_recheck=always_permit,
        )
        self.assertEqual(result.record["targets"]["STORY"]["status"], "SUCCEEDED")

    def test_ordinary_replay_does_not_retry_blocked_before_attempt(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        main_calls = []

        def main_runner(fmt):
            main_calls.append(fmt)
            return FakeResult("CANDIDATE_NOT_ELIGIBLE", reason_code="CANDIDATE_NOT_ELIGIBLE")

        common = dict(
            story_runner=lambda: FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story"),
            main_runner=main_runner,
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        dispatch.dispatch_draft_set(rr, **common)
        self.assertEqual(len(main_calls), 1)

        # Ordinary replay must never retry a BLOCKED_BEFORE_ATTEMPT target.
        dispatch.dispatch_draft_set(rr, **common)
        self.assertEqual(len(main_calls), 1)

    def test_main_can_be_explicitly_continued_after_story_success_never_repeats_story(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        story_calls = []

        def story_runner():
            story_calls.append(1)
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        dispatch.dispatch_draft_set(
            rr,
            story_runner=story_runner,
            main_runner=lambda fmt: FakeResult("CANDIDATE_NOT_ELIGIBLE", reason_code="CANDIDATE_NOT_ELIGIBLE"),
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )

        result = dispatch.continue_unattempted_target(
            rr,
            "FEED",
            main_runner=lambda fmt: FakeResult("DRAFT_CREATED", manifest_id="m-main", review_post_id="r-main"),
            authoritative_recheck=always_permit,
            main_capacity_recheck=always_capacity_ok,
        )
        self.assertEqual(result.record["targets"]["FEED"]["status"], "SUCCEEDED")
        self.assertEqual(len(story_calls), 1, "continuation of main must never repeat Story")

    def test_ambiguous_target_cannot_be_continued(self):
        rr = routing_result(targets=("STORY",))
        dispatch.dispatch_draft_set(
            rr,
            story_runner=lambda: FakeResult("REVIEW_DRAFT_AMBIGUOUS", reason_code="REVIEW_DRAFT_AMBIGUOUS"),
            authoritative_recheck=always_permit,
        )
        with self.assertRaises(dispatch.DraftSetError):
            dispatch.continue_unattempted_target(
                rr,
                "STORY",
                story_runner=lambda: FakeResult("DRAFT_CREATED"),
                authoritative_recheck=always_permit,
            )

    def test_conflicting_decision_cannot_continue(self):
        rr = routing_result(targets=("STORY",))
        dispatch.dispatch_draft_set(
            rr,
            story_runner=lambda: FakeResult("CANDIDATE_NOT_ELIGIBLE", reason_code="CANDIDATE_NOT_ELIGIBLE"),
            authoritative_recheck=always_permit,
        )
        conflicting = routing_result(targets=("STORY",), reason_code="DIFFERENT_REASON")
        with self.assertRaises(dispatch.DraftSetConflict):
            dispatch.continue_unattempted_target(
                conflicting,
                "STORY",
                story_runner=lambda: FakeResult("DRAFT_CREATED"),
                authoritative_recheck=always_permit,
            )

    def test_main_cannot_continue_before_story_succeeded(self):
        rr = routing_result(decision="IMMEDIATE_STORY_AND_MAIN_DRAFT", targets=("STORY", "FEED"))
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")
        with dispatch.draft_set_lock(set_id):
            record, _ = dispatch.reserve_draft_set(rr)
            # Contrive: FEED blocked-before-attempt while STORY still PENDING.
            dispatch._update_target(record, "FEED", status="BLOCKED_BEFORE_ATTEMPT")

        with self.assertRaises(dispatch.DraftSetError):
            dispatch.continue_unattempted_target(
                rr,
                "FEED",
                main_runner=lambda fmt: FakeResult("DRAFT_CREATED"),
                authoritative_recheck=always_permit,
                main_capacity_recheck=always_capacity_ok,
            )


class CapabilityNegativeTests(DispatchTestCase):
    def test_no_publisher_import(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(dispatch))
        # Drop the module docstring (and any other bare docstring
        # expression statements) -- prose mentioning what this module
        # deliberately does NOT do is not a capability.
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                if isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                    body.pop(0)

        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
        self.assertFalse(
            any("publish" in name for name in imported_names),
            f"unexpected publish-related import: {imported_names}",
        )
        # No subprocess argv literal may name the publisher/publish-bridge
        # scripts (unlike docstring prose, string-literal argv targets are
        # the actual capability surface).
        string_literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertFalse(any("publish-bridge" in s for s in string_literals))
        self.assertFalse(any("publisher-run" in s for s in string_literals))


if __name__ == "__main__":
    unittest.main()
