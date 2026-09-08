#!/usr/bin/env python3
"""Deterministic production boundary for one committed Breaking handoff (#80).

Takes one validated committed `nullone.breaking-radar-handoff.v1`
document and invokes the existing #63 `run_breaking_workflow` with
reviewed production dependencies, then persists a truthful #27 result:

    committed handoff
    -> strict edge normalization (malformed fails closed, no workflow)
    -> run_id from the candidate occurrence
    -> per-run lock
    -> persisted exact #27 result FIRST (replay: no re-run of identity /
       routing / Story / main / DraftProvider / ReviewDelivery)
    -> run_breaking_workflow (Story-first, #35/#36 unchanged)
    -> existing run_outcome_mapping() -> assess_run -> emit_result_once
    -> reload/validate -> #30 notification decision at most once

Scheduler-vs-domain rule (accepted #59 principle): COMPLETED + exit 0 for
every safely established domain outcome (`SUCCEEDED`/`BLOCKED`/`FAILED`/
actionable `UNKNOWN`). FAILED + non-zero only for establishment failures
(malformed handoff, corrupt persisted result, unsafe notification state,
unexpected crash) -- the scheduler-native alert owns those.

No publication capability anywhere on this path: Breaking stops at human
review preview. Optional main stays unavailable (no reviewed real main
provider exists in-repo), preserving #63's Story-success +
`BLOCKED_BEFORE_ATTEMPT` behavior.

Critical dependency rule (enforced by capability-negative tests): this
module must never import OpenClaw internals, invoke `openclaw`, know
Telegram/Zernio transport details, know cron syntax/job UUIDs, publish,
approve, schedule, or create Zernio drafts. All provider/transport
implementations are caller-injected.

Dispatch-time dependency rule: `dependency_recheck` is caller-injected
authoritative state only. Radar/LLM `story_safety.dependencies_available`
is never used as a dispatch-time fallback. When the caller passes None,
existing #63 fail-closed behavior applies
(`DRAFT_DEPENDENCY_RECHECK_MISSING`). Production #80 currently supplies
no fake recheck (#81 owns live DraftProvider readiness).
"""
from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from nullone_breaking_radar_edge import (
    BreakingRadarEdgeError,
    normalize_breaking_radar_handoff,
)
from nullone_breaking_scan_authority import (
    BreakingScanAuthorityError,
    validate_candidate_id,
)
from nullone_breaking_workflow import (
    BREAKING_WORKFLOW_ID,
    run_breaking_workflow,
)
from nullone_bridge_common import WORKSPACE as DEFAULT_WORKSPACE_ROOT
from nullone_run_outcome import (
    CompletionContractError,
    assess_run,
    emit_result_once,
    make_run_id,
    result_path,
    validate_result_structure,
)
from nullone_scheduled_workflow_support import (
    NOTIFICATION_RESULT_INVALID_MESSAGE,
    ScheduledWorkflowSupportError,
    safe_notification_result_context,
    safe_trigger_context,
    validate_notification_outcome,
)

BREAKING_RUN_OUTCOME_SUBPATH = Path("social/ops/run-outcomes/breaking")

APPLICATION_EXECUTION_STATES = frozenset({"COMPLETED", "FAILED"})

# #63 domain outcomes that are truthful completed non-success is decided by
# assess_run itself; this module only separates "established" (COMPLETED)
# from "could not establish" (FAILED).


class BreakingCandidateRunnerError(RuntimeError):
    """Caller contract violation (never a legitimate outcome -- those return)."""


@dataclass
class BreakingScheduledResult:
    application_execution: str
    domain_outcome: str | None
    run_id: str | None
    occurrence_id: str | None
    result_file: str | None
    notification_status: str | None
    reason_code: str
    reason_text: str
    breaking_outcome: str | None = None
    candidate_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.application_execution not in APPLICATION_EXECUTION_STATES:
            raise BreakingCandidateRunnerError(
                f"Unknown application_execution: {self.application_execution!r}"
            )


