#!/usr/bin/env python3
"""NullOne scheduler-facing application entrypoint (#59).

    nullone-scheduled-run.py morning --trigger-file <path>
    nullone-scheduled-run.py analytics --trigger-file <path>

Consumes an already-normalized/validated `nullone.scheduler-invocation.v1`
trigger (from a JSON file -- the OpenClaw edge, or any other future
scheduler adapter, is responsible for producing it; see
`nullone_openclaw_scheduler_adapter.py`) and invokes the corresponding
NullOne Application Runtime workflow. Suitable for eventual #37 OpenClaw
`--command` job activation (see
`docs/deployment/59-scheduled-workflows-deployment.md`); NOT wired into any
live job by this change, and this script never shells out to `openclaw`,
never mutates a scheduler job, and never calls Zernio itself -- all of that
stays behind the injected provider/notifier boundaries.

Critical exit-code contract (see
`docs/architecture/nullone-application-runtime.md`'s "Critical scheduler-
vs-domain rule" and `docs/deployment/59-scheduled-workflows-deployment.md`):

- Exit 0: a valid occurrence was fully orchestrated and a valid
  authoritative #27 result was established -- regardless of whether the
  domain outcome itself is SUCCEEDED, BLOCKED, FAILED, or actionable
  UNKNOWN.
- Exit 1: `application_execution == "FAILED"` -- orchestration itself could
  not safely establish/validate the occurrence, result, or notification
  state (invalid trigger, runtime crash before a valid result, missing/
  corrupt/mismatched persisted result, unreconciled in-memory/persisted
  disagreement, or an unsafe/corrupt notification state).

This deliberately does NOT use the legacy CLI convention
(`domain_outcome != SUCCEEDED -> exit 1`) that
`nullone-morning-editorial-run.py`/`nullone-daily-analytics-run.py` still
use -- those wrappers are unchanged by #59 (see their own docstrings/tests)
and are not the new application orchestration contract.

Daily Analytics production boundary (#61): the production
    `AnalyticsProvider` factory (`nullone_analytics_provider_factory
    .build_production_analytics_provider`) reads the credential only
    through the reviewed secret boundary
    (`nullone_secret_provider.EnvironmentSecretProvider`), which owns the
    sole environment-variable mapping -- see that module's docstring.
    Running `analytics` against a real trigger with #61 implemented and no
    credential configured yields `domain_outcome=BLOCKED`,
    `reason_code=ZERNIO_ANALYTICS_UNAUTHORIZED`, `application_execution=
    COMPLETED`, and hence scheduler-level exit 0 -- the missing-secret
    condition is a graceful blocked domain outcome, never a crash. An
    unexpected provider/factory crash (not one of #29's typed connector
    errors) is still reported as `RUNTIME_CRASHED`, and the exception's
    class name alone (`type(exc).__name__`) is echoed, never its message
    text, since a real credential/provider failure message must never be
    assumed safe to interpolate into operator-facing output.
    """
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from nullone_analytics_workflow import AnalyticsWorkflowResult, run_analytics_workflow
from nullone_bridge_common import BridgeError
from nullone_morning_workflow import MorningWorkflowResult, run_morning_workflow
from nullone_scheduled_run_dispatch import run_analytics_trigger, run_morning_trigger


def _load_trigger(path: str) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()

    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise BridgeError(f"Could not read trigger file: {resolved}: {exc}") from exc

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"Invalid trigger JSON: {resolved}: {exc}") from exc

    if not isinstance(value, dict):
        raise BridgeError(f"Trigger file must contain a JSON object: {resolved}")

    return value


def _report(result: MorningWorkflowResult | AnalyticsWorkflowResult) -> int:
    print(f"APPLICATION_EXECUTION={result.application_execution}")
    print(f"DOMAIN_OUTCOME={result.domain_outcome}")
    print(f"RUN_ID={result.run_id}")
    print(f"OCCURRENCE_ID={result.occurrence_id}")
    print(f"RESULT_FILE={result.result_file}")
    print(f"NOTIFICATION_STATUS={result.notification_status}")
    print(f"RECONCILIATION_REQUIRED={result.reconciliation_required}")
    print(f"REASON_CODE={result.reason_code}")
    print(f"REASON_TEXT={result.reason_text}")
    return 0 if result.application_execution == "COMPLETED" else 1


def morning(trigger_file: str) -> int:
    trigger = _load_trigger(trigger_file)
    result = run_morning_trigger(trigger)
    return _report(result)


def analytics(trigger_file: str) -> int:
    trigger = _load_trigger(trigger_file)
    result = run_analytics_trigger(trigger)
    return _report(result)


