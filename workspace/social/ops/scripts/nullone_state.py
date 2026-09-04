#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from nullone_bridge_common import (
    WORKSPACE,
    atomic_write_json,
    now_iso,
)

PUBLISH_LEDGER = WORKSPACE / "social/state/publish-ledger.jsonl"
TOPIC_LEDGER = WORKSPACE / "social/state/topic-ledger.jsonl"
QUEUE = WORKSPACE / "social/state/candidate-queue.md"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []

    if not path.exists():
        return out

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            obj = json.loads(line)
        except Exception:
            continue

        if isinstance(obj, dict):
            out.append(obj)

    return out


def append_jsonl_once(
    path: Path,
    record: dict[str, Any],
    unique: tuple[str, ...],
) -> bool:

    existing = read_jsonl(path)

    for row in existing:
        if all(
            row.get(k) == record.get(k)
            for k in unique
        ):
            return False

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )

    return True


def mark_queue_published_exact(
    *,
    topic: str,
    review_post_id: str | None,
    live_post_id: str | None,
    platform_post_id: str | None,
    permalink: str | None,
) -> bool:

    if not QUEUE.exists():
        return False

    lines = QUEUE.read_text(encoding="utf-8").splitlines()

    target = f"- **topic:** {topic}"
    changed = False

    i = 0

    while i < len(lines):
        if lines[i].strip() != target:
            i += 1
            continue

        j = i + 1

        while j < len(lines):
            if (
                lines[j].startswith("- **topic:**")
                or lines[j].startswith("### ")
                or lines[j].startswith("## ")
                or lines[j].strip() == "---"
            ):
                break
            j += 1

        status_found = False

        for k in range(i + 1, j):
            if lines[k].startswith("- **status:**"):
                lines[k] = "- **status:** PUBLISHED"
                status_found = True
                changed = True
                insert_at = k + 1
                break
        else:
            insert_at = i + 1

        additions = []

        block = "\n".join(lines[i:j])

        if review_post_id and "**review post ID:**" not in block:
            additions.append(
                f"- **review post ID:** {review_post_id}"
            )

        if live_post_id and "**live Zernio post ID:**" not in block:
            additions.append(
                f"- **live Zernio post ID:** {live_post_id}"
            )

        if platform_post_id and "**platform post ID:**" not in block:
            additions.append(
                f"- **platform post ID:** {platform_post_id}"
            )

        if permalink and "**permalink:**" not in block:
            additions.append(
                f"- **permalink:** {permalink}"
            )

        if additions:
            lines[insert_at:insert_at] = additions
            changed = True
            i += len(additions)
            j += len(additions)

        i = j

    if changed:
        QUEUE.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    return changed


def record_publication_event(
    manifest: dict[str, Any],
    event: str,
) -> None:

    pub = manifest["publication"]
    review = manifest["review"]

    record = {
        "timestamp": now_iso(),
        "event": event,
        "manifest_id": manifest.get("manifest_id"),
        "candidate_id": manifest.get("candidate_id"),
        "topic": manifest.get("topic"),
        "topic_cluster": manifest.get("topic_cluster"),
        "content_type": manifest.get("content_type"),
        "format": manifest.get("format"),
        "account_id": manifest.get("account_id"),
        "review_post_id": review.get("zernio_draft_id"),
        "live_zernio_post_id": pub.get("live_zernio_post_id"),
        "platform_post_id": pub.get("platform_post_id"),
        "permalink": pub.get("permalink"),
        "result": event,
    }

    append_jsonl_once(
        PUBLISH_LEDGER,
        record,
        ("event", "live_zernio_post_id"),
    )

    topic_record = {
        "timestamp": record["timestamp"],
        "topic": record["topic"],
        "topic_cluster": record["topic_cluster"],
        "status": event,
        "review_post_id": record["review_post_id"],
        "live_zernio_post_id": record["live_zernio_post_id"],
        "manifest_id": record["manifest_id"],
    }

    append_jsonl_once(
        TOPIC_LEDGER,
        topic_record,
        ("status", "live_zernio_post_id"),
    )


def reconcile(args: argparse.Namespace) -> int:

    timestamp = now_iso()

    publish = {
        "timestamp": timestamp,
        "event": "PUBLICATION_RECONCILED",
        "published_date": args.published_date,
        "topic": args.topic,
        "topic_cluster": args.topic_cluster,
        "content_type": args.content_type,
        "format": args.format,
        "account_id": args.account_id,
        "review_post_id": args.review_post_id,
        "live_zernio_post_id": args.live_post_id,
        "platform_post_id": args.platform_post_id,
        "permalink": args.permalink,
        "result": "PUBLISHED",
        "platform_status": "published",
        "source": "verified_pre_bridge_publication_reconciliation",
    }

    added_publish = append_jsonl_once(
        PUBLISH_LEDGER,
        publish,
        ("event", "live_zernio_post_id"),
    )

    topic = {
        "timestamp": timestamp,
        "topic": args.topic,
        "topic_cluster": args.topic_cluster,
        "status": "PUBLISHED",
        "review_post_id": args.review_post_id,
        "live_zernio_post_id": args.live_post_id,
        "platform_post_id": args.platform_post_id,
        "permalink": args.permalink,
        "source": "publication_reconciliation",
    }

    added_topic = append_jsonl_once(
        TOPIC_LEDGER,
        topic,
        ("status", "live_zernio_post_id"),
    )

    changed_queue = mark_queue_published_exact(
        topic=args.topic,
        review_post_id=args.review_post_id,
        live_post_id=args.live_post_id,
        platform_post_id=args.platform_post_id,
        permalink=args.permalink,
    )

    print(
        "PUBLISH_LEDGER="
        + ("APPENDED" if added_publish else "ALREADY_PRESENT")
    )

    print(
        "TOPIC_LEDGER="
        + ("APPENDED" if added_topic else "ALREADY_PRESENT")
    )

    print(
        "QUEUE="
        + ("UPDATED" if changed_queue else "NO_EXACT_MATCH")
    )

    print("RECONCILIATION=PASS")
    return 0


def main() -> int:

    p = argparse.ArgumentParser()

    sub = p.add_subparsers(
        dest="command",
        required=True,
    )

    r = sub.add_parser("reconcile")

    r.add_argument("--topic", required=True)
    r.add_argument("--topic-cluster", required=True)
    r.add_argument("--content-type", required=True)
    r.add_argument("--format", required=True)
    r.add_argument("--account-id", required=True)
    r.add_argument("--review-post-id", required=True)
    r.add_argument("--live-post-id", required=True)
    r.add_argument("--platform-post-id", required=True)
    r.add_argument("--permalink", required=True)
    r.add_argument("--published-date", required=True)

    args = p.parse_args()

    if args.command == "reconcile":
        return reconcile(args)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
