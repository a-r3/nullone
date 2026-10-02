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
from nullone_breaking_workflow_input import assessment_json_schema
from nullone_claude import run_structured

ALLOWED_TOOLS = ["Read", "WebSearch", "WebFetch"]

FIELDS = ("mode", "report_markdown", "assessments")
MODES = ("CANDIDATES_EMITTED", "NO_MATERIAL_DEVELOPMENT")
MAX_ASSESSMENTS = 25

# The nested assessment schema is generated deterministically from the
# authoritative workflow-input module's own constants (never a
# hand-maintained copy), so the CLI-level contract cannot drift from
# the deterministic validator. Cross-field rules stay exclusively with
# validate_breaking_workflow_input(), which remains the final
# authority; the deep preflight still rejects anything it rejects.
ASSESSMENT_SCHEMA = assessment_json_schema()

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
        "assessments": {
            "type": "array",
            "maxItems": MAX_ASSESSMENTS,
            "items": ASSESSMENT_SCHEMA,
        },
    },
}

SCAN_SCRIPT = "nullone-breaking-scan.py"
SCAN_SOURCE = "openclaw"
REPORT_GLOB_HINT = "breaking"

BAKU_ZONE = "Asia/Baku"

# Safe failure-stage telemetry (no raw transport content, ever).
#
# Every Radar failure surfaces as RadarStageError with a stable
# machine-readable reason_code. Only the code, the stage name
# (identical to the code), and -- for nonzero Claude exits -- a
# numeric exit code are observable. Raw stdout/stderr, prompt text,
# fetched content, model/tool URLs, source text, signed URLs, and
# secrets are never included.
RADAR_REASON_CLAUDE_TIMEOUT = "CLAUDE_TIMEOUT"
RADAR_REASON_CLAUDE_BINARY_MISSING = "CLAUDE_BINARY_MISSING"
RADAR_REASON_CLAUDE_EXIT_NONZERO = "CLAUDE_EXIT_NONZERO"
RADAR_REASON_CLAUDE_OUTPUT_INVALID = "CLAUDE_OUTPUT_INVALID"
RADAR_REASON_RESULT_VALIDATION = "RESULT_VALIDATION"
RADAR_REASON_SCAN_IDENTITY = "SCAN_IDENTITY"
RADAR_REASON_EMPTY_SCAN_RECEIPT = "EMPTY_SCAN_RECEIPT"
RADAR_REASON_BATCH_PREFLIGHT = "BATCH_PREFLIGHT"
RADAR_REASON_REPORT_WRITE = "REPORT_WRITE"
RADAR_REASON_STAGING_WRITE = "STAGING_WRITE"
RADAR_REASON_COMMIT = "COMMIT"
RADAR_REASON_UNKNOWN = "UNKNOWN_RADAR_FAILURE"

ALLOWED_RADAR_REASON_CODES = frozenset(
    {
        RADAR_REASON_CLAUDE_TIMEOUT,
        RADAR_REASON_CLAUDE_BINARY_MISSING,
        RADAR_REASON_CLAUDE_EXIT_NONZERO,
        RADAR_REASON_CLAUDE_OUTPUT_INVALID,
        RADAR_REASON_RESULT_VALIDATION,
        RADAR_REASON_SCAN_IDENTITY,
        RADAR_REASON_EMPTY_SCAN_RECEIPT,
        RADAR_REASON_BATCH_PREFLIGHT,
        RADAR_REASON_REPORT_WRITE,
        RADAR_REASON_STAGING_WRITE,
        RADAR_REASON_COMMIT,
        RADAR_REASON_UNKNOWN,
    }
)


class RadarStageError(BridgeError):
    """Deterministic Radar failure stage with safe telemetry only."""

    def __init__(self, reason_code: str, *, exit_code: int | None = None):
        code = (
            reason_code
            if reason_code in ALLOWED_RADAR_REASON_CODES
            else RADAR_REASON_UNKNOWN
        )
        self.reason_code = code
        if isinstance(exit_code, bool):
            self.exit_code: int | None = None
        elif isinstance(exit_code, int):
            self.exit_code = exit_code
        else:
            self.exit_code = None
        message = f"Radar stage failure: {self.reason_code}"
        if self.reason_code == RADAR_REASON_CLAUDE_EXIT_NONZERO and self.exit_code is not None:
            message += f" exit={self.exit_code}"
        super().__init__(message)


_CLAUDE_EXIT_RE = re.compile(r"^Claude invocation failed \(exit=(-?\d+)\)$")


