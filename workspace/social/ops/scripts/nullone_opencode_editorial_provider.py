#!/usr/bin/env python3
"""OpenCode CLI infrastructure adapter for Morning Editorial.

Side-by-side with `nullone_claude_editorial_provider.py`, which is
preserved unchanged as the fallback/rollback transport. Selection
between the two happens only in
`nullone_editorial_provider_factory.py` (`NULLONE_EDITORIAL_PROVIDER`);
no workflow module imports this adapter directly.

Invocation contract, verified against the installed OpenCode 1.18.30
(`opencode run --help`, `opencode agent create --help`):

    opencode run <prompt> --agent nullone-editorial \\
        --model <provider/model> --format json --dir <workspace>

- One-shot isolated session: no `--continue`/`--session` is ever
  passed, so a run can never continue an unrelated old session.
- Explicit model in `provider/model` syntax (`-m`/`--model`).
- Machine-readable output requested via `--format json`.
- Narrow capability boundary: no `--auto` is ever passed. Only the
  checked-in `nullone-editorial` agent's explicit rules
  (`workspace/.opencode/agents/nullone-editorial.md`) run unattended:
  writes default-denied and allowed only on the four Morning
  artifact/state paths, secret-bearing reads denied, shell, task
  delegation, skills, and outside-worktree access denied.
- Timeout is enforced externally by NullOne via the subprocess
  deadline (`PROVIDER_CALL_TIMEOUT_SECONDS`), identical to the Claude
  adapter, even though OpenCode has its own internal behavior.
- `cwd` and `--dir` are the exact same workspace path. In production
  the scripts deploy under `/home/oem/.openclaw/workspace`, so
  `WORKSPACE` resolves there and the run happens there.
- stdout/stderr are captured; failure messages are fixed strings
  plus the exit code only. The raw child output is inspected solely
  for the existing reachability pattern and never logged, so no
  credential material can leak through this boundary.

Failure semantics are identical to the Claude adapter (see
`nullone_editorial_runtime.py`):

- whole-process wall-clock expiry -> `ProviderExecutionTimeoutError`
  (distinct, non-retryable);
- reachability-pattern match on a failed run ->
  `ProviderUnreachableError` (retryable per existing policy);
- missing `opencode` binary -> `BridgeError` (startup
  misconfiguration, fail closed, never classified as reachability);
- any other non-zero exit -> `BridgeError` (non-retryable).

Malformed provider output fails closed downstream exactly as before:
this adapter returns `None` and writes nothing itself; the runtime's
artifact checks plus handoff validation (`HANDOFF_INCOMPLETE` /
`PARTIAL_EDITORIAL_ARTIFACT_SET` / `HANDOFF_INVALID`) and the
`VERIFICATION: PASS` gate are unchanged.

Model mapping (transport requires an explicit value; reasoning-model
policy itself is unchanged):

- logical role: Morning/Draft reasoning model;
- transport: OpenCode;
- provider/model: `NULLONE_OPENCODE_MODEL` when set, else
  `DEFAULT_OPENCODE_MODEL` below (deployment may override without
  touching workflow code).

Current reviewed OpenCode editorial model is Muse Spark 1.3
(`opencode/muse-spark-1.3-contributor-free`): an explicit
operational/product decision (Sonnet is currently unavailable, and
that is not a blocker), not an accidental fallback. Future
per-role provider/model routing belongs to issue #111 and must not
require rewriting workflow/domain logic.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_editorial_runtime import (
    PROVIDER_CALL_TIMEOUT_SECONDS,
    REACHABILITY_PATTERN,
    ProviderExecutionTimeoutError,
    ProviderUnreachableError,
)

PROMPT_PATH = WORKSPACE / "social/ops/prompts/morning-editorial.md"

OPENCODE_AGENT_NAME = "nullone-editorial"

OPENCODE_MODEL_ENV_VAR = "NULLONE_OPENCODE_MODEL"

DEFAULT_OPENCODE_MODEL = "opencode/muse-spark-1.3-contributor-free"


def resolve_opencode_model(raw: str | None = None) -> str:
    """Return the explicit `provider/model` value for `opencode run -m`.

    `NULLONE_OPENCODE_MODEL` overrides the checked-in default so a
    deployment can pin its model without touching workflow code. A
    missing or blank value falls back to the default; the value is a
    model identifier only, never credential material.
    """

    candidate = raw if raw is not None else os.environ.get(OPENCODE_MODEL_ENV_VAR, "")
    cleaned = (candidate or "").strip()
    return cleaned if cleaned else DEFAULT_OPENCODE_MODEL


def build_opencode_command(
    *,
    prompt: str,
    workspace: Path | str,
    model: str | None = None,
) -> list[str]:
    """Build the deterministic `opencode run` argv for one editorial cycle.

    Pure function (no I/O, no subprocess): the exact argv shape is
    pinned here so offline tests can prove it byte-for-byte.
    """

    resolved_model = model if model is not None else resolve_opencode_model()
    workspace_text = str(workspace)
    return [
        "opencode",
        "run",
        prompt,
        "--agent",
        OPENCODE_AGENT_NAME,
        "--model",
        resolved_model,
        "--format",
        "json",
        "--dir",
        workspace_text,
    ]


def describe_invocation(
    *,
    workspace: Path | str | None = None,
    model: str | None = None,
) -> str:
    """One-line secret-free description of this transport for run logging."""

    return (
        "editorial-provider-transport=opencode "
        f"agent={OPENCODE_AGENT_NAME} "
        f"model={model if model is not None else resolve_opencode_model()} "
        f"dir={workspace if workspace is not None else WORKSPACE}"
    )


def default_invoke_provider(
    prompt: str | None = None,
    workspace: Path | str | None = None,
    timeout: float | int | None = None,
) -> None:
    """Invoke one real Morning Editorial planning cycle via OpenCode.

    Zero-argument compatible with `run_morning_editorial`'s
    `invoke_provider` contract: every parameter defaults to the
    production wiring (prompt file, `WORKSPACE`,
    `PROVIDER_CALL_TIMEOUT_SECONDS`).
    """

    resolved_prompt = (
        prompt if prompt is not None else PROMPT_PATH.read_text(encoding="utf-8")
    )
    resolved_workspace = Path(workspace) if workspace is not None else WORKSPACE
    resolved_timeout = (
        PROVIDER_CALL_TIMEOUT_SECONDS if timeout is None else timeout
    )
    cmd: Sequence[str] = build_opencode_command(
        prompt=resolved_prompt,
        workspace=resolved_workspace,
    )

    try:
        cp = subprocess.run(
            cmd,
            cwd=resolved_workspace,
            text=True,
            capture_output=True,
            timeout=resolved_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        # The whole `opencode run` agent process exceeded its outer
        # wall-clock deadline. Same contract as the Claude adapter:
        # not proof of unreachability, never auto-retried.
        raise ProviderExecutionTimeoutError(
            "OpenCode invocation exceeded its execution deadline"
        ) from e
    except FileNotFoundError as e:
        raise BridgeError("opencode binary not found") from e

    if cp.returncode != 0:
        combined = f"{cp.stdout}\n{cp.stderr}"

        if REACHABILITY_PATTERN.search(combined):
            raise ProviderUnreachableError(
                "OpenCode invocation failed: provider unreachable"
            )

        raise BridgeError(
            f"OpenCode invocation failed (exit={cp.returncode})"
        )


def self_test() -> int:
    argv = build_opencode_command(
        prompt="probe",
        workspace=Path("/tmp/nullone-opencode-self-test"),
        model="opencode/muse-spark-1.3-contributor-free",
    )
    assert argv == [
        "opencode",
        "run",
        "probe",
        "--agent",
        OPENCODE_AGENT_NAME,
        "--model",
        "opencode/muse-spark-1.3-contributor-free",
        "--format",
        "json",
        "--dir",
        "/tmp/nullone-opencode-self-test",
    ]
    assert "--auto" not in argv
    assert "--continue" not in argv
    assert "--session" not in argv
    assert resolve_opencode_model("  ") == DEFAULT_OPENCODE_MODEL

    print("OPENCODE_EDITORIAL_PROVIDER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="NullOne OpenCode editorial provider adapter"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
