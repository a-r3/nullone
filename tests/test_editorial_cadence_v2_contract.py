#!/usr/bin/env python3
"""Shape/hygiene + deterministic replay for the Editorial Cadence V2 fixtures.

This deliberately does NOT implement or modify any production
cadence/controller runtime. The reference evaluator below is test-local:
it replays the per-surface evaluation order documented in
docs/contracts/editorial-cadence-v2.md §10 against the hand-authored
fixtures, so the contract's worked examples are machine-checked without
adding runtime code.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/editorial_cadence_v2_examples.json"
DOC = ROOT / "docs/contracts/editorial-cadence-v2.md"

ALLOWED_RECOMMENDATIONS = {"PREPARE_MAIN", "PREPARE_STORY", "NO_ACTION"}
ALLOWED_OUTCOMES = {
    "PREPARED",
    "NO_QUALITY_CANDIDATE",
    "BLOCKED_PENDING_REVIEW",
    "RECENT_ACTIVITY",
    "DUPLICATE_SUPPRESSED",
    "SOURCE_UNAVAILABLE",
    "TARGET_BAND_REACHED",
    "TARGET_MAX_REACHED",
}
ALLOWED_AUDIENCE = {
    "AUDIENCE_GAP",
    "AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW",
    "TARGET_BAND_REACHED",
    "TARGET_MAX_REACHED",
}
ALLOWED_ACTIONS = {"NONE", "CANDIDATE_SEARCH_AND_PREPARE"}

OUTCOME_FOR_RECOMMENDATION = {
    "PREPARE_MAIN": {"PREPARED"},
    "PREPARE_STORY": {"PREPARED"},
    "NO_ACTION": ALLOWED_OUTCOMES - {"PREPARED"},
}

# (target_min, target_max) per day profile and surface.
TARGETS = {
    "NORMAL": {"MAIN": (2, 2), "STORY": (3, 5)},
    "STRONG_NEWS": {"MAIN": (2, 2), "STORY": (4, 6)},
    "EXCEPTIONAL": {"MAIN": (2, 3), "STORY": (4, 6)},
    "QUIET": {"MAIN": (1, 2), "STORY": (2, 4)},
}

BREAKING_OPPORTUNITIES = {"MATERIAL_BREAKING", "EXCEPTIONAL_BREAKING"}

FORBIDDEN_LITERAL_PATTERNS = [
    re.compile(r"6a982bbf77555aae01c28f21", re.I),
    re.compile(r"api[_-]?key\s*[:=]", re.I),
    re.compile(r"authorization\s*[:=]", re.I),
    re.compile(r"bearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"oauth[_-]?(token|secret)\s*[:=]", re.I),
]


def fail(message: str) -> None:
    raise AssertionError(message)


def reference_evaluate(cin: dict) -> dict:
    """Test-local replay of the V2 §10 per-surface evaluation order."""
    surface = cin["surface"]
    load = cin["main_load"] if surface == "MAIN" else cin["story_load"]
    published = load["published_today"]
    pending = load["pending"]
    target_min, target_max = TARGETS[cin["day_profile"]][surface]
    if published < target_min:
        position = "gap"
    elif published < target_max:
        position = "band"
    else:
        position = "max"
    if position == "gap":
        audience_status = (
            "AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW"
            if pending > 0 else "AUDIENCE_GAP"
        )
    elif position == "band":
        audience_status = "TARGET_BAND_REACHED"
    else:
        audience_status = "TARGET_MAX_REACHED"

    cand = cin["candidate"]
    if cand["source_class"] == "NONE":
        return ("NO_ACTION", "SOURCE_UNAVAILABLE", audience_status)
    if cand["verification"] != "PASS" or not cand["quality_available"]:
        return ("NO_ACTION", "NO_QUALITY_CANDIDATE", audience_status)
    if not cand["incremental_value"]:
        return ("NO_ACTION", "DUPLICATE_SUPPRESSED", audience_status)
    if position == "max":
        # Hard maximum is never exceeded: breaking bypasses the
        # minimum, never the maximum.
        return ("NO_ACTION", "TARGET_MAX_REACHED", audience_status)
    opportunity = cin["opportunity"]
    last = load.get("last_published_at")
    if last is not None and opportunity not in BREAKING_OPPORTUNITIES:
        now = datetime.fromisoformat(cin["now"])
        prev = datetime.fromisoformat(last)
        elapsed_minutes = (now - prev).total_seconds() / 60
        if elapsed_minutes < cin["config"]["min_spacing_minutes"]:
            return ("NO_ACTION", "RECENT_ACTIVITY", audience_status)
    if (
        opportunity == "RECOVERY"
        and position != "gap"
        and not cin["signal"]["exceptional_development"]
    ):
        return (
            "NO_ACTION",
            "TARGET_BAND_REACHED" if position == "band" else "TARGET_MAX_REACHED",
            audience_status,
        )
    if pending > 0:
        return ("NO_ACTION", "BLOCKED_PENDING_REVIEW", audience_status)
    if surface == "MAIN" and position == "band":
        if cin["day_profile"] == "QUIET":
            # Optional in-band capacity toward max=2: a strong verified
            # ordinary second MAIN MAY prepare; fall through to PREPARE.
            pass
        else:
            exceptional = (
                opportunity == "EXCEPTIONAL_BREAKING"
                and (
                    cin["day_profile"] == "EXCEPTIONAL"
                    or cin["signal"]["exceptional_development"]
                )
            )
            if not exceptional:
                return ("NO_ACTION", "TARGET_BAND_REACHED", audience_status)
    recommendation = "PREPARE_MAIN" if surface == "MAIN" else "PREPARE_STORY"
    return (recommendation, "PREPARED", audience_status)


def main() -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert data["schema"] == "nullone.cadence-contract-examples.v2"
    assert data["contract_doc"] == "docs/contracts/editorial-cadence-v2.md"
    assert DOC.is_file()

    cases = data.get("cases")
    assert isinstance(cases, list) and cases

    names: set[str] = set()
    for case in cases:
        missing = {"name", "description", "input", "expected_output"} - set(case)
        if missing:
            fail(f"{case.get('name', '<unknown>')}: missing fields {sorted(missing)}")
        name = case["name"]
        if name in names:
            fail(f"duplicate case name: {name}")
        names.add(name)
        if not str(case["description"]).strip():
            fail(f"{name}: empty description")

        cin = case["input"]
        for field in (
            "schema", "surface", "opportunity", "day_profile", "now",
            "timezone", "config", "main_load", "story_load",
            "candidate", "signal",
        ):
            if field not in cin:
                fail(f"{name}: input missing field {field!r}")
        if cin["schema"] != "nullone.cadence-contract.v2":
            fail(f"{name}: unexpected input schema {cin['schema']!r}")
        if cin["timezone"] != "Asia/Baku":
            fail(f"{name}: timezone must be Asia/Baku")
        if cin["surface"] not in ("MAIN", "STORY"):
            fail(f"{name}: surface must be MAIN or STORY")
        if cin["day_profile"] not in TARGETS:
            fail(f"{name}: unknown day_profile {cin['day_profile']!r}")
        spacing = cin["config"].get("min_spacing_minutes")
        if not isinstance(spacing, int) or spacing < 0:
            fail(f"{name}: config.min_spacing_minutes must be a non-negative int")
        try:
            now_dt = datetime.fromisoformat(cin["now"])
            if now_dt.tzinfo is None:
                fail(f"{name}: now must be offset-aware ISO8601")
        except ValueError:
            fail(f"{name}: now is not valid ISO8601")
        for load_key in ("main_load", "story_load"):
            load = cin[load_key]
            if load["published_today"] < 0 or load["pending"] < 0:
                fail(f"{name}: {load_key} has a negative counter")
            last = load.get("last_published_at")
            if last is not None:
                try:
                    prev = datetime.fromisoformat(last)
                except ValueError:
                    fail(f"{name}: {load_key}.last_published_at is not valid ISO8601")
                if prev.tzinfo is None:
                    fail(f"{name}: {load_key}.last_published_at must be offset-aware")

        cout = case["expected_output"]
        for field in ("recommendation", "outcome", "audience_status", "permitted_action"):
            if field not in cout:
                fail(f"{name}: expected_output missing field {field!r}")
        rec, outcome, audience, action = (
            cout["recommendation"], cout["outcome"],
            cout["audience_status"], cout["permitted_action"],
        )
        if rec not in ALLOWED_RECOMMENDATIONS:
            fail(f"{name}: invalid recommendation {rec!r}")
        if outcome not in OUTCOME_FOR_RECOMMENDATION[rec]:
            fail(f"{name}: outcome {outcome!r} invalid for {rec!r}")
        if audience not in ALLOWED_AUDIENCE:
            fail(f"{name}: invalid audience_status {audience!r}")
        if action not in ALLOWED_ACTIONS:
            fail(f"{name}: invalid permitted_action {action!r}")
        if rec == "NO_ACTION" and action != "NONE":
            fail(f"{name}: NO_ACTION must carry permitted_action NONE")
        if rec != "NO_ACTION" and action != "CANDIDATE_SEARCH_AND_PREPARE":
            fail(f"{name}: {rec} must carry permitted_action CANDIDATE_SEARCH_AND_PREPARE")
        # Surface/recommendation agreement: independent surfaces, no cross-format output.
        if rec == "PREPARE_MAIN" and cin["surface"] != "MAIN":
            fail(f"{name}: PREPARE_MAIN from a non-MAIN surface evaluation")
        if rec == "PREPARE_STORY" and cin["surface"] != "STORY":
            fail(f"{name}: PREPARE_STORY from a non-STORY surface evaluation")
        # Audience/pending truthfulness: pending-blocked below the
        # minimum implies a real gap; pending-blocked inside the band
        # reports the band (minimum reached, maximum not reached).
        if outcome == "BLOCKED_PENDING_REVIEW" and audience not in (
            "AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW", "TARGET_BAND_REACHED",
        ):
            fail(f"{name}: backpressure must report audience state truthfully")
        # Terminal outcomes agree with the audience status.
        if outcome == "TARGET_MAX_REACHED" and audience != "TARGET_MAX_REACHED":
            fail(f"{name}: TARGET_MAX_REACHED outcome requires max-reached audience")
        if outcome == "TARGET_BAND_REACHED" and audience != "TARGET_BAND_REACHED":
            fail(f"{name}: TARGET_BAND_REACHED outcome requires band audience")

        # Deterministic replay: the contract's evaluation order must
        # reproduce the hand-authored expected output exactly.
        got = reference_evaluate(cin)
        if got != (rec, outcome, audience):
            fail(f"{name}: reference replay {got} != authored {(rec, outcome, audience)}")

    if len(cases) < 21:
        fail(f"expected at least 21 worked examples, found {len(cases)}")

    # Review-gate coverage: every required semantic scenario must be
    # present by name.
    for required in (
        "story_band_optional_capacity",
        "story_max_reached",
        "strong_news_band_optional_capacity",
        "exceptional_main_third",
        "ordinary_main_band_stop",
        "material_breaking_after_min",
        "story_inside_spacing_held",
        "story_outside_spacing_eligible",
        "quiet_second_main_reachable",
        "quiet_main_max_enforced",
        "exceptional_ordinary_band_stop",
    ):
        if required not in names:
            fail(f"missing required worked example: {required}")

    raw_fixture = FIXTURE.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_LITERAL_PATTERNS:
        if pattern.search(raw_fixture):
            fail(f"fixture hygiene violation: {pattern.pattern}")

    doc = DOC.read_text(encoding="utf-8")
    for required_phrase in (
        "quality > quota",
        "PREPARE_* != PUBLISH",
        "Asia/Baku",
        "AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW",
        "TARGET_BAND_REACHED",
        "TARGET_MAX_REACHED",
        "target_min",
        "target_max",
        "min_spacing_minutes",
        "surface = MAIN | STORY",
        "QUIET_DAY_WEAK_FILLER=FORBIDDEN",
        "fail-closed default profile",
    ):
        if required_phrase not in doc:
            fail(f"required contract statement missing from doc: {required_phrase}")

    print(f"EDITORIAL_CADENCE_V2_CONTRACT=PASS count={len(cases)}")
    print("FIXTURE_HYGIENE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
