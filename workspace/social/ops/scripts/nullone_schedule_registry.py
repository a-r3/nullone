#!/usr/bin/env python3
"""M0 NullOne-owned schedule registry (#59 remaining scope).

Answers, for exactly the two currently reviewed/live daily cadences, "what
is NullOne's own repo-owned daily schedule slot for this workflow" -- the
narrow question `nullone_scheduled_occurrence_authority.py` needs before it
can compute an exact `scheduled_for`.

This is deliberately NOT a scheduler, a cron engine, or a
user-configurable schedule store:

- no cron-expression parsing;
- no persistence, I/O, subprocess, or network;
- no generic/arbitrary schedule registration API;
- exactly one immutable `ScheduleSpec` per currently supported
  `workflow_id`, reviewed and committed as code, not data a caller can
  mutate at runtime.

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

# The M0-supported workflow_ids -- a strict subset of
# `nullone_scheduler_invocation.ALLOWED_WORKFLOW_IDS` (which also includes
# `"story"`/`"breaking"`, neither of which has a NullOne-owned daily
# schedule slot in M0).
SUPPORTED_WORKFLOW_IDS = frozenset({"morning-editorial", "daily-analytics"})

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


def _build_registry(specs: tuple[ScheduleSpec, ...]) -> dict[str, ScheduleSpec]:
    by_workflow: dict[str, ScheduleSpec] = {}
    seen_schedule_ids: set[str] = set()

    for spec in specs:
        if spec.workflow_id in by_workflow:
            raise ScheduleRegistryError(f"duplicate workflow_id definition: {spec.workflow_id!r}")
        if spec.schedule_id in seen_schedule_ids:
            raise ScheduleRegistryError(f"duplicate schedule_id: {spec.schedule_id!r}")
        by_workflow[spec.workflow_id] = spec
        seen_schedule_ids.add(spec.schedule_id)

    missing = SUPPORTED_WORKFLOW_IDS - set(by_workflow)
    if missing:
        raise ScheduleRegistryError(f"missing schedule definition(s) for: {sorted(missing)}")

    return by_workflow


# The exact M0 reviewed/live cadence. See the #59 remaining-scope goal:
# these two values reflect the schedules currently reviewed as live,
# expressed here as NullOne-owned repo config -- not read from OpenClaw,
# not user-editable at runtime.
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
    )
)


def get_schedule(workflow_id: str) -> ScheduleSpec:
    """Return the exact `ScheduleSpec` for `workflow_id`, or fail closed."""

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
        raise AssertionError("unsupported workflow_id was not rejected")
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
