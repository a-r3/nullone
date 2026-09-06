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


class FakeResult:
    def __init__(self, outcome, manifest_id=None, review_post_id=None, reason_code=None):
        self.outcome = outcome
        self.manifest_id = manifest_id
        self.review_post_id = review_post_id
        self.reason_code = reason_code


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

    def test_different_source_same_development_no_second_set(self):
        rr1 = routing_result(candidate_id="cand-a")
        rr2 = routing_result(candidate_id="cand-b")  # different source/candidate, same development
        set_id = dispatch.compute_draft_set_id("event-1", "dev-1")
        with dispatch.draft_set_lock(set_id):
            record1, created1 = dispatch.reserve_draft_set(rr1)
            record2, created2 = dispatch.reserve_draft_set(rr2)
        self.assertTrue(created1)
        self.assertFalse(created2)
        # The set stays bound to whichever candidate reserved it first.
        self.assertEqual(record2["candidate_id"], record1["candidate_id"])

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

        result = dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
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

        result = dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
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

        result = dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
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

        result1 = dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
        self.assertEqual(result1.record["targets"]["STORY"]["status"], "SUCCEEDED")
        self.assertEqual(result1.record["targets"]["CAROUSEL"]["status"], "BLOCKED_BEFORE_ATTEMPT")

        # Replay: Story must never be repeated.
        result2 = dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
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

        result1 = dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
        self.assertTrue(result1.reconciliation_required)
        self.assertEqual(result1.record["targets"]["FEED"]["status"], "AMBIGUOUS")

        result2 = dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
        self.assertEqual(len(story_calls), 1)
        self.assertEqual(len(main_calls), 1)
        self.assertEqual(result2.record["targets"]["FEED"]["status"], "AMBIGUOUS")

    def test_story_only_set_never_attempts_main(self):
        rr = routing_result(targets=("STORY",))

        def story_runner():
            return FakeResult("DRAFT_CREATED", manifest_id="m-story", review_post_id="r-story")

        def main_runner(fmt):  # pragma: no cover - must never be called
            raise AssertionError("main_runner must not be called for a Story-only set")

        result = dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
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

        dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
        dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
        dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)

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
            main_capacity_recheck=lambda: False,
        )
        self.assertEqual(main_calls, [])
        self.assertEqual(result.record["targets"]["FEED"]["status"], "BLOCKED_BEFORE_ATTEMPT")


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

        dr = dispatch.dispatch_draft_set(rr, story_runner=story_runner, main_runner=main_runner)
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, dr)
        self.assertEqual(outcome, "SUCCEEDED")

    def test_accelerated_ambiguous_target_is_unknown(self):
        rr = routing_result(targets=("STORY",))

        def story_runner():
            return FakeResult("REVIEW_DRAFT_AMBIGUOUS", reason_code="REVIEW_DRAFT_AMBIGUOUS")

        dr = dispatch.dispatch_draft_set(rr, story_runner=story_runner)
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, dr)
        self.assertEqual(outcome, "UNKNOWN")

    def test_accelerated_blocked_target_is_blocked(self):
        rr = routing_result(targets=("STORY",))

        def story_runner():
            return FakeResult("CANDIDATE_NOT_ELIGIBLE", reason_code="CANDIDATE_NOT_ELIGIBLE")

        dr = dispatch.dispatch_draft_set(rr, story_runner=story_runner)
        outcome, code, text = dispatch.dispatch_domain_outcome(rr, dr)
        self.assertEqual(outcome, "BLOCKED")


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
