#!/usr/bin/env python3
"""Offline contract tests for runtime instruction authority (issue #6 residual).

Proves retired publication protocols stay retired, agents have no
executable publication paths, the capability contract covers every role,
and stable identifiers/branding are preserved. No network, no production.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = ROOT / "agents" / "approval" / "AGENTS.md"
PUBLISHER = ROOT / "agents" / "publisher" / "AGENTS.md"
WORKSPACE = ROOT / "workspace" / "AGENTS.md"
CONTRACT = ROOT / "docs" / "contracts" / "runtime-permissions.md"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class RuntimeAuthorityTests(unittest.TestCase):
    def test_approval_has_no_direct_publish_path(self):
        text = read(APPROVAL)
        # Prohibitions must be present ...
        for required in (
            "MUST NOT",
            "call Zernio posts_get",
            "posts_publish_now",
            "NO Zernio tool-call permission",
            "Do NOT send PUBLISH_AUTHORIZED",
        ):
            self.assertIn(required, text)
        # ... and no AFFIRMATIVE executable direct-publish instruction may
        # remain (prohibition mentions of tool names are expected above).
        for forbidden in (
            "to publish the post",
            "publish it now",
            "execute `nullone-publisher-run",
            "run nullone-publisher-run.py execute",
            "call Zernio to publish",
        ):
            self.assertNotIn(forbidden, text)

    def test_publisher_cannot_authorize_or_publish(self):
        text = read(PUBLISHER)
        for required in (
            "You are NOT in the publication path",
            "send PUBLISH_AUTHORIZED or PUBLISH_RESULT anywhere",
            "use sessions_send for anything publication-related",
            "call Zernio MCP directly",
            "use Zernio REST",
            "NO_REPLY",
        ):
            self.assertIn(required, text)

    def test_workspace_no_publish_result_protocol(self):
        text = read(WORKSPACE)
        self.assertIn("RETIRED", text)
        # No executable ledger-write / state-update protocol language remains.
        for forbidden in (
            "record the actual result in publish-ledger.jsonl",
            "update candidate/topic state",
            "mark the draft/candidate REJECTED in local state",
            "create a NEW versioned Zernio draft",
            "send the new draft to Telegram approval again",
        ):
            self.assertNotIn(forbidden, text)
        # Refusal semantics replace the protocol.
        for required in (
            "MUST NOT accept such messages",
            "must not trigger or retry publication",
            "deterministic reviewed reconciliation path",
            "REFUSE",
        ):
            self.assertIn(required, text)

    def test_no_publish_authorized_executable_path(self):
        for path in (APPROVAL, PUBLISHER, WORKSPACE):
            text = read(path)
            # The token may appear only inside retired-documentation context.
            for line in text.splitlines():
                if "PUBLISH_AUTHORIZED" not in line:
                    continue
                self.assertTrue(
                    any(marker in text for marker in ("RETIRED", "retired")),
                    f"{path}: PUBLISH_AUTHORIZED outside retired context",
                )
                self.assertNotIn("Send exactly", line)
                self.assertNotIn("send:", line.lower() + " ")
        # Workspace file must never instruct sending it.
        ws_lines = [ln for ln in read(WORKSPACE).splitlines() if "PUBLISH_AUTHORIZED" in ln]
        for ln in ws_lines:
            self.assertNotIn("send", ln.lower())

    def test_contract_covers_all_roles(self):
        text = read(CONTRACT)
        for role in (
            "MORNING EDITORIAL",
            "RADAR",
            "DAILY ANALYTICS",
            "WEEKLY STRATEGY",
            "DRAFT FACTORY",
            "APPROVAL",
            "PUBLISHER COMPATIBILITY AGENT",
            "DETERMINISTIC FINAL PUBLISH CONTROLLER",
            "NOTIFIER",
        ):
            self.assertIn(role, text)

    def test_final_controller_only_publish_authority(self):
        text = read(CONTRACT)
        self.assertIn("ONLY role with final", text)
        # No other role section may claim publish authority.
        for role in ("MORNING EDITORIAL", "RADAR", "DAILY ANALYTICS",
                     "WEEKLY STRATEGY", "DRAFT FACTORY", "APPROVAL",
                     "PUBLISHER COMPATIBILITY AGENT", "NOTIFIER"):
            section = text.split(role, 1)[1].split("###", 1)[0] \
                if "###" in text.split(role, 1)[1] else text.split(role, 1)[1]
            self.assertNotIn("final\n  publish authority".replace("\n  ", " "), section)

    def test_enforcement_levels_distinguished(self):
        text = read(CONTRACT)
        for marker in (
            "A. Required by design",
            "B. Technically enforced",
            "C. Prompt/instruction only",
            "D. Not permitted",
            "Do NOT claim B where only C exists",
        ):
            self.assertIn(marker, text)

    def test_stable_texbrif_ids_preserved(self):
        for path in (APPROVAL, PUBLISHER, CONTRACT):
            text = read(path)
            for stable in ("texbrif-approval", "texbrif-publisher", "texbrif:"):
                self.assertIn(stable, text)
        # Rename prohibition, in each file's own wording.
        self.assertIn("remain intentionally unchanged", read(APPROVAL))
        self.assertIn("Do not rename", read(PUBLISHER))

    def test_public_brand_nullone(self):
        for path in (APPROVAL, PUBLISHER):
            text = read(path)
            self.assertIn("NullOne", text)
        approval = read(APPROVAL)
        self.assertIn("must say NullOne, never Texbrif", approval)

    def test_skill_ownership_decision_recorded(self):
        text = read(CONTRACT)
        self.assertIn("EXTERNAL_CONTROLLED_COMPONENT", text)
        self.assertIn("Legacy production skill", text)
        self.assertIn("REPLACE it (never adopt it)", text)

    def test_single_hierarchy_no_competing_overrides(self):
        ws = read(WORKSPACE)
        self.assertIn("Instruction authority hierarchy", ws)
        self.assertIn("runtime-permissions.md", ws)
        # The contract points back to the single hierarchy.
        self.assertIn("single override order", read(CONTRACT).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
