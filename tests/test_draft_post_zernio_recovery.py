#!/usr/bin/env python3
"""Draft post-Zernio completion recovery (local-only, offline).

Reproduces the proven 2026-10-14 state: remote draft exists, manifest
DRAFT_CREATED, queue DRAFTED, preview payload present, but no ledger
row, no report and no Telegram delivery proof.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_draft_recovery as rec  # noqa: E402

CID = "microsoft-execution-containers-agent-containment-2026-10-14"
MID = f"2026-10-14-{CID}"
RPID = "6acfb1c95c334804b44ef452"


def make_root(td, *, receipt=None, ledger_rows=(), report=False, first_stage=False):
    root = Path(td)
    (root / rec.MANIFEST_DIR).mkdir(parents=True)
    (root / rec.DRAFTS_DIR).mkdir(parents=True)
    (root / "social/state").mkdir(parents=True)
    manifest = {
        "manifest_id": MID,
        "candidate_id": CID,
        "topic": "Microsoft Execution Containers reach GA",
        "topic_cluster": "microsoft-execution-containers-agent-containment",
        "content_type": "EXPLAINER",
        "format": "FEED",
        "verification": "PASS",
        "review": {"state": "DRAFT_CREATED", "zernio_draft_id": RPID,
                   "created_at": "2026-10-14T16:46:01.836033+00:00"},
        "approval": {"first_stage": first_stage, "final_publish": False},
        "publication": {"state": "NOT_REQUESTED", "attempts": 0},
    }
    (root / rec.MANIFEST_DIR / f"{MID}.json").write_text(json.dumps(manifest))
    if receipt:
        (root / rec.DRAFTS_DIR / f"2026-10-14-{CID}-telegram-delivery.json").write_text(
            json.dumps({"schema": rec.DELIVERY_RECEIPT_SCHEMA, "candidate_id": CID,
                        "review_post_id": RPID, **receipt}))
    (root / rec.LEDGER_PATH).write_text(
        "".join(json.dumps(r) + "\n" for r in ledger_rows))
    if report:
        (root / rec.PUBLISHER_DIR).mkdir(parents=True)
        (root / rec.PUBLISHER_DIR / f"{MID}-draft.md").write_text("x\n")
    return root


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


class RecoveryTests(unittest.TestCase):
    def test_inspect_proven_state_is_read_only_and_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td)
            before = snapshot(root)
            items = rec.inspect_all(root)
            self.assertEqual(snapshot(root), before)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["completion"], "MISSING_LEDGER_AND_REPORT")
            self.assertEqual(items[0]["telegram_delivery"], "NO_RECEIPT")

    def test_apply_writes_only_ledger_and_report_without_claiming_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td)
            before = snapshot(root)
            out = rec.apply_one(root, MID)
            self.assertEqual(out["written"], ["LEDGER_ROW", "REPORT"])
            after = snapshot(root)
            changed = {k for k in after if before.get(k) != after[k]}
            self.assertEqual(changed, {rec.LEDGER_PATH, f"{rec.PUBLISHER_DIR}/{MID}-draft.md"})
            row = json.loads(after[rec.LEDGER_PATH].decode().splitlines()[-1])
            self.assertEqual(row["event"], "DRAFT_CREATED")
            self.assertEqual(row["review_post_id"], RPID)
            self.assertEqual(row["telegram_delivery"], "NO_RECEIPT")
            self.assertNotIn("telegram_notified", row)
            self.assertNotIn("telegram_approval_message_id", row)
            # manifest and queue untouched
            mpath = f"{rec.MANIFEST_DIR}/{MID}.json"
            self.assertEqual(after[mpath], before[mpath])

    def test_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td)
            rec.apply_one(root, MID)
            once = snapshot(root)
            out = rec.apply_one(root, MID)
            self.assertEqual(out["written"], [])
            self.assertEqual(snapshot(root), once)
            self.assertEqual(rec.inspect_all(root)[0]["completion"], "COMPLETE")

    def test_sent_receipt_is_the_only_source_of_delivery_claim(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, receipt={"state": "SENT", "approval_message_id": "777"})
            rec.apply_one(root, MID)
            row = json.loads((root / rec.LEDGER_PATH).read_text().splitlines()[-1])
            self.assertTrue(row["telegram_notified"])
            self.assertEqual(row["telegram_approval_message_id"], "777")

    def test_sending_and_failed_receipts_stay_unconfirmed(self):
        for state, expect in (("SENDING", "SENDING_UNCONFIRMED"), ("NOTIFY_FAILED", "NOTIFY_FAILED")):
            with tempfile.TemporaryDirectory() as td:
                root = make_root(td, receipt={"state": state})
                self.assertEqual(rec.inspect_all(root)[0]["telegram_delivery"], expect)
                rec.apply_one(root, MID)
                row = json.loads((root / rec.LEDGER_PATH).read_text().splitlines()[-1])
                self.assertNotIn("telegram_notified", row)
                self.assertEqual(row["telegram_delivery"], expect)

    def test_existing_ledger_row_not_duplicated(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, ledger_rows=[
                {"event": "DRAFT_CREATED", "review_post_id": RPID, "candidate_id": CID}])
            out = rec.apply_one(root, MID)
            self.assertEqual(out["written"], ["REPORT"])
            rows = (root / rec.LEDGER_PATH).read_text().splitlines()
            self.assertEqual(len(rows), 1)

    def test_approved_or_nondraft_manifest_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, first_stage=True)
            self.assertEqual(rec.inspect_all(root), [])
            with self.assertRaises(rec.RecoveryError):
                rec.apply_one(root, MID)
            with self.assertRaises(rec.RecoveryError):
                rec.apply_one(root, "../etc/passwd")

    def test_module_has_no_external_surface(self):
        import ast

        tree = ast.parse((SCRIPTS / "nullone_draft_recovery.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(
            imported, {"__future__", "argparse", "json", "re", "sys", "pathlib", "typing"}
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
