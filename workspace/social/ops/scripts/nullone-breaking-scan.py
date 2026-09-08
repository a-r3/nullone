#!/usr/bin/env python3
"""Deterministic Radar scan commit edge (#80).

The Radar agent (LLM) owns discovery/verification/scoring. Deterministic
code owns everything safety-critical around it:

    current-scan
        -> resolve the current raw scan slot (scan identity for the agent
           to stamp into its structured assessments)
    commit --assessment <staged-assessment.json>
        -> validate candidate-ID shape
        -> resolve the current scan slot (must be DUE)
        -> build the exact `nullone.breaking-radar-handoff.v1` envelope
        -> strict-validate the whole envelope (edge validator)
        -> atomic-commit to the canonical spool path
        -> update the scan receipt
    record-empty
        -> truthful NO_MATERIAL_DEVELOPMENT receipt for a scan with zero
           qualifying candidates

The human Markdown report stays a sibling audit artifact; it is never
read here. Handoffs carry `source_occurrence_id` + `scheduled_for` +
`triggered_at`; retry timing and mutable assessment prose never enter
identity (see `nullone_breaking_scan_authority.py`).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nullone_breaking_radar_edge import (
    BreakingRadarEdgeError,
    compute_candidate_external_occurrence_id,
    normalize_breaking_radar_handoff,
)
from nullone_breaking_scan_authority import (
    BreakingScanAuthorityError,
    resolve_radar_scan,
    validate_candidate_id,
)
from nullone_breaking_workflow_input import BreakingWorkflowInputError
from nullone_bridge_common import WORKSPACE, atomic_write_json

HANDOFFS_SUBPATH = Path("social/ops/breaking-handoffs")
RECEIPT_FILENAME = "scan-receipt.json"

RECEIPT_SCHEMA = "nullone.breaking-radar-scan-receipt.v1"
RECEIPT_CONTRACT_VERSION = "1.0.0"
RECEIPT_STATUSES = frozenset({"NO_MATERIAL_DEVELOPMENT", "CANDIDATES_EMITTED"})

M0_SCAN_SOURCES = frozenset({"openclaw"})


class BreakingScanCommitError(ValueError):
    """Stable commit-edge rejection (never a partial commit)."""


def _utc_now_canonical() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_m0_source(source: object) -> str:
    if not isinstance(source, str) or source not in M0_SCAN_SOURCES:
        raise BreakingScanCommitError(
            "unsupported scan source (reviewed M0 adapter namespace required)"
        )
    return source


def _resolve_due_scan(*, source: str, at: str) -> Any:
    try:
        resolution = resolve_radar_scan(source=source, triggered_at=at)
    except BreakingScanAuthorityError as exc:
        raise BreakingScanCommitError(str(exc)) from exc
    if resolution.status != "DUE":
        raise BreakingScanCommitError(
            "no Radar scan slot is currently due; refusing to mint scan identity"
        )
    return resolution


def _scan_dir(workspace_root: Path, source_occurrence_id: str) -> Path:
    return workspace_root / HANDOFFS_SUBPATH / source_occurrence_id


def _receipt_path(workspace_root: Path, source_occurrence_id: str) -> Path:
    return _scan_dir(workspace_root, source_occurrence_id) / RECEIPT_FILENAME


def _read_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BreakingScanCommitError("existing scan receipt is unreadable")
    if not isinstance(data, dict) or data.get("schema") != RECEIPT_SCHEMA:
        raise BreakingScanCommitError("existing scan receipt has wrong schema")
    return data


def _write_receipt(
    *,
    workspace_root: Path,
    source_occurrence_id: str,
    scheduled_for: str,
    source: str,
    status: str,
    candidates: list[str],
) -> Path:
    if status not in RECEIPT_STATUSES:
        raise BreakingScanCommitError(f"unknown receipt status: {status!r}")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "contract_version": RECEIPT_CONTRACT_VERSION,
        "source_occurrence_id": source_occurrence_id,
        "scheduled_for": scheduled_for,
        "source": source,
        "status": status,
        "candidates": list(candidates),
        "created_at": _utc_now_canonical(),
    }
    path = _receipt_path(workspace_root, source_occurrence_id)
    atomic_write_json(path, receipt)
    return path


def current_scan(
    *, source: str = "openclaw", at: str | None = None
) -> dict[str, Any]:
    """Resolve and report the current raw scan identity (no side effects)."""

    reviewed_source = _require_m0_source(source)
    resolution = _resolve_due_scan(
        source=reviewed_source, at=at or _utc_now_canonical()
    )
    return {
        "schedule_id": resolution.schedule_id,
        "scheduled_for": resolution.scheduled_for,
        "source_occurrence_id": resolution.source_occurrence_id,
        "source": resolution.source,
        "triggered_at": resolution.triggered_at,
    }


def commit_assessment(
    *,
    assessment_path: str | Path,
    source: str = "openclaw",
    at: str | None = None,
    workspace_root: Path = WORKSPACE,
) -> dict[str, Any]:
    """Validate, stamp, and atomically commit one staged assessment.

    Returns the committed handoff path, candidate external occurrence ID,
    and scan identity. Identical recommit is idempotent; conflicting
    content for the same candidate path is rejected (COMMIT_CONFLICT).
    """

    reviewed_source = _require_m0_source(source)
    triggered_at = at or _utc_now_canonical()
    resolution = _resolve_due_scan(source=reviewed_source, at=triggered_at)

    try:
        assessment = json.loads(Path(assessment_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BreakingScanCommitError(
            f"staged assessment unreadable: {assessment_path}"
        ) from exc
    if not isinstance(assessment, dict):
        raise BreakingScanCommitError("staged assessment must be a JSON object")

    try:
        validate_candidate_id(assessment.get("candidate_id"))
    except BreakingScanAuthorityError as exc:
        raise BreakingScanCommitError(str(exc)) from exc

    handoff = {
        "schema": "nullone.breaking-radar-handoff.v1",
        "contract_version": "1.0.0",
        "occurrence": {
            "source_occurrence_id": resolution.source_occurrence_id,
            "scheduled_for": resolution.scheduled_for,
            "triggered_at": triggered_at,
        },
        "assessment": assessment,
    }

    try:
        normalized = normalize_breaking_radar_handoff(
            handoff, source=reviewed_source
        )
    except (BreakingRadarEdgeError, BreakingWorkflowInputError, ValueError) as exc:
        raise BreakingScanCommitError(f"handoff validation failed: {exc}") from exc

    external_id = normalized.trigger["external_occurrence_id"]
    scan_dir = _scan_dir(workspace_root, resolution.source_occurrence_id)
    target = scan_dir / f"{external_id}.json"
    if target.is_symlink():
        raise BreakingScanCommitError("handoff target is a symlink; refusing commit")
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BreakingScanCommitError(
                "existing handoff unreadable; refusing overwrite"
            ) from exc
        if existing != handoff:
            raise BreakingScanCommitError(
                "COMMIT_CONFLICT: a different handoff already occupies "
                "this candidate path"
            )

    atomic_write_json(target, handoff)

    existing_receipt = _read_receipt(_receipt_path(workspace_root, resolution.source_occurrence_id))
    if existing_receipt is not None and existing_receipt.get("status") == "NO_MATERIAL_DEVELOPMENT":
        raise BreakingScanCommitError(
            "COMMIT_CONFLICT: scan already recorded NO_MATERIAL_DEVELOPMENT"
        )
    known = (
        list(existing_receipt.get("candidates", []))
        if isinstance(existing_receipt, dict)
        else []
    )
    if external_id not in known:
        known.append(external_id)
    _write_receipt(
        workspace_root=workspace_root,
        source_occurrence_id=resolution.source_occurrence_id,
        scheduled_for=resolution.scheduled_for,
        source=reviewed_source,
        status="CANDIDATES_EMITTED",
        candidates=sorted(known),
    )

    return {
        "handoff_path": str(target.relative_to(workspace_root.resolve())),
        "external_occurrence_id": external_id,
        "source_occurrence_id": resolution.source_occurrence_id,
        "scheduled_for": resolution.scheduled_for,
    }


def record_empty_scan(
    *,
    source: str = "openclaw",
    at: str | None = None,
    workspace_root: Path = WORKSPACE,
) -> dict[str, Any]:
    """Record a truthful zero-candidate scan receipt (no handoff)."""

    reviewed_source = _require_m0_source(source)
    resolution = _resolve_due_scan(
        source=reviewed_source, at=at or _utc_now_canonical()
    )
    existing = _read_receipt(
        _receipt_path(workspace_root, resolution.source_occurrence_id)
    )
    if existing is not None:
        if existing.get("status") == "CANDIDATES_EMITTED" or existing.get("candidates"):
            raise BreakingScanCommitError(
                "scan already emitted candidates; cannot record empty"
            )
        return {"receipt_path": RECEIPT_FILENAME, "status": "NO_MATERIAL_DEVELOPMENT"}
    path = _write_receipt(
        workspace_root=workspace_root,
        source_occurrence_id=resolution.source_occurrence_id,
        scheduled_for=resolution.scheduled_for,
        source=reviewed_source,
        status="NO_MATERIAL_DEVELOPMENT",
        candidates=[],
    )
    return {
        "receipt_path": str(path.relative_to(workspace_root.resolve())),
        "status": "NO_MATERIAL_DEVELOPMENT",
    }


def self_test() -> int:
    import tempfile

    def assessment(candidate_id="acme-widget-launch", **overrides):
        base = {
            "schema": "nullone.breaking-workflow-input.v1",
            "contract_version": "1.0.0",
            "candidate_id": candidate_id,
            "candidate_version": "v1",
            "assessment_ref": "assessment:selftest:1",
            "state_snapshot_ref": "state:selftest:1",
            "topic": "Acme Widget launch",
            "topic_cluster": "acme-widget",
            "content_type": "BREAKING",
            "evidence": [
                {
                    "ref": "evidence:official:1",
                    "supported_claim": "Acme Widget 2 is available.",
                    "source_url": "https://example.invalid/widget-2",
                    "announcement_id": "acme-widget-2-launch",
                    "product": "Acme Widget",
                    "version": "2",
                    "region": None,
                    "availability_stage": "GENERAL_AVAILABILITY",
                    "number_value": None,
                    "number_unit": None,
                    "number_population": None,
                    "number_period": None,
                }
            ],
            "follow_up_delta": None,
            "source_attribution": "Acme official",
            "limitations": ["Launch region only."],
            "product_version_region": {
                "product": "Acme Widget",
                "version": "2",
                "region": None,
            },
            "source_image": None,
            "verification": {"state": "PASS", "evidence_refs": ["evidence:official:1"]},
            "severity_assessment": {
                "classification": "MATERIAL_BREAKING",
                "reason_text": "Launch timing is material.",
            },
            "recent_coverage": {
                "related_coverage_exists": False,
                "incremental_value_present": True,
                "assessment_ref": "coverage:selftest:1",
                "freshness_ref": "freshness:selftest:1",
            },
            "story_safety": {
                "quality_pass": True,
                "quality_ref": "quality:selftest:1",
                "dependencies_available": True,
                "dependencies_ref": "dependencies:selftest:1",
            },
            "main_assessment": None,
        }
        base.update(overrides)
        return base

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        at = "2026-09-08T07:35:00Z"  # inside the 11:30 Baku scan slot

        # 1. current-scan resolves the slot identity, no side effects.
        scan = current_scan(source="openclaw", at=at)
        assert scan["source_occurrence_id"] == (
            "breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z"
        ), scan
        assert not (root / "social/ops/breaking-handoffs").exists()

        # 2. Commit one valid candidate.
        staged = root / "staged.json"
        staged.write_text(json.dumps(assessment()), encoding="utf-8")
        committed = commit_assessment(
            assessment_path=staged, source="openclaw", at=at, workspace_root=root
        )
        target = root / committed["handoff_path"]
        assert target.is_file()
        stored = json.loads(target.read_text(encoding="utf-8"))
        assert stored["occurrence"]["source_occurrence_id"] == scan["source_occurrence_id"]
        assert stored["occurrence"]["triggered_at"] == at

        # 3. Identical recommit is idempotent.
        again = commit_assessment(
            assessment_path=staged, source="openclaw", at=at, workspace_root=root
        )
        assert again["handoff_path"] == committed["handoff_path"]

        # 4. Conflicting content for the same candidate path is rejected.
        staged.write_text(
            json.dumps(assessment(topic="Changed topic")), encoding="utf-8"
        )
        try:
            commit_assessment(
                assessment_path=staged, source="openclaw", at=at, workspace_root=root
            )
            raise AssertionError("conflicting recommit was not rejected")
        except BreakingScanCommitError:
            pass

        # 5. Second candidate, same scan -> same source, distinct external ID.
        staged.write_text(
            json.dumps(assessment(candidate_id="acme-widget-price")), encoding="utf-8"
        )
        second = commit_assessment(
            assessment_path=staged, source="openclaw", at=at, workspace_root=root
        )
        assert second["source_occurrence_id"] == committed["source_occurrence_id"]
        assert second["external_occurrence_id"] != committed["external_occurrence_id"]

        # 6. Bad candidate ID rejected before any write.
        staged.write_text(
            json.dumps(assessment(candidate_id="Rank 1!!!")), encoding="utf-8"
        )
        try:
            commit_assessment(
                assessment_path=staged, source="openclaw", at=at, workspace_root=root
            )
            raise AssertionError("bad candidate_id was not rejected")
        except BreakingScanCommitError:
            pass

        # 7. Commit outside any scan slot is rejected.
        try:
            commit_assessment(
                assessment_path=staged,
                source="openclaw",
                at="2026-09-08T06:00:00Z",
                workspace_root=root,
            )
            raise AssertionError("out-of-slot commit was not rejected")
        except BreakingScanCommitError:
            pass

        # 8. Empty scan on a fresh slot records truthful receipt.
        empty = record_empty_scan(
            source="openclaw", at="2026-09-08T10:35:00Z", workspace_root=root
        )
        assert empty["status"] == "NO_MATERIAL_DEVELOPMENT", empty

        # 9. Empty record after candidates were emitted is rejected.
        try:
            record_empty_scan(
                source="openclaw", at=at, workspace_root=root
            )
            raise AssertionError("contradictory empty record was not rejected")
        except BreakingScanCommitError:
            pass

    print("BREAKING_SCAN_COMMIT_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_MARKDOWN_READS=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Radar scan commit edge")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")
    sub.add_parser("current-scan").add_argument("--at", default=None)

    commit = sub.add_parser("commit")
    commit.add_argument("--assessment", required=True)
    commit.add_argument("--at", default=None)
    commit.add_argument("--source", default="openclaw")

    empty = sub.add_parser("record-empty")
    empty.add_argument("--at", default=None)
    empty.add_argument("--source", default="openclaw")

    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()
    if args.command == "current-scan":
        print(json.dumps(current_scan(at=args.at)))
        return 0
    if args.command == "commit":
        try:
            result = commit_assessment(
                assessment_path=args.assessment, source=args.source, at=args.at
            )
        except BreakingScanCommitError as exc:
            print(f"COMMIT_REJECTED: {exc}")
            return 1
        print(json.dumps(result))
        return 0
    if args.command == "record-empty":
        try:
            result = record_empty_scan(source=args.source, at=args.at)
        except BreakingScanCommitError as exc:
            print(f"COMMIT_REJECTED: {exc}")
            return 1
        print(json.dumps(result))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
