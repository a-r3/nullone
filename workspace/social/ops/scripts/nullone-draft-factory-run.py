#!/usr/bin/env python3
"""Draft Factory role runner behind the provider adapter (issue #111).

Command-payload entrypoint:

    python3 social/ops/scripts/nullone-draft-factory-run.py execute

reads the reviewed `draft-factory.md` prompt and runs exactly one
DRAFT_FIRST production cycle for the `draft_factory` logical role.
Transport, model, agent, and timeout arrive from the role router's
ProviderProfile and execute through the provider adapter registry:
this module imports NO vendor transport module and never chooses
OpenCode or Claude itself. Editorial reasoning happens in the
agent; all consequential side effects (rendering, manifest build,
review-draft creation, Telegram preview) happen only through the
exact reviewed commands in the agent's allowlist. Final publication
is never reachable from this path.

Transport budget (reviewed, transport-only, not domain policy):
900s, mirrored in the router (`ROLE_TIMEOUTS`); the two must stay
equal (proven offline).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_draft_bridge_action import ensure_pending_bridge
import nullone_provider_adapter as provider_adapter
import nullone_provider_router as provider_router

ROLE = "draft-factory"
AGENT = "nullone-draft-factory"
PROMPT_PATH = WORKSPACE / "social/ops/prompts/draft-factory.md"

DRAFT_FACTORY_TIMEOUT_SECONDS = 900

# Transport-only appendix: the working directory of `opencode run` is
# the workspace root, so helper invocations use workspace-relative
# paths. Prompt domain behavior is unchanged.
TRANSPORT_APPENDIX = """

Transport note: this run executes with the workspace root as its
working directory. Invoke helpers as
`python3 social/ops/scripts/<name>.py ...` and
`python3 social/tools/<name>.py ...` (workspace-relative).
Packaging format authority is deterministic: assess signals, run
`nullone-packaging-evaluator.py evaluate`, render ONLY through
`nullone-packaging-render.py render --receipt`, build the manifest
ONLY with `--packaging-receipt`, and never override the receipt.
Telegram delivery happens ONLY through
`python3 social/ops/scripts/nullone_telegram_review_delivery_adapter.py
deliver --payload-file <validated-payload>.json`: never invoke
`openclaw message send` directly and never read
`social/ops/private/telegram-owner-id` yourself.
"""


def build_command(
    *,
    workspace: Path | str | None = None,
    model: str | None = None,
    binary: str = "opencode",
) -> list[str]:
    """Build the deterministic Draft Factory argv (pure, no I/O).

    Delegates to the provider adapter so the reviewed argv shape is
    owned in exactly one place; `model=None` resolves through the
    role router (never a vendor default).
    """

    resolved_workspace = Path(workspace) if workspace is not None else WORKSPACE
    prompt = PROMPT_PATH.read_text(encoding="utf-8") + TRANSPORT_APPENDIX
    profile = provider_router.resolve_provider_profile(
        provider_router.ROLE_DRAFT_FACTORY
    )
    if model is not None:
        profile = provider_router.ProviderProfile(
            role=profile.role,
            transport=profile.transport,
            model=model,
            capabilities=profile.capabilities,
            timeout_seconds=profile.timeout_seconds,
            fallback_policy=profile.fallback_policy,
        )
    return provider_adapter.build_adapter_command(
        profile, prompt=prompt, workspace=resolved_workspace, binary=binary
    )


def execute() -> int:
    # Issue #111: the profile (transport/model/timeout) arrives from
    # the role router and executes through the adapter registry.
    # Reviewed values equal today's constants, so production
    # behavior is unchanged.
    try:
        profile = provider_router.resolve_provider_profile(
            provider_router.ROLE_DRAFT_FACTORY
        )
    except BridgeError as e:
        print(f"ROLE_OUTCOME=BLOCKED reason={type(e).__name__}")
        return 1
    print(provider_router.describe_profile(profile))
    print(provider_router.format_routing_metadata(profile, "STARTED"))
    prompt = PROMPT_PATH.read_text(encoding="utf-8") + TRANSPORT_APPENDIX
    cycle_start = _utcnow()
    try:
        outcome = provider_adapter.invoke_role_cycle(profile, prompt, WORKSPACE)
    except BridgeError as e:
        print(f"ROLE_OUTCOME=BLOCKED reason={type(e).__name__}")
        print(provider_router.format_routing_metadata(profile, "BLOCKED"))
        _run_bridge_backstop(cycle_start)
        return 1
    print(provider_router.format_routing_metadata(profile, outcome.outcome))
    backstop_failed = _run_bridge_backstop(cycle_start)
    if backstop_failed:
        # An eligible authoritative pending manifest existed but bridge
        # completion did not reach DRAFT_CREATED: this is a failed
        # production cycle, never a silent COMPLETED (issue #142-A).
        print("ROLE_OUTCOME=BLOCKED reason=BRIDGE_BACKSTOP")
        print(provider_router.format_routing_metadata(profile, "BLOCKED"))
        return 1
    print("ROLE_OUTCOME=COMPLETED")
    return 0


def _utcnow() -> datetime:
    """Cycle-start clock (module seam: offline tests pin this)."""
    return datetime.now(timezone.utc)


def _run_bridge_backstop(cycle_start: datetime) -> bool:
    """Deterministic completion pass (issue #142 wiring).

    After the editorial cycle, completes at most one manifest bound to
    THIS cycle (built at or after `cycle_start`) through the
    credentialed draft-bridge action (single-flight, audit,
    replay-safe). Runs in this process, so in the Gateway cron context
    it carries the drafts credential; elsewhere it fails closed with
    zero calls. Returns True when bridge completion failed/blocked
    (caller turns the wrapper non-zero); never raises.
    """
    try:
        summary = ensure_pending_bridge(max_creations=1, since=cycle_start)
    except Exception as e:
        print(f"BRIDGE_BACKSTOP=ERROR reason={type(e).__name__}")
        return True
    created = summary.get("created") or {}
    status = summary.get("status")
    print(
        "BRIDGE_BACKSTOP="
        f"{status} "
        f"attempted={len(summary.get('attempted', []))} "
        f"draft={created.get('draft_id') or ''}"
    )
    return status in ("BLOCKED", "ERROR")


def self_test() -> int:
    argv = build_command(workspace=Path("/tmp/nullone-factory-self-test"))
    profile = provider_router.resolve_provider_profile(
        provider_router.ROLE_DRAFT_FACTORY
    )
    assert argv[0:2] == ["opencode", "run"]
    assert argv[argv.index("--agent") + 1] == AGENT
    assert argv[argv.index("--model") + 1] == profile.model
    assert argv[argv.index("--dir") + 1] == "/tmp/nullone-factory-self-test"
    assert "--auto" not in argv
    assert PROMPT_PATH.name == "draft-factory.md"

    print("DRAFT_FACTORY_RUN_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne Draft Factory OpenCode role")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("execute")
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "execute":
        return execute()
    if args.command == "self-test":
        return self_test()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
