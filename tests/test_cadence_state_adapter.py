#!/usr/bin/env python3
"""Behavioral tests for the #32 read-only cadence state adapter.

Exercises nullone_cadence_state_adapter.collect_format_loads() against
temp-fixture manifest directories and publish-ledger files only. No
production state, no network.
"""
from __future__ import annotations

import hashlib
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
    analyze_ledger_compatibility,
    assemble_cadence_request,
    collect_format_loads,
)
from nullone_cadence_controller import evaluate_cadence  # noqa: E402

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


def append_raw_ledger_row(ledger_path: Path, row: dict) -> None:
    """Append an arbitrary dict as one JSONL row (issue #60 fixtures).

    Unlike append_ledger_row(), this never injects a `format` key --
    used to reproduce historical pre-#60 rows that never carried it, and
    rows carrying an explicit invalid `format` value.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
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


class NativeFormatRowTests(unittest.TestCase):
    """#60 item 1-3: current native rows are unaffected by the compat layer."""

    def test_native_feed_row_is_native_and_compatible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_ledger_row(
                ledger_path, fmt="FEED", result="PUBLISHED", timestamp="2026-09-06T09:00:00+04:00"
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["status"], "COMPATIBLE")
            self.assertEqual(report["native_format_rows"], 1)
            self.assertEqual(report["recovered_format_rows"], 0)
            self.assertEqual(report["unknown_format_rows"], 0)

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 1)

    def test_native_carousel_row_is_native_and_compatible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_ledger_row(
                ledger_path, fmt="CAROUSEL", result="PUBLISHED", timestamp="2026-09-06T09:00:00+04:00"
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["native_format_rows"], 1)

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 1)

    def test_native_story_row_is_native_and_compatible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_ledger_row(
                ledger_path, fmt="STORY", result="PUBLISHED", timestamp="2026-09-06T09:00:00+04:00"
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["native_format_rows"], 1)

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["story_load"]["published_today"], 1)


class HistoricalFormatRecoveryTests(unittest.TestCase):
    """#60 items 4-9: deterministic read-time recovery from manifest linkage."""

    def test_missing_format_recovered_via_manifest_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(manifest_dir, "m-story", fmt="STORY")
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-08-01T09:00:00+04:00",  # old: decision-irrelevant
                    "manifest_id": "m-story",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["recovered_format_rows"], 1)
            self.assertEqual(report["unknown_format_rows"], 0)
            self.assertEqual(
                report["recovered_rows"][0]["format_source"], "MANIFEST_ID"
            )
            self.assertEqual(report["recovered_rows"][0]["effective_format"], "STORY")

    def test_missing_format_recovered_via_live_zernio_post_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(
                manifest_dir, "m-story-live", fmt="STORY", live_zernio_post_id="live-9"
            )
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-08-01T09:00:00+04:00",
                    "live_zernio_post_id": "live-9",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["recovered_format_rows"], 1)
            self.assertEqual(
                report["recovered_rows"][0]["format_source"], "LIVE_ZERNIO_POST_ID"
            )
            self.assertEqual(report["recovered_rows"][0]["effective_format"], "STORY")

    def test_missing_format_recovery_both_links_agree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(
                manifest_dir, "m-agree", fmt="STORY", live_zernio_post_id="live-agree"
            )
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-09-06T10:00:00+04:00",  # today: proves recovery via counts
                    "manifest_id": "m-agree",
                    "live_zernio_post_id": "live-agree",
                },
            )

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["story_load"]["published_today"], 1)
            self.assertEqual(loads["main_load"]["published_today"], 0)

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["recovered_format_rows"], 1)
            self.assertEqual(report["recovered_rows"][0]["effective_format"], "STORY")

    def test_missing_format_recovery_links_conflict_is_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(manifest_dir, "m-feed", fmt="FEED")
            write_manifest(
                manifest_dir,
                "m-story-live-conflict",
                fmt="STORY",
                live_zernio_post_id="live-conflict",
            )
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",  # old: decision-irrelevant
                    "manifest_id": "m-feed",
                    "live_zernio_post_id": "live-conflict",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["recovered_format_rows"], 0)
            self.assertEqual(report["unknown_format_rows"], 1)
            self.assertFalse(report["unresolved_rows"][0]["decision_relevant"])

            # Old and irrelevant: a normal read still succeeds, excluding
            # the unresolved row from both buckets rather than guessing.
            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 0)
            self.assertEqual(loads["story_load"]["published_today"], 0)

    def test_missing_format_no_linkage_is_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"

            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",
                    "manifest_id": "does-not-exist",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["unknown_format_rows"], 1)
            self.assertFalse(report["unresolved_rows"][0]["decision_relevant"])

    def test_duplicate_live_id_evidence_fails_safe(self):
        # #60 section 13: one identifier mapping to multiple incompatible
        # manifests/formats must never be silently resolved.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(manifest_dir, "m-dup-a", fmt="FEED", live_zernio_post_id="live-dup")
            write_manifest(manifest_dir, "m-dup-b", fmt="STORY", live_zernio_post_id="live-dup")
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",
                    "live_zernio_post_id": "live-dup",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["unknown_format_rows"], 1)
            self.assertEqual(report["recovered_format_rows"], 0)

    def test_mixed_native_recovered_unresolved_rows_counts_correctly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(manifest_dir, "m-story-mix", fmt="STORY")

            append_ledger_row(
                ledger_path, fmt="FEED", result="PUBLISHED", timestamp="2026-09-06T08:00:00+04:00"
            )
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-09-06T10:00:00+04:00",
                    "manifest_id": "m-story-mix",
                },
            )
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",  # old, no linkage
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["native_format_rows"], 1)
            self.assertEqual(report["recovered_format_rows"], 1)
            self.assertEqual(report["unknown_format_rows"], 1)
            self.assertEqual(report["status"], "DEGRADED_UNKNOWN_FORMAT")

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 1)
            self.assertEqual(loads["story_load"]["published_today"], 1)


