#!/usr/bin/env python3
"""Draft Factory STORY non-producible fallback regression tests (offline only).

Natural 2026-10-03 09:45 run: a ranked Draft candidate resolved to
POST/STORY, was recorded as the accepted candidate, and the provider
returned DELEGATED -- a no-op (no Draft->Story handoff exists) that
terminated the cycle before the next ranked FEED/CAROUSEL candidate.

Draft Factory is FEED/CAROUSEL-only. A POST/STORY packaging decision
is a Factory-local non-producible disposition: never accepted, never
rendered, never handed to StoryWorkflow; the cycle continues in the
frozen ranked order and may end as truthful NO_ACTION.

No network, no model calls, no production writes: every cycle runs
against a temp workspace with mocked transport and helper seams.
"""
from __future__ import annotations

import copy
import json
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

STORY_REASON = "TWO_ITEM_COMPARISON_FITS_STORY"


class StopAtProduce(BridgeError):
    """Raised by the fake transport when PRODUCE is reached."""


def story_item(candidate_id):
    item = ranked_item(candidate_id)
    item["packaging_request"]["candidate"].update(
        content_shape="COMPARISON", distinct_beat_count=2
    )
    item["packaging_request"]["assets"]["data_visualization_possible"] = True
    return item


def post_item(candidate_id):
    return ranked_item(candidate_id)


def skip_item(candidate_id):
    item = ranked_item(candidate_id)
    item["packaging_request"]["candidate"]["depicts_real_world_subject"] = True
    return item


