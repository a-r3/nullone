#!/usr/bin/env python3
"""Production AnalyticsProvider factory boundary (#59/#61 seam).

`AnalyticsWorkflow` (#59, `nullone_analytics_workflow.py`) depends only on
an injected `provider_factory` callable matching #29's existing
`run_daily_analytics(build_connector=...)` signature exactly. This module
supplies the *production* factory boundary: it constructs the #29
`ZernioReadOnlyAnalyticsConnector` behind the reviewed secret boundary
(`nullone_secret_provider.py`), wired in only at the CLI layer
(`nullone-scheduled-run.py`, `nullone-daily-analytics-run.py`) -- never
inside `nullone_analytics_workflow.py` itself.

Construction performs no network calls and writes nothing to disk. The
only effect of calling it is binding the canonical account id from
`nullone_bridge_common` to a GET-only connector whose transport was
created from the injected secret.

Secret failure semantics (#28):
- typed `SecretNotConfiguredError` (missing / blank / whitespace-only
  secret; unknown secret id is treated as missing the same way) ->
  `ConnectorUnauthorizedError` (domain `BLOCKED`), with the fixed reason
  text below;
- typed `SecretUnavailableError` (unreadable secret source) ->
  `ConnectorUnavailableError` (domain `BLOCKED`), sanitized -- the
  provider's own message is never propagated.

These two typed secret errors are the only provider failures this module
classifies. Anything else the provider raises -- for example an unrelated
`RuntimeError`, `TypeError`, `AttributeError`, or `AssertionError`, i.e. a
programming defect, not a typed secret failure -- is never swallowed or
reclassified: it propagates so the `AnalyticsWorkflow` reports it as
`RUNTIME_CRASHED` (`application_execution=FAILED`). The same applies to
truly unexpected programming defects below the provider call (for example
in transport construction).

The environment-variable binding for the credential exists only inside
`nullone_secret_provider.py`; this module requests the logical id
`zernio.analytics.bearer` and never mentions an environment variable.
"""
from __future__ import annotations

import argparse

from nullone_bridge_common import CANONICAL_ACCOUNT_ID
from nullone_secret_provider import (
    SECRET_ID_ZERNIO_ANALYTICS_BEARER,
    EnvironmentSecretProvider,
    SecretNotConfiguredError,
    SecretProvider,
    SecretUnavailableError,
)
from nullone_zernio_analytics_adapter import (
    ConnectorUnauthorizedError,
    ConnectorUnavailableError,
    ZernioReadOnlyAnalyticsConnector,
    build_authenticated_transport,
)

# Fixed, generic reason texts. Never derived from a provider exception.
CREDENTIAL_MISSING_REASON = "Zernio analytics credential is missing or was rejected."
CREDENTIAL_UNAVAILABLE_REASON = (
    "Zernio analytics secret could not be read from its runtime source."
)


def build_production_analytics_provider(
    *,
    secret_provider: SecretProvider | None = None,
) -> ZernioReadOnlyAnalyticsConnector:
    """Construct the production `AnalyticsProvider` (#29 connector).

    `secret_provider` defaults to `EnvironmentSecretProvider`, which reads
    the credential from the inherited process environment -- the runtime
    source proven for the OpenClaw Gateway's child commands (issue #61,
    branch A). The logical secret id requested here is
    `zernio.analytics.bearer`; the environment-variable mapping is owned by
    `nullone_secret_provider.py`.
    """

    provider: SecretProvider = (
        secret_provider if secret_provider is not None else EnvironmentSecretProvider()
    )

    try:
        token = provider.get_required(SECRET_ID_ZERNIO_ANALYTICS_BEARER)
    except SecretNotConfiguredError:
        raise ConnectorUnauthorizedError(CREDENTIAL_MISSING_REASON) from None
    except SecretUnavailableError:
        raise ConnectorUnavailableError(CREDENTIAL_UNAVAILABLE_REASON) from None

    transport = build_authenticated_transport(token=token)

    return ZernioReadOnlyAnalyticsConnector(
        transport,
        account_id=CANONICAL_ACCOUNT_ID,
    )


def self_test() -> int:
    from nullone_secret_provider import SecretUnavailableError, SecretValue

    marker = "FAKE_ZERNIO_SECRET_DO_NOT_LOG_123"

    # Missing/blank secret -> ConnectorUnauthorizedError (BLOCKED), fixed text.
    class MissingProvider:
        def get_required(self, secret_id):
            raise SecretNotConfiguredError("not configured")

    missing = _fails_closed_as(MissingProvider(), ConnectorUnauthorizedError)
    assert str(missing) == CREDENTIAL_MISSING_REASON

    # Unknown secret id -> missing-secret (BLOCKED).
    class UnknownIdProvider:
        def get_required(self, secret_id):
            raise SecretNotConfiguredError("unknown secret id")

    assert type(_fails_closed_as(UnknownIdProvider(), ConnectorUnauthorizedError)) is ConnectorUnauthorizedError

    # Typed SecretUnavailableError -> ConnectorUnavailableError (BLOCKED), sanitized:
    # the fake secret-like payload must never appear.
    class UnavailableProvider:
        def get_required(self, secret_id):
            raise SecretUnavailableError(
                f"credential store unreachable with value {marker}"
            )

    unavailable = _fails_closed_as(UnavailableProvider(), ConnectorUnavailableError)
    assert str(unavailable) == CREDENTIAL_UNAVAILABLE_REASON
    assert marker not in str(unavailable)

    # Unexpected programming defect (RuntimeError) -> propagates out of factory.
    class BrokenProvider:
        def get_required(self, secret_id):
            raise RuntimeError("secret daemon died")

    try:
        build_production_analytics_provider(secret_provider=BrokenProvider())
    except RuntimeError as exc:
        assert "secret daemon died" in str(exc)
    else:
        raise AssertionError("factory swallowed RuntimeError")

    # Present secret -> connector constructed with the canonical account
    # id; the value never leaks through any rendering.
    class PresentEnv(EnvironmentSecretProvider):
        def __init__(self):
            self._environ = {
                EnvironmentSecretProvider.bound_env_var(
                    SECRET_ID_ZERNIO_ANALYTICS_BEARER
                ): marker,
            }

    connector = build_production_analytics_provider(secret_provider=PresentEnv())
    assert connector._account_id == CANONICAL_ACCOUNT_ID
    assert marker not in repr(connector)
    assert marker not in repr(connector._transport)
    assert "redacted" in repr(connector._transport)
    assert type(connector._transport.token) is SecretValue

    print("ANALYTICS_PROVIDER_FACTORY_SELF_TEST=PASS")
    print("SECRET_REDACTED=TRUE")
    print("NO_NETWORK=TRUE")
    return 0


def _fails_closed_as(provider, expected_error):
    """Offline self-test helper: normalize the expected typed failure."""
    try:
        build_production_analytics_provider(secret_provider=provider)
    except expected_error as exc:
        return exc
    raise AssertionError(
        f"factory did not fail closed into {expected_error.__name__}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "NullOne AnalyticsProvider production factory boundary (#59/#61 seam)"
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())