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
import multiprocessing
import shutil
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

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
    def _writer(editorial_context):
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
    "use_source_image": False,
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
        media = payload["media"]
        media_path = bridge_common.resolve_workspace_path(media["local_path"])
        self_test = unittest.TestCase()
        self_test.assertTrue(media_path.is_file())
        self_test.assertEqual(bridge_common.sha256_file(media_path), media["sha256"])
        with Image.open(media_path) as image:
            self_test.assertEqual(image.size, (media["width"], media["height"]))
        self_test.assertEqual((media["width"], media["height"]), (1080, 1920))
        manifest_path = bridge_common.resolve_workspace_path(
            f"social/ops/manifests/{payload['manifest_id']}.json"
        )
        _, manifest = load_manifest(manifest_path)
        self_test.assertEqual(media, {
            key: manifest["media"][0][key]
            for key in ("local_path", "sha256", "width", "height", "content_type")
        })
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

    def source_image(self, name="source.png") -> str:
        path = self.tmp_path / "social/source-assets" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1600, 900), (20, 30, 40)).save(path, "PNG")
        return bridge_common.workspace_relative(path)

    def persist_final_spec(self, candidate=None, story_fields=None):
        candidate = candidate or make_candidate()
        fields = dict(story_fields or DEFAULT_SPEC)
        request_id = pipeline.compute_story_request_id(candidate)
        fields.update({
            "schema": pipeline.SCHEMA,
            "contract_version": pipeline.CONTRACT_VERSION,
            "story_request_id": request_id,
            "candidate_id": candidate["candidate_id"],
            "candidate_version": candidate.get("candidate_version"),
            "revision_of": candidate.get("revision_of"),
            "source_image": (
                pipeline._candidate_source_image(candidate)
                if fields.get("use_source_image")
                else None
            ),
            "evidence_refs": list(candidate["evidence_refs"]),
            "final_verification": {
                "status": "PASS",
                "reason": "test",
                "verifier": "test",
                "checked_at": now_iso(),
            },
        })
        fields["story_version_id"] = pipeline.compute_story_version_id(candidate, fields)
        path = pipeline._story_spec_path(request_id)
        atomic_write_json(path, fields)
        return fields, path

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
        source_path = self.source_image("private-source.png")
        candidate = make_candidate(
            candidate_id="secret-mechanics-check", source_image=source_path
        )
        context = pipeline.build_writer_context(candidate)
        forbidden_keys = {
            "candidate_id",
            "verification",
            "manifest_id",
            "review",
            "publication",
            "story_version_id",
            "source_image",
        }
        self.assertFalse(forbidden_keys & context.keys())
        self.assertIs(context["source_image_available"], True)
        self.assertNotIn(source_path, json.dumps(context))


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

    def test_writer_provider_exception_is_typed(self):
        def writer(_context):
            raise RuntimeError("provider unavailable")

        result = self.run_pipeline(writer=writer)
        self.assertEqual(result.outcome, "WRITER_FAILED")
        self.assertEqual(result.context["error_type"], "RuntimeError")

    def test_writer_non_mapping_is_invalid_not_provider_failure(self):
        result = self.run_pipeline(writer=lambda _context: "bad output")
        self.assertEqual(result.outcome, "WRITER_OUTPUT_INVALID")

    def test_image_available_and_writer_selects_use(self):
        self.install_real_renderer()
        source_image = self.source_image()
        spec = dict(DEFAULT_SPEC, use_source_image=True)
        result = self.run_pipeline(
            candidate=make_candidate(source_image=source_image),
            writer=make_writer(spec),
        )
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        persisted = json.loads(
            bridge_common.resolve_workspace_path(result.story_spec_path).read_text()
        )
        self.assertEqual(persisted["source_image"], source_image)

    def test_image_available_and_writer_declines(self):
        self.install_real_renderer()
        result = self.run_pipeline(
            candidate=make_candidate(source_image=self.source_image()),
            writer=make_writer(dict(DEFAULT_SPEC, use_source_image=False)),
        )
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        persisted = json.loads(
            bridge_common.resolve_workspace_path(result.story_spec_path).read_text()
        )
        self.assertIsNone(persisted["source_image"])

    def test_no_image_and_writer_requests_one_is_blocked(self):
        result = self.run_pipeline(
            writer=make_writer(dict(DEFAULT_SPEC, use_source_image=True))
        )
        self.assertEqual(result.outcome, "WRITER_OUTPUT_INVALID")

    def test_real_writer_schema_exposes_semantic_image_choice_only(self):
        properties = pipeline.WRITER_SCHEMA["properties"]
        self.assertEqual(properties["use_source_image"], {"type": "boolean"})
        self.assertIn("use_source_image", pipeline.WRITER_SCHEMA["required"])
        self.assertNotIn("source_image", properties)

    def test_writer_cannot_invent_source_image_path(self):
        spec = dict(DEFAULT_SPEC, source_image="/private/source.png")
        result = self.run_pipeline(writer=make_writer(spec))
        self.assertEqual(result.outcome, "WRITER_OUTPUT_INVALID")


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

    def test_verifier_exception_is_typed_and_never_renders(self):
        def verifier(_spec, _candidate):
            raise RuntimeError("verifier unavailable")

        result = self.run_pipeline(verifier=verifier)
        self.assertEqual(result.outcome, "VERIFIER_FAILED")
        self.assertFalse((self.tmp_path / "social/ops/manifests").exists())

    def test_verifier_malformed_result_is_typed(self):
        result = self.run_pipeline(verifier=lambda _spec, _candidate: {})
        self.assertEqual(result.outcome, "VERIFIER_FAILED")


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
# Request identity / finalized-spec persistence
# ---------------------------------------------------------------------------


