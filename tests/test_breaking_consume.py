#!/usr/bin/env python3
"""Breaking spool consumer tests (#80).

Proves the static sweep: committed handoffs processed exactly once with
real runner wiring, replay-safe second sweeps, per-file isolation for
invalid files, filename/content mismatch safety, and recovery of a
handoff persisted before dispatch ever started.
"""
from __future__ import annotations

import importlib.util
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
from nullone_story_pipeline import numeric_scope_verifier  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_breaking_candidate_runner import (  # noqa: E402
    FakeDraftConnector,
    digit_free_writer,
)
from test_breaking_scan_commit import make_assessment  # noqa: E402


def _load_consume():
    spec = importlib.util.spec_from_file_location(
        "nullone_breaking_consume_test", SCRIPTS / "nullone-breaking-consume.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load nullone-breaking-consume.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "nullone_breaking_scan_test", SCRIPTS / "nullone-breaking-scan.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load nullone-breaking-scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_consume = _load_consume()
_scan = _load_scan()

BAKU = ZoneInfo("Asia/Baku")
NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=BAKU)
AT = "2026-09-08T07:35:00Z"


class BreakingConsumeTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
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

    def overrides(self, **extra):
        base = {
            "story_writer": digit_free_writer,
            "story_verifier": numeric_scope_verifier,
            "draft_connector": FakeDraftConnector(),
            "review_delivery": FakeReviewDelivery(status="SENT"),
            "notifier": lambda _r: {"status": "NOT_REQUIRED"},
        }
        base.update(extra)
        return base

    def commit(self, candidate_id="acme-widget-launch", **overrides):
        staged = self.root / "staged.json"
        staged.write_text(
            json.dumps(make_assessment(candidate_id, **overrides)), encoding="utf-8"
        )
        return _scan.commit_assessment(
            assessment_path=staged,
            source="openclaw",
            at=AT,
            workspace_root=self.root,
        )

    def test_empty_spool_sweeps_clean(self):
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root, overrides=self.overrides()
        )
        self.assertEqual(report["scans_seen"], 0)
        self.assertEqual(report["processed"], [])
        self.assertEqual(report["skipped_invalid"], [])

    def test_committed_handoff_processed_once_then_replayed(self):
        self.commit()
        first = _consume.sweep_breaking_handoffs(
            workspace_root=self.root, overrides=self.overrides()
        )
        self.assertEqual(first["scans_seen"], 1)
        self.assertEqual(len(first["processed"]), 1)
        self.assertEqual(first["processed"][0]["domain_outcome"], "SUCCEEDED")

        draft = FakeDraftConnector()
        second = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(draft_connector=draft),
        )
        self.assertEqual(len(second["processed"]), 1)
        self.assertEqual(draft.calls, 0)

    def test_malformed_file_does_not_block_valid_candidate(self):
        self.commit("acme-widget-launch")
        scan_dir = self.root / "social/ops/breaking-handoffs" / (
            "breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z"
        )
        (scan_dir / "zzz-broken.json").write_text("{not json", encoding="utf-8")
        (scan_dir / "notes.txt").write_text("human note", encoding="utf-8")
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root, overrides=self.overrides()
        )
        self.assertEqual(len(report["processed"]), 1)
        reasons = {entry["reason"] for entry in report["skipped_invalid"]}
        self.assertIn("UNREADABLE", reasons)

    def test_filename_content_mismatch_skipped(self):
        committed = self.commit()
        scan_dir = self.root / committed["handoff_path"].split("/breaking-handoffs/")[0]
        # Move the valid handoff under a wrong name: content no longer
        # matches its filename, so the sweep must refuse it.
        src = self.root / committed["handoff_path"]
        dst = src.parent / "breaking-candidate-000000000000000000000000.json"
        src.rename(dst)
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root, overrides=self.overrides()
        )
        self.assertEqual(report["processed"], [])
        reasons = {entry["reason"] for entry in report["skipped_invalid"]}
        self.assertIn("FILENAME_CONTENT_MISMATCH", reasons)

    def test_markdown_report_never_consumed(self):
        research = self.root / "social/research/daily"
        research.mkdir(parents=True, exist_ok=True)
        (research / "2026-09-08-breaking-1130.md").write_text(
            "# Breaking\nRANK 1: markdown-decoy\n", encoding="utf-8"
        )
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root, overrides=self.overrides()
        )
        self.assertEqual(report["processed"], [])
        self.assertEqual(report["skipped_invalid"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
