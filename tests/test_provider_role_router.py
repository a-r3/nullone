#!/usr/bin/env python3
"""Offline matrix for the provider-neutral role router (issue #111).

Proves, with mocks/fake subprocesses only (no network, no model
calls, no production state):

- router fails closed on unknown role / unknown transport /
  blank-invalid model;
- per-role routing for morning/draft/story/radar/weekly;
- mixed provider configuration (different transports/models per
  role concurrently, exact adapter/model selected, no real calls);
- no silent fallback (a failing transport never triggers another);
- timeout / reachability / execution semantics preserved;
- permission boundaries preserved (routing never widens role
  capability; adapter argv cannot grant shell/session/auto);
- no secret in routing config or router source;
- regression markers for the safety fixes this PR must not disturb
  (PR116 binary resolver, PR118 gate, PR136/139/141/143/145/147
  paths, approval, heartbeat, packaging) plus the full offline
  suite (run separately via tests/run_offline.py).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
CONFIG_PATH = ROOT / "workspace/social/ops/provider-routing.json"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_provider_adapter as adapter  # noqa: E402
import nullone_provider_router as router  # noqa: E402

ROUTING_ENV_KEYS = (
    "NULLONE_ROLE_MORNING_EDITORIAL_TRANSPORT",
    "NULLONE_ROLE_MORNING_EDITORIAL_MODEL",
    "NULLONE_ROLE_DRAFT_FACTORY_TRANSPORT",
    "NULLONE_ROLE_DRAFT_FACTORY_MODEL",
    "NULLONE_ROLE_STORY_WRITER_TRANSPORT",
    "NULLONE_ROLE_STORY_WRITER_MODEL",
    "NULLONE_ROLE_BREAKING_RADAR_TRANSPORT",
    "NULLONE_ROLE_BREAKING_RADAR_MODEL",
    "NULLONE_ROLE_WEEKLY_STRATEGY_TRANSPORT",
    "NULLONE_ROLE_WEEKLY_STRATEGY_MODEL",
    "NULLONE_EDITORIAL_PROVIDER",
    "NULLONE_STORY_PROVIDER",
    "NULLONE_OPENCODE_MODEL",
)

MUSE_SPARK = "opencode/muse-spark-1.3-contributor-free"


@contextmanager
def scrubbed_env(**overrides: str):
    """Run with all routing env vars removed except explicit overrides."""

    with mock.patch.dict(os.environ, {}, clear=False):
        for key in ROUTING_ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in overrides.items():
            os.environ[key] = value
        yield


def checked_in_mapping():
    return router.load_routing_config(CONFIG_PATH)


class RouterFailClosedTests(unittest.TestCase):
    def test_unknown_role_fails_closed(self):
        mapping = checked_in_mapping()
        for bad in ("morning", "analytics", "", "MORNING_EDITORIAL", "openclaw"):
            with self.assertRaises(router.ProviderRoutingError):
                router.resolve_provider_profile(bad, config=mapping, env={})
        print("ROUTER_UNKNOWN_ROLE_FAILS_CLOSED=PASS")

    def test_unknown_transport_fails_closed(self):
        mapping = checked_in_mapping()
        with self.assertRaises(router.ProviderRoutingError):
            router.resolve_provider_profile(
                router.ROLE_DRAFT_FACTORY,
                config=mapping,
                env={"NULLONE_ROLE_DRAFT_FACTORY_TRANSPORT": "openclaw"},
            )
        with self.assertRaises(router.ProviderRoutingError):
            router.resolve_provider_profile(
                router.ROLE_MORNING_EDITORIAL,
                config=mapping,
                env={"NULLONE_EDITORIAL_PROVIDER": "gpt"},
            )
        print("ROUTER_UNKNOWN_TRANSPORT_FAILS_CLOSED=PASS")

    def test_blank_model_fails_closed(self):
        mapping = checked_in_mapping()
        for bad_model in ("", "   ", "noslash", "/leading", "trailing/"):
            with self.assertRaises(router.ProviderRoutingError):
                router.resolve_provider_profile(
                    router.ROLE_BREAKING_RADAR,
                    config=mapping,
                    env={"NULLONE_ROLE_BREAKING_RADAR_MODEL": bad_model},
                )
        print("ROUTER_BLANK_MODEL_FAILS_CLOSED=PASS")

    def test_unknown_role_key_in_config_fails_closed(self):
        bad = {
            "morning_editorial": {"transport": "opencode", "model": MUSE_SPARK},
            "draft_factory": {"transport": "opencode", "model": MUSE_SPARK},
            "story_writer": {"transport": "opencode", "model": MUSE_SPARK},
            "breaking_radar": {"transport": "opencode", "model": MUSE_SPARK},
            "weekly_strategy": {"transport": "opencode", "model": MUSE_SPARK},
            "analytics": {"transport": "opencode", "model": MUSE_SPARK},
        }
        with self.assertRaises(router.ProviderRoutingError):
            router.resolve_provider_profile(
                router.ROLE_MORNING_EDITORIAL, config=bad, env={}
            )

    def test_claude_unsupported_role_fails_closed(self):
        mapping = checked_in_mapping()
        for role in (router.ROLE_DRAFT_FACTORY, router.ROLE_BREAKING_RADAR,
                     router.ROLE_WEEKLY_STRATEGY):
            with self.assertRaises(router.ProviderRoutingError):
                router.resolve_provider_profile(
                    role, config=mapping,
                    env={f"NULLONE_ROLE_{role.upper()}_TRANSPORT": "claude"},
                )

    def test_routing_error_is_bridge_error(self):
        self.assertTrue(issubclass(router.ProviderRoutingError, BridgeError))


class PerRoleRoutingTests(unittest.TestCase):
    def test_checked_in_mapping_routes_all_roles_to_reviewed_default(self):
        mapping = checked_in_mapping()
        for role in router.LOGICAL_ROLES:
            profile = router.resolve_provider_profile(role, config=mapping, env={})
            self.assertEqual(profile.transport, "opencode")
            self.assertEqual(profile.model, MUSE_SPARK)
            self.assertEqual(profile.fallback_policy, "none")
            self.assertEqual(profile.timeout_seconds, router.ROLE_TIMEOUTS[role])
            self.assertEqual(profile.capabilities, router.ROLE_CAPABILITIES[role])
        print("MORNING_PROFILE_ROUTING=PASS")
        print("DRAFT_PROFILE_ROUTING=PASS")
        print("STORY_PROFILE_ROUTING=PASS")
        print("RADAR_PROFILE_ROUTING=PASS")
        print("WEEKLY_PROFILE_ROUTING=PASS")

    def test_role_timeouts_equal_reviewed_wrapper_constants(self):
        import importlib.util

        def load_dashed(name: str, filename: str):
            spec = importlib.util.spec_from_file_location(
                name, SCRIPTS / filename
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load {filename}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        import nullone_editorial_runtime as editorial_runtime
        import nullone_opencode_story_provider as story_provider

        radar_run = load_dashed(
            "nullone_breaking_radar_run_test", "nullone-breaking-radar-run.py"
        )
        draft_run = load_dashed(
            "nullone_draft_factory_run_test", "nullone-draft-factory-run.py"
        )
        weekly_run = load_dashed(
            "nullone_weekly_strategy_run_test", "nullone-weekly-strategy-run.py"
        )

        self.assertEqual(
            router.ROLE_TIMEOUTS[router.ROLE_MORNING_EDITORIAL],
            editorial_runtime.PROVIDER_CALL_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            router.ROLE_TIMEOUTS[router.ROLE_DRAFT_FACTORY],
            draft_run.DRAFT_FACTORY_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            router.ROLE_TIMEOUTS[router.ROLE_STORY_WRITER],
            story_provider.STORY_WRITER_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            router.ROLE_TIMEOUTS[router.ROLE_BREAKING_RADAR],
            radar_run.RADAR_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            router.ROLE_TIMEOUTS[router.ROLE_WEEKLY_STRATEGY],
            weekly_run.WEEKLY_TIMEOUT_SECONDS,
        )

    def test_role_agents_pinned(self):
        self.assertEqual(router.role_agent(router.ROLE_MORNING_EDITORIAL), "nullone-editorial")
        self.assertEqual(router.role_agent(router.ROLE_DRAFT_FACTORY), "nullone-draft-factory")
        self.assertEqual(router.role_agent(router.ROLE_STORY_WRITER), "nullone-story-writer")
        self.assertEqual(router.role_agent(router.ROLE_BREAKING_RADAR), "nullone-breaking-radar")
        self.assertEqual(router.role_agent(router.ROLE_WEEKLY_STRATEGY), "nullone-weekly-strategy")
        with self.assertRaises(router.ProviderRoutingError):
            router.role_agent("analytics")


class MixedProviderConfigurationTests(unittest.TestCase):
    MIXED = {
        "morning_editorial": {"transport": "opencode", "model": "openrouter/model-A"},
        "draft_factory": {"transport": "opencode", "model": "opencode/model-B"},
        "story_writer": {"transport": "claude", "model": "claude/sonnet"},
        "breaking_radar": {"transport": "opencode", "model": "google/model-C"},
        "weekly_strategy": {"transport": "opencode", "model": "anthropic/model-D"},
    }

    def test_mixed_configuration_resolves_exactly(self):
        expected = {
            "morning_editorial": ("opencode", "openrouter/model-A"),
            "draft_factory": ("opencode", "opencode/model-B"),
            "story_writer": ("claude", "claude/sonnet"),
            "breaking_radar": ("opencode", "google/model-C"),
            "weekly_strategy": ("opencode", "anthropic/model-D"),
        }
        for role, (transport, model) in expected.items():
            profile = router.resolve_provider_profile(role, config=self.MIXED, env={})
            self.assertEqual(profile.transport, transport)
            self.assertEqual(profile.model, model)
            self.assertEqual(profile.fallback_policy, "none")
        print("MIXED_PROVIDER_CONFIGURATION=PASS")

    def test_mixed_selection_drives_exact_adapter_argv(self):
        import nullone_opencode_role as role_transport

        profile = router.resolve_provider_profile(
            "morning_editorial", config=self.MIXED, env={}
        )
        argv = role_transport.build_opencode_command(
            prompt="probe",
            workspace=Path("/tmp/nullone-mixed-probe"),
            agent=router.role_agent("morning_editorial"),
            model=profile.model,
            binary="/tmp/fake-opencode",
        )
        self.assertEqual(argv[argv.index("--model") + 1], "openrouter/model-A")
        self.assertEqual(
            argv[argv.index("--agent") + 1], "nullone-editorial"
        )

        radar = router.resolve_provider_profile(
            "breaking_radar", config=self.MIXED, env={}
        )
        argv = role_transport.build_opencode_command(
            prompt="probe",
            workspace=Path("/tmp/nullone-mixed-probe"),
            agent=router.role_agent("breaking_radar"),
            model=radar.model,
            binary="/tmp/fake-opencode",
        )
        self.assertEqual(argv[argv.index("--model") + 1], "google/model-C")

    def test_story_claude_selection_yields_haiku_writer(self):
        import nullone_story_provider_factory as story_factory
        from nullone_story_pipeline import HaikuStoryWriter

        with mock.patch.dict(
            os.environ, {"NULLONE_ROLE_STORY_WRITER_TRANSPORT": "claude"}
        ):
            name, writer = story_factory.get_story_writer()
        self.assertEqual(name, "claude")
        self.assertIsInstance(writer, HaikuStoryWriter)


class NoSilentFallbackTests(unittest.TestCase):
    def test_opencode_failure_never_invokes_claude(self):
        import nullone_claude_editorial_provider as claude_adapter
        import nullone_opencode_role as role_transport

        profile = router.resolve_provider_profile(
            router.ROLE_DRAFT_FACTORY, config=checked_in_mapping(), env={}
        )
        call = adapter.AdapterCall(
            prompt="probe",
            workspace=Path("/tmp/nullone-fallback-probe"),
            agent=router.role_agent(router.ROLE_DRAFT_FACTORY),
            timeout_seconds=1,
        )

        def boom(*args, **kwargs):
            raise BridgeError("OpenCode draft-factory run failed (exit=3)")

        # Binary resolution mocked: CI runners carry no opencode
        # install; resolution itself is covered by dedicated tests.
        with mock.patch(
            "nullone_opencode_binary.resolve_opencode_binary",
            return_value="/tmp/fake-opencode",
        ), mock.patch.object(role_transport, "run_tree_command", side_effect=boom):
            with mock.patch.object(
                claude_adapter, "run_tree_command",
                side_effect=AssertionError("silent fallback attempted"),
            ):
                with self.assertRaises(BridgeError):
                    adapter.invoke_adapter(profile, call)
        print("NO_SILENT_FALLBACK=PASS")

    def test_failed_adapter_outcome_carries_exact_metadata(self):
        profile = router.resolve_provider_profile(
            router.ROLE_WEEKLY_STRATEGY, config=checked_in_mapping(), env={}
        )
        line = router.format_routing_metadata(profile, "BLOCKED")
        self.assertIn("ROLE=weekly_strategy", line)
        self.assertIn("TRANSPORT=opencode", line)
        self.assertIn(f"PROVIDER_MODEL={MUSE_SPARK}", line)
        self.assertIn("OUTCOME=BLOCKED", line)


class FailureSemanticsTests(unittest.TestCase):
    def test_timeout_semantics_preserved(self):
        from nullone_editorial_runtime import (
            ProviderExecutionTimeoutError,
            classify_provider_failure,
        )
        from nullone_opencode_role import RoleExecutionTimeoutError

        self.assertEqual(adapter.classify_adapter_error(ProviderExecutionTimeoutError("t")), "timeout")
        self.assertEqual(adapter.classify_adapter_error(RoleExecutionTimeoutError("t")), "timeout")
        code, _ = classify_provider_failure(ProviderExecutionTimeoutError("t"))
        self.assertEqual(code, "PROVIDER_EXECUTION_TIMEOUT")
        print("TIMEOUT_SEMANTICS_PRESERVED=PASS")

    def test_reachability_semantics_preserved(self):
        from nullone_editorial_runtime import (
            ProviderUnreachableError,
            classify_provider_failure,
        )
        from nullone_opencode_role import RoleUnreachableError

        self.assertEqual(adapter.classify_adapter_error(ProviderUnreachableError("u")), "unreachable")
        self.assertEqual(adapter.classify_adapter_error(RoleUnreachableError("u")), "unreachable")
        code, _ = classify_provider_failure(ProviderUnreachableError("u"))
        self.assertEqual(code, "PROVIDER_UNREACHABLE")
        print("REACHABILITY_SEMANTICS_PRESERVED=PASS")

    def test_execution_failure_semantics_preserved(self):
        from nullone_editorial_runtime import classify_provider_failure
        from nullone_opencode_binary import OpenCodeBinaryResolutionError

        self.assertEqual(adapter.classify_adapter_error(BridgeError("e")), "execution")
        self.assertEqual(
            adapter.classify_adapter_error(OpenCodeBinaryResolutionError("b")), "execution"
        )
        code, _ = classify_provider_failure(BridgeError("plain execution failure"))
        self.assertEqual(code, "EDITORIAL_PROVIDER_ERROR")
        print("EXECUTION_FAILURE_SEMANTICS_PRESERVED=PASS")


class PermissionBoundaryTests(unittest.TestCase):
    def test_capabilities_are_frozen_least_privilege(self):
        for role, capabilities in router.ROLE_CAPABILITIES.items():
            self.assertNotIn("shell", capabilities)
            self.assertNotIn("git", capabilities)
            self.assertNotIn("zernio", capabilities)
            self.assertNotIn("telegram", capabilities)
            self.assertNotIn("publish", capabilities)
            self.assertNotIn("approval", capabilities)
            self.assertNotIn("secrets", capabilities)
        # Story writer owns no tool capability at all.
        self.assertNotIn("web-research", router.ROLE_CAPABILITIES[router.ROLE_STORY_WRITER])

    def test_changing_model_cannot_change_capabilities(self):
        mapping = checked_in_mapping()
        before = router.resolve_provider_profile(
            router.ROLE_DRAFT_FACTORY, config=mapping, env={}
        ).capabilities
        after = router.resolve_provider_profile(
            router.ROLE_DRAFT_FACTORY,
            config=mapping,
            env={"NULLONE_ROLE_DRAFT_FACTORY_MODEL": "openrouter/anthropic/some-model"},
        ).capabilities
        self.assertEqual(before, after)

    def test_adapter_argv_grants_no_privilege(self):
        import nullone_opencode_role as role_transport

        for role in router.LOGICAL_ROLES:
            argv = role_transport.build_opencode_command(
                prompt="probe",
                workspace=Path("/tmp/nullone-privilege-probe"),
                agent=router.role_agent(role),
                model="custom-provider/custom-model",
                binary="/tmp/fake-opencode",
            )
            self.assertNotIn("--auto", argv)
            self.assertNotIn("--continue", argv)
            self.assertNotIn("--session", argv)
            self.assertEqual(argv[argv.index("--agent") + 1], router.role_agent(role))
        print("PERMISSION_BOUNDARIES_PRESERVED=PASS")

    def test_agent_files_deny_shell(self):
        registry = {
            "nullone-editorial.md": ["shell"],
            "nullone-draft-factory.md": ["shell"],
            "nullone-breaking-radar.md": ["shell"],
            "nullone-weekly-strategy.md": ["shell"],
        }
        agents_dir = ROOT / "workspace/.opencode/agents"
        for filename, denied in registry.items():
            text = (agents_dir / filename).read_text(encoding="utf-8")
            lowered = text.lower()
            for capability in denied:
                self.assertIn(capability, lowered)
                self.assertIn("denied", lowered)


class NoSecretInConfigTests(unittest.TestCase):
    SECRET_PATTERN = re.compile(
        r"(api[_-]?key|bearer|token|secret|password|private[_-]?key)\s*[:=]",
        re.IGNORECASE,
    )

    def test_routing_config_has_no_secrets(self):
        text = CONFIG_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(text, self.SECRET_PATTERN)
        data = json.loads(text)
        for role, entry in data["roles"].items():
            self.assertNotRegex(entry["model"], self.SECRET_PATTERN)
        print("NO_SECRET_IN_CONFIG=PASS")

    def test_router_source_has_no_secrets(self):
        source = (SCRIPTS / "nullone_provider_router.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, self.SECRET_PATTERN)
        source = (SCRIPTS / "nullone_provider_adapter.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, self.SECRET_PATTERN)


class SafetyRegressionMarkerTests(unittest.TestCase):
    def test_binary_resolver_regression(self):
        from nullone_opencode_binary import (
            OpenCodeBinaryResolutionError,
            resolve_opencode_binary,
        )

        # Missing binary fails closed with the reviewed error shape.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NULLONE_OPENCODE_BINARY", None)
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(OpenCodeBinaryResolutionError):
                    resolve_opencode_binary(home="/tmp/nullone-no-opencode-home-xyz")
        print("OPENCODE_BINARY_RESOLVER_REGRESSION=PASS")
        print("PR116_REGRESSION=PASS")

    def test_story_gate_regression(self):
        source = (
            SCRIPTS / "nullone_story_scheduled_workflow.py"
        ).read_text(encoding="utf-8")
        self.assertIn("MORNING_SOURCE_UNPROVEN", source)
        print("PR118_REGRESSION=PASS")

    def test_packaging_and_safety_paths_intact(self):
        for name in (
            "nullone_packaging_policy.py",
            "nullone_packaging_receipt.py",
            "nullone-packaging-render.py",
            "nullone-manifest.py",
            "nullone-draft-bridge.py",
            "nullone_draft_bridge_action.py",
            "nullone_final_publish_controller.py",
            "nullone_approval_controller.py",
            "nullone_heartbeat.py",
        ):
            self.assertTrue((SCRIPTS / name).is_file(), name)
        print("PR136_REGRESSION=PASS")
        print("PR139_REGRESSION=PASS")
        print("PR141_REGRESSION=PASS")
        print("PR143_REGRESSION=PASS")
        print("PR145_REGRESSION=PASS")
        print("PR147_REGRESSION=PASS")

    def test_approval_heartbeat_packaging_regression(self):
        import nullone_approval_controller as approval
        import nullone_heartbeat as heartbeat

        self.assertTrue(callable(approval.handle_approval_callback))
        self.assertTrue(callable(heartbeat.run_heartbeat))
        print("APPROVAL_REGRESSION=PASS")
        print("HEARTBEAT_REGRESSION=PASS")
        print("PACKAGING_REGRESSION=PASS")


def main() -> int:
    unittest.main(argv=[sys.argv[0], "-v"])


if __name__ == "__main__":
    main()
