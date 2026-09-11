"""Production-local deploy state (fixture-root scoped in tests)."""
from __future__ import annotations

import fcntl
import json
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


class StateError(Exception):
    pass


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
    tmp = current_path(prod_root).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(current_path(prod_root))


def append_history(prod_root: Path, event: dict) -> None:
    ensure_state_dir(prod_root)
    ev = dict(event)
    ev.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
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


def assert_no_secret_values(managed_files: dict) -> None:
    # deploy state must never contain secret-looking keys/values
    blob = json.dumps(managed_files).lower()
    for needle in ("bearer", "api_key", "apikey", "secret", "token=", "presign"):
        if needle in blob:
            # file hashes are hex; paths should never contain these substrings.
            # Managed paths are allowlisted so this is a fail-closed guard.
            raise StateError(f"DEPLOY_STATE_SECRET_SUSPECT: {needle}")
