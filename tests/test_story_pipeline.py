#!/usr/bin/env python3
"""Behavioral tests for the #33 lightweight Story draft pipeline.

Exercises nullone_story_pipeline against temp-fixture workspaces only.
No network, no real Zernio/Telegram/Claude calls. The real
render_story_v2.py IS exercised in the end-to-end acceptance test (it is a
pure local PNG renderer with no external calls); review-draft creation and
Telegram delivery always use injected fakes.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import textwrap
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


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_candidate(**overrides) -> dict:
    candidate = {
        "candidate_id": "cand-001",
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


def make_writer(spec: dict, raise_invalid: bool = False):
    def _writer(candidate, editorial_context):
        if raise_invalid:
            return {"layout": "not-a-real-layout"}
        return dict(spec)

    return _writer


DEFAULT_SPEC = {
    "layout": "big-stat",
    "headline": "Yeni model 42% daha sürətlidir",
    "body": "Rəsmi mənbəyə görə performans artımı təsdiqlənib.",
    "stat": "42%",
    "source_name": "Rəsmi mənbə",
    "cta": "@nullone.az",
}


class FakeDraftConnector:
    """Mirrors nullone-draft-bridge.py's own manifest-mutation contract."""

    _next_id = 0

    def __init__(self, scenario: str):
        self.scenario = scenario
        self.calls = 0

    def create_review_draft(self, manifest_path: Path) -> None:
        self.calls += 1
        _, m = load_manifest(manifest_path)

        if self.scenario == "success":
            FakeDraftConnector._next_id += 1
            m["review"]["create_attempts"] = 1
            m["review"]["state"] = "DRAFT_CREATED"
            m["review"]["zernio_draft_id"] = f"fake-review-{FakeDraftConnector._next_id}"
            m["review"]["created_at"] = now_iso()
            atomic_write_json(manifest_path, m)
            return

        if self.scenario == "blocked_before_attempt":
            # Preflight validation failed before consuming the attempt --
            # nothing on the manifest is mutated.
            raise BridgeError("fake preflight validation blocked")

        if self.scenario == "create_in_flight":
            m["review"]["create_attempts"] = 1
            m["review"]["state"] = "CREATE_IN_FLIGHT"
            atomic_write_json(manifest_path, m)
            return

        if self.scenario == "review_unknown":
            m["review"]["create_attempts"] = 1
            m["review"]["state"] = "REVIEW_UNKNOWN"
            atomic_write_json(manifest_path, m)
            return

        if self.scenario == "exception_after_attempt":
            m["review"]["create_attempts"] = 1
            m["review"]["state"] = "REVIEW_UNKNOWN"
            atomic_write_json(manifest_path, m)
            raise RuntimeError("fake connector crashed after attempt")

        raise ValueError(f"unknown fake scenario: {self.scenario}")


class FakeTelegramSender:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.sent: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.sent.append(payload)
        if self.should_fail:
            return {"status": "FAILED", "error": "fake delivery failure"}
        return {"status": "SENT"}


PASS_VERIFIER = pipeline.make_fake_verifier("PASS")
BLOCKED_VERIFIER = pipeline.make_fake_verifier("BLOCKED", reason="fake block")


def write_fake_renderer(path: Path, mode: str) -> None:
    if mode == "missing_output":
        body = "import sys\nsys.exit(0)\n"
    elif mode == "wrong_dimensions":
        body = textwrap.dedent(
            """
            import sys
            from PIL import Image
            args = sys.argv[1:]
            out = args[args.index("--output") + 1]
            Image.new("RGB", (800, 600), (0, 0, 0)).save(out, "PNG")
            sys.exit(0)
            """
        )
    elif mode == "nonzero_exit":
        body = 'import sys\nsys.stderr.write("boom\\n")\nsys.exit(1)\n'
    else:
        raise ValueError(mode)

    path.write_text(body, encoding="utf-8")


class StoryPipelineTestCase(unittest.TestCase):
    """Base class: isolates nullone_bridge_common.WORKSPACE to a temp dir."""

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

    def manifests_dir(self) -> Path:
        d = self.tmp_path / "social/ops/manifests"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run_pipeline(self, candidate=None, writer=None, verifier=PASS_VERIFIER, connector=None, sender=None):
        candidate = candidate or make_candidate()
        writer = writer or make_writer(DEFAULT_SPEC)
        connector = connector or FakeDraftConnector("success")
        return pipeline.run_story_pipeline(
            candidate,
            writer=writer,
            verifier=verifier,
            draft_connector=connector,
            telegram_sender=sender,
        )


