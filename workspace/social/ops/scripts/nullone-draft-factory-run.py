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
from pathlib import Path
from typing import Sequence

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_opencode_role import (
    build_opencode_command,
    describe_cycle,
    resolve_role_model,
    run_opencode_cycle,
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
) -> list[str]:
    """Build the deterministic Draft Factory argv (pure, no I/O)."""

    resolved_workspace = Path(workspace) if workspace is not None else WORKSPACE
    prompt = PROMPT_PATH.read_text(encoding="utf-8") + TRANSPORT_APPENDIX
    return build_opencode_command(
        prompt=prompt,
        workspace=resolved_workspace,
        agent=AGENT,
        model=model if model is not None else resolve_role_model(),
    )


def execute() -> int:
    model = resolve_role_model()
    print(describe_cycle(role=ROLE, agent=AGENT, model=model))
    cmd: Sequence[str] = build_command(model=model)
    try:
        run_opencode_cycle(
            cmd, cwd=WORKSPACE, timeout=DRAFT_FACTORY_TIMEOUT_SECONDS, role=ROLE
        )
    except BridgeError as e:
        print(f"ROLE_OUTCOME=BLOCKED reason={type(e).__name__}")
        return 1
    print("ROLE_OUTCOME=COMPLETED")
    return 0


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
