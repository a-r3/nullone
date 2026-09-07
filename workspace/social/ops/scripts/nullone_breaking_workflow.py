#!/usr/bin/env python3
"""Scheduler-independent NullOne BreakingWorkflow application layer (#63).

One validated scheduler occurrence and one strict Radar assessment flow through
the already-accepted #35 identity, #36 routing and #36 durable Story-first
dispatcher.  Draft creation and human-review preview delivery remain behind the
existing Story/main pipeline provider ports; this module cannot publish,
schedule, approve, or speak any OpenClaw/Telegram/Zernio protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import nullone_breaking_dispatch as breaking_dispatch
import nullone_breaking_identity as breaking_identity
import nullone_breaking_router as breaking_router
import nullone_cadence_controller as cadence_controller
from nullone_breaking_main_candidate_provider import (
    SELECTION_OK,
    BreakingMainCandidateProvider,
    select_bound_main_candidate,
)
from nullone_breaking_workflow_input import (
    BreakingWorkflowInput,
    BreakingWorkflowInputError,
    validate_breaking_workflow_input,
)
from nullone_cadence_controller import CadenceContractError
from nullone_cadence_state_adapter import CadenceStateError, collect_format_loads
from nullone_main_draft_pipeline import MainPipelineResult, run_main_pipeline
from nullone_scheduler_invocation import SchedulerInvocationError, accept_workflow_trigger
from nullone_story_pipeline import StoryCandidateNotEligible, run_story_pipeline, validate_candidate

BREAKING_WORKFLOW_ID = "breaking"
ACCELERATED_DECISIONS = breaking_dispatch.ACCELERATED_DECISIONS

DependencyRecheck = Callable[[str], bool]
RUN_OUTCOME_REASON_TEXT_MAX = 240


def _run_outcome_reason_text(value: str) -> str:
    """Return deterministic single-line #27-compatible diagnostic text."""

    normalized = " ".join(value.split())
    if not normalized:
        normalized = "Breaking workflow did not complete."
    if len(normalized) <= RUN_OUTCOME_REASON_TEXT_MAX:
        return normalized
    return normalized[: RUN_OUTCOME_REASON_TEXT_MAX - 1].rstrip() + "…"


@dataclass
class BreakingWorkflowResult:
    occurrence_id: str | None
    candidate_id: str | None
    assessment_ref: str | None
    identity_result: dict[str, Any] | None
    routing_result: dict[str, Any] | None
    draft_set_id: str | None
    dispatch_result: breaking_dispatch.DispatchResult | None
    domain_outcome: str
    reconciliation_required: bool
    reason_code: str
    reason_text: str

    def to_dict(self) -> dict[str, Any]:
        dispatch = None
        if self.dispatch_result is not None:
            dispatch = {
                "draft_set_id": self.dispatch_result.draft_set_id,
                "created": self.dispatch_result.created,
                "main_target": self.dispatch_result.main_target,
                "record": self.dispatch_result.record,
            }
        return {
            "occurrence_id": self.occurrence_id,
            "candidate_id": self.candidate_id,
            "assessment_ref": self.assessment_ref,
            "identity_result": self.identity_result,
            "routing_result": self.routing_result,
            "draft_set_id": self.draft_set_id,
            "dispatch_result": dispatch,
            "domain_outcome": self.domain_outcome,
            "reconciliation_required": self.reconciliation_required,
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
        }

    def run_outcome_mapping(self) -> dict[str, Any]:
        """Return the deterministic #27 inputs for a future persistent adapter.

        Persistence/scheduler health remain adapter responsibilities.  This
        mapping deliberately does not create a second run-outcome store.
        """

        required_artifacts: list[str] = []
        if self.dispatch_result is not None:
            for progress in self.dispatch_result.record.get("targets", {}).values():
                manifest_id = progress.get("manifest_id")
                if progress.get("status") == "SUCCEEDED" and manifest_id:
                    required_artifacts.append(f"social/ops/manifests/{manifest_id}.json")
        successful_no_action = self.domain_outcome == "SUCCEEDED" and not required_artifacts
        return {
            "domain_outcome": self.domain_outcome,
            "reason_code": None if self.domain_outcome == "SUCCEEDED" else self.reason_code,
            "reason_text": (
                None
                if self.domain_outcome == "SUCCEEDED"
                else _run_outcome_reason_text(self.reason_text)
            ),
            "empty_success": "NO_ACTION" if successful_no_action else None,
            "required_artifacts": required_artifacts,
        }


