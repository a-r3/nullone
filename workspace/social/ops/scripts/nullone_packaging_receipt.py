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
    "visual_requirement",
    "receipt_hash",
)

# Packaging FORMAT_DECISION -> manifest --format. STORY and SKIP have
# no manifest mapping: they are refused, never built.
MODEL_FACING_DIRNAME = "social/drafts/production"

RECEIPT_FILENAME_SUFFIX = "-packaging-decision.json"
RENDER_RECORD_FILENAME_SUFFIX = "-render-record.json"

RENDER_RECORD_SCHEMA = "nullone.packaging-render-record.v1"

ASSET_DESCRIPTOR_SCHEMA = "nullone.packaging-asset.v1"

# Receipt VISUAL_STYLE -> the one asset_kind a descriptor may claim.
STYLE_TO_ASSET_KIND = {
    "REAL_PHOTO": "REAL_PHOTO",
    "SOURCE_SCREENSHOT": "SOURCE_SCREENSHOT",
    "DATA_VISUALIZATION": "DATA_VISUALIZATION",
    "EDITORIAL_TYPOGRAPHY": "NONE",
    "BRANDED_GRAPHIC": "NONE",
    "GENERATED_ILLUSTRATION_ALLOWED": "NONE",
}

# Asset kinds that must name an existing workspace-contained file.
# DATA_VISUALIZATION is file-backed: the exact deterministic/local
# visualization artifact is bound by path + hash, never a free-form
# source string.
FILE_BACKED_ASSET_KINDS = frozenset({"REAL_PHOTO", "SOURCE_SCREENSHOT", "DATA_VISUALIZATION"})


def canonical_receipt_path(candidate_id: str, *, root: Path = WORKSPACE) -> Path:
    """The ONE authoritative receipt path for a candidate (derived, never caller-chosen)."""

    check_candidate_id(candidate_id)
    return (root / MODEL_FACING_DIRNAME / f"{candidate_id}{RECEIPT_FILENAME_SUFFIX}").resolve()


def canonical_render_record_path(candidate_id: str, *, root: Path = WORKSPACE) -> Path:
    check_candidate_id(candidate_id)
    return (root / MODEL_FACING_DIRNAME / f"{candidate_id}{RENDER_RECORD_FILENAME_SUFFIX}").resolve()


# Packaging FORMAT_DECISION -> manifest --format. STORY and SKIP have
# no manifest mapping: they are refused, never built.
FORMAT_TO_MANIFEST_FORMAT = {
    "SINGLE_POST": "FEED",
    "CAROUSEL": "CAROUSEL",
}


def require_canonical_receipt(path: Path, candidate_id: str, *, root: Path = WORKSPACE) -> Path:
    """Refuse any receipt path that is not the candidate's canonical one."""

    if contained_path(Path(path), root) != canonical_receipt_path(candidate_id, root=root):
        raise BridgeError("PACKAGING_INPUT_INVALID: receipt path is not the canonical candidate receipt")
    return contained_path(Path(path), root)


def require_canonical_render_record(path: Path, candidate_id: str, *, root: Path = WORKSPACE) -> Path:
    """Refuse any render-record path that is not the candidate's canonical one."""

    if contained_path(Path(path), root) != canonical_render_record_path(candidate_id, root=root):
        raise BridgeError("PACKAGING_INPUT_INVALID: render record path is not the canonical candidate record")
    return contained_path(Path(path), root)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def input_fingerprint(validated_request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(validated_request)).hexdigest()


# Declared visual requirement (issue #138, E2E e2e-20260915-1925-controlled).
#
# Renderers such as the V2 feed card always emit a hero visual region:
# a layout backed by no evidence renders an empty decorative frame
# that must never become publication-ready. The model declares, as a
# raw signal like depicts_real_world_subject, whether the intended
# layout needs source-grounded visual evidence:
#
#   NONE             layout intentionally declares NO visual block
#                    (typography-only remains valid);
#   SOURCE_GROUNDED  layout declares a visual block that must be
#                    backed by a real photo, source screenshot/document
#                    visual, or faithful data visualization.
#
# The declaration is mandatory: an undeclared requirement is malformed
# input and fails closed. A grounded requirement with no usable
# evidence fails closed before any receipt exists.
VISUAL_REQUIREMENT_VALUES = frozenset({"NONE", "SOURCE_GROUNDED"})


