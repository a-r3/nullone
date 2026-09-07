#!/usr/bin/env python3
"""Narrow prepared-main-candidate boundary for BreakingWorkflow (#63)."""
from __future__ import annotations

from typing import Any, Iterable, Protocol

from nullone_main_draft_pipeline import MainCandidateNotEligible, validate_candidate

SELECTION_OK = "OK"
SELECTION_UNAVAILABLE = "UNAVAILABLE"
SELECTION_AMBIGUOUS = "AMBIGUOUS"
SELECTION_INVALID = "INVALID"


class BreakingMainCandidateProvider(Protocol):
    """Return prepared candidates for one exact selected main target.

    The provider prepares editorial content; it does not choose the target,
    compute capacity, route, create drafts, or deliver previews.
    """

    def get_candidates(
        self, *, candidate_id: str, selected_format: str, request_lineage: str
    ) -> Iterable[dict[str, Any]]:
        ...


class StaticBreakingMainCandidateProvider:
    """Deterministic repository/test adapter around already-prepared candidates."""

    def __init__(self, candidates: Iterable[dict[str, Any]]):
        self._candidates = list(candidates)

    def get_candidates(
        self, *, candidate_id: str, selected_format: str, request_lineage: str
    ) -> Iterable[dict[str, Any]]:
        del candidate_id, selected_format, request_lineage
        return list(self._candidates)


def select_bound_main_candidate(
    candidates: Any,
    *,
    candidate_id: str,
    selected_format: str,
    request_lineage: str,
    evidence_refs: tuple[str, ...],
    source_attribution: str,
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Select exactly one candidate and verify all workflow-owned bindings."""

    if isinstance(candidates, (str, bytes, dict)):
        return None, SELECTION_INVALID, {"reason_code": "MAIN_PROVIDER_RESULT_INVALID"}
    try:
        items = list(candidates)
    except Exception:  # noqa: BLE001 - provider iteration failure is invalid input
        return None, SELECTION_INVALID, {"reason_code": "MAIN_PROVIDER_RESULT_INVALID"}

    if not items:
        return None, SELECTION_UNAVAILABLE, {"reason_code": "MAIN_CANDIDATE_UNAVAILABLE"}
    if len(items) != 1:
        return None, SELECTION_AMBIGUOUS, {
            "reason_code": "MAIN_CANDIDATE_AMBIGUOUS",
            "candidate_count": len(items),
        }

    candidate = items[0]
    if not isinstance(candidate, dict):
        return None, SELECTION_INVALID, {"reason_code": "MAIN_CANDIDATE_INVALID"}
    if candidate.get("candidate_id") != candidate_id:
        return None, SELECTION_INVALID, {"reason_code": "MAIN_CANDIDATE_ID_MISMATCH"}
    if candidate.get("format") != selected_format:
        return None, SELECTION_INVALID, {"reason_code": "MAIN_CANDIDATE_FORMAT_MISMATCH"}
    if candidate.get("verification") != "PASS":
        return None, SELECTION_INVALID, {"reason_code": "MAIN_CANDIDATE_NOT_VERIFIED"}
    if tuple(candidate.get("evidence_refs") or ()) != evidence_refs:
        return None, SELECTION_INVALID, {"reason_code": "MAIN_EVIDENCE_BINDING_MISMATCH"}
    if candidate.get("source_attribution") != source_attribution:
        return None, SELECTION_INVALID, {"reason_code": "MAIN_SOURCE_BINDING_MISMATCH"}
    if candidate.get("request_lineage") != request_lineage:
        return None, SELECTION_INVALID, {"reason_code": "MAIN_LINEAGE_MISMATCH"}

    try:
        validate_candidate(candidate)
    except MainCandidateNotEligible:
        return None, SELECTION_INVALID, {"reason_code": "MAIN_CANDIDATE_INVALID"}

    return dict(candidate), SELECTION_OK, {"candidate_count": 1}
