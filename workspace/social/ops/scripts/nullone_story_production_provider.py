#!/usr/bin/env python3
"""Real production `StoryCandidateProvider` for scheduled Story (#79).

Reads ONLY the validated structured Morning Editorial handoff snapshot
(`nullone.editorial-candidate-handoff.v1`, see
`nullone_editorial_candidate_handoff.py`). It never scans news, never
parses Markdown (board, queue, or ledger), never calls a model, never
touches Zernio/Telegram, and never publishes.

Selection rule (deterministic, no invented ranking):

1. keep Morning-flagged `story_eligible` candidates in Morning's own
   accepted `rank` order;
2. drop any candidate failing #33's independent `validate_candidate`
   admission (VERIFICATION: PASS, evidence, required fields);
3. drop any candidate whose Story review-draft attempt is already
   consumed (a STORY manifest with the same deterministic
   `story_request_id` and `review.create_attempts > 0` -- at-most-once);
4. return exactly the first remaining candidate, or nothing.

The provider therefore hands `select_single_candidate` at most one
candidate: `CANDIDATE_AMBIGUOUS` is unreachable through this path by
construction, and `CANDIDATE_UNAVAILABLE` truthfully means "no remaining
eligible candidate in this snapshot".
"""
from __future__ import annotations

from typing import Any

from nullone_editorial_candidate_handoff import (
    EditorialHandoffError,
    story_eligible_candidates,
)
from nullone_story_candidate_provider import StoryCandidateProvider
from nullone_story_pipeline import StoryCandidateNotEligible, compute_story_request_id, validate_candidate


class StoryProductionProviderError(RuntimeError):
    """The production provider itself is misconfigured or unreadable.

    Raised for caller contract violations (unvalidated snapshot, malformed
    consumed set) -- never for "zero remaining candidates", which is a
    normal empty return. A genuine upstream read failure inside
    `get_candidates` surfaces as `CANDIDATE_PROVIDER_FAILED` via
    `run_story_workflow`, matching the existing #62 boundary.
    """


def _require_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise StoryProductionProviderError("snapshot must be a validated handoff dict")
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list) or any(
        not isinstance(c, dict) for c in candidates
    ):
        raise StoryProductionProviderError(
            "snapshot must carry a validated candidates list "
            "(use nullone_editorial_candidate_handoff.validate_handoff)"
        )
    return snapshot


def _require_consumed(consumed: Any) -> frozenset[str]:
    if isinstance(consumed, frozenset) and all(
        isinstance(r, str) and r for r in consumed
    ):
        return consumed
    raise StoryProductionProviderError(
        "consumed_request_ids must be a frozenset of non-empty strings "
        "(use find_consumed_story_request_ids)"
    )


def _to_story_candidate(handoff_candidate: dict[str, Any]) -> dict[str, Any]:
    """Map one validated handoff candidate to #33's candidate contract.

    Copies exactly the #33-required fields plus the reviewed optional
    provenance fields the writer boundary accepts; nothing is invented.
    """

    mapped: dict[str, Any] = {
        "candidate_id": handoff_candidate["candidate_id"],
        "topic": handoff_candidate["topic"],
        "topic_cluster": handoff_candidate["topic_cluster"],
        "content_type": handoff_candidate["content_type"],
        "verification": handoff_candidate["verification"],
        "evidence_refs": list(handoff_candidate["evidence_refs"]),
        "source_attribution": handoff_candidate["source_attribution"],
    }
    for field in (
        "candidate_version",
        "request_lineage",
        "factual_inputs",
        "limitations",
        "claims",
    ):
        if handoff_candidate.get(field) is not None:
            value = handoff_candidate[field]
            mapped[field] = dict(value) if isinstance(value, dict) else value
    return mapped


