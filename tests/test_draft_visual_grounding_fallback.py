#!/usr/bin/env python3
"""Draft Factory visual-grounding fallback regression tests (offline only).

Natural 2026-10-03 16:15 catch-up run: PR #201 correctly recorded the
known STORY candidate as STORY_NOT_FACTORY_PRODUCIBLE and continued,
then the second candidate (github-copilot-computer-use-preview-2026-10-03)
raised PACKAGING_VISUAL_GROUNDING_UNMET -- a deliberate candidate-local
safety gate (SOURCE_GROUNDED layout, no usable evidence) -- which the
orchestration misclassified as a whole-cycle BridgeError.

Required behavior proven here: the typed grounding outcome is
Factory-non-producible (recorded, never accepted, no receipt
manufactured, no render/manifest/Zernio/Telegram), the cycle continues
in frozen ranked order, and interrupted prior-cycle request+asset bytes
(with no receipt) are recovered through the authoritative evaluator
instead of being overwritten.

No network, no model calls, no production writes: every cycle runs
against a temp workspace with mocked transport and helper seams, plus
real-subprocess parsing checks via stub commands only.
"""
from __future__ import annotations

import copy
import json
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
    queue_entry,
    ranked_item,
    select_result,
    write_queue,
)

GROUNDING_CODE = "PACKAGING_VISUAL_GROUNDING_UNMET"
CODE_LINE = f"BLOCKED_CODE={GROUNDING_CODE}"

WORKSPACE_PROD = ROOT / "workspace/social/drafts/production"


class StopAtProduce(BridgeError):
    """Raised by the fake transport when PRODUCE is reached."""


def grounding_item(candidate_id):
    """SOURCE_GROUNDED layout with zero usable evidence (16:15 shape)."""
    item = ranked_item(candidate_id)
    item["packaging_request"]["candidate"].update(
        content_shape="ANNOUNCEMENT",
        depicts_real_world_subject=True,
        visual_requirement="SOURCE_GROUNDED",
    )
    return item


def practical_grounding_item(candidate_id):
    """Exact 16:15 production signal shape (PRACTICAL at every level)."""
    item = grounding_item(candidate_id)
    item["content_type"] = "PRACTICAL"
    item["packaging_request"]["candidate"]["content_type"] = "PRACTICAL"
    return item


def story_item(candidate_id):
    item = ranked_item(candidate_id)
    item["packaging_request"]["candidate"].update(
        content_shape="COMPARISON", distinct_beat_count=2
    )
    item["packaging_request"]["assets"]["data_visualization_possible"] = True
    return item


def skip_item(candidate_id):
    item = ranked_item(candidate_id)
    item["packaging_request"]["candidate"]["depicts_real_world_subject"] = True
    return item


def post_item(candidate_id):
    return ranked_item(candidate_id)


def workspace_prod_snapshot():
    if not WORKSPACE_PROD.is_dir():
        return None
    return sorted(p.name for p in WORKSPACE_PROD.iterdir())


class Harness:
    """Run invoke_draft in a temp workspace with faithful evaluator seams."""

    def __init__(self, test, ranked_items):
        self.test = test
        self.items = ranked_items
        self.helper_calls: list[tuple[str, str]] = []
        self.rounds: list[str] = []
        self._td = tempfile.TemporaryDirectory()
        test.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        (self.root / "social/drafts/production").mkdir(parents=True)
        (self.root / "social/state").mkdir(parents=True)
        # Queue mirrors each item's top-level content_type so the
        # queue identity check passes; nested mismatches (if any)
        # are then purely a SELECT-boundary matter.
        entries = "\n".join(
            queue_entry(i["candidate_id"], content_type=i["content_type"])
            for i in ranked_items
        )
        self.queue_text = "# Probe queue\n\n" + entries
        write_queue(self.root, self.queue_text)
        self.prod = self.root / "social/drafts/production"
        self.before_ws = workspace_prod_snapshot()

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
        # Faithful production semantics: real validation + evaluation;
        # grounding impossibility surfaces as the typed signal (exactly
        # what the real _run_helper raises for the fixed code line).
        request = evaluator_cli.load_validated_request(
            Path(argv[argv.index("--request-file") + 1]), root=self.root
        )
        try:
            receipt = receipt_mod.evaluate_request(cid, request)
        except BridgeError as exc:
            if grounding_unmet_ok and str(exc).startswith(GROUNDING_CODE):
                raise draft._PackagingGroundingUnmet()
            raise
        out = receipt_mod.canonical_receipt_path(cid, root=self.root)
        out.parent.mkdir(parents=True, exist_ok=True)
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
                    workspace_prod_snapshot(), self.before_ws,
                    "production dir mutated",
                )

    def ledger(self):
        today = draft._today(self.root)
        return json.loads(
            (self.prod / f"{today}-draft-fallback-ledger.json").read_text(
                encoding="utf-8"
            )
        )

    def files(self):
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file()
        )

    def receipt_path(self, cid):
        return receipt_mod.canonical_receipt_path(cid, root=self.root)

    def assert_no_consequential_output(self, cid):
        for p in self.files():
            for marker in ("caption", "feed.png", "manifest", "preview-payload",
                           "render-record", "-draft.md"):
                self.test.assertFalse(
                    cid in p and marker in p, f"unexpected artifact {p}"
                )
        self.test.assertFalse((self.root / "social/ops/manifests").exists())
        self.test.assertFalse((self.root / "social/state/topic-ledger.jsonl").exists())