# ---------------------------------------------------------------------------
# Trigger adapter (#32 cadence)
# ---------------------------------------------------------------------------


class CadenceTriggerTests(unittest.TestCase):
    def base_response(self, **overrides):
        response = {
            "schema": "nullone.cadence-contract.v1",
            "recommendation": "PREPARE_STORY",
            "reason_code": "STORY_GAP",
            "permitted_action": "CANDIDATE_SEARCH_AND_PREPARE",
        }
        response.update(overrides)
        return response

    def test_no_action_is_rejected(self):
        response = self.base_response(
            recommendation="NO_ACTION", reason_code="TARGETS_MET", permitted_action="NONE"
        )
        with self.assertRaises(pipeline.StoryTriggerRejected):
            pipeline.validate_cadence_trigger(response)

    def test_prepare_main_candidate_is_rejected(self):
        response = self.base_response(
            recommendation="PREPARE_MAIN_CANDIDATE", reason_code="MAIN_GAP"
        )
        with self.assertRaises(pipeline.StoryTriggerRejected):
            pipeline.validate_cadence_trigger(response)

    def test_malformed_schema_is_rejected(self):
        with self.assertRaises(pipeline.StoryTriggerRejected):
            pipeline.validate_cadence_trigger({"recommendation": "PREPARE_STORY"})

    def test_malformed_non_dict_is_rejected(self):
        with self.assertRaises(pipeline.StoryTriggerRejected):
            pipeline.validate_cadence_trigger("PREPARE_STORY")

    def test_incompatible_reason_code_is_rejected(self):
        response = self.base_response(reason_code="MAIN_GAP")
        with self.assertRaises(pipeline.StoryTriggerRejected):
            pipeline.validate_cadence_trigger(response)

    def test_prepare_story_is_accepted(self):
        response = self.base_response()
        self.assertEqual(pipeline.validate_cadence_trigger(response), response)


# ---------------------------------------------------------------------------
# Candidate eligibility
# ---------------------------------------------------------------------------


