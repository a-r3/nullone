#!/usr/bin/env python3
"""Production read-only reconciliation provider factory (issue #169).

Mirrors `nullone_analytics_provider_factory.py`'s shape exactly: the
reconciliation CLI (`nullone-publish-reconcile.py`) depends only on an
injected `provider_factory` callable, and this module supplies the
*production* factory boundary, constructing the GET-only
`ZernioPublishReadOnlyReconciler` (`nullone_zernio_publish_adapter.py`)
behind the reviewed secret boundary (`nullone_secret_provider.py`).

This is DELIBERATELY separate from `nullone_publish_provider_factory.py`
(the daemon-owned, write-capable publish factory): the reconciliation
identity (`zernio.publish.reconcile.bearer`) is a distinct secret from
the publish bearer, and the object this factory returns has no
PUT/POST-capable method at all. Construction performs no network calls
and writes nothing to disk.

Secret failure semantics mirror the analytics factory (#28): typed
`SecretNotConfiguredError` -> `PublishConnectorUnauthorizedError`
(domain BLOCKED); typed `SecretUnavailableError` ->
`PublishConnectorUnavailableError` (domain BLOCKED), sanitized. Anything
else propagates as a programming defect, never swallowed.
"""
from __future__ import annotations

import argparse

from nullone_secret_provider import (
    SECRET_ID_ZERNIO_PUBLISH_RECONCILE_BEARER,
    EnvironmentSecretProvider,
    SecretNotConfiguredError,
    SecretProvider,
    SecretUnavailableError,
    SecretValue,
)
from nullone_zernio_publish_adapter import (
    PublishConnectorUnauthorizedError,
    PublishConnectorUnavailableError,
    ZernioPublishReadOnlyReconciler,
    build_direct_reconcile_provider,
)

# Fixed, generic failure text. Never derived from a provider exception.
CREDENTIAL_MISSING_REASON = (
    "Zernio reconciliation credential is missing or was rejected."
)
CREDENTIAL_UNAVAILABLE_REASON = (
    "Zernio reconciliation secret could not be read from its runtime source."
)


def build_production_reconcile_provider(
    *,
    secret_provider: SecretProvider | None = None,
) -> ZernioPublishReadOnlyReconciler:
    """Construct the production read-only reconciliation reader.

    `secret_provider` defaults to `EnvironmentSecretProvider`: reconciliation
    runs standalone (no controller daemon, no private spawn pipe), the
    same runtime-source shape proven for analytics/drafts. The logical
    secret id requested here is `zernio.publish.reconcile.bearer`, bound
    to `ZERNIO_PUBLISH_RECONCILE_API_TOKEN` -- never the daemon's
    `zernio.publish.bearer`.
    """
    provider: SecretProvider = (
        secret_provider if secret_provider is not None else EnvironmentSecretProvider()
    )

    try:
        provider.get_required(SECRET_ID_ZERNIO_PUBLISH_RECONCILE_BEARER)
    except SecretNotConfiguredError:
        raise PublishConnectorUnauthorizedError(
            CREDENTIAL_MISSING_REASON
        ) from None
    except SecretUnavailableError:
        raise PublishConnectorUnavailableError(
            CREDENTIAL_UNAVAILABLE_REASON
        ) from None

    return build_direct_reconcile_provider(secret_provider=provider)


def self_test() -> int:
    marker = "FAKE_ZERNIO_RECONCILE_SECRET_DO_NOT_LOG_123"

    class MissingProvider:
        def get_required(self, secret_id):
            raise SecretNotConfiguredError("not configured")

    missing = _fails_closed_as(MissingProvider(), PublishConnectorUnauthorizedError)
    assert str(missing) == CREDENTIAL_MISSING_REASON

    class UnavailableProvider:
        def get_required(self, secret_id):
            raise SecretUnavailableError(
                f"credential store unreachable with value {marker}"
            )

    unavailable = _fails_closed_as(
        UnavailableProvider(), PublishConnectorUnavailableError
    )
    assert str(unavailable) == CREDENTIAL_UNAVAILABLE_REASON
    assert marker not in str(unavailable)

    class BrokenProvider:
        def get_required(self, secret_id):
            raise RuntimeError("secret daemon died")

    try:
        build_production_reconcile_provider(secret_provider=BrokenProvider())
    except RuntimeError as exc:
        assert "secret daemon died" in str(exc)
    else:
        raise AssertionError("factory swallowed RuntimeError")

    class PresentEnv(EnvironmentSecretProvider):
        def __init__(self):
            self._environ = {
                EnvironmentSecretProvider.bound_env_var(
                    SECRET_ID_ZERNIO_PUBLISH_RECONCILE_BEARER
                ): marker,
            }

    provider = build_production_reconcile_provider(secret_provider=PresentEnv())
    assert isinstance(provider, ZernioPublishReadOnlyReconciler)
    assert not hasattr(provider, "promote_once")
    assert not hasattr(provider, "put")
    assert marker not in repr(provider._delegate)
    assert marker not in repr(provider._delegate._transport)
    assert type(provider._delegate._transport.token) is SecretValue

    print("PUBLISH_RECONCILE_PROVIDER_FACTORY_SELF_TEST=PASS")
    print("SECRET_REDACTED=TRUE")
    print("NO_NETWORK=TRUE")
    print("NO_PUT_CAPABLE_METHOD=TRUE")
    return 0


def _fails_closed_as(provider, expected_error):
    try:
        build_production_reconcile_provider(secret_provider=provider)
    except expected_error as exc:
        return exc
    raise AssertionError(
        f"factory did not fail closed into {expected_error.__name__}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "NullOne read-only publish-reconciliation provider factory (issue #169)"
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
