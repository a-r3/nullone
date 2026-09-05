#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from nullone_bridge_common import BridgeError, WORKSPACE
from nullone_run_outcome import (
    assess_run,
    emit_result_once,
    make_run_id,
    result_path,
)

WORKFLOW_ID = "morning-editorial"

RUN_OUTCOME_ROOT = WORKSPACE / "social/ops/run-outcomes/morning-editorial"

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (30, 90)

# Per-attempt wall-clock ceiling enforced by the provider invocation
# (see nullone-morning-editorial-run.py's subprocess timeout). Kept here,
# next to the retry/backoff policy it must be sized against, rather than
# duplicated as a second unrelated constant in the runner script.
PROVIDER_CALL_TIMEOUT_SECONDS = 120

# Confirmed 2026-09-05 evidence (issue #28) showed failed Morning
# Editorial occurrences spaced as little as ~10 minutes (600s) apart.
# The bounded retry policy's worst-case wall-clock cost for one
# occurrence must stay safely under that window, or a persistent
# reachability failure could still be running when the next scheduled
# occurrence starts. 480s (8 minutes) is chosen with a 120s margin
# below the observed ~10-minute spacing.
OCCURRENCE_FAILURE_BUDGET_SECONDS = 480


def worst_case_occurrence_seconds(
    *,
    max_attempts: int = MAX_ATTEMPTS,
    provider_call_timeout_seconds: int = PROVIDER_CALL_TIMEOUT_SECONDS,
    backoff_seconds: tuple[int, ...] = RETRY_BACKOFF_SECONDS,
) -> int:
    """Deterministic worst-case wall-clock cost of the bounded retry path.

    Every attempt can cost up to `provider_call_timeout_seconds`, and a
    backoff sleep is inserted before each retry (max_attempts - 1 of
    them). This does not model successful/short calls; it models the
    ceiling that must fit inside OCCURRENCE_FAILURE_BUDGET_SECONDS.
    """

    backoffs_applied = backoff_seconds[: max(max_attempts - 1, 0)]
    return max_attempts * provider_call_timeout_seconds + sum(backoffs_applied)


# Fail closed at import time if the constants above are ever edited
# out of sync: the worst case must always fit the declared budget, and
# the budget itself must always stay under the confirmed ~10-minute
# occurrence spacing.
assert worst_case_occurrence_seconds() <= OCCURRENCE_FAILURE_BUDGET_SECONDS, (
    "Morning Editorial worst-case retry duration exceeds its declared "
    "occurrence failure budget"
)
assert OCCURRENCE_FAILURE_BUDGET_SECONDS < 600, (
    "Occurrence failure budget must stay under the confirmed ~10-minute "
    "occurrence spacing"
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


def classify_provider_failure(exc: BaseException) -> tuple[str, str]:
    """Classify a provider failure into a stable reason_code/reason_text.

    Only PROVIDER_UNREACHABLE is treated as safe to retry: it matches the
    confirmed transient DNS/API reachability pattern. Any other failure
    is reported as a distinct, non-retried domain failure.
    """

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


def board_relative_path(board_date: str) -> str:
    return f"social/research/daily/{board_date}-editorial-board.md"


def _artifact_ready(artifact_root: Path, relative: str) -> bool:
    path = (artifact_root / relative).resolve()
    return path.is_file() and path.stat().st_size > 0


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
    (see nullone_run_outcome.make_run_id). Once that run_id has a
    persisted terminal result, later invocations return it unchanged
    and never call `invoke_provider` again — this is what prevents a
    retry or an accidental re-entry from producing a second editorial
    board or a second queue/state mutation for the same occurrence.
    """

    required_artifacts = (board_relative_path(board_date),)
    run_id = make_run_id(
        workflow_id=WORKFLOW_ID,
        occurrence_id=occurrence_id,
    )
    existing = result_path(output_root, run_id)

    if existing.is_file():
        return json.loads(existing.read_text(encoding="utf-8"))

    reason_code = "EDITORIAL_PROVIDER_ERROR"
    reason_text = "Editorial provider call failed."
    attempts_made = 0

    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt

        # A previous attempt for this same occurrence may already have
        # produced the board even though its call was later reported as
        # failed (e.g. a stalled/late response). Never call the provider
        # again once the required artifact exists.
        if _artifact_ready(artifact_root, required_artifacts[0]):
            break

        try:
            invoke_provider()
            break
        except Exception as exc:
            reason_code, reason_text = classify_provider_failure(exc)
            retryable = reason_code == "PROVIDER_UNREACHABLE"

            if not retryable or attempt == max_attempts:
                plural = "s" if attempts_made != 1 else ""
                final_text = f"{reason_text} ({attempts_made} attempt{plural})"

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

            sleep(backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)])
            continue

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
