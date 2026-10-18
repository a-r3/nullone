#!/usr/bin/env python3
"""Local-only Draft post-Zernio completion inspection / recovery.

Problem (proven from production artifacts, candidate
`microsoft-execution-containers-agent-containment-2026-10-14`): the Draft
cycle creates the Zernio review draft, flips the queue READY->DRAFTED,
sends the Telegram preview, and only THEN asks a model for the ledger
record and report. If anything after the Zernio side effect fails (that
run failed at LEDGER_WRITE), the valid remote draft is stranded: the
candidate is DRAFTED (never reselected), no ledger row, no report, and
-- before the delivery receipt added alongside this module -- no durable
proof of whether Telegram was ever delivered.

This module is deliberately NOT a re-run:

- NO network, NO subprocess, NO Zernio, NO Telegram, NO model call. It
  imports none of those surfaces and only reads/writes local files.
- It never moves queue state (DRAFTED stays DRAFTED), never rewrites a
  manifest, and never resends or recreates anything.
- `inspect` (default) is strictly read-only.
- `apply --manifest-id X` writes ONLY the two missing completion
  artifacts for exactly one manifest: one append-only ledger row and one
  report file, both built from manifest facts and the deterministic
  Telegram delivery receipt. Delivery is never claimed without that
  receipt: absent/`SENDING`/`NOTIFY_FAILED` all stay explicit.
- Idempotent: a ledger row for the same review post id, or an existing
  report, short-circuits that artifact.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

LEDGER_PATH = "social/state/topic-ledger.jsonl"
MANIFEST_DIR = "social/ops/manifests"
DRAFTS_DIR = "social/drafts/production"
PUBLISHER_DIR = "social/publisher"
DELIVERY_RECEIPT_SCHEMA = "nullone.draft-telegram-delivery.v1"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_SECRETISH = re.compile(r"(https?://|Bearer\s|sk-[A-Za-z0-9]|[?&]sig=)", re.I)


class RecoveryError(Exception):
    """Fail-closed recovery refusal (no partial writes)."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _ledger_rows(root: Path) -> list[dict[str, Any]]:
    path = root / LEDGER_PATH
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _has_completion_row(rows: list[dict[str, Any]], review_post_id: str) -> bool:
    return any(
        r.get("event") == "DRAFT_CREATED" and r.get("review_post_id") == review_post_id
        for r in rows
    )


def _delivery_state(root: Path, manifest: dict[str, Any]) -> tuple[str, str | None]:
    """(state, approval_message_id) from the deterministic receipt only."""
    candidate = manifest.get("candidate_id")
    manifest_id = manifest.get("manifest_id", "")
    if not isinstance(candidate, str) or not isinstance(manifest_id, str):
        return "NO_RECEIPT", None
    suffix = f"-{candidate}"
    if not manifest_id.endswith(suffix):
        return "NO_RECEIPT", None
    today = manifest_id[: -len(suffix)]
    path = root / DRAFTS_DIR / f"{today}-{candidate}-telegram-delivery.json"
    if not path.is_file() or path.is_symlink():
        return "NO_RECEIPT", None
    try:
        data = _read_json(path)
    except ValueError:
        return "NO_RECEIPT", None
    if (
        not isinstance(data, dict)
        or data.get("schema") != DELIVERY_RECEIPT_SCHEMA
        or data.get("candidate_id") != candidate
    ):
        return "NO_RECEIPT", None
    state = data.get("state")
    if state == "SENT":
        mid = data.get("approval_message_id")
        return "SENT", mid if isinstance(mid, str) and mid else None
    if state == "NOTIFY_FAILED":
        return "NOTIFY_FAILED", None
    if state == "SENDING":
        return "SENDING_UNCONFIRMED", None
    return "NO_RECEIPT", None


def report_path_for(root: Path, manifest_id: str) -> Path:
    return root / PUBLISHER_DIR / f"{manifest_id}-draft.md"


