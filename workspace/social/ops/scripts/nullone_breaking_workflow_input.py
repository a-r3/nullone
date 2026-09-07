#!/usr/bin/env python3
"""Strict machine-readable input for NullOne BreakingWorkflow (#63).

The current Breaking Radar Markdown report is research output, not an
application command.  This module defines and validates the deliberately
narrow handoff that an edge adapter must provide before BreakingWorkflow may
invoke the accepted #35 identity and #36 routing policies.

No prose parsing, discovery, network access, provider access, or side effects
belong here.  Unknown fields are rejected at every schema-owned object.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from nullone_breaking_identity import CandidateInput, EvidenceItem, FollowUpDelta

SCHEMA = "nullone.breaking-workflow-input.v1"
CONTRACT_VERSION = "1.0.0"

VERIFICATION_STATES = frozenset({"UNVERIFIED", "PARTIAL", "PASS", "BLOCKED"})
SEVERITIES = frozenset({"NORMAL", "MATERIAL_BREAKING", "EXCEPTIONAL_BREAKING"})
CONTENT_TYPES = frozenset({"NEWS", "BREAKING"})

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "contract_version",
        "candidate_id",
        "candidate_version",
        "assessment_ref",
        "state_snapshot_ref",
        "topic",
        "topic_cluster",
        "content_type",
        "evidence",
        "follow_up_delta",
        "source_attribution",
        "limitations",
        "product_version_region",
        "source_image",
        "verification",
        "severity_assessment",
        "recent_coverage",
        "story_safety",
        "main_assessment",
    }
)

_EVIDENCE_REQUIRED_FIELDS = frozenset({"ref", "supported_claim"})
_EVIDENCE_OPTIONAL_FIELDS = frozenset(
    {
        "source_url",
        "announcement_id",
        "product",
        "version",
        "region",
        "availability_stage",
        "number_value",
        "number_unit",
        "number_population",
        "number_period",
    }
)
_FOLLOW_UP_FIELDS = frozenset(
    {"delta_kind", "parent_claim", "new_claim", "evidence_ref"}
)
_VERIFICATION_FIELDS = frozenset({"state", "evidence_refs"})
_SEVERITY_FIELDS = frozenset({"classification", "reason_text"})
_RECENT_COVERAGE_FIELDS = frozenset(
    {
        "related_coverage_exists",
        "incremental_value_present",
        "assessment_ref",
        "freshness_ref",
    }
)
_STORY_SAFETY_FIELDS = frozenset(
    {"quality_pass", "quality_ref", "dependencies_available", "dependencies_ref"}
)
_MAIN_ASSESSMENT_FIELDS = frozenset({"standalone_justification", "findings"})
_MAIN_FINDING_FIELDS = frozenset(
    {
        "feed_score",
        "carousel_score",
        "single_visual_value",
        "concise_announcement_value",
        "meaningful_multi_slide_value",
        "comparison_value",
        "sequence_value",
        "multi_fact_value",
        "material_context_value",
        "available_source_media",
    }
)
_PRODUCT_VERSION_REGION_FIELDS = frozenset({"product", "version", "region"})


class BreakingWorkflowInputError(ValueError):
    """The Radar-to-workflow handoff is malformed or unsupported."""


def _fail(message: str) -> None:
    raise BreakingWorkflowInputError(message)


def _object(value: Any, field: str, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be an object")
    actual = set(value)
    if actual != fields:
        _fail(
            f"{field} field set mismatch "
            f"(missing={sorted(fields - actual)}, unknown={sorted(actual - fields)})"
        )
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{field} must be a boolean")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{field} must be a non-negative integer")
    return value


def _text_list(value: Any, field: str, *, non_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{field} must be a list")
    if non_empty and not value:
        _fail(f"{field} must be non-empty")
    parsed = tuple(_text(item, f"{field}[]") for item in value)
    if len(parsed) != len(set(parsed)):
        _fail(f"{field} must not contain duplicates")
    return parsed


@dataclass(frozen=True)
class VerificationAssessment:
    state: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class SeverityAssessment:
    classification: str | None
    reason_text: str | None


@dataclass(frozen=True)
class RecentCoverageAssessment:
    related_coverage_exists: bool
    incremental_value_present: bool
    assessment_ref: str
    freshness_ref: str


@dataclass(frozen=True)
class StorySafetyAssessment:
    quality_pass: bool
    quality_ref: str
    dependencies_available: bool
    dependencies_ref: str


@dataclass(frozen=True)
class MainAssessment:
    standalone_justification: str
    findings: Mapping[str, Any]


@dataclass(frozen=True)
class BreakingWorkflowInput:
    schema: str
    contract_version: str
    candidate_id: str
    candidate_version: str | None
    assessment_ref: str
    state_snapshot_ref: str
    topic: str
    topic_cluster: str
    content_type: str
    evidence: tuple[EvidenceItem, ...]
    follow_up_delta: FollowUpDelta | None
    source_attribution: str
    limitations: tuple[str, ...]
    product_version_region: Mapping[str, str | None]
    source_image: str | None
    verification: VerificationAssessment
    severity: SeverityAssessment
    recent_coverage: RecentCoverageAssessment
    story_safety: StorySafetyAssessment
    main_assessment: MainAssessment | None

    def identity_candidate(self) -> CandidateInput:
        """Build the exact #35 input; no caller-supplied identity result exists."""

        return CandidateInput(
            candidate_id=self.candidate_id,
            assessment_ref=self.assessment_ref,
            state_snapshot_ref=self.state_snapshot_ref,
            topic_cluster=self.topic_cluster,
            evidence=self.evidence,
            delta=self.follow_up_delta,
            topic_title=self.topic,
        )

    def story_candidate(self, *, request_lineage: str) -> dict[str, Any]:
        """Build the already-verified candidate consumed by the #33 Story core."""

        candidate: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "candidate_version": self.candidate_version,
            "request_lineage": request_lineage,
            "topic": self.topic,
            "topic_cluster": self.topic_cluster,
            "content_type": self.content_type,
            "verification": self.verification.state,
            "evidence_refs": list(self.verification.evidence_refs),
            "source_attribution": self.source_attribution,
            "claims": [item.supported_claim for item in self.evidence],
            "limitations": list(self.limitations),
            "product_version_region": dict(self.product_version_region),
            "factual_inputs": {
                "evidence": [
                    {
                        name: getattr(item, name)
                        for name in (
                            "ref",
                            "supported_claim",
                            "source_url",
                            "announcement_id",
                            "product",
                            "version",
                            "region",
                            "availability_stage",
                            "number_value",
                            "number_unit",
                            "number_population",
                            "number_period",
                        )
                    }
                    for item in self.evidence
                ]
            },
        }
        if self.source_image is not None:
            candidate["source_image"] = self.source_image
        return candidate


