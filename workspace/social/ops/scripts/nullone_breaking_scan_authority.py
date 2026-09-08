#!/usr/bin/env python3
"""Deterministic raw Radar scan-slot authority (#80).

Answers exactly one question, and nothing else:

    Given a trigger-adapter source and an observed scan instant, which
    exact NullOne-owned Radar scan slot (if any) is current right now?

A raw scan identity (`source_occurrence_id`) is stable per scheduled scan
slot -- never derived from wall-clock retry time, random UUIDs, mutable
candidate text, or model prose. The existing edge
(`nullone_breaking_radar_edge.compute_candidate_external_occurrence_id`)
then binds each scan to each candidate deterministically, so retries and
corrected assessments keep stable candidate occurrences.

This reuses the accepted #59 scheduled-occurrence concepts (immutable
`ScheduleSpec` slots from `nullone_schedule_registry`, latest-due-slot
coalescing, no previous-date backfill, no cron parser, no scheduler DB,
no queue ledger) without minting scheduler invocations: scans are not
workflows, and `nullone.scheduler-invocation.v1` knows no `breaking-radar`
workflow. Only validated committed handoffs become Breaking occurrences,
downstream in the edge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from nullone_schedule_registry import ScheduleRegistryError, ScheduleSpec, get_schedules

SCAN_NAMESPACE = "breaking-radar"

STATUSES = frozenset({"DUE", "NO_DUE_SCAN"})

# Production candidate-ID shape: 2-8 lowercase slug segments, at most 80
# characters. The Radar agent derives IDs from a stable candidate anchor
# (primary announcement/source identity + topic slug, e.g.
# `acme-model-2-launch-region-a`); this rule enforces stability-shaped IDs
# without parsing prose. Rank-, title-, timestamp-, and full-JSON-derived
# IDs are forbidden by the prompt contract; shape alone cannot prove the
# anchor, but an unstable-shaped ID always fails closed here.
_CANDIDATE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,7}$")
_MAX_CANDIDATE_ID_LEN = 80


class BreakingScanAuthorityError(ValueError):
    """Malformed scan authority input or candidate identity."""


@dataclass(frozen=True)
class RadarScanResolution:
    """The authority's typed pure result."""

    status: str
    schedule_id: str
    source: str
    triggered_at: str
    reason_code: str
    local_scheduled_date: str
    local_scheduled_time: str
    scheduled_for: str | None = None
    source_occurrence_id: str | None = None
    lateness_seconds: float | None = None
    next_local_scheduled_instant: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise BreakingScanAuthorityError(f"unknown status: {self.status!r}")
        if self.status == "DUE" and (
            self.scheduled_for is None or self.source_occurrence_id is None
        ):
            raise BreakingScanAuthorityError(
                "DUE resolution must carry scheduled_for/source_occurrence_id"
            )
        if self.status == "NO_DUE_SCAN" and self.source_occurrence_id is not None:
            raise BreakingScanAuthorityError(
                "NO_DUE_SCAN resolution must not carry source_occurrence_id"
            )


