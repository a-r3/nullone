#!/usr/bin/env python3
"""Capture safe runtime version metadata as machine-readable JSON.

Read-only. No network, no tokens, no auth/session inspection.
Font entries record path/existence/sha256/package metadata ONLY —
never font file contents. This is a capture mechanism, never a version
upgrader.
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REQUIRED_FONTS = {
    "regular": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
}


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


def _font_identity(path: str) -> dict:
    """Safe drift identity for one required font: existence, sha256,
    optional OS package/version. Never the file contents."""
    entry: dict = {"path": path}
    p = Path(path)
    entry["exists"] = p.is_file() and not p.is_symlink()
    if not entry["exists"]:
        entry["sha256"] = None
        entry["package"] = None
        entry["package_version"] = None
        return entry
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    entry["sha256"] = h.hexdigest()
    entry["package"] = None
    entry["package_version"] = None
    if shutil.which("dpkg-query") is not None:
        try:
            cp = subprocess.run(
                ["dpkg-query", "-S", str(p)], capture_output=True,
                text=True, timeout=30)
            if cp.returncode == 0 and ":" in cp.stdout:
                pkg = cp.stdout.split(":", 1)[0].strip().split(",")[0].strip()
                entry["package"] = pkg[:100] or None
                if entry["package"]:
                    cp2 = subprocess.run(
                        ["dpkg-query", "-W", "-f=${Version}", entry["package"]],
                        capture_output=True, text=True, timeout=30)
                    if cp2.returncode == 0 and cp2.stdout.strip():
                        entry["package_version"] = cp2.stdout.strip()[:100]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return entry


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
        "fonts": {name: _font_identity(path)
                  for name, path in REQUIRED_FONTS.items()},
    }


def main() -> int:
    print(json.dumps(capture(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
