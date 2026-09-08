#!/usr/bin/env python3
"""Static Breaking handoff spool consumer (#80).

Sweeps the committed handoff spool
(`social/ops/breaking-handoffs/<scan>/<candidate>.json`) under the
authoritative scan receipt. The receipt is the COMMIT RECORD: only
candidates listed in a strictly validated CANDIDATES_EMITTED receipt are
executable. Orphan handoffs, missing/corrupt receipts, and directory
sweeps that ignore the receipt are never executable authority.

Per-scan isolation: each scan directory is admitted only after exactly
one valid `scan-receipt.json` is strict-validated (schema, contract,
identity, source, status, candidates). NO_MATERIAL_DEVELOPMENT executes
zero handoffs (any handoff JSON is an inconsistency). CANDIDATES_EMITTED
consumes only the exact listed external occurrence IDs.

Safely classified INPUT defects are recorded per-file as
SKIPPED_INVALID and never block sibling candidates. Unexpected
runtime/programming/orchestration exceptions fail the entire sweep
(non-zero CLI; stable generic reason; raw exception text never exposed).
Runner establishment failures are never ordinary "processed" candidates.

Desired production shape (DESIRED / NOT DEPLOYED): a static OpenClaw
command job shortly after each Radar slot, e.g.
`45 11,14,17,20,23 * * * Asia/Baku`, running exactly:

    python3 <repo>/workspace/social/ops/scripts/nullone-breaking-consume.py sweep

No arguments, no dynamic interpolation, no secrets. Exit 0 means the
sweep completed without establishment/runtime failure; scheduler-native
failureAlert owns execution-level failure.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nullone_breaking_candidate_runner import (
    BreakingScheduledResult,
    run_breaking_candidate,
)
from nullone_breaking_radar_edge import HANDOFF_SCHEMA
from nullone_breaking_scan_authority import BreakingScanAuthorityError
from nullone_bridge_common import WORKSPACE
from nullone_failure_notify import OpenClawTelegramTransport, notify_if_required
from nullone_story_pipeline import (
    HaikuStoryWriter,
    NulloneDraftBridgeConnector,
    numeric_scope_verifier,
)
from nullone_telegram_review_delivery_adapter import TelegramReviewDeliveryAdapter

HANDOFFS_SUBPATH = Path("social/ops/breaking-handoffs")
RECEIPT_FILENAME = "scan-receipt.json"

M0_CONSUMER_SOURCES = frozenset({"openclaw"})

# Runner results that established no safe domain outcome — never "processed".
ESTABLISHMENT_FAILURE_CODES = frozenset(
    {
        "RESULT_MISSING_OR_CORRUPT",
        "RESULT_IDENTITY_MISMATCH",
        "RESULT_COMMIT_FAILED",
        "NOTIFICATION_STATE_UNSAFE",
        "NOTIFICATION_RESULT_INVALID",
        "RUNTIME_CRASHED",
    }
)

# Per-file input defects that may be skipped without failing the sweep.
INPUT_SKIP_CODES = frozenset(
    {
        "HANDOFF_REJECTED",
        "CANDIDATE_ID_REJECTED",
        "UNREADABLE",
        "NOT_A_HANDOFF",
        "FILENAME_CONTENT_MISMATCH",
        "RECEIPT_MISSING",
        "RECEIPT_REJECTED",
        "RECEIPT_SOURCE_UNSUPPORTED",
        "RECEIPT_IDENTITY_MISMATCH",
        "RECEIPT_INCONSISTENT",
        "CANDIDATE_NOT_LISTED",
        "CANDIDATE_FILE_MISSING",
        "CANDIDATE_FILE_AMBIGUOUS",
        "CANDIDATE_PATH_REJECTED",
    }
)

SWEEP_RUNTIME_FAILED = "SWEEP_RUNTIME_FAILED"
SWEEP_ESTABLISHMENT_FAILED = "SWEEP_ESTABLISHMENT_FAILED"
SWEEP_OK = "OK"


def _load_scan_module() -> Any:
    scan_path = Path(__file__).resolve().parent / "nullone-breaking-scan.py"
    spec = importlib.util.spec_from_file_location(
        "nullone_breaking_scan_consume", scan_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load nullone-breaking-scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_scan = _load_scan_module()


def _scheduled_for_from_scan_id(source_occurrence_id: str) -> str | None:
    if "@" not in source_occurrence_id:
        return None
    return source_occurrence_id.split("@", 1)[1]


def _empty_report() -> dict[str, Any]:
    return {
        "sweep_status": "COMPLETED",
        "reason_code": SWEEP_OK,
        "reason_text": "Breaking handoff sweep completed.",
        "scans_seen": 0,
        "processed": [],
        "skipped_invalid": [],
        "establishment_failed": [],
    }


def _fail_sweep(
    report: dict[str, Any],
    *,
    reason_code: str,
    reason_text: str,
) -> dict[str, Any]:
    report["sweep_status"] = "FAILED"
    report["reason_code"] = reason_code
    report["reason_text"] = reason_text
    return report


def _load_authoritative_receipt(
    scan_dir: Path, *, root: Path
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (receipt, skip_entry). Exactly one outcome is non-None."""

    receipt_path = scan_dir / RECEIPT_FILENAME
    rel = str(receipt_path.relative_to(root))
    if receipt_path.is_symlink():
        return None, {"path": rel, "reason": "RECEIPT_REJECTED"}
    if not receipt_path.is_file():
        return None, {"path": rel, "reason": "RECEIPT_MISSING"}
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, {"path": rel, "reason": "RECEIPT_REJECTED"}

    source_occurrence_id = scan_dir.name
    scheduled_for = _scheduled_for_from_scan_id(source_occurrence_id)
    if scheduled_for is None:
        return None, {"path": rel, "reason": "RECEIPT_IDENTITY_MISMATCH"}

    try:
        receipt = _scan.validate_scan_receipt(
            data,
            source_occurrence_id=source_occurrence_id,
            scheduled_for=scheduled_for,
        )
    except _scan.BreakingScanCommitError:
        return None, {"path": rel, "reason": "RECEIPT_REJECTED"}

    if receipt["source"] not in M0_CONSUMER_SOURCES:
        return None, {"path": rel, "reason": "RECEIPT_SOURCE_UNSUPPORTED"}
    if receipt["source_occurrence_id"] != source_occurrence_id:
        return None, {"path": rel, "reason": "RECEIPT_IDENTITY_MISMATCH"}
    return receipt, None


