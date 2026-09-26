#!/usr/bin/env python3
"""Story writer deterministic failure diagnostics (issue #157).

Persisted Story results collapsed every writer failure to generic
WRITER_FAILED + a class name. These tests prove, with injected fakes
only (no subprocess, no model, no network), that each failure mode now
carries a precise, secret-free diagnostic while the domain contract
(WRITER_FAILED / WRITER_OUTPUT_INVALID / gates) stays intact.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

import nullone_bridge_common as bridge_common  # noqa: E402
import nullone_story_pipeline as pipeline  # noqa: E402
from nullone_bridge_common import BridgeError  # noqa: E402
from nullone_opencode_story_provider import (  # noqa: E402
    OpenCodeBinaryResolutionError,
    OpenCodeStoryWriter,
    StoryWriterAuthError,
    StoryWriterPolicyBlockedError,
    StoryWriterRateLimitedError,
    StoryWriterTimeoutError,
    StoryWriterUnreachableError,
)
from support.morning_artifacts import (  # noqa: E402
    write_morning_artifacts,
)

from test_story_pipeline import (  # noqa: E402
    PASS_VERIFIER,
    FakeDraftConnector,
    FakeTelegramSender,
    make_candidate,
    make_writer,
)


class OpenCodeFakeWriter:
    """Fake writer object carrying provider identity like the real adapter."""

    def __init__(self, exc, model="opencode/muse-spark-1.3-contributor-free",
                 timeout=300):
        self._exc = exc
        self.model = model
        self._timeout = timeout

    def __call__(self, editorial_context):
        raise self._exc


class WriterDiagnosticsCase(unittest.TestCase):
    """Isolates WORKSPACE; writer raises before render (no renderer needed)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patcher = patch.object(bridge_common, "WORKSPACE", self.tmp_path)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def run_pipeline(self, writer):
        return pipeline.run_story_pipeline(
            make_candidate(),
            writer=writer,
            verifier=PASS_VERIFIER,
            draft_connector=FakeDraftConnector("success"),
            telegram_sender=FakeTelegramSender(),
        )

    def assert_diagnostics(self, result, error_class, **fields):
        self.assertEqual(result.outcome, "WRITER_FAILED")
        diag = result.context["writer_diagnostics"]
        self.assertEqual(diag["error_class"], error_class)
        for key, value in fields.items():
            self.assertEqual(diag[key], value, key)
        tag = pipeline.format_writer_diagnostic_tag(diag)
        self.assertIn(tag, result.reason_text)
        self.assertNotIn("\n", result.reason_text)
        self.assertLessEqual(len(result.reason_text), 240)
        return diag

    def test_auth_failure_classified(self):
        result = self.run_pipeline(
            OpenCodeFakeWriter(StoryWriterAuthError("x")))
        self.assert_diagnostics(
            result, "STORY_PROVIDER_AUTH_ERROR",
            failure_stage="writer-execution", transport="opencode",
            model="opencode/muse-spark-1.3-contributor-free",
            timeout_seconds=300, process_exit_code=None, retryable=False,
        )

    def test_policy_block_classified(self):
        result = self.run_pipeline(
            OpenCodeFakeWriter(StoryWriterPolicyBlockedError("x")))
        self.assert_diagnostics(
            result, "STORY_PROVIDER_POLICY_BLOCKED", retryable=False)

    def test_rate_limit_classified_retryable(self):
        result = self.run_pipeline(
            OpenCodeFakeWriter(StoryWriterRateLimitedError("x")))
        self.assert_diagnostics(
            result, "STORY_PROVIDER_RATE_LIMITED", retryable=True)

    def test_unreachable_classified_retryable(self):
        result = self.run_pipeline(
            OpenCodeFakeWriter(StoryWriterUnreachableError("x")))
        self.assert_diagnostics(
            result, "STORY_PROVIDER_UNAVAILABLE", retryable=True)

    def test_timeout_classified(self):
        result = self.run_pipeline(
            OpenCodeFakeWriter(StoryWriterTimeoutError("x")))
        self.assert_diagnostics(
            result, "STORY_PROVIDER_TIMEOUT",
            failure_stage="writer-execution", retryable=False)

    def test_binary_missing_is_startup_error(self):
        result = self.run_pipeline(
            OpenCodeFakeWriter(OpenCodeBinaryResolutionError("x")))
        self.assert_diagnostics(
            result, "STORY_PROVIDER_STARTUP_ERROR",
            failure_stage="writer-spawn")

    def test_nonzero_exit_carries_code(self):
        result = self.run_pipeline(
            OpenCodeFakeWriter(
                BridgeError("OpenCode Story writer failed (exit=3)")))
        self.assert_diagnostics(
            result, "STORY_PROVIDER_PROCESS_ERROR", process_exit_code=3)

    def test_non_json_output_classified(self):
        result = self.run_pipeline(
            OpenCodeFakeWriter(
                BridgeError("OpenCode Story writer returned non-JSON output")))
        self.assert_diagnostics(
            result, "STORY_OUTPUT_INVALID_JSON",
            failure_stage="output-parse")

    def test_unknown_error_is_internal_but_still_writer_failed(self):
        result = self.run_pipeline(OpenCodeFakeWriter(RuntimeError("boom")))
        diag = self.assert_diagnostics(result, "STORY_INTERNAL_ERROR")
        self.assertEqual(result.context["error_type"], "RuntimeError")
        # Raw message never persisted.
        self.assertNotIn("boom", result.reason_text)
        self.assertNotIn("boom", str(diag))

    def test_schema_invalid_output_keeps_contract_with_diagnostics(self):
        result = self.run_pipeline(make_writer({"layout": "nope"}))
        self.assertEqual(result.outcome, "WRITER_OUTPUT_INVALID")
        diag = result.context["writer_diagnostics"]
        self.assertEqual(diag["error_class"], "STORY_OUTPUT_CONTRACT_INVALID")
        self.assertEqual(diag["failure_stage"], "output-validation")

    def test_missing_output_is_output_missing(self):
        result = self.run_pipeline(make_writer({}))
        self.assertEqual(result.outcome, "WRITER_OUTPUT_INVALID")
        self.assertEqual(
            result.context["writer_diagnostics"]["error_class"],
            "STORY_OUTPUT_MISSING",
        )

    def test_error_classes_cover_taxonomy(self):
        for required in (
            "STORY_PROVIDER_AUTH_ERROR",
            "STORY_PROVIDER_POLICY_BLOCKED",
            "STORY_PROVIDER_RATE_LIMITED",
            "STORY_PROVIDER_UNAVAILABLE",
            "STORY_PROVIDER_TIMEOUT",
            "STORY_PROVIDER_PROCESS_ERROR",
            "STORY_PROVIDER_STARTUP_ERROR",
            "STORY_OUTPUT_MISSING",
            "STORY_OUTPUT_INVALID_JSON",
            "STORY_OUTPUT_CONTRACT_INVALID",
            "STORY_SOURCE_GATE_BLOCKED",
            "STORY_INTERNAL_ERROR",
        ):
            self.assertIn(required, pipeline.STORY_WRITER_ERROR_CLASSES)


