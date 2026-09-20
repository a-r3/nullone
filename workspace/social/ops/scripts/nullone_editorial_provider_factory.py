#!/usr/bin/env python3
"""Provider-neutral editorial transport selection for Morning Editorial.

`run_morning_editorial` / `run_morning_workflow` depend only on an
injected zero-argument `invoke_provider` callable. This module is the
single place that callable is chosen, so switching transports never
requires rewriting workflow code:

    NULLONE_EDITORIAL_PROVIDER=opencode | claude

- `opencode` -> `nullone_opencode_editorial_provider`
  (intended primary transport);
- `claude` -> `nullone_claude_editorial_provider`
  (preserved fallback/rollback transport);
- unset or blank -> `claude` (safe compatibility: no live switch
  happens by merely deploying this module);
- anything else -> `UnknownEditorialProviderError`, fail closed.
  There is deliberately no silent fallback from one transport to the
  other inside a run: a misconfigured name surfaces loudly instead
  of running on an unintended transport.

Callers log the resolved name (`EDITORIAL_PROVIDER=<name>` in the
Morning CLI; `context["editorial_provider"]` on the scheduled path)
so the choice is always visible in run metadata. Names are fixed
identifiers, never credential material.
"""
from __future__ import annotations

import os
from typing import Callable

from nullone_bridge_common import BridgeError
from nullone_claude_editorial_provider import (
    default_invoke_provider as claude_invoke_provider,
)
from nullone_opencode_editorial_provider import (
    default_invoke_provider as opencode_invoke_provider,
)
from nullone_provider_router import (
    ROLE_MORNING_EDITORIAL,
    ProviderProfile,
    resolve_provider_profile,
)

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


def _resolve_profile(name: str | None) -> ProviderProfile:
    from nullone_provider_router import ROLE_ENV_PREFIX

    overlay = dict(os.environ)
    if name is not None:
        overlay[f"{ROLE_ENV_PREFIX}{ROLE_MORNING_EDITORIAL.upper()}_TRANSPORT"] = (
            resolve_editorial_provider_name(name)
        )
    return resolve_provider_profile(ROLE_MORNING_EDITORIAL, env=overlay)


def get_editorial_provider(name: str | None = None) -> tuple[str, Callable[[], None]]:
    """Return `(transport_name, invoke_provider)` for Morning Editorial.

    Issue #111: the role router (`resolve_provider_profile`) is the
    sole authority. The returned zero-arg callable is bound to the
    profile's model; workflows never choose transports or models.
    The deprecated `name` argument (and `NULLONE_EDITORIAL_PROVIDER`
    inside the router) remains an explicit transport override only.
    Unknown transports fail closed via the router; no silent
    fallback ever occurs.
    """

    profile = _resolve_profile(name)
    return profile.transport, _bind_invoker(profile)


def get_editorial_profile(name: str | None = None) -> ProviderProfile:
    """Return the resolved ProviderProfile (observability helper)."""

    return _resolve_profile(name)


def _bind_invoker(profile: ProviderProfile) -> Callable[[], None]:
    if profile.transport == PROVIDER_OPENCODE:
        def invoke_opencode() -> None:
            opencode_invoke_provider(model=profile.model)

        return invoke_opencode
    def invoke_claude() -> None:
        claude_invoke_provider(model=profile.model)

    return invoke_claude


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
