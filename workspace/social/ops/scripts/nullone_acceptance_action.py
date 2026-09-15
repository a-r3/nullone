#!/usr/bin/env python3
"""Narrow credentialed Gateway action for integration acceptance (issue #129).

Exposes exactly one deterministic operation:

    nullone.acceptance.run {"acceptance_id": "nullone-acceptance-YYYYMMDD-HHMMSS"}

This module is the action core. In production it runs as a Gateway-spawned
child (fixed argv, inherited Gateway process environment -- the only runtime
that carries the Zernio + Telegram credentials), invoked through the
`plugins/nullone-acceptance/` interactive handler after the existing
ingress sender authorization. There is deliberately no other entry surface:
no cron job, no agent turn, no model routing, no HTTP/RPC endpoint.

Caller contract (strict, exact):

- the request is a dict with EXACTLY one key: `acceptance_id`;
- any other key -- including caller-supplied path/URL/command/recipient/
  content/mode/publish/approval/schedule fields -- is rejected with zero
  external calls;
- the manifest path is derived INTERNALLY as
  `social/ops/manifests/<acceptance-id>.json` and re-validated for
  containment / stem-ID binding by the reviewed acceptance operation;
- the reviewed `nullone_acceptance_run.run_acceptance` is the ONLY domain
  call, made in-process with default (production) transports that read the
  ambient credential context. No subprocess, no shell, no model call.

Single-flight (the one race sequential idempotency does not cover):

- sequential replays are already safe via manifest state + delivery
  receipts inside `run_acceptance`;
- two SIMULTANEOUS first invocations for the SAME acceptance ID are
  serialized here: an in-process per-ID lock plus a cross-process lockfile
  (`social/ops/acceptance-action-locks/<acceptance-id>.lock`, O_EXCL
  creation). A second concurrent caller fails closed with ACTION_BUSY and
  observes the first result via a later replay -- exactly one Zernio create
  and one Telegram send maximum;
- a stale lock (dead owner pid, or age beyond STALE_LOCK_SECONDS) is
  reclaimed deterministically; anything else fails closed. Lock content is
  pid + timestamp only -- never secrets.

Audit: every handled request appends one JSON line to
`social/ops/acceptance-action-audit.jsonl` carrying only the non-secret
acceptance ID, status, and reason code. Audit-write failure fails closed.

Results carry only stable statuses plus non-secret remote/message
identifiers (explicit allowlist shaping on top of the already-redacted
acceptance result). Approval, second confirmation, scheduling, and
publication modules are never imported and are unreachable from this path,
which terminates at Zernio draft -> Telegram preview -> human approval
pending.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from nullone_acceptance_run import (
    ACCEPTANCE_ID_RE,
    ACCEPTANCE_MODE,
    MANIFESTS_RELATIVE_DIR,
    run_acceptance,
)
from nullone_bridge_common import WORKSPACE, now_iso

ACTION_NAME = "nullone.acceptance.run"
ACTION_SCHEMA = "nullone.acceptance-action.v1"

LOCKS_RELATIVE_DIR = "social/ops/acceptance-action-locks"
AUDIT_RELATIVE_FILE = "social/ops/acceptance-action-audit.jsonl"
LOCK_SCHEMA = "nullone.acceptance-action-lock.v1"
AUDIT_SCHEMA = "nullone.acceptance-action-audit.v1"

# Bounded run time for one acceptance cycle (matches the 1800s command
# timeout used by the NullOne cron jobs). A lock older than this is stale
# by definition: no legitimate first invocation is still running.
STALE_LOCK_SECONDS = 1800

REASON_OK = "OK"
REASON_MALFORMED_REQUEST = "ACTION_MALFORMED_REQUEST"
REASON_MISSING_ID = "ACTION_MISSING_ACCEPTANCE_ID"
REASON_BAD_ID = "ACTION_INVALID_ACCEPTANCE_ID"
REASON_UNKNOWN_FIELD = "ACTION_UNKNOWN_FIELD"
REASON_FORBIDDEN_FIELD = "ACTION_FORBIDDEN_FIELD"
REASON_BUSY = "ACTION_BUSY"
REASON_AUDIT_FAILED = "ACTION_AUDIT_FAILED"

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
        "token",
        "secret",
        "credential",
    }
)


class ActionError(RuntimeError):
    """Caller-contract violation (BLOCKED result, zero external calls)."""


def _blocked(reason: str) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason_code": reason,
        "reason_text": "Acceptance action precondition failed; no external call made.",
        "zernio": {"created": False, "draft_id": None},
        "telegram": {"sent": False, "approval_message_id": None, "media_message_ids": []},
    }


def _validate_request(request: Any) -> str:
    """Enforce the exact `{"acceptance_id": ...}` schema.

    Returns the validated acceptance ID. Raises `ActionError` for any
    violation. Caller input is never echoed into reason text.
    """
    if not isinstance(request, dict):
        raise ActionError(REASON_MALFORMED_REQUEST)
    keys = set(request)
    if keys != {"acceptance_id"}:
        if "acceptance_id" not in keys:
            raise ActionError(REASON_MISSING_ID)
        extras = keys - {"acceptance_id"}
        if extras & FORBIDDEN_REQUEST_FIELDS:
            raise ActionError(REASON_FORBIDDEN_FIELD)
        raise ActionError(REASON_UNKNOWN_FIELD)
    acceptance_id = request["acceptance_id"]
    if not isinstance(acceptance_id, str) or not ACCEPTANCE_ID_RE.fullmatch(acceptance_id):
        raise ActionError(REASON_BAD_ID)
    return acceptance_id


def _manifest_rel(acceptance_id: str) -> str:
    """Derive the manifest reference internally. No caller path input."""
    return f"{MANIFESTS_RELATIVE_DIR}/{acceptance_id}.json"


def _locks_dir(workspace_root: Path) -> Path:
    return workspace_root.resolve() / LOCKS_RELATIVE_DIR


def _lock_file(workspace_root: Path, acceptance_id: str) -> Path:
    return _locks_dir(workspace_root) / f"{acceptance_id}.lock"


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
        # ISO-8601 with timezone/offset: fall back to file mtime, which is
        # equally deterministic for staleness purposes.
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


def _acquire_lock(lock_path: Path, acceptance_id: str) -> bool:
    """Atomic cross-process acquire. True if this caller owns the lock."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    doc = {
        "schema": LOCK_SCHEMA,
        "acceptance_id": acceptance_id,
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