def _map_claude_failure(exc: BaseException) -> RadarStageError:
    """Map a `run_structured` BridgeError to a safe stage code.

    Matches only known locally-generated messages from
    `nullone_claude.run_structured`. Anything else -- including
    arbitrary downstream text -- collapses to UNKNOWN_RADAR_FAILURE
    without echoing the original message.
    """
    message = str(exc) if isinstance(exc, BridgeError) else ""
    if message == "Claude invocation timed out":
        return RadarStageError(RADAR_REASON_CLAUDE_TIMEOUT)
    if message == "claude binary not found":
        return RadarStageError(RADAR_REASON_CLAUDE_BINARY_MISSING)
    matched = _CLAUDE_EXIT_RE.match(message)
    if matched is not None:
        try:
            exit_code = int(matched.group(1))
        except ValueError:
            exit_code = None
        return RadarStageError(
            RADAR_REASON_CLAUDE_EXIT_NONZERO, exit_code=exit_code
        )
    if message in (
        "Claude returned non-JSON output",
        "Claude JSON output is not an object",
    ):
        return RadarStageError(RADAR_REASON_CLAUDE_OUTPUT_INVALID)
    return RadarStageError(RADAR_REASON_UNKNOWN)


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
    per-assessment exact workflow-input field set, candidate_id
    pre-checks, and duplicate-free candidate_ids (a
    repeated id would collapse two staged payloads onto one
    `<staging>/<candidate_id>.json` path and bypass the scan
    helper's conflict logic, so identical and divergent duplicates
    are both rejected here, before any write). The per-assessment
    field-set check mirrors the validator's own exact-set rule, so
    it can never reject anything the deterministic validator would
    accept; full envelope validation still happens in the scan
    helper's shared prepare/commit path before anything becomes
    authoritative.

    All rejections raise RadarStageError(RESULT_VALIDATION) with no
    payload echo.
    """
    if not isinstance(result, dict) or set(result) != set(FIELDS):
        raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
    if result["mode"] not in MODES:
        raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
    report = result["report_markdown"]
    if not isinstance(report, str) or not report.strip():
        raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
    assessments = result["assessments"]
    if not isinstance(assessments, list):
        raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
    if result["mode"] == "NO_MATERIAL_DEVELOPMENT":
        if assessments:
            raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
    elif not assessments:
        raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
    required_assessment_fields = set(ASSESSMENT_SCHEMA["required"])
    seen_ids: set[str] = set()
    for item in assessments:
        if not isinstance(item, dict):
            raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
        # Legacy/editorial shapes (unknown fields such as score or
        # audience_value, or missing workflow fields) fail here,
        # before any scan contact or write.
        if set(item) != required_assessment_fields:
            raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
        candidate_id = item.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or len(candidate_id) > CANDIDATE_ID_MAX_LEN
            or not CANDIDATE_ID_RE.match(candidate_id)
        ):
            raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
        if candidate_id in seen_ids:
            raise RadarStageError(RADAR_REASON_RESULT_VALIDATION)
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
        raise BridgeError("Unparseable scan slot") from exc
    if slot.tzinfo is None:
        raise BridgeError("Scan slot must be timezone-aware")
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
    ids / empty-scan flag). Raises RadarStageError fail-closed with
    a stable reason_code on every failure -- with zero authoritative
    state left by a rejected batch: every CANDIDATES_EMITTED
    assessment is deep-validated through the scan helper's own
    non-mutating `prepare_assessment_commit` (the exact helper
    `commit_assessment` uses) before the first output write, so a
    deep-invalid batch produces no report, no staged assessment, no
    handoff, and no scan receipt.

    Failure ordering, write ordering, and authority are unchanged:
    a failure that previously blocked still blocks at the same
    point, and no new write precedes an existing write boundary.
    Only the error carrier changes (safe stage code, never raw
    transport content).

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
    try:
        return _invoke_radar_staged(
            prompt=prompt, workspace=workspace, model=model, timeout=timeout
        )
    except RadarStageError:
        raise
    except BridgeError:
        raise RadarStageError(RADAR_REASON_UNKNOWN) from None
    except Exception:
        raise RadarStageError(RADAR_REASON_UNKNOWN) from None


def _invoke_radar_staged(
    *,
    prompt: str,
    workspace: Path | str,
    model: str,
    timeout: int,
) -> dict[str, Any]:
    root = Path(workspace)
    if not prompt.strip():
        raise RadarStageError(RADAR_REASON_UNKNOWN)
    if not model.strip():
        raise RadarStageError(RADAR_REASON_UNKNOWN)
    if timeout <= 0:
        raise RadarStageError(RADAR_REASON_UNKNOWN)

    try:
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
    except RadarStageError:
        raise
    except BridgeError as exc:
        raise _map_claude_failure(exc) from None
    except Exception:
        raise RadarStageError(RADAR_REASON_UNKNOWN) from None
    try:
        validated = _validated(result)
    except RadarStageError:
        raise
    except BridgeError:
        raise RadarStageError(RADAR_REASON_RESULT_VALIDATION) from None
    except Exception:
        raise RadarStageError(RADAR_REASON_UNKNOWN) from None

    try:
        scanmod = _scan_module()
    except RadarStageError:
        raise
    except BridgeError:
        raise RadarStageError(RADAR_REASON_SCAN_IDENTITY) from None
    except Exception:
        raise RadarStageError(RADAR_REASON_UNKNOWN) from None
    try:
        try:
            scan = scanmod.current_scan(source=SCAN_SOURCE)
        except RadarStageError:
            raise
        except BridgeError:
            raise RadarStageError(RADAR_REASON_SCAN_IDENTITY) from None
        except Exception:
            raise RadarStageError(RADAR_REASON_SCAN_IDENTITY) from None
        cycle_at = scan.get("triggered_at") if isinstance(scan, dict) else None
        if not isinstance(cycle_at, str) or not cycle_at:
            raise RadarStageError(RADAR_REASON_SCAN_IDENTITY)
        scheduled_for = scan.get("scheduled_for") if isinstance(scan, dict) else None
        if not isinstance(scheduled_for, str) or not scheduled_for:
            raise RadarStageError(RADAR_REASON_SCAN_IDENTITY)
        try:
            filename = _report_filename(scheduled_for)
        except RadarStageError:
            raise
        except BridgeError:
            raise RadarStageError(RADAR_REASON_SCAN_IDENTITY) from None
        except Exception:
            raise RadarStageError(RADAR_REASON_SCAN_IDENTITY) from None
    except RadarStageError:
        raise
    except BridgeError:
        raise RadarStageError(RADAR_REASON_SCAN_IDENTITY) from None
    except Exception:
        raise RadarStageError(RADAR_REASON_UNKNOWN) from None

    daily = root / "social/research/daily"
    report_path = daily / filename

    if validated["mode"] == "NO_MATERIAL_DEVELOPMENT":
        # Receipt first: a rejected empty-record leaves no orphan report
        # for the wrapper's fresh-report gate to trip over.
        try:
            scanmod.record_empty_scan(
                source=SCAN_SOURCE, at=cycle_at, workspace_root=root
            )
        except RadarStageError:
            raise
        except BridgeError:
            raise RadarStageError(RADAR_REASON_EMPTY_SCAN_RECEIPT) from None
        except Exception:
            raise RadarStageError(RADAR_REASON_EMPTY_SCAN_RECEIPT) from None
        try:
            _write_text_file(report_path, validated["report_markdown"], workspace_root=root)
        except RadarStageError:
            raise
        except BridgeError:
            raise RadarStageError(RADAR_REASON_REPORT_WRITE) from None
        except Exception:
            raise RadarStageError(RADAR_REASON_REPORT_WRITE) from None
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
    for item in validated["assessments"]:
        try:
            scanmod.prepare_assessment_commit(
                assessment=item, source=SCAN_SOURCE, at=cycle_at
            )
        except RadarStageError:
            raise
        except BridgeError:
            raise RadarStageError(RADAR_REASON_BATCH_PREFLIGHT) from None
        except Exception:
            raise RadarStageError(RADAR_REASON_BATCH_PREFLIGHT) from None

    try:
        _write_text_file(report_path, validated["report_markdown"], workspace_root=root)
    except RadarStageError:
        raise
    except BridgeError:
        raise RadarStageError(RADAR_REASON_REPORT_WRITE) from None
    except Exception:
        raise RadarStageError(RADAR_REASON_REPORT_WRITE) from None

    try:
        staging = root / str(scanmod.STAGING_SUBPATH)
        staging.mkdir(parents=True, exist_ok=True)
    except RadarStageError:
        raise
    except BridgeError:
        raise RadarStageError(RADAR_REASON_STAGING_WRITE) from None
    except Exception:
        raise RadarStageError(RADAR_REASON_STAGING_WRITE) from None

    # Staged payloads are keyed by the duplicate-free candidate_ids
    # rejected above, so no two assessments can share a staged path.
    staged_paths: list[tuple[str, Path]] = []
    for item in validated["assessments"]:
        candidate_id = item["candidate_id"]
        staged = staging / f"{candidate_id}.json"
        payload = json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            _write_text_file(staged, payload + "\n", workspace_root=root)
        except RadarStageError:
            raise
        except BridgeError:
            raise RadarStageError(RADAR_REASON_STAGING_WRITE) from None
        except Exception:
            raise RadarStageError(RADAR_REASON_STAGING_WRITE) from None
        staged_paths.append((candidate_id, staged))

    committed: list[str] = []
    for candidate_id, staged in staged_paths:
        try:
            scanmod.commit_assessment(
                assessment_path=staged,
                source=SCAN_SOURCE,
                at=cycle_at,
                workspace_root=root,
            )
        except RadarStageError:
            raise
        except BridgeError:
            raise RadarStageError(RADAR_REASON_COMMIT) from None
        except Exception:
            raise RadarStageError(RADAR_REASON_COMMIT) from None
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
