#!/usr/bin/env python3
"""M0 NullOne-owned schedule registry (#59 remaining scope, extended by #79).

Answers, for each currently reviewed cadence, "what is NullOne's own
repo-owned schedule slot for this workflow" -- the narrow question
`nullone_scheduled_occurrence_authority.py` needs before it can compute
an exact `scheduled_for`.

This is deliberately NOT a scheduler, a cron engine, or a
user-configurable schedule store:

- no cron-expression parsing;
- no persistence, I/O, subprocess, or network;
- no generic/arbitrary schedule registration API;
- immutable `ScheduleSpec` entries only, reviewed and committed as code,
  not data a caller can mutate at runtime.

Morning Editorial and Daily Analytics each own exactly one daily slot
(unchanged by #79). Story owns exactly the four reviewed Story check
windows from the accepted cadence policy (`CONTENT_STRATEGY.md` §10,
already referenced by `docs/contracts/cadence-contract-v1.md`'s daypart
table): 10:30 / 13:30 / 18:30 / 21:30 Asia/Baku. Multiple missed Story
slots coalesce into one current evaluation -- the authority resolves the
latest due slot for the observed local date, never a queue of N replays.

A future schedule semantic change (a different local time, a different
timezone, or any other change to what "the slot" means) must introduce a
new `schedule_id` revision (e.g. `.v2`), never silently reinterpret an
existing one -- see `ScheduleSpec.schedule_id`'s docstring below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# NullOne-owned schedule namespaces. Morning/Daily/Story are scheduler-
# invocation workflows (a subset of
# `nullone_scheduler_invocation.ALLOWED_WORKFLOW_IDS`, which additionally
# includes `"breaking"` -- dispatched per handoff, never on a fixed slot).
# `breaking-radar` is a raw-scan slot namespace resolved only by
# `nullone_breaking_scan_authority.py`, never by a wake-up CLI.
SUPPORTED_WORKFLOW_IDS = frozenset(
    {"morning-editorial", "daily-analytics", "story", "breaking-radar"}
)

# Workflows with exactly one daily slot. `get_schedule()` serves these;
# Morning/Daily resolution behavior is byte-for-behavior unchanged by #79.
SINGLE_SLOT_WORKFLOW_IDS = frozenset({"morning-editorial", "daily-analytics"})

_SCHEDULE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+(?:-[a-z0-9]+)*\.v[0-9]+$")
_LOCAL_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9]):([0-5][0-9])$")


class ScheduleRegistryError(ValueError):
    """Malformed `ScheduleSpec` or unsupported `workflow_id`.

    Raised instead of silently accepting an ambiguous/unsupported schedule
    -- the M0 registry's own fail-closed requirement.
    """


@dataclass(frozen=True)
class ScheduleSpec:
    """One immutable, reviewed NullOne-owned daily schedule slot.

    `schedule_id` carries the schedule's own revision. Because it is part
    of the stable `external_occurrence_id`/`occurrence_id` identity chain
    (see `nullone_scheduled_occurrence_authority.py`), changing what a
    slot means -- a different `local_time` or `timezone_name` -- must
    introduce a new `schedule_id` (`morning-editorial.daily.v2`, etc.)
    rather than mutating this one in place; that keeps a schedule
    revision change from silently reinterpreting an already-computed past
    `occurrence_id`.
    """

    workflow_id: str
    schedule_id: str
    timezone_name: str
    local_time: str  # exact "HH:MM:SS", 24-hour, zero-padded

    def __post_init__(self) -> None:
        if self.workflow_id not in SUPPORTED_WORKFLOW_IDS:
            raise ScheduleRegistryError(f"unsupported workflow_id: {self.workflow_id!r}")

        if not isinstance(self.schedule_id, str) or not _SCHEDULE_ID_RE.fullmatch(self.schedule_id):
            raise ScheduleRegistryError(f"malformed schedule_id: {self.schedule_id!r}")

        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ScheduleRegistryError(f"unknown IANA timezone: {self.timezone_name!r}") from exc

        if not isinstance(self.local_time, str) or not _LOCAL_TIME_RE.fullmatch(self.local_time):
            raise ScheduleRegistryError(f"local_time must be exact HH:MM:SS: {self.local_time!r}")

    def local_time_of_day(self) -> time:
        """Parsed `local_time` as a `datetime.time` (no tzinfo)."""

        hour, minute, second = (int(part) for part in self.local_time.split(":"))
        return time(hour=hour, minute=minute, second=second)


def _build_registry(
    specs: tuple[ScheduleSpec, ...],
) -> dict[str, tuple[ScheduleSpec, ...]]:
    by_workflow: dict[str, list[ScheduleSpec]] = {}
    seen_schedule_ids: set[str] = set()

    for spec in specs:
        if spec.schedule_id in seen_schedule_ids:
            raise ScheduleRegistryError(f"duplicate schedule_id: {spec.schedule_id!r}")
        seen_schedule_ids.add(spec.schedule_id)
        by_workflow.setdefault(spec.workflow_id, []).append(spec)

    missing = SUPPORTED_WORKFLOW_IDS - set(by_workflow)
    if missing:
        raise ScheduleRegistryError(f"missing schedule definition(s) for: {sorted(missing)}")

    for workflow_id, workflow_specs in by_workflow.items():
        if workflow_id in SINGLE_SLOT_WORKFLOW_IDS and len(workflow_specs) != 1:
            raise ScheduleRegistryError(
                f"single-slot workflow must own exactly one slot: {workflow_id!r}"
            )

    return {
        workflow_id: tuple(
            sorted(workflow_specs, key=lambda spec: spec.local_time_of_day())
        )
        for workflow_id, workflow_specs in by_workflow.items()
    }


# The exact M0 reviewed/live cadence. See the #59 remaining-scope goal:
# Morning/Daily values reflect the schedules currently reviewed as live,
# expressed here as NullOne-owned repo config -- not read from OpenClaw,
# not user-editable at runtime. Story's four check windows are the
# approved Story windows from CONTENT_STRATEGY.md §10 (already referenced
# by the cadence contract's daypart table), expressed here as immutable
# reviewed slots.
_REGISTRY = _build_registry(
    (
        ScheduleSpec(
            workflow_id="morning-editorial",
            schedule_id="morning-editorial.daily.v1",
            timezone_name="Asia/Baku",
            local_time="08:30:00",
        ),
        ScheduleSpec(
            workflow_id="daily-analytics",
            schedule_id="daily-analytics.daily.v1",
            timezone_name="Asia/Baku",
            local_time="03:20:00",
        ),
        ScheduleSpec(
            workflow_id="story",
            schedule_id="story.check-1030.v1",
            timezone_name="Asia/Baku",
            local_time="10:30:00",
        ),
        ScheduleSpec(
            workflow_id="story",
            schedule_id="story.check-1330.v1",
            timezone_name="Asia/Baku",
            local_time="13:30:00",
        ),
        ScheduleSpec(
            workflow_id="story",
            schedule_id="story.check-1830.v1",
            timezone_name="Asia/Baku",
            local_time="18:30:00",
        ),
        ScheduleSpec(
            workflow_id="story",
            schedule_id="story.check-2130.v1",
            timezone_name="Asia/Baku",
            local_time="21:30:00",
        ),
        ScheduleSpec(
            workflow_id="breaking-radar",
            schedule_id="breaking-radar.scan-1130.v1",
            timezone_name="Asia/Baku",
            local_time="11:30:00",
        ),
        ScheduleSpec(
            workflow_id="breaking-radar",
            schedule_id="breaking-radar.scan-1430.v1",
            timezone_name="Asia/Baku",
            local_time="14:30:00",
        ),
        ScheduleSpec(
            workflow_id="breaking-radar",
            schedule_id="breaking-radar.scan-1730.v1",
            timezone_name="Asia/Baku",
            local_time="17:30:00",
        ),
        ScheduleSpec(
            workflow_id="breaking-radar",
            schedule_id="breaking-radar.scan-2030.v1",
            timezone_name="Asia/Baku",
            local_time="20:30:00",
        ),
        ScheduleSpec(
            workflow_id="breaking-radar",
            schedule_id="breaking-radar.scan-2330.v1",
            timezone_name="Asia/Baku",
            local_time="23:30:00",
        ),
    )
)


def get_schedule(workflow_id: str) -> ScheduleSpec:
    """Return the exact single `ScheduleSpec` for `workflow_id`.

    Single-slot workflows only; fails closed for multi-slot or
    unsupported workflows. Morning/Daily behavior is unchanged.
    """

    specs = get_schedules(workflow_id)
    if len(specs) != 1:
        raise ScheduleRegistryError(
            f"workflow_id owns {len(specs)} slots, not one: {workflow_id!r}"
        )
    return specs[0]


def get_schedules(workflow_id: str) -> tuple[ScheduleSpec, ...]:
    """Return all `ScheduleSpec` entries for `workflow_id`, ascending by local time."""

    if not isinstance(workflow_id, str) or workflow_id not in _REGISTRY:
        raise ScheduleRegistryError(f"no NullOne-owned schedule for workflow_id: {workflow_id!r}")
    return _REGISTRY[workflow_id]


def self_test() -> int:
    morning = get_schedule("morning-editorial")
    assert morning.schedule_id == "morning-editorial.daily.v1"
    assert morning.timezone_name == "Asia/Baku"
    assert morning.local_time == "08:30:00"
    assert morning.local_time_of_day() == time(8, 30, 0)

    analytics = get_schedule("daily-analytics")
    assert analytics.schedule_id == "daily-analytics.daily.v1"
    assert analytics.local_time == "03:20:00"

    try:
        get_schedule("story")
        raise AssertionError("multi-slot workflow_id was not rejected by get_schedule")
    except ScheduleRegistryError:
        pass

    story_specs = get_schedules("story")
    assert [spec.schedule_id for spec in story_specs] == [
        "story.check-1030.v1",
        "story.check-1330.v1",
        "story.check-1830.v1",
        "story.check-2130.v1",
    ]
    assert [spec.local_time for spec in story_specs] == [
        "10:30:00",
        "13:30:00",
        "18:30:00",
        "21:30:00",
    ]
    assert all(spec.timezone_name == "Asia/Baku" for spec in story_specs)

    radar_specs = get_schedules("breaking-radar")
    assert [spec.schedule_id for spec in radar_specs] == [
        "breaking-radar.scan-1130.v1",
        "breaking-radar.scan-1430.v1",
        "breaking-radar.scan-1730.v1",
        "breaking-radar.scan-2030.v1",
        "breaking-radar.scan-2330.v1",
    ]
    assert [spec.local_time for spec in radar_specs] == [
        "11:30:00",
        "14:30:00",
        "17:30:00",
        "20:30:00",
        "23:30:00",
    ]
    assert all(spec.timezone_name == "Asia/Baku" for spec in radar_specs)

    try:
        _build_registry(
            (
                ScheduleSpec(
                    workflow_id="morning-editorial",
                    schedule_id="morning-editorial.daily.v1",
                    timezone_name="Asia/Baku",
                    local_time="08:30:00",
                ),
            )
        )
        raise AssertionError("missing workflow definition was not rejected")
    except ScheduleRegistryError:
        pass

    try:
        get_schedule("does-not-exist")
        raise AssertionError("unknown workflow_id was not rejected")
    except ScheduleRegistryError:
        pass

    try:
        ScheduleSpec(
            workflow_id="morning-editorial",
            schedule_id="morning-editorial.daily.v1",
            timezone_name="Not/AZone",
            local_time="08:30:00",
        )
        raise AssertionError("invalid timezone was not rejected")
    except ScheduleRegistryError:
        pass

    try:
        ScheduleSpec(
            workflow_id="morning-editorial",
            schedule_id="morning-editorial.daily.v1",
            timezone_name="Asia/Baku",
            local_time="8:30:00",
        )
        raise AssertionError("malformed local_time was not rejected")
    except ScheduleRegistryError:
        pass

    try:
        ScheduleSpec(
            workflow_id="morning-editorial",
            schedule_id="not a valid schedule id",
            timezone_name="Asia/Baku",
            local_time="08:30:00",
        )
        raise AssertionError("malformed schedule_id was not rejected")
    except ScheduleRegistryError:
        pass

    try:
        _build_registry(
            (
                ScheduleSpec(
                    workflow_id="morning-editorial",
                    schedule_id="morning-editorial.daily.v1",
                    timezone_name="Asia/Baku",
                    local_time="08:30:00",
                ),
                ScheduleSpec(
                    workflow_id="daily-analytics",
                    schedule_id="morning-editorial.daily.v1",
                    timezone_name="Asia/Baku",
                    local_time="03:20:00",
                ),
            )
        )
        raise AssertionError("duplicate schedule_id was not rejected")
    except ScheduleRegistryError:
        pass

    print("SCHEDULE_REGISTRY_SELF_TEST=PASS")
    print("NO_CRON_PARSING=TRUE")
    print("NO_PERSISTENCE=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne M0 schedule registry")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
