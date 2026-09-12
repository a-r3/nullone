#!/usr/bin/env python3
"""Behavioral tests for the editorial packaging decision function.

Executes the real decision function, nullone_packaging_policy.
evaluate_packaging(), against the accepted
docs/contracts/editorial-packaging-contract-v1.md fixture plus targeted
scenarios for the carousel-eligibility gate, the real-photo fallback
ladder, slide-count clamping/trimming, malformed-input fail-closed
behavior, and purity/no-side-effect guarantees.

Unlike tests/test_packaging_contract_fixture.py (shape/hygiene only),
this imports and runs the module.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_packaging_policy as policy  # noqa: E402
from nullone_packaging_policy import (  # noqa: E402
    PackagingContractError,
    evaluate_packaging,
)

FIXTURE_PATH = ROOT / "tests/fixtures/packaging_contract_v1_examples.json"


def base_request(**overrides):
    request = {
        "candidate": {
            "content_type": "EXPLAINER",
            "content_shape": "MULTI_STEP_EXPLAINER",
            "timeliness": "THIS_WEEK",
            "verification_status": "PASS",
            "source_grounding": "STRONG_PRIMARY",
            "audience_value": "MEDIUM",
            "distinct_beat_count": 4,
            "depicts_real_world_subject": False,
            "still_developing": False,
        },
        "assets": {
            "has_official_or_source_image": False,
            "has_usable_screenshot": False,
            "image_on_topic": False,
            "image_quality_ok": False,
            "data_visualization_possible": True,
        },
    }
    for key, value in overrides.items():
        section, field = key.split(".", 1)
        request[section] = {**request[section], field: value}
    return request


class FixtureExamplesTest(unittest.TestCase):
    """Every worked example in the contract fixture must match exactly."""

    @classmethod
    def setUpClass(cls):
        cls.examples = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["examples"]

    def test_all_examples_match(self):
        for example in self.examples:
            with self.subTest(example=example["id"], description=example["description"]):
                request = copy.deepcopy(example["request"])
                result = evaluate_packaging(request)
                for field, expected_value in example["expected"].items():
                    self.assertEqual(
                        result[field],
                        expected_value,
                        msg=(
                            f"example {example['id']} field {field}: "
                            f"expected {expected_value!r}, got {result[field]!r}"
                        ),
                    )

    def test_examples_do_not_mutate_request(self):
        for example in self.examples:
            request = copy.deepcopy(example["request"])
            snapshot = copy.deepcopy(request)
            evaluate_packaging(request)
            self.assertEqual(request, snapshot, f"example {example['id']} mutated its input")


class VerificationGateTest(unittest.TestCase):
    def test_blocked_verification_always_skips(self):
        request = base_request(
            **{
                "candidate.verification_status": "BLOCKED",
                "candidate.content_shape": "MULTI_STEP_EXPLAINER",
                "candidate.distinct_beat_count": 10,
            }
        )
        result = evaluate_packaging(request)
        self.assertEqual(result["POST_DECISION"], "SKIP")
        self.assertEqual(result["FORMAT_DECISION"], "SKIP")
        self.assertEqual(result["FORMAT_REASON"], "VERIFICATION_BLOCKED")

    def test_verification_gate_fires_before_source_grounding_gate(self):
        request = base_request(
            **{
                "candidate.verification_status": "BLOCKED",
                "candidate.source_grounding": "WEAK_UNCONFIRMED",
            }
        )
        result = evaluate_packaging(request)
        self.assertEqual(result["FORMAT_REASON"], "VERIFICATION_BLOCKED")


class CarouselEligibilityTest(unittest.TestCase):
    """The central bug fix: carousel must never be the default."""

    def test_two_beats_never_carousels_even_for_explainer(self):
        request = base_request(**{"candidate.distinct_beat_count": 2})
        result = evaluate_packaging(request)
        self.assertNotEqual(result["FORMAT_DECISION"], "CAROUSEL")
        self.assertEqual(result["FORMAT_REASON"], "INSUFFICIENT_DISTINCT_BEATS")

    def test_single_fact_never_carousels_regardless_of_beat_count(self):
        request = base_request(
            **{
                "candidate.content_shape": "SINGLE_FACT",
                "candidate.distinct_beat_count": 8,
                "candidate.timeliness": "DURABLE",
            }
        )
        result = evaluate_packaging(request)
        self.assertNotEqual(result["FORMAT_DECISION"], "CAROUSEL")

    def test_breaking_developing_never_carousels_regardless_of_beat_count(self):
        request = base_request(
            **{
                "candidate.content_shape": "BREAKING_DEVELOPING",
                "candidate.distinct_beat_count": 8,
                "candidate.timeliness": "THIS_WEEK",
                "candidate.still_developing": True,
                "assets.has_usable_screenshot": True,
                "assets.image_on_topic": True,
            }
        )
        result = evaluate_packaging(request)
        self.assertNotEqual(result["FORMAT_DECISION"], "CAROUSEL")

    def test_breaking_timeliness_forbids_carousel_even_with_enough_beats(self):
        request = base_request(
            **{
                "candidate.content_shape": "ROUNDUP",
                "candidate.distinct_beat_count": 6,
                "candidate.timeliness": "BREAKING",
            }
        )
        result = evaluate_packaging(request)
        self.assertNotEqual(result["FORMAT_DECISION"], "CAROUSEL")
        self.assertEqual(result["FORMAT_REASON"], "BREAKING_TIMELINESS_FORBIDS_CAROUSEL")

    def test_exactly_three_beats_is_the_minimum_that_justifies_carousel(self):
        request = base_request(
            **{"candidate.content_shape": "ROUNDUP", "candidate.distinct_beat_count": 3}
        )
        result = evaluate_packaging(request)
        self.assertEqual(result["FORMAT_DECISION"], "CAROUSEL")

        request = base_request(
            **{"candidate.content_shape": "ROUNDUP", "candidate.distinct_beat_count": 2}
        )
        result = evaluate_packaging(request)
        self.assertNotEqual(result["FORMAT_DECISION"], "CAROUSEL")


class SlideCountDisciplineTest(unittest.TestCase):
    def test_slide_count_matches_beats_plus_overhead(self):
        request = base_request(**{"candidate.distinct_beat_count": 4})
        result = evaluate_packaging(request)
        self.assertEqual(result["FORMAT_DECISION"], "CAROUSEL")
        self.assertEqual(result["slide_count_recommendation"], 6)

    def test_slide_count_floor_is_four(self):
        request = base_request(**{"candidate.distinct_beat_count": 3})
        result = evaluate_packaging(request)
        self.assertEqual(result["slide_count_recommendation"], 5)

    def test_beat_count_beyond_trim_threshold_is_capped_not_inflated(self):
        request = base_request(**{"candidate.distinct_beat_count": 20})
        result = evaluate_packaging(request)
        self.assertEqual(result["FORMAT_DECISION"], "CAROUSEL")
        # 20 beats must trim to the strongest 6, not produce a 22-slide carousel.
        self.assertEqual(result["slide_count_recommendation"], 8)

    def test_non_carousel_never_carries_a_slide_count(self):
        request = base_request(**{"candidate.distinct_beat_count": 1})
        result = evaluate_packaging(request)
        self.assertNotEqual(result["FORMAT_DECISION"], "CAROUSEL")
        self.assertIsNone(result["slide_count_recommendation"])


class RealPhotoFallbackLadderTest(unittest.TestCase):
    def test_screenshot_fallback_used_before_data_visualization(self):
        request = base_request(
            **{
                "candidate.content_shape": "ANNOUNCEMENT",
                "candidate.distinct_beat_count": 1,
                "assets.has_usable_screenshot": True,
                "assets.image_on_topic": True,
                "assets.data_visualization_possible": True,
            }
        )
        result = evaluate_packaging(request)
        self.assertEqual(result["POST_DECISION"], "POST")
        self.assertEqual(result["VISUAL_STYLE"], "SOURCE_SCREENSHOT")

    def test_data_visualization_fallback_used_when_no_screenshot(self):
        request = base_request(
            **{
                "candidate.content_shape": "ANNOUNCEMENT",
                "candidate.distinct_beat_count": 1,
                "assets.data_visualization_possible": True,
            }
        )
        result = evaluate_packaging(request)
        self.assertEqual(result["POST_DECISION"], "POST")
        self.assertEqual(result["VISUAL_STYLE"], "DATA_VISUALIZATION")

    def test_no_fallback_available_skips_rather_than_generating_illustration(self):
        request = base_request(
            **{
                "candidate.content_shape": "ANNOUNCEMENT",
                "candidate.distinct_beat_count": 1,
                "assets.data_visualization_possible": False,
            }
        )
        result = evaluate_packaging(request)
        self.assertEqual(result["POST_DECISION"], "SKIP")
        self.assertEqual(result["FORMAT_REASON"], "REAL_PHOTO_REQUIRED_NO_FALLBACK")
        self.assertEqual(result["VISUAL_STYLE"], "GENERATED_ILLUSTRATION_FORBIDDEN")

    def test_generated_illustration_never_reachable_when_evidence_required(self):
        """Regression guard for the product bug this contract fixes: no path
        through evaluate_packaging may return a *_ALLOWED generated
        illustration style while VISUAL_EVIDENCE_REQUIRED is YES."""
        for depicts in (True, False):
            for shape in policy.CONTENT_SHAPES:
                request = base_request(
                    **{
                        "candidate.content_shape": shape,
                        "candidate.depicts_real_world_subject": depicts,
                        "candidate.distinct_beat_count": 4,
                    }
                )
                result = evaluate_packaging(request)
                if result["VISUAL_EVIDENCE_REQUIRED"] == "YES":
                    self.assertNotEqual(
                        result["VISUAL_STYLE"],
                        "GENERATED_ILLUSTRATION_ALLOWED",
                        msg=f"shape={shape} depicts={depicts} result={result}",
                    )


class AudienceValueGateTest(unittest.TestCase):
    def test_low_audience_value_skips(self):
        request = base_request(**{"candidate.audience_value": "LOW"})
        result = evaluate_packaging(request)
        self.assertEqual(result["POST_DECISION"], "SKIP")
        self.assertEqual(result["FORMAT_REASON"], "LOW_AUDIENCE_VALUE")

    def test_low_audience_value_does_not_suppress_developing_breaking_item(self):
        request = base_request(
            **{
                "candidate.audience_value": "LOW",
                "candidate.content_shape": "BREAKING_DEVELOPING",
                "candidate.timeliness": "BREAKING",
                "candidate.still_developing": True,
                "assets.has_usable_screenshot": True,
                "assets.image_on_topic": True,
            }
        )
        result = evaluate_packaging(request)
        self.assertEqual(result["POST_DECISION"], "POST")


class TextDensityTest(unittest.TestCase):
    def test_text_density_is_derived_from_beat_count(self):
        cases = {0: "LOW", 1: "LOW", 2: "MEDIUM", 3: "HIGH", 9: "HIGH"}
        for beats, expected in cases.items():
            request = base_request(**{"candidate.distinct_beat_count": beats})
            result = evaluate_packaging(request)
            self.assertEqual(result["TEXT_DENSITY"], expected, msg=f"beats={beats}")


class MalformedInputFailsClosedTest(unittest.TestCase):
    def test_unrecognized_content_shape_raises(self):
        request = base_request(**{"candidate.content_shape": "VIRAL_MEME"})
        with self.assertRaises(PackagingContractError):
            evaluate_packaging(request)

    def test_negative_beat_count_raises(self):
        request = base_request(**{"candidate.distinct_beat_count": -1})
        with self.assertRaises(PackagingContractError):
            evaluate_packaging(request)

    def test_non_boolean_asset_field_raises(self):
        request = base_request(**{"assets.image_on_topic": "yes"})
        with self.assertRaises(PackagingContractError):
            evaluate_packaging(request)

    def test_missing_candidate_section_raises(self):
        request = base_request()
        del request["candidate"]
        with self.assertRaises(PackagingContractError):
            evaluate_packaging(request)

    def test_missing_assets_section_raises(self):
        request = base_request()
        del request["assets"]
        with self.assertRaises(PackagingContractError):
            evaluate_packaging(request)

    def test_non_dict_request_raises(self):
        with self.assertRaises(PackagingContractError):
            evaluate_packaging("not a dict")  # type: ignore[arg-type]


class IdempotenceTest(unittest.TestCase):
    def test_same_request_yields_same_result(self):
        request = base_request()
        first = evaluate_packaging(copy.deepcopy(request))
        second = evaluate_packaging(copy.deepcopy(request))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
