#!/usr/bin/env python3
"""OpenClaw/Radar edge mapping for BreakingWorkflow (#63).

This infrastructure edge knows only how to normalize one external occurrence
and validate the strict machine-readable Radar handoff.  Identity, routing,
capacity, dispatch, draft, and review-delivery semantics remain in NullOne.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from nullone_breaking_workflow_input import (
    validate_breaking_workflow_input,
)
from nullone_scheduler_invocation import (
    CONTRACT_VERSION as INVOCATION_CONTRACT_VERSION,
    SCHEMA as INVOCATION_SCHEMA,
    compute_occurrence_id,
    validate_payload,
)

HANDOFF_SCHEMA = "nullone.breaking-radar-handoff.v1"
HANDOFF_CONTRACT_VERSION = "1.0.0"
_HANDOFF_FIELDS = frozenset({"schema", "contract_version", "occurrence", "assessment"})
_OCCURRENCE_FIELDS = frozenset(
    {"source_occurrence_id", "scheduled_for", "triggered_at"}
)


class BreakingRadarEdgeError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedBreakingHandoff:
    trigger: dict[str, Any]
    assessment: dict[str, Any]


def _exact_object(value: Any, field: str, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BreakingRadarEdgeError(f"{field} must be an object")
    actual = set(value)
    if actual != fields:
        raise BreakingRadarEdgeError(
            f"{field} field set mismatch "
            f"(missing={sorted(fields - actual)}, unknown={sorted(actual - fields)})"
        )
    return value


def compute_candidate_external_occurrence_id(
    source_occurrence_id: str, candidate_id: str
) -> str:
    """Bind one raw Radar scan occurrence to one exact candidate.

    Retry timing and mutable assessment content are deliberately excluded.
    #35 event/development identity remains a separate downstream decision.
    """

    if not isinstance(source_occurrence_id, str) or not source_occurrence_id.strip():
        raise BreakingRadarEdgeError("source_occurrence_id must be a non-empty string")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise BreakingRadarEdgeError("assessment.candidate_id must be a non-empty string")
    canonical = json.dumps(
        [source_occurrence_id, candidate_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return f"breaking-candidate-{digest}"


def normalize_breaking_radar_handoff(value: Any) -> NormalizedBreakingHandoff:
    """Return one validated scheduler invocation plus one validated assessment."""

    handoff = _exact_object(value, "handoff", _HANDOFF_FIELDS)
    if handoff["schema"] != HANDOFF_SCHEMA:
        raise BreakingRadarEdgeError(f"schema must be {HANDOFF_SCHEMA!r}")
    if handoff["contract_version"] != HANDOFF_CONTRACT_VERSION:
        raise BreakingRadarEdgeError(
            f"contract_version must be {HANDOFF_CONTRACT_VERSION!r}"
        )
    occurrence = _exact_object(
        handoff["occurrence"], "handoff.occurrence", _OCCURRENCE_FIELDS
    )

    try:
        assessment = validate_breaking_workflow_input(handoff["assessment"])
        external_occurrence_id = compute_candidate_external_occurrence_id(
            occurrence["source_occurrence_id"], assessment.candidate_id
        )
        trigger = {
            "schema": INVOCATION_SCHEMA,
            "contract_version": INVOCATION_CONTRACT_VERSION,
            "workflow_id": "breaking",
            "source": "openclaw",
            "external_occurrence_id": external_occurrence_id,
            "scheduled_for": occurrence["scheduled_for"],
            "triggered_at": occurrence["triggered_at"],
        }
        trigger["occurrence_id"] = compute_occurrence_id(
            trigger["workflow_id"],
            trigger["source"],
            trigger["external_occurrence_id"],
            trigger["scheduled_for"],
        )
        validate_payload(trigger)
    except ValueError as exc:
        raise BreakingRadarEdgeError(str(exc)) from exc

    return NormalizedBreakingHandoff(
        trigger=trigger, assessment=dict(handoff["assessment"])
    )
