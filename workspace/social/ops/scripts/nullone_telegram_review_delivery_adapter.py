#!/usr/bin/env python3
"""Telegram/OpenClaw infrastructure adapter for shared ReviewDelivery (#62).

This is the only #62 module that knows the transport is
``openclaw message send``. Its CLI contract is pinned to OpenClaw v2026.8.2,
commit ``0965053fe6b9341776df147a6934b7485c60b5ca``:

* each exact local media asset is sent sequentially with ``--media``;
* one final approval card carries the producer-owned ``presentation`` via
  ``--presentation`` (the pinned command has no ``--buttons`` option);
* every send must return a non-empty top-level camelCase ``messageId``.

The logical delivery is ``SENT`` only after every ordered media send and the
single approval-card send have independent proof. There is no retry or cleanup
after a failure or ambiguous timeout.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from nullone_bridge_common import (
    BridgeError,
    WORKSPACE,
    resolve_workspace_path,
    sha256_file,
    workspace_relative,
)
from nullone_review_delivery import (
    STORY_PREVIEW_SCHEMA,
    ReviewDeliveryError,
    validate_preview_payload,
)

OWNER_ID_FILE = WORKSPACE / "social/ops/private/telegram-owner-id"

DEFAULT_ACCOUNT = "texbrif"
DEFAULT_TIMEOUT_SECONDS = 60

OPENCLAW_VERSION = "v2026.8.2"
OPENCLAW_COMMIT = "0965053fe6b9341776df147a6934b7485c60b5ca"

DELIVERY_STATUSES = frozenset(
    {
        "SENT",
        "FAILED",
        "TIMEOUT",
        "MALFORMED_RESPONSE",
        "OWNER_TARGET_MISSING",
        "INVALID_PAYLOAD",
        "MEDIA_INVALID",
    }
)


class TelegramReviewDeliveryAdapter:
    """OpenClaw implementation shared by Story and main preview pipelines."""

    def __init__(
        self,
        *,
        account: str = DEFAULT_ACCOUNT,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        owner_id_file: Path = OWNER_ID_FILE,
        runner=subprocess.run,
    ) -> None:
        self._account = account
        self._timeout_seconds = timeout_seconds
        self._owner_id_file = owner_id_file
        self._runner = runner

    def _read_owner_id(self) -> str:
        if not self._owner_id_file.is_file():
            raise ReviewDeliveryError("Telegram owner ID file missing")

        owner_id = self._owner_id_file.read_text(encoding="utf-8").strip()
        if not owner_id:
            raise ReviewDeliveryError("Telegram owner ID is empty")
        return owner_id

    @staticmethod
    def _media_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
        media = payload["media"]
        if payload["schema"] == STORY_PREVIEW_SCHEMA:
            return [media]
        return list(media)

    @staticmethod
    def _resolve_and_verify_media(payload: dict[str, Any]) -> list[Path]:
        """Resolve, contain, require regular files, and verify every digest.

        All entries are verified before the first transport call, so one bad
        Carousel item cannot cause an earlier item to be partially delivered.
        """

        paths: list[Path] = []
        for item in TelegramReviewDeliveryAdapter._media_entries(payload):
            try:
                path = resolve_workspace_path(item["local_path"])
                workspace_relative(path)
                if not path.is_file():
                    raise ReviewDeliveryError("preview media is not a regular file")
                if sha256_file(path) != item["sha256"]:
                    raise ReviewDeliveryError("preview media sha256 does not match")
            except (BridgeError, OSError, ReviewDeliveryError, TypeError, ValueError) as exc:
                raise ReviewDeliveryError("preview media failed integrity validation") from exc
            paths.append(path)
        return paths

    def _send_once(self, argv: list[str]) -> tuple[str | None, dict[str, Any] | None]:
        try:
            completed = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, {"status": "TIMEOUT", "error": "Telegram delivery timed out"}
        except OSError:
            return None, {
                "status": "FAILED",
                "error": "Telegram transport invocation failed",
            }

        if completed.returncode != 0:
            return None, {
                "status": "FAILED",
                "error": f"Telegram delivery exited {completed.returncode}",
            }

        try:
            response = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return None, {
                "status": "MALFORMED_RESPONSE",
                "error": "Telegram response was not valid JSON",
            }

        if not isinstance(response, dict):
            return None, {
                "status": "MALFORMED_RESPONSE",
                "error": "Telegram response was not a JSON object",
            }

        message_id = response.get("messageId")
        if not isinstance(message_id, str) or not message_id.strip():
            return None, {
                "status": "MALFORMED_RESPONSE",
                "error": "Telegram response missing messageId proof",
            }
        return message_id.strip(), None

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            validate_preview_payload(payload)
            presentation_json = json.dumps(
                payload["presentation"], ensure_ascii=False, separators=(",", ":")
            )
        except (ReviewDeliveryError, TypeError, ValueError):
            return {"status": "INVALID_PAYLOAD"}

        try:
            media_paths = self._resolve_and_verify_media(payload)
        except ReviewDeliveryError:
            return {"status": "MEDIA_INVALID"}

        try:
            owner_id = self._read_owner_id()
        except (OSError, UnicodeError, ReviewDeliveryError):
            return {"status": "OWNER_TARGET_MISSING"}

        base_argv = [
            "openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--account",
            self._account,
            "--target",
            owner_id,
        ]

        media_message_ids: list[str] = []
        for index, media_path in enumerate(media_paths):
            message_id, failure = self._send_once(
                [*base_argv, "--media", str(media_path), "--json"]
            )
            if failure is not None or message_id is None:
                return {
                    **(
                        failure
                        or {
                            "status": "MALFORMED_RESPONSE",
                            "error": "Telegram response missing messageId proof",
                        }
                    ),
                    "media_message_ids": media_message_ids,
                    "failed_step": "MEDIA",
                    "failed_media_index": index,
                }
            media_message_ids.append(message_id)

        approval_message_id, failure = self._send_once(
            [
                *base_argv,
                "--message",
                payload["text"],
                "--presentation",
                presentation_json,
                "--json",
            ]
        )
        if failure is not None or approval_message_id is None:
            return {
                **(
                    failure
                    or {
                        "status": "MALFORMED_RESPONSE",
                        "error": "Telegram response missing messageId proof",
                    }
                ),
                "media_message_ids": media_message_ids,
                "failed_step": "APPROVAL",
            }

        return {
            "status": "SENT",
            "media_message_ids": media_message_ids,
            "approval_message_id": approval_message_id,
        }


def self_test() -> int:
    """Offline smoke test; every transport call is an injected fake."""
    import hashlib
    import tempfile

    import nullone_bridge_common as bridge_common

    with tempfile.TemporaryDirectory() as td:
        original_workspace = bridge_common.WORKSPACE
        try:
            bridge_common.WORKSPACE = Path(td)
            media_path = Path(td) / "social/drafts/story.png"
            media_path.parent.mkdir(parents=True)
            media_bytes = b"review-media"
            media_path.write_bytes(media_bytes)
            owner_file = Path(td) / "telegram-owner-id"
            owner_file.write_text("123456789\n", encoding="utf-8")
            payload = {
                "schema": "nullone.story-preview.v1",
                "brand": "NullOne",
                "review_post_id": "review-1",
                "media": {
                    "local_path": "social/drafts/story.png",
                    "sha256": hashlib.sha256(media_bytes).hexdigest(),
                    "width": 1080,
                    "height": 1920,
                    "content_type": "image/png",
                },
                "text": "preview text",
                "presentation": {
                    "blocks": [
                        {
                            "type": "buttons",
                            "buttons": [
                                {
                                    "label": "Approve",
                                    "value": "texbrif:approve:review-1",
                                    "style": "success",
                                },
                                {"label": "Reject", "value": "texbrif:reject:review-1"},
                                {"label": "Revise", "value": "texbrif:revise:review-1"},
                            ],
                        }
                    ]
                },
            }
            calls: list[list[str]] = []

            def fake_runner(argv, **_kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "action": "send",
                            "channel": "telegram",
                            "dryRun": False,
                            "handledBy": "plugin",
                            "messageId": f"message-{len(calls)}",
                            "payload": {},
                        }
                    ),
                    stderr="",
                )

            result = TelegramReviewDeliveryAdapter(
                owner_id_file=owner_file, runner=fake_runner
            ).send(payload)
            assert result == {
                "status": "SENT",
                "media_message_ids": ["message-1"],
                "approval_message_id": "message-2",
            }
            assert "--media" in calls[0]
            assert "--presentation" in calls[1]
            assert all("--buttons" not in argv for argv in calls)
        finally:
            bridge_common.WORKSPACE = original_workspace

    print("TELEGRAM_REVIEW_DELIVERY_ADAPTER_SELF_TEST=PASS")
    print("OPENCLAW_CONTRACT=v2026.8.2")
    print("EXTERNAL_CALLS=0")
    return 0


def deliver(payload_path: str) -> int:
    """Deliver one validated preview payload file, then exit by status.

    The ONLY model-reachable delivery edge: accepts a payload file
    path and nothing else. The Telegram target is read privately by
    the adapter itself (`OWNER_ID_FILE`); no caller-supplied target,
    account, or message argv exists on this surface, so arbitrary
    delivery destinations are structurally impossible. Invalid
    payloads fail closed before any transport attempt; ambiguous
    sends are never retried (adapter contract above).
    """

    try:
        raw = Path(payload_path).read_text(encoding="utf-8")
    except OSError:
        print("DELIVERY_STATUS=PAYLOAD_UNREADABLE")
        return 1
    try:
        payload = json.loads(raw)
    except ValueError:
        print("DELIVERY_STATUS=INVALID_PAYLOAD")
        return 1

    result = TelegramReviewDeliveryAdapter().send(payload)
    status = result.get("status", "FAILED")
    print(f"DELIVERY_STATUS={status}")
    if status == "SENT":
        print(f"MEDIA_MESSAGE_IDS={len(result.get('media_message_ids', []))}")
        print(f"APPROVAL_MESSAGE_ID={result.get('approval_message_id')}")
        return 0
    print(f"DELIVERY_ERROR={result.get('error', status)}")
    return 1


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Telegram ReviewDelivery adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    deliver_parser = sub.add_parser("deliver")
    deliver_parser.add_argument("--payload-file", required=True)
    args = parser.parse_args()
    if args.command == "self-test":
        return self_test()
    if args.command == "deliver":
        return deliver(args.payload_file)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
