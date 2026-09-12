#!/usr/bin/env python3
"""Deterministic editorial packaging decision function.

Implements docs/contracts/editorial-packaging-contract-v1.md exactly:
given one already-scored, already-READY candidate's structural/asset
signals, decide whether it should be posted at all, and if so whether it
should be packaged as SINGLE_POST, CAROUSEL, or STORY, and what visual
evidence that packaging requires.

This module is a pure function over its input dict. It performs no I/O,
no network access, no LLM calls, no candidate scoring/discovery, and has
no capability to create drafts, render media, send notifications, or
publish anything. It does not decide editorial scoring, cadence/quota,
breaking-routing, or verification -- those remain upstream inputs this
module consumes (see the contract's "Relationship to existing repository
material").

Reading real candidate/board state into the shape this module consumes
is a separate, later concern -- this module is not wired into any live
production workflow.
"""

from __future__ import annotations

from typing import Any

SCHEMA = "nullone.packaging-contract.v1"
CONTRACT_VERSION = "1.0.0"

MIN_CAROUSEL_BEATS = 3
CAROUSEL_SLIDE_FLOOR = 4
CAROUSEL_SLIDE_TARGET_CAP = 8
CAROUSEL_BEAT_TRIM_THRESHOLD = 6
CAROUSEL_SLIDE_OVERHEAD = 2  # cover + final/takeaway, never a counted beat

CONTENT_TYPES = frozenset(
    {"NEWS", "BREAKING", "EXPLAINER", "PRACTICAL", "COMPARISON", "AZ_CONTEXT", "EVERGREEN"}
)

CONTENT_SHAPES = frozenset(
    {
        "SINGLE_FACT",
        "ANNOUNCEMENT",
        "MULTI_STEP_EXPLAINER",
        "COMPARISON",
        "ROUNDUP",
        "BREAKING_DEVELOPING",
        "OPINION_ANALYSIS",
    }
)

CAROUSEL_NEVER_SHAPES = frozenset({"SINGLE_FACT", "BREAKING_DEVELOPING"})

TIMELINESS_VALUES = frozenset({"BREAKING", "TODAY", "THIS_WEEK", "DURABLE"})
VERIFICATION_VALUES = frozenset({"PASS", "BLOCKED"})
SOURCE_GROUNDING_VALUES = frozenset(
    {"STRONG_PRIMARY", "SECONDARY_CORROBORATED", "WEAK_UNCONFIRMED"}
)
AUDIENCE_VALUE_VALUES = frozenset({"LOW", "MEDIUM", "HIGH"})

ASSET_STRENGTH_VALUES = frozenset({"STRONG_OFFICIAL", "MODERATE", "WEAK", "NONE"})
TEXT_DENSITY_VALUES = frozenset({"LOW", "MEDIUM", "HIGH"})
FORMAT_DECISIONS = frozenset({"SINGLE_POST", "CAROUSEL", "STORY", "SKIP"})
POST_DECISIONS = frozenset({"POST", "SKIP"})
VISUAL_STYLES = frozenset(
    {
        "REAL_PHOTO",
        "SOURCE_SCREENSHOT",
        "DATA_VISUALIZATION",
        "EDITORIAL_TYPOGRAPHY",
        "GENERATED_ILLUSTRATION_ALLOWED",
        "GENERATED_ILLUSTRATION_FORBIDDEN",
        "NONE",
    }
)

REASON_CODES = frozenset(
    {
        "VERIFICATION_BLOCKED",
        "WEAK_SOURCE_GROUNDING",
        "LOW_AUDIENCE_VALUE",
        "REAL_PHOTO_REQUIRED_NO_FALLBACK",
        "SINGLE_FACT_FITS_SINGLE_POST",
        "BREAKING_DEVELOPING_PREFERS_STORY",
        "DEVELOPING_STORY_PREFERS_EPHEMERAL_FORMAT",
        "BREAKING_CONFIRMED_FITS_SINGLE_POST",
        "MULTI_FEATURE_ANNOUNCEMENT_JUSTIFIES_CAROUSEL",
        "SINGLE_PAYLOAD_ANNOUNCEMENT",
        "MULTI_ITEM_COMPARISON_JUSTIFIES_CAROUSEL",
        "TWO_ITEM_COMPARISON_FITS_STORY",
        "ROUNDUP_JUSTIFIES_CAROUSEL",
        "MULTI_BEAT_EXPLAINER_JUSTIFIES_CAROUSEL",
        "INSUFFICIENT_DISTINCT_BEATS",
        "BREAKING_TIMELINESS_FORBIDS_CAROUSEL",
    }
)

