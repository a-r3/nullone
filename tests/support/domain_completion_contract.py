"""Compatibility import for the #4/#5 behavioral contract."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "workspace/social/ops/scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from nullone_run_outcome import (  # noqa: E402,F401
    ALLOWED_EMPTY_SUCCESS,
    ALLOWED_OUTCOMES,
    CompletionContractError,
    validate_domain_completion,
)

__all__ = [
    "ALLOWED_EMPTY_SUCCESS",
    "ALLOWED_OUTCOMES",
    "CompletionContractError",
    "validate_domain_completion",
]
