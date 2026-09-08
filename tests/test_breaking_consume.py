#!/usr/bin/env python3
"""Breaking spool consumer tests (#80).

Proves receipt-authoritative sweep: committed handoffs processed only
when listed by a strict registry-backed scan receipt, orphan/unlisted/
missing-receipt paths never execute, authoritative spool corruption fails
the sweep non-zero, unexpected runner crashes fail the CLI closed, and
replay-safe second sweeps remain idempotent.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
from nullone_breaking_scan_authority import (  # noqa: E402
    BreakingScanAuthorityError,
    validate_committed_scan_identity,
)
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
SCAN_ID = "breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z"


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
        self.workflow_calls = 0

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
            # TEST ONLY: authoritative recheck. Production supplies None.
            "dependency_recheck": lambda _stage: True,
        }
        base.update(extra)
        return base

    def counting_runner(self, raises=None):
        real = _consume.run_breaking_candidate

        def _wrapped(*args, **kwargs):
            self.workflow_calls += 1
            if raises is not None:
                raise raises
            return real(*args, **kwargs)

        return _wrapped

    def commit(self, candidate_id="acme-widget-launch", **overrides):
        staging = self.root / "social/ops/breaking-staging"
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / "staged.json"
        staged.write_text(
            json.dumps(make_assessment(candidate_id, **overrides)), encoding="utf-8"
        )
        return _scan.commit_assessment(
            assessment_path=staged,
            source="openclaw",
            at=AT,
            workspace_root=self.root,
        )

    def scan_dir(self) -> Path:
        return self.root / "social/ops/breaking-handoffs" / SCAN_ID

    def assert_failed_authority(self, report, *, reason):
        self.assertEqual(report["sweep_status"], "FAILED")
        self.assertEqual(report["reason_code"], "SWEEP_AUTHORITY_CORRUPT")
        self.assertNotIn("FAKE_SECRET", json.dumps(report))
        reasons = {entry["reason"] for entry in report["establishment_failed"]}
        self.assertIn(reason, reasons)

    def test_empty_spool_sweeps_clean(self):
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root, overrides=self.overrides()
        )
        self.assertEqual(report["sweep_status"], "COMPLETED")
        self.assertEqual(report["scans_seen"], 0)
        self.assertEqual(report["processed"], [])
        self.assertEqual(report["skipped_invalid"], [])
        self.assertEqual(report["establishment_failed"], [])

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

    def test_unlisted_extra_handoff_never_executed(self):
        committed = self.commit("acme-widget-launch")
        extra = self.scan_dir() / "breaking-candidate-bbbbbbbbbbbbbbbbbbbbbbbb.json"
        extra.write_text(
            (self.root / committed["handoff_path"]).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(
                run_breaking_candidate=self.counting_runner()
            ),
        )
        self.assertEqual(report["sweep_status"], "COMPLETED")
        self.assertEqual(self.workflow_calls, 1)
        self.assertEqual(len(report["processed"]), 1)
        reasons = {entry["reason"] for entry in report["skipped_invalid"]}
        self.assertIn("CANDIDATE_NOT_LISTED", reasons)

    def test_malformed_file_unlisted_does_not_block_valid_candidate(self):
        self.commit("acme-widget-launch")
        (self.scan_dir() / "zzz-broken.json").write_text("{not json", encoding="utf-8")
        (self.scan_dir() / "notes.txt").write_text("human note", encoding="utf-8")
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root, overrides=self.overrides()
        )
        self.assertEqual(report["sweep_status"], "COMPLETED")
        self.assertEqual(len(report["processed"]), 1)
        reasons = {entry["reason"] for entry in report["skipped_invalid"]}
        self.assertIn("CANDIDATE_NOT_LISTED", reasons)

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

    def test_handoff_without_receipt_fails_sweep(self):
        committed = self.commit()
        (self.scan_dir() / "scan-receipt.json").unlink()
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(
                run_breaking_candidate=self.counting_runner()
            ),
        )
        self.assertEqual(self.workflow_calls, 0)
        self.assertEqual(report["processed"], [])
        self.assert_failed_authority(report, reason="RECEIPT_MISSING")
        self.assertTrue((self.root / committed["handoff_path"]).is_file())
        self.assertNotEqual(
            0 if report.get("sweep_status") == "COMPLETED" else 1, 0
        )

    def test_malformed_receipt_fails_sweep(self):
        self.commit()
        (self.scan_dir() / "scan-receipt.json").write_text("{not json", encoding="utf-8")
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(
                run_breaking_candidate=self.counting_runner()
            ),
        )
        self.assertEqual(self.workflow_calls, 0)
        self.assert_failed_authority(report, reason="RECEIPT_REJECTED")

    def test_listed_candidate_missing_file_fails_sweep(self):
        committed = self.commit()
        (self.root / committed["handoff_path"]).unlink()
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(
                run_breaking_candidate=self.counting_runner()
            ),
        )
        self.assertEqual(self.workflow_calls, 0)
        self.assert_failed_authority(report, reason="CANDIDATE_FILE_MISSING")

    def test_no_material_with_orphan_handoff_fails_sweep(self):
        committed = self.commit()
        handoff_path = self.root / committed["handoff_path"]
        receipt_path = self.scan_dir() / "scan-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["status"] = "NO_MATERIAL_DEVELOPMENT"
        receipt["candidates"] = []
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(
                run_breaking_candidate=self.counting_runner()
            ),
        )
        self.assertEqual(self.workflow_calls, 0)
        self.assert_failed_authority(report, reason="RECEIPT_INCONSISTENT")
        self.assertTrue(handoff_path.is_file())

    def test_listed_malformed_handoff_fails_sweep(self):
        committed = self.commit()
        path = self.root / committed["handoff_path"]
        path.write_text("{not a handoff", encoding="utf-8")
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(
                run_breaking_candidate=self.counting_runner()
            ),
        )
        self.assertEqual(self.workflow_calls, 0)
        self.assert_failed_authority(report, reason="UNREADABLE")

    def test_filename_content_mismatch_fails_sweep(self):
        committed = self.commit()
        src = self.root / committed["handoff_path"]
        receipt_path = self.scan_dir() / "scan-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        listed_id = receipt["candidates"][0]
        dst = src.parent / "breaking-candidate-000000000000000000000000.json"
        src.rename(dst)
        receipt["candidates"] = [dst.stem]
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(
                run_breaking_candidate=self.counting_runner()
            ),
        )
        self.assertEqual(self.workflow_calls, 0)
        self.assert_failed_authority(report, reason="FILENAME_CONTENT_MISMATCH")
        self.assertNotEqual(listed_id, dst.stem)

    def test_receipt_not_listing_handoff_lists_missing_fails(self):
        committed = self.commit()
        receipt_path = self.scan_dir() / "scan-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["candidates"] = ["breaking-candidate-aaaaaaaaaaaaaaaaaaaaaaaa"]
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(
                run_breaking_candidate=self.counting_runner()
            ),
        )
        self.assertEqual(self.workflow_calls, 0)
        self.assertEqual(report["processed"], [])
        # Unlisted orphan is non-authoritative skip; listed missing file fails.
        self.assert_failed_authority(report, reason="CANDIDATE_FILE_MISSING")
        skip_reasons = {entry["reason"] for entry in report["skipped_invalid"]}
        self.assertIn("CANDIDATE_NOT_LISTED", skip_reasons)
        self.assertTrue((self.root / committed["handoff_path"]).is_file())

    def test_fake_scan_identity_rejected(self):
        fake_id = "breaking-radar.fake-slot.v1@2026-09-08T07:30:00Z"
        fake_dir = self.root / "social/ops/breaking-handoffs" / fake_id
        fake_dir.mkdir(parents=True)
        receipt = {
            "schema": "nullone.breaking-radar-scan-receipt.v1",
            "contract_version": "1.0.0",
            "source_occurrence_id": fake_id,
            "scheduled_for": "2026-09-08T07:30:00Z",
            "source": "openclaw",
            "status": "NO_MATERIAL_DEVELOPMENT",
            "candidates": [],
            "created_at": "2026-09-08T07:35:00Z",
        }
        (fake_dir / "scan-receipt.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(
                run_breaking_candidate=self.counting_runner()
            ),
        )
        self.assertEqual(self.workflow_calls, 0)
        self.assert_failed_authority(report, reason="RECEIPT_IDENTITY_MISMATCH")

    def test_registry_backed_scan_identity_helper(self):
        validate_committed_scan_identity(
            source="openclaw",
            scheduled_for="2026-09-08T07:30:00Z",
            source_occurrence_id=SCAN_ID,
            scan_directory_name=SCAN_ID,
        )
        with self.assertRaises(BreakingScanAuthorityError):
            validate_committed_scan_identity(
                source="openclaw",
                scheduled_for="2026-09-08T07:30:00Z",
                source_occurrence_id="anything@2026-09-08T07:30:00Z",
                scan_directory_name="anything@2026-09-08T07:30:00Z",
            )

    def test_orphan_recommit_then_consumer_processes_once(self):
        t1 = "2026-09-08T07:35:00Z"
        t2 = "2026-09-08T08:15:00Z"
        staging = self.root / "social/ops/breaking-staging"
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / "staged.json"
        staged.write_text(json.dumps(make_assessment()), encoding="utf-8")
        first = _scan.commit_assessment(
            assessment_path=staged, source="openclaw", at=t1, workspace_root=self.root
        )
        (self.scan_dir() / "scan-receipt.json").unlink()
        _scan.commit_assessment(
            assessment_path=staged, source="openclaw", at=t2, workspace_root=self.root
        )
        preserved = json.loads(
            (self.root / first["handoff_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(preserved["occurrence"]["triggered_at"], t1)
        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root, overrides=self.overrides()
        )
        self.assertEqual(report["sweep_status"], "COMPLETED")
        self.assertEqual(len(report["processed"]), 1)

    def test_unexpected_runner_crash_nonzero_cli_hides_marker(self):
        self.commit()
        marker = "FAKE_SECRET_MARKER"

        def _boom(*_a, **_k):
            raise RuntimeError(marker)

        report = _consume.sweep_breaking_handoffs(
            workspace_root=self.root,
            overrides=self.overrides(run_breaking_candidate=_boom),
        )
        self.assertEqual(report["sweep_status"], "FAILED")
        self.assertEqual(report["reason_code"], "SWEEP_RUNTIME_FAILED")
        self.assertNotIn(marker, json.dumps(report))
        self.assertEqual(report["processed"], [])

        real_sweep = _consume.sweep_breaking_handoffs

        def _failing_sweep(**kwargs):
            kwargs = dict(kwargs)
            overrides = dict(kwargs.get("overrides") or {})
            overrides["run_breaking_candidate"] = _boom
            overrides.setdefault("story_writer", digit_free_writer)
            overrides.setdefault("story_verifier", numeric_scope_verifier)
            overrides.setdefault("draft_connector", FakeDraftConnector())
            overrides.setdefault(
                "review_delivery", FakeReviewDelivery(status="SENT")
            )
            overrides.setdefault("notifier", lambda _r: {"status": "NOT_REQUIRED"})
            overrides.setdefault("dependency_recheck", lambda _s: True)
            kwargs["overrides"] = overrides
            kwargs.setdefault("workspace_root", self.root)
            return real_sweep(**kwargs)

        with mock.patch.object(_consume, "sweep_breaking_handoffs", _failing_sweep):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with mock.patch.object(
                    sys, "argv", ["nullone-breaking-consume.py", "sweep"]
                ):
                    exit_code = _consume.main()
            printed = buf.getvalue()
        self.assertNotEqual(exit_code, 0)
        self.assertNotIn(marker, printed)
        self.assertIn("SWEEP_RUNTIME_FAILED", printed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