class CandidateEligibilityTests(StoryPipelineTestCase):
    def test_candidate_not_verified_is_blocked(self):
        candidate = make_candidate(verification="PARTIAL")
        result = self.run_pipeline(candidate=candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_candidate_missing_required_field_is_blocked(self):
        candidate = make_candidate()
        del candidate["source_attribution"]
        result = self.run_pipeline(candidate=candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_candidate_invalid_content_type_is_blocked(self):
        candidate = make_candidate(content_type="NOT_A_TYPE")
        result = self.run_pipeline(candidate=candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_candidate_empty_evidence_refs_is_blocked(self):
        candidate = make_candidate(evidence_refs=[])
        result = self.run_pipeline(candidate=candidate)
        self.assertEqual(result.outcome, "CANDIDATE_NOT_ELIGIBLE")

    def test_writer_context_excludes_mechanics(self):
        candidate = make_candidate(candidate_id="secret-mechanics-check")
        context = pipeline.build_writer_context(candidate)
        forbidden_keys = {
            "candidate_id",
            "verification",
            "manifest_id",
            "review",
            "publication",
            "story_version_id",
        }
        self.assertFalse(forbidden_keys & context.keys())


# ---------------------------------------------------------------------------
# Writer output shape / self-certification
# ---------------------------------------------------------------------------


class WriterOutputTests(StoryPipelineTestCase):
    def test_invalid_layout_is_blocked(self):
        result = self.run_pipeline(writer=make_writer({}, raise_invalid=True))
        self.assertEqual(result.outcome, "WRITER_OUTPUT_INVALID")

    def test_missing_headline_is_blocked(self):
        spec = dict(DEFAULT_SPEC)
        spec["headline"] = ""
        result = self.run_pipeline(writer=make_writer(spec))
        self.assertEqual(result.outcome, "WRITER_OUTPUT_INVALID")

    def test_writer_cannot_self_certify_pass(self):
        spec = dict(DEFAULT_SPEC)
        spec["verification"] = "PASS"
        spec["final_verification"] = {"status": "PASS", "reason": "trust me"}
        result = self.run_pipeline(writer=make_writer(spec), verifier=BLOCKED_VERIFIER)
        self.assertEqual(result.outcome, "VERIFICATION_BLOCKED")


# ---------------------------------------------------------------------------
# Final verification
# ---------------------------------------------------------------------------


class VerificationTests(StoryPipelineTestCase):
    def test_verifier_blocked_stops_before_render(self):
        result = self.run_pipeline(verifier=BLOCKED_VERIFIER)
        self.assertEqual(result.outcome, "VERIFICATION_BLOCKED")
        self.assertEqual(list((self.tmp_path / "social/ops/manifests").glob("*.json")) if (self.tmp_path / "social/ops/manifests").exists() else [], [])

    def test_numeric_scope_verifier_blocks_unsupported_number(self):
        candidate = make_candidate(evidence_refs=["Model daha sürətlidir, amma faiz açıqlanmayıb."])
        spec = dict(DEFAULT_SPEC)
        result = self.run_pipeline(candidate=candidate, writer=make_writer(spec), verifier=pipeline.numeric_scope_verifier)
        self.assertEqual(result.outcome, "VERIFICATION_BLOCKED")

    def test_numeric_scope_verifier_passes_supported_number(self):
        result = self.run_pipeline(verifier=pipeline.numeric_scope_verifier)
        # Renderer + manifest + fake draft connector should all succeed.
        self.install_real_renderer()
        result = self.run_pipeline(verifier=pipeline.numeric_scope_verifier)
        self.assertEqual(result.outcome, "DRAFT_CREATED")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class RenderTests(StoryPipelineTestCase):
    def _install_fake_renderer(self, mode: str):
        tools_dir = self.tmp_path / "social/tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        fake_path = tools_dir / "render_story_v2.py"
        write_fake_renderer(fake_path, mode)
        return patch.object(pipeline, "_RENDERER_SCRIPT", "social/tools/render_story_v2.py")

    def test_renderer_nonzero_exit_is_render_failed(self):
        with self._install_fake_renderer("nonzero_exit"):
            result = self.run_pipeline()
        self.assertEqual(result.outcome, "RENDER_FAILED")

    def test_renderer_missing_output_is_render_failed(self):
        with self._install_fake_renderer("missing_output"):
            result = self.run_pipeline()
        self.assertEqual(result.outcome, "RENDER_FAILED")

    def test_renderer_wrong_dimensions_is_render_failed(self):
        with self._install_fake_renderer("wrong_dimensions"):
            result = self.run_pipeline()
        self.assertEqual(result.outcome, "RENDER_FAILED")

    def test_real_renderer_produces_exact_1080x1920_and_hash(self):
        self.install_real_renderer()
        result = self.run_pipeline()
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        manifest_path = self.tmp_path / f"social/ops/manifests/{result.manifest_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        media = manifest["media"][0]
        self.assertEqual((media["width"], media["height"]), (1080, 1920))
        self.assertTrue(media["sha256"])


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class ManifestTests(StoryPipelineTestCase):
    def test_manifest_shape_is_story_one_media_zero_attempts(self):
        self.install_real_renderer()
        result = self.run_pipeline()
        manifest_path = self.tmp_path / f"social/ops/manifests/{result.manifest_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["format"], "STORY")
        self.assertEqual(len(manifest["media"]), 1)
        self.assertEqual(manifest["verification"], "PASS")
        # Attempts/approval reflect the state AFTER a successful create,
        # which is expected -- check the pre-create invariants directly.
        self.assertFalse(manifest["approval"]["first_stage"])
        self.assertFalse(manifest["approval"]["final_publish"])
        self.assertEqual(manifest["publication"]["attempts"], 0)

    def test_same_version_manifest_is_not_recreated(self):
        self.install_real_renderer()
        first = self.run_pipeline()
        manifest_path = self.tmp_path / f"social/ops/manifests/{first.manifest_id}.json"
        media_path = self.tmp_path / "social/drafts/production/story" / f"{first.manifest_id}.png"
        mtime_before = media_path.stat().st_mtime_ns

        # Second call for the exact same candidate/spec must reuse the
        # existing manifest, not re-render or mint a new one -- and must
        # not call the connector again (attempt already consumed).
        connector = FakeDraftConnector("success")
        second = self.run_pipeline(connector=connector)

        self.assertEqual(second.manifest_id, first.manifest_id)
        self.assertEqual(media_path.stat().st_mtime_ns, mtime_before)
        self.assertEqual(connector.calls, 0)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")

    def test_build_story_manifest_refuses_to_overwrite(self):
        self.install_real_renderer()
        candidate = make_candidate()
        spec = dict(DEFAULT_SPEC)
        spec["schema"] = pipeline.SCHEMA
        spec["candidate_id"] = candidate["candidate_id"]
        spec["final_verification"] = {"status": "PASS", "reason": "", "verifier": "x", "checked_at": now_iso()}
        spec["story_version_id"] = pipeline.compute_story_version_id(candidate, spec)
        manifest_id = pipeline._manifest_id_for_version(candidate["candidate_id"], spec["story_version_id"])

        media = pipeline.render_story_asset(spec, manifest_id)
        pipeline.build_story_manifest(candidate, spec, media, manifest_id)

        with self.assertRaises(pipeline.StoryManifestBlocked):
            pipeline.build_story_manifest(candidate, spec, media, manifest_id)


# ---------------------------------------------------------------------------
# Draft creation / duplicate safety
# ---------------------------------------------------------------------------


class DraftCreationTests(StoryPipelineTestCase):
    def test_success_exactly_once(self):
        self.install_real_renderer()
        connector = FakeDraftConnector("success")
        result = self.run_pipeline(connector=connector)
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        self.assertEqual(connector.calls, 1)
        self.assertTrue(result.review_post_id)

    def test_reentry_after_draft_created_never_calls_connector_again(self):
        self.install_real_renderer()
        connector = FakeDraftConnector("success")
        first = self.run_pipeline(connector=connector)
        self.assertEqual(first.outcome, "DRAFT_CREATED")

        second = self.run_pipeline(connector=connector)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(second.review_post_id, first.review_post_id)
        self.assertEqual(connector.calls, 1)

    def test_create_in_flight_blocks_retry(self):
        self.install_real_renderer()
        connector = FakeDraftConnector("create_in_flight")
        first = self.run_pipeline(connector=connector)
        self.assertEqual(first.outcome, "REVIEW_DRAFT_AMBIGUOUS")

        second = self.run_pipeline(connector=connector)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 1)

    def test_review_unknown_blocks_retry(self):
        self.install_real_renderer()
        connector = FakeDraftConnector("review_unknown")
        first = self.run_pipeline(connector=connector)
        self.assertEqual(first.outcome, "REVIEW_DRAFT_AMBIGUOUS")

        second = self.run_pipeline(connector=connector)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 1)

    def test_connector_exception_after_attempt_preserves_consumed_attempt(self):
        self.install_real_renderer()
        connector = FakeDraftConnector("exception_after_attempt")
        result = self.run_pipeline(connector=connector)
        self.assertEqual(result.outcome, "REVIEW_DRAFT_AMBIGUOUS")

        manifest_path = self.tmp_path / f"social/ops/manifests/{result.manifest_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["review"]["create_attempts"], 1)
        self.assertEqual(manifest["review"]["state"], "REVIEW_UNKNOWN")

        # And a subsequent call must not retry automatically.
        second = self.run_pipeline(connector=connector)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 1)

    def test_connector_failure_cannot_cause_auto_retry_within_one_call(self):
        self.install_real_renderer()
        connector = FakeDraftConnector("exception_after_attempt")
        self.run_pipeline(connector=connector)
        # A single pipeline invocation must consume at most one attempt.
        self.assertEqual(connector.calls, 1)

    def test_blocked_before_attempt_is_retryable(self):
        self.install_real_renderer()
        blocking_connector = FakeDraftConnector("blocked_before_attempt")
        first = self.run_pipeline(connector=blocking_connector)
        self.assertEqual(first.outcome, "REVIEW_DRAFT_PREFLIGHT_BLOCKED")
        self.assertEqual(blocking_connector.calls, 1)

        success_connector = FakeDraftConnector("success")
        second = self.run_pipeline(connector=success_connector)
        self.assertEqual(second.outcome, "DRAFT_CREATED")
        self.assertEqual(success_connector.calls, 1)

    def test_attempts_already_one_blocks_before_connector_is_ever_called(self):
        self.install_real_renderer()
        candidate = make_candidate()
        spec = dict(DEFAULT_SPEC)
        spec["schema"] = pipeline.SCHEMA
        spec["candidate_id"] = candidate["candidate_id"]
        spec["final_verification"] = {"status": "PASS", "reason": "", "verifier": "x", "checked_at": now_iso()}
        spec["story_version_id"] = pipeline.compute_story_version_id(candidate, spec)
        manifest_id = pipeline._manifest_id_for_version(candidate["candidate_id"], spec["story_version_id"])
        media = pipeline.render_story_asset(spec, manifest_id)
        manifest = pipeline.build_story_manifest(candidate, spec, media, manifest_id)

        manifest_path = self.tmp_path / f"social/ops/manifests/{manifest_id}.json"
        manifest["review"]["create_attempts"] = 1
        manifest["review"]["state"] = "CREATE_IN_FLIGHT"
        atomic_write_json(manifest_path, manifest)

        connector = FakeDraftConnector("success")
        result = self.run_pipeline(candidate=candidate, writer=make_writer(spec), connector=connector)
        self.assertEqual(result.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 0)