def _parse_evidence(value: Any) -> tuple[EvidenceItem, ...]:
    if not isinstance(value, list) or not value:
        _fail("evidence must be a non-empty list")
    parsed: list[EvidenceItem] = []
    refs: set[str] = set()
    allowed = _EVIDENCE_REQUIRED_FIELDS | _EVIDENCE_OPTIONAL_FIELDS
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            _fail(f"evidence[{index}] must be an object")
        actual = set(raw)
        missing = _EVIDENCE_REQUIRED_FIELDS - actual
        unknown = actual - allowed
        if missing or unknown:
            _fail(
                f"evidence[{index}] field set mismatch "
                f"(missing={sorted(missing)}, unknown={sorted(unknown)})"
            )
        kwargs: dict[str, Any] = {
            "ref": _text(raw["ref"], f"evidence[{index}].ref"),
            "supported_claim": _text(
                raw["supported_claim"], f"evidence[{index}].supported_claim"
            ),
        }
        for name in _EVIDENCE_OPTIONAL_FIELDS:
            kwargs[name] = _optional_text(raw.get(name), f"evidence[{index}].{name}")
        if kwargs["ref"] in refs:
            _fail("evidence refs must be unique")
        refs.add(kwargs["ref"])
        try:
            parsed.append(EvidenceItem(**kwargs))
        except ValueError as exc:
            _fail(f"evidence[{index}] is invalid: {exc}")
    return tuple(parsed)


