#!/usr/bin/env python3
"""Offline tests for the #89 authenticated plugin->controller IPC framing."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

import nullone_publish_ipc as ipc  # noqa: E402


def make_envelope(**overrides):
    base = {
        "schema": "nullone.publish-callback.v1",
        "post_id": "0123456789abcdef01234567",
        "account_id": "acct",
        "chat_id": "chat",
        "message_id": "mid",
        "sender_id": "sender",
        "nonce": "n" * 32,
    }
    base.update(overrides)
    return base


class FramingTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        key = ipc.generate_key()
        frame = ipc.encode_frame(make_envelope(), key)
        envelope, consumed = ipc.decode_frame(frame, key)
        self.assertEqual(consumed, len(frame))
        self.assertEqual(envelope["post_id"], "0123456789abcdef01234567")

    def test_canonical_bytes_deterministic(self) -> None:
        first = ipc.canonical_envelope_bytes(make_envelope())
        second = ipc.canonical_envelope_bytes(
            dict(reversed(list(make_envelope().items())))
        )
        self.assertEqual(first, second)
        self.assertNotIn(b" ", first)

    def test_wrong_key_rejected(self) -> None:
        frame = ipc.encode_frame(make_envelope(), ipc.generate_key())
        with self.assertRaises(ipc.AuthenticationError):
            ipc.decode_frame(frame, ipc.generate_key())

    def test_tampered_body_rejected(self) -> None:
        key = ipc.generate_key()
        frame = bytearray(ipc.encode_frame(make_envelope(), key))
        frame[20] ^= 0x01
        with self.assertRaises(ipc.AuthenticationError):
            ipc.decode_frame(bytes(frame), key)

    def test_missing_hmac_rejected(self) -> None:
        key = ipc.generate_key()
        frame = ipc.encode_frame(make_envelope(), key)
        with self.assertRaises(ipc.TruncatedFrameError):
            ipc.decode_frame(frame[:-1], key)

    def test_bad_magic_rejected(self) -> None:
        key = ipc.generate_key()
        frame = bytearray(ipc.encode_frame(make_envelope(), key))
        frame[0] = ord("X")
        with self.assertRaises(ipc.MalformedFrameError):
            ipc.decode_frame(bytes(frame), key)

    def test_bad_version_rejected(self) -> None:
        key = ipc.generate_key()
        frame = bytearray(ipc.encode_frame(make_envelope(), key))
        frame[3] = 0x7F
        with self.assertRaises(ipc.MalformedFrameError):
            ipc.decode_frame(bytes(frame), key)

    def test_oversize_declared_length_rejected_before_read(self) -> None:
        key = ipc.generate_key()
        header = ipc.MAGIC + bytes((ipc.VERSION,)) + ipc.LEN_STRUCT.pack(
            ipc.MAX_BODY_LEN + 1
        )
        with self.assertRaises(ipc.FrameTooLargeError):
            ipc.decode_frame(header, key)

    def test_truncated_body_rejected(self) -> None:
        key = ipc.generate_key()
        frame = ipc.encode_frame(make_envelope(), key)
        with self.assertRaises(ipc.TruncatedFrameError):
            ipc.decode_frame(frame[:10], key)

    def test_concatenated_frames(self) -> None:
        key = ipc.generate_key()
        first = ipc.encode_frame(make_envelope(), key)
        second = ipc.encode_frame(make_envelope(nonce="m" * 32), key)
        envelope, consumed = ipc.decode_frame(first + second, key)
        self.assertEqual(consumed, len(first))
        rest, _ = ipc.decode_frame((first + second)[consumed:], key)
        self.assertEqual(rest["nonce"], "m" * 32)

    def test_malformed_json_rejected(self) -> None:
        import hashlib
        import hmac as hmac_module
        import struct

        key = ipc.generate_key()
        body = b"{not json"
        header = ipc.MAGIC + bytes((ipc.VERSION,)) + struct.pack(">I", len(body))
        mac = hmac_module.new(key, header + body, hashlib.sha256).digest()
        with self.assertRaises(ipc.MalformedFrameError):
            ipc.decode_frame(header + body + mac, key)

    def test_unknown_schema_rejected(self) -> None:
        key = ipc.generate_key()
        with self.assertRaises(ipc.MalformedFrameError):
            ipc.encode_frame(make_envelope(schema="nope"), key)

    def test_unknown_field_rejected(self) -> None:
        key = ipc.generate_key()
        with self.assertRaises(ipc.MalformedFrameError):
            ipc.encode_frame(make_envelope(extra="x"), key)

    def test_missing_field_rejected(self) -> None:
        key = ipc.generate_key()
        envelope = make_envelope()
        del envelope["nonce"]
        with self.assertRaises(ipc.MalformedFrameError):
            ipc.encode_frame(envelope, key)

    def test_bad_post_id_rejected(self) -> None:
        key = ipc.generate_key()
        with self.assertRaises(ipc.MalformedFrameError):
            ipc.encode_frame(make_envelope(post_id="ZZZ"), key)

    def test_empty_string_field_rejected(self) -> None:
        key = ipc.generate_key()
        with self.assertRaises(ipc.MalformedFrameError):
            ipc.encode_frame(make_envelope(sender_id=""), key)

    def test_read_frame_stream(self) -> None:
        key = ipc.generate_key()
        frame = ipc.encode_frame(make_envelope(), key)
        envelope = ipc.read_frame(io.BytesIO(frame), key)
        self.assertEqual(envelope["post_id"], "0123456789abcdef01234567")

    def test_read_frame_eof_fails_closed(self) -> None:
        key = ipc.generate_key()
        with self.assertRaises(ipc.TruncatedFrameError):
            ipc.read_frame(io.BytesIO(b""), key)

    def test_key_lengths_enforced(self) -> None:
        with self.assertRaises(ipc.IpcError):
            ipc.encode_frame(make_envelope(), b"short")
        fingerprint = ipc.KeyFingerprint.of(ipc.generate_key())
        self.assertEqual(len(fingerprint.hex), 64)
        with self.assertRaises(ipc.IpcError):
            ipc.KeyFingerprint.of(b"short")


if __name__ == "__main__":
    unittest.main(verbosity=2)
