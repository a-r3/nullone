#!/usr/bin/env python3
"""Shared OpenCode one-shot transport for role wrappers (issue #112).

Used by the Draft Factory, Breaking Radar, and Weekly Strategy
role wrappers. It owns ONLY the transport mechanics shared by all
three roles:

- explicit `provider/model` resolution (`NULLONE_OPENCODE_MODEL`
  override, else the reviewed Muse Spark default);
- deterministic `opencode run` argv construction (isolated session,
  explicit agent, `--format json`, exact workspace `--dir`, never
  `--auto`);
- subprocess execution with an externally enforced timeout and
  fixed-string failure mapping.

Capability boundaries are NOT shared: each role pins its own agent
(`workspace/.opencode/agents/nullone-<role>.md`), prompt, and
timeout in its wrapper. No provider fallback lives here: any
non-zero OpenCode run surfaces as a `BridgeError` subclass and fails
closed under the calling workflow's own semantics. Per-role
provider/model routing belongs to issue #111.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_editorial_runtime import REACHABILITY_PATTERN

OPENCODE_MODEL_ENV_VAR = "NULLONE_OPENCODE_MODEL"

DEFAULT_OPENCODE_MODEL = "opencode/muse-spark-1.3-contributor-free"


class RoleExecutionTimeoutError(BridgeError):
    """The whole OpenCode role process exceeded its outer deadline."""


class RoleUnreachableError(BridgeError):
    """The OpenCode role run failed with a reachability signature."""


def resolve_role_model(raw: str | None = None) -> str:
    """Return the explicit `provider/model` value for a role run."""

    candidate = raw if raw is not None else os.environ.get(OPENCODE_MODEL_ENV_VAR, "")
    cleaned = (candidate or "").strip()
    return cleaned if cleaned else DEFAULT_OPENCODE_MODEL


def build_opencode_command(
    *,
    prompt: str,
    workspace: Path | str,
    agent: str,
    model: str | None = None,
) -> list[str]:
    """Build the deterministic `opencode run` argv for one role cycle.

    Pure function (no I/O, no subprocess) so offline tests can pin the
    exact argv shape per role.
    """

    resolved_model = model if model is not None else resolve_role_model()
    workspace_text = str(workspace)
    if not prompt.strip() or not agent.strip() or not workspace_text.strip():
        raise BridgeError("OpenCode role command misconfigured: blank prompt/agent/workspace")
    return [
        "opencode",
        "run",
        prompt,
        "--agent",
        agent,
        "--model",
        resolved_model,
        "--format",
        "json",
        "--dir",
        workspace_text,
    ]


def describe_cycle(*, role: str, agent: str, model: str | None = None) -> str:
    """One-line secret-free description of a role transport for logging."""

    return (
        f"role-provider-transport=opencode role={role} agent={agent} "
        f"model={model if model is not None else resolve_role_model()}"
    )


def run_opencode_cycle(
    cmd: Sequence[str],
    *,
    cwd: Path,
    timeout: float | int,
    role: str,
) -> None:
    """Execute one OpenCode role cycle, failing closed on any failure.

    `role` names the workflow for fixed-string errors only. The model
    response body is intentionally NOT parsed here: roles whose
    contract needs structured output parse through their own
    deterministic authority; file/side-effect roles succeed when the
    process exits zero.
    """

    try:
        cp = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RoleExecutionTimeoutError(
            f"OpenCode {role} run exceeded its execution deadline"
        ) from e
    except FileNotFoundError as e:
        raise BridgeError("opencode binary not found") from e

    if cp.returncode != 0:
        combined = f"{cp.stdout}\n{cp.stderr}"
        if REACHABILITY_PATTERN.search(combined):
            raise RoleUnreachableError(
                f"OpenCode {role} run failed: provider unreachable"
            )
        raise BridgeError(
            f"OpenCode {role} run failed (exit={cp.returncode})"
        )


def self_test() -> int:
    argv = build_opencode_command(
        prompt="probe",
        workspace=Path("/tmp/nullone-role-self-test"),
        agent="nullone-probe-role",
        model="opencode/muse-spark-1.3-contributor-free",
    )
    assert argv == [
        "opencode",
        "run",
        "probe",
        "--agent",
        "nullone-probe-role",
        "--model",
        "opencode/muse-spark-1.3-contributor-free",
        "--format",
        "json",
        "--dir",
        "/tmp/nullone-role-self-test",
    ]
    assert "--auto" not in argv
    assert "--continue" not in argv
    assert "--session" not in argv
    assert resolve_role_model("  ") == DEFAULT_OPENCODE_MODEL
    assert (
        describe_cycle(role="probe", agent="nullone-probe-role")
        == "role-provider-transport=opencode role=probe "
        "agent=nullone-probe-role model=opencode/muse-spark-1.3-contributor-free"
    )

    print("OPENCODE_ROLE_TRANSPORT_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="NullOne shared OpenCode role transport"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
