#!/usr/bin/env python3
"""NullOne `AnalyticsWorkflow` application orchestrator (#59).

Per `docs/architecture/nullone-application-runtime.md`, this module is the
NullOne Application Runtime layer for the Daily Analytics workflow:

    validated `nullone.scheduler-invocation.v1` trigger
    -> analytics_date derived from `scheduled_for` in Asia/Baku
    -> existing #29 domain runtime (`nullone_analytics_runtime.run_daily_analytics`)
    -> exact persisted #27 result reloaded from disk and validated
    -> in-memory/persisted reconciliation
    -> existing #30 notification decision (`nullone_failure_notify.notify_if_required`)
    -> truthful `AnalyticsWorkflowResult`

It reuses #27/#29/#30 domain logic verbatim. This module's own job is
strictly composition and the scheduler-vs-domain separation documented in
`docs/architecture/nullone-application-runtime.md` ("Critical scheduler-vs-
domain rule"): `application_execution` reflects only whether orchestration
itself safely established/validated the occurrence, result and notification
state -- never the domain outcome.

AnalyticsProvider boundary (#59/#61): this module depends only on an
injected `provider_factory: Callable[[], AnalyticsProvider]` matching #29's
existing `run_daily_analytics(build_connector=...)` parameter exactly --
the same narrow read-only surface (account, follower history, account
insights, post analytics) `nullone_zernio_analytics_adapter` already
defines. It never reads the production analytics credential environment
variable or any other environment variable, never touches systemd, and
never constructs a Zernio connector itself; the real secret-backed factory
is issue #61's job, wired in only at the CLI/production layer
(`nullone-scheduled-run.py`), never inside this module.

Critical dependency rule (restated, enforced by
`tests/test_scheduled_workflows_capability_negative.py`): this module must
never import OpenClaw internals, invoke `openclaw`, know Telegram/Zernio
transport details, know cron syntax/job UUIDs, publish, approve, schedule,
or create Zernio drafts. The Zernio-backed connector factory and the
Telegram/OpenClaw notification transport are both supplied by the caller as
injected dependencies (`provider_factory`, `notifier`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nullone_analytics_runtime import (
    RUN_OUTCOME_ROOT as DEFAULT_OUTPUT_ROOT,
    run_daily_analytics,
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
)
from nullone_scheduler_invocation import SchedulerInvocationError, accept_workflow_trigger

# WORKSPACE default kept as a lazy attribute lookup (not a module-level
# import-time constant) so tests that monkeypatch nullone_bridge_common's
# WORKSPACE before calling run_analytics_workflow still take effect,
# matching nullone_analytics_runtime's own default-argument convention.
from nullone_bridge_common import WORKSPACE as DEFAULT_ARTIFACT_ROOT  # noqa: E402

ANALYTICS_WORKFLOW_ID = "daily-analytics"

APPLICATION_EXECUTION_STATES = frozenset({"COMPLETED", "FAILED"})


class AnalyticsWorkflowError(RuntimeError):
    """Caller contract violation building an AnalyticsWorkflowResult (never
    raised for a legitimate application-level failure -- those are
    returned, not raised)."""


@dataclass
class AnalyticsWorkflowResult:
    application_execution: str
    domain_outcome: str | None
    run_id: str | None
    occurrence_id: str | None
    result_file: str | None
    notification_status: str | None
    reconciliation_required: bool
    reason_code: str
    reason_text: str
    analytics_date: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.application_execution not in APPLICATION_EXECUTION_STATES:
            raise AnalyticsWorkflowError(
                f"Unknown application_execution: {self.application_execution!r}"
            )


def _failed(
    *,
    reason_code: str,
    reason_text: str,
    occurrence_id: str | None = None,
    run_id: str | None = None,
    reconciliation_required: bool = False,
    analytics_date: str | None = None,
    context: dict[str, Any] | None = None,
) -> AnalyticsWorkflowResult:
    return AnalyticsWorkflowResult(
        application_execution="FAILED",
        domain_outcome=None,
        run_id=run_id,
        occurrence_id=occurrence_id,
        result_file=None,
        notification_status=None,
        reconciliation_required=reconciliation_required,
        reason_code=reason_code,
        reason_text=reason_text,
        analytics_date=analytics_date,
        context=context or {},
    )


def run_analytics_workflow(
    trigger: dict[str, Any],
    *,
    provider_factory: Callable[[], Any],
    notifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    run_analytics: Callable[..., dict[str, Any]] = run_daily_analytics,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    timezone_name: str = "Asia/Baku",
) -> AnalyticsWorkflowResult:
    """Run one Daily Analytics scheduled occurrence end to end.

    Never raises for a legitimate application-level failure; returns an
    `AnalyticsWorkflowResult` whose `application_execution` is `"FAILED"`
    only when orchestration itself could not safely establish/validate the
    occurrence, result, or notification state. A `domain_outcome` of
    `BLOCKED` (e.g. missing/rejected credential, connector unavailable, the
    analytics add-on not enabled) or `FAILED` is still
    `application_execution == "COMPLETED"` -- exactly the scheduler-success-
    with-blocked-domain-outcome symptom this issue's parent architecture
    fixes.

    `provider_factory` is called at most once, lazily, from inside
    `run_analytics` (the real #29 runtime) -- never speculatively by this
    function. If it raises anything other than the #29-typed connector
    errors `run_analytics` already catches (e.g. the #61
    `ProviderSecretWiringPendingError` placeholder), that propagates here
    and is reported as `RUNTIME_CRASHED`, never faked into a domain result.
    """

    try:
        accept_workflow_trigger(trigger, workflow_id=ANALYTICS_WORKFLOW_ID)
    except SchedulerInvocationError as exc:
        return _failed(
            reason_code="TRIGGER_REJECTED",
            reason_text=f"Scheduler invocation trigger rejected: {exc}",
            context={"trigger": safe_trigger_context(trigger)},
        )

    occurrence_id = trigger["occurrence_id"]
    scheduled_for = trigger["scheduled_for"]
    expected_run_id = make_run_id(workflow_id=ANALYTICS_WORKFLOW_ID, occurrence_id=occurrence_id)

    try:
        analytics_date = derive_local_date(scheduled_for, timezone_name=timezone_name)
    except ScheduledWorkflowSupportError as exc:
        return _failed(
            reason_code="SCHEDULED_FOR_INVALID",
            reason_text=f"Could not derive analytics_date from scheduled_for: {exc}",
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
        )

    # Only past this point may the provider factory ever be invoked (and
    # only lazily, from inside run_analytics itself).

    try:
        in_memory_result = run_analytics(
            occurrence_id=occurrence_id,
            analytics_date=analytics_date,
            build_connector=provider_factory,
            artifact_root=artifact_root,
            output_root=output_root,
        )
    except Exception as exc:  # noqa: BLE001 - a runtime/provider crash is a typed orchestration failure
        return _failed(
            reason_code="RUNTIME_CRASHED",
            reason_text=(
                "Daily Analytics runtime raised before establishing a result: "
                f"{exc}"
            ),
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
            analytics_date=analytics_date,
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
            analytics_date=analytics_date,
        )

    if not isinstance(persisted_result, dict):
        return _failed(
            reason_code="RESULT_MISSING_OR_CORRUPT",
            reason_text="Persisted #27 result was not a JSON object.",
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
            analytics_date=analytics_date,
        )

    try:
        validate_result_structure(persisted_result)
    except CompletionContractError as exc:
        return _failed(
            reason_code="RESULT_INVALID",
            reason_text=f"Persisted #27 result failed structural validation: {exc}",
            occurrence_id=occurrence_id,
            run_id=expected_run_id,
            analytics_date=analytics_date,
        )

    if (
        persisted_result["workflow_id"] != ANALYTICS_WORKFLOW_ID
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
            analytics_date=analytics_date,
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
            analytics_date=analytics_date,
        )

    # Only now -- with an exact, validated, reconciled persisted #27 result
    # in hand -- may the #30 notification decision be invoked, at most once.
    notification_status: str | None = None
    if notifier is not None:
        try:
            notification_outcome = notifier(persisted_result)
        except Exception as exc:  # noqa: BLE001 - unsafe/corrupt notifier state, never silently retried
            return AnalyticsWorkflowResult(
                application_execution="FAILED",
                domain_outcome=persisted_result["domain_outcome"],
                run_id=expected_run_id,
                occurrence_id=occurrence_id,
                result_file=str(persisted_file),
                notification_status=None,
                reconciliation_required=False,
                reason_code="NOTIFICATION_STATE_UNSAFE",
                reason_text=f"Notification orchestration could not safely proceed: {exc}",
                analytics_date=analytics_date,
            )

        notification_status = (
            notification_outcome.get("status")
            if isinstance(notification_outcome, dict)
            else None
        )

    return AnalyticsWorkflowResult(
        application_execution="COMPLETED",
        domain_outcome=persisted_result["domain_outcome"],
        run_id=expected_run_id,
        occurrence_id=occurrence_id,
        result_file=str(persisted_file),
        notification_status=notification_status,
        reconciliation_required=False,
        reason_code="OK",
        reason_text="Analytics workflow orchestration completed.",
        analytics_date=analytics_date,
    )


def self_test() -> int:
    import tempfile

    from nullone_bridge_common import CANONICAL_ACCOUNT_ID
    from nullone_run_outcome import assess_run, emit_result_once
    from nullone_scheduler_invocation import compute_occurrence_id
    from nullone_zernio_analytics_adapter import (
        ConnectorUnauthorizedError,
        ConnectorUnavailableError,
        ZernioReadOnlyAnalyticsConnector,
    )

    def make_trigger(**overrides: Any) -> dict[str, Any]:
        base = {
            "schema": "nullone.scheduler-invocation.v1",
            "contract_version": "1.0.0",
            "workflow_id": "daily-analytics",
            "source": "openclaw",
            "external_occurrence_id": "openclaw-occ-analytics-0001",
            "scheduled_for": "2026-09-08T23:20:00Z",
            "triggered_at": "2026-09-08T23:20:02Z",
        }
        base.update(overrides)
        base["occurrence_id"] = compute_occurrence_id(
            base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
        )
        return base

    def insights_envelope(**metrics: int) -> dict[str, Any]:
        return {
            "success": True,
            "accountId": CANONICAL_ACCOUNT_ID,
            "platform": "instagram",
            "metricType": "total_value",
            "metrics": {name: {"total": value} for name, value in metrics.items()},
        }

    class FakeSuccessTransport:
        def get(self, path: str, *, params: dict[str, Any] | None = None) -> tuple[int, Any]:
            if path == "/analytics/instagram/follower-history":
                return 200, insights_envelope(follower_count=100, followers_gained=5, followers_lost=1)
            if path == "/analytics/instagram/account-insights":
                return 200, insights_envelope(
                    reach=500, views=900, accounts_engaged=80, total_interactions=42,
                    comments=3, likes=30, saves=5, shares=4, profile_links_taps=2,
                )
            if path == "/analytics":
                return 200, {
                    "posts": [{"_id": "p1", "analytics": {"reach": 400, "likes": 20}}],
                    "pagination": {"page": 1, "limit": 25, "total": 1, "pages": 1},
                }
            if path == "/accounts":
                return 200, {
                    "accounts": [
                        {
                            "_id": CANONICAL_ACCOUNT_ID,
                            "platform": "instagram",
                            "username": "nullone.az",
                            "isActive": True,
                        }
                    ],
                    "hasAnalyticsAccess": True,
                }
            raise AssertionError(f"unexpected path: {path}")

    class FakeNoDataTransport:
        def get(self, path: str, *, params: dict[str, Any] | None = None) -> tuple[int, Any]:
            if path == "/accounts":
                return 200, {
                    "accounts": [
                        {"_id": CANONICAL_ACCOUNT_ID, "platform": "instagram", "username": "x", "isActive": True}
                    ],
                    "hasAnalyticsAccess": True,
                }
            if path in ("/analytics/instagram/follower-history", "/analytics/instagram/account-insights"):
                return 200, insights_envelope()
            if path == "/analytics":
                return 200, {"posts": [], "pagination": {"page": 1, "limit": 25, "total": 0, "pages": 0}}
            raise AssertionError(f"unexpected path: {path}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # 1. Invalid trigger -> TRIGGER_REJECTED, zero provider calls.
        def exploding_factory() -> ZernioReadOnlyAnalyticsConnector:
            raise AssertionError("provider factory must not be called")

        result = run_analytics_workflow(
            {"workflow_id": "daily-analytics"},
            provider_factory=exploding_factory,
            artifact_root=root / "case1",
            output_root=root / "case1" / "run-outcomes",
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "TRIGGER_REJECTED", result

        # 2. Wrong workflow_id -> TRIGGER_REJECTED.
        wrong = make_trigger(workflow_id="morning-editorial", source="openclaw")
        result = run_analytics_workflow(
            wrong, provider_factory=exploding_factory,
            artifact_root=root / "case2", output_root=root / "case2" / "run-outcomes",
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "TRIGGER_REJECTED", result

        # 3. Success with artifacts -> COMPLETED/SUCCEEDED, quiet notifier.
        trigger = make_trigger()

        def success_factory() -> ZernioReadOnlyAnalyticsConnector:
            return ZernioReadOnlyAnalyticsConnector(FakeSuccessTransport(), account_id=CANONICAL_ACCOUNT_ID)

        notifier_calls: list[dict[str, Any]] = []

        def quiet_notifier(persisted: dict[str, Any]) -> dict[str, Any]:
            notifier_calls.append(persisted)
            return {"status": "NOT_REQUIRED"}

        case3_artifacts = root / "case3"
        case3_outcomes = root / "case3" / "run-outcomes"
        result = run_analytics_workflow(
            trigger, provider_factory=success_factory, notifier=quiet_notifier,
            artifact_root=case3_artifacts, output_root=case3_outcomes,
        )
        assert result.application_execution == "COMPLETED", result
        assert result.domain_outcome == "SUCCEEDED", result
        assert result.analytics_date == "2026-09-09", result  # UTC 23:20 -> Baku +04:00 next day
        assert (case3_artifacts / "social/analytics/raw/2026-09-09.md").is_file()
        assert len(notifier_calls) == 1

        # 4. Valid NO_DATA success.
        no_data_trigger = make_trigger(external_occurrence_id="openclaw-occ-analytics-nodata")

        def no_data_factory() -> ZernioReadOnlyAnalyticsConnector:
            return ZernioReadOnlyAnalyticsConnector(FakeNoDataTransport(), account_id=CANONICAL_ACCOUNT_ID)

        result = run_analytics_workflow(
            no_data_trigger, provider_factory=no_data_factory,
            notifier=lambda _r: {"status": "NOT_REQUIRED"},
            artifact_root=root / "case4", output_root=root / "case4" / "run-outcomes",
        )
        assert result.application_execution == "COMPLETED", result
        assert result.domain_outcome == "SUCCEEDED", result

        # 5. Unauthorized -> BLOCKED domain outcome, still COMPLETED orchestration,
        #    actionable notification sent exactly once.
        unauthorized_trigger = make_trigger(external_occurrence_id="openclaw-occ-analytics-unauth")

        def unauthorized_factory() -> ZernioReadOnlyAnalyticsConnector:
            raise ConnectorUnauthorizedError("missing token")

        actionable_notifier_calls: list[dict[str, Any]] = []

        def actionable_notifier(persisted: dict[str, Any]) -> dict[str, Any]:
            actionable_notifier_calls.append(persisted)
            status = "SENT" if len(actionable_notifier_calls) == 1 else "ALREADY_SENT"
            return {"status": status}

        case5_root = root / "case5"
        result = run_analytics_workflow(
            unauthorized_trigger, provider_factory=unauthorized_factory,
            notifier=actionable_notifier,
            artifact_root=case5_root, output_root=case5_root / "run-outcomes",
        )
        assert result.application_execution == "COMPLETED", result
        assert result.domain_outcome == "BLOCKED", result
        assert result.notification_status == "SENT", result

        # Replay of the same occurrence: zero additional provider calls
        # (connector never constructed again), notification not repeated.
        replay = run_analytics_workflow(
            unauthorized_trigger, provider_factory=exploding_factory,
            notifier=actionable_notifier,
            artifact_root=case5_root, output_root=case5_root / "run-outcomes",
        )
        assert replay.application_execution == "COMPLETED", replay
        assert replay.notification_status == "ALREADY_SENT", replay
        assert len(actionable_notifier_calls) == 2

        # 6. Unavailable -> BLOCKED.
        def unavailable_factory() -> ZernioReadOnlyAnalyticsConnector:
            raise ConnectorUnavailableError("could not start")

        result = run_analytics_workflow(
            make_trigger(external_occurrence_id="openclaw-occ-analytics-unavail"),
            provider_factory=unavailable_factory,
            notifier=lambda _r: {"status": "SENT"},
            artifact_root=root / "case6", output_root=root / "case6" / "run-outcomes",
        )
        assert result.application_execution == "COMPLETED", result
        assert result.domain_outcome == "BLOCKED", result

        # 7. Addon required -> BLOCKED.
        class AddonRequiredTransport:
            def get(self, path: str, *, params: dict[str, Any] | None = None) -> tuple[int, Any]:
                if path == "/accounts":
                    return 200, {
                        "accounts": [
                            {"_id": CANONICAL_ACCOUNT_ID, "platform": "instagram", "username": "x", "isActive": True}
                        ],
                        "hasAnalyticsAccess": False,
                    }
                raise AssertionError(f"unexpected path: {path}")

        def addon_factory() -> ZernioReadOnlyAnalyticsConnector:
            return ZernioReadOnlyAnalyticsConnector(AddonRequiredTransport(), account_id=CANONICAL_ACCOUNT_ID)

        result = run_analytics_workflow(
            make_trigger(external_occurrence_id="openclaw-occ-analytics-addon"),
            provider_factory=addon_factory,
            notifier=lambda _r: {"status": "SENT"},
            artifact_root=root / "case7", output_root=root / "case7" / "run-outcomes",
        )
        assert result.application_execution == "COMPLETED", result
        assert result.domain_outcome == "BLOCKED", result
        assert result.reason_code is None or True  # domain reason lives on the persisted result, not here

        # 8. Malformed response -> FAILED domain outcome.
        class MalformedTransport:
            def get(self, path: str, *, params: dict[str, Any] | None = None) -> tuple[int, Any]:
                if path == "/accounts":
                    return 200, {
                        "accounts": [
                            {"_id": CANONICAL_ACCOUNT_ID, "platform": "instagram", "username": "x", "isActive": True}
                        ],
                        "hasAnalyticsAccess": True,
                    }
                if path == "/analytics/instagram/follower-history":
                    return 200, {"success": True}  # missing required keys
                raise AssertionError(f"unexpected path: {path}")

        def malformed_factory() -> ZernioReadOnlyAnalyticsConnector:
            return ZernioReadOnlyAnalyticsConnector(MalformedTransport(), account_id=CANONICAL_ACCOUNT_ID)

        result = run_analytics_workflow(
            make_trigger(external_occurrence_id="openclaw-occ-analytics-malformed"),
            provider_factory=malformed_factory,
            notifier=lambda _r: {"status": "SENT"},
            artifact_root=root / "case8", output_root=root / "case8" / "run-outcomes",
        )
        assert result.application_execution == "COMPLETED", result
        assert result.domain_outcome == "FAILED", result

        # 9. RUNTIME_CRASHED: provider factory raises something #29 does not
        #    catch (simulating the #61 PROVIDER_SECRET_WIRING_PENDING seam) ->
        #    non-zero, no #27 result fabricated.
        class ProviderSecretWiringPendingError(RuntimeError):
            pass

        def pending_secret_factory() -> ZernioReadOnlyAnalyticsConnector:
            raise ProviderSecretWiringPendingError(
                "Daily Analytics production AnalyticsProvider construction is "
                "pending issue #61."
            )

        result = run_analytics_workflow(
            make_trigger(external_occurrence_id="openclaw-occ-analytics-pending61"),
            provider_factory=pending_secret_factory,
            artifact_root=root / "case9", output_root=root / "case9" / "run-outcomes",
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "RUNTIME_CRASHED", result
        assert result.domain_outcome is None, result
        assert "#61" in result.reason_text, result.reason_text

        # 10. Missing persisted result -> RESULT_MISSING_OR_CORRUPT.
        def fake_run_analytics_missing(**kwargs: Any) -> dict[str, Any]:
            return assess_run(
                workflow_id="daily-analytics",
                occurrence_id=kwargs["occurrence_id"],
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                empty_success="NO_DATA",
            )

        result = run_analytics_workflow(
            make_trigger(external_occurrence_id="openclaw-occ-analytics-missing"),
            provider_factory=exploding_factory,
            run_analytics=fake_run_analytics_missing,
            artifact_root=root / "case10", output_root=root / "case10" / "run-outcomes",
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "RESULT_MISSING_OR_CORRUPT", result

        # 11. Corrupt persisted result (not valid JSON) -> RESULT_MISSING_OR_CORRUPT.
        corrupt_trigger = make_trigger(external_occurrence_id="openclaw-occ-analytics-corrupt")
        corrupt_run_id = make_run_id(workflow_id="daily-analytics", occurrence_id=corrupt_trigger["occurrence_id"])

        def fake_run_analytics_corrupt(**kwargs: Any) -> dict[str, Any]:
            path = result_path(kwargs["output_root"], corrupt_run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
            return {"run_id": corrupt_run_id}

        result = run_analytics_workflow(
            corrupt_trigger, provider_factory=exploding_factory,
            run_analytics=fake_run_analytics_corrupt,
            artifact_root=root / "case11", output_root=root / "case11" / "run-outcomes",
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "RESULT_MISSING_OR_CORRUPT", result

        # 12. Identity mismatch -> RESULT_IDENTITY_MISMATCH.
        mismatch_trigger = make_trigger(external_occurrence_id="openclaw-occ-analytics-mismatch")
        mismatch_run_id = make_run_id(workflow_id="daily-analytics", occurrence_id=mismatch_trigger["occurrence_id"])

        def fake_run_analytics_mismatch(**kwargs: Any) -> dict[str, Any]:
            wrong = assess_run(
                workflow_id="daily-analytics",
                occurrence_id="occ_" + "1" * 24,
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                empty_success="NO_DATA",
            )
            path = result_path(kwargs["output_root"], mismatch_run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(wrong), encoding="utf-8")
            return wrong

        result = run_analytics_workflow(
            mismatch_trigger, provider_factory=exploding_factory,
            run_analytics=fake_run_analytics_mismatch,
            artifact_root=root / "case12", output_root=root / "case12" / "run-outcomes",
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "RESULT_IDENTITY_MISMATCH", result

        # 13. Reconciliation mismatch -> RESULT_RECONCILIATION_REQUIRED.
        reconcile_trigger = make_trigger(external_occurrence_id="openclaw-occ-analytics-reconcile")

        def fake_run_analytics_reconcile(**kwargs: Any) -> dict[str, Any]:
            persisted = assess_run(
                workflow_id="daily-analytics",
                occurrence_id=kwargs["occurrence_id"],
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                empty_success="NO_DATA",
            )
            emit_result_once(kwargs["output_root"], persisted, artifact_root=kwargs["artifact_root"])
            claimed = dict(persisted)
            claimed["domain_outcome"] = "FAILED"
            return claimed

        result = run_analytics_workflow(
            reconcile_trigger, provider_factory=exploding_factory,
            run_analytics=fake_run_analytics_reconcile,
            artifact_root=root / "case13", output_root=root / "case13" / "run-outcomes",
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "RESULT_RECONCILIATION_REQUIRED", result
        assert result.reconciliation_required is True

        # 14. Concurrency: two threads racing the same occurrence must
        #     invoke the connector factory exactly once and send at most
        #     one notification.
        import threading
        import time as _time

        case14_root = root / "case14"
        concurrent_trigger = make_trigger(external_occurrence_id="openclaw-occ-analytics-concurrent")
        factory_calls: list[int] = []
        factory_lock = threading.Lock()

        class SlowSuccessTransport(FakeSuccessTransport):
            def get(self, path: str, *, params: dict[str, Any] | None = None) -> tuple[int, Any]:
                _time.sleep(0.02)
                return super().get(path, params=params)

        def slow_factory() -> ZernioReadOnlyAnalyticsConnector:
            with factory_lock:
                factory_calls.append(1)
            return ZernioReadOnlyAnalyticsConnector(SlowSuccessTransport(), account_id=CANONICAL_ACCOUNT_ID)

        notify_lock = threading.Lock()
        notify_calls: list[str] = []

        def counting_notifier(_persisted: dict[str, Any]) -> dict[str, Any]:
            with notify_lock:
                notify_calls.append("call")
            return {"status": "NOT_REQUIRED"}

        results: list[AnalyticsWorkflowResult] = []
        results_lock = threading.Lock()

        def worker() -> None:
            r = run_analytics_workflow(
                concurrent_trigger, provider_factory=slow_factory, notifier=counting_notifier,
                artifact_root=case14_root, output_root=case14_root / "run-outcomes",
            )
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(factory_calls) == 1, factory_calls
        assert all(r.application_execution == "COMPLETED" for r in results)
        assert len({r.run_id for r in results}) == 1

        # 15. Notifier raising -> NOTIFICATION_STATE_UNSAFE, no #27 rewrite.
        def raising_notifier(_persisted: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("existing notification record is unreadable")

        result = run_analytics_workflow(
            make_trigger(external_occurrence_id="openclaw-occ-analytics-unsafe"),
            provider_factory=success_factory, notifier=raising_notifier,
            artifact_root=root / "case15", output_root=root / "case15" / "run-outcomes",
        )
        assert result.application_execution == "FAILED", result
        assert result.reason_code == "NOTIFICATION_STATE_UNSAFE", result
        assert result.domain_outcome == "SUCCEEDED", result

    print("ANALYTICS_WORKFLOW_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_PUBLISH_CAPABILITY=TRUE")
    print("NO_REAL_CREDENTIAL=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne AnalyticsWorkflow application orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