class GroundingFallbackCycleTests(unittest.TestCase):
    def test_01_story_first_still_continues(self):
        h = Harness(self, [story_item("story-1"), post_item("post-2")])
        with self.assertRaises(StopAtProduce):
            h.run()
        self.assertEqual(h.ledger()["accepted_candidate_id"], "post-2")
        self.assertEqual(
            h.ledger()["attempts"][0]["disposition"], "STORY_NOT_FACTORY_PRODUCIBLE"
        )

    def test_02_grounding_second_is_typed_disposition_not_accepted(self):
        h = Harness(
            self,
            [story_item("story-1"), grounding_item("ground-2"), post_item("post-3")],
        )
        with self.assertRaises(StopAtProduce):
            h.run()
        # Third candidate attempted after STORY + grounding.
        self.assertEqual(
            [c[1] for c in h.helper_calls], ["story-1", "ground-2", "post-3"]
        )
        ledger = h.ledger()
        self.assertEqual(ledger["accepted_candidate_id"], "post-3")
        self.assertEqual(
            [(a["candidate_id"], a.get("disposition", "SKIP")) for a in ledger["attempts"]],
            [("story-1", "STORY_NOT_FACTORY_PRODUCIBLE"),
             ("ground-2", "VISUAL_GROUNDING_UNMET")],
        )
        # No receipt fabricated for the grounding candidate.
        self.assertFalse(h.receipt_path("ground-2").exists())
        h.assert_no_consequential_output("ground-2")
        # Ledger never claims delivery, handoff, or publication.
        text = json.dumps(ledger).lower()
        for forbidden in ("delegat", "handoff", "published", "delivered",
                           "telegram", "zernio", "post_decision=skip"):
            self.assertNotIn(forbidden, text)

    def test_09_all_exhausted_is_truthful_no_action(self):
        h = Harness(
            self,
            [story_item("story-1"), grounding_item("ground-2"), skip_item("skip-3")],
        )
        self.assertEqual(h.run(), {"status": "NO_ACTION", "candidate_id": None})
        self.assertEqual(h.rounds, ["SELECT"])
        ledger = h.ledger()
        self.assertIsNone(ledger["accepted_candidate_id"])
        self.assertEqual(len(ledger["attempts"]), 3)
        reason = fallback.next_candidate(ledger)["reason"]
        self.assertIn("ALL_RANKED_NOT_FACTORY_PRODUCIBLE", reason)
        self.assertIn("ground-2:VISUAL_GROUNDING_UNMET", reason)

    def test_10_unknown_evaluator_rejection_fails_closed(self):
        h = Harness(self, [post_item("post-1")])
        with mock.patch.object(
            draft, "_run_helper",
            side_effect=BridgeError("Draft helper rejected: helper (exit=2)"),
        ):
            with mock.patch.object(
                draft, "run_structured", side_effect=h.fake_structured
            ), mock.patch.object(
                draft.subprocess, "run",
                side_effect=AssertionError("no subprocess"),
            ):
                with self.assertRaises(BridgeError):
                    draft.invoke_draft(
                        prompt="p", workspace=h.root, model="sonnet", timeout=900
                    )
        # No fallback recorded, no receipt manufactured.
        today = draft._today(h.root)
        self.assertFalse(
            (h.prod / f"{today}-draft-fallback-ledger.json").exists()
        )
        self.assertFalse(h.receipt_path("post-1").exists())

    def test_11_input_invalid_fails_closed_without_fallback(self):
        h = Harness(self, [post_item("post-1")])
        bad = copy.deepcopy(h.items[0])
        bad["packaging_request"]["candidate"]["FORMAT_DECISION"] = "CAROUSEL"
        h.items = [bad]
        with self.assertRaises(BridgeError):
            h.run()
        # SELECT validation rejects before any helper or write.
        self.assertEqual(h.helper_calls, [])
        today = draft._today(h.root)
        self.assertFalse(
            (h.prod / f"{today}-post-1-packaging-request.json").exists()
        )
        self.assertFalse(h.receipt_path("post-1").exists())

    def test_22_at_most_one_acceptance(self):
        h = Harness(
            self,
            [grounding_item("ground-1"), post_item("post-2"), post_item("post-3")],
        )
        with self.assertRaises(StopAtProduce):
            h.run()
        self.assertEqual([c[1] for c in h.helper_calls], ["ground-1", "post-2"])
        self.assertEqual(h.ledger()["accepted_candidate_id"], "post-2")
        self.assertEqual(h.rounds.count("PRODUCE"), 1)

    def test_02b_exact_production_signal_shape_is_grounding(self):
        # 16:15 candidate at every authority level (queue/item/nested).
        h = Harness(
            self,
            [practical_grounding_item("github-copilot-computer-use-preview-2026-10-03"),
             post_item("post-2")],
        )
        with self.assertRaises(StopAtProduce):
            h.run()
        ledger = h.ledger()
        self.assertEqual(ledger["accepted_candidate_id"], "post-2")
        self.assertEqual(
            ledger["attempts"][0].get("disposition"), "VISUAL_GROUNDING_UNMET")
        self.assertFalse(
            h.receipt_path(
                "github-copilot-computer-use-preview-2026-10-03").exists())


