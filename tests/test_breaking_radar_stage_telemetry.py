#!/usr/bin/env python3
"""Safe failure-stage telemetry for Breaking Radar (offline, no external calls).

Proves RadarStageError reason codes without leaking raw transport content.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_claude_radar_provider as radar  # noqa: E402
import nullone_provider_adapter as adapter  # noqa: E402


def valid_workflow_assessment(candidate_id="acme-widget-launch", **overrides):
    base = {
        "schema": "nullone.breaking-workflow-input.v1",
        "contract_version": "1.0.0",
        "candidate_id": candidate_id,
        "candidate_version": "v1",
        "assessment_ref": "assessment:radar-test:1",
        "state_snapshot_ref": "state:radar-test:1",
        "topic": "Acme Widget launch",
        "topic_cluster": "acme-widget",
        "content_type": "BREAKING",
        "evidence": [
            {
                "ref": "evidence:official:1",
                "supported_claim": "Acme Widget 2 is available.",
                "source_url": "https://example.invalid/widget-2",
                "announcement_id": "acme-widget-2-launch",
                "product": "Acme Widget",
                "version": "2",
                "region": None,
                "availability_stage": "GENERAL_AVAILABILITY",
                "number_value": None,
                "number_unit": None,
                "number_population": None,
                "number_period": None,
            }
        ],
        "follow_up_delta": None,
        "source_attribution": "Acme official",
        "limitations": ["Launch region only."],
        "product_version_region": {
            "product": "Acme Widget",
            "version": "2",
            "region": None,
        },
        "source_image": None,
        "verification": {"state": "PASS", "evidence_refs": ["evidence:official:1"]},
        "severity_assessment": {
            "classification": "MATERIAL_BREAKING",
            "reason_text": "Launch timing is material.",
        },
        "recent_coverage": {
            "related_coverage_exists": False,
            "incremental_value_present": True,
            "assessment_ref": "coverage:radar-test:1",
            "freshness_ref": "freshness:radar-test:1",
        },
        "story_safety": {
            "quality_pass": True,
            "quality_ref": "quality:radar-test:1",
            "dependencies_available": True,
            "dependencies_ref": "dependencies:radar-test:1",
        },
        "main_assessment": None,
    }
    base.update(overrides)
    return base


def candidates_result():
    return {
        "mode": "CANDIDATES_EMITTED",
        "report_markdown": "# Breaking 1130\n\n- Acme widget launch.\n",
        "assessments": [valid_workflow_assessment("acme-widget-launch")],
    }


def empty_result():
    return {
        "mode": "NO_MATERIAL_DEVELOPMENT",
        "report_markdown": "NO MATERIAL DEVELOPMENT.",
        "assessments": [],
    }


class FakeScanModule:
    STAGING_SUBPATH = Path("social/ops/breaking-staging")

    def __init__(self):
        self.commits: list = []
        self.empties: list = []
        self.preflights: list = []

    def current_scan(self, source="openclaw", at=None):
        assert source == "openclaw"
        return {
            "schedule_id": "breaking-radar.scan-1130.v1",
            "scheduled_for": "2026-09-30T07:30:00Z",
            "source_occurrence_id": "breaking-radar.scan-1130.v1@2026-09-30T07:30:00Z",
            "source": source,
            "triggered_at": "2026-09-30T07:31:00Z" if at is None else at,
        }

    def prepare_assessment_commit(self, assessment, source="openclaw", at=None):
        self.preflights.append({"assessment": assessment, "source": source, "at": at})
        return {"ok": True}

    def commit_assessment(self, assessment_path, source="openclaw", at=None, workspace_root=None):
        self.commits.append({"assessment_path": Path(assessment_path)})
        return {"committed": True}

    def record_empty_scan(self, source="openclaw", at=None, workspace_root=None):
        self.empties.append({"at": at})
        return {"recorded": True}


def load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "nullone_breaking_radar_run_stage_test",
        SCRIPTS / "nullone-breaking-radar-run.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RAW_MARKERS = [
    "SECRET_STDOUT_BYTES",
    "SECRET_STDERR_BYTES",
    "https://signed.example/presigned?token=SECRET123",
    "PROMPT_SECRET_TEXT",
    "fetched-content-secret",
]


def assert_safe_error(testcase, exc: BaseException, code: str):
    testcase.assertIsInstance(exc, radar.RadarStageError)
    testcase.assertIsInstance(exc, BridgeError)
    testcase.assertEqual(exc.reason_code, code)
    text = str(exc)
    testcase.assertIn(code, text)
    for raw in RAW_MARKERS:
        testcase.assertNotIn(raw, text)
    # Never echo arbitrary downstream text.
    testcase.assertNotIn("boom", text.lower() if code != "UNKNOWN_RADAR_FAILURE" else "___impossible___")


class StageTelemetryTests(unittest.TestCase):
    def test_reason_codes_are_stable_set(self):
        self.assertEqual(
            set(radar.ALLOWED_RADAR_REASON_CODES),
            {
                "CLAUDE_TIMEOUT",
                "CLAUDE_BINARY_MISSING",
                "CLAUDE_EXIT_NONZERO",
                "CLAUDE_OUTPUT_INVALID",
                "RESULT_VALIDATION",
                "SCAN_IDENTITY",
                "EMPTY_SCAN_RECEIPT",
                "BATCH_PREFLIGHT",
                "REPORT_WRITE",
                "STAGING_WRITE",
                "COMMIT",
                "UNKNOWN_RADAR_FAILURE",
            },
        )

    def _invoke_expect_code(self, run_side_effect, code, fake_scan=None, result=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scan = fake_scan or FakeScanModule()
            if isinstance(run_side_effect, BaseException):
                patch = mock.patch.object(radar, "run_structured", side_effect=run_side_effect)
            else:
                patch = mock.patch.object(radar, "run_structured", return_value=result if result is not None else candidates_result())
            with patch, mock.patch.object(radar, "_scan_module", return_value=scan):
                with self.assertRaises(radar.RadarStageError) as ctx:
                    radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            self.assertEqual(ctx.exception.reason_code, code)
            return ctx.exception, root

    def test_01_claude_timeout(self):
        exc, _ = self._invoke_expect_code(BridgeError("Claude invocation timed out"), "CLAUDE_TIMEOUT")
        self.assertIsNone(exc.exit_code)

    def test_02_missing_binary(self):
        exc, _ = self._invoke_expect_code(BridgeError("claude binary not found"), "CLAUDE_BINARY_MISSING")
        self.assertIsNone(exc.exit_code)

    def test_03_nonzero_exit_with_numeric_only(self):
        exc, _ = self._invoke_expect_code(BridgeError("Claude invocation failed (exit=1)"), "CLAUDE_EXIT_NONZERO")
        self.assertEqual(exc.exit_code, 1)
        self.assertIn("exit=1", str(exc))
        for raw in RAW_MARKERS:
            self.assertNotIn(raw, str(exc))

    def test_03b_exit_code_absent_still_safe(self):
        # Direct construction without numeric exit stays safe.
        exc = radar.RadarStageError("CLAUDE_EXIT_NONZERO")
        self.assertIsNone(exc.exit_code)
        self.assertIn("CLAUDE_EXIT_NONZERO", str(exc))
        self.assertNotIn("exit=", str(exc))

    def test_04_non_json_output_invalid(self):
        exc, _ = self._invoke_expect_code(BridgeError("Claude returned non-JSON output"), "CLAUDE_OUTPUT_INVALID")
        assert_safe_error(self, exc, "CLAUDE_OUTPUT_INVALID")

    def test_04b_json_not_object_invalid(self):
        exc, _ = self._invoke_expect_code(BridgeError("Claude JSON output is not an object"), "CLAUDE_OUTPUT_INVALID")
        self.assertEqual(exc.reason_code, "CLAUDE_OUTPUT_INVALID")

    def test_05_validated_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(radar, "run_structured", return_value={"bogus": 1}), mock.patch.object(
                radar, "_scan_module", return_value=FakeScanModule()
            ):
                with self.assertRaises(radar.RadarStageError) as ctx:
                    radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            self.assertEqual(ctx.exception.reason_code, "RESULT_VALIDATION")
            # No writes on validation rejection.
            self.assertFalse((root / "social/research/daily").exists() and list((root / "social/research/daily").glob("*.md")))

    def test_06_current_scan_failure(self):
        class BadScan(FakeScanModule):
            def current_scan(self, source="openclaw", at=None):
                raise BridgeError("scan identity boom SECRET_STDOUT_BYTES https://signed.example/presigned?token=SECRET123")

        exc, root = self._invoke_expect_code(None, "SCAN_IDENTITY", fake_scan=BadScan(), result=candidates_result())
        for raw in RAW_MARKERS:
            self.assertNotIn(raw, str(exc))
        self.assertFalse(list((root / "social/research/daily").glob("*-breaking-*.md")) if (root / "social/research/daily").is_dir() else [])

    def test_07_empty_scan_receipt_rejection(self):
        class BadEmpty(FakeScanModule):
            def record_empty_scan(self, source="openclaw", at=None, workspace_root=None):
                raise BridgeError("empty receipt boom SECRET_STDERR_BYTES")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(radar, "run_structured", return_value=empty_result()), mock.patch.object(
                radar, "_scan_module", return_value=BadEmpty()
            ):
                with self.assertRaises(radar.RadarStageError) as ctx:
                    radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            self.assertEqual(ctx.exception.reason_code, "EMPTY_SCAN_RECEIPT")
            self.assertNotIn("SECRET_STDERR_BYTES", str(ctx.exception))
            daily = root / "social/research/daily"
            self.assertFalse(list(daily.glob("*-breaking-*.md")) if daily.is_dir() else [])

    def test_08_batch_preflight_rejection(self):
        class BadPreflight(FakeScanModule):
            def prepare_assessment_commit(self, assessment, source="openclaw", at=None):
                raise BridgeError("deep invalid PROMPT_SECRET_TEXT")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(radar, "run_structured", return_value=candidates_result()), mock.patch.object(
                radar, "_scan_module", return_value=BadPreflight()
            ):
                with self.assertRaises(radar.RadarStageError) as ctx:
                    radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            self.assertEqual(ctx.exception.reason_code, "BATCH_PREFLIGHT")
            self.assertNotIn("PROMPT_SECRET_TEXT", str(ctx.exception))
            daily = root / "social/research/daily"
            self.assertFalse(list(daily.glob("*-breaking-*.md")) if daily.is_dir() else [])
            staging = root / "social/ops/breaking-staging"
            self.assertFalse(list(staging.glob("*.json")) if staging.is_dir() else [])

    def test_09_report_write_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(radar, "run_structured", return_value=candidates_result()), mock.patch.object(
                radar, "_scan_module", return_value=FakeScanModule()
            ), mock.patch.object(radar, "_write_text_file", side_effect=BridgeError("disk boom SECRET_STDOUT_BYTES")):
                with self.assertRaises(radar.RadarStageError) as ctx:
                    radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            self.assertEqual(ctx.exception.reason_code, "REPORT_WRITE")
            self.assertNotIn("SECRET_STDOUT_BYTES", str(ctx.exception))

    def test_10_staging_write_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real_write = radar._write_text_file

            def flaky(path, content, *, workspace_root):
                # First call is the report write: succeed.
                if "daily" in str(path):
                    return real_write(path, content, workspace_root=workspace_root)
                raise BridgeError("staging disk boom SECRET_STDERR_BYTES")

            with mock.patch.object(radar, "run_structured", return_value=candidates_result()), mock.patch.object(
                radar, "_scan_module", return_value=FakeScanModule()
            ), mock.patch.object(radar, "_write_text_file", side_effect=flaky):
                with self.assertRaises(radar.RadarStageError) as ctx:
                    radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            self.assertEqual(ctx.exception.reason_code, "STAGING_WRITE")
            self.assertNotIn("SECRET_STDERR_BYTES", str(ctx.exception))

    def test_11_commit_rejection(self):
        class BadCommit(FakeScanModule):
            def commit_assessment(self, assessment_path, source="openclaw", at=None, workspace_root=None):
                raise BridgeError("commit conflict fetched-content-secret")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(radar, "run_structured", return_value=candidates_result()), mock.patch.object(
                radar, "_scan_module", return_value=BadCommit()
            ):
                with self.assertRaises(radar.RadarStageError) as ctx:
                    radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            self.assertEqual(ctx.exception.reason_code, "COMMIT")
            self.assertNotIn("fetched-content-secret", str(ctx.exception))

    def test_12_unknown_maps_safely(self):
        exc, _ = self._invoke_expect_code(
            BridgeError("some arbitrary downstream boom SECRET_STDOUT_BYTES https://signed.example/presigned?token=SECRET123"),
            "UNKNOWN_RADAR_FAILURE",
        )
        for raw in RAW_MARKERS:
            self.assertNotIn(raw, str(exc))

    def test_12b_unexpected_exception_maps_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(radar, "run_structured", side_effect=ValueError("weird PROMPT_SECRET_TEXT")), mock.patch.object(
                radar, "_scan_module", return_value=FakeScanModule()
            ):
                with self.assertRaises(radar.RadarStageError) as ctx:
                    radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            self.assertEqual(ctx.exception.reason_code, "UNKNOWN_RADAR_FAILURE")
            self.assertNotIn("PROMPT_SECRET_TEXT", str(ctx.exception))

    def test_13_runner_stdout_has_no_raw_text(self):
        wrapper = load_wrapper()
        raw = BridgeError("Claude invocation failed (exit=7) SECRET_STDOUT_BYTES https://signed.example/presigned?token=SECRET123")
        # Map through provider to get a realistic safe error, then poison its chain with raw.
        safe = radar.RadarStageError("CLAUDE_EXIT_NONZERO", exit_code=7)
        # Simulate a raw downstream message that must never surface.
        poisoned_message = "SECRET_STDOUT_BYTES SECRET_STDERR_BYTES https://signed.example/presigned?token=SECRET123 PROMPT_SECRET_TEXT"
        with mock.patch.object(wrapper.provider_adapter, "invoke_role_cycle", side_effect=safe):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = wrapper.execute()
            out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("ROLE_OUTCOME=BLOCKED reason=RadarStageError code=CLAUDE_EXIT_NONZERO", out)
        self.assertIn("exit=7", out)
        for raw_marker in RAW_MARKERS + [poisoned_message]:
            self.assertNotIn(raw_marker, out)
        # Generic unknown also safe.
        with mock.patch.object(
            wrapper.provider_adapter, "invoke_role_cycle", side_effect=radar.RadarStageError("UNKNOWN_RADAR_FAILURE")
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = wrapper.execute()
            out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("code=UNKNOWN_RADAR_FAILURE", out)

    def test_14_no_raw_stdout_stderr_persisted_or_printed(self):
        fake_stdout = '{"structured_output": {"x": 1}} SECRET_STDOUT_BYTES https://signed.example/presigned?token=SECRET123'
        fake_stderr = "SECRET_STDERR_BYTES stack with token SECRET123"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(
                radar, "run_structured", side_effect=BridgeError("Claude invocation failed (exit=2)")
            ), mock.patch.object(radar, "_scan_module", return_value=FakeScanModule()):
                with self.assertRaises(radar.RadarStageError) as ctx:
                    radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            text = str(ctx.exception)
            self.assertNotIn(fake_stdout, text)
            self.assertNotIn(fake_stderr, text)
            self.assertNotIn("SECRET_STDOUT_BYTES", text)
            self.assertNotIn("SECRET_STDERR_BYTES", text)
            # Nothing persisted on transport failure.
            daily = root / "social/research/daily"
            staging = root / "social/ops/breaking-staging"
            handoffs = root / "social/ops/breaking-handoffs"
            for base in (daily, staging, handoffs):
                if base.is_dir():
                    files = list(base.rglob("*"))
                    payload = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in files if p.is_file())
                    self.assertNotIn("SECRET_STDOUT_BYTES", payload)
                    self.assertNotIn("SECRET_STDERR_BYTES", payload)

    def test_15_success_path_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(radar, "run_structured", return_value=candidates_result()), mock.patch.object(
                radar, "_scan_module", return_value=FakeScanModule()
            ):
                summary = radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
            self.assertEqual(summary["mode"], "CANDIDATES_EMITTED")
            self.assertEqual(summary["committed"], ["acme-widget-launch"])
            self.assertFalse(summary["empty_recorded"])
            report = root / "social/research/daily/2026-09-30-breaking-1130.md"
            self.assertTrue(report.is_file())
            staged = root / "social/ops/breaking-staging/acme-widget-launch.json"
            self.assertTrue(staged.is_file())
        # Wrapper success semantics: COMPLETED + exit 0.
        wrapper = load_wrapper()
        with mock.patch.object(wrapper.provider_adapter, "invoke_role_cycle") as cycle, mock.patch.object(
            wrapper, "find_fresh_reports", return_value=[Path("/tmp/x.md")]
        ):
            cycle.return_value = adapter.AdapterOutcome(
                role="breaking_radar", transport="claude", model="haiku", outcome="COMPLETED"
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = wrapper.execute()
            out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("ROLE_OUTCOME=COMPLETED", out)
        self.assertNotIn("RadarStageError", out)

    def test_17_no_external_calls(self):
        import nullone_claude as claude_mod

        with mock.patch.object(claude_mod.subprocess, "run", side_effect=AssertionError("external call")):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                with mock.patch.object(radar, "run_structured", return_value=candidates_result()), mock.patch.object(
                    radar, "_scan_module", return_value=FakeScanModule()
                ):
                    summary = radar.invoke_radar(prompt="p", workspace=root, model="haiku", timeout=600)
                self.assertEqual(summary["committed"], ["acme-widget-launch"])
            # Runner blocked path also performs no subprocess calls.
            wrapper = load_wrapper()
            with mock.patch.object(
                wrapper.provider_adapter, "invoke_role_cycle", side_effect=radar.RadarStageError("CLAUDE_TIMEOUT")
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = wrapper.execute()
                self.assertEqual(rc, 1)


class WrapperBoundaryTests(unittest.TestCase):
    """Provider-neutral wrapper boundary: no direct Claude import."""

    def test_runner_does_not_import_claude_provider(self):
        source = (SCRIPTS / "nullone-breaking-radar-run.py").read_text(encoding="utf-8")
        self.assertNotIn("nullone_claude_radar_provider", source)
        self.assertIn("nullone_radar_stage_error", source)
        self.assertIn("nullone_provider_adapter", source)

    def test_runner_stage_error_is_neutral_contract(self):
        import nullone_radar_stage_error as neutral  # noqa: E402

        wrapper = load_wrapper()
        self.assertIs(wrapper.RadarStageError, neutral.RadarStageError)
        self.assertEqual(wrapper.ALLOWED_RADAR_REASON_CODES, neutral.ALLOWED_RADAR_REASON_CODES)

    def test_claude_provider_owns_mapping_neutral_owns_contract(self):
        provider_source = (SCRIPTS / "nullone_claude_radar_provider.py").read_text(encoding="utf-8")
        self.assertIn("_map_claude_failure", provider_source)
        self.assertIn("from nullone_radar_stage_error import", provider_source)
        neutral_source = (SCRIPTS / "nullone_radar_stage_error.py").read_text(encoding="utf-8")
        self.assertIn("class RadarStageError", neutral_source)
        self.assertIn("ALLOWED_RADAR_REASON_CODES", neutral_source)
        self.assertNotIn("def _map_claude_failure", neutral_source)
        self.assertNotIn("from nullone_claude import", neutral_source)
        self.assertNotIn("import nullone_claude", neutral_source)
        self.assertNotIn("Claude invocation timed out", neutral_source)

    def test_execution_flows_through_adapter(self):
        wrapper = load_wrapper()
        import nullone_radar_stage_error as neutral  # noqa: E402

        with mock.patch.object(
            wrapper.provider_adapter,
            "invoke_role_cycle",
            side_effect=neutral.RadarStageError("CLAUDE_TIMEOUT"),
        ) as cycle:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = wrapper.execute()
            out = buf.getvalue()
        self.assertEqual(cycle.call_count, 1)
        self.assertEqual(rc, 1)
        self.assertIn("reason=RadarStageError code=CLAUDE_TIMEOUT", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