def _result(
    *,
    trigger: Any,
    assessment: BreakingWorkflowInput | None,
    domain_outcome: str,
    reason_code: str,
    reason_text: str,
    identity_result: dict[str, Any] | None = None,
    routing_result: dict[str, Any] | None = None,
    draft_set_id: str | None = None,
    dispatch_result: breaking_dispatch.DispatchResult | None = None,
    reconciliation_required: bool = False,
) -> BreakingWorkflowResult:
    occurrence_id = trigger.get("occurrence_id") if isinstance(trigger, Mapping) else None
    return BreakingWorkflowResult(
        occurrence_id=occurrence_id if isinstance(occurrence_id, str) else None,
        candidate_id=assessment.candidate_id if assessment else None,
        assessment_ref=assessment.assessment_ref if assessment else None,
        identity_result=identity_result,
        routing_result=routing_result,
        draft_set_id=draft_set_id,
        dispatch_result=dispatch_result,
        domain_outcome=domain_outcome,
        reconciliation_required=reconciliation_required,
        reason_code=reason_code,
        reason_text=reason_text,
    )


def _validated_limits(config: dict[str, Any] | None) -> dict[str, Any]:
    """Reuse #32's exact defaults and deterministic config validation."""

    # `_merge_config` is the controller's single existing implementation of
    # unknown-field, type and cross-field validation.  Calling it here does
    # not evaluate regular cadence or grant PREPARE_* permission.
    return cadence_controller._merge_config(config or {})


def _effective_load(load: Mapping[str, Any], name: str) -> int:
    published = load.get("published_today")
    pending = load.get("pending")
    if (
        isinstance(published, bool)
        or not isinstance(published, int)
        or published < 0
        or isinstance(pending, bool)
        or not isinstance(pending, int)
        or pending < 0
    ):
        raise CadenceStateError(f"{name} load counters are malformed")
    return published + pending


def _load_capacity(
    *,
    state_root: Path,
    now: datetime,
    timezone_name: str,
    cadence_config: dict[str, Any] | None,
) -> tuple[dict[str, Any], int, int]:
    config = _validated_limits(cadence_config)
    loads = collect_format_loads(
        state_root=state_root,
        now=now,
        timezone_name=timezone_name,
        config=config,
    )
    return (
        config,
        _effective_load(loads["story_load"], "story"),
        _effective_load(loads["main_load"], "main"),
    )


def _main_findings(
    assessment: BreakingWorkflowInput, *, capacity_available: bool
) -> breaking_router.MainFormatFindings | None:
    if assessment.main_assessment is None:
        return None
    values = dict(assessment.main_assessment.findings)
    values["capacity_available"] = capacity_available
    return breaking_router.MainFormatFindings(**values)


def _routing_input(
    assessment: BreakingWorkflowInput,
    *,
    identity_result: dict[str, Any],
    story_capacity_available: bool,
    main_capacity_available: bool,
) -> breaking_router.RoutingInput:
    return breaking_router.RoutingInput(
        candidate_id=assessment.candidate_id,
        assessment_ref=assessment.assessment_ref,
        state_snapshot_ref=assessment.state_snapshot_ref,
        severity=assessment.severity.classification,
        severity_reason_text=assessment.severity.reason_text,
        verification=breaking_router.VerificationInput(
            state=assessment.verification.state,
            evidence_refs=assessment.verification.evidence_refs,
        ),
        identity=identity_result,
        recent_coverage=breaking_router.RecentCoverageInput(
            related_coverage_exists=assessment.recent_coverage.related_coverage_exists,
            incremental_value_present=assessment.recent_coverage.incremental_value_present,
        ),
        story_safety=breaking_router.StorySafetyInput(
            quality_pass=assessment.story_safety.quality_pass,
            load_pass=story_capacity_available,
            dependencies_available=assessment.story_safety.dependencies_available,
        ),
        main_justification=(
            assessment.main_assessment.standalone_justification
            if assessment.main_assessment is not None
            else None
        ),
        main_format=_main_findings(
            assessment, capacity_available=main_capacity_available
        ),
    )


