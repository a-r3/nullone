#!/usr/bin/env python3
"""Shape/hygiene validation for the editorial packaging contract fixtures.

Checks that tests/fixtures/packaging_contract_v1_examples.json is
internally well-formed, free of production identifiers, and that the
contract document contains the load-bearing statements this repository
relies on. Does not import or execute the real evaluator -- that is
tests/test_packaging_policy.py's job.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/packaging_contract_v1_examples.json"
DOC = ROOT / "docs/contracts/editorial-packaging-contract-v1.md"

CONTENT_SHAPES = {
    "SINGLE_FACT",
    "ANNOUNCEMENT",
    "MULTI_STEP_EXPLAINER",
    "COMPARISON",
    "ROUNDUP",
    "BREAKING_DEVELOPING",
    "OPINION_ANALYSIS",
}

TIMELINESS_VALUES = {"BREAKING", "TODAY", "THIS_WEEK", "DURABLE"}
FORMAT_DECISIONS = {"SINGLE_POST", "CAROUSEL", "STORY", "SKIP"}
POST_DECISIONS = {"POST", "SKIP"}

REQUIRED_ASSET_FIELDS = {
    "has_official_or_source_image",
    "has_usable_screenshot",
    "image_on_topic",
    "image_quality_ok",
    "data_visualization_possible",
}

REQUIRED_CANDIDATE_FIELDS = {
    "content_type",
    "content_shape",
    "timeliness",
    "verification_status",
    "source_grounding",
    "audience_value",
    "distinct_beat_count",
    "depicts_real_world_subject",
    "still_developing",
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
    assert DOC.is_file(), f"missing contract doc: {DOC}"
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert data["schema"] == "nullone.packaging-contract.v1"
    assert data["contract"] == "docs/contracts/editorial-packaging-contract-v1.md"

    examples = data.get("examples")
    assert isinstance(examples, list) and examples

    ids: set[int] = set()
    required_scenarios = {
        "breaking_strong_photo": False,
        "multi_step_explainer": False,
        "weakly_sourced_skip": False,
        "story_not_carousel": False,
        "real_photo_over_synthetic": False,
    }

    for example in examples:
        for field in ("id", "description", "request", "expected"):
            if field not in example:
                fail(f"example missing field {field!r}: {example}")

        ex_id = example["id"]
        if ex_id in ids:
            fail(f"duplicate example id: {ex_id}")
        ids.add(ex_id)

        if not str(example["description"]).strip():
            fail(f"example {ex_id}: empty description")

        request = example["request"]
        candidate = request.get("candidate", {})
        assets = request.get("assets", {})

        missing_candidate = REQUIRED_CANDIDATE_FIELDS - set(candidate)
        if missing_candidate:
            fail(f"example {ex_id}: candidate missing {sorted(missing_candidate)}")

        missing_assets = REQUIRED_ASSET_FIELDS - set(assets)
        if missing_assets:
            fail(f"example {ex_id}: assets missing {sorted(missing_assets)}")

        if candidate["content_shape"] not in CONTENT_SHAPES:
            fail(f"example {ex_id}: invalid content_shape {candidate['content_shape']!r}")

        if candidate["timeliness"] not in TIMELINESS_VALUES:
            fail(f"example {ex_id}: invalid timeliness {candidate['timeliness']!r}")

        if candidate["distinct_beat_count"] < 0:
            fail(f"example {ex_id}: distinct_beat_count must be non-negative")

        expected = example["expected"]
        if expected["POST_DECISION"] not in POST_DECISIONS:
            fail(f"example {ex_id}: invalid expected POST_DECISION")
        if expected["FORMAT_DECISION"] not in FORMAT_DECISIONS:
            fail(f"example {ex_id}: invalid expected FORMAT_DECISION")

        if expected["POST_DECISION"] == "SKIP" and expected["FORMAT_DECISION"] != "SKIP":
            fail(f"example {ex_id}: SKIP post_decision must carry FORMAT_DECISION=SKIP")

        if expected["FORMAT_DECISION"] == "CAROUSEL":
            slides = expected.get("slide_count_recommendation")
            if not isinstance(slides, int) or not (4 <= slides <= 8):
                fail(f"example {ex_id}: CAROUSEL must carry slide_count_recommendation in [4,8]")
        else:
            if expected.get("slide_count_recommendation") is not None:
                fail(f"example {ex_id}: non-CAROUSEL must carry slide_count_recommendation=null")

        desc = example["description"].lower()
        if ex_id == 1:
            required_scenarios["breaking_strong_photo"] = "single_post" == expected["FORMAT_DECISION"].lower()
        if ex_id == 2:
            required_scenarios["multi_step_explainer"] = expected["FORMAT_DECISION"] == "CAROUSEL"
        if ex_id == 3:
            required_scenarios["weakly_sourced_skip"] = expected["POST_DECISION"] == "SKIP"
        if ex_id == 4:
            required_scenarios["story_not_carousel"] = expected["FORMAT_DECISION"] == "STORY"
        if ex_id == 5:
            required_scenarios["real_photo_over_synthetic"] = expected["VISUAL_STYLE"] == "REAL_PHOTO"

    missing_scenarios = [name for name, ok in required_scenarios.items() if not ok]
    if missing_scenarios:
        fail(f"required example scenarios not satisfied: {missing_scenarios}")

    if len(examples) < 10:
        fail(f"expected at least 10 worked examples, found {len(examples)}")

    raw_fixture = FIXTURE.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_LITERAL_PATTERNS:
        if pattern.search(raw_fixture):
            fail(f"fixture hygiene violation: {pattern.pattern}")

    doc = DOC.read_text(encoding="utf-8")
    for required_phrase in (
        "generated/synthetic illustration is never an acceptable substitute for a\nrequired real photo",
        "CAROUSEL is allowed only if ALL of",
        "distinct_beat_count >= 3",
        "never as a default",
    ):
        if required_phrase not in doc:
            fail(f"required contract statement missing from doc: {required_phrase!r}")

    print(f"PACKAGING_CONTRACT_FIXTURE=PASS count={len(examples)}")
    print("FIXTURE_HYGIENE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
