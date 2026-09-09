#!/usr/bin/env python3
"""Offline tests for the reviewed secret boundary (#61).

Proves the `SecretValue` wrapper can never render its value through any
accidental path, that the `EnvironmentSecretProvider` reads and classifies
the production secret exactly as designed (typed missing/blank/unavailable,
never leaking), that the legacy key alias is absent, and that the
deployment-edge file-control report never reads the value. No network
access and no real credential is ever used.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_secret_provider import (  # noqa: E402
    ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN,
    SECRET_ID_ZERNIO_ANALYTICS_BEARER,
    SECRET_REDACTED_RENDER,
    EnvironmentSecretProvider,
    SecretFileControlReport,
    SecretNotConfiguredError,
    SecretPresence,
    SecretProviderError,
    SecretUnavailableError,
    SecretValue,
    secret_file_control_report,
)

MARKER = "FAKE_ZERNIO_SECRET_DO_NOT_LOG_123"


class SecretValueRenderSafetyTests(unittest.TestCase):
    def test_repr_never_contains_value(self):
        secret = SecretValue(MARKER)
        self.assertNotIn(MARKER, repr(secret))
        self.assertEqual(repr(secret), f"SecretValue({SECRET_REDACTED_RENDER!r})")

    def test_str_and_format_never_contain_value(self):
        secret = SecretValue(MARKER)
        for rendered in (str(secret), f"{secret}", f"{secret!s}", f"{secret:s}"):
            self.assertNotIn(MARKER, rendered)
            self.assertEqual(rendered, SECRET_REDACTED_RENDER)

    def test_container_serendipity_is_redacted(self):
        self.assertNotIn(MARKER, repr([SecretValue(MARKER)]))
        self.assertNotIn(MARKER, repr({"tok": SecretValue(MARKER)}))

    def test_reveal_is_the_only_escape_hatch(self):
        self.assertEqual(SecretValue(MARKER).reveal(), MARKER)

    def test_non_string_value_is_rejected(self):
        with self.assertRaises(TypeError):
            SecretValue(123)  # type: ignore[arg-type]

    def test_equality_compares_wrapped_values_only(self):
        self.assertEqual(SecretValue(MARKER), SecretValue(MARKER))
        self.assertNotEqual(SecretValue(MARKER), SecretValue("other"))
        self.assertNotEqual(SecretValue(MARKER), MARKER)

    def test_secret_value_is_unhashable(self):
        with self.assertRaises(TypeError):
            hash(SecretValue(MARKER))
        with self.assertRaises(TypeError):
            {SecretValue(MARKER)}


class EnvironmentSecretProviderTests(unittest.TestCase):
    def _provider(self, **overrides):
        env = {ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN: MARKER}
        env.update(overrides)
        return EnvironmentSecretProvider(environ=env)

    def test_present_secret_is_readable(self):
        provider = self._provider()
        self.assertIs(
            provider.probe(SECRET_ID_ZERNIO_ANALYTICS_BEARER),
            SecretPresence.PRESENT_READABLE,
        )
        self.assertEqual(
            provider.get_required(SECRET_ID_ZERNIO_ANALYTICS_BEARER),
            SecretValue(MARKER),
        )

    def test_missing_secret_is_typed_missing(self):
        provider = self._provider(**{ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN: None})
        self.assertIs(provider.probe(SECRET_ID_ZERNIO_ANALYTICS_BEARER), SecretPresence.MISSING)
        with self.assertRaises(SecretNotConfiguredError):
            provider.get_required(SECRET_ID_ZERNIO_ANALYTICS_BEARER)

    def test_blank_secret_is_typed_missing(self):
        for blank in ("", "   ", "\t\n"):
            provider = self._provider(**{ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN: blank})
            self.assertIs(
                provider.probe(SECRET_ID_ZERNIO_ANALYTICS_BEARER),
                SecretPresence.BLANK,
            )
            with self.assertRaises(SecretNotConfiguredError):
                provider.get_required(SECRET_ID_ZERNIO_ANALYTICS_BEARER)

    def test_unknown_secret_id_is_typed_missing(self):
        provider = self._provider()
        self.assertIs(provider.probe("unknown.id"), SecretPresence.UNAVAILABLE)
        with self.assertRaises(SecretNotConfiguredError):
            provider.get_required("unknown.id")

    def test_whitespace_values_never_leak_from_errors(self):
        for blank in ("   ", "\t\n"):
            provider = self._provider(**{ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN: blank})
            try:
                provider.get_required(SECRET_ID_ZERNIO_ANALYTICS_BEARER)
            except SecretProviderError as exc:
                self.assertNotIn(blank, str(exc))

    def test_legacy_alias_mapping_not_present(self):
        provider = self._provider()
        self.assertNotIn("zernio.bearer.legacy", provider.ENV_VAR_BY_SECRET_ID)
        # Two inherited-env bindings: analytics + drafts (#81). The
        # publication identity (#90) is intentionally NOT env-bound.
        self.assertEqual(len(provider.ENV_VAR_BY_SECRET_ID), 2)


class UnavailableSourceTests(unittest.TestCase):
    def test_unreadable_source_is_typed_unavailable(self):
        class ExplodingSource:
            def get(self, key):
                raise OSError("runtime source is gone")

        class BrokenEnv(EnvironmentSecretProvider):
            def __init__(self):
                self._environ = ExplodingSource()  # type: ignore[assignment]

        provider = BrokenEnv()
        self.assertIs(
            provider.probe(SECRET_ID_ZERNIO_ANALYTICS_BEARER),
            SecretPresence.UNAVAILABLE,
        )
        with self.assertRaises(SecretUnavailableError):
            provider.get_required(SECRET_ID_ZERNIO_ANALYTICS_BEARER)


class SecretFileControlReportTests(unittest.TestCase):
    def _layout(self):
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "secrets"
        path.mkdir(mode=0o700)
        secret_file = path / "zernio-analytics.env"
        secret_file.touch(mode=0o600)
        uid = os.getuid()
        return td, secret_file, uid

    def test_secure_layout_passes(self):
        td, secret_file, uid = self._layout()
        self.addCleanup(td.cleanup)
        report = secret_file_control_report(secret_file, expected_uid=uid)
        self.assertIsInstance(report, SecretFileControlReport)
        self.assertTrue(report.secure)
        self.assertEqual(report.reason_codes, ())

    def test_missing_file_reports_failure(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        report = secret_file_control_report(Path(td.name) / "missing" / "nope.env")
        self.assertFalse(report.exists)
        self.assertFalse(report.secure)
        self.assertIn("FILE_MISSING", report.reason_codes)

    def test_secret_file_symlink_is_rejected(self):
        td, _secret_file, uid = self._layout()
        self.addCleanup(td.cleanup)
        link = Path(td.name) / "link.env"
        link.symlink_to("/etc/hostname")
        report = secret_file_control_report(link, expected_uid=uid)
        self.assertFalse(report.secure)
        self.assertIn("SECRET_FILE_SYMLINK", report.reason_codes)

    def test_directory_is_rejected_as_non_regular(self):
        td, secret_file, uid = self._layout()
        self.addCleanup(td.cleanup)
        report = secret_file_control_report(secret_file.parent, expected_uid=uid)
        self.assertFalse(report.secure)
        self.assertIn("SECRET_FILE_NOT_REGULAR_FILE", report.reason_codes)

    def test_overly_open_file_mode_is_rejected(self):
        td, secret_file, uid = self._layout()
        self.addCleanup(td.cleanup)
        os.chmod(secret_file, 0o644)
        report = secret_file_control_report(secret_file, expected_uid=uid)
        self.assertFalse(report.secure)
        self.assertIn("FILE_MODE_NOT_0600", report.reason_codes)

    def test_file_mode_0400_is_rejected(self):
        td, secret_file, uid = self._layout()
        self.addCleanup(td.cleanup)
        os.chmod(secret_file, 0o400)
        report = secret_file_control_report(secret_file, expected_uid=uid)
        self.assertFalse(report.secure)
        self.assertIn("FILE_MODE_NOT_0600", report.reason_codes)

    def test_file_mode_0700_is_rejected(self):
        td, secret_file, uid = self._layout()
        self.addCleanup(td.cleanup)
        os.chmod(secret_file, 0o700)
        report = secret_file_control_report(secret_file, expected_uid=uid)
        self.assertFalse(report.secure)
        self.assertIn("FILE_MODE_NOT_0600", report.reason_codes)

    def test_parent_dir_mode_0500_is_rejected(self):
        td, secret_file, uid = self._layout()
        self.addCleanup(td.cleanup)
        os.chmod(secret_file.parent, 0o500)
        report = secret_file_control_report(secret_file, expected_uid=uid)
        self.assertFalse(report.secure)
        self.assertIn("PARENT_DIR_MODE_NOT_0700", report.reason_codes)

    def test_parent_dir_mode_0755_is_rejected(self):
        td, secret_file, uid = self._layout()
        self.addCleanup(td.cleanup)
        os.chmod(secret_file.parent, 0o755)
        report = secret_file_control_report(secret_file, expected_uid=uid)
        self.assertFalse(report.secure)
        self.assertIn("PARENT_DIR_MODE_NOT_0700", report.reason_codes)

    def test_parent_dir_symlink_is_rejected(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        uid = os.getuid()
        real_dir = Path(td.name) / "real_secrets"
        real_dir.mkdir(mode=0o700)
        secret_file = real_dir / "zernio-analytics.env"
        secret_file.touch(mode=0o600)
        link_dir = Path(td.name) / "link_secrets"
        link_dir.symlink_to(real_dir)
        linked_secret = link_dir / "zernio-analytics.env"
        report = secret_file_control_report(linked_secret, expected_uid=uid)
        self.assertFalse(report.secure)
        self.assertIn("PARENT_DIR_SYMLINK", report.reason_codes)

    def test_parent_dir_not_directory_is_rejected(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        not_a_dir = Path(td.name) / "not_a_dir"
        not_a_dir.touch(mode=0o600)
        secret_file = not_a_dir / "zernio-analytics.env"
        report = secret_file_control_report(secret_file, expected_uid=os.getuid())
        self.assertFalse(report.secure)
        self.assertIn("PARENT_DIR_NOT_DIRECTORY", report.reason_codes)


class ReadbackCliTests(unittest.TestCase):
    def test_readback_never_renders_value_and_reports_status(self):
        import contextlib
        import io
        from unittest import mock

        import nullone_secret_provider as sp

        with mock.patch.dict(os.environ, {}, clear=True):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = sp._cmd_readback(SECRET_ID_ZERNIO_ANALYTICS_BEARER)
            self.assertEqual(code, 1)
            output = buf.getvalue()
            self.assertIn("STATUS=MISSING", output)
            self.assertIn("VALUE_REDACTED=TRUE", output)

        with mock.patch.dict(
            os.environ,
            {ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN: MARKER},
            clear=True,
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = sp._cmd_readback(SECRET_ID_ZERNIO_ANALYTICS_BEARER)
            self.assertEqual(code, 0)
            output = buf.getvalue()
            self.assertIn("STATUS=PRESENT_READABLE", output)
            self.assertIn("VALUE_REDACTED=TRUE", output)
            self.assertNotIn(MARKER, output)


if __name__ == "__main__":
    unittest.main(verbosity=2)