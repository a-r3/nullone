#!/usr/bin/env python3
"""Offline tests for the GET-only post-publish reconciliation path (#169).

All fake/tempdir: manifests in an isolated workspace, a fake read-only
provider (scripted `readback` responses + spy counters), no real
credential, no OpenClaw, Telegram, Zernio, or network.

The regression gate this file exists to prove: for EVERY reconciliation
case (published, still-publishing, failed, unknown/ambiguous, duplicate
invocation, invalid manifest, ineligible state), the fake provider's
put/promote_once counters stay at zero. If the reconciler's own logic
ever grew a code path to either, the fake raises immediately.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_bridge_common as common  # noqa: E402
import nullone_publish_receipt as receipts  # noqa: E402
import nullone_state as nullone_state  # noqa: E402
from nullone_bridge_common import BridgeError  # noqa: E402

POST_ID = "0123456789abcdef01234567"


def load_reconcile():
    spec = importlib.util.spec_from_file_location(
        "nullone_publish_reconcile_under_test",
        SCRIPTS / "nullone-publish-reconcile.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeReconcileProvider:
    """Read-only fake: scripted `readback` responses + hard spy counters.

    `put` and `promote_once` exist ONLY so a regression that mistakenly
    reaches either fails loudly and countably -- the real
    `ZernioPublishReadOnlyReconciler` has neither method at all.
    """

    def __init__(self, readback_responses=()):
        self.readback_calls: list = []
        self._readback = list(readback_responses)
        self.put_calls = 0
        self.promote_once_calls = 0

    def readback(self, post_id, expected):
        self.readback_calls.append((post_id, expected))
        if not self._readback:
            raise AssertionError(f"unexpected readback call for {post_id!r}")
        item = self._readback.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def put(self, *args, **kwargs):
        self.put_calls += 1
        raise AssertionError("reconciler must never PUT")

    def promote_once(self, *args, **kwargs):
        self.promote_once_calls += 1
        raise AssertionError("reconciler must never promote_once")


class IsolatedWorkspace:
    """Isolated WORKSPACE + MANIFEST_DIR + ledger/queue paths for one test.

    `nullone_state.py`'s PUBLISH_LEDGER/TOPIC_LEDGER/QUEUE are module-level
    constants resolved once at import time from `nullone_bridge_common.
    WORKSPACE`; mutating `common.WORKSPACE` afterward does NOT make them
    follow (unlike `record_approval_decision`, which re-resolves
    dynamically -- see that function's own docstring). This fixture
    patches all three explicitly so real ledger/queue writes land inside
    the isolated tmp dir instead of silently targeting whatever path was
    live at first import.
    """

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._old_workspace = common.WORKSPACE
        self._old_manifest_dir = common.MANIFEST_DIR
        self._old_publish_ledger = nullone_state.PUBLISH_LEDGER
        self._old_topic_ledger = nullone_state.TOPIC_LEDGER
        self._old_queue = nullone_state.QUEUE

    def __enter__(self):
        common.WORKSPACE = self.root
        common.MANIFEST_DIR = self.root / "social/ops/manifests"
        nullone_state.PUBLISH_LEDGER = self.root / "social/state/publish-ledger.jsonl"
        nullone_state.TOPIC_LEDGER = self.root / "social/state/topic-ledger.jsonl"
        nullone_state.QUEUE = self.root / "social/state/candidate-queue.md"
        return self.root

    def __exit__(self, *exc):
        common.WORKSPACE = self._old_workspace
        common.MANIFEST_DIR = self._old_manifest_dir
        nullone_state.PUBLISH_LEDGER = self._old_publish_ledger
        nullone_state.TOPIC_LEDGER = self._old_topic_ledger
        nullone_state.QUEUE = self._old_queue
        self._tmp.cleanup()


def make_manifest(
    root: Path,
    *,
    state: str,
    attempts: int,
    review_post_id: str = POST_ID,
    live_zernio_post_id: str | None = None,
    last_checked_at: str | None = None,
    manifest_id: str = "reconcile-fixture",
) -> tuple[Path, dict]:
    caption_path = root / "social/drafts/reconcile-caption.txt"
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_path.write_text("NullOne reconciliation fixture.\n", encoding="utf-8")
    media_path = root / "social/drafts/reconcile-0.png"
    Image.new("RGB", (1080, 1350), (10, 20, 30)).save(media_path, "PNG")
    media = common.inspect_media(media_path, "FEED")
    media["public_url"] = "https://example.invalid/reconcile-0.png"

    manifest = {
        "schema": common.SCHEMA,
        "manifest_id": manifest_id,
        "created_at": common.now_iso(),
        "candidate_id": f"candidate-{manifest_id}",
        "topic": "Reconciliation fixture topic",
        "topic_cluster": "reconcile-fixture-cluster",
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
            "zernio_draft_id": review_post_id,
            "created_at": common.now_iso(),
        },
        "approval": {
            "first_stage": True,
            "first_stage_at": common.now_iso(),
            "final_publish": True,
            "final_publish_at": common.now_iso(),
            "source": "texbrif-approval",
            "operator": "Rauf",
            "human_confirmation": "two_step",
        },
        "publication": {
            "attempts": attempts,
            "state": state,
            "live_zernio_post_id": live_zernio_post_id,
            "platform_post_id": None,
            "permalink": None,
            "last_checked_at": last_checked_at,
            "error": None,
        },
    }
    path = root / f"social/ops/manifests/{manifest_id}.json"
    common.validate_manifest(manifest)
    common.atomic_write_json(path, manifest)
    return path, manifest


def make_receipt(root: Path, review_post_id: str, *, state: str = "SETTLED_PUBLISHING"):
    instance_id = receipts.derive_authorization_instance_id(
        "acct", "chat", "msg-1", review_post_id
    )
    record, created = receipts.claim_receipt(
        root,
        review_post_id,
        instance_id,
        {"attempts": 1, "publication_state": "PUBLISHING", "final_publish": True},
        "a" * 64,
    )
    assert created
    receipts.transition_receipt(root, review_post_id, instance_id, "EXECUTING")
    receipts.transition_receipt(
        root, review_post_id, instance_id, state, {"outcome": "PUBLISHING", "code": 0}
    )
    return instance_id


PUBLISHED_TRUTH = {
    "live_status": "published",
    "platform_status": "published",
    "platform_post_url": None,
}
STILL_PUBLISHING_TRUTH = {
    "live_status": "scheduled",
    "platform_status": "",
    "platform_post_url": None,
}
FAILED_TRUTH = {
    "live_status": "failed",
    "platform_status": "",
    "platform_post_url": None,
}
UNKNOWN_TRUTH = {
    "live_status": "some_undocumented_value",
    "platform_status": "",
    "platform_post_url": None,
}


class ReconcileToPublishedTests(unittest.TestCase):
    def test_reconcile_publishing_to_published(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            instance_id = make_receipt(root, POST_ID)
            provider = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])

            result = reconcile.reconcile_one(
                POST_ID, provider_factory=lambda: provider
            )

            self.assertEqual(result["outcome"], "RECONCILED_PUBLISHED")
            self.assertEqual(provider.put_calls, 0)
            self.assertEqual(provider.promote_once_calls, 0)

            _p, current = common.load_manifest(path)
            self.assertEqual(current["publication"]["state"], "PUBLISHED")
            self.assertEqual(current["publication"]["attempts"], 1)
            self.assertEqual(
                current["publication"]["live_zernio_post_id"], POST_ID
            )
            self.assertIsNone(current["publication"]["platform_post_id"])

            rec = receipts.read_receipt(root, POST_ID, instance_id)
            self.assertEqual(rec["state"], "SETTLED_PUBLISHED")

            ledger_rows = nullone_state.read_jsonl(nullone_state.PUBLISH_LEDGER)
            published_rows = [r for r in ledger_rows if r.get("event") == "PUBLISHED"]
            self.assertEqual(len(published_rows), 1)

    def test_attempts_never_increment(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            provider = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])
            reconcile.reconcile_one(POST_ID, provider_factory=lambda: provider)
            _p, current = common.load_manifest(path)
            self.assertEqual(current["publication"]["attempts"], 1)


class ReconcileStillPendingTests(unittest.TestCase):
    def test_reconcile_still_publishing_noop(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            provider = FakeReconcileProvider(readback_responses=[STILL_PUBLISHING_TRUTH])

            result = reconcile.reconcile_one(
                POST_ID, provider_factory=lambda: provider
            )

            self.assertEqual(result["outcome"], "STILL_PENDING")
            self.assertEqual(provider.put_calls, 0)
            self.assertEqual(provider.promote_once_calls, 0)

            _p, current = common.load_manifest(path)
            self.assertEqual(current["publication"]["state"], "PUBLISHING")
            self.assertEqual(current["publication"]["attempts"], 1)

            ledger_rows = nullone_state.read_jsonl(nullone_state.PUBLISH_LEDGER)
            self.assertEqual(len(ledger_rows), 0)


class ReconcileFailedTests(unittest.TestCase):
    def test_reconcile_failed_safe(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            instance_id = make_receipt(root, POST_ID)
            provider = FakeReconcileProvider(readback_responses=[FAILED_TRUTH])

            result = reconcile.reconcile_one(
                POST_ID, provider_factory=lambda: provider
            )

            self.assertEqual(result["outcome"], "RECONCILED_FAILED")
            self.assertEqual(provider.put_calls, 0)
            self.assertEqual(provider.promote_once_calls, 0)

            _p, current = common.load_manifest(path)
            self.assertEqual(current["publication"]["state"], "FAILED")
            self.assertEqual(current["publication"]["attempts"], 1)
            self.assertIsNotNone(current["publication"]["error"])

            rec = receipts.read_receipt(root, POST_ID, instance_id)
            self.assertEqual(rec["state"], "SETTLED_FAILED")


class ReconcileUnknownTests(unittest.TestCase):
    def test_reconcile_unknown_fail_closed(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            provider = FakeReconcileProvider(readback_responses=[UNKNOWN_TRUTH])

            result = reconcile.reconcile_one(
                POST_ID, provider_factory=lambda: provider
            )

            self.assertEqual(result["outcome"], "CHECK_REQUIRED")
            self.assertEqual(provider.put_calls, 0)
            self.assertEqual(provider.promote_once_calls, 0)

            _p, current = common.load_manifest(path)
            self.assertEqual(current["publication"]["state"], "PUBLISHING")
            self.assertEqual(current["publication"]["attempts"], 1)

    def test_readback_exception_fails_closed(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            provider = FakeReconcileProvider(
                readback_responses=[RuntimeError("network blew up")]
            )

            result = reconcile.reconcile_one(
                POST_ID, provider_factory=lambda: provider
            )

            self.assertEqual(result["outcome"], "CHECK_REQUIRED")
            self.assertEqual(provider.put_calls, 0)
            self.assertEqual(provider.promote_once_calls, 0)

            _p, current = common.load_manifest(path)
            self.assertEqual(current["publication"]["state"], "PUBLISHING")
            self.assertEqual(current["publication"]["attempts"], 1)


class IdempotencyTests(unittest.TestCase):
    def test_idempotent_second_run(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            make_receipt(root, POST_ID)
            provider = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])

            first = reconcile.reconcile_one(POST_ID, provider_factory=lambda: provider)
            self.assertEqual(first["outcome"], "RECONCILED_PUBLISHED")

            state_after_first = json.loads(path.read_text(encoding="utf-8"))
            ledger_after_first = nullone_state.read_jsonl(nullone_state.PUBLISH_LEDGER)

            second = reconcile.reconcile_one(POST_ID, provider_factory=lambda: provider)
            self.assertEqual(second["outcome"], "NOOP_ALREADY_PUBLISHED")

            state_after_second = json.loads(path.read_text(encoding="utf-8"))
            ledger_after_second = nullone_state.read_jsonl(nullone_state.PUBLISH_LEDGER)

            self.assertEqual(state_after_first, state_after_second)
            self.assertEqual(ledger_after_first, ledger_after_second)
            # Second run must not touch Zernio at all -- not even a GET.
            self.assertEqual(len(provider.readback_calls), 1)
            self.assertEqual(provider.put_calls, 0)
            self.assertEqual(provider.promote_once_calls, 0)

    def test_no_duplicate_ledger_events(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            # Two independent providers, each scripted for one PUBLISHED
            # readback -- simulating two separate reconcile invocations
            # racing to finalize the same post, serialized by the lock.
            provider_a = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])
            provider_b = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])

            reconcile.reconcile_one(POST_ID, provider_factory=lambda: provider_a)
            # Manifest is now PUBLISHED; a second attempt must short-circuit
            # before ever calling provider_b's readback.
            reconcile.reconcile_one(POST_ID, provider_factory=lambda: provider_b)

            self.assertEqual(len(provider_b.readback_calls), 0)

            ledger_rows = nullone_state.read_jsonl(nullone_state.PUBLISH_LEDGER)
            published_rows = [r for r in ledger_rows if r.get("event") == "PUBLISHED"]
            self.assertEqual(len(published_rows), 1)

            topic_rows = nullone_state.read_jsonl(nullone_state.TOPIC_LEDGER)
            published_topic_rows = [
                r for r in topic_rows if r.get("status") == "PUBLISHED"
            ]
            self.assertEqual(len(published_topic_rows), 1)


class QueueAndReceiptTests(unittest.TestCase):
    def test_queue_marked_once(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            nullone_state.QUEUE.parent.mkdir(parents=True, exist_ok=True)
            nullone_state.QUEUE.write_text(
                "\n".join(
                    [
                        f"- **topic:** {m['topic']}",
                        "- **status:** DRAFT_CREATED",
                        "---",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            provider = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])

            reconcile.reconcile_one(POST_ID, provider_factory=lambda: provider)
            after_first = nullone_state.QUEUE.read_text(encoding="utf-8")
            self.assertIn("**status:** PUBLISHED", after_first)

            provider_2 = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])
            reconcile.reconcile_one(POST_ID, provider_factory=lambda: provider_2)
            after_second = nullone_state.QUEUE.read_text(encoding="utf-8")

            self.assertEqual(after_first, after_second)

    def test_receipt_converges(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            instance_id = make_receipt(root, POST_ID, state="SETTLED_PUBLISHING")
            provider = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])

            reconcile.reconcile_one(POST_ID, provider_factory=lambda: provider)

            rec = receipts.read_receipt(root, POST_ID, instance_id)
            self.assertEqual(rec["state"], "SETTLED_PUBLISHED")
            self.assertTrue(receipts.is_terminal(rec))

    def test_receipt_convergence_direct_transition_still_blocked(self):
        """`transition_receipt` itself must keep refusing SETTLED_PUBLISHING
        as a source for anything but the one narrow convergence path."""
        with IsolatedWorkspace() as root:
            instance_id = make_receipt(root, POST_ID, state="SETTLED_PUBLISHING")
            with self.assertRaises(BridgeError):
                receipts.transition_receipt(
                    root, POST_ID, instance_id, "SETTLED_PUBLISHED"
                )


class EligibilityGateTests(unittest.TestCase):
    def test_already_published_noop(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHED",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            provider = FakeReconcileProvider(readback_responses=[])

            result = reconcile.reconcile_one(
                POST_ID, provider_factory=lambda: provider
            )

            self.assertEqual(result["outcome"], "NOOP_ALREADY_PUBLISHED")
            self.assertEqual(len(provider.readback_calls), 0)
            self.assertEqual(provider.put_calls, 0)
            self.assertEqual(provider.promote_once_calls, 0)

    def test_attempts_zero_blocked(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="NOT_REQUESTED",
                attempts=0,
            )
            provider = FakeReconcileProvider(readback_responses=[])

            result = reconcile.reconcile_one(
                POST_ID, provider_factory=lambda: provider
            )

            self.assertEqual(result["outcome"], "BLOCKED")
            self.assertIn("ATTEMPTS", result["reason"])
            self.assertEqual(len(provider.readback_calls), 0)
            self.assertEqual(provider.put_calls, 0)
            self.assertEqual(provider.promote_once_calls, 0)

    def test_publish_in_flight_never_reconciled(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISH_IN_FLIGHT",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            provider = FakeReconcileProvider(readback_responses=[])

            result = reconcile.reconcile_one(
                POST_ID, provider_factory=lambda: provider
            )

            self.assertEqual(result["outcome"], "BLOCKED")
            self.assertIn("INELIGIBLE_STATE", result["reason"])
            self.assertEqual(len(provider.readback_calls), 0)

    def test_invalid_manifest_blocked(self):
        reconcile = load_reconcile()
        bad_post_id = "ffffffffffffffffffffffff"
        with IsolatedWorkspace() as root:
            common.MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
            bad_path = common.MANIFEST_DIR / "bad.json"
            # Matches by review_post_id (so the lookup succeeds) but fails
            # `validate_manifest`'s very first check (wrong schema) --
            # a genuine "found but invalid" case, not a "not found" one.
            bad_path.write_text(
                json.dumps(
                    {
                        "schema": "not-the-real-schema",
                        "review": {"zernio_draft_id": bad_post_id},
                    }
                ),
                encoding="utf-8",
            )
            provider = FakeReconcileProvider(readback_responses=[])

            result = reconcile.reconcile_one(
                bad_post_id, provider_factory=lambda: provider
            )

            self.assertEqual(result["outcome"], "BLOCKED")
            self.assertIn("INVALID_MANIFEST", result["reason"])
            self.assertEqual(len(provider.readback_calls), 0)


class LockUsageTests(unittest.TestCase):
    def test_lock_used_for_mutation(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                live_zernio_post_id=POST_ID,
                last_checked_at=common.now_iso(),
            )
            provider = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])

            calls = []
            real_lock = reconcile.review_post_lock

            def spy_lock(post_id):
                calls.append(post_id)
                return real_lock(post_id)

            with patch.object(reconcile, "review_post_lock", side_effect=spy_lock):
                reconcile.reconcile_one(POST_ID, provider_factory=lambda: provider)

            self.assertEqual(calls, [POST_ID])


class AmazonOfflineRegressionFixtureTests(unittest.TestCase):
    """Reproduces the real 2026-09-25-amazon-seller-assistant-claude
    instance in an isolated fixture -- no production files/state used as
    the test target, per issue #169 section 9."""

    AMAZON_POST_ID = "6ab5c068f1c2af7c30f89020"

    def test_amazon_fixture_reconciles_then_noops(self):
        reconcile = load_reconcile()
        with IsolatedWorkspace() as root:
            path, m = make_manifest(
                root,
                state="PUBLISHING",
                attempts=1,
                review_post_id=self.AMAZON_POST_ID,
                live_zernio_post_id=self.AMAZON_POST_ID,
                last_checked_at="2026-09-25T00:51:31.483642+00:00",
                manifest_id="2026-09-25-amazon-seller-assistant-claude",
            )
            instance_id = make_receipt(root, self.AMAZON_POST_ID)
            nullone_state.QUEUE.parent.mkdir(parents=True, exist_ok=True)
            nullone_state.QUEUE.write_text(
                "\n".join(
                    [
                        f"- **topic:** {m['topic']}",
                        "- **status:** AWAITING_PUBLISH_CONFIRMATION",
                        "---",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            # Provider fake: external status=published (matches the real
            # Zernio posts_get result confirmed during the forensic check).
            provider = FakeReconcileProvider(readback_responses=[PUBLISHED_TRUTH])

            first = reconcile.reconcile_one(
                self.AMAZON_POST_ID, provider_factory=lambda: provider
            )

            self.assertEqual(first["outcome"], "RECONCILED_PUBLISHED")
            self.assertEqual(first["final_state"], "PUBLISHED")
            self.assertEqual(provider.put_calls, 0)
            self.assertEqual(provider.promote_once_calls, 0)

            _p, current = common.load_manifest(path)
            self.assertEqual(current["publication"]["state"], "PUBLISHED")
            self.assertEqual(current["publication"]["attempts"], 1)

            publish_rows = nullone_state.read_jsonl(nullone_state.PUBLISH_LEDGER)
            published_rows = [
                r for r in publish_rows if r.get("event") == "PUBLISHED"
            ]
            self.assertEqual(len(published_rows), 1)

            topic_rows = nullone_state.read_jsonl(nullone_state.TOPIC_LEDGER)
            published_topic_rows = [
                r for r in topic_rows if r.get("status") == "PUBLISHED"
            ]
            self.assertEqual(len(published_topic_rows), 1)

            self.assertIn(
                "**status:** PUBLISHED",
                nullone_state.QUEUE.read_text(encoding="utf-8"),
            )

            rec = receipts.read_receipt(root, self.AMAZON_POST_ID, instance_id)
            self.assertEqual(rec["state"], "SETTLED_PUBLISHED")

            state_after_first = json.loads(path.read_text(encoding="utf-8"))
            publish_ledger_after_first = publish_rows
            topic_ledger_after_first = topic_rows
            queue_after_first = nullone_state.QUEUE.read_text(encoding="utf-8")

            # Second run: no provider call at all, byte-identical state.
            provider_2 = FakeReconcileProvider(readback_responses=[])
            second = reconcile.reconcile_one(
                self.AMAZON_POST_ID, provider_factory=lambda: provider_2
            )

            self.assertEqual(second["outcome"], "NOOP_ALREADY_PUBLISHED")
            self.assertEqual(len(provider_2.readback_calls), 0)
            self.assertEqual(provider_2.put_calls, 0)
            self.assertEqual(provider_2.promote_once_calls, 0)

            state_after_second = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state_after_first, state_after_second)
            self.assertEqual(
                nullone_state.read_jsonl(nullone_state.PUBLISH_LEDGER),
                publish_ledger_after_first,
            )
            self.assertEqual(
                nullone_state.read_jsonl(nullone_state.TOPIC_LEDGER),
                topic_ledger_after_first,
            )
            self.assertEqual(
                nullone_state.QUEUE.read_text(encoding="utf-8"), queue_after_first
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
