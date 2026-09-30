#!/usr/bin/env python3
"""Bounded Claude reasoning and deterministic Breaking Radar persistence (issue #190).

Mirrors the reviewed Weekly Claude boundary
(`nullone_claude_weekly_provider.py`) but designed for Radar's actual
needs: the model has NO shell, NO file-write, and NO scan-helper
capability at all. It returns one structured assessment result; this
module validates it, writes the Markdown report and staged
assessment files to deterministic Python-controlled destinations,
and commits through the existing deterministic
`nullone-breaking-scan.py` boundary (`current_scan` /
`commit_assessment` / `record_empty_scan` with an explicit
`workspace_root`). Deep assessment-envelope validation stays where
it already lives -- the commit helper fails closed on anything the
validator rejects, so no partial authoritative handoff can result
from a malformed model result.

Radar domain semantics are unchanged: DELTA_MONITORING_ONLY,
Azerbaijani-first, source-driven research; no drafts, no Telegram,
no publication path; fresh-report gate stays in the wrapper.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nullone_bridge_common import BridgeError
from nullone_claude import run_structured

ALLOWED_TOOLS = ["Read", "WebSearch", "WebFetch"]

FIELDS = ("mode", "report_markdown", "assessments")
MODES = ("CANDIDATES_EMITTED", "NO_MATERIAL_DEVELOPMENT")
MAX_ASSESSMENTS = 25

# Cheap structural pre-check only (mirrors the prompt's validator-exact
# candidate_id rule); deep envelope validation stays in the commit
# helper, which fails closed and commits atomically per assessment.
CANDIDATE_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+){1,7}$")
CANDIDATE_ID_MAX_LEN = 80

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(FIELDS),
    "properties": {
        "mode": {"type": "string", "enum": list(MODES)},
        "report_markdown": {"type": "string", "minLength": 1},
        "assessments": {"type": "array", "maxItems": MAX_ASSESSMENTS, "items": {"type": "object"}},
    },
}

SCAN_SCRIPT = "nullone-breaking-scan.py"
SCAN_SOURCE = "openclaw"
REPORT_GLOB_HINT = "breaking"

BAKU_ZONE = "Asia/Baku"


def radar_security_settings(workspace: Path) -> dict:
    """Anchor Claude Read deny rules to this invocation's workspace.

    Same deny shape as the reviewed Weekly boundary: secrets, keys,
    and the private ops directory are unreadable. No Write/Edit/Bash
    grant exists at all -- persistence is deterministic Python.
    """
    root = workspace.resolve(strict=True)
    anchor = "//" + root.as_posix().lstrip("/")
    denied = []
    for name in (".env", ".env.*", "*.key", "*.pem"):
        denied.extend((f"Read({anchor}/{name})", f"Read({anchor}/**/{name})"))
    denied.append(f"Read({anchor}/social/ops/private/**)")
    return {"permissions": {"deny": denied}}


def _validated(result: object) -> dict:
    """Validate the structured Radar result (fail closed).

    Structural only: exact key set, mode enum, non-blank report,
    assessments list shape with mode-appropriate emptiness and
    candidate_id pre-checks. The commit helper performs the full
    envelope validation before anything becomes authoritative.
    """
    if not isinstance(result, dict) or set(result) != set(FIELDS):
        raise BridgeError("Malformed Radar Claude result")
    if result["mode"] not in MODES:
        raise BridgeError("Malformed Radar Claude result")
    report = result["report_markdown"]
    if not isinstance(report, str) or not report.strip():
        raise BridgeError("Malformed Radar Claude result")
    assessments = result["assessments"]
    if not isinstance(assessments, list):
        raise BridgeError("Malformed Radar Claude result")
    if result["mode"] == "NO_MATERIAL_DEVELOPMENT":
        if assessments:
            raise BridgeError("Malformed Radar Claude result")
    elif not assessments:
        raise BridgeError("Malformed Radar Claude result")
    for item in assessments:
        if not isinstance(item, dict):
            raise BridgeError("Malformed Radar Claude result")
        candidate_id = item.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or len(candidate_id) > CANDIDATE_ID_MAX_LEN
            or not CANDIDATE_ID_RE.match(candidate_id)
        ):
            raise BridgeError("Malformed Radar Claude result")
    return result


_SCAN_MODULE: Any = None


def _scan_module() -> Any:
    """Load the deterministic scan helper in-process (no subprocess).

    Same technique as the reconciliation bridge loader: the dash-named
    script is loaded by file location next to this module. Only the
    reviewed `current_scan` / `commit_assessment` / `record_empty_scan`
    entry points are used, with an explicit `workspace_root`.
    """
    global _SCAN_MODULE
    if _SCAN_MODULE is None:
        path = Path(__file__).resolve().parent / SCAN_SCRIPT
        spec = importlib.util.spec_from_file_location(
            "nullone_breaking_scan_loaded", path
        )
        if spec is None or spec.loader is None:
            raise BridgeError("Cannot load breaking scan helper")
        module = importlib.util.module_from_spec(spec)
        sys.modules["nullone_breaking_scan_loaded"] = module
        spec.loader.exec_module(module)
        _SCAN_MODULE = module
    return _SCAN_MODULE


def _report_filename(scheduled_for: str) -> str:
    """Deterministic report name from the due scan slot (Asia/Baku).

    Matches the existing `YYYY-MM-DD-breaking-HHMM.md` convention the
    wrapper's fresh-report gate globs. The model never supplies paths.
    """
    try:
        slot = datetime.fromisoformat(str(scheduled_for).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BridgeError(f"Unparseable scan slot: {scheduled_for!r}") from exc
    if slot.tzinfo is None:
        raise BridgeError(f"Scan slot must be timezone-aware: {scheduled_for!r}")
    local = slot.astimezone(ZoneInfo(BAKU_ZONE))
    return local.strftime("%Y-%m-%d-breaking-%H%M.md")


def _write_text_file(path: Path, content: str, *, workspace_root: Path) -> None:
    """Atomic workspace-contained text write (temp + replace).

    Symlink/escape fail closed, mirroring the Weekly persistence
    boundary. Deterministic Python owns every destination.
    """
    root = workspace_root.resolve(strict=True)
    try:
        path.resolve().relative_to(root)
    except (ValueError, OSError) as exc:
        raise BridgeError("Radar output path escapes workspace") from exc
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or parent.is_symlink():
        raise BridgeError("Radar output path is a symlink")
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".radar-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        resolved_tmp = Path(tmp_name).resolve()
        try:
            resolved_tmp.relative_to(root)
        except ValueError as exc:
            raise BridgeError("Radar output path escapes workspace") from exc
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def invoke_radar(
    *,
    prompt: str,
    workspace: Path | str,
    model: str,
    timeout: int,
) -> dict[str, Any]:
    """Run one bounded Claude Radar cycle and persist deterministically.

    Returns a summary dict (mode, report_path, committed candidate
    ids / empty-scan flag). Raises BridgeError fail-closed on
    transport failure, malformed output, or any commit rejection --
    with no partial file writes left ambiguous: per-assessment
    commits are atomic in the scan helper, and a commit failure
    aborts the cycle before the wrapper can claim completion.
    """
    root = Path(workspace)
    if not prompt.strip():
        raise BridgeError("Radar prompt must not be blank")
    if not model.strip():
        raise BridgeError("Radar model must not be blank")
    if timeout <= 0:
        raise BridgeError("Radar timeout must be positive")

    result = run_structured(
        prompt=prompt,
        allowed_tools=ALLOWED_TOOLS,
        schema=SCHEMA,
        model=model,
        max_turns=30,
        timeout=timeout,
        workspace=root,
        weekly_security_settings=radar_security_settings(root),
    )
    validated = _validated(result)

    scanmod = _scan_module()
    scan = scanmod.current_scan(source=SCAN_SOURCE)

    daily = root / "social/research/daily"
    report_path = daily / _report_filename(scan["scheduled_for"])
    _write_text_file(report_path, validated["report_markdown"], workspace_root=root)

    staging = root / str(scanmod.STAGING_SUBPATH)
    staging.mkdir(parents=True, exist_ok=True)

    committed: list[str] = []
    if validated["mode"] == "NO_MATERIAL_DEVELOPMENT":
        scanmod.record_empty_scan(source=SCAN_SOURCE, workspace_root=root)
        return {
            "mode": "NO_MATERIAL_DEVELOPMENT",
            "report_path": str(report_path),
            "committed": committed,
            "empty_recorded": True,
        }

    # Pre-write every staged assessment before the first commit so a
    # structurally malformed batch can never leave a partial
    # authoritative handoff behind.
    staged_paths: list[tuple[str, Path]] = []
    for item in validated["assessments"]:
        candidate_id = item["candidate_id"]
        staged = staging / f"{candidate_id}.json"
        payload = json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True)
        _write_text_file(staged, payload + "\n", workspace_root=root)
        staged_paths.append((candidate_id, staged))

    for candidate_id, staged in staged_paths:
        scanmod.commit_assessment(
            assessment_path=staged, source=SCAN_SOURCE, workspace_root=root
        )
        committed.append(candidate_id)

    return {
        "mode": "CANDIDATES_EMITTED",
        "report_path": str(report_path),
        "committed": committed,
        "empty_recorded": False,
    }


def self_test() -> int:
    if set(FIELDS) != {"mode", "report_markdown", "assessments"}:
        raise AssertionError("radar schema fields drifted")
    for bad in (None, {}, {"mode": "x", "report_markdown": "r", "assessments": []}):
        try:
            _validated(bad)
        except BridgeError:
            pass
        else:
            raise AssertionError(f"malformed result accepted: {bad!r}")
    print("RADAR_CLAUDE_PROVIDER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