REASON_TEXT: dict[str, str] = {
    "VERIFICATION_BLOCKED": "VERIFICATION is not PASS; this contract never overrides that gate.",
    "WEAK_SOURCE_GROUNDING": "Overall source grounding is WEAK_UNCONFIRMED.",
    "LOW_AUDIENCE_VALUE": "Estimated audience value is LOW and the item is not a genuinely developing breaking story.",
    "REAL_PHOTO_REQUIRED_NO_FALLBACK": "A real photo was required, none is available, and no screenshot or faithful data visualization exists.",
    "SINGLE_FACT_FITS_SINGLE_POST": "One fact, one number: a single durable post carries it without padding.",
    "BREAKING_DEVELOPING_PREFERS_STORY": "Breaking and still developing: an ephemeral format is safer than committing to the permanent grid.",
    "DEVELOPING_STORY_PREFERS_EPHEMERAL_FORMAT": "The event is still developing; Story is lower-commitment than a permanent feed post.",
    "BREAKING_CONFIRMED_FITS_SINGLE_POST": "Breaking but confirmed/stable: fits the durable single-post breaking-brief template.",
    "MULTI_FEATURE_ANNOUNCEMENT_JUSTIFIES_CAROUSEL": "The announcement bundles enough distinct features/changes to justify multiple slides.",
    "SINGLE_PAYLOAD_ANNOUNCEMENT": "The announcement has a single primary payload; no second slide would add material understanding.",
    "MULTI_ITEM_COMPARISON_JUSTIFIES_CAROUSEL": "Three or more genuinely comparable items justify a multi-slide comparison.",
    "TWO_ITEM_COMPARISON_FITS_STORY": "Only two comparable items; Story's comparison layout carries this without padding to a carousel.",
    "ROUNDUP_JUSTIFIES_CAROUSEL": "Enough distinct roundup items exist to justify a multi-slide digest.",
    "MULTI_BEAT_EXPLAINER_JUSTIFIES_CAROUSEL": "Enough distinct, source-grounded beats exist to justify multiple slides.",
    "INSUFFICIENT_DISTINCT_BEATS": "Fewer than 3 genuinely distinct beats exist; a carousel would pad rather than inform.",
    "BREAKING_TIMELINESS_FORBIDS_CAROUSEL": "Timeliness is BREAKING; carousel production is too slow/heavy for this window.",
}


class PackagingContractError(ValueError):
    """Raised on malformed or unrecognized packaging-contract input."""


def _fail(message: str) -> None:
    raise PackagingContractError(message)


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{field} must be a boolean")
    return value


def _require_non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field} must be a non-negative integer")
    if value < 0:
        _fail(f"{field} must be a non-negative integer")
    return value


