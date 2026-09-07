#!/usr/bin/env python3
"""Capability-negative + positive delegation regression coverage for #66.

Guards `agents/approval/AGENTS.md` and `agents/publisher/AGENTS.md` against
reintroducing a reachable instruction that directs the approval agent to
mutate/publish through Zernio directly, while still allowing legitimate
negative statements ("approval agent must not call Zernio directly").

Design: the text is split into small clauses (bullet items — with a
negation heading such as "Never:" / "It MUST NOT:" prefixed onto each bullet
beneath it — and sentence/semicolon-separated fragments). A clause that
mentions a Zernio write/mutate primitive is safe only if that SAME clause
carries a negation cue and no exception/grant marker. This is deliberately
clause-scoped, not paragraph-scoped: a paragraph mixing "do not call Zernio
before approval; after approval call posts_update_post" must still fail on
its second clause even though its first clause is a legitimate prohibition,
and "do not call X except after Y" must fail even within one clause, because
"except"/"after" mark it as a conditional grant rather than a pure ban.
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

# A clause containing one of these is a conditional/exception/imperative
# grant, not a pure prohibition -- even if a negation cue also appears in
# the same clause (e.g. "do not call X except after Y").
GRANT_MARKER_RE = re.compile(
    r"\b(except|unless|after|then|may|can|allowed)\b", re.IGNORECASE
)

_HEADING_RE = re.compile(r"^(.*:)\s*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;])\s+")


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _split_sentence_clauses(text: str) -> list[str]:
    return [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]


def _clauses_in_paragraph(paragraph: str) -> list[str]:
    raw_lines = paragraph.split("\n")

    # Merge indented continuation lines (wrapped bullet text) into the
    # previous logical line so a token split across two source lines is
    # evaluated as one clause.
    logical_lines: list[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and logical_lines:
            logical_lines[-1] = f"{logical_lines[-1]} {line.strip()}"
        else:
            logical_lines.append(line.strip())
    logical_lines = [line for line in logical_lines if line]
    if not logical_lines:
        return []

    # A leading "Never:" / "It MUST NOT:" / "You do NOT:" heading carries
    # its negation cue onto every bullet beneath it -- each bullet is
    # evaluated as "<heading> <bullet>", not in isolation.
    heading = ""
    body_lines = logical_lines
    if len(logical_lines) > 1 and _HEADING_RE.match(logical_lines[0]):
        heading = logical_lines[0]
        body_lines = logical_lines[1:]

    clauses: list[str] = []
    for line in body_lines:
        bullet = line.lstrip("-* ").strip()
        if not bullet:
            continue
        combined = f"{heading} {bullet}".strip() if heading else bullet
        clauses.extend(_split_sentence_clauses(combined))
    return clauses


def clauses_in_text(text: str) -> list[str]:
    clauses: list[str] = []
    for paragraph in split_paragraphs(text):
        clauses.extend(_clauses_in_paragraph(paragraph))
    return clauses


def find_capability_grant_violations(text: str) -> list[str]:
    """Return clauses that grant/permit a banned Zernio primitive.

    A clause mentioning a banned token is a violation unless it both (a)
    carries a negation cue and (b) carries no exception/grant marker.
    """
    violations = []
    for clause in clauses_in_text(text):
        lowered = clause.lower()
        if not any(token in lowered for token in BANNED_TOKENS):
            continue
        if GRANT_MARKER_RE.search(lowered):
            violations.append(clause)
            continue
        if any(cue in lowered for cue in NEGATION_CUES):
            continue
        violations.append(clause)
    return violations


class CapabilityNegativeDetectorSelfTest(unittest.TestCase):
    """Prove the detector distinguishes grant vs. prohibition at clause level."""

    # --- required PASS examples (issue #66 hardening request, section 5) ---

    def test_pass_pure_prohibition(self) -> None:
        self.assertEqual(
            find_capability_grant_violations("Never call posts_update_post."),
            [],
        )

    def test_pass_pure_prohibition_with_delegation(self) -> None:
        text = (
            "The approval agent must not call Zernio directly; "
            "delegate publication to texbrif-publisher."
        )
        self.assertEqual(find_capability_grant_violations(text), [])

    def test_pass_explicit_no_capability_statement(self) -> None:
        text = "This agent has no Zernio tool-call permission."
        self.assertEqual(find_capability_grant_violations(text), [])

    def test_pass_documented_prohibition_bullet_list(self) -> None:
        safe = (
            "## ABSOLUTE PROHIBITIONS\n\n"
            "Never:\n"
            "- call any Zernio MCP tool, including zernio call_tool, posts_get,\n"
            "  posts_update_post, or posts_publish_now\n"
        )
        self.assertEqual(find_capability_grant_violations(safe), [])

    def test_pass_prose_mentioning_zernio_without_mutation(self) -> None:
        safe = (
            "Zernio account ID:\n"
            "6a982bbf77555aae01c28f21\n\n"
            "approval agent must not call Zernio directly for publication.\n"
        )
        self.assertEqual(find_capability_grant_violations(safe), [])

    # --- required FAIL examples (issue #66 hardening request, section 5) ---

    def test_fail_direct_grant(self) -> None:
        violations = find_capability_grant_violations("Call posts_update_post.")
        self.assertTrue(violations, "a bare direct-call imperative must fail")

    def test_fail_conditional_grant(self) -> None:
        violations = find_capability_grant_violations(
            "You may call posts_update_post after final approval."
        )
        self.assertTrue(violations, "a conditional publish grant must fail")

    def test_fail_only_carveout(self) -> None:
        violations = find_capability_grant_violations(
            "zernio call_tool may be used ONLY for posts_update_post."
        )
        self.assertTrue(violations, "an ONLY carve-out grant must fail")

    def test_fail_mixed_prohibition_then_grant_in_same_paragraph(self) -> None:
        text = (
            "Do not call Zernio before approval; "
            "after approval call posts_update_post."
        )
        violations = find_capability_grant_violations(text)
        self.assertTrue(
            violations,
            "a later affirmative clause must fail even though an earlier "
            "clause in the same paragraph is a legitimate prohibition",
        )

    def test_fail_exception_wording(self) -> None:
        text = "Do not call posts_update_post except after texbrif:publish:<POST_ID>."
        violations = find_capability_grant_violations(text)
        self.assertTrue(
            violations,
            "'except after' must not be sanitized by the 'do not' in the "
            "same clause",
        )

    def test_fail_instruction_sequence(self) -> None:
        text = "First verify approval. Then invoke zernio call_tool for posts_update_post."
        violations = find_capability_grant_violations(text)
        self.assertTrue(
            violations,
            "a nearby unrelated negative sentence must not sanitize a "
            "separate affirmative command",
        )

    # --- original regression coverage: the actually-removed legacy text ----

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
        for marker in (
            "OVERRIDES OLDER PUBLISH INSTRUCTIONS",
            "PRODUCTION BRIDGE V1 — HIGHEST PRIORITY OVERRIDE",
            "APPROVAL DELIVERY DEDUP — HIGHEST PRIORITY OVERRIDE",
        ):
            self.assertNotIn(marker, self.approval_text)

    def test_purpose_does_not_claim_approval_executes_publication(self) -> None:
        match = re.search(r"## PURPOSE(.*?)(?=\n## |\Z)", self.approval_text, re.DOTALL)
        self.assertIsNotNone(match, "approval AGENTS.md must define a PURPOSE section")
        purpose = match.group(1)
        self.assertNotIn("perform the final human-authorized publish action", purpose)
        self.assertIn("delegate", purpose.lower())
        self.assertIn("texbrif-publisher", purpose)

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
