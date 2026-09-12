#!/usr/bin/env python3
"""Offline tests for the provider-neutral Story writer layer (issue #112).

Proves, with mocks/fake subprocesses only (no network, no model
calls, no production state):

A. OpenCode Story adapter command construction is deterministic
B. Muse Spark model is selected (default + shared override knob)
C. structured Story output parsing remains compatible (shared prompt,
   empty-string stripping, dict contract)
D. malformed output fails closed
E. timeout classification preserved (distinct timeout error)
F. startup/reachability classification preserved
G. no shell/Git/draft/notify capability in the adapter or agent
H. secret paths denied in the agent boundary
I. exact Story write scope: the writer agent allows no writes at all
J. Claude fallback writer preserved
K. unknown provider fails closed
L. no silent fallback (factory raises; dispatch returns misconfigured)
M. Story domain/provenance/cadence suites still pass (run via
   tests/run_offline.py; this file additionally pins the dispatch
   wiring and the untouched writer contract)
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
AGENT_FILE = ROOT / "workspace/.opencode/agents/nullone-story-writer.md"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_opencode_story_provider as story_adapter  # noqa: E402
import nullone_story_provider_factory as story_factory  # noqa: E402
from nullone_story_pipeline import (  # noqa: E402
    HaikuStoryWriter,
    StoryWriterOutputInvalid,
    _writer_prompt,
    validate_story_spec_shape,
)


VALID_SPEC = {
    "layout": "breaking",
    "headline": "Probe headline",
    "body": "Probe body",
    "stat": "",
    "source_name": "Probe source",
    "use_source_image": False,
    "cta": "",
    "left_stat": "",
    "right_stat": "",
    "left_label": "",
    "right_label": "",
}


def _json_events(payload: dict) -> str:
    return (
        '{"type":"text","part":{"type":"text","text":'
        + json.dumps(json.dumps(payload))
        + "}}\n"
    )


class CommandConstructionTests(unittest.TestCase):
    def test_argv_is_deterministic_and_exact(self):
        workspace = Path("/tmp/nullone-story-determinism-check")
        context = {"topic": "probe"}
        first = story_adapter.build_opencode_command(
            prompt=story_adapter.build_writer_prompt(context),
            workspace=workspace,
            model="opencode/muse-spark-1.3-contributor-free",
        )
        second = story_adapter.build_opencode_command(
            prompt=story_adapter.build_writer_prompt(context),
            workspace=workspace,
            model="opencode/muse-spark-1.3-contributor-free",
        )
        self.assertEqual(first, second)
        self.assertEqual(first[0:2], ["opencode", "run"])
        self.assertEqual(
            first[3:],
            [
                "--agent",
                "nullone-story-writer",
                "--model",
                "opencode/muse-spark-1.3-contributor-free",
                "--format",
                "json",
                "--dir",
                "/tmp/nullone-story-determinism-check",
            ],
        )

    def test_no_session_continuation_or_auto_approval_flags(self):
        argv = story_adapter.build_opencode_command(
            prompt="probe",
            workspace=Path("/tmp/x"),
            model="opencode/muse-spark-1.3-contributor-free",
        )
        for forbidden in ("--auto", "--continue", "--session", "--fork", "--share"):
            self.assertNotIn(forbidden, argv)

    def test_muse_spark_is_the_default_model(self):
        self.assertEqual(
            story_adapter.DEFAULT_STORY_MODEL,
            "opencode/muse-spark-1.3-contributor-free",
        )
        self.assertEqual(
            story_adapter.OpenCodeStoryWriter.model,
            "opencode/muse-spark-1.3-contributor-free",
        )

    def test_shared_model_override_knob_still_works(self):
        self.assertEqual(story_adapter.resolve_story_model("  "), story_adapter.DEFAULT_STORY_MODEL)
        with mock.patch.dict(
            os.environ, {story_adapter.OPENCODE_MODEL_ENV_VAR: "nvidia/meta/llama-3.1-8b-instruct"}
        ):
            self.assertEqual(
                story_adapter.resolve_story_model(),
                "nvidia/meta/llama-3.1-8b-instruct",
            )

    def test_writer_timeout_matches_previous_structured_default(self):
        self.assertEqual(story_adapter.STORY_WRITER_TIMEOUT_SECONDS, 300)

    def test_workspace_cwd_and_dir_are_exact(self):
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout=_json_events(VALID_SPEC), stderr="")

        workspace = Path(tempfile.mkdtemp(prefix="nullone-story-cwd-"))
        writer = story_adapter.OpenCodeStoryWriter(workspace=workspace, timeout=30)
        with mock.patch.object(story_adapter.subprocess, "run", side_effect=fake_run):
            writer({"topic": "probe"})
        self.assertEqual(captured["kwargs"]["cwd"], workspace)
        argv = captured["cmd"]
        self.assertEqual(argv[argv.index("--dir") + 1], str(workspace))
        self.assertEqual(captured["kwargs"]["timeout"], 30)


class StructuredOutputContractTests(unittest.TestCase):
    def test_shared_prompt_builder_is_reused(self):
        context = {"topic": "probe", "rank": 1}
        prompt = story_adapter.build_writer_prompt(context)
        self.assertIn(_writer_prompt(context), prompt)

    def test_valid_output_parses_and_strips_empties(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=_json_events(VALID_SPEC), stderr="")

        workspace = Path(tempfile.mkdtemp(prefix="nullone-story-parse-"))
        writer = story_adapter.OpenCodeStoryWriter(workspace=workspace)
        with mock.patch.object(story_adapter.subprocess, "run", side_effect=fake_run):
            result = writer({"topic": "probe"})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["layout"], "breaking")
        self.assertNotIn("stat", result)
        self.assertNotIn("cta", result)
        # The pipeline's own shape validation still accepts it.
        validate_story_spec_shape({**result, "use_source_image": result.get("use_source_image", False)})

    def test_wrong_shape_dict_is_returned_for_pipeline_validation(self):
        bad = {"layout": "not-a-layout", "headline": "x"}
        with mock.patch.object(
            story_adapter.subprocess,
            "run",
            side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=_json_events(bad), stderr=""),
        ):
            writer = story_adapter.OpenCodeStoryWriter(workspace=Path(tempfile.mkdtemp()))
            result = writer({"topic": "probe"})
        with self.assertRaises(StoryWriterOutputInvalid):
            validate_story_spec_shape(result)

    def test_non_json_output_fails_closed(self):
        with mock.patch.object(
            story_adapter.subprocess,
            "run",
            side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="not json at all", stderr=""),
        ):
            writer = story_adapter.OpenCodeStoryWriter(workspace=Path(tempfile.mkdtemp()))
            with self.assertRaises(BridgeError):
                writer({"topic": "probe"})

    def test_non_object_json_fails_closed(self):
        with mock.patch.object(
            story_adapter.subprocess,
            "run",
            side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout='["a", "list"]', stderr=""),
        ):
            writer = story_adapter.OpenCodeStoryWriter(workspace=Path(tempfile.mkdtemp()))
            with self.assertRaises(BridgeError):
                writer({"topic": "probe"})


class FailureClassificationTests(unittest.TestCase):
    def _writer(self):
        return story_adapter.OpenCodeStoryWriter(workspace=Path(tempfile.mkdtemp()))

    def test_timeout_is_distinct(self):
        def effect(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        with mock.patch.object(story_adapter.subprocess, "run", side_effect=effect):
            with self.assertRaises(story_adapter.StoryWriterTimeoutError):
                self._writer()({"topic": "probe"})

    def test_missing_binary_is_bridge_error(self):
        with mock.patch.object(
            story_adapter.subprocess, "run", side_effect=FileNotFoundError("no opencode")
        ):
            with self.assertRaises(BridgeError) as ctx:
                self._writer()({"topic": "probe"})
            self.assertNotIsInstance(ctx.exception, story_adapter.StoryWriterTimeoutError)
            self.assertNotIsInstance(ctx.exception, story_adapter.StoryWriterUnreachableError)

    def test_reachability_pattern_is_unreachable(self):
        def effect(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ENOTFOUND api")

        with mock.patch.object(story_adapter.subprocess, "run", side_effect=effect):
            with self.assertRaises(story_adapter.StoryWriterUnreachableError):
                self._writer()({"topic": "probe"})

    def test_generic_exit_is_bridge_error_without_output_leak(self):
        def effect(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 2, stdout="usage trouble", stderr="bad flags")

        with mock.patch.object(story_adapter.subprocess, "run", side_effect=effect):
            with self.assertRaises(BridgeError) as ctx:
                self._writer()({"topic": "probe"})
            self.assertNotIsInstance(ctx.exception, story_adapter.StoryWriterTimeoutError)
            self.assertNotIsInstance(ctx.exception, story_adapter.StoryWriterUnreachableError)
            self.assertNotIn("usage trouble", str(ctx.exception))


class FactorySelectionTests(unittest.TestCase):
    def test_selects_opencode_when_configured(self):
        with mock.patch.dict(os.environ, {story_factory.STORY_PROVIDER_ENV_VAR: "opencode"}):
            name, writer = story_factory.get_story_writer()
        self.assertEqual(name, "opencode")
        self.assertIsInstance(writer, story_adapter.OpenCodeStoryWriter)

    def test_selects_claude_when_configured(self):
        with mock.patch.dict(os.environ, {story_factory.STORY_PROVIDER_ENV_VAR: "claude"}):
            name, writer = story_factory.get_story_writer()
        self.assertEqual(name, "claude")
        self.assertIsInstance(writer, HaikuStoryWriter)

    def test_repo_default_preserves_current_behavior(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(story_factory.STORY_PROVIDER_ENV_VAR, None)
            self.assertEqual(story_factory.resolve_story_provider_name(), "claude")
            name, writer = story_factory.get_story_writer()
            self.assertEqual(name, "claude")
            self.assertIsInstance(writer, HaikuStoryWriter)

    def test_unknown_provider_fails_closed_without_fallback(self):
        for bad in ("auto", "haiku", "openclaw", "both"):
            with self.assertRaises(story_factory.UnknownStoryProviderError):
                story_factory.resolve_story_provider_name(bad)
            with mock.patch.dict(os.environ, {story_factory.STORY_PROVIDER_ENV_VAR: bad}):
                with self.assertRaises(story_factory.UnknownStoryProviderError):
                    story_factory.get_story_writer()

    def test_dispatch_misconfigured_provider_fails_closed_offline(self):
        import nullone_story_scheduled_workflow  # noqa: F401
        import nullone_scheduled_run_dispatch as dispatch

        with mock.patch.dict(os.environ, {story_factory.STORY_PROVIDER_ENV_VAR: "bogus"}):
            result = dispatch.run_story_trigger({"workflow_id": "story"})
        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "STORY_PROVIDER_MISCONFIGURED")
        self.assertNotIn("bogus", result.reason_text)
        self.assertEqual(result.context.get("story_provider"), "unknown")

    def test_dispatch_stamps_story_provider_in_result_context(self):
        import nullone_scheduled_run_dispatch as dispatch

        with mock.patch.dict(os.environ, {story_factory.STORY_PROVIDER_ENV_VAR: "claude"}):
            result = dispatch.run_story_trigger({"workflow_id": "story"})
        self.assertEqual(result.reason_code, "TRIGGER_REJECTED")
        self.assertEqual(result.context.get("story_provider"), "claude")

    def test_dispatch_does_not_hard_import_claude_writer(self):
        source = (SCRIPTS / "nullone_scheduled_run_dispatch.py").read_text(encoding="utf-8")
        self.assertNotIn("HaikuStoryWriter", source)
        self.assertNotIn("nullone_claude import", source)
        self.assertIn("nullone_story_provider_factory", source)

    def test_claude_fallback_writer_preserved(self):
        self.assertTrue(callable(HaikuStoryWriter()))
        self.assertEqual(HaikuStoryWriter.model, "haiku")


def _parse_agent_permission_block() -> dict:
    text = AGENT_FILE.read_text(encoding="utf-8")
    head = text.split("---", 2)[1]
    rules: dict = {}
    current: str | None = None
    in_permission = False
    for raw in head.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            in_permission = stripped == "permission:"
            current = None
            continue
        if not in_permission:
            continue
        if indent == 2:
            if stripped.endswith(":"):
                current = stripped[:-1]
                rules[current] = []
            else:
                key, value = stripped.split(":", 1)
                rules[key.strip()] = value.strip()
        elif indent == 4 and current is not None:
            pattern, action = stripped.rsplit(":", 1)
            rules[current].append((pattern.strip().strip('"'), action.strip()))
    return rules


def _resolve_permission(rules, path: str) -> str | None:
    if isinstance(rules, str):
        return rules
    result = None
    for pattern, action in rules:
        if fnmatch.fnmatch(path, pattern):
            result = action
    return result


class AgentBoundaryTests(unittest.TestCase):
    WS = "/home/oem/.openclaw/workspace"

    PROVIDER_SOURCES = (
        SCRIPTS / "nullone_opencode_story_provider.py",
        SCRIPTS / "nullone_story_provider_factory.py",
    )

    def test_forbidden_capabilities_absent_from_provider_code(self):
        forbidden = (
            "publish",
            "zernio",
            "telegram",
            "mcp",
            "sessions_send",
            "create_review_draft",
            "draftprovider(",
            "publish_authorized",
            "github",
        )
        for path in self.PROVIDER_SOURCES:
            source = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                self.assertNotIn(token, source, msg=f"{path.name} must not reference {token!r}")

    def test_no_secret_tokens_or_auth_flags(self):
        for path in self.PROVIDER_SOURCES:
            source = path.read_text(encoding="utf-8")
            for token in ("TOKEN", "SECRET", "API_KEY", "BEARER", "PASSWORD"):
                self.assertNotIn(token, source, msg=f"{path.name} must embed no {token}")

    def test_only_selection_config_env_vars_are_read(self):
        triple_quoted = __import__("re").compile(r'""".*?"""|\'\'\'.*?\'\'\'', __import__("re").DOTALL)
        line_comment = __import__("re").compile(r"#.*")

        def code_only(source: str) -> str:
            return line_comment.sub("", triple_quoted.sub("", source))

        import re

        adapter_names = set(
            re.findall(
                r"NULLONE_[A-Z_]+",
                code_only((SCRIPTS / "nullone_opencode_story_provider.py").read_text(encoding="utf-8")),
            )
        )
        factory_names = set(
            re.findall(
                r"NULLONE_[A-Z_]+",
                code_only((SCRIPTS / "nullone_story_provider_factory.py").read_text(encoding="utf-8")),
            )
        )
        self.assertEqual(adapter_names, {"NULLONE_OPENCODE_MODEL"})
        self.assertEqual(factory_names, {"NULLONE_STORY_PROVIDER"})

    def test_writer_agent_denies_every_tool(self):
        self.assertTrue(AGENT_FILE.is_file(), msg="checked-in Story agent config missing")
        rules = _parse_agent_permission_block()
        for key in (
            "bash",
            "task",
            "skill",
            "lsp",
            "question",
            "todowrite",
            "read",
            "glob",
            "grep",
            "list",
            "webfetch",
            "websearch",
            "external_directory",
        ):
            self.assertEqual(rules.get(key), "deny", msg=f"{key} must stay denied")
        self.assertEqual(_parse_agent_permission_block()["edit"], [("*", "deny")])

    def test_writer_agent_allows_no_writes_anywhere(self):
        rules = _parse_agent_permission_block()
        for path in (
            f"{self.WS}/social/research/daily/2026-09-13-story-spec.json",
            f"{self.WS}/social/state/candidate-queue.md",
            f"{self.WS}/social/ops/scripts/x.py",
            f"{self.WS}/probe.md",
        ):
            self.assertEqual(
                _resolve_permission(rules["edit"], path), "deny", msg=f"{path} must never be writable"
            )

    def test_writer_agent_denies_secret_reads(self):
        rules = _parse_agent_permission_block()
        for path in (
            f"{self.WS}/.env",
            f"{self.WS}/.env.local",
            f"{self.WS}/social/.env",
            f"{self.WS}/deploy.key",
        ):
            self.assertEqual(
                _resolve_permission(rules["read"], path), "deny", msg=f"{path} must never be readable"
            )

    def test_agent_file_states_delivery_denials(self):
        lowered = AGENT_FILE.read_text(encoding="utf-8").lower()
        for required in ("gateway", "callback"):
            self.assertIn(required, lowered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
