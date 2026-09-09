#!/usr/bin/env python3
"""Authenticated plugin-to-controller IPC for the #89 deterministic handoff.

The OpenClaw plugin (Node, Gateway process) and the NullOne final-publish
controller daemon (Python) communicate over a private inherited pipe. Every
per-click message is an HMAC-SHA256 authenticated frame keyed by a per-boot
256-bit key K that lives ONLY in Gateway/daemon process memory:

- K is generated at plugin activation (`generate_key`), passed to the daemon
  ONCE over the private spawn pipe, and never persisted, logged, rendered,
  or placed in argv/env/files.
- The daemon verifies the HMAC with constant-time comparison BEFORE any
  receipt, authorization, manifest, or publication action.
- A binary started without the authenticated pipe (raw shell invocation,
  publisher/main LLM direct exec, fabricated tuples) cannot produce a valid
  frame and fails closed before anything consequential.

Frame wire format (all integers big-endian)::

    MAGIC(3) | VERSION(1) | BODY_LEN(4) | BODY | MAC(32)

BODY is canonical JSON (UTF-8, sorted keys, no whitespace) of the envelope.
MAC = HMAC-SHA256(K, MAGIC | VERSION | BODY_LEN | BODY).

Bounded, fully validated reads: wrong magic, unknown version, oversize
length, truncation, malformed JSON, schema violations, and MAC mismatch all
fail closed with typed errors and consume nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import struct
from dataclasses import dataclass
from typing import Any, BinaryIO, Mapping

MAGIC = b"NP1"
VERSION = 1
LEN_STRUCT = struct.Struct(">I")
MAC_LEN = 32
# Bodies are small callback envelopes; anything larger is hostile/malformed.
MAX_BODY_LEN = 8192
# Raw key material is exactly 32 bytes on the spawn pipe.
KEY_LEN = 32

ENVELOPE_SCHEMA = "nullone.publish-callback.v1"

# Exact envelope key set. Unknown/duplicate handling: JSON objects cannot
# carry duplicates after parsing; any key outside this set rejects the frame.
ENVELOPE_KEYS = frozenset(
    {
        "schema",
        "post_id",
        "account_id",
        "chat_id",
        "message_id",
        "sender_id",
        "nonce",
    }
)

# Field length bounds (raw Telegram-side strings; never persisted, never logged).
_MAX_ID_LEN = 128
_POST_ID_RE = frozenset("0123456789abcdefABCDEF")


class IpcError(RuntimeError):
    """Base fail-closed IPC error. Never carries key material or raw ids."""


class FrameTooLargeError(IpcError):
    """Declared body length exceeds the hard frame bound."""


class TruncatedFrameError(IpcError):
    """Stream ended before the declared frame completed."""


class MalformedFrameError(IpcError):
    """Bad magic/version/JSON/schema. Never echoes the offending bytes."""


class AuthenticationError(IpcError):
    """HMAC verification failed. No further detail is safe to report."""


def generate_key() -> bytes:
    """Generate one per-boot 256-bit channel key (memory-only by contract)."""
    return secrets.token_bytes(KEY_LEN)


def canonical_envelope_bytes(envelope: Mapping[str, Any]) -> bytes:
    """Deterministic canonical serialization shared with the Node plugin.

    Contract (must match route/plugin canonicalization exactly):
    UTF-8, keys sorted recursively, no whitespace. Both sides implement the
    same canonical form; any deviation fails HMAC on the other side.
    """
    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def validate_envelope_shape(envelope: Any) -> dict[str, Any]:
    """Strict structural validation. Raises MalformedFrameError on any deviation."""
    if not isinstance(envelope, dict):
        raise MalformedFrameError("envelope is not an object")
    keys = set(envelope.keys())
    if keys != set(ENVELOPE_KEYS):
        raise MalformedFrameError("envelope key set mismatch")
    if envelope.get("schema") != ENVELOPE_SCHEMA:
        raise MalformedFrameError("envelope schema mismatch")
    post_id = envelope.get("post_id")
    if (
        not isinstance(post_id, str)
        or len(post_id) != 24
        or any(c not in _POST_ID_RE for c in post_id)
    ):
        raise MalformedFrameError("post_id shape invalid")
    for field in ("account_id", "chat_id", "message_id", "sender_id", "nonce"):
        value = envelope.get(field)
        if not isinstance(value, str) or not value or len(value) > _MAX_ID_LEN:
            raise MalformedFrameError("envelope field shape invalid")
    return dict(envelope)


def encode_frame(envelope: Mapping[str, Any], key: bytes) -> bytes:
    """Build one authenticated frame. Key must be exactly KEY_LEN bytes."""
    if len(key) != KEY_LEN:
        raise IpcError("channel key length invalid")
    clean = validate_envelope_shape(dict(envelope))
    body = canonical_envelope_bytes(clean)
    if len(body) > MAX_BODY_LEN:
        raise FrameTooLargeError("envelope exceeds frame bound")
    header = MAGIC + bytes((VERSION,)) + LEN_STRUCT.pack(len(body))
    mac = hmac.new(key, header + body, hashlib.sha256).digest()
    return header + body + mac


def verify_frame(buffer: bytes, key: bytes) -> tuple[bytes, int]:
    """Verify magic/version/length/MAC of one frame at the head of buffer.

    Returns (body_bytes, bytes_consumed). Schema parsing is separate so the
    daemon's reply frames (different schema, same wire security) reuse this.
    """
    if len(key) != KEY_LEN:
        raise IpcError("channel key length invalid")
    prefix = len(MAGIC) + 1 + LEN_STRUCT.size
    if len(buffer) < prefix:
        raise TruncatedFrameError("frame header incomplete")
    if buffer[: len(MAGIC)] != MAGIC:
        raise MalformedFrameError("frame magic mismatch")
    if buffer[len(MAGIC)] != VERSION:
        raise MalformedFrameError("frame version unsupported")
    (body_len,) = LEN_STRUCT.unpack(buffer[len(MAGIC) + 1 : prefix])
    if body_len > MAX_BODY_LEN:
        raise FrameTooLargeError("frame length exceeds bound")
    total = prefix + body_len + MAC_LEN
    if len(buffer) < total:
        raise TruncatedFrameError("frame body incomplete")
    header = buffer[:prefix]
    body = buffer[prefix : prefix + body_len]
    mac = buffer[prefix + body_len : total]
    expected = hmac.new(key, header + body, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise AuthenticationError("frame authentication failed")
    return body, total


def decode_frame(buffer: bytes, key: bytes) -> tuple[dict[str, Any], int]:
    """Verify + parse one envelope frame at the head of buffer.

    Returns (envelope, bytes_consumed). Raises on any deviation. Excess
    trailing bytes are left for the caller (framing tests use this for
    concatenated-frame coverage).
    """
    body, total = verify_frame(buffer, key)
    try:
        envelope = json.loads(body.decode("utf-8"))
    except Exception:
        raise MalformedFrameError("frame body is not JSON")
    return validate_envelope_shape(envelope), total


def read_frame(stream: BinaryIO, key: bytes) -> dict[str, Any]:
    """Read exactly one complete authenticated frame from a blocking stream."""
    prefix = len(MAGIC) + 1 + LEN_STRUCT.size
    header = _read_exact(stream, prefix)
    if header[: len(MAGIC)] != MAGIC:
        raise MalformedFrameError("frame magic mismatch")
    if header[len(MAGIC)] != VERSION:
        raise MalformedFrameError("frame version unsupported")
    (body_len,) = LEN_STRUCT.unpack(header[len(MAGIC) + 1 :])
    if body_len > MAX_BODY_LEN:
        raise FrameTooLargeError("frame length exceeds bound")
    rest = _read_exact(stream, body_len + MAC_LEN)
    envelope, _consumed = decode_frame(header + rest, key)
    return envelope


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise TruncatedFrameError("stream ended mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@dataclass(frozen=True)
class KeyFingerprint:
    """Non-sensitive fingerprint of K for audit binding (SHA-256 hex)."""

    hex: str

    @staticmethod
    def of(key: bytes) -> "KeyFingerprint":
        if len(key) != KEY_LEN:
            raise IpcError("channel key length invalid")
        return KeyFingerprint(hashlib.sha256(b"nullone-ipc-key-fp-v1" + key).hexdigest())
