#!/usr/bin/env python3
"""Harness: run the offline Node plugin-routing suite for #89.

Requires the `node` binary (preinstalled on CI runners). No network, no
OpenClaw runtime: the suite exercises only the dependency-free route.js.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_SUITES = [
    ROOT / "tests/js/test_plugin_route.js",
    ROOT / "tests/js/test_plugin_index.js",
    ROOT / "tests/js/test_plugin_link.js",
]
EXPECTED_PASSES = {
    "test_plugin_route.js": 9,
    "test_plugin_index.js": 28,
    "test_plugin_link.js": 16,
}


class PluginRoutingSuiteTests(unittest.TestCase):
    def test_node_routing_suite_passes(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node binary is required for #89 plugin tests")
        assert node is not None
        for suite in NODE_SUITES:
            proc = subprocess.run(
                [node, "--test", str(suite)],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(ROOT),
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"Node suite {suite.name} failed:\n" + proc.stdout + proc.stderr,
            )
            self.assertIn(
                f"# pass {EXPECTED_PASSES[suite.name]}",
                proc.stdout,
                f"Node suite {suite.name} pass-count mismatch:\n" + proc.stdout,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
