#!/usr/bin/env python3
"""Offline contract test for the Breaking Consumer delivery recipe (#183).

Proves the desired consumer creation recipe in
docs/deployment/80-breaking-radar-production-integration.md pins
no-delivery explicitly instead of relying on OpenClaw's default
delivery. Proven live defect: without the flag the job inherited
mode=announce/channel=last and every run ended not-delivered
(`Refusing implicit isolated cron delivery`) despite a healthy
sweep (exit 0). No network, no production, no mutation.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE_DOC = ROOT / "docs" / "deployment" / "80-breaking-radar-production-integration.md"


def read_recipe() -> str:
    return RECIPE_DOC.read_text(encoding="utf-8")


def consumer_recipe_block(text: str) -> str:
    """Extract the desired nullone-breaking-consumer create block."""
    match = re.search(
        r"```bash\n(openclaw automations create \"45 11,14,17,20,23 \* \* \*\" \\\n(?:.*\\\n)*?.*?nullone-breaking-consume\.py sweep.*?)```",
        text,
        re.DOTALL,
    )
    assert match is not None, "consumer recipe block not found"
    return match.group(1)


class BreakingConsumerDeliveryContractTests(unittest.TestCase):
    def test_recipe_pins_no_deliver(self):
        block = consumer_recipe_block(read_recipe())
        self.assertIn("--no-deliver", block)

    def test_recipe_never_announces_or_addresses(self):
        block = consumer_recipe_block(read_recipe())
        for forbidden in ("--announce", "--channel", "--to", "best-effort"):
            self.assertNotIn(
                forbidden, block, msg=f"recipe must not contain {forbidden!r}"
            )

    def test_recipe_shape_unchanged(self):
        block = consumer_recipe_block(read_recipe())
        for required in (
            '"45 11,14,17,20,23 * * *"',
            '--name "nullone-breaking-consumer"',
            "nullone-breaking-consume.py sweep",
            '--tz "Asia/Baku"',
        ):
            self.assertIn(required, block, msg=f"recipe must keep {required!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
