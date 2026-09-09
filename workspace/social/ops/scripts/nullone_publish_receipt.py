#!/usr/bin/env python3
"""Durable per-human-authorization-instance receipts for the #89 handoff.

A receipt represents ONE human authorization instance, NOT a publication
object: the instance id is a UUIDv5 digest over the canonical stable tuple
(schema tag, Telegram account identity, conversation/chat identity, callback
message identity, lower-cased review POST_ID). Raw Telegram authority fields
are NEVER persisted — only the digest plus non-sensitive publication identity.

Consequences (all enforced by callers under review_post_lock):

- same second-stage message redelivered / double-clicked / replayed after a
  Gateway restart  ->  SAME instance (same message id);
- a NEW second-stage confirmation message for the same POST_ID  ->  NEW
  instance (new message id), so a genuine fresh human confirmation after a
  safe BLOCKED-before-attempt outcome remains possible while replays of the
  old instance stay terminal.

Receipt states: RECEIVED -> EXECUTING -> SETTLED:<outcome> | REJECTED:* |
ABANDONED. Terminal states never execute. All writes are atomic; receipt
directories reject symlinks; paths are workspace-contained.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from nullone_bridge_common import (
    BridgeError,
    atomic_write_json,
    now_iso,
)

RECEIPT_SCHEMA = "nullone.publish-callback-receipt.v1"

# UUIDv5 namespace for authorization instances (fixed, reviewed constant).
_AUTH_NAMESPACE = uuid.UUID("9b3f2f2e-7f2c-5d1a-9c4e-6b0a1d2c3e4f")

# Terminal receipt states: the instance is dead and must never execute.
TERMINAL_STATES = frozenset(
    {
        "SETTLED_PUBLISHED",
        "SETTLED_PUBLISHING",
        "SETTLED_FAILED",
        "SETTLED_UNKNOWN",
        "SETTLED_CHECK_REQUIRED",
        "SETTLED_READBACK_FAILED",
        "SETTLED_BLOCKED",
        "REJECTED_MALFORMED",
        "REJECTED_UNAUTHORIZED",
        "ABANDONED",
    }
)

_NONTERMINAL_STATES = frozenset({"RECEIVED", "EXECUTING"})

_ALL_STATES = TERMINAL_STATES | _NONTERMINAL_STATES

_POST_ID_CHARS = frozenset("0123456789abcdefABCDEF")


def validate_post_id(review_post_id: Any) -> str:
    if (
        not isinstance(review_post_id, str)
        or len(review_post_id) != 24
        or any(c not in _POST_ID_CHARS for c in review_post_id)
    ):
        raise BridgeError("Invalid Zernio review post ID format")
    return review_post_id.lower()


def derive_authorization_instance_id(
    account_id: str,
    chat_id: str,
    message_id: str,
    review_post_id: str,
) -> str:
    """Deterministic non-reversible instance id (UUIDv5 hex).

    All inputs are bounded non-empty strings; raw values are used only
    in memory by the caller and never leave this function except inside the
    one-way digest.
    """
    for value in (account_id, chat_id, message_id):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise BridgeError("Authorization tuple field invalid")
    post = validate_post_id(review_post_id)
    name = "|".join(
        [
            "nullone.final-publish-auth.v1",
            account_id,
            chat_id,
            message_id,
            post,
        ]
    )
    return uuid.uuid5(_AUTH_NAMESPACE, name).hex


def receipts_root(workspace: Path) -> Path:
    return Path(workspace) / "social" / "ops" / "publish-callback-receipts"


def _ensure_safe_dir(path: Path) -> Path:
    """Create dir tree while rejecting symlink components (lstat-based).

    Walks the UNRESOLVED path so a symlinked component is observed itself
    rather than resolved away.
    """
    absolute = path if path.is_absolute() else Path(os.getcwd()) / path
    current = Path(absolute.anchor)
    for part in absolute.relative_to(absolute.anchor).parts:
        current = current / part
        if os.path.islink(current):
            raise BridgeError(f"Refusing symlinked receipt path: {current}")
        current.mkdir(exist_ok=True)
    return path


def receipt_path(workspace: Path, review_post_id: str, instance_id: str) -> Path:
    post = validate_post_id(review_post_id)
    if (
        not isinstance(instance_id, str)
        or len(instance_id) != 32
        or any(c not in "0123456789abcdef" for c in instance_id)
    ):
        raise BridgeError("Invalid authorization instance id")
    base = receipts_root(Path(workspace))
    post_dir = base / post
    _ensure_safe_dir(post_dir)
    candidate = post_dir / f"{instance_id}.json"
    # Containment: resolved path must stay inside the receipts root.
    try:
        relative = Path(os.path.realpath(candidate)).relative_to(
            Path(os.path.realpath(base))
        )
    except ValueError:
        raise BridgeError("Receipt path escapes receipts root")
    if relative.parts[:1] == ("..",):
        raise BridgeError("Receipt path escapes receipts root")
    return candidate


def validate_receipt(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise BridgeError("Receipt is not an object")
    if record.get("schema") != RECEIPT_SCHEMA:
        raise BridgeError("Receipt schema mismatch")
    if record.get("state") not in _ALL_STATES:
        raise BridgeError("Receipt state unknown")
    validate_post_id(record.get("review_post_id"))
    instance = record.get("authorization_instance_id")
    if (
        not isinstance(instance, str)
        or len(instance) != 32
        or any(c not in "0123456789abcdef" for c in instance)
    ):
        raise BridgeError("Receipt instance id invalid")
    fingerprint = record.get("pre_fingerprint")
    if not isinstance(fingerprint, dict):
        raise BridgeError("Receipt fingerprint missing")
    return dict(record)


def new_receipt(
    review_post_id: str,
    instance_id: str,
    pre_fingerprint: dict[str, Any],
    nonce_sha256: str,
) -> dict[str, Any]:
    if (
        not isinstance(nonce_sha256, str)
        or len(nonce_sha256) != 64
        or any(c not in "0123456789abcdef" for c in nonce_sha256)
    ):
        raise BridgeError("Receipt nonce binding invalid")
    now = now_iso()
    return {
        "schema": RECEIPT_SCHEMA,
        "authorization_instance_id": instance_id,
        "review_post_id": validate_post_id(review_post_id),
        "state": "RECEIVED",
        "created_at": now,
        "updated_at": now,
        "pre_fingerprint": {
            "attempts": int(pre_fingerprint.get("attempts", 0)),
            "publication_state": str(pre_fingerprint.get("publication_state", "")),
            "final_publish": bool(pre_fingerprint.get("final_publish", False)),
        },
        "nonce_sha256": nonce_sha256,
        "result": None,
    }


def claim_receipt(
    workspace: Path,
    review_post_id: str,
    instance_id: str,
    pre_fingerprint: dict[str, Any],
    nonce_sha256: str,
) -> tuple[dict[str, Any], bool]:
    """Atomically claim a receipt (O_EXCL create).

    Returns (record, created). created=True means this caller owns the new
    RECEIVED instance; created=False means a receipt already exists and the
    caller MUST honor it (terminal or recovery rules) instead of executing.
    """
    path = receipt_path(workspace, review_post_id, instance_id)
    record = new_receipt(review_post_id, instance_id, pre_fingerprint, nonce_sha256)
    encoded = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = validate_receipt(
            json.loads(path.read_text(encoding="utf-8"))
        )
        return existing, False
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)
    return record, True


def transition_receipt(
    workspace: Path,
    review_post_id: str,
    instance_id: str,
    state: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically move a receipt to a new state (validated)."""
    if state not in _ALL_STATES:
        raise BridgeError("Receipt transition to unknown state")
    path = receipt_path(workspace, review_post_id, instance_id)
    try:
        record = validate_receipt(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise BridgeError("Receipt missing for transition")
    if record["state"] in TERMINAL_STATES:
        raise BridgeError("Receipt already terminal")
    record["state"] = state
    record["updated_at"] = now_iso()
    if result is not None:
        record["result"] = dict(result)
    atomic_write_json(path, record)
    return record


def read_receipt(
    workspace: Path, review_post_id: str, instance_id: str
) -> dict[str, Any] | None:
    path = receipt_path(workspace, review_post_id, instance_id)
    try:
        return validate_receipt(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None


def is_terminal(record: dict[str, Any]) -> bool:
    return record.get("state") in TERMINAL_STATES


def scan_receipts(workspace: Path) -> list[tuple[str, str, dict[str, Any]]]:
    """List all receipts as (post_id, instance_id, record). Corrupt files raise."""
    base = receipts_root(Path(workspace))
    found: list[tuple[str, str, dict[str, Any]]] = []
    if not base.is_dir():
        return found
    for post_dir in sorted(base.iterdir()):
        if not post_dir.is_dir() or os.path.islink(post_dir):
            continue
        for entry in sorted(post_dir.glob("*.json")):
            if os.path.islink(entry):
                continue
            record = validate_receipt(json.loads(entry.read_text(encoding="utf-8")))
            found.append((post_dir.name, entry.stem, record))
    return found