class AdapterSignalTests(unittest.TestCase):
    """Adapter output-pattern mapping without any real subprocess."""

    def _run(self, returncode=1, stdout="", stderr=""):
        writer = OpenCodeStoryWriter(workspace=Path("/tmp"), timeout=5)
        cp = subprocess.CompletedProcess(
            ["opencode"], returncode, stdout=stdout, stderr=stderr)
        with mock.patch(
            "nullone_opencode_story_provider.resolve_opencode_binary",
            return_value="/tmp/fake-opencode",
        ), mock.patch(
            "nullone_opencode_story_provider.subprocess.run",
            return_value=cp,
        ):
            return writer({})

    def test_auth_signal(self):
        with self.assertRaises(StoryWriterAuthError):
            self._run(stderr="Error 401: unauthorized")

    def test_rate_limit_signal(self):
        with self.assertRaises(StoryWriterRateLimitedError):
            self._run(stderr="429 too many requests")

    def test_policy_signal(self):
        with self.assertRaises(StoryWriterPolicyBlockedError):
            self._run(stderr="blocked by content policy")

    def test_reachability_still_first(self):
        with self.assertRaises(StoryWriterUnreachableError):
            self._run(stderr="ENOTFOUND api.example.com")

    def test_generic_exit(self):
        with self.assertRaises(BridgeError) as ctx:
            self._run(returncode=3, stderr="something odd")
        self.assertIn("exit=3", str(ctx.exception))

    def test_timeout(self):
        writer = OpenCodeStoryWriter(workspace=Path("/tmp"), timeout=5)
        with mock.patch(
            "nullone_opencode_story_provider.resolve_opencode_binary",
            return_value="/tmp/fake-opencode",
        ), mock.patch(
            "nullone_opencode_story_provider.subprocess.run",
            side_effect=subprocess.TimeoutExpired("opencode", 5),
        ):
            with self.assertRaises(StoryWriterTimeoutError):
                writer({})

    def test_configured_model_is_executed(self):
        """Reported .model must equal the --model flag actually executed."""
        writer = OpenCodeStoryWriter(
            workspace=Path("/tmp"), model="opencode/custom-model")
        cp = subprocess.CompletedProcess(["opencode"], 0, stdout='{"a": 1}', stderr="")
        with mock.patch(
            "nullone_opencode_story_provider.resolve_opencode_binary",
            return_value="/tmp/fake-opencode",
        ), mock.patch(
            "nullone_opencode_story_provider.subprocess.run",
            return_value=cp,
        ) as run:
            writer({})
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("--model") + 1], writer.model)


class SourceGateUnchangedTests(unittest.TestCase):
    """The MORNING_SOURCE_UNPROVEN gate still holds with no morning result."""

    def test_no_morning_result_still_gated(self):
        from nullone_scheduler_invocation import compute_occurrence_id
        from nullone_story_scheduled_workflow import run_story_trigger

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trigger = {
                "schema": "nullone.scheduler-invocation.v1",
                "contract_version": "1.0.0",
                "workflow_id": "story",
                "source": "openclaw",
                "external_occurrence_id": "story.check.1030.v1@2026-09-08T06:30:00Z",
                "scheduled_for": "2026-09-08T06:30:00Z",
                "triggered_at": "2026-09-08T06:30:02Z",
            }
            trigger["occurrence_id"] = compute_occurrence_id(
                trigger["workflow_id"], trigger["source"],
                trigger["external_occurrence_id"], trigger["scheduled_for"],
            )

            def exploding(*args, **kwargs):
                raise AssertionError("must not be called")

            result = run_story_trigger(
                trigger,
                writer=exploding,
                verifier=exploding,
                draft_connector=exploding,
                review_delivery=exploding,
                workspace_root=root,
                output_root=root / "run-outcomes",
            )
        self.assertEqual(result.reason_code, "MORNING_SOURCE_UNPROVEN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