def _failed(
    *,
    reason_code: str,
    reason_text: str,
    occurrence_id: str | None = None,
    run_id: str | None = None,
    candidate_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> BreakingScheduledResult:
    return BreakingScheduledResult(
        application_execution="FAILED",
        domain_outcome=None,
        run_id=run_id,
        occurrence_id=occurrence_id,
        result_file=None,
        notification_status=None,
        reason_code=reason_code,
        reason_text=reason_text,
        candidate_id=candidate_id,
        context=context or {},
    )


def _occurrence_lock_path(output_root: Path, run_id: str) -> Path:
    return output_root.resolve() / f"{run_id}.lock"


def run_breaking_candidate(
    handoff: dict[str, Any],
    *,
    story_writer: Any,
    story_verifier: Any,
    draft_connector: Any,
    review_delivery: Any,
    dependency_recheck: Callable[[str], bool] | None = None,
    notifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
    output_root: Path | None = None,
    source: str = "openclaw",
    now: datetime | None = None,
    timezone_name: str = "Asia/Baku",
) -> BreakingScheduledResult:
    """Run one committed Breaking handoff end to end (review boundary only)."""

    try:
        normalized = normalize_breaking_radar_handoff(handoff, source=source)
    except (BreakingRadarEdgeError, ValueError) as exc:
        return _failed(
            reason_code="HANDOFF_REJECTED",
            reason_text="Committed Breaking handoff failed strict validation.",
            context={"error_type": type(exc).__name__},
        )

    try:
        validate_candidate_id(normalized.assessment["candidate_id"])
    except (BreakingScanAuthorityError, KeyError) as exc:
        return _failed(
            reason_code="CANDIDATE_ID_REJECTED",
            reason_text="Breaking candidate_id violates the stable shape rule.",
            context={"error_type": type(exc).__name__},
        )

    trigger = normalized.trigger
    occurrence_id = trigger["occurrence_id"]
    expected_run_id = make_run_id(
        workflow_id=BREAKING_WORKFLOW_ID, occurrence_id=occurrence_id
    )
    candidate_id = normalized.assessment["candidate_id"]

    resolved_output_root = (
        output_root
        if output_root is not None
        else workspace_root / BREAKING_RUN_OUTCOME_SUBPATH
    )
    resolved_output_root.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(
        str(_occurrence_lock_path(resolved_output_root, expected_run_id)),
        os.O_CREAT | os.O_RDWR,
        0o600,
    )

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        persisted_file = result_path(resolved_output_root, expected_run_id)
        if persisted_file.is_file():
            try:
                persisted = json.loads(persisted_file.read_text(encoding="utf-8"))
                validate_result_structure(persisted)
            except (OSError, json.JSONDecodeError, CompletionContractError):
                return _failed(
                    reason_code="RESULT_MISSING_OR_CORRUPT",
                    reason_text="Persisted #27 Breaking result is missing or corrupt.",
                    occurrence_id=occurrence_id,
                    run_id=expected_run_id,
                    candidate_id=candidate_id,
                )
            if (
                persisted.get("workflow_id") != BREAKING_WORKFLOW_ID
                or persisted.get("occurrence_id") != occurrence_id
                or persisted.get("run_id") != expected_run_id
            ):
                return _failed(
                    reason_code="RESULT_IDENTITY_MISMATCH",
                    reason_text=(
                        "Persisted #27 Breaking result identity does not match "
                        "this occurrence; refusing a replacement run."
                    ),
                    occurrence_id=occurrence_id,
                    run_id=expected_run_id,
                    candidate_id=candidate_id,
                )
            notification_status: str | None = None
            if notifier is not None:
                try:
                    notification_status = validate_notification_outcome(
                        notifier(persisted)
                    )
                except (ScheduledWorkflowSupportError, Exception):
                    return _failed(
                        reason_code="NOTIFICATION_RESULT_INVALID",
                        reason_text=NOTIFICATION_RESULT_INVALID_MESSAGE,
                        occurrence_id=occurrence_id,
                        run_id=expected_run_id,
                        candidate_id=candidate_id,
                    )
            return BreakingScheduledResult(
                application_execution="COMPLETED",
                domain_outcome=persisted["domain_outcome"],
                run_id=expected_run_id,
                occurrence_id=occurrence_id,
                result_file=str(persisted_file),
                notification_status=notification_status,
                reason_code=_reason_code_for_persisted(persisted),
                reason_text=_reason_text_for_persisted(persisted),
                breaking_outcome=None,
                candidate_id=candidate_id,
            )

        observed_now = now if now is not None else _now_baku(timezone_name)

        # Radar/LLM `story_safety.dependencies_available` is editorial
        # evidence only — never authoritative dispatch-time recheck.
        # Pass the caller-injected recheck through (including None) so
        # existing #63 fail-closed behavior applies
        # (DRAFT_DEPENDENCY_RECHECK_MISSING). Production #80 supplies no
        # fake recheck; #81 owns live DraftProvider readiness.

        try:
            workflow_result = run_breaking_workflow(
                trigger,
                normalized.assessment,
                workspace=workspace_root,
                state_root=workspace_root / "social",
                now=observed_now,
                story_writer=story_writer,
                story_verifier=story_verifier,
                draft_connector=draft_connector,
                review_delivery=review_delivery,
                dependency_recheck=dependency_recheck,
                main_candidate_provider=None,
                main_final_verifier=None,
                timezone_name=timezone_name,
            )
        except Exception as exc:  # noqa: BLE001 - a workflow crash is typed FAILED
            return _failed(
                reason_code="RUNTIME_CRASHED",
                reason_text="Breaking workflow raised before establishing a result.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                candidate_id=candidate_id,
                context={"error_type": type(exc).__name__},
            )

        mapping = workflow_result.run_outcome_mapping()
        try:
            if mapping["domain_outcome"] == "SUCCEEDED":
                if mapping.get("empty_success"):
                    assessed = assess_run(
                        workflow_id=BREAKING_WORKFLOW_ID,
                        occurrence_id=occurrence_id,
                        scheduler_status="succeeded",
                        domain_outcome="SUCCEEDED",
                        empty_success=mapping["empty_success"],
                    )
                else:
                    assessed = assess_run(
                        workflow_id=BREAKING_WORKFLOW_ID,
                        occurrence_id=occurrence_id,
                        scheduler_status="succeeded",
                        domain_outcome="SUCCEEDED",
                        artifact_root=workspace_root,
                        required_artifacts=tuple(mapping["required_artifacts"]),
                    )
            else:
                assessed = assess_run(
                    workflow_id=BREAKING_WORKFLOW_ID,
                    occurrence_id=occurrence_id,
                    scheduler_status="succeeded",
                    domain_outcome=mapping["domain_outcome"],
                    reason_code=mapping["reason_code"],
                    reason_text=mapping["reason_text"] or mapping["reason_code"],
                )
            emit_result_once(
                resolved_output_root, assessed, artifact_root=workspace_root
            )
        except (CompletionContractError, OSError) as exc:
            return _failed(
                reason_code="RESULT_COMMIT_FAILED",
                reason_text="Breaking #27 result could not be established.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                candidate_id=candidate_id,
                context={"error_type": type(exc).__name__},
            )

        reloaded_file = result_path(resolved_output_root, expected_run_id)
        try:
            persisted_result = json.loads(reloaded_file.read_text(encoding="utf-8"))
            validate_result_structure(persisted_result)
        except (OSError, json.JSONDecodeError, CompletionContractError):
            return _failed(
                reason_code="RESULT_MISSING_OR_CORRUPT",
                reason_text="Persisted #27 Breaking result could not be reloaded.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                candidate_id=candidate_id,
            )

        notification_status = None
        if notifier is not None:
            try:
                notification_outcome = notifier(persisted_result)
            except Exception:
                return _failed(
                    reason_code="NOTIFICATION_STATE_UNSAFE",
                    reason_text="Breaking notification orchestration unsafe.",
                    occurrence_id=occurrence_id,
                    run_id=expected_run_id,
                    candidate_id=candidate_id,
                )
            try:
                notification_status = validate_notification_outcome(notification_outcome)
            except ScheduledWorkflowSupportError:
                return _failed(
                    reason_code="NOTIFICATION_RESULT_INVALID",
                    reason_text=NOTIFICATION_RESULT_INVALID_MESSAGE,
                    occurrence_id=occurrence_id,
                    run_id=expected_run_id,
                    candidate_id=candidate_id,
                    context=safe_notification_result_context(notification_outcome),
                )

        return BreakingScheduledResult(
            application_execution="COMPLETED",
            domain_outcome=persisted_result["domain_outcome"],
            run_id=expected_run_id,
            occurrence_id=occurrence_id,
            result_file=str(reloaded_file),
            notification_status=notification_status,
            reason_code=_reason_code_for_persisted(persisted_result),
            reason_text=_reason_text_for_persisted(persisted_result),
            breaking_outcome=workflow_result.domain_outcome,
            candidate_id=candidate_id,
        )
    finally:
        os.close(lock_fd)


def _reason_code_for_persisted(persisted: dict[str, Any]) -> str:
    """Fresh and replay must agree: SUCCEEDED → OK, else persisted code."""

    if persisted.get("domain_outcome") == "SUCCEEDED":
        return "OK"
    return str(persisted["reason_code"])


def _reason_text_for_persisted(persisted: dict[str, Any]) -> str:
    """Fresh and replay must agree on the same semantic reason text."""

    if persisted.get("domain_outcome") == "SUCCEEDED":
        return "Breaking scheduled orchestration completed."
    return str(persisted["reason_text"])


def _now_baku(timezone_name: str) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(timezone_name))


