#!/usr/bin/env python3
"""Focused SELECT packaging-contract regression tests (offline only).

Proves the Claude Draft SELECT boundary enforces the deterministic
packaging evaluator input contract BEFORE any production file write,
and that the asset descriptor boundary accepts only structurally
valid `nullone.packaging-asset.v1` shapes while receipt/style
semantic authority stays downstream.

No network, no model calls, no production writes: every cycle runs
against a temp workspace with mocked transport and helper seams.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_bridge_common import BridgeError  # noqa: E402
import nullone_claude_draft_provider as draft  # noqa: E402
import nullone_draft_candidate_queue as draft_queue  # noqa: E402
import nullone_packaging_policy as policy  # noqa: E402
import nullone_packaging_receipt as receipt_mod  # noqa: E402


def load_hyphenated(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evaluator_cli = load_hyphenated("select_contract_evaluator_cli", "nullone-packaging-evaluator.py")

WORKSPACE_PROD = Path(__file__).resolve().parents[1] / "workspace/social/drafts/production"


def valid_candidate(**overrides):
    candidate = {
        "content_type": "NEWS",
        "content_shape": "SINGLE_FACT",
        "timeliness": "TODAY",
        "verification_status": "PASS",
        "source_grounding": "STRONG_PRIMARY",
        "audience_value": "HIGH",
        "distinct_beat_count": 1,
        "depicts_real_world_subject": False,
        "still_developing": False,
        "visual_requirement": "NONE",
    }
    candidate.update(overrides)
    return candidate


def valid_assets(**overrides):
    assets = {
        "has_official_or_source_image": False,
        "has_usable_screenshot": False,
        "image_on_topic": False,
        "image_quality_ok": False,
        "data_visualization_possible": False,
    }
    assets.update(overrides)
    return assets


def valid_descriptor(candidate_id="probe-candidate-one", **overrides):
    descriptor = {
        "schema": receipt_mod.ASSET_DESCRIPTOR_SCHEMA,
        "candidate_id": candidate_id,
        "asset_kind": "NONE",
        "local_path": None,
        "source_url": None,
        "provenance": "probe provenance",
        "sha256": None,
    }
    descriptor.update(overrides)
    return descriptor


def ranked_item(candidate_id="probe-candidate-one", **overrides):
    item = {
        "candidate_id": candidate_id,
        "topic": "Probe topic",
        "topic_cluster": "probe",
        "content_type": "NEWS",
        "packaging_request": {
            "candidate": valid_candidate(),
            "assets": valid_assets(),
        },
        "asset": valid_descriptor(candidate_id),
    }
    item.update(overrides)
    if "asset" not in overrides:
        item["asset"] = dict(item["asset"])
        item["asset"]["candidate_id"] = item["candidate_id"]
    return item


def select_result(ranked):
    return {"decision": "SELECT", "ranked": ranked, "notes": "probe"}


def queue_entry(candidate_id="probe-candidate-one", *, topic="Probe topic",
                cluster="probe", content_type="NEWS", status="READY"):
    return (
        f"- **candidate_id:** {candidate_id}\n"
        f"- **topic:** {topic}\n"
        f"- **topic_cluster:** {cluster}\n"
        f"- **content_type:** {content_type}\n"
        f"- **status:** {status}\n"
        "- **verification_status:** PASS\n"
    )


def write_queue(root, content):
    path = root / draft_queue.QUEUE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def workspace_prod_snapshot():
    if not WORKSPACE_PROD.is_dir():
        return None
    return sorted(p.name for p in WORKSPACE_PROD.iterdir())


class SelectPackagingContractTests(unittest.TestCase):
    def _invoke_select(self, item, candidate_id="probe-candidate-one"):
        """Run a full SELECT cycle; return (error, root, helper_calls, files)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            queue = write_queue(root, queue_entry(candidate_id))
            before_ws = workspace_prod_snapshot()
            with mock.patch.object(
                draft, "run_structured", return_value=select_result([item])
            ), mock.patch.object(
                draft, "_run_helper", side_effect=AssertionError("helper must not run")
            ) as helper, mock.patch.object(
                draft.subprocess, "run", side_effect=AssertionError("no subprocess")
            ):
                try:
                    draft.invoke_draft(
                        prompt="p", workspace=root, model="sonnet", timeout=900
                    )
                except BridgeError as exc:
                    error = exc
                else:
                    error = None
                helper_calls = helper.call_count
                files = sorted(
                    str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
                )
                after_ws = workspace_prod_snapshot()
            self.assertEqual(before_ws, after_ws, "production dir mutated")
            self.assertEqual(files, [str(queue.relative_to(root))])
            return error, helper_calls

    def test_01_missing_candidate_rejected_before_write(self):
        item = ranked_item()
        del item["packaging_request"]["candidate"]
        with self.assertRaises(BridgeError):
            draft._validated_select(select_result([item]))
        error, helper_calls = self._invoke_select(item)
        self.assertIsNotNone(error)
        self.assertEqual(helper_calls, 0)

    def test_02_candidate_non_object_rejected(self):
        item = ranked_item()
        item["packaging_request"] = {
            "candidate": "not-an-object",
            "assets": valid_assets(),
        }
        with self.assertRaises(BridgeError):
            draft._validated_select(select_result([item]))
        error, helper_calls = self._invoke_select(item)
        self.assertIsNotNone(error)
        self.assertEqual(helper_calls, 0)

    def test_03_missing_assets_rejected(self):
        item = ranked_item()
        del item["packaging_request"]["assets"]
        with self.assertRaises(BridgeError):
            draft._validated_select(select_result([item]))
        error, helper_calls = self._invoke_select(item)
        self.assertIsNotNone(error)
        self.assertEqual(helper_calls, 0)

    def test_04_assets_non_object_rejected(self):
        item = ranked_item()
        item["packaging_request"] = {
            "candidate": valid_candidate(),
            "assets": ["not-an-object"],
        }
        with self.assertRaises(BridgeError):
            draft._validated_select(select_result([item]))
        error, helper_calls = self._invoke_select(item)
        self.assertIsNotNone(error)
        self.assertEqual(helper_calls, 0)

    def test_05_each_candidate_field_missing_rejected(self):
        for field in draft.PACKAGING_CANDIDATE_FIELDS:
            with self.subTest(field=field):
                item = ranked_item()
                del item["packaging_request"]["candidate"][field]
                with self.assertRaises(BridgeError):
                    draft._validated_select(select_result([item]))

    def test_06_each_asset_boolean_missing_rejected(self):
        for field in draft.PACKAGING_ASSET_BOOL_FIELDS:
            with self.subTest(field=field):
                item = ranked_item()
                del item["packaging_request"]["assets"][field]
                with self.assertRaises(BridgeError):
                    draft._validated_select(select_result([item]))

    def test_07_invalid_candidate_enum_or_type_rejected(self):
        bad_values = [
            ("content_type", "GOSSIP"),
            ("content_shape", "HOT_TAKE"),
            ("timeliness", "SOMEDAY"),
            ("verification_status", "MAYBE"),
            ("source_grounding", "VIBES"),
            ("audience_value", "HUGE"),
            ("distinct_beat_count", -1),
            ("distinct_beat_count", True),
            ("distinct_beat_count", "3"),
            ("depicts_real_world_subject", 1),
            ("still_developing", "no"),
            ("visual_requirement", "SORT_OF"),
        ]
        for field, value in bad_values:
            with self.subTest(field=field, value=value):
                item = ranked_item()
                item["packaging_request"]["candidate"][field] = value
                with self.assertRaises(BridgeError):
                    draft._validated_select(select_result([item]))
        # Non-boolean asset signals rejected (0/1 are not booleans).
        for field in draft.PACKAGING_ASSET_BOOL_FIELDS:
            with self.subTest(field=field):
                item = ranked_item()
                item["packaging_request"]["assets"][field] = 1
                with self.assertRaises(BridgeError):
                    draft._validated_select(select_result([item]))

    def test_08_forbidden_model_owned_fields_rejected(self):
        for field in (
            "FORMAT_DECISION",
            "FORMAT_REASON",
            "VISUAL_STYLE",
            "slide_count_recommendation",
        ):
            with self.subTest(field=field):
                item = ranked_item()
                item["packaging_request"]["candidate"][field] = "CAROUSEL"
                with self.assertRaises(BridgeError):
                    draft._validated_select(select_result([item]))
        # Schema itself must not admit evaluator outputs.
        schema_candidate = draft.SELECT_SCHEMA["properties"]["ranked"]["items"][
            "properties"
        ]["packaging_request"]["properties"]["candidate"]
        for field in (
            "FORMAT_DECISION",
            "FORMAT_REASON",
            "VISUAL_STYLE",
            "slide_count_recommendation",
        ):
            self.assertNotIn(field, schema_candidate["properties"])
            self.assertNotIn(field, schema_candidate["required"])

    def test_09_exact_production_failure_shape_rejected(self):
        # 2026-10-02 20:45: evaluator read-only diagnostic was
        # PACKAGING_INPUT_INVALID: candidate must be an object.
        item = ranked_item("anthropic-robots-physical-work-exposure-2026-10-02")
        item["packaging_request"] = {
            "candidate": "anthropic robots summary text, not an object",
            "assets": valid_assets(),
        }
        with self.assertRaises(BridgeError):
            draft._validated_select(select_result([item]))
        # The deterministic evaluator fails closed on the same shape.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prod = root / "social/drafts/production"
            prod.mkdir(parents=True)
            request_path = prod / "request.json"
            request_path.write_text(
                json.dumps(item["packaging_request"]), encoding="utf-8"
            )
            with self.assertRaisesRegex(BridgeError, "candidate must be an object"):
                evaluator_cli.load_validated_request(request_path, root=root)
            self.assertFalse(
                list(prod.glob("*-packaging-decision.json")),
                "no receipt may exist for the malformed shape",
            )

    def test_10_valid_packaging_request_passes(self):
        item = ranked_item()
        validated = draft._validated_select(select_result([item]))
        self.assertEqual(len(validated["ranked"]), 1)
        # Schema requires exactly the authoritative field sets.
        schema_candidate = draft.SELECT_SCHEMA["properties"]["ranked"]["items"][
            "properties"
        ]["packaging_request"]["properties"]["candidate"]
        self.assertEqual(
            set(schema_candidate["required"]), set(draft.PACKAGING_CANDIDATE_FIELDS)
        )
        schema_assets = draft.SELECT_SCHEMA["properties"]["ranked"]["items"][
            "properties"
        ]["packaging_request"]["properties"]["assets"]
        self.assertEqual(
            set(schema_assets["required"]), set(draft.PACKAGING_ASSET_BOOL_FIELDS)
        )
        # The valid request also passes the REAL evaluator end to end.
        receipt = receipt_mod.evaluate_request(
            "probe-candidate-one",
            {"candidate": valid_candidate(), "assets": valid_assets()},
        )
        self.assertEqual(receipt["POST_DECISION"], "POST")

    def test_11_malformed_asset_descriptor_rejected_before_write(self):
        cases = {
            "missing-schema": lambda d: d.pop("schema"),
            "wrong-schema": lambda d: d.update(schema="nullone.packaging-asset.v9"),
            "candidate-mismatch": lambda d: d.update(candidate_id="someone-else"),
            "blank-candidate": lambda d: d.update(candidate_id=" "),
            "unknown-kind": lambda d: d.update(asset_kind="GENERATED_IMAGE"),
            "file-backed-no-path": lambda d: d.update(
                asset_kind="REAL_PHOTO", local_path=None
            ),
            "file-backed-blank-path": lambda d: d.update(
                asset_kind="SOURCE_SCREENSHOT", local_path="  "
            ),
            "file-backed-no-provenance": lambda d: d.update(
                asset_kind="REAL_PHOTO",
                local_path="social/drafts/production/evidence.png",
                provenance="  ",
            ),
            "none-names-file": lambda d: d.update(
                local_path="social/drafts/production/evidence.png"
            ),
            "extra-key": lambda d: d.update(VISUAL_STYLE="REAL_PHOTO"),
            "non-object": "not-a-dict",
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                item = ranked_item()
                if name == "non-object":
                    item["asset"] = "not-a-dict"
                else:
                    descriptor = valid_descriptor(item["candidate_id"])
                    mutate(descriptor)
                    item["asset"] = descriptor
                with self.assertRaises(BridgeError):
                    draft._validated_select(select_result([item]))
                error, helper_calls = self._invoke_select(item)
                self.assertIsNotNone(error, name)
                self.assertEqual(helper_calls, 0, name)

    def test_12_valid_none_descriptor_passes(self):
        item = ranked_item()
        validated = draft._validated_select(select_result([item]))
        self.assertEqual(validated["ranked"][0]["asset"]["asset_kind"], "NONE")

    def test_13_file_backed_shape_passes_select_but_receipt_rules_downstream(self):
        item = ranked_item()
        item["asset"] = valid_descriptor(
            item["candidate_id"],
            asset_kind="REAL_PHOTO",
            local_path="social/drafts/production/evidence.png",
            provenance="Official source",
        )
        validated = draft._validated_select(select_result([item]))
        self.assertEqual(
            validated["ranked"][0]["asset"]["asset_kind"], "REAL_PHOTO"
        )
        # The SINGLE_FACT/NONE-grounding request earns an EDITORIAL_TYPOGRAPHY
        # receipt needing NONE, so the REAL_PHOTO descriptor still fails
        # downstream where receipt/style authority lives.
        receipt = receipt_mod.evaluate_request(
            item["candidate_id"],
            {"candidate": valid_candidate(), "assets": valid_assets()},
        )
        self.assertEqual(
            receipt_mod.STYLE_TO_ASSET_KIND[receipt["VISUAL_STYLE"]], "NONE"
        )
        with self.assertRaisesRegex(BridgeError, "PACKAGING_ASSET_MISMATCH"):
            with tempfile.TemporaryDirectory() as td:
                receipt_mod.validate_asset_descriptor(
                    dict(item["asset"]), receipt, root=Path(td)
                )

    def test_14_evaluator_and_fallback_regressions_green(self):
        # Deterministic evaluator still decides the reviewed SINGLE_FACT
        # shape exactly as the packaging suite expects.
        receipt = receipt_mod.evaluate_request(
            "probe-candidate",
            {"candidate": valid_candidate(), "assets": valid_assets()},
        )
        self.assertEqual(receipt["FORMAT_DECISION"], "SINGLE_POST")
        self.assertEqual(
            receipt["FORMAT_REASON"], "SINGLE_FACT_FITS_SINGLE_POST"
        )
        self.assertEqual(
            policy.evaluate_packaging(
                {"candidate": valid_candidate(), "assets": valid_assets()}
            )["POST_DECISION"],
            "POST",
        )

    def test_15_no_external_calls(self):
        import nullone_claude as claude_mod  # noqa: E402

        with mock.patch.object(
            claude_mod.subprocess, "run", side_effect=AssertionError("no subprocess")
        ), mock.patch.object(
            draft.subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            item = ranked_item()
            self.assertEqual(
                len(draft._validated_select(select_result([item]))["ranked"]), 1
            )
            with self.assertRaises(BridgeError):
                bad = ranked_item()
                bad["packaging_request"] = {"candidate": None, "assets": {}}
                draft._validated_select(select_result([bad]))

    def test_17_split_content_type_authority_rejected_before_write(self):
        # Top-level NEWS + nested EXPLAINER: structurally valid enums,
        # but split authority (receipt would bind EXPLAINER while the
        # manifest builds from queue NEWS).
        item = ranked_item()
        self.assertEqual(item["content_type"], "NEWS")
        item["packaging_request"]["candidate"]["content_type"] = "EXPLAINER"
        with self.assertRaises(BridgeError):
            draft._validated_select(select_result([item]))
        error, helper_calls = self._invoke_select(item)
        self.assertIsNotNone(error)
        self.assertEqual(helper_calls, 0)

    def test_18_matching_content_type_passes(self):
        item = ranked_item()
        item["content_type"] = "EXPLAINER"
        item["packaging_request"]["candidate"]["content_type"] = "EXPLAINER"
        validated = draft._validated_select(select_result([item]))
        self.assertEqual(
            validated["ranked"][0]["packaging_request"]["candidate"]["content_type"],
            validated["ranked"][0]["content_type"],
        )
        # The default NEWS/NEWS chain passes as well.
        defaulted = draft._validated_select(select_result([ranked_item()]))
        self.assertEqual(
            defaulted["ranked"][0]["packaging_request"]["candidate"]["content_type"],
            "NEWS",
        )

    def test_16_no_production_writes(self):
        before = workspace_prod_snapshot()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_queue(root, queue_entry())
            bad = ranked_item()
            del bad["packaging_request"]["candidate"]["visual_requirement"]
            with mock.patch.object(
                draft, "run_structured", return_value=select_result([bad])
            ), mock.patch.object(
                draft, "_run_helper", side_effect=AssertionError("no helpers")
            ):
                with self.assertRaises(BridgeError):
                    draft.invoke_draft(
                        prompt="p", workspace=root, model="sonnet", timeout=900
                    )
            leftovers = sorted(
                str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
            )
            self.assertTrue(
                all("packaging-request" not in name for name in leftovers),
                leftovers,
            )
            self.assertTrue(
                all("packaging-asset" not in name for name in leftovers), leftovers
            )
        self.assertEqual(workspace_prod_snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
