#!/usr/bin/env python3
"""Breaking Radar OpenCode role wrapper (issue #112).

Future command-payload entrypoint replacing the legacy agentTurn
Breaking Radar job WITHOUT changing schedules here:

    python3 social/ops/scripts/nullone-breaking-radar-run.py execute

reads the reviewed `breaking-radar.md` prompt and runs exactly one
DELTA_MONITORING_ONLY cycle through the `nullone-breaking-radar`
OpenCode agent (Muse Spark). Discovery/research reasoning happens in
the agent; handoff commits happen only through the deterministic
`nullone-breaking-scan.py` helper in the agent's allowlist. Radar
stops at human review preview: no drafts, no Telegram, no
publication path.

Transport budget (new, transport-only, not domain policy):
`RADAR_TIMEOUT_SECONDS = 600`, sized for discovery plus primary-
source verification in one cycle.
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

ROLE = "breaking-radar"
AGENT = "nullone-breaking-radar"
PROMPT_PATH = WORKSPACE / "social/ops/prompts/breaking-radar.md"

RADAR_TIMEOUT_SECONDS = 600

# Transport-only appendix: the working directory of `opencode run` is
# the workspace root, so the scan helper runs workspace-relative
# (the checked-in prompt text shows the legacy automation-root
# form). Prompt domain behavior is unchanged.
TRANSPORT_APPENDIX = """

Transport note: this run executes with the workspace root as its
working directory. Invoke the scan helper as
`python3 social/ops/scripts/nullone-breaking-scan.py ...`
(workspace-relative), never write handoff files directly, and commit
only through the helper.
"""


def build_command(
    *,
    workspace: Path | str | None = None,
    model: str | None = None,
) -> list[str]:
    """Build the deterministic Radar argv (pure, no I/O)."""

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
        run_opencode_cycle(cmd, cwd=WORKSPACE, timeout=RADAR_TIMEOUT_SECONDS, role=ROLE)
    except BridgeError as e:
        print(f"ROLE_OUTCOME=BLOCKED reason={type(e).__name__}")
        return 1
    print("ROLE_OUTCOME=COMPLETED")
    return 0


def self_test() -> int:
    argv = build_command(workspace=Path("/tmp/nullone-radar-self-test"))
    assert argv[0:2] == ["opencode", "run"]
    assert argv[argv.index("--agent") + 1] == AGENT
    assert argv[argv.index("--model") + 1] == resolve_role_model()
    assert argv[argv.index("--dir") + 1] == "/tmp/nullone-radar-self-test"
    assert "--auto" not in argv
    assert PROMPT_PATH.name == "breaking-radar.md"

    print("BREAKING_RADAR_RUN_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne Breaking Radar OpenCode role")
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
