#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from nullone_bridge_common import BridgeError
from nullone_editorial_provider_factory import get_editorial_provider
from nullone_editorial_runtime import (
    ProviderUnreachableError,
    run_morning_editorial,
)

HERE = Path(__file__).resolve().parent


def execute(occurrence_id: str, board_date: str | None) -> int:
    resolved_board_date = board_date or occurrence_id[:10]

    provider_name, invoke_provider = get_editorial_provider()

    result = run_morning_editorial(
        occurrence_id=occurrence_id,
        board_date=resolved_board_date,
        invoke_provider=invoke_provider,
    )

    print(f"EDITORIAL_PROVIDER={provider_name}")
    print(f"RUN_ID={result['run_id']}")
    print(f"DOMAIN_OUTCOME={result['domain_outcome']}")

    if result["domain_outcome"] != "SUCCEEDED":
        print(f"REASON_CODE={result['reason_code']}")
        print(f"REASON_TEXT={result['reason_text']}")
        return 1

    return 0


def self_test() -> int:
    calls: list[int] = []

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
            handoff = root / "social/research/daily/2026-01-01-editorial-candidates.json"
            handoff.write_text(
                '{"schema":"nullone.editorial-candidate-handoff.v1",'
                '"contract_version":"1.0.0",'
                '"editorial_date":"2026-01-01",'
                '"board_path":"social/research/daily/2026-01-01-editorial-board.md",'
                '"candidates":[]}',
                encoding="utf-8",
            )

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
