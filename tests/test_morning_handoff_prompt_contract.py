#!/usr/bin/env python3
"""Contract-alignment regression test for the Morning Editorial prompt (#100-follow-up).

Proves the 2026-09-12 production incident cannot silently recur: Morning's
structured JSON handoff instruction in
`workspace/social/ops/prompts/morning-editorial.md` must name the exact
literal field `verification` (not `verification_status`, and not any other
synonym), must explicitly forbid `verification_status`, must cover every
field `nullone_editorial_candidate_handoff.CANDIDATE_REQUIRED_FIELDS`
declares, and must ship a worked JSON example that actually satisfies the
real strict validator -- with no drift tolerated on either side.

This test derives its expectations from CANDIDATE_REQUIRED_FIELDS itself
(never a hand-copied field list) so a future contract change that isn't
mirrored into the prompt fails this test immediately, instead of waiting
for another production HANDOFF_INVALID.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
PROMPT_PATH = ROOT / "workspace/social/ops/prompts/morning-editorial.md"
sys.path.insert(0, str(SCRIPTS))

from nullone_editorial_candidate_handoff import (  # noqa: E402
    CANDIDATE_REQUIRED_FIELDS,
    CONTRACT_VERSION,
    SCHEMA,
    EditorialHandoffError,
    validate_handoff,
)

FORBIDDEN_FIELD = "verification_status"


def _prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _structured_field_list_block(text: str) -> str:
    """The bullet list of literal JSON keys, isolated from surrounding prose.

    Bounded between the "literal JSON keys" heading and the sentence that
    explains the `verification` field -- the region where every listed key
    is a *producer instruction* to emit that key, as opposed to the
    prohibition sentence further down that necessarily still contains the
    string "verification_status" in prose (forbidding it, not listing it).
    """

    start = text.index("literal JSON keys")
    end = text.index("The verification field is named")
    return text[start:end]


def _json_example(text: str) -> dict:
    match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    if match is None:
        raise AssertionError("morning-editorial.md has no fenced ```json example")
    return json.loads(match.group(1))


def _wrap_as_handoff(candidate: dict) -> dict:
    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "editorial_date": "2026-09-08",
        "board_path": "social/research/daily/2026-09-08-editorial-board.md",
        "candidates": [candidate],
    }


class PromptUsesExactVerificationKeyTests(unittest.TestCase):
    def test_field_list_contains_literal_verification_key(self):
        block = _structured_field_list_block(_prompt_text())
        self.assertIn(
            "`verification`",
            block,
            "structured field list must name the exact literal key `verification`",
        )

    def test_field_list_never_lists_verification_status_as_a_field(self):
        block = _structured_field_list_block(_prompt_text())
        self.assertNotIn(
            FORBIDDEN_FIELD,
            block,
            "the literal-key field list must not (re)introduce verification_status",
        )


class VerificationStatusForbiddenTests(unittest.TestCase):
    def test_prompt_explicitly_forbids_verification_status(self):
        text = _prompt_text()
        self.assertRegex(
            text,
            r"NEVER emit a field called `verification_status`",
            "prompt must explicitly and literally forbid verification_status",
        )


class AllRequiredFieldsAlignedTests(unittest.TestCase):
    def test_every_contract_required_field_named_literally_in_prompt(self):
        block = _structured_field_list_block(_prompt_text())
        missing = [
            field
            for field in CANDIDATE_REQUIRED_FIELDS
            if f"`{field}`" not in block
        ]
        self.assertEqual(
            missing,
            [],
            f"prompt field list is missing exact literal key(s): {missing} "
            "-- producer instruction has drifted from "
            "nullone_editorial_candidate_handoff.CANDIDATE_REQUIRED_FIELDS",
        )


class JsonExampleValidatesTests(unittest.TestCase):
    def test_example_candidate_passes_the_real_validator(self):
        example = _json_example(_prompt_text())
        # Must be a self-contained, schema-legal candidate, not a fragment.
        snapshot = validate_handoff(_wrap_as_handoff(example))
        self.assertEqual(len(snapshot["candidates"]), 1)

    def test_example_uses_verification_not_verification_status(self):
        example = _json_example(_prompt_text())
        self.assertIn("verification", example)
        self.assertNotIn(FORBIDDEN_FIELD, example)


class StrictValidatorUnchangedTests(unittest.TestCase):
    """Reproduces the exact 2026-09-12 production failure against the live
    validator, proving the fix is in the prompt, not a loosened contract."""

    def test_renaming_verification_to_verification_status_still_fails_closed(self):
        example = dict(_json_example(_prompt_text()))
        value = example.pop("verification")
        example[FORBIDDEN_FIELD] = value

        with self.assertRaises(EditorialHandoffError) as ctx:
            validate_handoff(_wrap_as_handoff(example))

        self.assertIn("unknown field", str(ctx.exception))
        self.assertIn(FORBIDDEN_FIELD, str(ctx.exception))

    def test_validator_still_rejects_missing_required_verification(self):
        example = dict(_json_example(_prompt_text()))
        del example["verification"]

        with self.assertRaises(EditorialHandoffError) as ctx:
            validate_handoff(_wrap_as_handoff(example))

        self.assertIn("missing required field", str(ctx.exception))

    def test_validator_still_rejects_unknown_fields_in_general(self):
        example = dict(_json_example(_prompt_text()))
        example["totally_made_up_field"] = "nope"

        with self.assertRaises(EditorialHandoffError):
            validate_handoff(_wrap_as_handoff(example))


if __name__ == "__main__":
    unittest.main(verbosity=2)
