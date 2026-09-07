#!/usr/bin/env python3
"""Deterministic Story candidate-selection boundary (#62).

`StoryWorkflow` owns the application-level decision to hand exactly one
eligible candidate to #33's `run_story_pipeline`, but must not invent
editorial ranking heuristics not already accepted elsewhere. Repository
inspection for #62 found no existing structured, authoritative candidate
source: `social/state/candidate-queue.md` and `social/state/topic-ledger.jsonl`
are operational, runtime-only Markdown/JSONL state (not checked into this
repository) that the accepted cadence contract explicitly places at the
lowest precedence and forbids using as a load-bearing decision source
outside dedup context -- and this module does not parse them heuristically
to manufacture a selection policy that was never reviewed.

This module therefore defines only the narrow injected boundary
(`StoryCandidateProvider`) plus the deterministic, safe selection rule
requested by #62:

- zero eligible candidates -> no draft (`CANDIDATE_UNAVAILABLE`);
- exactly one eligible candidate -> proceed;
- multiple eligible candidates with no existing accepted deterministic
  ordering -> fail closed / ambiguous (`CANDIDATE_AMBIGUOUS`) -- no
  ranking by filename, filesystem order, modified time, title, or an
  invented score;
- a candidate must still independently pass #33's own
  `nullone_story_pipeline.validate_candidate()` (`VERIFICATION: PASS`,
  non-empty `evidence_refs`, all required fields) -- this module never
  reimplements that check, only calls it once per candidate to classify
  it.

A real, reviewed production `StoryCandidateProvider` (reading whatever
authoritative structured candidate source a future issue introduces) is
NOT implemented here -- there is nothing in the repository yet for it to
read that would not require the heuristic Markdown parsing this issue
explicitly forbids. `run_story_workflow` accepts any object satisfying the
protocol; tests exercise the deterministic selection rule with fakes.
"""
from __future__ import annotations

from typing import Any, Protocol

from nullone_story_pipeline import StoryCandidateNotEligible, validate_candidate

# Selection outcomes -- distinct from nullone_story_workflow's own
# StoryWorkflowResult.outcome values, which wrap these with cadence/trigger
# context.
SELECTION_UNAVAILABLE = "CANDIDATE_UNAVAILABLE"
SELECTION_AMBIGUOUS = "CANDIDATE_AMBIGUOUS"
SELECTION_INVALID = "CANDIDATE_INVALID"
SELECTION_OK = "OK"

SELECTION_OUTCOMES = frozenset(
    {SELECTION_UNAVAILABLE, SELECTION_AMBIGUOUS, SELECTION_INVALID, SELECTION_OK}
)


class StoryCandidateProvider(Protocol):
    def get_candidates(self) -> list[dict[str, Any]]:
        """Return zero or more raw candidate dicts for evaluation.

        Each dict is shaped per #33's candidate contract
        (`nullone_story_pipeline.validate_candidate`'s required fields).
        This method may raise on a genuine provider-side failure (e.g. an
        upstream read error); `run_story_workflow` treats that as a
        distinct `CANDIDATE_PROVIDER_FAILED` outcome, never as "zero
        candidates".
        """


class StaticStoryCandidateProvider:
    """Trivial provider returning a fixed, injected candidate list.

    Not a production discovery mechanism -- a minimal adapter useful for
    wiring a single already-selected candidate (e.g. from an editorial
    board export) without inventing a heuristic parser. Tests use this
    directly; a future reviewed provider can replace it without changing
    `nullone_story_workflow.py`.
    """

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self._candidates = list(candidates)

    def get_candidates(self) -> list[dict[str, Any]]:
        return list(self._candidates)


def select_single_candidate(
    raw_candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Apply the deterministic #62 candidate-selection boundary.

    Returns `(candidate_or_none, outcome, context)`. `context` carries
    diagnostic detail (counts, per-candidate rejection reasons) without
    ever inventing a ranking or silently picking one of several equally
    eligible candidates.
    """

    if not raw_candidates:
        return None, SELECTION_UNAVAILABLE, {"candidate_count": 0}

    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for candidate in raw_candidates:
        try:
            validate_candidate(candidate)
        except StoryCandidateNotEligible as exc:
            rejected.append(
                {
                    "candidate_id": (
                        candidate.get("candidate_id")
                        if isinstance(candidate, dict)
                        else None
                    ),
                    "reason": str(exc),
                }
            )
            continue
        eligible.append(candidate)

    if not eligible:
        return (
            None,
            SELECTION_INVALID,
            {"candidate_count": len(raw_candidates), "rejected": rejected},
        )

    if len(eligible) > 1:
        return (
            None,
            SELECTION_AMBIGUOUS,
            {
                "candidate_count": len(raw_candidates),
                "eligible_candidate_ids": [c.get("candidate_id") for c in eligible],
            },
        )

    return (
        eligible[0],
        SELECTION_OK,
        {"candidate_count": len(raw_candidates), "rejected": rejected},
    )


def self_test() -> int:
    def candidate(**overrides):
        base = {
            "candidate_id": "cand-1",
            "topic": "Test topic",
            "topic_cluster": "test",
            "content_type": "NEWS",
            "verification": "PASS",
            "evidence_refs": ["evidence"],
            "source_attribution": "source",
        }
        base.update(overrides)
        return base

    # Zero candidates.
    result, outcome, _ctx = select_single_candidate([])
    assert outcome == SELECTION_UNAVAILABLE and result is None

    # Exactly one eligible candidate.
    result, outcome, _ctx = select_single_candidate([candidate()])
    assert outcome == SELECTION_OK and result is not None

    # Multiple eligible candidates -- ambiguous, no invented ranking.
    result, outcome, _ctx = select_single_candidate(
        [candidate(candidate_id="a"), candidate(candidate_id="b")]
    )
    assert outcome == SELECTION_AMBIGUOUS and result is None

    # All candidates invalid (not PASS) -> CANDIDATE_INVALID, not UNAVAILABLE.
    result, outcome, _ctx = select_single_candidate(
        [candidate(verification="PARTIAL")]
    )
    assert outcome == SELECTION_INVALID and result is None

    # A mix: one invalid, one valid -> the valid one is still selected.
    result, outcome, _ctx = select_single_candidate(
        [candidate(candidate_id="bad", verification="PARTIAL"), candidate(candidate_id="good")]
    )
    assert outcome == SELECTION_OK and result["candidate_id"] == "good"

    provider = StaticStoryCandidateProvider([candidate()])
    assert len(provider.get_candidates()) == 1

    print("STORY_CANDIDATE_PROVIDER_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_HEURISTIC_MARKDOWN_PARSING=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Story candidate-selection boundary")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
