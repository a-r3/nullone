#!/usr/bin/env python3
"""Morning Editorial provider binding behind the role router (issue #111).

`run_morning_editorial` / `run_morning_workflow` depend only on an
injected zero-argument `invoke_provider` callable. This module binds
that callable from the role router's ProviderProfile for
`morning_editorial` -- it imports NO vendor transport module: vendor
imports terminate behind `nullone_provider_adapter`.

Deprecated compatibility: `NULLONE_EDITORIAL_PROVIDER=opencode |
claude` remains an explicit transport override (honored inside the
router); anything else fails closed via the router. There is
deliberately no silent fallback from one transport to the other
inside a run.
"""
from __future__ import annotations

import os
from typing import Callable

from nullone_bridge_common import BridgeError
import nullone_provider_adapter as provider_adapter
import nullone_provider_router as provider_router

EDITORIAL_PROVIDER_ENV_VAR = "NULLONE_EDITORIAL_PROVIDER"

PROVIDER_OPENCODE = "opencode"
PROVIDER_CLAUDE = "claude"

ACCEPTED_PROVIDERS = (PROVIDER_OPENCODE, PROVIDER_CLAUDE)

# Repository/offline default preserves safe compatibility: deploying
# this module alone changes nothing live. Switching the live
# transport is an explicit deployment decision, never a side effect.
DEFAULT_EDITORIAL_PROVIDER = PROVIDER_CLAUDE


class UnknownEditorialProviderError(BridgeError):
    """Raised when the configured editorial provider name is not accepted."""


def resolve_editorial_provider_name(raw: str | None = None) -> str:
    """Validate the editorial provider selection and return its name.

    Reads `NULLONE_EDITORIAL_PROVIDER` unless `raw` is given. Matching
    is case- and whitespace-insensitive; the returned name is always
    the canonical lowercase value. Unknown values fail closed. The
    rejected value is never echoed: it is operator configuration, and
    error text stays fixed.
    """

    candidate = raw if raw is not None else os.environ.get(EDITORIAL_PROVIDER_ENV_VAR, "")
    normalized = (candidate or "").strip().lower()
    if not normalized:
        return DEFAULT_EDITORIAL_PROVIDER
    if normalized in ACCEPTED_PROVIDERS:
        return normalized
    raise UnknownEditorialProviderError(
        "Unknown NULLONE_EDITORIAL_PROVIDER value; "
        "expected 'opencode' or 'claude'."
    )


def _resolve_profile(name: str | None) -> provider_router.ProviderProfile:
    overlay = dict(os.environ)
    if name is not None:
        overlay[
            f"{provider_router.ROLE_ENV_PREFIX}"
            f"{provider_router.ROLE_MORNING_EDITORIAL.upper()}_TRANSPORT"
        ] = resolve_editorial_provider_name(name)
    return provider_router.resolve_provider_profile(
        provider_router.ROLE_MORNING_EDITORIAL, env=overlay
    )


def get_editorial_provider(name: str | None = None) -> tuple[str, Callable[[], None]]:
    """Return `(transport_name, invoke_provider)` for Morning Editorial.

    Issue #111: the role router is the sole authority and the
    provider adapter is the sole execution path. The returned
    zero-arg callable runs the profile's transport through the
    adapter registry with the profile's model; workflows never
    choose transports or models. The deprecated `name` argument
    (and `NULLONE_EDITORIAL_PROVIDER` inside the router) remains an
    explicit transport override only. Unknown transports fail
    closed via the router; no silent fallback ever occurs.
    """

    from nullone_bridge_common import WORKSPACE

    profile = _resolve_profile(name)

    def invoke() -> None:
        prompt = (WORKSPACE / "social/ops/prompts/morning-editorial.md").read_text(
            encoding="utf-8"
        )
        provider_adapter.invoke_role_cycle(profile, prompt, WORKSPACE)

    return profile.transport, invoke


def get_editorial_profile(name: str | None = None) -> provider_router.ProviderProfile:
    """Return the resolved ProviderProfile (observability helper)."""

    return _resolve_profile(name)


def self_test() -> int:
    saved = os.environ.pop(EDITORIAL_PROVIDER_ENV_VAR, None)
    try:
        assert resolve_editorial_provider_name() == DEFAULT_EDITORIAL_PROVIDER
    finally:
        if saved is not None:
            os.environ[EDITORIAL_PROVIDER_ENV_VAR] = saved
    assert resolve_editorial_provider_name("") == DEFAULT_EDITORIAL_PROVIDER
    assert resolve_editorial_provider_name("   ") == DEFAULT_EDITORIAL_PROVIDER
    assert resolve_editorial_provider_name("opencode") == PROVIDER_OPENCODE
    assert resolve_editorial_provider_name("OpEnCoDe") == PROVIDER_OPENCODE
    assert resolve_editorial_provider_name("claude") == PROVIDER_CLAUDE
    for bad in ("auto", "both", "openclaw", "claude-code"):
        try:
            resolve_editorial_provider_name(bad)
        except UnknownEditorialProviderError:
            pass
        else:
            raise AssertionError(f"provider {bad!r} did not fail closed")

    print("EDITORIAL_PROVIDER_FACTORY_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="NullOne provider-neutral editorial transport selection"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
