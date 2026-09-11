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

Not exercised by most tests in this repository: most inject a fake
`invoke_provider` into `run_morning_editorial`/`run_morning_workflow`
instead. This default path IS the deployed production wiring -- it is
imported directly by `nullone_scheduled_run_dispatch.run_morning_trigger`
and confirmed executed by the real 2026-09-11 natural Morning Editorial
occurrence (run `run_28849dc4436e74d25ae99dbf`); it is not a dormant or
unused default.
"""
from __future__ import annotations

import subprocess

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_editorial_runtime import (
    PROVIDER_CALL_TIMEOUT_SECONDS,
    REACHABILITY_PATTERN,
    ProviderExecutionTimeoutError,
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
        # The whole `claude -p` agent process exceeded its outer
        # wall-clock deadline. This is NOT proof the provider/runtime
        # was unreachable -- proven live evidence (2026-09-11) shows
        # the child can still be actively succeeding at WebSearch/
        # WebFetch/Bash calls when it is killed. Keep this distinct
        # from ProviderUnreachableError so it is never auto-retried.
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
