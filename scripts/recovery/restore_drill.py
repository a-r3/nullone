#!/usr/bin/env python3
"""Disposable publisher-disabled restore drill (issue #10, fixture-only).

Restores a SANITIZED snapshot into a fresh disposable root, validates
checksums/counts/references/media/critical-history against the ADR-01
recovery contract, queries a read-only fake provider, and writes a
machine-readable report. Publisher starts DISABLED and can NEVER be
enabled by this drill; no code path here performs publication.

Safety: refuses destinations under ~/.openclaw (or HOME itself), refuses
non-empty roots, makes no network calls, spawns no subprocesses, touches
no secret store. Real production snapshots require a separate explicit
operator authorization that this tool never assumes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

SNAPSHOT_SCHEMA = "nullone.restore-snapshot/v1"
REPORT_SCHEMA = "nullone.restore-report/v1"

READY_MARKER = "RESTORE_READY"
STAGING_DIRNAME = ".staging"
REPORT_FILENAME = "restore-report.json"

PUBLISHER_STATE = "DISABLED"  # invariant: the drill has no enable path


class DrillRefused(Exception):
    pass


class DrillFailed(Exception):
    pass


# ---------- path safety ----------

def _home() -> Path:
    return Path.home().resolve()


def refuse_unsafe_root(restore_root: Path) -> Path:
    """Resolve and refuse production-ish or non-fresh destinations."""
    root = Path(restore_root)
    try:
        resolved = root.resolve()
    except OSError as e:
        raise DrillRefused(f"RESTORE_ROOT_UNRESOLVABLE: {e}") from e
    home = _home()
    oc = home / ".openclaw"
    if resolved == home or resolved == oc or str(resolved).startswith(str(oc) + "/"):
        raise DrillRefused(
            "RESTORE_ROOT_FORBIDDEN: destination is inside live runtime "
            "(~/.openclaw); disposable fixture roots only")
    if resolved == Path("/") or resolved == home:
        raise DrillRefused("RESTORE_ROOT_FORBIDDEN: refusing filesystem/home root")
    if resolved.exists():
        if not resolved.is_dir():
            raise DrillRefused("RESTORE_ROOT_NOT_A_DIRECTORY")
        if any(resolved.iterdir()):
            raise DrillRefused("RESTORE_ROOT_NOT_EMPTY: restore root must be empty/new")
    return resolved


# ---------- fake provider / notifier (read-only unless misused) ----------

class FakeProviderTruth:
    """Read-only fake provider truth with call counters.

    Valid states: PUBLISHED | DRAFT | UNREACHABLE | UNKNOWN.
    Write/publish methods exist ONLY to fail loudly if ever invoked.
    """

    VALID = ("PUBLISHED", "DRAFT", "UNREACHABLE", "UNKNOWN")

    def __init__(self, state: str = "UNKNOWN"):
        if state not in self.VALID:
            raise ValueError(f"bad fake provider state: {state!r}")
        self.state = state
        self.read_calls = 0
        self.write_calls = 0
        self.publish_calls = 0

    def read_status(self, post_id: str) -> str:
        self.read_calls += 1
        if self.state == "UNREACHABLE":
            return "UNKNOWN"
        return self.state

    def write_status(self, *args, **kwargs):  # pragma: no cover - must never run
        self.write_calls += 1
        raise AssertionError("FAKE_PROVIDER_WRITE_FORBIDDEN")

    def publish(self, *args, **kwargs):  # pragma: no cover - must never run
        self.publish_calls += 1
        raise AssertionError("FAKE_PROVIDER_PUBLISH_FORBIDDEN")


class FakeNotifier:
    """Fake Telegram/result notifier. The drill never sends."""

    def __init__(self):
        self.send_calls = 0

    def send(self, *args, **kwargs):  # pragma: no cover - must never run here
        self.send_calls += 1
        raise AssertionError("FAKE_TELEGRAM_SEND_FORBIDDEN")


# ---------- snapshot handling ----------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_snapshot_manifest(snapshot_dir: Path) -> dict:
    manifest_path = Path(snapshot_dir) / "snapshot-manifest.json"
    if not manifest_path.is_file():
        raise DrillFailed("SNAPSHOT_MANIFEST_MISSING")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise DrillFailed(f"SNAPSHOT_MANIFEST_INVALID: {e}") from e
    if manifest.get("schema") != SNAPSHOT_SCHEMA:
        raise DrillFailed(f"SNAPSHOT_SCHEMA_MISMATCH: {manifest.get('schema')!r}")
    if manifest.get("source_kind") != "SANITIZED_FIXTURE":
        raise DrillFailed("SNAPSHOT_SOURCE_UNTRUSTED: only SANITIZED_FIXTURE accepted")
    if not isinstance(manifest.get("files"), list) or not manifest["files"]:
        raise DrillFailed("SNAPSHOT_FILES_MISSING")
    return manifest


def _copy_verified(src: Path, dest: Path, expected_sha: str) -> int:
    try:
        data = src.read_bytes()
    except OSError as e:
        raise DrillFailed(f"SNAPSHOT_SOURCE_UNREADABLE: {src.name}: {e}") from e
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise DrillFailed(f"CHECKSUM_MISMATCH: {src.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)


# ---------- validation phases (read-only over staged tree) ----------

def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise DrillFailed(f"JSON_INVALID: {path.name}: {e}") from e


def check_record_counts(staged: Path, manifest: dict) -> tuple[bool, list[str]]:
    """Verify JSONL/event counts declared in the manifest."""
    gaps: list[str] = []
    expected_counts = manifest.get("record_counts", {})
    for rel, expected in sorted(expected_counts.items()):
        p = staged / rel
        if not p.is_file():
            gaps.append(f"{rel}: missing")
            continue
        lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
        if len(lines) != expected:
            gaps.append(f"{rel}: count {len(lines)} != declared {expected}")
            continue
        for i, ln in enumerate(lines):
            try:
                json.loads(ln)
            except json.JSONDecodeError:
                gaps.append(f"{rel}: line {i} invalid JSON")
                break
    return (len(gaps) == 0, gaps)


def check_referential(staged: Path) -> tuple[bool, list[str]]:
    """Cross-file reference checks. References never confer authority."""
    gaps: list[str] = []
    manifests = sorted((staged / "manifests").glob("*.json")) \
        if (staged / "manifests").is_dir() else []
    if not manifests:
        return False, ["no manifests restored"]
    for mp in manifests:
        try:
            manifest = json.loads(mp.read_text())
        except (json.JSONDecodeError, OSError):
            gaps.append(f"{mp.name}: unreadable manifest")
            continue
        post_id = manifest.get("post_id")
        if not post_id:
            gaps.append(f"{mp.name}: missing post_id")
            continue
        # receipt binding (presence only; authority decided elsewhere)
        receipts = list((staged / "receipts" / post_id).glob("*.json")) \
            if (staged / "receipts" / post_id).is_dir() else []
        manifest.setdefault("_receipt_count", len(receipts))
        # ledger rows referencing this post must agree on post_id shape
        ledger = staged / "publish-ledger.jsonl"
        if ledger.is_file():
            for ln in ledger.read_text().splitlines():
                if not ln.strip():
                    continue
                try:
                    row = json.loads(ln)
                except json.JSONDecodeError:
                    gaps.append("publish-ledger.jsonl: invalid JSON row")
                    break
                if row.get("post_id") == post_id and not isinstance(
                        row.get("result"), str):
                    gaps.append(f"publish-ledger.jsonl: malformed row for {post_id}")
    # notifier records must reference known posts/workflows
    known_posts = set()
    for mp in manifests:
        try:
            known_posts.add(json.loads(mp.read_text()).get("post_id"))
        except (json.JSONDecodeError, OSError):
            pass
    notifier_dir = staged / "notifier"
    if notifier_dir.is_dir():
        for record in sorted(notifier_dir.rglob("*.json")):
            try:
                row = json.loads(record.read_text())
            except (json.JSONDecodeError, OSError):
                gaps.append(f"{record.name}: invalid notifier record")
                continue
            if row.get("post_id") not in known_posts:
                gaps.append(f"{record.name}: references unknown post")
    return (len(gaps) == 0, gaps)


def check_media(staged: Path) -> tuple[bool, list[str]]:
    """Exact retained bytes must match SHA256; URL/reference alone fails."""
    gaps: list[str] = []
    meta_path = staged / "media" / "media-meta.json"
    if not meta_path.is_file():
        return False, ["media-meta.json missing"]
    try:
        meta = json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return False, [f"media-meta.json invalid: {e}"]
    for entry in meta.get("artifacts", []):
        rel = entry.get("path")
        expected = entry.get("sha256")
        if not rel or not expected:
            gaps.append("media-meta.json: malformed artifact entry")
            continue
        p = staged / rel
        if not p.is_file():
            gaps.append(f"MEDIA_RECOVERY_GAP: {rel} bytes absent (reference is not a copy)")
            continue
        if sha256_file(p) != expected:
            gaps.append(f"MEDIA_HASH_MISMATCH: {rel}")
    return (len(gaps) == 0, gaps)


def check_critical_history(staged: Path) -> tuple[str, list[str]]:
    """Every manifest needs a sibling authorization receipt and a sane
    attempts value. Returns (COMPLETE|INCOMPLETE, gaps)."""
    gaps: list[str] = []
    manifests = sorted((staged / "manifests").glob("*.json")) \
        if (staged / "manifests").is_dir() else []
    if not manifests:
        return "INCOMPLETE", ["no manifests"]
    for mp in manifests:
        try:
            manifest = json.loads(mp.read_text())
        except (json.JSONDecodeError, OSError):
            gaps.append(f"{mp.name}: unreadable (critical history invalid)")
            continue
        attempts = manifest.get("attempts")
        if not isinstance(attempts, int) or attempts < 0:
            gaps.append(f"{mp.name}: bad attempts value")
            continue
        post_id = manifest.get("post_id")
        receipts = list((staged / "receipts" / (post_id or "")).glob("*.json")) \
            if post_id else []
        valid_receipts = 0
        for rp in receipts:
            try:
                row = json.loads(rp.read_text())
                if isinstance(row, dict) and row.get("post_id") == post_id:
                    valid_receipts += 1
                else:
                    gaps.append(f"{rp.name}: corrupt receipt (post mismatch)")
            except (json.JSONDecodeError, OSError):
                gaps.append(f"{rp.name}: corrupt receipt (invalid JSON)")
        if valid_receipts == 0:
            gaps.append(f"{mp.name}: authorization receipt missing")
    return ("COMPLETE" if not gaps else "INCOMPLETE"), gaps


# ---------- drill runner ----------

def run_drill(*,
              snapshot_dir: str | Path,
              restore_root: str | Path,
              scenario: str,
              provider_state: str = "UNKNOWN",
              interrupt_at: str | None = None,
              require_decryption_key: bool = False,
              decryption_key_available: bool = False,
              remote_backup_available: bool = True) -> dict:
    """Run one fixture restore drill. Always writes restore-report.json.
    Publisher stays DISABLED; no publication/notifier side effects exist."""
    started = time.monotonic()
    from datetime import datetime, timezone
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    root = refuse_unsafe_root(Path(restore_root))
    snapshot = Path(snapshot_dir)
    report: dict = {
        "schema": REPORT_SCHEMA,
        "scenario": scenario,
        "snapshot_kind": "SANITIZED_FIXTURE",
        "restore_root": str(root),
        "publisher_state": PUBLISHER_STATE,
        "automatic_retry_allowed": False,
        "fake_provider_read_count": 0,
        "fake_provider_write_count": 0,
        "fake_provider_publish_count": 0,
        "fake_telegram_send_count": 0,
        "gaps": [],
        "external_gates": {},
    }

    def finish(result: str, **extra) -> dict:
        duration_ms = int((time.monotonic() - started) * 1000)
        report.update(extra)
        report["recovery_result"] = result
        report["started_at"] = started_at
        report["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report["duration_ms"] = duration_ms
        root.mkdir(parents=True, exist_ok=True)
        (root / REPORT_FILENAME).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n")
        return report

    provider = FakeProviderTruth(provider_state)
    notifier = FakeNotifier()  # never sent in this drill

    # External gate: encrypted backup without key (truthful, no crypto theater).
    if require_decryption_key and not decryption_key_available:
        report["external_gates"]["encrypted_backup"] = {
            "present": True, "decryption_key_available": False,
            "backup_usable": False}
        report["gaps"].append("ENCRYPTED_BACKUP_UNUSABLE_WITHOUT_KEY")
        return finish("BLOCKED")

    manifest = load_snapshot_manifest(snapshot)

    # Stage: copy with per-file hash verification.
    staging = root / STAGING_DIRNAME
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for entry in manifest["files"]:
            rel = entry["path"]
            if ".." in rel.split("/") or rel.startswith("/"):
                raise DrillFailed(f"SNAPSHOT_PATH_TRAVERSAL: {rel!r}")
            _copy_verified(snapshot / rel, staging / rel, entry["sha256"])
    except DrillFailed as e:
        report["checksum_result"] = f"FAILED: {e}"
        report["gaps"].append(str(e))
        return finish("FAILED")

    if interrupt_at == "before_finalize":
        report["checksum_result"] = "STAGED_ONLY"
        report["gaps"].append("RESTORE_INTERRUPTED_BEFORE_FINALIZE")
        return finish("INTERRUPTED")

    # Validation phases over the staged tree (read-only).
    counts_ok, counts_gaps = check_record_counts(staging, manifest)
    report["checksum_result"] = "PASS"
    report["record_count_result"] = "PASS" if counts_ok else "FAILED"
    report["gaps"].extend(counts_gaps)
    if not counts_ok:
        return finish("FAILED")

    ref_ok, ref_gaps = check_referential(staging)
    report["referential_result"] = "PASS" if ref_ok else "FAILED"
    report["gaps"].extend(ref_gaps)
    if not ref_ok:
        return finish("FAILED")

    media_ok, media_gaps = check_media(staging)
    report["media_result"] = "PASS" if media_ok else "GAP"
    report["gaps"].extend(media_gaps)

    critical_status, critical_gaps = check_critical_history(staging)
    report["critical_history_result"] = critical_status
    report["gaps"].extend(critical_gaps)

    # Read-only provider truth (never a write).
    truth = provider.read_status("fixture-probe")
    report["provider_truth"] = truth
    report["fake_provider_read_count"] = provider.read_calls

    # Reconciliation semantics (ADR-01): forward-only on positive proof.
    reconciled = None
    if truth == "PUBLISHED":
        reconciled = {"post_id": "POST_fix001", "result": "PUBLISHED",
                      "provenance": "fake-provider-read-only-truth",
                      "retry_performed": False}
        (staging / "reconciled-result.json").write_text(
            json.dumps(reconciled, indent=2, sort_keys=True) + "\n")
        report["reconciled"] = reconciled
    elif truth == "DRAFT":
        report["gaps"].append(
            "REMOTE_DRAFT_PROVES_NOTHING: no retry inferred from DRAFT")

    # Notifier rule: missing/ambiguous history forbids auto-resend.
    notifier_dir = staging / "notifier"
    notifier_records = list(notifier_dir.rglob("*.json")) \
        if notifier_dir.is_dir() else []
    report["notifier_auto_resend_allowed"] = False
    if not notifier_records:
        report["gaps"].append("NOTIFIER_HISTORY_MISSING: auto-resend FORBIDDEN")

    if not remote_backup_available:
        report["external_gates"]["remote_backup"] = {"available": False}
        report["gaps"].append("REMOTE_BACKUP_OUTAGE: ordinary availability degraded")

    # Verdict: integrity failures already returned FAILED above.
    # Critical gaps and media gaps need operator review; everything else
    # restores successfully with publisher unconditionally disabled.
    if critical_status != "COMPLETE":
        result = "CHECK_REQUIRED"
    elif any(g.startswith("MEDIA_RECOVERY_GAP") for g in report["gaps"]):
        result = "CHECK_REQUIRED"
    else:
        result = "SUCCESS"

    # Finalize: move staged tree into place, mark READY only on success path.
    finalized = root / "restored"
    if finalized.exists():
        shutil.rmtree(finalized)
    shutil.move(str(staging), str(finalized))
    if result == "SUCCESS":
        (root / READY_MARKER).write_text(
            f"RESTORE_READY scenario={scenario}\n")
    report["fake_provider_write_count"] = provider.write_calls
    report["fake_provider_publish_count"] = provider.publish_calls
    report["fake_telegram_send_count"] = notifier.send_calls
    return finish(result)


def validate_restore_root(restore_root: str | Path) -> dict:
    """Re-validate a previous drill root. Incomplete roots are never READY."""
    root = Path(restore_root).resolve()
    report_path = root / REPORT_FILENAME
    if not report_path.is_file():
        return {"ready": False, "reason": "NO_REPORT"}
    try:
        report = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"ready": False, "reason": "REPORT_CORRUPT"}
    if not (root / READY_MARKER).is_file():
        return {"ready": False, "reason": "NOT_FINALIZED",
                "recovery_result": report.get("recovery_result")}
    if not (root / "restored").is_dir():
        return {"ready": False, "reason": "RESTORED_TREE_MISSING"}
    return {"ready": True, "recovery_result": report.get("recovery_result")}


def run_private_snapshot(*, snapshot_path=None, output_root=None,
                         operator_acknowledgement=False, **kwargs):
    """Gated future interface. NEVER runs without explicit operator ack."""
    if not operator_acknowledgement:
        raise DrillRefused(
            "PRIVATE_SNAPSHOT_AUTHORIZATION=REQUIRED_RAUF_ALIZADA: "
            "private snapshot validation requires separate explicit "
            "operator authorization; not exercised")
    raise DrillRefused(
        "PRIVATE_SNAPSHOT_NOT_IMPLEMENTED: interface gated, no execution path")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="restore_drill")
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--restore-root", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--provider-state", default="UNKNOWN",
                    choices=list(FakeProviderTruth.VALID))
    ap.add_argument("--interrupt-at", default=None)
    ap.add_argument("--require-decryption-key", action="store_true")
    ap.add_argument("--remote-backup-unavailable", action="store_true")
    args = ap.parse_args(argv)
    try:
        report = run_drill(
            snapshot_dir=args.snapshot,
            restore_root=args.restore_root,
            scenario=args.scenario,
            provider_state=args.provider_state,
            interrupt_at=args.interrupt_at,
            require_decryption_key=args.require_decryption_key,
            remote_backup_available=not args.remote_backup_unavailable)
    except (DrillRefused, DrillFailed) as e:
        print(f"DRILL_REFUSED_OR_FAILED: {e}", file=sys.stderr)
        return 2
    print(f"RECOVERY_RESULT={report['recovery_result']}")
    print(f"PUBLISHER_STATE={report['publisher_state']}")
    print(f"FAKE_PROVIDER_PUBLISH_COUNT={report['fake_provider_publish_count']}")
    print(f"DURATION_MS={report['duration_ms']}")
    return 0 if report["recovery_result"] == "SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
