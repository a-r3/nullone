#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_editorial_runtime import (
    REACHABILITY_PATTERN,
    ProviderUnreachableError,
    run_morning_editorial,
)

HERE = Path(__file__).resolve().parent
PROMPT_PATH = WORKSPACE / "social/ops/prompts/morning-editorial.md"
INVOCATION_TIMEOUT_SECONDS = 900


def _default_invoke_provider() -> None:
    """Invoke the real Morning Editorial planning cycle.

    Not exercised by any test in this repository: tests inject a fake
    `invoke_provider` into `run_morning_editorial` instead. Production
    wiring of this default path has not been deployed.
    """

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
            timeout=INVOCATION_TIMEOUT_SECONDS,
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


def execute(occurrence_id: str, board_date: str | None) -> int:
    resolved_board_date = board_date or occurrence_id[:10]

    result = run_morning_editorial(
        occurrence_id=occurrence_id,
        board_date=resolved_board_date,
        invoke_provider=_default_invoke_provider,
    )

    print(f"RUN_ID={result['run_id']}")
    print(f"DOMAIN_OUTCOME={result['domain_outcome']}")

    if result["domain_outcome"] != "SUCCEEDED":
        print(f"REASON_CODE={result['reason_code']}")
        print(f"REASON_TEXT={result['reason_text']}")
        return 1

    return 0


def self_test() -> int:
    calls: list[int] = []

    def flaky_then_ok() -> None:
        calls.append(1)
        if len(calls) < 2:
            raise ProviderUnreachableError(
                "API Error: Can't reach the API server — ENOTFOUND"
            )

        board = (
            WORKSPACE
            / "social/research/daily/2026-01-01-editorial-board.md"
        )
        board.parent.mkdir(parents=True, exist_ok=True)
        board.write_text("# Editorial board\n", encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        board = root / "social/research/daily/2026-01-01-editorial-board.md"

        def invoke() -> None:
            calls.append(1)
            if len(calls) < 2:
                raise ProviderUnreachableError(
                    "API Error: Can't reach the API server — ENOTFOUND"
                )
            board.parent.mkdir(parents=True, exist_ok=True)
            board.write_text("# Editorial board\n", encoding="utf-8")

        result = run_morning_editorial(
            occurrence_id="2026-01-01T08:30:00+04:00",
            board_date="2026-01-01",
            invoke_provider=invoke,
            sleep=lambda _seconds: None,
            artifact_root=root,
            output_root=root / "run-outcomes",
        )

        assert result["domain_outcome"] == "SUCCEEDED"
        assert len(calls) == 2

    print("MORNING_EDITORIAL_RUNNER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")

    e = sub.add_parser("execute")
    e.add_argument("--occurrence-id", required=True)
    e.add_argument("--board-date", default=None)

    args = p.parse_args()

    try:
        if args.command == "self-test":
            return self_test()

        if args.command == "execute":
            return execute(args.occurrence_id, args.board_date)

        raise BridgeError("Unknown command")

    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