class DecisionRelevanceTests(unittest.TestCase):
    """#60 items 10-12: unresolved-format decision relevance rule."""

    def test_unresolved_published_row_today_blocks_normal_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"

            append_raw_ledger_row(
                ledger_path,
                {"result": "PUBLISHED", "timestamp": "2026-09-06T08:00:00+04:00"},
            )

            with self.assertRaises(CadenceStateError):
                collect_format_loads(state_root=root, now=NOW)

            with self.assertRaises(CadenceStateError):
                assemble_cadence_request(
                    state_root=root,
                    now=NOW,
                    candidate_availability={
                        "main_quality_candidate_available": True,
                        "story_quality_candidate_available": True,
                    },
                )

    def test_unresolved_published_row_inside_spacing_window_across_midnight_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"

            # 10 minutes before local midnight-plus-5: inside both the
            # default main (120min) and Story (45min) spacing windows,
            # but its Baku calendar date is yesterday relative to `now`.
            append_raw_ledger_row(
                ledger_path,
                {"result": "PUBLISHED", "timestamp": "2026-09-06T23:50:00+04:00"},
            )

            just_after_midnight = datetime(2026, 9, 7, 0, 5, 0, tzinfo=BAKU)

            with self.assertRaises(CadenceStateError):
                collect_format_loads(state_root=root, now=just_after_midnight)

    def test_unresolved_published_row_old_enough_is_decision_irrelevant(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"

            # Two days earlier and far outside any spacing window.
            append_raw_ledger_row(
                ledger_path,
                {"result": "PUBLISHED", "timestamp": "2026-09-04T09:00:00+04:00"},
            )

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 0)
            self.assertEqual(loads["story_load"]["published_today"], 0)

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["unknown_format_rows"], 1)
            self.assertFalse(report["unresolved_rows"][0]["decision_relevant"])

            # A normal evaluation still succeeds end to end.
            request = assemble_cadence_request(
                state_root=root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": True,
                    "story_quality_candidate_available": True,
                },
            )
            result = evaluate_cadence(request)
            self.assertIn(
                result["recommendation"],
                {"NO_ACTION", "PREPARE_MAIN_CANDIDATE", "PREPARE_STORY"},
            )

    def test_non_published_missing_format_row_never_blocks(self):
        # Section 12: a result that never participates in audience-facing
        # count/index logic is compatibility-diagnostic only, regardless
        # of timing.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"

            append_raw_ledger_row(
                ledger_path,
                {"result": "FAILED", "timestamp": "2026-09-06T08:00:00+04:00"},
            )

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 0)
            self.assertEqual(loads["story_load"]["published_today"], 0)


