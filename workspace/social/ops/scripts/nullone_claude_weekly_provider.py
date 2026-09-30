#!/usr/bin/env python3
"""Bounded Claude reasoning and deterministic Weekly Strategy persistence."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nullone_bridge_common import BridgeError
from nullone_claude import run_structured

ALLOWED_TOOLS = ["Read", "WebSearch", "WebFetch"]
FIELDS = ("evidence", "reference_review", "observations", "hypotheses", "decisions", "memory_update")
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(FIELDS),
    "properties": {
        "evidence": {"type": "string", "minLength": 1},
        "reference_review": {"type": "string", "minLength": 1},
        "observations": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "hypotheses": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "decisions": {"type": "array", "maxItems": 3, "items": {"type": "string", "minLength": 1}},
        "memory_update": {"type": "string"},
    },
}


def _validated(result: object) -> dict:
    if not isinstance(result, dict) or set(result) != set(FIELDS):
        raise BridgeError("Malformed Weekly Claude result")
    for field in ("evidence", "reference_review", "memory_update"):
        if not isinstance(result[field], str):
            raise BridgeError("Malformed Weekly Claude result")
    if not result["evidence"].strip() or not result["reference_review"].strip():
        raise BridgeError("Malformed Weekly Claude result")
    for field in ("observations", "hypotheses", "decisions"):
        values = result[field]
        if not isinstance(values, list) or (field != "decisions" and not values):
            raise BridgeError("Malformed Weekly Claude result")
        if field == "decisions" and len(values) > 3:
            raise BridgeError("Malformed Weekly Claude result")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise BridgeError("Malformed Weekly Claude result")
    return result


def _destination(workspace: Path, relative: str) -> Path:
    root = workspace.resolve(strict=True)
    target = root / relative
    # Reject redirects even if a symlink happens to point back inside.
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise BridgeError("Weekly output path is outside workspace")
    if not target.parent.resolve().is_relative_to(root):
        raise BridgeError("Weekly output path is outside workspace")
    return target


def persist_weekly(result: object, *, workspace: Path, now: datetime | None = None) -> Path:
    """Write one ISO-week report and optionally append to workspace MEMORY.md."""
    data = _validated(result)
    when = (now or datetime.now(ZoneInfo("Asia/Baku"))).astimezone(ZoneInfo("Asia/Baku"))
    year, week, _ = when.isocalendar()
    report = _destination(workspace, f"social/analytics/reports/{year}-{week:02d}-weekly-strategy.md")
    memory = _destination(workspace, "MEMORY.md")
    sections = [f"# Weekly Strategy — {year}-W{week:02d}",
                "## Evidence (previous 7 days)", data["evidence"].strip(),
                "## Reference review", data["reference_review"].strip()]
    for label, key in (("OBSERVATION", "observations"),
                       ("HYPOTHESIS", "hypotheses"), ("DECISION", "decisions")):
        sections.extend((f"## {label}",
                         "\n".join(f"- {item.strip()}" for item in data[key]) or "- Keep strategy stable."))
    report_text = "\n\n".join(sections) + "\n"
    report.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after directory creation; no model-supplied path is accepted.
    report = _destination(workspace, f"social/analytics/reports/{year}-{week:02d}-weekly-strategy.md")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=report.parent,
                                         prefix=".weekly-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(report_text)
        os.replace(temporary, report)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    if data["memory_update"].strip():
        memory = _destination(workspace, "MEMORY.md")
        with memory.open("a", encoding="utf-8") as handle:
            handle.write("\n" + data["memory_update"].strip() + "\n")
    return report


def invoke_weekly(*, prompt: str, workspace: Path, model: str, timeout: int) -> None:
    result = run_structured(prompt=prompt, allowed_tools=ALLOWED_TOOLS,
                            schema=SCHEMA, model=model, max_turns=30, timeout=timeout,
                            workspace=workspace)
    persist_weekly(result, workspace=workspace)
