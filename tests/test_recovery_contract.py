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

    def test_ordinary_rpo_rto_targets_and_acceptance(self):
        policy = json.loads(POLICY.read_text())
        by_name = {c["name"]: c for c in policy["state_classes"]}
        for ordinary in ("candidate_queue",
                         "run_outcomes_and_editorial_artifacts",
                         "notifier_state"):
            self.assertLessEqual(by_name[ordinary]["rpo_seconds"], 900)
            self.assertLessEqual(by_name[ordinary]["rto_seconds"], 14400)
        acceptance = policy["operator_acceptance"]
        self.assertEqual(acceptance["status"], "ACCEPTED")
        self.assertEqual(acceptance["accepted_by"], "Rauf Alizada (@a-r3)")
        self.assertEqual(acceptance["ordinary_rpo_target_seconds"], 900)
        self.assertEqual(acceptance["ordinary_rto_target_seconds"], 14400)
        contract = read(CONTRACT)
        self.assertIn("OPERATOR_ACCEPTANCE=ACCEPTED", contract)
        self.assertIn("ACCEPTED_BY=Rauf Alizada (@a-r3)", contract)

    def test_accepted_target_still_design_target_not_proven(self):
        policy = json.loads(POLICY.read_text())
        by_name = {c["name"]: c for c in policy["state_classes"]}
        self.assertEqual(by_name["candidate_queue"]["guarantee_status"],
                         "DESIGN_TARGET")
        text = read(ADR)
        self.assertIn("accepted DESIGN TARGETS", text)
        self.assertIn("no backup infrastructure exists yet", text)
        contract = read(CONTRACT)
        self.assertIn("CURRENT_LIMITATION (no snapshot job)", contract)

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

    def test_no_unresolved_owner_or_retention(self):
        policy = json.loads(POLICY.read_text())
        for cls in policy["state_classes"]:
            owner = cls.get("recovery_owner", "")
            self.assertTrue(owner and "TBD" not in owner,
                            f"{cls['name']} owner unresolved")
            retention = json.dumps(cls.get("retention_policy", ""))
            self.assertTrue(retention and "TBD" not in retention
                            and "undecided" not in retention.lower(),
                            f"{cls['name']} retention unresolved")
        contract = read(CONTRACT)
        for banned in ("Owner: TBD", "owner TBD", "retention owner TBD",
                       "operator-defined (undecided", "owner TBD (pending)"):
            self.assertNotIn(banned, contract)

    def test_key_recovery_custodian_defined_without_material(self):
        policy = json.loads(POLICY.read_text())
        custodian = policy.get("key_recovery_custodian", "")
        self.assertIn("Rauf Alizada", custodian)
        self.assertIn("no key material", custodian.lower())
        contract = read(CONTRACT)
        self.assertIn("Rauf Alizada (@a-r3)", contract)
        blob = (POLICY.read_text() + read(CONTRACT) + read(ADR)).lower()
        for marker in ("private key", "secret key=", "key material:"):
            self.assertNotIn(marker, blob)

    def test_structural_not_exact_byte(self):
        contract = read(CONTRACT)
        self.assertIn("STRUCTURAL re-render is NOT exact-byte recovery", contract)
        self.assertIn("NEW DERIVED ARTIFACT", contract)

    def test_published_media_exact_byte_retention(self):
        policy = json.loads(POLICY.read_text())
        media = next(c for c in policy["state_classes"]
                     if c["name"] == "rendered_and_source_media")
        self.assertTrue(media["exact_bytes_required"])
        self.assertIn("project-lifetime", media["retention_policy"])
        contract = read(CONTRACT)
        self.assertIn("retain the actual output bytes independently", contract)
        self.assertIn("NON_RECOVERABLE_FROM_SOURCE", contract)

    def test_missing_notifier_history_no_auto_resend(self):
        policy = json.loads(POLICY.read_text())
        notifier = next(c for c in policy["state_classes"]
                        if c["name"] == "notifier_state")
        action = notifier["missing_history_action"]
        self.assertIn("NOTIFIER_STATE=CHECK_REQUIRED", action)
        self.assertIn("AUTOMATIC_RESEND=FORBIDDEN", action)
        contract = read(CONTRACT)
        self.assertIn("automatic result resend is FORBIDDEN", contract)

    def test_remote_draft_missing_history_no_retry(self):
        for path in (ADR, CONTRACT):
            text = read(path)
            self.assertIn("remote DRAFT proves", text)
        contract = read(CONTRACT)
        self.assertIn("tabletop", contract.lower())
        self.assertIn("ATTEMPTS=0 + REMOTE DRAFT + CRITICAL HISTORY MISSING",
                      contract)

    def test_publish_ledger_classified_not_authority(self):
        contract = read(CONTRACT)
        self.assertIn("social/state/publish-ledger.jsonl", contract)
        self.assertIn("NOT sufficient by itself to create authorization", contract)
        self.assertIn("NOT sufficient by itself", contract)
        self.assertIn("Neither queue nor ledger is publication", contract)

    def test_current_vs_legacy_oauth_distinguished(self):
        contract = read(CONTRACT)
        self.assertIn("LEGACY_OR_EXTERNAL_CONTROLLED", contract)
        self.assertIn("CURRENT_EXTERNAL_AUTHORITY", contract)

    def test_critical_retention_indefinite(self):
        policy = json.loads(POLICY.read_text())
        for name in ("publication_attempt_history",
                     "final_authorization_evidence"):
            cls = next(c for c in policy["state_classes"] if c["name"] == name)
            self.assertIn("indefinite", cls["retention_policy"])

    def test_adr_governance_status_truthful(self):
        text = read(ADR)
        self.assertIn("Status: ACCEPTED", text)
        self.assertIn("Rauf Alizada (@a-r3)", text)
        self.assertIn("subject to final merge of the reviewed head", text)
        self.assertIn("no backup infrastructure exists yet", text)
        self.assertIn("ordinary_rpo_target_seconds: 900", text)
        self.assertIn("ordinary_rto_target_seconds: 14400", text)
        # Accepted as design targets, never as deployed infrastructure.
        self.assertIn("DESIGN TARGETS", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
