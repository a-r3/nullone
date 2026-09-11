#!/usr/bin/env python3
"""Offline tests for the fixture-only restore drill (issue #10).

Every test uses sanitized fixtures in temp directories. No network, no
production, no secrets, no publication side effects. Publisher stays
DISABLED in all scenarios by construction.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "recovery"))

from restore_drill import (  # noqa: E402
    DrillFailed,
    DrillRefused,
    FakeNotifier,
    FakeProviderTruth,
    PUBLISHER_STATE,
    REPORT_FILENAME,
    run_drill,
    run_private_snapshot,
    validate_restore_root,
)

FIXTURE = ROOT / "tests" / "fixtures" / "recovery"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def make_snapshot(base: Path = FIXTURE, mutate=None, remanifest: bool = True) -> Path:
    """Copy the sanitized fixture to temp, apply mutate(dir), and rewrite
    the snapshot manifest to match (tests opt into each break explicitly).
    remanifest=False keeps a deliberately broken manifest untouched."""
    td = Path(tempfile.mkdtemp(prefix="nullone-snap-"))
    for src in sorted(base.rglob("*")):
        if not src.is_file():
            continue
        dest = td / src.relative_to(base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))
    if mutate is not None:
        mutate(td)
    if remanifest:
        rewrite_manifest(td)
    return td


def rewrite_manifest(snap: Path) -> None:
    files = []
    counts = {}
    for p in sorted(snap.rglob("*")):
        if not p.is_file() or p.name == "snapshot-manifest.json":
            continue
        rel = str(p.relative_to(snap))
        files.append({"path": rel, "bytes": len(p.read_bytes()),
                      "sha256": sha256_file(p)})
        if p.suffix == ".jsonl":
            counts[rel] = sum(1 for ln in p.read_text().splitlines() if ln.strip())
    old = json.loads((snap / "snapshot-manifest.json").read_text())
    manifest = {**old, "files": files, "record_counts": counts}
    (snap / "snapshot-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")


class RestoreDrillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nullone-drill-")
        self.addCleanup(self.tmp.cleanup)
        self.snaps: list[Path] = []
        self.addCleanup(self._rm_snaps)

    def _rm_snaps(self):
        for s in self.snaps:
            shutil.rmtree(s, ignore_errors=True)

    def _snap(self, mutate=None, remanifest: bool = True) -> Path:
        s = make_snapshot(mutate=mutate, remanifest=remanifest)
        self.snaps.append(s)
        return s

    def _root(self, name: str) -> Path:
        return Path(self.tmp.name) / name

    def _report(self, root: Path) -> dict:
        return json.loads((root / REPORT_FILENAME).read_text())

    # -- A: complete fixture restores, publisher disabled --
    def test_fresh_temp_root_restore_succeeds(self):
        snap = self._snap()
        root = self._root("rA")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="A")
        self.assertEqual(report["recovery_result"], "SUCCESS")
        self.assertEqual(report["publisher_state"], "DISABLED")
        self.assertEqual(PUBLISHER_STATE, "DISABLED")
        self.assertFalse(report["automatic_retry_allowed"])
        self.assertEqual(report["fake_provider_publish_count"], 0)
        self.assertEqual(report["fake_provider_write_count"], 0)
        self.assertEqual(report["fake_telegram_send_count"], 0)
        self.assertEqual(report["checksum_result"], "PASS")
        self.assertTrue((root / "RESTORE_READY").is_file())
        self.assertGreaterEqual(report["duration_ms"], 0)
        self.assertTrue(report["gaps"] == [])

    def test_nonempty_destination_rejected(self):
        snap = self._snap()
        root = self._root("rN")
        root.mkdir(parents=True)
        (root / "existing.txt").write_text("x")
        with self.assertRaises(DrillRefused):
            run_drill(snapshot_dir=snap, restore_root=root, scenario="A")

    def test_openclaw_like_destination_rejected(self):
        # Real ~/.openclaw subtree (nonexistent probe path): refusal happens
        # before any mkdir/write, so nothing is created or touched.
        probe = Path.home() / ".openclaw" / "nullone-drill-probe-nonexistent"
        self.assertFalse(probe.exists())
        snap = self._snap()
        with self.assertRaises(DrillRefused):
            run_drill(snapshot_dir=snap, restore_root=probe, scenario="A")
        self.assertFalse(probe.exists())

    def test_no_publication_side_effects(self):
        snap = self._snap()
        root = self._root("rS")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="A")
        self.assertEqual(report["fake_provider_publish_count"], 0)
        self.assertEqual(report["fake_telegram_send_count"], 0)
        # The drill module exposes no publisher enable path.
        import restore_drill as rd

        self.assertFalse(hasattr(rd, "enable_publisher"))
        self.assertFalse(hasattr(rd, "publish"))

    def test_complete_history_does_not_enable_publisher(self):
        snap = self._snap()
        root = self._root("rE")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="A",
                           provider_state="PUBLISHED")
        self.assertEqual(report["publisher_state"], "DISABLED")
        self.assertEqual(report["recovery_result"], "SUCCESS")

    # -- C: partial critical history --
    def test_partial_history_check_required(self):
        def drop_receipt(d: Path):
            shutil.rmtree(d / "receipts" / "POST_fix001")

        snap = self._snap(mutate=drop_receipt)
        root = self._root("rC")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="C")
        self.assertEqual(report["recovery_result"], "CHECK_REQUIRED")
        self.assertEqual(report["critical_history_result"], "INCOMPLETE")
        self.assertEqual(report["publisher_state"], "DISABLED")
        self.assertEqual(report["fake_provider_publish_count"], 0)
        self.assertFalse((root / "RESTORE_READY").is_file())

    # -- D: stale attempts=0 + PUBLISHED reconciles forward, no retry --
    def test_stale_published_no_retry(self):
        snap = self._snap()
        root = self._root("rD")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="D",
                           provider_state="PUBLISHED")
        self.assertEqual(report["recovery_result"], "SUCCESS")
        self.assertEqual(report.get("reconciled", {}).get("result"), "PUBLISHED")
        self.assertFalse(report.get("reconciled", {}).get("retry_performed"))
        self.assertEqual(report["fake_provider_publish_count"], 0)
        self.assertEqual(report["fake_provider_write_count"], 0)
        self.assertEqual(report["publisher_state"], "DISABLED")

    # -- H: attempts=0 + DRAFT + missing history --
    def test_remote_draft_missing_history_no_retry(self):
        def drop_receipt(d: Path):
            shutil.rmtree(d / "receipts" / "POST_fix001")

        snap = self._snap(mutate=drop_receipt)
        root = self._root("rH")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="H",
                           provider_state="DRAFT")
        self.assertEqual(report["recovery_result"], "CHECK_REQUIRED")
        self.assertEqual(report["publisher_state"], "DISABLED")
        self.assertFalse(report["automatic_retry_allowed"])
        self.assertEqual(report["fake_provider_publish_count"], 0)
        self.assertNotIn("reconciled", report)

    # -- G: provider unreachable forces CHECK_REQUIRED (never SUCCESS) --
    def test_provider_unreachable_check_required(self):
        snap = self._snap()
        root = self._root("rG")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="G",
                           provider_state="UNREACHABLE")
        self.assertEqual(report["provider_truth"], "UNKNOWN")
        self.assertEqual(report["provider_reachability"], "UNREACHABLE")
        self.assertEqual(report["recovery_result"], "CHECK_REQUIRED")
        self.assertEqual(report["publisher_state"], "DISABLED")
        self.assertFalse(report["automatic_retry_allowed"])
        self.assertFalse((root / "RESTORE_READY").is_file(),
                         "PROVIDER_UNREACHABLE_READY_MARKER must be NO")
        self.assertEqual(report["fake_provider_publish_count"], 0)
        self.assertEqual(report["fake_provider_write_count"], 0)
        self.assertIn("PROVIDER_UNREACHABLE_DURING_RECOVERY",
                      " ".join(report["gaps"]))

    # -- notifier history missing: no auto resend --
    def test_missing_notifier_no_auto_resend(self):
        def drop_notifier(d: Path):
            shutil.rmtree(d / "notifier")

        snap = self._snap(mutate=drop_notifier)
        root = self._root("rN2")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="A")
        self.assertFalse(report["notifier_auto_resend_allowed"])
        self.assertEqual(report["fake_telegram_send_count"], 0)
        self.assertIn("NOTIFIER_HISTORY_MISSING", " ".join(report["gaps"]))

    # -- E: expired signed URL without bytes --
    def test_expired_url_without_bytes_is_gap(self):
        def drop_bytes(d: Path):
            (d / "media" / "artifact-fix001.bin").unlink()

        snap = self._snap(mutate=drop_bytes)
        root = self._root("rE2")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="E")
        self.assertEqual(report["media_result"], "GAP")
        self.assertTrue(any(g.startswith("MEDIA_RECOVERY_GAP") for g in report["gaps"]))

    def test_exact_retained_media_sha_validated(self):
        snap = self._snap()
        root = self._root("rM")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="A")
        self.assertEqual(report["media_result"], "PASS")

    # -- B: encrypted backup, key lost (external gate, no crypto theater) --
    def test_encrypted_backup_key_lost_blocked(self):
        snap = self._snap()
        root = self._root("rB")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="B",
                           require_decryption_key=True,
                           decryption_key_available=False)
        self.assertEqual(report["recovery_result"], "BLOCKED")
        self.assertFalse(report["external_gates"]["encrypted_backup"]["backup_usable"])
        self.assertEqual(report["publisher_state"], "DISABLED")

    # -- F: remote backup outage degrades ordinary, safety unchanged --
    def test_remote_backup_outage(self):
        snap = self._snap()
        root = self._root("rF")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="F",
                           remote_backup_available=False)
        self.assertEqual(report["recovery_result"], "SUCCESS")
        self.assertIn("REMOTE_BACKUP_OUTAGE", " ".join(report["gaps"]))
        self.assertEqual(report["publisher_state"], "DISABLED")

    # -- I/J/K: integrity failures fail closed --
    def test_truncated_copy_fail_closed(self):
        # Truncate AFTER the manifest is written, so declared hash/count
        # still describe the intact file and tampering is detected.
        pristine = json.loads((FIXTURE / "snapshot-manifest.json").read_text())
        pristine_entry = next(e for e in pristine["files"]
                              if e["path"] == "topic-ledger.jsonl")

        def truncate(d: Path):
            p = d / "topic-ledger.jsonl"
            lines = p.read_text().splitlines()
            p.write_text("\n".join(lines[:1]) + "\n")

        snap = self._snap(mutate=truncate)
        cur = json.loads((snap / "snapshot-manifest.json").read_text())
        for e in cur["files"]:
            if e["path"] == "topic-ledger.jsonl":
                e["sha256"] = pristine_entry["sha256"]
        cur["record_counts"]["topic-ledger.jsonl"] = 2
        (snap / "snapshot-manifest.json").write_text(
            json.dumps(cur, indent=2, sort_keys=True) + "\n")
        root = self._root("rI")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="I")
        self.assertEqual(report["recovery_result"], "FAILED")
        self.assertFalse((root / "RESTORE_READY").is_file())

    def test_missing_file_fail_closed(self):
        def drop(d: Path):
            (d / "candidate-queue.md").unlink()

        snap = self._snap(mutate=drop)
        # manifest still declares the file but rewritten hashes would hide
        # the absence; restore the original manifest entry instead.
        rewrite_manifest(snap)
        old = json.loads((FIXTURE / "snapshot-manifest.json").read_text())
        cur = json.loads((snap / "snapshot-manifest.json").read_text())
        entry = next(e for e in old["files"] if e["path"] == "candidate-queue.md")
        cur["files"].append(entry)
        cur["files"].sort(key=lambda e: e["path"])
        (snap / "snapshot-manifest.json").write_text(
            json.dumps(cur, indent=2, sort_keys=True) + "\n")
        root = self._root("rJ")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="J")
        self.assertEqual(report["recovery_result"], "FAILED")
        self.assertIn("candidate-queue.md", " ".join(report["gaps"]))
        self.assertFalse((root / "RESTORE_READY").is_file())

    def test_corrupt_receipt_fail_closed(self):
        def corrupt(d: Path):
            (d / "receipts" / "POST_fix001" / "receipt-uuid-fix001.json").write_text(
                "{not json")

        snap = self._snap(mutate=corrupt)
        root = self._root("rK")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="K")
        self.assertEqual(report["recovery_result"], "CHECK_REQUIRED")
        self.assertEqual(report["critical_history_result"], "INCOMPLETE")
        self.assertEqual(report["publisher_state"], "DISABLED")

    # -- L: interrupted restore never READY --
    def test_interrupted_restore_not_ready(self):
        snap = self._snap()
        root = self._root("rL")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="L",
                           interrupt_at="before_finalize")
        self.assertEqual(report["recovery_result"], "INTERRUPTED")
        self.assertFalse((root / "RESTORE_READY").is_file())
        check = validate_restore_root(root)
        self.assertFalse(check["ready"])
        self.assertEqual(check["reason"], "NOT_FINALIZED")
        self.assertEqual(report["fake_provider_publish_count"], 0)

    def test_validate_complete_root_ready(self):
        snap = self._snap()
        root = self._root("rV")
        run_drill(snapshot_dir=snap, restore_root=root, scenario="A")
        check = validate_restore_root(root)
        self.assertTrue(check["ready"])

    # -- report shape --
    def test_report_contains_duration_and_gaps(self):
        snap = self._snap()
        root = self._root("rR")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="A")
        for field in ("schema", "scenario", "snapshot_kind", "started_at",
                      "finished_at", "duration_ms", "restore_root",
                      "checksum_result", "record_count_result",
                      "referential_result", "media_result",
                      "critical_history_result", "provider_truth",
                      "provider_reachability", "notifier_auto_resend_allowed",
                      "publisher_state", "automatic_retry_allowed",
                      "fake_provider_read_count", "fake_provider_write_count",
                      "fake_provider_publish_count", "fake_telegram_send_count",
                      "recovery_result", "gaps", "external_gates"):
            self.assertIn(field, report, f"report missing {field}")
        self.assertGreaterEqual(report["duration_ms"], 0)

    def test_complete_report_schema_all_outcomes(self):
        required = ("checksum_result", "record_count_result",
                    "referential_result", "media_result",
                    "critical_history_result", "provider_truth",
                    "provider_reachability", "notifier_auto_resend_allowed",
                    "publisher_state", "automatic_retry_allowed",
                    "fake_provider_read_count", "fake_provider_write_count",
                    "fake_provider_publish_count", "fake_telegram_send_count",
                    "recovery_result", "gaps", "external_gates",
                    "started_at", "finished_at", "duration_ms")

        def check(report, result):
            self.assertEqual(report["recovery_result"], result)
            for field in required:
                self.assertIn(field, report, f"{result} report missing {field}")
            self.assertEqual(report["publisher_state"], "DISABLED")
            self.assertFalse(report["automatic_retry_allowed"])
            self.assertEqual(report["fake_provider_write_count"], 0)
            self.assertEqual(report["fake_provider_publish_count"], 0)
            self.assertEqual(report["fake_telegram_send_count"], 0)

        snap = self._snap()
        check(run_drill(snapshot_dir=snap, restore_root=self._root("s-ok"),
                         scenario="A"), "SUCCESS")
        check(run_drill(snapshot_dir=snap, restore_root=self._root("s-blk"),
                         scenario="B", require_decryption_key=True,
                         decryption_key_available=False), "BLOCKED")

        def drop_receipt(d: Path):
            shutil.rmtree(d / "receipts" / "POST_fix001")

        snap_c = self._snap(mutate=drop_receipt)
        check(run_drill(snapshot_dir=snap_c, restore_root=self._root("s-chk"),
                         scenario="C"), "CHECK_REQUIRED")
        check(run_drill(snapshot_dir=snap, restore_root=self._root("s-int"),
                         scenario="L", interrupt_at="before_finalize"),
              "INTERRUPTED")

        def break_manifest(d: Path):
            (d / "snapshot-manifest.json").write_text("{invalid")

        snap_bad = self._snap(mutate=break_manifest, remanifest=False)
        rep = run_drill(snapshot_dir=snap_bad, restore_root=self._root("s-bad"),
                        scenario="X")
        check(rep, "FAILED")

    def test_invalid_manifest_schema_refused_with_report(self):
        def bad_schema(d: Path):
            m = json.loads((d / "snapshot-manifest.json").read_text())
            m["schema"] = "evil-schema"
            (d / "snapshot-manifest.json").write_text(json.dumps(m))

        snap = self._snap(mutate=bad_schema, remanifest=False)
        root = self._root("rSCH")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="X")
        self.assertEqual(report["recovery_result"], "REFUSED")
        self.assertEqual(report["publisher_state"], "DISABLED")
        self.assertIn("checksum_result", report)
        # Unproven input must not be labeled sanitized.
        self.assertEqual(report["snapshot_kind"], "UNKNOWN")

    def _manifest_with_entries(self, snap: Path, files, counts=None) -> None:
        m = json.loads((snap / "snapshot-manifest.json").read_text())
        m["files"] = files
        if counts is not None:
            m["record_counts"] = counts
        (snap / "snapshot-manifest.json").write_text(json.dumps(m))

    def test_manifest_entry_missing_path(self):
        snap = self._snap()
        self._manifest_with_entries(
            snap, [{"sha256": "a" * 64, "bytes": 1}])
        report = run_drill(snapshot_dir=snap, restore_root=self._root("m1"),
                           scenario="X")
        self.assertEqual(report["recovery_result"], "FAILED")
        self.assertEqual(report["publisher_state"], "DISABLED")
        self.assertFalse((self._root("m1") / "RESTORE_READY").exists())

    def test_manifest_entry_missing_sha256(self):
        snap = self._snap()
        self._manifest_with_entries(
            snap, [{"path": "candidate-queue.md", "bytes": 1}])
        report = run_drill(snapshot_dir=snap, restore_root=self._root("m2"),
                           scenario="X")
        self.assertEqual(report["recovery_result"], "FAILED")

    def test_manifest_entry_not_an_object(self):
        snap = self._snap()
        self._manifest_with_entries(snap, ["candidate-queue.md"])
        report = run_drill(snapshot_dir=snap, restore_root=self._root("m3"),
                           scenario="X")
        self.assertEqual(report["recovery_result"], "FAILED")

    def test_manifest_entry_malformed_sha256(self):
        snap = self._snap()
        self._manifest_with_entries(
            snap, [{"path": "candidate-queue.md", "sha256": "not-a-hash"}])
        report = run_drill(snapshot_dir=snap, restore_root=self._root("m4"),
                           scenario="X")
        self.assertEqual(report["recovery_result"], "FAILED")

    def test_record_counts_wrong_type(self):
        snap = self._snap()
        self._manifest_with_entries(
            snap, json.loads((snap / "snapshot-manifest.json").read_text())["files"],
            counts=["not-an-object"])
        report = run_drill(snapshot_dir=snap, restore_root=self._root("m5"),
                           scenario="X")
        self.assertEqual(report["recovery_result"], "FAILED")
        self.assertEqual(report["publisher_state"], "DISABLED")
        self.assertEqual(report["fake_provider_publish_count"], 0)

    def test_symlink_entry_blocked_before_external_read(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir(exist_ok=True)
        secret = outside / "external.txt"
        secret.write_text("EXTERNAL_MARKER_BYTES_NEVER_CONSUME\n")
        snap = self._snap()

        def plant_link(d: Path):
            (d / "linked.txt").symlink_to(secret)

        plant_link(snap)
        # Declare the symlink with the outside file's hash: the harness
        # must refuse before consuming those bytes.
        import hashlib as _hl

        self._manifest_with_entries(snap, [{
            "path": "linked.txt",
            "sha256": _hl.sha256(secret.read_bytes()).hexdigest(),
            "bytes": len(secret.read_bytes()),
        }])
        root = self._root("rLINK")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="X")
        self.assertEqual(report["recovery_result"], "FAILED")
        self.assertIn("SNAPSHOT_ENTRY_FORBIDDEN", " ".join(report["gaps"]))
        self.assertEqual(report["publisher_state"], "DISABLED")
        self.assertFalse((root / "RESTORE_READY").exists())
        # External bytes must not appear anywhere under the restore root.
        for p in root.rglob("*"):
            if p.is_file() and not p.is_symlink():
                self.assertNotIn("EXTERNAL_MARKER_BYTES_NEVER_CONSUME",
                                 p.read_text(errors="replace"))

    def test_manifest_symlink_blocked(self):
        snap = self._snap()
        outside = Path(self.tmp.name) / "outside2"
        outside.mkdir(exist_ok=True)
        fake_manifest = outside / "evil-manifest.json"
        fake_manifest.write_text('{"schema": "evil"}')
        (snap / "snapshot-manifest.json").unlink()
        (snap / "snapshot-manifest.json").symlink_to(fake_manifest)
        root = self._root("rMSL")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="X")
        self.assertEqual(report["recovery_result"], "FAILED")
        self.assertIn("SNAPSHOT_ENTRY_FORBIDDEN", " ".join(report["gaps"]))
        self.assertFalse((root / "RESTORE_READY").exists())

    def test_snapshot_kind_truthful(self):
        snap = self._snap()
        ok = run_drill(snapshot_dir=snap, restore_root=self._root("k-ok"),
                       scenario="A")
        self.assertEqual(ok["snapshot_kind"], "SANITIZED_FIXTURE")

        def bad_schema(d: Path):
            m = json.loads((d / "snapshot-manifest.json").read_text())
            m["schema"] = "evil-schema"
            (d / "snapshot-manifest.json").write_text(json.dumps(m))

        snap_bad = self._snap(mutate=bad_schema, remanifest=False)
        bad = run_drill(snapshot_dir=snap_bad, restore_root=self._root("k-bad"),
                        scenario="X")
        self.assertEqual(bad["snapshot_kind"], "UNKNOWN")

    def test_production_snapshot_source_blocked(self):
        from restore_drill import refuse_unsafe_snapshot_source

        snap = self._snap()
        home = Path.home()
        oc = home / ".openclaw"
        for forbidden in (oc, oc / "workspace", oc / "workspace" / "social", home):
            with self.subTest(path=str(forbidden)):
                with self.assertRaises(DrillRefused) as cm:
                    run_drill(snapshot_dir=forbidden,
                              restore_root=self._root("rPS"), scenario="A")
                self.assertIn("SNAPSHOT_SOURCE_FORBIDDEN", str(cm.exception))
                with self.assertRaises(DrillRefused):
                    refuse_unsafe_snapshot_source(forbidden)
        # Temp sanitized fixture copy stays accepted.
        root = self._root("rPSok")
        report = run_drill(snapshot_dir=snap, restore_root=root, scenario="A")
        self.assertEqual(report["recovery_result"], "SUCCESS")

    def test_snapshot_source_refusal_writes_nothing(self):
        home = Path.home()
        probe_root = Path(self.tmp.name) / "untouched"
        with self.assertRaises(DrillRefused):
            run_drill(snapshot_dir=home / ".openclaw",
                      restore_root=probe_root, scenario="A")
        self.assertFalse(probe_root.exists())

    def test_no_secret_fixture_leakage(self):
        # Every fixture file must be committable: repo secret-safety
        # ignore rules (auth*, token*, secrets*, ...) must not silently
        # exclude fixture content from checkouts (regression: an
        # auth-uuid receipt once went missing in CI for exactly this reason).
        import subprocess as _sp

        for p in sorted(FIXTURE.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(ROOT))
            cp = _sp.run(["git", "check-ignore", "-q", rel],
                         capture_output=True, cwd=str(ROOT))
            self.assertNotEqual(
                cp.returncode, 0,
                f"fixture file is git-ignored and would vanish from checkouts: {rel}")
        tracked = subprocess.run(
            ["git", "ls-files", "tests/fixtures/recovery/"],
            capture_output=True, text=True, cwd=str(ROOT)).stdout.split()
        for p in sorted(FIXTURE.rglob("*")):
            if p.is_file():
                self.assertIn(str(p.relative_to(ROOT)), tracked)
        blob_parts = []
        for p in FIXTURE.rglob("*"):
            if p.is_file():
                blob_parts.append(p.read_text(errors="replace"))
        blob = "\n".join(blob_parts).lower()
        for marker in ("telegram-owner", "presign", "bearer", "oauth",
                       "BEGIN PRIVATE KEY", "sk-", "ghp_"):
            self.assertNotIn(marker, blob)
        manifest = json.loads((FIXTURE / "snapshot-manifest.json").read_text())
        self.assertEqual(manifest["source_kind"], "SANITIZED_FIXTURE")

    def test_private_snapshot_gate(self):
        with self.assertRaises(DrillRefused) as cm:
            run_private_snapshot(snapshot_path="/x", output_root="/y")
        self.assertIn("REQUIRED_RAUF_ALIZADA", str(cm.exception))

    def test_fake_provider_writes_forbidden(self):
        provider = FakeProviderTruth("PUBLISHED")
        self.assertEqual(provider.read_status("POST_fix001"), "PUBLISHED")
        self.assertEqual(provider.read_calls, 1)
        with self.assertRaises(AssertionError):
            provider.publish("POST_fix001")
        with self.assertRaises(AssertionError):
            provider.write_status("POST_fix001")
        notifier = FakeNotifier()
        with self.assertRaises(AssertionError):
            notifier.send("hello")

    def test_harness_has_no_network_or_subprocess(self):
        text = (ROOT / "scripts" / "recovery" / "restore_drill.py").read_text()
        for forbidden_import in ("import socket", "import urllib", "import ssl",
                                 "import subprocess", "import requests",
                                 "from urllib", "import http"):
            self.assertNotIn(forbidden_import, text)
        for forbidden_call in ("urlopen(", "socket.", "subprocess.",
                               "Popen(", "os.environ", "getpass("):
            self.assertNotIn(forbidden_call, text)
        self.assertNotIn("enable_publisher", text)

    def test_cli_entry_point(self):
        snap = self._snap()
        root = self._root("rCLI")
        cp = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "recovery" / "restore_drill.py"),
             "--snapshot", str(snap), "--restore-root", str(root),
             "--scenario", "A"],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("RECOVERY_RESULT=SUCCESS", cp.stdout)
        self.assertIn("PUBLISHER_STATE=DISABLED", cp.stdout)
        self.assertIn("FAKE_PROVIDER_PUBLISH_COUNT=0", cp.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