# ---------------------------------------------------------------------------
# Telegram preview
# ---------------------------------------------------------------------------


class TelegramPreviewTests(StoryPipelineTestCase):
    def test_preview_contains_required_identity_fields(self):
        self.install_real_renderer()
        sender = FakeTelegramSender()
        result = self.run_pipeline(sender=sender)
        self.assertEqual(result.outcome, "DRAFT_CREATED")

        payload = result.preview_payload
        self.assertEqual(payload["format"], "STORY")
        self.assertEqual(payload["story_version_id"], result.story_version_id)
        self.assertEqual(payload["manifest_id"], result.manifest_id)
        self.assertEqual(payload["review_post_id"], result.review_post_id)
        self.assertTrue(payload["media_fingerprint"])
        self.assertEqual(len(sender.sent), 1)

    def test_buttons_use_legacy_value_not_typed_action(self):
        self.install_real_renderer()
        result = self.run_pipeline(sender=FakeTelegramSender())
        buttons = result.preview_payload["presentation"]["blocks"][0]["buttons"]

        self.assertEqual(len(buttons), 3)
        for button in buttons:
            self.assertIn("value", button)
            self.assertNotIn("action", button)

        values = {b["value"] for b in buttons}
        rid = result.review_post_id
        self.assertEqual(
            values,
            {f"texbrif:approve:{rid}", f"texbrif:reject:{rid}", f"texbrif:revise:{rid}"},
        )

    def test_no_public_texbrif_wording(self):
        self.install_real_renderer()
        result = self.run_pipeline(sender=FakeTelegramSender())
        payload = result.preview_payload

        user_facing_text = " ".join(
            [payload["brand"], payload["text"], payload["topic"], payload["caption_excerpt"]]
        )
        self.assertNotIn("texbrif", user_facing_text.lower())
        self.assertIn("NullOne", payload["brand"])

    def test_delivery_failure_never_retries_draft_creation(self):
        self.install_real_renderer()
        connector = FakeDraftConnector("success")
        sender = FakeTelegramSender(should_fail=True)
        result = self.run_pipeline(connector=connector, sender=sender)

        self.assertEqual(result.outcome, "DRAFT_CREATED")
        self.assertEqual(result.preview_delivery["status"], "FAILED")
        self.assertEqual(connector.calls, 1)

        manifest_path = self.tmp_path / f"social/ops/manifests/{result.manifest_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["review"]["state"], "DRAFT_CREATED")

    def test_delivery_exception_is_reported_not_raised(self):
        self.install_real_renderer()

        class ExplodingSender:
            def send(self, payload):
                raise RuntimeError("telegram is down")

        result = self.run_pipeline(sender=ExplodingSender())
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        self.assertEqual(result.preview_delivery["status"], "FAILED")


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------