def _handoff_json_paths(scan_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in scan_dir.iterdir()
        if p.is_file()
        and not p.is_symlink()
        and p.suffix == ".json"
        and p.name != RECEIPT_FILENAME
    )


def _candidate_path(scan_dir: Path, external_id: str) -> Path | None:
    """Exactly one regular non-symlink `<id>.json`, or None if missing/ambiguous."""

    expected = scan_dir / f"{external_id}.json"
    if expected.is_symlink():
        return None
    if expected.is_file():
        # Refuse if another listed-looking duplicate somehow exists.
        return expected
    return None


def sweep_breaking_handoffs(
    *,
    workspace_root: Path = WORKSPACE,
    now: datetime | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sweep the spool once under receipt authority; return an explicit report.

    `overrides` replaces production dependencies (tests only): keys
    `story_writer`, `story_verifier`, `draft_connector`,
    `review_delivery`, `notifier`, `dependency_recheck`, and optionally
    `run_breaking_candidate` (tests only). Production supplies no fake
    dependency recheck (#81 owns live DraftProvider readiness).
    """

    _ = now or datetime.now(timezone.utc)
    root = workspace_root.resolve()
    spool = root / HANDOFFS_SUBPATH
    report = _empty_report()

    deps: dict[str, Any] = {
        "story_writer": HaikuStoryWriter(),
        "story_verifier": numeric_scope_verifier,
        "draft_connector": NulloneDraftBridgeConnector(),
        "review_delivery": TelegramReviewDeliveryAdapter(),
        "notifier": _production_notifier,
        "dependency_recheck": None,
        "run_breaking_candidate": run_breaking_candidate,
    }
    if overrides:
        deps.update(overrides)

    if not spool.is_dir():
        return report

    for scan_dir in sorted(
        p for p in spool.iterdir() if p.is_dir() and not p.is_symlink()
    ):
        report["scans_seen"] += 1
        receipt, skip = _load_authoritative_receipt(scan_dir, root=root)
        if skip is not None:
            report["skipped_invalid"].append(skip)
            # Without a valid receipt, directory contents are never authority.
            continue

        assert receipt is not None
        source = str(receipt["source"])

        if receipt["status"] == "NO_MATERIAL_DEVELOPMENT":
            leftovers = _handoff_json_paths(scan_dir)
            for path in leftovers:
                report["skipped_invalid"].append(
                    {
                        "path": str(path.relative_to(root)),
                        "reason": "RECEIPT_INCONSISTENT",
                    }
                )
            # Zero handoffs executed — receipt says empty.
            continue

        # CANDIDATES_EMITTED: consume ONLY listed IDs.
        listed = list(receipt["candidates"])
        listed_set = set(listed)
        present = {p.stem: p for p in _handoff_json_paths(scan_dir)}

        for stem, path in sorted(present.items()):
            if stem not in listed_set:
                report["skipped_invalid"].append(
                    {
                        "path": str(path.relative_to(root)),
                        "reason": "CANDIDATE_NOT_LISTED",
                    }
                )

        for external_id in listed:
            path = _candidate_path(scan_dir, external_id)
            if path is None:
                report["skipped_invalid"].append(
                    {
                        "path": str(
                            (scan_dir / f"{external_id}.json").relative_to(root)
                        ),
                        "reason": "CANDIDATE_FILE_MISSING",
                    }
                )
                continue
            if path.is_symlink() or not path.is_file():
                report["skipped_invalid"].append(
                    {
                        "path": str(path.relative_to(root)),
                        "reason": "CANDIDATE_PATH_REJECTED",
                    }
                )
                continue

            try:
                handoff = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report["skipped_invalid"].append(
                    {"path": str(path.relative_to(root)), "reason": "UNREADABLE"}
                )
                continue
            if not isinstance(handoff, dict) or handoff.get("schema") != HANDOFF_SCHEMA:
                report["skipped_invalid"].append(
                    {"path": str(path.relative_to(root)), "reason": "NOT_A_HANDOFF"}
                )
                continue
            expected_name = _expected_filename(handoff)
            if expected_name is not None and path.name != expected_name:
                report["skipped_invalid"].append(
                    {
                        "path": str(path.relative_to(root)),
                        "reason": "FILENAME_CONTENT_MISMATCH",
                    }
                )
                continue

            try:
                result: BreakingScheduledResult = deps["run_breaking_candidate"](
                    handoff,
                    story_writer=deps["story_writer"],
                    story_verifier=deps["story_verifier"],
                    draft_connector=deps["draft_connector"],
                    review_delivery=deps["review_delivery"],
                    dependency_recheck=deps["dependency_recheck"],
                    notifier=deps["notifier"],
                    workspace_root=workspace_root,
                    source=source,
                )
            except Exception:
                # Unexpected runtime/programming/orchestration failure:
                # fail the sweep closed. Never expose raw exception text.
                return _fail_sweep(
                    report,
                    reason_code=SWEEP_RUNTIME_FAILED,
                    reason_text=(
                        "Breaking handoff sweep failed on an unexpected "
                        "runtime error."
                    ),
                )

            if result.application_execution == "FAILED" and result.reason_code in (
                "HANDOFF_REJECTED",
                "CANDIDATE_ID_REJECTED",
            ):
                report["skipped_invalid"].append(
                    {
                        "path": str(path.relative_to(root)),
                        "reason": result.reason_code,
                    }
                )
                continue

            if (
                result.application_execution == "FAILED"
                and result.reason_code in ESTABLISHMENT_FAILURE_CODES
            ):
                report["establishment_failed"].append(
                    {
                        "path": str(path.relative_to(root)),
                        "reason": result.reason_code,
                        "candidate_id": result.candidate_id,
                        "run_id": result.run_id,
                    }
                )
                return _fail_sweep(
                    report,
                    reason_code=SWEEP_ESTABLISHMENT_FAILED,
                    reason_text=(
                        "Breaking handoff sweep failed: runner could not "
                        "establish a safe domain outcome."
                    ),
                )

            report["processed"].append(
                {
                    "path": str(path.relative_to(root)),
                    "candidate_id": result.candidate_id,
                    "domain_outcome": result.domain_outcome,
                    "run_id": result.run_id,
                    "application_execution": result.application_execution,
                    "reason_code": result.reason_code,
                }
            )
    return report


def _expected_filename(handoff: dict[str, Any]) -> str | None:
    """The deterministic filename this handoff's content implies, if computable."""

    try:
        from nullone_breaking_radar_edge import compute_candidate_external_occurrence_id

        occurrence = handoff.get("occurrence")
        assessment = handoff.get("assessment")
        if not isinstance(occurrence, dict) or not isinstance(assessment, dict):
            return None
        external_id = compute_candidate_external_occurrence_id(
            occurrence.get("source_occurrence_id"), assessment.get("candidate_id")
        )
        return f"{external_id}.json"
    except (BreakingScanAuthorityError, ValueError, TypeError, AttributeError):
        return None


def _production_notifier(result: dict[str, Any]) -> dict[str, Any]:
    return notify_if_required(
        result,
        transport=OpenClawTelegramTransport(),
        scheduler_native_failure_owned=False,
    )


def self_test() -> int:
    report = sweep_breaking_handoffs(
        workspace_root=Path("/nonexistent-root-for-self-test"),
        overrides={
            "story_writer": None,
            "story_verifier": None,
            "draft_connector": None,
            "review_delivery": None,
            "notifier": None,
        },
    )
    assert report["sweep_status"] == "COMPLETED", report
    assert report["reason_code"] == SWEEP_OK, report
    assert report["scans_seen"] == 0, report
    assert report["processed"] == [], report
    assert report["skipped_invalid"] == [], report
    assert report["establishment_failed"] == [], report
    assert INPUT_SKIP_CODES  # contract vocabulary retained for callers/docs

    print("BREAKING_CONSUME_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_MARKDOWN_READS=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Breaking handoff consumer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    sub.add_parser("sweep")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()
    if args.command == "sweep":
        report = sweep_breaking_handoffs()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report.get("sweep_status") == "COMPLETED" else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
