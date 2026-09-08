#!/usr/bin/env python3
"""Scheduled production Story trigger boundary (#79).

Composes one validated `nullone.scheduler-invocation.v1` Story trigger
into a truthful, persisted, observable scheduled run:

    validated trigger
    -> Baku editorial date derived from `scheduled_for`
    -> load/validate ONE structured handoff snapshot (fail-closed source)
    -> derive `story_quality_candidate_available` from that same snapshot
    -> production provider bound to that same snapshot
    -> existing `run_story_workflow` (cadence + candidate + #33 + delivery)
    -> #27 `assess_run` + `emit_result_once` under run-outcomes/story
    -> #30 notification decision at most once
    -> typed `StoryScheduledResult`

Scheduler-vs-domain rule (accepted #59 principle, unchanged): orchestration
COMPLETED means the occurrence, result, and notification state were safely
established -- never that a draft was produced. A legitimate completed
domain outcome (`SUCCEEDED`/`BLOCKED`/`FAILED`) exits 0. Only an
orchestration-establishment failure (rejected trigger, unreadable source
or cadence state, unexpected crash, untrusted notification state) is
application FAILED (non-zero, owned by the scheduler-native alert, never
duplicated by the domain notifier -- the domain notifier is invoked only
on the COMPLETED path).

Critical dependency rule (enforced by capability-negative tests): this
module must never import OpenClaw internals, invoke `openclaw`, know
Telegram/Zernio transport details, know cron syntax/job UUIDs, publish,
approve, schedule, or create Zernio drafts. Writer/verifier/draft/
delivery/notifier implementations are caller-injected; this module never
imports their concrete infrastructure adapters itself.
"""
from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from nullone_bridge_common import WORKSPACE as DEFAULT_WORKSPACE_ROOT
from nullone_editorial_candidate_handoff import (
    EditorialHandoffError,
    find_consumed_story_request_ids,
    handoff_relative_path,
    load_handoff_snapshot,
)
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
    derive_local_date,
    safe_notification_result_context,
    safe_trigger_context,
    validate_notification_outcome,
)
from nullone_scheduler_invocation import SchedulerInvocationError, accept_workflow_trigger
from nullone_story_production_provider import (
    StructuredHandoffStoryProvider,
    snapshot_story_availability,
)
from nullone_story_workflow import STORY_WORKFLOW_ID, run_story_workflow

STORY_RUN_OUTCOME_SUBPATH = Path("social/ops/run-outcomes/story")

APPLICATION_EXECUTION_STATES = frozenset({"COMPLETED", "FAILED"})

# StoryWorkflow outcomes that are truthful, completed domain non-success:
# the occurrence ran; no draft exists; nothing crashed.
DOMAIN_BLOCKED_OUTCOMES = frozenset(
    {
        "CANDIDATE_UNAVAILABLE",
        "CANDIDATE_INVALID",
        "CANDIDATE_AMBIGUOUS",
        "REVIEW_DRAFT_ALREADY_CONSUMED",
    }
)


class StoryScheduledWorkflowError(RuntimeError):
    """Caller contract violation building a StoryScheduledResult (never
    raised for a legitimate application-level outcome -- those return)."""


@dataclass
class StoryScheduledResult:
    application_execution: str
    domain_outcome: str | None
    run_id: str | None
    occurrence_id: str | None
    result_file: str | None
    notification_status: str | None
    reason_code: str
    reason_text: str
    story_outcome: str | None = None
    editorial_date: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.application_execution not in APPLICATION_EXECUTION_STATES:
            raise StoryScheduledWorkflowError(
                f"Unknown application_execution: {self.application_execution!r}"
            )


def _failed(
    *,
    reason_code: str,
    reason_text: str,
    occurrence_id: str | None = None,
    run_id: str | None = None,
    editorial_date: str | None = None,
    context: dict[str, Any] | None = None,
) -> StoryScheduledResult:
    return StoryScheduledResult(
        application_execution="FAILED",
        domain_outcome=None,
        run_id=run_id,
        occurrence_id=occurrence_id,
        result_file=None,
        notification_status=None,
        reason_code=reason_code,
        reason_text=reason_text,
        editorial_date=editorial_date,
        context=context or {},
    )


def _occurrence_lock_path(output_root: Path, run_id: str) -> Path:
    return output_root.resolve() / f"{run_id}.lock"


