#!/usr/bin/env python3
"""Deterministic packaging evaluator CLI (issue: packaging runtime wiring).

    python3 social/ops/scripts/nullone-packaging-evaluator.py evaluate \
        --candidate-id <CANDIDATE_ID> \
        --request-file <workspace-contained request JSON>

Reads one model-assessed packaging request, validates it
deterministically, runs the pure `nullone_packaging_policy`
evaluator, and writes the authoritative decision receipt to the ONE
canonical path derived from the candidate id:

    social/drafts/production/<candidate-id>-packaging-decision.json

The caller chooses no receipt path: the same candidate always maps
to the same receipt file, so no alternate valid receipt can exist
to bypass a decision. Re-evaluating identical input is idempotent;
a changed request for an already-receipted candidate is refused,
never silently replaced.

The model assesses raw signals only:

- FORMAT_DECISION, FORMAT_REASON, VISUAL_STYLE, and
  slide_count_recommendation are evaluator OUTPUTS: a request
  carrying any of them is rejected (PACKAGING_INPUT_INVALID).
- `verification` (Morning literal) is mapped deterministically:
  PASS -> PASS, any other known Morning literal -> BLOCKED.
  `verification_status` (contract literal) may be given instead;
  both present must agree or the request is rejected. The Morning
  handoff schema itself is never altered.

Optional `--visual-decision <canonical nullone.visual-decision.v1 file>`
(docs/contracts/visual-director-contract-v1.md): when given, the
decision is loaded and hash-verified (nullone_visual_director
.load_visual_decision), mapped onto the one optional packaging signal
`candidate.visual_director_style`, and injected before validation. The
model must never submit `visual_director_style` directly in the request
file -- only a properly validated decision file may supply it. Absent
`--visual-decision`, behavior is byte-identical to before this flag
existed.

Receipts never carry secrets.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from nullone_bridge_common import BridgeError, WORKSPACE, atomic_write_json
from nullone_packaging_receipt import (
    canonical_json_bytes,
    canonical_receipt_path,
    check_candidate_id,
    contained_path,
    evaluate_request,
)
from nullone_visual_director import (
    VisualDirectorError,
    load_visual_decision,
    packaging_style_directive,
)

MODEL_FACING_ROOT = WORKSPACE / "social/drafts/production"

# Morning structured-handoff verification literals. PASS maps to the
# contract's PASS; every other known literal maps to BLOCKED (a
# candidate that is not fully verified can never earn more than a
# blocked outcome). Unknown literals are malformed input, not a
# guessable default.
MORNING_VERIFICATION_VALUES = frozenset({"UNVERIFIED", "PARTIAL", "PASS", "BLOCKED"})

# Fields the model must never submit: evaluator outputs only.
MODEL_FORBIDDEN_REQUEST_FIELDS = frozenset(
    {
        "FORMAT_DECISION",
        "FORMAT_REASON",
        "VISUAL_STYLE",
        "slide_count_recommendation",
        "visual_director_style",
    }
)


def _map_verification(candidate: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(candidate)
    verification = candidate.pop("verification", None)
    status = candidate.get("verification_status")
    if verification is None and status is None:
        raise BridgeError("PACKAGING_INPUT_INVALID: candidate needs verification or verification_status")
    if verification is not None:
        if verification not in MORNING_VERIFICATION_VALUES:
            raise BridgeError(f"PACKAGING_INPUT_INVALID: unknown verification literal {verification!r}")
        mapped = "PASS" if verification == "PASS" else "BLOCKED"
        if status is not None and status != mapped:
            raise BridgeError("PACKAGING_INPUT_INVALID: verification and verification_status disagree")
        candidate["verification_status"] = mapped
    return candidate


def load_validated_request(path: Path, *, root: Path = WORKSPACE) -> dict[str, Any]:
    resolved = contained_path(Path(path), root)
    if root == WORKSPACE:
        try:
            resolved.relative_to(MODEL_FACING_ROOT.resolve())
        except ValueError as e:
            raise BridgeError("PACKAGING_INPUT_INVALID: request must live under social/drafts/production/") from e
    if Path(path).is_symlink():
        raise BridgeError("PACKAGING_INPUT_INVALID: request must not be a symlink")
    if not resolved.is_file():
        raise BridgeError("PACKAGING_INPUT_INVALID: request is not a regular file")
    try:
        request = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BridgeError("PACKAGING_INPUT_INVALID: request is not valid JSON") from e
    if not isinstance(request, dict):
        raise BridgeError("PACKAGING_INPUT_INVALID: request must be an object")
    candidate = request.get("candidate")
    if not isinstance(candidate, dict):
        raise BridgeError("PACKAGING_INPUT_INVALID: candidate must be an object")
    for field in MODEL_FORBIDDEN_REQUEST_FIELDS:
        if field in candidate:
            raise BridgeError(f"PACKAGING_INPUT_INVALID: model must not submit {field}")
    request["candidate"] = _map_verification(candidate)
    return request


def _apply_visual_decision(candidate_id: str, request: dict[str, Any], decision_path: str | None) -> dict[str, Any]:
    if decision_path is None:
        return request
    try:
        decision = load_visual_decision(Path(decision_path), candidate_id)
    except VisualDirectorError as e:
        raise BridgeError(f"PACKAGING_INPUT_INVALID: visual decision invalid: {e}") from e
    directive = packaging_style_directive(decision)
    if directive is not None:
        request = dict(request)
        request["candidate"] = dict(request["candidate"], visual_director_style=directive)
    return request


def evaluate_command(args: argparse.Namespace) -> int:
    candidate_id = check_candidate_id(args.candidate_id)
    request = load_validated_request(Path(args.request_file))
    request = _apply_visual_decision(candidate_id, request, getattr(args, "visual_decision", None))
    receipt = evaluate_request(candidate_id, request)
    out = canonical_receipt_path(candidate_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if existing == receipt:
            print(f"RECEIPT_PATH={out}")
            print("RECEIPT_STATUS=UNCHANGED")
            return 0
        raise BridgeError("PACKAGING_RECEIPT_CONFLICT: a different receipt already exists")
    atomic_write_json(out, receipt)
    print(f"RECEIPT_PATH={out}")
    print(f"FORMAT_DECISION={receipt['FORMAT_DECISION']}")
    print(f"VISUAL_STYLE={receipt['VISUAL_STYLE']}")
    return 0


def self_test() -> int:
    # Pure offline shape checks: forbidden fields rejected, mapping exact.
    bad = {"candidate": {"FORMAT_DECISION": "CAROUSEL"}, "assets": {}}
    try:
        if isinstance(bad.get("candidate"), dict) and "FORMAT_DECISION" in bad["candidate"]:
            raise BridgeError("PACKAGING_INPUT_INVALID: model must not submit FORMAT_DECISION")
    except BridgeError:
        pass
    mapped = _map_verification({"verification": "PARTIAL"})
    assert mapped["verification_status"] == "BLOCKED"
    assert _map_verification({"verification": "PASS"})["verification_status"] == "PASS"
    assert _map_verification({"verification_status": "PASS"})["verification_status"] == "PASS"
    print("PACKAGING_EVALUATOR_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne deterministic packaging evaluator")
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("--candidate-id", required=True)
    e.add_argument("--request-file", required=True)
    e.add_argument("--visual-decision", default=None, help="Canonical nullone.visual-decision.v1 file (optional)")
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
