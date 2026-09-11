"""Production-local deploy state (fixture-root scoped in tests)."""
from __future__ import annotations

import fcntl
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = "nullone.deploy-state/v1"

STATE_DIRNAME = "deploy-state"
CURRENT_NAME = "current.json"
HISTORY_NAME = "history.jsonl"
BACKUPS_DIRNAME = "backups"
LOCK_NAME = "lock"
TXN_NAME = "transaction.json"

HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
BACKUP_ID_RE = re.compile(r"^[0-9A-Za-z._:-]{1,128}$")

CURRENT_ALLOWED_KEYS = frozenset({
    "schema",
    "deployed_sha",
    "deployed_at",
    "release_tool_version",
    "policy_version",
    "policy_sha256",
    "managed_files",
    "managed_modes",
    "rolled_back_from_backup",
})
HISTORY_ALLOWED_KEYS = frozenset({
    "event",
    "at",
    "previous_sha",
    "target_sha",
    "restored_sha",
    "backup_id",
    "changed",
    "ci",
    "release_tool_version",
    "policy_version",
    "policy_sha256",
    "baseline_sha",
    "managed_count",
})
TXN_ALLOWED_KEYS = frozenset({
    "status",
    "phase",
    "target_sha",
    "backup_id",
    "started_at",
    "error",
})


class StateError(Exception):
    pass


def _check_safe_relpath(v: object, what: str) -> str:
    if not isinstance(v, str) or not (1 <= len(v) <= 512):
        raise StateError(f"DEPLOY_STATE_UNSAFE_{what}: bad length/type")
    if v.startswith("/") or "\\" in v or "\x00" in v:
        raise StateError(f"DEPLOY_STATE_UNSAFE_{what}: {v!r}")
    if ".." in v.split("/"):
        raise StateError(f"DEPLOY_STATE_UNSAFE_{what}: {v!r}")
    if not all(32 <= ord(c) < 127 for c in v):
        raise StateError(f"DEPLOY_STATE_UNSAFE_{what}: non-printable chars")
    return v


def _check_short_str(v: object, what: str, max_len: int = 128) -> None:
    if not isinstance(v, str) or len(v) > max_len:
        raise StateError(f"DEPLOY_STATE_UNSAFE_{what}: bad string")
    if "\x00" in v:
        raise StateError(f"DEPLOY_STATE_UNSAFE_{what}: NUL byte")


def _check_sha(v: object, what: str, allow_none: bool = False) -> None:
    if v is None and allow_none:
        return
    if not isinstance(v, str) or not HEX40_RE.match(v):
        raise StateError(f"DEPLOY_STATE_UNSAFE_{what}: not a full SHA")


def validate_safe_metadata(data: dict, kind: str) -> None:
    """Structural safe-metadata validator.

    Deploy state must be metadata only (SHAs, timestamps, versions, file
    hashes, modes, path lists) — never file contents, secrets, or payloads.
    Shape allowlists + hex/length caps prove this structurally, without any
    filename-substring blocking.
    """
    if not isinstance(data, dict):
        raise StateError(f"DEPLOY_STATE_UNSAFE_{kind}: not a mapping")
    if kind == "current":
        unknown = set(data) - CURRENT_ALLOWED_KEYS
        if unknown:
            raise StateError(f"DEPLOY_STATE_UNSAFE_current: unknown keys {sorted(unknown)}")
        if data.get("schema") != SCHEMA:
            raise StateError("DEPLOY_STATE_UNSAFE_current: schema mismatch")
        _check_sha(data.get("deployed_sha"), "deployed_sha", allow_none=True)
        _check_short_str(data.get("deployed_at", ""), "deployed_at", 64)
        _check_short_str(data.get("release_tool_version", ""), "release_tool_version", 32)
        _check_short_str(data.get("policy_version", ""), "policy_version", 32)
        ps = data.get("policy_sha256")
        if not isinstance(ps, str) or not HEX64_RE.match(ps):
            raise StateError("DEPLOY_STATE_UNSAFE_current: policy_sha256 must be hex64")
        mf = data.get("managed_files")
        if not isinstance(mf, dict):
            raise StateError("DEPLOY_STATE_UNSAFE_current: managed_files must be a mapping")
        for k, v in mf.items():
            _check_safe_relpath(k, "managed_path")
            if not isinstance(v, str) or not HEX64_RE.match(v):
                raise StateError("DEPLOY_STATE_UNSAFE_current: managed_files values must be sha256 hex")
        mm = data.get("managed_modes", {})
        if not isinstance(mm, dict):
            raise StateError("DEPLOY_STATE_UNSAFE_current: managed_modes must be a mapping")
        for k, v in mm.items():
            _check_safe_relpath(k, "managed_mode_path")
            if v not in ("0644", "0755"):
                raise StateError("DEPLOY_STATE_UNSAFE_current: managed_modes values must be 0644/0755")
        rb = data.get("rolled_back_from_backup")
        if rb is not None and (not isinstance(rb, str) or not BACKUP_ID_RE.match(rb)):
            raise StateError("DEPLOY_STATE_UNSAFE_current: bad backup id")
        blob = json.dumps(data, sort_keys=True)
        if len(blob) > 2_000_000:
            raise StateError("DEPLOY_STATE_UNSAFE_current: oversize (contents do not belong here)")
    elif kind == "history":
        unknown = set(data) - HISTORY_ALLOWED_KEYS
        if unknown:
            raise StateError(f"DEPLOY_STATE_UNSAFE_history: unknown keys {sorted(unknown)}")
        if data.get("event") not in ("update", "rollback", "bootstrap"):
            raise StateError("DEPLOY_STATE_UNSAFE_history: bad event")
        _check_short_str(data.get("at", ""), "at", 64)
        for key in ("previous_sha", "target_sha", "restored_sha", "baseline_sha"):
            if key in data:
                _check_sha(data[key], key, allow_none=True)
        if "backup_id" in data and data["backup_id"] is not None:
            _check_short_str(data["backup_id"], "backup_id", 128)
            if not BACKUP_ID_RE.match(data["backup_id"]):
                raise StateError("DEPLOY_STATE_UNSAFE_history: bad backup id")
        if "changed" in data:
            ch = data["changed"]
            if not isinstance(ch, list) or len(ch) > 10000:
                raise StateError("DEPLOY_STATE_UNSAFE_history: bad changed list")
            for item in ch:
                _check_safe_relpath(item, "changed_path")
        for key in ("ci", "release_tool_version", "policy_version"):
            if key in data:
                _check_short_str(data[key], key, 512)
        if "policy_sha256" in data:
            ps = data["policy_sha256"]
            if not isinstance(ps, str) or not HEX64_RE.match(ps):
                raise StateError("DEPLOY_STATE_UNSAFE_history: bad policy_sha256")
        if "managed_count" in data and not isinstance(data["managed_count"], int):
            raise StateError("DEPLOY_STATE_UNSAFE_history: bad managed_count")
        blob = json.dumps(data, sort_keys=True)
        if len(blob) > 256_000:
            raise StateError("DEPLOY_STATE_UNSAFE_history: oversize (contents do not belong here)")
    elif kind == "transaction":
        unknown = set(data) - TXN_ALLOWED_KEYS
        if unknown:
            raise StateError(f"DEPLOY_STATE_UNSAFE_transaction: unknown keys {sorted(unknown)}")
        if data.get("status") not in ("IN_PROGRESS", "INTERRUPTED"):
            raise StateError("DEPLOY_STATE_UNSAFE_transaction: bad status")
        if data.get("phase") not in ("backup", "install", "rollback", "bootstrap"):
            raise StateError("DEPLOY_STATE_UNSAFE_transaction: bad phase")
        if "target_sha" in data:
            _check_sha(data["target_sha"], "target_sha", allow_none=True)
        if "backup_id" in data and data["backup_id"] is not None:
            _check_short_str(data["backup_id"], "backup_id", 128)
        if "started_at" in data and not isinstance(data["started_at"], (int, float)):
            raise StateError("DEPLOY_STATE_UNSAFE_transaction: bad started_at")
        if "error" in data:
            _check_short_str(data["error"], "error", 2048)
        blob = json.dumps(data, sort_keys=True)
        if len(blob) > 16_000:
            raise StateError("DEPLOY_STATE_UNSAFE_transaction: oversize")
    else:
        raise StateError(f"DEPLOY_STATE_UNSAFE: unknown kind {kind!r}")


