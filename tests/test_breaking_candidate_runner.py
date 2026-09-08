#!/usr/bin/env python3
"""Breaking candidate runner tests (#80).

Proves the deterministic production boundary: strict handoff admission,
persisted #27 replay authority, real Story-first execution with no main
provider, truthful persistence/notification, and fail-closed corrupt or
non-qualifying inputs -- with zero re-runs on replay.
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
from nullone_breaking_candidate_runner import run_breaking_candidate  # noqa: E402
from nullone_review_delivery import FakeReviewDelivery  # noqa: E402
from nullone_story_pipeline import numeric_scope_verifier  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_breaking_scan_commit import make_assessment  # noqa: E402

BAKU = ZoneInfo("Asia/Baku")
NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=BAKU)

AT = "2026-09-08T07:35:00Z"
SCAN_ID = "breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z"


def make_handoff(candidate_id="acme-widget-launch", **overrides):
    assessment = make_assessment(candidate_id)
    assessment.update(overrides)
    return {
        "schema": "nullone.breaking-radar-handoff.v1",
        "contract_version": "1.0.0",
        "occurrence": {
            "source_occurrence_id": SCAN_ID,
            "scheduled_for": "2026-09-08T07:30:00Z",
            "triggered_at": AT,
        },
        "assessment": assessment,
    }


def digit_free_writer(_context):
    return {
        "layout": "big-stat",
        "headline": "Breaking headline",
        "body": "Breaking body.",
        "stat": "Breaking stat",
        "source_name": "Breaking source",
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
        manifest["review"]["zernio_draft_id"] = "breaking-review-1"
        manifest["review"]["created_at"] = bridge_common.now_iso()
        bridge_common.atomic_write_json(manifest_path, manifest)


class Exploding:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"{self.name} must not be called")


class BreakingRunnerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.out = self.root / "run-outcomes"
        self.original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = self.root
        (self.root / "social/ops/manifests").mkdir(parents=True)
        state = self.root / "social/state"
        state.mkdir(parents=True)
        for name in ("publish-ledger.jsonl", "topic-ledger.jsonl", "candidate-queue.md"):
            (state / name).touch()
        tools_dir = self.root / "social/tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            ROOT / "workspace/social/tools/render_story_v2.py",
            tools_dir / "render_story_v2.py",
        )

    def tearDown(self):
        bridge_common.WORKSPACE = self.original_workspace
        self.td.cleanup()

    def base_kwargs(self, **overrides):
        kwargs = {
            "story_writer": digit_free_writer,
            "story_verifier": numeric_scope_verifier,
            "draft_connector": FakeDraftConnector(),
            "review_delivery": FakeReviewDelivery(status="SENT"),
            "notifier": lambda _r: {"status": "NOT_REQUIRED"},
            # TEST ONLY authoritative recheck. Production #80 passes None.
            "dependency_recheck": lambda _stage: True,
            "workspace_root": self.root,
            "output_root": self.out,
            "now": NOW,
        }
        kwargs.update(overrides)
        return kwargs

    def test_malformed_handoff_fails_closed_without_workflow(self):
        result = run_breaking_candidate(
            {"schema": "nope"},
            **self.base_kwargs(
                story_writer=Exploding("writer"),
                draft_connector=Exploding("draft"),
                review_delivery=Exploding("delivery"),
                dependency_recheck=None,
            ),
        )
        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "HANDOFF_REJECTED")

    def test_non_pass_verification_produces_no_draft(self):
        assessment = make_assessment()
        assessment["verification"] = {
            "state": "PARTIAL",
            "evidence_refs": ["evidence:official:1"],
        }
        assessment["severity_assessment"] = {"classification": None, "reason_text": None}
        draft = FakeDraftConnector()
        result = run_breaking_candidate(
            {
                "schema": "nullone.breaking-radar-handoff.v1",
                "contract_version": "1.0.0",
                "occurrence": {
                    "source_occurrence_id": SCAN_ID,
                    "scheduled_for": "2026-09-08T07:30:00Z",
                    "triggered_at": AT,
                },
                "assessment": assessment,
            },
            **self.base_kwargs(draft_connector=draft, dependency_recheck=None),
        )
        self.assertEqual(result.application_execution, "COMPLETED")
        self.assertEqual(draft.calls, 0)

    def test_llm_dependency_attestation_is_not_authoritative_recheck(self):
        # Radar says dependencies_available=true, but no injected recheck.
        handoff = make_handoff()
        self.assertTrue(
            handoff["assessment"]["story_safety"]["dependencies_available"]
        )
        draft = FakeDraftConnector()
        delivery = FakeReviewDelivery(status="SENT")
        result = run_breaking_candidate(
            handoff,
            **self.base_kwargs(
                draft_connector=draft,
                review_delivery=delivery,
                dependency_recheck=None,
            ),
        )
        self.assertEqual(result.application_execution, "COMPLETED")
        self.assertEqual(result.domain_outcome, "BLOCKED")
        self.assertEqual(result.reason_code, "DRAFT_DEPENDENCY_RECHECK_MISSING")
        self.assertEqual(draft.calls, 0)
        self.assertEqual(len(delivery.sent), 0)

    def test_injected_authoritative_recheck_allows_story_path(self):
        draft = FakeDraftConnector()
        delivery = FakeReviewDelivery(status="SENT")
        result = run_breaking_candidate(
            make_handoff(),
            **self.base_kwargs(draft_connector=draft, review_delivery=delivery),
        )
        self.assertEqual(result.application_execution, "COMPLETED")
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(result.reason_code, "OK")
        self.assertEqual(draft.calls, 1)
        self.assertEqual(len(delivery.sent), 1)

    def test_fresh_and_replay_agree_on_blocked_reason(self):
        handoff = make_handoff()
        fresh = run_breaking_candidate(
            handoff, **self.base_kwargs(dependency_recheck=None)
        )
        self.assertEqual(fresh.application_execution, "COMPLETED")
        self.assertEqual(fresh.domain_outcome, "BLOCKED")
        self.assertEqual(fresh.reason_code, "DRAFT_DEPENDENCY_RECHECK_MISSING")
        persisted = json.loads(Path(fresh.result_file).read_text(encoding="utf-8"))
        self.assertEqual(persisted["domain_outcome"], "BLOCKED")
        self.assertEqual(persisted["reason_code"], fresh.reason_code)
        self.assertEqual(persisted["reason_text"], fresh.reason_text)

        replay = run_breaking_candidate(
            handoff,
            **self.base_kwargs(
                story_writer=Exploding("writer"),
                draft_connector=Exploding("draft"),
                review_delivery=Exploding("delivery"),
                dependency_recheck=None,
                notifier=lambda _r: {"status": "NOT_REQUIRED"},
            ),
        )
        self.assertEqual(replay.domain_outcome, fresh.domain_outcome)
        self.assertEqual(replay.reason_code, fresh.reason_code)
        self.assertEqual(replay.reason_text, fresh.reason_text)

    def test_fresh_and_replay_agree_on_failed_reason(self):
        delivery = FakeReviewDelivery(status="FAILED", error="down")
        draft = FakeDraftConnector()
        handoff = make_handoff()
        fresh = run_breaking_candidate(
            handoff,
            **self.base_kwargs(draft_connector=draft, review_delivery=delivery),
        )
        self.assertEqual(fresh.application_execution, "COMPLETED")
        # Preview failure is a Failed domain outcome, not application crash.
        self.assertEqual(fresh.domain_outcome, "FAILED")
        persisted = json.loads(Path(fresh.result_file).read_text(encoding="utf-8"))
        self.assertEqual(persisted["domain_outcome"], "FAILED")
        self.assertEqual(fresh.reason_code, persisted["reason_code"])
        self.assertEqual(fresh.reason_text, persisted["reason_text"])
        self.assertNotEqual(fresh.reason_code, "OK")

        replay = run_breaking_candidate(
            handoff,
            **self.base_kwargs(
                story_writer=Exploding("writer"),
                draft_connector=Exploding("draft"),
                review_delivery=Exploding("delivery"),
                notifier=lambda _r: {"status": "ALREADY_SENT"},
            ),
        )
        self.assertEqual(replay.domain_outcome, fresh.domain_outcome)
        self.assertEqual(replay.reason_code, fresh.reason_code)
        self.assertEqual(replay.reason_text, fresh.reason_text)

    def test_material_breaking_runs_story_first_without_main(self):
        delivery = FakeReviewDelivery(status="SENT")
        draft = FakeDraftConnector()
        result = run_breaking_candidate(
            make_handoff(),
            **self.base_kwargs(draft_connector=draft, review_delivery=delivery),
        )
        self.assertEqual(result.application_execution, "COMPLETED")
        self.assertEqual(result.domain_outcome, "SUCCEEDED")
        self.assertEqual(draft.calls, 1)
        self.assertEqual(len(delivery.sent), 1)
        persisted = json.loads(Path(result.result_file).read_text(encoding="utf-8"))
        self.assertEqual(persisted["domain_outcome"], "SUCCEEDED")
        self.assertTrue(persisted["required_artifacts"])

    def test_replay_runs_nothing_twice(self):
        handoff = make_handoff()
        first = run_breaking_candidate(handoff, **self.base_kwargs())
        self.assertEqual(first.domain_outcome, "SUCCEEDED")
        second = run_breaking_candidate(
            handoff,
            **self.base_kwargs(
                story_writer=Exploding("writer"),
                draft_connector=Exploding("draft"),
                review_delivery=Exploding("delivery"),
                notifier=lambda _r: {"status": "ALREADY_SENT"},
            ),
        )
        self.assertEqual(second.application_execution, "COMPLETED")
        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(second.notification_status, "ALREADY_SENT")
        self.assertEqual(second.reason_code, first.reason_code)
        self.assertEqual(second.reason_text, first.reason_text)

    def test_corrupt_persisted_result_fails_closed(self):
        handoff = make_handoff()
        first = run_breaking_candidate(handoff, **self.base_kwargs())
        self.assertEqual(first.domain_outcome, "SUCCEEDED")
        Path(first.result_file).write_text("{corrupt", encoding="utf-8")
        second = run_breaking_candidate(
            handoff,
            **self.base_kwargs(
                draft_connector=Exploding("draft"),
                review_delivery=Exploding("delivery"),
            ),
        )
        self.assertEqual(second.application_execution, "FAILED")
        self.assertEqual(second.reason_code, "RESULT_MISSING_OR_CORRUPT")

    def test_two_candidates_yield_two_distinct_runs(self):
        second_assessment_overrides = {
            "topic": "Acme Gadget launch",
            "topic_cluster": "acme-gadget",
            "assessment_ref": "assessment:commit-test:2",
            "state_snapshot_ref": "state:commit-test:2",
            "evidence": [
                {
                    "ref": "evidence:official:2",
                    "supported_claim": "Acme Gadget 2 is available.",
                    "source_url": "https://example.invalid/gadget-2",
                    "announcement_id": "acme-gadget-2-launch",
                    "product": "Acme Gadget",
                    "version": "2",
                    "region": None,
                    "availability_stage": "GENERAL_AVAILABILITY",
                    "number_value": None,
                    "number_unit": None,
                    "number_population": None,
                    "number_period": None,
                }
            ],
            "verification": {"state": "PASS", "evidence_refs": ["evidence:official:2"]},
        }
        first = run_breaking_candidate(
            make_handoff("acme-widget-launch"), **self.base_kwargs()
        )
        second = run_breaking_candidate(
            make_handoff("acme-gadget-launch", **second_assessment_overrides),
            **self.base_kwargs(),
        )
        self.assertEqual(first.domain_outcome, "SUCCEEDED")
        self.assertEqual(second.domain_outcome, "SUCCEEDED")
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertNotEqual(first.occurrence_id, second.occurrence_id)

    def test_story_preview_failure_runs_no_main(self):
        delivery = FakeReviewDelivery(status="FAILED", error="down")
        draft = FakeDraftConnector()
        result = run_breaking_candidate(
            make_handoff(),
            **self.base_kwargs(draft_connector=draft, review_delivery=delivery),
        )
        self.assertEqual(result.application_execution, "COMPLETED")
        # Story preview failure is a Failed dispatch; main is never attempted.
        self.assertEqual(draft.calls, 1)
        self.assertEqual(len(delivery.sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