def _require_enum(value: Any, allowed: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(f"{field} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _validate_candidate(request: dict[str, Any]) -> dict[str, Any]:
    candidate = _require_dict(request.get("candidate"), "candidate")
    validated = {
        "content_type": _require_enum(
            candidate.get("content_type"), CONTENT_TYPES, "candidate.content_type"
        ),
        "content_shape": _require_enum(
            candidate.get("content_shape"), CONTENT_SHAPES, "candidate.content_shape"
        ),
        "timeliness": _require_enum(
            candidate.get("timeliness"), TIMELINESS_VALUES, "candidate.timeliness"
        ),
        "verification_status": _require_enum(
            candidate.get("verification_status"),
            VERIFICATION_VALUES,
            "candidate.verification_status",
        ),
        "source_grounding": _require_enum(
            candidate.get("source_grounding"),
            SOURCE_GROUNDING_VALUES,
            "candidate.source_grounding",
        ),
        "audience_value": _require_enum(
            candidate.get("audience_value"), AUDIENCE_VALUE_VALUES, "candidate.audience_value"
        ),
        "distinct_beat_count": _require_non_negative_int(
            candidate.get("distinct_beat_count"), "candidate.distinct_beat_count"
        ),
        "depicts_real_world_subject": _require_bool(
            candidate.get("depicts_real_world_subject"),
            "candidate.depicts_real_world_subject",
        ),
        "still_developing": _require_bool(
            candidate.get("still_developing"), "candidate.still_developing"
        ),
    }
    return validated


def _validate_assets(request: dict[str, Any]) -> dict[str, Any]:
    assets = _require_dict(request.get("assets"), "assets")
    fields = (
        "has_official_or_source_image",
        "has_usable_screenshot",
        "image_on_topic",
        "image_quality_ok",
        "data_visualization_possible",
    )
    return {field: _require_bool(assets.get(field), f"assets.{field}") for field in fields}


def _asset_strength(assets: dict[str, Any]) -> str:
    has_image = assets["has_official_or_source_image"] or assets["has_usable_screenshot"]
    on_topic = assets["image_on_topic"]
    quality_ok = assets["image_quality_ok"]

    if assets["has_official_or_source_image"] and on_topic and quality_ok:
        return "STRONG_OFFICIAL"
    if has_image and on_topic:
        return "MODERATE"
    if has_image and not on_topic:
        return "WEAK"
    return "NONE"


def _real_photo_available(assets: dict[str, Any]) -> bool:
    return bool(
        assets["has_official_or_source_image"]
        and assets["image_on_topic"]
        and assets["image_quality_ok"]
    )


def _real_photo_required(candidate: dict[str, Any]) -> bool:
    return (
        candidate["content_shape"] in {"ANNOUNCEMENT", "BREAKING_DEVELOPING"}
        or candidate["depicts_real_world_subject"]
        or candidate["timeliness"] == "BREAKING"
    )


def _visual_evidence_required(candidate: dict[str, Any], real_photo_required: bool) -> bool:
    return real_photo_required or candidate["content_shape"] in {"COMPARISON", "ANNOUNCEMENT"}


def _text_density(distinct_beat_count: int) -> str:
    if distinct_beat_count <= 1:
        return "LOW"
    if distinct_beat_count == 2:
        return "MEDIUM"
    return "HIGH"


def _fallback_visual_style(assets: dict[str, Any]) -> str | None:
    """Fallback ladder used when a required real photo is unavailable.

    Returns the resulting VISUAL_STYLE, or None if no fallback exists
    (caller must SKIP in that case -- never falls through to synthetic
    illustration).
    """
    if assets["has_usable_screenshot"] and assets["image_on_topic"]:
        return "SOURCE_SCREENSHOT"
    if assets["data_visualization_possible"]:
        return "DATA_VISUALIZATION"
    return None


def _carousel_allowed(candidate: dict[str, Any]) -> bool:
    if candidate["content_shape"] in CAROUSEL_NEVER_SHAPES:
        return False
    if candidate["timeliness"] == "BREAKING":
        return False
    if candidate["distinct_beat_count"] < MIN_CAROUSEL_BEATS:
        return False
    return True


def _slide_count_recommendation(distinct_beat_count: int) -> int:
    beats = min(distinct_beat_count, CAROUSEL_BEAT_TRIM_THRESHOLD)
    slides = beats + CAROUSEL_SLIDE_OVERHEAD
    return max(CAROUSEL_SLIDE_FLOOR, min(slides, CAROUSEL_SLIDE_TARGET_CAP))


def _decide_format(candidate: dict[str, Any]) -> tuple[str, str]:
    """Return (FORMAT_DECISION, FORMAT_REASON) once no SKIP gate fired."""

    shape = candidate["content_shape"]
    timeliness = candidate["timeliness"]
    beats = candidate["distinct_beat_count"]
    breaking = timeliness == "BREAKING"
    carousel_ok = _carousel_allowed(candidate)

    if shape == "SINGLE_FACT":
        if breaking and candidate["still_developing"]:
            return "STORY", "BREAKING_DEVELOPING_PREFERS_STORY"
        return "SINGLE_POST", "SINGLE_FACT_FITS_SINGLE_POST"

    if shape == "BREAKING_DEVELOPING":
        if candidate["still_developing"]:
            return "STORY", "DEVELOPING_STORY_PREFERS_EPHEMERAL_FORMAT"
        return "SINGLE_POST", "BREAKING_CONFIRMED_FITS_SINGLE_POST"

    if shape == "ANNOUNCEMENT":
        if carousel_ok and beats >= MIN_CAROUSEL_BEATS:
            return "CAROUSEL", "MULTI_FEATURE_ANNOUNCEMENT_JUSTIFIES_CAROUSEL"
        if beats <= 1:
            return "SINGLE_POST", "SINGLE_PAYLOAD_ANNOUNCEMENT"
        if breaking:
            return "STORY", "BREAKING_TIMELINESS_FORBIDS_CAROUSEL"
        return "SINGLE_POST", "SINGLE_PAYLOAD_ANNOUNCEMENT"

    if shape == "COMPARISON":
        if carousel_ok and beats >= MIN_CAROUSEL_BEATS:
            return "CAROUSEL", "MULTI_ITEM_COMPARISON_JUSTIFIES_CAROUSEL"
        if breaking and beats >= MIN_CAROUSEL_BEATS:
            return "STORY", "BREAKING_TIMELINESS_FORBIDS_CAROUSEL"
        return "STORY", "TWO_ITEM_COMPARISON_FITS_STORY"

    if shape == "ROUNDUP":
        if carousel_ok and beats >= MIN_CAROUSEL_BEATS:
            return "CAROUSEL", "ROUNDUP_JUSTIFIES_CAROUSEL"
        if breaking and beats >= MIN_CAROUSEL_BEATS:
            return "STORY", "BREAKING_TIMELINESS_FORBIDS_CAROUSEL"
        return "SINGLE_POST", "INSUFFICIENT_DISTINCT_BEATS"

    if shape == "MULTI_STEP_EXPLAINER":
        if carousel_ok and beats >= MIN_CAROUSEL_BEATS:
            return "CAROUSEL", "MULTI_BEAT_EXPLAINER_JUSTIFIES_CAROUSEL"
        if breaking:
            return "STORY", "BREAKING_TIMELINESS_FORBIDS_CAROUSEL"
        return "SINGLE_POST", "INSUFFICIENT_DISTINCT_BEATS"

    # OPINION_ANALYSIS
    if carousel_ok and beats >= MIN_CAROUSEL_BEATS:
        return "CAROUSEL", "MULTI_BEAT_EXPLAINER_JUSTIFIES_CAROUSEL"
    return "SINGLE_POST", "INSUFFICIENT_DISTINCT_BEATS"


def _resolve_visual_style(
    assets: dict[str, Any], visual_evidence_required: bool
) -> str:
    strength = _asset_strength(assets)
    if strength == "STRONG_OFFICIAL" and assets["has_official_or_source_image"]:
        return "REAL_PHOTO"
    if strength in {"STRONG_OFFICIAL", "MODERATE"} and assets["has_usable_screenshot"]:
        return "SOURCE_SCREENSHOT"
    if assets["data_visualization_possible"]:
        return "DATA_VISUALIZATION"
    if not visual_evidence_required:
        return "EDITORIAL_TYPOGRAPHY" if strength == "NONE" else "GENERATED_ILLUSTRATION_ALLOWED"
    return "GENERATED_ILLUSTRATION_ALLOWED"


def evaluate_packaging(request: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one nullone.packaging-contract.v1 request.

    Pure function: does not mutate `request`, performs no I/O, and has
    no side effects. Returns a nullone.packaging-contract.v1 response
    dict. Raises PackagingContractError on malformed/unsupported input
    rather than silently defaulting.
    """

    request = _require_dict(request, "request")
    candidate = _validate_candidate(request)
    assets = _validate_assets(request)

    asset_strength = _asset_strength(assets)
    real_photo_available = _real_photo_available(assets)
    real_photo_required = _real_photo_required(candidate)
    visual_evidence_required = _visual_evidence_required(candidate, real_photo_required)
    text_density = _text_density(candidate["distinct_beat_count"])

    def _skip(reason_code: str) -> dict[str, Any]:
        return _build_response(
            candidate=candidate,
            asset_strength=asset_strength,
            real_photo_available=real_photo_available,
            real_photo_required=real_photo_required,
            visual_evidence_required=visual_evidence_required,
            text_density=text_density,
            post_decision="SKIP",
            format_decision="SKIP",
            format_reason=reason_code,
            visual_style="NONE",
            slide_count_recommendation=None,
        )

    # 0. Verification gate.
    if candidate["verification_status"] != "PASS":
        return _skip("VERIFICATION_BLOCKED")

    # 1. Source-grounding gate.
    if candidate["source_grounding"] == "WEAK_UNCONFIRMED":
        return _skip("WEAK_SOURCE_GROUNDING")

    # 2. Audience-value gate.
    if candidate["audience_value"] == "LOW" and candidate["content_shape"] != "BREAKING_DEVELOPING":
        return _skip("LOW_AUDIENCE_VALUE")

    # 3/4/5. Real-photo / visual-evidence requirement with fallback ladder.
    # real_photo_available is exactly the STRONG_OFFICIAL condition, so
    # reaching this branch always means asset_strength != STRONG_OFFICIAL.
    forced_visual_style: str | None = None
    if (real_photo_required or visual_evidence_required) and not real_photo_available:
        fallback = _fallback_visual_style(assets)
        if fallback is None:
            return _build_response(
                candidate=candidate,
                asset_strength=asset_strength,
                real_photo_available=real_photo_available,
                real_photo_required=real_photo_required,
                visual_evidence_required=visual_evidence_required,
                text_density=text_density,
                post_decision="SKIP",
                format_decision="SKIP",
                format_reason="REAL_PHOTO_REQUIRED_NO_FALLBACK",
                visual_style="GENERATED_ILLUSTRATION_FORBIDDEN",
                slide_count_recommendation=None,
            )
        forced_visual_style = fallback

    # 6/7. Format decision.
    format_decision, format_reason = _decide_format(candidate)

    slide_count_recommendation = None
    if format_decision == "CAROUSEL":
        slide_count_recommendation = _slide_count_recommendation(candidate["distinct_beat_count"])

    # 9. Visual-style resolution.
    visual_style = forced_visual_style or _resolve_visual_style(
        assets, visual_evidence_required
    )

    return _build_response(
        candidate=candidate,
        asset_strength=asset_strength,
        real_photo_available=real_photo_available,
        real_photo_required=real_photo_required,
        visual_evidence_required=visual_evidence_required,
        text_density=text_density,
        post_decision="POST",
        format_decision=format_decision,
        format_reason=format_reason,
        visual_style=visual_style,
        slide_count_recommendation=slide_count_recommendation,
    )


def _build_response(
    *,
    candidate: dict[str, Any],
    asset_strength: str,
    real_photo_available: bool,
    real_photo_required: bool,
    visual_evidence_required: bool,
    text_density: str,
    post_decision: str,
    format_decision: str,
    format_reason: str,
    visual_style: str,
    slide_count_recommendation: int | None,
) -> dict[str, Any]:
    assert asset_strength in ASSET_STRENGTH_VALUES
    assert text_density in TEXT_DENSITY_VALUES
    assert post_decision in POST_DECISIONS
    assert format_decision in FORMAT_DECISIONS
    assert format_reason in REASON_CODES
    assert visual_style in VISUAL_STYLES

    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "CONTENT_SHAPE": candidate["content_shape"],
        "ASSET_STRENGTH": asset_strength,
        "REAL_PHOTO_AVAILABLE": "YES" if real_photo_available else "NO",
        "REAL_PHOTO_REQUIRED": "YES" if real_photo_required else "NO",
        "VISUAL_EVIDENCE_REQUIRED": "YES" if visual_evidence_required else "NO",
        "TEXT_DENSITY": text_density,
        "TIMELINESS": candidate["timeliness"],
        "FORMAT_DECISION": format_decision,
        "FORMAT_REASON": format_reason,
        "FORMAT_REASON_TEXT": REASON_TEXT[format_reason],
        "VISUAL_STYLE": visual_style,
        "POST_DECISION": post_decision,
        "slide_count_recommendation": slide_count_recommendation,
        "source_grounding": candidate["source_grounding"],
        "distinct_beat_count": candidate["distinct_beat_count"],
        "content_type": candidate["content_type"],
    }
