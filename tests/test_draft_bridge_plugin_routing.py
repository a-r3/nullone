#!/usr/bin/env python3
"""Harness: run the offline Node draft-bridge plugin suites for #142.

Requires the `node` binary (preinstalled on CI runners). No network, no
OpenClaw runtime: the suites exercise only the dependency-free route.js
and the SDK-stubbed index.js of plugins/nullone-draft-bridge/.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_SUITES = [
    ROOT / "tests/js/test_draft_bridge_route.js",
    ROOT / "tests/js/test_draft_bridge_index.js",
]
EXPECTED_PASSES = {
    "test_draft_bridge_route.js": 7,
    # +1 for the P0 texbrif namespace collision fix: register() no longer
    # calls registerInteractiveHandler itself (nullone-final-publish is now
    # the sole registrant and delegates texbrif:draft:* here directly).
    "test_draft_bridge_index.js": 11,
}


class DraftBridgePluginSuiteTests(unittest.TestCase):
    def test_node_draft_bridge_plugin_suites_pass(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node binary is required for #142 plugin tests")
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
