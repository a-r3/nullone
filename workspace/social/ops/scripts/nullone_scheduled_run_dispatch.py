#!/usr/bin/env python3
"""Shared production dependency wiring for #59/#79 scheduler-facing entrypoints.

Extracted (#59 remaining scope, section 16) so both the exact-trigger-file
CLI (`nullone-scheduled-run.py`) and the wake-up-only CLI
(`nullone-scheduled-wakeup.py`) invoke the exact same production wiring for:

    validated Morning SchedulerInvocation -> MorningWorkflow with the
    production Claude CLI provider and OpenClaw/Telegram notifier

    validated Analytics SchedulerInvocation -> AnalyticsWorkflow with the
    production AnalyticsProvider factory and OpenClaw/Telegram notifier

    validated Story SchedulerInvocation -> Story scheduled boundary with
    the production Haiku writer, deterministic final verifier,
    MCP-backed DraftConnector (live path still UNPROVEN, owned by #81),
    and OpenClaw/Telegram ReviewDelivery + notifier (#79)

Neither caller duplicates this wiring; this module knows the same and only
the same infrastructure adapters the CLIs always had:
`default_invoke_provider`, `build_production_analytics_provider`,
`HaikuStoryWriter`/`numeric_scope_verifier`/`NulloneDraftBridgeConnector`/
`TelegramReviewDeliveryAdapter`, and `OpenClawTelegramTransport`, all
injected -- never imported by the application workflow modules themselves.
"""
from __future__ import annotations

from typing import Any

from nullone_analytics_provider_factory import build_production_analytics_provider
from nullone_analytics_workflow import AnalyticsWorkflowResult, run_analytics_workflow
from nullone_claude_editorial_provider import default_invoke_provider
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
    """Validated Morning `nullone.scheduler-invocation.v1` -> production `MorningWorkflow`."""

    return run_morning_workflow(
        trigger,
        invoke_provider=default_invoke_provider,
        notifier=production_notifier,
    )


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
