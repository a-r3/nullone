#!/usr/bin/env python3
"""Offline contract tests for recovery guarantees (issue #9 decision scope).

Proves the ADR/contract/policy declare every state class with honest
statuses, keep ordinary RPO/RTO as pending targets, forbid replay, and
exclude credentials — without inventing zero-loss guarantees.
No network, no production, no mutation.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADR = ROOT / "docs" / "adr" / "ADR-01-recovery-and-artifact-durability.md"
CONTRACT = ROOT / "docs" / "operations" / "recovery-contract.md"
POLICY = ROOT / "ops" / "recovery-policy.json"

REQUIRED_CLASSES = {
    "repository_source",
    "release_deploy_metadata",
    "candidate_queue",
    "run_outcomes_and_editorial_artifacts",
    "publication_attempt_history",
    "final_authorization_evidence",
    "notifier_state",
    "rendered_and_source_media",
    "openclaw_automation_config",
    "credentials_oauth_sessions",
}

FORBIDDEN_SECRET_MARKERS = (
    "ZERNIO_ANALYTICS_API_TOKEN=",
    "ZERNIO_DRAFT_API_TOKEN=",
    "ZERNIO_PUBLISH_API_TOKEN=",
    "bearer ",
    "BEGIN PRIVATE KEY",
    "telegram-owner-id",
)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class RecoveryContractTests(unittest.TestCase):
    def test_policy_parses_and_covers_all_classes(self):
        policy = json.loads(POLICY.read_text())
        self.assertEqual(policy["schema"], "nullone.recovery-policy/v1")
        names = {c["name"] for c in policy["state_classes"]}
        self.assertEqual(names, REQUIRED_CLASSES)
        for cls in policy["state_classes"]:
            for field in ("examples", "authoritative_copy", "guarantee_status",
                          "publisher_required", "retention_class",
                          "restore_priority", "replay_rule"):
                self.assertIn(field, cls, f"{cls['name']} missing {field}")
            self.assertIn(cls["guarantee_status"],
                          ("PROVEN", "DESIGN_TARGET", "CURRENT_LIMITATION",
                           "EXTERNAL_AUTHORITY"))

    def test_ordinary_rpo_rto_targets_and_pending_acceptance(self):
        policy = json.loads(POLICY.read_text())
        by_name = {c["name"]: c for c in policy["state_classes"]}
        for ordinary in ("candidate_queue",
                         "run_outcomes_and_editorial_artifacts",
                         "notifier_state"):
            self.assertLessEqual(by_name[ordinary]["rpo_seconds"], 900)
            self.assertLessEqual(by_name[ordinary]["rto_seconds"], 14400)
        self.assertEqual(policy["operator_acceptance"], "PENDING_RAUF_ALIZADA")
        contract = read(CONTRACT)
        self.assertIn("OPERATOR_ACCEPTANCE", contract)
        self.assertIn("PENDING_RAUF_ALIZADA", contract)

    def test_critical_state_not_replayable(self):
        policy = json.loads(POLICY.read_text())
        by_name = {c["name"]: c for c in policy["state_classes"]}
        for critical in ("publication_attempt_history",
                         "final_authorization_evidence"):
            cls = by_name[critical]
            self.assertTrue(cls["publisher_required"])
            self.assertIsNone(cls["rpo_seconds"])
            self.assertIsNone(cls["rto_seconds"])
            self.assertEqual(cls["guarantee_status"], "CURRENT_LIMITATION")
            self.assertIn("PUBLISHER_STATE=DISABLED", cls["safety_rule"])
            self.assertIn("AUTOMATIC_RETRY=FORBIDDEN", cls["safety_rule"])

    def test_unknown_forbids_retry_in_docs(self):
        for path in (ADR, CONTRACT):
            text = read(path)
            self.assertIn("CHECK_REQUIRED", text)
            self.assertIn("AUTOMATIC_RETRY=FORBIDDEN", text)

    def test_presigned_url_not_durable(self):
        contract = read(CONTRACT)
        self.assertIn("Signed/presigned URLs are NOT durable", contract)
        policy = json.loads(POLICY.read_text())
        media = next(c for c in policy["state_classes"]
                     if c["name"] == "rendered_and_source_media")
        self.assertEqual(media["replay_rule"], "url-is-not-a-copy")

    def test_credentials_excluded(self):
        blob = POLICY.read_text()
        for marker in FORBIDDEN_SECRET_MARKERS:
            self.assertNotIn(marker, blob)
        policy = json.loads(blob)
        creds = next(c for c in policy["state_classes"]
                     if c["name"] == "credentials_oauth_sessions")
        self.assertEqual(creds["retention_class"], "never-in-git")
        self.assertEqual(creds["guarantee_status"], "EXTERNAL_AUTHORITY")
        contract = " ".join(read(ADR).split())
        self.assertIn("NEVER part of recovery artifacts", contract)

    def test_terminal_receipts_historical(self):
        for path in (ADR, CONTRACT):
            text = read(path)
            self.assertIn("terminal", text.lower())
        contract = " ".join(read(CONTRACT).split())
        self.assertIn("does not rewrite them", contract)

    def test_merge_deploy_separate(self):
        self.assertIn("MERGE != DEPLOY", read(CONTRACT))

    def test_no_zero_loss_fabrication(self):
        # Zero-loss language may appear ONLY as an explicitly rejected
        # alternative or a labeled future target — never as a current claim.
        adr_low = read(ADR).lower()
        self.assertIn("zero-loss sla for everything.** rejected", adr_low)
        self.assertIn("would be a fabricated guarantee", adr_low)
        self.assertIn("design_target only", adr_low)
        for path in (ADR, CONTRACT):
            low = " ".join(read(path).lower().split())
            self.assertNotIn("zero-loss sla is accepted", low)
            self.assertNotIn("we guarantee zero loss", low)
            self.assertNotIn("current guarantee: rpo=0", low)
        policy = json.loads(POLICY.read_text())
        for cls in policy["state_classes"]:
            if cls["guarantee_status"] in ("DESIGN_TARGET", "CURRENT_LIMITATION",
                                           "EXTERNAL_AUTHORITY"):
                self.assertNotEqual(
                    (cls["rpo_seconds"], cls["guarantee_status"]), (0, "PROVEN"))

    def test_sqlite_none_documented(self):
        contract = read(CONTRACT)
        self.assertIn("CURRENT_SQLITE_CRITICAL_STATE=NONE", contract)
        self.assertIn("checkpoint", contract)

    def test_tabletop_scenarios_present(self):
        contract = read(CONTRACT)
        for marker in ("KEY LOST", "attempts=0", "SIGNED URL EXPIRED",
                       "UNREACHABLE DURING RECOVERY", "PARTIAL",
                       "REMOTE BACKUP OUTAGE", "DISK LOSS"):
            self.assertIn(marker, contract)

    def test_issue10_boundary(self):
        contract = read(CONTRACT)
        self.assertIn("#9 = RECOVERY GUARANTEE / ADR", contract)
        self.assertIn("#10 = RESTORE DRILL / EMPIRICAL PROOF", contract)

    def test_adr_structure(self):
        text = read(ADR)
        for section in ("Status", "Context", "Decision", "Alternatives",
                        "Consequences", "Migration",
                        "DATA AVAILABILITY", "PUBLICATION SAFETY"):
            self.assertIn(section, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
