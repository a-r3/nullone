#!/usr/bin/env python3
"""NullOne `MorningWorkflow` application orchestrator (#59).

Per `docs/architecture/nullone-application-runtime.md`, this module is the
NullOne Application Runtime layer for the Morning Editorial workflow:

    validated `nullone.scheduler-invocation.v1` trigger
    -> board_date derived from `scheduled_for` in Asia/Baku
    -> existing #28 domain runtime (`nullone_editorial_runtime.run_morning_editorial`)
    -> exact persisted #27 result reloaded from disk and validated
    -> in-memory/persisted reconciliation
    -> existing #30 notification decision (`nullone_failure_notify.notify_if_required`)
    -> truthful `MorningWorkflowResult`

It reuses #27/#28/#30 domain logic verbatim: no retry policy, no board
rendering, no notification/actionability/sanitizer logic is reimplemented
here. This module's own job is strictly composition and the scheduler-vs-
domain separation documented in
`docs/architecture/nullone-application-runtime.md` ("Critical scheduler-vs-
domain rule"): `application_execution` reflects only whether orchestration
itself safely established/validated the occurrence, result and notification
state -- never the domain outcome. A `domain_outcome` of `BLOCKED`, `FAILED`,
or `UNKNOWN` is still a completed, healthy *orchestration*.

Critical dependency rule (restated, enforced by
`tests/test_scheduled_workflows_capability_negative.py`): this module must
never import OpenClaw internals, invoke `openclaw`, know Telegram/Zernio
transport details, know cron syntax/job UUIDs, publish, approve, schedule,
or create Zernio drafts. The Claude CLI provider invocation and the
Telegram/OpenClaw notification transport are both supplied by the caller as
injected dependencies (`invoke_provider`, `notifier`); this module never
imports their concrete infrastructure adapters itself.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nullone_editorial_runtime import (
    RUN_OUTCOME_ROOT as DEFAULT_OUTPUT_ROOT,
    run_morning_editorial,
)
from nullone_run_outcome import (
    CompletionContractError,
    make_run_id,
    result_path,
    validate_result_structure,
)
from nullone_scheduled_workflow_support import (
    ScheduledWorkflowSupportError,
    derive_local_date,
    results_agree,
    safe_trigger_context,
    validate_notification_outcome,
)
from nullone_scheduler_invocation import SchedulerInvocationError, accept_workflow_trigger

MORNING_WORKFLOW_ID = "morning-editorial"

# WORKSPACE default kept as a lazy attribute lookup (not a module-level
# import-time constant) so tests that monkeypatch nullone_bridge_common's
# WORKSPACE before calling run_morning_workflow still take effect, matching
# nullone_editorial_runtime's own default-argument convention.
from nullone_bridge_common import WORKSPACE as DEFAULT_ARTIFACT_ROOT  # noqa: E402

APPLICATION_EXECUTION_STATES = frozenset({"COMPLETED", "FAILED"})


class MorningWorkflowError(RuntimeError):
    """Caller contract violation building a MorningWorkflowResult (never
    raised for a legitimate application-level failure -- those are
    returned, not raised)."""


@dataclass
class MorningWorkflowResult:
    application_execution: str
    domain_outcome: str | None
    run_id: str | None
    occurrence_id: str | None
    result_file: str | None
    notification_status: str | None
    reconciliation_required: bool
    reason_code: str
    reason_text: str
    board_date: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.application_execution not in APPLICATION_EXECUTION_STATES:
            raise MorningWorkflowError(
                f"Unknown application_execution: {self.application_execution!r}"
            )


def _failed(
    *,
    reason_code: str,
    reason_text: str,
    occurrence_id: str | None = None,
    run_id: str | None = None,
    reconciliation_required: bool = False,
    board_date: str | None = None,
    context: dict[str, Any] | None = None,
) -> MorningWorkflowResult:
    return MorningWorkflowResult(
        application_execution="FAILED",
        domain_outcome=None,
        run_id=run_id,
        occurrence_id=occurrence_id,
        result_file=None,
        notification_status=None,
        reconciliation_required=reconciliation_required,
        reason_code=reason_code,
        reason_text=reason_text,
        board_date=board_date,
        context=context or {},
    )


def _notification_failed(
    *,
    reason_code: str,
    reason_text: str,
    occurrence_id: str,
    run_id: str,
    persisted_result: dict[str, Any],
    result_file: Path,
    board_date: str | None,
    context: dict[str, Any] | None = None,
) -> MorningWorkflowResult:
    """A #27 result was established/validated/reconciled, but the
    notification decision itself could not be trusted. Distinct from
    `_failed()`: domain_outcome/result_file are known-good here and must
    still be reported."""

    return MorningWorkflowResult(
        application_execution="FAILED",
        domain_outcome=persisted_result["domain_outcome"],
        run_id=run_id,
        occurrence_id=occurrence_id,
        result_file=str(result_file),
        notification_status=None,
        reconciliation_required=False,
        reason_code=reason_code,
        reason_text=reason_text,
        board_date=board_date,
        context=context or {},
    )


def run_morning_workflow(
    trigger: dict[str, Any],
    *,
    invoke_provider: Callable[[], None],
    notifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    run_editorial: Callable[..., dict[str, Any]] = run_morning_editorial,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    sleep: Callable[[float], None] = time.sleep,
    timezone_name: str = "Asia/Baku",
) -> MorningWorkflowResult:
    """Run one Morning Editorial scheduled occurrence end to end.

    Never raises for a legitimate application-level failure; returns a
    `MorningWorkflowResult` whose `application_execution` is `"FAILED"`
    only when orchestration itself could not safely establish/validate the
    occurrence, result, or notification state (see module docstring). A
    `domain_outcome` of anything other than `SUCCEEDED` is still
    `application_execution == "COMPLETED"`.

    `run_editorial` defaults to the real #28 runtime; tests may inject a
    fake to exercise this module's own reload/reconciliation logic in
    isolation, without duplicating #28's own exhaustive test suite.
    """

    try:
        accept_workflow_trigger(trigger, workflow_id=MORNING_WORKFLOW_ID)
    except SchedulerInvocationError as exc:
        return _failed(
            reason_code="TRIGGER_REJECTED",
            reason_text=f"Scheduler invocation trigger rejected: {exc}",
            context={"trigger": safe_trigger_context(trigger)},
        )

    occurrence_id = trigger["occurrence_id"]
    scheduled_for = trigger["scheduled_for"]
    expected_run_id = make_run_id(workflow_id=MORNING_WORKFLOW_ID, occurrence_id=occurrence_id)

    try:
        board_date = derive_local_date(scheduled_for, timezone_name=timezone_name)
    except ScheduledWorkflowSupportError as exc:
        return _failed(
            reason_code="SCHEDULED_FOR_INVALID",
            reason_text=f"Could not derive board_date from scheduled_for: {exc}",
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
        )

    # Only past this point may the provider/runtime ever be invoked.

    try:
        in_memory_result = run_editorial(
            occurrence_id=occurrence_id,
            board_date=board_date,
            invoke_provider=invoke_provider,
            artifact_root=artifact_root,
            output_root=output_root,
            sleep=sleep,
        )
    except Exception as exc:  # noqa: BLE001 - a runtime crash is a typed orchestration failure
        return _failed(
            reason_code="RUNTIME_CRASHED",
            reason_text="Morning Editorial runtime raised before establishing a result.",
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
            board_date=board_date,
            context={"error_type": type(exc).__name__},
        )

    # The exact persisted #27 result is authoritative -- never the in-memory
    # return, stdout, or CLI exit code alone.
    persisted_file = result_path(output_root, expected_run_id)

    try:
        persisted_result = json.loads(persisted_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _failed(
            reason_code="RESULT_MISSING_OR_CORRUPT",
            reason_text=f"Could not reload persisted #27 result: {exc}",
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
            board_date=board_date,
        )

    if not isinstance(persisted_result, dict):
        return _failed(
            reason_code="RESULT_MISSING_OR_CORRUPT",
            reason_text="Persisted #27 result was not a JSON object.",
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
            board_date=board_date,
        )

    try:
        validate_result_structure(persisted_result)
    except CompletionContractError as exc:
        return _failed(
            reason_code="RESULT_INVALID",
            reason_text=f"Persisted #27 result failed structural validation: {exc}",
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
            board_date=board_date,
        )

    if (
        persisted_result["workflow_id"] != MORNING_WORKFLOW_ID
        or persisted_result["occurrence_id"] != occurrence_id
        or persisted_result["run_id"] != expected_run_id
    ):
        return _failed(
            reason_code="RESULT_IDENTITY_MISMATCH",
            reason_text=(
                "Persisted #27 result identity does not match this occurrence's "
                "workflow_id/occurrence_id/run_id."
            ),
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
            board_date=board_date,
        )

    if not results_agree(in_memory_result, persisted_result):
        return _failed(
            reason_code="RESULT_RECONCILIATION_REQUIRED",
            reason_text=(
                "The in-memory runtime return disagrees with the exact persisted "
                "#27 result; refusing to proceed to notification."
            ),
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
            reconciliation_required=True,
            board_date=board_date,
        )

    # Only now -- with an exact, validated, reconciled persisted #27 result
    # in hand -- may the #30 notification decision be invoked, at most once.
    notification_status: str | None = None
    if notifier is not None:
        try:
            notification_outcome = notifier(persisted_result)
        except Exception as exc:  # noqa: BLE001 - unsafe/corrupt notifier state, never silently retried
            return _notification_failed(
                reason_code="NOTIFICATION_STATE_UNSAFE",
                reason_text="Notification orchestration could not safely proceed.",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                persisted_result=persisted_result,
                result_file=persisted_file,
                board_date=board_date,
                context={"error_type": type(exc).__name__},
            )

        try:
            notification_status = validate_notification_outcome(notification_outcome)
        except ScheduledWorkflowSupportError as exc:
            return _notification_failed(
                reason_code="NOTIFICATION_RESULT_INVALID",
                reason_text=f"Notifier returned an invalid result: {exc}",
                occurrence_id=occurrence_id,
                run_id=expected_run_id,
                persisted_result=persisted_result,
                result_file=persisted_file,
                board_date=board_date,
            )

    return MorningWorkflowResult(
        application_execution="COMPLETED",
        domain_outcome=persisted_result["domain_outcome"],
        run_id=expected_run_id,
        occurrence_id=occurrence_id,
        result_file=str(persisted_file),
        notification_status=notification_status,
        reconciliation_required=False,
        reason_code="OK",
        reason_text="Morning workflow orchestration completed.",
        board_date=board_date,
    )


def self_test() -> int:
    import tempfile

    from nullone_editorial_runtime import ProviderUnreachableError
    from nullone_run_outcome import assess_run, emit_result_once
    from nullone_scheduler_invocation import compute_occurrence_id

    def make_trigger(**overrides: Any) -> dict[str, Any]:
        base = {
            "schema": "nullone.scheduler-invocation.v1",
            "contract_version": "1.0.0",
            "workflow_id": "morning-editorial",
            "source": "openclaw",
            "external_occurrence_id": "openclaw-occ-morning-0001",
            "scheduled_for": "2026-09-08T04:30:00Z",
            "triggered_at": "2026-09-08T04:30:02Z",
        }
        base.update(overrides)
        base["occurrence_id"] = compute_occurrence_id(
            base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
        )
        return base

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        artifact_root = root / "artifacts"
        output_root = root / "run-outcomes"

        # 1. Invalid trigger -> TRIGGER_REJECTED, zero provider calls.
        def exploding_provider() -> None:
            raise AssertionError("provider must not be called")

        result = run_morning_workflow(
            {"workflow_id": "morning-editorial"},
            invoke_provider=exploding_provider,
            artifact_root=artifact_root,
            output_root=output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "TRIGGER_REJECTED", result

        # 2. Wrong workflow_id -> TRIGGER_REJECTED.
        wrong = make_trigger(workflow_id="daily-analytics")
        wrong["source"] = "openclaw"
        result = run_morning_workflow(
            wrong, invoke_provider=exploding_provider, artifact_root=artifact_root,
            output_root=output_root, sleep=lambda _s: None,
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "TRIGGER_REJECTED", result

        # 3. Valid success on first attempt -> COMPLETED/SUCCEEDED, quiet notifier.
        case3_artifact_root = root / "case3" / "artifacts"
        case3_output_root = root / "case3" / "run-outcomes"
        trigger = make_trigger()
        calls: list[int] = []

        def succeed_immediately() -> None:
            calls.append(1)
            board = case3_artifact_root / f"social/research/daily/{'2026-09-08'}-editorial-board.md"
            board.parent.mkdir(parents=True, exist_ok=True)
            board.write_text("# Editorial board\n", encoding="utf-8")

        notifier_calls: list[dict[str, Any]] = []

        def fake_notifier(persisted: dict[str, Any]) -> dict[str, Any]:
            notifier_calls.append(persisted)
            return {"status": "NOT_REQUIRED"}

        result = run_morning_workflow(
            trigger,
            invoke_provider=succeed_immediately,
            notifier=fake_notifier,
            artifact_root=case3_artifact_root,
            output_root=case3_output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "COMPLETED", result
        assert result.domain_outcome == "SUCCEEDED", result
        assert result.board_date == "2026-09-08", result
        assert result.notification_status == "NOT_REQUIRED", result
        assert len(calls) == 1
        assert len(notifier_calls) == 1

        # 4. Baku date-boundary case: a UTC instant that is a different
        #    calendar date once converted to Asia/Baku (+04:00, no DST).
        case4_artifact_root = root / "case4" / "artifacts"
        case4_output_root = root / "case4" / "run-outcomes"
        boundary_trigger = make_trigger(
            external_occurrence_id="openclaw-occ-morning-boundary",
            scheduled_for="2026-09-08T21:30:00Z",
        )
        calls2: list[int] = []

        def succeed_boundary() -> None:
            calls2.append(1)
            board = case4_artifact_root / "social/research/daily/2026-09-09-editorial-board.md"
            board.parent.mkdir(parents=True, exist_ok=True)
            board.write_text("# Editorial board\n", encoding="utf-8")

        result = run_morning_workflow(
            boundary_trigger,
            invoke_provider=succeed_boundary,
            notifier=lambda _r: {"status": "NOT_REQUIRED"},
            artifact_root=case4_artifact_root,
            output_root=case4_output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "COMPLETED", result
        assert result.board_date == "2026-09-09", result  # UTC date would be 2026-09-08

        # 5. Provider-unreachable bounded retry delegated to #28: fails once,
        #    then succeeds -- MorningWorkflow does not reimplement retry.
        case5_artifact_root = root / "case5" / "artifacts"
        case5_output_root = root / "case5" / "run-outcomes"
        retry_trigger = make_trigger(external_occurrence_id="openclaw-occ-morning-retry")
        retry_calls: list[int] = []

        def flaky_then_succeeds() -> None:
            retry_calls.append(1)
            if len(retry_calls) < 2:
                raise ProviderUnreachableError("ENOTFOUND")
            board = case5_artifact_root / "social/research/daily/2026-09-08-editorial-board.md"
            board.parent.mkdir(parents=True, exist_ok=True)
            if not board.is_file():
                board.write_text("# Editorial board\n", encoding="utf-8")

        result = run_morning_workflow(
            retry_trigger,
            invoke_provider=flaky_then_succeeds,
            notifier=lambda _r: {"status": "NOT_REQUIRED"},
            artifact_root=case5_artifact_root,
            output_root=case5_output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "COMPLETED", result
        assert result.domain_outcome == "SUCCEEDED", result
        assert len(retry_calls) == 2

        # 6. Non-retryable #28 failure -> COMPLETED orchestration, FAILED
        #    domain outcome, scheduler-native (scheduler_status="error")
        #    notification deferral -- proving the scheduler-vs-domain rule.
        case6_artifact_root = root / "case6" / "artifacts"
        case6_output_root = root / "case6" / "run-outcomes"
        fail_trigger = make_trigger(external_occurrence_id="openclaw-occ-morning-fail")

        def always_fails() -> None:
            raise RuntimeError("EDITORIAL_PROVIDER_ERROR: not retryable")

        deferred_notifier_calls: list[dict[str, Any]] = []

        def deferred_notifier(persisted: dict[str, Any]) -> dict[str, Any]:
            deferred_notifier_calls.append(persisted)
            assert persisted["scheduler_status"] == "error"
            return {"status": "NOT_REQUIRED", "policy": "SCHEDULER_NATIVE_FAILURE_ALERT"}

        result = run_morning_workflow(
            fail_trigger,
            invoke_provider=always_fails,
            notifier=deferred_notifier,
            artifact_root=case6_artifact_root,
            output_root=case6_output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "COMPLETED", result
        assert result.domain_outcome == "FAILED", result
        assert result.notification_status == "NOT_REQUIRED", result
        assert len(deferred_notifier_calls) == 1

        # 7. Replay of the exact same trigger: zero additional provider
        #    calls, exact same run_id/result reused, notifier invoked again
        #    but #30's own idempotence (simulated here) still applies.
        replay_notifier_calls: list[dict[str, Any]] = []

        def replay_notifier(persisted: dict[str, Any]) -> dict[str, Any]:
            replay_notifier_calls.append(persisted)
            status = "SENT" if len(replay_notifier_calls) == 1 else "ALREADY_SENT"
            return {"status": status}

        actionable_trigger = make_trigger(external_occurrence_id="openclaw-occ-morning-actionable")

        # Directly persist a non-scheduler-native actionable result via a
        # fake run_editorial, proving MorningWorkflow's own notifier
        # composition (real #28 never itself emits a non-scheduler-native
        # FAILED result today -- see docs/deployment/59-scheduled-workflows-deployment.md).
        actionable_run_id = make_run_id(
            workflow_id="morning-editorial",
            occurrence_id=actionable_trigger["occurrence_id"],
        )

        def fake_run_editorial_actionable(**kwargs: Any) -> dict[str, Any]:
            existing = result_path(kwargs["output_root"], actionable_run_id)
            if existing.is_file():
                return json.loads(existing.read_text(encoding="utf-8"))
            built = assess_run(
                workflow_id="morning-editorial",
                occurrence_id=kwargs["occurrence_id"],
                scheduler_status="succeeded",
                domain_outcome="FAILED",
                reason_code="EDITORIAL_PROVIDER_ERROR",
                reason_text="Simulated non-scheduler-native failure for composition test.",
            )
            emit_result_once(kwargs["output_root"], built, artifact_root=kwargs["artifact_root"])
            return built

        first = run_morning_workflow(
            actionable_trigger,
            invoke_provider=exploding_provider,
            notifier=replay_notifier,
            run_editorial=fake_run_editorial_actionable,
            artifact_root=artifact_root,
            output_root=output_root,
            sleep=lambda _s: None,
        )
        assert first.application_execution == "COMPLETED", first
        assert first.notification_status == "SENT", first

        second = run_morning_workflow(
            actionable_trigger,
            invoke_provider=exploding_provider,
            notifier=replay_notifier,
            run_editorial=fake_run_editorial_actionable,
            artifact_root=artifact_root,
            output_root=output_root,
            sleep=lambda _s: None,
        )
        assert second.application_execution == "COMPLETED", second
        assert second.notification_status == "ALREADY_SENT", second
        assert len(replay_notifier_calls) == 2
        assert second.run_id == first.run_id

        # 8. Concurrent replay: two threads racing the same occurrence
        #    against the real #28 runtime must still invoke the provider
        #    exactly once and persist exactly one result.
        import threading

        case8_artifact_root = root / "case8" / "artifacts"
        case8_output_root = root / "case8" / "run-outcomes"
        concurrent_trigger = make_trigger(external_occurrence_id="openclaw-occ-morning-concurrent")
        concurrent_calls: list[int] = []
        call_lock = threading.Lock()

        def slow_provider() -> None:
            with call_lock:
                concurrent_calls.append(1)
            time.sleep(0.05)
            board = case8_artifact_root / "social/research/daily/2026-09-08-editorial-board.md"
            board.parent.mkdir(parents=True, exist_ok=True)
            if not board.is_file():
                board.write_text("# Editorial board\n", encoding="utf-8")

        concurrent_notifications: list[str] = []
        notify_lock = threading.Lock()

        def counting_notifier(_persisted: dict[str, Any]) -> dict[str, Any]:
            with notify_lock:
                concurrent_notifications.append("call")
            return {"status": "NOT_REQUIRED"}

        results: list[MorningWorkflowResult] = []
        results_lock = threading.Lock()

        def worker() -> None:
            r = run_morning_workflow(
                concurrent_trigger,
                invoke_provider=slow_provider,
                notifier=counting_notifier,
                artifact_root=case8_artifact_root,
                output_root=case8_output_root,
                sleep=lambda _s: None,
            )
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(concurrent_calls) == 1, concurrent_calls
        assert all(r.application_execution == "COMPLETED" for r in results)
        assert len({r.run_id for r in results}) == 1

        # 9. Missing persisted result -> RESULT_MISSING_OR_CORRUPT.
        missing_trigger = make_trigger(external_occurrence_id="openclaw-occ-morning-missing")

        def fake_run_editorial_missing(**kwargs: Any) -> dict[str, Any]:
            return assess_run(
                workflow_id="morning-editorial",
                occurrence_id=kwargs["occurrence_id"],
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                empty_success="NO_ACTION",
            )

        result = run_morning_workflow(
            missing_trigger,
            invoke_provider=exploding_provider,
            run_editorial=fake_run_editorial_missing,
            artifact_root=artifact_root,
            output_root=output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "RESULT_MISSING_OR_CORRUPT", result

        # 10. Malformed persisted result (not valid JSON) -> RESULT_MISSING_OR_CORRUPT.
        malformed_trigger = make_trigger(external_occurrence_id="openclaw-occ-morning-malformed")
        malformed_run_id = make_run_id(
            workflow_id="morning-editorial", occurrence_id=malformed_trigger["occurrence_id"]
        )

        def fake_run_editorial_malformed(**kwargs: Any) -> dict[str, Any]:
            path = result_path(kwargs["output_root"], malformed_run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
            return {"run_id": malformed_run_id}

        result = run_morning_workflow(
            malformed_trigger,
            invoke_provider=exploding_provider,
            run_editorial=fake_run_editorial_malformed,
            artifact_root=artifact_root,
            output_root=output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "RESULT_MISSING_OR_CORRUPT", result

        # 11. Persisted result identity mismatch -> RESULT_IDENTITY_MISMATCH.
        mismatch_trigger = make_trigger(external_occurrence_id="openclaw-occ-morning-mismatch")
        mismatch_run_id = make_run_id(
            workflow_id="morning-editorial", occurrence_id=mismatch_trigger["occurrence_id"]
        )

        def fake_run_editorial_mismatch(**kwargs: Any) -> dict[str, Any]:
            wrong = assess_run(
                workflow_id="morning-editorial",
                occurrence_id="occ_" + "0" * 24,
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                empty_success="NO_ACTION",
            )
            path = result_path(kwargs["output_root"], mismatch_run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(wrong), encoding="utf-8")
            return wrong

        result = run_morning_workflow(
            mismatch_trigger,
            invoke_provider=exploding_provider,
            run_editorial=fake_run_editorial_mismatch,
            artifact_root=artifact_root,
            output_root=output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "RESULT_IDENTITY_MISMATCH", result

        # 12. In-memory/persisted reconciliation mismatch -> RESULT_RECONCILIATION_REQUIRED.
        reconcile_trigger = make_trigger(external_occurrence_id="openclaw-occ-morning-reconcile")
        reconcile_run_id = make_run_id(
            workflow_id="morning-editorial", occurrence_id=reconcile_trigger["occurrence_id"]
        )

        def fake_run_editorial_reconcile(**kwargs: Any) -> dict[str, Any]:
            persisted = assess_run(
                workflow_id="morning-editorial",
                occurrence_id=kwargs["occurrence_id"],
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                empty_success="NO_ACTION",
            )
            emit_result_once(kwargs["output_root"], persisted, artifact_root=kwargs["artifact_root"])
            claimed = dict(persisted)
            claimed["domain_outcome"] = "FAILED"
            return claimed

        result = run_morning_workflow(
            reconcile_trigger,
            invoke_provider=exploding_provider,
            run_editorial=fake_run_editorial_reconcile,
            artifact_root=artifact_root,
            output_root=output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "RESULT_RECONCILIATION_REQUIRED", result
        assert result.reconciliation_required is True

        # 13. Notifier raising (unsafe/corrupt notification state) -> FAILED,
        #     without rerunning Morning or rewriting the #27 result. The
        #     raised exception's own text (here containing a fake
        #     secret-like marker) must never be echoed into reason_text.
        unsafe_trigger = make_trigger(external_occurrence_id="openclaw-occ-morning-unsafe")
        unsafe_calls: list[int] = []

        def succeed_unsafe() -> None:
            unsafe_calls.append(1)
            board = artifact_root / "social/research/daily/2026-09-08-editorial-board.md"
            board.parent.mkdir(parents=True, exist_ok=True)
            if not board.is_file():
                board.write_text("# Editorial board\n", encoding="utf-8")

        FAKE_NOTIFIER_SECRET = "FAKE-NOTIFIER-SECRET-should-never-be-echoed"

        def raising_notifier(_persisted: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError(f"transport auth failed token={FAKE_NOTIFIER_SECRET}")

        result = run_morning_workflow(
            unsafe_trigger,
            invoke_provider=succeed_unsafe,
            notifier=raising_notifier,
            artifact_root=artifact_root,
            output_root=output_root,
            sleep=lambda _s: None,
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "NOTIFICATION_STATE_UNSAFE", result
        assert result.domain_outcome == "SUCCEEDED", result
        assert result.result_file is not None, result
        assert FAKE_NOTIFIER_SECRET not in result.reason_text, result.reason_text
        assert result.context.get("error_type") == "RuntimeError", result

        # 14. Malformed notifier results must fail closed -- application
        #     FAILED, no rerun, no #27 rewrite, no second attempt; a
        #     legitimate #30 FAILED/UNKNOWN/ALREADY_* outcome must NOT be
        #     treated as malformed.
        malformed_cases = [None, "sent", {}, {"status": None}, {"status": ""}, {"status": "SUCCESS"}]
        for index, bad_outcome in enumerate(malformed_cases):
            case_artifact_root = root / f"case14-{index}" / "artifacts"
            case_output_root = root / f"case14-{index}" / "run-outcomes"
            trigger = make_trigger(external_occurrence_id=f"openclaw-occ-morning-malformed-notifier-{index}")

            def succeed_case(_ar=case_artifact_root) -> None:
                board = _ar / "social/research/daily/2026-09-08-editorial-board.md"
                board.parent.mkdir(parents=True, exist_ok=True)
                board.write_text("# Editorial board\n", encoding="utf-8")

            result = run_morning_workflow(
                trigger,
                invoke_provider=succeed_case,
                notifier=lambda _r, _bad=bad_outcome: _bad,
                artifact_root=case_artifact_root,
                output_root=case_output_root,
                sleep=lambda _s: None,
            )
            assert result.application_execution == "FAILED", (index, bad_outcome, result)
            assert result.reason_code == "NOTIFICATION_RESULT_INVALID", (index, result)
            assert result.notification_status is None, (index, result)
            assert result.domain_outcome == "SUCCEEDED", (index, result)

        legitimate_cases = [
            "FAILED", "UNKNOWN", "ALREADY_FAILED", "ALREADY_UNKNOWN",
            "ALREADY_PENDING", "NOT_REQUIRED", "SENT", "ALREADY_SENT",
        ]
        for index, status in enumerate(legitimate_cases):
            case_artifact_root = root / f"case15-{index}" / "artifacts"
            case_output_root = root / f"case15-{index}" / "run-outcomes"
            trigger = make_trigger(external_occurrence_id=f"openclaw-occ-morning-legit-notifier-{index}")

            def succeed_case(_ar=case_artifact_root) -> None:
                board = _ar / "social/research/daily/2026-09-08-editorial-board.md"
                board.parent.mkdir(parents=True, exist_ok=True)
                board.write_text("# Editorial board\n", encoding="utf-8")

            result = run_morning_workflow(
                trigger,
                invoke_provider=succeed_case,
                notifier=lambda _r, _status=status: {"status": _status},
                artifact_root=case_artifact_root,
                output_root=case_output_root,
                sleep=lambda _s: None,
            )
            assert result.application_execution == "COMPLETED", (index, status, result)
            assert result.notification_status == status, (index, result)

    print("MORNING_WORKFLOW_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_PUBLISH_CAPABILITY=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne MorningWorkflow application orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
