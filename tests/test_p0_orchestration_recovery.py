#!/usr/bin/env python3
"""P0 #140 orchestration recovery: candidate fallback + stale approval lifecycle.

Deterministic offline tests (no network, no Zernio, no Telegram, no model).

Coverage map (goal section 10):
  A. rank1 SKIP -> rank2 PASS: rank2 proceeds, exactly one accepted,
     rank1 zero consequential side effects.
  B. all SKIP -> NO_ACTION with aggregate reasons, zero side effects.
  C. system/evaluator ERROR -> fail closed immediately, rank2 untouched.
  D. same candidate cannot be attempted twice.
  E. candidate order preserved.
  F. visual-grounding protections (#139) still PASS.
  G. generated illustration still cannot satisfy required real evidence.
  H. previous-day pending STORY does not block today's eligible Story.
  I. same-day pending STORY still blocks duplicate Story preview.
  J. previous-day pending FEED does not count as current-day blocking.
  K. expired old callback fails closed.
  L. expired callback: zero publish / Zernio / second-confirmation.
  M. historical manifest remains unchanged / immutable.
  N. Asia/Baku date-transition determinism.
Plus section 11: real-workflow contract rehearsal (real packaging
authority + fallback + fake consequential boundaries).
Regressions O-V are owned by their existing suites in run_offline.py.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_approval_controller as approval  # noqa: E402
import nullone_draft_candidate_fallback as fallback  # noqa: E402
import nullone_packaging_policy as policy  # noqa: E402
import nullone_packaging_receipt as receipt_mod  # noqa: E402
import nullone_review_lifecycle as lifecycle  # noqa: E402
from nullone_approval_controller import handle_approval_callback  # noqa: E402
from nullone_bridge_common import BridgeError  # noqa: E402
from nullone_cadence_controller import evaluate_cadence  # noqa: E402
from nullone_cadence_state_adapter import (  # noqa: E402
    assemble_cadence_request,
    collect_format_loads,
)
from nullone_draft_candidate_fallback import DraftFallbackError  # noqa: E402

BAKU = ZoneInfo("Asia/Baku")


def skip_request(candidate_id="rank1-skip"):
    """Mirrors the proven 2026-09-16 incident: ANNOUNCEMENT, no photo."""
    return {
        "candidate": {
            "content_type": "NEWS",
            "content_shape": "ANNOUNCEMENT",
            "timeliness": "TODAY",
            "verification_status": "PASS",
            "source_grounding": "STRONG_PRIMARY",
            "audience_value": "HIGH",
            "distinct_beat_count": 2,
            "depicts_real_world_subject": False,
            "still_developing": False,
            "visual_requirement": "NONE",
        },
        "assets": {
            "has_official_or_source_image": False,
            "has_usable_screenshot": False,
            "image_on_topic": False,
            "image_quality_ok": False,
            "data_visualization_possible": False,
        },
    }


def post_request():
    """A request the real authority POSTs: explainer with dataviz."""
    return {
        "candidate": {
            "content_type": "EXPLAINER",
            "content_shape": "MULTI_STEP_EXPLAINER",
            "timeliness": "THIS_WEEK",
            "verification_status": "PASS",
            "source_grounding": "STRONG_PRIMARY",
            "audience_value": "MEDIUM",
            "distinct_beat_count": 4,
            "depicts_real_world_subject": False,
            "still_developing": False,
            "visual_requirement": "NONE",
        },
        "assets": {
            "has_official_or_source_image": False,
            "has_usable_screenshot": False,
            "image_on_topic": False,
            "image_quality_ok": False,
            "data_visualization_possible": True,
        },
    }


class ConsequentialRecorder:
    """Fake downstream boundary: records crossings, performs none."""

    def __init__(self):
        self.render_calls: list[str] = []
        self.manifest_calls: list[str] = []
        self.bridge_calls: list[str] = []
        self.telegram_calls: list[str] = []

    def render(self, candidate_id):
        self.render_calls.append(candidate_id)

    def manifest(self, candidate_id):
        self.manifest_calls.append(candidate_id)

    def bridge(self, candidate_id):
        self.bridge_calls.append(candidate_id)

    def telegram(self, candidate_id):
        self.telegram_calls.append(candidate_id)

    def total(self):
        return (
            len(self.render_calls)
            + len(self.manifest_calls)
            + len(self.bridge_calls)
            + len(self.telegram_calls)
        )


def run_candidate_cycle(ranked, requests, recorder):
    """Real authority + fallback + gated fake consequential boundary."""
    ledger = fallback.new_ledger(
        editorial_date="2026-09-16", ranked_candidate_ids=list(ranked)
    )
    while True:
        step = fallback.next_candidate(ledger)
        if step["decision"] == fallback.DECISION_ALL_SKIPPED:
            return ledger, step
        cid = step["candidate_id"]
        body = receipt_mod.evaluate_request(cid, requests[cid])
        kind, reason = fallback.classify_packaging_receipt(body)
        if kind == "SKIP_FALLBACK":
            fallback.record_skip(ledger, candidate_id=cid, skip_reason=reason)
            continue
        fallback.record_acceptance(ledger, candidate_id=cid)
        recorder.render(cid)
        recorder.manifest(cid)
        recorder.bridge(cid)
        recorder.telegram(cid)
        return ledger, {"decision": "ACCEPTED", "candidate_id": cid}


class FallbackSemanticsTest(unittest.TestCase):
    def test_a_skip_then_post_accepts_second_with_zero_skip_side_effects(self):
        recorder = ConsequentialRecorder()
        ledger, outcome = run_candidate_cycle(
            ["rank1", "rank2"],
            {"rank1": skip_request(), "rank2": post_request()},
            recorder,
        )
        self.assertEqual(outcome["decision"], "ACCEPTED")
        self.assertEqual(outcome["candidate_id"], "rank2")
        self.assertEqual(ledger["accepted_candidate_id"], "rank2")
        self.assertEqual(
            [a["candidate_id"] for a in ledger["attempts"]], ["rank1"]
        )
        self.assertEqual(
            ledger["attempts"][0]["skip_reason"],
            "REAL_PHOTO_REQUIRED_NO_FALLBACK",
        )
        for calls in (
            recorder.render_calls,
            recorder.manifest_calls,
            recorder.bridge_calls,
            recorder.telegram_calls,
        ):
            self.assertEqual(calls, ["rank2"])

    def test_b_all_skip_is_legitimate_no_action_with_aggregate(self):
        recorder = ConsequentialRecorder()
        ledger, outcome = run_candidate_cycle(
            ["r1", "r2", "r3"],
            {"r1": skip_request(), "r2": skip_request(), "r3": skip_request()},
            recorder,
        )
        self.assertEqual(outcome["decision"], fallback.DECISION_ALL_SKIPPED)
        self.assertEqual(outcome["domain_outcome"], "NO_ACTION")
        self.assertIn("r1:REAL_PHOTO_REQUIRED_NO_FALLBACK", outcome["reason"])
        self.assertIn("r2:REAL_PHOTO_REQUIRED_NO_FALLBACK", outcome["reason"])
        self.assertIn("r3:REAL_PHOTO_REQUIRED_NO_FALLBACK", outcome["reason"])
        self.assertEqual(len(outcome["skipped"]), 3)
        self.assertEqual(recorder.total(), 0)
        self.assertIsNone(ledger["accepted_candidate_id"])

    def test_c_system_failure_fails_closed_and_stops(self):
        ledger = fallback.new_ledger(
            editorial_date="2026-09-16",
            ranked_candidate_ids=["rank1", "rank2"],
        )
        with self.assertRaises(BridgeError):
            receipt_mod.evaluate_request("rank1", {"candidate": {}, "assets": {}})
        # The failure happened BEFORE any ledger record: rank2 untouched,
        # no skip recorded, next still points at rank1 (cycle halted).
        self.assertEqual(ledger["attempts"], [])
        self.assertEqual(
            fallback.next_candidate(ledger)["candidate_id"], "rank1"
        )
        # A malformed receipt dict also stops instead of continuing.
        with self.assertRaises(DraftFallbackError):
            fallback.classify_packaging_receipt(
                {"POST_DECISION": "SKIP", "FORMAT_DECISION": "SKIP",
                 "FORMAT_REASON": "INVENTED"}
            )

    def test_d_same_candidate_cannot_be_attempted_twice(self):
        ledger = fallback.new_ledger(
            editorial_date="2026-09-16", ranked_candidate_ids=["a", "b"]
        )
        fallback.record_skip(
            ledger, candidate_id="a",
            skip_reason="REAL_PHOTO_REQUIRED_NO_FALLBACK",
        )
        with self.assertRaises(DraftFallbackError):
            fallback.record_skip(
                ledger, candidate_id="a",
                skip_reason="REAL_PHOTO_REQUIRED_NO_FALLBACK",
            )
        with self.assertRaises(DraftFallbackError):
            fallback.record_acceptance(ledger, candidate_id="a")

    def test_e_candidate_order_preserved(self):
        ledger = fallback.new_ledger(
            editorial_date="2026-09-16",
            ranked_candidate_ids=["first", "second", "third"],
        )
        seen = []
        for _ in range(3):
            step = fallback.next_candidate(ledger)
            if step["decision"] != fallback.DECISION_NEXT:
                break
            seen.append(step["candidate_id"])
            fallback.record_skip(
                ledger, candidate_id=step["candidate_id"],
                skip_reason="LOW_AUDIENCE_VALUE",
            )
        self.assertEqual(seen, ["first", "second", "third"])

    def test_f_visual_grounding_unmet_still_fails_closed(self):
        grounded = skip_request()
        grounded["candidate"]["visual_requirement"] = "SOURCE_GROUNDED"
        with self.assertRaises(BridgeError) as ctx:
            receipt_mod.evaluate_request("grounded-no-evidence", grounded)
        self.assertIn("PACKAGING_VISUAL_GROUNDING_UNMET", str(ctx.exception))

    def test_g_generated_illustration_cannot_satisfy_real_evidence(self):
        body = receipt_mod.evaluate_request("rank1", skip_request())
        self.assertEqual(body["POST_DECISION"], "SKIP")
        self.assertEqual(body["FORMAT_DECISION"], "SKIP")
        self.assertEqual(
            body["FORMAT_REASON"], "REAL_PHOTO_REQUIRED_NO_FALLBACK"
        )
        # The authority forbids the generated stand-in explicitly.
        self.assertEqual(body["VISUAL_STYLE"], "GENERATED_ILLUSTRATION_FORBIDDEN")
        self.assertNotEqual(body["VISUAL_STYLE"], "GENERATED_ILLUSTRATION_ALLOWED")


def write_manifest(
    manifest_dir,
    manifest_id,
    fmt="STORY",
    review_state="DRAFT_CREATED",
    created_at="2026-09-15T09:30:54.139355+00:00",
    first_stage=False,
    final_publish=False,
    pub_state="NOT_REQUESTED",
):
    manifest = {
        "schema": "nullone.production.v1",
        "manifest_id": manifest_id,
        "created_at": created_at,
        "format": fmt,
        "review": {"state": review_state, "create_attempts": 1},
        "approval": {
            "first_stage": first_stage,
            "final_publish": final_publish,
        },
        "publication": {"state": pub_state},
    }
    path = manifest_dir / f"{manifest_id}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def make_state_root(tmp, manifests=()):
    root = Path(tmp)
    manifest_dir = root / "ops" / "manifests"
    manifest_dir.mkdir(parents=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    for spec in manifests:
        write_manifest(manifest_dir, **spec)
    return root


def append_published_main_rows(root, count=2):
    """Close the main gap so the story format is the evaluated one."""
    ledger = root / "state" / "publish-ledger.jsonl"
    with ledger.open("a", encoding="utf-8") as fh:
        for i in range(count):
            fh.write(
                json.dumps(
                    {
                        "timestamp": "2026-09-16T07:00:00+00:00",
                        "result": "PUBLISHED",
                        "format": "FEED",
                        "manifest_id": f"main-published-{i}",
                        "candidate_id": f"main-published-{i}",
                    }
                )
                + "\n"
            )


class StaleLifecycleTest(unittest.TestCase):
    def test_h_previous_day_pending_story_does_not_block_today(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = make_state_root(
                tmp,
                manifests=[
                    {
                        "manifest_id": "story-yesterday",
                        "fmt": "STORY",
                        "created_at": "2026-09-15T09:30:54.139355+00:00",
                    }
                ],
            )
            now = datetime(2026, 9, 16, 10, 30, tzinfo=BAKU)
            loads = collect_format_loads(state_root=root, now=now)
            self.assertEqual(loads["story_load"]["pending"], 0)
            self.assertEqual(loads["story_load"]["expired_suppressed"], 1)
            request = assemble_cadence_request(
                state_root=root,
                now=now,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
            )
            rec = evaluate_cadence(request)
            self.assertNotEqual(rec["reason_code"], "PENDING_STORY_EXISTS")
            self.assertEqual(rec["recommendation"], "PREPARE_STORY")

    def test_i_same_day_pending_story_still_blocks_duplicates(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = make_state_root(
                tmp,
                manifests=[
                    {
                        "manifest_id": "story-today",
                        "fmt": "STORY",
                        "created_at": "2026-09-16T06:30:00+00:00",
                    }
                ],
            )
            now = datetime(2026, 9, 16, 13, 30, tzinfo=BAKU)
            loads = collect_format_loads(state_root=root, now=now)
            self.assertEqual(loads["story_load"]["pending"], 1)
            self.assertEqual(loads["story_load"]["expired_suppressed"], 0)
            append_published_main_rows(root)
            request = assemble_cadence_request(
                state_root=root,
                now=now,
                candidate_availability={
                    "main_quality_candidate_available": True,
                    "story_quality_candidate_available": True,
                },
            )
            rec = evaluate_cadence(request)
            self.assertEqual(rec["reason_code"], "PENDING_STORY_EXISTS")
            self.assertEqual(rec["recommendation"], "NO_ACTION")

    def test_j_previous_day_pending_feed_does_not_block_today(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = make_state_root(
                tmp,
                manifests=[
                    {
                        "manifest_id": "feed-yesterday",
                        "fmt": "FEED",
                        "created_at": "2026-09-15T15:30:11.905290+00:00",
                    }
                ],
            )
            now = datetime(2026, 9, 16, 10, 30, tzinfo=BAKU)
            loads = collect_format_loads(state_root=root, now=now)
            self.assertEqual(loads["main_load"]["pending"], 0)
            self.assertEqual(loads["main_load"]["expired_suppressed"], 1)

    def test_n_baku_date_boundary_is_deterministic(self):
        # 23:59:59 Baku Sep-15 is yesterday; 00:00:00 Baku Sep-16 is today.
        self.assertEqual(
            lifecycle.parse_baku_date("2026-09-15T19:59:59+00:00").isoformat(),
            "2026-09-15",
        )
        self.assertEqual(
            lifecycle.parse_baku_date("2026-09-15T20:00:00+00:00").isoformat(),
            "2026-09-16",
        )
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = make_state_root(
                tmp,
                manifests=[
                    {
                        "manifest_id": "edge-old",
                        "fmt": "STORY",
                        "created_at": "2026-09-15T19:59:59+00:00",
                    },
                    {
                        "manifest_id": "edge-new",
                        "fmt": "FEED",
                        "created_at": "2026-09-15T20:00:00+00:00",
                    },
                ],
            )
            now = datetime(2026, 9, 16, 0, 1, tzinfo=BAKU)
            loads = collect_format_loads(state_root=root, now=now)
            self.assertEqual(loads["story_load"]["pending"], 0)
            self.assertEqual(loads["main_load"]["pending"], 1)

    def test_unproven_and_future_dates_stay_blocking(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_dir = root / "ops" / "manifests"
            manifest_dir.mkdir(parents=True)
            (root / "state").mkdir(parents=True, exist_ok=True)
            # Missing created_at: fail closed to blocking.
            manifest = {
                "schema": "nullone.production.v1",
                "manifest_id": "no-date",
                "format": "STORY",
                "review": {"state": "DRAFT_CREATED"},
                "approval": {"first_stage": False, "final_publish": False},
                "publication": {"state": "NOT_REQUESTED"},
            }
            (manifest_dir / "no-date.json").write_text(json.dumps(manifest))
            # Future clock skew: fail closed to blocking.
            write_manifest(
                manifest_dir,
                "future",
                fmt="FEED",
                created_at="2026-09-17T10:00:00+00:00",
            )
            now = datetime(2026, 9, 16, 10, 30, tzinfo=BAKU)
            loads = collect_format_loads(state_root=root, now=now)
            self.assertEqual(loads["story_load"]["pending"], 1)
            self.assertEqual(loads["main_load"]["pending"], 1)
            self.assertEqual(loads["story_load"]["expired_suppressed"], 0)

    def test_m_historical_manifest_bytes_unchanged(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = make_state_root(
                tmp,
                manifests=[
                    {
                        "manifest_id": "story-yesterday",
                        "fmt": "STORY",
                        "created_at": "2026-09-15T09:30:54.139355+00:00",
                    }
                ],
            )
            path = root / "ops" / "manifests" / "story-yesterday.json"
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            now = datetime(2026, 9, 16, 10, 30, tzinfo=BAKU)
            collect_format_loads(state_root=root, now=now)
            assemble_cadence_request(
                state_root=root,
                now=now,
                candidate_availability={
                    "main_quality_candidate_available": True,
                    "story_quality_candidate_available": True,
                },
            )
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(before, after)


POST_ID = "0123456789abcdef01234567"
IDS = {
    "message_id": 424242,
    "chat_id": "770011",
    "account_id": "test-bot-account",
    "sender_id": "990022",
}


class ExpiredCallbackTest(unittest.TestCase):
    def call(self, **kw):
        args = {
            "authorized": True,
            "action": "approve",
            "post_id": POST_ID,
            "current_stage": approval.STAGE_DRAFT_READY,
            **IDS,
            **kw,
        }
        return handle_approval_callback(**args)

    def test_k_expired_callback_fails_closed_on_every_action(self):
        for action in ("approve", "reject", "revise", "back"):
            for stage in (
                approval.STAGE_DRAFT_READY,
                approval.STAGE_AWAITING,
                approval.STAGE_REJECTED,
                approval.STAGE_REVISION,
            ):
                seen: set[str] = set()
                res = self.call(
                    action=action,
                    current_stage=stage,
                    seen_keys=seen,
                    review_expired=True,
                )
                self.assertEqual(
                    res["outcome"], approval.OUTCOME_REJECTED_EXPIRED,
                    (action, stage),
                )
                self.assertEqual(res["to_stage"], stage, (action, stage))
                self.assertEqual(seen, set(), (action, stage))

    def test_l_expired_callback_zero_publish_zernio_confirmation(self):
        seen: set[str] = set()
        res = self.call(seen_keys=seen, review_expired=True)
        self.assertFalse(res["publish_authorized"])
        self.assertEqual(res["zernio_calls"], 0)
        self.assertFalse(res["receipt"]["publish_authorized"])
        self.assertIsNone(res["reply"]["buttons"])

    def test_default_path_unchanged_without_expiry_flag(self):
        res = self.call()
        self.assertEqual(res["outcome"], approval.OUTCOME_TRANSITIONED)
        self.assertEqual(res["to_stage"], approval.STAGE_AWAITING)


class ContractRehearsalTest(unittest.TestCase):
    """Section 11: real boundaries end-to-end with fake consequentials."""

    def test_first_skip_second_pass_only_second_crosses(self):
        ranked = ["apple-siri-ai-rehearsal", "gates-equity-rehearsal"]
        requests = {
            "apple-siri-ai-rehearsal": skip_request(),
            "gates-equity-rehearsal": post_request(),
        }
        recorder = ConsequentialRecorder()
        ledger, outcome = run_candidate_cycle(ranked, requests, recorder)
        self.assertEqual(outcome["candidate_id"], "gates-equity-rehearsal")
        summary = fallback.ledger_summary(ledger)
        self.assertEqual(
            summary["candidates_considered"], ranked
        )
        self.assertEqual(
            summary["attempts"],
            [
                {
                    "candidate_id": "apple-siri-ai-rehearsal",
                    "skip_reason": "REAL_PHOTO_REQUIRED_NO_FALLBACK",
                }
            ],
        )
        self.assertEqual(
            summary["final_selected_candidate_id"], "gates-equity-rehearsal"
        )
        self.assertEqual(recorder.render_calls, ["gates-equity-rehearsal"])
        self.assertEqual(recorder.bridge_calls, ["gates-equity-rehearsal"])
        self.assertEqual(recorder.telegram_calls, ["gates-equity-rehearsal"])

    def test_tampered_receipt_is_system_failure_not_fallback(self):
        import tempfile

        body = receipt_mod.evaluate_request("rank1", skip_request())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prod = root / "social" / "drafts" / "production"
            prod.mkdir(parents=True)
            receipt_path = prod / "rank1-packaging-decision.json"
            receipt_path.write_text(
                json.dumps(body, indent=2, sort_keys=True), encoding="utf-8"
            )
            loaded = receipt_mod.load_receipt(receipt_path, root=root)
            self.assertEqual(loaded["FORMAT_REASON"], "REAL_PHOTO_REQUIRED_NO_FALLBACK")
            tampered = copy.deepcopy(body)
            tampered["FORMAT_DECISION"] = "SINGLE_POST"
            receipt_path.write_text(
                json.dumps(tampered, indent=2, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaises(BridgeError):
                receipt_mod.load_receipt(receipt_path, root=root)

    def test_yesterday_pending_plus_fresh_candidate_allows_new_story(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = make_state_root(
                tmp,
                manifests=[
                    {
                        "manifest_id": (
                            "story-frontier-pace-slowdown-ipo-2026-09-15-x"
                        ),
                        "fmt": "STORY",
                        "created_at": "2026-09-15T09:30:54.139355+00:00",
                    }
                ],
            )
            now = datetime(2026, 9, 16, 10, 30, tzinfo=BAKU)
            loads = collect_format_loads(state_root=root, now=now)
            self.assertEqual(loads["story_load"]["pending"], 0)
            request = assemble_cadence_request(
                state_root=root,
                now=now,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
            )
            rec = evaluate_cadence(request)
            self.assertEqual(rec["recommendation"], "PREPARE_STORY")


if __name__ == "__main__":
    unittest.main()
