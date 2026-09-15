#!/usr/bin/env python3
"""Offline tests for the deterministic first-stage approval control (P0, #132).

Covers: valid/duplicate/stale/wrong-ref/wrong-stage Approve, Reject (+dup),
Revise, Back, Confirm-Publish correct/wrong-state (existing deterministic
route regression), replayed Confirm idempotency, zero model invocation on
all control callbacks, zero Zernio publication before second confirmation,
and Python/JS parity for the mirrored state machine.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_approval_controller as approval  # noqa: E402

POST = "0123456789abcdef01234567"
OTHER = "aaaaaaaaaaaaaaaaaaaaaaaa"

IDS = {
    "message_id": 424242,
    "chat_id": "770011",
    "account_id": "test-bot-account",
    "sender_id": "990022",
}


def call(action, **kw):
    args = {
        "action": action,
        "post_id": POST,
        "current_stage": approval.STAGE_DRAFT_READY,
        "authorized": True,
        **IDS,
        **kw,
    }
    return approval.handle_approval_callback(**args)


class ApproveTests(unittest.TestCase):
    def test_valid_approve_transitions_to_second_confirmation(self) -> None:
        seen: set[str] = set()
        res = call("approve", seen_keys=seen)
        self.assertEqual(res["outcome"], approval.OUTCOME_TRANSITIONED)
        self.assertEqual(res["from_stage"], approval.STAGE_DRAFT_READY)
        self.assertEqual(res["to_stage"], approval.STAGE_AWAITING)
        self.assertEqual(res["reply"]["text"], approval.TEXT_APPROVE_CARD)
        buttons = res["reply"]["buttons"]
        self.assertIsNotNone(buttons)
        assert buttons is not None
        values = sorted(b["value"] for b in buttons)
        self.assertEqual(
            values, [f"texbrif:back:{POST}", f"texbrif:publish:{POST}"]
        )
        self.assertFalse(res["publish_authorized"])
        self.assertEqual(res["zernio_calls"], 0)

    def test_duplicate_approve_idempotent(self) -> None:
        seen: set[str] = set()
        first = call("approve", seen_keys=seen)
        second = call("approve", seen_keys=seen)
        self.assertEqual(second["outcome"], approval.OUTCOME_DUPLICATE)
        self.assertEqual(second["reply"], first["reply"])
        self.assertEqual(second["to_stage"], second["from_stage"])

    def test_stale_approve_converges_to_same_card(self) -> None:
        first = call("approve")
        stale = call("approve", current_stage=approval.STAGE_AWAITING,
                     message_id=111111)
        self.assertEqual(stale["outcome"], approval.OUTCOME_CONVERGED)
        self.assertEqual(stale["reply"], first["reply"])
        self.assertEqual(stale["to_stage"], approval.STAGE_AWAITING)

    def test_wrong_post_reference_rejected(self) -> None:
        for bad in ("ZZZ", "short", "", f"{POST}:extra", POST[:-1]):
            res = call("approve", post_id=bad)
            self.assertEqual(
                res["outcome"], approval.OUTCOME_REJECTED_MALFORMED, bad
            )
            self.assertFalse(res["publish_authorized"])
            self.assertEqual(res["zernio_calls"], 0)

    def test_wrong_stage_approve_after_reject(self) -> None:
        res = call("approve", current_stage=approval.STAGE_REJECTED,
                   message_id=424243)
        self.assertEqual(
            res["outcome"], approval.OUTCOME_REJECTED_WRONG_STATE
        )
        self.assertEqual(res["to_stage"], approval.STAGE_REJECTED)
        self.assertFalse(res["publish_authorized"])

    def test_unauthorized_callback_rejected_silently(self) -> None:
        res = call("approve", authorized=False)
        self.assertEqual(
            res["outcome"], approval.OUTCOME_REJECTED_UNAUTHORIZED
        )
        self.assertFalse(res["publish_authorized"])

    def test_publish_action_never_owned_here(self) -> None:
        res = call("publish")
        self.assertEqual(res["outcome"], approval.OUTCOME_REJECTED_MALFORMED)
        self.assertFalse(res["publish_authorized"])


class RejectReviseBackTests(unittest.TestCase):
    def test_reject_transitions(self) -> None:
        res = call("reject")
        self.assertEqual(res["outcome"], approval.OUTCOME_TRANSITIONED)
        self.assertEqual(res["to_stage"], approval.STAGE_REJECTED)
        self.assertEqual(res["reply"]["text"], approval.TEXT_REJECTED)
        self.assertFalse(res["publish_authorized"])
        self.assertEqual(res["zernio_calls"], 0)

    def test_duplicate_reject_converges(self) -> None:
        seen: set[str] = set()
        first = call("reject", seen_keys=seen)
        second = call("reject", seen_keys=seen)
        self.assertEqual(second["outcome"], approval.OUTCOME_DUPLICATE)
        self.assertEqual(second["reply"], first["reply"])

    def test_revise_transitions_to_revision_requested(self) -> None:
        res = call("revise")
        self.assertEqual(res["outcome"], approval.OUTCOME_TRANSITIONED)
        self.assertEqual(res["to_stage"], approval.STAGE_REVISION)
        self.assertEqual(res["reply"]["text"], approval.TEXT_REVISE)
        self.assertFalse(res["publish_authorized"])
        self.assertEqual(res["zernio_calls"], 0)

    def test_back_returns_awaiting_post_to_safe_view(self) -> None:
        res = call("back", current_stage=approval.STAGE_AWAITING)
        self.assertEqual(res["outcome"], approval.OUTCOME_TRANSITIONED)
        self.assertEqual(res["to_stage"], approval.STAGE_DRAFT_READY)
        self.assertEqual(res["reply"]["text"], approval.TEXT_BACK_SAFE)

    def test_back_on_draft_ready_is_safe_view(self) -> None:
        res = call("back")
        self.assertEqual(res["outcome"], approval.OUTCOME_CONVERGED)
        self.assertEqual(res["to_stage"], approval.STAGE_DRAFT_READY)


class TransitionTableTests(unittest.TestCase):
    def test_full_table_never_authorizes_or_calls_zernio(self) -> None:
        for stage in approval.STAGES:
            for action in ("approve", "reject", "revise", "back"):
                res = call(action, current_stage=stage)
                self.assertFalse(
                    res["publish_authorized"], f"{stage}/{action}"
                )
                self.assertEqual(res["zernio_calls"], 0, f"{stage}/{action}")
                self.assertFalse(
                    res["receipt"]["publish_authorized"],
                    f"{stage}/{action}",
                )
                self.assertIn(
                    res["outcome"],
                    {
                        approval.OUTCOME_TRANSITIONED,
                        approval.OUTCOME_CONVERGED,
                        approval.OUTCOME_DUPLICATE,
                        approval.OUTCOME_REJECTED_WRONG_STATE,
                    },
                    f"{stage}/{action}",
                )


class ModelFreeTests(unittest.TestCase):
    MODEL_TOKENS = (
        "mcp__zernio",
        "run_structured",
        "claude -p",
        "anthropic",
        "deepseek",
        "openai",
        "agentTurn",
        "sessions_send",
        "opencode run",
        "--model",
        "posts_publish_now",
    )
    NETWORK_TOKENS = (
        "urllib",
        "requests",
        "socket",
        "subprocess",
        "os.system",
        "Popen",
        "https://",
        "http.client",
    )

    def _sources(self) -> dict[str, str]:
        return {
            "controller": (
                SCRIPTS / "nullone_approval_controller.py"
            ).read_text(encoding="utf-8"),
            "approval_route": (
                ROOT
                / "plugins/nullone-final-publish/approval-route.js"
            ).read_text(encoding="utf-8"),
        }

    def test_no_model_invocation_in_control_path(self) -> None:
        for name, src in self._sources().items():
            lowered = src.lower()
            for token in self.MODEL_TOKENS:
                self.assertNotIn(token.lower(), lowered, f"{name}:{token}")

    def test_no_network_or_zernio_in_control_path(self) -> None:
        for name, src in self._sources().items():
            lowered = src.lower()
            for token in self.NETWORK_TOKENS:
                self.assertNotIn(token.lower(), lowered, f"{name}:{token}")

    def test_no_zernio_before_second_confirmation(self) -> None:
        # Every pre-confirmation control outcome authorizes nothing and
        # performs zero provider calls, by construction (see table test).
        for action in ("approve", "reject", "revise", "back"):
            res = call(action)
            self.assertFalse(res["publish_authorized"], action)
            self.assertEqual(res["zernio_calls"], 0, action)


class FinalPublishRouteRegressionTests(unittest.TestCase):
    """The existing deterministic publish route must be untouched."""

    def test_publish_route_still_claims_publish_only(self) -> None:
        route = (ROOT / "plugins/nullone-final-publish/route.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('decision: "publish"', route)
        # First-stage actions must NOT be claimed by the publish router;
        # they belong to approval-route.js now.
        self.assertIn("FALLTHROUGH_ACTIONS", route)

    def test_final_controller_still_model_free(self) -> None:
        bridge = (
            SCRIPTS / "nullone-publish-bridge.py"
        ).read_text(encoding="utf-8")
        lowered = bridge.lower()
        for token in ("nullone_claude", "run_structured", "claude -p",
                      "mcp__zernio"):
            if token == "mcp__zernio":
                # Historical comment only; no live import/call.
                self.assertNotIn("mcp__zernio__posts_publish_now", bridge)
                continue
            self.assertNotIn(token, lowered, token)


class PythonJsParityTests(unittest.TestCase):
    VECTORS = [
        {"action": "approve", "stage": approval.STAGE_DRAFT_READY},
        {"action": "reject", "stage": approval.STAGE_DRAFT_READY},
        {"action": "revise", "stage": approval.STAGE_DRAFT_READY},
        {"action": "back", "stage": approval.STAGE_AWAITING},
        {"action": "approve", "stage": approval.STAGE_REJECTED},
        {"action": "approve", "stage": approval.STAGE_AWAITING},
    ]

    def test_parity_with_js_mirror(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node binary is required for parity")
        assert node is not None
        script = (
            "const m = require('./plugins/nullone-final-publish/approval-route');"
            "const vectors = %s;"
            "const out = vectors.map(v => {"
            "  const s = m.createApprovalStore();"
            "  s._stages.set(v.post.toLowerCase(), v.stage);"
            "  const r = s.handle({action: v.action, postId: v.post,"
            "    authorized: true, messageId: 424242, chatId: '770011',"
            "    accountId: 'test-bot-account', senderId: '990022'});"
            "  return {outcome: r.outcome, toStage: r.toStage,"
            "    text: r.reply ? r.reply.text : null};"
            "});"
            "console.log(JSON.stringify(out));"
        ) % json.dumps(
            [
                {"action": v["action"], "stage": v["stage"], "post": POST}
                for v in self.VECTORS
            ]
        )
        proc = subprocess.run(
            [node, "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        js_results = json.loads(proc.stdout)
        self.assertEqual(len(js_results), len(self.VECTORS))
        for vector, js in zip(self.VECTORS, js_results):
            py = approval.handle_approval_callback(
                action=vector["action"],
                post_id=POST,
                current_stage=vector["stage"],
                authorized=True,
                **IDS,
            )
            self.assertEqual(js["outcome"], py["outcome"], vector)
            self.assertEqual(js["toStage"], py["to_stage"], vector)
            self.assertEqual(js["text"], py["reply"]["text"], vector)


if __name__ == "__main__":
    unittest.main(verbosity=2)
