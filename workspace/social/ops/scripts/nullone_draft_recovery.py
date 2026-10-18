#!/usr/bin/env python3
"""Local-only Draft post-Zernio completion inspection / recovery.

Problem (proven from production artifacts, candidate
`microsoft-execution-containers-agent-containment-2026-10-14`): the Draft
cycle creates the Zernio review draft, flips the queue READY->DRAFTED,
sends the Telegram preview, and only THEN asks a model for the ledger
record and report. If anything after the Zernio side effect fails (that
run failed at LEDGER_WRITE), the valid remote draft is stranded: the
candidate is DRAFTED (never reselected), no ledger row, no report, and
-- before the delivery receipt written by the provider -- no durable
proof of whether Telegram was ever delivered.

This module is deliberately NOT a re-run:

- NO network, NO subprocess, NO Zernio, NO Telegram, NO model call. It
  only reads/writes local files (queue parsing reuses the Draft queue
  authority `nullone_draft_candidate_queue`).
- It never moves queue state, never rewrites a manifest, never resends
  or recreates anything.
- `inspect` (default) is strictly read-only and reports inconsistencies.
- `apply --manifest-id X` writes ONLY the missing completion artifacts
  for exactly one manifest, and only after proving: the manifest is an
  un-approved DRAFT_CREATED Draft, the queue holds that candidate
  exactly once with status DRAFTED and matching identity, and the
  existing ledger carries no conflicting row.
- Delivery is claimed ONLY from a durable receipt that is bound to the
  manifest's review post id and carries a valid approval message id.
  Everything else stays explicitly unproven.

Ledger compatibility. The COMPLETE ledger record is model-authored and
intentionally flexible, so recovery relies on the stable identity the
current readers use (`nullone_breaking_identity._topic_ledger_matches`
links rows by candidate_id / manifest_id / review_post_id) and on the
real Draft row convention (event=DRAFT_CREATED, result=DRAFTED,
review_state=DRAFT_CREATED, manifest_path, report, timestamp, topic,
topic_cluster, content_type, format, verification). Minimum recovery
identity: candidate_id + manifest_id + review_post_id. Idempotency does
not depend on `event`: any row with a Draft-created marker (event /
state / review_state / status == DRAFT_CREATED, or result == DRAFTED)
and the same review_post_id AND candidate counts as the completion row;
any identity conflict fails closed instead of appending.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from nullone_bridge_common import BridgeError  # noqa: E402
from nullone_draft_candidate_queue import load_queue  # noqa: E402

LEDGER_PATH = "social/state/topic-ledger.jsonl"
MANIFEST_DIR = "social/ops/manifests"
DRAFTS_DIR = "social/drafts/production"
PUBLISHER_DIR = "social/publisher"
DELIVERY_RECEIPT_SCHEMA = "nullone.draft-telegram-delivery.v1"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_SECRETISH = re.compile(r"(https?://|Bearer\s|sk-[A-Za-z0-9]|[?&]sig=)", re.I)
_APPROVAL_MESSAGE_ID = re.compile(r"^[0-9A-Za-z_.:-]{1,64}$")
_MARKER_KEYS = ("event", "state", "review_state", "status")


class RecoveryError(Exception):
    """Fail-closed recovery refusal (no partial writes)."""


def valid_approval_message_id(value: object) -> bool:
    return isinstance(value, str) and bool(_APPROVAL_MESSAGE_ID.fullmatch(value))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _ledger_rows(root: Path) -> tuple[list[dict[str, Any]], bool]:
    """(rows, readable). Any unparseable/non-object line => not readable."""
    path = root / LEDGER_PATH
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows, True
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return rows, False
        if not isinstance(row, dict):
            return rows, False
        rows.append(row)
    return rows, True


def _is_draft_marker(row: dict[str, Any]) -> bool:
    return any(row.get(k) == "DRAFT_CREATED" for k in _MARKER_KEYS) or (
        row.get("result") == "DRAFTED"
    )


def ledger_identity(
    rows: list[dict[str, Any]], *, candidate_id: str, manifest_id: str, review_post_id: str
) -> str:
    """NONE | MATCH | CONFLICT for the existing Draft completion row.

    Strongest stable identity = review_post_id plus candidate_id (and
    manifest_id when the row carries one). `event` is not required.
    Any contradiction or malformed identifier fails closed (CONFLICT).
    """
    found = False
    for row in rows:
        rp, cid, mid = (row.get(k) for k in ("review_post_id", "candidate_id", "manifest_id"))
        for value in (rp, cid, mid):
            if value is not None and not isinstance(value, str):
                if rp == review_post_id or cid == candidate_id:
                    return "CONFLICT"
        if rp == review_post_id:
            if (cid is not None and cid != candidate_id) or (
                mid is not None and mid != manifest_id
            ):
                return "CONFLICT"
            if _is_draft_marker(row):
                found = True
        elif cid == candidate_id and _is_draft_marker(row):
            # Same candidate already completed under a different or
            # missing review identity: never append a second row.
            return "CONFLICT"
    return "MATCH" if found else "NONE"


def _delivery_state(root: Path, manifest: dict[str, Any]) -> tuple[str, str | None]:
    """(state, approval_message_id) from the deterministic receipt only.

    States: SENT (proven), DELIVERY_UNCONFIRMED (attempted, no proof),
    NO_RECEIPT, RECEIPT_INVALID (schema/binding/proof violation).
    """
    candidate = manifest.get("candidate_id")
    manifest_id = manifest.get("manifest_id", "")
    review_post_id = (manifest.get("review") or {}).get("zernio_draft_id")
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
        return "RECEIPT_INVALID", None
    if (
        not isinstance(data, dict)
        or data.get("schema") != DELIVERY_RECEIPT_SCHEMA
        or data.get("candidate_id") != candidate
        or not isinstance(review_post_id, str)
        or data.get("review_post_id") != review_post_id
    ):
        return "RECEIPT_INVALID", None
    state = data.get("state")
    if state == "SENT":
        mid = data.get("approval_message_id")
        if valid_approval_message_id(mid):
            return "SENT", mid
        return "RECEIPT_INVALID", None
    if state in {"SENDING", "DELIVERY_UNCONFIRMED"}:
        return "DELIVERY_UNCONFIRMED", None
    return "RECEIPT_INVALID", None


def queue_state(root: Path, manifest: dict[str, Any]) -> str:
    """QUEUE_DRAFTED or a QUEUE_* refusal reason (never raises)."""
    candidate = manifest.get("candidate_id")
    try:
        snapshot = load_queue(root)
    except BridgeError:
        return "QUEUE_UNREADABLE"
    matches = [e for e in snapshot.entries if e.candidate_id == candidate]
    if not matches:
        return "QUEUE_MISSING"
    if len(matches) != 1:
        return "QUEUE_DUPLICATED"
    entry = matches[0]
    if entry.status != "DRAFTED":
        return f"QUEUE_NOT_DRAFTED:{entry.status}"
    if entry.topic != manifest.get("topic") or entry.fields.get(
        "topic_cluster"
    ) != manifest.get("topic_cluster"):
        return "QUEUE_IDENTITY_MISMATCH"
    return "QUEUE_DRAFTED"


def report_path_for(root: Path, manifest_id: str) -> Path:
    return root / PUBLISHER_DIR / f"{manifest_id}-draft.md"


def report_state(root: Path, manifest: dict[str, Any], item: dict[str, Any]) -> str:
    """REPORT_ABSENT | REPORT_MATCH | REPORT_DIFFERS | REPORT_UNSAFE | REPORT_UNBUILDABLE.

    MATCH means the existing file is byte-identical to the exact
    deterministic recovered report. A differing historical/model-authored
    report is reported as DIFFERS and is never overwritten or repaired.
    """
    path = report_path_for(root, item["manifest_id"])
    try:
        expected = _report_text(manifest, item)
    except RecoveryError:
        return "REPORT_UNBUILDABLE"
    if path.is_symlink():
        return "REPORT_UNSAFE"
    if not path.exists():
        return "REPORT_ABSENT"
    if not path.is_file():
        return "REPORT_UNSAFE"
    try:
        current = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "REPORT_UNSAFE"
    return "REPORT_MATCH" if current == expected else "REPORT_DIFFERS"


def inspect_manifest(
    root: Path,
    manifest_path: Path,
    rows: list[dict[str, Any]],
    ledger_readable: bool,
) -> dict[str, Any] | None:
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
    manifest_id, candidate_id = m.get("manifest_id"), m.get("candidate_id")
    if not isinstance(manifest_id, str) or not isinstance(candidate_id, str):
        return None
    review_post_id = review["zernio_draft_id"]
    delivery, message_id = _delivery_state(root, m)
    queue = queue_state(root, m)
    base = {
        "manifest_id": manifest_id,
        "candidate_id": candidate_id,
        "review_post_id": review_post_id,
        "queue": queue,
        "telegram_delivery": delivery,
        "approval_message_id": message_id,
    }
    rstate = report_state(root, m, base)
    ident = "UNREADABLE"
    if not ledger_readable:
        completion = "LEDGER_UNREADABLE"
    else:
        ident = ledger_identity(
            rows,
            candidate_id=candidate_id,
            manifest_id=manifest_id,
            review_post_id=review_post_id,
        )
        if ident == "CONFLICT":
            completion = "LEDGER_CONFLICT"
        elif rstate in {"REPORT_DIFFERS", "REPORT_UNSAFE", "REPORT_UNBUILDABLE"}:
            completion = "REPORT_INCONSISTENT"
        elif ident == "MATCH" and rstate == "REPORT_MATCH":
            completion = "COMPLETE"
        elif ident == "MATCH":
            completion = "MISSING_REPORT"
        elif rstate == "REPORT_MATCH":
            completion = "MISSING_LEDGER"
        else:
            completion = "MISSING_LEDGER_AND_REPORT"
    return {**base, "completion": completion, "report": rstate, "ledger": ident}


def inspect_all(root: Path) -> list[dict[str, Any]]:
    rows, readable = _ledger_rows(root)
    out = []
    for path in sorted((root / MANIFEST_DIR).glob("*.json")):
        item = inspect_manifest(root, path, rows, readable)
        if item is not None:
            out.append(item)
    return out


def _clean(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value).split())[:limit]
    if _SECRETISH.search(text):
        raise RecoveryError("recovery field looks secret-bearing; refusing write")
    return text


def write_report_atomic(path: Path, content: str) -> str:
    """Crash-safe, never-overwrite report write.

    Returns WRITTEN or UNCHANGED (identical bytes already present).
    Raises RecoveryError if a DIFFERENT file exists, or the target is a
    symlink. The content is fsynced to a temp file in the same directory
    and published with an atomic no-replace hard link, so a crash can
    never leave a partial report under the final name.
    """
    if path.is_symlink():
        raise RecoveryError("report path is a symlink")
    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return "UNCHANGED"
        raise RecoveryError("report already exists with different content")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".draft-recovery-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") == content:
                return "UNCHANGED"
            raise RecoveryError("report already exists with different content")
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    return "WRITTEN"


def _append_ledger_row(root: Path, row: dict[str, Any]) -> None:
    ledger = root / LEDGER_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if ledger.is_file() and ledger.stat().st_size:
        with ledger.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                prefix = "\n"
    line = prefix + json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _report_text(m: dict[str, Any], item: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# Draft production report (recovered) - {item['manifest_id']}",
            "",
            f"- candidate: {_clean(m['candidate_id'])}",
            f"- review post id: {item['review_post_id']}",
            "- review state: DRAFT_CREATED",
            "- queue status: DRAFTED (verified)",
            f"- telegram delivery: {item['telegram_delivery']}",
            "",
            "Recovered deterministically from the manifest, the verified queue",
            "entry and the durable Telegram receipt after the original cycle",
            "failed past the Zernio side effect. No Zernio draft or Telegram",
            "message was created or resent by recovery.",
            "",
        ]
    )


def apply_one(root: Path, manifest_id: str) -> dict[str, Any]:
    """Write ONLY the missing ledger row / report for one manifest."""
    if not isinstance(manifest_id, str) or not _SAFE_ID.match(manifest_id):
        raise RecoveryError("manifest id invalid")
    manifest_path = root / MANIFEST_DIR / f"{manifest_id}.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RecoveryError("manifest not found")
    rows, readable = _ledger_rows(root)
    item = inspect_manifest(root, manifest_path, rows, readable)
    if item is None:
        raise RecoveryError("manifest is not an un-approved Draft in DRAFT_CREATED")
    if item["queue"] != "QUEUE_DRAFTED":
        raise RecoveryError(f"queue state refuses recovery: {item['queue']}")
    if item["completion"] in {"LEDGER_UNREADABLE", "LEDGER_CONFLICT"}:
        raise RecoveryError(f"ledger refuses recovery: {item['completion']}")
    # Report pre-flight: fail closed BEFORE any mutation (including the
    # ledger append) if an existing report is not the exact expected one.
    if item["report"] not in {"REPORT_ABSENT", "REPORT_MATCH"}:
        raise RecoveryError(f"report refuses recovery: {item['report']}")
    m = _read_json(manifest_path)
    report = report_path_for(root, manifest_id)
    written: list[str] = []

    # Same order as the provider: ledger first, then report.
    if item["completion"] in {"MISSING_LEDGER", "MISSING_LEDGER_AND_REPORT"}:
        row: dict[str, Any] = {
            "candidate_id": _clean(m["candidate_id"]),
            "content_type": _clean(m.get("content_type", "")),
            "event": "DRAFT_CREATED",
            "format": _clean(m.get("format", "")),
            "manifest_id": manifest_id,
            "manifest_path": f"{MANIFEST_DIR}/{manifest_id}.json",
            "recovered_by": "nullone_draft_recovery",
            "report": f"{PUBLISHER_DIR}/{manifest_id}-draft.md",
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
            row["telegram_approval_message_id"] = item["approval_message_id"]
        _append_ledger_row(root, row)
        written.append("LEDGER_ROW")

    if item["report"] == "REPORT_ABSENT":
        if write_report_atomic(report, _report_text(m, item)) == "WRITTEN":
            written.append("REPORT")

    return {"manifest_id": manifest_id, "written": written, **item}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Draft post-Zernio completion recovery (local only)"
    )
    parser.add_argument("--workspace", default=str(_HERE.parents[2]))
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
                f"report={item['report']} queue={item['queue']} "
                f"telegram={item['telegram_delivery']}"
            )
    print("DRAFT_RECOVERY=INSPECT_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
