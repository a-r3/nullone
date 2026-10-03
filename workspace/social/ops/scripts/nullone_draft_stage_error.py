#!/usr/bin/env python3
"""Vendor-neutral safe Draft Factory failure-stage contract (no transport content).

Owns only the transport-neutral telemetry shape used by the Draft
Factory wrapper and every current/future Draft transport provider:

- stable ``reason_code`` constants
- ``ALLOWED_DRAFT_REASON_CODES``
- ``DraftStageError(BridgeError)`` with safe numeric ``exit_code``
  normalization
- ``mark_stage`` / ``stage_error_from``: an attribute-only stage
  tag a provider attaches to the ORIGINAL exception (type and
  message untouched) and the conversion the adapter boundary uses
  to turn a tagged failure into a ``DraftStageError``

Transport-specific mapping (for example Claude ``run_structured``
message matching) lives in the vendor provider module
(``nullone_claude_draft_provider``), never here. This module
imports no vendor transport and never reads, stores, or echoes the
original exception message: raw stdout/stderr, prompts, fetched
content, URLs, source text, caption text, model output, signed
URLs, tokens, secrets, and credentials cannot reach the telemetry.
"""
from __future__ import annotations

from nullone_bridge_common import BridgeError

# Transport / Claude
DRAFT_REASON_CLAUDE_TIMEOUT = "CLAUDE_TIMEOUT"
DRAFT_REASON_CLAUDE_BINARY_MISSING = "CLAUDE_BINARY_MISSING"
DRAFT_REASON_CLAUDE_EXIT_NONZERO = "CLAUDE_EXIT_NONZERO"
DRAFT_REASON_CLAUDE_OUTPUT_INVALID = "CLAUDE_OUTPUT_INVALID"
# Pre-side-effect Draft stages
DRAFT_REASON_QUEUE_LOAD = "QUEUE_LOAD"
DRAFT_REASON_CLAUDE_SELECT = "CLAUDE_SELECT"
DRAFT_REASON_SELECT_VALIDATION = "SELECT_VALIDATION"
DRAFT_REASON_SELECT_QUEUE_IDENTITY = "SELECT_QUEUE_IDENTITY"
DRAFT_REASON_FALLBACK_LEDGER_INIT = "FALLBACK_LEDGER_INIT"
DRAFT_REASON_FALLBACK_NEXT = "FALLBACK_NEXT"
DRAFT_REASON_EXISTING_RECEIPT_VALIDATION = "EXISTING_RECEIPT_VALIDATION"
DRAFT_REASON_INTERRUPTED_STATE_VALIDATION = "INTERRUPTED_STATE_VALIDATION"
# Packaging
DRAFT_REASON_PACKAGING_INPUT_WRITE = "PACKAGING_INPUT_WRITE"
DRAFT_REASON_PACKAGING_EVALUATOR = "PACKAGING_EVALUATOR"
DRAFT_REASON_PACKAGING_RECEIPT_VALIDATION = "PACKAGING_RECEIPT_VALIDATION"
DRAFT_REASON_PACKAGING_CLASSIFICATION = "PACKAGING_CLASSIFICATION"
DRAFT_REASON_FALLBACK_RECORD = "FALLBACK_RECORD"
# Post-acceptance
DRAFT_REASON_CLAUDE_PRODUCE = "CLAUDE_PRODUCE"
DRAFT_REASON_PRODUCE_VALIDATION = "PRODUCE_VALIDATION"
DRAFT_REASON_CAPTION_WRITE = "CAPTION_WRITE"
DRAFT_REASON_RENDER = "RENDER"
DRAFT_REASON_MANIFEST = "MANIFEST"
DRAFT_REASON_DRAFT_BRIDGE = "DRAFT_BRIDGE"
DRAFT_REASON_QUEUE_STATUS_FLIP = "QUEUE_STATUS_FLIP"
DRAFT_REASON_PREVIEW_PAYLOAD = "PREVIEW_PAYLOAD"
DRAFT_REASON_TELEGRAM_DELIVERY = "TELEGRAM_DELIVERY"
DRAFT_REASON_CLAUDE_COMPLETE = "CLAUDE_COMPLETE"
DRAFT_REASON_LEDGER_WRITE = "LEDGER_WRITE"
DRAFT_REASON_REPORT_WRITE = "REPORT_WRITE"
# Fallback
DRAFT_REASON_UNKNOWN = "UNKNOWN_DRAFT_FAILURE"

ALLOWED_DRAFT_REASON_CODES = frozenset(
    {
        DRAFT_REASON_CLAUDE_TIMEOUT,
        DRAFT_REASON_CLAUDE_BINARY_MISSING,
        DRAFT_REASON_CLAUDE_EXIT_NONZERO,
        DRAFT_REASON_CLAUDE_OUTPUT_INVALID,
        DRAFT_REASON_QUEUE_LOAD,
        DRAFT_REASON_CLAUDE_SELECT,
        DRAFT_REASON_SELECT_VALIDATION,
        DRAFT_REASON_SELECT_QUEUE_IDENTITY,
        DRAFT_REASON_FALLBACK_LEDGER_INIT,
        DRAFT_REASON_FALLBACK_NEXT,
        DRAFT_REASON_EXISTING_RECEIPT_VALIDATION,
        DRAFT_REASON_INTERRUPTED_STATE_VALIDATION,
        DRAFT_REASON_PACKAGING_INPUT_WRITE,
        DRAFT_REASON_PACKAGING_EVALUATOR,
        DRAFT_REASON_PACKAGING_RECEIPT_VALIDATION,
        DRAFT_REASON_PACKAGING_CLASSIFICATION,
        DRAFT_REASON_FALLBACK_RECORD,
        DRAFT_REASON_CLAUDE_PRODUCE,
        DRAFT_REASON_PRODUCE_VALIDATION,
        DRAFT_REASON_CAPTION_WRITE,
        DRAFT_REASON_RENDER,
        DRAFT_REASON_MANIFEST,
        DRAFT_REASON_DRAFT_BRIDGE,
        DRAFT_REASON_QUEUE_STATUS_FLIP,
        DRAFT_REASON_PREVIEW_PAYLOAD,
        DRAFT_REASON_TELEGRAM_DELIVERY,
        DRAFT_REASON_CLAUDE_COMPLETE,
        DRAFT_REASON_LEDGER_WRITE,
        DRAFT_REASON_REPORT_WRITE,
        DRAFT_REASON_UNKNOWN,
    }
)

