#!/usr/bin/env python3
"""Capability-negative + positive delegation regression coverage for #66.

Guards `agents/approval/AGENTS.md` and `agents/publisher/AGENTS.md` against
reintroducing a reachable instruction that directs the approval agent to
mutate/publish through Zernio directly, while still allowing legitimate
negative statements ("approval agent must not call Zernio directly").

The check is paragraph-scoped rather than a blanket word ban: a paragraph
that mentions a Zernio write/mutate primitive is only a violation if that
same paragraph contains no negation cue. A prohibition list ("Never: ...
posts_update_post ...") and an affirmative grant ("zernio call_tool may be
used ONLY for posts_update_post") both mention the same token, but only the
second lacks a negation cue.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPROVAL_AGENTS_MD = ROOT / "agents/approval/AGENTS.md"
PUBLISHER_AGENTS_MD = ROOT / "agents/publisher/AGENTS.md"

# Zernio primitives capable of mutating/publishing a post, plus the generic
# dynamic dispatcher and the REST escape hatch. Read-only posts_get is
# included because this file's own authoritative design (PUBLICATION FLOW)
# forbids the approval agent from calling it directly at all.
BANNED_TOKENS = (
    "call_tool",
    "posts_update_post",
    "posts_publish_now",
    "posts_delete",
    "posts_unpublish_post",
    "posts_get",
    "zernio rest",
)

NEGATION_CUES = (
    "never",
    "must not",
    "do not",
    "don't",
    "forbidden",
    "prohibit",
    "no zernio",
    "has no",
    "not use",
    "not call",
    "not invoke",
    "may not",
    "cannot",
)


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def find_capability_grant_violations(text: str) -> list[str]:
    """Return paragraphs that mention a banned token with no negation cue."""
    violations = []
    for paragraph in split_paragraphs(text):
        lowered = paragraph.lower()
        if not any(token in lowered for token in BANNED_TOKENS):
            continue
        if any(cue in lowered for cue in NEGATION_CUES):
            continue
        violations.append(paragraph)
    return violations


class CapabilityNegativeDetectorSelfTest(unittest.TestCase):
    """Prove the detector itself distinguishes grant vs. prohibition."""

    def test_detector_flags_reintroduced_direct_publish_grant(self) -> None:
        reintroduced = (
            "## FINAL PUBLISH\n\n"
            "Then use zernio call_tool ONLY to invoke:\n\n"
            "posts_update_post\n\n"
            "with EXACT minimal payload:\n"
        )
        violations = find_capability_grant_violations(reintroduced)
        self.assertTrue(
            violations,
            "detector must flag a reintroduced direct Zernio publish grant",
        )

    def test_detector_flags_reintroduced_direct_read_call(self) -> None:
        reintroduced = "1. Call zernio posts_get.\n2. Require:\n   - exact same POST_ID\n"
        violations = find_capability_grant_violations(reintroduced)
        self.assertTrue(
            violations,
            "detector must flag a reintroduced direct posts_get call",
        )

    def test_detector_allows_legitimate_negative_statement(self) -> None:
        safe = (
            "## ABSOLUTE PROHIBITIONS\n\n"
            "Never:\n"
            "- call any Zernio MCP tool, including zernio call_tool, posts_get,\n"
            "  posts_update_post, or posts_publish_now\n"
        )
        violations = find_capability_grant_violations(safe)
        self.assertEqual(
            violations,
            [],
            "a documented prohibition must not be flagged as a capability grant",
        )

    def test_detector_allows_prose_mentioning_zernio_without_mutation(self) -> None:
        safe = (
            "Zernio account ID:\n"
            "6a982bbf77555aae01c28f21\n\n"
            "approval agent must not call Zernio directly for publication.\n"
        )
        violations = find_capability_grant_violations(safe)
        self.assertEqual(violations, [])


class ApprovalPublicationInstructionSafetyTests(unittest.TestCase):
    """The actual repository regression guard for issue #66."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.approval_text = APPROVAL_AGENTS_MD.read_text(encoding="utf-8")
        cls.publisher_text = PUBLISHER_AGENTS_MD.read_text(encoding="utf-8")

    # --- capability-negative -------------------------------------------------

    def test_approval_agent_has_no_direct_zernio_mutation_grant(self) -> None:
        violations = find_capability_grant_violations(self.approval_text)
        self.assertEqual(
            violations,
            [],
            "agents/approval/AGENTS.md contains an executable instruction "
            "that grants direct Zernio mutation/publication capability:\n\n"
            + "\n---\n".join(violations),
        )

    def test_publisher_agent_has_no_direct_zernio_mutation_grant(self) -> None:
        violations = find_capability_grant_violations(self.publisher_text)
        self.assertEqual(
            violations,
            [],
            "agents/publisher/AGENTS.md contains an executable instruction "
            "that grants direct Zernio mutation/publication capability:\n\n"
            + "\n---\n".join(violations),
        )

    def test_no_highest_priority_override_framing_remains(self) -> None:
        # These markers are a signal of unresolved contradictory-instruction
        # precedence left for the reader to resolve; #66 requires deleting
        # the superseded instructions instead of layering another override.
        self.assertNotIn(
            "OVERRIDES OLDER PUBLISH INSTRUCTIONS",
            self.approval_text,
        )
        self.assertNotIn(
            "PRODUCTION BRIDGE V1 — HIGHEST PRIORITY OVERRIDE",
            self.approval_text,
        )

    # --- positive delegation invariant ---------------------------------------

    def test_approval_delegates_final_publish_via_sessions_send_to_publisher(
        self,
    ) -> None:
        match = re.search(
            r"### FINAL STAGE(.*?)(?=\n### |\Z)",
            self.approval_text,
            re.DOTALL,
        )
        self.assertIsNotNone(
            match, "approval AGENTS.md must define a FINAL STAGE section"
        )
        final_stage = match.group(1)
        self.assertIn("sessions_send", final_stage)
        self.assertIn("texbrif-publisher", final_stage)
        self.assertIn("PUBLISH_AUTHORIZED", final_stage)
        self.assertIn("Do not itself publish", final_stage)

    def test_approval_publication_flow_forbids_direct_zernio_execution(
        self,
    ) -> None:
        match = re.search(
            r"## PUBLICATION FLOW(.*?)(?=\n## |\Z)",
            self.approval_text,
            re.DOTALL,
        )
        self.assertIsNotNone(
            match, "approval AGENTS.md must define a PUBLICATION FLOW section"
        )
        flow = match.group(1)
        self.assertIn("MUST NOT", flow)
        self.assertIn("posts_update_post", flow)
        self.assertIn("execute publication itself", flow)

    def test_publisher_authorizes_only_deterministic_wrapper(self) -> None:
        self.assertIn("nullone-publisher-run.py", self.publisher_text)
        self.assertIn("execute <POST_ID>", self.publisher_text)
        self.assertIn("call Zernio MCP directly", self.publisher_text)
        self.assertIn("use Zernio REST", self.publisher_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
