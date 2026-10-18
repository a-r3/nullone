#!/usr/bin/env python3
"""Draft post-Zernio completion recovery (local-only, offline).

Reproduces the proven 2026-10-14 state: remote draft exists, manifest
DRAFT_CREATED, queue DRAFTED, preview payload present, but no ledger
row, no report and no Telegram delivery proof.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_draft_recovery as rec  # noqa: E402

CID = "microsoft-execution-containers-agent-containment-2026-10-14"
CLUSTER = "microsoft-execution-containers-agent-containment"
TOPIC = "Microsoft Execution Containers reach GA"
MID = f"2026-10-14-{CID}"
RPID = "6acfb1c95c334804b44ef452"
OTHER = "someone-else"


def queue_entry(cid=CID, status="DRAFTED", topic=TOPIC, cluster=CLUSTER):
    return (
        f"- **candidate_id:** {cid}\n- **topic:** {topic}\n"
        f"- **topic_cluster:** {cluster}\n- **content_type:** EXPLAINER\n"
        f"- **status:** {status}\n- **verification_status:** PASS\n"
    )


def make_root(td, *, receipt=None, ledger_rows=(), report=False, first_stage=False,
              queue=None, raw_ledger=None):
    root = Path(td)
    (root / rec.MANIFEST_DIR).mkdir(parents=True)
    (root / rec.DRAFTS_DIR).mkdir(parents=True)
    (root / "social/state").mkdir(parents=True)
    manifest = {
        "manifest_id": MID, "candidate_id": CID, "topic": TOPIC,
        "topic_cluster": CLUSTER, "content_type": "EXPLAINER", "format": "FEED",
        "verification": "PASS",
        "review": {"state": "DRAFT_CREATED", "zernio_draft_id": RPID,
                   "created_at": "2026-10-14T16:46:01.836033+00:00"},
        "approval": {"first_stage": first_stage, "final_publish": False},
        "publication": {"state": "NOT_REQUESTED", "attempts": 0},
    }
    (root / rec.MANIFEST_DIR / f"{MID}.json").write_text(json.dumps(manifest))
    (root / "social/state/candidate-queue.md").write_text(
        queue if queue is not None else queue_entry())
    if receipt is not None:
        body = {"schema": rec.DELIVERY_RECEIPT_SCHEMA, "candidate_id": CID,
                "review_post_id": RPID}
        body.update(receipt)
        (root / rec.DRAFTS_DIR / f"2026-10-14-{CID}-telegram-delivery.json").write_text(
            json.dumps(body))
    (root / rec.LEDGER_PATH).write_text(
        raw_ledger if raw_ledger is not None
        else "".join(json.dumps(r) + "\n" for r in ledger_rows))
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
            self.assertEqual(items[0]["queue"], "QUEUE_DRAFTED")
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
            # Stable identity for current readers (candidate/manifest/review).
            for key, val in (("candidate_id", CID), ("manifest_id", MID),
                             ("review_post_id", RPID), ("event", "DRAFT_CREATED"),
                             ("result", "DRAFTED"), ("review_state", "DRAFT_CREATED")):
                self.assertEqual(row[key], val)
            self.assertEqual(row["telegram_delivery"], "NO_RECEIPT")
            self.assertNotIn("telegram_notified", row)
            self.assertNotIn("telegram_approval_message_id", row)
            report = after[f"{rec.PUBLISHER_DIR}/{MID}-draft.md"].decode()
            self.assertIn("telegram delivery: NO_RECEIPT", report)
            self.assertIn("queue status: DRAFTED (verified)", report)
            for path in (f"{rec.MANIFEST_DIR}/{MID}.json", "social/state/candidate-queue.md"):
                self.assertEqual(after[path], before[path])

    def test_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td)
            rec.apply_one(root, MID)
            once = snapshot(root)
            out = rec.apply_one(root, MID)
            self.assertEqual(out["written"], [])
            self.assertEqual(snapshot(root), once)
            self.assertEqual(rec.inspect_all(root)[0]["completion"], "COMPLETE")

    # -- ledger idempotency -------------------------------------------
    def _existing_row_no_duplicate(self, row):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, ledger_rows=[row])
            out = rec.apply_one(root, MID)
            self.assertEqual(out["written"], ["REPORT"])
            self.assertEqual(len((root / rec.LEDGER_PATH).read_text().splitlines()), 1)

    def test_existing_row_event_marker(self):
        self._existing_row_no_duplicate(
            {"event": "DRAFT_CREATED", "review_post_id": RPID, "candidate_id": CID})

    def test_existing_row_state_marker_from_complete_contract(self):
        self._existing_row_no_duplicate(
            {"candidate_id": CID, "review_post_id": RPID, "state": "DRAFT_CREATED"})

    def test_existing_row_result_marker_and_manifest_id(self):
        self._existing_row_no_duplicate(
            {"candidate_id": CID, "review_post_id": RPID, "result": "DRAFTED",
             "manifest_id": MID})

    def test_existing_row_without_candidate_but_same_review_id(self):
        self._existing_row_no_duplicate({"review_post_id": RPID, "state": "DRAFT_CREATED"})

    def test_conflicting_identity_fails_closed(self):
        bad_rows = [
            {"review_post_id": RPID, "candidate_id": OTHER, "state": "DRAFT_CREATED"},
            {"review_post_id": RPID, "candidate_id": 123, "event": "DRAFT_CREATED"},
            {"review_post_id": RPID, "candidate_id": CID, "manifest_id": "other-manifest",
             "event": "DRAFT_CREATED"},
            {"review_post_id": ["x"], "candidate_id": CID, "event": "DRAFT_CREATED"},
            # same candidate already completed under a different review id
            {"review_post_id": "6aaaaaaaaaaaaaaaaaaaaaaa", "candidate_id": CID,
             "event": "DRAFT_CREATED"},
            {"candidate_id": CID, "state": "DRAFT_CREATED"},
        ]
        for row in bad_rows:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as td:
                root = make_root(td, ledger_rows=[row])
                before = snapshot(root)
                self.assertEqual(rec.inspect_all(root)[0]["completion"], "LEDGER_CONFLICT")
                with self.assertRaises(rec.RecoveryError):
                    rec.apply_one(root, MID)
                self.assertEqual(snapshot(root), before)

    def test_unrelated_rows_do_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, ledger_rows=[
                {"event": "MORNING_EDITORIAL_SCAN", "candidate_id": CID, "status": "READY"},
                {"event": "DRAFT_CREATED", "review_post_id": "6bbb", "candidate_id": OTHER}])
            self.assertEqual(rec.apply_one(root, MID)["written"], ["LEDGER_ROW", "REPORT"])

    def test_unreadable_ledger_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, raw_ledger='{"event": "x"}\n{not json\n')
            before = snapshot(root)
            self.assertEqual(rec.inspect_all(root)[0]["completion"], "LEDGER_UNREADABLE")
            with self.assertRaises(rec.RecoveryError):
                rec.apply_one(root, MID)
            self.assertEqual(snapshot(root), before)

    def test_ledger_without_trailing_newline_stays_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, raw_ledger='{"event": "x"}')
            rec.apply_one(root, MID)
            lines = (root / rec.LEDGER_PATH).read_text().splitlines()
            self.assertEqual(len(lines), 2)
            [json.loads(line) for line in lines]

    # -- delivery proof binding ---------------------------------------
    def test_delivery_proof_matrix(self):
        cases = [
            ({"state": "SENT", "approval_message_id": "777"}, "SENT"),
            ({"state": "SENT", "approval_message_id": "777", "review_post_id": "6other"},
             "RECEIPT_INVALID"),
            ({"state": "SENT"}, "RECEIPT_INVALID"),
            ({"state": "SENT", "approval_message_id": ""}, "RECEIPT_INVALID"),
            ({"state": "SENT", "approval_message_id": "  "}, "RECEIPT_INVALID"),
            ({"state": "SENT", "approval_message_id": 777}, "RECEIPT_INVALID"),
            ({"state": "SENDING"}, "DELIVERY_UNCONFIRMED"),
            ({"state": "DELIVERY_UNCONFIRMED"}, "DELIVERY_UNCONFIRMED"),
            ({"state": "NOTIFY_FAILED"}, "RECEIPT_INVALID"),
            ({"state": "SENT", "approval_message_id": "7", "candidate_id": OTHER},
             "RECEIPT_INVALID"),
            ({"state": "SENT", "approval_message_id": "7", "schema": "x.v9"},
             "RECEIPT_INVALID"),
        ]
        for receipt, expect in cases:
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as td:
                root = make_root(td, receipt=receipt)
                item = rec.inspect_all(root)[0]
                self.assertEqual(item["telegram_delivery"], expect)
                rec.apply_one(root, MID)
                row = json.loads((root / rec.LEDGER_PATH).read_text().splitlines()[-1])
                if expect == "SENT":
                    self.assertTrue(row["telegram_notified"])
                    self.assertEqual(row["telegram_approval_message_id"], "777")
                else:
                    self.assertNotIn("telegram_notified", row)
                    self.assertNotIn("telegram_approval_message_id", row)
                    self.assertEqual(row["telegram_delivery"], expect)

    # -- queue verification -------------------------------------------
    def test_queue_states_refuse_apply_but_inspect_reports(self):
        cases = {
            "QUEUE_NOT_DRAFTED:READY": queue_entry(status="READY"),
            "QUEUE_NOT_DRAFTED:PUBLISHED": queue_entry(status="PUBLISHED"),
            "QUEUE_MISSING": queue_entry(cid="other-candidate"),
            "QUEUE_UNREADABLE_DUP": queue_entry() + "\n" + queue_entry(),  # queue authority rejects duplicates
            "QUEUE_IDENTITY_MISMATCH": queue_entry(topic="A different topic"),
            "QUEUE_MISMATCH_CLUSTER": queue_entry(cluster="other"),
            "QUEUE_UNREADABLE": None,
        }
        for expect, queue in cases.items():
            with self.subTest(expect=expect), tempfile.TemporaryDirectory() as td:
                root = make_root(td, queue=queue if queue is not None else "")
                if queue is None:
                    os.unlink(root / "social/state/candidate-queue.md")
                before = snapshot(root)
                item = rec.inspect_all(root)[0]
                want = {"QUEUE_MISMATCH_CLUSTER": "QUEUE_IDENTITY_MISMATCH",
                        "QUEUE_UNREADABLE_DUP": "QUEUE_UNREADABLE"}.get(expect, expect)
                self.assertEqual(item["queue"], want)
                with self.assertRaises(rec.RecoveryError):
                    rec.apply_one(root, MID)
                self.assertEqual(snapshot(root), before)

    def test_approved_or_nondraft_manifest_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, first_stage=True)
            self.assertEqual(rec.inspect_all(root), [])
            with self.assertRaises(rec.RecoveryError):
                rec.apply_one(root, MID)
            with self.assertRaises(rec.RecoveryError):
                rec.apply_one(root, "../etc/passwd")

    # -- atomic report ------------------------------------------------
    def test_report_write_is_atomic_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub/r.md"
            self.assertEqual(rec.write_report_atomic(path, "a\n"), "WRITTEN")
            self.assertEqual(rec.write_report_atomic(path, "a\n"), "UNCHANGED")
            with self.assertRaises(rec.RecoveryError):
                rec.write_report_atomic(path, "b\n")
            self.assertEqual(path.read_text(), "a\n")
            self.assertEqual([p.name for p in path.parent.iterdir()], ["r.md"])
            link = Path(td) / "sub/link.md"
            link.symlink_to(path)
            with self.assertRaises(rec.RecoveryError):
                rec.write_report_atomic(link, "a\n")

    def test_crash_during_report_write_leaves_no_partial_report(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "r.md"
            real_link = os.link

            def boom(*_a, **_k):
                raise OSError("simulated crash before publish")

            os.link = boom
            try:
                with self.assertRaises(OSError):
                    rec.write_report_atomic(path, "full content\n")
            finally:
                os.link = real_link
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(td).iterdir()), [])
            self.assertEqual(rec.write_report_atomic(path, "full content\n"), "WRITTEN")

    def test_apply_after_ledger_crash_completes_report_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, ledger_rows=[
                {"candidate_id": CID, "review_post_id": RPID, "state": "DRAFT_CREATED"}])
            out = rec.apply_one(root, MID)
            self.assertEqual(out["written"], ["REPORT"])
            self.assertEqual(rec.apply_one(root, MID)["written"], [])

    def test_existing_differing_report_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_root(td, report=True)
            before = (root / rec.PUBLISHER_DIR / f"{MID}-draft.md").read_bytes()
            out = rec.apply_one(root, MID)
            self.assertEqual(out["written"], ["LEDGER_ROW"])
            self.assertEqual((root / rec.PUBLISHER_DIR / f"{MID}-draft.md").read_bytes(), before)

    # -- surface / parity ---------------------------------------------
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
            imported,
            {"__future__", "argparse", "json", "os", "re", "sys", "tempfile", "pathlib",
             "typing", "nullone_bridge_common", "nullone_draft_candidate_queue"},
        )

    def test_message_id_validator_matches_provider(self):
        import nullone_claude_draft_provider as provider

        for value in ("777", "12:3", "", " ", None, 5, "a b", "x" * 65, "ok_id-1.2"):
            self.assertEqual(rec.valid_approval_message_id(value),
                             provider.valid_approval_message_id(value), repr(value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