class RevisionTests(StoryPipelineTestCase):
    def test_revision_creates_new_version_and_preserves_old_manifest(self):
        self.install_real_renderer()
        original = self.run_pipeline(sender=FakeTelegramSender())
        self.assertEqual(original.outcome, "DRAFT_CREATED")

        original_manifest_path = self.tmp_path / f"social/ops/manifests/{original.manifest_id}.json"
        original_bytes_before = original_manifest_path.read_bytes()

        candidate = make_candidate()
        revised_candidate = pipeline.build_revision_candidate(
            candidate,
            operator_instruction="Faizi vurğula, başlığı qısalt.",
            parent_manifest_id=original.manifest_id,
            parent_review_post_id=original.review_post_id,
        )
        revised_spec = dict(DEFAULT_SPEC)
        revised_spec["headline"] = "42% sürət artımı"

        revision_connector = FakeDraftConnector("success")
        revised = pipeline.run_story_pipeline(
            revised_candidate,
            writer=make_writer(revised_spec),
            verifier=PASS_VERIFIER,
            draft_connector=revision_connector,
            telegram_sender=FakeTelegramSender(),
        )

        self.assertEqual(revised.outcome, "DRAFT_CREATED")
        self.assertNotEqual(revised.story_version_id, original.story_version_id)
        self.assertNotEqual(revised.manifest_id, original.manifest_id)
        self.assertNotEqual(revised.review_post_id, original.review_post_id)

        # Old manifest is byte-for-byte unchanged.
        self.assertEqual(original_manifest_path.read_bytes(), original_bytes_before)

        revised_manifest = json.loads(
            (self.tmp_path / f"social/ops/manifests/{revised.manifest_id}.json").read_text()
        )
        self.assertFalse(revised_manifest["approval"]["first_stage"])
        self.assertFalse(revised_manifest["approval"]["final_publish"])
        self.assertEqual(revised_manifest["publication"]["attempts"], 0)
        self.assertEqual(revised_manifest["review"]["create_attempts"], 1)

    def test_blocked_revision_never_creates_review_draft(self):
        self.install_real_renderer()
        candidate = make_candidate()
        revised_candidate = pipeline.build_revision_candidate(
            candidate,
            operator_instruction="Şişirt.",
            parent_manifest_id="m-parent",
            parent_review_post_id="r-parent",
        )

        connector = FakeDraftConnector("success")
        result = pipeline.run_story_pipeline(
            revised_candidate,
            writer=make_writer(DEFAULT_SPEC),
            verifier=BLOCKED_VERIFIER,
            draft_connector=connector,
            telegram_sender=FakeTelegramSender(),
        )

        self.assertEqual(result.outcome, "VERIFICATION_BLOCKED")
        self.assertEqual(connector.calls, 0)
        manifests_dir = self.tmp_path / "social/ops/manifests"
        self.assertEqual(list(manifests_dir.glob("*.json")) if manifests_dir.exists() else [], [])


