#!/usr/bin/env python3
"""Shared value/validation module for `nullone.scheduler-invocation.v1` (#65/#62).

Implements exactly the contract in
`docs/contracts/scheduler-invocation-v1.md`: exact schema/version/field-set,
`workflow_id`/`source`/`external_occurrence_id`/timestamp semantics, and the
deterministic `occurrence_id` canonical-serialization/derivation rule
(`occ_` + first 24 lowercase hex chars of
`sha256(json.dumps([...], ensure_ascii=True, separators=(",", ":")))`),
matching the existing `make_run_id` convention in `nullone_run_outcome.py`.

This module is a pure value/validation boundary only:

- no scheduler, cron, job queue, or OpenClaw automation;
- no I/O, no subprocess, no network;
- no workflow execution -- application workflows (`nullone_story_workflow.py`
  and friends) call `validate_payload()` at their trigger edge and then own
  everything downstream themselves.

`tests/test_scheduler_invocation_contract_fixture.py` imports this module's
`validate_payload`/`compute_occurrence_id`/`ContractError` rather than
duplicating the rule a second time; the fixture test still fully owns the
fixture-file assertions.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA = "nullone.scheduler-invocation.v1"
CONTRACT_VERSION = "1.0.0"

REQUIRED_FIELDS = frozenset(
    {
        "schema",
        "contract_version",
        "workflow_id",
        "source",
        "external_occurrence_id",
        "scheduled_for",
        "triggered_at",
        "occurrence_id",
    }
)

ALLOWED_WORKFLOW_IDS = frozenset(
    {
        "morning-editorial",
        "daily-analytics",
        "story",
        "breaking",
    }
)

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
OCCURRENCE_ID_RE = re.compile(r"^occ_[0-9a-f]{24}$")

# Fields that participate in occurrence identity. `triggered_at` and
# `occurrence_id` itself are deliberately excluded -- see the contract doc's
# "Occurrence identity derivation" section.
STABLE_FIELDS = ("workflow_id", "source", "external_occurrence_id", "scheduled_for")


class SchedulerInvocationError(ValueError):
    """Malformed/unsupported `nullone.scheduler-invocation.v1` payload.

    Raised instead of silently substituting a default (e.g. the current
    time for a missing `external_occurrence_id`) -- the contract's fail-
    closed requirement.
    """


# Backwards-compatible alias matching the name the fixture test originally
# defined locally.
ContractError = SchedulerInvocationError


def compute_occurrence_id(
    workflow_id: str, source: str, external_occurrence_id: str, scheduled_for: str
) -> str:
    """Deterministic `occurrence_id` per the contract's canonical rule.

    `triggered_at` never participates: a retry or delayed delivery of the
    same logical occurrence must produce the same `occurrence_id`.
    """

    canonical = json.dumps(
        [SCHEMA, workflow_id, source, external_occurrence_id, scheduled_for],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return f"occ_{digest}"


def validate_payload(payload: Any) -> dict[str, Any]:
    """Pure contract-shape validation. No adapter, no I/O, no network.

    Enforces: exact schema/contract_version literals, the exact required
    field set (no missing, no unknown field), an allowed `workflow_id`, a
    non-empty `source`/`external_occurrence_id`, canonical UTC RFC3339
    `scheduled_for`/`triggered_at`, and that `occurrence_id` is well-formed
    AND matches the deterministic recomputation from the four stable
    fields -- a mismatch is invalid input, never reinterpreted as a new
    occurrence.

    Returns the payload unchanged (already-validated) on success; raises
    `SchedulerInvocationError` otherwise. Fails closed on any non-dict
    input.
    """

    if not isinstance(payload, dict):
        raise SchedulerInvocationError("payload must be an object")

    fields = set(payload)
    if fields != REQUIRED_FIELDS:
        missing = REQUIRED_FIELDS - fields
        extra = fields - REQUIRED_FIELDS
        if missing:
            raise SchedulerInvocationError(f"missing required field(s): {sorted(missing)}")
        raise SchedulerInvocationError(f"unknown field(s): {sorted(extra)}")

    if payload["schema"] != SCHEMA:
        raise SchedulerInvocationError("schema mismatch")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise SchedulerInvocationError("contract_version mismatch")

    workflow_id = payload["workflow_id"]
    if workflow_id not in ALLOWED_WORKFLOW_IDS:
        raise SchedulerInvocationError(f"unknown workflow_id: {workflow_id!r}")

    source = payload["source"]
    if not isinstance(source, str) or not source.strip():
        raise SchedulerInvocationError("source must be non-empty")

    external_occurrence_id = payload["external_occurrence_id"]
    if not isinstance(external_occurrence_id, str) or not external_occurrence_id.strip():
        raise SchedulerInvocationError("external_occurrence_id must be non-empty")

    scheduled_for = payload["scheduled_for"]
    if not isinstance(scheduled_for, str) or not TIMESTAMP_RE.fullmatch(scheduled_for):
        raise SchedulerInvocationError("scheduled_for must be canonical UTC RFC3339")

    triggered_at = payload["triggered_at"]
    if not isinstance(triggered_at, str) or not TIMESTAMP_RE.fullmatch(triggered_at):
        raise SchedulerInvocationError("triggered_at must be canonical UTC RFC3339")

    occurrence_id = payload["occurrence_id"]
    if not isinstance(occurrence_id, str) or not OCCURRENCE_ID_RE.fullmatch(occurrence_id):
        raise SchedulerInvocationError("malformed occurrence_id")

    expected = compute_occurrence_id(
        workflow_id, source, external_occurrence_id, scheduled_for
    )
    if occurrence_id != expected:
        raise SchedulerInvocationError(
            "occurrence_id does not match recomputed identity"
        )

    return payload


def accept_workflow_trigger(payload: Any, *, workflow_id: str) -> dict[str, Any]:
    """Validate `payload` and additionally pin it to one exact `workflow_id`.

    This is the exact boundary an application workflow (e.g.
    `nullone_story_workflow.run_story_workflow`) calls with its own stable
    `workflow_id` (`"story"`); a payload that is otherwise well-formed but
    carries a different `workflow_id` still fails closed here rather than
    being silently accepted by the wrong workflow.
    """

    validate_payload(payload)
    if payload["workflow_id"] != workflow_id:
        raise SchedulerInvocationError(
            f"trigger workflow_id {payload['workflow_id']!r} does not match "
            f"the expected {workflow_id!r} boundary"
        )
    return payload


def self_test() -> int:
    base = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "workflow_id": "story",
        "source": "openclaw",
        "external_occurrence_id": "openclaw-occ-story-0001",
        "scheduled_for": "2026-09-08T10:30:00Z",
        "triggered_at": "2026-09-08T10:30:02Z",
    }
    base["occurrence_id"] = compute_occurrence_id(
        base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
    )

    validate_payload(dict(base))
    accept_workflow_trigger(dict(base), workflow_id="story")

    try:
        accept_workflow_trigger(dict(base), workflow_id="breaking")
        raise AssertionError("mismatched workflow_id boundary was not rejected")
    except SchedulerInvocationError:
        pass

    replay = dict(base)
    replay["triggered_at"] = "2026-09-08T10:31:59Z"
    validate_payload(replay)
    assert replay["occurrence_id"] == base["occurrence_id"], "replay must keep occurrence_id stable"

    mismatched = dict(base)
    mismatched["occurrence_id"] = "occ_" + "0" * 24
    try:
        validate_payload(mismatched)
        raise AssertionError("occurrence_id mismatch was not rejected")
    except SchedulerInvocationError:
        pass

    missing = dict(base)
    del missing["external_occurrence_id"]
    try:
        validate_payload(missing)
        raise AssertionError("missing required field was not rejected")
    except SchedulerInvocationError:
        pass

    unknown_field = dict(base)
    unknown_field["extra"] = "nope"
    try:
        validate_payload(unknown_field)
        raise AssertionError("unknown field was not rejected")
    except SchedulerInvocationError:
        pass

    print("SCHEDULER_INVOCATION_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_ADAPTER=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne scheduler-invocation.v1 value module")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
