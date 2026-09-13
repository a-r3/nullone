#!/usr/bin/env python3
"""Shared packaging decision receipt helpers (no CLI, no side effects).

A packaging decision receipt is the durable, deterministic authority
produced by `nullone-packaging-evaluator.py` from one validated
packaging request, and consumed by the manifest gate
(`nullone-manifest.py build --packaging-receipt`) and the render
dispatcher (`nullone-packaging-render.py render --receipt`).

Receipt shape (all fields fixed; no secrets ever):

    schema, contract_version, candidate_id, input_fingerprint,
    POST_DECISION, CONTENT_SHAPE, REAL_PHOTO_AVAILABLE,
    REAL_PHOTO_REQUIRED, VISUAL_EVIDENCE_REQUIRED, ASSET_STRENGTH,
    TEXT_DENSITY, TIMELINESS, FORMAT_DECISION, FORMAT_REASON,
    VISUAL_STYLE, slide_count_recommendation, source_grounding,
    distinct_beat_count, content_type, receipt_hash

- `input_fingerprint` binds the exact validated request bytes.
- `receipt_hash` is the sha256 of the canonical JSON of every other
  field, so any tampering fails closed at load time.

Manifest `--format` mapping (packaging decision -> manifest format):

    SINGLE_POST -> FEED
    CAROUSEL    -> CAROUSEL
    STORY       -> refused in normal Draft Factory (delegated)
    SKIP        -> refused (nothing may be built)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_packaging_policy import (
    CONTRACT_VERSION,
    SCHEMA,
    evaluate_packaging,
)

RECEIPT_REQUIRED_FIELDS = (
    "schema",
    "contract_version",
    "candidate_id",
    "input_fingerprint",
    "POST_DECISION",
    "CONTENT_SHAPE",
    "REAL_PHOTO_AVAILABLE",
    "REAL_PHOTO_REQUIRED",
    "VISUAL_EVIDENCE_REQUIRED",
    "ASSET_STRENGTH",
    "TEXT_DENSITY",
    "TIMELINESS",
    "FORMAT_DECISION",
    "FORMAT_REASON",
    "VISUAL_STYLE",
    "slide_count_recommendation",
    "source_grounding",
    "distinct_beat_count",
    "content_type",
    "receipt_hash",
)

# Packaging FORMAT_DECISION -> manifest --format. STORY and SKIP have
# no manifest mapping: they are refused, never built.
FORMAT_TO_MANIFEST_FORMAT = {
    "SINGLE_POST": "FEED",
    "CAROUSEL": "CAROUSEL",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def input_fingerprint(validated_request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(validated_request)).hexdigest()


def build_receipt_body(
    *, candidate_id: str, validated_request: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    body = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "candidate_id": candidate_id,
        "input_fingerprint": input_fingerprint(validated_request),
        "POST_DECISION": decision["POST_DECISION"],
        "CONTENT_SHAPE": decision["CONTENT_SHAPE"],
        "REAL_PHOTO_AVAILABLE": decision["REAL_PHOTO_AVAILABLE"],
        "REAL_PHOTO_REQUIRED": decision["REAL_PHOTO_REQUIRED"],
        "VISUAL_EVIDENCE_REQUIRED": decision["VISUAL_EVIDENCE_REQUIRED"],
        "ASSET_STRENGTH": decision["ASSET_STRENGTH"],
        "TEXT_DENSITY": decision["TEXT_DENSITY"],
        "TIMELINESS": decision["TIMELINESS"],
        "FORMAT_DECISION": decision["FORMAT_DECISION"],
        "FORMAT_REASON": decision["FORMAT_REASON"],
        "VISUAL_STYLE": decision["VISUAL_STYLE"],
        "slide_count_recommendation": decision["slide_count_recommendation"],
        "source_grounding": decision["source_grounding"],
        "distinct_beat_count": decision["distinct_beat_count"],
        "content_type": decision["content_type"],
    }
    body["receipt_hash"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return body


def check_candidate_id(candidate_id: Any) -> str:
    if (
        not isinstance(candidate_id, str)
        or not candidate_id.strip()
        or "/" in candidate_id
        or "\\" in candidate_id
        or ".." in candidate_id
    ):
        raise BridgeError("PACKAGING_INPUT_INVALID: candidate_id must be a path-safe non-empty string")
    return candidate_id


def evaluate_request(candidate_id: str, validated_request: dict[str, Any]) -> dict[str, Any]:
    """Run the pure policy evaluator (no I/O). Raises BridgeError on failure."""

    from nullone_packaging_policy import PackagingContractError

    candidate_id = check_candidate_id(candidate_id)
    try:
        decision = evaluate_packaging(validated_request)
    except PackagingContractError as e:
        raise BridgeError(f"PACKAGING_EVALUATION_FAILED: {e}") from e
    return build_receipt_body(
        candidate_id=candidate_id, validated_request=validated_request, decision=decision
    )


def contained_path(path: Path, root: Path) -> Path:
    """Resolve `path` and require it to stay inside `root` (no traversal/symlink escape)."""

    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as e:
        raise BridgeError(f"PACKAGING_INPUT_INVALID: path escapes workspace: {path}") from e
    return resolved


def load_receipt(path: Path, *, root: Path = WORKSPACE) -> dict[str, Any]:
    """Load and verify a decision receipt. Any defect fails closed."""

    resolved = contained_path(Path(path), root)
    if Path(path).is_symlink():
        raise BridgeError("PACKAGING_INPUT_INVALID: receipt must not be a symlink")
    if not resolved.is_file():
        raise BridgeError("PACKAGING_INPUT_INVALID: receipt is not a regular file")
    try:
        receipt = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BridgeError("PACKAGING_INPUT_INVALID: receipt is not valid JSON") from e
    if not isinstance(receipt, dict):
        raise BridgeError("PACKAGING_INPUT_INVALID: receipt is not an object")
    for field in RECEIPT_REQUIRED_FIELDS:
        if field not in receipt:
            raise BridgeError(f"PACKAGING_INPUT_INVALID: receipt missing field {field!r}")
    if receipt.get("schema") != SCHEMA or receipt.get("contract_version") != CONTRACT_VERSION:
        raise BridgeError("PACKAGING_INPUT_INVALID: receipt schema/contract mismatch")
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    expected = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    if receipt.get("receipt_hash") != expected:
        raise BridgeError("PACKAGING_RECEIPT_TAMPERED: receipt hash mismatch")
    return receipt


def manifest_format_for_receipt(receipt: dict[str, Any]) -> str:
    """Map a POST receipt to its one allowed manifest format (else raise)."""

    if receipt.get("POST_DECISION") != "POST":
        raise BridgeError("PACKAGING_SKIPPED: receipt is not a POST decision")
    decision = receipt.get("FORMAT_DECISION")
    if decision == "STORY":
        raise BridgeError("PACKAGING_STORY_DELEGATED: normal Draft Factory never produces Story")
    mapped = FORMAT_TO_MANIFEST_FORMAT.get(decision)
    if mapped is None:
        raise BridgeError(f"PACKAGING_DECISION_MISMATCH: no manifest mapping for {decision!r}")
    return mapped