_TAG_CODE_ATTR = "_draft_stage_code"
_TAG_EXIT_ATTR = "_draft_stage_exit_code"
_MAX_CAUSE_DEPTH = 8


def _safe_exit_code(exit_code: object) -> int | None:
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return None
    return exit_code


class DraftStageError(BridgeError):
    """Deterministic Draft failure stage with safe telemetry only."""

    def __init__(self, reason_code: str, *, exit_code: int | None = None):
        code = (
            reason_code
            if isinstance(reason_code, str) and reason_code in ALLOWED_DRAFT_REASON_CODES
            else DRAFT_REASON_UNKNOWN
        )
        self.reason_code = code
        self.exit_code: int | None = _safe_exit_code(exit_code)
        message = f"Draft stage failure: {self.reason_code}"
        if self.reason_code == DRAFT_REASON_CLAUDE_EXIT_NONZERO and self.exit_code is not None:
            message += f" exit={self.exit_code}"
        super().__init__(message)


def mark_stage(
    exc: BaseException, reason_code: str, *, exit_code: int | None = None
) -> None:
    """Attach a safe stage tag to an in-flight exception (first tag wins).

    Attribute-only: the exception's type, message, and traceback are
    untouched, so existing fail-closed behavior and diagnostics are
    identical. The innermost (first-applied) tag is kept so the most
    specific stage is reported.
    """
    if getattr(exc, _TAG_CODE_ATTR, None) is not None:
        return
    code = (
        reason_code
        if isinstance(reason_code, str) and reason_code in ALLOWED_DRAFT_REASON_CODES
        else DRAFT_REASON_UNKNOWN
    )
    try:
        setattr(exc, _TAG_CODE_ATTR, code)
        setattr(exc, _TAG_EXIT_ATTR, _safe_exit_code(exit_code))
    except Exception:
        # Tagging is best-effort observability; never alter control flow.
        return


def stage_error_from(exc: BaseException) -> DraftStageError:
    """Convert a failure into a ``DraftStageError`` (never echoes text).

    Uses the stage tag on ``exc`` or, if absent, on its explicit
    ``__cause__`` chain (a provider may re-wrap a tagged inner
    failure). Untagged failures become UNKNOWN_DRAFT_FAILURE.
    """
    if isinstance(exc, DraftStageError):
        return exc
    current: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None:
            break
        code = getattr(current, _TAG_CODE_ATTR, None)
        if isinstance(code, str):
            return DraftStageError(
                code, exit_code=getattr(current, _TAG_EXIT_ATTR, None)
            )
        current = current.__cause__
    return DraftStageError(DRAFT_REASON_UNKNOWN)


def format_draft_stage_blocked(exc: BaseException) -> str:
    """Safe deterministic BLOCKED line for a DraftStageError.

    Exposes only the stable reason code and, for nonzero Claude
    exits, the numeric exit code. Never includes raw exception text.
    """
    code = getattr(exc, "reason_code", DRAFT_REASON_UNKNOWN)
    if not isinstance(code, str) or code not in ALLOWED_DRAFT_REASON_CODES:
        code = DRAFT_REASON_UNKNOWN
    exit_code = getattr(exc, "exit_code", None)
    if (
        code == DRAFT_REASON_CLAUDE_EXIT_NONZERO
        and isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
    ):
        return f"ROLE_OUTCOME=BLOCKED reason=DraftStageError code={code} exit={exit_code}"
    return f"ROLE_OUTCOME=BLOCKED reason=DraftStageError code={code}"


def self_test() -> int:
    assert DRAFT_REASON_UNKNOWN == "UNKNOWN_DRAFT_FAILURE"
    assert len(ALLOWED_DRAFT_REASON_CODES) == 30
    err = DraftStageError("CLAUDE_EXIT_NONZERO", exit_code=3)
    assert err.reason_code == "CLAUDE_EXIT_NONZERO"
    assert err.exit_code == 3
    assert "exit=3" in str(err)
    assert DraftStageError("NOPE").reason_code == "UNKNOWN_DRAFT_FAILURE"
    assert DraftStageError("CLAUDE_EXIT_NONZERO", exit_code=True).exit_code is None
    inner = BridgeError("secret https://x/?sig=abc token=zzz")
    mark_stage(inner, "RENDER")
    mark_stage(inner, "MANIFEST")  # first tag wins
    converted = stage_error_from(inner)
    assert converted.reason_code == "RENDER"
    assert "secret" not in str(converted) and "sig=" not in str(converted)
    assert stage_error_from(BridgeError("x")).reason_code == "UNKNOWN_DRAFT_FAILURE"
    print("DRAFT_STAGE_ERROR_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
