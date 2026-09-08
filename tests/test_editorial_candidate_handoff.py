#!/usr/bin/env python3
"""Contract tests for the structured Morning Editorial handoff (#79).

Proves the strict `nullone.editorial-candidate-handoff.v1` validator and
snapshot loader: valid empty/single/multi candidate documents, upstream
rank ordering reads, and fail-closed behavior for every malformed,
missing, stale, or ambiguous input in §26's source-safety list.
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
    CONTRACT_VERSION,
    SCHEMA,
    EditorialHandoffError,
    handoff_relative_path,
    load_handoff_snapshot,
    story_eligible_candidates,
    validate_handoff,
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


def make_handoff(**overrides):
    base = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "editorial_date": "2026-09-08",
        "board_path": "social/research/daily/2026-09-08-editorial-board.md",
        "candidates": [],
    }
    base.update(overrides)
    return base


class ValidHandoffTests(unittest.TestCase):
    def test_zero_candidates_is_valid_truthful_empty(self):
        snap = validate_handoff(make_handoff())
        self.assertEqual(snap["candidates"], [])
        self.assertEqual(story_eligible_candidates(snap), [])

    def test_one_eligible_candidate(self):
        snap = validate_handoff(make_handoff(candidates=[make_candidate()]))
        eligible = story_eligible_candidates(snap)
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]["candidate_id"], "cand-1")

    def test_eligible_order_follows_upstream_rank(self):
        snap = validate_handoff(
            make_handoff(
                candidates=[
                    make_candidate(candidate_id="b", rank=2),
                    make_candidate(candidate_id="a", rank=1),
                    make_candidate(candidate_id="c", rank=3, story_eligible=False),
                ]
            )
        )
        self.assertEqual(
            [c["candidate_id"] for c in story_eligible_candidates(snap)],
            ["a", "b"],
        )

    def test_optional_fields_accepted(self):
        snap = validate_handoff(
            make_handoff(
                candidates=[
                    make_candidate(
                        candidate_version="v3",
                        score=41,
                        source_urls=["https://example.com/x"],
                        factual_inputs={"claim": "x"},
                        limitations={"note": "y"},
                    )
                ]
            )
        )
        self.assertEqual(len(snap["candidates"]), 1)

    def test_handoff_path_convention(self):
        self.assertEqual(
            handoff_relative_path("2026-09-08"),
            "social/research/daily/2026-09-08-editorial-candidates.json",
        )


class FailClosedTests(unittest.TestCase):
    def test_duplicate_rank_fails_closed(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(
                make_handoff(
                    candidates=[
                        make_candidate(candidate_id="a", rank=1),
                        make_candidate(candidate_id="b", rank=1),
                    ]
                )
            )

    def test_duplicate_candidate_id_fails_closed(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(
                make_handoff(
                    candidates=[
                        make_candidate(candidate_id="a", rank=1),
                        make_candidate(candidate_id="a", rank=2),
                    ]
                )
            )

    def test_unknown_top_level_field_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(make_handoff(surprise=1))

    def test_unknown_candidate_field_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(make_handoff(candidates=[make_candidate(mystery=1)]))

    def test_unknown_schema_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(make_handoff(schema="nullone.something-else.v1"))

    def test_unknown_version_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(make_handoff(contract_version="9.9.9"))

    def test_missing_evidence_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(make_handoff(candidates=[make_candidate(evidence_refs=[])]))

    def test_eligible_but_unverified_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(
                make_handoff(candidates=[make_candidate(verification="PARTIAL")])
            )

    def test_eligible_requires_ready_status(self):
        for status in ("DEFERRED", "REJECTED", "NEW", "RESEARCHING"):
            with self.assertRaises(EditorialHandoffError, msg=status):
                validate_handoff(
                    make_handoff(
                        candidates=[make_candidate(editorial_status=status)]
                    )
                )

    def test_ready_but_not_eligible_is_valid_and_unselected(self):
        snap = validate_handoff(
            make_handoff(
                candidates=[
                    make_candidate(editorial_status="READY", story_eligible=False)
                ]
            )
        )
        self.assertEqual(story_eligible_candidates(snap), [])

    def test_misbound_board_path_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(
                make_handoff(
                    candidates=[make_candidate()],
                    board_path="social/research/daily/2026-09-07-editorial-board.md",
                )
            )
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(
                make_handoff(
                    candidates=[make_candidate()],
                    board_path="social/research/somewhere-else.json",
                )
            )

    def test_impossible_calendar_date_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            handoff_relative_path("2026-02-31")
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(make_handoff(editorial_date="2026-02-31"))

    def test_bad_content_type_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(
                make_handoff(candidates=[make_candidate(content_type="REEL")])
            )

    def test_non_boolean_story_eligible_rejected(self):
        with self.assertRaises(EditorialHandoffError):
            validate_handoff(
                make_handoff(candidates=[make_candidate(story_eligible="yes")])
            )


class SnapshotLoaderTests(unittest.TestCase):
    def test_load_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            research = root / "social/research/daily"
            research.mkdir(parents=True, exist_ok=True)
            (research / "2026-09-08-editorial-candidates.json").write_text(
                json.dumps(make_handoff(candidates=[make_candidate()])),
                encoding="utf-8",
            )
            snap = load_handoff_snapshot(
                workspace_root=root, editorial_date="2026-09-08"
            )
            self.assertEqual(len(snap["candidates"]), 1)

    def test_missing_file_is_source_failure(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(EditorialHandoffError):
                load_handoff_snapshot(
                    workspace_root=Path(td), editorial_date="2026-09-08"
                )

    def test_malformed_json_is_source_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            research = root / "social/research/daily"
            research.mkdir(parents=True, exist_ok=True)
            (research / "2026-09-08-editorial-candidates.json").write_text(
                "{not json", encoding="utf-8"
            )
            with self.assertRaises(EditorialHandoffError):
                load_handoff_snapshot(
                    workspace_root=root, editorial_date="2026-09-08"
                )

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            research = root / "social/research/daily"
            research.mkdir(parents=True, exist_ok=True)
            real = research / "real.json"
            real.write_text(json.dumps(make_handoff()), encoding="utf-8")
            link = research / "2026-09-08-editorial-candidates.json"
            link.symlink_to(real)
            with self.assertRaises(EditorialHandoffError):
                load_handoff_snapshot(
                    workspace_root=root, editorial_date="2026-09-08"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
