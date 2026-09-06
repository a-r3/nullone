#!/usr/bin/env python3
"""Durable breaking draft-set reservation and Story-first dispatch (#36).

Consumes one validated `nullone.breaking-routing.v1` accelerated decision
(IMMEDIATE_STORY_DRAFT / IMMEDIATE_STORY_AND_MAIN_DRAFT) from
`nullone_breaking_router.py` and orchestrates draft creation through the
existing #33 Story core (`nullone_story_pipeline.run_story_pipeline`) and
the #36 main core (`nullone_main_draft_pipeline.run_main_pipeline`).

Safety invariants enforced here (see issue #36 sections 13-19):

* One development (event_id + development_id) owns at most one durable
  draft set, independent of target format. A conflicting target set for
  the same development fails closed rather than silently upgrading it.
* Exact replay of the same accepted decision reuses the existing set and
  never repeats a target.
* Dispatch order is always STORY first, then the selected MAIN target --
  never main first, and never main as compensation for a blocked/
  ambiguous Story.
* Story failure/ambiguity never attempts main. Main failure/ambiguity
  never repeats Story. Target progress is independent once Story has
  succeeded, but safety-linked (an ambiguous main outcome requires
  reconciliation; it never triggers an automatic Story retry).
* Concurrent dispatch for the same development is serialized with
  `fcntl.flock` on a per-draft-set lock file; at most one set is ever
  reserved for one development.

This module performs no network calls itself; it delegates all actual
draft creation to the injected #33/#36 pipeline runners (real runners
shell out to `nullone-draft-bridge.py`, matching the existing pattern --
tests inject fakes). It has no publisher capability: it never imports
`nullone-publish-bridge.py` or `nullone-publisher-run.py`.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from nullone_bridge_common import (
    BridgeError,
    atomic_write_json,
    now_iso,
    resolve_workspace_path,
    workspace_relative,
)

SCHEMA = "nullone.breaking-draft-set.v1"
CONTRACT_VERSION = "1.0.0"

ACCELERATED_DECISIONS = frozenset(
    {"IMMEDIATE_STORY_DRAFT", "IMMEDIATE_STORY_AND_MAIN_DRAFT"}
)

TARGET_STATUSES = frozenset(
    {"PENDING", "SUCCEEDED", "BLOCKED_BEFORE_ATTEMPT", "AMBIGUOUS", "NOT_ATTEMPTED"}
)


class DraftSetError(RuntimeError):
    """Durable draft-set state is malformed, inaccessible or conflicting."""


class DraftSetConflict(DraftSetError):
    """An existing set for this development has a different target set."""


class DispatchRejected(RuntimeError):
    """The caller passed a routing decision this dispatcher cannot service."""


# ---------------------------------------------------------------------------
# draft_set_id -- derived from stable development identity + contract
# version ONLY. Target format is deliberately excluded so an escalated or
# reformatted request for the same development can never mint a second set.
# ---------------------------------------------------------------------------


def compute_draft_set_id(event_id: str, development_id: str) -> str:
    if not event_id or not isinstance(event_id, str):
        raise DraftSetError("event_id is required to compute a draft_set_id")
    if not development_id or not isinstance(development_id, str):
        raise DraftSetError("development_id is required to compute a draft_set_id")

    canonical = json.dumps(
        [CONTRACT_VERSION, event_id, development_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"breaking-set-{digest}"


def draft_set_path(draft_set_id: str) -> Path:
    return resolve_workspace_path(f"social/drafts/production/breaking/sets/{draft_set_id}.json")


def draft_set_lock_path(draft_set_id: str) -> Path:
    return resolve_workspace_path(f"social/drafts/production/breaking/locks/{draft_set_id}.lock")


@contextmanager
def draft_set_lock(draft_set_id: str):
    """Serialize reservation and dispatch for one development's draft set."""

    lock_path = draft_set_lock_path(draft_set_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Durable record I/O -- fail closed on malformed state.
# ---------------------------------------------------------------------------


def _decision_hash(routing_result: dict[str, Any]) -> str:
    canonical = json.dumps(routing_result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_draft_set(draft_set_id: str) -> dict[str, Any] | None:
    """Load exact durable draft-set state, failing closed on malformed data."""

    path = draft_set_path(draft_set_id)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise DraftSetError(f"Draft set record is unreadable/malformed: {path}") from e
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise DraftSetError(f"Draft set record has invalid schema: {path}")
    if record.get("draft_set_id") != draft_set_id:
        raise DraftSetError(f"Draft set record identity mismatch: {path}")
    return record


def _write_draft_set(record: dict[str, Any]) -> None:
    record["updated_at"] = now_iso()
    atomic_write_json(draft_set_path(record["draft_set_id"]), record)


def reserve_draft_set(routing_result: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Reserve (or idempotently reuse) the durable draft set for this decision.

    Caller must hold `draft_set_lock(draft_set_id)`. Returns
    `(record, created)`. Raises `DraftSetConflict` if an existing set for
    this development has a different `allowed_targets` -- policy grants no
    later main upgrade (or downgrade) for the same already-reserved
    development; a truly distinct development requires a new
    development_id from #35.
    """

    if routing_result.get("routing_decision") not in ACCELERATED_DECISIONS:
        raise DispatchRejected(
            "reserve_draft_set only accepts an accelerated routing_decision "
            f"(IMMEDIATE_STORY_DRAFT/IMMEDIATE_STORY_AND_MAIN_DRAFT); got "
            f"{routing_result.get('routing_decision')!r}"
        )

    event = routing_result.get("event") or {}
    event_id = event.get("event_id")
    development_id = event.get("development_id")
    draft_set_id = compute_draft_set_id(event_id, development_id)

    allowed_targets = list(routing_result.get("draft_targets") or [])
    decision_hash = _decision_hash(routing_result)

    existing = load_draft_set(draft_set_id)
    if existing is not None:
        if existing.get("allowed_targets") != allowed_targets:
            raise DraftSetConflict(
                f"Existing draft set {draft_set_id} has allowed_targets "
                f"{existing.get('allowed_targets')!r}; a new request for "
                f"{allowed_targets!r} on the same development is rejected "
                "-- no automatic upgrade/downgrade for an already-reserved "
                "development."
            )
        return existing, False

    targets: dict[str, dict[str, Any]] = {
        target: {
            "status": "PENDING",
            "manifest_id": None,
            "review_post_id": None,
            "outcome": None,
            "reason_code": None,
            "updated_at": None,
        }
        for target in allowed_targets
    }

    record = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "draft_set_id": draft_set_id,
        "event_id": event_id,
        "development_id": development_id,
        "candidate_id": routing_result.get("candidate_id"),
        "routing_decision": routing_result.get("routing_decision"),
        "reason_code": routing_result.get("reason_code"),
        "decision_hash": decision_hash,
        "allowed_targets": allowed_targets,
        "main_draft_justification": routing_result.get("main_draft_justification"),
        "targets": targets,
        "reconciliation_required": False,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    _write_draft_set(record)
    return record, True


def _update_target(
    record: dict[str, Any],
    target: str,
    *,
    status: str,
    manifest_id: str | None = None,
    review_post_id: str | None = None,
    outcome: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    if status not in TARGET_STATUSES:
        raise DraftSetError(f"Invalid target status: {status!r}")
    record["targets"][target] = {
        "status": status,
        "manifest_id": manifest_id,
        "review_post_id": review_post_id,
        "outcome": outcome,
        "reason_code": reason_code,
        "updated_at": now_iso(),
    }
    if status == "AMBIGUOUS":
        record["reconciliation_required"] = True
    _write_draft_set(record)
    return record


# ---------------------------------------------------------------------------
# Runner protocols -- thin adapters around the #33 Story core and the #36
# main core. Real runners shell to nullone-draft-bridge.py (see
# nullone_story_pipeline.NulloneDraftBridgeConnector); tests inject fakes.
# ---------------------------------------------------------------------------


class StoryRunner(Protocol):
    def __call__(self) -> Any:
        """Run the #33 Story core once and return its StoryPipelineResult."""


class MainRunner(Protocol):
    def __call__(self, main_format: str) -> Any:
        """Run the #36 main core once for `main_format` and return its result."""


class MainCapacityRecheck(Protocol):
    def __call__(self) -> bool:
        """Re-read authoritative main load/capacity immediately before dispatch."""


_STORY_SUCCESS_OUTCOMES = frozenset({"DRAFT_CREATED", "PREVIEW_DELIVERY_FAILED"})
_STORY_AMBIGUOUS_OUTCOMES = frozenset(
    {"REVIEW_DRAFT_AMBIGUOUS", "REVIEW_DRAFT_ALREADY_CONSUMED"}
)
_MAIN_SUCCESS_OUTCOMES = frozenset({"DRAFT_CREATED", "PREVIEW_DELIVERY_FAILED"})
_MAIN_AMBIGUOUS_OUTCOMES = frozenset(
    {"REVIEW_DRAFT_AMBIGUOUS", "REVIEW_DRAFT_ALREADY_CONSUMED"}
)


@dataclass
class DispatchResult:
    draft_set_id: str
    created: bool
    record: dict[str, Any]
    story_result: Any | None = None
    main_result: Any | None = None
    main_target: str | None = None

    @property
    def reconciliation_required(self) -> bool:
        return bool(self.record.get("reconciliation_required"))


def dispatch_draft_set(
    routing_result: dict[str, Any],
    *,
    story_runner: StoryRunner,
    main_runner: MainRunner | None = None,
    main_capacity_recheck: MainCapacityRecheck | None = None,
) -> DispatchResult:
    """Reserve (or reuse) the draft set and dispatch Story-first.

    Rechecks the durable set's own per-target progress before invoking any
    runner, so exact replay of an already-serviced set never repeats a
    target. Story is always attempted before main; main is attempted only
    if Story's outcome proves a draft exists (DRAFT_CREATED or
    PREVIEW_DELIVERY_FAILED -- both mean the review draft/manifest exist,
    only Telegram delivery differs) and the set actually includes a main
    target. A blocked-before-attempt or ambiguous Story outcome stops the
    whole set; main is never attempted as compensation.
    """

    event = routing_result.get("event") or {}
    event_id = event.get("event_id")
    development_id = event.get("development_id")
    draft_set_id = compute_draft_set_id(event_id, development_id)

    with draft_set_lock(draft_set_id):
        record, created = reserve_draft_set(routing_result)
        allowed_targets = record["allowed_targets"]

        story_result = None
        main_result = None
        main_target = next((t for t in allowed_targets if t != "STORY"), None)

        story_progress = record["targets"].get("STORY", {})
        story_status = story_progress.get("status", "PENDING")

        if story_status == "SUCCEEDED":
            story_succeeded = True
        elif story_status in ("BLOCKED_BEFORE_ATTEMPT", "AMBIGUOUS"):
            # Terminal for this set: never repeat, never fall through to main.
            return DispatchResult(draft_set_id, created, record, main_target=main_target)
        else:
            story_result = story_runner()
            outcome = getattr(story_result, "outcome", None)

            if outcome in _STORY_SUCCESS_OUTCOMES:
                record = _update_target(
                    record,
                    "STORY",
                    status="SUCCEEDED",
                    manifest_id=getattr(story_result, "manifest_id", None),
                    review_post_id=getattr(story_result, "review_post_id", None),
                    outcome=outcome,
                )
                story_succeeded = True
            elif outcome in _STORY_AMBIGUOUS_OUTCOMES:
                record = _update_target(
                    record, "STORY", status="AMBIGUOUS", outcome=outcome,
                    reason_code=getattr(story_result, "reason_code", None),
                )
                return DispatchResult(draft_set_id, created, record, story_result, main_target=main_target)
            else:
                record = _update_target(
                    record, "STORY", status="BLOCKED_BEFORE_ATTEMPT", outcome=outcome,
                    reason_code=getattr(story_result, "reason_code", None),
                )
                return DispatchResult(draft_set_id, created, record, story_result, main_target=main_target)

        if main_target is None:
            return DispatchResult(draft_set_id, created, record, story_result, main_target=None)

        main_progress = record["targets"].get(main_target, {})
        main_status = main_progress.get("status", "PENDING")

        if main_status in ("SUCCEEDED", "BLOCKED_BEFORE_ATTEMPT", "AMBIGUOUS"):
            # Already serviced (in any terminal sense) -- never repeat.
            return DispatchResult(draft_set_id, created, record, story_result, main_target=main_target)

        assert main_runner is not None, "main_runner is required when the set includes a main target"

        if main_capacity_recheck is not None and not main_capacity_recheck():
            record = _update_target(
                record, main_target, status="BLOCKED_BEFORE_ATTEMPT",
                outcome="MAIN_CAPACITY_UNAVAILABLE_AT_DISPATCH",
                reason_code="MAIN_CAPACITY_UNAVAILABLE_AT_DISPATCH",
            )
            return DispatchResult(draft_set_id, created, record, story_result, main_target=main_target)

        main_result = main_runner(main_target)
        outcome = getattr(main_result, "outcome", None)

        if outcome in _MAIN_SUCCESS_OUTCOMES:
            record = _update_target(
                record, main_target, status="SUCCEEDED",
                manifest_id=getattr(main_result, "manifest_id", None),
                review_post_id=getattr(main_result, "review_post_id", None),
                outcome=outcome,
            )
        elif outcome in _MAIN_AMBIGUOUS_OUTCOMES:
            record = _update_target(
                record, main_target, status="AMBIGUOUS", outcome=outcome,
                reason_code=getattr(main_result, "reason_code", None),
            )
        else:
            record = _update_target(
                record, main_target, status="BLOCKED_BEFORE_ATTEMPT", outcome=outcome,
                reason_code=getattr(main_result, "reason_code", None),
            )

        return DispatchResult(draft_set_id, created, record, story_result, main_result, main_target)


# ---------------------------------------------------------------------------
# #27 domain outcome integration (issue #36 section 28). Not wired into any
# scheduled runner -- that remains #37. Offline-testable in isolation.
# ---------------------------------------------------------------------------


def dispatch_domain_outcome(
    routing_result: dict[str, Any],
    dispatch_result: DispatchResult | None,
) -> tuple[str, str | None, str | None]:
    """Map one routing+dispatch attempt to a #27 (domain_outcome, reason_code, reason_text) triple.

    A routing decision success is not proof that requested drafts exist:
    the decision artifact and the dispatch attempt are checked separately.
    """

    decision = routing_result.get("routing_decision")

    if decision not in ACCELERATED_DECISIONS:
        if routing_result.get("reconciliation_required"):
            return (
                "BLOCKED",
                routing_result.get("reason_code") or "ROUTING_BLOCKED",
                routing_result.get("reason_text") or "Routing decision requires reconciliation.",
            )
        if decision in ("SUPPRESS_DUPLICATE", "SUPPRESS_RECENT_COVERAGE"):
            return ("SUCCEEDED", None, None)  # valid explicit NO_ACTION
        if decision == "NORMAL_QUEUE":
            return ("SUCCEEDED", None, None)  # valid explicit NO_ACTION
        return (
            "BLOCKED",
            routing_result.get("reason_code") or "ROUTING_BLOCKED",
            routing_result.get("reason_text") or "Routing decision blocked.",
        )

    if dispatch_result is None:
        return ("UNKNOWN", "DISPATCH_NOT_ATTEMPTED", "Accelerated routing decision was never dispatched.")

    if dispatch_result.reconciliation_required:
        return ("UNKNOWN", "DRAFT_SET_RECONCILIATION_REQUIRED", "One or more targets returned an ambiguous outcome.")

    targets = dispatch_result.record.get("targets", {})
    for target, progress in targets.items():
        status = progress.get("status")
        if status == "BLOCKED_BEFORE_ATTEMPT":
            return (
                "BLOCKED",
                progress.get("reason_code") or "DRAFT_TARGET_BLOCKED",
                f"Target {target} was blocked before any create attempt.",
            )
        if status not in ("SUCCEEDED",):
            return (
                "FAILED",
                "DRAFT_TARGET_INCOMPLETE",
                f"Target {target} did not reach a terminal SUCCEEDED state.",
            )

    return ("SUCCEEDED", None, None)


# ---------------------------------------------------------------------------
# Self-test (offline, no fixtures, no network) -- registered in run_offline.py
# ---------------------------------------------------------------------------


def self_test() -> int:
    import tempfile
    import nullone_bridge_common as bridge_common

    with tempfile.TemporaryDirectory() as td:
        original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = Path(td)
        try:
            set_id_1 = compute_draft_set_id("event-1", "dev-1")
            set_id_2 = compute_draft_set_id("event-1", "dev-1")
            assert set_id_1 == set_id_2, "non-deterministic draft_set_id"

            set_id_3 = compute_draft_set_id("event-1", "dev-2")
            assert set_id_1 != set_id_3, "different developments collided"

            routing_result = {
                "routing_decision": "IMMEDIATE_STORY_DRAFT",
                "reason_code": "MATERIAL_TIME_VALUE",
                "candidate_id": "cand-1",
                "event": {"event_id": "event-1", "development_id": "dev-1"},
                "draft_targets": ["STORY"],
                "main_draft_justification": None,
            }

            with draft_set_lock(set_id_1):
                record, created = reserve_draft_set(routing_result)
                assert created is True
                record2, created2 = reserve_draft_set(routing_result)
                assert created2 is False
                assert record2["draft_set_id"] == record["draft_set_id"]

            conflicting = dict(routing_result)
            conflicting["routing_decision"] = "IMMEDIATE_STORY_AND_MAIN_DRAFT"
            conflicting["draft_targets"] = ["STORY", "CAROUSEL"]
            try:
                with draft_set_lock(set_id_1):
                    reserve_draft_set(conflicting)
                raise AssertionError("expected DraftSetConflict")
            except DraftSetConflict:
                pass

            # #27 mapping: NORMAL_QUEUE is a valid explicit no-op success.
            outcome, code, text = dispatch_domain_outcome(
                {"routing_decision": "NORMAL_QUEUE", "reason_code": "NORMAL_CADENCE"}, None
            )
            assert outcome == "SUCCEEDED"
        finally:
            bridge_common.WORKSPACE = original_workspace

    print("BREAKING_DISPATCH_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_PUBLISH_CAPABILITY=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Breaking Draft Dispatch V1")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
