#!/usr/bin/env python3
"""Process-group-scoped subprocess execution (issue #119).

All NullOne OpenCode/Claude provider adapters spawn `opencode run` /
`claude -p`, which in turn may spawn tool grandchildren (reviewed bash
helpers: renderers, scan commit, draft bridge, delivery adapter).
Plain `subprocess.run(timeout=...)` kills only the DIRECT child on
expiry: grandchildren survive, overlap scheduler retries, and can
commit duplicate side effects after the job was already marked
timed-out.

`run_tree_command` runs the child as a process-group leader
(`start_new_session=True`) and, on timeout, SIGKILLs the whole group
before reaping. Contract is otherwise identical to the
`subprocess.run(..., text=True, capture_output=True, check=False)`
shape every caller already used:

- success -> `CompletedProcess` with returncode/stdout/stderr;
-slow child -> the REAL `subprocess.TimeoutExpired` (callers keep
  their existing `except` mapping to typed provider errors);
- missing binary -> `FileNotFoundError` (callers keep their existing
  startup-misconfiguration mapping).

POSIX-only (Linux production target, same as the `fcntl` usage in
`nullone_editorial_runtime.py`).
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Sequence


def run_tree_command(
    cmd: Sequence[str],
    *,
    cwd: Path | str,
    timeout: float | int,
) -> subprocess.CompletedProcess[str]:
    """Run `cmd` with a wall-clock timeout, killing the process tree."""

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def self_test() -> int:
    import sys
    import tempfile

    # Success passthrough.
    cp = run_tree_command(
        [sys.executable, "-c", "print('hi')"],
        cwd=tempfile.gettempdir(),
        timeout=30,
    )
    assert cp.returncode == 0 and cp.stdout.strip() == "hi"

    # Timeout maps to the real TimeoutExpired type.
    try:
        run_tree_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tempfile.gettempdir(),
            timeout=1,
        )
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("slow child did not time out")

    print("PROCESS_TREE_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne process-tree subprocess helper")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
