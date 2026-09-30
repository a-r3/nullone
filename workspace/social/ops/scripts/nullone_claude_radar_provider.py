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
`prepare_assessment_commit` / `commit_assessment` / `record_empty_scan`
with an explicit `workspace_root`).

Batch authority rule: EVERY assessment in a CANDIDATES_EMITTED result
is deep-validated through the scan helper's own non-mutating
`prepare_assessment_commit` -- the exact helper `commit_assessment`
itself uses -- BEFORE the first output write (no report, no staged
file) and therefore before the first authoritative mutation (no
handoff, no scan receipt). A malformed batch fails closed with zero
authoritative state. Per-assessment commit atomicity alone would NOT
prevent partial batch authority, so the full-batch preflight is what
carries the invariant.

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

# Cheap structural pre-checks only (mirrors the prompt's validator-exact
# candidate_id rule); deep envelope validation stays in the scan
# helper's shared prepare/commit path, which fails closed.
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
    assessments list shape with mode-appropriate emptiness,
    candidate_id pre-checks, and duplicate-free candidate_ids (a
    repeated id would collapse two staged payloads onto one
    `<staging>/<candidate_id>.json` path and bypass the scan
    helper's conflict logic, so identical and divergent duplicates
    are both rejected here, before any write). The scan helper's
    shared prepare/commit path performs the full envelope
    validation before anything becomes authoritative.
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
    seen_ids: set[str] = set()
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
        if candidate_id in seen_ids:
            raise BridgeError(
                "Duplicate candidate_id in Radar Claude result"
            )
        seen_ids.add(candidate_id)
    return result


_SCAN_MODULE: Any = None


def _scan_module() -> Any:
    """Load the deterministic scan helper in-process (no subprocess).

    Same technique as the reconciliation bridge loader: the dash-named
    script is loaded by file location next to this module. Only the
    reviewed `current_scan` / `prepare_assessment_commit` /
    `commit_assessment` / `record_empty_scan` entry points are used,
    with an explicit `workspace_root`.
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
    transport failure, malformed output, duplicate candidate_ids, or
    any scan-helper rejection -- with zero authoritative state left
    by a rejected batch: every CANDIDATES_EMITTED assessment is
    deep-validated through the scan helper's own non-mutating
    `prepare_assessment_commit` (the exact helper `commit_assessment`
    uses) before the first output write, so a deep-invalid batch
    produces no report, no staged assessment, no handoff, and no
    scan receipt.

    Scan-identity binding: the whole cycle -- report name,
    prevalidation, and every commit -- binds the single
    `triggered_at` returned by this cycle's `current_scan`, so the
    report identity and all authoritative commits provably derive
    from the same scan occurrence (no parallel identity mechanism).

    NOTE on residual risk: the preflight eliminates every
    *validation-driven* partial commit. A commit-time receipt
    conflict (e.g. a concurrent writer recorded a contradictory
    receipt between preflight and commit) still fails closed inside
    the scan helper's per-scan lock via its own COMMIT_CONFLICT
    rules; that path is outside model-malformed scope.
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
    try:
        scan = scanmod.current_scan(source=SCAN_SOURCE)
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(f"Radar scan identity unavailable: {exc}") from exc
    cycle_at = scan.get("triggered_at") if isinstance(scan, dict) else None
    if not isinstance(cycle_at, str) or not cycle_at:
        raise BridgeError("Radar scan identity missing triggered_at")
    scheduled_for = scan.get("scheduled_for") if isinstance(scan, dict) else None
    if not isinstance(scheduled_for, str) or not scheduled_for:
        raise BridgeError("Radar scan identity missing scheduled_for")

    daily = root / "social/research/daily"
    report_path = daily / _report_filename(scheduled_for)

    if validated["mode"] == "NO_MATERIAL_DEVELOPMENT":
        # Receipt first: a rejected empty-record leaves no orphan report
        # for the wrapper's fresh-report gate to trip over.
        try:
            scanmod.record_empty_scan(
                source=SCAN_SOURCE, at=cycle_at, workspace_root=root
            )
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(f"Radar empty scan rejected: {exc}") from exc
        _write_text_file(report_path, validated["report_markdown"], workspace_root=root)
        return {
            "mode": "NO_MATERIAL_DEVELOPMENT",
            "report_path": str(report_path),
            "committed": [],
            "empty_recorded": True,
        }

    # Full-batch deep preflight through the scan helper's own
    # validation authority, BEFORE any output write. Any rejection
    # aborts here with no report, no staged file, no handoff, and no
    # receipt mutation.
    try:
        for item in validated["assessments"]:
            scanmod.prepare_assessment_commit(
                assessment=item, source=SCAN_SOURCE, at=cycle_at
            )
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(f"Radar batch deep validation failed: {exc}") from exc

    _write_text_file(report_path, validated["report_markdown"], workspace_root=root)

    staging = root / str(scanmod.STAGING_SUBPATH)
    staging.mkdir(parents=True, exist_ok=True)

    # Staged payloads are keyed by the duplicate-free candidate_ids
    # rejected above, so no two assessments can share a staged path.
    staged_paths: list[tuple[str, Path]] = []
    for item in validated["assessments"]:
        candidate_id = item["candidate_id"]
        staged = staging / f"{candidate_id}.json"
        payload = json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True)
        _write_text_file(staged, payload + "\n", workspace_root=root)
        staged_paths.append((candidate_id, staged))

    committed: list[str] = []
    try:
        for candidate_id, staged in staged_paths:
            scanmod.commit_assessment(
                assessment_path=staged,
                source=SCAN_SOURCE,
                at=cycle_at,
                workspace_root=root,
            )
            committed.append(candidate_id)
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(f"Radar commit rejected: {exc}") from exc

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
