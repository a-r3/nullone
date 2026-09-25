#!/usr/bin/env python3
"""NullOne Visual Director CLI (docs/contracts/visual-director-contract-v1.md).

    python3 social/ops/scripts/nullone-visual-director.py evaluate \
        --candidate-id <CANDIDATE_ID> \
        --request-file <workspace-contained raw decision JSON>

Reads one model-authored `nullone.visual-decision.v1` document, validates
it strictly and fails closed on anything malformed, unknown, or
contradictory (`nullone_visual_director.validate_decision` -- see that
module for the exact rules, including: SOURCE_PHOTO/DATA_VISUALIZATION
require an already-downloaded, hash-verified local asset file, never a
bare URL; BRANDED_GRAPHIC/EDITORIAL_TYPOGRAPHY must carry no evidence
claim at all; free-text reasoning/rationale fields are rejected
outright), and writes the authoritative decision to the ONE canonical
path derived from the candidate id:

    social/drafts/production/<candidate-id>-visual-decision.json

The caller chooses no path: the same candidate always maps to the same
decision file, so no alternate decision can exist to bypass this step.
Re-evaluating identical input is idempotent; a changed request for an
already-decided candidate is refused, never silently replaced.

This module makes NO editorial judgment of its own -- NullOne (the
visual-director role, see docs/architecture/provider-role-routing.md)
decides the visual style; this CLI only proves the decision is well-
formed, internally consistent, and (for evidence-backed styles) backed
by a real, validated local file before it can reach the packaging
evaluator, the renderer, or the brand gate.

Decisions never carry secrets.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from nullone_bridge_common import BridgeError, WORKSPACE, atomic_write_json
from nullone_packaging_receipt import check_candidate_id, contained_path
from nullone_visual_director import (
    VisualDirectorError,
    canonical_visual_decision_path,
    finalize_decision,
    validate_decision,
)

MODEL_FACING_ROOT = WORKSPACE / "social/drafts/production"


def load_raw_decision(path: Path, *, root: Path) -> dict[str, Any]:
    resolved = contained_path(Path(path), root)
    model_facing_root = root / "social/drafts/production"
    try:
        resolved.relative_to(model_facing_root.resolve())
    except ValueError as e:
        raise BridgeError(
            "VISUAL_DECISION_INPUT_INVALID: request must live under social/drafts/production/"
        ) from e
    if Path(path).is_symlink():
        raise BridgeError("VISUAL_DECISION_INPUT_INVALID: request must not be a symlink")
    if not resolved.is_file():
        raise BridgeError("VISUAL_DECISION_INPUT_INVALID: request is not a regular file")
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BridgeError("VISUAL_DECISION_INPUT_INVALID: request is not valid JSON") from e
    return raw


def evaluate_command(args: argparse.Namespace, *, root: Path = WORKSPACE) -> int:
    candidate_id = check_candidate_id(args.candidate_id)
    raw = load_raw_decision(Path(args.request_file), root=root)
    if isinstance(raw, dict) and raw.get("candidate_id") not in (None, candidate_id):
        raise BridgeError("VISUAL_DECISION_INPUT_INVALID: candidate_id mismatch")
    if isinstance(raw, dict):
        raw = dict(raw, candidate_id=candidate_id)
    try:
        validated = validate_decision(raw, root=root)
    except VisualDirectorError as e:
        raise BridgeError(str(e)) from e
    decision = finalize_decision(validated)

    out = canonical_visual_decision_path(candidate_id, root=root)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if existing == decision:
            print(f"DECISION_PATH={out}")
            print("DECISION_STATUS=UNCHANGED")
            return 0
        raise BridgeError("VISUAL_DECISION_CONFLICT: a different decision already exists")
    atomic_write_json(out, decision)
    print(f"DECISION_PATH={out}")
    print(f"VISUAL_STYLE={decision['visual_style']}")
    print(f"DECISION_REASON_CODE={decision['decision_reason_code']}")
    return 0


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "social/drafts/production").mkdir(parents=True)
        req = root / "social/drafts/production/probe-visual-decision-request.json"
        req.write_text(
            json.dumps(
                {
                    "schema": "nullone.visual-decision.v1",
                    "contract_version": "1.0.0",
                    "candidate_id": "probe",
                    "visual_style": "EDITORIAL_TYPOGRAPHY",
                    "source_asset_required": False,
                    "source_asset_url": None,
                    "source_asset_type": None,
                    "source_provenance": None,
                    "local_path": None,
                    "sha256": None,
                    "headline": "Self test headline",
                    "deck": None,
                    "stat": None,
                    "visual_motif": None,
                    "decision_reason_code": "EDITORIAL_TYPOGRAPHY_SUFFICIENT_CONTENT",
                    "recent_feed_context_used": False,
                }
            ),
            encoding="utf-8",
        )

        class Args:
            candidate_id = "probe"
            request_file = str(req)

        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = evaluate_command(Args(), root=root)
        assert rc == 0, buf.getvalue()
        out_path = root / "social/drafts/production/probe-visual-decision.json"
        assert out_path.is_file()
        # Idempotent re-run.
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc2 = evaluate_command(Args(), root=root)
        assert rc2 == 0
        assert "DECISION_STATUS=UNCHANGED" in buf2.getvalue()

    print("VISUAL_DIRECTOR_CLI_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne Visual Director CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("--candidate-id", required=True)
    e.add_argument("--request-file", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    try:
        if args.command == "evaluate":
            return evaluate_command(args)
        if args.command == "self-test":
            return self_test()
        raise BridgeError("Unknown command")
    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
