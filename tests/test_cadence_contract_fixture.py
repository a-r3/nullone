#!/usr/bin/env python3
"""Shape/hygiene validation for the #31 cadence contract fixtures.

This deliberately does NOT execute a decision function against these
fixtures: no such function exists in this repository, because
implementing the cadence controller is issue #32, not #31. This test
only checks that the documentation fixture is internally well-formed and
free of production identifiers, so it cannot silently drift from the
vocabulary defined in docs/contracts/cadence-contract-v1.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/cadence_contract_v1_examples.json"
DOC = ROOT / "docs/contracts/cadence-contract-v1.md"

ALLOWED_RECOMMENDATIONS = {
    "NO_ACTION",
    "PREPARE_STORY",
    "PREPARE_MAIN_CANDIDATE",
}

ALLOWED_REASON_CODES = {
    "MAIN_GAP",
    "STORY_GAP",
    "NO_QUALITY_CANDIDATE",
    "PENDING_MAIN_EXISTS",
    "PENDING_STORY_EXISTS",
    "RECENT_AUDIENCE_ACTIVITY",
    "COALESCED_AFTER_DOWNTIME",
    "QUIET_HOURS",
    "TARGETS_MET",
}

ALLOWED_PERMITTED_ACTIONS = {
    "NONE",
    "CANDIDATE_SEARCH_AND_PREPARE",
}

ALLOWED_DAYPARTS = {
    "QUIET",
    "MORNING",
    "AFTERNOON",
    "EVENING",
}

REASON_FOR_RECOMMENDATION = {
    "PREPARE_MAIN_CANDIDATE": {"MAIN_GAP"},
    "PREPARE_STORY": {"STORY_GAP"},
    "NO_ACTION": ALLOWED_REASON_CODES - {"MAIN_GAP", "STORY_GAP"},
}

FORBIDDEN_LITERAL_PATTERNS = [
    re.compile(r"6a982bbf77555aae01c28f21", re.I),  # production account ID
    re.compile(r"api[_-]?key\s*[:=]", re.I),
    re.compile(r"authorization\s*[:=]", re.I),
    re.compile(r"bearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"oauth[_-]?(token|secret)\s*[:=]", re.I),
]


def fail(message: str) -> None:
    raise AssertionError(message)


def main() -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert data["schema"] == "nullone.cadence-contract-examples.v1"
    assert data["contract_doc"] == "docs/contracts/cadence-contract-v1.md"
    assert DOC.is_file()

    cases = data.get("cases")
    assert isinstance(cases, list) and cases

    names: set[str] = set()

    for case in cases:
        required = {"name", "description", "input", "expected_output"}
        missing = required - set(case)
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
            "schema",
            "now",
            "timezone",
            "main_load",
            "story_load",
            "candidate_availability",
            "signal",
        ):
            if field not in cin:
                fail(f"{name}: input missing field {field!r}")

        if cin["schema"] != "nullone.cadence-contract.v1":
            fail(f"{name}: unexpected input schema {cin['schema']!r}")

        if cin["timezone"] != "Asia/Baku":
            fail(f"{name}: timezone must be Asia/Baku, got {cin['timezone']!r}")

        for load_key in ("main_load", "story_load"):
            load = cin[load_key]
            for f in ("published_today", "pending", "last_published_at"):
                if f not in load:
                    fail(f"{name}: {load_key} missing field {f!r}")
            if load["published_today"] < 0 or load["pending"] < 0:
                fail(f"{name}: {load_key} has a negative counter")

        cav = cin["candidate_availability"]
        for f in ("main_quality_candidate_available", "story_quality_candidate_available"):
            if not isinstance(cav.get(f), bool):
                fail(f"{name}: candidate_availability.{f} must be boolean")

        cout = case["expected_output"]
        for field in ("recommendation", "reason_code", "permitted_action", "daypart"):
            if field not in cout:
                fail(f"{name}: expected_output missing field {field!r}")

        rec = cout["recommendation"]
        reason = cout["reason_code"]
        action = cout["permitted_action"]
        daypart = cout["daypart"]

        if rec not in ALLOWED_RECOMMENDATIONS:
            fail(f"{name}: invalid recommendation {rec!r}")

        if reason not in ALLOWED_REASON_CODES:
            fail(f"{name}: invalid reason_code {reason!r}")

        if reason not in REASON_FOR_RECOMMENDATION[rec]:
            fail(f"{name}: reason_code {reason!r} is not valid for recommendation {rec!r}")

        if action not in ALLOWED_PERMITTED_ACTIONS:
            fail(f"{name}: invalid permitted_action {action!r}")

        if rec == "NO_ACTION" and action != "NONE":
            fail(f"{name}: NO_ACTION must carry permitted_action NONE")

        if rec != "NO_ACTION" and action != "CANDIDATE_SEARCH_AND_PREPARE":
            fail(f"{name}: {rec} must carry permitted_action CANDIDATE_SEARCH_AND_PREPARE")

        if daypart not in ALLOWED_DAYPARTS:
            fail(f"{name}: invalid daypart {daypart!r}")

    if len(cases) < 12:
        fail(f"expected at least 12 worked examples, found {len(cases)}")

    raw_fixture = FIXTURE.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_LITERAL_PATTERNS:
        if pattern.search(raw_fixture):
            fail(f"fixture hygiene violation: {pattern.pattern}")

    doc = DOC.read_text(encoding="utf-8")
    for required_phrase in (
        "quality > quota",
        "PREPARE_* != PUBLISH",
        "Asia/Baku",
        "stale missed slot is not an obligation",
    ):
        if required_phrase not in doc:
            fail(f"required contract statement missing from doc: {required_phrase}")

    print(f"CADENCE_CONTRACT_FIXTURE=PASS count={len(cases)}")
    print("FIXTURE_HYGIENE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