def inspect_manifest(root: Path, manifest_path: Path, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        m = _read_json(manifest_path)
    except ValueError:
        return None
    if not isinstance(m, dict):
        return None
    review = m.get("review") or {}
    if review.get("state") != "DRAFT_CREATED" or not review.get("zernio_draft_id"):
        return None
    if (m.get("publication") or {}).get("state") != "NOT_REQUESTED":
        return None
    if (m.get("approval") or {}).get("first_stage"):
        return None
    if m.get("format") not in {"FEED", "CAROUSEL"}:
        return None
    manifest_id = m.get("manifest_id")
    if not isinstance(manifest_id, str):
        return None
    review_post_id = review["zernio_draft_id"]
    has_row = _has_completion_row(rows, review_post_id)
    has_report = report_path_for(root, manifest_id).is_file() or any(
        r.get("review_post_id") == review_post_id and r.get("report") for r in rows
    )
    delivery, message_id = _delivery_state(root, m)
    if has_row and has_report:
        completion = "COMPLETE"
    elif has_row:
        completion = "MISSING_REPORT"
    elif has_report:
        completion = "MISSING_LEDGER"
    else:
        completion = "MISSING_LEDGER_AND_REPORT"
    return {
        "manifest_id": manifest_id,
        "candidate_id": m.get("candidate_id"),
        "review_post_id": review_post_id,
        "completion": completion,
        "telegram_delivery": delivery,
        "approval_message_id": message_id,
    }


def inspect_all(root: Path) -> list[dict[str, Any]]:
    rows = _ledger_rows(root)
    out = []
    for path in sorted((root / MANIFEST_DIR).glob("*.json")):
        item = inspect_manifest(root, path, rows)
        if item is not None:
            out.append(item)
    return out


def _clean(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value).split())[:limit]
    if _SECRETISH.search(text):
        raise RecoveryError("recovery field looks secret-bearing; refusing write")
    return text


def apply_one(root: Path, manifest_id: str) -> dict[str, Any]:
    """Write ONLY the missing ledger row / report for one manifest."""
    if not _SAFE_ID.match(manifest_id or ""):
        raise RecoveryError("manifest id invalid")
    manifest_path = root / MANIFEST_DIR / f"{manifest_id}.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RecoveryError("manifest not found")
    rows = _ledger_rows(root)
    item = inspect_manifest(root, manifest_path, rows)
    if item is None:
        raise RecoveryError("manifest is not an un-approved Draft in DRAFT_CREATED")
    m = _read_json(manifest_path)
    done: list[str] = []

    if item["completion"] in {"MISSING_LEDGER", "MISSING_LEDGER_AND_REPORT"}:
        row: dict[str, Any] = {
            "candidate_id": _clean(m["candidate_id"]),
            "content_type": _clean(m.get("content_type", "")),
            "event": "DRAFT_CREATED",
            "format": _clean(m.get("format", "")),
            "manifest_path": f"{MANIFEST_DIR}/{manifest_id}.json",
            "recovered_by": "nullone_draft_recovery",
            "result": "DRAFTED",
            "review_post_id": item["review_post_id"],
            "review_state": "DRAFT_CREATED",
            "telegram_delivery": item["telegram_delivery"],
            "timestamp": _clean((m.get("review") or {}).get("created_at", "")),
            "topic": _clean(m.get("topic", "")),
            "topic_cluster": _clean(m.get("topic_cluster", "")),
            "verification": _clean(m.get("verification", "")),
        }
        if item["telegram_delivery"] == "SENT":
            row["telegram_notified"] = True
            if item["approval_message_id"]:
                row["telegram_approval_message_id"] = item["approval_message_id"]
        ledger = root / LEDGER_PATH
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        done.append("LEDGER_ROW")

    if item["completion"] in {"MISSING_REPORT", "MISSING_LEDGER_AND_REPORT"}:
        report = report_path_for(root, manifest_id)
        if not report.exists():
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                "\n".join(
                    [
                        f"# Draft production report (recovered) - {manifest_id}",
                        "",
                        f"- candidate: {_clean(m['candidate_id'])}",
                        f"- review post id: {item['review_post_id']}",
                        "- review state: DRAFT_CREATED (queue: DRAFTED)",
                        f"- telegram delivery: {item['telegram_delivery']}",
                        "",
                        "Recovered deterministically from the manifest after the original",
                        "cycle failed past the Zernio side effect. No Zernio draft or",
                        "Telegram message was created or resent by recovery.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            done.append("REPORT")

    return {"manifest_id": manifest_id, "written": done, **item}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft post-Zernio completion recovery (local only)")
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[3]))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("inspect")
    ap = sub.add_parser("apply")
    ap.add_argument("--manifest-id", required=True)
    args = parser.parse_args(argv)
    root = Path(args.workspace).resolve()
    if args.command == "apply":
        try:
            result = apply_one(root, args.manifest_id)
        except RecoveryError as exc:
            print(f"DRAFT_RECOVERY=REFUSED reason={exc}")
            return 1
        print(f"DRAFT_RECOVERY=APPLIED written={','.join(result['written']) or 'NOTHING'}")
        return 0
    for item in inspect_all(root):
        if item["completion"] != "COMPLETE":
            print(
                f"{item['manifest_id']} completion={item['completion']} "
                f"telegram={item['telegram_delivery']}"
            )
    print("DRAFT_RECOVERY=INSPECT_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
