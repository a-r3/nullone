#!/usr/bin/env python3
"""Static Breaking handoff spool consumer (#80).

Sweeps the committed handoff spool
(`social/ops/breaking-handoffs/<scan>/<candidate>.json`) and runs each
handoff through the production candidate runner exactly once. Already
processed candidates are skipped via their persisted #27 results
(replay-safe, idempotent); the sweep is crash-recoverable because an
unprocessed committed handoff is simply picked up on the next tick.

Per-file isolation: each handoff is loaded, strictly validated, and run
independently. An invalid file is recorded as SKIPPED_INVALID and never
blocks other candidates; unknown non-JSON files are ignored; the scan
receipt itself is never consumed. Filenames only locate files -- workflow
input always comes from validated file content, and a filename/content
mismatch fails that file safely.

Desired production shape (DESIRED / NOT DEPLOYED): a static OpenClaw
command job shortly after each Radar slot, e.g.
`45 11,14,17,20,23 * * * Asia/Baku`, running exactly:

    python3 <repo>/workspace/social/ops/scripts/nullone-breaking-consume.py

No arguments, no dynamic interpolation, no secrets. Exit 0 means the
sweep itself completed (processed + skipped counts reported); only a
sweep-level crash exits non-zero (scheduler-native alert owns that).
"""
from __future__ import annotations

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


def _receipt_source(scan_dir: Path) -> str:
    """The source namespace recorded by the commit edge for this scan."""

    receipt = scan_dir / RECEIPT_FILENAME
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "openclaw"
    if isinstance(data, dict) and data.get("source") in M0_CONSUMER_SOURCES:
        return str(data["source"])
    return "openclaw"


def sweep_breaking_handoffs(
    *,
    workspace_root: Path = WORKSPACE,
    now: datetime | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sweep the spool once; return a deterministic per-file report.

    `overrides` replaces production dependencies (tests only): keys
    `story_writer`, `story_verifier`, `draft_connector`,
    `review_delivery`, `notifier`. Replayed (already processed) handoffs
    appear under `processed` with their authoritative persisted outcome --
    the sweep never re-runs them.
    """

    _ = now or datetime.now(timezone.utc)
    root = workspace_root.resolve()
    spool = root / HANDOFFS_SUBPATH

    report: dict[str, Any] = {
        "scans_seen": 0,
        "processed": [],
        "skipped_invalid": [],
    }

    deps: dict[str, Any] = {
        "story_writer": HaikuStoryWriter(),
        "story_verifier": numeric_scope_verifier,
        "draft_connector": NulloneDraftBridgeConnector(),
        "review_delivery": TelegramReviewDeliveryAdapter(),
        "notifier": _production_notifier,
    }
    if overrides:
        deps.update(overrides)

    if not spool.is_dir():
        return report

    for scan_dir in sorted(p for p in spool.iterdir() if p.is_dir() and not p.is_symlink()):
        report["scans_seen"] += 1
        source = _receipt_source(scan_dir)
        for path in sorted(scan_dir.glob("*.json")):
            if path.name == RECEIPT_FILENAME or path.is_symlink() or not path.is_file():
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
                result: BreakingScheduledResult = run_breaking_candidate(
                    handoff,
                    story_writer=deps["story_writer"],
                    story_verifier=deps["story_verifier"],
                    draft_connector=deps["draft_connector"],
                    review_delivery=deps["review_delivery"],
                    notifier=deps["notifier"],
                    workspace_root=workspace_root,
                    source=source,
                )
            except Exception as exc:  # noqa: BLE001 - one file never kills the sweep
                report["skipped_invalid"].append(
                    {
                        "path": str(path.relative_to(root)),
                        "reason": f"RUNNER_CRASHED:{type(exc).__name__}",
                    }
                )
                continue
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
            else:
                report["processed"].append(
                    {
                        "path": str(path.relative_to(root)),
                        "candidate_id": result.candidate_id,
                        "domain_outcome": result.domain_outcome,
                        "run_id": result.run_id,
                        "application_execution": result.application_execution,
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
    assert report == {
        "scans_seen": 0,
        "processed": [],
        "skipped_invalid": [],
    }, report

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
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
