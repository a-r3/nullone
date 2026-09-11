"""CI gate: prove exact target SHA has successful NullOne CI. Fail closed.

Real implementation uses authenticated local `gh` tooling. Tests inject
a mock via NULONE_CI_MOCK env var or explicit adapter.
"""
from __future__ import annotations

import json
import os
import subprocess


class CIError(Exception):
    pass


MOCK_VAR = "NULONE_CI_MOCK"


def check_ci_mock_mode() -> str | None:
    v = os.environ.get(MOCK_VAR, "").strip().lower()
    return v or None


def check_ci_success(repo_root: str, sha: str) -> tuple[bool, str]:
    """Return (proven_success, detail). Raises CIError when status cannot be proven.

    Mock modes (env NULONE_CI_MOCK): success | failure | pending | missing | error
    """
    mock = check_ci_mock_mode()
    if mock:
        if mock == "success":
            return True, "MOCK_CI_SUCCESS"
        if mock == "failure":
            raise CIError("CI_FAILED (mock)")
        if mock == "pending":
            raise CIError("CI_PENDING (mock)")
        if mock == "missing":
            raise CIError("CI_MISSING (mock)")
        if mock == "error":
            raise CIError("CI_STATUS_UNKNOWN (mock transport error)")
        raise CIError(f"CI_MOCK_INVALID: {mock!r}")
    return _check_ci_via_gh(sha)


def _check_ci_via_gh(sha: str) -> tuple[bool, str]:
    # Prefer check-runs for the exact commit; fail closed on any ambiguity.
    # Uses `gh api` so auth comes from the local gh session (never stored).
    endpoints = [
        ["gh", "api", f"repos/a-r3/nullone/commits/{sha}/check-runs", "--paginate", "-q", ".check_runs | map(.conclusion + ':' + .status) | join(',')"],
    ]
    last_err = "unknown"
    for cmd in endpoints:
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise CIError("CI_STATUS_UNKNOWN: `gh` CLI not available")
        except subprocess.TimeoutExpired as e:
            raise CIError(f"CI_STATUS_UNKNOWN: gh timeout") from e
        if cp.returncode != 0:
            last_err = cp.stderr.strip()[:300] or cp.stdout.strip()[:300]
            continue
        out = cp.stdout.strip()
        if not out:
            raise CIError("CI_MISSING: no check runs for commit")
        parts = [p.strip() for p in out.split(",") if p.strip()]
        if not parts:
            raise CIError("CI_MISSING: no check runs for commit")
        # proven success requires: at least one run, all completed+success
        bad = [p for p in parts if p != "success:completed"]
        if bad:
            # distinguish pending vs failed
            if any(":in_progress" in p or ":queued" in p or ":pending" in p for p in bad):
                raise CIError(f"CI_PENDING: {out[:300]}")
            raise CIError(f"CI_FAILED: {out[:300]}")
        return True, f"GH_CHECK_RUNS_SUCCESS: {out[:300]}"
    raise CIError(f"CI_STATUS_UNKNOWN: gh api failed: {last_err[:300]}")