def visual_requirement_of(validated_request: dict[str, Any]) -> str:
    candidate = validated_request.get("candidate")
    requirement = candidate.get("visual_requirement") if isinstance(candidate, dict) else None
    if requirement not in VISUAL_REQUIREMENT_VALUES:
        raise BridgeError(
            "PACKAGING_INPUT_INVALID: candidate.visual_requirement must be one of"
            " ['NONE', 'SOURCE_GROUNDED']"
        )
    return requirement


def visual_evidence_available(validated_request: dict[str, Any]) -> bool:
    """Coarse evidence presence: any file-backed asset flag or dataviz flag.

    Finer checks (on-topic, quality, provenance, hash) stay in
    validate_asset_descriptor; this gate only refuses the fully
    assetless grounded case before consequential work starts.
    """

    assets = validated_request.get("assets")
    if not isinstance(assets, dict):
        return False
    return bool(
        assets.get("has_official_or_source_image")
        or assets.get("has_usable_screenshot")
        or assets.get("data_visualization_possible")
    )


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
        "visual_requirement": visual_requirement_of(validated_request),
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
    requirement = visual_requirement_of(validated_request)
    if requirement == "SOURCE_GROUNDED" and not visual_evidence_available(validated_request):
        raise BridgeError(
            "PACKAGING_VISUAL_GROUNDING_UNMET: layout declares a visual block but"
            " no usable evidence asset, source screenshot, or data visualization exists"
        )
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
        if field == "visual_requirement":
            continue
        if field not in receipt:
            raise BridgeError(f"PACKAGING_INPUT_INVALID: receipt missing field {field!r}")
    if receipt.get("schema") != SCHEMA or receipt.get("contract_version") != CONTRACT_VERSION:
        raise BridgeError("PACKAGING_INPUT_INVALID: receipt schema/contract mismatch")
    grandfathered = "visual_requirement" not in receipt
    if grandfathered:
        # Grandfather pre-grounding receipts (created before issue
        # #138): their hash covers the fieldless body, so the default
        # is applied without altering the verified bytes. No NEW
        # evaluation can produce a fieldless receipt.
        receipt = dict(receipt)
        receipt["visual_requirement"] = "NONE"
    elif receipt.get("visual_requirement") not in VISUAL_REQUIREMENT_VALUES:
        raise BridgeError("PACKAGING_INPUT_INVALID: receipt visual_requirement unknown")
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    if grandfathered:
        del body["visual_requirement"]
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


