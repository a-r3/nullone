"""CI gate: prove the `NullOne CI` workflow itself succeeded for the exact target SHA.

Fail closed. The live path always uses the real GitHub/gh adapter; no
environment variable can manufacture CI success. Tests inject the adapter
explicitly in Python (mock.patch of _fetch_runs_via_gh or an equivalent
explicit double) — never through process environment.
"""
from __future__ import annotations

import json
import subprocess


class CIError(Exception):
    pass


NULONE_WORKFLOW_NAME = "NullOne CI"
NULONE_REPO = "a-r3/nullone"


def evaluate_runs(runs: list[dict], sha: str) -> tuple[bool, str]:
    """Shared evaluator for real and mocked workflow runs.

    Each run: {name, head_sha, status, conclusion}. Requires at least one
    `NullOne CI` run for head_sha == sha, all completed, all success.
    Unrelated successful checks never count as proof.
    """
    nul = [r for r in runs
           if r.get("name") == NULONE_WORKFLOW_NAME and r.get("head_sha") == sha]
    if not nul:
        raise CIError(f"CI_MISSING: no '{NULONE_WORKFLOW_NAME}' run for {sha[:12]}")
    pending = [r for r in nul if r.get("status") != "completed"]
    if pending:
        raise CIError(f"CI_PENDING: '{NULONE_WORKFLOW_NAME}' not completed for {sha[:12]}")
    bad = [r for r in nul if r.get("conclusion") != "success"]
    if bad:
        conclusions = sorted({str(r.get("conclusion")) for r in bad})
        raise CIError(f"CI_FAILED: '{NULONE_WORKFLOW_NAME}' conclusions={conclusions} for {sha[:12]}")
    return True, f"NULONE_CI_SUCCESS: {len(nul)} run(s) completed/success for {sha[:12]}"


def check_ci_success(repo_root: str, sha: str) -> tuple[bool, str]:
    """Return (proven_success, detail) via the real gh adapter.

    Raises CIError when status cannot be proven. There is intentionally no
    environment-variable override: NULONE_CI_MOCK (or any similar selector)
    has zero effect on this path. Offline tests patch _fetch_runs_via_gh
    explicitly in Python.
    """
    runs = _fetch_runs_via_gh(sha)
    return evaluate_runs(runs, sha)


def build_runs_command(sha: str) -> list[str]:
    """Exact `gh api` command shape for the read-only Actions runs query."""
    return [
        "gh", "api", "--method", "GET", f"repos/{NULONE_REPO}/actions/runs",
        "-f", f"head_sha={sha}", "-f", "per_page=100",
    ]


def _fetch_runs_via_gh(sha: str) -> list[dict]:
    # NOTE: `gh api -f/--field` implies POST. The Actions runs endpoint is
    # a GET endpoint, so --method GET must be explicit (regression-tested).
    cmd = build_runs_command(sha)
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise CIError("CI_STATUS_UNKNOWN: `gh` CLI not available")
    except subprocess.TimeoutExpired as e:
        raise CIError("CI_STATUS_UNKNOWN: gh timeout") from e
    if cp.returncode != 0:
        detail = (cp.stderr.strip() or cp.stdout.strip())[:300]
        raise CIError(f"CI_STATUS_UNKNOWN: gh api failed: {detail}")
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise CIError(f"CI_STATUS_UNKNOWN: gh api invalid JSON: {e}") from e
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        raise CIError("CI_STATUS_UNKNOWN: gh api missing workflow_runs")
    if isinstance(data.get("total_count"), int) and data["total_count"] > len(runs):
        raise CIError("CI_STATUS_UNKNOWN: workflow run list truncated, cannot prove")
    out = []
    for r in runs:
        if not isinstance(r, dict):
            raise CIError("CI_STATUS_UNKNOWN: malformed workflow run entry")
        out.append({
            "name": r.get("name"),
            "head_sha": r.get("head_sha"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
        })
    return out
