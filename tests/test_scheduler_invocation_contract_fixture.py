#!/usr/bin/env python3
"""Contract fixture validation for the #65 scheduler invocation contract.

This validates that `tests/fixtures/scheduler_invocation_v1_examples.json`
is internally well-formed and that its pre-computed `occurrence_id` values
are correct against the canonical serialization rule defined in
`docs/contracts/scheduler-invocation-v1.md`. It does NOT implement or
invoke a scheduler adapter, does not import OpenClaw, and performs no
subprocess or network access. Implementing an adapter is #59/#61/#62/#63's
job, not this contract-validation test's.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/scheduler_invocation_v1_examples.json"
DOC = ROOT / "docs/contracts/scheduler-invocation-v1.md"

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

ALLOWED_WORKFLOW_IDS = {
    "morning-editorial",
    "daily-analytics",
    "story",
    "breaking",
}

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
OCCURRENCE_ID_RE = re.compile(r"^occ_[0-9a-f]{24}$")

STABLE_FIELDS = ("workflow_id", "source", "external_occurrence_id", "scheduled_for")

REQUIRED_INVALID_REASONS = {
    "missing_required_field",
    "empty_required_field",
    "unknown_workflow_id",
    "non_canonical_timestamp",
    "malformed_occurrence_id",
    "occurrence_id_mismatch",
    "unknown_field",
}

FORBIDDEN_LITERAL_PATTERNS = [
    re.compile(r"api[_-]?key\s*[:=]", re.I),
    re.compile(r"authorization\s*[:=]", re.I),
    re.compile(r"bearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"oauth[_-]?(token|secret)\s*[:=]", re.I),
]


class ContractError(ValueError):
    pass


def compute_occurrence_id(
    workflow_id: str, source: str, external_occurrence_id: str, scheduled_for: str
) -> str:
    canonical = json.dumps(
        [SCHEMA, workflow_id, source, external_occurrence_id, scheduled_for],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return f"occ_{digest}"


def validate_payload(payload: dict) -> None:
    """Pure contract-shape validation only. No adapter, no I/O, no network."""

    fields = set(payload)
    if fields != REQUIRED_FIELDS:
        missing = REQUIRED_FIELDS - fields
        extra = fields - REQUIRED_FIELDS
        if missing:
            raise ContractError(f"missing required field(s): {sorted(missing)}")
        raise ContractError(f"unknown field(s): {sorted(extra)}")

    if payload["schema"] != SCHEMA:
        raise ContractError("schema mismatch")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise ContractError("contract_version mismatch")

    workflow_id = payload["workflow_id"]
    if workflow_id not in ALLOWED_WORKFLOW_IDS:
        raise ContractError(f"unknown workflow_id: {workflow_id!r}")

    source = payload["source"]
    if not isinstance(source, str) or not source.strip():
        raise ContractError("source must be non-empty")

    external_occurrence_id = payload["external_occurrence_id"]
    if not isinstance(external_occurrence_id, str) or not external_occurrence_id.strip():
        raise ContractError("external_occurrence_id must be non-empty")

    scheduled_for = payload["scheduled_for"]
    if not isinstance(scheduled_for, str) or not TIMESTAMP_RE.fullmatch(scheduled_for):
        raise ContractError("scheduled_for must be canonical UTC RFC3339")

    triggered_at = payload["triggered_at"]
    if not isinstance(triggered_at, str) or not TIMESTAMP_RE.fullmatch(triggered_at):
        raise ContractError("triggered_at must be canonical UTC RFC3339")

    occurrence_id = payload["occurrence_id"]
    if not isinstance(occurrence_id, str) or not OCCURRENCE_ID_RE.fullmatch(occurrence_id):
        raise ContractError("malformed occurrence_id")

    expected = compute_occurrence_id(
        workflow_id, source, external_occurrence_id, scheduled_for
    )
    if occurrence_id != expected:
        raise ContractError("occurrence_id does not match recomputed identity")


def fail(message: str) -> None:
    raise AssertionError(message)


def main() -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert data["schema"] == "nullone.scheduler-invocation-examples.v1"
    assert data["contract_doc"] == "docs/contracts/scheduler-invocation-v1.md"
    assert DOC.is_file()

    valid_cases = data.get("valid_cases")
    assert isinstance(valid_cases, list) and len(valid_cases) >= 5

    by_name: dict[str, dict] = {}
    for case in valid_cases:
        name = case["name"]
        if name in by_name:
            fail(f"duplicate case name: {name}")
        by_name[name] = case
        try:
            validate_payload(case["payload"])
        except ContractError as exc:
            fail(f"{name}: expected valid payload but got {exc}")

    workflow_ids_seen = {case["payload"]["workflow_id"] for case in valid_cases}
    for wf in ALLOWED_WORKFLOW_IDS:
        if wf not in workflow_ids_seen:
            fail(f"valid_cases missing coverage for workflow_id {wf!r}")

    sources_seen = {case["payload"]["source"] for case in valid_cases}
    if "openclaw" not in sources_seen:
        fail("valid_cases must include an openclaw source example")
    if len(sources_seen) < 2:
        fail("valid_cases must include at least one alternate-scheduler source besides openclaw")

    # Replay examples: same logical occurrence, different triggered_at, same occurrence_id.
    replay_examples = data.get("replay_examples")
    assert isinstance(replay_examples, list) and replay_examples

    for case in replay_examples:
        name = case["name"]
        try:
            validate_payload(case["payload"])
        except ContractError as exc:
            fail(f"{name}: expected valid payload but got {exc}")

        baseline_name = case["same_occurrence_as"]
        baseline = by_name.get(baseline_name)
        if baseline is None:
            fail(f"{name}: unknown baseline case {baseline_name!r}")

        if case["payload"]["occurrence_id"] != baseline["payload"]["occurrence_id"]:
            fail(f"{name}: replay must produce the same occurrence_id as {baseline_name}")

        if case["payload"]["triggered_at"] == baseline["payload"]["triggered_at"]:
            fail(f"{name}: replay example must use a different triggered_at than its baseline")

        for field in STABLE_FIELDS:
            if case["payload"][field] != baseline["payload"][field]:
                fail(f"{name}: replay example must keep {field} identical to its baseline")

    # Distinct occurrence examples: exactly one stable field differs, occurrence_id differs.
    distinct_examples = data.get("distinct_occurrence_examples")
    assert isinstance(distinct_examples, list) and len(distinct_examples) >= 4

    covered_changed_fields: set[str] = set()

    for case in distinct_examples:
        name = case["name"]
        try:
            validate_payload(case["payload"])
        except ContractError as exc:
            fail(f"{name}: expected valid payload but got {exc}")

        baseline_name = case["differs_from"]
        baseline = by_name.get(baseline_name)
        if baseline is None:
            fail(f"{name}: unknown baseline case {baseline_name!r}")

        changed_field = case["changed_field"]
        if changed_field not in STABLE_FIELDS:
            fail(f"{name}: changed_field must be one of {STABLE_FIELDS}")
        covered_changed_fields.add(changed_field)

        differing = [f for f in STABLE_FIELDS if case["payload"][f] != baseline["payload"][f]]
        if differing != [changed_field]:
            fail(
                f"{name}: expected exactly {changed_field!r} to differ from "
                f"{baseline_name}, got {differing}"
            )

        if case["payload"]["occurrence_id"] == baseline["payload"]["occurrence_id"]:
            fail(
                f"{name}: changing {changed_field!r} must produce a different "
                f"occurrence_id than {baseline_name}"
            )

    missing_stable_coverage = set(STABLE_FIELDS) - covered_changed_fields
    if missing_stable_coverage:
        fail(f"distinct_occurrence_examples missing coverage for: {sorted(missing_stable_coverage)}")

    # Invalid cases: each must be rejected by validate_payload.
    invalid_cases = data.get("invalid_cases")
    assert isinstance(invalid_cases, list) and len(invalid_cases) >= 7

    seen_reasons: set[str] = set()
    for case in invalid_cases:
        name = case["name"]
        reason = case["reason"]
        seen_reasons.add(reason)
        try:
            validate_payload(case["payload"])
        except ContractError:
            pass
        else:
            fail(f"{name}: expected invalid payload ({reason}) to be rejected but it validated")

    missing_reasons = REQUIRED_INVALID_REASONS - seen_reasons
    if missing_reasons:
        fail(f"invalid_cases missing coverage for reasons: {sorted(missing_reasons)}")

    # Fixture hygiene: no production identifiers/secrets.
    raw_fixture = FIXTURE.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_LITERAL_PATTERNS:
        if pattern.search(raw_fixture):
            fail(f"fixture hygiene violation: {pattern.pattern}")

    # Contract doc must state the key invariants this test locks in.
    doc = DOC.read_text(encoding="utf-8")
    for required_phrase in (
        "occ_",
        "ensure_ascii=True",
        'separators=(",", ":")',
        "triggered_at",
        "must never be replaced by the current time",
    ):
        if required_phrase not in doc:
            fail(f"required contract statement missing from doc: {required_phrase!r}")

    print(
        "SCHEDULER_INVOCATION_CONTRACT_FIXTURE=PASS "
        f"valid={len(valid_cases)} replay={len(replay_examples)} "
        f"distinct={len(distinct_examples)} invalid={len(invalid_cases)}"
    )
    print("FIXTURE_HYGIENE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