class UnsafeRecommendationSuppressionTests(unittest.TestCase):
    """#60 section 16: explicit unsafe-PREPARE_* suppression proofs."""

    def test_unresolved_today_row_prevents_prepare_story(self):
        candidate_availability = {
            "main_quality_candidate_available": True,
            "story_quality_candidate_available": True,
        }

        # Control: with main load already met and no unresolved row, the
        # real Story gap would normally produce PREPARE_STORY.
        with tempfile.TemporaryDirectory() as td_control:
            root = Path(td_control)
            ledger_path = root / "state/publish-ledger.jsonl"
            (root / "ops/manifests").mkdir(parents=True)
            append_ledger_row(
                ledger_path, fmt="FEED", result="PUBLISHED", timestamp="2026-09-06T08:00:00+04:00"
            )
            append_ledger_row(
                ledger_path,
                fmt="CAROUSEL",
                result="PUBLISHED",
                timestamp="2026-09-06T09:00:00+04:00",
            )

            control_request = assemble_cadence_request(
                state_root=root, now=NOW, candidate_availability=candidate_availability
            )
            control_result = evaluate_cadence(control_request)
            self.assertEqual(control_result["recommendation"], "PREPARE_STORY")

        # Same main-satisfied state, plus one unresolved PUBLISHED row
        # dated today with no linkage: must never reach PREPARE_STORY.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_path = root / "state/publish-ledger.jsonl"
            (root / "ops/manifests").mkdir(parents=True)
            append_ledger_row(
                ledger_path, fmt="FEED", result="PUBLISHED", timestamp="2026-09-06T08:00:00+04:00"
            )
            append_ledger_row(
                ledger_path,
                fmt="CAROUSEL",
                result="PUBLISHED",
                timestamp="2026-09-06T09:00:00+04:00",
            )
            append_raw_ledger_row(
                ledger_path,
                {"result": "PUBLISHED", "timestamp": "2026-09-06T11:00:00+04:00"},
            )

            with self.assertRaises(CadenceStateError):
                assemble_cadence_request(
                    state_root=root, now=NOW, candidate_availability=candidate_availability
                )

    def test_unresolved_today_row_prevents_prepare_main_candidate(self):
        candidate_availability = {
            "main_quality_candidate_available": True,
            "story_quality_candidate_available": True,
        }

        # Control: fully empty state with a candidate available produces
        # PREPARE_MAIN_CANDIDATE (main is checked before Story).
        with tempfile.TemporaryDirectory() as td_control:
            root = Path(td_control)
            (root / "ops/manifests").mkdir(parents=True)

            control_request = assemble_cadence_request(
                state_root=root, now=NOW, candidate_availability=candidate_availability
            )
            control_result = evaluate_cadence(control_request)
            self.assertEqual(control_result["recommendation"], "PREPARE_MAIN_CANDIDATE")

        # Same empty state, plus one unresolved PUBLISHED row dated today
        # with no linkage: must never reach PREPARE_MAIN_CANDIDATE.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_raw_ledger_row(
                ledger_path,
                {"result": "PUBLISHED", "timestamp": "2026-09-06T11:00:00+04:00"},
            )

            with self.assertRaises(CadenceStateError):
                assemble_cadence_request(
                    state_root=root, now=NOW, candidate_availability=candidate_availability
                )

    def test_safe_old_irrelevant_history_still_allows_normal_evaluation(self):
        candidate_availability = {
            "main_quality_candidate_available": True,
            "story_quality_candidate_available": True,
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_raw_ledger_row(
                ledger_path,
                {"result": "PUBLISHED", "timestamp": "2026-09-01T09:00:00+04:00"},
            )

            request = assemble_cadence_request(
                state_root=root, now=NOW, candidate_availability=candidate_availability
            )
            result = evaluate_cadence(request)
            self.assertEqual(result["recommendation"], "PREPARE_MAIN_CANDIDATE")

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["unknown_format_rows"], 1)


