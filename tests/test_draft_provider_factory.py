#!/usr/bin/env python3
"""Offline tests for the production DraftProvider factory (#81).

Proves the factory constructs the #81 `ZernioDraftProvider` behind the
reviewed secret boundary: typed missing/blank -> unauthorized (BLOCKED),
unavailable secret source -> unavailable (BLOCKED, sanitized), present
secret -> constructed provider that never renders the value, and
programming defects below the provider call propagate (crash) instead of
being swallowed. No network access is ever performed and no real
credential is used.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import CANONICAL_ACCOUNT_ID  # noqa: E402
from nullone_draft_provider_factory import (  # noqa: E402
    CREDENTIAL_MISSING_REASON,
    CREDENTIAL_UNAVAILABLE_REASON,
    build_production_draft_provider,
)
from nullone_secret_provider import (  # noqa: E402
    ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN,
    ENV_VAR_ZERNIO_DRAFT_API_TOKEN,
    SECRET_ID_ZERNIO_ANALYTICS_BEARER,
    SECRET_ID_ZERNIO_DRAFTS_BEARER,
    EnvironmentSecretProvider,
    SecretNotConfiguredError,
    SecretProviderError,
    SecretUnavailableError,
    SecretValue,
)
from nullone_zernio_draft_adapter import (  # noqa: E402
    DraftConnectorUnauthorizedError,
    DraftConnectorUnavailableError,
    ZernioDraftProvider,
)

MARKER = "FAKE_ZERNIO_DRAFT_SECRET_DO_NOT_LOG_123"


class MissingSecretSemanticsTests(unittest.TestCase):
    def test_absent_secret_fails_closed_to_unauthorized(self):
        provider = EnvironmentSecretProvider(environ={})
        with self.assertRaises(DraftConnectorUnauthorizedError) as ctx:
            build_production_draft_provider(secret_provider=provider)
        self.assertEqual(str(ctx.exception), CREDENTIAL_MISSING_REASON)

    def test_blank_secret_is_missing_not_crash(self):
        for blank in ("", "   ", "\t\n"):
            provider = EnvironmentSecretProvider(
                environ={ENV_VAR_ZERNIO_DRAFT_API_TOKEN: blank}
            )
            with self.assertRaises(DraftConnectorUnauthorizedError):
                build_production_draft_provider(secret_provider=provider)

    def test_unknown_secret_id_is_missing(self):
        class UnknownProvider:
            def get_required(self, secret_id):
                raise SecretNotConfiguredError("not bound")

        with self.assertRaises(DraftConnectorUnauthorizedError):
            build_production_draft_provider(secret_provider=UnknownProvider())

    def test_no_network_on_failure_paths(self):
        provider = EnvironmentSecretProvider(environ={})
        with self.assertRaises(DraftConnectorUnauthorizedError):
            build_production_draft_provider(secret_provider=provider)


class UnavailableSecretSemanticsTests(unittest.TestCase):
    def _assert_sanitized(self, provider):
        with self.assertRaises(DraftConnectorUnavailableError) as ctx:
            build_production_draft_provider(secret_provider=provider)
        self.assertEqual(str(ctx.exception), CREDENTIAL_UNAVAILABLE_REASON)
        self.assertNotIn(MARKER, str(ctx.exception))

    def test_typed_unavailable_secret_is_sanitized(self):
        class UnavailableProvider:
            def get_required(self, secret_id):
                raise SecretUnavailableError(
                    f"credential store unreachable; raw={MARKER}"
                )

        self._assert_sanitized(UnavailableProvider())

    def test_unexpected_runtime_error_propagates_as_crash(self):
        class BrokenProvider:
            def get_required(self, secret_id):
                raise RuntimeError("secret daemon died at 10.0.0.7")

        with self.assertRaises(RuntimeError) as ctx:
            build_production_draft_provider(secret_provider=BrokenProvider())
        self.assertIn("secret daemon died", str(ctx.exception))

    def test_raw_provider_message_never_surfaces(self):
        marker_msg = f"leaked {MARKER} during readback fail"

        class VerboseBrokenProvider:
            def get_required(self, secret_id):
                raise SecretUnavailableError(marker_msg)

        self._assert_sanitized(VerboseBrokenProvider())


class ProgrammingDefectPropagationTests(unittest.TestCase):
    def test_wrong_return_type_is_not_swallowed(self):
        class WrongTypeProvider:
            def get_required(self, secret_id):
                return "raw-secret-must-never-be-a-token"

        with self.assertRaises(TypeError):
            build_production_draft_provider(secret_provider=WrongTypeProvider())


class PresentSecretConstructionTests(unittest.TestCase):
    def _provider(self):
        provider = EnvironmentSecretProvider(
            environ={ENV_VAR_ZERNIO_DRAFT_API_TOKEN: MARKER}
        )
        return build_production_draft_provider(secret_provider=provider)

    def test_provider_is_constructed_with_canonical_account(self):
        provider = self._provider()
        self.assertIsInstance(provider, ZernioDraftProvider)
        self.assertEqual(provider._account_id, CANONICAL_ACCOUNT_ID)

    def test_transport_holds_secret_value_and_never_renders_it(self):
        provider = self._provider()
        transport = provider._transport
        self.assertIsInstance(transport.token, SecretValue)
        for rendering in (repr(transport), str(transport), repr(provider)):
            self.assertNotIn(MARKER, rendering)
        self.assertIn("redacted", repr(transport))

    def test_default_provider_uses_runtime_environment(self):
        import os
        from unittest import mock

        with mock.patch.dict(
            os.environ, {ENV_VAR_ZERNIO_DRAFT_API_TOKEN: MARKER}
        ):
            provider = build_production_draft_provider()
        self.assertIsInstance(provider, ZernioDraftProvider)
        self.assertNotIn(MARKER, repr(provider._transport))

    def test_factory_accepts_secret_provider_as_keyword_only(self):
        import inspect

        sig = inspect.signature(build_production_draft_provider)
        self.assertEqual(list(sig.parameters), ["secret_provider"])
        self.assertIs(
            sig.parameters["secret_provider"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )


class CapabilitySeparationTests(unittest.TestCase):
    """#81: draft credential must be distinct from analytics credential."""

    def test_draft_secret_id_differs_from_analytics(self):
        self.assertNotEqual(
            SECRET_ID_ZERNIO_DRAFTS_BEARER,
            SECRET_ID_ZERNIO_ANALYTICS_BEARER,
        )

    def test_draft_env_var_differs_from_analytics(self):
        self.assertNotEqual(
            ENV_VAR_ZERNIO_DRAFT_API_TOKEN,
            ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN,
        )

    def test_draft_env_var_bound_in_secret_provider(self):
        bound = EnvironmentSecretProvider.bound_env_var(
            SECRET_ID_ZERNIO_DRAFTS_BEARER
        )
        self.assertEqual(bound, ENV_VAR_ZERNIO_DRAFT_API_TOKEN)

    def test_analytics_env_var_still_bound(self):
        bound = EnvironmentSecretProvider.bound_env_var(
            SECRET_ID_ZERNIO_ANALYTICS_BEARER
        )
        self.assertEqual(bound, ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN)

    def test_two_distinct_bindings(self):
        # analytics + drafts (#81); publish (#90) is not env-bound
        self.assertEqual(
            len(EnvironmentSecretProvider.ENV_VAR_BY_SECRET_ID),
            2,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)