#!/usr/bin/env python3
"""AnalyticsProvider production factory boundary (#59/#61 seam).

`AnalyticsWorkflow` (#59, `nullone_analytics_workflow.py`) depends only on
an injected `provider_factory` callable matching #29's existing
`run_daily_analytics(build_connector=...)` signature exactly. This module
supplies the *production* factory boundary that a real, secret-backed
`AnalyticsProvider` (the #29 `ZernioReadOnlyAnalyticsConnector`) would be
constructed behind, wired in only at the CLI layer
(`nullone-scheduled-run.py`) -- never inside `nullone_analytics_workflow.py`
itself.

Issue #61 (secure scheduled `ZERNIO_ANALYTICS_API_TOKEN` injection) has not
been implemented. This module therefore, deliberately:

- never reads `ZERNIO_ANALYTICS_API_TOKEN` or any other environment
  variable;
- never implements systemd/environment secret loading;
- never creates, reads, or provisions any secret file;
- never constructs a real Zernio connector or calls Zernio.

It fails closed with a distinct, honestly-labeled error instead of faking a
connector attempt. #61 completes real production wiring behind this exact
seam (`build_production_analytics_provider`'s call site in
`nullone-scheduled-run.py`); `AnalyticsWorkflow` and its tests do not need
to change when it does.
"""
from __future__ import annotations

from typing import Any

REASON_CODE_PROVIDER_SECRET_WIRING_PENDING = "PROVIDER_SECRET_WIRING_PENDING_61"


class ProviderSecretWiringPendingError(RuntimeError):
    """Raised instead of constructing a real production AnalyticsProvider.

    Deliberately NOT one of `nullone_zernio_analytics_adapter`'s connector
    errors (`ConnectorUnauthorizedError`/`ConnectorUnavailableError`/...):
    those are caught inside `run_daily_analytics` and become a domain
    `BLOCKED` #27 result, which would misleadingly report that a *real*
    Zernio bootstrap attempt was made. This is a distinct, uncaught
    orchestration condition instead -- `AnalyticsWorkflow` reports it as a
    scheduler/application execution-level failure
    (`reason_code=RUNTIME_CRASHED`, `context.error_type=
    ProviderSecretWiringPendingError`), never as a domain outcome, and no
    #27 result is fabricated. This exception's own message (constructed
    below) is never included in that reported `reason_text`/`context` --
    only the exception's stable class name is -- so the exact pending-#61
    fact is documented statically here and at the CLI docstring, never
    recovered through raw exception text.
    """


def build_production_analytics_provider() -> Any:
    """Fail-closed placeholder for the real production `AnalyticsProvider`.

    Never exercised by any test in this repository (tests inject a fake
    `provider_factory` into `run_analytics_workflow` instead). Completing
    this function to actually construct a
    `nullone_zernio_analytics_adapter.ZernioReadOnlyAnalyticsConnector`
    backed by a securely-injected credential is issue #61's job, not #59's.
    """

    raise ProviderSecretWiringPendingError(
        "Daily Analytics production AnalyticsProvider construction is "
        f"{REASON_CODE_PROVIDER_SECRET_WIRING_PENDING}: pending issue #61 "
        "(secure scheduled secret injection). This placeholder "
        "intentionally never reads any secret, environment variable, or "
        "credential file, and never calls Zernio."
    )


def self_test() -> int:
    try:
        build_production_analytics_provider()
        raise AssertionError("production factory placeholder did not fail closed")
    except ProviderSecretWiringPendingError as exc:
        assert REASON_CODE_PROVIDER_SECRET_WIRING_PENDING in str(exc)

    print("ANALYTICS_PROVIDER_FACTORY_SELF_TEST=PASS")
    print("NO_SECRET_READ=TRUE")
    print("NO_NETWORK=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="NullOne AnalyticsProvider production factory boundary (#59/#61 seam)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
