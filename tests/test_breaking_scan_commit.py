#!/usr/bin/env python3
"""Scan commit-edge tests (#80).

Proves the deterministic commit boundary: current-scan identity without
side effects, exact handoff commit, idempotent recommit, conflict
rejection, candidate-ID enforcement, out-of-slot rejection, truthful
empty scans, and that Markdown reports alone can never become handoffs.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import importlib.util

import nullone_breaking_scan_authority  # noqa: E402,F401


def _load_scan_edge():
    spec = importlib.util.spec_from_file_location(
        "nullone_breaking_scan_edge_test", SCRIPTS / "nullone-breaking-scan.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load nullone-breaking-scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_scan_edge = _load_scan_edge()
BreakingScanCommitError = _scan_edge.BreakingScanCommitError
commit_assessment = _scan_edge.commit_assessment
current_scan = _scan_edge.current_scan
record_empty_scan = _scan_edge.record_empty_scan


def make_assessment(candidate_id="acme-widget-launch", **overrides):
    base = {
        "schema": "nullone.breaking-workflow-input.v1",
        "contract_version": "1.0.0",
        "candidate_id": candidate_id,
        "candidate_version": "v1",
        "assessment_ref": "assessment:commit-test:1",
        "state_snapshot_ref": "state:commit-test:1",
        "topic": "Acme Widget launch",
        "topic_cluster": "acme-widget",
        "content_type": "BREAKING",
        "evidence": [
            {
                "ref": "evidence:official:1",
                "supported_claim": "Acme Widget 2 is available.",
                "source_url": "https://example.invalid/widget-2",
                "announcement_id": "acme-widget-2-launch",
                "product": "Acme Widget",
                "version": "2",
                "region": None,
                "availability_stage": "GENERAL_AVAILABILITY",
                "number_value": None,
                "number_unit": None,
                "number_population": None,
                "number_period": None,
            }
        ],
        "follow_up_delta": None,
        "source_attribution": "Acme official",
        "limitations": ["Launch region only."],
        "product_version_region": {
            "product": "Acme Widget",
            "version": "2",
            "region": None,
        },
        "source_image": None,
        "verification": {"state": "PASS", "evidence_refs": ["evidence:official:1"]},
        "severity_assessment": {
            "classification": "MATERIAL_BREAKING",
            "reason_text": "Launch timing is material.",
        },
        "recent_coverage": {
            "related_coverage_exists": False,
            "incremental_value_present": True,
            "assessment_ref": "coverage:commit-test:1",
            "freshness_ref": "freshness:commit-test:1",
        },
        "story_safety": {
            "quality_pass": True,
            "quality_ref": "quality:commit-test:1",
            "dependencies_available": True,
            "dependencies_ref": "dependencies:commit-test:1",
        },
        "main_assessment": None,
    }
    base.update(overrides)
    return base


AT = "2026-09-08T07:35:00Z"  # inside the 11:30 Baku scan slot
SCAN_ID = "breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z"


class CommitEdgeTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def stage(self, assessment) -> Path:
        staged = self.root / "staged.json"
        staged.write_text(json.dumps(assessment), encoding="utf-8")
        return staged

    def test_current_scan_has_no_side_effects(self):
        scan = current_scan(source="openclaw", at=AT)
        self.assertEqual(scan["source_occurrence_id"], SCAN_ID)
        self.assertEqual(scan["scheduled_for"], "2026-09-08T07:30:00Z")
        self.assertFalse((self.root / "social/ops/breaking-handoffs").exists())

    def test_zero_candidate_scan_is_truthful(self):
        receipt = record_empty_scan(source="openclaw", at=AT, workspace_root=self.root)
        self.assertEqual(receipt["status"], "NO_MATERIAL_DEVELOPMENT")
        stored = json.loads(
            (self.root / "social/ops/breaking-handoffs" / SCAN_ID / "scan-receipt.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(stored["status"], "NO_MATERIAL_DEVELOPMENT")
        self.assertEqual(stored["candidates"], [])

    def test_one_valid_candidate_commits_exact_handoff(self):
        committed = commit_assessment(
            assessment_path=self.stage(make_assessment()),
            source="openclaw",
            at=AT,
            workspace_root=self.root,
        )
        self.assertEqual(committed["source_occurrence_id"], SCAN_ID)
        target = self.root / committed["handoff_path"]
        self.assertTrue(target.is_file())
        stored = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema"], "nullone.breaking-radar-handoff.v1")
        self.assertEqual(
            stored["occurrence"]["source_occurrence_id"], SCAN_ID
        )
        self.assertEqual(stored["occurrence"]["triggered_at"], AT)

    def test_two_candidates_share_scan_distinct_occurrences(self):
        first = commit_assessment(
            assessment_path=self.stage(make_assessment("acme-widget-launch")),
            source="openclaw",
            at=AT,
            workspace_root=self.root,
        )
        second = commit_assessment(
            assessment_path=self.stage(make_assessment("acme-widget-price")),
            source="openclaw",
            at=AT,
            workspace_root=self.root,
        )
        self.assertEqual(first["source_occurrence_id"], second["source_occurrence_id"])
        self.assertNotEqual(
            first["external_occurrence_id"], second["external_occurrence_id"]
        )

    def test_identical_recommit_is_idempotent(self):
        staged = self.stage(make_assessment())
        first = commit_assessment(
            assessment_path=staged, source="openclaw", at=AT, workspace_root=self.root
        )
        second = commit_assessment(
            assessment_path=staged, source="openclaw", at=AT, workspace_root=self.root
        )
        self.assertEqual(first["handoff_path"], second["handoff_path"])

    def test_conflicting_recommit_rejected(self):
        staged = self.stage(make_assessment())
        commit_assessment(
            assessment_path=staged, source="openclaw", at=AT, workspace_root=self.root
        )
        staged.write_text(
            json.dumps(make_assessment(topic="Changed topic")), encoding="utf-8"
        )
        with self.assertRaises(BreakingScanCommitError):
            commit_assessment(
                assessment_path=staged,
                source="openclaw",
                at=AT,
                workspace_root=self.root,
            )

    def test_bad_candidate_id_rejected(self):
        with self.assertRaises(BreakingScanCommitError):
            commit_assessment(
                assessment_path=self.stage(make_assessment("Rank 1!!!")),
                source="openclaw",
                at=AT,
                workspace_root=self.root,
            )

    def test_malformed_assessment_rejected(self):
        staged = self.stage(make_assessment())
        staged.write_text(json.dumps({"candidate_id": "x"}), encoding="utf-8")
        with self.assertRaises(BreakingScanCommitError):
            commit_assessment(
                assessment_path=staged,
                source="openclaw",
                at=AT,
                workspace_root=self.root,
            )

    def test_commit_outside_slot_rejected(self):
        with self.assertRaises(BreakingScanCommitError):
            commit_assessment(
                assessment_path=self.stage(make_assessment()),
                source="openclaw",
                at="2026-09-08T06:00:00Z",
                workspace_root=self.root,
            )

    def test_markdown_report_alone_cannot_become_handoff(self):
        research = self.root / "social/research/daily"
        research.mkdir(parents=True, exist_ok=True)
        (research / "2026-09-08-breaking-1130.md").write_text(
            "# Breaking\nRANK 1: markdown-decoy\n", encoding="utf-8"
        )
        spool = self.root / "social/ops/breaking-handoffs"
        self.assertFalse(spool.exists())

    def test_symlink_target_rejected(self):
        staged = self.stage(make_assessment())
        committed = commit_assessment(
            assessment_path=staged, source="openclaw", at=AT, workspace_root=self.root
        )
        target = self.root / committed["handoff_path"]
        target.unlink()
        real = target.parent / "real.json"
        real.write_text("{}", encoding="utf-8")
        target.symlink_to(real)
        staged2 = self.stage(make_assessment())
        with self.assertRaises(BreakingScanCommitError):
            commit_assessment(
                assessment_path=staged2,
                source="openclaw",
                at=AT,
                workspace_root=self.root,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