class Harness:
    """Run invoke_draft in a temp workspace with recorded seams."""

    def __init__(self, test, ranked_items, *, preexisting=None):
        self.test = test
        self.items = ranked_items
        self.helper_calls: list[tuple[str, str]] = []
        self.rounds: list[str] = []
        self.prompts: list[str] = []
        self._td = tempfile.TemporaryDirectory()
        test.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        (self.root / "social/drafts/production").mkdir(parents=True)
        (self.root / "social/state").mkdir(parents=True)
        entries = "\n".join(queue_entry(i["candidate_id"]) for i in ranked_items)
        self.queue_text = "# Probe queue\n\n" + entries
        write_queue(self.root, self.queue_text)
        self.prod = self.root / "social/drafts/production"

    def fake_structured(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
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
        receipt = receipt_mod.evaluate_request(cid, request)
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
            return draft.invoke_draft(
                prompt="p", workspace=self.root, model="sonnet", timeout=900
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

    def assert_no_consequential_output(self, test, cid):
        for p in self.files():
            for marker in ("caption", "feed.png", "manifest", "preview-payload",
                           "render-record", "-draft.md"):
                test.assertFalse(
                    cid in p and marker in p, f"unexpected artifact {p}"
                )
        test.assertFalse((self.root / "social/ops/manifests").exists())
        test.assertEqual(
            (self.root / draft.QUEUE_PATH).read_text(encoding="utf-8"),
            self.queue_text,
        )
        test.assertFalse((self.root / "social/state/topic-ledger.jsonl").exists())


class StoryFallbackCycleTests(unittest.TestCase):
    def test_story_first_then_post_accepts_only_second(self):
        h = Harness(self, [story_item("story-1"), post_item("post-2")])
        with self.assertRaises(StopAtProduce):
            h.run()
        self.assertEqual(
            h.helper_calls,
            [("nullone-packaging-evaluator.py", "story-1"),
             ("nullone-packaging-evaluator.py", "post-2")],
        )
        ledger = h.ledger()
        self.assertEqual(ledger["ranked_candidate_ids"], ["story-1", "post-2"])
        self.assertEqual(ledger["accepted_candidate_id"], "post-2")
        self.assertEqual(len(ledger["attempts"]), 1)
        attempt = ledger["attempts"][0]
        self.assertEqual(attempt["candidate_id"], "story-1")
        self.assertEqual(attempt["disposition"], "STORY_NOT_FACTORY_PRODUCIBLE")
        self.assertEqual(attempt["format_reason"], STORY_REASON)
        # PRODUCE round was told about the second candidate only.
        produce_prompt = h.prompts[-1]
        self.assertIn("post-2", produce_prompt)
        self.assertNotIn("story-1", produce_prompt)
        h.assert_no_consequential_output(self, "story-1")

    def test_story_ledger_never_claims_delivery_or_handoff(self):
        h = Harness(self, [story_item("story-1"), post_item("post-2")])
        with self.assertRaises(StopAtProduce):
            h.run()
        text = json.dumps(h.ledger()).lower()
        for forbidden in ("delegat", "handoff", "handed", "published", "delivered",
                          "telegram", "zernio"):
            self.assertNotIn(forbidden, text)
        # STORY is never publication authority: it is not accepted.
        self.assertNotEqual(h.ledger()["accepted_candidate_id"], "story-1")

    def test_all_ranked_story_is_truthful_no_action(self):
        h = Harness(self, [story_item("story-1"), story_item("story-2")])
        summary = h.run()
        self.assertEqual(summary, {"status": "NO_ACTION", "candidate_id": None})
        self.assertEqual(h.rounds, ["SELECT"])  # PRODUCE never attempted
        ledger = h.ledger()
        self.assertIsNone(ledger["accepted_candidate_id"])
        self.assertEqual(
            [(a["candidate_id"], a["disposition"]) for a in ledger["attempts"]],
            [("story-1", "STORY_NOT_FACTORY_PRODUCIBLE"),
             ("story-2", "STORY_NOT_FACTORY_PRODUCIBLE")],
        )
        self.assertEqual(
            fallback.next_candidate(ledger)["domain_outcome"], "NO_ACTION"
        )
        self.assertEqual(
            [c[0] for c in h.helper_calls],
            ["nullone-packaging-evaluator.py"] * 2,
        )
        for cid in ("story-1", "story-2"):
            h.assert_no_consequential_output(self, cid)

    def test_story_and_skip_combinations_preserve_frozen_ranking(self):
        h = Harness(
            self,
            [skip_item("skip-1"), story_item("story-2"), post_item("post-3")],
        )
        with self.assertRaises(StopAtProduce):
            h.run()
        self.assertEqual(
            [c[1] for c in h.helper_calls], ["skip-1", "story-2", "post-3"]
        )
        ledger = h.ledger()
        self.assertEqual(
            ledger["ranked_candidate_ids"], ["skip-1", "story-2", "post-3"]
        )
        self.assertEqual(ledger["accepted_candidate_id"], "post-3")
        self.assertEqual(ledger["attempts"][0]["skip_reason"],
                         "REAL_PHOTO_REQUIRED_NO_FALLBACK")
        self.assertEqual(ledger["attempts"][1]["disposition"],
                         "STORY_NOT_FACTORY_PRODUCIBLE")

    def test_story_then_skip_only_is_no_action(self):
        h = Harness(self, [story_item("story-1"), skip_item("skip-2")])
        self.assertEqual(h.run()["status"], "NO_ACTION")
        ledger = h.ledger()
        self.assertEqual(len(ledger["attempts"]), 2)
        self.assertIsNone(ledger["accepted_candidate_id"])
        self.assertIn(
            "ALL_RANKED_NOT_FACTORY_PRODUCIBLE",
            fallback.next_candidate(ledger)["reason"],
        )

    def test_at_most_one_candidate_accepted(self):
        h = Harness(
            self, [story_item("story-1"), post_item("post-2"), post_item("post-3")]
        )
        with self.assertRaises(StopAtProduce):
            h.run()
        # post-3 is never even evaluated once post-2 is accepted.
        self.assertEqual(
            [c[1] for c in h.helper_calls], ["story-1", "post-2"]
        )
        self.assertEqual(h.ledger()["accepted_candidate_id"], "post-2")
        self.assertEqual(h.rounds.count("PRODUCE"), 1)

    def test_story_never_returns_delegated(self):
        h = Harness(self, [story_item("story-1")])
        self.assertNotEqual(h.run().get("status"), "DELEGATED")
        self.assertNotIn(
            "DELEGATED",
            (SCRIPTS / "nullone_claude_draft_provider.py").read_text(encoding="utf-8"),
        )


class ExistingStoryReceiptTests(unittest.TestCase):
    def _preseed(self, h, cid, item):
        request = copy.deepcopy(item["packaging_request"])
        body = receipt_mod.evaluate_request(cid, request)
        path = receipt_mod.canonical_receipt_path(cid, root=h.root)
        atomic_write_json(path, body)
        return path, body

    def test_existing_story_receipt_skips_rewrite_and_evaluator(self):
        story = story_item("story-1")
        h = Harness(self, [story, post_item("post-2")])
        path, body = self._preseed(h, "story-1", story)
        before = path.read_bytes()
        # Changed model bytes on this cycle must not matter.
        h.items = [
            ranked_item("story-1", packaging_request=copy.deepcopy(
                post_item("story-1")["packaging_request"])),
            post_item("post-2"),
        ]
        with self.assertRaises(StopAtProduce):
            h.run()
        self.assertEqual(h.helper_calls, [("nullone-packaging-evaluator.py", "post-2")])
        self.assertEqual(path.read_bytes(), before)  # receipt not rewritten
        today = draft._today(h.root)
        self.assertFalse((h.prod / f"{today}-story-1-packaging-request.json").exists())
        self.assertFalse((h.prod / f"{today}-story-1-packaging-asset.json").exists())
        ledger = h.ledger()
        self.assertEqual(ledger["accepted_candidate_id"], "post-2")
        self.assertEqual(ledger["attempts"][0]["format_reason"], body["FORMAT_REASON"])

    def test_existing_story_receipt_alone_is_no_action_without_evaluator(self):
        story = story_item("story-1")
        h = Harness(self, [story])
        self._preseed(h, "story-1", story)
        self.assertEqual(h.run()["status"], "NO_ACTION")
        self.assertEqual(h.helper_calls, [])
        self.assertEqual(h.rounds, ["SELECT"])

    def test_tampered_existing_receipt_fails_closed(self):
        story = story_item("story-1")
        h = Harness(self, [story, post_item("post-2")])
        path, body = self._preseed(h, "story-1", story)
        tampered = dict(body, FORMAT_DECISION="SINGLE_POST")  # hash now wrong
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(BridgeError) as ctx:
            h.run()
        self.assertIn("TAMPERED", str(ctx.exception))
        self.assertEqual(h.helper_calls, [])
        self.assertEqual(h.rounds, ["SELECT"])
        self.assertEqual(json.loads(path.read_text()), tampered)  # not overwritten

    def test_malformed_existing_receipt_fails_closed(self):
        story = story_item("story-1")
        h = Harness(self, [story, post_item("post-2")])
        path = receipt_mod.canonical_receipt_path("story-1", root=h.root)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(BridgeError):
            h.run()
        self.assertEqual(h.helper_calls, [])
        self.assertEqual(path.read_text(), "{not json")

    def test_receipt_for_other_candidate_fails_closed(self):
        story = story_item("story-1")
        h = Harness(self, [story, post_item("post-2")])
        path, body = self._preseed(h, "story-1", story)
        other = receipt_mod.evaluate_request("someone-else", story["packaging_request"])
        atomic_write_json(path, other)
        with self.assertRaises(BridgeError):
            h.run()
        self.assertEqual(h.helper_calls, [])

    def test_existing_post_receipt_keeps_idempotent_evaluator_path(self):
        post = post_item("post-1")
        h = Harness(self, [post])
        self._preseed(h, "post-1", post)
        with self.assertRaises(StopAtProduce):
            h.run()
        self.assertEqual(h.helper_calls, [("nullone-packaging-evaluator.py", "post-1")])


class FallbackContractTests(unittest.TestCase):
    def test_classification_is_typed(self):
        story = {"POST_DECISION": "POST", "FORMAT_DECISION": "STORY",
                 "FORMAT_REASON": STORY_REASON}
        self.assertEqual(
            fallback.classify_packaging_receipt(story),
            ("STORY_FALLBACK", STORY_REASON),
        )
        self.assertEqual(
            fallback.classify_packaging_receipt(
                {"POST_DECISION": "POST", "FORMAT_DECISION": "CAROUSEL"}
            ),
            ("PROCEED", None),
        )
        self.assertEqual(
            fallback.classify_packaging_receipt(
                {"POST_DECISION": "SKIP", "FORMAT_DECISION": "SKIP",
                 "FORMAT_REASON": "LOW_AUDIENCE_VALUE"}
            ),
            ("SKIP_FALLBACK", "LOW_AUDIENCE_VALUE"),
        )
        for bad in (
            dict(story, FORMAT_REASON="INVENTED"),
            {k: v for k, v in story.items() if k != "FORMAT_REASON"},
            {"POST_DECISION": "SKIP", "FORMAT_DECISION": "STORY",
             "FORMAT_REASON": STORY_REASON},
        ):
            with self.assertRaises(fallback.DraftFallbackError):
                fallback.classify_packaging_receipt(bad)

    def test_story_disposition_guards(self):
        ledger = fallback.new_ledger(
            editorial_date="2026-10-03", ranked_candidate_ids=["a", "b", "c"]
        )
        with self.assertRaises(fallback.DraftFallbackError):
            fallback.record_story_disposition(ledger, candidate_id="a", format_reason="X")
        with self.assertRaises(fallback.DraftFallbackError):
            fallback.record_story_disposition(
                ledger, candidate_id="injected", format_reason=STORY_REASON)
        fallback.record_story_disposition(
            ledger, candidate_id="a", format_reason=STORY_REASON)
        with self.assertRaises(fallback.DraftFallbackError):  # once only
            fallback.record_story_disposition(
                ledger, candidate_id="a", format_reason=STORY_REASON)
        with self.assertRaises(fallback.DraftFallbackError):  # never accepted
            fallback.record_acceptance(ledger, candidate_id="a")
        fallback.record_acceptance(ledger, candidate_id="b")
        with self.assertRaises(fallback.DraftFallbackError):  # one maximum
            fallback.record_story_disposition(
                ledger, candidate_id="c", format_reason=STORY_REASON)

    def test_legacy_ledger_without_disposition_is_unchanged(self):
        legacy = {
            "schema": fallback.SCHEMA,
            "editorial_date": "2026-09-16",
            "ranked_candidate_ids": ["a", "b"],
            "attempts": [{
                "candidate_id": "a",
                "skip_reason": "REAL_PHOTO_REQUIRED_NO_FALLBACK",
                "recorded_at": "2026-09-16T00:00:00+00:00",
            }],
            "accepted_candidate_id": None,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            ledger = fallback.load_ledger(path)
        summary = fallback.ledger_summary(ledger)
        self.assertEqual(summary["attempts"], [{
            "candidate_id": "a", "skip_reason": "REAL_PHOTO_REQUIRED_NO_FALLBACK"}])
        fallback.record_skip(ledger, candidate_id="b", skip_reason="LOW_AUDIENCE_VALUE")
        end = fallback.next_candidate(ledger)
        self.assertTrue(end["reason"].startswith("ALL_ELIGIBLE_SKIPPED"))
        self.assertIn("a:REAL_PHOTO_REQUIRED_NO_FALLBACK", end["reason"])


class AuthorityUnchangedTests(unittest.TestCase):
    STORY_SOURCES = (
        "nullone_story_workflow.py",
        "nullone_story_scheduled_workflow.py",
        "nullone_story_candidate_provider.py",
        "nullone_story_pipeline.py",
        "nullone_story_production_provider.py",
    )

    def test_no_draft_to_story_handoff_exists(self):
        for name in self.STORY_SOURCES:
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertNotIn("nullone_draft_candidate_fallback", text, name)
            self.assertNotIn("draft-fallback-ledger", text, name)
        for name in ("nullone_draft_candidate_fallback.py",
                     "nullone_claude_draft_provider.py"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            for forbidden in ("nullone_story", "editorial-candidates"):
                self.assertNotIn(forbidden, text, name)

    def test_cycle_creates_no_story_artifacts(self):
        h = Harness(self, [story_item("story-1"), post_item("post-2")])
        with self.assertRaises(StopAtProduce):
            h.run()
        for p in h.files():
            self.assertNotIn("story", Path(p).name.replace("story-1", "").lower(), p)
            self.assertNotIn("handoff", p.lower())
            self.assertNotIn("editorial-candidates", p)


if __name__ == "__main__":
    unittest.main()
