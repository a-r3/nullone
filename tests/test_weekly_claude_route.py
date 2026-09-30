#!/usr/bin/env python3
"""Offline regression coverage for the bounded Weekly Claude route."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_claude as claude  # noqa: E402
import nullone_claude_weekly_provider as weekly  # noqa: E402
import nullone_provider_adapter as adapter  # noqa: E402
import nullone_provider_router as router  # noqa: E402

NOW = datetime(2026, 9, 30, tzinfo=ZoneInfo("Asia/Baku"))


def result(memory_update=""):
    return {
        "evidence": "Seven day metrics and sources; insufficient samples for timing.",
        "reference_review": "Small recent sample of reference accounts; no interaction.",
        "observations": ["Reach was limited."],
        "hypotheses": ["The topic may be narrow."],
        "decisions": ["Keep strategy stable."],
        "memory_update": memory_update,
    }


class WeeklyClaudeTests(unittest.TestCase):
    def test_checked_in_profile_and_other_routes(self):
        expected = {
            "weekly_strategy": ("claude", "sonnet", 600),
            "morning_editorial": ("claude", "sonnet", 600),
            "story_writer": ("claude", "haiku", 300),
            "draft_factory": ("opencode", "opencode/muse-spark-1.3-contributor-free", 900),
            "breaking_radar": ("claude", "haiku", 600),
        }
        for role, wanted in expected.items():
            profile = router.resolve_provider_profile(role, env={})
            self.assertEqual((profile.transport, profile.model, profile.timeout_seconds), wanted)
            self.assertEqual(profile.fallback_policy, "none")

    def test_actual_prompt_workspace_tools_model_and_timeout(self):
        profile = router.resolve_provider_profile("weekly_strategy", env={})
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            captured = {}

            def fake_structured(**kwargs):
                captured.update(kwargs)
                return result()

            with mock.patch.object(weekly, "run_structured", side_effect=fake_structured), \
                 mock.patch.object(adapter.opencode_role, "run_opencode_cycle") as opencode, \
                 mock.patch("nullone_claude_editorial_provider.default_invoke_provider") as morning:
                outcome = adapter.invoke_adapter(profile, adapter.AdapterCall(
                    role="weekly_strategy", prompt="EXACT WEEKLY PROMPT", workspace=root))
            self.assertEqual(captured["prompt"], "EXACT WEEKLY PROMPT")
            self.assertEqual(captured["workspace"], root)
            self.assertEqual(captured["model"], "sonnet")
            self.assertEqual(captured["timeout"], 600)
            self.assertEqual(captured["allowed_tools"], ["Read", "WebSearch", "WebFetch"])
            self.assertEqual(captured["weekly_security_settings"], weekly.weekly_security_settings(root))
            self.assertEqual((outcome.role, outcome.transport, outcome.model),
                             ("weekly_strategy", "claude", "sonnet"))
            self.assertTrue((root / f"social/analytics/reports/{NOW.year}-40-weekly-strategy.md").exists()
                         or list((root / "social/analytics/reports").glob("*-weekly-strategy.md")))
            opencode.assert_not_called()
            morning.assert_not_called()

    def test_cli_tool_boundary_workspace_and_no_session(self):
        with tempfile.TemporaryDirectory() as td:
            seen = {}

            def fake_run(cmd, **kwargs):
                seen.update(cmd=cmd, kwargs=kwargs)
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"structured_output": result()}), stderr="")

            with mock.patch.object(claude.subprocess, "run", side_effect=fake_run):
                claude.run_structured(prompt="weekly only", allowed_tools=weekly.ALLOWED_TOOLS,
                                      schema=weekly.SCHEMA, model="sonnet", timeout=600,
                                      workspace=Path(td),
                                      weekly_security_settings=weekly.weekly_security_settings(Path(td)))
            cmd = seen["cmd"]
            self.assertEqual(seen["kwargs"]["cwd"], Path(td))
            self.assertEqual(seen["kwargs"]["timeout"], 600)
            self.assertEqual(cmd[cmd.index("--allowedTools") + 1:cmd.index("--restricted")], weekly.ALLOWED_TOOLS)
            self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,WebSearch,WebFetch")
            self.assertIn("--no-session-persistence", cmd)
            self.assertIn("--restricted", cmd)
            self.assertIn("--safe-mode", cmd)
            self.assertIn("--strict-mcp-config", cmd)
            denied_tools = cmd[cmd.index("--disallowedTools") + 1].split(",")
            self.assertIn("mcp__*", denied_tools)
            for denied in ("Agent", "Bash", "Edit", "Write"):
                self.assertIn(denied, denied_tools)
            settings = json.loads(cmd[cmd.index("--settings") + 1])
            anchor = "//" + Path(td).resolve().as_posix().lstrip("/")
            denied_reads = settings["permissions"]["deny"]
            for name in (".env", ".env.*", "*.key", "*.pem"):
                self.assertIn(f"Read({anchor}/{name})", denied_reads)
                self.assertIn(f"Read({anchor}/**/{name})", denied_reads)
            self.assertIn(f"Read({anchor}/social/ops/private/**)", denied_reads)
            for denied in ("Bash", "Write", "Edit", "shell", "Git", "Zernio",
                           "Telegram", "publish", "approval", "MCP"):
                self.assertNotIn(denied, weekly.ALLOWED_TOOLS)

    def test_story_default_argv_matches_pre_pr(self):
        schema = {"type": "object"}
        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(cmd=cmd, kwargs=kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout='{"structured_output":{}}', stderr="")

        with mock.patch.object(claude.subprocess, "run", side_effect=fake_run):
            claude.run_structured(prompt="story prompt", allowed_tools=[], schema=schema)
        self.assertEqual(seen["cmd"], [
            "claude", "-p", "--tools", "", "--permission-mode", "dontAsk",
            "--model", "haiku", "--no-session-persistence", "--disable-slash-commands",
            "--max-turns", "8", "--output-format", "json", "--json-schema",
            '{"type":"object"}', "story prompt",
        ])
        self.assertIsNone(seen["kwargs"]["cwd"])

    def test_deterministic_paths_optional_memory_and_malformed_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = weekly.persist_weekly(result(), workspace=root, now=NOW)
            self.assertEqual(target, root / "social/analytics/reports/2026-40-weekly-strategy.md")
            self.assertFalse((root / "MEMORY.md").exists())
            self.assertIn("## OBSERVATION", target.read_text())
            data = result("Durable learning")
            weekly.persist_weekly(data, workspace=root, now=NOW)
            self.assertIn("Durable learning", (root / "MEMORY.md").read_text())
            for bad in (None, {}, {**data, "report_path": "../escape"},
                        {**data, "decisions": ["x"] * 4},
                        {**data, "observations": []},
                        {**data, "memory_update": {"path": "elsewhere"}}):
                with self.assertRaises(BridgeError):
                    weekly.persist_weekly(bad, workspace=root, now=NOW)
            self.assertFalse((root.parent / "escape").exists())

    def test_symlink_redirect_rejected(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root = Path(td)
            (root / "social").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(BridgeError):
                weekly.persist_weekly(result(), workspace=root, now=NOW)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_failed_claude_never_calls_opencode(self):
        profile = router.resolve_provider_profile("weekly_strategy", env={})
        with mock.patch.object(weekly, "run_structured", side_effect=BridgeError("failed")), \
             mock.patch.object(adapter.opencode_role, "run_opencode_cycle") as opencode:
            with self.assertRaises(BridgeError):
                adapter.invoke_adapter(profile, adapter.AdapterCall(
                    role="weekly_strategy", prompt="weekly", workspace=Path("/tmp")))
            opencode.assert_not_called()

    def test_weekly_prompt_and_fresh_report_proof(self):
        prompt = (ROOT / "workspace/social/ops/prompts/weekly-strategy.md").read_text()
        for required in ("previous 7 days", "OBSERVATION", "HYPOTHESIS", "DECISION",
                         "MAXIMUM 3", "DO NOT PUBLISH ANYTHING", "MEMORY.md"):
            self.assertIn(required, prompt)
        spec = importlib.util.spec_from_file_location("weekly_run_test_188", SCRIPTS / "nullone-weekly-strategy-run.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module.provider_adapter, "invoke_role_cycle", return_value=adapter.AdapterOutcome(
            role="weekly_strategy", transport="claude", model="sonnet", outcome="COMPLETED")), \
             mock.patch.object(module, "find_fresh_reports", return_value=[]):
            self.assertEqual(module.execute(), 1)


if __name__ == "__main__":
    unittest.main()
