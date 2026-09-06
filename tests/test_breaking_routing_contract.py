#!/usr/bin/env python3
"""Validate decision data only; no severity, identity or routing implementation."""
from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/breaking_routing_policy_v1.json"
DOC = ROOT / "docs/contracts/breaking-routing-policy-v1.md"
SCHEMA = "nullone.breaking-routing.v1"
SEVERITIES = {"NORMAL", "MATERIAL_BREAKING", "EXCEPTIONAL_BREAKING"}
REASONS = {
    "NORMAL_QUEUE": {"NORMAL_CADENCE"},
    "IMMEDIATE_STORY_DRAFT": {"MATERIAL_TIME_VALUE", "EXCEPTIONAL_STORY_ONLY"},
    "IMMEDIATE_STORY_AND_MAIN_DRAFT": {"EXCEPTIONAL_MAIN_VALUE"},
    "SUPPRESS_DUPLICATE": {
        "EXISTING_CONSEQUENTIAL_STATE", "UNRESOLVED_EVENT_HISTORY",
        "EXISTING_DRAFT_REQUEST", "CANDIDATE_EXCLUDED", "EXACT_EVENT_DUPLICATE",
        "SAME_EVENT_DIFFERENT_SOURCE",
    },
    "SUPPRESS_RECENT_COVERAGE": {"NO_INCREMENTAL_AUDIENCE_VALUE"},
    "BLOCKED_UNVERIFIED": {"EVIDENCE_INSUFFICIENT"},
    "BLOCKED_AMBIGUOUS_IDENTITY": {
        "IDENTITY_UNRESOLVED", "STATE_UNAVAILABLE_OR_CONFLICTING",
    },
    "BLOCKED_DRAFT_SAFETY": {
        "STORY_QUALITY_BLOCK", "STORY_LOAD_BLOCK", "DRAFT_DEPENDENCY_UNAVAILABLE",
    },
}
FOLLOW_UP_REASONS = {
    "AVAILABILITY_CHANGED", "OFFICIAL_NUMBER_CHANGED", "AFFECTED_REGION_CHANGED",
    "MATERIAL_CORRECTION", "PRODUCT_VERSION_CHANGED", "USER_CONSEQUENCE_CHANGED",
}
FIELDS = {
    "schema", "candidate_id", "assessment_ref", "state_snapshot_ref", "severity",
    "event", "verification", "dedup", "routing_decision", "reason_code",
    "reason_text", "draft_targets", "main_draft_justification",
    "reconciliation_required",
}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def references(value: object) -> bool:
    return isinstance(value, list) and all(text(item) for item in value)


