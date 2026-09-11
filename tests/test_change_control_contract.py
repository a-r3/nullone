#!/usr/bin/env python3
"""Offline contract tests for auditable change control (issue #7 residual).

Proves the governance documents and templates retain their required
fields and separations. No network, no production, no mutation.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "contracts" / "change-control.md"
WORKFLOW = ROOT / "docs" / "REPOSITORY_WORKFLOW.md"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template" / "pull_request_template.md"
ISSUE_TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "engineering-change.md"
RELEASE_DOC = ROOT / "docs" / "deployment" / "release-cli.md"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class ChangeControlContractTests(unittest.TestCase):
    def test_merge_deploy_separation(self):
        text = read(CONTRACT)
        for required in (
            "MERGED != DEPLOYED",
            "DEPLOYED != HEALTHY",
            "SCHEDULER_SUCCESS != DOMAIN_SUCCESS",
            "HUMAN PR REVIEW != CONTENT PUBLICATION APPROVAL",
        ):
            self.assertIn(required, text)

    def test_exact_head_review_required(self):
        text = read(CONTRACT)
        for required in (
            "reviewed_head_sha",
            "EXACT head SHA",
            "decision",
            "APPROVE",
            "REQUEST_CHANGES",
            "ci_evidence",
            "deployment_required",
        ):
            self.assertIn(required, text)

    def test_stale_review_on_head_change(self):
        text = read(CONTRACT).lower()
        self.assertIn("stale", text)
        self.assertIn("new exact-head", text)

    def test_one_person_manual_signoff(self):
        text = read(CONTRACT)
        self.assertIn("one-person", text)
        self.assertIn("manual sign-off", text)
        self.assertIn("advisory only", text)
        # must not pretend native self-approval exists
        self.assertIn("does NOT pretend", text)

    def test_deployment_change_record(self):
        text = read(CONTRACT)
        for required in (
            "change_id",
            "source_pr",
            "reviewed_sha",
            "target_sha",
            "preflight_result",
            "deployed_by",
            "backup_or_rollback_reference",
            "post_deploy_validation",
            "ROLLED_BACK",
            "CHECK_REQUIRED",
        ):
            self.assertIn(required, text)
        self.assertIn("deployment_required", text)

    def test_deployment_record_forbids_secrets(self):
        text = read(CONTRACT)
        low = text.lower()
        self.assertIn("never include", low)
        for forbidden in ("secrets", "tokens", "credentials", "telegram owner"):
            self.assertIn(forbidden, low)

    def test_publication_approval_separate(self):
        text = read(CONTRACT)
        self.assertIn("two-stage", text)
        self.assertIn("never a substitute", text)

    def test_no_auto_deploy_claim(self):
        for path in (CONTRACT, WORKFLOW, PR_TEMPLATE):
            text = read(path)
            self.assertNotIn("automatic deployment", text.lower().replace(
                "no automatic production deployment", "").replace(
                "no automatic background deployment", "").replace(
                "no automatic background deploy", ""))
        contract = read(CONTRACT)
        self.assertIn("No merge, timer, bot, or", contract)

    def test_pr_template_fields(self):
        text = read(PR_TEMPLATE)
        for required in (
            "Issue / intent",
            "Change type",
            "Production impact",
            "Deployment required? YES/NO",
            "Exact head SHA",
            "CI evidence",
            "Risk / rollback notes",
            "Human review receipt",
            "Post-merge deployment status",
            "REVIEW_RECEIPT",
            "reviewed_head_sha",
        ):
            self.assertIn(required, text)

    def test_issue_template_minimal(self):
        self.assertTrue(ISSUE_TEMPLATE.is_file())
        text = read(ISSUE_TEMPLATE)
        for required in (
            "Purpose",
            "Scope",
            "Non-goals",
            "Acceptance criteria",
            "Production impact",
            "Risk",
            "Dependencies",
        ):
            self.assertIn(required, text)
        low = text.lower()
        self.assertNotIn("secret", low)
        self.assertNotIn("token", low)

    def test_workflow_references_contracts(self):
        text = read(WORKFLOW)
        self.assertIn("docs/contracts/change-control.md", text)
        self.assertIn("docs/deployment/release-cli.md", text)
        self.assertIn("exact-head", text)
        self.assertIn("does NOT claim technical enforcement", text)

    def test_release_cli_not_duplicated(self):
        # The contract references PR102's implementation; it must not
        # re-specify release-CLI internals (policy blobs, backup dirs, modes).
        text = read(CONTRACT)
        self.assertIn("release-cli.md", text)
        for internal in ("MANAGED_TARGET", "managed_modes", "POLICY_SHA256", "0644/0755"):
            self.assertNotIn(internal, text)
        # ...but the release doc itself still owns them
        release = read(RELEASE_DOC)
        self.assertIn("POLICY_SHA256", release)


if __name__ == "__main__":
    unittest.main(verbosity=2)
