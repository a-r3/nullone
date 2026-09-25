#!/usr/bin/env python3
"""Deterministic validation/reconciliation core for the NullOne Visual
Director (nullone.visual-decision.v1).

NullOne itself decides the per-post visual format at runtime; Claude Code
is not part of this decision. This module is the deterministic core that
makes that true in code, not just in prompt text:

- `validate_decision` strictly validates one model-authored raw decision
  file against the schema (fail closed on anything malformed, unknown, or
  forbidden -- never guesses a default).
- `packaging_style_directive` maps a validated decision into the ONE
  optional signal the existing, unchanged, heavily-reviewed packaging
  contract (`nullone_packaging_policy.evaluate_packaging`) accepts:
  `candidate.visual_director_style`. That signal is consulted ONLY in the
  branch the packaging contract already reaches when neither a real photo
  nor other visual evidence is required (`VISUAL_EVIDENCE_REQUIRED=NO`) --
  today that branch blindly defaults to EDITORIAL_TYPOGRAPHY or an
  unusable GENERATED_ILLUSTRATION_ALLOWED stub. Everywhere else (a real
  photo/screenshot/data-visualization is required or genuinely available)
  the packaging contract's existing evidence-based ladder remains
  authoritative and unchanged: this module never overrides a hard
  requirement.
- SOURCE_PHOTO / DATA_VISUALIZATION decisions are never routed around
  the packaging contract's own evidence ladder. Choosing SOURCE_PHOTO is
  a DIRECTIVE to acquire and validate a real asset (validated right here,
  by file containment + hash, exactly like `nullone_packaging_receipt
  .validate_asset_descriptor`): if that validation fails, this module
  fails closed rather than silently falling back to typography or
  inventing a placeholder. No network access, no image generation, no
  Zernio/Telegram capability anywhere in this file.

Pure function module except for the local-file containment/hash checks
required to prove a declared asset is real; no network I/O.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_packaging_receipt import check_candidate_id, contained_path

SCHEMA = "nullone.visual-decision.v1"
CONTRACT_VERSION = "1.0.0"

# NullOne's own vocabulary for this contract (distinct from, and mapped
# onto, the packaging contract's REAL_PHOTO/SOURCE_SCREENSHOT/etc.
# tokens -- see docs/contracts/visual-director-contract-v1.md).
VISUAL_STYLES = frozenset(
    {"SOURCE_PHOTO", "BRANDED_GRAPHIC", "DATA_VISUALIZATION", "EDITORIAL_TYPOGRAPHY"}
)

# Styles this module may hand to the packaging contract as
# `candidate.visual_director_style`: exactly the two that carry no
# file-backed evidence claim. SOURCE_PHOTO/DATA_VISUALIZATION are never
# forwarded -- the packaging contract's existing evidence ladder is
# authoritative for those (see module docstring).
PACKAGING_DIRECTIVE_STYLES = frozenset({"BRANDED_GRAPHIC", "EDITORIAL_TYPOGRAPHY"})

# Evidence-backed styles that require a validated local asset file.
EVIDENCE_STYLES = frozenset({"SOURCE_PHOTO", "DATA_VISUALIZATION"})

SOURCE_ASSET_TYPES = frozenset(
    {"official_photo", "official_screenshot", "product_ui", "official_graphic"}
)

REASON_CODES = frozenset(
    {
        "SOURCE_PHOTO_STRONG_PRIMARY_MATCH",
        "BRANDED_GRAPHIC_ABSTRACT_NO_SOURCE",
        "BRANDED_GRAPHIC_AVOIDS_TYPOGRAPHY_REPETITION",
        "DATA_VISUALIZATION_CENTRAL_METRIC",
        "EDITORIAL_TYPOGRAPHY_DELIBERATE_FALLBACK",
        "EDITORIAL_TYPOGRAPHY_SUFFICIENT_CONTENT",
        "FEED_RHYTHM_AVOIDS_REPETITION",
        "TOPIC_SUITABILITY_OVERRIDES_RHYTHM",
    }
)

REASON_TEXT: dict[str, str] = {
    "SOURCE_PHOTO_STRONG_PRIMARY_MATCH": "A verified primary-source visual materially helps explain the story.",
    "BRANDED_GRAPHIC_ABSTRACT_NO_SOURCE": "Abstract story, no strong source image; a NullOne motif meaningfully supports it.",
    "BRANDED_GRAPHIC_AVOIDS_TYPOGRAPHY_REPETITION": "Recent feed rhythm leaned on typography; this story reads better with the branded-graphic treatment.",
    "DATA_VISUALIZATION_CENTRAL_METRIC": "Structured numeric content is central and can be represented faithfully.",
    "EDITORIAL_TYPOGRAPHY_DELIBERATE_FALLBACK": "Deliberate editorial typography choice with enough structured content to stand alone.",
    "EDITORIAL_TYPOGRAPHY_SUFFICIENT_CONTENT": "Typography carries the story on its own merits; no evidence or motif adds value here.",
    "FEED_RHYTHM_AVOIDS_REPETITION": "Recent published posts repeated one visual format; this choice restores variety.",
    "TOPIC_SUITABILITY_OVERRIDES_RHYTHM": "Recent feed rhythm suggested variety, but topic suitability outweighs it here.",
}

# Free-text fields the model must never submit: this contract persists
# concise, typed reason codes, never chain-of-thought/rationale prose.
FORBIDDEN_FIELDS = frozenset({"reasoning", "chain_of_thought", "rationale", "notes", "explanation"})

REQUIRED_RAW_FIELDS = (
    "schema",
    "contract_version",
    "candidate_id",
    "visual_style",
    "source_asset_required",
    "source_asset_url",
    "source_asset_type",
    "source_provenance",
    "local_path",
    "sha256",
    "headline",
    "deck",
    "stat",
    "visual_motif",
    "decision_reason_code",
    "recent_feed_context_used",
)

MAX_TEXT_LEN = 200
MAX_LONG_TEXT_LEN = 320


class VisualDirectorError(BridgeError):
    """Malformed/contradictory visual decision. Always fail closed."""


def _fail(message: str) -> None:
    raise VisualDirectorError(f"VISUAL_DECISION_INVALID: {message}")


def _opt_str(raw: dict[str, Any], field: str, *, max_len: int) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string or null")
    if len(value) > max_len:
        _fail(f"{field} exceeds max length {max_len}")
    return value


def validate_decision(raw: Any, *, root: Path = WORKSPACE) -> dict[str, Any]:
    """Strictly validate one raw `nullone.visual-decision.v1` document.

    Returns the validated/normalized decision (no hash yet). Raises
    VisualDirectorError on anything malformed, unknown, contradictory, or
    forbidden -- this function never silently defaults or repairs input.
    """

    if not isinstance(raw, dict):
        _fail("decision must be an object")

    forbidden_present = sorted(FORBIDDEN_FIELDS & raw.keys())
    if forbidden_present:
        _fail(f"forbidden free-text field(s) present: {forbidden_present}")

    missing = [f for f in REQUIRED_RAW_FIELDS if f not in raw]
    if missing:
        _fail(f"missing field(s): {missing}")

    if raw.get("schema") != SCHEMA:
        _fail("schema mismatch")
    if raw.get("contract_version") != CONTRACT_VERSION:
        _fail("contract_version mismatch")

    candidate_id = check_candidate_id(raw.get("candidate_id"))

    visual_style = raw.get("visual_style")
    if visual_style not in VISUAL_STYLES:
        _fail(f"visual_style must be one of {sorted(VISUAL_STYLES)}")

    decision_reason_code = raw.get("decision_reason_code")
    if decision_reason_code not in REASON_CODES:
        _fail(f"decision_reason_code must be one of {sorted(REASON_CODES)}")

    recent_feed_context_used = raw.get("recent_feed_context_used")
    if not isinstance(recent_feed_context_used, bool):
        _fail("recent_feed_context_used must be a boolean")

    source_asset_required = raw.get("source_asset_required")
    if not isinstance(source_asset_required, bool):
        _fail("source_asset_required must be a boolean")

    headline = raw.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        _fail("headline must be a non-empty string")
    if len(headline) > MAX_TEXT_LEN:
        _fail(f"headline exceeds max length {MAX_TEXT_LEN}")

    deck = _opt_str(raw, "deck", max_len=MAX_LONG_TEXT_LEN)
    stat = _opt_str(raw, "stat", max_len=MAX_TEXT_LEN)
    visual_motif = _opt_str(raw, "visual_motif", max_len=MAX_TEXT_LEN)
    source_provenance = _opt_str(raw, "source_provenance", max_len=MAX_LONG_TEXT_LEN)
    source_asset_url = _opt_str(raw, "source_asset_url", max_len=MAX_LONG_TEXT_LEN)
    local_path_raw = raw.get("local_path")
    sha_raw = raw.get("sha256")

    source_asset_type = raw.get("source_asset_type")
    if source_asset_type is not None and source_asset_type not in SOURCE_ASSET_TYPES:
        _fail(f"source_asset_type must be one of {sorted(SOURCE_ASSET_TYPES)} or null")

    validated: dict[str, Any] = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "candidate_id": candidate_id,
        "visual_style": visual_style,
        "decision_reason_code": decision_reason_code,
        "recent_feed_context_used": recent_feed_context_used,
        "headline": headline,
        "deck": deck,
        "stat": stat,
        "visual_motif": visual_motif,
    }

    if visual_style in EVIDENCE_STYLES:
        # Evidence-backed styles are a DIRECTIVE to acquire and validate a
        # real asset -- never a claim that may go unverified. Requirement 7:
        # "no silent fallback to random web search" / "no empty SOURCE_PHOTO
        # card" is enforced here by requiring an already-downloaded,
        # hash-verified local file, exactly like the packaging contract's
        # own asset descriptor. A bare remote URL is never sufficient.
        if not source_asset_required:
            _fail(f"{visual_style} requires source_asset_required=true")
        if source_asset_url is None:
            _fail(f"{visual_style} requires a non-empty source_asset_url")
        if source_provenance is None:
            _fail(f"{visual_style} requires a non-empty source_provenance")
        if visual_style == "SOURCE_PHOTO" and source_asset_type is None:
            _fail("SOURCE_PHOTO requires a non-null source_asset_type")
        if not isinstance(local_path_raw, str) or not local_path_raw.strip():
            _fail(f"{visual_style} requires local_path naming an already-validated file")
        if Path(local_path_raw).is_symlink():
            _fail("local_path must not be a symlink")
        resolved = contained_path(Path(local_path_raw), root)
        if not resolved.is_file():
            _fail("local_path file missing or not a regular file")
        actual_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if sha_raw is not None and sha_raw != actual_sha:
            _fail("sha256 mismatch: declared hash does not match the file on disk")
        validated.update(
            {
                "source_asset_required": True,
                "source_asset_url": source_asset_url,
                "source_asset_type": source_asset_type,
                "source_provenance": source_provenance,
                "local_path": str(resolved),
                "sha256": actual_sha,
            }
        )
    else:
        # BRANDED_GRAPHIC / EDITORIAL_TYPOGRAPHY: no evidence claim is
        # permitted at all -- refuse smuggled evidence past a non-evidence
        # style (mirrors nullone_packaging_receipt.validate_asset_descriptor).
        if source_asset_required:
            _fail(f"{visual_style} must declare source_asset_required=false")
        for field, value in (
            ("source_asset_url", source_asset_url),
            ("source_asset_type", source_asset_type),
            ("source_provenance", source_provenance),
            ("local_path", local_path_raw),
            ("sha256", sha_raw),
        ):
            if value is not None:
                _fail(f"{visual_style} must not name {field}")
        if visual_style == "BRANDED_GRAPHIC" and visual_motif is None:
            _fail("BRANDED_GRAPHIC requires a non-empty visual_motif")
        validated.update(
            {
                "source_asset_required": False,
                "source_asset_url": None,
                "source_asset_type": None,
                "source_provenance": None,
                "local_path": None,
                "sha256": None,
            }
        )

    return validated


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def finalize_decision(validated: dict[str, Any]) -> dict[str, Any]:
    """Add the tamper-evident hash to an already-validated decision."""

    body = dict(validated)
    body["decision_hash"] = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    return body


def canonical_visual_decision_path(candidate_id: str, *, root: Path = WORKSPACE) -> Path:
    check_candidate_id(candidate_id)
    return (root / "social/drafts/production" / f"{candidate_id}-visual-decision.json").resolve()


def load_visual_decision(path: Path, candidate_id: str, *, root: Path = WORKSPACE) -> dict[str, Any]:
    """Load and verify a persisted decision. Any defect fails closed."""

    resolved = contained_path(Path(path), root)
    if Path(path).is_symlink():
        _fail("decision file must not be a symlink")
    if resolved != canonical_visual_decision_path(candidate_id, root=root):
        _fail("decision path is not the canonical candidate decision")
    if not resolved.is_file():
        _fail("decision file missing or not a regular file")
    try:
        decision = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _fail(f"decision is not valid JSON: {e}")
    if not isinstance(decision, dict):
        _fail("decision is not an object")
    body = {k: v for k, v in decision.items() if k != "decision_hash"}
    expected = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    if decision.get("decision_hash") != expected:
        _fail("decision hash mismatch (tampered or hand-edited)")
    if decision.get("candidate_id") != candidate_id:
        _fail("decision candidate_id mismatch")
    return decision


def packaging_style_directive(decision: dict[str, Any]) -> str | None:
    """Map a validated decision onto the packaging contract's one optional
    input signal (`candidate.visual_director_style`).

    Returns None for SOURCE_PHOTO/DATA_VISUALIZATION: those are never
    routed around the packaging contract's own evidence-based ladder (see
    module docstring) -- their only effect is upstream asset acquisition
    that, if it truly succeeded, already shows up truthfully in the
    `assets.*` signals the packaging contract already consumes.
    """

    style = decision.get("visual_style")
    if style not in VISUAL_STYLES:
        _fail("cannot derive a packaging directive from an unvalidated decision")
    if style in PACKAGING_DIRECTIVE_STYLES:
        return style
    return None


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "social/drafts/production").mkdir(parents=True)
        asset = root / "social/drafts/production/asset.jpg"
        asset.write_bytes(b"fake-jpeg-bytes")
        asset_sha = hashlib.sha256(asset.read_bytes()).hexdigest()

        typography = {
            "schema": SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "candidate_id": "probe",
            "visual_style": "EDITORIAL_TYPOGRAPHY",
            "source_asset_required": False,
            "source_asset_url": None,
            "source_asset_type": None,
            "source_provenance": None,
            "local_path": None,
            "sha256": None,
            "headline": "Amazon Seller Central-a Claude qoşulur",
            "deck": None,
            "stat": "60 saniyə",
            "visual_motif": None,
            "decision_reason_code": "EDITORIAL_TYPOGRAPHY_SUFFICIENT_CONTENT",
            "recent_feed_context_used": True,
        }
        validated = validate_decision(typography, root=root)
        assert packaging_style_directive(validated) == "EDITORIAL_TYPOGRAPHY"
        final = finalize_decision(validated)
        assert final["decision_hash"]

        branded = dict(typography, visual_style="BRANDED_GRAPHIC", visual_motif="signal_accent",
                        decision_reason_code="BRANDED_GRAPHIC_ABSTRACT_NO_SOURCE")
        validated_branded = validate_decision(branded, root=root)
        assert packaging_style_directive(validated_branded) == "BRANDED_GRAPHIC"

        source_photo = dict(
            typography,
            visual_style="SOURCE_PHOTO",
            source_asset_required=True,
            source_asset_url="https://www.aboutamazon.com/news/example",
            source_asset_type="official_screenshot",
            source_provenance="Official Amazon Seller Central announcement page",
            local_path=str(asset),
            sha256=asset_sha,
            decision_reason_code="SOURCE_PHOTO_STRONG_PRIMARY_MATCH",
        )
        validated_photo = validate_decision(source_photo, root=root)
        assert packaging_style_directive(validated_photo) is None
        assert validated_photo["sha256"] == asset_sha

        # Fail-closed cases.
        for bad, expect in (
            (dict(typography, schema="wrong"), "schema mismatch"),
            (dict(typography, visual_style="MADE_UP"), "visual_style"),
            (dict(typography, reasoning="because I said so"), "forbidden"),
            (dict(source_photo, local_path=None), "local_path"),
            (dict(source_photo, sha256="0" * 64), "sha256 mismatch"),
            (dict(branded, source_asset_required=True), "source_asset_required"),
            (dict(typography, headline=""), "headline"),
        ):
            try:
                validate_decision(bad, root=root)
            except VisualDirectorError as e:
                assert expect in str(e), (expect, e)
            else:
                raise AssertionError(f"expected fail-closed for case expecting {expect!r}")

    print("VISUAL_DIRECTOR_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
