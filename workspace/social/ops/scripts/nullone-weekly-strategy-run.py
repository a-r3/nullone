#!/usr/bin/env python3
"""Weekly Strategy OpenCode role wrapper (issue #112).

Future command-payload entrypoint replacing the legacy agentTurn
Weekly Strategy job WITHOUT changing schedules here:

    python3 social/ops/scripts/nullone-weekly-strategy-run.py execute

reads the reviewed `weekly-strategy.md` prompt and runs exactly one
strategy review through the `nullone-weekly-strategy` OpenCode agent
(Muse Spark). The agent has no shell: analysis plus the two reviewed
report writes, nothing else. Strategy-only, never publication.

Transport budget (new, transport-only, not domain policy):
`WEEKLY_TIMEOUT_SECONDS = 600`, sized for a 7-day review plus
report writing in one cycle.
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

ROLE = "weekly-strategy"
AGENT = "nullone-weekly-strategy"
PROMPT_PATH = WORKSPACE / "social/ops/prompts/weekly-strategy.md"

WEEKLY_TIMEOUT_SECONDS = 600

TRANSPORT_APPENDIX = """

Transport note: this run executes with the workspace root as its
working directory and has no shell. Write only the reviewed strategy
report path and MEMORY.md; leave any other strategy-hypothesis
update for a reviewed follow-up.
"""


def build_command(
    *,
    workspace: Path | str | None = None,
    model: str | None = None,
) -> list[str]:
    """Build the deterministic Weekly argv (pure, no I/O)."""

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
        run_opencode_cycle(cmd, cwd=WORKSPACE, timeout=WEEKLY_TIMEOUT_SECONDS, role=ROLE)
    except BridgeError as e:
        print(f"ROLE_OUTCOME=BLOCKED reason={type(e).__name__}")
        return 1
    print("ROLE_OUTCOME=COMPLETED")
    return 0


def self_test() -> int:
    argv = build_command(workspace=Path("/tmp/nullone-weekly-self-test"))
    assert argv[0:2] == ["opencode", "run"]
    assert argv[argv.index("--agent") + 1] == AGENT
    assert argv[argv.index("--model") + 1] == resolve_role_model()
    assert argv[argv.index("--dir") + 1] == "/tmp/nullone-weekly-self-test"
    assert "--auto" not in argv
    assert PROMPT_PATH.name == "weekly-strategy.md"

    print("WEEKLY_STRATEGY_RUN_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne Weekly Strategy OpenCode role")
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
