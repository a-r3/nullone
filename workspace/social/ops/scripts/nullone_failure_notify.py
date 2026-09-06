#!/usr/bin/env python3
"""Domain failure Telegram notifier (issue #30).

Consumes an already-validated explicit run-outcome record (issue #27's
`nullone_run_outcome` contract) and decides whether the operator needs a
concise Telegram alert. The structured `domain_outcome` / `reason_code`
fields remain the sole source of business-health truth, so a
scheduler-`succeeded` receipt can never mask a business
`BLOCKED`/`FAILED`/actionable `UNKNOWN` result. `scheduler_status` is
consulted only for a narrower, separate purpose: routing notification
*ownership*. A true scheduler-level execution failure (`error`/`failed`)
belongs to OpenClaw's own scheduler-native `failureAlert` (see
docs/deployment/37-preflight-notification-requirements.md), so this
module stays quiet for that case rather than issuing a second, duplicate
alert for the same incident once that native alert is activated at #37.

Capability-negative by construction: this module has no import, symbol,
or code path capable of creating, approving, scheduling, retrying, or
otherwise mutating a publication attempt, a review draft, or a workflow
retry. Its only outbound side effect is a single injected transport call
carrying a short, sanitized status line. A transport failure only ever
changes this module's own local notification-attempt record — it can
never rewrite the run-outcome record it was given.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from nullone_bridge_common import WORKSPACE, atomic_write_json, now_iso
from nullone_run_outcome import health_decision, validate_result_structure

SCHEMA = "nullone.failure-notification.v1"

NOTIFICATION_ROOT = WORKSPACE / "social/ops/notifications"
OWNER_ID_FILE = WORKSPACE / "social/ops/private/telegram-owner-id"

WORKFLOW_DISPLAY_NAMES: dict[str, str] = {
    "morning-editorial": "Morning Editorial",
    "daily-analytics": "Daily Analytics",
}

# UNKNOWN reason codes that do NOT require operator action/reconciliation.
# Empty by default: no workflow currently emits a non-actionable UNKNOWN
# reason code. Extend this set deliberately when one is identified —
# never treat an UNKNOWN as quiet just because it looks inconvenient.
NON_ACTIONABLE_UNKNOWN_REASON_CODES: frozenset[str] = frozenset()

# scheduler_status values (case-insensitive) that mean OpenClaw itself
# already recorded this occurrence as a scheduler-level execution
# failure. That surface belongs to OpenClaw's own scheduler-native
# `failureAlert`, not this module — see the ownership-routing note on
# `_is_scheduler_native_execution_failure`. This is a routing rule only,
# never a substitute for `domain_outcome` as the source of business
# health truth.
SCHEDULER_NATIVE_FAILURE_STATUSES: frozenset[str] = frozenset({"error", "failed"})

NOTIFICATION_DEFERRED_POLICY = "SCHEDULER_NATIVE_FAILURE_ALERT"

MAX_REASON_TEXT_LEN = 200

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "[REDACTED]"),
    (re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S+"), "api_key=[REDACTED]"),
    (re.compile(r"(?i)oauth[_-]?(?:token|secret)\s*[:=]\s*\S+"), "[REDACTED]"),
    (re.compile(r"(?i)(?:access|refresh)[_-]?token\s*[:=]\s*\S+"), "[REDACTED]"),
    (
        re.compile(
            r"(?i)(?:x-amz-signature|signature|sig|token)=[^&\s]+"
        ),
        "[REDACTED]",
    ),
    (re.compile(r"[\r\n]+"), " "),
)


class NotifierError(RuntimeError):
    pass


class TransportAmbiguousError(NotifierError):
    """Delivery outcome could not be determined (e.g. a timeout).

    Unsafe to blindly resend automatically — the message may already
    have gone out.
    """


class TransportFailedError(NotifierError):
    """Delivery definitely did not happen (non-zero exit, missing
    configuration, etc.)."""


class Transport(Protocol):
    def send(self, message: str) -> None: ...


class OpenClawTelegramTransport:
    """Injectable Telegram transport, reusing the existing owner-ID file
    and CLI invocation convention already used for publication-result
    notifications, with the same ambiguous-timeout-vs-definite-failure
    distinction."""

    def __init__(
        self,
        *,
        account: str = "texbrif",
        timeout_seconds: int = 60,
    ) -> None:
        self._account = account
        self._timeout_seconds = timeout_seconds

    def send(self, message: str) -> None:
        if not OWNER_ID_FILE.is_file():
            raise TransportFailedError("Telegram owner ID file missing")

        owner_id = OWNER_ID_FILE.read_text(encoding="utf-8").strip()

        if not owner_id:
            raise TransportFailedError("Telegram owner ID is empty")

        cmd = [
            "openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--account",
            self._account,
            "--target",
            owner_id,
            "--message",
            message,
            "--json",
        ]

        try:
            cp = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportAmbiguousError(
                "Telegram notification timed out"
            ) from exc

        if cp.returncode != 0:
            raise TransportFailedError(
                f"Telegram notification failed (exit={cp.returncode})"
            )


def is_actionable(
    result: dict[str, Any],
    *,
    non_actionable_unknown_reason_codes: (
        frozenset[str]
    ) = NON_ACTIONABLE_UNKNOWN_REASON_CODES,
) -> bool:
    """Default actionability policy.

    Alert: BLOCKED, FAILED, and UNKNOWN unless its reason_code is
    explicitly listed as non-actionable.
    Quiet: SUCCEEDED, including its NO_ACTION/NO_DATA empty_success
    variants.
    """

    validate_result_structure(result)
    outcome = result["domain_outcome"]

    if outcome in ("BLOCKED", "FAILED"):
        return True

    if outcome == "UNKNOWN":
        return result["reason_code"] not in non_actionable_unknown_reason_codes

    return False


def _is_scheduler_native_execution_failure(result: dict[str, Any]) -> bool:
    """Ownership-routing check, not a health check.

    True when `scheduler_status` itself (case-insensitively) reports a
    scheduler-level execution failure — the surface OpenClaw's own
    `failureAlert` is meant to own once activated at #37. This never
    changes whether the result is *actionable*: `is_actionable()` still
    decides that from `domain_outcome` alone. It only decides whether
    this module — as opposed to the native scheduler alert — is the one
    that should speak, so one real incident cannot produce two alerts.
    """

    scheduler_status = result.get("scheduler_status", "")
    return (
        isinstance(scheduler_status, str)
        and scheduler_status.strip().lower() in SCHEDULER_NATIVE_FAILURE_STATUSES
    )


def sanitize_reason_text(text: str | None) -> str | None:
    """Deterministic, allowlist-adjacent redaction for outbound text.

    Reason text already comes from the #27 contract as a short,
    single-line, machine-authored string rather than raw provider/log
    output, but this is defense in depth: it never trusts that
    invariant blindly.
    """

    if text is None:
        return None

    sanitized = text

    for pattern, replacement in _REDACTIONS:
        sanitized = pattern.sub(replacement, sanitized)

    sanitized = sanitized.strip()

    if len(sanitized) > MAX_REASON_TEXT_LEN:
        sanitized = sanitized[: MAX_REASON_TEXT_LEN - 1].rstrip() + "…"

    return sanitized


def format_occurrence_time(occurrence_id: str) -> str:
    """Best-effort human-readable time for the alert.

    occurrence_id is an opaque identifier by #27's contract, not
    guaranteed to be a timestamp, and a future value could carry
    internal/private context. When it parses as a full ISO 8601
    datetime — which by construction can only ever contain date/time/
    offset digits, nothing else — render it concisely (labeling the
    Baku +04:00 offset used by both current scheduled workflows).
    Otherwise, never emit the raw identifier: return a neutral
    placeholder. Traceability is still available through the
    deterministic `run_id` already included in the alert.
    """

    try:
        parsed = datetime.fromisoformat(occurrence_id)
    except ValueError:
        return "unavailable"

    if parsed.utcoffset() is not None and parsed.strftime("%z") == "+0400":
        return parsed.strftime("%Y-%m-%d %H:%M") + " Baku"

    return parsed.isoformat()


def render_alert_text(result: dict[str, Any]) -> str:
    """Deterministic alert rendering — never delegated to a model."""

    validate_result_structure(result)

    workflow_id = result["workflow_id"]
    display_name = WORKFLOW_DISPLAY_NAMES.get(workflow_id, workflow_id)
    time_text = format_occurrence_time(result["occurrence_id"])
    reason_text = sanitize_reason_text(result.get("reason_text"))

    lines = [
        f"⚠️ NullOne — {display_name}",
        f"Status: {result['domain_outcome']}",
        f"Reason: {result['reason_code']}",
        f"Time: {time_text}",
        f"Run: {result['run_id']}",
    ]

    if reason_text:
        lines.append(f"Note: {reason_text}")

    return "\n".join(lines)


def _failure_identity(result: dict[str, Any]) -> str:
    decision = health_decision(result)
    identity = decision["failure_identity"]

    if identity is None:
        raise NotifierError("healthy result has no failure identity")

    return identity


def _safe_identity_slug(failure_identity: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", failure_identity)


def _notification_dir(workflow_id: str, output_root: Path) -> Path:
    """Resolve the per-workflow notification directory, independently
    verifying it cannot escape `output_root`.

    #27's `workflow_id` contract only guarantees a non-empty single-line
    string within a length bound — it does not guarantee path safety.
    This notifier does not weaken that general contract; it independently
    enforces its own filesystem boundary instead, since a path-escaping
    workflow_id here has consequences (writing/reading outside the
    configured notification root) that #27 itself was never responsible
    for preventing.
    """

    if Path(workflow_id).is_absolute():
        raise NotifierError(
            f"workflow_id must not be an absolute path: {workflow_id!r}"
        )

    root = output_root.resolve()
    candidate = (root / workflow_id).resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise NotifierError(
            f"workflow_id would escape the notification root: {workflow_id!r}"
        ) from exc

    return candidate


def _notification_lock_path(
    workflow_id: str,
    slug: str,
    output_root: Path,
) -> Path:
    return _notification_dir(workflow_id, output_root) / f"{slug}.lock"


def _notification_record_path(
    workflow_id: str,
    slug: str,
    output_root: Path,
) -> Path:
    return _notification_dir(workflow_id, output_root) / f"{slug}.json"


def _read_existing_record(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotifierError(
            "existing notification record is unreadable"
        ) from exc

    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise NotifierError("existing notification record is invalid")

    return value


def notify_if_required(
    result: dict[str, Any],
    *,
    transport: Transport,
    output_root: Path = NOTIFICATION_ROOT,
    non_actionable_unknown_reason_codes: (
        frozenset[str]
    ) = NON_ACTIONABLE_UNKNOWN_REASON_CODES,
) -> dict[str, Any]:
    """Send at most one automatic Telegram alert for one stable failure
    identity (`run_id:reason_code`, reusing #27's `health_decision`).

    This does not claim provider-side exactly-once Telegram delivery:
    only that NullOne consumes at most one automatic local notification
    attempt per stable failure identity. The notification attempt is
    durably reserved (written as `PENDING`) before the outbound call, so
    a crash between the two never allows a second automatic attempt —
    any existing record for this identity, in any state, blocks a new
    send and is returned as-is. A concurrent call for the same identity
    is serialized by an exclusive `fcntl.flock` on a lock file named
    after it, so two processes racing on the same failure cannot both
    reach the transport.

    Recovery (a later healthy result for the same workflow) is not
    detected or announced here — there is no code path that compares
    against a prior failure, so recovery stays quiet by construction,
    not by a policy flag that could be flipped by mistake.

    A true scheduler-level execution failure (`scheduler_status` of
    `error`/`failed`, case-insensitive) is also left quiet here, deferring
    to OpenClaw's own scheduler-native `failureAlert` for that surface —
    see `_is_scheduler_native_execution_failure`. This is ownership
    routing only, never a domain-health judgment: no notification state
    is created for this case, so it can never block a later automatic
    alert if a different, later failure identity for the same workflow
    genuinely needs this module's attention.

    The passed-in `result` is read-only from this function's
    perspective: it is never mutated, and its own record on disk (if
    any) is never opened, rewritten, or reconciled.
    """

    validate_result_structure(result)

    if not is_actionable(
        result,
        non_actionable_unknown_reason_codes=non_actionable_unknown_reason_codes,
    ):
        return {"status": "NOT_REQUIRED"}

    if _is_scheduler_native_execution_failure(result):
        return {
            "status": "NOT_REQUIRED",
            "policy": NOTIFICATION_DEFERRED_POLICY,
        }

    workflow_id = result["workflow_id"]
    failure_identity = _failure_identity(result)
    slug = _safe_identity_slug(failure_identity)

    notif_dir = _notification_dir(workflow_id, output_root)
    notif_dir.mkdir(parents=True, exist_ok=True)

    lock_path = _notification_lock_path(workflow_id, slug, output_root)
    record_path = _notification_record_path(workflow_id, slug, output_root)

    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        existing = _read_existing_record(record_path)

        if existing is not None:
            return {"status": f"ALREADY_{existing['state']}", "record": existing}

        message = render_alert_text(result)

        record: dict[str, Any] = {
            "schema": SCHEMA,
            "failure_identity": failure_identity,
            "run_id": result["run_id"],
            "workflow_id": workflow_id,
            "reason_code": result["reason_code"],
            "state": "PENDING",
            "attempts": 1,
            "created_at": now_iso(),
            "last_attempt_at": now_iso(),
        }

        # Reserve the attempt BEFORE the outbound side effect.
        atomic_write_json(record_path, record)

        try:
            transport.send(message)
        except TransportAmbiguousError as exc:
            record["state"] = "UNKNOWN"
            record["last_attempt_at"] = now_iso()
            record["error"] = str(exc)
            atomic_write_json(record_path, record)
            return {"status": "UNKNOWN", "record": record}
        except TransportFailedError as exc:
            record["state"] = "FAILED"
            record["last_attempt_at"] = now_iso()
            record["error"] = str(exc)
            atomic_write_json(record_path, record)
            return {"status": "FAILED", "record": record}

        record["state"] = "SENT"
        record["last_attempt_at"] = now_iso()
        atomic_write_json(record_path, record)
        return {"status": "SENT", "record": record}

    finally:
        os.close(lock_fd)
