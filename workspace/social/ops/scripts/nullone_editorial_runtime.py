#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_editorial_candidate_handoff import (
    EditorialHandoffError,
    board_relative_path,
    handoff_relative_path,
    load_handoff_snapshot,
)
from nullone_run_outcome import (
    assess_run,
    emit_result_once,
    make_run_id,
    result_path,
)
from nullone_schedule_registry import get_schedule, get_schedules

WORKFLOW_ID = "morning-editorial"

RUN_OUTCOME_ROOT = WORKSPACE / "social/ops/run-outcomes/morning-editorial"

MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (60,)

# Per-attempt wall-clock ceiling enforced by the provider invocation
# (see nullone_claude_editorial_provider.py's subprocess timeout). Kept
# here, next to the retry/backoff policy it must be sized against,
# rather than duplicated as a second unrelated constant in the runner
# script.
#
# Proven live evidence (2026-09-11 natural occurrence, run
# run_28849dc4436e74d25ae99dbf): the prior 210s value -- sized only
# against a single historical ~118s run -- killed BOTH real attempts
# while they were still doing genuine agent/tool work (WebSearch/
# WebFetch/Bash calls actively succeeding; no artifact write ever
# reached). The 210s ceiling wraps the ENTIRE Claude agent process
# (research, verification/scoring, board construction, structured
# handoff creation, state I/O), not merely a network connect/read --
# it was never a realistic budget for that workload. 600s is the
# reviewed replacement, sized for the current heavier production
# contract rather than the old single-sample baseline.
PROVIDER_CALL_TIMEOUT_SECONDS = 600


def _seconds_since_midnight(value: object) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def morning_to_first_story_gap_seconds() -> int:
    """Seconds between Morning's own slot and Story's earliest daily slot.

    Reads both slots from `nullone_schedule_registry` -- the single
    reviewed source of NullOne-owned schedule truth -- instead of
    duplicating either cron string here. Both schedules currently share
    one IANA timezone with no DST, so a same-day time-of-day
    subtraction is exact; a future schedule change that put them in
    different timezones must fail closed rather than silently produce
    a wrong gap.
    """

    morning = get_schedule("morning-editorial")
    first_story = get_schedules("story")[0]  # ascending by local time

    if morning.timezone_name != first_story.timezone_name:
        raise UnsafeRetryPolicyError(
            "morning-editorial and story schedules must share one "
            "timezone to compute a safe occurrence failure budget; "
            f"got {morning.timezone_name!r} vs {first_story.timezone_name!r}"
        )

    morning_seconds = _seconds_since_midnight(morning.local_time_of_day())
    story_seconds = _seconds_since_midnight(first_story.local_time_of_day())
    gap = story_seconds - morning_seconds

    if gap <= 0:
        raise UnsafeRetryPolicyError(
            "story's earliest daily slot must be strictly after "
            f"morning-editorial's slot; computed non-positive gap {gap}s"
        )

    return gap


# Reviewed replacement for the retired historical
# `_MIN_OBSERVED_OCCURRENCE_SPACING_SECONDS = 600` occurrence-spacing
# assumption (that value modeled one 2026-09-05 observation, not the
# actual reviewed production schedule, and never accounted for the fact
# Morning has exactly one daily slot -- there is no "next Morning
# occurrence" to collide with; the real downstream consumer is Story's
# earliest daily check). The failure budget below is instead bounded
# against the live schedule-registry-derived Morning-to-first-Story gap
# (`morning_to_first_story_gap_seconds()`, currently 7200s for
# 08:30 -> 10:30 Asia/Baku), enforced by `validate_occurrence_policy`.
#
# Worst-case retry cost with MAX_ATTEMPTS=2,
# PROVIDER_CALL_TIMEOUT_SECONDS=600, RETRY_BACKOFF_SECONDS=(60,) is
# 1260s. 1800s (30 minutes) is chosen with comfortable headroom above
# that worst case while remaining well below the 7200s Morning-to-
# first-Story gap.
OCCURRENCE_FAILURE_BUDGET_SECONDS = 1800


