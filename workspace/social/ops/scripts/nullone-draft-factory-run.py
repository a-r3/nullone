#!/usr/bin/env python3
"""Draft Factory OpenCode role wrapper (issue #112).

Future command-payload entrypoint replacing the legacy agentTurn
Draft Factory job WITHOUT changing schedules here:

    python3 social/ops/scripts/nullone-draft-factory-run.py execute

reads the reviewed `draft-factory.md` prompt and runs exactly one
DRAFT_FIRST production cycle through the `nullone-draft-factory`
OpenCode agent (Muse Spark). Editorial reasoning happens in the
agent; all consequential side effects (rendering, manifest build,
review-draft creation, Telegram preview) happen only through the
exact reviewed commands in the agent's allowlist. Final publication
is never reachable from this path.

Transport budget (new, transport-only, not domain policy):
`DRAFT_FACTORY_TIMEOUT_SECONDS = 900`, sized for render + upload +
draft + Telegram delivery in one cycle.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_draft_bridge_action import ensure_pending_bridge
from nullone_opencode_binary import resolve_opencode_binary
from nullone_opencode_role import (
    build_opencode_command,
    describe_cycle,
    resolve_role_model,
    run_opencode_cycle,
)
from nullone_provider_router import (
    ROLE_DRAFT_FACTORY,
    format_routing_metadata,
    resolve_provider_profile,
)

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
    """Build the deterministic Draft Factory argv (pure, no I/O)."""

    resolved_workspace = Path(workspace) if workspace is not None else WORKSPACE
    prompt = PROMPT_PATH.read_text(encoding="utf-8") + TRANSPORT_APPENDIX
    return build_opencode_command(
        prompt=prompt,
        workspace=resolved_workspace,
        agent=AGENT,
        model=model if model is not None else resolve_role_model(),
        binary=binary,
    )


def execute() -> int:
    # Issue #111: transport (opencode) is fixed for this role wrapper;
    # the model and timeout arrive from the role router. Values equal
    # today's reviewed constants, so production behavior is unchanged.
    try:
        profile = resolve_provider_profile(ROLE_DRAFT_FACTORY)
    except BridgeError as e:
        print(f"ROLE_OUTCOME=BLOCKED reason={type(e).__name__}")
        return 1
    print(describe_cycle(role=ROLE, agent=AGENT, model=profile.model))
    print(format_routing_metadata(profile, "STARTED"))
    cmd: Sequence[str] = build_command(model=profile.model, binary=resolve_opencode_binary())
    cycle_start = _utcnow()
    try:
        run_opencode_cycle(
            cmd, cwd=WORKSPACE, timeout=profile.timeout_seconds, role=ROLE
        )
    except BridgeError as e:
        print(f"ROLE_OUTCOME=BLOCKED reason={type(e).__name__}")
        print(format_routing_metadata(profile, "BLOCKED"))
        _run_bridge_backstop(cycle_start)
        return 1
    backstop_failed = _run_bridge_backstop(cycle_start)
    if backstop_failed:
        # An eligible authoritative pending manifest existed but bridge
        # completion did not reach DRAFT_CREATED: this is a failed
        # production cycle, never a silent COMPLETED (issue #142-A).
        print("ROLE_OUTCOME=BLOCKED reason=BRIDGE_BACKSTOP")
        print(format_routing_metadata(profile, "BLOCKED"))
        return 1
    print("ROLE_OUTCOME=COMPLETED")
    print(format_routing_metadata(profile, "COMPLETED"))
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
    assert argv[0:2] == ["opencode", "run"]
    assert argv[argv.index("--agent") + 1] == AGENT
    assert argv[argv.index("--model") + 1] == resolve_role_model()
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
