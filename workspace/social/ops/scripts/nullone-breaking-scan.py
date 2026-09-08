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

import fcntl
import json
import os
import sys
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

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
STAGING_SUBPATH = Path("social/ops/breaking-staging")
RECEIPT_FILENAME = "scan-receipt.json"
SCAN_LOCK_FILENAME = "scan.lock"

RECEIPT_SCHEMA = "nullone.breaking-radar-scan-receipt.v1"
RECEIPT_CONTRACT_VERSION = "1.0.0"
RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "contract_version",
        "source_occurrence_id",
        "scheduled_for",
        "source",
        "status",
        "candidates",
        "created_at",
    }
)
RECEIPT_STATUSES = frozenset({"NO_MATERIAL_DEVELOPMENT", "CANDIDATES_EMITTED"})

_EXTERNAL_ID_RE = re.compile(r"^breaking-candidate-[0-9a-f]{24}$")

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


def validate_scan_receipt(
    data: Any, *, source_occurrence_id: str, scheduled_for: str
) -> dict[str, Any]:
    """Strictly validate a scan receipt against its scan identity.

    Exact field set, exact schema/version, supported source, exact scan
    IDs, canonical timestamp shape, exact status vocabulary, duplicate-free
    candidate list with expected external-ID shape, and the status/
    candidates semantic invariants. Anything else fails closed -- garbage
    is never reinterpreted as an empty scan.
    """

    from nullone_scheduler_invocation import TIMESTAMP_RE

    if not isinstance(data, dict):
        raise BreakingScanCommitError("scan receipt must be a JSON object")
    actual = set(data)
    if actual != RECEIPT_FIELDS:
        raise BreakingScanCommitError(
            "scan receipt field set mismatch "
            f"(missing={sorted(RECEIPT_FIELDS - actual)}, "
            f"unknown={sorted(actual - RECEIPT_FIELDS)})"
        )
    if data["schema"] != RECEIPT_SCHEMA:
        raise BreakingScanCommitError("scan receipt has wrong schema")
    if data["contract_version"] != RECEIPT_CONTRACT_VERSION:
        raise BreakingScanCommitError("scan receipt has wrong contract_version")
    if data["source_occurrence_id"] != source_occurrence_id:
        raise BreakingScanCommitError("scan receipt names a different scan")
    if data["scheduled_for"] != scheduled_for:
        raise BreakingScanCommitError("scan receipt names a different slot")
    if data["source"] not in M0_SCAN_SOURCES:
        raise BreakingScanCommitError("scan receipt names an unsupported source")
    if not isinstance(data["created_at"], str) or not TIMESTAMP_RE.fullmatch(
        data["created_at"]
    ):
        raise BreakingScanCommitError("scan receipt has malformed created_at")
    if data["status"] not in RECEIPT_STATUSES:
        raise BreakingScanCommitError("scan receipt has unknown status")
    candidates = data["candidates"]
    if not isinstance(candidates, list) or any(
        not isinstance(c, str) for c in candidates
    ):
        raise BreakingScanCommitError("scan receipt candidates must be a list[str]")
    if len(candidates) != len(set(candidates)):
        raise BreakingScanCommitError("scan receipt candidates must be duplicate-free")
    for candidate in candidates:
        if not _EXTERNAL_ID_RE.fullmatch(candidate):
            raise BreakingScanCommitError(
                "scan receipt candidate has unexpected external-ID shape"
            )
    if data["status"] == "NO_MATERIAL_DEVELOPMENT" and candidates:
        raise BreakingScanCommitError(
            "scan receipt contradicts itself: empty status with candidates"
        )
    if data["status"] == "CANDIDATES_EMITTED" and not candidates:
        raise BreakingScanCommitError(
            "scan receipt contradicts itself: emitted status without candidates"
        )
    return data


@contextmanager
def _scan_locked(
    workspace_root: Path, source_occurrence_id: str
) -> Iterator[Path]:
    """Serialize all commit-edge mutations for one scan (fcntl, not local)."""

    scan_dir = workspace_root / HANDOFFS_SUBPATH / source_occurrence_id
    scan_dir.mkdir(parents=True, exist_ok=True)
    lock_path = scan_dir / SCAN_LOCK_FILENAME
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield scan_dir
    finally:
        os.close(lock_fd)


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
    path = (
        workspace_root / HANDOFFS_SUBPATH / source_occurrence_id / RECEIPT_FILENAME
    )
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