def state_dir(prod_root: Path) -> Path:
    return prod_root / STATE_DIRNAME


def current_path(prod_root: Path) -> Path:
    return state_dir(prod_root) / CURRENT_NAME


def history_path(prod_root: Path) -> Path:
    return state_dir(prod_root) / HISTORY_NAME


def backups_dir(prod_root: Path) -> Path:
    return state_dir(prod_root) / BACKUPS_DIRNAME


def lock_path(prod_root: Path) -> Path:
    return state_dir(prod_root) / LOCK_NAME


def txn_path(prod_root: Path) -> Path:
    return state_dir(prod_root) / TXN_NAME


def ensure_state_dir(prod_root: Path) -> Path:
    d = state_dir(prod_root)
    d.mkdir(parents=True, exist_ok=True)
    backups_dir(prod_root).mkdir(parents=True, exist_ok=True)
    return d


def read_current(prod_root: Path) -> dict | None:
    p = current_path(prod_root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise StateError(f"DEPLOY_STATE_CORRUPT: {e}") from e
    if data.get("schema") != SCHEMA:
        raise StateError(f"DEPLOY_STATE_SCHEMA_MISMATCH: {data.get('schema')!r}")
    return data


def write_current(prod_root: Path, data: dict) -> None:
    ensure_state_dir(prod_root)
    data = dict(data)
    data["schema"] = SCHEMA
    validate_safe_metadata(data, "current")
    tmp = current_path(prod_root).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(current_path(prod_root))


def append_history(prod_root: Path, event: dict) -> None:
    ensure_state_dir(prod_root)
    ev = dict(event)
    ev.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    validate_safe_metadata(ev, "history")
    with history_path(prod_root).open("a") as f:
        f.write(json.dumps(ev, sort_keys=True) + "\n")


def read_txn(prod_root: Path) -> dict | None:
    p = txn_path(prod_root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise StateError(f"TRANSACTION_CORRUPT: {e}") from e


def write_txn(prod_root: Path, data: dict) -> None:
    ensure_state_dir(prod_root)
    validate_safe_metadata(dict(data), "transaction")
    tmp = txn_path(prod_root).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(txn_path(prod_root))


def clear_txn(prod_root: Path) -> None:
    try:
        txn_path(prod_root).unlink()
    except FileNotFoundError:
        pass


@contextmanager
def deploy_lock(prod_root: Path):
    """Exclusive non-blocking deploy lock. Raises StateError if held."""
    ensure_state_dir(prod_root)
    lp = lock_path(prod_root)
    fh = lp.open("a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise StateError("DEPLOY_LOCK_HELD: another update/rollback is running") from e
        yield fh
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()