def worst_case_occurrence_seconds(
    *,
    max_attempts: int = MAX_ATTEMPTS,
    provider_call_timeout_seconds: int = PROVIDER_CALL_TIMEOUT_SECONDS,
    backoff_seconds: tuple[int, ...] = RETRY_BACKOFF_SECONDS,
) -> int:
    """Deterministic worst-case wall-clock cost of the bounded retry path.

    Pure function: every attempt can cost up to
    `provider_call_timeout_seconds`, and a backoff sleep is inserted
    before each retry (max_attempts - 1 of them). This does not model
    successful/short calls; it models the ceiling that must fit inside
    OCCURRENCE_FAILURE_BUDGET_SECONDS.
    """

    backoffs_applied = backoff_seconds[: max(max_attempts - 1, 0)]
    return max_attempts * provider_call_timeout_seconds + sum(backoffs_applied)


class UnsafeRetryPolicyError(RuntimeError):
    """Raised when a retry/timeout/backoff policy violates the occurrence
    failure budget invariant (issue #28): the worst-case retry duration
    must fit inside a declared budget, and that budget must itself stay
    strictly below the schedule-registry-derived Morning-to-first-Story
    gap (`morning_to_first_story_gap_seconds()`).
    """


def validate_occurrence_policy(
    *,
    max_attempts: int,
    provider_call_timeout_seconds: int,
    backoff_seconds: tuple[int, ...],
    budget_seconds: int,
    gap_seconds: int | None = None,
) -> None:
    """Raise UnsafeRetryPolicyError if this policy is not safe to run.

    This is an operational safety invariant, so it is enforced with an
    explicit, catchable exception rather than a bare `assert` (asserts
    can be stripped with `python -O`). `gap_seconds` defaults to the
    live `morning_to_first_story_gap_seconds()` reading; tests may pass
    an explicit value to exercise the boundary without depending on the
    schedule registry's current numbers.
    """

    if gap_seconds is None:
        gap_seconds = morning_to_first_story_gap_seconds()

    if budget_seconds >= gap_seconds:
        raise UnsafeRetryPolicyError(
            f"Occurrence failure budget ({budget_seconds}s) must stay "
            f"strictly under the {gap_seconds}s Morning-to-first-Story "
            "schedule gap"
        )

    worst_case = worst_case_occurrence_seconds(
        max_attempts=max_attempts,
        provider_call_timeout_seconds=provider_call_timeout_seconds,
        backoff_seconds=backoff_seconds,
    )

    if worst_case > budget_seconds:
        raise UnsafeRetryPolicyError(
            f"Worst-case retry duration ({worst_case}s) exceeds the "
            f"declared occurrence failure budget ({budget_seconds}s)"
        )


# Fail closed at import time if the default/production policy constants
# above are ever edited out of sync with the occurrence budget invariant.
validate_occurrence_policy(
    max_attempts=MAX_ATTEMPTS,
    provider_call_timeout_seconds=PROVIDER_CALL_TIMEOUT_SECONDS,
    backoff_seconds=RETRY_BACKOFF_SECONDS,
    budget_seconds=OCCURRENCE_FAILURE_BUDGET_SECONDS,
)

# Confirmed 2026-09-05 pattern: transient provider/runtime reachability
# failure (DNS/socket unreachable), not a proven permanent fault.
REACHABILITY_PATTERN = re.compile(
    r"ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ECONNREFUSED|"
    r"can.t reach the api server",
    re.IGNORECASE,
)


class ProviderUnreachableError(BridgeError):
    """Raised when the model provider/runtime could not be reached."""


class ProviderExecutionTimeoutError(BridgeError):
    """Raised when the whole editorial provider process exceeded its
    outer wall-clock deadline (`PROVIDER_CALL_TIMEOUT_SECONDS`).

    Deliberately NOT a subclass of `ProviderUnreachableError`. Proven
    live evidence (2026-09-11) shows a whole-process timeout can fire
    while the child is still doing genuine, successful agent/tool work
    -- it is not proof the provider was unreachable, and conflating the
    two previously caused an automatic, expensive second attempt on
    every such timeout. This outcome is non-retryable: see
    `classify_provider_failure`.
    """