def _resolve_staged_path(workspace_root: Path, assessment_path: str | Path) -> Path:
    """Contain the staged assessment inside the canonical staging root.

    The commit edge never reads an arbitrary host file: the path must
    resolve inside `<workspace>/social/ops/breaking-staging`, be a
    regular non-symlink file, and carry a `.json` suffix.
    """

    root = workspace_root.resolve()
    staging = root / STAGING_SUBPATH
    # Build the candidate path without resolving symlinks yet
    if Path(str(assessment_path)).is_absolute():
        candidate = Path(str(assessment_path))
    else:
        candidate = staging / str(assessment_path)
    # Check for symlink now, before resolving
    if candidate.is_symlink():
        raise BreakingScanCommitError("staged assessment must be a regular file, not a symlink")
    # Now resolve to get the absolute path and to check if it's inside the staging root
    candidate = candidate.resolve()
    try:
        candidate.relative_to(staging.resolve())
    except (ValueError, OSError) as exc:
        raise BreakingScanCommitError(
            "staged assessment must live inside the canonical staging root"
        ) from exc
    if not candidate.is_file():
        raise BreakingScanCommitError(
            "staged assessment must be a regular file, not a symlink"
        )
    if candidate.suffix != ".json":
        raise BreakingScanCommitError("staged assessment must be a JSON file")
    return candidate


