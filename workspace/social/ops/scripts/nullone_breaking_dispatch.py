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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import nullone_breaking_identity as identity
import nullone_breaking_router as router
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

# DISPATCH_IN_FLIGHT: written durably immediately before a runner is
# invoked (#36 hardening section 7) so a crash between the external side
# effect and `_update_target` is detectable on restart -- never silently
# replayed. PREVIEW_DELIVERY_FAILED: a review draft/manifest definitely
# exists but the required human-review Telegram preview was not delivered
# (#36 hardening section 5) -- draft existence is not completed review
# workflow, so this is never treated as SUCCEEDED.
TARGET_STATUSES = frozenset(
    {
        "PENDING",
        "DISPATCH_IN_FLIGHT",
        "SUCCEEDED",
        "PREVIEW_DELIVERY_FAILED",
        "BLOCKED_BEFORE_ATTEMPT",
        "AMBIGUOUS",
        "NOT_ATTEMPTED",
    }
)

# Terminal-for-this-set statuses: once a target reaches one of these,
# ordinary dispatch/replay must never invoke its runner again. Only
# BLOCKED_BEFORE_ATTEMPT is eligible for explicit continuation (section 14).
_TARGET_HALTS_DISPATCH = frozenset(
    {"SUCCEEDED", "BLOCKED_BEFORE_ATTEMPT", "AMBIGUOUS", "PREVIEW_DELIVERY_FAILED", "DISPATCH_IN_FLIGHT"}
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


def _validate_routing_artifact(routing_result: Any) -> dict[str, Any]:
    """Strictly validate an incoming routing artifact before any side effect.

    #36 hardening (Blocker B): a caller merely claiming a dict came from
    `nullone_breaking_router.evaluate_routing()` is never trusted on its
    own -- `dispatch_draft_set`, `reserve_draft_set` and
    `continue_unattempted_target` all call this first, before computing or
    reserving any durable target state, writing a draft-set sidecar,
    invoking any recheck, or invoking a Story/main runner. Malformed input
    is rejected as `DispatchRejected`, never silently accepted or
    partially trusted.
    """

    try:
        return router.validate_routing_result_dict(routing_result)
    except router.PolicyInputError as e:
        raise DispatchRejected(
            f"routing_result failed strict {router.SCHEMA} validation: {e}"
        ) from e


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
    # #36 hardening section 11: the exact accepted routing decision is
    # persisted verbatim alongside its own hash; a stored decision that no
    # longer hashes to its own recorded decision_hash is malformed/tampered
    # and must fail closed rather than being trusted.
    stored_decision = record.get("routing_decision_object")
    if not isinstance(stored_decision, dict) or _decision_hash(stored_decision) != record.get("decision_hash"):
        raise DraftSetError(
            f"Draft set record's persisted routing decision does not match its own hash: {path}"
        )
    # #36 hardening (Blocker B): a routing artifact from earlier is not
    # permanent authorization, and a hash-consistent record is not
    # necessarily a semantically valid one -- re-run the strict
    # nullone.breaking-routing.v1 validator on every reload so a
    # tampered/inconsistent stored decision still fails closed even though
    # it matches its own persisted hash.
    try:
        router.validate_routing_result_dict(stored_decision)
    except router.PolicyInputError as e:
        raise DraftSetError(
            f"Draft set record's persisted routing decision failed strict "
            f"{router.SCHEMA} validation despite matching its own hash: {path}"
        ) from e
    return record


def _write_draft_set(record: dict[str, Any]) -> None:
    record["updated_at"] = now_iso()
    atomic_write_json(draft_set_path(record["draft_set_id"]), record)


def _mark_reconciliation_required(record: dict[str, Any]) -> dict[str, Any]:
    if not record.get("reconciliation_required"):
        record["reconciliation_required"] = True
        _write_draft_set(record)
    return record


def reserve_draft_set(
    routing_result: dict[str, Any], *, main_format_reason: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Reserve (or idempotently reuse) the durable draft set for this decision.

    Caller must hold `draft_set_lock(draft_set_id)`. Returns
    `(record, created)`.

    #36 hardening section 12: exact replay is bound to the exact immutable
    accepted decision, not merely to `allowed_targets`. A pre-existing set
    for this development whose persisted `decision_hash` differs from this
    call's decision -- whether from a different candidate/source, a
    severity escalation, a changed main format, or any other field --
    raises `DraftSetConflict` and never overwrites the stored decision or
    runs a runner against it. A second source, escalation or reformatted
    request can never hijack an unfinished set; a truly distinct
    development requires a new development_id from #35.
    """

    routing_result = _validate_routing_artifact(routing_result)

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
        if existing.get("decision_hash") != decision_hash:
            raise DraftSetConflict(
                f"Existing draft set {draft_set_id} was reserved under a "
                f"different accepted routing decision (decision_hash="
                f"{existing.get('decision_hash')!r}); a new request "
                f"(decision_hash={decision_hash!r}) for the same development "
                "is rejected -- no automatic upgrade/downgrade/hijack of an "
                "already-reserved development. Reconciliation required."
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
        "assessment_ref": routing_result.get("assessment_ref"),
        "state_snapshot_ref": routing_result.get("state_snapshot_ref"),
        "routing_decision": routing_result.get("routing_decision"),
        "reason_code": routing_result.get("reason_code"),
        # #36 hardening section 11: the complete validated
        # nullone.breaking-routing.v1 decision object, persisted verbatim
        # (not just a hash + selected fields) BEFORE any target dispatch,
        # so it can be re-verified byte-for-byte on every reload.
        "routing_decision_object": dict(routing_result),
        "decision_hash": decision_hash,
        "allowed_targets": allowed_targets,
        "main_draft_justification": routing_result.get("main_draft_justification"),
        # #36 hardening section 13: durable FEED/CAROUSEL structural
        # selection reason, kept as draft-set audit metadata outside the
        # strict nullone.breaking-routing.v1 schema object (which never
        # grows a 15th field) -- distinct from main_draft_justification
        # (why standalone main has audience value at all).
        "main_format_reason": main_format_reason,
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


@dataclass(frozen=True)
class RecheckResult:
    """Outcome of one `AuthoritativeDispatchRecheck` call.

    `permitted=False` means: do not create this target now. `reason_code`/
    `reason_text` are recorded on the target's BLOCKED_BEFORE_ATTEMPT
    status. `reconciliation_required=True` additionally flags the whole
    draft set for manual reconciliation (e.g. the fresh state itself is
    ambiguous/conflicting, not merely suppressing).
    """

    permitted: bool
    reason_code: str | None = None
    reason_text: str | None = None
    reconciliation_required: bool = False


class AuthoritativeDispatchRecheck(Protocol):
    def __call__(self, *, stage: str, record: dict[str, Any]) -> RecheckResult:
        """Read-only recheck of authoritative state immediately before dispatch.

        Called once before the first Story dispatch (`stage="STORY"`) and
        again immediately before main dispatch (`stage=<FEED|CAROUSEL>`),
        always with the current durable draft-set `record` so an
        implementation can distinguish this set's own already-created
        Story/main manifests (an expected, intentionally-allowed sibling)
        from any external/equivalent state it does not own. A routing
        artifact from earlier is not permanent authorization -- this is
        mandatory for every accelerated dispatch, never optional.
        """


def make_state_authoritative_recheck(
    candidate_input: "identity.CandidateInput",
    workspace: Path,
    *,
    prior_developments: Any = None,
    prior_developments_path: Path | None = None,
) -> AuthoritativeDispatchRecheck:
    """Build a real `AuthoritativeDispatchRecheck` reusing #35 semantics exactly.

    Reuses `nullone_breaking_identity.load_repository_state(...)` and
    `.evaluate(...)` verbatim -- this never reimplements or forks #35
    matching/precedence rules. `candidate_input` must be the exact
    `CandidateInput` the caller already built for the original #35
    evaluation (identity is a pure function of it; only the freshly-read
    `RepositoryState` can change between routing and dispatch).

    For the main stage, a fresh suppressing match against exactly this
    draft set's own Story manifest (already recorded on `record`) is the
    expected sibling and is permitted; any other suppressing/ambiguous
    match blocks.
    """

    def _recheck(*, stage: str, record: dict[str, Any]) -> RecheckResult:
        state = identity.load_repository_state(
            workspace,
            prior_developments=prior_developments,
            prior_developments_path=prior_developments_path,
        )
        fresh = identity.evaluate(candidate_input, state)

        if fresh.reconciliation_required or fresh.reason_code in router._AMBIGUOUS_CODES:
            return RecheckResult(
                permitted=False,
                reason_code=fresh.reason_code,
                reason_text=fresh.reason_text,
                reconciliation_required=True,
            )

        if fresh.reason_code in router._SUPPRESS_CODES:
            if stage != "STORY":
                story_manifest_id = record.get("targets", {}).get("STORY", {}).get("manifest_id")
                owned_ref = f"manifest:{story_manifest_id}" if story_manifest_id else None
                matched_refs = list(fresh.dedup.get("matched_refs") or [])
                if owned_ref and matched_refs and all(ref == owned_ref for ref in matched_refs):
                    return RecheckResult(permitted=True)
            return RecheckResult(
                permitted=False, reason_code=fresh.reason_code, reason_text=fresh.reason_text
            )

        return RecheckResult(permitted=True)

    return _recheck


_DRAFT_CREATED_OUTCOME = "DRAFT_CREATED"
_PREVIEW_DELIVERY_FAILED_OUTCOME = "PREVIEW_DELIVERY_FAILED"
_PIPELINE_AMBIGUOUS_OUTCOMES = frozenset(
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


def _preview_delivery_failure_reason(preview_delivery: Any) -> str | None:
    """Return None if `preview_delivery` proves Telegram SENT; otherwise a
    specific review-delivery failure reason code.

    #36 hardening (Blocker A): a review draft/manifest existing is never
    proof that the required human-review Telegram preview was delivered.
    Missing delivery proof is never classified as UNKNOWN -- when a sender
    was never supplied (or its result cannot be validated), the workflow
    has definitively not proven delivery, which is exactly as much a
    review-delivery failure as an explicit non-SENT status.
    """

    if not isinstance(preview_delivery, Mapping):
        return "PREVIEW_DELIVERY_UNPROVEN"
    status = preview_delivery.get("status")
    if status == "SENT":
        return None
    if isinstance(status, str) and status:
        return "PREVIEW_NOT_SENT"
    return "PREVIEW_DELIVERY_UNPROVEN"


def _classify_pipeline_outcome(
    record: dict[str, Any],
    target: str,
    result: Any,
) -> dict[str, Any]:
    """Apply one #33/#36 pipeline result to `target`'s durable status.

    DRAFT_CREATED -> SUCCEEDED only with independent proof of Telegram
    delivery (`preview_delivery.status == "SENT"`); a DRAFT_CREATED result
    with `preview_delivery` missing, malformed, or reporting anything
    other than SENT is a definite review-delivery failure (Blocker A) --
    the review draft/manifest exists and is never recreated, no automatic
    Telegram resend is attempted, and this is classified as
    PREVIEW_DELIVERY_FAILED, never SUCCEEDED and never UNKNOWN.
    PREVIEW_DELIVERY_FAILED (the pipeline's own native outcome, e.g. an
    explicit sender failure) -> its own terminal status for the same
    reason: the review draft/manifest definitely exists, but the required
    human-review Telegram preview was not delivered, so it is never
    SUCCEEDED. A recognized ambiguous pipeline outcome -> AMBIGUOUS
    (reconciliation required, never auto-retried). Anything else
    (including a malformed/unrecognized result object, which proves
    nothing about whether a side effect occurred) fails closed to
    AMBIGUOUS rather than being assumed untouched, EXCEPT the pipelines'
    own well-known before-create-attempt outcomes, which do prove no
    review-create attempt was consumed.
    """

    outcome = getattr(result, "outcome", None)
    manifest_id = getattr(result, "manifest_id", None)
    review_post_id = getattr(result, "review_post_id", None)
    reason_code = getattr(result, "reason_code", None)
    preview_delivery = getattr(result, "preview_delivery", None)

    if outcome == _DRAFT_CREATED_OUTCOME:
        delivery_failure_reason = _preview_delivery_failure_reason(preview_delivery)
        if delivery_failure_reason is None:
            return _update_target(
                record, target, status="SUCCEEDED",
                manifest_id=manifest_id, review_post_id=review_post_id, outcome=outcome,
            )
        return _update_target(
            record, target, status="PREVIEW_DELIVERY_FAILED",
            manifest_id=manifest_id, review_post_id=review_post_id,
            outcome=outcome, reason_code=delivery_failure_reason,
        )
    if outcome == _PREVIEW_DELIVERY_FAILED_OUTCOME:
        return _update_target(
            record, target, status="PREVIEW_DELIVERY_FAILED",
            manifest_id=manifest_id, review_post_id=review_post_id,
            outcome=outcome,
            reason_code=_preview_delivery_failure_reason(preview_delivery) or "PREVIEW_NOT_SENT",
        )
    if outcome in _PIPELINE_AMBIGUOUS_OUTCOMES:
        return _update_target(
            record, target, status="AMBIGUOUS", outcome=outcome, reason_code=reason_code,
        )
    if isinstance(outcome, str) and outcome in _KNOWN_BEFORE_ATTEMPT_OUTCOMES:
        return _update_target(
            record, target, status="BLOCKED_BEFORE_ATTEMPT", outcome=outcome, reason_code=reason_code,
        )
    # Unrecognized/malformed result: cannot prove no side effect occurred.
    return _update_target(
        record, target, status="AMBIGUOUS",
        outcome="RUNNER_RESULT_UNRECOGNIZED", reason_code=str(outcome) if outcome else None,
    )


# Pipeline outcomes that are provably reached BEFORE any review-create
# attempt is consumed (see nullone_story_pipeline.py / #36
# nullone_main_draft_pipeline.py outcome sets) -- safe to treat as
# BLOCKED_BEFORE_ATTEMPT. Anything not in this set and not a recognized
# success/ambiguous outcome is NOT assumed safe; it fails closed to
# AMBIGUOUS instead.
_KNOWN_BEFORE_ATTEMPT_OUTCOMES = frozenset(
    {
        "CANDIDATE_NOT_ELIGIBLE",
        "REVISION_PARENT_INVALID",
        "REVISION_SUPERSESSION_CONFLICT",
        "WRITER_FAILED",
        "WRITER_OUTPUT_INVALID",
        "VERIFIER_FAILED",
        "VERIFICATION_BLOCKED",
        "STORY_SPEC_BLOCKED",
        "MAIN_SPEC_BLOCKED",
        "MAIN_SPEC_CONFLICT",
        "RENDER_FAILED",
        "MANIFEST_BLOCKED",
        "REVIEW_DRAFT_BLOCKED_BEFORE_ATTEMPT",
    }
)


def _run_story(
    record: dict[str, Any],
    story_runner: StoryRunner,
    authoritative_recheck: AuthoritativeDispatchRecheck,
) -> tuple[dict[str, Any], Any | None]:
    """Attempt Story exactly once. Caller must ensure STORY is PENDING."""

    recheck = authoritative_recheck(stage="STORY", record=record)
    if not recheck.permitted:
        record = _update_target(
            record, "STORY", status="BLOCKED_BEFORE_ATTEMPT",
            outcome="AUTHORITATIVE_RECHECK_BLOCKED",
            reason_code=recheck.reason_code or "AUTHORITATIVE_RECHECK_BLOCKED",
        )
        if recheck.reconciliation_required:
            record = _mark_reconciliation_required(record)
        return record, None

    # #36 hardening section 7: write a durable IN_FLIGHT reservation
    # BEFORE invoking the runner, so a crash between an external side
    # effect and `_update_target` is never silently replayed on restart.
    record = _update_target(record, "STORY", status="DISPATCH_IN_FLIGHT")
    try:
        story_result = story_runner()
    except Exception as e:
        # Unexpected exception: no read-only recovery is available here,
        # so this fails closed to AMBIGUOUS (never back to PENDING, never
        # auto-retried) rather than guessing the runner had no side effect.
        record = _update_target(
            record, "STORY", status="AMBIGUOUS",
            outcome="RUNNER_EXCEPTION", reason_code=type(e).__name__,
        )
        return record, None

    record = _classify_pipeline_outcome(record, "STORY", story_result)
    return record, story_result


def _run_main(
    record: dict[str, Any],
    main_target: str,
    main_runner: MainRunner,
    authoritative_recheck: AuthoritativeDispatchRecheck,
    main_capacity_recheck: MainCapacityRecheck,
) -> tuple[dict[str, Any], Any | None]:
    """Attempt main exactly once. Caller must ensure `main_target` is PENDING."""

    if not main_capacity_recheck():
        record = _update_target(
            record, main_target, status="BLOCKED_BEFORE_ATTEMPT",
            outcome="MAIN_CAPACITY_UNAVAILABLE_AT_DISPATCH",
            reason_code="MAIN_CAPACITY_UNAVAILABLE_AT_DISPATCH",
        )
        return record, None

    recheck = authoritative_recheck(stage=main_target, record=record)
    if not recheck.permitted:
        record = _update_target(
            record, main_target, status="BLOCKED_BEFORE_ATTEMPT",
            outcome="AUTHORITATIVE_RECHECK_BLOCKED",
            reason_code=recheck.reason_code or "AUTHORITATIVE_RECHECK_BLOCKED",
        )
        if recheck.reconciliation_required:
            record = _mark_reconciliation_required(record)
        return record, None

    record = _update_target(record, main_target, status="DISPATCH_IN_FLIGHT")
    try:
        main_result = main_runner(main_target)
    except Exception as e:
        record = _update_target(
            record, main_target, status="AMBIGUOUS",
            outcome="RUNNER_EXCEPTION", reason_code=type(e).__name__,
        )
        return record, None

    record = _classify_pipeline_outcome(record, main_target, main_result)
    return record, main_result


def dispatch_draft_set(
    routing_result: dict[str, Any],
    *,
    story_runner: StoryRunner,
    authoritative_recheck: AuthoritativeDispatchRecheck,
    main_runner: MainRunner | None = None,
    main_capacity_recheck: MainCapacityRecheck | None = None,
    main_format_reason: str | None = None,
) -> DispatchResult:
    """Reserve (or reuse) the draft set and dispatch Story-first.

    Rechecks the durable set's own per-target progress before invoking any
    runner, so exact replay of an already-serviced set never repeats a
    target. Story is always attempted before main; main is attempted only
    once Story has fully reached the human-review boundary (SUCCEEDED --
    draft created AND preview delivered). A Story outcome of
    PREVIEW_DELIVERY_FAILED means a review draft definitely exists but
    human-review delivery is incomplete: it is recorded on its own
    terminal status and STOPS the whole set -- main is never attempted as
    compensation, and this is never treated as SUCCEEDED. A
    blocked-before-attempt or ambiguous Story outcome likewise stops the
    whole set.

    `authoritative_recheck` is mandatory for every accelerated dispatch
    (issue #36 section 9): a routing artifact from earlier is not
    permanent authorization, and this dispatcher never reimplements #35
    identity/dedup semantics itself. `main_capacity_recheck` is mandatory
    whenever the set includes a main target (section 10) -- its absence is
    never interpreted as PASS; it blocks the main target with an explicit
    dependency/safety reason instead.

    `routing_result` is strictly validated (Blocker B) before any durable
    state is touched -- a caller merely claiming it came from
    `evaluate_routing()` is never trusted on its own.
    """

    routing_result = _validate_routing_artifact(routing_result)

    event = routing_result.get("event") or {}
    event_id = event.get("event_id")
    development_id = event.get("development_id")
    draft_set_id = compute_draft_set_id(event_id, development_id)

    with draft_set_lock(draft_set_id):
        record, created = reserve_draft_set(routing_result, main_format_reason=main_format_reason)
        allowed_targets = record["allowed_targets"]

        story_result = None
        main_result = None
        main_target = next((t for t in allowed_targets if t != "STORY"), None)

        story_status = record["targets"].get("STORY", {}).get("status", "PENDING")

        if story_status == "SUCCEEDED":
            pass
        elif story_status in _TARGET_HALTS_DISPATCH:
            if story_status == "DISPATCH_IN_FLIGHT":
                # #36 hardening section 7: an IN_FLIGHT reservation found
                # on (re)entry (e.g. after a process restart) is unsafe to
                # resume automatically -- it requires reconciliation unless
                # a deterministic read-only recovery proves the exact
                # outcome, which this dispatcher does not attempt.
                record = _mark_reconciliation_required(record)
            return DispatchResult(draft_set_id, created, record, main_target=main_target)
        else:
            record, story_result = _run_story(record, story_runner, authoritative_recheck)
            if record["targets"]["STORY"]["status"] != "SUCCEEDED":
                return DispatchResult(draft_set_id, created, record, story_result, main_target=main_target)

        if main_target is None:
            return DispatchResult(draft_set_id, created, record, story_result, main_target=None)

        main_status = record["targets"].get(main_target, {}).get("status", "PENDING")

        if main_status in _TARGET_HALTS_DISPATCH:
            if main_status == "DISPATCH_IN_FLIGHT":
                record = _mark_reconciliation_required(record)
            return DispatchResult(draft_set_id, created, record, story_result, main_target=main_target)

        assert main_runner is not None, "main_runner is required when the set includes a main target"

        if main_capacity_recheck is None:
            # #36 hardening section 10: absence of the mandatory main
            # capacity recheck is never interpreted as PASS.
            record = _update_target(
                record, main_target, status="BLOCKED_BEFORE_ATTEMPT",
                outcome="MAIN_CAPACITY_RECHECK_MISSING",
                reason_code="MAIN_CAPACITY_RECHECK_MISSING",
            )
            return DispatchResult(draft_set_id, created, record, story_result, main_target=main_target)

        record, main_result = _run_main(
            record, main_target, main_runner, authoritative_recheck, main_capacity_recheck
        )
        return DispatchResult(draft_set_id, created, record, story_result, main_result, main_target)


def continue_unattempted_target(
    routing_result: dict[str, Any],
    target: str,
    *,
    authoritative_recheck: AuthoritativeDispatchRecheck,
    story_runner: StoryRunner | None = None,
    main_runner: MainRunner | None = None,
    main_capacity_recheck: MainCapacityRecheck | None = None,
) -> DispatchResult:
    """Explicitly continue exactly one target proven never-attempted (section 14).

    Accepted #34 semantics allow continuation of an existing draft set only
    for a target proven `BLOCKED_BEFORE_ATTEMPT` -- never SUCCEEDED (would
    repeat a completed target), never AMBIGUOUS/DISPATCH_IN_FLIGHT/
    PREVIEW_DELIVERY_FAILED (a review-create attempt may have been
    consumed; manual reconciliation is required instead). Ordinary replay
    (`dispatch_draft_set`) never performs this -- it is a distinct, only
    explicitly-invoked continuation path.

    Requires the exact persisted draft set and exact persisted routing
    decision (bound by `decision_hash`, matching section 12's exact-replay
    binding): a caller with a different decision for the same development
    gets `DraftSetConflict`, never a silent continuation under drifted
    terms. Main can only continue once Story has reached SUCCEEDED; Story
    continuation never repeats main. Fresh `authoritative_recheck` and (for
    main) `main_capacity_recheck` are re-applied exactly as in ordinary
    dispatch -- continuation grants no bypass of either safety gate.

    `routing_result` is strictly validated (Blocker B) before any durable
    state is touched, same as `dispatch_draft_set`.
    """

    routing_result = _validate_routing_artifact(routing_result)

    event = routing_result.get("event") or {}
    event_id = event.get("event_id")
    development_id = event.get("development_id")
    draft_set_id = compute_draft_set_id(event_id, development_id)

    with draft_set_lock(draft_set_id):
        record = load_draft_set(draft_set_id)
        if record is None:
            raise DraftSetError(f"No existing draft set to continue: {draft_set_id}")

        if record.get("decision_hash") != _decision_hash(routing_result):
            raise DraftSetConflict(
                "continue_unattempted_target: incoming routing decision does "
                "not match this draft set's persisted accepted decision -- "
                "a second source, escalation or reformatted request cannot "
                "hijack continuation of an unfinished set."
            )

        if target not in record.get("allowed_targets", []):
            raise DraftSetError(f"Target {target!r} is not part of this draft set's allowed_targets.")

        progress = record["targets"].get(target, {})
        if progress.get("status") != "BLOCKED_BEFORE_ATTEMPT":
            raise DraftSetError(
                f"continue_unattempted_target refused: target {target!r} status "
                f"is {progress.get('status')!r}, not a proven never-attempted block."
            )

        main_target = next((t for t in record["allowed_targets"] if t != "STORY"), None)

        if target == "STORY":
            assert story_runner is not None, "story_runner is required to continue STORY"
            record, story_result = _run_story(record, story_runner, authoritative_recheck)
            return DispatchResult(draft_set_id, False, record, story_result, main_target=main_target)

        story_status = record["targets"].get("STORY", {}).get("status")
        if story_status != "SUCCEEDED":
            raise DraftSetError(
                "continue_unattempted_target refused: main target cannot "
                "continue until Story has reached SUCCEEDED."
            )
        assert main_runner is not None, "main_runner is required to continue a main target"

        if main_capacity_recheck is None:
            record = _update_target(
                record, target, status="BLOCKED_BEFORE_ATTEMPT",
                outcome="MAIN_CAPACITY_RECHECK_MISSING",
                reason_code="MAIN_CAPACITY_RECHECK_MISSING",
            )
            return DispatchResult(draft_set_id, False, record, main_target=target)

        record, main_result = _run_main(
            record, target, main_runner, authoritative_recheck, main_capacity_recheck
        )
        return DispatchResult(draft_set_id, False, record, main_result=main_result, main_target=target)


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
        if status == "SUCCEEDED":
            continue
        if status == "BLOCKED_BEFORE_ATTEMPT":
            return (
                "BLOCKED",
                progress.get("reason_code") or "DRAFT_TARGET_BLOCKED",
                f"Target {target} was blocked before any create attempt.",
            )
        if status == "PREVIEW_DELIVERY_FAILED":
            # Draft existence is not completed review workflow: a review
            # draft/manifest exists for `target`, but the required
            # human-review Telegram preview was not delivered. Never
            # SUCCEEDED.
            return (
                "FAILED",
                "PREVIEW_DELIVERY_FAILED",
                f"Target {target} has a review draft, but human-review preview "
                "delivery failed; the review workflow did not complete.",
            )
        if status in ("AMBIGUOUS", "DISPATCH_IN_FLIGHT"):
            # Defense in depth: dispatch_result.reconciliation_required
            # already catches this above in the normal case.
            return (
                "UNKNOWN",
                "DRAFT_TARGET_RECONCILIATION_REQUIRED",
                f"Target {target} requires reconciliation before an outcome can be proven.",
            )
        return (
            "FAILED",
            "DRAFT_TARGET_INCOMPLETE",
            f"Target {target} did not reach a terminal SUCCEEDED state.",
        )

    return ("SUCCEEDED", None, None)


# ---------------------------------------------------------------------------
# Self-test (offline, no fixtures, no network) -- registered in run_offline.py
# ---------------------------------------------------------------------------


def _self_test_routing_result(
    *,
    decision="IMMEDIATE_STORY_DRAFT",
    targets=("STORY",),
    event_id="event-1",
    development_id="dev-1",
    candidate_id="cand-1",
    reason_code="MATERIAL_TIME_VALUE",
    main_justification=None,
) -> dict[str, Any]:
    return {
        "schema": router.SCHEMA,
        "candidate_id": candidate_id,
        "assessment_ref": "assess-1",
        "state_snapshot_ref": "state-1",
        "severity": "MATERIAL_BREAKING" if len(targets) == 1 else "EXCEPTIONAL_BREAKING",
        "event": {
            "event_id": event_id,
            "development_id": development_id,
            "topic_id": "topic-1",
            "identity_basis": "EXACT_IDENTIFIER",
            "identity_refs": ["ref-1"],
        },
        "verification": {"state": "PASS", "evidence_refs": ["ev-1"]},
        "dedup": {
            "decision": "DISTINCT_EVENT",
            "matched_refs": [],
            "parent_development_id": None,
            "follow_up_reason": None,
        },
        "routing_decision": decision,
        "reason_code": reason_code,
        "reason_text": "test",
        "draft_targets": list(targets),
        "main_draft_justification": main_justification,
        "reconciliation_required": False,
    }


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

            routing_result = _self_test_routing_result()

            with draft_set_lock(set_id_1):
                record, created = reserve_draft_set(routing_result)
                assert created is True
                record2, created2 = reserve_draft_set(routing_result)
                assert created2 is False
                assert record2["draft_set_id"] == record["draft_set_id"]

            conflicting = _self_test_routing_result(
                decision="IMMEDIATE_STORY_AND_MAIN_DRAFT",
                targets=("STORY", "CAROUSEL"),
                reason_code="EXCEPTIONAL_MAIN_VALUE",
                main_justification="Distinct standalone value.",
            )
            try:
                with draft_set_lock(set_id_1):
                    reserve_draft_set(conflicting)
                raise AssertionError("expected DraftSetConflict")
            except DraftSetConflict:
                pass

            # Blocker B: a malformed/unaccepted routing artifact is rejected
            # before any durable state is touched, never silently trusted.
            malformed = dict(routing_result)
            malformed["main_format_reason"] = "not part of the strict schema"
            try:
                dispatch_draft_set(
                    malformed,
                    story_runner=lambda: (_ for _ in ()).throw(AssertionError("must not run")),
                    authoritative_recheck=lambda *, stage, record: RecheckResult(permitted=True),
                )
                raise AssertionError("expected DispatchRejected")
            except DispatchRejected:
                pass

            # #27 mapping: NORMAL_QUEUE is a valid explicit no-op success.
            outcome, code, text = dispatch_domain_outcome(
                {"routing_decision": "NORMAL_QUEUE", "reason_code": "NORMAL_CADENCE"}, None
            )
            assert outcome == "SUCCEEDED"

            def _always_permit(*, stage, record):
                return RecheckResult(permitted=True)

            _unset = object()

            class _Result:
                def __init__(self, outcome, manifest_id=None, review_post_id=None, preview_delivery=_unset):
                    self.outcome = outcome
                    self.manifest_id = manifest_id
                    self.review_post_id = review_post_id
                    self.reason_code = None
                    if preview_delivery is _unset:
                        preview_delivery = {"status": "SENT"} if outcome == _DRAFT_CREATED_OUTCOME else None
                    self.preview_delivery = preview_delivery

            set_id_4 = compute_draft_set_id("event-2", "dev-2")
            story_only = _self_test_routing_result(
                event_id="event-2", development_id="dev-2", candidate_id="cand-2",
            )
            dr = dispatch_draft_set(
                story_only,
                story_runner=lambda: _Result("PREVIEW_DELIVERY_FAILED", "m-1", "r-1"),
                authoritative_recheck=_always_permit,
            )
            assert dr.record["targets"]["STORY"]["status"] == "PREVIEW_DELIVERY_FAILED"
            outcome, code, text = dispatch_domain_outcome(story_only, dr)
            assert outcome == "FAILED", "PREVIEW_DELIVERY_FAILED must never map to SUCCEEDED"

            # Blocker A: DRAFT_CREATED without independent SENT proof must
            # never become target SUCCEEDED -- a sender that was never
            # supplied is a definite, not an unknown, delivery failure.
            set_id_4b = compute_draft_set_id("event-2b", "dev-2b")
            story_only_b = _self_test_routing_result(
                event_id="event-2b", development_id="dev-2b", candidate_id="cand-2b",
            )
            dr_b = dispatch_draft_set(
                story_only_b,
                story_runner=lambda: _Result("DRAFT_CREATED", "m-1b", "r-1b", preview_delivery=None),
                authoritative_recheck=_always_permit,
            )
            assert dr_b.record["targets"]["STORY"]["status"] == "PREVIEW_DELIVERY_FAILED"
            assert dr_b.record["targets"]["STORY"]["reason_code"] == "PREVIEW_DELIVERY_UNPROVEN"

            # Missing mandatory main_capacity_recheck blocks, never PASSes silently.
            set_id_5 = compute_draft_set_id("event-3", "dev-3")
            story_and_main = _self_test_routing_result(
                decision="IMMEDIATE_STORY_AND_MAIN_DRAFT",
                targets=("STORY", "FEED"),
                event_id="event-3", development_id="dev-3", candidate_id="cand-3",
                reason_code="EXCEPTIONAL_MAIN_VALUE",
                main_justification="Distinct standalone value.",
            )
            dr2 = dispatch_draft_set(
                story_and_main,
                story_runner=lambda: _Result("DRAFT_CREATED", "m-story", "r-story"),
                main_runner=lambda fmt: _Result("DRAFT_CREATED", "m-main", "r-main"),
                authoritative_recheck=_always_permit,
            )
            assert dr2.record["targets"]["FEED"]["status"] == "BLOCKED_BEFORE_ATTEMPT"
            assert dr2.record["targets"]["FEED"]["reason_code"] == "MAIN_CAPACITY_RECHECK_MISSING"
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
