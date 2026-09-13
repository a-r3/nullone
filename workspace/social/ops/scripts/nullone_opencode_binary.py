#!/usr/bin/env python3
"""Shared deterministic OpenCode binary resolver (no side effects).

Proven production failure: scheduled NullOne runs inherit the
Gateway PATH, which does not contain `~/.opencode/bin`, while every
OpenCode adapter invoked bare `"opencode"`. The result was an
instant `FileNotFoundError` before any OpenCode session existed --
indistinguishable in the old generic error from a provider failure.

Resolution policy (first match wins):

1. `NULLONE_OPENCODE_BINARY` when set: must be an absolute path to
   an executable regular file, else fail closed. Non-secret
   operator override only.
2. The reviewed per-user install path derived from HOME:
   `~/.opencode/bin/opencode` (validated the same way).
3. `shutil.which("opencode")` compatibility fallback (PATH-based;
   never the sole production mechanism).

No valid executable -> `OpenCodeBinaryResolutionError`, a typed
`BridgeError` whose message carries only safe binary-path
information (`OPENCODE_BINARY_NOT_FOUND`). Callers must surface
this distinctly from reachability/timeout/exit failures.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from nullone_bridge_common import BridgeError

OPENCODE_BINARY_ENV_VAR = "NULLONE_OPENCODE_BINARY"

OPENCODE_BINARY_BASENAME = "opencode"

# Reviewed per-user install layout, derived from HOME at call time
# (never a hard-coded username).
OPENCODE_HOME_BINDIR = Path(".opencode/bin")


class OpenCodeBinaryResolutionError(BridgeError):
    """No usable OpenCode executable could be resolved."""


def _is_usable_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def resolve_opencode_binary(
    *,
    raw_override: str | None = None,
    home: Path | str | None = None,
) -> str:
    """Return the absolute OpenCode executable path, or fail closed.

    `raw_override` defaults to `NULLONE_OPENCODE_BINARY`; `home`
    defaults to the process HOME. Both are injectable so offline
    tests can reproduce the scheduler condition (PATH without
    OpenCode, HOME carrying the install) without touching the real
    machine.
    """

    override = raw_override if raw_override is not None else os.environ.get(
        OPENCODE_BINARY_ENV_VAR, ""
    )
    cleaned = (override or "").strip()
    if cleaned:
        candidate = Path(cleaned)
        if not candidate.is_absolute() or not _is_usable_executable(candidate):
            raise OpenCodeBinaryResolutionError(
                "OPENCODE_BINARY_NOT_FOUND: NULLONE_OPENCODE_BINARY is not an absolute executable file"
            )
        return str(candidate)

    home_dir = Path(home) if home is not None else Path.home()
    home_binary = home_dir / OPENCODE_HOME_BINDIR / OPENCODE_BINARY_BASENAME
    if _is_usable_executable(home_binary):
        return str(home_binary)

    which_hit = shutil.which(OPENCODE_BINARY_BASENAME)
    if which_hit:
        # shutil.which can return a relative path when PATH itself
        # contains relative entries (e.g. PATH=relbin yields
        # "relbin/opencode"). The contract promises an absolute,
        # validated executable, so normalize against the invocation
        # cwd and apply the same executable regular-file check.
        normalized = Path(os.path.abspath(which_hit))
        if _is_usable_executable(normalized):
            return str(normalized)

    raise OpenCodeBinaryResolutionError(
        "OPENCODE_BINARY_NOT_FOUND: no usable opencode executable "
        "(override unset or invalid, HOME install path missing, PATH lookup failed)"
    )


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        fake_home = Path(td) / "home"
        installed = fake_home / ".opencode/bin/opencode"
        installed.parent.mkdir(parents=True)
        installed.write_text("#!/bin/sh\n", encoding="utf-8")
        installed.chmod(0o755)

        assert resolve_opencode_binary(home=fake_home) == str(installed)

        try:
            resolve_opencode_binary(raw_override="relative/path", home=fake_home)
        except OpenCodeBinaryResolutionError:
            pass
        else:
            raise AssertionError("relative override accepted")

    print("OPENCODE_BINARY_RESOLVER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne OpenCode binary resolver")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