class InterruptedRecoveryTests(unittest.TestCase):
    def _preseed_request_asset(self, h, item):
        today = draft._today(h.root)
        request_path = h.prod / f"{today}-{item['candidate_id']}-packaging-request.json"
        asset_path = h.prod / f"{today}-{item['candidate_id']}-packaging-asset.json"
        request_path.write_text(
            json.dumps(item["packaging_request"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        asset_path.write_text(
            json.dumps(item["asset"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return request_path, asset_path

    def test_13_existing_bytes_recovered_changed_model_bytes_ignored(self):
        ground = grounding_item("ground-1")
        h = Harness(self, [ground, post_item("post-2")])
        request_path, asset_path = self._preseed_request_asset(h, ground)
        before_request, before_asset = (
            request_path.read_bytes(), asset_path.read_bytes())
        # Changed model bytes on this cycle must not overwrite.
        changed = grounding_item("ground-1")
        changed["packaging_request"]["candidate"]["audience_value"] = "MEDIUM"
        h.items = [changed, post_item("post-2")]
        with self.assertRaises(StopAtProduce):
            h.run()
        self.assertEqual(request_path.read_bytes(), before_request)
        self.assertEqual(asset_path.read_bytes(), before_asset)
        ledger = h.ledger()
        self.assertEqual(ledger["accepted_candidate_id"], "post-2")
        dispositions = [
            (a["candidate_id"], a.get("disposition", "SKIP"))
            for a in ledger["attempts"]
        ]
        self.assertIn(("ground-1", "VISUAL_GROUNDING_UNMET"), dispositions)
        self.assertFalse(h.receipt_path("ground-1").exists())

    def test_16_existing_valid_request_resumes_to_receipt(self):
        post = post_item("post-1")
        h = Harness(self, [post])
        request_path, asset_path = self._preseed_request_asset(h, post)
        before = (request_path.read_bytes(), asset_path.read_bytes())
        changed = post_item("post-1")
        changed["packaging_request"]["candidate"]["audience_value"] = "MEDIUM"
        h.items = [changed]
        with self.assertRaises(StopAtProduce):
            h.run()
        # Existing bytes reused: receipt matches the PRESEEDED request,
        # not the changed model bytes.
        self.assertEqual(
            (request_path.read_bytes(), asset_path.read_bytes()), before)
        receipt = receipt_mod.load_receipt(h.receipt_path("post-1"), root=h.root)
        expected = receipt_mod.evaluate_request(
            "post-1", json.loads(before[0].decode("utf-8")))
        self.assertEqual(receipt, expected)
        self.assertEqual(h.ledger()["accepted_candidate_id"], "post-1")

    def test_17_request_only_partial_state_fails_closed(self):
        ground = grounding_item("ground-1")
        h = Harness(self, [ground, post_item("post-2")])
        today = draft._today(h.root)
        (h.prod / f"{today}-ground-1-packaging-request.json").write_text(
            json.dumps(ground["packaging_request"]), encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "partial"):
            h.run()
        self.assertEqual(h.helper_calls, [])

    def test_18_asset_only_partial_state_fails_closed(self):
        ground = grounding_item("ground-1")
        h = Harness(self, [ground, post_item("post-2")])
        today = draft._today(h.root)
        (h.prod / f"{today}-ground-1-packaging-asset.json").write_text(
            json.dumps(ground["asset"]), encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "partial"):
            h.run()
        self.assertEqual(h.helper_calls, [])

    def test_19_malformed_or_mismatched_existing_state_fails_closed(self):
        cases = {
            "bad-json": ("{not json", None),
            "non-object": ("[1, 2]", None),
            "candidate-mismatch": (None, "other-candidate"),
        }
        for name, (request_body, asset_cid) in cases.items():
            with self.subTest(case=name):
                ground = grounding_item("ground-1")
                h = Harness(self, [ground, post_item("post-2")])
                today = draft._today(h.root)
                request_path = h.prod / f"{today}-ground-1-packaging-request.json"
                asset_path = h.prod / f"{today}-ground-1-packaging-asset.json"
                request_path.write_text(
                    request_body
                    if request_body is not None
                    else json.dumps(ground["packaging_request"]),
                    encoding="utf-8",
                )
                asset = copy.deepcopy(ground["asset"])
                if asset_cid is not None:
                    asset["candidate_id"] = asset_cid
                asset_path.write_text(json.dumps(asset), encoding="utf-8")
                with self.assertRaises(BridgeError):
                    h.run()
                self.assertEqual(h.helper_calls, [])

    def test_19b_interrupted_asset_fully_validated_before_evaluator(self):
        """candidate_id match alone never suffices for recovery."""
        ground = grounding_item("ground-1")
        bad_assets = {
            "missing-schema": lambda d: d.pop("schema"),
            "wrong-schema": lambda d: d.update(
                schema="nullone.packaging-asset.v9"),
            "missing-field": lambda d: d.pop("provenance"),
            "invalid-kind": lambda d: d.update(asset_kind="GENERATED_IMAGE"),
            "file-backed-blank-path": lambda d: d.update(
                asset_kind="REAL_PHOTO", local_path="  "),
            "file-backed-no-provenance": lambda d: d.update(
                asset_kind="REAL_PHOTO",
                local_path="social/drafts/production/evidence.png",
                provenance="  "),
            "none-names-file": lambda d: d.update(
                local_path="social/drafts/production/evidence.png"),
            "candidate-id-only": lambda d: (
                d.clear(), d.update(candidate_id="ground-1")),
        }
        for name, mutate in bad_assets.items():
            with self.subTest(case=name):
                h = Harness(self, [ground, post_item("post-2")])
                today = draft._today(h.root)
                (h.prod / f"{today}-ground-1-packaging-request.json").write_text(
                    json.dumps(ground["packaging_request"]), encoding="utf-8")
                asset = copy.deepcopy(ground["asset"])
                mutate(asset)
                (h.prod / f"{today}-ground-1-packaging-asset.json").write_text(
                    json.dumps(asset), encoding="utf-8")
                with self.assertRaises(BridgeError):
                    h.run()
                # Rejected during recovery validation: the evaluator
                # never ran for this candidate.
                self.assertEqual(h.helper_calls, [])
                self.assertFalse(h.receipt_path("ground-1").exists())

    def test_19c_interrupted_request_fully_validated_before_evaluator(self):
        ground = grounding_item("ground-1")
        bad_requests = {
            "missing-candidate-field": lambda r: r["candidate"].pop(
                "visual_requirement"),
            "forbidden-output-field": lambda r: r["candidate"].update(
                FORMAT_DECISION="CAROUSEL"),
            "wrong-enum": lambda r: r["candidate"].update(
                verification_status="MAYBE"),
            "content-type-split": lambda r: r["candidate"].update(
                content_type="EXPLAINER"),
        }
        for name, mutate in bad_requests.items():
            with self.subTest(case=name):
                h = Harness(self, [ground, post_item("post-2")])
                today = draft._today(h.root)
                request = copy.deepcopy(ground["packaging_request"])
                mutate(request)
                (h.prod / f"{today}-ground-1-packaging-request.json").write_text(
                    json.dumps(request), encoding="utf-8")
                (h.prod / f"{today}-ground-1-packaging-asset.json").write_text(
                    json.dumps(ground["asset"]), encoding="utf-8")
                with self.assertRaises(BridgeError):
                    h.run()
                self.assertEqual(h.helper_calls, [])

    def test_16b_recovered_asset_mismatch_fails_before_acceptance(self):
        # Structurally valid POST request + structurally valid asset whose
        # kind contradicts the receipt: evaluator succeeds, but the
        # recovered asset must fail closed before acceptance/PRODUCE.
        post = post_item("post-1")
        h = Harness(self, [post])
        today = draft._today(h.root)
        (h.prod / f"{today}-post-1-packaging-request.json").write_text(
            json.dumps(post["packaging_request"]), encoding="utf-8")
        mismatched = copy.deepcopy(post["asset"])
        mismatched.update(
            asset_kind="REAL_PHOTO",
            local_path="social/drafts/production/evidence.png",
            provenance="Official source",
        )
        (h.prod / f"{today}-post-1-packaging-asset.json").write_text(
            json.dumps(mismatched), encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "MISMATCH"):
            h.run()
        self.assertEqual(h.helper_calls, [("nullone-packaging-evaluator.py", "post-1")])
        self.assertEqual(h.rounds, ["SELECT"])  # PRODUCE never attempted
        # Nothing accepted and no ledger acceptance persisted.
        today = draft._today(h.root)
        ledger_path = h.prod / f"{today}-draft-fallback-ledger.json"
        self.assertFalse(ledger_path.exists())

    def test_10b_exact_1615_grounding_state_recovers_without_deletion(self):
        # Faithful replica of the 16:15 production interrupted state:
        # valid PRACTICAL grounding request+asset, no receipt.
        cid = "github-copilot-computer-use-preview-2026-10-03"
        ground = practical_grounding_item(cid)
        h = Harness(self, [ground, post_item("post-2")])
        today = draft._today(h.root)
        request_path = h.prod / f"{today}-{cid}-packaging-request.json"
        asset_path = h.prod / f"{today}-{cid}-packaging-asset.json"
        request_path.write_text(
            json.dumps(ground["packaging_request"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        asset_path.write_text(
            json.dumps(ground["asset"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        before = (request_path.read_bytes(), asset_path.read_bytes())
        with self.assertRaises(StopAtProduce):
            h.run()
        # Recovered as-is: no overwrite, typed disposition, next
        # candidate accepted, still no receipt for the grounded one.
        self.assertEqual(
            (request_path.read_bytes(), asset_path.read_bytes()), before)
        self.assertFalse(h.receipt_path(cid).exists())
        ledger = h.ledger()
        self.assertEqual(ledger["accepted_candidate_id"], "post-2")
        self.assertEqual(
            ledger["attempts"][0].get("disposition"), "VISUAL_GROUNDING_UNMET")

    def test_20_existing_story_receipt_behavior_unchanged(self):
        story = story_item("story-1")
        h = Harness(self, [story, grounding_item("ground-2"), post_item("post-3")])
        body = receipt_mod.evaluate_request("story-1", story["packaging_request"])
        receipt_path = receipt_mod.canonical_receipt_path("story-1", root=h.root)
        atomic_write_json(receipt_path, body)
        before = receipt_path.read_bytes()
        with self.assertRaises(StopAtProduce):
            h.run()
        # Receipt-covered candidate caused no writes and no evaluator call.
        self.assertEqual(
            [c[1] for c in h.helper_calls], ["ground-2", "post-3"])
        self.assertEqual(receipt_path.read_bytes(), before)
        today = draft._today(h.root)
        self.assertFalse((h.prod / f"{today}-story-1-packaging-request.json").exists())
        ledger = h.ledger()
        self.assertEqual(ledger["accepted_candidate_id"], "post-3")
        self.assertEqual(
            [(a["candidate_id"], a.get("disposition", "SKIP"))
             for a in ledger["attempts"]],
            [("story-1", "STORY_NOT_FACTORY_PRODUCIBLE"),
             ("ground-2", "VISUAL_GROUNDING_UNMET")],
        )

    def test_21_tampered_receipt_still_fails_closed(self):
        post = post_item("post-1")
        h = Harness(self, [post])
        body = receipt_mod.evaluate_request("post-1", post["packaging_request"])
        path = receipt_mod.canonical_receipt_path("post-1", root=h.root)
        tampered = dict(body, FORMAT_DECISION="CAROUSEL")
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(BridgeError) as ctx:
            h.run()
        self.assertIn("TAMPERED", str(ctx.exception))
        self.assertEqual(h.helper_calls, [])


class HelperSeamContractTests(unittest.TestCase):
    STUB = [sys.executable, "-c", ""]

    def _run_stub(self, stdout_text, exit_code, **kwargs):
        argv = [sys.executable, "-c",
                f"import sys; sys.stdout.write({stdout_text!r}); sys.exit({exit_code})"]
        with tempfile.TemporaryDirectory() as td:
            return draft._run_helper(
                argv, workspace_root=Path(td), timeout=30,
                marker="RECEIPT_PATH=", **kwargs)

    def test_10_grounding_code_line_is_typed_signal(self):
        with self.assertRaises(draft._PackagingGroundingUnmet):
            self._run_stub(CODE_LINE + "\n", 2, grounding_unmet_ok=True)

    def test_10_code_line_on_other_exits_fails_closed(self):
        # The reviewed evaluator contract for BridgeError is exit 2:
        # the fixed code line on any other exit is a system failure.
        with self.assertRaises(BridgeError):
            self._run_stub(CODE_LINE + "\n", 1, grounding_unmet_ok=True)
        with self.assertRaises(BridgeError):
            self._run_stub(CODE_LINE + "\n", 3, grounding_unmet_ok=True)

    def test_10_unknown_code_or_exit_fails_closed(self):
        with self.assertRaises(BridgeError):
            self._run_stub("BLOCKED=something else\n", 2, grounding_unmet_ok=True)
        with self.assertRaises(BridgeError):
            # Bare non-zero exit with no code at all.
            self._run_stub("", 2, grounding_unmet_ok=True)
        with self.assertRaises(BridgeError):
            # Code line present but success-shaped exit without marker.
            self._run_stub(CODE_LINE + "\n", 0, grounding_unmet_ok=True)
        with self.assertRaises(BridgeError):
            # Flag off: grounding line is just another rejection.
            self._run_stub(CODE_LINE + "\n", 2)

    def test_12_timeout_and_startup_fail_closed(self):
        with mock.patch.object(
            draft.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1),
        ):
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaisesRegex(BridgeError, "timed out"):
                    draft._run_helper(
                        [sys.executable, "nullone-packaging-evaluator.py"],
                        workspace_root=Path(td), timeout=30,
                        marker="RECEIPT_PATH=", grounding_unmet_ok=True)
        with mock.patch.object(
            draft.subprocess, "run", side_effect=OSError("nope")):
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaisesRegex(BridgeError, "failed to start"):
                    draft._run_helper(
                        [sys.executable, "nullone-packaging-evaluator.py"],
                        workspace_root=Path(td), timeout=30,
                        marker="RECEIPT_PATH=", grounding_unmet_ok=True)

    def test_code_line_matches_evaluator_contract(self):
        self.assertEqual(
            draft.EVALUATOR_GROUNDING_CODE_LINE,
            f"BLOCKED_CODE={receipt_mod.VISUAL_GROUNDING_UNMET_CODE}",
        )
        source = (SCRIPTS / "nullone-packaging-evaluator.py").read_text(
            encoding="utf-8")
        self.assertIn("BLOCKED_CODE={VISUAL_GROUNDING_UNMET_CODE}", source)

    def test_no_raw_output_in_typed_signal(self):
        try:
            self._run_stub(
                CODE_LINE + "\nSECRET_STDOUT_BYTES https://x.invalid/t?s=1\n",
                2, grounding_unmet_ok=True)
        except draft._PackagingGroundingUnmet as exc:
            text = str(exc)
        else:
            self.fail("typed signal not raised")
        self.assertNotIn("SECRET_STDOUT_BYTES", text)
        self.assertNotIn("https://x.invalid", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
