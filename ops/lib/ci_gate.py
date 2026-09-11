"""CI gate: prove the `NullOne CI` workflow itself succeeded for the exact target SHA.

Fail closed. Real implementation uses authenticated local `gh` read-only
API tooling. Tests inject a mock via NULONE_CI_MOCK env var.
"""
from __future__ import annotations

import json
import os
import subprocess


class CIError(Exception):
    pass


MOCK_VAR = "NULONE_CI_MOCK"
NULONE_WORKFLOW_NAME = "NullOne CI"
NULONE_REPO = "a-r3/nullone"


def check_ci_mock_mode() -> str | None:
    v = os.environ.get(MOCK_VAR, "").strip().lower()
    return v or None


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


def _mock_runs(mode: str, sha: str) -> list[dict]:
    nul = {"name": NULONE_WORKFLOW_NAME, "head_sha": sha}
    other = {"name": "Some Other Check", "head_sha": sha,
             "status": "completed", "conclusion": "success"}
    table = {
        # legacy modes (kept for compatibility)
        "success": [{**nul, "status": "completed", "conclusion": "success"}],
        "failure": [{**nul, "status": "completed", "conclusion": "failure"}],
        "pending": [{**nul, "status": "in_progress", "conclusion": None}],
        "missing": [],
        # explicit NullOne-CI modes
        "nullone-success": [{**nul, "status": "completed", "conclusion": "success"}],
        "nullone-failure": [{**nul, "status": "completed", "conclusion": "failure"}],
        "nullone-cancelled": [{**nul, "status": "completed", "conclusion": "cancelled"}],
        "nullone-pending": [{**nul, "status": "in_progress", "conclusion": None}],
        "nullone-queued": [{**nul, "status": "queued", "conclusion": None}],
        "nullone-missing": [],
        "unrelated-only": [other],
        "unrelated-only-plus-pending": [
            other,
            {**nul, "status": "in_progress", "conclusion": None},
        ],
    }
    if mode == "error" or mode == "ci-error":
        raise CIError("CI_STATUS_UNKNOWN (mock transport error)")
    if mode not in table:
        raise CIError(f"CI_MOCK_INVALID: {mode!r}")
    return table[mode]


def check_ci_success(repo_root: str, sha: str) -> tuple[bool, str]:
    """Return (proven_success, detail). Raises CIError when status cannot be proven."""
    mock = check_ci_mock_mode()
    if mock:
        runs = _mock_runs(mock, sha)
        return evaluate_runs(runs, sha)
    runs = _fetch_runs_via_gh(sha)
    return evaluate_runs(runs, sha)


def _fetch_runs_via_gh(sha: str) -> list[dict]:
    try:
        cp = subprocess.run(
            ["gh", "api", f"repos/{NULONE_REPO}/actions/runs",
             "-f", f"head_sha={sha}", "-f", "per_page=100"],
            capture_output=True, text=True, timeout=60,
        )
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