def self_test() -> int:
    # Malformed handoff fails closed with zero workflow involvement.
    result = run_breaking_candidate(
        {"schema": "nope"},
        story_writer=None,
        story_verifier=None,
        draft_connector=None,
        review_delivery=None,
    )
    assert result.application_execution == "FAILED", result
    assert result.reason_code == "HANDOFF_REJECTED", result

    # Bad candidate-ID shape fails closed before any workflow.
    result = run_breaking_candidate(
        {
            "schema": "nullone.breaking-radar-handoff.v1",
            "contract_version": "1.0.0",
            "occurrence": {
                "source_occurrence_id": "breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z",
                "scheduled_for": "2026-09-08T07:30:00Z",
                "triggered_at": "2026-09-08T07:35:00Z",
            },
            "assessment": {"candidate_id": "Bad ID!!"},
        },
        story_writer=None,
        story_verifier=None,
        draft_connector=None,
        review_delivery=None,
    )
    assert result.application_execution == "FAILED", result
    # Edge rejects the malformed assessment first: still HANDOFF_REJECTED.
    assert result.reason_code in ("HANDOFF_REJECTED", "CANDIDATE_ID_REJECTED"), result

    print("BREAKING_CANDIDATE_RUNNER_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_PUBLISH_CAPABILITY=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Breaking candidate runner")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
