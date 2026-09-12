#!/usr/bin/env python3
"""Provider-neutral Story writer selection (issue #112).

`run_story_trigger` depends only on an injected `writer` callable
matching the `StoryWriter` protocol
(`(editorial_context) -> spec dict`). This module is the single place
that writer is chosen, so switching transports never requires
rewriting workflow code:

    NULLONE_STORY_PROVIDER=opencode | claude

- `opencode` -> `OpenCodeStoryWriter` (intended primary transport,
  Muse Spark);
- `claude` -> `HaikuStoryWriter` (preserved fallback/rollback
  writer);
- unset or blank -> `claude` (safe compatibility: deploying this
  module alone changes nothing live);
- anything else -> `UnknownStoryProviderError`, fail closed. There
  is deliberately no silent fallback from one transport to the other
  inside a run.

This is the minimal immediate seam, not the #111 role router: it
covers the Story writer role only. Per-role model routing belongs to
issue #111.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from nullone_bridge_common import BridgeError
from nullone_opencode_story_provider import OpenCodeStoryWriter
from nullone_story_pipeline import HaikuStoryWriter

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


def get_story_writer(
    name: str | None = None,
) -> tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """Return `(provider_name, writer)` for the selected transport."""

    resolved = resolve_story_provider_name(name)
    if resolved == PROVIDER_OPENCODE:
        return resolved, OpenCodeStoryWriter()
    return resolved, HaikuStoryWriter()


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