def _story_outcome_to_domain(outcome: str) -> tuple[str, str, str]:
    """Map a StoryWorkflow outcome to (scheduler_status, domain_outcome, reason_code)."""

    if outcome == "DRAFT_CREATED":
        return ("succeeded", "SUCCEEDED", "OK")
    if outcome == "NO_ACTION":
        return ("succeeded", "SUCCEEDED", "OK")
    if outcome in DOMAIN_BLOCKED_OUTCOMES:
        return ("succeeded", "BLOCKED", outcome)
    return ("succeeded", "FAILED", outcome)


def run_story_trigger(
    trigger: dict[str, Any],
    *,
    writer: Any,
    verifier: Any,
    draft_connector: Any,
    review_delivery: Any,
    notifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
    output_root: Path | None = None,
    timezone_name: str = "Asia/Baku",
    now: datetime | None = None,
) -> StoryScheduledResult:
    """Run one scheduled Story occurrence end to end (review-draft boundary).

    Never raises for a legitimate application-level outcome. Concurrent
    calls for the same occurrence serialize on a per-run lock; a replay
    returns the exact persisted #27 result without re-invoking the
    provider, writer, draft connector, or review delivery.
    """

    try:
        accept_workflow_trigger(trigger, workflow_id=STORY_WORKFLOW_ID)
    except SchedulerInvocationError as exc:
        return _failed(
            reason_code="TRIGGER_REJECTED",
            reason_text=f"Scheduler invocation trigger rejected: {exc}",
            context={"trigger": safe_trigger_context(trigger)},
        )

    occurrence_id = trigger["occurrence_id"]
    scheduled_for = trigger["scheduled_for"]
    expected_run_id = make_run_id(workflow_id=STORY_WORKFLOW_ID, occurrence_id=occurrence_id)

    try:
        editorial_date = derive_local_date(scheduled_for, timezone_name=timezone_name)
    except ScheduledWorkflowSupportError as exc:
        return _failed(
            reason_code="SCHEDULED_FOR_INVALID",
            reason_text=f"Could not derive editorial_date from scheduled_for: {exc}",
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
        )

    resolved_output_root = (
        output_root
        if output_root is not None
        else workspace_root / STORY_RUN_OUTCOME_SUBPATH
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
                    reason_text="Persisted #27 Story result is missing or corrupt.",
                    occurrence_id=occurrence_id,
                    run_id=expected_run_id,
                    editorial_date=editorial_date,
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
                        editorial_date=editorial_date,
                    )
            return StoryScheduledResult(
                application_execution=(
                    "COMPLETED"
                    if persisted["domain_outcome"] in ("SUCCEEDED", "BLOCKED", "FAILED")
                    else "FAILED"
                ),
                domain_outcome=persisted["domain_outcome"],
                run_id=expected_run_id,
                occurrence_id=occurrence_id,
                result_file=str(persisted_file),
                notification_status=notification_status,
                reason_code="OK" if persisted["domain_outcome"] == "SUCCEEDED" else persisted["reason_code"],
                reason_text=(
                    "Story scheduled orchestration completed (replay)."
                    if persisted["domain_outcome"] == "SUCCEEDED"
                    else persisted["reason_text"]
                ),
                story_outcome=None,
                editorial_date=editorial_date,
            )

        try:
            snapshot = load_handoff_snapshot(
                workspace_root=workspace_root, editorial_date=editorial_date
            )
        except EditorialHandoffError as exc:
            message = str(exc)
            code = (
                "SOURCE_HANDOFF_MISSING"
                if "missing" in message
                else "SOURCE_HANDOFF_INVALID"
            )
            return _failed(
                reason_code=code,
                reason_text=(
                    "Structured Morning handoff unavailable or invalid; "
                    "refusing to invent a candidate."
                ),
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                editorial_date=editorial_date,
                context={"handoff_artifact": handoff_relative_path(editorial_date)},
            )

        try:
            consumed = find_consumed_story_request_ids(workspace_root=workspace_root)
        except EditorialHandoffError:
            return _failed(
                reason_code="STORY_STATE_UNREADABLE",
                reason_text="Story manifest state could not be read; failing closed.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                editorial_date=editorial_date,
            )

        availability = {"story_quality_candidate_available": False}
        try:
            availability["story_quality_candidate_available"] = (
                snapshot_story_availability(snapshot, consumed)
            )
            provider = StructuredHandoffStoryProvider(snapshot, consumed)
        except Exception:
            return _failed(
                reason_code="RUNTIME_CRASHED",
                reason_text="Story candidate snapshot/provider failed before workflow.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                editorial_date=editorial_date,
            )

        try:
            story_result = run_story_workflow(
                trigger,
                state_root=workspace_root / "social",
                now=now if now is not None else _now_baku(timezone_name),
                # This scheduled edge evaluates Story only and offers no
                # main candidate: `main_quality_candidate_available` is
                # honestly False (this caller cannot produce main drafts;
                # main production stays on the Draft Factory path). This
                # never manufactures a main recommendation -- cadence can
                # only recommend PREPARE_STORY or NO_ACTION from here.
                candidate_availability={
                    "main_quality_candidate_available": False,
                    **availability,
                },
                candidate_provider=provider,
                writer=writer,
                verifier=verifier,
                draft_connector=draft_connector,
                review_delivery=review_delivery,
                timezone_name=timezone_name,
            )
        except Exception as exc:  # noqa: BLE001 - a workflow crash is typed FAILED
            return _failed(
                reason_code="RUNTIME_CRASHED",
                reason_text="Story workflow raised before establishing a result.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                editorial_date=editorial_date,
                context={"error_type": type(exc).__name__},
            )

        outcome = story_result.outcome

        if outcome == "STATE_BLOCKED":
            return _failed(
                reason_code="STATE_BLOCKED",
                reason_text="Authoritative cadence state unreadable; failing closed.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                editorial_date=editorial_date,
            )
        if outcome == "CANDIDATE_PROVIDER_FAILED":
            return _failed(
                reason_code="CANDIDATE_PROVIDER_FAILED",
                reason_text="Story candidate provider failed unexpectedly.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                editorial_date=editorial_date,
            )
        if outcome == "TRIGGER_REJECTED":
            return _failed(
                reason_code="TRIGGER_REJECTED",
                reason_text="Story trigger rejected inside workflow.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                editorial_date=editorial_date,
            )

        if outcome == "DRAFT_CREATED":
            preview = (story_result.story_pipeline.preview_delivery if story_result.story_pipeline else None) or {}
            if preview.get("status") != "SENT":
                return _failed(
                    reason_code="PREVIEW_UNVERIFIED",
                    reason_text="Story draft exists but preview SENT proof missing.",
                    occurrence_id=occurrence_id,
                    run_id=expected_run_id,
                    editorial_date=editorial_date,
                )

        scheduler_status, domain_outcome, _mapped_code = _story_outcome_to_domain(outcome)

        try:
            if domain_outcome == "SUCCEEDED":
                if outcome == "NO_ACTION":
                    assessed = assess_run(
                        workflow_id=STORY_WORKFLOW_ID,
                        occurrence_id=occurrence_id,
                        scheduler_status=scheduler_status,
                        domain_outcome=domain_outcome,
                        empty_success="NO_ACTION",
                    )
                else:
                    manifest_rel = _manifest_relative(story_result)
                    assessed = assess_run(
                        workflow_id=STORY_WORKFLOW_ID,
                        occurrence_id=occurrence_id,
                        scheduler_status=scheduler_status,
                        domain_outcome=domain_outcome,
                        artifact_root=workspace_root,
                        required_artifacts=(manifest_rel,),
                    )
            else:
                assessed = assess_run(
                    workflow_id=STORY_WORKFLOW_ID,
                    occurrence_id=occurrence_id,
                    scheduler_status=scheduler_status,
                    domain_outcome=domain_outcome,
                    reason_code=_mapped_code,
                    reason_text=_domain_reason_text(story_result),
                )
            emit_result_once(
                resolved_output_root, assessed, artifact_root=workspace_root
            )
        except (CompletionContractError, OSError) as exc:
            return _failed(
                reason_code="RESULT_COMMIT_FAILED",
                reason_text="Story #27 result could not be established.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                editorial_date=editorial_date,
                context={"error_type": type(exc).__name__},
            )

        reloaded_file = result_path(resolved_output_root, expected_run_id)
        try:
            persisted_result = json.loads(reloaded_file.read_text(encoding="utf-8"))
            validate_result_structure(persisted_result)
        except (OSError, json.JSONDecodeError, CompletionContractError):
            return _failed(
                reason_code="RESULT_MISSING_OR_CORRUPT",
                reason_text="Persisted #27 Story result could not be reloaded.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                editorial_date=editorial_date,
            )

        notification_status = None
        if notifier is not None:
            try:
                notification_outcome = notifier(persisted_result)
            except Exception:
                return _failed(
                    reason_code="NOTIFICATION_STATE_UNSAFE",
                    reason_text="Story notification orchestration unsafe.",
                    occurrence_id=occurrence_id,
                    run_id=expected_run_id,
                    editorial_date=editorial_date,
                )
            try:
                notification_status = validate_notification_outcome(notification_outcome)
            except ScheduledWorkflowSupportError:
                return _failed(
                    reason_code="NOTIFICATION_RESULT_INVALID",
                    reason_text=NOTIFICATION_RESULT_INVALID_MESSAGE,
                    occurrence_id=occurrence_id,
                    run_id=expected_run_id,
                    editorial_date=editorial_date,
                    context=safe_notification_result_context(notification_outcome),
                )

        return StoryScheduledResult(
            application_execution="COMPLETED",
            domain_outcome=persisted_result["domain_outcome"],
            run_id=expected_run_id,
            occurrence_id=occurrence_id,
            result_file=str(reloaded_file),
            notification_status=notification_status,
            reason_code="OK",
            reason_text="Story scheduled orchestration completed.",
            story_outcome=outcome,
            editorial_date=editorial_date,
        )
    finally:
        os.close(lock_fd)


