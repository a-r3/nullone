#!/usr/bin/env python3
"""Telegram/OpenClaw infrastructure adapter for the shared ReviewDelivery port (#62).

Infrastructure adapter, per
`docs/architecture/nullone-application-runtime.md`: this is the ONLY module
in the #62 review-delivery path allowed to know that the current transport
happens to be `openclaw message send`. `nullone_story_workflow.py` and
`nullone_review_delivery.py` never import this module's transport details;
they depend only on the `ReviewDelivery` port (structurally, `.send(payload)
-> dict`).

Reuses the existing owner-ID file convention and CLI invocation shape
already used by `nullone_failure_notify.OpenClawTelegramTransport` and
`nullone-publish-notify.py` (`social/ops/private/telegram-owner-id`,
`openclaw message send --channel telegram --account ... --target ...
--message ... --json`), extended with a `--buttons` argument carrying the
exact button list (label + legacy `texbrif:` callback value) from the
preview payload, unmodified. The owner target is read at call time, never
logged, never embedded in an exception message, and has no fallback value:
a missing or blank owner-ID file fails closed before any subprocess call.

Fail-closed delivery semantics (goal section 16):

- a subprocess timeout is TIMEOUT, never SENT (ambiguous transport outcome
  is never claimed as success);
- a non-zero exit is FAILED;
- a zero exit whose stdout does not parse as JSON, or parses but is
  missing a non-empty `message_id` proving message identity, is
  MALFORMED_RESPONSE -- a CLI-reported "success" is never trusted without
  the exact proof this adapter's own contract requires;
- only a zero exit with a well-formed JSON object carrying a truthy
  `message_id` is SENT.

No live `openclaw` invocation happens as part of building/testing #62;
every test injects a fake `subprocess.run`. Live behavior of this exact
CLI surface for structured button delivery is unproven (see
`docs/deployment/62-story-workflow-deployment.md`,
`LIVE_SCHEDULED_PATH_UNPROVEN`-equivalent note) -- this adapter's job is to
be correctly composed and fail closed, not to claim live proof it cannot
have offline.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from nullone_bridge_common import WORKSPACE
from nullone_review_delivery import ReviewDeliveryError, validate_preview_payload

OWNER_ID_FILE = WORKSPACE / "social/ops/private/telegram-owner-id"

DEFAULT_ACCOUNT = "texbrif"
DEFAULT_TIMEOUT_SECONDS = 60

DELIVERY_STATUSES = frozenset(
    {"SENT", "FAILED", "TIMEOUT", "MALFORMED_RESPONSE", "OWNER_TARGET_MISSING", "INVALID_PAYLOAD"}
)


class TelegramReviewDeliveryAdapter:
    """Real `ReviewDelivery` implementation: shells out to `openclaw message send`.

    Structurally satisfies `nullone_review_delivery.ReviewDelivery` (and,
    by the same structural shape, the pre-existing local
    `TelegramPreviewSender` protocols in `nullone_story_pipeline.py` /
    `nullone_main_draft_pipeline.py` -- no change to either file is
    required to accept an instance of this class as their
    `telegram_sender`).
    """

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

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Payload-shape errors (never a transport failure) still raise --
        # the caller (#33/#36 core) only ever calls this after building a
        # well-formed payload itself, so this is defense in depth, not the
        # expected path.
        validate_preview_payload(payload)

        try:
            owner_id = self._read_owner_id()
        except ReviewDeliveryError:
            # Never echo the owner-target value or its file path in the
            # returned result -- only a neutral status.
            return {"status": "OWNER_TARGET_MISSING"}

        buttons = self._extract_buttons(payload)

        argv = [
            "openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--account",
            self._account,
            "--target",
            owner_id,
            "--message",
            payload["text"],
            "--buttons",
            json.dumps(buttons, ensure_ascii=False, separators=(",", ":")),
            "--json",
        ]

        try:
            completed = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"status": "TIMEOUT", "error": "Telegram delivery timed out"}
        except OSError as exc:
            return {"status": "FAILED", "error": f"Telegram transport invocation failed: {exc}"}

        if completed.returncode != 0:
            return {
                "status": "FAILED",
                "error": f"Telegram delivery exited {completed.returncode}",
            }

        try:
            response = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return {"status": "MALFORMED_RESPONSE", "error": "Telegram response was not valid JSON"}

        if not isinstance(response, dict):
            return {"status": "MALFORMED_RESPONSE", "error": "Telegram response was not a JSON object"}

        message_id = response.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            return {
                "status": "MALFORMED_RESPONSE",
                "error": "Telegram response missing message_id proof",
            }

        return {"status": "SENT", "message_id": message_id}

    @staticmethod
    def _extract_buttons(payload: dict[str, Any]) -> list[dict[str, str]]:
        for block in payload["presentation"]["blocks"]:
            if isinstance(block, dict) and block.get("type") == "buttons":
                return [
                    {"label": button["label"], "value": button["value"]}
                    for button in block["buttons"]
                ]
        raise ReviewDeliveryError("preview payload has no buttons block")


def self_test() -> int:
    import tempfile

    story_payload = {
        "schema": "nullone.story-preview.v1",
        "brand": "NullOne",
        "review_post_id": "review-1",
        "text": "preview text",
        "presentation": {
            "blocks": [
                {
                    "type": "buttons",
                    "buttons": [
                        {"label": "Approve", "value": "texbrif:approve:review-1"},
                        {"label": "Reject", "value": "texbrif:reject:review-1"},
                    ],
                }
            ]
        },
    }

    with tempfile.TemporaryDirectory() as td:
        owner_file = Path(td) / "telegram-owner-id"

        # Missing owner target -- fails closed, no subprocess call.
        calls = []

        def unreachable_runner(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("subprocess must not be called without an owner target")

        adapter = TelegramReviewDeliveryAdapter(owner_id_file=owner_file, runner=unreachable_runner)
        result = adapter.send(story_payload)
        assert result == {"status": "OWNER_TARGET_MISSING"}, result
        assert calls == []

        owner_file.write_text("123456789\n", encoding="utf-8")

        def fake_runner_success(argv, **kwargs):
            assert "--buttons" in argv
            assert "123456789" in argv  # the owner target IS the intended CLI --target value
            buttons_index = argv.index("--buttons") + 1
            buttons = json.loads(argv[buttons_index])
            assert buttons == [
                {"label": "Approve", "value": "texbrif:approve:review-1"},
                {"label": "Reject", "value": "texbrif:reject:review-1"},
            ]
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"message_id": "tg-msg-1"}), stderr=""
            )

        adapter = TelegramReviewDeliveryAdapter(owner_id_file=owner_file, runner=fake_runner_success)
        result = adapter.send(story_payload)
        assert result == {"status": "SENT", "message_id": "tg-msg-1"}, result
        assert "123456789" not in json.dumps(result)  # owner target never echoed in the result

        def fake_runner_malformed(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")

        adapter = TelegramReviewDeliveryAdapter(owner_id_file=owner_file, runner=fake_runner_malformed)
        result = adapter.send(story_payload)
        assert result["status"] == "MALFORMED_RESPONSE", result

        def fake_runner_failed(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

        adapter = TelegramReviewDeliveryAdapter(owner_id_file=owner_file, runner=fake_runner_failed)
        result = adapter.send(story_payload)
        assert result["status"] == "FAILED", result

        def fake_runner_timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

        adapter = TelegramReviewDeliveryAdapter(owner_id_file=owner_file, runner=fake_runner_timeout)
        result = adapter.send(story_payload)
        assert result["status"] == "TIMEOUT", result

    print("TELEGRAM_REVIEW_DELIVERY_ADAPTER_SELF_TEST=PASS")
    print("EXTERNAL_CALLS=0")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Telegram ReviewDelivery adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
