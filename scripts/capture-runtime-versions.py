#!/usr/bin/env python3
"""Capture safe runtime version metadata as machine-readable JSON.

Read-only. No network, no tokens, no auth/session inspection.
This is a capture mechanism, never a version upgrader.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys


def _run(exe: str, args: list[str]) -> str | None:
    if shutil.which(exe) is None:
        return None
    try:
        cp = subprocess.run([exe, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    out = (cp.stdout.strip() or cp.stderr.strip()).splitlines()
    return out[0].strip()[:200] if out else None


def _pillow() -> str | None:
    try:
        import PIL

        return str(PIL.__version__)[:50]
    except ImportError:
        return None


def capture() -> dict:
    return {
        "schema": "nullone.runtime-versions/v1",
        "python": platform.python_version(),
        "pillow": _pillow(),
        "node": _run("node", ["--version"]),
        "openclaw": _run("openclaw", ["--version"]),
        "claude": _run("claude", ["--version"]),
        "git": _run("git", ["--version"]),
        "platform": platform.platform(),
    }


def main() -> int:
    print(json.dumps(capture(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
