#!/usr/bin/env python3
"""Shared production dependency wiring for #59's scheduler-facing entrypoints.

Extracted (#59 remaining scope, section 16) so both the exact-trigger-file
CLI (`nullone-scheduled-run.py`) and the wake-up-only CLI
(`nullone-scheduled-wakeup.py`) invoke the exact same production wiring for:

    validated Morning SchedulerInvocation -> MorningWorkflow with the
    production Claude CLI provider and OpenClaw/Telegram notifier

    validated Analytics SchedulerInvocation -> AnalyticsWorkflow with the
    production AnalyticsProvider factory and OpenClaw/Telegram notifier

Neither caller duplicates this wiring; this module knows the same and only
the same infrastructure adapters `nullone-scheduled-run.py` always has:
`default_invoke_provider`, `build_production_analytics_provider`, and
`OpenClawTelegramTransport`, all injected -- never imported by
`nullone_morning_workflow.py`/`nullone_analytics_workflow.py` themselves.
"""
from __future__ import annotations

from typing import Any

from nullone_analytics_provider_factory import build_production_analytics_provider
from nullone_analytics_workflow import AnalyticsWorkflowResult, run_analytics_workflow
from nullone_claude_editorial_provider import default_invoke_provider
from nullone_failure_notify import OpenClawTelegramTransport, notify_if_required
from nullone_morning_workflow import MorningWorkflowResult, run_morning_workflow


def production_notifier(result: dict[str, Any]) -> dict[str, Any]:
    """The one place either CLI knows OpenClaw/Telegram is the transport.

    `scheduler_native_failure_owned=False` because this is only ever
    reached after `run_morning_workflow`/`run_analytics_workflow` have
    already established a valid, reconciled persisted #27 result -- see
    `nullone-scheduled-run.py`'s original docstring for the full
    ownership-routing rationale this narrow override participates in
    (unchanged by this extraction).
    """

    return notify_if_required(
        result,
        transport=OpenClawTelegramTransport(),
        scheduler_native_failure_owned=False,
    )


def run_morning_trigger(trigger: dict[str, Any]) -> MorningWorkflowResult:
    """Validated Morning `nullone.scheduler-invocation.v1` -> production `MorningWorkflow`."""

    return run_morning_workflow(
        trigger,
        invoke_provider=default_invoke_provider,
        notifier=production_notifier,
    )


def run_analytics_trigger(trigger: dict[str, Any]) -> AnalyticsWorkflowResult:
    """Validated Analytics `nullone.scheduler-invocation.v1` -> production `AnalyticsWorkflow`.

    Until #61, the production `AnalyticsProvider` factory is a fail-closed
    placeholder that never reads `ZERNIO_ANALYTICS_API_TOKEN` -- see
    `nullone_analytics_provider_factory.py`. This is expected and is not
    faked around here.
    """

    return run_analytics_workflow(
        trigger,
        provider_factory=build_production_analytics_provider,
        notifier=production_notifier,
    )
