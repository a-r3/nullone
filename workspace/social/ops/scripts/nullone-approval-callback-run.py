#!/usr/bin/env python3
"""CLI entrypoint: one durable first-stage Telegram approval callback.

Reads exactly one JSON request object from stdin and writes exactly one
JSON result object to stdout. Designed to be spawned fresh per callback by
the NullOne final-publish plugin (Node) -- first-stage callbacks are human
button clicks, not a hot path, so a short-lived subprocess per callback
keeps this layer simple and avoids a second long-lived daemon/protocol next
to the existing second-stage publish daemon.

Request shape (all fields required; unexpected/missing shape exits 2):

    {
      "action": "approve" | "reject" | "revise" | "back",
      "review_post_id": "<24-hex>",
      "authorized": true | false,
      "message_id": <int|string>,
      "chat_id": <string>,
      "account_id": <string>,
      "sender_id": <string>
    }

Exit code 0 with a JSON result on stdout covers EVERY expected business
outcome, including rejections (unauthorized, malformed, wrong-state,
expired, not-found, persistence-failed) -- those are correct answers, not
process failures. A non-zero exit means this script itself broke (bad
input shape, internal exception) and the caller must NOT interpret stdout
as a valid result.
"""

from __future__ import annotations

import json
import sys

from nullone_approval_durable import handle_durable_approval_callback


def main() -> int:
    try:
        raw = sys.stdin.read()
        request = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - malformed input from the caller
        print(json.dumps({"error": f"invalid request JSON: {exc}"}))
        return 2

    if not isinstance(request, dict):
        print(json.dumps({"error": "request must be a JSON object"}))
        return 2

    required = (
        "action",
        "review_post_id",
        "authorized",
        "message_id",
        "chat_id",
        "account_id",
        "sender_id",
    )
    missing = [k for k in required if k not in request]
    if missing:
        print(json.dumps({"error": f"missing fields: {missing}"}))
        return 2

    try:
        result = handle_durable_approval_callback(
            action=request["action"],
            review_post_id=request["review_post_id"],
            authorized=request["authorized"],
            message_id=request["message_id"],
            chat_id=request["chat_id"],
            account_id=request["account_id"],
            sender_id=request["sender_id"],
        )
    except Exception as exc:  # noqa: BLE001 - internal failure, never silent
        print(json.dumps({"error": f"internal failure: {exc}"}))
        return 2

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