def classify_provider_failure(exc: BaseException) -> tuple[str, str]:
    """Classify a provider failure into a stable reason_code/reason_text.

    `ProviderExecutionTimeoutError` is checked first and is always
    non-retryable (`PROVIDER_EXECUTION_TIMEOUT`): an outer wall-clock
    timeout on the whole agent process is ambiguous about *why* it
    fired (proven live evidence shows the child can still be actively
    making progress), so automatically restarting the whole agent could
    duplicate expensive research or repeat side effects.

    Only PROVIDER_UNREACHABLE is treated as safe to retry: it matches
    the confirmed transient DNS/API reachability pattern. Any other
    failure is reported as a distinct, non-retried domain failure.
    """

    if isinstance(exc, ProviderExecutionTimeoutError):
        return (
            "PROVIDER_EXECUTION_TIMEOUT",
            "Editorial provider exceeded its execution deadline.",
        )

    if isinstance(exc, ProviderUnreachableError) or REACHABILITY_PATTERN.search(
        str(exc)
    ):
        return (
            "PROVIDER_UNREACHABLE",
            "Provider/runtime API was not reachable.",
        )

    return (
        "EDITORIAL_PROVIDER_ERROR",
        "Editorial provider call failed.",
    )


def _artifact_ready(artifact_root: Path, relative: str) -> bool:
    path = (artifact_root / relative).resolve()
    return path.is_file() and path.stat().st_size > 0


def _all_artifacts_ready(artifact_root: Path, required: tuple[str, ...]) -> bool:
    return all(_artifact_ready(artifact_root, rel) for rel in required)


def _handoff_valid_for_date(artifact_root: Path, board_date: str) -> bool:
    """The date's handoff artifact parses, validates, and binds exactly.

    Uses the same canonical loader Story reads (`load_handoff_snapshot`
    with the expected board_date), so Morning success proves the exact
    date/board binding -- not merely generic handoff shape. Presence
    alone never counts as a healthy machine-readable cycle: a missing,
    stale, misbound, or malformed handoff fails closed here
    (non-retryable) instead of masquerading as success. Never infers
    from Markdown.
    """

    try:
        load_handoff_snapshot(workspace_root=artifact_root, editorial_date=board_date)
    except (OSError, json.JSONDecodeError, EditorialHandoffError):
        return False
    return True


def _occurrence_lock_path(output_root: Path, run_id: str) -> Path:
    return output_root.resolve() / f"{run_id}.lock"


