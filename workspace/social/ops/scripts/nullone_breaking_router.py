#!/usr/bin/env python3
"""Deterministic breaking draft routing decision core (#36).

Implements `nullone.breaking-routing.v1`
(docs/contracts/breaking-routing-policy-v1.md, issue #34) exactly: given
one candidate's already-resolved severity assessment, its already-computed
`nullone.breaking-identity.v1` result (issue #35), and already-resolved
quality/format/load/dependency findings, deterministically compute a
routing decision.

Scope boundaries (see issue #36):

* No network access, no LLM calls, no severity classification from raw
  article prose. Severity is an accepted upstream assessment input.
* Consumes a validated #35 identity/dedup result; never reimplements
  identity matching, state-precedence or suppression rules -- the #35
  result's own `reason_code` already carries the correct routing-order
  tier (EXISTING_CONSEQUENTIAL_STATE / UNRESOLVED_EVENT_HISTORY /
  EXISTING_DRAFT_REQUEST / CANDIDATE_EXCLUDED / EXACT_EVENT_DUPLICATE /
  SAME_EVENT_DIFFERENT_SOURCE / IDENTITY_UNRESOLVED /
  STATE_UNAVAILABLE_OR_CONFLICTING), so this module maps those codes
  directly onto routing orders 1-4 rather than re-deriving precedence.
* No draft creation, no manifest writes, no Telegram, no publication.
  This module returns a decision object only; dispatch/side effects are
  #36's separate `nullone_breaking_dispatch.py`.
* Invalid/malformed input is rejected as `PolicyInputError`
  (POLICY_INPUT_INVALID) before any routing decision is produced -- a
  consumer/domain error, never a ninth route.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

SCHEMA = "nullone.breaking-routing.v1"
CONTRACT_VERSION = "1.0.0"

IDENTITY_SCHEMA = "nullone.breaking-identity.v1"

SEVERITIES = frozenset({"NORMAL", "MATERIAL_BREAKING", "EXCEPTIONAL_BREAKING"})
VERIFICATION_STATES = frozenset({"UNVERIFIED", "PARTIAL", "PASS", "BLOCKED"})
DEDUP_DECISIONS = frozenset(
    {
        "EXACT_DUPLICATE",
        "SAME_EVENT",
        "MATERIAL_FOLLOW_UP",
        "DISTINCT_EVENT",
        "AMBIGUOUS_IDENTITY",
    }
)

ROUTING_DECISIONS = frozenset(
    {
        "NORMAL_QUEUE",
        "IMMEDIATE_STORY_DRAFT",
        "IMMEDIATE_STORY_AND_MAIN_DRAFT",
        "SUPPRESS_DUPLICATE",
        "SUPPRESS_RECENT_COVERAGE",
        "BLOCKED_UNVERIFIED",
        "BLOCKED_AMBIGUOUS_IDENTITY",
        "BLOCKED_DRAFT_SAFETY",
    }
)

ACCELERATED_DECISIONS = frozenset(
    {"IMMEDIATE_STORY_DRAFT", "IMMEDIATE_STORY_AND_MAIN_DRAFT"}
)

# reason codes belonging to routing orders 1-3 (consequential/reserved/
# excluded/exact/same-source duplicate), sourced verbatim from #35.
_ORDER_1_CODES = frozenset({"EXISTING_CONSEQUENTIAL_STATE", "UNRESOLVED_EVENT_HISTORY"})
_ORDER_2_CODES = frozenset({"EXISTING_DRAFT_REQUEST", "CANDIDATE_EXCLUDED"})
_ORDER_3_CODES = frozenset({"EXACT_EVENT_DUPLICATE", "SAME_EVENT_DIFFERENT_SOURCE"})
_SUPPRESS_CODES = _ORDER_1_CODES | _ORDER_2_CODES | _ORDER_3_CODES

_AMBIGUOUS_CODES = frozenset({"IDENTITY_UNRESOLVED", "STATE_UNAVAILABLE_OR_CONFLICTING"})

# Existing production scoring thresholds (SCORING.md / accepted #34
# policy) -- not invented here.
STORY_SCORE_THRESHOLD = 26
FEED_SCORE_THRESHOLD = 38
CAROUSEL_SCORE_THRESHOLD = 42

MAIN_FORMATS = frozenset({"FEED", "CAROUSEL"})


class PolicyInputError(ValueError):
    """Structurally invalid/inconsistent input -- POLICY_INPUT_INVALID.

    Raised before any routing decision is produced. Never a ninth route.
    """


def _fail(message: str) -> None:
    raise PolicyInputError(message)


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field_name} is required and must be a non-empty string")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{field_name} must be a boolean")
    return value


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{field_name} must be a non-negative integer")
    return value


# ---------------------------------------------------------------------------
# Structured input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationInput:
    state: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in VERIFICATION_STATES:
            _fail(f"verification.state is invalid: {self.state!r}")
        if self.state == "PASS" and not self.evidence_refs:
            _fail("verification.evidence_refs must be non-empty for PASS")


@dataclass(frozen=True)
class RecentCoverageInput:
    related_coverage_exists: bool
    incremental_value_present: bool

    def __post_init__(self) -> None:
        _require_bool(self.related_coverage_exists, "recent_coverage.related_coverage_exists")
        _require_bool(self.incremental_value_present, "recent_coverage.incremental_value_present")


@dataclass(frozen=True)
class StorySafetyInput:
    """Already-resolved Story quality/load/dependency safety findings.

    These are safety gates, never bypassed by breaking timing (issue #36
    section 9) -- computed upstream (e.g. from #32's cadence/load state
    adapter), never re-derived here.
    """

    quality_pass: bool
    load_pass: bool
    dependencies_available: bool

    def __post_init__(self) -> None:
        _require_bool(self.quality_pass, "story_safety.quality_pass")
        _require_bool(self.load_pass, "story_safety.load_pass")
        _require_bool(self.dependencies_available, "story_safety.dependencies_available")


@dataclass(frozen=True)
class MainFormatFindings:
    """Structured, auditable main-format suitability findings.

    Consumed only for deterministic FEED/CAROUSEL selection; the router
    never asks an LLM to choose a format (issue #36 section 11).
    `capacity_available` is the already-resolved main-load safety gate
    (FEED+CAROUSEL share one load class); it is never bypassed either.
    """

    feed_score: int
    carousel_score: int
    single_visual_value: bool
    concise_announcement_value: bool
    meaningful_multi_slide_value: bool
    comparison_value: bool
    sequence_value: bool
    multi_fact_value: bool
    material_context_value: bool
    available_source_media: bool
    capacity_available: bool

    def __post_init__(self) -> None:
        _require_non_negative_int(self.feed_score, "main_format.feed_score")
        _require_non_negative_int(self.carousel_score, "main_format.carousel_score")
        for name in (
            "single_visual_value",
            "concise_announcement_value",
            "meaningful_multi_slide_value",
            "comparison_value",
            "sequence_value",
            "multi_fact_value",
            "material_context_value",
            "available_source_media",
            "capacity_available",
        ):
            _require_bool(getattr(self, name), f"main_format.{name}")


@dataclass(frozen=True)
class RoutingInput:
    candidate_id: str
    assessment_ref: str
    state_snapshot_ref: str
    severity: str | None
    verification: VerificationInput
    identity: Mapping[str, Any]
    recent_coverage: RecentCoverageInput
    story_safety: StorySafetyInput
    severity_reason_text: str | None = None
    main_justification: str | None = None
    main_format: MainFormatFindings | None = None

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        _require_text(self.assessment_ref, "assessment_ref")
        _require_text(self.state_snapshot_ref, "state_snapshot_ref")

        if self.severity is not None and self.severity not in SEVERITIES:
            _fail(f"severity is invalid: {self.severity!r}")

        if self.severity is not None and self.verification.state != "PASS":
            _fail("severity must be null unless verification.state is PASS")

        if self.severity in ("MATERIAL_BREAKING", "EXCEPTIONAL_BREAKING"):
            if not isinstance(self.severity_reason_text, str) or not self.severity_reason_text.strip():
                _fail(
                    "severity_reason_text is required (non-empty) for "
                    "MATERIAL_BREAKING/EXCEPTIONAL_BREAKING"
                )

        if not isinstance(self.identity, Mapping):
            _fail("identity must be an object")
        if self.identity.get("schema") != IDENTITY_SCHEMA:
            _fail(f"identity.schema must be {IDENTITY_SCHEMA!r}")
        if self.identity.get("candidate_id") != self.candidate_id:
            _fail("identity.candidate_id must match candidate_id")
        dedup = self.identity.get("dedup")
        if not isinstance(dedup, Mapping) or dedup.get("decision") not in DEDUP_DECISIONS:
            _fail("identity.dedup.decision is missing or invalid")
        event = self.identity.get("event")
        if not isinstance(event, Mapping):
            _fail("identity.event is missing or invalid")
        if not isinstance(self.identity.get("reconciliation_required"), bool):
            _fail("identity.reconciliation_required must be a boolean")
        if not isinstance(self.identity.get("reason_code"), str) or not self.identity["reason_code"].strip():
            _fail("identity.reason_code is required")
        if not isinstance(self.identity.get("reason_text"), str) or not self.identity["reason_text"].strip():
            _fail("identity.reason_text is required")

        if self.main_justification is not None:
            if not isinstance(self.main_justification, str) or not self.main_justification.strip():
                _fail("main_justification must be a non-empty string when provided")
            if self.severity != "EXCEPTIONAL_BREAKING":
                _fail("main_justification may only be provided for EXCEPTIONAL_BREAKING")
            if self.main_format is None:
                _fail("main_format findings are required when main_justification is provided")


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingResult:
    candidate_id: str
    assessment_ref: str
    state_snapshot_ref: str
    severity: str | None
    event: Mapping[str, Any]
    verification: Mapping[str, Any]
    dedup: Mapping[str, Any]
    routing_decision: str
    reason_code: str
    reason_text: str
    draft_targets: tuple[str, ...]
    main_draft_justification: str | None
    reconciliation_required: bool
    main_format_reason: str | None = None

    def __post_init__(self) -> None:
        if self.routing_decision not in ROUTING_DECISIONS:
            raise PolicyInputError(f"Unknown routing_decision: {self.routing_decision!r}")
        allowed_target_sets = ((), ("STORY",), ("STORY", "FEED"), ("STORY", "CAROUSEL"))
        if tuple(self.draft_targets) not in allowed_target_sets:
            raise PolicyInputError(f"Invalid draft_targets combination: {self.draft_targets!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "candidate_id": self.candidate_id,
            "assessment_ref": self.assessment_ref,
            "state_snapshot_ref": self.state_snapshot_ref,
            "severity": self.severity,
            "event": dict(self.event),
            "verification": dict(self.verification),
            "dedup": dict(self.dedup),
            "routing_decision": self.routing_decision,
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "draft_targets": list(self.draft_targets),
            "main_draft_justification": self.main_draft_justification,
            "reconciliation_required": self.reconciliation_required,
        }


def _verification_dict(v: VerificationInput) -> dict[str, Any]:
    return {"state": v.state, "evidence_refs": list(v.evidence_refs)}


def _base_kwargs(ri: RoutingInput) -> dict[str, Any]:
    return {
        "candidate_id": ri.candidate_id,
        "assessment_ref": ri.assessment_ref,
        "state_snapshot_ref": ri.state_snapshot_ref,
        "event": ri.identity["event"],
        "verification": _verification_dict(ri.verification),
    }


def _dedup_dict(ri: RoutingInput) -> dict[str, Any]:
    dedup = ri.identity["dedup"]
    return {
        "decision": dedup["decision"],
        "matched_refs": list(dedup.get("matched_refs") or []),
        "parent_development_id": dedup.get("parent_development_id"),
        "follow_up_reason": dedup.get("follow_up_reason"),
    }


# ---------------------------------------------------------------------------
# Deterministic FEED vs CAROUSEL selection (issue #36 section 11)
# ---------------------------------------------------------------------------


def select_main_format(findings: MainFormatFindings) -> tuple[str, str] | None:
    """Return (format, reason) or None if no target should be selected.

    None means: no format qualifies, capacity is unavailable, or both
    qualify but structural preference cannot be proven deterministically.
    Quality > extra format -- ambiguity never resolves to an arbitrary
    pick.
    """

    if not findings.capacity_available:
        return None

    feed_qualifies = (
        findings.feed_score >= FEED_SCORE_THRESHOLD and findings.available_source_media
    )
    carousel_qualifies = (
        findings.carousel_score >= CAROUSEL_SCORE_THRESHOLD
        and findings.meaningful_multi_slide_value
    )

    if not feed_qualifies and not carousel_qualifies:
        return None

    if carousel_qualifies and not feed_qualifies:
        return (
            "CAROUSEL",
            "Carousel score meets threshold with meaningful multi-slide "
            "value; Feed did not qualify.",
        )

    if feed_qualifies and not carousel_qualifies:
        return (
            "FEED",
            "Feed score meets threshold with available source media; "
            "Carousel did not qualify (score or multi-slide value).",
        )

    # Both qualify: structural preference must be deterministic.
    multi_slide_signal = (
        findings.comparison_value
        or findings.sequence_value
        or findings.multi_fact_value
        or findings.material_context_value
    )
    single_signal = findings.single_visual_value or findings.concise_announcement_value

    if multi_slide_signal and not single_signal:
        return (
            "CAROUSEL",
            "Both formats qualify; structural evidence (comparison/"
            "sequence/multi-fact/material context) supports Carousel.",
        )

    if single_signal and not multi_slide_signal:
        return (
            "FEED",
            "Both formats qualify; structural evidence supports one "
            "concise announcement / one dominant visual -> Feed.",
        )

    return None


# ---------------------------------------------------------------------------
# Routing order 1-11 (docs/contracts/breaking-routing-policy-v1.md)
# ---------------------------------------------------------------------------


def evaluate_routing(ri: RoutingInput) -> RoutingResult:
    """Pure deterministic evaluation of one candidate's routing decision.

    Raises PolicyInputError for structurally invalid input (never a
    route). Performs no I/O, no network, no side effects. Calling twice
    with identical arguments returns an identical result.
    """

    dedup = ri.identity["dedup"]
    decision = dedup["decision"]
    identity_reason_code = ri.identity["reason_code"]
    identity_reason_text = ri.identity["reason_text"]
    identity_reconciliation = ri.identity["reconciliation_required"]

    base = _base_kwargs(ri)
    dedup_dict = _dedup_dict(ri)

    # Orders 1-3: consequential/reserved/excluded/exact/same-source
    # duplicate-like suppression -- #35's own reason_code already encodes
    # the correct tier; never re-derived here.
    if decision in ("EXACT_DUPLICATE", "SAME_EVENT", "MATERIAL_FOLLOW_UP") and (
        identity_reason_code in _SUPPRESS_CODES
    ):
        return RoutingResult(
            **base,
            severity=ri.severity,
            dedup=dedup_dict,
            routing_decision="SUPPRESS_DUPLICATE",
            reason_code=identity_reason_code,
            reason_text=identity_reason_text,
            draft_targets=(),
            main_draft_justification=None,
            reconciliation_required=identity_reconciliation,
        )

    # Order 4: ambiguous identity/state.
    if decision == "AMBIGUOUS_IDENTITY":
        if identity_reason_code not in _AMBIGUOUS_CODES:
            _fail(f"Unexpected ambiguous identity reason_code: {identity_reason_code!r}")
        return RoutingResult(
            **base,
            severity=ri.severity,
            dedup=dedup_dict,
            routing_decision="BLOCKED_AMBIGUOUS_IDENTITY",
            reason_code=identity_reason_code,
            reason_text=identity_reason_text,
            draft_targets=(),
            main_draft_justification=None,
            reconciliation_required=True,
        )

    # From here decision is DISTINCT_EVENT, or a safely-linked
    # MATERIAL_FOLLOW_UP (parent history resolved, non-ambiguous).
    if decision == "MATERIAL_FOLLOW_UP" and identity_reconciliation:
        _fail(
            "MATERIAL_FOLLOW_UP with reconciliation_required=True must "
            "have been handled by order 1 (UNRESOLVED_EVENT_HISTORY)"
        )

    # Order 5: verification not PASS.
    if ri.verification.state != "PASS":
        return RoutingResult(
            **base,
            severity=None,
            dedup=dedup_dict,
            routing_decision="BLOCKED_UNVERIFIED",
            reason_code="EVIDENCE_INSUFFICIENT",
            reason_text=(
                f"Candidate verification is not PASS (state={ri.verification.state})."
            ),
            draft_targets=(),
            main_draft_justification=None,
            reconciliation_required=False,
        )

    # Order 6: recent related coverage without incremental value.
    if ri.recent_coverage.related_coverage_exists and not ri.recent_coverage.incremental_value_present:
        return RoutingResult(
            **base,
            severity=ri.severity,
            dedup=dedup_dict,
            routing_decision="SUPPRESS_RECENT_COVERAGE",
            reason_code="NO_INCREMENTAL_AUDIENCE_VALUE",
            reason_text=(
                "Related coverage exists without an explicit incremental "
                "audience value finding."
            ),
            draft_targets=(),
            main_draft_justification=None,
            reconciliation_required=False,
        )

    # Order 7: NORMAL.
    if ri.severity == "NORMAL":
        return RoutingResult(
            **base,
            severity="NORMAL",
            dedup=dedup_dict,
            routing_decision="NORMAL_QUEUE",
            reason_code="NORMAL_CADENCE",
            reason_text=ri.severity_reason_text or "Ordinary editorial queue.",
            draft_targets=(),
            main_draft_justification=None,
            reconciliation_required=False,
        )

    if ri.severity not in ("MATERIAL_BREAKING", "EXCEPTIONAL_BREAKING"):
        _fail(
            "severity must be resolved to NORMAL/MATERIAL_BREAKING/"
            "EXCEPTIONAL_BREAKING once verification is PASS and no "
            f"earlier terminal rule applies; got {ri.severity!r}"
        )

    # Order 8: Story quality/load/dependency safety, in priority order.
    if not ri.story_safety.quality_pass:
        return _blocked_draft_safety(ri, base, dedup_dict, "STORY_QUALITY_BLOCK")
    if not ri.story_safety.load_pass:
        return _blocked_draft_safety(ri, base, dedup_dict, "STORY_LOAD_BLOCK")
    if not ri.story_safety.dependencies_available:
        return _blocked_draft_safety(ri, base, dedup_dict, "DRAFT_DEPENDENCY_UNAVAILABLE")

    # Order 9: MATERIAL_BREAKING -> Story only, never automatic main.
    if ri.severity == "MATERIAL_BREAKING":
        return RoutingResult(
            **base,
            severity="MATERIAL_BREAKING",
            dedup=dedup_dict,
            routing_decision="IMMEDIATE_STORY_DRAFT",
            reason_code="MATERIAL_TIME_VALUE",
            reason_text=ri.severity_reason_text or "Material breaking time value.",
            draft_targets=("STORY",),
            main_draft_justification=None,
            reconciliation_required=False,
        )

    # severity == EXCEPTIONAL_BREAKING: orders 10-11.
    main_choice = None
    if ri.main_justification is not None:
        assert ri.main_format is not None
        main_choice = select_main_format(ri.main_format)

    if main_choice is not None:
        main_format, format_reason = main_choice
        # format_reason is durable audit context for *why FEED vs CAROUSEL*
        # fits structurally -- distinct from main_draft_justification (why
        # standalone main has audience value at all). RoutingResult.to_dict()
        # deliberately never grows a 15th field (nullone.breaking-routing.v1
        # is a strict, accepted schema -- see test_breaking_routing_contract
        # .py), so the explanation is folded into the existing reason_text
        # field to survive persistence; the raw format_reason also remains
        # available on this RoutingResult object (main_format_reason) for a
        # caller that wants to persist it separately as draft-set audit
        # metadata outside the routing schema (see nullone_breaking_dispatch
        # .reserve_draft_set's main_format_reason parameter).
        combined_reason_text = (
            f"{ri.severity_reason_text or 'Exceptional breaking main value.'} "
            f"Main format selection ({main_format}): {format_reason}"
        )
        return RoutingResult(
            **base,
            severity="EXCEPTIONAL_BREAKING",
            dedup=dedup_dict,
            routing_decision="IMMEDIATE_STORY_AND_MAIN_DRAFT",
            reason_code="EXCEPTIONAL_MAIN_VALUE",
            reason_text=combined_reason_text,
            draft_targets=("STORY", main_format),
            main_draft_justification=ri.main_justification,
            reconciliation_required=False,
            main_format_reason=format_reason,
        )

    # Order 11: Story eligible but main omitted/blocked -- no main-only
    # fallback, ever.
    return RoutingResult(
        **base,
        severity="EXCEPTIONAL_BREAKING",
        dedup=dedup_dict,
        routing_decision="IMMEDIATE_STORY_DRAFT",
        reason_code="EXCEPTIONAL_STORY_ONLY",
        reason_text=ri.severity_reason_text or "Exceptional breaking; main omitted or ineligible.",
        draft_targets=("STORY",),
        main_draft_justification=None,
        reconciliation_required=False,
    )


def _blocked_draft_safety(
    ri: RoutingInput,
    base: dict[str, Any],
    dedup_dict: dict[str, Any],
    reason_code: str,
) -> RoutingResult:
    return RoutingResult(
        **base,
        severity=ri.severity,
        dedup=dedup_dict,
        routing_decision="BLOCKED_DRAFT_SAFETY",
        reason_code=reason_code,
        reason_text=f"Story draft safety gate failed: {reason_code}.",
        draft_targets=(),
        main_draft_justification=None,
        reconciliation_required=False,
    )


# ---------------------------------------------------------------------------
# Self-test (offline, no fixtures, no network) -- registered in run_offline.py
# ---------------------------------------------------------------------------


def _identity(
    *,
    event_id: str | None,
    development_id: str | None,
    decision: str,
    reason_code: str,
    reason_text: str = "test",
    reconciliation_required: bool = False,
    candidate_id: str = "cand-1",
) -> dict[str, Any]:
    return {
        "schema": IDENTITY_SCHEMA,
        "candidate_id": candidate_id,
        "event": {
            "event_id": event_id,
            "development_id": development_id,
            "topic_id": "topic-1",
            "identity_basis": "EXACT_IDENTIFIER" if event_id else "UNRESOLVED",
            "identity_refs": [],
        },
        "dedup": {
            "decision": decision,
            "matched_refs": [],
            "parent_development_id": None,
            "follow_up_reason": None,
        },
        "reason_code": reason_code,
        "reason_text": reason_text,
        "reconciliation_required": reconciliation_required,
    }


def self_test() -> int:
    verification_pass = VerificationInput(state="PASS", evidence_refs=("ev-1",))
    quiet_coverage = RecentCoverageInput(related_coverage_exists=False, incremental_value_present=False)
    safe_story = StorySafetyInput(quality_pass=True, load_pass=True, dependencies_available=True)
    distinct_identity = _identity(
        event_id="event-1", development_id="dev-1", decision="DISTINCT_EVENT",
        reason_code="DISTINCT_DEVELOPMENT",
    )

    normal_input = RoutingInput(
        candidate_id="cand-1",
        assessment_ref="assess-1",
        state_snapshot_ref="state-1",
        severity="NORMAL",
        verification=verification_pass,
        identity=distinct_identity,
        recent_coverage=quiet_coverage,
        story_safety=safe_story,
    )
    normal_result = evaluate_routing(normal_input)
    assert normal_result.routing_decision == "NORMAL_QUEUE"
    assert normal_result.draft_targets == ()

    material_input = RoutingInput(
        candidate_id="cand-1",
        assessment_ref="assess-1",
        state_snapshot_ref="state-1",
        severity="MATERIAL_BREAKING",
        severity_reason_text="Material time value.",
        verification=verification_pass,
        identity=distinct_identity,
        recent_coverage=quiet_coverage,
        story_safety=safe_story,
    )
    material_result = evaluate_routing(material_input)
    assert material_result.routing_decision == "IMMEDIATE_STORY_DRAFT"
    assert material_result.draft_targets == ("STORY",)
    assert material_result.reason_code == "MATERIAL_TIME_VALUE"

    carousel_findings = MainFormatFindings(
        feed_score=30,
        carousel_score=50,
        single_visual_value=False,
        concise_announcement_value=False,
        meaningful_multi_slide_value=True,
        comparison_value=True,
        sequence_value=False,
        multi_fact_value=False,
        material_context_value=False,
        available_source_media=True,
        capacity_available=True,
    )
    exceptional_input = RoutingInput(
        candidate_id="cand-1",
        assessment_ref="assess-1",
        state_snapshot_ref="state-1",
        severity="EXCEPTIONAL_BREAKING",
        severity_reason_text="Exceptional main value.",
        verification=verification_pass,
        identity=distinct_identity,
        recent_coverage=quiet_coverage,
        story_safety=safe_story,
        main_justification="Standalone comparison value beyond the Story.",
        main_format=carousel_findings,
    )
    exceptional_result = evaluate_routing(exceptional_input)
    assert exceptional_result.routing_decision == "IMMEDIATE_STORY_AND_MAIN_DRAFT"
    assert exceptional_result.draft_targets == ("STORY", "CAROUSEL")

    # Determinism: identical input twice -> identical result.
    again = evaluate_routing(exceptional_input)
    assert again.to_dict() == exceptional_result.to_dict()

    # MATERIAL_BREAKING can never carry a main justification.
    try:
        RoutingInput(
            candidate_id="cand-1",
            assessment_ref="assess-1",
            state_snapshot_ref="state-1",
            severity="MATERIAL_BREAKING",
            severity_reason_text="x",
            verification=verification_pass,
            identity=distinct_identity,
            recent_coverage=quiet_coverage,
            story_safety=safe_story,
            main_justification="not allowed",
            main_format=carousel_findings,
        )
        raise AssertionError("expected PolicyInputError")
    except PolicyInputError:
        pass

    print("BREAKING_ROUTER_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("DETERMINISTIC=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Breaking Draft Router V1")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
