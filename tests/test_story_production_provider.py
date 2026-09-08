#!/usr/bin/env python3
"""Production StoryCandidateProvider tests (#79).

Proves the real provider reads only the validated structured snapshot,
selects deterministically by upstream rank, suppresses already-consumed
review attempts, derives availability from the same snapshot, and can
never be steered by board/queue/ledger Markdown content.
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

from nullone_editorial_candidate_handoff import (  # noqa: E402
    find_consumed_story_request_ids,
    validate_handoff,
)
from nullone_story_pipeline import compute_story_request_id  # noqa: E402
from nullone_story_production_provider import (  # noqa: E402
    StoryProductionProviderError,
    StructuredHandoffStoryProvider,
    snapshot_story_availability,
)


def make_candidate(**overrides):
    base = {
        "candidate_id": "cand-1",
        "rank": 1,
        "topic": "Topic",
        "topic_cluster": "cluster",
        "content_type": "NEWS",
        "angle": "Angle",
        "verification": "PASS",
        "evidence_refs": ["evidence"],
        "source_attribution": "Source",
        "editorial_status": "READY",
        "story_eligible": True,
    }
    base.update(overrides)
    return base


def make_snapshot(candidates):
    return validate_handoff(
        {
            "schema": "nullone.editorial-candidate-handoff.v1",
            "contract_version": "1.0.0",
            "editorial_date": "2026-09-08",
            "board_path": "social/research/daily/2026-09-08-editorial-board.md",
            "candidates": candidates,
        }
    )


def mapped_for(candidate_id="cand-1"):
    return {
        "candidate_id": candidate_id,
        "topic": "Topic",
        "topic_cluster": "cluster",
        "content_type": "NEWS",
        "verification": "PASS",
        "evidence_refs": ["evidence"],
        "source_attribution": "Source",
    }


class SelectionRuleTests(unittest.TestCase):
    def test_empty_snapshot_gives_no_candidate(self):
        snap = make_snapshot([])
        provider = StructuredHandoffStoryProvider(snap, frozenset())
        self.assertEqual(provider.get_candidates(), [])
        self.assertFalse(snapshot_story_availability(snap, frozenset()))

    def test_single_eligible_candidate_returned_exactly(self):
        snap = make_snapshot([make_candidate()])
        got = StructuredHandoffStoryProvider(snap, frozenset()).get_candidates()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["candidate_id"], "cand-1")
        self.assertTrue(snapshot_story_availability(snap, frozenset()))

    def test_lowest_upstream_rank_wins_deterministically(self):
        snap = make_snapshot(
            [
                make_candidate(candidate_id="second", rank=2),
                make_candidate(candidate_id="first", rank=1),
                make_candidate(candidate_id="skip", rank=3, story_eligible=False),
            ]
        )
        got = StructuredHandoffStoryProvider(snap, frozenset()).get_candidates()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["candidate_id"], "first")

    def test_consumed_top_candidate_falls_through_without_redraft(self):
        snap = make_snapshot(
            [
                make_candidate(candidate_id="top", rank=1),
                make_candidate(candidate_id="next", rank=2),
            ]
        )
        consumed = frozenset({compute_story_request_id(mapped_for("top"))})
        got = StructuredHandoffStoryProvider(snap, consumed).get_candidates()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["candidate_id"], "next")
        self.assertTrue(snapshot_story_availability(snap, consumed))

    def test_all_consumed_is_truthful_empty_not_error(self):
        snap = make_snapshot([make_candidate()])
        consumed = frozenset({compute_story_request_id(mapped_for())})
        self.assertEqual(
            StructuredHandoffStoryProvider(snap, consumed).get_candidates(), []
        )
        self.assertFalse(snapshot_story_availability(snap, consumed))

    def test_mapped_candidate_passes_33_admission(self):
        from nullone_story_pipeline import validate_candidate

        snap = make_snapshot([make_candidate()])
        got = StructuredHandoffStoryProvider(snap, frozenset()).get_candidates()
        self.assertEqual(validate_candidate(got[0]), got[0])


class ConsumedScanTests(unittest.TestCase):
    def _manifest(self, request_id, attempts, fmt="STORY"):
        return {
            "schema": "nullone.production.v1",
            "manifest_id": "m1",
            "format": fmt,
            "story_request_id": request_id,
            "review": {"create_attempts": attempts, "state": "X"},
        }

    def test_consumed_attempt_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "social/ops/manifests"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            (manifest_dir / "a.json").write_text(
                json.dumps(self._manifest("story-request-abc", 1)), encoding="utf-8"
            )
            (manifest_dir / "b.json").write_text(
                json.dumps(self._manifest("story-request-untouched", 0)),
                encoding="utf-8",
            )
            consumed = find_consumed_story_request_ids(workspace_root=root)
            self.assertEqual(consumed, frozenset({"story-request-abc"}))

    def test_missing_manifest_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                find_consumed_story_request_ids(workspace_root=Path(td)),
                frozenset(),
            )

    def test_non_story_manifests_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "social/ops/manifests"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            (manifest_dir / "a.json").write_text(
                json.dumps(self._manifest("story-request-x", 3, fmt="FEED")),
                encoding="utf-8",
            )
            self.assertEqual(
                find_consumed_story_request_ids(workspace_root=root), frozenset()
            )


class NoMarkdownInfluenceTests(unittest.TestCase):
    def test_board_queue_ledger_content_cannot_steer_selection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Tempting Markdown content naming a "better" candidate that
            # exists nowhere in the structured snapshot.
            research = root / "social/research/daily"
            research.mkdir(parents=True, exist_ok=True)
            (research / "2026-09-08-editorial-board.md").write_text(
                "# Board\nRANK 1: markdown-decoy, VERIFICATION: PASS\n",
                encoding="utf-8",
            )
            state = root / "social/state"
            state.mkdir(parents=True, exist_ok=True)
            (state / "candidate-queue.md").write_text(
                "candidate_id: markdown-decoy, status: READY\n", encoding="utf-8"
            )
            (state / "topic-ledger.jsonl").write_text(
                '{"candidate_id": "markdown-decoy"}\n', encoding="utf-8"
            )

            snap = make_snapshot([make_candidate(candidate_id="real")])
            got = StructuredHandoffStoryProvider(snap, frozenset()).get_candidates()
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["candidate_id"], "real")


if __name__ == "__main__":
    unittest.main(verbosity=2)
