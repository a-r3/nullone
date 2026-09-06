#!/usr/bin/env python3
"""Behavioral tests for the #32 read-only cadence state adapter.

Exercises nullone_cadence_state_adapter.collect_format_loads() against
temp-fixture manifest directories and publish-ledger files only. No
production state, no network.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_cadence_state_adapter import (  # noqa: E402
    CadenceStateError,
    assemble_cadence_request,
    collect_format_loads,
)

BAKU = ZoneInfo("Asia/Baku")
NOW = datetime(2026, 9, 6, 17, 0, 0, tzinfo=BAKU)


def write_manifest(
    manifest_dir: Path,
    manifest_id: str,
    *,
    fmt: str,
    review_state: str = "NOT_CREATED",
    first_stage: bool = False,
    final_publish: bool = False,
    publication_state: str = "NOT_REQUESTED",
    live_zernio_post_id: str | None = None,
) -> None:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": "nullone.production.v1",
        "manifest_id": manifest_id,
        "format": fmt,
        "review": {"state": review_state, "zernio_draft_id": None},
        "approval": {"first_stage": first_stage, "final_publish": final_publish},
        "publication": {
            "state": publication_state,
            "live_zernio_post_id": live_zernio_post_id,
        },
    }
    (manifest_dir / f"{manifest_id}.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def append_ledger_row(
    ledger_path: Path,
    *,
    fmt: str,
    result: str,
    timestamp: str,
    manifest_id: str | None = None,
    live_zernio_post_id: str | None = None,
) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "format": fmt,
        "result": result,
        "timestamp": timestamp,
        "manifest_id": manifest_id,
        "live_zernio_post_id": live_zernio_post_id,
    }
    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


class InitializedEmptyStateTests(unittest.TestCase):
    def test_existing_empty_state_root_returns_zero_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            # No publish-ledger.jsonl at all: legitimately no publications yet.

            result = collect_format_loads(state_root=root, now=NOW)

            self.assertEqual(
                result["main_load"],
                {"published_today": 0, "pending": 0, "last_published_at": None},
            )
            self.assertEqual(
                result["story_load"],
                {"published_today": 0, "pending": 0, "last_published_at": None},
            )

    def test_root_with_no_manifest_dir_and_no_ledger_is_still_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.mkdir(exist_ok=True)  # root itself exists; nothing under it yet

            result = collect_format_loads(state_root=root, now=NOW)

            self.assertEqual(result["main_load"]["published_today"], 0)
            self.assertEqual(result["main_load"]["pending"], 0)


class MissingRequiredStateTests(unittest.TestCase):
    def test_missing_state_root_raises(self):
        with tempfile.TemporaryDirectory() as td:
            missing_root = Path(td) / "does-not-exist"
            with self.assertRaises(CadenceStateError):
                collect_format_loads(state_root=missing_root, now=NOW)

    def test_naive_now_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(CadenceStateError):
                collect_format_loads(
                    state_root=root, now=datetime(2026, 9, 6, 17, 0, 0)
                )


class MalformedStateTests(unittest.TestCase):
    def test_malformed_manifest_json_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "broken.json").write_text("{not valid json", encoding="utf-8")

            with self.assertRaises(CadenceStateError):
                collect_format_loads(state_root=root, now=NOW)

    def test_manifest_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "incomplete.json").write_text(
                json.dumps({"manifest_id": "x", "format": "FEED"}),
                encoding="utf-8",
            )

            with self.assertRaises(CadenceStateError):
                collect_format_loads(state_root=root, now=NOW)

    def test_malformed_ledger_line_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"
            ledger_path.parent.mkdir(parents=True)
            ledger_path.write_text("{not valid json\n", encoding="utf-8")

            with self.assertRaises(CadenceStateError):
                collect_format_loads(state_root=root, now=NOW)


class NormalValidStateTests(unittest.TestCase):
    def test_main_and_story_counters_are_independent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(manifest_dir, "m-feed-pending", fmt="FEED", review_state="DRAFT_CREATED")
            write_manifest(manifest_dir, "m-story-pending", fmt="STORY", review_state="DRAFT_CREATED")

            append_ledger_row(
                ledger_path,
                fmt="CAROUSEL",
                result="PUBLISHED",
                timestamp="2026-09-06T09:00:00+04:00",
                manifest_id="m-carousel-published",
            )
            append_ledger_row(
                ledger_path,
                fmt="STORY",
                result="PUBLISHED",
                timestamp="2026-09-06T10:00:00+04:00",
                manifest_id="m-story-published",
            )

            result = collect_format_loads(state_root=root, now=NOW)

            self.assertEqual(result["main_load"]["published_today"], 1)  # CAROUSEL
            self.assertEqual(result["main_load"]["pending"], 1)  # FEED draft
            self.assertEqual(result["story_load"]["published_today"], 1)
            self.assertEqual(result["story_load"]["pending"], 1)

    def test_each_pending_state_class_counts_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"

            write_manifest(manifest_dir, "draft", fmt="FEED", review_state="DRAFT_CREATED")
            write_manifest(
                manifest_dir,
                "approved-not-final",
                fmt="FEED",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=False,
                publication_state="NOT_REQUESTED",
            )
            for state in (
                "PUBLISH_IN_FLIGHT",
                "PUBLISH_ACCEPTED",
                "PUBLISHING",
                "CHECK_REQUIRED",
                "UNKNOWN",
                "READBACK_FAILED",
            ):
                write_manifest(
                    manifest_dir,
                    f"pub-{state.lower()}",
                    fmt="FEED",
                    review_state="DRAFT_CREATED",
                    first_stage=True,
                    final_publish=True,
                    publication_state=state,
                )
            write_manifest(
                manifest_dir,
                "definitively-failed",
                fmt="FEED",
                review_state="DRAFT_CREATED",
                publication_state="FAILED",
            )
            write_manifest(
                manifest_dir,
                "pre-review",
                fmt="FEED",
                review_state="NOT_CREATED",
                publication_state="NOT_REQUESTED",
            )

            result = collect_format_loads(state_root=root, now=NOW)

            # draft + approved-not-final + the 6 unsafe publication states = 8.
            self.assertEqual(result["main_load"]["pending"], 8)
            self.assertEqual(result["main_load"]["published_today"], 0)

    def test_unknown_publication_state_counts_as_pending_not_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            write_manifest(
                manifest_dir,
                "ambiguous",
                fmt="FEED",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=True,
                publication_state="UNKNOWN",
            )

            result = collect_format_loads(state_root=root, now=NOW)

            self.assertEqual(result["main_load"]["pending"], 1)
            self.assertEqual(result["main_load"]["published_today"], 0)

    def test_baku_midnight_boundary_excludes_previous_day_publication(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_ledger_row(
                ledger_path,
                fmt="STORY",
                result="PUBLISHED",
                timestamp="2026-09-06T23:50:00+04:00",
                manifest_id="m-late-story",
            )

            just_after_midnight = datetime(2026, 9, 7, 0, 5, 0, tzinfo=BAKU)
            result = collect_format_loads(state_root=root, now=just_after_midnight)

            self.assertEqual(result["story_load"]["published_today"], 0)
            self.assertEqual(
                result["story_load"]["last_published_at"],
                "2026-09-06T23:50:00+04:00",
            )

    def test_last_published_at_is_most_recent_regardless_of_today(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_ledger_row(
                ledger_path,
                fmt="FEED",
                result="PUBLISHED",
                timestamp="2026-09-05T20:00:00+04:00",
                manifest_id="m-old",
            )
            append_ledger_row(
                ledger_path,
                fmt="FEED",
                result="PUBLISHED",
                timestamp="2026-09-06T09:00:00+04:00",
                manifest_id="m-newer",
            )

            result = collect_format_loads(state_root=root, now=NOW)

            self.assertEqual(
                result["main_load"]["last_published_at"],
                "2026-09-06T09:00:00+04:00",
            )


class PrecedenceConflictTests(unittest.TestCase):
    def test_confirmed_ledger_publication_overrides_stale_pending_manifest(self):
        # The manifest is locally stale (still shows PUBLISH_ACCEPTED),
        # but the append-only ledger already recorded PUBLISHED for the
        # same manifest_id. The ledger must win: this must count as
        # published, not double-count as pending too.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(
                manifest_dir,
                "reconciled",
                fmt="FEED",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=True,
                publication_state="PUBLISH_ACCEPTED",
                live_zernio_post_id="live-123",
            )
            append_ledger_row(
                ledger_path,
                fmt="FEED",
                result="PUBLISHED",
                timestamp="2026-09-06T09:00:00+04:00",
                manifest_id="reconciled",
                live_zernio_post_id="live-123",
            )

            result = collect_format_loads(state_root=root, now=NOW)

            self.assertEqual(result["main_load"]["published_today"], 1)
            self.assertEqual(result["main_load"]["pending"], 0)

    def test_failed_manifest_never_counts_as_pending_or_published(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            write_manifest(
                manifest_dir,
                "clean-failure",
                fmt="STORY",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=False,
                publication_state="FAILED",
            )

            result = collect_format_loads(state_root=root, now=NOW)

            self.assertEqual(result["story_load"]["pending"], 0)
            self.assertEqual(result["story_load"]["published_today"], 0)


class AssembleCadenceRequestTests(unittest.TestCase):
    def test_assembles_full_contract_shaped_request(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)

            request = assemble_cadence_request(
                state_root=root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": True,
                    "story_quality_candidate_available": False,
                },
            )

            self.assertEqual(request["schema"], "nullone.cadence-contract.v1")
            self.assertEqual(request["timezone"], "Asia/Baku")
            self.assertIn("main_load", request)
            self.assertIn("story_load", request)
            self.assertEqual(request["signal"], {"breaking_day": False, "downtime_marker": None})

            # The assembled request must be directly consumable by the
            # pure evaluator with no further transformation.
            sys.path.insert(0, str(SCRIPTS))
            from nullone_cadence_controller import evaluate_cadence

            result = evaluate_cadence(request)
            self.assertIn(result["recommendation"], {"NO_ACTION", "PREPARE_MAIN_CANDIDATE", "PREPARE_STORY"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