def _now_baku(timezone_name: str) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(timezone_name))


def _manifest_relative(story_result: Any) -> str:
    pipeline = story_result.story_pipeline
    manifest_path = pipeline.manifest_path if pipeline else None
    if not isinstance(manifest_path, str) or not manifest_path:
        raise CompletionContractError("Story DRAFT_CREATED has no manifest path")
    return manifest_path


def _domain_reason_text(story_result: Any) -> str:
    text = story_result.reason_text or story_result.outcome
    collapsed = " ".join(str(text).split())
    return collapsed[:240] or story_result.outcome


def self_test() -> int:
    import tempfile
    from datetime import timezone
    from zoneinfo import ZoneInfo

    from nullone_scheduler_invocation import compute_occurrence_id

    def make_trigger(**overrides):
        base = {
            "schema": "nullone.scheduler-invocation.v1",
            "contract_version": "1.0.0",
            "workflow_id": "story",
            "source": "openclaw",
            "external_occurrence_id": "story.check.1030.v1@2026-09-08T06:30:00Z",
            "scheduled_for": "2026-09-08T06:30:00Z",
            "triggered_at": "2026-09-08T06:30:02Z",
        }
        base.update(overrides)
        base["occurrence_id"] = compute_occurrence_id(
            base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
        )
        return base

    def exploding(value_name="dependency"):
        def _explode(*args, **kwargs):
            raise AssertionError(f"{value_name} must not be called")

        return _explode

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out = root / "run-outcomes"

        # 1. Invalid trigger -> FAILED/TRIGGER_REJECTED, zero calls.
        result = run_story_trigger(
            {"workflow_id": "story"},
            writer=exploding("writer"),
            verifier=exploding("verifier"),
            draft_connector=exploding("draft"),
            review_delivery=exploding("delivery"),
            workspace_root=root,
            output_root=out,
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "TRIGGER_REJECTED", result

        # 2. Missing handoff snapshot -> FAILED/SOURCE_HANDOFF_MISSING.
        trigger = make_trigger()
        result = run_story_trigger(
            trigger,
            writer=exploding("writer"),
            verifier=exploding("verifier"),
            draft_connector=exploding("draft"),
            review_delivery=exploding("delivery"),
            workspace_root=root,
            output_root=out,
            notifier=lambda _r: {"status": "NOT_REQUIRED"},
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "SOURCE_HANDOFF_MISSING", result

        # 3. Malformed handoff -> FAILED/SOURCE_HANDOFF_INVALID.
        research = root / "social/research/daily"
        research.mkdir(parents=True, exist_ok=True)
        (research / "2026-09-08-editorial-candidates.json").write_text(
            "{not json", encoding="utf-8"
        )
        result = run_story_trigger(
            trigger,
            writer=exploding("writer"),
            verifier=exploding("verifier"),
            draft_connector=exploding("draft"),
            review_delivery=exploding("delivery"),
            workspace_root=root,
            output_root=out,
            notifier=lambda _r: {"status": "NOT_REQUIRED"},
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "SOURCE_HANDOFF_INVALID", result

    print("STORY_SCHEDULED_WORKFLOW_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_PUBLISH_CAPABILITY=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne scheduled Story trigger boundary")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
