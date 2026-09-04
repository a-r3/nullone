#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
from typing import Any

from nullone_bridge_common import BridgeError


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
) -> dict[str, Any]:

    cmd = [
        "claude",
        "-p",

        # No Bash/Read/Edit/Web/etc.
        "--tools",
        "",
    ]

    if allowed_tools:
        cmd.extend(
            ["--allowedTools", *allowed_tools]
        )

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
        raise BridgeError(
            f"Claude invocation failed "
            f"(exit={cp.returncode})"
        )

    return _extract_structured(cp.stdout)
