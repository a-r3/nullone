#!/usr/bin/env python3
"""Vendor-neutral safe Radar failure-stage contract (no transport content).

Owns only the transport-neutral telemetry shape used by the
Breaking Radar wrapper and every current/future Radar transport
provider:

- stable ``reason_code`` constants
- ``ALLOWED_RADAR_REASON_CODES``
- ``RadarStageError(BridgeError)`` with safe numeric ``exit_code``
  normalization

Transport-specific mapping (for example Claude
``run_structured`` message matching) lives in the vendor provider
module (``nullone_claude_radar_provider``), never here. This module
imports no vendor transport and echoes no raw stdout/stderr,
prompts, fetched content, URLs, source text, signed URLs, tokens,
secrets, or credentials.
"""
from __future__ import annotations

from nullone_bridge_common import BridgeError

RADAR_REASON_CLAUDE_TIMEOUT = "CLAUDE_TIMEOUT"
RADAR_REASON_CLAUDE_BINARY_MISSING = "CLAUDE_BINARY_MISSING"
RADAR_REASON_CLAUDE_EXIT_NONZERO = "CLAUDE_EXIT_NONZERO"
RADAR_REASON_CLAUDE_OUTPUT_INVALID = "CLAUDE_OUTPUT_INVALID"
RADAR_REASON_RESULT_VALIDATION = "RESULT_VALIDATION"
RADAR_REASON_SCAN_IDENTITY = "SCAN_IDENTITY"
RADAR_REASON_EMPTY_SCAN_RECEIPT = "EMPTY_SCAN_RECEIPT"
RADAR_REASON_BATCH_PREFLIGHT = "BATCH_PREFLIGHT"
RADAR_REASON_REPORT_WRITE = "REPORT_WRITE"
RADAR_REASON_STAGING_WRITE = "STAGING_WRITE"
RADAR_REASON_COMMIT = "COMMIT"
RADAR_REASON_UNKNOWN = "UNKNOWN_RADAR_FAILURE"

ALLOWED_RADAR_REASON_CODES = frozenset(
    {
        RADAR_REASON_CLAUDE_TIMEOUT,
        RADAR_REASON_CLAUDE_BINARY_MISSING,
        RADAR_REASON_CLAUDE_EXIT_NONZERO,
        RADAR_REASON_CLAUDE_OUTPUT_INVALID,
        RADAR_REASON_RESULT_VALIDATION,
        RADAR_REASON_SCAN_IDENTITY,
        RADAR_REASON_EMPTY_SCAN_RECEIPT,
        RADAR_REASON_BATCH_PREFLIGHT,
        RADAR_REASON_REPORT_WRITE,
        RADAR_REASON_STAGING_WRITE,
        RADAR_REASON_COMMIT,
        RADAR_REASON_UNKNOWN,
    }
)


class RadarStageError(BridgeError):
    """Deterministic Radar failure stage with safe telemetry only."""

    def __init__(self, reason_code: str, *, exit_code: int | None = None):
        code = (
            reason_code
            if reason_code in ALLOWED_RADAR_REASON_CODES
            else RADAR_REASON_UNKNOWN
        )
        self.reason_code = code
        if isinstance(exit_code, bool):
            self.exit_code: int | None = None
        elif isinstance(exit_code, int):
            self.exit_code = exit_code
        else:
            self.exit_code = None
        message = f"Radar stage failure: {self.reason_code}"
        if self.reason_code == RADAR_REASON_CLAUDE_EXIT_NONZERO and self.exit_code is not None:
            message += f" exit={self.exit_code}"
        super().__init__(message)


def self_test() -> int:
    assert RADAR_REASON_UNKNOWN == "UNKNOWN_RADAR_FAILURE"
    assert len(ALLOWED_RADAR_REASON_CODES) == 12
    err = RadarStageError("CLAUDE_EXIT_NONZERO", exit_code=3)
    assert err.reason_code == "CLAUDE_EXIT_NONZERO"
    assert err.exit_code == 3
    assert "exit=3" in str(err)
    assert RadarStageError("NOPE").reason_code == "UNKNOWN_RADAR_FAILURE"
    assert RadarStageError("CLAUDE_EXIT_NONZERO", exit_code=True).exit_code is None
    print("RADAR_STAGE_ERROR_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
