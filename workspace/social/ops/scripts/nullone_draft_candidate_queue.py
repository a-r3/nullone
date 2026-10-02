"""Draft-only authority for the production Markdown candidate queue.

Current Morning entries begin at candidate_id, then topic. Legacy entries
begin at topic and have no candidate ID authority. This parser deliberately
does not use Breaking identity's topic-anchored queue representation.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from nullone_bridge_common import BridgeError
from nullone_packaging_receipt import check_candidate_id

QUEUE_PATH = "social/state/candidate-queue.md"
FIELD = re.compile(r"^- \*\*([a-z_]+):\*\* (.*)$")
READY_STATUS_LINE = b"- **status:** READY"
HISTORICAL_STATUS_TRANSITIONS = {
    ("READY", "DRAFTED"),
    ("READY", "PUBLISHED"),
    ("DRAFTED", "PUBLISHED"),
}


@dataclass(frozen=True)
class QueueEntry:
    candidate_id: str | None
    topic: str
    fields: dict[str, str]
    start_line: int
    end_line: int
    status_line: int | None
    raw: bytes

    @property
    def status(self) -> str | None:
        return self.fields.get("status")


@dataclass(frozen=True)
class QueueSnapshot:
    data: bytes
    lines: tuple[bytes, ...]
    entries: tuple[QueueEntry, ...]

    def eligible(self) -> dict[str, QueueEntry]:
        return {
            entry.candidate_id: entry
            for entry in self.entries
            if entry.candidate_id is not None and entry.status == "READY"
            and _line_body(self.lines[entry.status_line]) == READY_STATUS_LINE
        }

    def ready_entry(self, candidate_id: str, *, baseline: QueueEntry | None = None) -> QueueEntry:
        matches = [entry for entry in self.entries if entry.candidate_id == candidate_id]
        if len(matches) != 1 or candidate_id not in self.eligible():
            raise BridgeError(f"Draft candidate is not uniquely READY: {candidate_id}")
        entry = matches[0]
        if baseline is not None and (
            entry.topic != baseline.topic or entry.fields != baseline.fields
            or entry.raw != baseline.raw
        ):
            raise BridgeError(f"Draft candidate changed during cycle: {candidate_id}")
        return entry


def _line_body(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def parse_queue(data: bytes) -> QueueSnapshot:
    """Parse actual field blocks, preserving every original byte and line."""
    try:
        data.decode("utf-8")
    except UnicodeError as exc:
        raise BridgeError("Draft candidate queue is not UTF-8") from exc
    lines = tuple(data.splitlines(keepends=True))
    entries: list[QueueEntry] = []
    seen_ids: set[str] = set()
    start: int | None = None
    fields: dict[str, str] = {}
    status_line: int | None = None
    current = False

    def finish(end: int) -> None:
        nonlocal start, fields, status_line, current
        if start is None:
            return
        candidate_id = fields.get("candidate_id") if current else None
        topic = fields.get("topic")
        if current and (
            not candidate_id or not topic or not fields.get("topic_cluster")
            or not fields.get("content_type") or not fields.get("status")
        ):
            raise BridgeError("Malformed current Draft queue entry")
        if not topic:
            raise BridgeError("Malformed legacy Draft queue entry")
        if candidate_id is not None:
            check_candidate_id(candidate_id)
        if candidate_id in seen_ids:
            raise BridgeError(f"Duplicate Draft queue candidate_id: {candidate_id}")
        if candidate_id is not None:
            seen_ids.add(candidate_id)
        entries.append(QueueEntry(
            candidate_id=candidate_id, topic=topic, fields=fields,
            start_line=start, end_line=end, status_line=status_line,
            raw=b"".join(lines[start:end]),
        ))
        start, fields, status_line, current = None, {}, None, False

    for index, raw_line in enumerate(lines):
        line = _line_body(raw_line).decode("utf-8")
        if line.startswith("#"):
            finish(index)
            continue
        match = FIELD.fullmatch(line)
        if not match:
            if current and "topic" not in fields:
                raise BridgeError("Draft queue candidate_id is not followed by topic")
            continue
        key, value = match.groups()
        if current and "topic" not in fields and key != "topic":
            raise BridgeError("Draft queue candidate_id is not followed by topic")
        if key == "discovered_at":
            finish(index)
            continue  # Morning writes this prelude before candidate_id.
        if key == "candidate_id":
            finish(index)
            start, current = index, True
        elif key == "topic":
            if current and "topic" in fields and (
                index == 0 or lines[index - 1].strip()
            ):
                raise BridgeError("Ambiguous Draft queue topic boundary")
            if start is None or "topic" in fields:
                finish(index)
                start, current = index, False
        elif start is None:
            continue  # Section metadata before the next candidate block.
        if key in fields:
            if key != "status" or (fields[key], value.strip()) not in HISTORICAL_STATUS_TRANSITIONS:
                raise BridgeError(f"Duplicate Draft queue field: {key}")
        if current and key == "topic" and "candidate_id" not in fields:
            raise BridgeError("Draft queue topic has no preceding candidate_id")
        fields[key] = value.strip()
        if key == "status":
            status_line = index
    finish(len(lines))
    return QueueSnapshot(data=data, lines=lines, entries=tuple(entries))


def _queue_file(workspace_root: Path) -> Path:
    try:
        root = workspace_root.resolve(strict=True)
    except OSError as exc:
        raise BridgeError("Draft workspace unreadable") from exc
    path = workspace_root / QUEUE_PATH
    if path.is_symlink():
        raise BridgeError("Draft candidate queue is a symlink")
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise BridgeError("Draft candidate queue unreadable or outside workspace") from exc
    return path


def load_queue(workspace_root: Path) -> QueueSnapshot:
    path = _queue_file(workspace_root)
    try:
        return parse_queue(path.read_bytes())
    except OSError as exc:
        raise BridgeError("Draft candidate queue unreadable") from exc


def flip_ready_to_drafted(workspace_root: Path, candidate_id: str,
                          baseline: QueueEntry) -> None:
    """Atomically change only the selected entry's exact READY status line."""
    path = _queue_file(workspace_root)
    snapshot = load_queue(workspace_root)
    entry = snapshot.ready_entry(candidate_id, baseline=baseline)
    assert entry.status_line is not None
    old_line = snapshot.lines[entry.status_line]
    if _line_body(old_line) != READY_STATUS_LINE:
        raise BridgeError("Draft queue READY status is not exact")
    replacement = old_line.replace(READY_STATUS_LINE, b"- **status:** DRAFTED", 1)
    changed = list(snapshot.lines)
    changed[entry.status_line] = replacement
    new_data = b"".join(changed)
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".draft-tmp-")
    except OSError as exc:
        raise BridgeError("Draft candidate queue unwritable") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(new_data)
        # Re-read immediately before replace so a concurrent queue edit
        # cannot be silently discarded by this cycle.
        if _queue_file(workspace_root).read_bytes() != snapshot.data:
            raise BridgeError("Draft candidate queue changed before status flip")
        os.replace(tmp_name, path)
    except BaseException as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise BridgeError("Draft candidate queue unwritable") from exc
        raise
