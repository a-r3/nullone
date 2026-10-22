#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from nullone_bridge_common import BridgeError
from nullone_draft_stage_error import (
    DRAFT_REASON_CLAUDE_AUTH_EXPIRED,
    DRAFT_REASON_CLAUDE_AUTH_FAILED,
    DRAFT_REASON_CLAUDE_EXIT_NONZERO,
    mark_stage,
)


def _extract_structured(stdout: str) -> dict[str, Any]:
    try:
        obj = json.loads(stdout)
    except Exception as e:
        raise BridgeError(
            "Claude returned non-JSON output"
        ) from e

    if not isinstance(obj, dict):
        raise BridgeError(
            "Claude JSON output is not an object"
        )

    for key in ("structured_output", "structuredOutput"):
        value = obj.get(key)
        if isinstance(value, dict):
            return value

    result = obj.get("result")

    if isinstance(result, dict):
        for key in ("structured_output", "structuredOutput"):
            value = result.get(key)
            if isinstance(value, dict):
                return value

    if isinstance(result, str):
        try:
            nested = json.loads(result)
        except Exception:
            nested = None

        if isinstance(nested, dict):
            return nested

    # Some Claude CLI versions may emit the validated object directly.
    return obj


def run_structured(
    *,
    prompt: str,
    allowed_tools: list[str],
    schema: dict[str, Any],
    model: str = "haiku",
    max_turns: int = 8,
    timeout: int = 300,
    workspace: Path | None = None,
    weekly_security_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:

    cmd = [
        "claude",
        "-p",

        # Expose only the caller's reviewed tool set; empty denies all.
        "--tools",
        ",".join(allowed_tools),
    ]

    if allowed_tools:
        cmd.extend(
            ["--allowedTools", *allowed_tools]
        )

    if weekly_security_settings is not None:
        if workspace is None:
            raise BridgeError("Weekly Claude security requires a workspace")
        cmd.extend([
            "--restricted",
            "--safe-mode",
            "--strict-mcp-config",
            "--disallowedTools",
            "mcp__*,Agent,Bash,Edit,Write,NotebookEdit,PowerShell,REPL",
            "--settings",
            json.dumps(weekly_security_settings, separators=(",", ":")),
        ])

    cmd.extend(
        [
            "--permission-mode",
            "dontAsk",

            "--model",
            model,

            "--no-session-persistence",
            "--disable-slash-commands",

            "--max-turns",
            str(max_turns),

            "--output-format",
            "json",

            "--json-schema",
            json.dumps(
                schema,
                ensure_ascii=False,
                separators=(",", ":"),
            ),

            prompt,
        ]
    )

    try:
        cp = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=workspace,
        )
    except subprocess.TimeoutExpired as e:
        raise BridgeError(
            "Claude invocation timed out"
        ) from e
    except FileNotFoundError as e:
        raise BridgeError(
            "claude binary not found"
        ) from e

    if cp.returncode != 0:
        # Intentionally do not surface raw stdout/stderr because
        # presign calls may contain temporary signed URLs.
        # Extract only deterministic, safe error categories from
        # Claude CLI's own machine-readable JSON output on non-zero
        # exit. Falls back to the generic exit code for any unknown
        # or unparsable output.
        stage_code = DRAFT_REASON_CLAUDE_EXIT_NONZERO
        try:
            out_obj = json.loads(cp.stdout or "{}")
            if isinstance(out_obj, dict) and out_obj.get("is_error") is True:
                result = out_obj.get("result", "")
                if isinstance(result, str):
                    if "OAuth session expired" in result:
                        stage_code = DRAFT_REASON_CLAUDE_AUTH_EXPIRED
                    elif "Failed to authenticate" in result:
                        stage_code = DRAFT_REASON_CLAUDE_AUTH_FAILED
        except (ValueError, TypeError):
            pass
        exc = BridgeError(
            f"Claude invocation failed "
            f"(exit={cp.returncode})"
        )
        mark_stage(exc, stage_code, exit_code=cp.returncode)
        raise exc

    return _extract_structured(cp.stdout)