def _compose_authoritative_recheck(
    *,
    candidate_input: breaking_identity.CandidateInput,
    workspace: Path,
    state_root: Path,
    now: datetime,
    timezone_name: str,
    cadence_config: dict[str, Any] | None,
    dependency_recheck: DependencyRecheck | None,
    prior_developments: Any,
    prior_developments_path: Path | None,
) -> breaking_dispatch.AuthoritativeDispatchRecheck:
    identity_recheck = breaking_dispatch.make_state_authoritative_recheck(
        candidate_input,
        workspace,
        prior_developments=prior_developments,
        prior_developments_path=prior_developments_path,
    )

    def _recheck(*, stage: str, record: dict[str, Any]) -> breaking_dispatch.RecheckResult:
        identity_result = identity_recheck(stage=stage, record=record)
        if not identity_result.permitted:
            return identity_result

        if stage == "STORY":
            try:
                config, story_load, _ = _load_capacity(
                    state_root=state_root,
                    now=now,
                    timezone_name=timezone_name,
                    cadence_config=cadence_config,
                )
            except (CadenceStateError, CadenceContractError, KeyError, TypeError):
                return breaking_dispatch.RecheckResult(
                    permitted=False,
                    reason_code="STORY_LOAD_STATE_UNAVAILABLE",
                    reason_text="Authoritative Story load could not be proven at dispatch.",
                    reconciliation_required=True,
                )
            if story_load >= config["story_target_max_breaking"]:
                return breaking_dispatch.RecheckResult(
                    permitted=False,
                    reason_code="STORY_LOAD_BLOCK",
                    reason_text="Breaking Story maximum is exhausted at dispatch.",
                )

        if dependency_recheck is None:
            return breaking_dispatch.RecheckResult(
                permitted=False,
                reason_code="DRAFT_DEPENDENCY_RECHECK_MISSING",
                reason_text="No authoritative dispatch-time dependency recheck was supplied.",
            )
        try:
            dependencies_available = dependency_recheck(stage)
        except Exception:  # noqa: BLE001 - unknown dependency read is never PASS
            return breaking_dispatch.RecheckResult(
                permitted=False,
                reason_code="DRAFT_DEPENDENCY_STATE_UNAVAILABLE",
                reason_text="Draft dependency state could not be proven at dispatch.",
                reconciliation_required=True,
            )
        if dependencies_available is not True:
            return breaking_dispatch.RecheckResult(
                permitted=False,
                reason_code="DRAFT_DEPENDENCY_UNAVAILABLE",
                reason_text="A required draft dependency is unavailable at dispatch.",
            )
        return breaking_dispatch.RecheckResult(permitted=True)

    return _recheck


