#!/usr/bin/env python3
"""Regression tests for issue #146: Story writer output contract.

Production STORY E2E (2026-09-17) fail-closed with:

    WRITER_OUTPUT_INVALID:
    writer output has unexpected field(s): ['source_attribution']

Root cause: build_writer_context() exposed the candidate-level
`source_attribution` key verbatim inside the writer prompt context.
The OpenCode provider path (production) reuses _writer_prompt()
verbatim with no API-level schema enforcement, so the model echoed
`source_attribution` back as an output field. The strict output
contract (_WRITER_SPEC_FIELDS / WRITER_SCHEMA, additionalProperties
False) correctly rejected it. The canonical writer-output provenance
field is `source_name` (rendered as the Mənbə: line).

Fix: candidate provenance now reaches the writer ONLY under the
input-only `source_name_hint` context key, and the prompt maps it
explicitly onto `source_name`. The output schema stays strict:
`source_attribution` and any other unknown field still fail closed.

Offline only: temp-fixture workspaces, injected fake writers. No
network, no Zernio/Telegram/model calls.
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_story_pipeline as pipeline  # noqa: E402
from nullone_bridge_common import BridgeError, atomic_write_json, load_manifest, now_iso  # noqa: E402


REAL_RENDERER = ROOT / "workspace/social/tools/render_story_v2.py"


def make_candidate(**overrides) -> dict:
    candidate = {
        "candidate_id": "cand-146",
        "topic": "Yeni AI modeli sürət rekordu",
        "topic_cluster": "ai-performance",
        "content_type": "NEWS",
        "verification": "PASS",
        "evidence_refs": [
            "Rəsmi elan: yeni model əvvəlkindən 42% daha sürətlidir.",
        ],
        "source_attribution": "Rəsmi mənbə blogu",
        "factual_inputs": {},
    }
    candidate.update(overrides)
    return candidate


def make_writer(spec: dict):
    def _writer(_editorial_context):
        return dict(spec)

    return _writer


DEFAULT_SPEC = {
    "layout": "big-stat",
    "headline": "Yeni model 42% daha sürətlidir",
    "body": "Rəsmi mənbəyə görə performans artımı təsdiqlənib.",
    "stat": "42%",
    "source_name": "Rəsmi mənbə",
    "use_source_image": False,
    "cta": "@nullone.az",
}


class FakeDraftConnector:
    def create_review_draft(self, manifest_path: Path) -> None:
        _, m = load_manifest(manifest_path)
        m["review"]["create_attempts"] = 1
        m["review"]["state"] = "DRAFT_CREATED"
        m["review"]["zernio_draft_id"] = "fake-review-146"
        m["review"]["created_at"] = now_iso()
        atomic_write_json(manifest_path, m)


PASS_VERIFIER = pipeline.make_fake_verifier("PASS")


class StoryWriterOutputContractTests(unittest.TestCase):
    """Base: isolates nullone_bridge_common.WORKSPACE to a temp dir."""

    def setUp(self):
        self._tmpdir_ctx = __import__("tempfile").TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir_ctx.name)
        self._patcher = patch.object(bridge_common, "WORKSPACE", self.tmp_path)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._tmpdir_ctx.cleanup)

    def install_real_renderer(self):
        tools_dir = self.tmp_path / "social/tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(REAL_RENDERER, tools_dir / "render_story_v2.py")

    def run_pipeline(self, candidate=None, writer=None):
        return pipeline.run_story_pipeline(
            candidate or make_candidate(),
            writer=writer or make_writer(DEFAULT_SPEC),
            verifier=PASS_VERIFIER,
            draft_connector=FakeDraftConnector(),
            telegram_sender=None,
        )

    # -- exact production failure stays fail-closed ----------------------

    def test_production_failure_shape_still_rejected(self):
        """The exact 2026-09-17 production output must still be rejected."""
        spec = dict(DEFAULT_SPEC)
        spec["source_attribution"] = "Rəsmi mənbə blogu"
        result = self.run_pipeline(writer=make_writer(spec))
        self.assertEqual(result.outcome, "WRITER_OUTPUT_INVALID")
        self.assertIn("source_attribution", result.reason_text)

    def test_unknown_extra_field_still_rejected(self):
        """Arbitrary unknown fields other than the reviewed contract fail."""
        spec = dict(DEFAULT_SPEC)
        spec["some_future_field"] = "x"
        result = self.run_pipeline(writer=make_writer(spec))
        self.assertEqual(result.outcome, "WRITER_OUTPUT_INVALID")
        self.assertIn("some_future_field", result.reason_text)

    # -- echo path removed -------------------------------------------------

    def test_writer_context_has_no_source_attribution_key(self):
        context = pipeline.build_writer_context(make_candidate())
        self.assertNotIn("source_attribution", context)
        self.assertEqual(context["source_name_hint"], "Rəsmi mənbə blogu")

    def test_writer_prompt_maps_hint_to_source_name(self):
        context = pipeline.build_writer_context(make_candidate())
        prompt = pipeline._writer_prompt(context)
        self.assertNotIn('"source_attribution"', prompt)
        self.assertIn("source_name", prompt)
        self.assertIn("source_name_hint", prompt)

    def test_output_schema_still_strict(self):
        self.assertFalse(pipeline.WRITER_SCHEMA["additionalProperties"])
        self.assertNotIn("source_attribution", pipeline._WRITER_SPEC_FIELDS)
        self.assertIn("source_name", pipeline._WRITER_SPEC_FIELDS)
        self.assertNotIn(
            "source_attribution", pipeline.WRITER_SCHEMA["properties"]
        )

    # -- canonical field flows end to end -----------------------------------

    def test_valid_spec_with_source_name_proceeds(self):
        """Canonical output passes writer validation; pipeline completes."""
        self.install_real_renderer()
        result = self.run_pipeline(writer=make_writer(dict(DEFAULT_SPEC)))
        self.assertEqual(result.outcome, "DRAFT_CREATED")

    def test_shape_validator_accepts_canonical_spec(self):
        validated = pipeline.validate_story_spec_shape(dict(DEFAULT_SPEC))
        self.assertEqual(validated["source_name"], "Rəsmi mənbə")

    def test_shape_validator_rejects_attribution_echo(self):
        spec = dict(DEFAULT_SPEC)
        spec["source_attribution"] = "Rəsmi mənbə blogu"
        with self.assertRaises(pipeline.StoryWriterOutputInvalid):
            pipeline.validate_story_spec_shape(spec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
