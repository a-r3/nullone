#!/usr/bin/env python3
"""NullOne `StoryWorkflow` application orchestrator (#62).

Per `docs/architecture/nullone-application-runtime.md`, this module is the
NullOne Application Runtime layer for the Story workflow:

    validated trigger
    -> authoritative state read (#32 cadence state adapter)
    -> #32 cadence request/evaluation
    -> PREPARE_STORY?
    -> deterministic candidate boundary (#62 candidate provider)
    -> exactly one already-verified eligible candidate
    -> #33 Story core (nullone_story_pipeline.run_story_pipeline)
    -> DraftProvider (#33's existing DraftConnector) + ReviewDelivery
    -> truthful StoryWorkflowResult

It reuses #31/#32/#33 domain logic verbatim -- it does not reimplement
cadence arithmetic, Story rendering, Story request/version identity, Story
supersession, manifest construction, verification semantics, or the
review-attempt guard. This module's own job is strictly composition:
trigger normalization, cadence gating, the candidate-availability
consistency check, and unifying the result into one typed
`StoryWorkflowResult`.

Critical dependency rule (restated, enforced by
`tests/test_story_workflow_capability_negative.py`): this module must
never import OpenClaw internals, invoke `openclaw`, know Telegram CLI
syntax, know Zernio MCP/REST details, know cron syntax/job UUIDs, publish,
approve, schedule, or process approval callbacks. Provider/transport
details stop at the injected `DraftConnector`/`ReviewDelivery` adapters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from nullone_cadence_controller import CadenceContractError, evaluate_cadence
from nullone_cadence_state_adapter import CadenceStateError, assemble_cadence_request
from nullone_scheduler_invocation import SchedulerInvocationError, accept_workflow_trigger
from nullone_story_candidate_provider import (
    SELECTION_AMBIGUOUS,
    SELECTION_INVALID,
    SELECTION_OK,
    SELECTION_UNAVAILABLE,
    StoryCandidateProvider,
    select_single_candidate,
)
from nullone_story_pipeline import STORY_PIPELINE_OUTCOMES, StoryPipelineResult, run_story_pipeline

STORY_WORKFLOW_ID = "story"

ACCEPTED_CADENCE_RECOMMENDATION = "PREPARE_STORY"
ACCEPTED_CADENCE_REASON_CODE = "STORY_GAP"
ACCEPTED_CADENCE_PERMITTED_ACTION = "CANDIDATE_SEARCH_AND_PREPARE"

# Workflow-level outcomes that are NOT #33 pipeline outcomes -- everything
# past candidate selection reuses STORY_PIPELINE_OUTCOMES verbatim rather
# than reinventing a parallel enum.
WORKFLOW_LEVEL_OUTCOMES = frozenset(
    {
        "TRIGGER_REJECTED",
        "STATE_BLOCKED",
        "NO_ACTION",
        "CANDIDATE_UNAVAILABLE",
        "CANDIDATE_INVALID",
        "CANDIDATE_AMBIGUOUS",
        "CANDIDATE_PROVIDER_FAILED",
    }
)

STORY_WORKFLOW_OUTCOMES = WORKFLOW_LEVEL_OUTCOMES | STORY_PIPELINE_OUTCOMES

_SELECTION_TO_WORKFLOW_OUTCOME = {
    SELECTION_UNAVAILABLE: "CANDIDATE_UNAVAILABLE",
    SELECTION_INVALID: "CANDIDATE_INVALID",
    SELECTION_AMBIGUOUS: "CANDIDATE_AMBIGUOUS",
}


class StoryWorkflowError(RuntimeError):
    """Caller contract violation building a StoryWorkflowResult (never raised
    for a legitimate blocked/no-action outcome -- those are returned, not
    raised)."""


@dataclass
class StoryWorkflowResult:
    outcome: str
    reason_text: str
    cadence: dict[str, Any] | None = None
    candidate_id: str | None = None
    story_pipeline: StoryPipelineResult | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome not in STORY_WORKFLOW_OUTCOMES:
            raise StoryWorkflowError(f"Unknown StoryWorkflow outcome: {self.outcome!r}")


def _result(outcome: str, reason_text: str, **kwargs: Any) -> StoryWorkflowResult:
    return StoryWorkflowResult(outcome=outcome, reason_text=reason_text, **kwargs)


def run_story_workflow(
    trigger: dict[str, Any],
    *,
    state_root: Path,
    now: datetime,
    candidate_availability: dict[str, bool],
    candidate_provider: StoryCandidateProvider,
    writer: Any,
    verifier: Any,
    draft_connector: Any,
    review_delivery: Any,
    cadence_config: dict[str, Any] | None = None,
    cadence_signal: dict[str, Any] | None = None,
    timezone_name: str = "Asia/Baku",
) -> StoryWorkflowResult:
    """Run one Story workflow occurrence end to end (review-draft boundary only).

    `candidate_availability` is the same opaque, externally-computed
    `nullone.cadence-contract.v1` input the cadence contract already
    documents ("computed upstream ... consumed here as an opaque boolean
    input") -- this workflow does not compute it by calling
    `candidate_provider` speculatively. This keeps the NO_ACTION /
    PREPARE_MAIN_CANDIDATE / malformed-state paths below genuinely free of
    any candidate-provider call, and it is exactly what makes the
    candidate-availability-consistency check meaningful: if the caller's
    supplied signal said a Story candidate was available but
    `candidate_provider` then returns none, that is a real contradiction
    (see `CANDIDATE_UNAVAILABLE` below), never silently patched over.

    Returns a `StoryWorkflowResult`; never raises for a legitimate blocked,
    no-action, or ambiguous outcome. Zero draft/candidate-provider/review-
    delivery calls occur for an invalid trigger, malformed/blocked state,
    `NO_ACTION`, or `PREPARE_MAIN_CANDIDATE`.
    """

    try:
        accept_workflow_trigger(trigger, workflow_id=STORY_WORKFLOW_ID)
    except SchedulerInvocationError as exc:
        return _result(
            "TRIGGER_REJECTED",
            f"Scheduler invocation trigger rejected: {exc}",
            context={"trigger": _safe_trigger_context(trigger)},
        )

    try:
        cadence_request = assemble_cadence_request(
            state_root=state_root,
            now=now,
            candidate_availability=candidate_availability,
            config=cadence_config,
            signal=cadence_signal,
            timezone_name=timezone_name,
        )
        cadence_response = evaluate_cadence(cadence_request)
    except (CadenceStateError, CadenceContractError) as exc:
        return _result(
            "STATE_BLOCKED",
            f"Authoritative cadence state could not be assembled/evaluated: {exc}",
            context={"trigger": _safe_trigger_context(trigger)},
        )

    if (
        cadence_response["recommendation"] != ACCEPTED_CADENCE_RECOMMENDATION
        or cadence_response["reason_code"] != ACCEPTED_CADENCE_REASON_CODE
        or cadence_response["permitted_action"] != ACCEPTED_CADENCE_PERMITTED_ACTION
    ):
        return _result(
            "NO_ACTION",
            f"Cadence did not recommend PREPARE_STORY/STORY_GAP: "
            f"{cadence_response['recommendation']}/{cadence_response['reason_code']}",
            cadence=cadence_response,
        )

    # Only past this point may the candidate provider, writer, verifier,
    # draft connector, or review-delivery adapter ever be invoked.

    try:
        raw_candidates = candidate_provider.get_candidates()
    except Exception as exc:  # noqa: BLE001 - provider failure is a typed outcome, not a crash
        return _result(
            "CANDIDATE_PROVIDER_FAILED",
            "Story candidate provider failed.",
            cadence=cadence_response,
            context={"error_type": type(exc).__name__},
        )

    candidate, selection_outcome, selection_context = select_single_candidate(raw_candidates)

    if selection_outcome != SELECTION_OK:
        workflow_outcome = _SELECTION_TO_WORKFLOW_OUTCOME[selection_outcome]
        reason_text = {
            "CANDIDATE_UNAVAILABLE": (
                "Cadence signaled a Story quality candidate was available, but "
                "the candidate provider returned none. Refusing to fabricate "
                "content to satisfy cadence quota."
            ),
            "CANDIDATE_INVALID": (
                "Every candidate returned by the provider failed independent "
                "#33 admission (verification/evidence/required fields)."
            ),
            "CANDIDATE_AMBIGUOUS": (
                "Multiple independently-eligible candidates were returned with "
                "no existing accepted deterministic ordering; refusing to guess."
            ),
        }[workflow_outcome]
        return _result(
            workflow_outcome,
            reason_text,
            cadence=cadence_response,
            context=selection_context,
        )

    assert candidate is not None

    pipeline_result = run_story_pipeline(
        candidate,
        writer=writer,
        verifier=verifier,
        draft_connector=draft_connector,
        telegram_sender=review_delivery,
    )

    return _result(
        pipeline_result.outcome,
        pipeline_result.reason_text,
        cadence=cadence_response,
        candidate_id=candidate.get("candidate_id"),
        story_pipeline=pipeline_result,
        context=selection_context,
    )


def _safe_trigger_context(trigger: Any) -> dict[str, Any]:
    """Echo only non-sensitive, already-public trigger fields for diagnosis.

    Never assumes `trigger` parsed successfully -- this is called from the
    TRIGGER_REJECTED path, where `trigger` may be malformed or not even a
    dict.
    """

    if not isinstance(trigger, dict):
        return {}
    return {
        key: trigger.get(key)
        for key in ("workflow_id", "source", "scheduled_for", "occurrence_id")
        if key in trigger
    }


def self_test() -> int:
    import shutil
    import tempfile
    from datetime import timezone
    from zoneinfo import ZoneInfo

    import nullone_bridge_common as bridge_common
    from nullone_review_delivery import FakeReviewDelivery
    from nullone_scheduler_invocation import compute_occurrence_id
    from nullone_story_candidate_provider import StaticStoryCandidateProvider
    from nullone_story_pipeline import make_fake_verifier

    def make_trigger(**overrides) -> dict[str, Any]:
        base = {
            "schema": "nullone.scheduler-invocation.v1",
            "contract_version": "1.0.0",
            "workflow_id": "story",
            "source": "openclaw",
            "external_occurrence_id": "openclaw-occ-story-0001",
            "scheduled_for": "2026-09-08T10:30:00Z",
            "triggered_at": "2026-09-08T10:30:02Z",
        }
        base.update(overrides)
        base["occurrence_id"] = compute_occurrence_id(
            base["workflow_id"], base["source"], base["external_occurrence_id"], base["scheduled_for"]
        )
        return base

    def make_candidate(**overrides) -> dict[str, Any]:
        base = {
            "candidate_id": "cand-1",
            "topic": "Self-test topic",
            "topic_cluster": "self-test",
            "content_type": "NEWS",
            "verification": "PASS",
            "evidence_refs": ["Self-test evidence, 10%."],
            "source_attribution": "Self-test source",
            "factual_inputs": {},
        }
        base.update(overrides)
        return base

    def writer(_context):
        return {
            "layout": "big-stat",
            "headline": "10% headline",
            "body": "Body.",
            "stat": "10%",
            "source_name": "Self-test",
            "use_source_image": False,
            "cta": "@nullone.az",
        }

    NOW = datetime(2026, 9, 8, 14, 30, 0, tzinfo=ZoneInfo("Asia/Baku"))
    # Isolates Story's own gap/pending logic from Main's independent gap
    # (main_target_min default 2 would otherwise always be gapped too,
    # since this self-test never records a main publication, and the
    # deterministic reason-selection order checks MAIN before STORY).
    STORY_ONLY_CONFIG = {"main_target_min": 0}

    with tempfile.TemporaryDirectory() as td:
        original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = Path(td)
        try:
            state_root = Path(td) / "social"

            class FakeDraftConnector:
                def create_review_draft(self, manifest_path: Path) -> None:
                    _, manifest = bridge_common.load_manifest(manifest_path)
                    manifest["review"]["create_attempts"] = 1
                    manifest["review"]["state"] = "DRAFT_CREATED"
                    manifest["review"]["zernio_draft_id"] = "self-test-review-1"
                    manifest["review"]["created_at"] = bridge_common.now_iso()
                    bridge_common.atomic_write_json(manifest_path, manifest)

            # 1. Invalid trigger -> TRIGGER_REJECTED, zero calls anywhere.
            class ExplodingProvider:
                def get_candidates(self):
                    raise AssertionError("candidate provider must not be called")

            result = run_story_workflow(
                {"workflow_id": "story"},
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
                candidate_provider=ExplodingProvider(),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=FakeReviewDelivery(status="SENT"),
            )
            assert result.outcome == "TRIGGER_REJECTED", result.outcome

            # 2. Wrong workflow_id -> TRIGGER_REJECTED.
            wrong_workflow_trigger = make_trigger(workflow_id="breaking")
            result = run_story_workflow(
                wrong_workflow_trigger,
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
                candidate_provider=ExplodingProvider(),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=FakeReviewDelivery(status="SENT"),
            )
            assert result.outcome == "TRIGGER_REJECTED", result.outcome

            # 3. Missing state root -> STATE_BLOCKED, zero draft/delivery.
            trigger = make_trigger()
            result = run_story_workflow(
                trigger,
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
                candidate_provider=ExplodingProvider(),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=FakeReviewDelivery(status="SENT"),
                cadence_config=STORY_ONLY_CONFIG,
            )
            assert result.outcome == "STATE_BLOCKED", result.outcome

            state_root.mkdir(parents=True, exist_ok=True)

            tools_dir = Path(td) / "social/tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            real_workspace_root = Path(__file__).resolve().parents[3]
            shutil.copy(
                real_workspace_root / "social/tools/render_story_v2.py",
                tools_dir / "render_story_v2.py",
            )

            # 4. Empty state, candidate_availability both False -> NO_ACTION
            #    (NO_QUALITY_CANDIDATE), zero candidate-provider calls.
            result = run_story_workflow(
                trigger,
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": False,
                },
                candidate_provider=ExplodingProvider(),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=FakeReviewDelivery(status="SENT"),
                cadence_config=STORY_ONLY_CONFIG,
            )
            assert result.outcome == "NO_ACTION", result.outcome
            assert result.cadence["reason_code"] == "NO_QUALITY_CANDIDATE", result.cadence

            # 5. main_quality_candidate_available True -> PREPARE_MAIN_CANDIDATE
            #    path -> NO_ACTION at Story workflow level, zero provider calls.
            result = run_story_workflow(
                trigger,
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": True,
                    "story_quality_candidate_available": True,
                },
                candidate_provider=ExplodingProvider(),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=FakeReviewDelivery(status="SENT"),
            )
            assert result.outcome == "NO_ACTION", result.outcome
            assert result.cadence["recommendation"] == "PREPARE_MAIN_CANDIDATE", result.cadence

            # 6. story_quality_candidate_available True, main False -> cadence
            #    recommends PREPARE_STORY; provider returns zero candidates ->
            #    truthful CANDIDATE_UNAVAILABLE (consistency check), not a
            #    fabricated draft.
            result = run_story_workflow(
                trigger,
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
                candidate_provider=StaticStoryCandidateProvider([]),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=FakeReviewDelivery(status="SENT"),
                cadence_config=STORY_ONLY_CONFIG,
            )
            assert result.outcome == "CANDIDATE_UNAVAILABLE", result.outcome
            assert result.cadence["recommendation"] == "PREPARE_STORY", result.cadence

            # 7. Multiple eligible candidates -> CANDIDATE_AMBIGUOUS. Run
            #    before any draft is created so cadence state is still empty
            #    (a pending draft would otherwise suppress PREPARE_STORY
            #    itself before candidate selection is ever reached).
            result = run_story_workflow(
                trigger,
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
                candidate_provider=StaticStoryCandidateProvider(
                    [make_candidate(candidate_id="x"), make_candidate(candidate_id="y")]
                ),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=FakeReviewDelivery(status="SENT"),
                cadence_config=STORY_ONLY_CONFIG,
            )
            assert result.outcome == "CANDIDATE_AMBIGUOUS", result.outcome
            assert result.cadence["recommendation"] == "PREPARE_STORY", result.cadence

            # 8. Candidate provider raising -> CANDIDATE_PROVIDER_FAILED. Same
            #    still-empty state as steps 6/7.
            class FailingProvider:
                def get_candidates(self):
                    raise RuntimeError("upstream read failed")

            result = run_story_workflow(
                trigger,
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
                candidate_provider=FailingProvider(),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=FakeReviewDelivery(status="SENT"),
                cadence_config=STORY_ONLY_CONFIG,
            )
            assert result.outcome == "CANDIDATE_PROVIDER_FAILED", result.outcome

            # 9. Exactly one eligible candidate -> DRAFT_CREATED, real #33 core.
            sender = FakeReviewDelivery(status="SENT")
            result = run_story_workflow(
                trigger,
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
                candidate_provider=StaticStoryCandidateProvider([make_candidate()]),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=sender,
                cadence_config=STORY_ONLY_CONFIG,
            )
            assert result.outcome == "DRAFT_CREATED", result.outcome
            assert len(sender.sent) == 1

            # 10. Replay of the exact same trigger/candidate against the now-
            #     updated real state: the pending Story draft just created is
            #     counted by the #32 state adapter, so cadence itself now
            #     suppresses a repeat PREPARE_STORY (PENDING_STORY_EXISTS) --
            #     the candidate provider/writer/verifier/draft/delivery are
            #     never reached a second time, at a stronger layer than #33's
            #     own per-request lock.
            second = run_story_workflow(
                trigger,
                state_root=state_root,
                now=NOW,
                candidate_availability={
                    "main_quality_candidate_available": False,
                    "story_quality_candidate_available": True,
                },
                candidate_provider=ExplodingProvider(),
                writer=writer,
                verifier=make_fake_verifier("PASS"),
                draft_connector=FakeDraftConnector(),
                review_delivery=sender,
                cadence_config=STORY_ONLY_CONFIG,
            )
            assert second.outcome == "NO_ACTION", second.outcome
            assert second.cadence["reason_code"] == "PENDING_STORY_EXISTS", second.cadence
            assert len(sender.sent) == 1
        finally:
            bridge_common.WORKSPACE = original_workspace

    print("STORY_WORKFLOW_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_PUBLISH_CAPABILITY=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne StoryWorkflow application orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
