#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from nullone_bridge_common import BridgeError
from nullone_failure_notify import (
    NOTIFICATION_ROOT,
    NotifierError,
    OpenClawTelegramTransport,
    notify_if_required,
)
from nullone_run_outcome import CompletionContractError, assess_run


def _load_result(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BridgeError(f"Could not read run-outcome file: {path}: {exc}") from exc

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"Invalid run-outcome JSON: {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise BridgeError(f"Run-outcome file must contain a JSON object: {path}")

    return value


def notify(result_file: str, output_root: str | None = None) -> int:
    path = Path(result_file).expanduser().resolve()
    result = _load_result(path)

    resolved_output_root = (
        Path(output_root).expanduser().resolve()
        if output_root is not None
        else NOTIFICATION_ROOT
    )

    try:
        outcome = notify_if_required(
            result,
            transport=OpenClawTelegramTransport(),
            output_root=resolved_output_root,
        )
    except CompletionContractError as exc:
        raise BridgeError(f"Invalid run-outcome record: {exc}") from exc
    except NotifierError as exc:
        # NotifierError messages are already scrubbed of raw
        # workflow_id/identity/state content at the source — never add
        # any raw record/file content here.
        raise BridgeError(f"Notifier state error: {exc}") from exc

    print(f"NOTIFICATION_STATUS={outcome['status']}")
    return 0


def self_test() -> int:
    calls: list[str] = []

    class FakeTransport:
        def send(self, message: str) -> None:
            calls.append(message)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        result = assess_run(
            workflow_id="daily-analytics",
            occurrence_id="2026-01-01T03:20:00+04:00",
            scheduler_status="succeeded",
            domain_outcome="BLOCKED",
            reason_code="EXAMPLE_DEPENDENCY_UNAVAILABLE",
            reason_text="Analytics access could not be established.",
        )

        first = notify_if_required(
            result,
            transport=FakeTransport(),
            output_root=root / "notifications",
        )

        assert first["status"] == "SENT"
        assert len(calls) == 1

        second = notify_if_required(
            result,
            transport=FakeTransport(),
            output_root=root / "notifications",
        )

        assert second["status"] == "ALREADY_SENT"
        assert len(calls) == 1

    print("FAILURE_NOTIFY_RUNNER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")

    n = sub.add_parser("notify")
    n.add_argument("--result-file", required=True)
    n.add_argument(
        "--output-root",
        default=None,
        help=(
            "Override the notification state directory. Intended for "
            "tests/tooling only — production omits this and uses the "
            "default under social/ops/notifications/."
        ),
    )

    args = p.parse_args()

    try:
        if args.command == "self-test":
            return self_test()

        if args.command == "notify":
            return notify(args.result_file, args.output_root)

        raise BridgeError("Unknown command")

    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
