#!/usr/bin/env python3
"""Offline regression coverage for the bounded Breaking Radar Claude route (issue #190).

Mirrors tests/test_weekly_claude_route.py: real router/adapter dispatch,
mocked Claude CLI boundary, fake scan-helper surface plus signature
checks against the real deterministic scan module. No network, no
production, no external calls.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_claude as claude  # noqa: E402
import nullone_claude_radar_provider as radar  # noqa: E402
import nullone_provider_adapter as adapter  # noqa: E402
import nullone_provider_router as router  # noqa: E402


def candidates_result():
    return {
        "mode": "CANDIDATES_EMITTED",
        "report_markdown": "# Breaking 1130\n\n- Acme widget launch (primary source).\n",
        "assessments": [
            {"candidate_id": "acme-widget-launch", "content_type": "NEWS"},
        ],
    }


def empty_result():
    return {
        "mode": "NO_MATERIAL_DEVELOPMENT",
        "report_markdown": "NO MATERIAL DEVELOPMENT.",
        "assessments": [],
    }


class FakeScanModule:
    """Fake deterministic scan surface: records calls, writes nothing."""

    STAGING_SUBPATH = Path("social/ops/breaking-staging")

    def __init__(self):
        self.commits: list = []
        self.empties: list = []

    def current_scan(self, source="openclaw"):
        assert source == "openclaw"
        return {
            "schedule_id": "breaking-radar.scan-1130.v1",
            "scheduled_for": "2026-09-30T07:30:00Z",
            "source_occurrence_id": "breaking-radar.scan-1130.v1@2026-09-30T07:30:00Z",
            "source": source,
            "triggered_at": "2026-09-30T07:31:00Z",
        }

    def commit_assessment(self, assessment_path, source="openclaw", workspace_root=None):
        self.commits.append(
            {
                "assessment_path": Path(assessment_path),
                "source": source,
                "workspace_root": Path(workspace_root),
            }
        )
        return {"committed": True}

    def record_empty_scan(self, source="openclaw", workspace_root=None):
        self.empties.append({"source": source, "workspace_root": Path(workspace_root)})
        return {"recorded": True}


def load_scan_module_real():
    spec = importlib.util.spec_from_file_location(
        "nullone_breaking_scan_real",
        SCRIPTS / "nullone-breaking-scan.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_radar_wrapper():
    spec = importlib.util.spec_from_file_location(
        "nullone_breaking_radar_run_under_test",
        SCRIPTS / "nullone-breaking-radar-run.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RadarClaudeRouteTests(unittest.TestCase):
    def test_checked_in_profile_and_other_routes_unchanged(self):
        expected = {
            "breaking_radar": ("claude", "haiku", 600),
            "draft_factory": ("opencode", "opencode/muse-spark-1.3-contributor-free", 900),
            "morning_editorial": ("claude", "sonnet", 600),
            "story_writer": ("claude", "haiku", 300),
            "weekly_strategy": ("claude", "sonnet", 600),
        }
        for role, wanted in expected.items():
            profile = router.resolve_provider_profile(role, env={})
            self.assertEqual(
                (profile.transport, profile.model, profile.timeout_seconds), wanted, role
            )
            self.assertEqual(profile.fallback_policy, "none", role)

    def test_legacy_opencode_model_inert_under_claude_radar(self):
        with mock.patch.dict(
            __import__("os").environ,
            {"NULLONE_OPENCODE_MODEL": "opencode/muse-spark-1.3-contributor-free"},
        ):
            profile = router.resolve_provider_profile("breaking_radar", env={})
        self.assertEqual((profile.transport, profile.model), ("claude", "haiku"))

    def test_adapter_dispatches_radar_to_dedicated_claude_provider(self):
        profile = router.resolve_provider_profile("breaking_radar", env={})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_scan = FakeScanModule()
            with mock.patch.object(
                radar, "run_structured", return_value=candidates_result()
            ) as structured, mock.patch.object(
                radar, "_scan_module", return_value=fake_scan
            ), mock.patch.object(
                adapter.opencode_role, "run_opencode_cycle"
            ) as opencode, mock.patch(
                "nullone_claude_editorial_provider.default_invoke_provider"
            ) as morning:
                outcome = adapter.invoke_adapter(
                    profile,
                    adapter.AdapterCall(
                        role="breaking_radar", prompt="EXACT RADAR PROMPT", workspace=root
                    ),
                )
            self.assertEqual(structured.call_count, 1)
            self.assertEqual(
                (outcome.role, outcome.transport, outcome.model),
                ("breaking_radar", "claude", "haiku"),
            )
            opencode.assert_not_called()
            morning.assert_not_called()

    def test_structured_call_carries_exact_model_timeout_and_tools(self):
        profile = router.resolve_provider_profile("breaking_radar", env={})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            captured = {}

            def fake_structured(**kwargs):
                captured.update(kwargs)
                return empty_result()

            fake_scan = FakeScanModule()
            with mock.patch.object(radar, "run_structured", side_effect=fake_structured), \
                 mock.patch.object(radar, "_scan_module", return_value=fake_scan):
                adapter.invoke_adapter(
                    profile,
                    adapter.AdapterCall(
                        role="breaking_radar", prompt="EXACT RADAR PROMPT", workspace=root
                    ),
                )
            self.assertEqual(captured["prompt"], "EXACT RADAR PROMPT")
            self.assertEqual(captured["workspace"], root)
            self.assertEqual(captured["model"], "haiku")
            self.assertEqual(captured["timeout"], 600)
            self.assertEqual(captured["allowed_tools"], ["Read", "WebSearch", "WebFetch"])
            self.assertEqual(
                captured["weekly_security_settings"],
                radar.radar_security_settings(root),
            )

    def test_candidates_persist_report_stage_commit(self):
        profile = router.resolve_provider_profile("breaking_radar", env={})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_scan = FakeScanModule()
            with mock.patch.object(
                radar, "run_structured", return_value=candidates_result()
            ), mock.patch.object(radar, "_scan_module", return_value=fake_scan):
                summary = adapter.invoke_adapter(
                    profile,
                    adapter.AdapterCall(
                        role="breaking_radar", prompt="p", workspace=root
                    ),
                )
            # 07:30Z == 11:30 Asia/Baku: deterministic report name.
            report = root / "social/research/daily/2026-09-30-breaking-1130.md"
            self.assertTrue(report.is_file())
            self.assertIn("Acme widget launch", report.read_text(encoding="utf-8"))
            staged = root / "social/ops/breaking-staging/acme-widget-launch.json"
            self.assertTrue(staged.is_file())
            self.assertEqual(len(fake_scan.commits), 1)
            commit = fake_scan.commits[0]
            self.assertEqual(commit["assessment_path"], staged)
            self.assertEqual(commit["source"], "openclaw")
            self.assertEqual(commit["workspace_root"], root)
            self.assertEqual(fake_scan.empties, [])
            self.assertEqual(summary.model, "haiku")

    def test_empty_scan_records_receipt_without_commit(self):
        profile = router.resolve_provider_profile("breaking_radar", env={})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_scan = FakeScanModule()
            with mock.patch.object(
                radar, "run_structured", return_value=empty_result()
            ), mock.patch.object(radar, "_scan_module", return_value=fake_scan):
                adapter.invoke_adapter(
                    profile,
                    adapter.AdapterCall(
                        role="breaking_radar", prompt="p", workspace=root
                    ),
                )
            report = root / "social/research/daily/2026-09-30-breaking-1130.md"
            self.assertTrue(report.is_file())
            self.assertEqual(fake_scan.commits, [])
            self.assertEqual(len(fake_scan.empties), 1)
            self.assertEqual(fake_scan.empties[0]["workspace_root"], root)

    def test_malformed_results_fail_closed_with_no_writes(self):
        bad_results = [
            None,
            {},
            {"mode": "CANDIDATES_EMITTED", "report_markdown": "r", "assessments": []},
            {"mode": "NO_MATERIAL_DEVELOPMENT", "report_markdown": "r",
             "assessments": [{"candidate_id": "x-y"}]},
            {"mode": "CANDIDATES_EMITTED", "report_markdown": "  ",
             "assessments": [{"candidate_id": "x-y"}]},
            {"mode": "CANDIDATES_EMITTED", "report_markdown": "r",
             "assessments": [{"candidate_id": "INVALID ID"}]},
            {"mode": "CANDIDATES_EMITTED", "report_markdown": "r",
             "assessments": [{"candidate_id": "single"}]},
            {"mode": "CANDIDATES_EMITTED", "report_markdown": "r",
             "assessments": [{"candidate_id": "a" * 81}]},
            {"mode": "CANDIDATES_EMITTED", "report_markdown": "r",
             "assessments": ["not-a-dict"]},
            {"mode": "CANDIDATES_EMITTED", "report_markdown": "r",
             "assessments": [{"candidate_id": "x-y"}], "extra": 1},
            {"mode": "WRONG", "report_markdown": "r", "assessments": []},
        ]
        for bad in bad_results:
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    fake_scan = FakeScanModule()
                    with mock.patch.object(
                        radar, "run_structured", return_value=bad
                    ), mock.patch.object(radar, "_scan_module", return_value=fake_scan):
                        with self.assertRaises(BridgeError):
                            radar.invoke_radar(
                                prompt="p", workspace=root, model="haiku", timeout=600
                            )
                    self.assertEqual(fake_scan.commits, [])
                    self.assertEqual(fake_scan.empties, [])
                    daily = root / "social/research/daily"
                    self.assertFalse(
                        list(daily.glob("*-breaking-*.md")) if daily.is_dir() else []
                    )

    def test_failed_claude_propagates_without_opencode_fallback(self):
        profile = router.resolve_provider_profile("breaking_radar", env={})
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(
                radar, "run_structured", side_effect=BridgeError("boom")
            ), mock.patch.object(adapter.opencode_role, "run_opencode_cycle") as opencode:
                with self.assertRaises(BridgeError):
                    adapter.invoke_adapter(
                        profile,
                        adapter.AdapterCall(
                            role="breaking_radar", prompt="p",
                            workspace=Path(td),
                        ),
                    )
            opencode.assert_not_called()

    def test_real_scan_module_exposes_compatible_boundary(self):
        scan = load_scan_module_real()
        self.assertEqual(scan.STAGING_SUBPATH, Path("social/ops/breaking-staging"))
        for name, required in (
            ("current_scan", ("source",)),
            ("commit_assessment", ("assessment_path", "source", "workspace_root")),
            ("record_empty_scan", ("source", "workspace_root")),
        ):
            params = inspect.signature(getattr(scan, name)).parameters
            for key in required:
                self.assertIn(key, params, f"{name} missing {key}")

    def test_cli_tool_boundary_denies_everything_but_research_reads(self):
        with tempfile.TemporaryDirectory() as td:
            seen = {}

            def fake_run(cmd, **kwargs):
                seen.update(cmd=cmd, kwargs=kwargs)
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"structured_output": empty_result()}), stderr=""
                )

            with mock.patch.object(claude.subprocess, "run", side_effect=fake_run):
                claude.run_structured(
                    prompt="radar only", allowed_tools=radar.ALLOWED_TOOLS,
                    schema=radar.SCHEMA, model="haiku", timeout=600,
                    workspace=Path(td),
                    weekly_security_settings=radar.radar_security_settings(Path(td)),
                )
            cmd = seen["cmd"]
            self.assertEqual(seen["kwargs"]["cwd"], Path(td))
            self.assertEqual(seen["kwargs"]["timeout"], 600)
            self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
            self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,WebSearch,WebFetch")
            self.assertEqual(
                cmd[cmd.index("--allowedTools") + 1:cmd.index("--restricted")],
                ["Read", "WebSearch", "WebFetch"],
            )
            self.assertIn("--no-session-persistence", cmd)
            self.assertIn("--permission-mode", cmd)
            self.assertIn("dontAsk", cmd)
            self.assertIn("--restricted", cmd)
            self.assertIn("--safe-mode", cmd)
            self.assertIn("--strict-mcp-config", cmd)
            denied_tools = cmd[cmd.index("--disallowedTools") + 1].split(",")
            for denied in ("mcp__*", "Agent", "Bash", "Edit", "Write"):
                self.assertIn(denied, denied_tools)
            settings = json.loads(cmd[cmd.index("--settings") + 1])
            anchor = "//" + Path(td).resolve().as_posix().lstrip("/")
            denied_reads = settings["permissions"]["deny"]
            for name in (".env", ".env.*", "*.key", "*.pem"):
                self.assertIn(f"Read({anchor}/{name})", denied_reads)
                self.assertIn(f"Read({anchor}/**/{name})", denied_reads)
            self.assertIn(f"Read({anchor}/social/ops/private/**)", denied_reads)
            self.assertEqual(radar.ALLOWED_TOOLS, ["Read", "WebSearch", "WebFetch"])
            for denied in ("Bash", "Write", "Edit", "shell", "Git", "Zernio",
                           "Telegram", "publish", "approval", "MCP"):
                self.assertNotIn(denied, radar.ALLOWED_TOOLS)

    def test_wrapper_hollow_completion_still_blocked(self):
        wrapper = load_radar_wrapper()
        with mock.patch.object(
            wrapper.provider_adapter, "invoke_role_cycle"
        ) as cycle, mock.patch.object(
            wrapper, "find_fresh_reports", return_value=[]
        ):
            cycle.return_value = adapter.AdapterOutcome(
                role="breaking_radar", transport="claude", model="haiku",
                outcome="COMPLETED",
            )
            self.assertEqual(wrapper.execute(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
