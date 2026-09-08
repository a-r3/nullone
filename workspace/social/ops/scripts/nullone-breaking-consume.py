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
identity, source, status, candidates) AND proven against the real
`breaking-radar` ScheduleSpec registry. NO_MATERIAL_DEVELOPMENT executes
zero handoffs (any handoff JSON is an authoritative inconsistency).
CANDIDATES_EMITTED consumes only the exact listed external occurrence IDs.

Authoritative commit-record corruption (missing/corrupt/contradictory
receipt or listed-candidate integrity failure) fails the entire sweep
with a stable generic reason and non-zero CLI so scheduler-native
failureAlert can own it. Unlisted extra junk is never executable and may
remain a non-authoritative skipped input without granting false
authority. Unexpected runtime exceptions likewise fail the sweep; raw
exception text is never exposed.

Desired production shape (DESIRED / NOT DEPLOYED): a static OpenClaw
command job shortly after each Radar slot, e.g.
`45 11,14,17,20,23 * * * Asia/Baku`, running exactly:

    python3 <repo>/workspace/social/ops/scripts/nullone-breaking-consume.py sweep

No arguments, no dynamic interpolation, no secrets.
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
from nullone_breaking_scan_authority import (
    BreakingScanAuthorityError,
    validate_committed_scan_identity,
)
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

# Authoritative commit-record defects: impossible/unsafe after a valid
# commit; fail the sweep (non-zero) so production cannot stick silently.
AUTHORITATIVE_CORRUPTION_CODES = frozenset(
    {
        "RECEIPT_MISSING",
        "RECEIPT_REJECTED",
        "RECEIPT_SOURCE_UNSUPPORTED",
        "RECEIPT_IDENTITY_MISMATCH",
        "RECEIPT_INCONSISTENT",
        "CANDIDATE_FILE_MISSING",
        "CANDIDATE_PATH_REJECTED",
        "UNREADABLE",
        "NOT_A_HANDOFF",
        "FILENAME_CONTENT_MISMATCH",
        "HANDOFF_REJECTED",
        "CANDIDATE_ID_REJECTED",
    }
)

# Non-authoritative directory noise: never executable, never fails sweep.
NON_AUTHORITATIVE_SKIP_CODES = frozenset({"CANDIDATE_NOT_LISTED"})

SWEEP_RUNTIME_FAILED = "SWEEP_RUNTIME_FAILED"
SWEEP_ESTABLISHMENT_FAILED = "SWEEP_ESTABLISHMENT_FAILED"
SWEEP_AUTHORITY_CORRUPT = "SWEEP_AUTHORITY_CORRUPT"
SWEEP_OK = "OK"

SWEEP_AUTHORITY_CORRUPT_TEXT = (
    "Breaking handoff sweep failed: authoritative spool record is "
    "missing, corrupt, or inconsistent."
)
SWEEP_ESTABLISHMENT_FAILED_TEXT = (
    "Breaking handoff sweep failed: runner could not establish a safe "
    "domain outcome."
)
SWEEP_RUNTIME_FAILED_TEXT = (
    "Breaking handoff sweep failed on an unexpected runtime error."
)


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


def _authority_fail(
    report: dict[str, Any], *, path: str, reason: str
) -> dict[str, Any]:
    report["establishment_failed"].append({"path": path, "reason": reason})
    return _fail_sweep(
        report,
        reason_code=SWEEP_AUTHORITY_CORRUPT,
        reason_text=SWEEP_AUTHORITY_CORRUPT_TEXT,
    )


