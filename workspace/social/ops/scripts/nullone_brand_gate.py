#!/usr/bin/env python3
"""Deterministic, template-aware Visual V2 brand-compliance gate.

Reads a renderer's own semantic brand-metadata sidecar
(schema `nullone.render-brand-metadata.v1`, written by
`render_texbrif_v2.py`) and decides PASS/BLOCKED for a SINGLE_POST render
before it may proceed to the manifest / Zernio draft / Telegram path.

Deliberately reads renderer-reported facts rather than scanning rendered
pixels: the renderer is the only component that actually knows what
color it used for the stat, what margin it drew to, how many brand marks
it placed, and at what opacity, or how much of the typography band it
actually filled.

Template-aware (docs/contracts/visual-director-contract-v1.md section
10): one check SET applies to every render (brand identity is style-
independent -- margin, exactly one low-opacity bottom-right mark, valid
text bounds), and each VISUAL_STYLE adds its own required checks on top.
Earlier revision of this gate only ever blocked a stat-bearing render
(`gate = BLOCKED if (stat_present and failed) else PASS`): a bare
headline-only render, or a headline+stat render whose canvas was mostly
empty, could satisfy every structural check and still pass. That gap is
closed here: every check in a style's required set gates the render,
regardless of whether a stat is present, and EDITORIAL_TYPOGRAPHY /
BRANDED_GRAPHIC additionally require CONTENT_COVERAGE_SUFFICIENT so a
sparse headline+stat card over an otherwise-empty canvas (the exact
2026-09-25 production incident) fails closed instead of reaching Zernio.

Pure function module: no I/O, no network, no Zernio/Telegram capability
anywhere in this file.
"""
from __future__ import annotations

from typing import Any

SCHEMA = "nullone.render-brand-metadata.v1"
REQUIRED_ACCENT = [253, 69, 3]  # #FD4503, social/references/visual-rules.md
MIN_MARGIN_PX = 90

# See render_texbrif_v2.MIN_TYPOGRAPHY_COVERAGE -- kept as an independent
# constant here (rather than imported) because the gate must be
# evaluable from the metadata sidecar alone, with no renderer import.
MIN_TYPOGRAPHY_COVERAGE = 0.45

VISUAL_STYLES = frozenset(
    {"REAL_PHOTO", "SOURCE_SCREENSHOT", "DATA_VISUALIZATION", "EDITORIAL_TYPOGRAPHY", "BRANDED_GRAPHIC"}
)

# Checks required for every style, regardless of stat presence.
BASE_CHECKS = (
    "STAT_ACCENT_COMPLIANT",
    "MIN_MARGIN_90PX",
    "BRAND_MARK_COUNT_EXACTLY_ONE",
    "BRAND_MARK_BOTTOM_RIGHT",
    "BRAND_MARK_LOW_OPACITY",
    "TEXT_BOUNDS_VALID",
)

# Per-style checks added on top of BASE_CHECKS.
STYLE_EXTRA_CHECKS: dict[str, tuple[str, ...]] = {
    "REAL_PHOTO": ("VISUAL_REGION_PRESENT",),
    "SOURCE_SCREENSHOT": ("VISUAL_REGION_PRESENT",),
    "DATA_VISUALIZATION": ("VISUAL_REGION_PRESENT",),
    "EDITORIAL_TYPOGRAPHY": ("CONTENT_COVERAGE_SUFFICIENT",),
    "BRANDED_GRAPHIC": ("CONTENT_COVERAGE_SUFFICIENT",),
}

REQUIRED_METADATA_FIELDS = (
    "visual_style",
    "has_photo",
    "stat_present",
    "stat_color",
    "margin_px",
    "brand_mark_count",
    "brand_mark_position",
    "brand_mark_opacity",
    "text_bounds_valid",
    "content_coverage_ratio",
)


class BrandGateError(RuntimeError):
    """Malformed/missing brand metadata. Fails closed, never guesses."""


def _required_checks(visual_style: str) -> tuple[str, ...]:
    return BASE_CHECKS + STYLE_EXTRA_CHECKS[visual_style]