class RequiredFieldCompatibilityTests(unittest.TestCase):
    """#60 item 4 (defect) + items 14/15/16: field-level requirements."""

    def test_missing_timestamp_still_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_raw_ledger_row(ledger_path, {"result": "PUBLISHED", "format": "FEED"})

            with self.assertRaises(CadenceStateError):
                collect_format_loads(state_root=root, now=NOW)

    def test_missing_result_still_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_raw_ledger_row(
                ledger_path, {"timestamp": "2026-09-06T09:00:00+04:00", "format": "FEED"}
            )

            with self.assertRaises(CadenceStateError):
                collect_format_loads(state_root=root, now=NOW)

    def test_missing_format_alone_is_not_automatically_malformed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_raw_ledger_row(
                ledger_path,
                {"result": "PUBLISHED", "timestamp": "2026-01-01T09:00:00+04:00"},
            )

            # Does not raise: a historical row missing only `format` is a
            # compatibility case, not malformed input.
            collect_format_loads(state_root=root, now=NOW)

    def test_unknown_invalid_explicit_format_treated_like_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)
            ledger_path = root / "state/publish-ledger.jsonl"
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",
                    "format": "BOGUS_LEGACY_VALUE",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["unknown_format_rows"], 1)
            self.assertEqual(report["native_format_rows"], 0)


class ExistingManifestSemanticsRegressionTests(unittest.TestCase):
    """#60 items 17/18: consequential manifest states are unchanged."""

    def test_existing_unknown_manifest_state_remains_pending(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            write_manifest(
                manifest_dir,
                "still-unknown",
                fmt="FEED",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=True,
                publication_state="UNKNOWN",
            )

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["pending"], 1)
            self.assertEqual(loads["main_load"]["published_today"], 0)

    def test_existing_check_required_manifest_state_remains_pending(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            write_manifest(
                manifest_dir,
                "still-check-required",
                fmt="STORY",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=True,
                publication_state="CHECK_REQUIRED",
            )

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["story_load"]["pending"], 1)
            self.assertEqual(loads["story_load"]["published_today"], 0)


class ReadOnlyProofTests(unittest.TestCase):
    """#60 item 20 / section 17: the ledger is never mutated."""

    def test_ledger_bytes_unchanged_after_adapter_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(manifest_dir, "m-readonly", fmt="STORY")
            append_ledger_row(
                ledger_path, fmt="FEED", result="PUBLISHED", timestamp="2026-09-06T09:00:00+04:00"
            )
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",
                    "manifest_id": "m-readonly",
                },
            )
            append_raw_ledger_row(
                ledger_path,
                {"result": "PUBLISHED", "timestamp": "2026-01-02T09:00:00+04:00"},
            )

            before_bytes = ledger_path.read_bytes()
            before_hash = hashlib.sha256(before_bytes).hexdigest()

            collect_format_loads(state_root=root, now=NOW)
            analyze_ledger_compatibility(state_root=root, now=NOW)
            assemble_cadence_request(
                state_root=root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": True,
                    "story_quality_candidate_available": True,
                },
            )

            after_bytes = ledger_path.read_bytes()
            after_hash = hashlib.sha256(after_bytes).hexdigest()

            self.assertEqual(before_bytes, after_bytes)
            self.assertEqual(before_hash, after_hash)


class DuplicateManifestIdEvidenceTests(unittest.TestCase):
    """Hardening finding A: duplicate manifest_id must not be resolved by
    file order/filename/mtime -- it must preserve all format evidence."""

    def test_duplicate_same_format_manifest_id_still_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            # Two manifest files both claim manifest_id "same", agreeing
            # on STORY. This is unusual but not itself ambiguous: the
            # identifier's evidence set is {STORY}, one format.
            write_manifest(manifest_dir, "same", fmt="STORY")
            (manifest_dir / "same-duplicate.json").write_text(
                (manifest_dir / "same.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",
                    "manifest_id": "same",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["recovered_format_rows"], 1)
            self.assertEqual(report["recovered_rows"][0]["effective_format"], "STORY")

    def test_duplicate_conflicting_format_manifest_id_is_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            # Two manifest files share manifest_id "same" but disagree on
            # format: FEED vs STORY. Whichever file glob/sort happens to
            # read last must NOT silently win.
            write_manifest(manifest_dir, "same", fmt="FEED")
            (manifest_dir / "aaa-conflict.json").write_text(
                json.dumps(
                    {
                        "schema": "nullone.production.v1",
                        "manifest_id": "same",
                        "format": "STORY",
                        "review": {"state": "DRAFT_CREATED", "zernio_draft_id": None},
                        "approval": {"first_stage": False, "final_publish": False},
                        "publication": {"state": "NOT_REQUESTED", "live_zernio_post_id": None},
                    }
                ),
                encoding="utf-8",
            )

            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",  # old/irrelevant
                    "manifest_id": "same",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["recovered_format_rows"], 0)
            self.assertEqual(report["unknown_format_rows"], 1)

            # Old and irrelevant: a normal read still succeeds.
            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 0)
            self.assertEqual(loads["story_load"]["published_today"], 0)

    def test_duplicate_conflicting_manifest_id_today_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(manifest_dir, "same", fmt="FEED")
            (manifest_dir / "aaa-conflict.json").write_text(
                json.dumps(
                    {
                        "schema": "nullone.production.v1",
                        "manifest_id": "same",
                        "format": "STORY",
                        "review": {"state": "DRAFT_CREATED", "zernio_draft_id": None},
                        "approval": {"first_stage": False, "final_publish": False},
                        "publication": {"state": "NOT_REQUESTED", "live_zernio_post_id": None},
                    }
                ),
                encoding="utf-8",
            )

            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-09-06T08:00:00+04:00",  # today
                    "manifest_id": "same",
                },
            )

            with self.assertRaises(CadenceStateError):
                collect_format_loads(state_root=root, now=NOW)


