#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMMANDS = [
    [sys.executable, "tests/test_acceptance_contracts.py"],
    [sys.executable, "tests/test_breaking_routing_contract.py"],
    [sys.executable, "tests/test_cadence_contract_fixture.py"],
    [sys.executable, "tests/test_cadence_controller.py"],
    [sys.executable, "tests/test_cadence_state_adapter.py"],
    [sys.executable, "tests/test_run_outcomes.py"],
    [sys.executable, "tests/test_behavioral_regressions.py"],
    [sys.executable, "tests/test_morning_editorial.py"],
    [sys.executable, "tests/test_daily_analytics.py"],
    [sys.executable, "tests/test_failure_notify.py"],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-manifest.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-draft-bridge.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-publish-bridge.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-publisher-run.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-morning-editorial-run.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-daily-analytics-run.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-failure-notify-run.py",
        "self-test",
    ],
]


def main() -> int:
    for cmd in COMMANDS:
        print("+", " ".join(cmd), flush=True)
        cp = subprocess.run(cmd, cwd=ROOT, check=False)
        if cp.returncode != 0:
            return cp.returncode

    print("OFFLINE_REGRESSION_SUITE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
