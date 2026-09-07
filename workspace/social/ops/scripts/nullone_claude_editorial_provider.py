#!/usr/bin/env python3
"""Claude CLI infrastructure adapter for Morning Editorial (#28/#59).

Extracted verbatim from `nullone-morning-editorial-run.py`'s previous
in-file `_default_invoke_provider` so the legacy CLI wrapper and the new
NullOne Application Runtime entrypoint (`nullone-scheduled-run.py`) share
exactly one production Claude invocation implementation -- same model,
timeout, tool allowlist, and provider-failure classification. Neither
`nullone_morning_workflow.py` (the application layer) nor any test imports
this module; only the two CLI/runner layers do, per
`docs/architecture/nullone-application-runtime.md`'s layering rule that
provider-specific invocation details stop at the infrastructure adapter
boundary.

Not exercised by any test in this repository: tests inject a fake
`invoke_provider` into `run_morning_editorial`/`run_morning_workflow`
instead. Production wiring of this default path has not been deployed.
"""
from __future__ import annotations

import subprocess

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_editorial_runtime import (
    PROVIDER_CALL_TIMEOUT_SECONDS,
    REACHABILITY_PATTERN,
    ProviderUnreachableError,
)

PROMPT_PATH = WORKSPACE / "social/ops/prompts/morning-editorial.md"


def default_invoke_provider() -> None:
    """Invoke the real Morning Editorial planning cycle via the Claude CLI."""

    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    try:
        cp = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--model",
                "sonnet",
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                "Read,Write,WebSearch,WebFetch",
            ],
            cwd=WORKSPACE,
            text=True,
            capture_output=True,
            timeout=PROVIDER_CALL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ProviderUnreachableError(
            "Claude invocation timed out"
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