def _format_canonical(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_triggered_at(triggered_at: str) -> datetime:
    from nullone_scheduler_invocation import TIMESTAMP_RE

    if not isinstance(triggered_at, str) or not TIMESTAMP_RE.fullmatch(triggered_at):
        raise BreakingScanAuthorityError(
            f"triggered_at must be canonical UTC RFC3339: {triggered_at!r}"
        )
    return datetime.strptime(triggered_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _slot_instant(spec: ScheduleSpec, observed_local: datetime) -> datetime:
    local_tz = ZoneInfo(spec.timezone_name)
    local_time = spec.local_time_of_day()
    return datetime(
        year=observed_local.year,
        month=observed_local.month,
        day=observed_local.day,
        hour=local_time.hour,
        minute=local_time.minute,
        second=local_time.second,
        tzinfo=local_tz,
    )


def validate_candidate_id(candidate_id: Any) -> str:
    """Enforce the stable production candidate-ID shape (fail closed)."""

    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or len(candidate_id) > _MAX_CANDIDATE_ID_LEN
        or not _CANDIDATE_ID_RE.fullmatch(candidate_id)
    ):
        raise BreakingScanAuthorityError(
            "candidate_id must be a stable lowercase slug "
            "(2-8 hyphen-separated segments, max 80 chars)"
        )
    return candidate_id


def resolve_radar_scan(
    *,
    source: str,
    triggered_at: str,
) -> RadarScanResolution:
    """Pure resolver: which exact NullOne-owned Radar scan slot is current?

    Before the first slot of the local date: `NO_DUE_SCAN` (never a
    previous-date backfill). At/after a slot: the latest due slot is DUE;
    missed theoretical slots coalesce into this one scan, never N scans.
    """

    if not isinstance(source, str) or not source.strip():
        raise BreakingScanAuthorityError("source must be non-empty")

    triggered_at_instant = _parse_triggered_at(triggered_at)

    try:
        specs = get_schedules(SCAN_NAMESPACE)
    except ScheduleRegistryError as exc:
        raise BreakingScanAuthorityError(str(exc)) from exc

    local_tz = ZoneInfo(specs[0].timezone_name)
    observed_local = triggered_at_instant.astimezone(local_tz)

    first_local = _slot_instant(specs[0], observed_local)
    if observed_local < first_local:
        return RadarScanResolution(
            status="NO_DUE_SCAN",
            schedule_id=specs[0].schedule_id,
            source=source,
            triggered_at=triggered_at,
            reason_code="BEFORE_TODAYS_FIRST_SCAN",
            local_scheduled_date=first_local.strftime("%Y-%m-%d"),
            local_scheduled_time=specs[0].local_time,
            next_local_scheduled_instant=_format_canonical(first_local),
        )

    due_spec = specs[0]
    due_instant = first_local
    for spec in specs[1:]:
        candidate = _slot_instant(spec, observed_local)
        if observed_local >= candidate:
            due_spec, due_instant = spec, candidate

    scheduled_for = _format_canonical(due_instant)
    return RadarScanResolution(
        status="DUE",
        schedule_id=due_spec.schedule_id,
        source=source,
        triggered_at=triggered_at,
        reason_code="OK",
        local_scheduled_date=due_instant.strftime("%Y-%m-%d"),
        local_scheduled_time=due_spec.local_time,
        scheduled_for=scheduled_for,
        source_occurrence_id=f"{due_spec.schedule_id}@{scheduled_for}",
        lateness_seconds=(triggered_at_instant - due_instant).total_seconds(),
    )


def self_test() -> int:
    # Before first slot (11:29:59 Baku = 07:29:59Z) -> NO_DUE.
    early = resolve_radar_scan(source="openclaw", triggered_at="2026-09-08T07:29:59Z")
    assert early.status == "NO_DUE_SCAN", early
    assert early.schedule_id == "breaking-radar.scan-1130.v1", early

    # 11:30 exactly -> DUE scan A.
    scan_a = resolve_radar_scan(source="openclaw", triggered_at="2026-09-08T07:30:00Z")
    assert scan_a.status == "DUE", scan_a
    assert scan_a.scheduled_for == "2026-09-08T07:30:00Z", scan_a
    assert scan_a.source_occurrence_id == (
        "breaking-radar.scan-1130.v1@2026-09-08T07:30:00Z"
    ), scan_a

    # Retry inside the same slot -> same raw scan identity.
    retry = resolve_radar_scan(source="openclaw", triggered_at="2026-09-08T09:00:00Z")
    assert retry.source_occurrence_id == scan_a.source_occurrence_id, retry

    # Later slots -> new identities.
    scan_b = resolve_radar_scan(source="openclaw", triggered_at="2026-09-08T10:30:00Z")
    assert scan_b.schedule_id == "breaking-radar.scan-1430.v1", scan_b
    assert scan_b.source_occurrence_id != scan_a.source_occurrence_id

    # Next day before first slot -> NO_DUE, no backfill.
    next_early = resolve_radar_scan(
        source="openclaw", triggered_at="2026-09-09T06:00:00Z"
    )
    assert next_early.status == "NO_DUE_SCAN", next_early

    # Candidate-ID shape rule.
    assert validate_candidate_id("acme-model-2-launch") == "acme-model-2-launch"
    for bad in ("", "A", "has spaces", "rank-1-title!", "x", "a" * 81, "UPPER"):
        try:
            validate_candidate_id(bad)
            raise AssertionError(f"bad candidate_id accepted: {bad!r}")
        except BreakingScanAuthorityError:
            pass

    # Malformed inputs fail closed.
    for bad_kwargs in (
        {"source": "", "triggered_at": "2026-09-08T07:30:00Z"},
        {"source": "openclaw", "triggered_at": "not-a-timestamp"},
    ):
        try:
            resolve_radar_scan(**bad_kwargs)
            raise AssertionError(f"malformed input was not rejected: {bad_kwargs}")
        except BreakingScanAuthorityError:
            pass

    print("BREAKING_SCAN_AUTHORITY_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_PERSISTENCE=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Radar scan authority")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
