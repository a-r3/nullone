#!/usr/bin/env python3
"""Offline tests for the provider-neutral editorial transport layer.

Proves, with mocks/fake subprocesses only (no network, no model
calls, no production state):

A. factory selects opencode when configured
B. factory selects claude when configured, and defaults to claude
   (safe compatibility) when unset/blank
C. unknown provider fails closed (factory raises; dispatch returns a
   FAILED orchestration result; nothing is invoked)
D. Morning/scheduled workflows no longer hard-import the Claude
   transport adapter
E. OpenCode command construction is deterministic (byte-for-byte argv)
F. workspace cwd (and --dir) is the exact workspace path
G. whole-process timeout maps to ProviderExecutionTimeoutError
H. startup/exit failures map correctly: missing binary is BridgeError
   (never reachability); reachability-pattern output is
   ProviderUnreachableError; other non-zero exits are BridgeError
I. execution timeout remains non-retryable in the Morning runtime
J. reachability remains retryable in the Morning runtime
K. Claude adapter preserved (importable, same timeout constant)
L. OpenCode adapter cannot request broad/forbidden capabilities
   (no --auto, pinned narrow agent, agent file denies shell)
M. no draft/post/notify capability enters the provider config
N. no secrets embedded in the provider modules
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
AGENT_FILE = ROOT / "workspace/.opencode/agents/nullone-editorial.md"
sys.path.insert(0, str(SCRIPTS))

from nullone_editorial_runtime import (  # noqa: E402
    MAX_ATTEMPTS,
    PROVIDER_CALL_TIMEOUT_SECONDS,
    ProviderExecutionTimeoutError,
    ProviderUnreachableError,
    classify_provider_failure,
    run_morning_editorial,
)
import nullone_claude_editorial_provider as claude_adapter  # noqa: E402
import nullone_editorial_provider_factory as factory  # noqa: E402
import nullone_opencode_editorial_provider as opencode_adapter  # noqa: E402
from support.morning_artifacts import (  # noqa: E402
    write_board_only,
    write_morning_artifacts,
)


def _resolve_with_env(value: str | None) -> str:
    """Resolve the provider name with NULLONE_EDITORIAL_PROVIDER controlled."""

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(factory.EDITORIAL_PROVIDER_ENV_VAR, None)
        if value is not None:
            os.environ[factory.EDITORIAL_PROVIDER_ENV_VAR] = value
        return factory.resolve_editorial_provider_name()


class FactorySelectionTests(unittest.TestCase):
    def test_selects_opencode_when_configured(self):
        with mock.patch.dict(
            os.environ, {factory.EDITORIAL_PROVIDER_ENV_VAR: "opencode"}
        ):
            name, invoke = factory.get_editorial_provider()
        self.assertEqual(name, "opencode")
        self.assertIs(invoke, opencode_adapter.default_invoke_provider)

    def test_selects_claude_when_configured(self):
        with mock.patch.dict(
            os.environ, {factory.EDITORIAL_PROVIDER_ENV_VAR: "claude"}
        ):
            name, invoke = factory.get_editorial_provider()
        self.assertEqual(name, "claude")
        self.assertIs(invoke, claude_adapter.default_invoke_provider)

    def test_repo_default_preserves_claude_compatibility(self):
        self.assertEqual(_resolve_with_env(None), "claude")
        self.assertEqual(_resolve_with_env(""), "claude")
        self.assertEqual(_resolve_with_env("   "), "claude")

    def test_selection_is_case_and_whitespace_insensitive(self):
        self.assertEqual(_resolve_with_env("OpEnCoDe"), "opencode")
        self.assertEqual(_resolve_with_env("  claude  "), "claude")

    def test_unknown_provider_fails_closed(self):
        for bad in ("auto", "both", "openclaw", "claude-code", "gpt"):
            with self.assertRaises(factory.UnknownEditorialProviderError):
                factory.resolve_editorial_provider_name(bad)
            with mock.patch.dict(
                os.environ, {factory.EDITORIAL_PROVIDER_ENV_VAR: bad}
            ):
                with self.assertRaises(factory.UnknownEditorialProviderError):
                    factory.get_editorial_provider()

    def test_unknown_provider_error_text_is_fixed(self):
        marker = "should-never-appear-in-error-text-123"
        try:
            factory.resolve_editorial_provider_name(marker)
        except factory.UnknownEditorialProviderError as exc:
            self.assertNotIn(marker, str(exc))
            self.assertIn("opencode", str(exc))
            self.assertIn("claude", str(exc))
        else:
            raise AssertionError("unknown provider did not fail closed")


class WorkflowsAreProviderNeutralTests(unittest.TestCase):
    def test_morning_runner_does_not_hard_import_claude_transport(self):
        source = (SCRIPTS / "nullone-morning-editorial-run.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("nullone_claude_editorial_provider", source)
        self.assertNotIn("from nullone_claude import", source)
        self.assertIn("nullone_editorial_provider_factory", source)
        self.assertIn("EDITORIAL_PROVIDER=", source)

    def test_dispatch_does_not_hard_import_claude_transport(self):
        source = (SCRIPTS / "nullone_scheduled_run_dispatch.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("nullone_claude_editorial_provider", source)
        self.assertNotIn("from nullone_claude import", source)
        self.assertIn("nullone_editorial_provider_factory", source)

    def test_dispatch_module_has_no_direct_provider_binding(self):
        import nullone_scheduled_run_dispatch as dispatch

        self.assertFalse(hasattr(dispatch, "default_invoke_provider"))

    def test_dispatch_misconfigured_provider_fails_closed_offline(self):
        import nullone_scheduled_run_dispatch as dispatch

        with mock.patch.dict(
            os.environ, {factory.EDITORIAL_PROVIDER_ENV_VAR: "bogus-transport"}
        ):
            result = dispatch.run_morning_trigger({"workflow_id": "morning-editorial"})
        self.assertEqual(result.application_execution, "FAILED")
        self.assertEqual(result.reason_code, "EDITORIAL_PROVIDER_MISCONFIGURED")
        self.assertNotIn("bogus-transport", result.reason_text)
        self.assertEqual(result.context.get("editorial_provider"), "unknown")

    def test_dispatch_stamps_provider_name_in_result_context(self):
        import nullone_scheduled_run_dispatch as dispatch

        with mock.patch.dict(
            os.environ, {factory.EDITORIAL_PROVIDER_ENV_VAR: "claude"}
        ):
            result = dispatch.run_morning_trigger({"workflow_id": "morning-editorial"})
        # Invalid trigger is rejected before any provider call (offline),
        # but the resolved transport must still be visible in metadata.
        self.assertEqual(result.reason_code, "TRIGGER_REJECTED")
        self.assertEqual(result.context.get("editorial_provider"), "claude")


class OpenCodeCommandConstructionTests(unittest.TestCase):
    def test_argv_is_deterministic_and_exact(self):
        workspace = Path("/tmp/nullone-opencode-determinism-check")
        first = opencode_adapter.build_opencode_command(
            prompt="probe",
            workspace=workspace,
            model="opencode/muse-spark-1.3-contributor-free",
        )
        second = opencode_adapter.build_opencode_command(
            prompt="probe",
            workspace=workspace,
            model="opencode/muse-spark-1.3-contributor-free",
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            [
                "opencode",
                "run",
                "probe",
                "--agent",
                "nullone-editorial",
                "--model",
                "opencode/muse-spark-1.3-contributor-free",
                "--format",
                "json",
                "--dir",
                "/tmp/nullone-opencode-determinism-check",
            ],
        )

    def test_no_session_continuation_or_auto_approval_flags(self):
        argv = opencode_adapter.build_opencode_command(
            prompt="probe",
            workspace=Path("/tmp/x"),
            model="opencode/muse-spark-1.3-contributor-free",
        )
        for forbidden in ("--auto", "--continue", "--session", "--fork", "--share"):
            self.assertNotIn(forbidden, argv)

    def test_workspace_cwd_and_dir_are_exact(self):
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        workspace = Path(tempfile.mkdtemp(prefix="nullone-opencode-cwd-"))
        with mock.patch.object(
            opencode_adapter.subprocess, "run", side_effect=fake_run
        ):
            opencode_adapter.default_invoke_provider(
                prompt="probe",
                workspace=workspace,
                timeout=30,
            )
        self.assertEqual(captured["kwargs"]["cwd"], workspace)
        argv = captured["cmd"]
        self.assertEqual(
            argv[argv.index("--dir") + 1],
            str(workspace),
        )
        self.assertEqual(captured["kwargs"]["timeout"], 30)
        self.assertTrue(captured["kwargs"]["capture_output"])
        self.assertNotIn("--auto", argv)

    def test_model_override_mechanism(self):
        self.assertEqual(
            opencode_adapter.resolve_opencode_model("  "), opencode_adapter.DEFAULT_OPENCODE_MODEL
        )
        with mock.patch.dict(
            os.environ, {opencode_adapter.OPENCODE_MODEL_ENV_VAR: "nvidia/meta/llama-3.1-8b-instruct"}
        ):
            self.assertEqual(
                opencode_adapter.resolve_opencode_model(),
                "nvidia/meta/llama-3.1-8b-instruct",
            )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(opencode_adapter.OPENCODE_MODEL_ENV_VAR, None)
            self.assertEqual(
                opencode_adapter.resolve_opencode_model(),
                opencode_adapter.DEFAULT_OPENCODE_MODEL,
            )


class OpenCodeFailureMappingTests(unittest.TestCase):
    def _invoke_with(self, effect, **kwargs):
        workspace = Path(tempfile.mkdtemp(prefix="nullone-opencode-fail-"))
        with mock.patch.object(
            opencode_adapter.subprocess, "run", side_effect=effect
        ):
            opencode_adapter.default_invoke_provider(
                prompt="probe", workspace=workspace, timeout=30, **kwargs
            )

    def test_timeout_maps_to_execution_timeout(self):
        def effect(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        with self.assertRaises(ProviderExecutionTimeoutError):
            self._invoke_with(effect)

    def test_missing_binary_is_bridge_error_not_reachability(self):
        with self.assertRaises(opencode_adapter.BridgeError) as ctx:
            self._invoke_with(FileNotFoundError("no opencode"))
        self.assertNotIsInstance(ctx.exception, ProviderUnreachableError)
        self.assertNotIsInstance(ctx.exception, ProviderExecutionTimeoutError)

    def test_reachability_pattern_maps_to_unreachable(self):
        def effect(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Error: ENOTFOUND api.opencode.ai"
            )

        with self.assertRaises(ProviderUnreachableError):
            self._invoke_with(effect)

    def test_generic_nonzero_exit_is_bridge_error(self):
        def effect(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 2, stdout="usage error", stderr="bad flags"
            )

        with self.assertRaises(opencode_adapter.BridgeError) as ctx:
            self._invoke_with(effect)
        self.assertNotIsInstance(ctx.exception, ProviderUnreachableError)
        self.assertNotIsInstance(ctx.exception, ProviderExecutionTimeoutError)
        # Raw child output must not leak into the error text.
        self.assertNotIn("usage error", str(ctx.exception))
        self.assertNotIn("bad flags", str(ctx.exception))

    def test_success_returns_quietly(self):
        def effect(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        self._invoke_with(effect)


class MorningRetrySemanticsPreservedTests(unittest.TestCase):
    def test_execution_timeout_remains_non_retryable(self):
        code, _ = classify_provider_failure(
            ProviderExecutionTimeoutError("deadline")
        )
        self.assertEqual(code, "PROVIDER_EXECUTION_TIMEOUT")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls: list[int] = []
            sleeps: list[float] = []

            def invoke() -> None:
                calls.append(1)
                raise ProviderExecutionTimeoutError("deadline")

            result = run_morning_editorial(
                occurrence_id="2026-09-12T08:30:00+04:00",
                board_date="2026-09-12",
                invoke_provider=invoke,
                sleep=sleeps.append,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )
            self.assertEqual(result["reason_code"], "PROVIDER_EXECUTION_TIMEOUT")
            self.assertEqual(len(calls), 1)
            self.assertEqual(sleeps, [])

    def test_reachability_remains_retryable(self):
        code, _ = classify_provider_failure(
            ProviderUnreachableError("ENOTFOUND")
        )
        self.assertEqual(code, "PROVIDER_UNREACHABLE")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls: list[int] = []

            def invoke() -> None:
                calls.append(1)
                raise ProviderUnreachableError("ENOTFOUND")

            result = run_morning_editorial(
                occurrence_id="2026-09-12T08:30:00+04:00",
                board_date="2026-09-12",
                invoke_provider=invoke,
                sleep=lambda _s: None,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )
            self.assertEqual(result["reason_code"], "PROVIDER_UNREACHABLE")
            self.assertEqual(len(calls), MAX_ATTEMPTS)

    def test_opencode_success_still_requires_valid_handoff(self):
        # A provider that exits 0 but writes only the board must fail
        # closed as HANDOFF_INCOMPLETE, never as success.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def invoke() -> None:
                write_board_only(root, "2026-09-12")

            result = run_morning_editorial(
                occurrence_id="2026-09-12T08:30:00+04:00",
                board_date="2026-09-12",
                invoke_provider=invoke,
                sleep=lambda _s: None,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )
            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["reason_code"], "HANDOFF_INCOMPLETE")

    def test_opencode_success_with_valid_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def invoke() -> None:
                write_morning_artifacts(root, "2026-09-12")

            result = run_morning_editorial(
                occurrence_id="2026-09-12T08:30:00+04:00",
                board_date="2026-09-12",
                invoke_provider=invoke,
                sleep=lambda _s: None,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )
            self.assertEqual(result["domain_outcome"], "SUCCEEDED")


class ClaudeAdapterPreservedTests(unittest.TestCase):
    def test_claude_adapter_still_exposes_default_provider(self):
        self.assertTrue(callable(claude_adapter.default_invoke_provider))

    def test_shared_timeout_budget_unchanged(self):
        self.assertEqual(PROVIDER_CALL_TIMEOUT_SECONDS, 600)

    def test_claude_timeout_mapping_unchanged(self):
        def effect(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        with mock.patch.object(claude_adapter.subprocess, "run", side_effect=effect):
            with self.assertRaises(ProviderExecutionTimeoutError):
                claude_adapter.default_invoke_provider()


class CapabilityBoundaryTests(unittest.TestCase):
    PROVIDER_SOURCES = (
        SCRIPTS / "nullone_opencode_editorial_provider.py",
        SCRIPTS / "nullone_editorial_provider_factory.py",
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
                self.assertNotIn(
                    token,
                    source,
                    msg=f"{path.name} must not reference {token!r}",
                )

    def test_no_secret_tokens_or_auth_flags(self):
        for path in self.PROVIDER_SOURCES:
            source = path.read_text(encoding="utf-8")
            for token in ("TOKEN", "SECRET", "API_KEY", "BEARER", "PASSWORD"):
                self.assertNotIn(
                    token, source, msg=f"{path.name} must embed no {token}"
                )
        argv = opencode_adapter.build_opencode_command(
            prompt="probe",
            workspace=Path("/tmp/x"),
            model="opencode/muse-spark-1.3-contributor-free",
        )
        for flag in ("--password", "--username", "--attach"):
            self.assertNotIn(flag, argv)

    def test_only_selection_config_env_vars_are_read(self):
        import re

        triple_quoted = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)
        line_comment = re.compile(r"#.*")

        def code_only(source: str) -> str:
            return line_comment.sub("", triple_quoted.sub("", source))

        adapter_names = set(
            re.findall(
                r"NULLONE_[A-Z_]+",
                code_only(
                    (SCRIPTS / "nullone_opencode_editorial_provider.py").read_text(
                        encoding="utf-8"
                    )
                ),
            )
        )
        factory_names = set(
            re.findall(
                r"NULLONE_[A-Z_]+",
                code_only(
                    (SCRIPTS / "nullone_editorial_provider_factory.py").read_text(
                        encoding="utf-8"
                    )
                ),
            )
        )
        self.assertEqual(adapter_names, {"NULLONE_OPENCODE_MODEL"})
        self.assertEqual(factory_names, {"NULLONE_EDITORIAL_PROVIDER"})

    def test_agent_file_exists_with_narrow_boundary(self):
        self.assertTrue(AGENT_FILE.is_file(), msg="checked-in agent config missing")
        text = AGENT_FILE.read_text(encoding="utf-8")
        head, _, _ = text.split("---", 2)[1].partition("---")
        for required in (
            "bash: deny",
            "task: deny",
            "skill: deny",
            "external_directory: deny",
            "edit: allow",
            "read: allow",
            "webfetch: allow",
            "websearch: allow",
        ):
            self.assertIn(required, head)
        self.assertNotIn("bash: allow", head)
        self.assertNotIn('"*": "allow"', head)
        self.assertNotIn("--auto", head)

    def test_agent_file_states_production_denials(self):
        lowered = AGENT_FILE.read_text(encoding="utf-8").lower()
        for required in ("zernio", "telegram", "gateway", "verification: pass"):
            self.assertIn(required, lowered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
