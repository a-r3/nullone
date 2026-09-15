#!/usr/bin/env python3
"""Deterministic NullOne heartbeat (P0, issue #131).

Replaces the legacy model-backed heartbeat agent (which failed repeatedly
with agent-runner-failure once its provider chain died) with pure
deterministic checks. No model. No tokens. No publication. No editorial
state mutation. Stdlib only.

Checks (each yields ok / degraded / fail / skipped):

- gateway reachable via the local scheduler CLI (best effort, read-only)
- critical automation enabled state (a disabled critical job is a fail)
- consecutiveErrors against documented thresholds
- required domain/artifact paths exist (caller-supplied; default: skipped)
- OpenCode binary resolver health (existence only, mirroring the reviewed
  resolver policy: NULLONE_OPENCODE_BINARY, ~/.opencode/bin, PATH)
- filesystem / storage sanity (read-only probes, no writes)
- current code SHA for correlation (informational only, never fails)
- safe read-only connector health ONLY when the caller injects a probe;
  heartbeat itself never opens network connections

Overall: FAIL if any check fails, else DEGRADED if any check is degraded,
else HEALTHY. Alerts are derived ONLY from concrete failing/degraded
conditions and are deduplicated by cooldown (see ``dedupe_alerts``).
Delivery itself stays outside this module (existing failure-notify path).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve()
# Script lives at <workspace>/social/ops/scripts/nullone_heartbeat.py.
WORKSPACE = SCRIPT.parents[3]

STATUS_HEALTHY = "HEALTHY"
STATUS_DEGRADED = "DEGRADED"
STATUS_FAIL = "FAIL"

SEV_OK = "ok"
SEV_DEGRADED = "degraded"
SEV_FAIL = "fail"
SEV_SKIPPED = "skipped"
SEV_INFO = "info"

# Consecutive-error thresholds for scheduler jobs.
ERRORS_DEGRADED_AT = 3
ERRORS_FAIL_AT = 10

# Storage thresholds (free ratio).
DISK_FAIL_BELOW = 0.10
DISK_DEGRADED_BELOW = 0.20

# Critical automation coverage: a job matches when one of these keys is a
# substring of its id/name (case-insensitive).
CRITICAL_JOB_KEYS = (
    "morning",
    "story",
    "draft",
    "radar",
    "weekly",
    "analytics",
    "heartbeat",
)

DEFAULT_ALERT_COOLDOWN_S = 6 * 3600

EXIT_HEALTHY = 0
EXIT_DEGRADED = 1
EXIT_FAIL = 2


def _check(name: str, severity: str, detail: str) -> dict:
    return {"name": name, "severity": severity, "detail": detail}


def check_gateway(snapshot: dict | None) -> dict:
    """Classify Gateway reachability from a scheduler snapshot."""
    if not isinstance(snapshot, dict) or snapshot.get("reachable") is not True:
        reason = "unreachable"
        if isinstance(snapshot, dict) and snapshot.get("reason"):
            reason = str(snapshot["reason"])[:200]
        return _check("gateway", SEV_FAIL, f"gateway unreachable: {reason}")
    return _check("gateway", SEV_OK, "gateway reachable")


def _job_text(job: dict) -> str:
    return f"{job.get('id', '')} {job.get('name', '')}".lower()


def check_critical_jobs(snapshot: dict | None) -> list[dict]:
    """Enabled-state, error-budget, and coverage checks for critical jobs."""
    results: list[dict] = []
    if not isinstance(snapshot, dict) or snapshot.get("reachable") is not True:
        return [_check("critical_jobs", SEV_SKIPPED, "gateway unreachable")]
    jobs = snapshot.get("jobs")
    if not isinstance(jobs, list):
        return [_check("critical_jobs", SEV_SKIPPED, "no job list in snapshot")]
    seen_keys: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        text = _job_text(job)
        matched = [k for k in CRITICAL_JOB_KEYS if k in text]
        if not matched:
            continue
        label = str(job.get("id") or job.get("name") or "job")[:80]
        for key in matched:
            seen_keys.add(key)
        enabled = job.get("enabled", True)
        if enabled is not True:
            results.append(
                _check(
                    f"job:{label}",
                    SEV_FAIL,
                    f"critical job disabled: {label}",
                )
            )
            continue
        errors = job.get("consecutiveErrors", 0)
        try:
            errors = int(errors)
        except Exception:
            errors = 0
        if errors >= ERRORS_FAIL_AT:
            results.append(
                _check(
                    f"job:{label}",
                    SEV_FAIL,
                    f"consecutiveErrors={errors} >= {ERRORS_FAIL_AT}: {label}",
                )
            )
        elif errors >= ERRORS_DEGRADED_AT:
            results.append(
                _check(
                    f"job:{label}",
                    SEV_DEGRADED,
                    f"consecutiveErrors={errors} >= {ERRORS_DEGRADED_AT}: {label}",
                )
            )
    missing = [k for k in CRITICAL_JOB_KEYS if k not in seen_keys]
    if missing:
        results.append(
            _check(
                "critical_job_coverage",
                SEV_DEGRADED,
                "no scheduler entry matched: " + ",".join(sorted(missing)),
            )
        )
    if not results:
        results.append(
            _check("critical_jobs", SEV_OK, "critical jobs enabled, error budgets ok")
        )
    return results


def check_required_paths(
    paths: list[str], workspace: Path = WORKSPACE
) -> dict:
    """Required domain/artifact completion: every path must exist."""
    if not paths:
        return _check("required_artifacts", SEV_SKIPPED, "no required paths configured")
    missing: list[str] = []
    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workspace / raw
        if not candidate.exists():
            missing.append(raw)
    if missing:
        return _check(
            "required_artifacts",
            SEV_FAIL,
            "missing required artifacts: " + ",".join(missing[:10]),
        )
    return _check(
        "required_artifacts", SEV_OK, f"{len(paths)} required paths present"
    )


def resolve_opencode_binary(
    env: dict | None = None, home: Path | None = None
) -> dict:
    """Mirror the reviewed resolver policy (existence only, no execution)."""
    environ = env if env is not None else os.environ
    override = environ.get("NULLONE_OPENCODE_BINARY", "")
    if override:
        candidate = Path(override)
        if (
            candidate.is_absolute()
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ):
            return {"ok": True, "path": str(candidate), "source": "env"}
        return {"ok": False, "path": override, "source": "env-invalid"}
    base = home if home is not None else Path.home()
    per_user = base / ".opencode" / "bin" / "opencode"
    if per_user.is_file() and os.access(per_user, os.X_OK):
        return {"ok": True, "path": str(per_user), "source": "home"}
    # PATH lookup against the injected environ (never the ambient process
    # PATH when a test/operator environ is supplied).
    for directory in environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "opencode" if directory else None
        if (
            candidate is not None
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ):
            return {"ok": True, "path": str(candidate), "source": "path"}
    return {"ok": False, "path": "", "source": "not-found"}


def check_opencode_binary(
    env: dict | None = None, home: Path | None = None
) -> dict:
    """OpenCode binary availability (degraded when absent, never fatal)."""
    resolved = resolve_opencode_binary(env=env, home=home)
    if resolved["ok"]:
        return _check(
            "opencode_binary",
            SEV_OK,
            f"resolver ok: {resolved['source']}:{resolved['path']}"[:200],
        )
    return _check(
        "opencode_binary",
        SEV_DEGRADED,
        f"resolver unavailable ({resolved['source']})",
    )


def check_filesystem(
    workspace: Path = WORKSPACE, disk_probe=None
) -> list[dict]:
    """Read-only storage sanity. Never writes.

    ``disk_probe`` is an injectable returning ``(total, used, free)``;
    default uses the real filesystem.
    """
    results: list[dict] = []
    if not workspace.is_dir():
        return [_check("filesystem", SEV_FAIL, "workspace root missing")]
    if not os.access(workspace, os.R_OK):
        return [_check("filesystem", SEV_FAIL, "workspace root unreadable")]
    if not os.access(workspace, os.W_OK):
        results.append(
            _check("filesystem", SEV_DEGRADED, "workspace root not writable")
        )
    try:
        if disk_probe is not None:
            total, _used, free = disk_probe()
        else:
            usage = shutil.disk_usage(workspace)
            total, free = usage.total, usage.free
        free_ratio = free / total if total else 0.0
        if free_ratio < DISK_FAIL_BELOW:
            results.append(
                _check(
                    "storage",
                    SEV_FAIL,
                    f"disk free {free_ratio:.1%} < {DISK_FAIL_BELOW:.0%}",
                )
            )
        elif free_ratio < DISK_DEGRADED_BELOW:
            results.append(
                _check(
                    "storage",
                    SEV_DEGRADED,
                    f"disk free {free_ratio:.1%} < {DISK_DEGRADED_BELOW:.0%}",
                )
            )
        else:
            results.append(
                _check("storage", SEV_OK, f"disk free {free_ratio:.1%}")
            )
    except Exception as exc:
        results.append(
            _check("storage", SEV_SKIPPED, f"disk probe unavailable: {exc}"[:160])
        )
    if not results:
        results.append(_check("filesystem", SEV_OK, "workspace readable"))
    else:
        results.insert(0, _check("filesystem", SEV_OK, "workspace readable"))
    return results


def code_health(root: Path | None = None) -> dict:
    """Current code SHA for correlation. Informational only, never fails."""
    repo = root if root is not None else WORKSPACE
    git = shutil.which("git")
    if not git:
        return _check("code", SEV_INFO, "git unavailable")
    try:
        proc = subprocess.run(
            [git, "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        return _check("code", SEV_INFO, f"git probe failed: {exc}"[:160])
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        return _check("code", SEV_INFO, "git sha unavailable")
    return _check("code", SEV_INFO, f"sha={sha[:12]}")


def check_connector(probe=None) -> dict:
    """Safe read-only connector health, ONLY via an injected probe.

    Default (no probe): skipped. Heartbeat itself never opens connections.
    """
    if probe is None:
        return _check("connector", SEV_SKIPPED, "no connector probe configured")
    try:
        result = probe()
    except Exception as exc:
        return _check("connector", SEV_FAIL, f"connector probe error: {exc}"[:160])
    if not isinstance(result, dict):
        return _check("connector", SEV_FAIL, "connector probe malformed")
    if result.get("ok") is True:
        return _check("connector", SEV_OK, str(result.get("detail", "ok"))[:200])
    return _check(
        "connector", SEV_FAIL, str(result.get("detail", "connector unhealthy"))[:200]
    )


def fetch_gateway_snapshot(
    timeout: int = 20,
    runner=None,
) -> dict:
    """Best-effort read-only scheduler snapshot. Never raises, never fails.

    Default runner shells to the local scheduler CLI with a bounded
    timeout; any absence/failure yields ``reachable: False`` (the
    heartbeat then reports FAIL from concrete evidence, not from a
    crash). ``runner`` is injectable for offline tests.
    """
    run = runner if runner is not None else subprocess.run
    binary = shutil.which("openclaw")
    if binary is None and runner is None:
        return {"reachable": False, "reason": "scheduler CLI unavailable"}
    cmd = [binary or "openclaw", "cron", "list", "--json"]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return {"reachable": False, "reason": f"scheduler query failed: {exc}"[:200]}
    if proc.returncode != 0:
        return {
            "reachable": False,
            "reason": f"scheduler query exit={proc.returncode}",
        }
    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception:
        return {"reachable": False, "reason": "scheduler output not JSON"}
    jobs = payload.get("jobs", payload.get("crons", []))
    if not isinstance(jobs, list):
        jobs = []
    normalized: list[dict] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        normalized.append(
            {
                "id": str(job.get("id", "")),
                "name": str(job.get("name", job.get("title", ""))),
                "enabled": job.get("enabled", True),
                "consecutiveErrors": job.get(
                    "consecutiveErrors", job.get("consecutive_errors", 0)
                ),
                "lastRunStatus": str(job.get("lastRunStatus", job.get("last_run", ""))),
            }
        )
    return {"reachable": True, "jobs": normalized}


def run_heartbeat(
    *,
    snapshot: dict | None = None,
    required_paths: list[str] | None = None,
    connector_probe=None,
    workspace: Path = WORKSPACE,
    env: dict | None = None,
    home: Path | None = None,
    disk_probe=None,
) -> dict:
    """Run all deterministic checks and summarize. Pure orchestration."""
    live_snapshot = snapshot
    checks: list[dict] = [check_gateway(live_snapshot)]
    checks.extend(check_critical_jobs(live_snapshot))
    checks.append(check_required_paths(list(required_paths or []), workspace))
    checks.append(check_opencode_binary(env=env, home=home))
    checks.extend(check_filesystem(workspace, disk_probe))
    checks.append(code_health(workspace))
    checks.append(check_connector(connector_probe))

    severities = {c["severity"] for c in checks}
    if SEV_FAIL in severities:
        overall = STATUS_FAIL
    elif SEV_DEGRADED in severities:
        overall = STATUS_DEGRADED
    else:
        overall = STATUS_HEALTHY
    alerts = [
        f"{c['name']}:{c['detail']}"
        for c in checks
        if c["severity"] in (SEV_FAIL, SEV_DEGRADED)
    ]
    return {
        "schema": "nullone.heartbeat.v1",
        "status": overall,
        "model_calls": 0,
        "published": False,
        "mutated_editorial_state": False,
        "checks": checks,
        "alerts": alerts,
        "timestamp": int(time.time()),
    }


def dedupe_alerts(
    alerts: list[str],
    previous: dict,
    now: float,
    cooldown_s: int = DEFAULT_ALERT_COOLDOWN_S,
) -> tuple[list[str], dict]:
    """Cooldown-gated alert dedup. Pure: returns (due, updated_state)."""
    prev = dict(previous or {})
    due: list[str] = []
    for alert in alerts:
        last = prev.get(alert)
        try:
            last_f = float(last) if last is not None else None
        except Exception:
            last_f = None
        if last_f is None or (now - last_f) >= cooldown_s:
            due.append(alert)
            prev[alert] = now
    # Drop keys for conditions that cleared.
    active = set(alerts)
    for key in [k for k in prev if k not in active]:
        del prev[key]
    return due, prev


def default_alert_state_path(workspace: Path = WORKSPACE) -> Path:
    return workspace / "social" / "ops" / ".heartbeat-alert-state.json"


def load_alert_state(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_alert_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def self_test() -> None:
    """Offline deterministic self-test (no network, no model)."""
    import tempfile

    healthy_snapshot = {
        "reachable": True,
        "jobs": [
            {"id": "morning", "enabled": True, "consecutiveErrors": 0},
            {"id": "story", "enabled": True, "consecutiveErrors": 0},
            {"id": "draft", "enabled": True, "consecutiveErrors": 0},
            {"id": "radar", "enabled": True, "consecutiveErrors": 0},
            {"id": "weekly", "enabled": True, "consecutiveErrors": 0},
            {"id": "analytics", "enabled": True, "consecutiveErrors": 0},
            {"id": "heartbeat-main", "enabled": True, "consecutiveErrors": 0},
        ],
    }
    healthy_disk = lambda: (1000, 500, 500)  # noqa: E731 - 50% free fixture
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        fake_bin = Path(tmp) / "opencode"
        fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_bin.chmod(0o755)
        env = {"PATH": tmp, "NULLONE_OPENCODE_BINARY": ""}
        res = run_heartbeat(
            snapshot=healthy_snapshot,
            workspace=Path(tmp),
            env=env,
            home=home,
            disk_probe=healthy_disk,
        )
        assert res["status"] == STATUS_HEALTHY, res
        assert res["model_calls"] == 0
        assert res["published"] is False
        assert res["mutated_editorial_state"] is False
        assert res["alerts"] == [], res

        # Gateway down -> FAIL.
        down = run_heartbeat(
            snapshot={"reachable": False, "reason": "test"},
            workspace=Path(tmp),
            env=env,
            home=home,
            disk_probe=healthy_disk,
        )
        assert down["status"] == STATUS_FAIL, down
        assert any(a.startswith("gateway:") for a in down["alerts"]), down

        # Disabled critical job -> FAIL.
        disabled = run_heartbeat(
            snapshot={
                "reachable": True,
                "jobs": [{"id": "morning", "enabled": False}],
            },
            workspace=Path(tmp),
            env=env,
            home=home,
            disk_probe=healthy_disk,
        )
        assert disabled["status"] == STATUS_FAIL, disabled

        # Consecutive errors cross degraded/fail thresholds.
        deg = run_heartbeat(
            snapshot={
                "reachable": True,
                "jobs": [{"id": "radar", "enabled": True, "consecutiveErrors": 4}],
            },
            workspace=Path(tmp),
            env=env,
            home=home,
            disk_probe=healthy_disk,
        )
        assert deg["status"] == STATUS_DEGRADED, deg
        fail = run_heartbeat(
            snapshot={
                "reachable": True,
                "jobs": [
                    {"id": "heartbeat-main", "enabled": True, "consecutiveErrors": 27}
                ],
            },
            workspace=Path(tmp),
            env=env,
            home=home,
            disk_probe=healthy_disk,
        )
        assert fail["status"] == STATUS_FAIL, fail

        # Missing required artifact -> FAIL.
        missing = run_heartbeat(
            snapshot=healthy_snapshot,
            required_paths=["social/analytics/raw/2026-09-15.md"],
            workspace=Path(tmp),
            env=env,
            home=home,
            disk_probe=healthy_disk,
        )
        assert missing["status"] == STATUS_FAIL, missing

        # Missing binary -> DEGRADED (never fatal for heartbeat itself).
        empty_bin = Path(tmp) / "empty-bin"
        empty_bin.mkdir()
        no_bin = run_heartbeat(
            snapshot=healthy_snapshot,
            workspace=Path(tmp),
            env={"PATH": str(empty_bin)},
            home=home,
            disk_probe=healthy_disk,
        )
        assert no_bin["status"] == STATUS_DEGRADED, no_bin
        assert any(
            c["name"] == "opencode_binary" and c["severity"] == SEV_DEGRADED
            for c in no_bin["checks"]
        ), no_bin

        # Connector probe failure -> FAIL; absent probe -> skipped.
        bad_conn = run_heartbeat(
            snapshot=healthy_snapshot,
            workspace=Path(tmp),
            env=env,
            home=home,
            disk_probe=healthy_disk,
            connector_probe=lambda: {"ok": False, "detail": "auth refused"},
        )
        assert bad_conn["status"] == STATUS_FAIL, bad_conn
        assert all(
            c["severity"] != SEV_FAIL or c["name"] != "connector"
            for c in run_heartbeat(
                snapshot=healthy_snapshot,
                workspace=Path(tmp),
                env=env,
                home=home,
                disk_probe=healthy_disk,
            )["checks"]
        )

        # Storage thresholds are classified from injected probes.
        low = check_filesystem(Path(tmp), lambda: (1000, 950, 50))
        assert any(
            c["name"] == "storage" and c["severity"] == SEV_FAIL for c in low
        ), low
        tight = check_filesystem(Path(tmp), lambda: (1000, 850, 150))
        assert any(
            c["name"] == "storage" and c["severity"] == SEV_DEGRADED
            for c in tight
        ), tight

    # Alert dedup: first fire due, repeat suppressed, re-fire after cooldown.
    due, state = dedupe_alerts(["a:1"], {}, 1000.0, 3600)
    assert due == ["a:1"] and state == {"a:1": 1000.0}, (due, state)
    due, state = dedupe_alerts(["a:1"], state, 2000.0, 3600)
    assert due == [] and state == {"a:1": 1000.0}, (due, state)
    due, state = dedupe_alerts(["a:1"], state, 5000.0, 3600)
    assert due == ["a:1"] and state == {"a:1": 5000.0}, (due, state)
    due, state = dedupe_alerts([], state, 6000.0, 3600)
    assert due == [] and state == {}, (due, state)

    print("HEARTBEAT_SELF_TEST=PASS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["heartbeat", "self-test"])
    parser.add_argument("--snapshot-json", default="",
                        help="read-only scheduler snapshot file (offline/rehearsal)")
    parser.add_argument("--required-path", action="append", default=[],
                        help="required artifact path (repeatable)")
    parser.add_argument("--alert-state", default="",
                        help="alert dedup state file (default: workspace ops dotfile)")
    parser.add_argument("--alert-cooldown-s", type=int,
                        default=DEFAULT_ALERT_COOLDOWN_S)
    parser.add_argument("--no-alert-write", action="store_true",
                        help="evaluate only; do not persist alert state")
    args = parser.parse_args(argv)
    if args.command == "self-test":
        self_test()
        return 0
    snapshot = None
    if args.snapshot_json:
        try:
            snapshot = json.loads(Path(args.snapshot_json).read_text(
                encoding="utf-8"))
        except Exception as exc:
            print(json.dumps({"schema": "nullone.heartbeat.v1",
                              "status": STATUS_FAIL,
                              "model_calls": 0,
                              "published": False,
                              "mutated_editorial_state": False,
                              "checks": [{"name": "snapshot",
                                          "severity": SEV_FAIL,
                                          "detail": f"snapshot unreadable: {exc}"[:200]}],
                              "alerts": ["snapshot:snapshot unreadable"]}))
            return EXIT_FAIL
    else:
        snapshot = fetch_gateway_snapshot()
    result = run_heartbeat(snapshot=snapshot,
                           required_paths=args.required_path)
    state_path = (Path(args.alert_state) if args.alert_state
                  else default_alert_state_path())
    previous = load_alert_state(state_path)
    due, updated = dedupe_alerts(result["alerts"], previous, time.time(),
                                 args.alert_cooldown_s)
    result["alerts_due"] = due
    if not args.no_alert_write:
        try:
            save_alert_state(state_path, updated)
        except Exception as exc:
            result["checks"].append(
                {"name": "alert_state", "severity": SEV_DEGRADED,
                 "detail": f"alert state unwritable: {exc}"[:160]})
            if result["status"] == STATUS_HEALTHY:
                result["status"] = STATUS_DEGRADED
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == STATUS_FAIL:
        return EXIT_FAIL
    if result["status"] == STATUS_DEGRADED:
        return EXIT_DEGRADED
    return EXIT_HEALTHY


if __name__ == "__main__":
    raise SystemExit(main())