class StructuredHandoffStoryProvider:
    """Production provider bound to one immutable validated snapshot."""

    def __init__(
        self,
        snapshot: dict[str, Any],
        consumed_request_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._snapshot = _require_snapshot(snapshot)
        self._consumed = _require_consumed(consumed_request_ids)

    def get_candidates(self) -> list[dict[str, Any]]:
        """Return zero or one production Story candidate.

        May raise on a genuine provider-side failure (e.g. an unreadable
        snapshot binding); `run_story_workflow` maps that to
        `CANDIDATE_PROVIDER_FAILED`, never to "zero candidates".
        """

        for handoff_candidate in story_eligible_candidates(self._snapshot):
            mapped = _to_story_candidate(handoff_candidate)
            try:
                validate_candidate(mapped)
            except StoryCandidateNotEligible:
                continue
            if compute_story_request_id(mapped) in self._consumed:
                continue
            return [mapped]
        return []


def snapshot_story_availability(
    snapshot: dict[str, Any],
    consumed_request_ids: frozenset[str] = frozenset(),
) -> bool:
    """Derive `story_quality_candidate_available` from the SAME snapshot.

    The caller uses this boolean for the cadence input and binds a
    provider to the identical snapshot object, so the signal and the
    provider can never disagree about which snapshot they read: one
    immutable snapshot, one availability bit, one provider.
    """

    provider = StructuredHandoffStoryProvider(snapshot, consumed_request_ids)
    return provider.get_candidates() != []


def self_test() -> int:
    from nullone_editorial_candidate_handoff import validate_handoff

    def handoff_doc(candidates):
        return {
            "schema": "nullone.editorial-candidate-handoff.v1",
            "contract_version": "1.0.0",
            "editorial_date": "2026-09-08",
            "board_path": "social/research/daily/2026-09-08-editorial-board.md",
            "candidates": candidates,
        }

    def candidate(**overrides):
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

    # 1. Empty snapshot -> no candidates, availability False.
    snap = validate_handoff(handoff_doc([]))
    provider = StructuredHandoffStoryProvider(snap, frozenset())
    assert provider.get_candidates() == []
    assert snapshot_story_availability(snap, frozenset()) is False

    # 2. One eligible candidate -> exactly it, availability True.
    snap = validate_handoff(handoff_doc([candidate()]))
    assert snapshot_story_availability(snap, frozenset()) is True
    got = StructuredHandoffStoryProvider(snap, frozenset()).get_candidates()
    assert len(got) == 1 and got[0]["candidate_id"] == "cand-1"
    assert got[0]["verification"] == "PASS"

    # 3. Several eligible: deterministic lowest-rank selection, exactly one.
    snap = validate_handoff(
        handoff_doc(
            [
                candidate(candidate_id="low", rank=2),
                candidate(candidate_id="top", rank=1),
                candidate(candidate_id="ineligible", rank=3, story_eligible=False),
            ]
        )
    )
    got = StructuredHandoffStoryProvider(snap, frozenset()).get_candidates()
    assert len(got) == 1 and got[0]["candidate_id"] == "top", got

    # 4. Consumed top candidate -> next remaining, not a redraft.
    from nullone_story_pipeline import compute_story_request_id as _req_id

    top_mapped = {
        "candidate_id": "top",
        "topic": "Topic",
        "topic_cluster": "cluster",
        "content_type": "NEWS",
        "verification": "PASS",
        "evidence_refs": ["evidence"],
        "source_attribution": "Source",
    }
    consumed = frozenset({_req_id(top_mapped)})
    got = StructuredHandoffStoryProvider(snap, consumed).get_candidates()
    assert len(got) == 1 and got[0]["candidate_id"] == "low", got
    assert snapshot_story_availability(snap, consumed) is True

    # 5. All consumed -> empty, availability False (truthful, not an error).
    low_mapped = dict(top_mapped, candidate_id="low")
    consumed_all = frozenset({_req_id(top_mapped), _req_id(low_mapped)})
    assert StructuredHandoffStoryProvider(snap, consumed_all).get_candidates() == []
    assert snapshot_story_availability(snap, consumed_all) is False

    # 6. Unvalidated snapshot / malformed consumed set rejected at construction.
    try:
        StructuredHandoffStoryProvider({"candidates": "nope"}, frozenset())
        raise AssertionError("unvalidated snapshot was not rejected")
    except StoryProductionProviderError:
        pass
    try:
        StructuredHandoffStoryProvider(snap, ["not-a-frozenset"])  # type: ignore[arg-type]
        raise AssertionError("malformed consumed set was not rejected")
    except StoryProductionProviderError:
        pass

    print("STORY_PRODUCTION_PROVIDER_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_MARKDOWN_PARSING=TRUE")
    print("NO_MODEL_CALL=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne production Story provider")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
