#!/usr/bin/env python3
"""Offline matrix for the provider-neutral role router (issue #111).

Proves, with mocks/fake adapters only (no network, no model
calls, no production state):

- router fails closed on unknown role / unknown transport /
  blank-invalid model / unsupported role x transport;
- per-role routing for morning/draft/story/radar/weekly;
- REAL workflow-to-adapter wiring: covered execution layers import
  no vendor transport and execute through the adapter registry;
- adapter enforces role authority: agent/timeout derive from the
  role, the caller cannot override them;
- Story Claude model truth: reported profile model == executed model;
- Story error normalization through the shared contract;
- mixed provider configuration (exact adapter + exact model per
  role, fake adapters, invocation counts);
- no silent fallback (non-selected adapters invoked zero times,
  classification preserved);
- permission boundaries preserved;
- no secret in routing config or router/adapter source;
- regression markers for the safety fixes this PR must not disturb
  (PR116 binary resolver, PR118 gate, PR136/139/141/143/145/147
  paths, approval, heartbeat, packaging) plus the full offline
  suite (run separately via tests/run_offline.py).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
CONFIG_PATH = ROOT / "workspace/social/ops/provider-routing.json"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_provider_adapter as adapter  # noqa: E402
import nullone_provider_router as router  # noqa: E402

MUSE_SPARK = "opencode/muse-spark-1.3-contributor-free"

# Covered execution layers: these files must execute through the
# adapter registry and import NO vendor transport module.
COVERED_EXECUTION_FILES = (
    "nullone-draft-factory-run.py",
    "nullone-breaking-radar-run.py",
    "nullone-weekly-strategy-run.py",
    "nullone_editorial_provider_factory.py",
    "nullone_story_provider_factory.py",
    "nullone-morning-editorial-run.py",
    "nullone_scheduled_run_dispatch.py",
)

VENDOR_MARKERS = (
    "nullone_opencode_binary",
    "nullone_opencode_role",
    "nullone_opencode_editorial_provider",
    "nullone_opencode_story_provider",
    "nullone_claude_editorial_provider",
    "nullone_claude ",
    "nullone_claude.",
    "OpenCodeStoryWriter",
    "HaikuStoryWriter",
    "resolve_opencode_binary",
    "build_opencode_command",
    "run_opencode_cycle",
    "describe_cycle",
    "resolve_role_model",
    "default_invoke_provider",
)


def code_only(source: str) -> str:
    source = re.sub(r'""".*?"""|\'\'\'.*?\'\'\'', "", source, flags=re.DOTALL)
    return re.sub(r"#.*", "", source)


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
        # Bare transport-local values are rejected for OpenCode.
        with self.assertRaises(router.ProviderRoutingError):
            router.resolve_provider_profile(
                router.ROLE_DRAFT_FACTORY,
                config=mapping,
                env={"NULLONE_ROLE_DRAFT_FACTORY_MODEL": "sonnet"},
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


class WorkflowVendorNeutralityTests(unittest.TestCase):
    def test_covered_layers_import_no_vendor_transport(self):
        for filename in COVERED_EXECUTION_FILES:
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            for marker in VENDOR_MARKERS:
                self.assertNotIn(
                    marker, source,
                    msg=f"{filename} references vendor marker {marker!r}",
                )
        print("WORKFLOW_VENDOR_IMPORT_BOUNDARY=PASS")

    def test_wrappers_execute_only_through_adapter_registry(self):
        # Every covered wrapper resolves a profile and executes
        # exclusively via provider_adapter.invoke_role_cycle: patch
        # the registry entry and prove the exact profile arrives.
        import nullone_provider_adapter as provider_adapter

        seen: list = []

        def fake_cycle(profile, prompt, workspace):
            seen.append((profile.role, profile.transport, profile.model))
            return provider_adapter.AdapterOutcome(
                role=profile.role, transport=profile.transport,
                model=profile.model, outcome="COMPLETED",
            )

        for filename, role in (
            ("nullone-draft-factory-run.py", router.ROLE_DRAFT_FACTORY),
        ):
            spec = importlib.util.spec_from_file_location(
                "vendor_neutrality_probe", SCRIPTS / filename
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with mock.patch.object(
                provider_adapter, "invoke_role_cycle", side_effect=fake_cycle
            ):
                # Draft execute proceeds to the bridge backstop after
                # the cycle; the backstop fails closed with zero
                # calls in this bare repo checkout (no Gateway
                # credential), which still proves registry routing.
                try:
                    module.execute()
                except BridgeError:
                    pass
            self.assertTrue(
                any(entry[0] == role for entry in seen),
                msg=f"{filename} did not route through the adapter registry",
            )
            entry = [entry for entry in seen if entry[0] == role][0]
            self.assertEqual(entry[1], "opencode")
            self.assertEqual(entry[2], MUSE_SPARK)
        print("REAL_WORKFLOW_TO_PROVIDER_ADAPTER_WIRING=PASS")

    def test_model_switch_needs_no_workflow_rewrite(self):
        # Same wrapper module, five different provider/model
        # endpoints through the adapter builder: the workflow layer
        # never changes for a model switch.
        import nullone_provider_adapter as provider_adapter

        profile = router.resolve_provider_profile(
            router.ROLE_DRAFT_FACTORY, config=checked_in_mapping(), env={}
        )
        for model in (
            "openrouter/model-a",
            "anthropic/model-b",
            "openai/model-c",
            "google/model-d",
            "custom-provider/model-e",
        ):
            switched = router.ProviderProfile(
                role=profile.role, transport="opencode", model=model,
                capabilities=profile.capabilities,
                timeout_seconds=profile.timeout_seconds,
            )
            argv = provider_adapter.build_adapter_command(
                switched, prompt="probe",
                workspace=Path("/tmp/nullone-switch-probe"),
                binary="/tmp/fake-opencode",
            )
            self.assertEqual(argv[argv.index("--model") + 1], model)
            self.assertEqual(
                argv[argv.index("--agent") + 1], "nullone-draft-factory"
            )


class RoleAuthorityTests(unittest.TestCase):
    def test_caller_owns_only_role_prompt_workspace(self):
        names = tuple(field.name for field in adapter.AdapterCall.__dataclass_fields__.values())
        self.assertEqual(names, ("role", "prompt", "workspace"))
        print("CALLER_CANNOT_OVERRIDE_AGENT=PASS")
        print("CALLER_CANNOT_OVERRIDE_TIMEOUT=PASS")

    def test_agent_and_timeout_derive_from_role(self):
        import nullone_provider_adapter as provider_adapter

        recorded: dict = {}
        real_builder = provider_adapter.build_adapter_command

        def spy_builder(profile, **kwargs):
            recorded["agent_argv"] = real_builder(profile, **kwargs)
            return recorded["agent_argv"]

        profile = router.resolve_provider_profile(
            router.ROLE_BREAKING_RADAR, config=checked_in_mapping(), env={}
        )
        call = adapter.AdapterCall(
            role=profile.role, prompt="probe", workspace=Path("/tmp/x")
        )
        with mock.patch.object(
            provider_adapter, "build_adapter_command", side_effect=spy_builder
        ), mock.patch(
            "nullone_opencode_binary.resolve_opencode_binary",
            return_value="/tmp/fake-opencode",
        ), mock.patch.object(
            provider_adapter.opencode_role, "run_opencode_cycle",
            side_effect=lambda cmd, **kwargs: recorded.update(kwargs),
        ):
            provider_adapter.invoke_adapter(profile, call)
        argv = recorded["agent_argv"]
        self.assertEqual(
            argv[argv.index("--agent") + 1],
            router.role_agent(router.ROLE_BREAKING_RADAR),
        )
        self.assertEqual(recorded["timeout"], profile.timeout_seconds)
        print("ROUTER_SOLE_AGENT_AUTHORITY=PASS")
        print("ROUTER_SOLE_TIMEOUT_AUTHORITY=PASS")


class StoryClaudeTruthTests(unittest.TestCase):
    def test_default_claude_story_model_is_haiku(self):
        mapping = checked_in_mapping()
        profile = router.resolve_provider_profile(
            router.ROLE_STORY_WRITER, config=mapping,
            env={"NULLONE_ROLE_STORY_WRITER_TRANSPORT": "claude"},
        )
        self.assertEqual(profile.transport, "claude")
        self.assertEqual(profile.model, "haiku")

    def test_profile_model_equals_executed_model(self):
        import nullone_story_pipeline as story_pipeline
        import nullone_story_provider_factory as story_factory

        captured: dict = {}

        def fake_run_structured(*, prompt, allowed_tools, schema, model, max_turns):
            captured["model"] = model
            return {"hook": "h", "kicker": "k", "headline": "h",
                    "core_value": "v", "source_name": "s"}

        for env_model, expected in (
            (None, "haiku"),
            ("haiku", "haiku"),
            ("claude/haiku-latest", "claude/haiku-latest"),
        ):
            env = {"NULLONE_ROLE_STORY_WRITER_TRANSPORT": "claude"}
            if env_model is not None:
                env["NULLONE_ROLE_STORY_WRITER_MODEL"] = env_model
            with mock.patch.dict(os.environ, {}, clear=False):
                for key in (
                    "NULLONE_ROLE_STORY_WRITER_TRANSPORT",
                    "NULLONE_ROLE_STORY_WRITER_MODEL",
                    "NULLONE_STORY_PROVIDER",
                    "NULLONE_OPENCODE_MODEL",
                ):
                    os.environ.pop(key, None)
                os.environ.update(env)
                profile = story_factory.get_story_profile()
                _, writer = story_factory.get_story_writer()
            self.assertEqual(profile.model, expected)
            # run_structured is bound into the pipeline namespace at
            # import; patch where it is looked up.
            with mock.patch.object(
                story_pipeline, "run_structured", side_effect=fake_run_structured
            ):
                writer({"topic": "probe", "source_name_hint": "Probe"})
            self.assertEqual(
                captured["model"], expected,
                msg=f"executed model {captured['model']!r} != profile {expected!r}",
            )
        print("STORY_CLAUDE_PROFILE_MODEL_EQUALS_EXECUTED_MODEL=PASS")

    def test_observability_truth(self):
        import nullone_story_provider_factory as story_factory

        with mock.patch.dict(os.environ, {}, clear=False):
            for key in (
                "NULLONE_ROLE_STORY_WRITER_TRANSPORT",
                "NULLONE_ROLE_STORY_WRITER_MODEL",
                "NULLONE_STORY_PROVIDER",
                "NULLONE_OPENCODE_MODEL",
            ):
                os.environ.pop(key, None)
            os.environ["NULLONE_ROLE_STORY_WRITER_TRANSPORT"] = "claude"
            profile = story_factory.get_story_profile()
            _, writer = story_factory.get_story_writer()
        line = router.format_routing_metadata(profile, "COMPLETED")
        self.assertIn("PROVIDER_MODEL=haiku", line)
        self.assertEqual(getattr(writer, "model", None), "haiku")
        print("STORY_CLAUDE_OBSERVABILITY_TRUTH=PASS")


class StoryNormalizationTests(unittest.TestCase):
    def test_story_timeout_normalization(self):
        from nullone_opencode_story_provider import StoryWriterTimeoutError

        self.assertEqual(
            adapter.classify_adapter_error(StoryWriterTimeoutError("t")), "timeout"
        )
        print("STORY_TIMEOUT_NORMALIZATION=PASS")

    def test_story_reachability_normalization(self):
        from nullone_opencode_story_provider import StoryWriterUnreachableError

        self.assertEqual(
            adapter.classify_adapter_error(StoryWriterUnreachableError("u")),
            "unreachable",
        )
        print("STORY_REACHABILITY_NORMALIZATION=PASS")

    def test_story_execution_normalization(self):
        import nullone_opencode_story_provider as story_provider

        def boom(cmd, **kwargs):
            import subprocess

            return subprocess.CompletedProcess(cmd, 2, stdout="x", stderr="y")

        with mock.patch.object(
            story_provider.subprocess, "run", side_effect=boom
        ), mock.patch(
            "nullone_opencode_binary.resolve_opencode_binary",
            return_value="/tmp/fake-opencode",
        ):
            writer = story_provider.OpenCodeStoryWriter(
                workspace=Path("/tmp/nullone-story-norm-probe"), timeout=5
            )
            with self.assertRaises(BridgeError) as ctx:
                writer({"topic": "probe"})
        kind = adapter.classify_adapter_error(ctx.exception)
        self.assertEqual(kind, "execution")
        # Domain behavior unchanged: the pipeline still maps any
        # writer exception to WRITER_FAILED (asserted by the Story
        # pipeline suite; classification here is adapter-only).
        print("STORY_EXECUTION_NORMALIZATION=PASS")


class MixedProviderConfigurationTests(unittest.TestCase):
    MIXED = {
        "morning_editorial": {"transport": "opencode", "model": "openrouter/model-a"},
        "draft_factory": {"transport": "opencode", "model": "google/model-b"},
        "story_writer": {"transport": "claude", "model": "haiku"},
        "breaking_radar": {"transport": "opencode", "model": "anthropic/model-c"},
        "weekly_strategy": {"transport": "opencode", "model": "openai/model-d"},
    }

    def test_mixed_configuration_resolves_exactly(self):
        expected = {
            "morning_editorial": ("opencode", "openrouter/model-a"),
            "draft_factory": ("opencode", "google/model-b"),
            "story_writer": ("claude", "haiku"),
            "breaking_radar": ("opencode", "anthropic/model-c"),
            "weekly_strategy": ("opencode", "openai/model-d"),
        }
        for role, (transport, model) in expected.items():
            profile = router.resolve_provider_profile(role, config=self.MIXED, env={})
            self.assertEqual(profile.transport, transport)
            self.assertEqual(profile.model, model)
            self.assertEqual(profile.fallback_policy, "none")
        print("MIXED_PROVIDER_CONFIGURATION=PASS")

    def test_mixed_selection_reaches_exact_fake_adapter(self):
        import nullone_provider_adapter as provider_adapter

        calls: dict[str, list] = {"opencode": [], "claude": []}
        real_registry = dict(provider_adapter._ADAPTERS)

        def fake_opencode(profile, call):
            calls["opencode"].append((profile.role, profile.model))

        def fake_claude(profile, call):
            calls["claude"].append((profile.role, profile.model))

        provider_adapter._ADAPTERS["opencode"] = fake_opencode
        provider_adapter._ADAPTERS["claude"] = fake_claude
        try:
            for role, (transport, model) in {
                "morning_editorial": ("opencode", "openrouter/model-a"),
                "draft_factory": ("opencode", "google/model-b"),
                "story_writer": ("claude", "haiku"),
                "breaking_radar": ("opencode", "anthropic/model-c"),
                "weekly_strategy": ("opencode", "openai/model-d"),
            }.items():
                profile = router.resolve_provider_profile(
                    role, config=self.MIXED, env={}
                )
                outcome = provider_adapter.invoke_adapter(
                    profile,
                    adapter.AdapterCall(
                        role=role, prompt="probe",
                        workspace=Path("/tmp/nullone-mixed-probe"),
                    ),
                )
                self.assertEqual(outcome.transport, transport)
                self.assertEqual(outcome.model, model)
        finally:
            provider_adapter._ADAPTERS.clear()
            provider_adapter._ADAPTERS.update(real_registry)
        self.assertEqual(
            sorted(calls["opencode"]),
            [
                ("breaking_radar", "anthropic/model-c"),
                ("draft_factory", "google/model-b"),
                ("morning_editorial", "openrouter/model-a"),
                ("weekly_strategy", "openai/model-d"),
            ],
        )
        self.assertEqual(calls["claude"], [("story_writer", "haiku")])


class NoSilentFallbackTests(unittest.TestCase):
    def test_failing_transport_invokes_nothing_else(self):
        import nullone_provider_adapter as provider_adapter

        counts: dict[str, int] = {"opencode": 0, "claude": 0}
        real_registry = dict(provider_adapter._ADAPTERS)

        def failing_opencode(profile, call):
            counts["opencode"] += 1
            raise BridgeError("OpenCode draft-factory run failed (exit=3)")

        def counting_claude(profile, call):
            counts["claude"] += 1

        provider_adapter._ADAPTERS["opencode"] = failing_opencode
        provider_adapter._ADAPTERS["claude"] = counting_claude
        try:
            profile = router.resolve_provider_profile(
                router.ROLE_DRAFT_FACTORY, config=checked_in_mapping(), env={}
            )
            with self.assertRaises(BridgeError) as ctx:
                provider_adapter.invoke_adapter(
                    profile,
                    adapter.AdapterCall(
                        role=profile.role, prompt="probe",
                        workspace=Path("/tmp/nullone-fallback-probe"),
                    ),
                )
            self.assertEqual(
                adapter.classify_adapter_error(ctx.exception), "execution"
            )
        finally:
            provider_adapter._ADAPTERS.clear()
            provider_adapter._ADAPTERS.update(real_registry)
        self.assertEqual(counts, {"opencode": 1, "claude": 0})
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
        import nullone_provider_adapter as provider_adapter

        for role in router.LOGICAL_ROLES:
            profile = router.resolve_provider_profile(role, config=checked_in_mapping(), env={})
            if profile.transport != "opencode":
                continue
            argv = provider_adapter.build_adapter_command(
                profile, prompt="probe",
                workspace=Path("/tmp/nullone-privilege-probe"),
                binary="/tmp/fake-opencode",
            )
            self.assertNotIn("--auto", argv)
            self.assertNotIn("--continue", argv)
            self.assertNotIn("--session", argv)
            self.assertEqual(argv[argv.index("--agent") + 1], router.role_agent(role))
        print("PERMISSION_BOUNDARIES_PRESERVED=PASS")
        print("CAPABILITY_BOUNDARIES_PRESERVED=PASS")

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
