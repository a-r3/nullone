#!/usr/bin/env python3
"""Offline tests for #89 per-human-authorization-instance receipts."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_publish_receipt as receipts  # noqa: E402
from nullone_bridge_common import BridgeError  # noqa: E402

POST_ID = "0123456789ABCDEF01234567"
NONCE = "a" * 64


def fingerprint():
    return {"attempts": 0, "publication_state": "NOT_REQUESTED", "final_publish": False}


class IdentityTests(unittest.TestCase):
    def test_same_tuple_same_instance(self) -> None:
        first = receipts.derive_authorization_instance_id("a", "c", "m", POST_ID)
        second = receipts.derive_authorization_instance_id("a", "c", "m", POST_ID)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)

    def test_post_id_case_insensitive(self) -> None:
        first = receipts.derive_authorization_instance_id(
            "a", "c", "m", POST_ID.lower()
        )
        second = receipts.derive_authorization_instance_id("a", "c", "m", POST_ID)
        self.assertEqual(first, second)

    def test_new_message_new_instance(self) -> None:
        first = receipts.derive_authorization_instance_id("a", "c", "m1", POST_ID)
        second = receipts.derive_authorization_instance_id("a", "c", "m2", POST_ID)
        self.assertNotEqual(first, second)

    def test_different_post_new_instance(self) -> None:
        first = receipts.derive_authorization_instance_id(
            "a", "c", "m", POST_ID
        )
        second = receipts.derive_authorization_instance_id(
            "a", "c", "m", "fedcba987654321001234567"
        )
        self.assertNotEqual(first, second)

    def test_malformed_post_rejected(self) -> None:
        with self.assertRaises(BridgeError):
            receipts.derive_authorization_instance_id("a", "c", "m", "zzz")

    def test_empty_tuple_field_rejected(self) -> None:
        with self.assertRaises(BridgeError):
            receipts.derive_authorization_instance_id("", "c", "m", POST_ID)

    def test_no_raw_ids_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = receipts.derive_authorization_instance_id(
                "secret-account", "secret-chat", "secret-message", POST_ID
            )
            receipts.claim_receipt(root, POST_ID, instance, fingerprint(), NONCE)
            blob = ""
            for path in (root / "social/ops/publish-callback-receipts").rglob("*"):
                if path.is_file():
                    blob += path.read_text(encoding="utf-8")
            for secret in ("secret-account", "secret-chat", "secret-message"):
                self.assertNotIn(secret, blob)


class ClaimTests(unittest.TestCase):
    def test_claim_then_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = receipts.derive_authorization_instance_id(
                "a", "c", "m", POST_ID
            )
            record, created = receipts.claim_receipt(
                root, POST_ID, instance, fingerprint(), NONCE
            )
            self.assertTrue(created)
            self.assertEqual(record["state"], "RECEIVED")
            same, created_again = receipts.claim_receipt(
                root, POST_ID, instance, fingerprint(), NONCE
            )
            self.assertFalse(created_again)
            self.assertEqual(same["state"], "RECEIVED")

    def test_transition_and_terminal_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = receipts.derive_authorization_instance_id(
                "a", "c", "m", POST_ID
            )
            receipts.claim_receipt(root, POST_ID, instance, fingerprint(), NONCE)
            record = receipts.transition_receipt(
                root, POST_ID, instance, "EXECUTING", {"outcome": "started"}
            )
            self.assertEqual(record["state"], "EXECUTING")
            record = receipts.transition_receipt(
                root, POST_ID, instance, "SETTLED_BLOCKED", {"outcome": "BLOCKED"}
            )
            self.assertTrue(receipts.is_terminal(record))
            with self.assertRaises(BridgeError):
                receipts.transition_receipt(
                    root, POST_ID, instance, "EXECUTING", {}
                )

    def test_unknown_state_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = receipts.derive_authorization_instance_id(
                "a", "c", "m", POST_ID
            )
            receipts.claim_receipt(root, POST_ID, instance, fingerprint(), NONCE)
            with self.assertRaises(BridgeError):
                receipts.transition_receipt(root, POST_ID, instance, "NOPE", {})

    def test_receipt_file_mode_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = receipts.derive_authorization_instance_id(
                "a", "c", "m", POST_ID
            )
            path = receipts.receipt_path(root, POST_ID, instance)
            receipts.claim_receipt(root, POST_ID, instance, fingerprint(), NONCE)
            mode = oct(os.stat(path).st_mode & 0o777)
            self.assertEqual(mode, "0o600")

    def test_symlinked_post_dir_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = receipts.receipts_root(root)
            base.mkdir(parents=True)
            target = root / "elsewhere"
            target.mkdir()
            os.symlink(target, base / POST_ID.lower())
            instance = receipts.derive_authorization_instance_id(
                "a", "c", "m", POST_ID
            )
            with self.assertRaises(BridgeError):
                receipts.claim_receipt(root, POST_ID, instance, fingerprint(), NONCE)

    def test_corrupt_receipt_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = receipts.derive_authorization_instance_id(
                "a", "c", "m", POST_ID
            )
            receipts.claim_receipt(root, POST_ID, instance, fingerprint(), NONCE)
            path = receipts.receipt_path(root, POST_ID, instance)
            blob = json.loads(path.read_text(encoding="utf-8"))
            blob["state"] = "SOMETHING_ELSE"
            path.write_text(json.dumps(blob), encoding="utf-8")
            with self.assertRaises(BridgeError):
                receipts.read_receipt(root, POST_ID, instance)

    def test_scan_lists_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(receipts.scan_receipts(root), [])
            first = receipts.derive_authorization_instance_id("a", "c", "m1", POST_ID)
            second = receipts.derive_authorization_instance_id("a", "c", "m2", POST_ID)
            receipts.claim_receipt(root, POST_ID, first, fingerprint(), NONCE)
            receipts.claim_receipt(root, POST_ID, second, fingerprint(), NONCE)
            self.assertEqual(len(receipts.scan_receipts(root)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
