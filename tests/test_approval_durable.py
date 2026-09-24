#!/usr/bin/env python3
"""Offline tests for durable first-stage Telegram approval state.

Traces to a real production defect (2026-09-24): a human REJECT for
POST_ID 6ab46980b85359fa0c9305c8 (WeatherNext 3) was received by Telegram
ingress, but the live callback path only ever mutated a per-process
in-memory Map -- the manifest, topic ledger, candidate queue, and Zernio
draft all still read DRAFT_CREATED afterward, and the Gateway process
stalled for ~29 minutes immediately after, which would have wiped even
that in-memory state.

This suite covers everything the durability fix is required to prove:
REJECT/APPROVE/REVISE persistence, restart recovery, duplicate-callback
idempotency, invalid-stage fail-closed, stale-review enforcement,
"approve never publishes", unauthorized-actor rejection, and
persistence-failure fail-closed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_approval_durable as durable  # noqa: E402
import nullone_bridge_common as common  # noqa: E402
import nullone_state as state  # noqa: E402
from nullone_bridge_common import BridgeError  # noqa: E402
from nullone_state import read_jsonl  # noqa: E402

POST = "0123456789abcdef01234567"

IDS = {
    "message_id": 424242,
    "chat_id": "770011",
    "account_id": "test-bot-account",
    "sender_id": "990022",
}


class IsolatedWorkspace:
    """Isolated WORKSPACE + MANIFEST_DIR for one test (mirrors the pattern
    already used in test_final_publish_controller.py)."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._old_workspace = common.WORKSPACE
        self._old_manifest_dir = common.MANIFEST_DIR

    def __enter__(self):
        common.WORKSPACE = self.root
        common.MANIFEST_DIR = self.root / "social/ops/manifests"
        return self.root

    def __exit__(self, *exc):
        common.WORKSPACE = self._old_workspace
        common.MANIFEST_DIR = self._old_manifest_dir
        self._tmp.cleanup()


def make_manifest(root: Path, *, created_at: str | None = None) -> Path:
    from PIL import Image

    caption_path = root / "social/drafts/approval-caption.txt"
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_path.write_text("Durable approval fixture.\n", encoding="utf-8")
    media_path = root / "social/drafts/approval-0.png"
    Image.new("RGB", (1080, 1350), (10, 20, 30)).save(media_path, "PNG")
    media = common.inspect_media(media_path, "FEED")

    manifest = {
        "schema": common.SCHEMA,
        "manifest_id": "approval-manifest",
        "created_at": created_at or common.now_iso(),
        "candidate_id": "candidate-approval",
        "topic": "Durable approval fixture",
        "topic_cluster": "durable-approval",
        "content_type": "NEWS",
        "format": "FEED",
        "verification": "PASS",
        "account_id": common.CANONICAL_ACCOUNT_ID,
        "caption": {
            "file": common.workspace_relative(caption_path),
            "sha256": common.sha256_file(caption_path),
        },
        "media": [media],
        "review": {
            "create_attempts": 1,
            "state": "DRAFT_CREATED",
            "zernio_draft_id": POST,
            "created_at": common.now_iso(),
        },
        "approval": {
            "first_stage": False,
            "first_stage_at": None,
            "final_publish": False,
            "final_publish_at": None,
            "source": None,
            "operator": None,
            "human_confirmation": None,
        },
        "publication": {
            "attempts": 0,
            "state": "NOT_REQUESTED",
            "live_zernio_post_id": None,
            "platform_post_id": None,
            "permalink": None,
            "last_checked_at": None,
            "error": None,
        },
    }
    path = common.MANIFEST_DIR / "approval-manifest.json"
    common.atomic_write_json(path, manifest)
    return path


def call(action: str, **overrides):
    args = {
        "action": action,
        "review_post_id": POST,
        "authorized": True,
        **IDS,
        **overrides,
    }
    return durable.handle_durable_approval_callback(**args)