def validate_output(out: dict) -> None:
    """Check an authored expected output, never derive one from a candidate."""
    check(set(out) == FIELDS, "output fields")
    check(out["schema"] == SCHEMA, "schema")
    for name in ("candidate_id", "assessment_ref", "state_snapshot_ref", "reason_text"):
        check(text(out[name]), name)
    route = out["routing_decision"]
    check(route in REASONS, "route")
    check(out["reason_code"] in REASONS[route], "route reason")
    check(out["severity"] is None or out["severity"] in SEVERITIES, "severity")
    check(type(out["reconciliation_required"]) is bool, "reconciliation flag")
    event, verification, dedup = out["event"], out["verification"], out["dedup"]
    check(set(event) == {"event_id", "development_id", "topic_id", "identity_basis", "identity_refs"}, "event fields")
    for name in ("event_id", "development_id"):
        check(event[name] is None or text(event[name]), name)
    check(text(event["topic_id"]), "topic_id")
    check(event["identity_basis"] in {"EXACT_IDENTIFIER", "CANONICAL_SOURCE", "NORMALIZED_CLAIM", "UNRESOLVED"}, "identity basis")
    check(references(event["identity_refs"]), "identity references")
    check(set(verification) == {"state", "evidence_refs"}, "verification fields")
    check(verification["state"] in {"UNVERIFIED", "PARTIAL", "PASS", "BLOCKED"}, "verification state")
    check(references(verification["evidence_refs"]), "evidence references")
    if verification["state"] == "PASS":
        check(bool(verification["evidence_refs"]), "PASS evidence")
    else:
        check(out["severity"] is None, "unverified severity")
    check(set(dedup) == {"decision", "matched_refs", "parent_development_id", "follow_up_reason"}, "dedup fields")
    check(dedup["decision"] in {"EXACT_DUPLICATE", "SAME_EVENT", "MATERIAL_FOLLOW_UP", "DISTINCT_EVENT", "AMBIGUOUS_IDENTITY"}, "dedup decision")
    check(references(dedup["matched_refs"]), "matched references")
    if dedup["decision"] == "MATERIAL_FOLLOW_UP":
        check(text(dedup["parent_development_id"]) and dedup["parent_development_id"] != event["development_id"], "follow-up parent")
        check(dedup["follow_up_reason"] in FOLLOW_UP_REASONS, "follow-up reason")
        check(bool(dedup["matched_refs"]), "follow-up coverage")
    else:
        check(dedup["parent_development_id"] is None and dedup["follow_up_reason"] is None, "unexpected follow-up")
    if dedup["decision"] in {"EXACT_DUPLICATE", "SAME_EVENT"}:
        check(bool(dedup["matched_refs"]), "duplicate coverage")
        check(route == "SUPPRESS_DUPLICATE", "duplicate acceleration")
    accelerated = route in {"IMMEDIATE_STORY_DRAFT", "IMMEDIATE_STORY_AND_MAIN_DRAFT"}
    if accelerated:
        check(verification["state"] == "PASS", "acceleration verification")
        check(out["severity"] in {"MATERIAL_BREAKING", "EXCEPTIONAL_BREAKING"}, "acceleration severity")
        check(text(event["event_id"]) and text(event["development_id"]), "acceleration identity")
        check(event["identity_basis"] != "UNRESOLVED" and bool(event["identity_refs"]), "acceleration identity evidence")
        check(dedup["decision"] in {"DISTINCT_EVENT", "MATERIAL_FOLLOW_UP"}, "acceleration dedup")
        check(not out["reconciliation_required"], "acceleration reconciliation")
    if route == "IMMEDIATE_STORY_AND_MAIN_DRAFT":
        check(out["severity"] == "EXCEPTIONAL_BREAKING", "main severity")
        check(out["draft_targets"] in (["STORY", "FEED"], ["STORY", "CAROUSEL"]), "main targets")
        check(text(out["main_draft_justification"]), "main justification")
    else:
        check(out["main_draft_justification"] is None, "unexpected main justification")
        check(out["draft_targets"] == (["STORY"] if accelerated else []), "targets")
    if route == "NORMAL_QUEUE":
        check(out["severity"] == "NORMAL" and verification["state"] == "PASS", "normal admission")
    if route == "BLOCKED_UNVERIFIED":
        check(verification["state"] != "PASS", "unverified block")
    if route == "BLOCKED_AMBIGUOUS_IDENTITY":
        check(out["reconciliation_required"], "ambiguity reconciliation")
    if route == "IMMEDIATE_STORY_DRAFT":
        expected_reason = "MATERIAL_TIME_VALUE" if out["severity"] == "MATERIAL_BREAKING" else "EXCEPTIONAL_STORY_ONLY"
        check(out["reason_code"] == expected_reason, "Story reason")


class BreakingRoutingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(FIXTURE.read_text())
        cls.doc = DOC.read_text()

    def test_authored_examples_and_document_agree(self):
        self.assertEqual(self.data["schema"], "nullone.breaking-routing-cases.v1")
        self.assertEqual(self.data["policy_schema"], SCHEMA)
        self.assertEqual(self.data["enforcement"], "NOT_IMPLEMENTED")
        cases = self.data["cases"]
        self.assertEqual([c["id"] for c in cases], [f"B34-{i:03d}" for i in range(1, 29)])
        table = [line for line in self.doc.splitlines() if line.startswith("| B34-")]
        self.assertEqual(len(table), len(cases))
        for case, line in zip(cases, table):
            with self.subTest(case=case["id"]):
                out, findings = case["expected"], case["input_findings"]
                validate_output(out)
                columns = [col.strip() for col in line.split("|")[1:-1]]
                self.assertEqual(columns[0], case["id"])
                self.assertEqual(columns[2:], [out["severity"] or "null", out["routing_decision"], out["reason_code"]])
                for field in ("assessment_ref", "state_snapshot_ref"):
                    self.assertEqual(out[field], findings[field])
                self.assertEqual(out["verification"]["state"], findings["verification_state"])
                self.assertEqual(out["verification"]["evidence_refs"], [e["ref"] for e in findings["evidence"]])
                self.assertEqual(findings["coverage_config_ref"], self.data["coverage_config"]["ref"])
                self.assertTrue(findings["state_variants"])
                if out["dedup"]["decision"] == "MATERIAL_FOLLOW_UP":
                    self.assertIn(findings["delta"]["evidence_ref"], out["verification"]["evidence_refs"])
                    self.assertNotEqual(findings["delta"]["parent_claim"], findings["delta"]["new_claim"])
                if out["draft_targets"]:
                    for gate in ("state_complete", "story_quality_pass", "story_load_pass", "dependencies_available"):
                        self.assertIs(findings[gate], True)
                    self.assertTrue(findings["timing_loss"])
                    self.assertTrue(findings["incremental_value"])
                if len(out["draft_targets"]) == 2:
                    self.assertIs(findings["main_selected"], True)
                    self.assertIs(findings["main_eligible"], True)
                    self.assertEqual(out["main_draft_justification"], findings["standalone_main_value"])
        self.assertEqual({c["expected"]["routing_decision"] for c in cases}, set(REASONS))

    def test_document_json_example(self):
        examples = re.findall(r"```json\n(.*?)\n```", self.doc, re.S)
        self.assertEqual(len(examples), 1)
        example = json.loads(examples[0])
        validate_output(example)
        self.assertEqual(example, self.data["cases"][2]["expected"])

    def test_synthetic_references_and_variant_coverage(self):
        raw = FIXTURE.read_text()
        for url in re.findall(r'https?://[^\s"<>]+', raw):
            self.assertEqual(urlsplit(url).netloc, "example.invalid")
        states = " ".join(variant for c in self.data["cases"] for variant in c["input_findings"]["state_variants"])
        for state in ("PUBLISHED", "UNKNOWN", "PUBLISH_IN_FLIGHT", "PUBLISHING", "PUBLISH_ACCEPTED", "READBACK_FAILED", "CHECK_REQUIRED", "FAILED", "REVIEW_UNKNOWN", "CREATE_IN_FLIGHT", "DRAFT_CREATED", "REJECTED", "LEGACY_DRAFT", "SUPERSEDED_DRAFT", "DRAFTED", "scheduled", "approval.first_stage=true"):
            self.assertIn(state, states)
        for reason in FOLLOW_UP_REASONS:
            self.assertIn(f"`{reason}`", self.doc)

    def test_rejects_unsafe_output_mutations(self):
        base = self.data["cases"][2]["expected"]
        mutations = [
            {"schema": "nullone.breaking-routing.v2"},
            {"routing_decision": "PUBLISH"},
            {"approval": True},
            {"draft_targets": ["FEED"]},
            {"severity": "MATERIAL_BREAKING"},
            {"main_draft_justification": None},
            {"reconciliation_required": True},
            {"verification": {"state": "PARTIAL", "evidence_refs": []}},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                out = copy.deepcopy(base)
                out.update(mutation)
                with self.assertRaises(ValueError):
                    validate_output(out)
        follow_up = copy.deepcopy(self.data["cases"][8]["expected"])
        follow_up["dedup"]["follow_up_reason"] = "NEW_HEADLINE"
        with self.assertRaises(ValueError):
            validate_output(follow_up)


if __name__ == "__main__":
    unittest.main()
