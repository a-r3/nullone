#!/usr/bin/env python3
"""Scheduled production Story boundary tests (#79).

Proves the exact production entrypoint composition: validated trigger
-> structured snapshot -> availability -> provider -> StoryWorkflow ->
persisted #27 result -> single notification, with scheduler-vs-domain
exit semantics, replay stability, and truthful DRAFT_CREATED success
(DRAFT_CREATED + preview SENT only).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
from nullone_review_delivery import FakeReviewDelivery  # noqa: E402
from nullone_scheduler_invocation import compute_occurrence_id  # noqa: E402
from nullone_story_pipeline import make_fake_verifier, numeric_scope_verifier  # noqa: E402
from nullone_story_scheduled_workflow import run_story_trigger  # noqa: E402
from support.morning_artifacts import write_morning_artifacts  # noqa: E402

BAKU = ZoneInfo("Asia/Baku")
NOW = datetime(2026, 9, 8, 14, 30, 0, tzinfo=BAKU)  # AFTERNOON, never QUIET


def make_candidate(**overrides):
    base = {
        "candidate_id": "sched-1",
        "rank": 1,
        "topic": "Scheduled topic",
        "topic_cluster": "scheduled",
        "content_type": "NEWS",
        "angle": "Angle",
        "verification": "PASS",
        "evidence_refs": ["Scheduled evidence"],
        "source_attribution": "Scheduled source",
        "editorial_status": "READY",
        "story_eligible": True,
    }
    base.update(overrides)
    return base


def write_handoff(workspace_root: Path, date: str, candidates) -> None:
    research = workspace_root / "social/research/daily"
    research.mkdir(parents=True, exist_ok=True)
    (research / f"{date}-editorial-candidates.json").write_text(
        json.dumps(
            {
                "schema": "nullone.editorial-candidate-handoff.v1",
                "contract_version": "1.0.0",
                "editorial_date": date,
                "board_path": f"social/research/daily/{date}-editorial-board.md",
                "candidates": candidates,
            }
        ),
        encoding="utf-8",
    )


def make_trigger(**overrides):
    base = {
        "schema": "nullone.scheduler-invocation.v1",
        "contract_version": "1.0.0",
        "workflow_id": "story",
        "source": "openclaw",
        "external_occurrence_id": "story.check-1330.v1@2026-09-08T09:30:00Z",
        "scheduled_for": "2026-09-08T09:30:00Z",
        "triggered_at": "2026-09-08T09:30:02Z",
    }
    base.update(overrides)
    base["occurrence_id"] = compute_occurrence_id(
        base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
    )
    return base


def digit_free_writer(_context):
    return {
        "layout": "big-stat",
        "headline": "Scheduled headline",
        "body": "Scheduled body.",
        "stat": "Scheduled stat",
        "source_name": "Scheduled source",
        "use_source_image": False,
        "cta": "@nullone.az",
    }


class FakeDraftConnector:
    def __init__(self) -> None:
        self.calls = 0

    def create_review_draft(self, manifest_path: Path) -> None:
        self.calls += 1
        _, manifest = bridge_common.load_manifest(manifest_path)
        manifest["review"]["create_attempts"] = 1
        manifest["review"]["state"] = "DRAFT_CREATED"
        manifest["review"]["zernio_draft_id"] = "sched-review-1"
        manifest["review"]["created_at"] = bridge_common.now_iso()
        bridge_common.atomic_write_json(manifest_path, manifest)


class Exploding:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"{self.name} must not be called")


class StoryScheduledBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.out = self.root / "run-outcomes"
        self.original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = self.root
        tools_dir = self.root / "social/tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        real_workspace_root = Path(__file__).resolve().parents[1]
        shutil.copy(
            real_workspace_root / "workspace/social/tools/render_story_v2.py",
            tools_dir / "render_story_v2.py",
        )
        (self.root / "social").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        bridge_common.WORKSPACE = self.original_workspace
        self.td.cleanup()

    def base_kwargs(self, **overrides):
        kwargs = {
            "writer": digit_free_writer,
            "verifier": numeric_scope_verifier,
            "draft_connector": FakeDraftConnector(),
            "review_delivery": FakeReviewDelivery(status="SENT"),
            "notifier": lambda _r: {"status": "NOT_REQUIRED"},
            "workspace_root": self.root,
            "output_root": self.out,
            "now": NOW,
        }
        kwargs.update(overrides)
        return kwargs

    def test_empty_handoff_is_truthful_no_action_success(self):
        write_handoff(self.root, "2026-09-08", [])
        result = run_story_trigger(make_trigger(), **self.base_kwargs())
        self.assertEqual(result.application_execution, "COMPLETED")
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(result.story_outcome, "NO_ACTION")
        self.assertIsNotNone(result.run_id)
        persisted = json.loads(Path(result.result_file).read_text(encoding="utf-8"))
        self.assertEqual(persisted["empty_success"], "NO_ACTION")
        self.assertEqual(result.notification_status, "NOT_REQUIRED")

    def test_one_candidate_produces_sent_success(self):
        write_handoff(self.root, "2026-09-08", [make_candidate()])
        delivery = FakeReviewDelivery(status="SENT")
        result = run_story_trigger(
            make_trigger(), **self.base_kwargs(review_delivery=delivery)
        )
        self.assertEqual(result.application_execution, "COMPLETED")
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(result.story_outcome, "DRAFT_CREATED")
        self.assertEqual(len(delivery.sent), 1)
        persisted = json.loads(Path(result.result_file).read_text(encoding="utf-8"))
        self.assertEqual(persisted["domain_outcome"], "SUCCEEDED")
        self.assertTrue(persisted["required_artifacts"])

    def test_replay_returns_persisted_result_without_side_effects(self):
        write_handoff(self.root, "2026-09-08", [make_candidate()])
        trigger = make_trigger()
        first = run_story_trigger(trigger, **self.base_kwargs())
        self.assertEqual(first.domain_outcome, "SUCCEEDED")

        second = run_story_trigger(
            trigger,
            **self.base_kwargs(
                writer=Exploding("writer"),
                verifier=Exploding("verifier"),
                draft_connector=Exploding("draft"),
                review_delivery=Exploding("delivery"),
                notifier=lambda _r: {"status": "ALREADY_SENT"},
            ),
        )
        self.assertEqual(second.application_execution, "COMPLETED")
        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(second.notification_status, "ALREADY_SENT")

    def test_writer_crash_is_completed_domain_failure(self):
        write_handoff(self.root, "2026-09-08", [make_candidate()])

        def crashing_writer(_context):
            raise RuntimeError("writer exploded")

        result = run_story_trigger(
            make_trigger(), **self.base_kwargs(writer=crashing_writer)
        )
        self.assertEqual(result.application_execution, "COMPLETED")
        self.assertEqual(result.domain_outcome, "FAILED")

    def test_malformed_notifier_fails_closed(self):
        write_handoff(self.root, "2026-09-08", [])
        result = run_story_trigger(
            make_trigger(),
            **self.base_kwargs(notifier=lambda _r: {"status": "BOGUS"}),
        )
        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "NOTIFICATION_RESULT_INVALID")

    def test_missing_handoff_never_invents_candidate(self):
        result = run_story_trigger(
            make_trigger(),
            **self.base_kwargs(
                writer=Exploding("writer"),
                draft_connector=Exploding("draft"),
                review_delivery=Exploding("delivery"),
            ),
        )
        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "SOURCE_HANDOFF_MISSING")


if __name__ == "__main__":
    unittest.main(verbosity=2)