def self_test() -> int:
    """Offline smoke test proving CLI exit-code semantics only.

    Exercises `run_morning_workflow`/`run_analytics_workflow` directly with
    fakes -- never the real production `default_invoke_provider` /
    `build_production_analytics_provider` / `OpenClawTelegramTransport`, and
    never through `_load_trigger`'s filesystem path (that is a thin,
    already-covered JSON-loading helper). This also carries the mandatory
    #59 scheduler-vs-domain regression: a valid persisted `BLOCKED` domain
    result must still report `application_execution=COMPLETED` (exit 0),
    while a corrupt/missing persisted result must report `FAILED` (exit
    nonzero).
    """
    import tempfile

    from nullone_run_outcome import assess_run, emit_result_once, make_run_id
    from nullone_scheduler_invocation import compute_occurrence_id

    def make_trigger(workflow_id: str, **overrides: Any) -> dict[str, Any]:
        base = {
            "schema": "nullone.scheduler-invocation.v1",
            "contract_version": "1.0.0",
            "workflow_id": workflow_id,
            "source": "openclaw",
            "external_occurrence_id": f"openclaw-occ-{workflow_id}-cli-self-test",
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

        # Mandatory regression: Daily Analytics BLOCKED domain outcome with
        # scheduler_status=succeeded still reports application_execution
        # COMPLETED / exit 0, and the #30 domain notifier is evaluated.
        blocked_trigger = make_trigger("daily-analytics", external_occurrence_id="cli-blocked")

        def unauthorized_factory() -> Any:
            from nullone_zernio_analytics_adapter import ConnectorUnauthorizedError

            raise ConnectorUnauthorizedError("missing token")

        notifier_calls: list[dict[str, Any]] = []

        def notifier(persisted: dict[str, Any]) -> dict[str, Any]:
            notifier_calls.append(persisted)
            return {"status": "SENT"}

        blocked_result = run_analytics_workflow(
            blocked_trigger,
            provider_factory=unauthorized_factory,
            notifier=notifier,
            artifact_root=root / "blocked",
            output_root=root / "blocked" / "run-outcomes",
        )
        assert blocked_result.application_execution == "COMPLETED", blocked_result
        assert blocked_result.domain_outcome == "BLOCKED", blocked_result
        assert _report(blocked_result) == 0
        assert len(notifier_calls) == 1

        # Corrupt/missing exact #27 result -> application non-zero.
        corrupt_trigger = make_trigger("daily-analytics", external_occurrence_id="cli-corrupt")

        def fake_run_analytics_missing(**kwargs: Any) -> dict[str, Any]:
            return assess_run(
                workflow_id="daily-analytics",
                occurrence_id=kwargs["occurrence_id"],
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                empty_success="NO_DATA",
            )

        corrupt_result = run_analytics_workflow(
            corrupt_trigger,
            provider_factory=unauthorized_factory,
            run_analytics=fake_run_analytics_missing,
            artifact_root=root / "corrupt",
            output_root=root / "corrupt" / "run-outcomes",
        )
        assert corrupt_result.application_execution == "FAILED", corrupt_result
        assert _report(corrupt_result) != 0

        # Morning: valid success -> exit 0, notifier evaluated (quiet).
        morning_trigger = make_trigger("morning-editorial", external_occurrence_id="cli-morning-ok")
        morning_artifact_root = root / "morning"

        def succeed_provider() -> None:
            board = morning_artifact_root / "social/research/daily/2026-09-08-editorial-board.md"
            board.parent.mkdir(parents=True, exist_ok=True)
            board.write_text("# Editorial board\n", encoding="utf-8")

        morning_result = run_morning_workflow(
            morning_trigger,
            invoke_provider=succeed_provider,
            notifier=lambda _r: {"status": "NOT_REQUIRED"},
            artifact_root=morning_artifact_root,
            output_root=morning_artifact_root / "run-outcomes",
            sleep=lambda _s: None,
        )
        assert morning_result.application_execution == "COMPLETED", morning_result
        assert _report(morning_result) == 0

        # Invalid trigger -> application non-zero, regardless of workflow.
        rejected_result = run_morning_workflow(
            {"workflow_id": "morning-editorial"},
            invoke_provider=lambda: (_ for _ in ()).throw(AssertionError("must not be called")),
        )
        assert rejected_result.application_execution == "FAILED", rejected_result
        assert _report(rejected_result) != 0

    print("SCHEDULED_RUN_CLI_SELF_TEST=PASS")
    print("NO_OPENCLAW_EXECUTION=TRUE")
    print("NO_ZERNIO_CALL=TRUE")
    print("NO_CLAUDE_CALL=TRUE")
    print("NO_TELEGRAM_SEND=TRUE")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne scheduled workflow application entrypoint")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")

    m = sub.add_parser("morning")
    m.add_argument("--trigger-file", required=True)

    a = sub.add_parser("analytics")
    a.add_argument("--trigger-file", required=True)

    args = parser.parse_args()

    try:
        if args.command == "self-test":
            return self_test()

        if args.command == "morning":
            return morning(args.trigger_file)

        if args.command == "analytics":
            return analytics(args.trigger_file)

        raise BridgeError("Unknown command")

    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
