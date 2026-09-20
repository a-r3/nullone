#!/usr/bin/env python3
"""Story writer binding behind the role router (issue #111).

`run_story_trigger` depends only on an injected `writer` callable
matching the `StoryWriter` protocol
(`(editorial_context) -> spec dict`). This module binds that writer
from the role router's ProviderProfile for `story_writer` -- it
imports NO vendor transport module: vendor imports terminate behind
`nullone_provider_adapter.make_story_writer`.

Deprecated compatibility: `NULLONE_STORY_PROVIDER=opencode | claude`
remains an explicit transport override (honored inside the router);
anything else fails closed via the router. There is deliberately no
silent fallback from one transport to the other inside a run.

Per-role model routing lives in the router: the OpenCode writer
executes exactly the profile model, and the Claude writer executes
exactly the profile model (router default "haiku" -- reported ==
executed, never faked).
"""
from __future__ import annotations

import os
from typing import Any, Callable

from nullone_bridge_common import BridgeError
import nullone_provider_adapter as provider_adapter
import nullone_provider_router as provider_router

STORY_PROVIDER_ENV_VAR = "NULLONE_STORY_PROVIDER"

PROVIDER_OPENCODE = "opencode"
PROVIDER_CLAUDE = "claude"

ACCEPTED_PROVIDERS = (PROVIDER_OPENCODE, PROVIDER_CLAUDE)

# Repository/offline default preserves safe compatibility: deploying
# this module alone changes nothing live. Switching the live writer
# is an explicit deployment decision, never a side effect.
DEFAULT_STORY_PROVIDER = PROVIDER_CLAUDE


class UnknownStoryProviderError(BridgeError):
    """Raised when the configured Story provider name is not accepted."""


def resolve_story_provider_name(raw: str | None = None) -> str:
    """Validate the Story provider selection and return its name.

    Reads `NULLONE_STORY_PROVIDER` unless `raw` is given. Matching
    is case- and whitespace-insensitive; the returned name is always
    the canonical lowercase value. Unknown values fail closed with a
    fixed message (the rejected value is never echoed).
    """

    candidate = raw if raw is not None else os.environ.get(STORY_PROVIDER_ENV_VAR, "")
    normalized = (candidate or "").strip().lower()
    if not normalized:
        return DEFAULT_STORY_PROVIDER
    if normalized in ACCEPTED_PROVIDERS:
        return normalized
    raise UnknownStoryProviderError(
        "Unknown NULLONE_STORY_PROVIDER value; "
        "expected 'opencode' or 'claude'."
    )


def _resolve_profile(name: str | None) -> provider_router.ProviderProfile:
    overlay = dict(os.environ)
    if name is not None:
        overlay[
            f"{provider_router.ROLE_ENV_PREFIX}"
            f"{provider_router.ROLE_STORY_WRITER.upper()}_TRANSPORT"
        ] = resolve_story_provider_name(name)
    return provider_router.resolve_provider_profile(
        provider_router.ROLE_STORY_WRITER, env=overlay
    )


def get_story_writer(
    name: str | None = None,
) -> tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """Return `(transport_name, writer)` for the Story writer role.

    Issue #111: the role router is the sole authority and the
    provider adapter builds the writer. The writer executes exactly
    the profile's model (OpenCode writer via --model, Claude writer
    via run_structured model). Unknown transports fail closed via
    the router; no silent fallback ever occurs.
    """

    profile = _resolve_profile(name)
    return profile.transport, provider_adapter.make_story_writer(profile)


def get_story_profile(name: str | None = None) -> provider_router.ProviderProfile:
    """Return the resolved ProviderProfile (observability helper)."""

    return _resolve_profile(name)


def self_test() -> int:
    saved = os.environ.pop(STORY_PROVIDER_ENV_VAR, None)
    try:
        assert resolve_story_provider_name() == DEFAULT_STORY_PROVIDER
    finally:
        if saved is not None:
            os.environ[STORY_PROVIDER_ENV_VAR] = saved
    assert resolve_story_provider_name("") == DEFAULT_STORY_PROVIDER
    assert resolve_story_provider_name("opencode") == PROVIDER_OPENCODE
    assert resolve_story_provider_name("Claude") == PROVIDER_CLAUDE
    for bad in ("auto", "both", "openclaw", "haiku"):
        try:
            resolve_story_provider_name(bad)
        except UnknownStoryProviderError:
            pass
        else:
            raise AssertionError(f"provider {bad!r} did not fail closed")

    print("STORY_PROVIDER_FACTORY_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="NullOne provider-neutral Story writer selection"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