# ---------------------------------------------------------------------------
# End-to-end offline acceptance (issue #33 section 24)
# ---------------------------------------------------------------------------


class EndToEndOfflineAcceptanceTests(StoryPipelineTestCase):
    def test_full_offline_path_cadence_to_preview(self):
        self.install_real_renderer()

        cadence_response = {
            "schema": "nullone.cadence-contract.v1",
            "contract_version": "1.0.0",
            "recommendation": "PREPARE_STORY",
            "reason_code": "STORY_GAP",
            "reason_text": "Story load is below today's guidance.",
            "permitted_action": "CANDIDATE_SEARCH_AND_PREPARE",
            "daypart": "AFTERNOON",
        }

        candidate = make_candidate(candidate_id="e2e-candidate-1")
        connector = FakeDraftConnector("success")
        sender = FakeTelegramSender()

        result = pipeline.prepare_story_from_cadence_recommendation(
            cadence_response,
            candidate,
            writer=make_writer(DEFAULT_SPEC),
            verifier=pipeline.numeric_scope_verifier,
            draft_connector=connector,
            telegram_sender=sender,
        )

        self.assertEqual(result.outcome, "DRAFT_CREATED")
        self.assertEqual(connector.calls, 1)
        self.assertEqual(len(sender.sent), 1)

        manifest_path = self.tmp_path / f"social/ops/manifests/{result.manifest_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "STORY")
        self.assertEqual((manifest["media"][0]["width"], manifest["media"][0]["height"]), (1080, 1920))
        self.assertEqual(manifest["review"]["state"], "DRAFT_CREATED")

        buttons = result.preview_payload["presentation"]["blocks"][0]["buttons"]
        self.assertEqual(len(buttons), 3)


# ---------------------------------------------------------------------------
# Publication capability-negative
# ---------------------------------------------------------------------------


class PublicationCapabilityNegativeTests(unittest.TestCase):
    def test_module_source_has_no_publication_capability(self):
        source = (SCRIPTS / "nullone_story_pipeline.py").read_text(encoding="utf-8")
        forbidden_substrings = (
            "nullone-publish-bridge",
            "nullone-publisher-run",
            "publish_now",
            "posts_publish_now",
            "posts_delete",
            "posts_unpublish_post",
            "schedule",
            "scheduled_for",
        )
        for forbidden in forbidden_substrings:
            self.assertNotIn(
                forbidden,
                source,
                msg=f"Story pipeline module must never reference {forbidden!r}",
            )

    def test_module_does_not_import_publisher_modules(self):
        module_names = set(sys.modules.keys())
        self.assertNotIn("nullone-publish-bridge", module_names)
        self.assertNotIn("nullone-publisher-run", module_names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
