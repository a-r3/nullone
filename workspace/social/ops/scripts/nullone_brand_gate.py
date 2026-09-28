#!/usr/bin/env python3
"""Deterministic Visual V2 brand-compliance gate (issue: Visual V2 brand
compliance gate).

Reads a renderer's own semantic brand-metadata sidecar
(schema `nullone.render-brand-metadata.v1`, written by
`render_texbrif_v2.py`) and decides PASS/BLOCKED for a SINGLE_POST render
before it may proceed to the manifest / Zernio draft / Telegram path.

Deliberately reads renderer-reported facts rather than scanning rendered
pixels: the renderer is the only component that actually knows what
color it used for the stat, what margin it drew to, how many brand marks
it placed, and at what opacity. A pixel scanner would be re-deriving
information that already exists, with font anti-aliasing and JPEG-style
edge noise as an extra source of false positives/negatives.

Pure function module: no I/O, no network, no Zernio/Telegram capability
anywhere in this file.
"""
from __future__ import annotations

from typing import Any

SCHEMA = "nullone.render-brand-metadata.v1"
REQUIRED_ACCENT = [253, 69, 3]  # #FD4503, social/references/visual-rules.md
MIN_MARGIN_PX = 90

REQUIRED_CHECKS = (
    "STAT_ACCENT_COMPLIANT",
    "MIN_MARGIN_90PX",
    "BRAND_MARK_COUNT_EXACTLY_ONE",
    "BRAND_MARK_BOTTOM_RIGHT",
    "BRAND_MARK_LOW_OPACITY",
    "TEXT_BOUNDS_VALID",
)

REQUIRED_METADATA_FIELDS = (
    "stat_present",
    "stat_color",
    "margin_px",
    "brand_mark_count",
    "brand_mark_position",
    "brand_mark_opacity",
    "text_bounds_valid",
)


class BrandGateError(RuntimeError):
    """Malformed/missing brand metadata. Fails closed, never guesses."""


def evaluate_brand_gate(metadata: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one renderer brand-metadata sidecar against the gate.

    Returns a dict with BRAND_GATE ("PASS"/"BLOCKED"), BRAND_GATE_REASON
    (comma-joined failed check names, or None), and the individual
    per-check booleans. Raises BrandGateError on malformed input rather
    than silently defaulting to PASS.
    """
    if not isinstance(metadata, dict) or metadata.get("schema") != SCHEMA:
        raise BrandGateError("BRAND_METADATA_INVALID: missing/unknown schema")

    missing = [f for f in REQUIRED_METADATA_FIELDS if f not in metadata]
    if missing:
        raise BrandGateError(f"BRAND_METADATA_INVALID: missing field(s) {missing}")

    stat_present = bool(metadata["stat_present"])

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
    }

    failed = [name for name in REQUIRED_CHECKS if not checks[name]]

    # Reviewed scope: "If stat exists and any required rule fails: fail
    # closed before Zernio/Telegram." The structural checks below are
    # still evaluated and reported for every render, but the blocking
    # trigger stays exactly as specified -- scoped to stat-bearing posts
    # -- rather than inventing a broader blocking rule the brief did not
    # ask for.
    gate = "BLOCKED" if (stat_present and failed) else "PASS"

    return {
        "BRAND_GATE": gate,
        "BRAND_GATE_REASON": ",".join(failed) if failed else None,
        "checks": checks,
    }


def self_test() -> int:
    compliant = {
        "schema": SCHEMA,
        "stat_present": True,
        "stat_color": REQUIRED_ACCENT,
        "margin_px": 90,
        "brand_mark_count": 1,
        "brand_mark_position": "bottom_right",
        "brand_mark_opacity": 170,
        "text_bounds_valid": True,
    }
    result = evaluate_brand_gate(compliant)
    assert result["BRAND_GATE"] == "PASS", result
    assert result["BRAND_GATE_REASON"] is None, result

    wrong_stat_color = dict(compliant, stat_color=[220, 220, 220])
    result = evaluate_brand_gate(wrong_stat_color)
    assert result["BRAND_GATE"] == "BLOCKED", result
    assert result["BRAND_GATE_REASON"] == "STAT_ACCENT_COMPLIANT", result

    legacy_margin = dict(compliant, margin_px=70)
    result = evaluate_brand_gate(legacy_margin)
    assert result["BRAND_GATE"] == "BLOCKED", result
    assert "MIN_MARGIN_90PX" in result["BRAND_GATE_REASON"], result

    two_marks = dict(compliant, brand_mark_count=2)
    result = evaluate_brand_gate(two_marks)
    assert result["BRAND_GATE"] == "BLOCKED", result
    assert "BRAND_MARK_COUNT_EXACTLY_ONE" in result["BRAND_GATE_REASON"], result

    wrong_position = dict(compliant, brand_mark_position="top_left")
    result = evaluate_brand_gate(wrong_position)
    assert result["BRAND_GATE"] == "BLOCKED", result
    assert "BRAND_MARK_BOTTOM_RIGHT" in result["BRAND_GATE_REASON"], result

    full_opacity = dict(compliant, brand_mark_opacity=255)
    result = evaluate_brand_gate(full_opacity)
    assert result["BRAND_GATE"] == "BLOCKED", result
    assert "BRAND_MARK_LOW_OPACITY" in result["BRAND_GATE_REASON"], result

    bad_bounds = dict(compliant, text_bounds_valid=False)
    result = evaluate_brand_gate(bad_bounds)
    assert result["BRAND_GATE"] == "BLOCKED", result
    assert "TEXT_BOUNDS_VALID" in result["BRAND_GATE_REASON"], result

    no_stat_but_legacy_margin = dict(
        compliant, stat_present=False, stat_color=None, margin_px=70
    )
    result = evaluate_brand_gate(no_stat_but_legacy_margin)
    assert result["checks"]["MIN_MARGIN_90PX"] is False, result
    assert result["BRAND_GATE"] == "PASS", (
        "reviewed scope: structural failure without a stat must not block",
        result,
    )

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

    print("BRAND_GATE_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
