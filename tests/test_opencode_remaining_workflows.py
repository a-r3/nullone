#!/usr/bin/env python3
"""Offline tests for the remaining OpenCode role migrations (issue #112).

Covers the shared role transport plus Draft Factory, Breaking Radar,
and Weekly Strategy wrappers/agents -- with mocks/fake subprocesses
only (no network, no model calls, no production state):

COMMON (per role):
- Muse Spark selected (default + shared override knob)
- OpenCode command deterministic and exact (agent/model/dir/format)
- correct cwd/workspace, per-role timeout
- no --auto / session-continuation flags
- provider/model metadata correct and secret-free
- malformed config fails closed; transport failure maps to BLOCKED
- secrets not embedded; only NULLONE_OPENCODE_MODEL is read
- no active Anthropic model dependency in new config

DRAFT FACTORY:
- agent allows exactly the reviewed script/telegram commands
- sibling mutating message subcommands denied
- writes scoped to drafts/production, publisher reports, queue/ledger
- no final-publication path, no Zernio keys/REST, no secret egress

RADAR:
- web/search available; writes scoped to breaking reports + staging
- spool/handoff direct writes denied; no draft/Telegram/publication path
- scan-helper-only shell

WEEKLY:
- no shell at all; writes scoped to strategy report + MEMORY.md
- strategy-hypothesis files outside the two paths denied
- output contract path preserved

Boundary resolution replicates the documented 1.18.30 matcher
(last matching rule wins; `*` matches any characters).
"""
from __future__ import annotations

import fnmatch
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
AGENTS = ROOT / "workspace/.opencode/agents"
PROMPTS = ROOT / "workspace/social/ops/prompts"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_opencode_role as role_transport  # noqa: E402


