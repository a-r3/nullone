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
    "TARGET_MET",
}
ALLOWED_AUDIENCE = {
    "AUDIENCE_GAP",
    "AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW",
    "TARGET_MET",
}
ALLOWED_ACTIONS = {"NONE", "CANDIDATE_SEARCH_AND_PREPARE"}

OUTCOME_FOR_RECOMMENDATION = {
    "PREPARE_MAIN": {"PREPARED"},
    "PREPARE_STORY": {"PREPARED"},
    "NO_ACTION": ALLOWED_OUTCOMES - {"PREPARED"},
}

TARGET_MIN = {
    "NORMAL": {"MAIN": 2, "STORY": 3},
    "STRONG_NEWS": {"MAIN": 2, "STORY": 4},
    "EXCEPTIONAL": {"MAIN": 2, "STORY": 4},
    "QUIET": {"MAIN": 1, "STORY": 2},
}

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
    target_min = TARGET_MIN[cin["day_profile"]][surface]
    audience_met = published >= target_min
    if audience_met:
        audience_status = "TARGET_MET"
    elif pending > 0:
        audience_status = "AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW"
    else:
        audience_status = "AUDIENCE_GAP"

    cand = cin["candidate"]
    if cand["source_class"] == "NONE":
        return ("NO_ACTION", "SOURCE_UNAVAILABLE", audience_status)
    if cand["verification"] != "PASS" or not cand["quality_available"]:
        return ("NO_ACTION", "NO_QUALITY_CANDIDATE", audience_status)
    if not cand["incremental_value"]:
        return ("NO_ACTION", "DUPLICATE_SUPPRESSED", audience_status)
    if (
        cin["opportunity"] == "RECOVERY"
        and audience_met
        and not cin["signal"]["exceptional_development"]
    ):
        return ("NO_ACTION", "TARGET_MET", audience_status)
    if pending > 0:
        return ("NO_ACTION", "BLOCKED_PENDING_REVIEW", audience_status)
    if audience_met:
        return ("NO_ACTION", "TARGET_MET", audience_status)
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
            "timezone", "main_load", "story_load", "candidate", "signal",
        ):
            if field not in cin:
                fail(f"{name}: input missing field {field!r}")
        if cin["schema"] != "nullone.cadence-contract.v2":
            fail(f"{name}: unexpected input schema {cin['schema']!r}")
        if cin["timezone"] != "Asia/Baku":
            fail(f"{name}: timezone must be Asia/Baku")
        if cin["surface"] not in ("MAIN", "STORY"):
            fail(f"{name}: surface must be MAIN or STORY")
        for load_key in ("main_load", "story_load"):
            load = cin[load_key]
            if load["published_today"] < 0 or load["pending"] < 0:
                fail(f"{name}: {load_key} has a negative counter")

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
        # Audience/pending truthfulness: pending-blocked implies a real gap.
        if outcome == "BLOCKED_PENDING_REVIEW" and audience != "AUDIENCE_GAP_BLOCKED_BY_PENDING_REVIEW":
            fail(f"{name}: backpressure must report the audience gap truthfully")

        # Deterministic replay: the contract's evaluation order must
        # reproduce the hand-authored expected output exactly.
        got = reference_evaluate(cin)
        if got != (rec, outcome, audience):
            fail(f"{name}: reference replay {got} != authored {(rec, outcome, audience)}")

    if len(cases) < 10:
        fail(f"expected at least 10 worked examples, found {len(cases)}")

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
        "surface = MAIN | STORY",
        "QUIET_DAY_WEAK_FILLER=FORBIDDEN",
    ):
        if required_phrase not in doc:
            fail(f"required contract statement missing from doc: {required_phrase}")

    print(f"EDITORIAL_CADENCE_V2_CONTRACT=PASS count={len(cases)}")
    print("FIXTURE_HYGIENE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
