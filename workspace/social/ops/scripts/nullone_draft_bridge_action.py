#!/usr/bin/env python3
"""Narrow credentialed Gateway action for draft-bridge Zernio creation (issue #142).

Exposes exactly one deterministic operation:

    nullone.draft-bridge.run {"manifest_id": "<production-manifest-stem>"}

This module is the action core. In production it runs as a Gateway-spawned
child (fixed argv, inherited Gateway process environment -- the only runtime
that carries the Zernio drafts credential), invoked through the
`plugins/nullone-draft-bridge/` handler after the existing ingress sender
authorization. There is deliberately no other entry surface: no cron job,
no agent turn, no model routing, no HTTP/RPC endpoint, no general exec.

Caller contract (strict, exact):

- the request is a dict with EXACTLY one key: `manifest_id`;
- any other key -- including caller-supplied path/URL/command/recipient/
  content/mode/publish/approval/schedule fields -- is rejected with zero
  external calls;
- the manifest path is derived INTERNALLY as
  `social/ops/manifests/<manifest-id>.json` and re-validated for
  containment / stem-ID binding by the reviewed bridge operation;
- the reviewed `nullone-draft-bridge.execute` is the ONLY domain call,
  made in-process. No subprocess, no shell, no model call.

Preconditions (all enforced, refusal = BLOCKED with zero Zernio calls):

- manifest loads and validates (`validate_manifest` inside the bridge);
- loaded `manifest_id` equals the requested stem (binding);
- `review.create_attempts == 0`, `review.state == "NOT_CREATED"`,
  no `review.zernio_draft_id`.

Exactly-once:

- sequential replay after DRAFT_CREATED returns the existing result
  (COMPLETED with the stored draft id) with zero external calls;
- two SIMULTANEOUS first invocations for the SAME manifest ID are
  serialized here: an in-process per-ID lock plus a cross-process
  lockfile (`social/ops/draft-bridge-action-locks/<manifest-id>.lock`,
  O_EXCL creation). A second concurrent caller fails closed with
  ACTION_BUSY and observes the first result via a later replay --
  exactly one Zernio creation maximum;
- a stale lock (dead owner pid, or age beyond STALE_LOCK_SECONDS) is
  reclaimed deterministically; anything else fails closed. Lock content
  is pid + timestamp only -- never secrets.

Audit: every handled request appends one JSON line to
`social/ops/draft-bridge-action-audit.jsonl` carrying only the
non-secret manifest ID, status, and reason code. Audit-write failure
fails closed.

Results carry only stable statuses plus the non-secret remote draft
identifier (explicit allowlist shaping). Telegram, approval, second
confirmation, scheduling, and publication modules are never imported
and are unreachable from this path, which terminates at Zernio draft
creation. Human approval and second Confirm Publish remain mandatory
downstream and are untouched.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import importlib.util
import io
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import nullone_bridge_common as bridge_common
from nullone_bridge_common import BridgeError, now_iso

ACTION_NAME = "nullone.draft-bridge.run"
ACTION_SCHEMA = "nullone.draft-bridge-action.v1"

MANIFESTS_RELATIVE_DIR = "social/ops/manifests"
LOCKS_RELATIVE_DIR = "social/ops/draft-bridge-action-locks"
AUDIT_RELATIVE_FILE = "social/ops/draft-bridge-action-audit.jsonl"
LOCK_SCHEMA = "nullone.draft-bridge-action-lock.v1"
AUDIT_SCHEMA = "nullone.draft-bridge-action-audit.v1"

BRIDGE_FILENAME = "nullone-draft-bridge.py"

# Bounded run time for one bridge execute (presign + upload + create +
# readback). A lock older than this is stale by definition: no legitimate
# first invocation is still running.
STALE_LOCK_SECONDS = 1800

# Production manifest stems: date/slug/hex shapes such as
# `2026-09-16-ai-access-equity-gates-pledge-2026-09-16`. Strict allowlist;
# the bridge re-validates containment and stem-ID binding.
MANIFEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

REASON_OK = "OK"
REASON_OK_REPLAY = "OK_REPLAY_EXISTING_DRAFT"
REASON_MALFORMED_REQUEST = "ACTION_MALFORMED_REQUEST"
REASON_MISSING_ID = "ACTION_MISSING_MANIFEST_ID"
REASON_BAD_ID = "ACTION_INVALID_MANIFEST_ID"
REASON_UNKNOWN_FIELD = "ACTION_UNKNOWN_FIELD"
REASON_FORBIDDEN_FIELD = "ACTION_FORBIDDEN_FIELD"
REASON_BUSY = "ACTION_BUSY"
REASON_AUDIT_FAILED = "ACTION_AUDIT_FAILED"
REASON_MANIFEST_UNREADABLE = "ACTION_MANIFEST_UNREADABLE"
REASON_MANIFEST_MISMATCH = "ACTION_MANIFEST_ID_MISMATCH"
REASON_ALREADY_CONSUMED = "ACTION_ALREADY_CONSUMED"
REASON_BRIDGE_BLOCKED = "ACTION_BRIDGE_BLOCKED"
REASON_BRIDGE_AMBIGUOUS = "ACTION_BRIDGE_AMBIGUOUS"

# Caller-controlled fields that must never exist on this action. The exact
# field-set rule below already rejects them; this set exists so audit
# reason codes distinguish a dangerous field from a merely unknown one.
FORBIDDEN_REQUEST_FIELDS = frozenset(
    {
        "manifest",
        "manifest_path",
        "manifest_rel",
        "path",
        "file",
        "url",
        "public_url",
        "command",
        "argv",
        "shell",
        "recipient",
        "target",
        "chat_id",
        "owner_id",
        "destination",
        "content",
        "text",
        "caption",
        "media",
        "mode",
        "publish",
        "approve",
        "reject",
        "revise",
        "second_confirm",
        "second-confirm",
        "schedule",
        "telegram",
        "token",
        "secret",
        "credential",
    }
)


class ActionError(RuntimeError):
    """Caller-contract violation (BLOCKED result, zero external calls)."""


# Test seam: production always lazy-loads the reviewed bridge file from
# this module's own directory. Offline tests inject a fake module object
# here; no test hook exists on any production path.
_BRIDGE_OVERRIDE: Any = None


def _bridge_module() -> Any:
    if _BRIDGE_OVERRIDE is not None:
        return _BRIDGE_OVERRIDE
    path = Path(__file__).with_name(BRIDGE_FILENAME)
    spec = importlib.util.spec_from_file_location(
        "nullone_draft_bridge_action_loaded", path
    )
    if spec is None or spec.loader is None:
        raise ActionError(REASON_BRIDGE_BLOCKED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workspace_root(workspace_root: Path | str | None) -> Path:
    if workspace_root is not None:
        return Path(workspace_root)
    return Path(bridge_common.WORKSPACE)


def _completed(manifest_id: str, draft_id: str, reason: str) -> dict[str, Any]:
    return {
        "status": "COMPLETED",
        "reason_code": reason,
        "manifest_id": manifest_id,
        "zernio": {"created": True, "draft_id": draft_id},
    }


def _blocked(reason: str, manifest_id: str | None = None) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason_code": reason,
        "reason_text": "Draft-bridge action precondition failed; no external call made.",
        "manifest_id": manifest_id,
        "zernio": {"created": False, "draft_id": None},
    }


def _validate_request(request: Any) -> str:
    """Enforce the exact `{"manifest_id": ...}` schema.

    Returns the validated manifest ID. Raises `ActionError` for any
    violation. Caller input is never echoed into reason text.
    """
    if not isinstance(request, dict):
        raise ActionError(REASON_MALFORMED_REQUEST)
    keys = set(request)
    if keys != {"manifest_id"}:
        if "manifest_id" not in keys:
            raise ActionError(REASON_MISSING_ID)
        extras = keys - {"manifest_id"}
        if extras & FORBIDDEN_REQUEST_FIELDS:
            raise ActionError(REASON_FORBIDDEN_FIELD)
        raise ActionError(REASON_UNKNOWN_FIELD)
    manifest_id = request["manifest_id"]
    if not isinstance(manifest_id, str) or not MANIFEST_ID_RE.fullmatch(manifest_id):
        raise ActionError(REASON_BAD_ID)
    return manifest_id


def _manifest_rel(manifest_id: str) -> str:
    """Derive the manifest reference internally. No caller path input."""
    return f"{MANIFESTS_RELATIVE_DIR}/{manifest_id}.json"


def _locks_dir(workspace_root: Path) -> Path:
    return workspace_root.resolve() / LOCKS_RELATIVE_DIR


def _lock_file(workspace_root: Path, manifest_id: str) -> Path:
    return _locks_dir(workspace_root) / f"{manifest_id}.lock"


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_age_seconds(lock_path: Path) -> float | None:
    try:
        started = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(started, dict) or started.get("schema") != LOCK_SCHEMA:
        return None
    try:
        then = time.mktime(time.strptime(started.get("started_at", ""), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        try:
            return time.time() - lock_path.stat().st_mtime
        except OSError:
            return None
    return time.time() - then


def _try_reclaim(lock_path: Path) -> bool:
    """Deterministic stale-lock recovery. Returns True if reclaimed."""
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(data, dict) or data.get("schema") != LOCK_SCHEMA:
        return False
    pid = data.get("pid")
    age = _lock_age_seconds(lock_path)
    stale_by_age = age is not None and age > STALE_LOCK_SECONDS
    if _pid_alive(pid) and not stale_by_age:
        return False
    try:
        lock_path.unlink()
    except OSError:
        return False
    return True


def _acquire_lock(lock_path: Path, manifest_id: str) -> bool:
    """Atomic cross-process acquire. True if this caller owns the lock."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    doc = {
        "schema": LOCK_SCHEMA,
        "manifest_id": manifest_id,
        "pid": os.getpid(),
        "started_at": now_iso(),
    }
    payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            return False
        if not _try_reclaim(lock_path):
            return False
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return False
    try:
        os.write(fd, payload)
    except OSError:
        try:
            os.close(fd)
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass
        return False
    os.close(fd)
    return True


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _process_lock(manifest_id: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(manifest_id)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[manifest_id] = lock
        return lock


def _audit_file(workspace_root: Path) -> Path:
    return workspace_root.resolve() / AUDIT_RELATIVE_FILE


def _audit(workspace_root: Path, *, manifest_id: str, status: str, reason_code: str) -> bool:
    """Append one non-secret audit line. Returns False on any failure."""
    doc = {
        "schema": AUDIT_SCHEMA,
        "action": ACTION_NAME,
        "manifest_id": manifest_id,
        "status": status,
        "reason_code": reason_code,
        "at": now_iso(),
    }
    line = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    path = _audit_file(workspace_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        return False
    return True


def _read_manifest_state(
    workspace_root: Path, manifest_id: str
) -> tuple[str | None, int | None, str | None]:
    """Read-only review triple (state, create_attempts, draft_id).

    Returns (None, None, None) when the manifest is missing or invalid.
    Never raises; never mutates.
    """
    try:
        _, m = bridge_common.load_manifest(
            workspace_root / _manifest_rel(manifest_id)
        )
    except (BridgeError, OSError, ValueError):
        return (None, None, None)
    if not isinstance(m, dict) or m.get("manifest_id") != manifest_id:
        return (None, None, None)
    review = m.get("review")
    if not isinstance(review, dict):
        return (None, None, None)
    attempts = review.get("create_attempts")
    draft_id = review.get("zernio_draft_id")
    return (
        review.get("state"),
        attempts if isinstance(attempts, int) else None,
        draft_id if isinstance(draft_id, str) and draft_id else None,
    )


def _preconditions_hold(
    workspace_root: Path, manifest_id: str
) -> tuple[bool, str]:
    """Explicit pre-flight for contract section 6.

    Returns (True, OK) only when the manifest validates, binds to the
    requested stem, and permits creation (attempts == 0, NOT_CREATED,
    no draft id). Anything else is a named refusal with zero Zernio
    calls.
    """
    try:
        _, m = bridge_common.load_manifest(
            workspace_root / _manifest_rel(manifest_id)
        )
    except (BridgeError, OSError, ValueError):
        return (False, REASON_MANIFEST_UNREADABLE)
    if not isinstance(m, dict) or m.get("manifest_id") != manifest_id:
        return (False, REASON_MANIFEST_MISMATCH)
    review = m.get("review")
    if not isinstance(review, dict):
        return (False, REASON_MANIFEST_UNREADABLE)
    if (
        review.get("create_attempts") != 0
        or review.get("state") != "NOT_CREATED"
        or review.get("zernio_draft_id")
    ):
        return (False, REASON_ALREADY_CONSUMED)
    return (True, REASON_OK)


def handle_request(
    request: Any, *, workspace_root: Path | str | None = None
) -> dict[str, Any]:
    """Handle one `nullone.draft-bridge.run` request deterministically.

    Returns an allowlist-shaped result dict (status COMPLETED/BLOCKED,
    reason_code, manifest_id, zernio.created/draft_id). Exactly one
    Zernio creation maximum per manifest ID across sequential and
    concurrent callers. Never raises for domain outcomes; raises
    ActionError only for caller-contract violations handled by the
    caller into BLOCKED.
    """
    root = _workspace_root(workspace_root).resolve()
    manifest_id = _validate_request(request)

    # Sequential idempotent replay: an already-created draft returns the
    # existing result with zero external calls (no lock needed).
    state, _attempts, draft_id = _read_manifest_state(root, manifest_id)
    if state == "DRAFT_CREATED" and draft_id:
        result = _completed(manifest_id, draft_id, REASON_OK_REPLAY)
        if not _audit(root, manifest_id=manifest_id, status="COMPLETED", reason_code=REASON_OK_REPLAY):
            return _blocked(REASON_AUDIT_FAILED, manifest_id)
        return result

    with _process_lock(manifest_id):
        lock_path = _lock_file(root, manifest_id)
        if not _acquire_lock(lock_path, manifest_id):
            result = _blocked(REASON_BUSY, manifest_id)
            _audit(root, manifest_id=manifest_id, status="BLOCKED", reason_code=REASON_BUSY)
            return result
        try:
            # Re-read under the lock: a concurrent winner may have
            # completed between our replay check and lock acquisition.
            state, _attempts, draft_id = _read_manifest_state(root, manifest_id)
            if state == "DRAFT_CREATED" and draft_id:
                result = _completed(manifest_id, draft_id, REASON_OK_REPLAY)
                if not _audit(root, manifest_id=manifest_id, status="COMPLETED", reason_code=REASON_OK_REPLAY):
                    return _blocked(REASON_AUDIT_FAILED, manifest_id)
                return result

            ok, reason = _preconditions_hold(root, manifest_id)
            if not ok:
                result = _blocked(reason, manifest_id)
                if not _audit(root, manifest_id=manifest_id, status="BLOCKED", reason_code=reason):
                    return _blocked(REASON_AUDIT_FAILED, manifest_id)
                return result

            bridge = _bridge_module()
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    exit_code = bridge.execute(_manifest_rel(manifest_id))
            except (BridgeError, OSError, ValueError, RuntimeError):
                # Pre-provider gates raise here (canonical path, shape,
                # authority, credential): zero Zernio calls by construction.
                result = _blocked(REASON_BRIDGE_BLOCKED, manifest_id)
                if not _audit(root, manifest_id=manifest_id, status="BLOCKED", reason_code=REASON_BRIDGE_BLOCKED):
                    return _blocked(REASON_AUDIT_FAILED, manifest_id)
                return result

            # Ground truth re-read: only a reloaded DRAFT_CREATED with a
            # draft id counts as creation. Anything else (including an
            # ambiguous provider outcome) is BLOCKED, never retried here.
            state, _attempts, draft_id = _read_manifest_state(root, manifest_id)
            if exit_code == 0 and state == "DRAFT_CREATED" and draft_id:
                result = _completed(manifest_id, draft_id, REASON_OK)
            elif state in ("REVIEW_UNKNOWN", "REVIEW_DRAFT_AMBIGUOUS"):
                result = _blocked(REASON_BRIDGE_AMBIGUOUS, manifest_id)
            else:
                result = _blocked(REASON_BRIDGE_BLOCKED, manifest_id)
            if not _audit(
                root,
                manifest_id=manifest_id,
                status=result["status"],
                reason_code=result["reason_code"],
            ):
                return _blocked(REASON_AUDIT_FAILED, manifest_id)
            return result
        finally:
            _release_lock(lock_path)


def self_test() -> None:
    """Offline deterministic self-test (no network, no model, no Zernio)."""
    manifest_id = "2026-09-16-self-test-manifest-2026-09-16"

    import tempfile

    if True:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / MANIFESTS_RELATIVE_DIR).mkdir(parents=True)
            # Validate against the temp root (same patch pattern the
            # offline test suite uses for bridge_common.WORKSPACE).
            previous_workspace = bridge_common.WORKSPACE
            bridge_common.WORKSPACE = root
            from PIL import Image

            caption = root / "caption.txt"
            caption.write_text("NullOne self-test caption", encoding="utf-8")
            import hashlib

            caption_sha = hashlib.sha256(caption.read_bytes()).hexdigest()
            png = root / "feed.png"
            Image.new("RGB", (1080, 1350), (1, 2, 3)).save(png, "PNG")
            png_sha = hashlib.sha256(png.read_bytes()).hexdigest()
            manifest = {
                "schema": "nullone.production.v1",
                "manifest_id": manifest_id,
                "created_at": "2026-09-16T10:44:10+00:00",
                "candidate_id": "self-test-2026-09-16",
                "topic": "Self-test",
                "topic_cluster": "self-test",
                "content_type": "NEWS",
                "format": "FEED",
                "verification": "PASS",
                "account_id": "6a982bbf77555aae01c28f21",
                "caption": {"file": "caption.txt", "sha256": caption_sha},
                "media": [
                    {
                        "local_path": "feed.png",
                        "sha256": png_sha,
                        "content_type": "image/png",
                        "width": 1080,
                        "height": 1350,
                    }
                ],
                "review": {
                    "create_attempts": 0,
                    "state": "NOT_CREATED",
                    "zernio_draft_id": None,
                },
                "approval": {"first_stage": False, "final_publish": False},
                "publication": {"state": "NOT_REQUESTED", "attempts": 0},
            }
            (root / _manifest_rel(manifest_id)).write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            # Malformed callers fail closed with zero bridge calls.
            for bad in (
                {},
                {"manifest_id": manifest_id, "command": "x"},
                {"manifest_id": manifest_id, "token": "x"},
                {"manifest_id": "../escape"},
                {"manifest_id": ""},
                "not-a-dict",
            ):
                try:
                    _validate_request(bad)
                except ActionError:
                    pass
                else:
                    raise AssertionError(f"bad request accepted: {bad!r}")

            # Preconditions refuse a consumed manifest before any call.
            manifest["review"]["create_attempts"] = 1
            (root / _manifest_rel(manifest_id)).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            ok, reason = _preconditions_hold(root, manifest_id)
            assert ok is False and reason == REASON_ALREADY_CONSUMED, (ok, reason)
            manifest["review"]["create_attempts"] = 0
            (root / _manifest_rel(manifest_id)).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            ok, reason = _preconditions_hold(root, manifest_id)
            assert ok is True, (ok, reason)
            bridge_common.WORKSPACE = previous_workspace

    print("DRAFT_BRIDGE_ACTION_SELF_TEST=PASS")


def main(argv: list[str] | None = None) -> int:
    """CLI: the fixed production argv (mirrored by the plugin spawn).

    `python3 nullone_draft_bridge_action.py handle --manifest-id <id>`
    prints machine-shaped ACTION_STATUS/REASON_CODE/DRAFT_ID lines for
    the plugin outcome mapping. Exit 0 on COMPLETED, 1 on
    BLOCKED/BUSY, 2 on contract or audit failure.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    p_handle = sub.add_parser("handle")
    p_handle.add_argument("--manifest-id", required=True)
    args = parser.parse_args(argv)

    if args.command == "self-test":
        self_test()
        return 0

    try:
        result = handle_request({"manifest_id": args.manifest_id})
    except ActionError as exc:
        print("ACTION_STATUS=BLOCKED")
        print(f"REASON_CODE={exc}")
        print("DRAFT_ID=")
        return 2
    print(f"ACTION_STATUS={result['status']}")
    print(f"REASON_CODE={result['reason_code']}")
    print(f"DRAFT_ID={result['zernio']['draft_id'] or ''}")
    if result["status"] == "COMPLETED":
        return 0
    if result["reason_code"] == REASON_AUDIT_FAILED:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