def evaluate_brand_gate(metadata: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one renderer brand-metadata sidecar against the gate.

    Returns a dict with BRAND_GATE ("PASS"/"BLOCKED"), BRAND_GATE_REASON
    (comma-joined failed check names, or None), VISUAL_STYLE, and the
    individual per-check booleans. Raises BrandGateError on malformed
    input rather than silently defaulting to PASS.
    """
    if not isinstance(metadata, dict) or metadata.get("schema") != SCHEMA:
        raise BrandGateError("BRAND_METADATA_INVALID: missing/unknown schema")

    missing = [f for f in REQUIRED_METADATA_FIELDS if f not in metadata]
    if missing:
        raise BrandGateError(f"BRAND_METADATA_INVALID: missing field(s) {missing}")

    visual_style = metadata["visual_style"]
    if visual_style not in VISUAL_STYLES:
        raise BrandGateError(f"BRAND_METADATA_INVALID: unknown visual_style {visual_style!r}")

    stat_present = bool(metadata["stat_present"])
    has_photo = bool(metadata["has_photo"])
    coverage = metadata["content_coverage_ratio"]

    checks = {
        # Vacuously compliant when there is no stat to color.
        "STAT_ACCENT_COMPLIANT": (
            (not stat_present) or metadata["stat_color"] == REQUIRED_ACCENT
        ),
        "MIN_MARGIN_90PX": (
            isinstance(metadata["margin_px"], int)
            and metadata["margin_px"] >= MIN_MARGIN_PX
        ),
        "BRAND_MARK_COUNT_EXACTLY_ONE": metadata["brand_mark_count"] == 1,
        "BRAND_MARK_BOTTOM_RIGHT": metadata["brand_mark_position"] == "bottom_right",
        "BRAND_MARK_LOW_OPACITY": (
            isinstance(metadata["brand_mark_opacity"], int)
            and 0 <= metadata["brand_mark_opacity"] < 255
        ),
        "TEXT_BOUNDS_VALID": metadata["text_bounds_valid"] is True,
        # SOURCE_PHOTO/SOURCE_SCREENSHOT/DATA_VISUALIZATION: the renderer
        # must actually have composited a hero visual region, not a
        # typography layout mislabeled with an evidence style.
        "VISUAL_REGION_PRESENT": has_photo,
        # EDITORIAL_TYPOGRAPHY/BRANDED_GRAPHIC: the typography band must
        # not read as an empty canvas (2026-09-25 production incident).
        "CONTENT_COVERAGE_SUFFICIENT": (
            isinstance(coverage, (int, float)) and coverage >= MIN_TYPOGRAPHY_COVERAGE
        ),
    }

    required = _required_checks(visual_style)
    failed = [name for name in required if not checks[name]]

    gate = "BLOCKED" if failed else "PASS"

    return {
        "BRAND_GATE": gate,
        "BRAND_GATE_REASON": ",".join(failed) if failed else None,
        "VISUAL_STYLE": visual_style,
        "checks": {name: checks[name] for name in required},
    }


def self_test() -> int:
    compliant_typography = {
        "schema": SCHEMA,
        "visual_style": "EDITORIAL_TYPOGRAPHY",
        "has_photo": False,
        "stat_present": True,
        "stat_color": REQUIRED_ACCENT,
        "margin_px": 90,
        "brand_mark_count": 1,
        "brand_mark_position": "bottom_right",
        "brand_mark_opacity": 170,
        "text_bounds_valid": True,
        "content_coverage_ratio": 0.6,
    }
    result = evaluate_brand_gate(compliant_typography)
    assert result["BRAND_GATE"] == "PASS", result
    assert result["BRAND_GATE_REASON"] is None, result

    # The exact 2026-09-25 production incident shape: short headline +
    # short stat, huge empty canvas -- must now BLOCK.
    sparse = dict(compliant_typography, content_coverage_ratio=0.34)
    result = evaluate_brand_gate(sparse)
    assert result["BRAND_GATE"] == "BLOCKED", result
    assert "CONTENT_COVERAGE_SUFFICIENT" in result["BRAND_GATE_REASON"], result

    # A branded-graphic render with the motif filling the band passes.
    branded = dict(
        compliant_typography, visual_style="BRANDED_GRAPHIC", content_coverage_ratio=0.96
    )
    assert evaluate_brand_gate(branded)["BRAND_GATE"] == "PASS"

    # A real-photo render needs has_photo=True regardless of coverage
    # (coverage is not computed/meaningful for photo-backed styles).
    photo = dict(
        compliant_typography,
        visual_style="REAL_PHOTO",
        has_photo=True,
        content_coverage_ratio=None,
    )
    assert evaluate_brand_gate(photo)["BRAND_GATE"] == "PASS"
    mislabeled = dict(photo, has_photo=False)
    result = evaluate_brand_gate(mislabeled)
    assert result["BRAND_GATE"] == "BLOCKED"
    assert "VISUAL_REGION_PRESENT" in result["BRAND_GATE_REASON"], result

    data_viz = dict(compliant_typography, visual_style="DATA_VISUALIZATION", has_photo=True)
    assert evaluate_brand_gate(data_viz)["BRAND_GATE"] == "PASS"

    # Structural failures now block regardless of stat presence (the old
    # vacuous-pass-without-a-stat loophole is gone).
    no_stat_bad_margin = dict(
        compliant_typography, stat_present=False, stat_color=None, margin_px=70
    )
    result = evaluate_brand_gate(no_stat_bad_margin)
    assert result["BRAND_GATE"] == "BLOCKED", (
        "structural failures must block regardless of stat presence",
        result,
    )
    assert "MIN_MARGIN_90PX" in result["BRAND_GATE_REASON"], result

    for field, bad_value, reason in (
        ("stat_color", [220, 220, 220], "STAT_ACCENT_COMPLIANT"),
        ("margin_px", 70, "MIN_MARGIN_90PX"),
        ("brand_mark_count", 2, "BRAND_MARK_COUNT_EXACTLY_ONE"),
        ("brand_mark_position", "top_left", "BRAND_MARK_BOTTOM_RIGHT"),
        ("brand_mark_opacity", 255, "BRAND_MARK_LOW_OPACITY"),
        ("text_bounds_valid", False, "TEXT_BOUNDS_VALID"),
    ):
        bad = dict(compliant_typography, **{field: bad_value})
        result = evaluate_brand_gate(bad)
        assert result["BRAND_GATE"] == "BLOCKED", (field, result)
        assert reason in result["BRAND_GATE_REASON"], (field, result)

    try:
        evaluate_brand_gate({"schema": "wrong-schema"})
    except BrandGateError:
        pass
    else:
        raise AssertionError("invalid metadata schema must raise, not default to PASS")

    try:
        evaluate_brand_gate({"schema": SCHEMA})
    except BrandGateError:
        pass
    else:
        raise AssertionError("missing fields must raise, not default to PASS")

    try:
        evaluate_brand_gate(dict(compliant_typography, visual_style="MADE_UP"))
    except BrandGateError:
        pass
    else:
        raise AssertionError("unknown visual_style must raise, not default to PASS")

    print("BRAND_GATE_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