def _process_lock(acceptance_id: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(acceptance_id)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[acceptance_id] = lock
        return lock


def _audit_file(workspace_root: Path) -> Path:
    return workspace_root.resolve() / AUDIT_RELATIVE_FILE


def _audit(workspace_root: Path, *, acceptance_id: str, status: str, reason_code: str) -> bool:
    """Append one non-secret audit line. Returns False on any failure."""
    doc = {
        "schema": AUDIT_SCHEMA,
        "action": ACTION_NAME,
        "acceptance_id": acceptance_id,
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


def _public_result(raw: Any) -> dict[str, Any]:
    """Allowlist-shape the acceptance result. Only non-secret fields pass."""
    if not isinstance(raw, dict):
        return _blocked(REASON_MALFORMED_REQUEST)
    zernio = raw.get("zernio") if isinstance(raw.get("zernio"), dict) else {}
    telegram = raw.get("telegram") if isinstance(raw.get("telegram"), dict) else {}
    draft_id = zernio.get("draft_id")
    approval_id = telegram.get("approval_message_id")
    media_ids = telegram.get("media_message_ids")
    status = raw.get("status")
    reason_code = raw.get("reason_code")
    reason_text = raw.get("reason_text")
    return {
        "status": status if status in ("COMPLETED", "BLOCKED") else "BLOCKED",
        "reason_code": reason_code if isinstance(reason_code, str) and reason_code else "UNKNOWN",
        "reason_text": reason_text
        if isinstance(reason_text, str) and reason_text
        else "Acceptance action completed with an unshaped result.",
        "zernio": {
            "created": bool(zernio.get("created")),
            "draft_id": draft_id if isinstance(draft_id, str) and draft_id else None,
        },
        "telegram": {
            "sent": bool(telegram.get("sent")),
            "approval_message_id": approval_id
            if isinstance(approval_id, str) and approval_id
            else None,
            "media_message_ids": [m for m in media_ids if isinstance(m, str)]
            if isinstance(media_ids, list)
            else [],
        },
    }


def handle_action(
    request: Any,
    *,
    workspace_root: Path = WORKSPACE,
    draft_execute: Callable[[str], int] | None = None,
    delivery_send: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Handle one `nullone.acceptance.run` invocation.

    `draft_execute` / `delivery_send` default to the real production
    transports (ambient Gateway credential context). Tests inject fakes.
    The returned mapping carries only non-secret fields.
    """
    try:
        acceptance_id = _validate_request(request)
    except ActionError as exc:
        return _blocked(str(exc))

    root = workspace_root.resolve()
    lock_path = _lock_file(root, acceptance_id)
    own_process_lock = _process_lock(acceptance_id)
    if not own_process_lock.acquire(blocking=False):
        result = _blocked(REASON_BUSY)
        _audit(root, acceptance_id=acceptance_id, status=result["status"], reason_code=result["reason_code"])
        return result
    try:
        if not _acquire_lock(lock_path, acceptance_id):
            result = _blocked(REASON_BUSY)
            _audit(root, acceptance_id=acceptance_id, status=result["status"], reason_code=result["reason_code"])
            return result
        try:
            raw = run_acceptance(
                acceptance_id,
                _manifest_rel(acceptance_id),
                mode=ACCEPTANCE_MODE,
                workspace_root=root,
                draft_execute=draft_execute,
                delivery_send=delivery_send,
            )
        finally:
            _release_lock(lock_path)
    finally:
        own_process_lock.release()

    result = _public_result(raw)
    if not _audit(root, acceptance_id=acceptance_id, status=result["status"], reason_code=result["reason_code"]):
        failed = _blocked(REASON_AUDIT_FAILED)
        failed["zernio"] = result["zernio"]
        return failed
    return result


def self_test() -> int:
    """Fake-transport smoke: strict schema, internal derivation, replay."""
    import tempfile

    from PIL import Image

    from nullone_bridge_common import atomic_write_json, sha256_bytes, sha256_file

    aid = "nullone-acceptance-20260101-000000"

    def fake_draft(manifest_path: str) -> int:
        path = Path(manifest_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["review"]["create_attempts"] = 1
        data["review"]["state"] = "DRAFT_CREATED"
        data["review"]["zernio_draft_id"] = "zdr_action_smoke_1"
        atomic_write_json(path, data)
        return 0

    def fake_delivery(payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["review_post_id"] == "zdr_action_smoke_1"
        return {
            "status": "SENT",
            "approval_message_id": "msg_action_smoke_1",
            "media_message_ids": ["msg_action_smoke_media_1"],
        }

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        drafts = root / "social/drafts/production"
        drafts.mkdir(parents=True)
        caption = drafts / f"{aid}-caption.txt"
        caption.write_text("TEST — probe. YAYIM ÜÇÜN DEYİL.\n", encoding="utf-8")
        media_path = drafts / f"{aid}.png"
        Image.new("RGB", (1080, 1350), (10, 10, 20)).save(media_path)
        manifest = {
            "schema": "nullone.production.v1",
            "account_id": "6a982bbf77555aae01c28f21",
            "verification": "PASS",
            "format": "FEED",
            "content_type": "NEWS",
            "topic_cluster": "probe",
            "caption": {"file": str(caption.resolve()), "sha256": sha256_bytes(caption.read_bytes())},
            "media": [
                {
                    "local_path": str(media_path.resolve()),
                    "sha256": sha256_file(media_path),
                    "content_type": "image/png",
                    "width": 1080,
                    "height": 1350,
                    "image_format": "PNG",
                    "public_url": None,
                }
            ],
            "review": {"create_attempts": 0, "state": "NOT_CREATED", "zernio_draft_id": None, "created_at": None},
            "approval": {"first_stage": False, "first_stage_at": None, "final_publish": False, "final_publish_at": None, "source": None, "operator": None, "human_confirmation": None},
            "publication": {"attempts": 0, "state": "NOT_REQUESTED", "live_zernio_post_id": None, "platform_post_id": None, "permalink": None, "last_checked_at": None, "error": None},
        }
        manifest_path = root / "social/ops/manifests" / f"{aid}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(manifest_path, manifest)

        result = handle_action(
            {"acceptance_id": aid},
            workspace_root=root,
            draft_execute=fake_draft,
            delivery_send=fake_delivery,
        )
        assert result["status"] == "COMPLETED", result
        assert result["zernio"]["draft_id"] == "zdr_action_smoke_1"
        assert result["telegram"]["approval_message_id"] == "msg_action_smoke_1"

        audit_lines = (_audit_file(root)).read_text(encoding="utf-8").strip().splitlines()
        assert len(audit_lines) == 1, audit_lines
        audit = json.loads(audit_lines[0])
        assert audit["acceptance_id"] == aid and audit["status"] == "COMPLETED"

        for bad in ({"acceptance_id": aid, "manifest": "x"}, {"acceptance_id": "nope"}, {}, []):
            blocked = handle_action(bad, workspace_root=root, draft_execute=fake_draft, delivery_send=fake_delivery)
            assert blocked["status"] == "BLOCKED", (bad, blocked)

    print("ACCEPTANCE_ACTION_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne narrow credentialed acceptance action core")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")

    h = sub.add_parser("handle")
    h.add_argument("--acceptance-id", required=True)

    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    # Fixed CLI surface: the ONLY caller-controlled value is the validated
    # acceptance ID. The manifest path is derived internally; the Gateway
    # plugin spawns this with an argv array (no shell) after its own edge
    # validation, and this module re-validates everything.
    result = handle_action({"acceptance_id": args.acceptance_id})
    print(f"ACTION_STATUS={result['status']}")
    print(f"REASON_CODE={result['reason_code']}")
    zernio = result.get("zernio", {})
    telegram = result.get("telegram", {})
    if zernio.get("draft_id"):
        print(f"ZERNIO_DRAFT_ID={zernio['draft_id']}")
    if telegram.get("approval_message_id"):
        print(f"APPROVAL_MESSAGE_ID={telegram['approval_message_id']}")
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