class PublishedIdReconciliationHardeningTests(unittest.TestCase):
    """Hardening finding B: unresolved rows must never contribute
    published_ids, and therefore must never suppress a pending manifest
    or manufacture a false cadence gap."""

    def test_unresolved_row_contributes_zero_published_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(manifest_dir, "m-feed", fmt="FEED")
            write_manifest(
                manifest_dir, "m-story", fmt="STORY", live_zernio_post_id="live-story"
            )

            # Conflicting evidence (manifest_id -> FEED, live id -> STORY)
            # -> UNKNOWN, and old enough to be decision-irrelevant.
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",
                    "manifest_id": "m-feed",
                    "live_zernio_post_id": "live-story",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["unknown_format_rows"], 1)

    def test_old_unresolved_conflicting_row_never_suppresses_pending_manifests(self):
        # This is the regression that would FAIL on pre-hardening head
        # 3ce74944deb0cead24bfcbcb999fb0a3bcec5753: raw published_ids
        # indexing there would add both "m-feed" and "live-story" to
        # published_ids from this single UNKNOWN row, silently
        # reconciling (and zeroing out) both pending manifests below.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(
                manifest_dir,
                "m-feed",
                fmt="FEED",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=False,
            )
            write_manifest(
                manifest_dir,
                "m-story",
                fmt="STORY",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=False,
                live_zernio_post_id="live-story",
            )

            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",  # old + irrelevant
                    "manifest_id": "m-feed",
                    "live_zernio_post_id": "live-story",
                },
            )

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["pending"], 1)
            self.assertEqual(loads["story_load"]["pending"], 1)

    def test_old_unresolved_conflicting_row_cannot_produce_false_prepare(self):
        # Controller-level proof of the same fixture: with correct
        # pending accounting (1 pending each) and a config whose minimums
        # are already met by that one pending item each (effective_load
        # == target_min == 1 => gap == False for both), the deterministic
        # evaluation must not recommend PREPARE_MAIN_CANDIDATE or
        # PREPARE_STORY. Pre-hardening, the same fixture's false
        # published_id suppression would have zeroed both pending counts
        # (effective_load 0 < target_min 1 => gap True for both) and,
        # with both candidate-availability flags true and neither format
        # recently active/quiet, produced PREPARE_MAIN_CANDIDATE instead
        # (main is checked before Story).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(
                manifest_dir,
                "m-feed",
                fmt="FEED",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=False,
            )
            write_manifest(
                manifest_dir,
                "m-story",
                fmt="STORY",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=False,
                live_zernio_post_id="live-story",
            )

            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-01-01T09:00:00+04:00",
                    "manifest_id": "m-feed",
                    "live_zernio_post_id": "live-story",
                },
            )

            config = {"main_target_min": 1, "story_target_min": 1}
            request = assemble_cadence_request(
                state_root=root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": True,
                    "story_quality_candidate_available": True,
                },
                config=config,
            )
            result = evaluate_cadence(request)

            self.assertNotEqual(result["recommendation"], "PREPARE_MAIN_CANDIDATE")
            self.assertNotEqual(result["recommendation"], "PREPARE_STORY")
            self.assertEqual(result["recommendation"], "NO_ACTION")
            self.assertEqual(result["reason_code"], "TARGETS_MET")

    def test_recovered_known_format_row_still_reconciles_its_own_manifest(self):
        # Section 6: the fix must not disable reconciliation outright --
        # a deterministically recovered row must still perform the
        # existing stale-pending-manifest-vs-confirmed-publish precedence.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"

            write_manifest(
                manifest_dir,
                "m-feed",
                fmt="FEED",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=True,
                publication_state="PUBLISH_ACCEPTED",
            )
            append_raw_ledger_row(
                ledger_path,
                {
                    "result": "PUBLISHED",
                    "timestamp": "2026-09-06T09:00:00+04:00",  # today
                    "manifest_id": "m-feed",
                },
            )

            report = analyze_ledger_compatibility(state_root=root, now=NOW)
            self.assertEqual(report["recovered_format_rows"], 1)
            self.assertEqual(report["recovered_rows"][0]["effective_format"], "FEED")

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 1)
            self.assertEqual(loads["main_load"]["pending"], 0)

    def test_native_feed_reconciliation_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"
            write_manifest(
                manifest_dir,
                "native-feed",
                fmt="FEED",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=True,
                publication_state="PUBLISH_ACCEPTED",
                live_zernio_post_id="live-native-feed",
            )
            append_ledger_row(
                ledger_path,
                fmt="FEED",
                result="PUBLISHED",
                timestamp="2026-09-06T09:00:00+04:00",
                manifest_id="native-feed",
                live_zernio_post_id="live-native-feed",
            )

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 1)
            self.assertEqual(loads["main_load"]["pending"], 0)

    def test_native_carousel_reconciliation_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"
            write_manifest(
                manifest_dir,
                "native-carousel",
                fmt="CAROUSEL",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=True,
                publication_state="PUBLISH_ACCEPTED",
            )
            append_ledger_row(
                ledger_path,
                fmt="CAROUSEL",
                result="PUBLISHED",
                timestamp="2026-09-06T09:00:00+04:00",
                manifest_id="native-carousel",
            )

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["main_load"]["published_today"], 1)
            self.assertEqual(loads["main_load"]["pending"], 0)

    def test_native_story_reconciliation_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "ops/manifests"
            ledger_path = root / "state/publish-ledger.jsonl"
            write_manifest(
                manifest_dir,
                "native-story",
                fmt="STORY",
                review_state="DRAFT_CREATED",
                first_stage=True,
                final_publish=True,
                publication_state="PUBLISH_ACCEPTED",
                live_zernio_post_id="live-native-story",
            )
            append_ledger_row(
                ledger_path,
                fmt="STORY",
                result="PUBLISHED",
                timestamp="2026-09-06T09:00:00+04:00",
                manifest_id="native-story",
                live_zernio_post_id="live-native-story",
            )

            loads = collect_format_loads(state_root=root, now=NOW)
            self.assertEqual(loads["story_load"]["published_today"], 1)
            self.assertEqual(loads["story_load"]["pending"], 0)


class SpacingConfigValidationTests(unittest.TestCase):
    """Item 12: malformed spacing config values must fail deterministically,
    never raise an unrelated TypeError or produce an unsafe classification."""

    def test_non_integer_main_spacing_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)

            with self.assertRaises(CadenceStateError):
                collect_format_loads(
                    state_root=root,
                    now=NOW,
                    config={"main_min_spacing_minutes": "not-a-number"},
                )

    def test_boolean_story_spacing_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)

            with self.assertRaises(CadenceStateError):
                collect_format_loads(
                    state_root=root,
                    now=NOW,
                    config={"story_min_spacing_minutes": True},
                )

    def test_negative_main_spacing_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)

            with self.assertRaises(CadenceStateError):
                collect_format_loads(
                    state_root=root,
                    now=NOW,
                    config={"main_min_spacing_minutes": -5},
                )

    def test_valid_integer_spacing_overrides_are_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ops/manifests").mkdir(parents=True)

            # Must not raise.
            collect_format_loads(
                state_root=root,
                now=NOW,
                config={"main_min_spacing_minutes": 30, "story_min_spacing_minutes": 15},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