def _load_authoritative_receipt(
    scan_dir: Path, *, root: Path
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (receipt, failure_entry). Exactly one outcome is non-None.

    failure_entry.reason is always an AUTHORITATIVE_CORRUPTION code.
    """

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
    if not isinstance(data, dict) or "scheduled_for" not in data:
        return None, {"path": rel, "reason": "RECEIPT_REJECTED"}
    scheduled_for = data.get("scheduled_for")
    if not isinstance(scheduled_for, str):
        return None, {"path": rel, "reason": "RECEIPT_REJECTED"}

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

    try:
        validate_committed_scan_identity(
            source=str(receipt["source"]),
            scheduled_for=str(receipt["scheduled_for"]),
            source_occurrence_id=str(receipt["source_occurrence_id"]),
            scan_directory_name=scan_dir.name,
        )
    except BreakingScanAuthorityError:
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
        receipt, failure = _load_authoritative_receipt(scan_dir, root=root)
        if failure is not None:
            return _authority_fail(
                report, path=failure["path"], reason=failure["reason"]
            )

        assert receipt is not None
        source = str(receipt["source"])

        if receipt["status"] == "NO_MATERIAL_DEVELOPMENT":
            leftovers = _handoff_json_paths(scan_dir)
            if leftovers:
                return _authority_fail(
                    report,
                    path=str(leftovers[0].relative_to(root)),
                    reason="RECEIPT_INCONSISTENT",
                )
            continue

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
            expected = scan_dir / f"{external_id}.json"
            rel = str(expected.relative_to(root))
            if expected.is_symlink():
                return _authority_fail(
                    report, path=rel, reason="CANDIDATE_PATH_REJECTED"
                )
            if not expected.is_file():
                return _authority_fail(
                    report, path=rel, reason="CANDIDATE_FILE_MISSING"
                )

            try:
                handoff = json.loads(expected.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return _authority_fail(report, path=rel, reason="UNREADABLE")
            if not isinstance(handoff, dict) or handoff.get("schema") != HANDOFF_SCHEMA:
                return _authority_fail(report, path=rel, reason="NOT_A_HANDOFF")
            expected_name = _expected_filename(handoff)
            if expected_name is not None and expected.name != expected_name:
                return _authority_fail(
                    report, path=rel, reason="FILENAME_CONTENT_MISMATCH"
                )

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
                return _fail_sweep(
                    report,
                    reason_code=SWEEP_RUNTIME_FAILED,
                    reason_text=SWEEP_RUNTIME_FAILED_TEXT,
                )

            if result.application_execution == "FAILED" and result.reason_code in (
                "HANDOFF_REJECTED",
                "CANDIDATE_ID_REJECTED",
            ):
                return _authority_fail(
                    report, path=rel, reason=result.reason_code
                )

            if (
                result.application_execution == "FAILED"
                and result.reason_code in ESTABLISHMENT_FAILURE_CODES
            ):
                report["establishment_failed"].append(
                    {
                        "path": rel,
                        "reason": result.reason_code,
                        "candidate_id": result.candidate_id,
                        "run_id": result.run_id,
                    }
                )
                return _fail_sweep(
                    report,
                    reason_code=SWEEP_ESTABLISHMENT_FAILED,
                    reason_text=SWEEP_ESTABLISHMENT_FAILED_TEXT,
                )

            report["processed"].append(
                {
                    "path": rel,
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
    assert AUTHORITATIVE_CORRUPTION_CODES
    assert NON_AUTHORITATIVE_SKIP_CODES == frozenset({"CANDIDATE_NOT_LISTED"})

    # Registry-backed identity: reviewed slot accepted; fake rejected.
    validate_committed_scan_identity(
        source="openclaw",
        scheduled_for="2026-09-08T07:30:00Z",
        source_occurrence_id="breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z",
        scan_directory_name="breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z",
    )
    try:
        validate_committed_scan_identity(
            source="openclaw",
            scheduled_for="2026-09-08T07:30:00Z",
            source_occurrence_id="breaking-radar.fake-slot.v1@2026-09-08T07:30:00Z",
            scan_directory_name="breaking-radar.fake-slot.v1@2026-09-08T07:30:00Z",
        )
        raise AssertionError("fake scan identity was accepted")
    except BreakingScanAuthorityError:
        pass

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