def validate_asset_descriptor(descriptor: Any, receipt: dict[str, Any], *, root: Path = WORKSPACE) -> dict[str, Any]:
    """Validate a model-written asset descriptor against the receipt's VISUAL_STYLE.

    Returns the validated descriptor. Kind must equal the receipt's
    implied kind exactly; file-backed kinds must name an existing
    workspace-contained file (sha256 verified when given); provenance
    is required wherever evidence matters; NONE-kind descriptors must
    name no file (no smuggled evidence past typography).
    """

    if not isinstance(descriptor, dict):
        raise BridgeError("PACKAGING_INPUT_INVALID: asset descriptor must be an object")
    if descriptor.get("schema") != ASSET_DESCRIPTOR_SCHEMA:
        raise BridgeError("PACKAGING_INPUT_INVALID: asset descriptor schema mismatch")
    if descriptor.get("candidate_id") != receipt.get("candidate_id"):
        raise BridgeError("PACKAGING_INPUT_INVALID: asset descriptor candidate mismatch")
    style = receipt.get("VISUAL_STYLE")
    expected_kind = STYLE_TO_ASSET_KIND.get(style)
    if expected_kind is None:
        raise BridgeError(f"PACKAGING_INPUT_INVALID: receipt style needs no asset path: {style!r}")
    kind = descriptor.get("asset_kind")
    if kind != expected_kind:
        raise BridgeError(
            f"PACKAGING_ASSET_MISMATCH: receipt needs {expected_kind}, descriptor claims {kind!r}"
        )
    if receipt.get("REAL_PHOTO_REQUIRED") == "YES" and expected_kind not in FILE_BACKED_ASSET_KINDS and style != "DATA_VISUALIZATION":
        raise BridgeError("PACKAGING_ASSET_REQUIREMENT_UNMET: required real photo has no file evidence")
    provenance = descriptor.get("provenance")
    if expected_kind != "NONE" and (not isinstance(provenance, str) or not provenance.strip()):
        raise BridgeError("PACKAGING_INPUT_INVALID: asset provenance required")
    local_path = descriptor.get("local_path")
    if expected_kind in FILE_BACKED_ASSET_KINDS:
        if not isinstance(local_path, str) or not local_path.strip():
            raise BridgeError("PACKAGING_INPUT_INVALID: file-backed asset needs local_path")
        if Path(local_path).is_symlink():
            raise BridgeError("PACKAGING_INPUT_INVALID: asset file must not be a symlink")
        resolved = contained_path(Path(local_path), root)
        if not resolved.is_file():
            raise BridgeError("PACKAGING_INPUT_INVALID: asset file missing or not regular")
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        given_sha = descriptor.get("sha256")
        if given_sha is not None and given_sha != actual:
            raise BridgeError("PACKAGING_ASSET_MISMATCH: asset sha256 mismatch")
        descriptor = dict(descriptor)
        descriptor["local_path"] = str(resolved)
        descriptor["sha256"] = actual
    else:
        if local_path is not None:
            raise BridgeError("PACKAGING_INPUT_INVALID: non-evidence asset must not name a file")
        local_data = descriptor.get("source_url")
        if local_data is not None and not isinstance(local_data, str):
            raise BridgeError("PACKAGING_INPUT_INVALID: source_url must be a string")
    return descriptor


def build_render_record(
    *, candidate_id: str, receipt_hash: str, format_decision: str, asset_kind: str, outputs: list[dict[str, str]]
) -> dict[str, Any]:
    body = {
        "schema": RENDER_RECORD_SCHEMA,
        "candidate_id": candidate_id,
        "receipt_hash": receipt_hash,
        "format": format_decision,
        "asset_kind": asset_kind,
        "outputs": outputs,
    }
    body["record_hash"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return body


def load_render_record(path: Path, *, root: Path = WORKSPACE) -> dict[str, Any]:
    resolved = contained_path(Path(path), root)
    if Path(path).is_symlink() or not resolved.is_file():
        raise BridgeError("PACKAGING_INPUT_INVALID: render record is not a regular file")
    try:
        record = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BridgeError("PACKAGING_INPUT_INVALID: render record is not valid JSON") from e
    if not isinstance(record, dict) or record.get("schema") != RENDER_RECORD_SCHEMA:
        raise BridgeError("PACKAGING_INPUT_INVALID: render record schema mismatch")
    for field in ("candidate_id", "receipt_hash", "format", "asset_kind", "outputs", "record_hash"):
        if field not in record:
            raise BridgeError(f"PACKAGING_INPUT_INVALID: render record missing {field!r}")
    body = {k: v for k, v in record.items() if k != "record_hash"}
    if record.get("record_hash") != hashlib.sha256(canonical_json_bytes(body)).hexdigest():
        raise BridgeError("PACKAGING_RECEIPT_TAMPERED: render record hash mismatch")
    if not isinstance(record["outputs"], list) or not record["outputs"]:
        raise BridgeError("PACKAGING_INPUT_INVALID: render record has no outputs")
    for entry in record["outputs"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(
            entry.get("sha256"), str
        ):
            raise BridgeError("PACKAGING_INPUT_INVALID: render record output malformed")
    return record
