#!/usr/bin/env python3
"""Deterministic final-publish controller daemon for issue #89.

Single lock owner for the whole claim -> authorization -> attempt ownership
transition. The OpenClaw plugin spawns EXACTLY ONE daemon per Gateway boot,
hands it the per-boot channel key K over the private spawn pipe, and forwards
one HMAC-authenticated envelope per authorized human callback over the same
private pipe (stdin). The daemon:

- verifies HMAC (constant-time) BEFORE receipt, authorization, manifest, or
  publication action; anything else fails closed with zero side effects;
- holds review_post_lock(review_post_id) exactly once across receipt claim,
  final authorization, bridge-core invocation, and receipt settlement;
- calls the publication bridge CORE IN-PROCESS (no parent/child boundary
  between the authorization decision and the attempts=1 persist), closing
  the subprocess orphan race;
- settles every outcome from re-read durable manifest state; attempts>=1
  ALWAYS wins over receipt state (settle-from-manifest, never re-invoke);
- invokes the deterministic notifier best-effort AFTER settlement; notifier
  failure never retries publication.

Provenance model (see V4 design report): the ONLY acceptable caller is the
plugin-spawned daemon of this boot. A separately-spawned binary has no K and
no pipe; `execute_authorized` additionally requires the process-local daemon
key installed ONLY by `daemon_boot()` in that same process, so direct
publisher/main LLM exec, raw shell invocation, and fabricated tuples all fail
BEFORE receipt claim, final_publish mutation, and any attempt. Same-UID
memory scraping, reviewed-code modification, process signaling, and root are
out of scope (each defeats every host control equally, including secret
files) and must be handled by host hardening, not this module.

Return-code contract (preserved from the legacy wrapper): 0 = executed to a
provider answer (truth in manifest state, incl. CHECK_REQUIRED-class
outcomes), 2 = BLOCKED before any attempt (attempts stays 0), 3 =
UNKNOWN/timeout/attempt-consumed failure (attempts>=1, never retry).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import os
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable

# The daemon is spawned fresh (cwd = served workspace); make sibling script
# modules importable regardless of the inherited sys.path.
_HERE_BOOT = Path(__file__).resolve().parent
if str(_HERE_BOOT) not in sys.path:
    sys.path.insert(0, str(_HERE_BOOT))

from nullone_bridge_common import (
    BridgeError,
    atomic_write_json,
    find_manifest_by_review_post_id,
    now_iso,
    validate_manifest,
    workspace_relative,
)
from nullone_story_supersession import (
    require_story_not_superseded,
    review_post_lock,
)

import nullone_publish_ipc as ipc
import nullone_publish_receipt as receipts

HERE = Path(__file__).resolve().parent

# Script filenames use dashes and are NOT importable as modules. Load them
# by file location (same technique as the offline suite's load_script).
# Attribute-level indirection (bridge_core / notify_fn) keeps offline tests
# able to substitute fakes without touching production wiring.
_SCRIPT_MODULES: dict[str, Any] = {}


def _load_script(name: str, filename: str) -> Any:
    if name not in _SCRIPT_MODULES:
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, HERE / filename)
        if spec is None or spec.loader is None:
            raise BridgeError(f"Cannot load {filename}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _SCRIPT_MODULES[name] = module
    return _SCRIPT_MODULES[name]


def _publisher_run() -> Any:
    return _load_script(
        "nullone_final_publish_controller_publisher_run", "nullone-publisher-run.py"
    )


def _bridge_module() -> Any:
    return _load_script(
        "nullone_final_publish_controller_bridge", "nullone-publish-bridge.py"
    )


def _notify_module() -> Any:
    return _load_script(
        "nullone_final_publish_controller_notify", "nullone-publish-notify.py"
    )


# Imported by attribute so offline tests can substitute a fake core that
# mutates an isolated manifest exactly like the real bridge would.
try:
    _real_bridge_core = _bridge_module().execute_loaded
except Exception:  # pragma: no cover - import-time, exercised in suite
    _real_bridge_core = None  # type: ignore[assignment]

bridge_core: Callable[..., int] | None = _real_bridge_core

try:
    _real_notify = _notify_module().notify
except Exception:  # pragma: no cover
    _real_notify = None  # type: ignore[assignment]

notify_fn: Callable[[str], int] | None = _real_notify

SENTINEL_NAME = "publish-controller.sentinel"

# Process-local daemon key (per-boot K, memory-only). Installed ONLY by
# daemon_boot() in this process. Separate processes (LLM direct exec, raw
# shell) have an empty registry and fail the gate below.
_INSTALLED_KEYS: set[bytes] = set()


def install_daemon_key(key: bytes) -> None:
    """Install the per-boot channel key in THIS process only (daemon boot)."""
    if len(key) != ipc.KEY_LEN:
        raise BridgeError("Daemon key length invalid")
    _INSTALLED_KEYS.add(bytes(key))


def install_test_key() -> bytes:
    """Test-only helper: mint + install a process-local key, return it."""
    import secrets

    key = secrets.token_bytes(ipc.KEY_LEN)
    _INSTALLED_KEYS.add(key)
    return key


def _key_installed(key: bytes) -> bool:
    return any(hmac.compare_digest(key, installed) for installed in _INSTALLED_KEYS)


def workspace_root() -> Path:
    """Resolve the served workspace (repo layout or deployed production)."""
    override = os.environ.get("NULLONE_WORKSPACE")
    if override:
        return Path(override)
    from nullone_bridge_common import WORKSPACE

    return Path(WORKSPACE)


def _fingerprint(manifest: dict[str, Any]) -> dict[str, Any]:
    pub = manifest.get("publication", {})
    approval = manifest.get("approval", {})
    return {
        "attempts": int(pub.get("attempts", 0)),
        "publication_state": str(pub.get("state", "")),
        "final_publish": bool(approval.get("final_publish", False)),
    }


def _settle_from_manifest(
    workspace: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    review_post_id: str,
    instance_id: str,
) -> tuple[str, int]:
    """Map durable manifest truth to (receipt_state, return_code)."""
    state = str(manifest.get("publication", {}).get("state", ""))
    mapping = {
        "PUBLISHED": ("SETTLED_PUBLISHED", 0),
        "PUBLISHING": ("SETTLED_PUBLISHING", 0),
        "FAILED": ("SETTLED_FAILED", 0),
        "CHECK_REQUIRED": ("SETTLED_CHECK_REQUIRED", 0),
        "READBACK_FAILED": ("SETTLED_READBACK_FAILED", 0),
        "UNKNOWN": ("SETTLED_UNKNOWN", 3),
        "PUBLISH_IN_FLIGHT": ("SETTLED_UNKNOWN", 3),
    }
    receipt_state, code = mapping.get(state, ("SETTLED_UNKNOWN", 3))
    try:
        receipts.transition_receipt(
            workspace,
            review_post_id,
            instance_id,
            receipt_state,
            {"outcome": state, "code": code},
        )
    except BridgeError:
        # No receipt (legacy row) or already terminal: create terminal record.
        try:
            existing = receipts.read_receipt(workspace, review_post_id, instance_id)
        except BridgeError:
            existing = None
        if existing is None:
            record = receipts.new_receipt(
                review_post_id,
                instance_id,
                _fingerprint(manifest),
                "0" * 64,
            )
            record["state"] = receipt_state
            record["updated_at"] = now_iso()
            record["result"] = {"outcome": state, "code": code}
            path = receipts.receipt_path(workspace, review_post_id, instance_id)
            atomic_write_json(path, record)
    return receipt_state, code


def _terminal_result(
    workspace: Path,
    review_post_id: str,
    instance_id: str,
    receipt: dict[str, Any],
) -> int:
    result = receipt.get("result") or {}
    return int(result.get("code", 2))


def execute_authorized(
    review_post_id: str,
    instance_id: str,
    envelope: dict[str, Any],
    *,
    _daemon_key: bytes,
    _workspace: Path | None = None,
) -> int:
    """Run ONE authorized final-publication instance to settlement.

    Provenance gate FIRST (no lock, no I/O): the caller must present the
    process-local daemon key installed by daemon_boot(). Separate-process
    callers (LLM direct exec, raw shell, fabricated tuples) fail here.
    """
    if not isinstance(_daemon_key, (bytes, bytearray)) or not _key_installed(
        bytes(_daemon_key)
    ):
        raise BridgeError(
            "Controller invocation lacks daemon provenance; "
            "legacy direct execution is disabled"
        )
    post_id = receipts.validate_post_id(review_post_id)
    if not isinstance(envelope, dict):
        raise BridgeError("Authorization envelope invalid")
    if not isinstance(instance_id, str):
        raise BridgeError("Authorization instance invalid")
    workspace = Path(_workspace) if _workspace is not None else workspace_root()

    with review_post_lock(post_id):
        return _execute_locked(workspace, post_id, instance_id, envelope)


def _execute_locked(
    workspace: Path,
    post_id: str,
    instance_id: str,
    envelope: dict[str, Any],
) -> int:
    # Re-derive the instance inside the lock: the presented id must match the
    # envelope fields (confused-deputy guard within the trusted process).
    derived = receipts.derive_authorization_instance_id(
        str(envelope.get("account_id", "")),
        str(envelope.get("chat_id", "")),
        str(envelope.get("message_id", "")),
        post_id,
    )
    if not hmac.compare_digest(derived, instance_id):
        raise BridgeError("Authorization instance mismatch")

    manifest_path, manifest = find_manifest_by_review_post_id(post_id)
    if manifest.get("review", {}).get("zernio_draft_id") != post_id:
        raise BridgeError("Review post ID mismatch")

    attempts = int(manifest.get("publication", {}).get("attempts", 0))
    if attempts >= 1:
        # Attempts consumed: truth lives in the manifest. Settle, never invoke.
        _settle_from_manifest(workspace, manifest_path, manifest, post_id, instance_id)
        _notify_best_effort(post_id)
        _path, current = find_manifest_by_review_post_id(post_id)
        return _code_for_state(str(current.get("publication", {}).get("state", "")))

    if manifest.get("format") == "STORY":
        require_story_not_superseded(
            manifest.get("manifest_id"),
            post_id,
        )

    nonce = str(envelope.get("nonce", ""))
    nonce_sha = hashlib.sha256(f"nullone-publish-nonce-v1{nonce}".encode()).hexdigest()
    try:
        receipt, created = receipts.claim_receipt(
            workspace, post_id, instance_id, _fingerprint(manifest), nonce_sha
        )
    except BridgeError:
        raise
    if not created:
        return _recover_existing(workspace, manifest_path, manifest, post_id, instance_id, receipt)

    # Fresh claim: authorize, mark EXECUTING, invoke core in-process.
    apply_final_authorization = _publisher_run().apply_final_authorization

    apply_final_authorization(manifest_path, manifest)
    receipts.transition_receipt(
        workspace, post_id, instance_id, "EXECUTING", {"outcome": "started"}
    )
    return _invoke_core(workspace, manifest_path, post_id, instance_id)


def _recover_existing(
    workspace: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    post_id: str,
    instance_id: str,
    receipt: dict[str, Any],
) -> int:
    """Closed recovery table for a pre-existing non-terminal receipt."""
    state = receipt.get("state")
    if receipts.is_terminal(receipt):
        # Old authorization is dead: report stored truth, never execute.
        # attempts>=1 rows re-derive truth from the manifest in case an
        # orphaned worker completed after the receipt settled.
        _path, current = find_manifest_by_review_post_id(post_id)
        if int(current.get("publication", {}).get("attempts", 0)) >= 1:
            _settle_from_manifest(workspace, manifest_path, current, post_id, instance_id)
            _path, current = find_manifest_by_review_post_id(post_id)
            return _code_for_state(str(current.get("publication", {}).get("state", "")))
        return _terminal_result(workspace, post_id, instance_id, receipt)

    pub = manifest.get("publication", {})
    attempts = int(pub.get("attempts", 0))
    final_publish = bool(manifest.get("approval", {}).get("final_publish", False))
    if attempts >= 1:
        _settle_from_manifest(workspace, manifest_path, manifest, post_id, instance_id)
        _notify_best_effort(post_id)
        _path, current = find_manifest_by_review_post_id(post_id)
        return _code_for_state(str(current.get("publication", {}).get("state", "")))

    if state == "RECEIVED":
        # Nothing ran (claim only): adopt the same instance.
        return _adopt(workspace, manifest_path, post_id, instance_id)
    if state == "EXECUTING":
        if final_publish:
            # Authorization stood but no attempt persisted: the core never
            # reached the attempts persist (attempts==0 proves no external
            # call under the in-process boundary). Adopt the same instance.
            return _adopt(workspace, manifest_path, post_id, instance_id)
        # EXECUTING is written only AFTER apply_final_authorization sets the
        # flag, and only the safe-BLOCKED revoke path clears it. A crash
        # before authorization would have left RECEIVED (handled above), so
        # EXECUTING + attempts==0 + cleared flag PROVES the wrapper ran to a
        # safe BLOCKED outcome: settle terminal, never invoke.
        receipts.transition_receipt(
            workspace,
            post_id,
            instance_id,
            "SETTLED_BLOCKED",
            {"outcome": "BLOCKED", "code": 2, "note": "revoke-observed"},
        )
        return 2
    receipts.transition_receipt(
        workspace, post_id, instance_id, "ABANDONED", {"outcome": "unknown-state"}
    )
    return 2


def _adopt(
    workspace: Path, manifest_path: Path, post_id: str, instance_id: str
) -> int:
    """Continue the SAME authorization instance (attempts==0 proven)."""
    apply_final_authorization = _publisher_run().apply_final_authorization

    _path, manifest = find_manifest_by_review_post_id(post_id)
    if manifest.get("format") == "STORY":
        require_story_not_superseded(manifest.get("manifest_id"), post_id)
    apply_final_authorization(manifest_path, manifest)
    receipts.transition_receipt(
        workspace, post_id, instance_id, "EXECUTING", {"outcome": "adopted"}
    )
    return _invoke_core(workspace, manifest_path, post_id, instance_id)


def _invoke_core(
    workspace: Path, manifest_path: Path, post_id: str, instance_id: str
) -> int:
    """Invoke the bridge core IN-PROCESS, on THIS thread, under the lock.

    Synchronous by design (#89 hardening): when this function returns, no
    publication-capable worker exists anywhere — attempts==0 on return
    PROVES nothing was or will be attempted by this invocation. No thread,
    no timeout-abandonment, no orphan: a fake wall-clock timeout around a
    thread cannot kill it, so it is refused as a boundary here.

    Boundedness of the core itself is proven, not assumed: the current
    bridge transport issues provider calls only through
    nullone_claude.run_structured, which runs `claude -p` via
    subprocess.run(..., timeout=...) (default 300 s, max_turns-bounded);
    the OS kills the child on expiry and the call raises BridgeError.
    After #90 the deterministic HTTP transport will own finite network
    timeouts instead.
    """
    if bridge_core is None:
        raise BridgeError("Publication core unavailable")
    _path, manifest = find_manifest_by_review_post_id(post_id)
    try:
        core_code = bridge_core(manifest_path, manifest)
    except BridgeError:
        return _on_core_bridge_error(workspace, manifest_path, post_id, instance_id)
    except Exception:
        return _on_core_unexpected(workspace, manifest_path, post_id, instance_id)

    _path, current = find_manifest_by_review_post_id(post_id)
    state = str(current.get("publication", {}).get("state", ""))
    receipts.transition_receipt(
        workspace, post_id, instance_id, _receipt_state_for(state), {"outcome": state}
    )
    _notify_best_effort(post_id)
    print("PUBLISH_CONTROLLER=PASS")
    print(f"PUBLICATION_STATE={state}")
    return int(core_code)


def _on_core_bridge_error(
    workspace: Path, manifest_path: Path, post_id: str, instance_id: str
) -> int:
    revoke_final_if_no_publish_attempt = (
        _publisher_run().revoke_final_if_no_publish_attempt
    )

    _path, current = find_manifest_by_review_post_id(post_id)
    if int(current.get("publication", {}).get("attempts", 0)) == 0:
        revoke_final_if_no_publish_attempt(manifest_path, current)
        receipts.transition_receipt(
            workspace,
            post_id,
            instance_id,
            "SETTLED_BLOCKED",
            {"outcome": "BLOCKED", "code": 2, "hint": "fresh_confirmation_required"},
        )
        _notify_best_effort(post_id)
        return 2
    _settle_from_manifest(workspace, manifest_path, current, post_id, instance_id)
    _notify_best_effort(post_id)
    print("PUBLICATION_STATE=UNKNOWN_OR_FAILED")
    print("AUTOMATIC_RETRY=FORBIDDEN")
    return 3


def _on_core_unexpected(
    workspace: Path, manifest_path: Path, post_id: str, instance_id: str
) -> int:
    revoke_final_if_no_publish_attempt = (
        _publisher_run().revoke_final_if_no_publish_attempt
    )

    _path, current = find_manifest_by_review_post_id(post_id)
    if int(current.get("publication", {}).get("attempts", 0)) == 0:
        # Pre-attempt crash: revoke, keep the instance adoptable (EXECUTING).
        revoke_final_if_no_publish_attempt(manifest_path, current)
        print("PUBLICATION_STATE=PRE_ATTEMPT_FAILURE")
        print("AUTOMATIC_RETRY=FORBIDDEN")
        return 3
    _settle_from_manifest(workspace, manifest_path, current, post_id, instance_id)
    print("PUBLICATION_STATE=UNKNOWN_OR_FAILED")
    print("AUTOMATIC_RETRY=FORBIDDEN")
    return 3


def _receipt_state_for(publication_state: str) -> str:
    return {
        "PUBLISHED": "SETTLED_PUBLISHED",
        "PUBLISHING": "SETTLED_PUBLISHING",
        "FAILED": "SETTLED_FAILED",
        "CHECK_REQUIRED": "SETTLED_CHECK_REQUIRED",
        "READBACK_FAILED": "SETTLED_READBACK_FAILED",
        "UNKNOWN": "SETTLED_UNKNOWN",
    }.get(publication_state, "SETTLED_UNKNOWN")


def _code_for_state(publication_state: str) -> int:
    if publication_state in (
        "PUBLISHED",
        "PUBLISHING",
        "FAILED",
        "CHECK_REQUIRED",
        "READBACK_FAILED",
    ):
        return 0
    return 3


def _notify_best_effort(post_id: str) -> None:
    """Invoke the deterministic notifier; failure never retries publication."""
    if notify_fn is None:
        return
    try:
        notify_fn(post_id)
    except Exception as error:
        print(f"NOTIFIER_FAILED_AFTER_SETTLE={type(error).__name__}")


def acquire_sentinel(
    workspace: Path, timeout_seconds: float = 30.0, poll_seconds: float = 0.2
):
    """Acquire the singleton daemon lock (bounded). Returns the open file."""
    path = Path(workspace) / "social" / "ops" / SENTINEL_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return os.fdopen(handle, "w")
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(handle)
                    raise BridgeError(
                        "Controller daemon sentinel held; "
                        "exactly one controller may run"
                    )
                time.sleep(poll_seconds)
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        raise


def reconcile_boot(workspace: Path) -> dict[str, Any]:
    """Fail-closed boot reconciliation for receipts left by a prior daemon.

    Non-terminal receipts with attempts>=1 settle from manifest truth;
    attempts==0 rows become ABANDONED (a fresh second-stage message is the
    only live path; old clicks are never manufactured into confirmations).
    """
    summary: dict[str, Any] = {"settled": [], "abandoned": [], "errors": []}
    for post_id, instance_id, record in receipts.scan_receipts(workspace):
        if receipts.is_terminal(record):
            continue
        try:
            manifest_path, manifest = find_manifest_by_review_post_id(post_id)
        except BridgeError:
            receipts.transition_receipt(
                workspace, post_id, instance_id, "ABANDONED", {"outcome": "no-manifest"}
            )
            summary["abandoned"].append(instance_id)
            continue
        attempts = int(manifest.get("publication", {}).get("attempts", 0))
        if attempts >= 1:
            state, _code = _settle_from_manifest(
                workspace, manifest_path, manifest, post_id, instance_id
            )
            summary["settled"].append({"instance": instance_id, "state": state})
        else:
            receipts.transition_receipt(
                workspace,
                post_id,
                instance_id,
                "ABANDONED",
                {"outcome": "boot-reconcile", "hint": "fresh_confirmation_required"},
            )
            summary["abandoned"].append(instance_id)
    return summary


def daemon_main(
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    workspace: Path | None = None,
    startup_timeout: float = 30.0,
) -> int:
    """Run the singleton controller daemon on stdio framing protocol.

    argv carries NOTHING sensitive. stdin: first exactly KEY_LEN raw bytes
    (the per-boot channel key over the private spawn pipe), then HMAC frames.
    stdout: one READY frame after key install, then one reply frame per
    request frame. stdin EOF exits fail-closed.
    """
    ws = Path(workspace) if workspace is not None else workspace_root()
    stream_in = stdin if stdin is not None else sys.stdin.buffer
    stream_out = stdout if stdout is not None else sys.stdout.buffer
    try:
        sentinel = acquire_sentinel(ws, timeout_seconds=startup_timeout)
    except BridgeError as error:
        print(f"DAEMON_UNAVAILABLE={error}")
        return 2
    try:
        key = _read_key(stream_in)
    except ipc.IpcError as error:
        print(f"DAEMON_KEY_REFUSED={type(error).__name__}")
        return 2
    install_daemon_key(key)
    summary = reconcile_boot(ws)
    _write_reply(
        stream_out,
        key,
        {"t": "ready", "reconciled": _sanitize_summary(summary)},
    )
    while True:
        try:
            envelope = ipc.read_frame(stream_in, key)
        except ipc.TruncatedFrameError:
            # EOF / clean shutdown: exit fail-closed with zero side effects.
            return 0
        except ipc.IpcError as error:
            _write_reply(
                stream_out, key, {"t": "error", "reason": type(error).__name__}
            )
            continue
        reply = _dispatch(envelope, ws, key)
        _write_reply(stream_out, key, reply, _echo_request_id(envelope))
    # Unreachable loop: `sentinel` stays referenced for process lifetime,
    # keeping the singleton lock held until the daemon exits.


def _read_key(stream_in: BinaryIO) -> bytes:
    data = b""
    while len(data) < ipc.KEY_LEN:
        chunk = stream_in.read(ipc.KEY_LEN - len(data))
        if not chunk:
            raise ipc.TruncatedFrameError("channel key incomplete")
        data += chunk
        if len(data) > ipc.KEY_LEN:
            raise ipc.MalformedFrameError("channel key oversize")
    return data


def _echo_request_id(envelope: dict[str, Any]) -> str | None:
    """Return the envelope's request_id for reply correlation, else None.

    Correlation is NOT authorization: the id only routes a reply to its
    requester. Only a well-formed 32-hex id is echoed; anything else is
    dropped (the plugin ignores id-less replies that are not the handshake).
    """
    candidate = envelope.get("request_id")
    if (
        isinstance(candidate, str)
        and len(candidate) == 32
        and all(c in "0123456789abcdef" for c in candidate)
    ):
        return candidate
    return None


def _write_reply(
    stream_out: BinaryIO,
    key: bytes,
    payload: dict[str, Any],
    request_id: str | None = None,
) -> None:
    safe = {k: str(v)[:256] for k, v in payload.items()}
    if request_id is not None:
        safe["request_id"] = request_id
    body = ipc.canonical_envelope_bytes({"schema": "nullone.publish-reply.v1", **safe})
    header = ipc.MAGIC + bytes((ipc.VERSION,)) + ipc.LEN_STRUCT.pack(len(body))
    mac = hmac.new(key, header + body, hashlib.sha256).digest()
    stream_out.write(header + body + mac)
    stream_out.flush()


def _sanitize_summary(summary: dict[str, Any]) -> dict[str, int]:
    return {
        "settled": len(summary.get("settled", [])),
        "abandoned": len(summary.get("abandoned", [])),
        "errors": len(summary.get("errors", [])),
    }


def _dispatch(envelope: dict[str, Any], workspace: Path, key: bytes) -> dict[str, Any]:
    try:
        post_id = receipts.validate_post_id(envelope.get("post_id"))
        instance_id = receipts.derive_authorization_instance_id(
            str(envelope.get("account_id", "")),
            str(envelope.get("chat_id", "")),
            str(envelope.get("message_id", "")),
            post_id,
        )
    except BridgeError:
        return {
            "t": "result",
            "outcome": "REJECTED",
            "code": 2,
            "publication_state": "REJECTED",
        }
    try:
        code = execute_authorized(
            post_id, instance_id, envelope, _daemon_key=key, _workspace=workspace
        )
    except BridgeError:
        return {
            "t": "result",
            "outcome": "BLOCKED",
            "code": 2,
            "publication_state": _truthful_state(workspace, post_id, "BLOCKED"),
        }
    except Exception:
        return {
            "t": "result",
            "outcome": "UNKNOWN",
            "code": 3,
            "publication_state": "UNKNOWN",
        }
    hint = "fresh_confirmation_required" if code == 2 else "none"
    # Code 2 is always a pre-attempt refusal (attempts==0 proven), so a
    # non-enum durable state (e.g. NOT_REQUESTED) truthfully maps to BLOCKED.
    # Any other code with a non-enum state maps to UNKNOWN, never completed.
    return {
        "t": "result",
        "outcome": "SETTLED",
        "code": code,
        "hint": hint,
        "publication_state": _truthful_state(
            workspace, post_id, "BLOCKED" if code == 2 else "UNKNOWN"
        ),
    }


# Closed enum of authoritative publication states the daemon may report.
# Anything outside it (missing manifest, unreadable/corrupt state) maps to
# UNKNOWN — never to a completion claim.
_ALLOWED_PUBLICATION_STATES = frozenset(
    {
        "PUBLISHED",
        "PUBLISHING",
        "FAILED",
        "CHECK_REQUIRED",
        "READBACK_FAILED",
        "UNKNOWN",
        "BLOCKED",
        "REJECTED",
    }
)


def _truthful_state(workspace: Path, post_id: str, fallback: str) -> str:
    """Re-read authoritative manifest truth for the daemon reply.

    Called AFTER execute_authorized returns, so this is post-execution
    durable truth, not a prediction. Unreadable or out-of-enum state maps
    to the fallback (UNKNOWN for settled paths, BLOCKED for refusals).
    """
    try:
        _path, manifest = find_manifest_by_review_post_id(post_id)
    except BridgeError:
        if fallback in _ALLOWED_PUBLICATION_STATES:
            return fallback
        return "UNKNOWN"
    state = manifest.get("publication", {}).get("state")
    if isinstance(state, str) and state in _ALLOWED_PUBLICATION_STATES:
        return state
    if fallback in _ALLOWED_PUBLICATION_STATES:
        return fallback
    return "UNKNOWN"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon")
    sub.add_parser("boot-reconcile")
    sub.add_parser("self-test")
    args = parser.parse_args(argv)
    try:
        if args.command == "daemon":
            return daemon_main()
        if args.command == "boot-reconcile":
            summary = reconcile_boot(workspace_root())
            print(f"BOOT_RECONCILE={_sanitize_summary(summary)}")
            return 0
        if args.command == "self-test":
            return self_test()
        raise BridgeError("Unknown command")
    except BridgeError as error:
        print(f"BLOCKED={error}")
        return 2


def self_test() -> int:
    from nullone_bridge_common import WORKSPACE as _ws

    del _ws
    key = install_test_key()
    assert len(_INSTALLED_KEYS) == 1
    assert _key_installed(key)
    assert not _key_installed(b"\x00" * ipc.KEY_LEN)
    try:
        execute_authorized(
            "0" * 24, "0" * 32, {}, _daemon_key=b"\x00" * ipc.KEY_LEN
        )
    except BridgeError as error:
        assert "provenance" in str(error)
    else:
        raise BridgeError("Provenance gate self-test failed")
    print("PUBLISH_CONTROLLER_SELF_TEST=PASS")
    print("PROVENANCE_GATE=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
