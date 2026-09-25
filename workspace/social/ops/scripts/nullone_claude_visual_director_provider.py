#!/usr/bin/env python3
"""Claude CLI infrastructure adapter for the NullOne Visual Director
(docs/contracts/visual-director-contract-v1.md).

Mirrors `nullone_claude_editorial_provider.py`'s established shape (same
`claude -p` invocation pattern, same timeout/reachability
classification) rather than inventing a new one, per the reviewed
provider-role-routing architecture
(docs/architecture/provider-role-routing.md): Visual Director is a
Claude-transport role reusing the exact same reviewed route Morning
already uses, not a new transport.

Tool allowlist is deliberately narrower than Morning's: `Read,Write`
only. The Visual Director role's capabilities are `visual-reasoning` and
`draft-artifacts` -- no `web-research` (ROLE_CAPABILITIES in
nullone_provider_router.py) -- because it must reason only over
already-verified signals and already-discovered source assets, never
broaden factual claims by going looking for new ones. No Bash, no MCP
tool, no Zernio/Telegram capability of any kind.
"""
from __future__ import annotations

import subprocess

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_editorial_runtime import (
    REACHABILITY_PATTERN,
    ProviderExecutionTimeoutError,
    ProviderUnreachableError,
)
from nullone_process_tree import run_tree_command

PROMPT_PATH = WORKSPACE / "social/ops/prompts/visual-director.md"

# Must equal ROLE_TIMEOUTS[ROLE_VISUAL_DIRECTOR] in nullone_provider_router.py.
VISUAL_DIRECTOR_CALL_TIMEOUT_SECONDS = 300

# Reviewed rollback default: preserved byte-for-byte with Morning's own
# `claude -p --model sonnet` default (issue #111: the model travels in
# the ProviderProfile; this is only the transport-local fallback).
CLAUDE_DEFAULT_MODEL = "sonnet"


def build_claude_command(*, prompt: str, model: str | None = None) -> list[str]:
    """Build the deterministic `claude -p` argv for one Visual Director
    decision. Pure function (no I/O, no subprocess) so offline tests can
    pin the exact argv shape."""

    resolved_model = (model or "").strip() or CLAUDE_DEFAULT_MODEL
    return [
        "claude",
        "-p",
        prompt,
        "--model",
        resolved_model,
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "Read,Write",
    ]


def default_invoke_provider(model: str | None = None) -> None:
    """Invoke the real Visual Director decision cycle via the Claude CLI.

    `model` arrives from the role router's ProviderProfile; None
    preserves the reviewed `sonnet` default.
    """

    resolved_model = (model or "").strip() or CLAUDE_DEFAULT_MODEL
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    try:
        cp = run_tree_command(
            build_claude_command(prompt=prompt, model=resolved_model),
            cwd=WORKSPACE,
            timeout=VISUAL_DIRECTOR_CALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise ProviderExecutionTimeoutError(
            "Claude invocation exceeded its execution deadline"
        ) from e
    except FileNotFoundError as e:
        raise BridgeError("claude binary not found") from e

    if cp.returncode != 0:
        combined = f"{cp.stdout}\n{cp.stderr}"

        if REACHABILITY_PATTERN.search(combined):
            raise ProviderUnreachableError(
                "Claude invocation failed: provider unreachable"
            )

        raise BridgeError(
            f"Claude invocation failed (exit={cp.returncode})"
        )


def self_test() -> int:
    argv = build_claude_command(prompt="probe-prompt", model=None)
    assert argv[0] == "claude"
    assert argv[argv.index("--model") + 1] == CLAUDE_DEFAULT_MODEL
    tools = argv[argv.index("--allowedTools") + 1]
    assert tools == "Read,Write", tools
    assert "WebSearch" not in tools and "WebFetch" not in tools and "Bash" not in tools

    pinned = build_claude_command(prompt="probe-prompt", model="sonnet-4-5")
    assert pinned[pinned.index("--model") + 1] == "sonnet-4-5"

    print("VISUAL_DIRECTOR_CLAUDE_PROVIDER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