class StoryRequestPersistenceTests(StoryPipelineTestCase):
    def test_finalized_spec_is_persisted_before_render(self):
        result = self.run_pipeline()
        self.assertEqual(result.outcome, "RENDER_FAILED")
        spec_path = bridge_common.resolve_workspace_path(result.story_spec_path)
        spec = json.loads(spec_path.read_text())
        self.assertEqual(spec["schema"], "nullone.story-spec.v1")
        self.assertEqual(spec["contract_version"], "1.0.0")
        self.assertEqual(spec["story_request_id"], result.story_request_id)
        self.assertEqual(spec["story_version_id"], result.story_version_id)
        self.assertEqual(spec["candidate_id"], "cand-001")
        self.assertEqual(spec["final_verification"]["status"], "PASS")

    def test_same_request_reuses_spec_before_writer_and_verifier(self):
        calls = {"writer": 0, "verifier": 0}
        specs = [dict(DEFAULT_SPEC), dict(DEFAULT_SPEC, headline="Different retry")]

        def writer(_context):
            result = specs[calls["writer"]]
            calls["writer"] += 1
            return result

        def verifier(_spec, _candidate):
            calls["verifier"] += 1
            return {"status": "PASS", "reason": "ok"}

        first = self.run_pipeline(writer=writer, verifier=verifier)
        second = self.run_pipeline(writer=writer, verifier=verifier)
        self.assertEqual(first.outcome, "RENDER_FAILED")
        self.assertEqual(second.outcome, "RENDER_FAILED")
        self.assertEqual(calls, {"writer": 1, "verifier": 1})
        self.assertEqual(first.story_request_id, second.story_request_id)
        self.assertEqual(first.story_version_id, second.story_version_id)

    def test_render_failure_retry_reuses_exact_spec_then_succeeds(self):
        writer_calls = 0

        def writer(_context):
            nonlocal writer_calls
            writer_calls += 1
            return dict(DEFAULT_SPEC)

        first = self.run_pipeline(writer=writer)
        self.assertEqual(first.outcome, "RENDER_FAILED")
        persisted_before = bridge_common.resolve_workspace_path(
            first.story_spec_path
        ).read_bytes()
        self.install_real_renderer()
        second = self.run_pipeline(writer=writer)
        self.assertEqual(second.outcome, "DRAFT_CREATED")
        self.assertEqual(writer_calls, 1)
        self.assertEqual(
            bridge_common.resolve_workspace_path(second.story_spec_path).read_bytes(),
            persisted_before,
        )

    def test_manifest_failure_retry_reuses_exact_spec(self):
        self.install_real_renderer()
        writer_calls = 0

        def writer(_context):
            nonlocal writer_calls
            writer_calls += 1
            return dict(DEFAULT_SPEC)

        real_builder = pipeline.build_story_manifest
        with patch.object(
            pipeline,
            "build_story_manifest",
            side_effect=pipeline.StoryManifestBlocked("synthetic manifest failure"),
        ):
            first = self.run_pipeline(writer=writer)
        self.assertEqual(first.outcome, "MANIFEST_BLOCKED")
        second = self.run_pipeline(writer=writer)
        self.assertEqual(second.outcome, "DRAFT_CREATED")
        self.assertEqual(writer_calls, 1)
        self.assertEqual(first.story_version_id, second.story_version_id)
        self.assertIs(pipeline.build_story_manifest, real_builder)

    def test_candidate_version_distinguishes_genuine_upstream_revision(self):
        first = pipeline.compute_story_request_id(make_candidate(candidate_version="v1"))
        second = pipeline.compute_story_request_id(make_candidate(candidate_version="v2"))
        self.assertNotEqual(first, second)

    def test_selected_source_image_must_still_match_persisted_request(self):
        source_image = self.source_image()
        candidate = make_candidate(source_image=source_image)
        first = self.run_pipeline(
            candidate=candidate,
            writer=make_writer(dict(DEFAULT_SPEC, use_source_image=True)),
        )
        self.assertEqual(first.outcome, "RENDER_FAILED")
        bridge_common.resolve_workspace_path(source_image).unlink()

        writer_calls = 0

        def writer(_context):
            nonlocal writer_calls
            writer_calls += 1
            return dict(DEFAULT_SPEC)

        second = self.run_pipeline(candidate=candidate, writer=writer)
        self.assertEqual(second.outcome, "STORY_SPEC_BLOCKED")
        self.assertEqual(writer_calls, 0)


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
        self.assertEqual(manifest["story_request_id"], result.story_request_id)
        spec_path = bridge_common.resolve_workspace_path(result.story_spec_path)
        self.assertEqual(manifest["story_spec"]["file"], result.story_spec_path)
        self.assertEqual(
            manifest["story_spec"]["sha256"],
            bridge_common.sha256_bytes(spec_path.read_bytes()),
        )

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
        spec, spec_path = self.persist_final_spec(candidate)
        manifest_id = pipeline._manifest_id_for_version(candidate["candidate_id"], spec["story_version_id"])

        media = pipeline.render_story_asset(spec, manifest_id)
        pipeline.build_story_manifest(candidate, spec, media, manifest_id, spec_path)

        with self.assertRaises(pipeline.StoryManifestBlocked):
            pipeline.build_story_manifest(candidate, spec, media, manifest_id, spec_path)


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
        calls = 0

        def changing_writer(_context):
            nonlocal calls
            calls += 1
            return dict(DEFAULT_SPEC, headline=f"Version {calls}")

        first = self.run_pipeline(connector=connector, writer=changing_writer)
        self.assertEqual(first.outcome, "DRAFT_CREATED")

        second = self.run_pipeline(connector=connector, writer=changing_writer)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(second.review_post_id, first.review_post_id)
        self.assertEqual(connector.calls, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(second.story_version_id, first.story_version_id)

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
        calls = 0

        def changing_writer(_context):
            nonlocal calls
            calls += 1
            return dict(DEFAULT_SPEC, headline=f"Version {calls}")

        first = self.run_pipeline(connector=connector, writer=changing_writer)
        self.assertEqual(first.outcome, "REVIEW_DRAFT_AMBIGUOUS")

        second = self.run_pipeline(connector=connector, writer=changing_writer)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(second.story_version_id, first.story_version_id)

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
        writer_calls = 0

        def writer(_context):
            nonlocal writer_calls
            writer_calls += 1
            return dict(DEFAULT_SPEC, headline=f"First spec {writer_calls}")

        blocking_connector = FakeDraftConnector("blocked_before_attempt")
        first = self.run_pipeline(connector=blocking_connector, writer=writer)
        self.assertEqual(first.outcome, "REVIEW_DRAFT_BLOCKED_BEFORE_ATTEMPT")
        self.assertEqual(blocking_connector.calls, 1)

        success_connector = FakeDraftConnector("success")
        second = self.run_pipeline(connector=success_connector, writer=writer)
        self.assertEqual(second.outcome, "DRAFT_CREATED")
        self.assertEqual(success_connector.calls, 1)
        self.assertEqual(writer_calls, 1)
        self.assertEqual(first.story_version_id, second.story_version_id)

    def test_attempts_already_one_blocks_before_connector_is_ever_called(self):
        self.install_real_renderer()
        candidate = make_candidate()
        spec, spec_path = self.persist_final_spec(candidate)
        manifest_id = pipeline._manifest_id_for_version(candidate["candidate_id"], spec["story_version_id"])
        media = pipeline.render_story_asset(spec, manifest_id)
        manifest = pipeline.build_story_manifest(
            candidate, spec, media, manifest_id, spec_path
        )

        manifest_path = self.tmp_path / f"social/ops/manifests/{manifest_id}.json"
        manifest["review"]["create_attempts"] = 1
        manifest["review"]["state"] = "CREATE_IN_FLIGHT"
        atomic_write_json(manifest_path, manifest)

        connector = FakeDraftConnector("success")
        result = self.run_pipeline(candidate=candidate, writer=make_writer(spec), connector=connector)
        self.assertEqual(result.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 0)

    def test_concurrent_same_request_calls_connector_at_most_once(self):
        self.install_real_renderer()
        connector = FakeDraftConnector("success")
        writer_calls = 0
        writer_entered = threading.Event()

        def slow_writer(_context):
            nonlocal writer_calls
            writer_calls += 1
            writer_entered.set()
            time.sleep(0.1)
            return dict(DEFAULT_SPEC)

        results = []

        def invoke():
            results.append(self.run_pipeline(writer=slow_writer, connector=connector))

        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        first.start()
        self.assertTrue(writer_entered.wait(timeout=2))
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(connector.calls, 1)
        self.assertEqual(writer_calls, 1)
        self.assertEqual(
            {result.outcome for result in results},
            {"DRAFT_CREATED", "REVIEW_DRAFT_ALREADY_CONSUMED"},
        )
        self.assertEqual(len({result.story_request_id for result in results}), 1)
        self.assertEqual(len({result.story_version_id for result in results}), 1)

    def test_interprocess_same_request_calls_connector_at_most_once(self):
        self.install_real_renderer()
        process_context = multiprocessing.get_context("fork")
        connector_calls = process_context.Value("i", 0)
        writer_calls = process_context.Value("i", 0)
        results = process_context.Queue()

        class SharedConnector:
            def create_review_draft(self, manifest_path):
                with connector_calls.get_lock():
                    connector_calls.value += 1
                _, manifest = load_manifest(manifest_path)
                manifest["review"].update(
                    {
                        "create_attempts": 1,
                        "state": "DRAFT_CREATED",
                        "zernio_draft_id": "interprocess-review",
                        "created_at": now_iso(),
                    }
                )
                atomic_write_json(manifest_path, manifest)

        def slow_writer(_context):
            with writer_calls.get_lock():
                writer_calls.value += 1
            time.sleep(0.2)
            return dict(DEFAULT_SPEC)

        def invoke():
            result = pipeline.run_story_pipeline(
                make_candidate(),
                writer=slow_writer,
                verifier=PASS_VERIFIER,
                draft_connector=SharedConnector(),
            )
            results.put(
                (result.outcome, result.story_request_id, result.story_version_id)
            )

        processes = [process_context.Process(target=invoke) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)

        observed = [results.get(timeout=2) for _ in processes]
        self.assertEqual(connector_calls.value, 1)
        self.assertEqual(writer_calls.value, 1)
        self.assertEqual(
            {outcome for outcome, _request_id, _version_id in observed},
            {"DRAFT_CREATED", "REVIEW_DRAFT_ALREADY_CONSUMED"},
        )
        self.assertEqual(len({request_id for _, request_id, _ in observed}), 1)
        self.assertEqual(len({version_id for _, _, version_id in observed}), 1)


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
        self.assertEqual(payload["story_request_id"], result.story_request_id)
        self.assertEqual(payload["manifest_id"], result.manifest_id)
        self.assertEqual(payload["review_post_id"], result.review_post_id)
        self.assertTrue(payload["media"]["sha256"])
        self.assertEqual(payload["media"]["width"], 1080)
        self.assertEqual(payload["media"]["height"], 1920)
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
        writer_calls = 0

        def writer(_context):
            nonlocal writer_calls
            writer_calls += 1
            return dict(DEFAULT_SPEC, headline=f"Attempt {writer_calls}")

        result = self.run_pipeline(connector=connector, sender=sender, writer=writer)

        self.assertEqual(result.outcome, "PREVIEW_DELIVERY_FAILED")
        self.assertEqual(result.preview_delivery["status"], "FAILED")
        self.assertEqual(connector.calls, 1)

        manifest_path = self.tmp_path / f"social/ops/manifests/{result.manifest_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["review"]["state"], "DRAFT_CREATED")
        self.assertEqual(manifest["publication"]["attempts"], 0)

        second = self.run_pipeline(connector=connector, sender=sender, writer=writer)
        self.assertEqual(second.outcome, "REVIEW_DRAFT_ALREADY_CONSUMED")
        self.assertEqual(connector.calls, 1)
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(writer_calls, 1)

    def test_delivery_exception_is_reported_not_raised(self):
        self.install_real_renderer()

        class ExplodingSender:
            def send(self, payload):
                raise RuntimeError("telegram is down")

        result = self.run_pipeline(sender=ExplodingSender())
        self.assertEqual(result.outcome, "PREVIEW_DELIVERY_FAILED")
        self.assertEqual(result.preview_delivery["status"], "FAILED")
        self.assertTrue(result.manifest_id)
        self.assertTrue(result.story_version_id)
        self.assertTrue(result.review_post_id)
        self.assertIsNotNone(result.preview_payload)

    def test_arbitrary_sender_mapping_is_not_success(self):
        self.install_real_renderer()

        class AmbiguousSender:
            def send(self, payload):
                return {"message_id": "not-a-success-contract"}

        result = self.run_pipeline(sender=AmbiguousSender())
        self.assertEqual(result.outcome, "PREVIEW_DELIVERY_FAILED")
        self.assertEqual(result.preview_delivery["status"], "FAILED")

    def test_sent_sender_preserves_draft_created_outcome(self):
        self.install_real_renderer()
        result = self.run_pipeline(sender=FakeTelegramSender())
        self.assertEqual(result.outcome, "DRAFT_CREATED")
        self.assertEqual(result.preview_delivery, {"status": "SENT"})

    def test_preview_media_exactly_matches_manifest(self):
        self.install_real_renderer()
        result = self.run_pipeline(sender=FakeTelegramSender())
        manifest = json.loads(
            bridge_common.resolve_workspace_path(result.manifest_path).read_text()
        )
        expected = {
            key: manifest["media"][0][key]
            for key in ("local_path", "sha256", "width", "height", "content_type")
        }
        self.assertEqual(result.preview_payload["media"], expected)
        path = bridge_common.resolve_workspace_path(expected["local_path"])
        self.assertTrue(path.is_file())
        self.assertEqual(bridge_common.sha256_file(path), expected["sha256"])


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------


class RevisionTests(StoryPipelineTestCase):
    def _create_parent(self):
        self.install_real_renderer()
        candidate = make_candidate()
        original = self.run_pipeline(candidate=candidate, sender=FakeTelegramSender())
        self.assertEqual(original.outcome, "DRAFT_CREATED")
        path = bridge_common.resolve_workspace_path(original.manifest_path)
        return candidate, original, path

    def _revision(self, candidate, original, **overrides):
        values = {
            "operator_instruction": "Faizi vurğula, başlığı qısalt.",
            "parent_manifest_id": original.manifest_id,
            "parent_review_post_id": original.review_post_id,
        }
        values.update(overrides)
        return pipeline.build_revision_candidate(candidate, **values)

    def test_revision_creates_new_version_and_preserves_old_manifest(self):
        candidate, original, original_manifest_path = self._create_parent()
        original_bytes_before = original_manifest_path.read_bytes()

        revised_candidate = self._revision(candidate, original)
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
        self.assertNotEqual(revised.story_request_id, original.story_request_id)
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
        revised_spec = json.loads(
            bridge_common.resolve_workspace_path(revised.story_spec_path).read_text()
        )
        self.assertEqual(
            revised_spec["revision_of"]["parent_manifest_id"], original.manifest_id
        )
        self.assertEqual(
            revised_spec["revision_of"]["parent_review_post_id"], original.review_post_id
        )

    def test_missing_revision_parent_is_typed_and_never_calls_writer(self):
        self.install_real_renderer()
        candidate = make_candidate()
        revised_candidate = pipeline.build_revision_candidate(
            candidate,
            operator_instruction="Şişirt.",
            parent_manifest_id="m-parent",
            parent_review_post_id="r-parent",
        )

        writer_calls = 0

        def writer(_context):
            nonlocal writer_calls
            writer_calls += 1
            return dict(DEFAULT_SPEC)

        connector = FakeDraftConnector("success")
        result = pipeline.run_story_pipeline(
            revised_candidate,
            writer=writer,
            verifier=BLOCKED_VERIFIER,
            draft_connector=connector,
            telegram_sender=FakeTelegramSender(),
        )

        self.assertEqual(result.outcome, "REVISION_PARENT_INVALID")
        self.assertEqual(connector.calls, 0)
        self.assertEqual(writer_calls, 0)
        manifests_dir = self.tmp_path / "social/ops/manifests"
        self.assertEqual(list(manifests_dir.glob("*.json")) if manifests_dir.exists() else [], [])

    def test_parent_review_post_mismatch_is_blocked(self):
        candidate, original, _path = self._create_parent()
        revision = self._revision(
            candidate, original, parent_review_post_id="wrong-review-id"
        )
        result = self.run_pipeline(candidate=revision)
        self.assertEqual(result.outcome, "REVISION_PARENT_INVALID")

    def test_parent_candidate_mismatch_is_blocked(self):
        _candidate, original, _path = self._create_parent()
        revision = self._revision(
            make_candidate(candidate_id="different-candidate"), original
        )
        result = self.run_pipeline(candidate=revision)
        self.assertEqual(result.outcome, "REVISION_PARENT_INVALID")

    def test_non_story_parent_is_blocked(self):
        candidate, original, path = self._create_parent()
        manifest = json.loads(path.read_text())
        feed_path = self.tmp_path / "social/drafts/production/feed-parent.png"
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1080, 1350), (1, 2, 3)).save(feed_path, "PNG")
        manifest["format"] = "FEED"
        manifest["media"] = [bridge_common.inspect_media(feed_path, "FEED")]
        atomic_write_json(path, manifest)
        revision = self._revision(candidate, original)
        result = self.run_pipeline(candidate=revision)
        self.assertEqual(result.outcome, "REVISION_PARENT_INVALID")
        self.assertIn("not a Story", result.reason_text)

    def test_published_or_attempted_parent_is_blocked(self):
        candidate, original, path = self._create_parent()
        for state in ("UNKNOWN", "PUBLISHED"):
            with self.subTest(state=state):
                manifest = json.loads(path.read_text())
                manifest["publication"]["attempts"] = 1
                manifest["publication"]["state"] = state
                manifest["publication"]["live_zernio_post_id"] = "live-parent"
                atomic_write_json(path, manifest)
                revision = self._revision(candidate, original)
                result = self.run_pipeline(candidate=revision)
                self.assertEqual(result.outcome, "REVISION_PARENT_INVALID")
                self.assertIn("published, attempted", result.reason_text)

    def test_empty_operator_instruction_is_blocked(self):
        candidate, original, _path = self._create_parent()
        revision = self._revision(candidate, original, operator_instruction="   ")
        result = self.run_pipeline(candidate=revision)
        self.assertEqual(result.outcome, "REVISION_PARENT_INVALID")


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
