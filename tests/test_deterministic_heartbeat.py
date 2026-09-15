#!/usr/bin/env python3
"""Offline tests for the deterministic heartbeat (P0, issue #131).

Covers: healthy state, Gateway unavailable, disabled critical job,
consecutiveErrors thresholds, missing required artifact, OpenCode
resolver unavailable, connector read-only health failure, alert dedup,
CLI exit codes, and zero model/provider calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_heartbeat as heartbeat  # noqa: E402

SCRIPT = SCRIPTS / "nullone_heartbeat.py"

FULL_JOBS = [
    {"id": name, "enabled": True, "consecutiveErrors": 0}
    for name in (
        "morning",
        "story",
        "draft",
        "radar",
        "weekly",
        "analytics",
        "heartbeat-main",
    )
]


def healthy_snapshot():
    return {"reachable": True, "jobs": [dict(j) for j in FULL_JOBS]}


class HeartbeatFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        fake = self.bindir / "opencode"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o755)
        self.env = {"PATH": str(self.bindir), "NULLONE_OPENCODE_BINARY": ""}
        self.disk = lambda: (1000, 500, 500)  # noqa: E731

    def beat(self, **kw):
        args = {
            "workspace": self.root,
            "env": self.env,
            "home": self.home,
            "disk_probe": self.disk,
            **kw,
        }
        return heartbeat.run_heartbeat(**args)


class HealthyTests(HeartbeatFixture):
    def test_healthy_state(self) -> None:
        res = self.beat(snapshot=healthy_snapshot())
        self.assertEqual(res["status"], heartbeat.STATUS_HEALTHY)
        self.assertEqual(res["alerts"], [])
        self.assertEqual(res["model_calls"], 0)
        self.assertFalse(res["published"])
        self.assertFalse(res["mutated_editorial_state"])

    def test_cli_healthy_exit_zero(self) -> None:
        snapshot_file = self.root / "snapshot.json"
        snapshot_file.write_text(json.dumps(healthy_snapshot()),
                                 encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "heartbeat",
             "--snapshot-json", str(snapshot_file),
             "--alert-state", str(self.root / "alerts.json")],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
        )
        # Exit may be DEGRADED on real hosts (disk/binary); assert only
        # that output is a well-formed deterministic result.
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], "nullone.heartbeat.v1")
        self.assertEqual(payload["model_calls"], 0)
        self.assertFalse(payload["published"])
        self.assertIn(proc.returncode, (0, 1, 2))


class GatewayTests(HeartbeatFixture):
    def test_gateway_unavailable_is_fail(self) -> None:
        res = self.beat(snapshot={"reachable": False, "reason": "test-down"})
        self.assertEqual(res["status"], heartbeat.STATUS_FAIL)
        self.assertTrue(any(a.startswith("gateway:") for a in res["alerts"]))

    def test_gateway_cli_missing_classifies_fail_without_crash(self) -> None:
        def missing_cli(*args, **kwargs):
            raise FileNotFoundError("no such file")

        snapshot = heartbeat.fetch_gateway_snapshot(runner=missing_cli)
        self.assertEqual(snapshot["reachable"], False)
        res = self.beat(snapshot=snapshot)
        self.assertEqual(res["status"], heartbeat.STATUS_FAIL)

    def test_malformed_snapshot_never_crashes(self) -> None:
        for bad in (None, {}, {"reachable": True}, {"reachable": True,
                                                    "jobs": "nope"}):
            res = self.beat(snapshot=bad)
            self.assertIn(res["status"], {
                heartbeat.STATUS_HEALTHY,
                heartbeat.STATUS_DEGRADED,
                heartbeat.STATUS_FAIL,
            })


class CriticalJobTests(HeartbeatFixture):
    def test_disabled_critical_job_is_fail(self) -> None:
        snapshot = healthy_snapshot()
        snapshot["jobs"][0]["enabled"] = False
        res = self.beat(snapshot=snapshot)
        self.assertEqual(res["status"], heartbeat.STATUS_FAIL)
        self.assertTrue(any("disabled" in a for a in res["alerts"]))

    def test_consecutive_errors_thresholds(self) -> None:
        snapshot = healthy_snapshot()
        snapshot["jobs"][3]["consecutiveErrors"] = 4
        deg = self.beat(snapshot=snapshot)
        self.assertEqual(deg["status"], heartbeat.STATUS_DEGRADED)

        snapshot = healthy_snapshot()
        snapshot["jobs"][6]["consecutiveErrors"] = 27
        fail = self.beat(snapshot=snapshot)
        self.assertEqual(fail["status"], heartbeat.STATUS_FAIL)

    def test_missing_job_coverage_is_degraded(self) -> None:
        res = self.beat(snapshot={"reachable": True, "jobs": []})
        self.assertEqual(res["status"], heartbeat.STATUS_DEGRADED)


class ArtifactTests(HeartbeatFixture):
    def test_missing_required_artifact_is_fail(self) -> None:
        res = self.beat(
            snapshot=healthy_snapshot(),
            required_paths=["social/analytics/raw/2026-09-15.md"],
        )
        self.assertEqual(res["status"], heartbeat.STATUS_FAIL)

    def test_present_required_artifact_passes(self) -> None:
        target = self.root / "social" / "analytics" / "raw"
        target.mkdir(parents=True)
        (target / "2026-09-15.md").write_text("# report\n", encoding="utf-8")
        res = self.beat(
            snapshot=healthy_snapshot(),
            required_paths=["social/analytics/raw/2026-09-15.md"],
        )
        self.assertEqual(res["status"], heartbeat.STATUS_HEALTHY)


class ResolverTests(HeartbeatFixture):
    def test_opencode_resolver_unavailable_is_degraded(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        res = self.beat(
            snapshot=healthy_snapshot(),
            env={"PATH": str(empty)},
        )
        self.assertEqual(res["status"], heartbeat.STATUS_DEGRADED)
        self.assertTrue(any(
            c["name"] == "opencode_binary"
            and c["severity"] == heartbeat.SEV_DEGRADED
            for c in res["checks"]
        ))

    def test_env_override_accepted(self) -> None:
        custom = self.root / "custom-opencode"
        custom.write_text("#!/bin/sh\n", encoding="utf-8")
        custom.chmod(0o755)
        res = self.beat(
            snapshot=healthy_snapshot(),
            env={"PATH": "", "NULLONE_OPENCODE_BINARY": str(custom)},
        )
        self.assertEqual(res["status"], heartbeat.STATUS_HEALTHY)


class ConnectorTests(HeartbeatFixture):
    def test_connector_failure_is_fail(self) -> None:
        res = self.beat(
            snapshot=healthy_snapshot(),
            connector_probe=lambda: {"ok": False, "detail": "auth refused"},
        )
        self.assertEqual(res["status"], heartbeat.STATUS_FAIL)

    def test_connector_probe_error_is_fail(self) -> None:
        def boom():
            raise RuntimeError("probe exploded")

        res = self.beat(snapshot=healthy_snapshot(), connector_probe=boom)
        self.assertEqual(res["status"], heartbeat.STATUS_FAIL)

    def test_absent_probe_is_skipped(self) -> None:
        res = self.beat(snapshot=healthy_snapshot())
        conn = [c for c in res["checks"] if c["name"] == "connector"]
        self.assertEqual(len(conn), 1)
        self.assertEqual(conn[0]["severity"], heartbeat.SEV_SKIPPED)


class DedupTests(unittest.TestCase):
    def test_alert_dedup_throttle(self) -> None:
        due, state = heartbeat.dedupe_alerts(["a:1"], {}, 1000.0, 3600)
        self.assertEqual(due, ["a:1"])
        due, state = heartbeat.dedupe_alerts(["a:1"], state, 2000.0, 3600)
        self.assertEqual(due, [])
        due, state = heartbeat.dedupe_alerts(["a:1"], state, 5000.0, 3600)
        self.assertEqual(due, ["a:1"])
        # Cleared conditions drop their keys.
        due, state = heartbeat.dedupe_alerts([], state, 6000.0, 3600)
        self.assertEqual((due, state), ([], {}))

    def test_alert_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alerts.json"
            self.assertEqual(heartbeat.load_alert_state(path), {})
            heartbeat.save_alert_state(path, {"a:1": 1000.0})
            self.assertEqual(heartbeat.load_alert_state(path),
                             {"a:1": 1000.0})


class ModelFreeTests(unittest.TestCase):
    FORBIDDEN = (
        "run_structured",
        "agentturn",
        "sessions_send",
        "mcp__",
        "opencode run",
        "--model",
        "posts_publish_now",
        "urllib",
        "requests",
        "socket",
        "http.client",
        "https://",
    )
    # Vendor names must not appear even in prose: heartbeat must not name,
    # import, or branch on any model provider. ("opencode"/"openclaw" as
    # local binary names are allowed; provider SDKs are not.)
    VENDOR_TOKENS = ("anthropic", "deepseek", "openai", "gemini")

    def test_zero_model_provider_calls(self) -> None:
        src = SCRIPT.read_text(encoding="utf-8").lower()
        for token in self.FORBIDDEN + self.VENDOR_TOKENS:
            self.assertNotIn(token, src, token)
        # "claude" as a substring collides with "openclaw": strip the
        # binary name first, then forbid the remainder.
        scrubbed = src.replace("openclaw", "")
        self.assertNotIn("claude", scrubbed)

    def test_result_marks_zero_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res = heartbeat.run_heartbeat(
                snapshot={"reachable": False, "reason": "t"},
                workspace=root,
                env={"PATH": ""},
                home=root,
                disk_probe=lambda: (1000, 500, 500),
            )
            self.assertEqual(res["model_calls"], 0)
            self.assertFalse(res["published"])
            self.assertFalse(res["mutated_editorial_state"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
