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
NODE_SUITE = ROOT / "tests/js/test_plugin_route.js"


class PluginRoutingSuiteTests(unittest.TestCase):
    def test_node_routing_suite_passes(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node binary is required for #89 plugin tests")
        assert node is not None
        proc = subprocess.run(
            [node, "--test", str(NODE_SUITE)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
        )
        self.assertEqual(
            proc.returncode,
            0,
            "Node plugin routing suite failed:\n" + proc.stdout + proc.stderr,
        )
        self.assertIn("# pass 9", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
