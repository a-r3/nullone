#!/usr/bin/env python3
"""Shared production dependency wiring for #59/#79 scheduler-facing entrypoints.

Extracted (#59 remaining scope, section 16) so both the exact-trigger-file
CLI (`nullone-scheduled-run.py`) and the wake-up-only CLI
(`nullone-scheduled-wakeup.py`) invoke the exact same production wiring for:

    validated Morning SchedulerInvocation -> MorningWorkflow with the
    production editorial provider (OpenCode primary / Claude fallback,
    selected by `NULLONE_EDITORIAL_PROVIDER`) and OpenClaw/Telegram
    notifier

    validated Analytics SchedulerInvocation -> AnalyticsWorkflow with the
    production AnalyticsProvider factory and OpenClaw/Telegram notifier

    validated Story SchedulerInvocation -> Story scheduled boundary with
    the production Haiku writer, deterministic final verifier,
    MCP-backed DraftConnector (live path still UNPROVEN, owned by #81),
    and OpenClaw/Telegram ReviewDelivery + notifier (#79)

Neither caller duplicates this wiring; this module knows the same and only
the same infrastructure adapters the CLIs always had:
the editorial provider factory, `build_production_analytics_provider`,
`HaikuStoryWriter`/`numeric_scope_verifier`/`NulloneDraftBridgeConnector`/
`TelegramReviewDeliveryAdapter`, and `OpenClawTelegramTransport`, all
injected -- never imported by the application workflow modules themselves.
"""
from __future__ import annotations

from typing import Any

from nullone_analytics_provider_factory import build_production_analytics_provider
from nullone_analytics_workflow import AnalyticsWorkflowResult, run_analytics_workflow
from nullone_editorial_provider_factory import (
    UnknownEditorialProviderError,
    get_editorial_provider,
)
from nullone_failure_notify import OpenClawTelegramTransport, notify_if_required
from nullone_morning_workflow import MorningWorkflowResult, run_morning_workflow
from nullone_story_pipeline import (
    HaikuStoryWriter,
    NulloneDraftBridgeConnector,
    numeric_scope_verifier,
)
from nullone_story_scheduled_workflow import (
    StoryScheduledResult,
    run_story_trigger as run_story_scheduled,
)
from nullone_telegram_review_delivery_adapter import TelegramReviewDeliveryAdapter


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
    """Validated Morning `nullone.scheduler-invocation.v1` -> production `MorningWorkflow`.

    The editorial transport is resolved from the provider factory, so a
    misconfigured `NULLONE_EDITORIAL_PROVIDER` fails closed here as a
    `FAILED` orchestration result before any workflow side effect. The
    resolved provider name is stamped into the result context so run
    metadata always shows which transport ran, without secrets.
    """

    try:
        provider_name, invoke_provider = get_editorial_provider()
    except UnknownEditorialProviderError:
        occurrence_id = trigger.get("occurrence_id") if isinstance(trigger, dict) else None
        return MorningWorkflowResult(
            application_execution="FAILED",
            domain_outcome=None,
            run_id=None,
            occurrence_id=occurrence_id,
            result_file=None,
            notification_status=None,
            reconciliation_required=False,
            reason_code="EDITORIAL_PROVIDER_MISCONFIGURED",
            reason_text="Editorial provider selection is misconfigured.",
            board_date=None,
            context={"editorial_provider": "unknown"},
        )

    result = run_morning_workflow(
        trigger,
        invoke_provider=invoke_provider,
        notifier=production_notifier,
    )
    result.context["editorial_provider"] = provider_name
    return result


def run_analytics_trigger(trigger: dict[str, Any]) -> AnalyticsWorkflowResult:
    """Validated Analytics `nullone.scheduler-invocation.v1` -> production `AnalyticsWorkflow`.

    The production `AnalyticsProvider` factory (#61) reads the credential
    only through the reviewed secret boundary and, with no credential
    configured, yields a graceful domain `BLOCKED` result (never a crash)
    -- see `nullone_analytics_provider_factory.py` and
    `nullone_secret_provider.py`. This is the expected production
    behavior and is not faked around here.
    """

    return run_analytics_workflow(
        trigger,
        provider_factory=build_production_analytics_provider,
        notifier=production_notifier,
    )


def run_story_trigger(trigger: dict[str, Any]) -> StoryScheduledResult:
    """Validated Story `nullone.scheduler-invocation.v1` -> production Story boundary.

    Wires the reviewed production dependencies: Haiku writer, deterministic
    numeric-scope final verifier, the existing MCP-backed DraftConnector
    (whose scheduled-session live path remains UNPROVEN and stays owned by
    #81 -- this wiring neither proves nor replaces it), and the shared
    Telegram ReviewDelivery adapter. Story ends at review preview; no
    publication capability is wired anywhere on this path.
    """

    return run_story_scheduled(
        trigger,
        writer=HaikuStoryWriter(),
        verifier=numeric_scope_verifier,
        draft_connector=NulloneDraftBridgeConnector(),
        review_delivery=TelegramReviewDeliveryAdapter(),
        notifier=production_notifier,
    )
