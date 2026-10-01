#!/usr/bin/env python3
"""Morning Editorial Claude/Sonnet route contract (issue #155).

Benchmark-validated: transport=claude, `--model sonnet` resolving to
claude-sonnet-5, timeout 600, agent nullone-editorial, no fallback.

Also covers the Story Writer Claude/Haiku route (#172-era migration
off OpenCode, FreeTierError root-cause debugging closed as an
engineering direction): transport=claude, model=haiku, timeout 300
unchanged, agent nullone-story-writer, no fallback. Draft Factory,
Breaking Radar and Draft Factory remain on the reviewed OpenCode route.

NO model invocation. NO network. Checked-in config + router only.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
CONFIG_PATH = ROOT / "workspace/social/ops/provider-routing.json"
sys.path.insert(0, str(SCRIPTS))

import nullone_provider_router as router  # noqa: E402


def checked_in_mapping():
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert raw.get("schema") == router.CONFIG_SCHEMA
    return raw["roles"]


class MorningClaudeRouteTests(unittest.TestCase):
    """A-E: Morning resolves to the exact validated Claude/Sonnet route."""

    def test_morning_transport_is_claude(self):
        profile = router.resolve_provider_profile(
            router.ROLE_MORNING_EDITORIAL, env={}
        )
        self.assertEqual(profile.transport, "claude")

    def test_morning_model_is_sonnet_selector(self):
        profile = router.resolve_provider_profile(
            router.ROLE_MORNING_EDITORIAL, env={}
        )
        # Bare transport-local selector (benchmark-executed as
        # `claude -p --model sonnet` -> claude-sonnet-5). A slash-less
        # `claude-sonnet-5` would fail the router closed; only `sonnet`
        # is the valid representation here.
        self.assertEqual(profile.model, "sonnet")
        self.assertEqual(profile.model, router.CLAUDE_DEFAULT_MODEL)

    def test_morning_timeout_is_600(self):
        profile = router.resolve_provider_profile(
            router.ROLE_MORNING_EDITORIAL, env={}
        )
        self.assertEqual(profile.timeout_seconds, 600)
        self.assertEqual(
            profile.timeout_seconds,
            router.ROLE_TIMEOUTS[router.ROLE_MORNING_EDITORIAL],
        )

    def test_morning_agent_is_nullone_editorial(self):
        self.assertEqual(
            router.role_agent(router.ROLE_MORNING_EDITORIAL),
            "nullone-editorial",
        )

    def test_morning_has_no_fallback(self):
        profile = router.resolve_provider_profile(
            router.ROLE_MORNING_EDITORIAL, env={}
        )
        self.assertEqual(profile.fallback_policy, "none")
        self.assertEqual(profile.fallback_policy, router.FALLBACK_NONE)

    def test_morning_claude_argv_uses_sonnet_without_shell(self):
        from nullone_claude_editorial_provider import build_claude_command

        argv = build_claude_command(prompt="probe", model="sonnet")
        self.assertEqual(
            argv,
            [
                "claude", "-p", "probe", "--model", "sonnet",
                "--permission-mode", "dontAsk",
                "--allowedTools", "Read,Write,WebSearch,WebFetch",
            ],
        )


class StoryClaudeRouteTests(unittest.TestCase):
    """Story resolves to the exact reviewed Claude/Haiku route.

    #172-era migration off OpenCode: config-only change, the
    HaikuStoryWriter dispatch path and its wiring into
    make_story_writer() already existed and were already reviewed.
    """

    def test_story_transport_is_claude(self):
        profile = router.resolve_provider_profile(
            router.ROLE_STORY_WRITER, env={}
        )
        self.assertEqual(profile.transport, "claude")

    def test_story_model_is_haiku_selector(self):
        profile = router.resolve_provider_profile(
            router.ROLE_STORY_WRITER, env={}
        )
        # Reviewed Haiku selector for this role -- never Sonnet/Opus.
        self.assertEqual(profile.model, "haiku")
        self.assertEqual(profile.model, router.HAIKU_DEFAULT_MODEL)

    def test_story_timeout_is_300_unchanged(self):
        profile = router.resolve_provider_profile(
            router.ROLE_STORY_WRITER, env={}
        )
        self.assertEqual(profile.timeout_seconds, 300)
        self.assertEqual(
            profile.timeout_seconds,
            router.ROLE_TIMEOUTS[router.ROLE_STORY_WRITER],
        )

    def test_story_agent_is_nullone_story_writer(self):
        self.assertEqual(
            router.role_agent(router.ROLE_STORY_WRITER),
            "nullone-story-writer",
        )

    def test_story_has_no_fallback(self):
        profile = router.resolve_provider_profile(
            router.ROLE_STORY_WRITER, env={}
        )
        self.assertEqual(profile.fallback_policy, "none")
        self.assertEqual(profile.fallback_policy, router.FALLBACK_NONE)


class OtherRolesUnchangedTests(unittest.TestCase):
    """F-I: Draft Factory moved to its reviewed Claude/Sonnet route in
    #194 (Radar moved to Claude/Haiku in #190, covered by its own
    route suite)."""

    def test_other_roles_unchanged(self):
        mapping = checked_in_mapping()
        for role in (
            router.ROLE_DRAFT_FACTORY,
        ):
            profile = router.resolve_provider_profile(
                role, config=mapping, env={}
            )
            self.assertEqual(profile.transport, "claude", role)
            self.assertEqual(profile.model, "sonnet", role)
            self.assertEqual(profile.fallback_policy, "none", role)
            self.assertEqual(
                profile.timeout_seconds, router.ROLE_TIMEOUTS[role], role
            )


class RoutingConfigHygieneTests(unittest.TestCase):
    """J-K: schema valid, no secret material in config."""

    def test_schema_valid_and_complete(self):
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(raw.get("schema"), "nullone.provider-routing.v1")
        self.assertEqual(set(raw["roles"]), set(router.LOGICAL_ROLES))
        router.load_routing_config(CONFIG_PATH)

    def test_no_secret_material_in_config(self):
        text = CONFIG_PATH.read_text(encoding="utf-8").lower()
        for needle in ("token", "secret", "key", "password", "credential", "auth"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