def run_morning_editorial(
    *,
    occurrence_id: str,
    board_date: str,
    invoke_provider: Callable[[], None],
    max_attempts: int = MAX_ATTEMPTS,
    backoff_seconds: tuple[int, ...] = RETRY_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    artifact_root: Path = WORKSPACE,
    output_root: Path = RUN_OUTCOME_ROOT,
) -> dict[str, Any]:
    """Run one Morning Editorial scheduled occurrence with bounded retry.

    Every attempt made for one `occurrence_id` shares the same run_id
    (see nullone_run_outcome.make_run_id). An exclusive `fcntl.flock` on
    a lock file named after that run_id, under `output_root`, is held
    for the full duration of this call — acquired before the persisted
    result / artifact are even checked, and released only when this
    call returns (naturally released by the OS if the process exits
    instead). A second concurrent call for the same occurrence_id
    therefore blocks until the first is completely done, then re-checks
    the persisted result and returns it directly. This, combined with
    the persisted-result and artifact checks below, is what prevents a
    retry, a re-entry, or a genuinely concurrent second invocation from
    producing a second editorial board or a second queue/state mutation
    for the same occurrence.
    """

    required_artifacts = (board_relative_path(board_date), handoff_relative_path(board_date))
    run_id = make_run_id(
        workflow_id=WORKFLOW_ID,
        occurrence_id=occurrence_id,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(
        str(_occurrence_lock_path(output_root, run_id)),
        os.O_CREAT | os.O_RDWR,
        0o600,
    )

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        existing = result_path(output_root, run_id)

        if existing.is_file():
            return json.loads(existing.read_text(encoding="utf-8"))

        reason_code = "EDITORIAL_PROVIDER_ERROR"
        reason_text = "Editorial provider call failed."
        attempts_made = 0

        for attempt in range(1, max_attempts + 1):
            attempts_made = attempt

            # A previous attempt for this same occurrence may already
            # have produced the artifacts even though its call was later
            # reported as failed (e.g. a stalled/late response). Never
            # call the provider again once ALL required artifacts exist --
            # this is what prevents a retry, a re-entry, or a genuinely
            # concurrent second invocation from producing a second
            # editorial board/handoff or a second queue/state mutation
            # for the same occurrence.
            if _all_artifacts_ready(artifact_root, required_artifacts):
                break

            try:
                invoke_provider()
                break
            except Exception as exc:
                reason_code, reason_text = classify_provider_failure(exc)
                retryable = reason_code == "PROVIDER_UNREACHABLE"

                # #28 material-progress guard: once ANY provider-owned
                # artifact for this occurrence exists, the attempt made
                # material progress (board write and queue/ledger mutation
                # may already have happened) -- never invoke the editorial
                # provider again for this occurrence, even when the
                # failure itself looks retryable. Bounded retry survives
                # only for a genuinely empty first attempt.
                if _artifact_ready(
                    artifact_root, required_artifacts[0]
                ) or _artifact_ready(artifact_root, required_artifacts[1]):
                    break

                if not retryable or attempt == max_attempts:
                    plural = "s" if attempts_made != 1 else ""
                    final_text = (
                        f"{reason_text} ({attempts_made} attempt{plural})"
                    )

                    result = assess_run(
                        workflow_id=WORKFLOW_ID,
                        occurrence_id=occurrence_id,
                        scheduler_status="error",
                        domain_outcome="FAILED",
                        reason_code=reason_code,
                        reason_text=final_text,
                    )
                    emit_result_once(
                        output_root,
                        result,
                        artifact_root=artifact_root,
                    )
                    return result

                sleep(
                    backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                )
                continue

        # A successful Morning occurrence always produces a complete,
        # healthy machine-readable cycle. Partial artifact sets fail
        # closed with stable codes (non-retryable -- partial or malformed
        # provider output never earns another attempt):
        # - board present, handoff missing -> HANDOFF_INCOMPLETE;
        # - handoff present, board missing -> PARTIAL_EDITORIAL_ARTIFACT_SET;
        # - handoff present but malformed -> HANDOFF_INVALID.
        board_ready = _artifact_ready(artifact_root, required_artifacts[0])
        handoff_ready = _artifact_ready(artifact_root, required_artifacts[1])
        partial_code: str | None = None
        if board_ready and not handoff_ready:
            partial_code = "HANDOFF_INCOMPLETE"
        elif handoff_ready and not board_ready:
            partial_code = "PARTIAL_EDITORIAL_ARTIFACT_SET"
        elif handoff_ready and not _handoff_valid_for_date(artifact_root, board_date):
            partial_code = "HANDOFF_INVALID"
        if partial_code is not None:
            result = assess_run(
                workflow_id=WORKFLOW_ID,
                occurrence_id=occurrence_id,
                scheduler_status="succeeded",
                domain_outcome="FAILED",
                reason_code=partial_code,
                reason_text="Structured Morning artifact set incomplete or invalid.",
            )
            emit_result_once(
                output_root,
                result,
                artifact_root=artifact_root,
            )
            return result

        result = assess_run(
            workflow_id=WORKFLOW_ID,
            occurrence_id=occurrence_id,
            scheduler_status="succeeded",
            domain_outcome="SUCCEEDED",
            artifact_root=artifact_root,
            required_artifacts=required_artifacts,
        )
        emit_result_once(
            output_root,
            result,
            artifact_root=artifact_root,
        )
        return result
    finally:
        os.close(lock_fd)