def _load_wrapper(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


draft_wrapper = _load_wrapper("draft_factory_run_test", "nullone-draft-factory-run.py")
radar_wrapper = _load_wrapper("radar_run_test", "nullone-breaking-radar-run.py")
weekly_wrapper = _load_wrapper("weekly_run_test", "nullone-weekly-strategy-run.py")

WRAPPERS = (
    ("draft-factory", "nullone-draft-factory", draft_wrapper, 900),
    ("breaking-radar", "nullone-breaking-radar", radar_wrapper, 600),
    ("weekly-strategy", "nullone-weekly-strategy", weekly_wrapper, 600),
)

MUSE_SPARK = "opencode/muse-spark-1.3-contributor-free"
WS = "/home/oem/.openclaw/workspace"


def _parse_agent_permission_block(agent_file: Path) -> dict:
    text = agent_file.read_text(encoding="utf-8")
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


def _resolve_permission(rules, value: str) -> str | None:
    if isinstance(rules, str):
        return rules
    result = None
    for pattern, action in rules:
        if fnmatch.fnmatch(value, pattern):
            result = action
    return result


def code_only(source: str) -> str:
    source = re.sub(r'""".*?"""|\'\'\'.*?\'\'\'', "", source, flags=re.DOTALL)
    return re.sub(r"#.*", "", source)


class SharedTransportTests(unittest.TestCase):
    def test_muse_spark_default_and_override(self):
        self.assertEqual(role_transport.DEFAULT_OPENCODE_MODEL, MUSE_SPARK)
        self.assertEqual(role_transport.resolve_role_model("  "), MUSE_SPARK)
        with mock.patch.dict(os.environ, {role_transport.OPENCODE_MODEL_ENV_VAR: "x/y-model"}):
            self.assertEqual(role_transport.resolve_role_model(), "x/y-model")

    def test_only_model_env_var_is_read(self):
        names = set(re.findall(r"NULLONE_[A-Z_]+", code_only((SCRIPTS / "nullone_opencode_role.py").read_text(encoding="utf-8"))))
        self.assertEqual(names, {"NULLONE_OPENCODE_MODEL"})

    def test_malformed_command_config_fails_closed(self):
        for bad in ({"prompt": "  ", "agent": "a"}, {"prompt": "p", "agent": " "}):
            with self.assertRaises(BridgeError):
                role_transport.build_opencode_command(
                    prompt=bad["prompt"], workspace=Path("/tmp/x"), agent=bad["agent"]
                )

    def test_timeout_and_reachability_mapping(self):
        def timeout_effect(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        with mock.patch.object(role_transport.subprocess, "run", side_effect=timeout_effect):
            with self.assertRaises(role_transport.RoleExecutionTimeoutError):
                role_transport.run_opencode_cycle(["opencode"], cwd=Path("/tmp"), timeout=1, role="probe")

        def unreachable_effect(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ETIMEDOUT now")

        with mock.patch.object(role_transport.subprocess, "run", side_effect=unreachable_effect):
            with self.assertRaises(role_transport.RoleUnreachableError):
                role_transport.run_opencode_cycle(["opencode"], cwd=Path("/tmp"), timeout=1, role="probe")

        with mock.patch.object(
            role_transport.subprocess, "run", side_effect=FileNotFoundError("x")
        ):
            with self.assertRaises(BridgeError):
                role_transport.run_opencode_cycle(["opencode"], cwd=Path("/tmp"), timeout=1, role="probe")

    def test_no_secret_or_anthropic_tokens_in_transport(self):
        source = (SCRIPTS / "nullone_opencode_role.py").read_text(encoding="utf-8")
        for token in ("TOKEN", "SECRET", "API_KEY", "BEARER", "PASSWORD", "anthropic", "sonnet", "haiku"):
            self.assertNotIn(token, source)


class RoleWrapperTests(unittest.TestCase):
    def test_command_shape_per_role(self):
        for role, agent, module, timeout in WRAPPERS:
            with self.subTest(role=role):
                argv = module.build_command(workspace=Path("/tmp/nullone-role-check"))
                self.assertEqual(argv[0:2], ["opencode", "run"])
                self.assertEqual(argv[argv.index("--agent") + 1], agent)
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(role_transport.OPENCODE_MODEL_ENV_VAR, None)
                    argv_default = module.build_command(workspace=Path("/tmp/nullone-role-check"))
                self.assertEqual(argv_default[argv_default.index("--model") + 1], MUSE_SPARK)
                self.assertEqual(argv[argv.index("--dir") + 1], "/tmp/nullone-role-check")
                self.assertEqual(argv[argv.index("--format") + 1], "json")
                for forbidden in ("--auto", "--continue", "--session", "--fork", "--share"):
                    self.assertNotIn(forbidden, argv)

    def test_per_role_timeout_budgets(self):
        self.assertEqual(draft_wrapper.DRAFT_FACTORY_TIMEOUT_SECONDS, 900)
        self.assertEqual(radar_wrapper.RADAR_TIMEOUT_SECONDS, 600)
        self.assertEqual(weekly_wrapper.WEEKLY_TIMEOUT_SECONDS, 600)

    def test_execute_maps_transport_failure_to_blocked(self):
        for role, agent, module, timeout in WRAPPERS:
            with self.subTest(role=role):
                def effect(cmd, **kwargs):
                    return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="boom")

                with mock.patch.object(role_transport.subprocess, "run", side_effect=effect):
                    self.assertEqual(module.execute(), 1)

    def test_execute_success_returns_zero(self):
        def effect(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        with mock.patch.object(role_transport.subprocess, "run", side_effect=effect):
            self.assertEqual(radar_wrapper.execute(), 0)

    def test_metadata_describe_is_secret_free(self):
        text = role_transport.describe_cycle(role="breaking-radar", agent="nullone-breaking-radar", model=MUSE_SPARK)
        self.assertIn("provider", text)
        self.assertIn(MUSE_SPARK, text)
        for token in ("TOKEN", "SECRET", "KEY", "BEARER"):
            self.assertNotIn(token, text)

    def test_wrappers_read_no_secret_env_and_no_anthropic(self):
        for filename in (
            "nullone-draft-factory-run.py",
            "nullone-breaking-radar-run.py",
            "nullone-weekly-strategy-run.py",
        ):
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            self.assertNotIn("os.environ", source)
            self.assertNotIn("getenv", source)
            for token in ("anthropic", "sonnet", "haiku", "TOKEN", "SECRET", "API_KEY"):
                self.assertNotIn(token, source)


class DraftFactoryBoundaryTests(unittest.TestCase):
    RULES = _parse_agent_permission_block(AGENTS / "nullone-draft-factory.md")

    def test_bash_allows_exactly_reviewed_commands(self):
        cases = {
            "python3 social/tools/render_texbrif_v2.py --output x.png": "allow",
            "python3 social/tools/render_carousel_v2.py --output x.png": "allow",
            "python3 social/tools/render_story_v2.py --output x.png": "allow",
            "python3 social/ops/scripts/nullone-manifest.py build --candidate-id c": "allow",
            "python3 social/ops/scripts/nullone-draft-bridge.py execute m.json": "allow",
            "openclaw message send --account texbrif -m hi": "allow",
            "openclaw message ban @x": "deny",
            "openclaw message delete --message-id 1": "deny",
            "openclaw message broadcast -m spam": "deny",
            "openclaw message read": "deny",
            "git push": "deny",
            "python3 social/ops/scripts/nullone-publisher-run.py x": "deny",
            "curl https://x": "deny",
            "ls": "deny",
        }
        for command, expected in cases.items():
            self.assertEqual(
                _resolve_permission(self.RULES["bash"], command), expected, msg=command
            )

    def test_write_scope_exact(self):
        allowed = (
            f"{WS}/social/drafts/production/2026-09-13-x-caption.txt",
            f"{WS}/social/drafts/production/2026-09-13-x.png",
            f"{WS}/social/publisher/2026-09-13-x-draft.md",
            f"{WS}/social/state/candidate-queue.md",
            f"{WS}/social/state/topic-ledger.jsonl",
        )
        denied = (
            f"{WS}/social/ops/scripts/nullone-manifest.py",
            f"{WS}/social/ops/prompts/draft-factory.md",
            f"{WS}/AGENTS.md",
            f"{WS}/social/state/publish-ledger.jsonl",
            f"{WS}/.opencode/agents/nullone-draft-factory.md",
            f"{WS}/social/drafts/review/x.md",
        )
        for path in allowed:
            self.assertEqual(_resolve_permission(self.RULES["edit"], path), "allow", msg=path)
        for path in denied:
            self.assertEqual(_resolve_permission(self.RULES["edit"], path), "deny", msg=path)

    def test_secret_reads_denied(self):
        for path in (f"{WS}/.env", f"{WS}/.env.local", f"{WS}/social/.env", f"{WS}/k.pem"):
            self.assertEqual(_resolve_permission(self.RULES["read"], path), "deny", msg=path)

    def test_other_denials_preserved(self):
        for key in ("task", "skill", "lsp", "question", "todowrite", "external_directory"):
            self.assertEqual(self.RULES.get(key), "deny")
        for key in ("webfetch", "websearch", "glob", "grep", "list"):
            self.assertEqual(self.RULES.get(key), "allow")

    def test_no_final_publication_or_secret_egress_tokens(self):
        source = (SCRIPTS / "nullone-draft-factory-run.py").read_text(encoding="utf-8")
        agent = (AGENTS / "nullone-draft-factory.md").read_text(encoding="utf-8")
        for token in (
            "posts_publish_now",
            "posts_delete",
            "PUBLISH_AUTHORIZED",
            "final_publish",
            "nullone-publish-bridge",
            "nullone-publisher-run",
            "ZERNIO_API_KEY",
            "mcp__zernio",
            "call_tool(",
            "publishNow",
            "scheduledFor",
        ):
            self.assertNotIn(token, source, msg=f"wrapper must not contain {token!r}")
            self.assertNotIn(token, agent, msg=f"agent must not contain {token!r}")
        for token in ("requests.", "urllib", "http.client"):
            self.assertNotIn(token, code_only(source))


class RadarBoundaryTests(unittest.TestCase):
    RULES = _parse_agent_permission_block(AGENTS / "nullone-breaking-radar.md")

    def test_shell_is_scan_helper_only(self):
        allowed = {
            "python3 social/ops/scripts/nullone-breaking-scan.py current-scan": "allow",
            "python3 social/ops/scripts/nullone-breaking-scan.py commit --assessment x.json": "allow",
            "python3 social/ops/scripts/nullone-breaking-scan.py record-empty": "allow",
        }
        denied = (
            "python3 social/ops/scripts/nullone-breaking-consume.py sweep",
            "python3 social/tools/render_texbrif_v2.py --output x",
            "openclaw message send -m hi",
            "git status",
            "ls",
        )
        for command, expected in allowed.items():
            self.assertEqual(_resolve_permission(self.RULES["bash"], command), expected, msg=command)
        for command in denied:
            self.assertEqual(_resolve_permission(self.RULES["bash"], command), "deny", msg=command)

    def test_write_scope_exact(self):
        allowed = (
            f"{WS}/social/research/daily/2026-09-13-breaking-1130.md",
            f"{WS}/social/ops/breaking-staging/abc123.json",
        )
        denied = (
            f"{WS}/social/ops/breaking-handoffs/scan/x.json",
            f"{WS}/social/state/publish-ledger.jsonl",
            f"{WS}/social/state/candidate-queue.md",
            f"{WS}/social/drafts/production/x.md",
        )
        for path in allowed:
            self.assertEqual(_resolve_permission(self.RULES["edit"], path), "allow", msg=path)
        for path in denied:
            self.assertEqual(_resolve_permission(self.RULES["edit"], path), "deny", msg=path)

    def test_web_available_and_secrets_denied(self):
        self.assertEqual(self.RULES.get("webfetch"), "allow")
        self.assertEqual(self.RULES.get("websearch"), "allow")
        for path in (f"{WS}/.env", f"{WS}/social/.env"):
            self.assertEqual(_resolve_permission(self.RULES["read"], path), "deny", msg=path)

    def test_no_draft_notify_publication_path(self):
        agent = (AGENTS / "nullone-breaking-radar.md").read_text(encoding="utf-8")
        for token in ("posts_create", "posts_publish_now", "PUBLISH_AUTHORIZED", "--auto"):
            self.assertNotIn(token, agent)


class WeeklyBoundaryTests(unittest.TestCase):
    RULES = _parse_agent_permission_block(AGENTS / "nullone-weekly-strategy.md")

    def test_no_shell_at_all(self):
        self.assertEqual(self.RULES.get("bash"), "deny")

    def test_write_scope_exact(self):
        allowed = (
            f"{WS}/social/analytics/reports/2026-37-weekly-strategy.md",
            f"{WS}/MEMORY.md",
        )
        denied = (
            f"{WS}/social/CONTENT_STRATEGY.md",
            f"{WS}/social/ACCOUNT.md",
            f"{WS}/social/state/publish-ledger.jsonl",
            f"{WS}/social/analytics/reports/raw.json",
        )
        for path in allowed:
            self.assertEqual(_resolve_permission(self.RULES["edit"], path), "allow", msg=path)
        for path in denied:
            self.assertEqual(_resolve_permission(self.RULES["edit"], path), "deny", msg=path)

    def test_no_publication_capability(self):
        agent = (AGENTS / "nullone-weekly-strategy.md").read_text(encoding="utf-8")
        lowered = agent.lower()
        self.assertIn("do not publish anything", lowered)
        for token in ("posts_create", "posts_publish_now", "--auto", "openclaw message send"):
            self.assertNotIn(token, agent)

    def test_output_contract_path_preserved(self):
        prompt = (PROMPTS / "weekly-strategy.md").read_text(encoding="utf-8")
        self.assertIn("social/analytics/reports/YYYY-WW-weekly-strategy.md", prompt)


class NoActiveAnthropicDependencyTests(unittest.TestCase):
    def test_prompts_require_no_anthropic_model(self):
        for prompt in ("draft-factory.md", "breaking-radar.md", "weekly-strategy.md"):
            text = (PROMPTS / prompt).read_text(encoding="utf-8").lower()
            for token in ("anthropic/", "claude-sonnet", "claude-haiku", "claude-opus"):
                self.assertNotIn(token, text, msg=f"{prompt} must not require {token!r}")

    def test_new_role_config_has_no_anthropic_model(self):
        paths = [
            SCRIPTS / "nullone_opencode_role.py",
            SCRIPTS / "nullone-draft-factory-run.py",
            SCRIPTS / "nullone-breaking-radar-run.py",
            SCRIPTS / "nullone-weekly-strategy-run.py",
            AGENTS / "nullone-draft-factory.md",
            AGENTS / "nullone-breaking-radar.md",
            AGENTS / "nullone-weekly-strategy.md",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8").lower()
            for token in ("anthropic/", "claude-sonnet", "claude-haiku", "claude-opus"):
                self.assertNotIn(token, text, msg=f"{path.name} must not require {token!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