def commit_assessment(
    *,
    assessment_path: str | Path,
    source: str = "openclaw",
    at: str | None = None,
    workspace_root: Path = WORKSPACE,
) -> dict[str, Any]:
    """Validate, stamp, and atomically commit one staged assessment.

    Commit authority rule: the candidate handoff may be written first,
    but the scan receipt is the COMMIT RECORD -- only candidates listed
    in a valid CANDIDATES_EMITTED receipt are consumable, so a crash
    between the two writes leaves an orphan that a recommit repairs.

    Everything mutating happens inside the per-scan fcntl lock: receipt
    state is re-read and strictly validated there, conflicts and
    idempotency are decided there, and only then are the handoff and
    receipt written. In particular a NO_MATERIAL_DEVELOPMENT receipt is
    checked BEFORE any handoff write, so a rejected candidate is never
    left durably committed.

    Returns the committed handoff path, candidate external occurrence ID,
    and scan identity. Identical recommit is idempotent (and repairs a
    missing receipt listing); conflicting content for the same candidate
    path is rejected (COMMIT_CONFLICT).
    """

    reviewed_source = _require_m0_source(source)
    triggered_at = at or _utc_now_canonical()
    resolution = _resolve_due_scan(source=reviewed_source, at=triggered_at)
    root = workspace_root.resolve()
    staged = _resolve_staged_path(root, assessment_path)
    try:
        assessment = json.loads(staged.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BreakingScanCommitError(
            f"staged assessment unreadable: {staged.name}"
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

    with _scan_locked(root, resolution.source_occurrence_id) as scan_dir:
        receipt_path = scan_dir / RECEIPT_FILENAME
        existing_receipt: dict[str, Any] | None = None
        if receipt_path.is_file() or receipt_path.is_symlink():
            if receipt_path.is_symlink():
                raise BreakingScanCommitError("scan receipt is a symlink")
            try:
                existing_receipt = validate_scan_receipt(
                    json.loads(receipt_path.read_text(encoding="utf-8")),
                    source_occurrence_id=resolution.source_occurrence_id,
                    scheduled_for=resolution.scheduled_for,
                )
            except (OSError, ValueError) as exc:
                raise BreakingScanCommitError(
                    "existing scan receipt is unreadable"
                ) from exc

        if (
            existing_receipt is not None
            and existing_receipt["status"] == "NO_MATERIAL_DEVELOPMENT"
        ):
            # Checked BEFORE any handoff write: a rejected candidate is
            # never left durably committed.
            raise BreakingScanCommitError(
                "COMMIT_CONFLICT: scan already recorded NO_MATERIAL_DEVELOPMENT"
            )

        target = scan_dir / f"{external_id}.json"
        if target.is_symlink():
            raise BreakingScanCommitError("handoff target is a symlink; refusing commit")
        if target.is_file():
            try:
                existing_handoff = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise BreakingScanCommitError(
                    "existing handoff unreadable; refusing overwrite"
                ) from exc
            if existing_handoff != handoff:
                raise BreakingScanCommitError(
                    "COMMIT_CONFLICT: a different handoff already occupies "
                    "this candidate path"
                )

        atomic_write_json(target, handoff)

        known = (
            list(existing_receipt["candidates"])
            if isinstance(existing_receipt, dict)
            else []
        )
        if external_id not in known:
            known.append(external_id)
        _write_receipt(
            workspace_root=root,
            source_occurrence_id=resolution.source_occurrence_id,
            scheduled_for=resolution.scheduled_for,
            source=reviewed_source,
            status="CANDIDATES_EMITTED",
            candidates=sorted(known),
        )

    return {
        "handoff_path": str(target.relative_to(root)),
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
    """Record a truthful zero-candidate scan receipt (no handoff).

    Serialized with candidate commits under the same per-scan lock: a
    concurrent candidate commit and an empty record cannot both win --
    exactly one logical outcome survives, and a contradictory state is
    rejected rather than merged. A malformed existing receipt fails
    closed (never reinterpreted as an empty scan).
    """

    reviewed_source = _require_m0_source(source)
    resolution = _resolve_due_scan(
        source=reviewed_source, at=at or _utc_now_canonical()
    )
    root = workspace_root.resolve()
    with _scan_locked(root, resolution.source_occurrence_id) as scan_dir:
        receipt_path = scan_dir / RECEIPT_FILENAME
        if receipt_path.is_symlink():
            raise BreakingScanCommitError("scan receipt is a symlink")
        if receipt_path.is_file():
            try:
                existing = validate_scan_receipt(
                    json.loads(receipt_path.read_text(encoding="utf-8")),
                    source_occurrence_id=resolution.source_occurrence_id,
                    scheduled_for=resolution.scheduled_for,
                )
            except (OSError, ValueError) as exc:
                raise BreakingScanCommitError(
                    "existing scan receipt is unreadable"
                ) from exc
            if existing["status"] == "CANDIDATES_EMITTED":
                raise BreakingScanCommitError(
                    "scan already emitted candidates; cannot record empty"
                )
            return {
                "receipt_path": str(receipt_path.relative_to(root)),
                "status": "NO_MATERIAL_DEVELOPMENT",
            }
        path = _write_receipt(
            workspace_root=root,
            source_occurrence_id=resolution.source_occurrence_id,
            scheduled_for=resolution.scheduled_for,
            source=reviewed_source,
            status="NO_MATERIAL_DEVELOPMENT",
            candidates=[],
        )
        return {
            "receipt_path": str(path.relative_to(root)),
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
        staging = root / "social/ops/breaking-staging"
        staging.mkdir(parents=True, exist_ok=True)
        at = "2026-09-08T07:35:00Z"  # inside the 11:30 Baku scan slot

        # 1. current-scan resolves the slot identity, no side effects.
        scan = current_scan(source="openclaw", at=at)
        assert scan["source_occurrence_id"] == (
            "breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z"
        ), scan
        assert not (root / "social/ops/breaking-handoffs").exists()

        # 2. Commit one valid candidate.
        staged = staging / "staged.json"
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

        # 10. NO_MATERIAL receipt then candidate commit: rejected BEFORE
        #     any handoff write (never a partial commit).
        scan_dir = (
            root
            / "social/ops/breaking-handoffs"
            / "breaking-radar.scan-1430.v1@2026-09-08T10:30:00Z"
        )
        record_empty_scan(
            source="openclaw", at="2026-09-08T10:35:00Z", workspace_root=root
        )
        staged.write_text(json.dumps(assessment()), encoding="utf-8")
        try:
            commit_assessment(
                assessment_path=staged,
                source="openclaw",
                at="2026-09-08T10:35:00Z",
                workspace_root=root,
            )
            raise AssertionError("post-empty candidate commit was not rejected")
        except BreakingScanCommitError:
            pass
        leftovers = [
            p for p in scan_dir.iterdir() if p.suffix == ".json" and p.name != "scan-receipt.json"
        ]
        assert leftovers == [], leftovers

        # 11. Concurrent A/B commits: both land, receipt lists both once.
        import threading

        def _commit(candidate_id: str) -> None:
            path = staging / f"{candidate_id}.json"
            path.write_text(json.dumps(assessment(candidate_id)), encoding="utf-8")
            commit_assessment(
                assessment_path=path,
                source="openclaw",
                at="2026-09-08T13:35:00Z",
                workspace_root=root,
            )

        threads = [
            threading.Thread(target=_commit, args=(cid,))
            for cid in ("concurrent-alpha-x", "concurrent-beta-y")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        receipt = json.loads(
            (
                root
                / "social/ops/breaking-handoffs"
                / "breaking-radar.scan-1730.v1@2026-09-08T13:30:00Z"
                / "scan-receipt.json"
            ).read_text(encoding="utf-8")
        )
        assert receipt["status"] == "CANDIDATES_EMITTED", receipt
        assert len(receipt["candidates"]) == 2, receipt
        assert len(set(receipt["candidates"])) == 2

        # 12. Staged path outside the canonical root is rejected.
        outside = root / "elsewhere.json"
        outside.write_text(json.dumps(assessment()), encoding="utf-8")
        try:
            commit_assessment(
                assessment_path=outside,
                source="openclaw",
                at=at,
                workspace_root=root,
            )
            raise AssertionError("outside-staging path was not rejected")
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
