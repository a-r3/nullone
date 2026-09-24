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


class LedgerOutboxRecoveryTests(unittest.TestCase):
    """Blocker 1 (PR #162 review): a ledger-append failure AFTER a
    successful manifest write used to be reported to the human as
    PERSISTENCE_FAILED ("nothing changed") while the manifest had actually
    already transitioned, and the audit row was permanently lost. Fixed via
    a durable pending_ledger_event outbox marker committed in the SAME
    atomic write as the stage transition."""

    def _break_ledger(self):
        original = durable.record_approval_decision

        def boom(*_args, **_kwargs):
            raise OSError("synthetic ledger disk failure")

        durable.record_approval_decision = boom
        return original

    def _restore_ledger(self, original):
        durable.record_approval_decision = original

    def test_MANIFEST_WRITE_SUCCESS_LEDGER_FAILURE_DOES_NOT_LIE_TO_HUMAN(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            original = self._break_ledger()
            try:
                result = call("reject")
            finally:
                self._restore_ledger(original)

            # The human must be told the truth: the transition happened.
            self.assertEqual(result["outcome"], "TRANSITIONED")
            self.assertEqual(result["to_stage"], "REJECTED")
            self.assertEqual(result["reply"]["text"], "❌ İmtina edildi. Heç nə yayımlanmadı.")
            _, manifest = common.load_manifest(path)
            self.assertEqual(manifest["approval"]["stage"], "REJECTED")

    def test_LEDGER_FAILURE_LEAVES_RECOVERABLE_DURABLE_MARKER(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            original = self._break_ledger()
            try:
                call("reject")
            finally:
                self._restore_ledger(original)

            _, manifest = common.load_manifest(path)
            pending = manifest["approval"].get("pending_ledger_event")
            self.assertIsNotNone(pending)
            self.assertEqual(pending["to_stage"], "REJECTED")
            self.assertEqual(pending["review_post_id"], POST)
            # And, matching the bug this fixes: the ledger itself is still
            # missing the row until recovery runs.
            rows = read_jsonl(root / "social/state/topic-ledger.jsonl")
            self.assertEqual(len([r for r in rows if r.get("event") == "APPROVAL_DECISION"]), 0)

    def test_RESTART_RECOVERS_PENDING_LEDGER_EVENT(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            original = self._break_ledger()
            try:
                call("reject")
            finally:
                self._restore_ledger(original)

            # "Restart": fresh module import, no in-memory state carried
            # over, then the explicit recovery sweep (also how a real
            # Gateway boot or periodic reconciliation job would call this).
            for name in list(sys.modules):
                if name == "nullone_approval_durable":
                    del sys.modules[name]
            import nullone_approval_durable as fresh_durable

            flushed = fresh_durable.recover_pending_ledger_events()
            self.assertIn(POST, flushed)

            _, manifest = common.load_manifest(path)
            self.assertIsNone(manifest["approval"].get("pending_ledger_event"))
            rows = read_jsonl(root / "social/state/topic-ledger.jsonl")
            approval_rows = [r for r in rows if r.get("event") == "APPROVAL_DECISION"]
            self.assertEqual(len(approval_rows), 1)
            self.assertEqual(approval_rows[0]["to_stage"], "REJECTED")

    def test_RESTART_RECOVERS_VIA_NEXT_OPPORTUNISTIC_ACCESS(self):
        """Recovery doesn't require the explicit sweep either: the very
        next callback for the same post opportunistically flushes it."""
        with IsolatedWorkspace() as root:
            make_manifest(root)
            original = self._break_ledger()
            try:
                call("reject")
            finally:
                self._restore_ledger(original)

            rows_before = read_jsonl(root / "social/state/topic-ledger.jsonl")
            self.assertEqual(len(rows_before), 0)

            # A later callback for the SAME (now-terminal) post -- e.g. a
            # replayed old button press -- opportunistically flushes the
            # pending event before doing anything else.
            replay = call("reject")
            self.assertEqual(replay["outcome"], "CONVERGED")
            rows_after = read_jsonl(root / "social/state/topic-ledger.jsonl")
            approval_rows = [r for r in rows_after if r.get("event") == "APPROVAL_DECISION"]
            self.assertEqual(len(approval_rows), 1)

    def test_RETRY_DOES_NOT_DUPLICATE_LEDGER_EVENT(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            original = self._break_ledger()
            try:
                call("reject")
            finally:
                self._restore_ledger(original)

            # Recover twice in a row (e.g. two overlapping sweeps, or a
            # sweep racing an opportunistic flush) -- idempotent either way.
            durable.recover_pending_ledger_events()
            durable.recover_pending_ledger_events()
            durable.recover_pending_ledger_events(POST)

            rows = read_jsonl(root / "social/state/topic-ledger.jsonl")
            approval_rows = [r for r in rows if r.get("event") == "APPROVAL_DECISION"]
            self.assertEqual(len(approval_rows), 1)

    def test_SUCCESS_CLEARS_PENDING_AUDIT_STATE(self):
        with IsolatedWorkspace() as root:
            path = make_manifest(root)
            # Normal path: ledger append succeeds first try.
            call("reject")
            _, manifest = common.load_manifest(path)
            self.assertIsNone(manifest["approval"].get("pending_ledger_event"))
            self.assertEqual(manifest["approval"]["stage"], "REJECTED")
            rows = read_jsonl(root / "social/state/topic-ledger.jsonl")
            self.assertEqual(len([r for r in rows if r.get("event") == "APPROVAL_DECISION"]), 1)

    def test_MANIFEST_FAILURE_DOES_NOT_APPEND_LEDGER(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            original_write = durable.atomic_write_json

            def boom(*_args, **_kwargs):
                raise OSError("synthetic disk failure")

            durable.atomic_write_json = boom
            try:
                result = call("reject")
            finally:
                durable.atomic_write_json = original_write

            self.assertEqual(result["outcome"], durable.OUTCOME_PERSISTENCE_FAILED)
            rows = read_jsonl(root / "social/state/topic-ledger.jsonl")
            self.assertEqual(len(rows), 0)

    def test_NO_FALSE_SUCCESS_IF_AUTHORITATIVE_MANIFEST_WRITE_FAILS(self):
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

            self.assertNotEqual(result["reply"]["text"], "❌ İmtina edildi. Heç nə yayımlanmadı.")
            self.assertEqual(result["to_stage"], "DRAFT_READY")
            _, manifest = common.load_manifest(path)
            self.assertNotIn("stage", manifest["approval"])


class LedgerSyncObservabilityTests(unittest.TestCase):
    """PR #162 review round 3: ledger_sync must be exposed on every
    result so operators can distinguish "decision persisted" from "audit
    projection pending" -- without ever changing the human-facing outcome."""

    def test_ledger_sync_flushed_on_normal_success(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            result = call("reject")
            self.assertEqual(result["outcome"], "TRANSITIONED")
            self.assertEqual(result["ledger_sync"], "flushed")

    def test_ledger_sync_pending_when_ledger_append_fails(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            original = durable.record_approval_decision

            def boom(*_args, **_kwargs):
                raise OSError("synthetic ledger disk failure")

            durable.record_approval_decision = boom
            try:
                result = call("reject")
            finally:
                durable.record_approval_decision = original

            # The human-facing result must be untouched: still the true
            # transition, still the true reply text.
            self.assertEqual(result["outcome"], "TRANSITIONED")
            self.assertEqual(result["to_stage"], "REJECTED")
            self.assertEqual(result["reply"]["text"], "❌ İmtina edildi. Heç nə yayımlanmadı.")
            # Only observability differs.
            self.assertEqual(result["ledger_sync"], "pending")

    def test_ledger_sync_flushed_after_recovery(self):
        with IsolatedWorkspace() as root:
            make_manifest(root)
            original = durable.record_approval_decision

            def boom(*_args, **_kwargs):
                raise OSError("synthetic ledger disk failure")

            durable.record_approval_decision = boom
            try:
                call("reject")
            finally:
                durable.record_approval_decision = original

            durable.recover_pending_ledger_events()
            # A later callback for the same post now reports flushed.
            replay = call("reject")
            self.assertEqual(replay["outcome"], "CONVERGED")
            self.assertEqual(replay["ledger_sync"], "flushed")


class AutomaticRecoveryCliTests(unittest.TestCase):
    """The exact CLI entrypoint plugins/nullone-final-publish/index.js
    invokes automatically on register()/plugin-reload
    (`nullone_approval_durable.py recover-pending`).

    Drives it through ``main()`` directly rather than a real subprocess:
    nullone_bridge_common.WORKSPACE is resolved once from `__file__` at
    import time (not NULLONE_WORKSPACE-aware), so a real subprocess here
    would scan this checkout's actual workspace/social/ops/manifests
    instead of the isolated fixture -- calling main() in-process keeps
    this test honestly isolated while still exercising the real CLI
    arg-parsing/dispatch/print-format code path index.js's subprocess
    call depends on (see test_plugin_index.js for the subprocess-spawn
    plumbing itself, faked there for the same reason).
    """

    def test_recover_pending_cli_flushes_and_reports_count(self):
        import contextlib
        import io

        with IsolatedWorkspace() as root:
            make_manifest(root)
            original = durable.record_approval_decision

            def boom(*_args, **_kwargs):
                raise OSError("synthetic ledger disk failure")

            durable.record_approval_decision = boom
            try:
                call("reject")
            finally:
                durable.record_approval_decision = original

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exit_code = durable.main(["recover-pending"])
            self.assertEqual(exit_code, 0)
            self.assertIn("RECOVERED_COUNT=1", out.getvalue())
            self.assertIn(f"RECOVERED_REVIEW_POST_ID={POST}", out.getvalue())


class LegacyManifestProtectionTests(unittest.TestCase):
    """Blocker 2 (PR #162 review): production has 29 manifests predating
    approval.stage, 9 of which are already published/attempted. Defaulting
    an absent approval.stage straight to DRAFT_READY let a stray callback
    on one of those old approval cards mutate an already-published
    manifest's approval state. Fixed by deriving a safe fallback from the
    EXISTING publication/approval fields instead."""

    def _legacy_manifest(self, root: Path, *, publication_overrides=None, approval_overrides=None):
        path = make_manifest(root)
        _, manifest = common.load_manifest(path)
        if publication_overrides:
            manifest["publication"].update(publication_overrides)
        if approval_overrides:
            manifest["approval"].update(approval_overrides)
        common.atomic_write_json(path, manifest)
        return path

    def test_LEGACY_PUBLISHED_FIRST_STAGE_CALLBACK_FAILS_CLOSED(self):
        with IsolatedWorkspace() as root:
            path = self._legacy_manifest(
                root,
                publication_overrides={"attempts": 1, "state": "PUBLISHED"},
                approval_overrides={"first_stage": True},
            )
            for action in ("approve", "reject", "revise", "back"):
                result = call(action)
                self.assertEqual(result["outcome"], durable.OUTCOME_REJECTED_LEGACY_UNSAFE, action)
            _, manifest = common.load_manifest(path)
            self.assertNotIn("stage", manifest["approval"])
            self.assertEqual(manifest["publication"]["state"], "PUBLISHED")

    def test_LEGACY_ATTEMPTED_FIRST_STAGE_CALLBACK_FAILS_CLOSED(self):
        # Attempted but NOT published (e.g. FAILED/UNKNOWN/CHECK_REQUIRED) --
        # attempts>=1 alone is enough, regardless of the exact terminal state.
        for state in ("FAILED", "UNKNOWN", "CHECK_REQUIRED", "PUBLISHING"):
            with IsolatedWorkspace() as root:
                self._legacy_manifest(
                    root,
                    publication_overrides={"attempts": 1, "state": state},
                    approval_overrides={"first_stage": True},
                )
                result = call("reject")
                self.assertEqual(
                    result["outcome"], durable.OUTCOME_REJECTED_LEGACY_UNSAFE, state
                )

    def test_LEGACY_PENDING_MANIFEST_READS_DRAFT_READY(self):
        with IsolatedWorkspace() as root:
            # Genuinely untouched legacy manifest: no approval.stage, never
            # attempted publication, no prior first-stage signal.
            self._legacy_manifest(root)
            result = call("reject")
            self.assertEqual(result["outcome"], "TRANSITIONED")
            self.assertEqual(result["to_stage"], "REJECTED")

    def test_LEGACY_APPROVED_FIELDS_DERIVE_CORRECT_STAGE_IF_SUPPORTED(self):
        # No legacy field distinguishes "approved" from "rejected" from
        # "revised" -- only the generic first_stage/final_publish booleans
        # exist pre-dating approval.stage. Inventing a specific derived
        # stage from an ambiguous signal would be guessing, which the
        # review explicitly ruled out ("fail closed rather than DRAFT_READY
        # ... do not invent new meanings"). Verify the fail-closed path
        # instead of a fabricated APPROVED-equivalent stage.
        with IsolatedWorkspace() as root:
            self._legacy_manifest(
                root,
                approval_overrides={"first_stage": True, "final_publish": True},
            )
            result = call("approve")
            self.assertEqual(result["outcome"], durable.OUTCOME_REJECTED_LEGACY_UNSAFE)

    def test_LEGACY_REJECTED_FIELDS_DERIVE_CORRECT_STAGE_IF_SUPPORTED(self):
        # Same reasoning as above for the reject direction: first_stage=True
        # with no publication activity and no stage field is ambiguous
        # (could have been approve, reject, or revise) -- fails closed
        # rather than guessing REJECTED.
        with IsolatedWorkspace() as root:
            self._legacy_manifest(root, approval_overrides={"first_stage": True})
            result = call("reject")
            self.assertEqual(result["outcome"], durable.OUTCOME_REJECTED_LEGACY_UNSAFE)

    def test_NO_TERMINAL_ITEM_CAN_BE_RESURRECTED_BY_STALE_CALLBACK(self):
        with IsolatedWorkspace() as root:
            path = self._legacy_manifest(
                root,
                publication_overrides={"attempts": 1, "state": "PUBLISHED"},
                approval_overrides={"first_stage": True},
            )
            before = path.stat().st_mtime_ns
            result = call("reject")
            self.assertEqual(result["outcome"], durable.OUTCOME_REJECTED_LEGACY_UNSAFE)
            after = path.stat().st_mtime_ns
            self.assertEqual(before, after, "a fail-closed legacy rejection must never write")
            rows = read_jsonl(root / "social/state/topic-ledger.jsonl")
            self.assertEqual(len(rows), 0)

    def test_real_production_manifests_are_correctly_classified(self):
        """Cross-checks the fix against the ACTUAL production manifest
        directory (read-only) -- the exact 9/20 split the PR #162 review
        found. Skips gracefully if that directory isn't present (e.g. CI)."""
        import json

        real_dir = Path("/home/oem/.openclaw/workspace/social/ops/manifests")
        if not real_dir.is_dir():
            self.skipTest("production manifest directory not present in this environment")
        safe = 0
        blocked = 0
        for p in sorted(real_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            approval = data.get("approval") or {}
            if "stage" in approval:
                continue
            try:
                durable.approval_stage(data)
                safe += 1
            except durable.LegacyStageUnsafeError:
                blocked += 1
            except common.BridgeError:
                blocked += 1
        # Not asserting exact counts (production data changes over time);
        # asserting the INVARIANT the fix must uphold: every already-
        # published/attempted legacy manifest is blocked, none silently
        # pass through as DRAFT_READY.
        for p in sorted(real_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            approval = data.get("approval") or {}
            if "stage" in approval:
                continue
            pub = data.get("publication") or {}
            attempts = pub.get("attempts", 0)
            pub_state = pub.get("state")
            already_published = (isinstance(attempts, int) and attempts >= 1) or (
                pub_state not in (None, "NOT_REQUESTED")
            )
            if already_published:
                with self.assertRaises(durable.LegacyStageUnsafeError, msg=p.name):
                    durable.approval_stage(data)


if __name__ == "__main__":
    unittest.main()
