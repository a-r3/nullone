#!/usr/bin/env python3
"""Offline tests for process-group-scoped subprocess execution (#119).

Uses REAL short-lived local processes only (sleep/print/marker file,
1-4s timeouts -- never 600 real seconds, no network, no model calls):

- child finishing before the timeout succeeds with output passthrough;
- slow child raises the REAL subprocess.TimeoutExpired (the typed
  provider-error mapping depends on this exact type);
- on timeout the whole process tree dies: direct child reaped AND
  grandchildren reaped (no marker file, no surviving sleepers);
- missing binary raises FileNotFoundError (startup mapping preserved);
- nonzero exit passes through without raising (check=False semantics).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_process_tree import run_tree_command  # noqa: E402


class ProcessTreeTests(unittest.TestCase):
    def test_success_passes_through_output(self):
        cp = run_tree_command(
            [sys.executable, "-c", "print('hi')"],
            cwd=tempfile.gettempdir(),
            timeout=30,
        )
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout.strip(), "hi")

    def test_nonzero_exit_passes_through_without_raising(self):
        cp = run_tree_command(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            cwd=tempfile.gettempdir(),
            timeout=30,
        )
        self.assertEqual(cp.returncode, 3)

    def test_timeout_raises_real_timeout_expired(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_tree_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tempfile.gettempdir(),
                timeout=1,
            )

    def test_timeout_kills_whole_tree(self):
        with tempfile.TemporaryDirectory() as td:
            marker = str(Path(td) / "grandchild.marker")
            script = (
                "import subprocess,time;"
                f"subprocess.Popen(['bash','-lc','sleep 3; touch {marker}; sleep 30']);"
                "time.sleep(30)"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                run_tree_command(
                    [sys.executable, "-c", script],
                    cwd=td,
                    timeout=1,
                )
            time.sleep(4)  # past the grandchild marker time
            self.assertFalse(
                os.path.exists(marker),
                msg="grandchild survived the timeout: process tree not cleaned up",
            )

    def test_missing_binary_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            run_tree_command(
                ["/tmp/nullone-definitely-no-such-binary-xyz"],
                cwd=tempfile.gettempdir(),
                timeout=5,
            )

    def test_consecutive_timeouts_are_stateless(self):
        """Two back-to-back timeouts both raise promptly: no leaked
        child holds resources that would overlap a scheduler retry."""

        for _ in range(2):
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_tree_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=tempfile.gettempdir(),
                    timeout=1,
                )
            self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