def _parse_follow_up(value: Any) -> FollowUpDelta | None:
    if value is None:
        return None
    raw = _object(value, "follow_up_delta", _FOLLOW_UP_FIELDS)
    try:
        return FollowUpDelta(
            delta_kind=_text(raw["delta_kind"], "follow_up_delta.delta_kind"),
            parent_claim=_text(raw["parent_claim"], "follow_up_delta.parent_claim"),
            new_claim=_text(raw["new_claim"], "follow_up_delta.new_claim"),
            evidence_ref=_text(raw["evidence_ref"], "follow_up_delta.evidence_ref"),
        )
    except ValueError as exc:
        _fail(f"follow_up_delta is invalid: {exc}")


def _parse_main(value: Any, severity: str | None) -> MainAssessment | None:
    if value is None:
        return None
    raw = _object(value, "main_assessment", _MAIN_ASSESSMENT_FIELDS)
    if severity != "EXCEPTIONAL_BREAKING":
        _fail("main_assessment is only allowed for EXCEPTIONAL_BREAKING")
    findings_raw = _object(
        raw["findings"], "main_assessment.findings", _MAIN_FINDING_FIELDS
    )
    findings: dict[str, Any] = {
        "feed_score": _non_negative_int(
            findings_raw["feed_score"], "main_assessment.findings.feed_score"
        ),
        "carousel_score": _non_negative_int(
            findings_raw["carousel_score"], "main_assessment.findings.carousel_score"
        ),
    }
    for name in _MAIN_FINDING_FIELDS - {"feed_score", "carousel_score"}:
        findings[name] = _boolean(
            findings_raw[name], f"main_assessment.findings.{name}"
        )
    return MainAssessment(
        standalone_justification=_text(
            raw["standalone_justification"], "main_assessment.standalone_justification"
        ),
        findings=findings,
    )


