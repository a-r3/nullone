#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from nullone_bridge_common import (
    WORKSPACE,
    BridgeError,
    atomic_write_json,
    find_manifest_by_review_post_id,
    now_iso,
)


OWNER_ID_FILE = (
    WORKSPACE
    / "social/ops/private/telegram-owner-id"
)


def build_message(m: dict) -> str:
    pub = m["publication"]
    state = pub.get("state", "UNKNOWN")

    lines = [
        "NullOne publish nəticəsi",
        "",
        f"Mövzu: {m.get('topic', '')}",
        f"Status: {state}",
        f"Review ID: {m['review'].get('zernio_draft_id', '')}",
    ]

    live_id = pub.get("live_zernio_post_id")
    platform_id = pub.get("platform_post_id")
    permalink = pub.get("permalink")
    error = pub.get("error")

    if live_id:
        lines.append(f"Live Zernio ID: {live_id}")

    if platform_id:
        lines.append(f"Instagram post ID: {platform_id}")

    if permalink:
        lines.append(f"Link: {permalink}")

    if state == "PUBLISHED":
        lines.insert(0, "✅ NullOne paylaşımı yayımlandı.")
    elif state in {"PUBLISHING", "PUBLISH_ACCEPTED"}:
        lines.insert(0, "⏳ NullOne paylaşımı emal olunur.")
    elif state in {
        "UNKNOWN",
        "READBACK_FAILED",
        "CHECK_REQUIRED",
    }:
        lines.insert(
            0,
            "⚠️ NullOne paylaşımının statusu qeyri-müəyyəndir. "
            "Avtomatik təkrar edilməyəcək."
        )
    elif state == "FAILED":
        lines.insert(0, "❌ NullOne paylaşımı uğursuz oldu.")

    if error and state != "PUBLISHED":
        lines.append(f"Qeyd: {error}")

    return "\n".join(lines)


def notify(review_post_id: str) -> int:
    manifest_path, m = find_manifest_by_review_post_id(
        review_post_id
    )

    pub = m["publication"]

    if pub.get("telegram_notification_state") == "SENT":
        print("TELEGRAM_NOTIFICATION=ALREADY_SENT")
        return 0

    attempts = int(
        pub.get("telegram_notification_attempts", 0)
    )

    if attempts >= 1:
        raise BridgeError(
            "Telegram notification attempt already consumed; "
            "manual reconciliation required"
        )

    if not OWNER_ID_FILE.is_file():
        raise BridgeError(
            "Telegram owner ID file missing"
        )

    owner_id = OWNER_ID_FILE.read_text(
        encoding="utf-8"
    ).strip()

    if not owner_id:
        raise BridgeError(
            "Telegram owner ID is empty"
        )

    message = build_message(m)

    # Consume notification attempt BEFORE the outbound side effect.
    pub["telegram_notification_attempts"] = attempts + 1
    pub["telegram_notification_state"] = "SENDING"
    pub["telegram_notification_last_attempt_at"] = now_iso()

    atomic_write_json(
        manifest_path,
        m,
    )

    cmd = [
        "openclaw",
        "message",
        "send",
        "--channel",
        "telegram",
        "--account",
        "texbrif",
        "--target",
        owner_id,
        "--message",
        message,
        "--json",
    ]

    try:
        cp = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pub["telegram_notification_state"] = "UNKNOWN"
        atomic_write_json(manifest_path, m)

        raise BridgeError(
            "Telegram notification timed out; "
            "automatic resend forbidden"
        )

    if cp.returncode != 0:
        pub["telegram_notification_state"] = "FAILED"
        atomic_write_json(manifest_path, m)

        raise BridgeError(
            f"Telegram notification failed "
            f"(exit={cp.returncode})"
        )

    pub["telegram_notification_state"] = "SENT"
    pub["telegram_notified_at"] = now_iso()

    atomic_write_json(
        manifest_path,
        m,
    )

    print("TELEGRAM_NOTIFICATION=SENT")
    print(f"PUBLICATION_STATE={pub.get('state')}")
    print(
        "LIVE_ZERNIO_POST_ID="
        + str(pub.get("live_zernio_post_id") or "")
    )

    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("review_post_id")
    args = p.parse_args()

    try:
        return notify(args.review_post_id)
    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