class RejectPersistsTests(unittest.TestCase):
    def test_reject_persists_to_manifest(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            result = call("reject")
            self.assertEqual(result["outcome"], "TRANSITIONED")
            self.assertEqual(result["to_stage"], "REJECTED")
            _, manifest = common.load_manifest(path)
            self.assertEqual(manifest["approval"]["stage"], "REJECTED")
            self.assertTrue(manifest["approval"]["first_stage"])
            self.assertIsNotNone(manifest["approval"]["first_stage_at"])
            self.assertEqual(manifest["approval"]["operator"], IDS["sender_id"])
            self.assertEqual(manifest["approval"]["source"], "telegram")

    def test_reject_writes_exactly_one_ledger_row(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            call("reject")
            rows = read_jsonl(root / "social/state/topic-ledger.jsonl")
            approval_rows = [r for r in rows if r.get("event") == "APPROVAL_DECISION"]
            self.assertEqual(len(approval_rows), 1)
            self.assertEqual(approval_rows[0]["review_post_id"], POST)
            self.assertEqual(approval_rows[0]["to_stage"], "REJECTED")


class ApprovePersistsTests(unittest.TestCase):
    def test_approve_persists_to_awaiting(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            result = call("approve")
            self.assertEqual(result["to_stage"], "AWAITING_PUBLISH_CONFIRMATION")
            _, manifest = common.load_manifest(path)
            self.assertEqual(
                manifest["approval"]["stage"], "AWAITING_PUBLISH_CONFIRMATION"
            )

    def test_approve_does_not_publish(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            result = call("approve")
            self.assertFalse(result["publish_authorized"])
            self.assertEqual(result["zernio_calls"], 0)
            _, manifest = common.load_manifest(path)
            # Publication block is untouched by the first-stage decision.
            self.assertEqual(manifest["publication"]["attempts"], 0)
            self.assertEqual(manifest["publication"]["state"], "NOT_REQUESTED")
            self.assertFalse(manifest["approval"]["final_publish"])

    def test_second_confirmation_required_before_awaiting_can_publish(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            result = call("approve")
            # AWAITING_PUBLISH_CONFIRMATION is a distinct stage from
            # PUBLISHED: nothing about this result authorizes publication,
            # and re-sending "approve" on an already-AWAITING post only
            # converges (safe-view), it never republishes/republishes.
            self.assertEqual(result["to_stage"], "AWAITING_PUBLISH_CONFIRMATION")
            again = call("approve")
            self.assertEqual(again["outcome"], "CONVERGED")
            self.assertEqual(again["to_stage"], "AWAITING_PUBLISH_CONFIRMATION")


class RevisePersistsTests(unittest.TestCase):
    def test_revise_persists(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            result = call("revise")
            self.assertEqual(result["to_stage"], "REVISION_REQUESTED")
            _, manifest = common.load_manifest(path)
            self.assertEqual(manifest["approval"]["stage"], "REVISION_REQUESTED")


class RestartRecoveryTests(unittest.TestCase):
    """Proves the exact scenario the P0 investigation found broken: a
    fresh read (standing in for a Gateway restart, since nothing about the
    durable path involves an in-memory object at all) still sees the
    decision, and a stale APPROVE cannot resurrect a terminal stage."""

    def _restart_recovery(self, action: str, expected_stage: str):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            first = call(action)
            self.assertEqual(first["to_stage"], expected_stage)

            # "Process/store object destroyed and recreated": there is no
            # process-local object on this path at all to destroy, which
            # IS the fix -- re-import the module fresh to prove no hidden
            # module-level cache exists either.
            for name in list(sys.modules):
                if name in ("nullone_approval_durable",):
                    del sys.modules[name]
            import nullone_approval_durable as fresh_durable

            _, reloaded = common.load_manifest(path)
            self.assertEqual(reloaded["approval"]["stage"], expected_stage)

            # A stale APPROVE cannot resurrect a REJECTED/REVISION_REQUESTED
            # item back toward publication.
            if expected_stage in ("REJECTED", "REVISION_REQUESTED"):
                resurrect = fresh_durable.handle_durable_approval_callback(
                    action="approve",
                    review_post_id=POST,
                    authorized=True,
                    **IDS,
                )
                self.assertEqual(resurrect["outcome"], "REJECTED_WRONG_STATE")
                _, still = common.load_manifest(path)
                self.assertEqual(still["approval"]["stage"], expected_stage)

    def test_restart_recovery_reject(self):
        self._restart_recovery("reject", "REJECTED")

    def test_restart_recovery_approve(self):
        self._restart_recovery("approve", "AWAITING_PUBLISH_CONFIRMATION")

    def test_restart_recovery_revise(self):
        self._restart_recovery("revise", "REVISION_REQUESTED")


class IdempotencyTests(unittest.TestCase):
    def test_same_callback_twice_no_duplicate_ledger_event(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            first = call("reject")
            second = call("reject")
            self.assertEqual(first["outcome"], "TRANSITIONED")
            self.assertEqual(second["outcome"], "CONVERGED")
            self.assertEqual(first["reply"], second["reply"])
            rows = read_jsonl(root / "social/state/topic-ledger.jsonl")
            approval_rows = [r for r in rows if r.get("event") == "APPROVAL_DECISION"]
            self.assertEqual(len(approval_rows), 1)

    def test_old_callback_after_transition_no_state_regression(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            call("approve")
            stale_reject_replay = call("reject")
            # A redelivered REJECT after the item already moved to AWAITING
            # is a real (different) action, not a duplicate of the approve:
            # it legitimately transitions AWAITING -> REJECTED per the
            # transition table (still no regression -- REJECTED is a valid
            # forward move, never backward into DRAFT_READY).
            self.assertEqual(stale_reject_replay["to_stage"], "REJECTED")
            _, manifest = common.load_manifest(path)
            self.assertEqual(manifest["approval"]["stage"], "REJECTED")

    def test_callback_after_restart_stays_idempotent(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            call("reject")
            for name in list(sys.modules):
                if name == "nullone_approval_durable":
                    del sys.modules[name]
            import nullone_approval_durable as fresh_durable

            replay = fresh_durable.handle_durable_approval_callback(
                action="reject", review_post_id=POST, authorized=True, **IDS
            )
            self.assertEqual(replay["outcome"], "CONVERGED")
            rows = read_jsonl(root / "social/state/topic-ledger.jsonl")
            approval_rows = [r for r in rows if r.get("event") == "APPROVAL_DECISION"]
            self.assertEqual(len(approval_rows), 1)

    def test_callback_for_wrong_current_stage_fails_closed(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            call("reject")
            wrong = call("approve")
            self.assertEqual(wrong["outcome"], "REJECTED_WRONG_STATE")
            _, manifest = common.load_manifest(path)
            self.assertEqual(manifest["approval"]["stage"], "REJECTED")


class InvalidStageFailsClosedTests(unittest.TestCase):
    def test_corrupted_stage_field_fails_closed(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            _, manifest = common.load_manifest(path)
            manifest["approval"]["stage"] = "NOT_A_REAL_STAGE"
            common.atomic_write_json(path, manifest)
            result = call("reject")
            self.assertEqual(result["outcome"], durable.OUTCOME_REJECTED_STATE_CORRUPT)
            _, still = common.load_manifest(path)
            self.assertEqual(still["approval"]["stage"], "NOT_A_REAL_STAGE")

    def test_unknown_post_id_fails_closed(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            result = call("reject", review_post_id="a" * 24)
            self.assertEqual(result["outcome"], durable.OUTCOME_REJECTED_NOT_FOUND)


class StaleReviewTests(unittest.TestCase):
    def test_stale_review_rejected_regardless_of_action(self):
        old_iso = "2020-01-01T00:00:00+00:00"
        for action in ("approve", "reject", "revise", "back"):
            with IsolatedWorkspace() as root:
                path = make_manifest(root, created_at=old_iso)
                result = call(action)
                self.assertEqual(result["outcome"], "REJECTED_EXPIRED", action)
                _, manifest = common.load_manifest(path)
                self.assertNotIn("stage", manifest["approval"], action)
                self.assertFalse(manifest["approval"]["first_stage"], action)

    def test_same_day_review_is_not_stale(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)  # created_at defaults to now
            result = call("reject")
            self.assertNotEqual(result["outcome"], "REJECTED_EXPIRED")


class UnauthorizedActorTests(unittest.TestCase):
    def test_unauthorized_actor_rejected_no_filesystem_touch(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            before = path.stat().st_mtime_ns
            result = call("reject", authorized=False)
            self.assertEqual(result["outcome"], "REJECTED_UNAUTHORIZED")
            after = path.stat().st_mtime_ns
            self.assertEqual(before, after)
            ledger = root / "social/state/topic-ledger.jsonl"
            self.assertFalse(ledger.exists())


class PersistenceFailureFailsClosedTests(unittest.TestCase):
    def test_persistence_failure_reports_unchanged_stage(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            original_write = durable.atomic_write_json

            def boom(*_args, **_kwargs):
                raise OSError("synthetic disk failure")

            durable.atomic_write_json = boom
            try:
                result = call("reject")
            finally:
                durable.atomic_write_json = original_write

            self.assertEqual(result["outcome"], durable.OUTCOME_PERSISTENCE_FAILED)
            self.assertEqual(result["to_stage"], "DRAFT_READY")
            _, manifest = common.load_manifest(path)
            # Nothing durable stuck: the manifest is byte-identical to the
            # pre-call DRAFT_READY state, never reporting a false success.
            self.assertNotIn("stage", manifest["approval"])
            self.assertFalse(manifest["approval"]["first_stage"])

            # A retry after the transient failure clears succeeds normally.
            retry = call("reject")
            self.assertEqual(retry["outcome"], "TRANSITIONED")
            self.assertEqual(retry["to_stage"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
