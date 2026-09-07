#!/usr/bin/env python3
"""OpenClaw/Radar edge mapping for BreakingWorkflow (#63).

This infrastructure edge knows only how to normalize one external occurrence
and validate the strict machine-readable Radar handoff.  Identity, routing,
capacity, dispatch, draft, and review-delivery semantics remain in NullOne.
"""
from __future__ import annotations

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
    {"external_occurrence_id", "scheduled_for", "triggered_at"}
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

    trigger = {
        "schema": INVOCATION_SCHEMA,
        "contract_version": INVOCATION_CONTRACT_VERSION,
        "workflow_id": "breaking",
        "source": "openclaw",
        "external_occurrence_id": occurrence["external_occurrence_id"],
        "scheduled_for": occurrence["scheduled_for"],
        "triggered_at": occurrence["triggered_at"],
    }
    try:
        trigger["occurrence_id"] = compute_occurrence_id(
            trigger["workflow_id"],
            trigger["source"],
            trigger["external_occurrence_id"],
            trigger["scheduled_for"],
        )
        validate_payload(trigger)
        validate_breaking_workflow_input(handoff["assessment"])
    except ValueError as exc:
        raise BreakingRadarEdgeError(str(exc)) from exc

    return NormalizedBreakingHandoff(
        trigger=trigger, assessment=dict(handoff["assessment"])
    )