def run_breaking_workflow(
    trigger: dict[str, Any],
    assessment_payload: dict[str, Any],
    *,
    workspace: Path,
    state_root: Path,
    now: datetime,
    story_writer: Any,
    story_verifier: Any,
    draft_connector: Any,
    review_delivery: Any,
    dependency_recheck: DependencyRecheck | None,
    main_candidate_provider: BreakingMainCandidateProvider | None = None,
    main_final_verifier: Any | None = None,
    cadence_config: dict[str, Any] | None = None,
    timezone_name: str = "Asia/Baku",
    prior_developments: Any = None,
    prior_developments_path: Path | None = None,
) -> BreakingWorkflowResult:
    """Evaluate and, only for an accelerated route, dispatch one draft set."""

    try:
        accept_workflow_trigger(trigger, workflow_id=BREAKING_WORKFLOW_ID)
    except SchedulerInvocationError as exc:
        return _result(
            trigger=trigger,
            assessment=None,
            domain_outcome="BLOCKED",
            reason_code="TRIGGER_REJECTED",
            reason_text=f"Breaking scheduler invocation rejected: {exc}",
        )

    try:
        assessment = validate_breaking_workflow_input(assessment_payload)
    except BreakingWorkflowInputError as exc:
        return _result(
            trigger=trigger,
            assessment=None,
            domain_outcome="BLOCKED",
            reason_code="INPUT_REJECTED",
            reason_text=f"Breaking assessment rejected: {exc}",
        )

    if state_root.resolve() != (workspace.resolve() / "social"):
        return _result(
            trigger=trigger,
            assessment=assessment,
            domain_outcome="BLOCKED",
            reason_code="STATE_ROOT_MISMATCH",
            reason_text="Identity and cadence loads must resolve through the same workspace state root.",
        )

    candidate_input = assessment.identity_candidate()
    try:
        identity_state = breaking_identity.load_repository_state(
            workspace,
            prior_developments=prior_developments,
            prior_developments_path=prior_developments_path,
        )
        identity = breaking_identity.evaluate(candidate_input, identity_state)
        identity_dict = identity.to_dict()
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _result(
            trigger=trigger,
            assessment=assessment,
            domain_outcome="BLOCKED",
            reason_code="IDENTITY_STATE_BLOCKED",
            reason_text=f"Authoritative identity state could not be evaluated: {exc}",
            reconciliation_required=True,
        )

    try:
        config, story_load, main_load = _load_capacity(
            state_root=state_root,
            now=now,
            timezone_name=timezone_name,
            cadence_config=cadence_config,
        )
        routing = breaking_router.evaluate_routing(
            _routing_input(
                assessment,
                identity_result=identity_dict,
                story_capacity_available=(
                    story_load < config["story_target_max_breaking"]
                ),
                main_capacity_available=(main_load < config["main_target_max_breaking"]),
            )
        )
        routing_dict = breaking_router.validate_routing_result_dict(routing.to_dict())
    except (CadenceStateError, CadenceContractError, breaking_router.PolicyInputError, KeyError, TypeError) as exc:
        return _result(
            trigger=trigger,
            assessment=assessment,
            identity_result=identity_dict,
            domain_outcome="BLOCKED",
            reason_code="STATE_OR_ROUTING_BLOCKED",
            reason_text=f"Authoritative load/routing evaluation failed: {exc}",
            reconciliation_required=identity.reconciliation_required,
        )

    if routing.routing_decision not in ACCELERATED_DECISIONS:
        domain_outcome, reason_code, reason_text = breaking_dispatch.dispatch_domain_outcome(
            routing_dict, None
        )
        return _result(
            trigger=trigger,
            assessment=assessment,
            identity_result=identity_dict,
            routing_result=routing_dict,
            domain_outcome=domain_outcome,
            reason_code=reason_code or routing.reason_code,
            reason_text=reason_text or routing.reason_text,
            reconciliation_required=routing.reconciliation_required,
        )

    event_id = routing_dict["event"]["event_id"]
    development_id = routing_dict["event"]["development_id"]
    draft_set_id = breaking_dispatch.compute_draft_set_id(event_id, development_id)

    story_candidate = assessment.story_candidate(request_lineage=draft_set_id)
    try:
        validate_candidate(story_candidate)
    except StoryCandidateNotEligible as exc:
        return _result(
            trigger=trigger,
            assessment=assessment,
            identity_result=identity_dict,
            routing_result=routing_dict,
            draft_set_id=draft_set_id,
            domain_outcome="BLOCKED",
            reason_code="STORY_CANDIDATE_INVALID",
            reason_text=str(exc),
        )

    main_target = next((target for target in routing.draft_targets if target != "STORY"), None)
    authoritative_recheck = _compose_authoritative_recheck(
        candidate_input=candidate_input,
        workspace=workspace,
        state_root=state_root,
        now=now,
        timezone_name=timezone_name,
        cadence_config=cadence_config,
        dependency_recheck=dependency_recheck,
        prior_developments=prior_developments,
        prior_developments_path=prior_developments_path,
    )

    def _story_runner() -> Any:
        return run_story_pipeline(
            story_candidate,
            writer=story_writer,
            verifier=story_verifier,
            draft_connector=draft_connector,
            telegram_sender=review_delivery,
        )

    def _main_runner(selected_format: str) -> Any:
        def _blocked(reason_code: str, reason_text: str) -> MainPipelineResult:
            return MainPipelineResult(
                outcome="CANDIDATE_NOT_ELIGIBLE",
                reason_code=reason_code,
                reason_text=reason_text,
            )

        # The dispatcher invokes this closure only after Story has reached
        # SUCCEEDED (draft created + exact SENT preview proof). Optional-main
        # preparation therefore cannot suppress an otherwise valid Story.
        if main_candidate_provider is None or main_final_verifier is None:
            return _blocked(
                "MAIN_DRAFT_DEPENDENCY_UNAVAILABLE",
                "Selected main target has no candidate provider/final verifier.",
            )
        try:
            raw_main_candidates = main_candidate_provider.get_candidates(
                candidate_id=assessment.candidate_id,
                selected_format=selected_format,
                request_lineage=draft_set_id,
            )
        except Exception as exc:  # noqa: BLE001 - provider has no review-create capability
            return _blocked(
                "MAIN_CANDIDATE_PROVIDER_FAILED",
                f"Prepared main candidate provider failed ({type(exc).__name__}).",
            )
        main_candidate, selection, selection_context = select_bound_main_candidate(
            raw_main_candidates,
            candidate_id=assessment.candidate_id,
            selected_format=selected_format,
            request_lineage=draft_set_id,
            evidence_refs=assessment.verification.evidence_refs,
            source_attribution=assessment.source_attribution,
        )
        if selection != SELECTION_OK:
            return _blocked(
                selection_context["reason_code"],
                "Prepared main candidate is unavailable, ambiguous, or not exactly bound.",
            )
        assert main_candidate is not None
        return run_main_pipeline(
            main_candidate,
            final_verifier=main_final_verifier,
            draft_connector=draft_connector,
            telegram_sender=review_delivery,
        )

    def _main_capacity_recheck() -> bool:
        try:
            fresh_config, _, fresh_main_load = _load_capacity(
                state_root=state_root,
                now=now,
                timezone_name=timezone_name,
                cadence_config=cadence_config,
            )
        except (CadenceStateError, CadenceContractError, KeyError, TypeError):
            return False
        return fresh_main_load < fresh_config["main_target_max_breaking"]

    try:
        dispatched = breaking_dispatch.dispatch_draft_set(
            routing_dict,
            story_runner=_story_runner,
            authoritative_recheck=authoritative_recheck,
            main_runner=_main_runner if main_target is not None else None,
            main_capacity_recheck=(
                _main_capacity_recheck if main_target is not None else None
            ),
            main_format_reason=routing.main_format_reason,
        )
    except breaking_dispatch.DraftSetConflict as exc:
        return _result(
            trigger=trigger,
            assessment=assessment,
            identity_result=identity_dict,
            routing_result=routing_dict,
            draft_set_id=draft_set_id,
            domain_outcome="UNKNOWN",
            reason_code="DRAFT_SET_CONFLICT",
            reason_text=str(exc),
            reconciliation_required=True,
        )
    except (breaking_dispatch.DraftSetError, breaking_dispatch.DispatchRejected) as exc:
        return _result(
            trigger=trigger,
            assessment=assessment,
            identity_result=identity_dict,
            routing_result=routing_dict,
            draft_set_id=draft_set_id,
            domain_outcome="UNKNOWN",
            reason_code="DRAFT_SET_STATE_UNAVAILABLE",
            reason_text=str(exc),
            reconciliation_required=True,
        )

    domain_outcome, reason_code, reason_text = breaking_dispatch.dispatch_domain_outcome(
        routing_dict, dispatched
    )
    return _result(
        trigger=trigger,
        assessment=assessment,
        identity_result=identity_dict,
        routing_result=routing_dict,
        draft_set_id=draft_set_id,
        dispatch_result=dispatched,
        domain_outcome=domain_outcome,
        reason_code=reason_code or routing.reason_code,
        reason_text=reason_text or routing.reason_text,
        reconciliation_required=dispatched.reconciliation_required,
    )
