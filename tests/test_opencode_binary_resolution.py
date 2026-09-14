#!/usr/bin/env python3
"""Offline tests for deterministic OpenCode binary resolution.

Proves the scheduler-PATH failure (`FileNotFoundError: 'opencode'`)
cannot recur: every OpenCode transport resolves an absolute,
validated executable instead of relying on PATH.

A. HOME install present, PATH empty of OpenCode -> HOME binary wins
B. explicit override valid -> override wins
C. explicit override relative -> rejected
D. explicit override missing/non-executable -> fail closed
E. HOME missing but shutil.which hits -> compatibility fallback
F. nothing resolves -> typed OPENCODE_BINARY_NOT_FOUND failure
G. shared role transport production path uses resolved absolute binary
H. Morning production path uses resolved absolute binary
I. Story production path uses resolved absolute binary
J. command builders preserve agent/model/format/dir, no auto/continue/session
K. model/provider selection unchanged
L. no final publication capability added

Plus the exact production regression: scheduler PATH without
~/.opencode/bin + fake HOME install -> runners receive the absolute
HOME-derived binary for the shared role transport, Morning, and Story.

No network/model calls. No real filesystem dependence outside temp dirs.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_opencode_binary as binary_mod  # noqa: E402
from nullone_opencode_binary import (  # noqa: E402
    OPENCODE_BINARY_ENV_VAR,
    OpenCodeBinaryResolutionError,
    resolve_opencode_binary,
)
import nullone_opencode_role as role_mod  # noqa: E402


def _make_home_with_binary(parent: Path, executable: bool = True) -> Path:
    home = parent / "fakehome"
    binary = home / ".opencode/bin/opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755 if executable else 0o644)
    return home


def _clear_env():
    return mock.patch.dict(os.environ, {}, clear=False)


class ResolverPolicyTests(unittest.TestCase):
    def test_home_install_wins_without_path(self):
        with tempfile.TemporaryDirectory() as td:
            home = _make_home_with_binary(Path(td))
            with _clear_env():
                os.environ.pop(OPENCODE_BINARY_ENV_VAR, None)
                with mock.patch.object(binary_mod.shutil, "which", return_value=None):
                    self.assertEqual(
                        resolve_opencode_binary(home=home), str(home / ".opencode/bin/opencode")
                    )

    def test_explicit_valid_override_wins(self):
        with tempfile.TemporaryDirectory() as td:
            home = _make_home_with_binary(Path(td))
            custom = Path(td) / "custom-opencode"
            custom.write_text("#!/bin/sh\n", encoding="utf-8")
            custom.chmod(0o755)
            with mock.patch.dict(os.environ, {OPENCODE_BINARY_ENV_VAR: str(custom)}):
                self.assertEqual(resolve_opencode_binary(home=home), str(custom))

    def test_relative_override_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            home = _make_home_with_binary(Path(td))
            with mock.patch.dict(os.environ, {OPENCODE_BINARY_ENV_VAR: "relative/opencode"}):
                with self.assertRaises(OpenCodeBinaryResolutionError) as ctx:
                    resolve_opencode_binary(home=home)
                self.assertIn("OPENCODE_BINARY_NOT_FOUND", str(ctx.exception))

    def test_missing_or_non_executable_override_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            home = _make_home_with_binary(Path(td))
            for bad in ("/tmp/nullone-no-such-binary-xyz", str(Path(td) / "notexec")):
                Path(td, "notexec").write_text("x", encoding="utf-8")
                with mock.patch.dict(os.environ, {OPENCODE_BINARY_ENV_VAR: bad}):
                    with self.assertRaises(OpenCodeBinaryResolutionError):
                        resolve_opencode_binary(home=home)
            with mock.patch.dict(os.environ, {OPENCODE_BINARY_ENV_VAR: str(Path(td) / "adir")}):
                Path(td, "adir").mkdir(exist_ok=True)
                with self.assertRaises(OpenCodeBinaryResolutionError):
                    resolve_opencode_binary(home=home)

    def test_which_fallback_when_home_missing(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "emptyhome"
            home.mkdir()
            real = Path(td) / "bin" / "opencode"
            real.parent.mkdir()
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            with _clear_env():
                os.environ.pop(OPENCODE_BINARY_ENV_VAR, None)
                with mock.patch.object(binary_mod.shutil, "which", return_value=str(real)):
                    self.assertEqual(resolve_opencode_binary(home=home), str(real))

    def test_relative_which_result_resolves_to_validated_absolute(self):
        # Faithful simulation (no which mock): a relative PATH entry
        # makes real shutil.which return a relative path; the resolver
        # must still hand back a validated ABSOLUTE executable.
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "work"
            relbin = work / "relbin"
            relbin.mkdir(parents=True)
            binary = relbin / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            home = Path(td) / "emptyhome"
            home.mkdir()
            previous_cwd = os.getcwd()
            os.chdir(work)
            try:
                with _clear_env():
                    os.environ.pop(OPENCODE_BINARY_ENV_VAR, None)
                    with mock.patch.dict(os.environ, {"PATH": "relbin"}):
                        resolved = resolve_opencode_binary(home=home)
            finally:
                os.chdir(previous_cwd)
            self.assertTrue(os.path.isabs(resolved))
            self.assertEqual(resolved, str(work / "relbin/opencode"))

    def test_relative_which_result_non_executable_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "work"
            relbin = work / "relbin"
            relbin.mkdir(parents=True)
            binary = relbin / "opencode"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o644)
            home = Path(td) / "emptyhome"
            home.mkdir()
            previous_cwd = os.getcwd()
            os.chdir(work)
            try:
                with _clear_env():
                    os.environ.pop(OPENCODE_BINARY_ENV_VAR, None)
                    with mock.patch.dict(os.environ, {"PATH": "relbin"}):
                        with self.assertRaises(OpenCodeBinaryResolutionError) as ctx:
                            resolve_opencode_binary(home=home)
            finally:
                os.chdir(previous_cwd)
            self.assertIn("OPENCODE_BINARY_NOT_FOUND", str(ctx.exception))

    def test_nothing_resolves_fails_closed_with_typed_error(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "emptyhome"
            home.mkdir()
            with _clear_env():
                os.environ.pop(OPENCODE_BINARY_ENV_VAR, None)
                with mock.patch.object(binary_mod.shutil, "which", return_value=None):
                    with self.assertRaises(OpenCodeBinaryResolutionError) as ctx:
                        resolve_opencode_binary(home=home)
                    self.assertIn("OPENCODE_BINARY_NOT_FOUND", str(ctx.exception))
                    self.assertIsInstance(ctx.exception, BridgeError)

    def test_error_carries_no_secret_material(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "emptyhome"
            home.mkdir()
            marker = "should-never-appear-987"
            with mock.patch.dict(os.environ, {OPENCODE_BINARY_ENV_VAR: marker}):
                try:
                    resolve_opencode_binary(home=home)
                except OpenCodeBinaryResolutionError as exc:
                    self.assertNotIn(marker, str(exc))
                else:
                    raise AssertionError("relative value did not fail closed")


class SchedulerRegressionTests(unittest.TestCase):
    """Reproduce the production scheduler condition: PATH without
    ~/.opencode/bin, HOME carrying the install. Every transport must
    hand subprocess an absolute HOME-derived binary."""

    def test_role_transport_uses_absolute_binary(self):
        with tempfile.TemporaryDirectory() as td:
            home = _make_home_with_binary(Path(td))
            expected = str(home / ".opencode/bin/opencode")
            with _clear_env():
                os.environ.pop(OPENCODE_BINARY_ENV_VAR, None)
                with mock.patch.object(binary_mod.shutil, "which", return_value=None):
                    binary = resolve_opencode_binary(home=home)
            self.assertEqual(binary, expected)
            argv = role_mod.build_opencode_command(
                prompt="probe",
                workspace=Path("/tmp/x"),
                agent="nullone-draft-factory",
                model="opencode/muse-spark-1.3-contributor-free",
                binary=binary,
            )
            self.assertEqual(argv[0], expected)
            self.assertNotEqual(argv[0], "opencode")

    def test_role_execute_resolves_before_subprocess(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "draft_factory_run_binary_test", SCRIPTS / "nullone-draft-factory-run.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            home = _make_home_with_binary(Path(td))
            expected = str(home / ".opencode/bin/opencode")
            captured: dict = {}

            def fake_resolve():
                return expected

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                import subprocess as _sp

                return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

            with mock.patch.object(module, "resolve_opencode_binary", side_effect=fake_resolve):
                with mock.patch.object(role_mod, "run_tree_command", side_effect=fake_run):
                    self.assertEqual(module.execute(), 0)
            self.assertEqual(captured["cmd"][0], expected)

    def test_morning_production_invocation_uses_absolute_binary(self):
        import nullone_opencode_editorial_provider as editorial

        with tempfile.TemporaryDirectory() as td:
            home = _make_home_with_binary(Path(td))
            expected = str(home / ".opencode/bin/opencode")
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                import subprocess as _sp

                return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

            with mock.patch.object(
                editorial, "resolve_opencode_binary", return_value=expected
            ):
                with mock.patch.object(editorial, "run_tree_command", side_effect=fake_run):
                    editorial.default_invoke_provider(
                        prompt="probe", workspace=Path(td), timeout=30
                    )
            self.assertEqual(captured["cmd"][0], expected)

    def test_story_production_invocation_uses_absolute_binary(self):
        import nullone_opencode_story_provider as story

        with tempfile.TemporaryDirectory() as td:
            home = _make_home_with_binary(Path(td))
            expected = str(home / ".opencode/bin/opencode")
            captured: dict = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                import subprocess as _sp

                return _sp.CompletedProcess(cmd, 0, stdout='{"layout": "x"}', stderr="")

            writer = story.OpenCodeStoryWriter(workspace=Path(td), timeout=30)
            with mock.patch.object(story, "resolve_opencode_binary", return_value=expected):
                with mock.patch.object(story.subprocess, "run", side_effect=fake_run):
                    self.assertEqual(writer({"topic": "probe"}), {"layout": "x"})
            self.assertEqual(captured["cmd"][0], expected)


class CommandShapePreservedTests(unittest.TestCase):
    def test_builders_preserve_flags(self):
        import nullone_opencode_editorial_provider as editorial
        import nullone_opencode_story_provider as story

        for argv in (
            role_mod.build_opencode_command(
                prompt="p", workspace="/tmp/x", agent="a", model="m",
            ),
            editorial.build_opencode_command(prompt="p", workspace="/tmp/x", model="m"),
            story.build_opencode_command(prompt="p", workspace="/tmp/x", model="m"),
        ):
            self.assertEqual(argv[0], "opencode")
            self.assertIn("--agent", argv)
            self.assertIn("--model", argv)
            self.assertIn("--format", argv)
            self.assertEqual(argv[argv.index("--format") + 1], "json")
            self.assertIn("--dir", argv)
            for forbidden in ("--auto", "--continue", "--session"):
                self.assertNotIn(forbidden, argv)

    def test_model_selection_unchanged(self):
        import nullone_opencode_editorial_provider as editorial
        import nullone_opencode_story_provider as story

        self.assertEqual(
            editorial.DEFAULT_OPENCODE_MODEL, "opencode/muse-spark-1.3-contributor-free"
        )
        self.assertEqual(
            story.DEFAULT_STORY_MODEL, "opencode/muse-spark-1.3-contributor-free"
        )
        self.assertEqual(
            role_mod.DEFAULT_OPENCODE_MODEL, "opencode/muse-spark-1.3-contributor-free"
        )

    def test_no_publication_capability_added(self):
        for filename in (
            "nullone_opencode_binary.py",
            "nullone_opencode_role.py",
            "nullone_opencode_editorial_provider.py",
            "nullone_opencode_story_provider.py",
        ):
            source = (SCRIPTS / filename).read_text(encoding="utf-8").lower()
            for token in ("publish", "zernio", "telegram"):
                self.assertNotIn(token, source, msg=f"{filename} must not reference {token!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
