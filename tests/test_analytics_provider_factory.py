#!/usr/bin/env python3
"""Offline tests for the production AnalyticsProvider factory (#61).

Proves the factory constructs the #29 `ZernioReadOnlyAnalyticsConnector`
behind the reviewed secret boundary: typed missing/blank -> unauthorized
(BLOCKED), unavailable secret source -> unavailable (BLOCKED, sanitized),
present secret -> constructed connector that never renders the value, and
programming defects below the provider call propagate (RUNTIME_CRASHED)
instead of being swallowed. No network access is ever performed and no real
credential is used.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_analytics_provider_factory import (  # noqa: E402
    CREDENTIAL_MISSING_REASON,
    CREDENTIAL_UNAVAILABLE_REASON,
    build_production_analytics_provider,
)
from nullone_bridge_common import CANONICAL_ACCOUNT_ID  # noqa: E402
from nullone_secret_provider import (  # noqa: E402
    ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN,
    SECRET_ID_ZERNIO_ANALYTICS_BEARER,
    EnvironmentSecretProvider,
    SecretNotConfiguredError,
    SecretProviderError,
    SecretUnavailableError,
    SecretValue,
)
from nullone_zernio_analytics_adapter import (  # noqa: E402
    ConnectorUnauthorizedError,
    ConnectorUnavailableError,
    ZernioReadOnlyAnalyticsConnector,
)

MARKER = "FAKE_ZERNIO_SECRET_DO_NOT_LOG_123"


class MissingSecretSemanticsTests(unittest.TestCase):
    def test_absent_secret_fails_closed_to_unauthorized(self):
        provider = EnvironmentSecretProvider(environ={})
        with self.assertRaises(ConnectorUnauthorizedError) as ctx:
            build_production_analytics_provider(secret_provider=provider)
        self.assertEqual(str(ctx.exception), CREDENTIAL_MISSING_REASON)
        self.assertNotIn(ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN, str(ctx.exception))

    def test_blank_secret_is_missing_not_crash(self):
        for blank in ("", "   ", "\t\n"):
            provider = EnvironmentSecretProvider(
                environ={ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN: blank}
            )
            with self.assertRaises(ConnectorUnauthorizedError):
                build_production_analytics_provider(secret_provider=provider)

    def test_unknown_secret_id_is_missing(self):
        class UnknownProvider:
            def get_required(self, secret_id):
                raise SecretNotConfiguredError("not bound")

        with self.assertRaises(ConnectorUnauthorizedError):
            build_production_analytics_provider(secret_provider=UnknownProvider())

    def test_no_network_on_failure_paths(self):
        # Construction must never touch a transport: an unauthorized /
        # unavailable secret fails before any Zernio endpoint is considered.
        provider = EnvironmentSecretProvider(environ={})
        with self.assertRaises(ConnectorUnauthorizedError):
            build_production_analytics_provider(secret_provider=provider)


class UnavailableSecretSemanticsTests(unittest.TestCase):
    def _assert_sanitized(self, provider):
        with self.assertRaises(ConnectorUnavailableError) as ctx:
            build_production_analytics_provider(secret_provider=provider)
        self.assertEqual(str(ctx.exception), CREDENTIAL_UNAVAILABLE_REASON)
        self.assertNotIn(MARKER, str(ctx.exception))

    def test_typed_unavailable_secret_is_sanitized(self):
        class UnavailableProvider:
            def get_required(self, secret_id):
                raise SecretUnavailableError(
                    f"credential store unreachable; raw={MARKER}"
                )

        self._assert_sanitized(UnavailableProvider())

    def test_generic_run_error_is_normal_unavailability_not_crash(self):
        class BrokenProvider:
            def get_required(self, secret_id):
                raise RuntimeError("secret daemon died at 10.0.0.7")

        self._assert_sanitized(BrokenProvider())

    def test_raw_provider_message_never_surfaces(self):
        marker_msg = f"leaked {MARKER} during readback fail"
        class VerboseBrokenProvider:
            def get_required(self, secret_id):
                raise SecretProviderError(marker_msg)

        self._assert_sanitized(VerboseBrokenProvider())


class ProgrammingDefectPropagationTests(unittest.TestCase):
    def test_wrong_return_type_is_not_swallowed(self):
        class WrongTypeProvider:
            def get_required(self, secret_id):
                return "raw-secret-must-never-be-a-token"  # not a SecretValue

        with self.assertRaises(TypeError):
            build_production_analytics_provider(secret_provider=WrongTypeProvider())


class PresentSecretConstructionTests(unittest.TestCase):
    def _connector(self):
        provider = EnvironmentSecretProvider(
            environ={ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN: MARKER}
        )
        return build_production_analytics_provider(secret_provider=provider)

    def test_connector_is_constructed_with_canonical_account(self):
        connector = self._connector()
        self.assertIsInstance(connector, ZernioReadOnlyAnalyticsConnector)
        self.assertEqual(connector._account_id, CANONICAL_ACCOUNT_ID)

    def test_connector_is_get_only(self):
        connector = self._connector()
        public = {
            name
            for name in dir(connector)
            if not name.startswith("_")
        }
        self.assertEqual(
            public,
            {
                "get_account",
                "get_account_insights",
                "get_follower_history",
                "get_post_analytics",
            },
        )

    def test_transport_holds_secret_value_and_never_renders_it(self):
        connector = self._connector()
        transport = connector._transport
        self.assertIsInstance(transport.token, SecretValue)
        for rendering in (repr(transport), str(transport), repr(connector)):
            self.assertNotIn(MARKER, rendering)
        self.assertIn("redacted", repr(transport))

    def test_default_provider_uses_runtime_environment(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {ENV_VAR_ZERNIO_ANALYTICS_API_TOKEN: MARKER}):
            connector = build_production_analytics_provider()
        self.assertIsInstance(connector, ZernioReadOnlyAnalyticsConnector)
        self.assertNotIn(MARKER, repr(connector._transport))

    def test_factory_accepts_secret_provider_as_keyword_only(self):
        import inspect

        sig = inspect.signature(build_production_analytics_provider)
        self.assertEqual(list(sig.parameters), ["secret_provider"])
        self.assertIs(sig.parameters["secret_provider"].kind, inspect.Parameter.KEYWORD_ONLY)


class ProductionSeamToWorkflowTests(unittest.TestCase):
    def test_missing_secret_at_seam_yields_blocked_domain_result(self):
        import json
        import os
        from unittest import mock

        from nullone_analytics_workflow import run_analytics_workflow

        def make_trigger(**overrides):
            from nullone_scheduler_invocation import compute_occurrence_id

            base = {
                "schema": "nullone.scheduler-invocation.v1",
                "contract_version": "1.0.0",
                "workflow_id": "daily-analytics",
                "source": "openclaw",
                "external_occurrence_id": "occ-factory-seam-61",
                "scheduled_for": "2026-09-08T23:20:00Z",
                "triggered_at": "2026-09-08T23:20:02Z",
            }
            base.update(overrides)
            base["occurrence_id"] = compute_occurrence_id(
                base["workflow_id"],
                base["source"],
                base["external_occurrence_id"],
                base["scheduled_for"],
            )
            return base

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.dict(os.environ, {}, clear=True):
                result = run_analytics_workflow(
                    make_trigger(),
                    provider_factory=build_production_analytics_provider,
                    artifact_root=root,
                    output_root=root / "run-outcomes",
                )
            # Workflow-level contract: orchestration completed with a
            # graceful blocked domain result (never RUNTIME_CRASHED).
            self.assertEqual(result.application_execution, "COMPLETED")
            self.assertEqual(result.domain_outcome, "BLOCKED")
            self.assertEqual(result.reason_code, "OK")
            self.assertNotIn(MARKER, result.reason_text)

            # The authoritative #27 domain reason lives on the persisted
            # result, exactly as the missing-secret mapping requires.
            from nullone_run_outcome import make_run_id, result_path

            persisted = json.loads(
                result_path(
                    root / "run-outcomes",
                    make_run_id(
                        workflow_id="daily-analytics",
                        occurrence_id=make_trigger()["occurrence_id"],
                    ),
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                persisted["reason_code"], "ZERNIO_ANALYTICS_UNAUTHORIZED"
            )
            self.assertNotIn(MARKER, persisted["reason_text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)