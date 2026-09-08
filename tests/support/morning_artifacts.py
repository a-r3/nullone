#!/usr/bin/env python3
"""Shared fake Morning Editorial artifact writer for offline tests (#79).

Simulates a completed Morning provider cycle: writes BOTH required
artifacts for one board date -- the human-readable board Markdown and
the structured machine-readable candidate handoff JSON. Any test fake
that writes only the board simulates a partial provider cycle and must
expect FAILED/REQUIRED_ARTIFACT_MISSING.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_editorial_candidate_handoff import (  # noqa: E402
    CONTRACT_VERSION,
    SCHEMA,
    board_relative_path,
    handoff_relative_path,
)


def write_morning_artifacts(
    artifact_root: Path,
    board_date: str,
    candidates: tuple[dict, ...] = (),
) -> tuple[Path, Path]:
    """Write board + handoff for `board_date`; return both paths."""

    board = artifact_root / board_relative_path(board_date)
    board.parent.mkdir(parents=True, exist_ok=True)
    board.write_text("# Editorial board\n", encoding="utf-8")

    handoff = artifact_root / handoff_relative_path(board_date)
    handoff.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "contract_version": CONTRACT_VERSION,
                "editorial_date": board_date,
                "board_path": board_relative_path(board_date),
                "candidates": list(candidates),
            }
        ),
        encoding="utf-8",
    )
    return board, handoff


def write_board_only(artifact_root: Path, board_date: str) -> Path:
    """Write ONLY the board Markdown (partial cycle simulation)."""

    board = artifact_root / board_relative_path(board_date)
    board.parent.mkdir(parents=True, exist_ok=True)
    board.write_text("# Editorial board\n", encoding="utf-8")
    return board
