#!/usr/bin/env python3
"""Daily Analytics domain runtime (issue #29).

Connector-agnostic: this module knows nothing about Zernio HTTPS paths
or credentials, only the small read-only connector surface defined in
`nullone_zernio_analytics_adapter`. That keeps the connector
replaceable (issue #13) without changing domain outcome semantics.

Reuses the #27 workflow/domain contract (`nullone_run_outcome`):
- workflow_id is always "daily-analytics";
- run_id is occurrence-scoped and stable across retries/re-entry;
- bootstrap/dependency/auth unavailability becomes BLOCKED, never
  SUCCEEDED, even though the scheduler-level status may itself read as
  "succeeded" (this is the exact Sep 4-5 production symptom this issue
  fixes: `ok/succeeded` scheduler receipt with a blocked business
  result);
- a valid no-data response is explicit `SUCCEEDED` with
  `empty_success="NO_DATA"`, never a silently fabricated success.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from nullone_bridge_common import WORKSPACE
from nullone_run_outcome import assess_run, emit_result_once, make_run_id, result_path
from nullone_zernio_analytics_adapter import (
    AnalyticsResponseError,
    ConnectorUnauthorizedError,
    ConnectorUnavailableError,
    ZernioReadOnlyAnalyticsConnector,
)

WORKFLOW_ID = "daily-analytics"

RUN_OUTCOME_ROOT = WORKSPACE / "social/ops/run-outcomes/daily-analytics"

# Documented no-data signal (workspace/social/ops/prompts/daily-analytics.md):
# "total_interactions=-1 may mean no data".
NO_DATA_SENTINEL = -1


def raw_relative_path(analytics_date: str) -> str:
    return f"social/analytics/raw/{analytics_date}.md"


def report_relative_path(analytics_date: str) -> str:
    return f"social/analytics/reports/{analytics_date}.md"


def _occurrence_lock_path(output_root: Path, run_id: str) -> Path:
    return output_root.resolve() / f"{run_id}.lock"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _has_reportable_data(
    insights: dict[str, Any],
    post_analytics: dict[str, Any],
) -> bool:
    total_interactions = insights.get("total_interactions")

    has_insight_data = (
        isinstance(total_interactions, (int, float))
        and total_interactions != NO_DATA_SENTINEL
    )
    has_post_data = bool(post_analytics.get("posts"))

    return has_insight_data or has_post_data


def _render_raw_markdown(
    *,
    analytics_date: str,
    account: dict[str, Any],
    follower_history: dict[str, Any],
    insights: dict[str, Any],
    post_analytics: dict[str, Any],
) -> str:
    def _block(title: str, payload: dict[str, Any]) -> list[str]:
        return [
            f"## {title}",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]

    lines = [f"# Daily Analytics raw snapshot — {analytics_date}", ""]
    lines += _block("Account", account)
    lines += _block("Follower history", follower_history)
    lines += _block("Account insights", insights)
    lines += _block("Post analytics", post_analytics)
    lines.append(
        "Account insights may lag up to 48h; follower history is a "
        "separate daily series and may lag up to 24h. Same-day posts "
        "are not judged prematurely."
    )
    lines.append("")

    return "\n".join(lines)


def _render_report_markdown(
    *,
    analytics_date: str,
    insights: dict[str, Any],
    post_analytics: dict[str, Any],
) -> str:
    lines = [
        f"# Daily Analytics report — {analytics_date}",
        "",
        f"- reach: {insights.get('reach')}",
        f"- views: {insights.get('views')}",
        f"- accounts_engaged: {insights.get('accounts_engaged')}",
        f"- total_interactions: {insights.get('total_interactions')}",
        "",
        "## Post-level ratios (mature posts only, when reach is available)",
        "",
    ]

    posts = post_analytics.get("posts") or []

    if not posts:
        lines.append("No mature published-post analytics available yet.")
    else:
        for post in posts:
            post_reach = post.get("reach")
            row = f"- post {post.get('post_id')}: reach={post_reach}"

            if isinstance(post_reach, (int, float)) and post_reach > 0:
                for metric in ("likes", "comments", "saves", "shares"):
                    value = post.get(metric)
                    if isinstance(value, (int, float)):
                        row += f", {metric}/reach={value / post_reach:.4f}"

            lines.append(row)

    lines.append("")
    lines.append(
        "No causation is claimed here; no strategy change is made from "
        "a single post."
    )
    lines.append("")

    return "\n".join(lines)


def run_daily_analytics(
    *,
    occurrence_id: str,
    analytics_date: str,
    build_connector: Callable[[], ZernioReadOnlyAnalyticsConnector],
    artifact_root: Path = WORKSPACE,
    output_root: Path = RUN_OUTCOME_ROOT,
) -> dict[str, Any]:
    """Run one Daily Analytics scheduled occurrence.

    `build_connector` must return a connector exposing only the narrow
    read-only surface from `nullone_zernio_analytics_adapter` — it has
    no method capable of publishing, drafting, scheduling, or
    otherwise mutating any social state. A connector bootstrap/auth
    failure (including a `bundle-mcp`-style startup failure) always
    becomes domain BLOCKED here, never SUCCEEDED, even though this
    function still reports `scheduler_status="succeeded"` (this
    process itself completed without crashing) — reproducing, and
    fixing the business-outcome side of, the exact Sep 4-5 symptom:
    scheduler `ok/succeeded` cannot mask a blocked domain outcome.

    Required raw/report artifacts are written atomically and only
    after every fetched response has been validated; a malformed or
    partial response never leaves partial files behind.
    """

    run_id = make_run_id(workflow_id=WORKFLOW_ID, occurrence_id=occurrence_id)

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

        def _blocked(reason_code: str, reason_text: str) -> dict[str, Any]:
            result = assess_run(
                workflow_id=WORKFLOW_ID,
                occurrence_id=occurrence_id,
                scheduler_status="succeeded",
                domain_outcome="BLOCKED",
                reason_code=reason_code,
                reason_text=reason_text,
            )
            emit_result_once(output_root, result, artifact_root=artifact_root)
            return result

        def _failed(reason_code: str, reason_text: str) -> dict[str, Any]:
            result = assess_run(
                workflow_id=WORKFLOW_ID,
                occurrence_id=occurrence_id,
                scheduler_status="succeeded",
                domain_outcome="FAILED",
                reason_code=reason_code,
                reason_text=reason_text,
            )
            emit_result_once(output_root, result, artifact_root=artifact_root)
            return result

        try:
            connector = build_connector()
        except ConnectorUnauthorizedError:
            return _blocked(
                "ZERNIO_ANALYTICS_UNAUTHORIZED",
                "Zernio analytics credential is missing or was rejected.",
            )
        except ConnectorUnavailableError:
            return _blocked(
                "ZERNIO_ANALYTICS_UNAVAILABLE",
                "Zernio analytics connector could not be started.",
            )

        try:
            account = connector.get_account()
            follower_history = connector.get_follower_history()
            insights = connector.get_account_insights()
            post_analytics = connector.get_post_analytics()
        except ConnectorUnauthorizedError:
            return _blocked(
                "ZERNIO_ANALYTICS_UNAUTHORIZED",
                "Zernio analytics credential is missing or was rejected.",
            )
        except ConnectorUnavailableError:
            return _blocked(
                "ZERNIO_ANALYTICS_UNAVAILABLE",
                "Zernio analytics connector could not be started.",
            )
        except AnalyticsResponseError:
            return _failed(
                "ANALYTICS_RESPONSE_INVALID",
                "Zernio analytics response was malformed or incomplete.",
            )

        if not _has_reportable_data(insights, post_analytics):
            result = assess_run(
                workflow_id=WORKFLOW_ID,
                occurrence_id=occurrence_id,
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                empty_success="NO_DATA",
            )
            emit_result_once(output_root, result, artifact_root=artifact_root)
            return result

        raw_relative = raw_relative_path(analytics_date)
        report_relative = report_relative_path(analytics_date)

        _atomic_write_text(
            artifact_root / raw_relative,
            _render_raw_markdown(
                analytics_date=analytics_date,
                account=account,
                follower_history=follower_history,
                insights=insights,
                post_analytics=post_analytics,
            ),
        )
        _atomic_write_text(
            artifact_root / report_relative,
            _render_report_markdown(
                analytics_date=analytics_date,
                insights=insights,
                post_analytics=post_analytics,
            ),
        )

        result = assess_run(
            workflow_id=WORKFLOW_ID,
            occurrence_id=occurrence_id,
            scheduler_status="succeeded",
            domain_outcome="SUCCEEDED",
            artifact_root=artifact_root,
            required_artifacts=(raw_relative, report_relative),
        )
        emit_result_once(output_root, result, artifact_root=artifact_root)
        return result
    finally:
        os.close(lock_fd)