def validate_breaking_workflow_input(value: Any) -> BreakingWorkflowInput:
    """Validate one exact ``nullone.breaking-workflow-input.v1`` artifact."""

    raw = _object(value, "input", _TOP_LEVEL_FIELDS)
    if raw["schema"] != SCHEMA:
        _fail(f"schema must be {SCHEMA!r}")
    if raw["contract_version"] != CONTRACT_VERSION:
        _fail(f"contract_version must be {CONTRACT_VERSION!r}")

    candidate_id = _text(raw["candidate_id"], "candidate_id")
    candidate_version = _optional_text(raw["candidate_version"], "candidate_version")
    assessment_ref = _text(raw["assessment_ref"], "assessment_ref")
    state_snapshot_ref = _text(raw["state_snapshot_ref"], "state_snapshot_ref")
    topic = _text(raw["topic"], "topic")
    topic_cluster = _text(raw["topic_cluster"], "topic_cluster")
    content_type = _text(raw["content_type"], "content_type")
    if content_type not in CONTENT_TYPES:
        _fail(f"content_type must be one of {sorted(CONTENT_TYPES)}")

    evidence = _parse_evidence(raw["evidence"])
    follow_up_delta = _parse_follow_up(raw["follow_up_delta"])
    if follow_up_delta is not None and follow_up_delta.evidence_ref not in {
        item.ref for item in evidence
    }:
        _fail("follow_up_delta.evidence_ref must reference an evidence item")

    limitations = _text_list(raw["limitations"], "limitations")
    pvr_raw = _object(
        raw["product_version_region"],
        "product_version_region",
        _PRODUCT_VERSION_REGION_FIELDS,
    )
    product_version_region = {
        name: _optional_text(pvr_raw[name], f"product_version_region.{name}")
        for name in _PRODUCT_VERSION_REGION_FIELDS
    }

    verification_raw = _object(raw["verification"], "verification", _VERIFICATION_FIELDS)
    verification_state = verification_raw["state"]
    if verification_state not in VERIFICATION_STATES:
        _fail(f"verification.state must be one of {sorted(VERIFICATION_STATES)}")
    verification_refs = _text_list(
        verification_raw["evidence_refs"],
        "verification.evidence_refs",
        non_empty=verification_state == "PASS",
    )
    evidence_refs = tuple(item.ref for item in evidence)
    if verification_refs != evidence_refs:
        _fail("verification.evidence_refs must exactly match evidence refs in order")

    severity_raw = _object(
        raw["severity_assessment"], "severity_assessment", _SEVERITY_FIELDS
    )
    severity = severity_raw["classification"]
    if severity is not None and severity not in SEVERITIES:
        _fail(f"severity_assessment.classification must be null or one of {sorted(SEVERITIES)}")
    severity_reason = _optional_text(
        severity_raw["reason_text"], "severity_assessment.reason_text"
    )
    if verification_state != "PASS" and (severity is not None or severity_reason is not None):
        _fail("severity classification/reason must be null unless verification.state is PASS")
    if verification_state == "PASS" and severity is None:
        _fail("severity_assessment.classification is required when verification.state is PASS")
    if severity in {"MATERIAL_BREAKING", "EXCEPTIONAL_BREAKING"} and severity_reason is None:
        _fail("severity_assessment.reason_text is required for breaking severity")

    coverage_raw = _object(
        raw["recent_coverage"], "recent_coverage", _RECENT_COVERAGE_FIELDS
    )
    recent_coverage = RecentCoverageAssessment(
        related_coverage_exists=_boolean(
            coverage_raw["related_coverage_exists"],
            "recent_coverage.related_coverage_exists",
        ),
        incremental_value_present=_boolean(
            coverage_raw["incremental_value_present"],
            "recent_coverage.incremental_value_present",
        ),
        assessment_ref=_text(
            coverage_raw["assessment_ref"], "recent_coverage.assessment_ref"
        ),
        freshness_ref=_text(
            coverage_raw["freshness_ref"], "recent_coverage.freshness_ref"
        ),
    )

    safety_raw = _object(raw["story_safety"], "story_safety", _STORY_SAFETY_FIELDS)
    story_safety = StorySafetyAssessment(
        quality_pass=_boolean(safety_raw["quality_pass"], "story_safety.quality_pass"),
        quality_ref=_text(safety_raw["quality_ref"], "story_safety.quality_ref"),
        dependencies_available=_boolean(
            safety_raw["dependencies_available"],
            "story_safety.dependencies_available",
        ),
        dependencies_ref=_text(
            safety_raw["dependencies_ref"], "story_safety.dependencies_ref"
        ),
    )

    main_assessment = _parse_main(raw["main_assessment"], severity)

    return BreakingWorkflowInput(
        schema=SCHEMA,
        contract_version=CONTRACT_VERSION,
        candidate_id=candidate_id,
        candidate_version=candidate_version,
        assessment_ref=assessment_ref,
        state_snapshot_ref=state_snapshot_ref,
        topic=topic,
        topic_cluster=topic_cluster,
        content_type=content_type,
        evidence=evidence,
        follow_up_delta=follow_up_delta,
        source_attribution=_text(raw["source_attribution"], "source_attribution"),
        limitations=limitations,
        product_version_region=product_version_region,
        source_image=_optional_text(raw["source_image"], "source_image"),
        verification=VerificationAssessment(verification_state, verification_refs),
        severity=SeverityAssessment(severity, severity_reason),
        recent_coverage=recent_coverage,
        story_safety=story_safety,
        main_assessment=main_assessment,
    )
