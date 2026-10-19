#!/usr/bin/env python3
"""Draft Factory packaging-receipt-conflict fallback regression tests."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

from nullone_bridge_common import BridgeError, atomic_write_json  # noqa: E402
import nullone_claude_draft_provider as draft  # noqa: E402
import nullone_draft_candidate_fallback as fallback  # noqa: E402
import nullone_packaging_receipt as receipt_mod  # noqa: E402
from test_draft_factory_claude_route import (  # noqa: E402
    evaluator_cli,
    load_hyphenated,
    queue_entry,
    ranked_item,
    select_result,
    write_queue,
)

CONFLICT_CODE = receipt_mod.PACKAGING_RECEIPT_CONFLICT_CODE
CONFLICT_CODE_LINE = f"BLOCKED_CODE={CONFLICT_CODE}"
GROUNDING_CODE = receipt_mod.VISUAL_GROUNDING_UNMET_CODE
GROUNDING_CODE_LINE = f"BLOCKED_CODE={GROUNDING_CODE}"

import importlib.util
_evaluator_spec = importlib.util.spec_from_file_location(
    "nullone_packaging_evaluator_test", SCRIPTS / "nullone-packaging-evaluator.py")
_evaluator_module = importlib.util.module_from_spec(_evaluator_spec)
_evaluator_spec.loader.exec_module(_evaluator_module)
blocked_code_line = _evaluator_module.blocked_code_line

WORKSPACE_PROD = ROOT / "workspace/social/drafts/production"


class StopAtProduce(BridgeError):
    """Raised by the fake transport when PRODUCE is reached."""


def post_item(candidate_id):
    return ranked_item(candidate_id)


class ConflictHarness:
    """Run invoke_draft in a temp workspace with faithful evaluator seams."""

    def __init__(self, test, ranked_items):
        self.test = test
        self.items = ranked_items
        self.helper_calls = []
        self.rounds = []
        self._td = tempfile.TemporaryDirectory()
        test.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        (self.root / "social/drafts/production").mkdir(parents=True)
        (self.root / "social/state").mkdir(parents=True)
        entries = "\n".join(
            queue_entry(i["candidate_id"], content_type=i["content_type"])
            for i in ranked_items
        )
        self.queue_text = "# Probe queue\n\n" + entries
        write_queue(self.root, self.queue_text)
        self.prod = self.root / "social/drafts/production"
        self.before_ws = self._workspace_prod_snapshot()

    def _workspace_prod_snapshot(self):
        if not WORKSPACE_PROD.is_dir():
            return None
        return sorted(p.name for p in WORKSPACE_PROD.iterdir())

    def fake_structured(self, **kwargs):
        if not self.rounds:
            self.rounds.append("SELECT")
            return select_result(copy.deepcopy(self.items))
        self.rounds.append("PRODUCE")
        raise StopAtProduce("PRODUCE reached")

    def fake_helper(self, argv, *, workspace_root, timeout, marker,
                    grounding_unmet_ok=False):
        name = Path(argv[1]).name
        cid = argv[argv.index("--candidate-id") + 1] if "--candidate-id" in argv else ""
        self.helper_calls.append((name, cid))
        if name != "nullone-packaging-evaluator.py":
            raise AssertionError(f"consequential helper invoked: {name}")
        request = evaluator_cli.load_validated_request(
            Path(argv[argv.index("--request-file") + 1]), root=self.root
        )
        try:
            receipt = receipt_mod.evaluate_request(cid, request)
        except BridgeError as exc:
            if grounding_unmet_ok and str(exc).startswith(GROUNDING_CODE):
                raise draft._PackagingGroundingUnmet()
            if grounding_unmet_ok and str(exc).startswith(CONFLICT_CODE):
                raise draft._PackagingReceiptConflict()
            raise
        out = receipt_mod.canonical_receipt_path(cid, root=self.root)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            try:
                existing = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = None
            if existing != receipt:
                raise draft._PackagingReceiptConflict()
        atomic_write_json(out, receipt)
        return f"RECEIPT_PATH={out}\n"

    def run(self):
        with mock.patch.object(
            draft, "run_structured", side_effect=self.fake_structured
        ), mock.patch.object(
            draft, "_run_helper", side_effect=self.fake_helper
        ), mock.patch.object(
            draft.subprocess, "run", side_effect=AssertionError("no subprocess")
        ):
            try:
                return draft.invoke_draft(
                    prompt="p", workspace=self.root, model="sonnet", timeout=900
                )
            finally:
                self.test.assertEqual(
                    self._workspace_prod_snapshot(), self.before_ws,
                    "production dir mutated",
                )

    def ledger(self):
        today = draft._today(self.root)
        return json.loads(
            (self.prod / f"{today}-draft-fallback-ledger.json").read_text(
                encoding="utf-8"
            )
        )

    def receipt_path(self, cid):
        return receipt_mod.canonical_receipt_path(cid, root=self.root)

    def assert_no_consequential_output(self, cid):
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root).as_posix()
            for marker in ("caption", "feed.png", "manifest", "preview-payload",
                           "render-record", "-draft.md"):
                self.test.assertFalse(
                    cid in rel and marker in rel, f"unexpected artifact {rel}"
                )
        self.test.assertFalse((self.root / "social/ops/manifests").exists())
        self.test.assertFalse((self.root / "social/state/topic-ledger.jsonl").exists())


class ReceiptConflictCycleTests(unittest.TestCase):
    """End-to-end cycle tests: receipt conflict is candidate-local."""

    def test_01_conflict_second_is_typed_disposition_then_next_accepted(self):
        conflict = ranked_item("conflict-candidate")
        conflict["content_type"] = "PRACTICAL"
        conflict["packaging_request"]["candidate"].update(
            content_shape="SINGLE_FACT",
            visual_requirement="NONE",
        )
        conflict["packaging_request"]["candidate"]["content_type"] = "PRACTICAL"
        conflict["packaging_request"]["assets"] = {
            "data_visualization_possible": False,
            "has_official_or_source_image": False,
            "has_usable_screenshot": False,
            "image_on_topic": False,
            "image_quality_ok": False,
        }
        conflict["asset"] = {
            "schema": "nullone.packaging-asset.v1",
            "candidate_id": "conflict-candidate",
            "asset_kind": "NONE",
            "local_path": None,
            "provenance": "probe provenance",
            "sha256": None,
        }

        stale_receipt = receipt_mod.evaluate_request(
            "conflict-candidate", conflict["packaging_request"]
        )
        stale_receipt = dict(stale_receipt)
        stale_receipt["CONTENT_SHAPE"] = "ANNOUNCEMENT"
        stale_receipt["FORMAT_REASON"] = "SINGLE_PAYLOAD_ANNOUNCEMENT"
        stale_receipt["REAL_PHOTO_REQUIRED"] = "YES"
        stale_receipt["VISUAL_EVIDENCE_REQUIRED"] = "YES"
        stale_receipt["VISUAL_STYLE"] = "DATA_VISUALIZATION"
        stale_receipt["visual_requirement"] = "SOURCE_GROUNDED"
        stale_receipt["input_fingerprint"] = "stale-fingerprint-bytes"
        stale_receipt["receipt_hash"] = _receipt_hash(stale_receipt)

        h = ConflictHarness(
            self,
            [conflict, post_item("post-2")],
        )
        atomic_write_json(h.receipt_path("conflict-candidate"), stale_receipt)

        with self.assertRaises(StopAtProduce):
            h.run()

        ledger = h.ledger()
        self.assertEqual(ledger["accepted_candidate_id"], "post-2")
        dispositions = [
            (a["candidate_id"], a.get("disposition", "SKIP"))
            for a in ledger["attempts"]
        ]
        self.assertIn(("conflict-candidate", "PACKAGING_RECEIPT_CONFLICT"), dispositions)
        self.assertEqual(
            json.loads(h.receipt_path("conflict-candidate").read_text(encoding="utf-8")),
            stale_receipt,
        )
        self.assertIn("PRODUCE", h.rounds)
        h.assert_no_consequential_output("conflict-candidate")

    def test_02_identical_receipt_is_idempotent_no_conflict(self):
        post = post_item("post-1")
        h = ConflictHarness(self, [post])
        expected = receipt_mod.evaluate_request("post-1", post["packaging_request"])
        atomic_write_json(h.receipt_path("post-1"), expected)
        with self.assertRaises(StopAtProduce):
            h.run()
        self.assertEqual(h.ledger()["accepted_candidate_id"], "post-1")
        self.assertEqual(h.helper_calls, [("nullone-packaging-evaluator.py", "post-1")])

    def test_03_conflict_does_not_block_third_candidate(self):
        h = ConflictHarness(self, [])

        c1 = ranked_item("conflict-1")
        c1["content_type"] = "PRACTICAL"
        c1["packaging_request"]["candidate"].update(
            content_shape="SINGLE_FACT", visual_requirement="NONE")
        c1["packaging_request"]["candidate"]["content_type"] = "PRACTICAL"
        c1["packaging_request"]["assets"] = {
            "data_visualization_possible": False,
            "has_official_or_source_image": False,
            "has_usable_screenshot": False,
            "image_on_topic": False,
            "image_quality_ok": False,
        }
        c1["asset"] = {"schema": "nullone.packaging-asset.v1",
                       "candidate_id": "conflict-1", "asset_kind": "NONE",
                       "local_path": None, "provenance": "p", "sha256": None}
        stale1 = receipt_mod.evaluate_request("conflict-1", c1["packaging_request"])
        stale1 = dict(stale1, CONTENT_SHAPE="ANNOUNCEMENT", VISUAL_STYLE="DATA_VISUALIZATION",
                      visual_requirement="SOURCE_GROUNDED",
                      input_fingerprint="stale-1")
        stale1["receipt_hash"] = _receipt_hash(stale1)
        atomic_write_json(h.receipt_path("conflict-1"), stale1)

        c2 = ranked_item("conflict-2")
        c2["content_type"] = "PRACTICAL"
        c2["packaging_request"]["candidate"].update(
            content_shape="SINGLE_FACT", visual_requirement="NONE")
        c2["packaging_request"]["candidate"]["content_type"] = "PRACTICAL"
        c2["packaging_request"]["assets"] = {
            "data_visualization_possible": False,
            "has_official_or_source_image": False,
            "has_usable_screenshot": False,
            "image_on_topic": False,
            "image_quality_ok": False,
        }
        c2["asset"] = {"schema": "nullone.packaging-asset.v1",
                       "candidate_id": "conflict-2", "asset_kind": "NONE",
                       "local_path": None, "provenance": "p", "sha256": None}
        stale2 = receipt_mod.evaluate_request("conflict-2", c2["packaging_request"])
        stale2 = dict(stale2, CONTENT_SHAPE="ANNOUNCEMENT", VISUAL_STYLE="DATA_VISUALIZATION",
                      visual_requirement="SOURCE_GROUNDED",
                      input_fingerprint="stale-2")
        stale2["receipt_hash"] = _receipt_hash(stale2)
        atomic_write_json(h.receipt_path("conflict-2"), stale2)

        entries = "\n".join(
            queue_entry(i["candidate_id"], content_type=i["content_type"])
            for i in [c1, c2, post_item("post-3")]
        )
        h.queue_text = "# Probe queue\n\n" + entries
        write_queue(h.root, h.queue_text)
        h.items = [c1, c2, post_item("post-3")]

        with self.assertRaises(StopAtProduce):
            h.run()
        ledger = h.ledger()
        self.assertEqual(ledger["accepted_candidate_id"], "post-3")
        dispositions = [
            (a["candidate_id"], a.get("disposition", "SKIP"))
            for a in ledger["attempts"]
        ]
        self.assertEqual(dispositions[0][1], "PACKAGING_RECEIPT_CONFLICT")
        self.assertEqual(dispositions[1][1], "PACKAGING_RECEIPT_CONFLICT")
        self.assertEqual(ledger["accepted_candidate_id"], "post-3")


def _receipt_hash(receipt):
    return hashlib.sha256(
        receipt_mod.canonical_json_bytes(
            {k: v for k, v in receipt.items() if k != "receipt_hash"}
        )
    ).hexdigest()


class BlockedCodeLineTests(unittest.TestCase):
    """blocked_code_line() returns exact fixed constants, never echoing
    dynamic exception text."""

    def test_grounding_exact_fixed_line(self):
        pass  # blocked_code_line imported at module level
        exc = BridgeError("PACKAGING_VISUAL_GROUNDING_UNMET")
        self.assertEqual(
            blocked_code_line(exc),
            f"BLOCKED_CODE={GROUNDING_CODE}",
        )

    def test_grounding_with_suffix_exact_fixed_line(self):
        pass  # blocked_code_line imported at module level
        exc = BridgeError("PACKAGING_VISUAL_GROUNDING_UNMET: layout declares a visual block")
        self.assertEqual(
            blocked_code_line(exc),
            f"BLOCKED_CODE={GROUNDING_CODE}",
        )

    def test_conflict_exact_fixed_line(self):
        pass  # blocked_code_line imported at module level
        exc = BridgeError("PACKAGING_RECEIPT_CONFLICT")
        self.assertEqual(
            blocked_code_line(exc),
            f"BLOCKED_CODE={CONFLICT_CODE}",
        )

    def test_conflict_with_suffix_exact_fixed_line(self):
        pass  # blocked_code_line imported at module level
        exc = BridgeError("PACKAGING_RECEIPT_CONFLICT: a different receipt already exists")
        self.assertEqual(
            blocked_code_line(exc),
            f"BLOCKED_CODE={CONFLICT_CODE}",
        )

    def test_other_errors_return_none(self):
        pass  # blocked_code_line imported at module level
        self.assertIsNone(blocked_code_line(BridgeError("PACKAGING_INPUT_INVALID: x")))
        self.assertIsNone(blocked_code_line(ValueError("x")))
        self.assertIsNone(blocked_code_line(BridgeError("PACKAGING_RECEIPT_TAMPERED: hash mismatch")))
        self.assertIsNone(blocked_code_line(BridgeError("PACKAGING_SKIPPED: receipt is not a POST decision")))


class ReceiptValidationSafetyTests(unittest.TestCase):
    """Existing receipt validation safety: only valid receipts qualify for
    typed candidate-local conflict. Malformed/tampered/unsafe receipts
    fail closed as whole-cycle integrity errors."""

    def test_malformed_existing_json_fails_closed(self):
        from nullone_packaging_receipt import load_receipt
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "social/drafts/production").mkdir(parents=True)
            receipt_path = receipt_mod.canonical_receipt_path("post-1", root=root)
            receipt_path.write_text("{not valid json", encoding="utf-8")

            with self.assertRaises(BridgeError) as ctx:
                load_receipt(receipt_path, root=root)
            self.assertIn("PACKAGING_INPUT_INVALID", str(ctx.exception))
            exc = BridgeError(str(ctx.exception))
            self.assertIsNone(blocked_code_line(exc))

    def test_tampered_receipt_hash_fails_closed(self):
        from nullone_packaging_receipt import load_receipt
        post = ranked_item("post-1")
        post["content_type"] = "PRACTICAL"
        post["packaging_request"]["candidate"]["content_type"] = "PRACTICAL"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "social/drafts/production").mkdir(parents=True)
            receipt_path = receipt_mod.canonical_receipt_path("post-1", root=root)

            valid = receipt_mod.evaluate_request("post-1", post["packaging_request"])
            tampered = dict(valid)
            tampered["receipt_hash"] = "tampered-hash-value"
            atomic_write_json(receipt_path, tampered)

            with self.assertRaises(BridgeError) as ctx:
                load_receipt(receipt_path, root=root)
            self.assertIn("TAMPERED", str(ctx.exception))
            exc = BridgeError(str(ctx.exception))
            self.assertIsNone(blocked_code_line(exc))

    def test_symlink_unsafe_receipt_fails_closed(self):
        from nullone_packaging_receipt import load_receipt
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "social/drafts/production").mkdir(parents=True)
            receipt_path = receipt_mod.canonical_receipt_path("post-1", root=root)
            target = root / "social/drafts/production/other.json"
            target.write_text("{}", encoding="utf-8")
            os.symlink(target, receipt_path)

            with self.assertRaises(BridgeError) as ctx:
                load_receipt(receipt_path, root=root)
            self.assertIn("symlink", str(ctx.exception).lower())
            exc = BridgeError(str(ctx.exception))
            self.assertIsNone(blocked_code_line(exc))


class HelperSeamContractTests(unittest.TestCase):
    """_run_helper seam contract: typed signals only for reviewed exit contract."""

    def _run_stub(self, stdout_text, exit_code, **kwargs):
        argv = [sys.executable, "-c",
                f"import sys; sys.stdout.write({stdout_text!r}); sys.exit({exit_code})"]
        with tempfile.TemporaryDirectory() as td:
            return draft._run_helper(
                argv, workspace_root=Path(td), timeout=30,
                marker="RECEIPT_PATH=", **kwargs)

    def test_10_conflict_code_line_is_typed_signal(self):
        with self.assertRaises(draft._PackagingReceiptConflict):
            self._run_stub(CONFLICT_CODE_LINE + "\n", 2, grounding_unmet_ok=True)

    def test_10_conflict_code_line_on_other_exits_fails_closed(self):
        with self.assertRaises(BridgeError):
            self._run_stub(CONFLICT_CODE_LINE + "\n", 1, grounding_unmet_ok=True)
        with self.assertRaises(BridgeError):
            self._run_stub(CONFLICT_CODE_LINE + "\n", 3, grounding_unmet_ok=True)

    def test_10_unknown_code_or_exit_fails_closed(self):
        with self.assertRaises(BridgeError):
            self._run_stub("BLOCKED=something else\n", 2, grounding_unmet_ok=True)
        with self.assertRaises(BridgeError):
            self._run_stub("", 2, grounding_unmet_ok=True)
        with self.assertRaises(BridgeError):
            self._run_stub(CONFLICT_CODE_LINE + "\n", 0, grounding_unmet_ok=True)
        with self.assertRaises(BridgeError):
            self._run_stub(CONFLICT_CODE_LINE + "\n", 2)

    def test_10_no_raw_output_in_typed_signal(self):
        try:
            self._run_stub(
                CONFLICT_CODE_LINE + "\nSECRET_STDOUT_BYTES https://x.invalid/t?s=1\n",
                2, grounding_unmet_ok=True)
        except draft._PackagingReceiptConflict as exc:
            text = str(exc)
        else:
            self.fail("typed signal not raised")
        self.assertNotIn("SECRET_STDOUT_BYTES", text)
        self.assertNotIn("https://x.invalid", text)

    def test_code_line_matches_evaluator_contract(self):
        self.assertEqual(
            draft.EVALUATOR_RECEIPT_CONFLICT_CODE_LINE,
            f"BLOCKED_CODE={receipt_mod.PACKAGING_RECEIPT_CONFLICT_CODE}",
        )
        source = (SCRIPTS / "nullone-packaging-evaluator.py").read_text(
            encoding="utf-8")
        self.assertIn("PACKAGING_RECEIPT_CONFLICT_CODE", source)
        source_receipt = (SCRIPTS / "nullone_packaging_receipt.py").read_text(
            encoding="utf-8")
        self.assertIn("PACKAGING_RECEIPT_CONFLICT_CODE =", source_receipt)


if __name__ == "__main__":
    unittest.main(verbosity=2)