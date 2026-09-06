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
  `empty_success="NO_DATA"`, never a silently fabricated success;
- the raw+report artifact pair is committed as one failure-safe unit
  (see `_commit_artifact_pair`) so a filesystem failure between the
  two writes can never leave a newly partial pair on disk, and never
  destroys a valid pre-existing artifact.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from nullone_bridge_common import WORKSPACE
from nullone_run_outcome import assess_run, emit_result_once, make_run_id, result_path
from nullone_zernio_analytics_adapter import (
    AnalyticsCapabilityUnavailableError,
    AnalyticsResponseError,
    ConnectorUnauthorizedError,
    ConnectorUnavailableError,
    ZernioReadOnlyAnalyticsConnector,
    metric_total,
)

WORKFLOW_ID = "daily-analytics"

RUN_OUTCOME_ROOT = WORKSPACE / "social/ops/run-outcomes/daily-analytics"

# Legacy no-data sentinel documented in
# workspace/social/ops/prompts/daily-analytics.md ("total_interactions=-1
# may mean no data"), predating the confirmed Zernio HTTPS contract
# (issue #29 blocker fix), which instead OMITS an unavailable metric
# entirely. Both are treated as "no data" here for backward safety.
NO_DATA_SENTINEL = -1


class ArtifactCommitError(RuntimeError):
    """Raised when the two-artifact commit could not complete safely."""


def raw_relative_path(analytics_date: str) -> str:
    return f"social/analytics/raw/{analytics_date}.md"


def report_relative_path(analytics_date: str) -> str:
    return f"social/analytics/reports/{analytics_date}.md"


def _occurrence_lock_path(output_root: Path, run_id: str) -> Path:
    return output_root.resolve() / f"{run_id}.lock"


@dataclass
class _StagedArtifact:
    path: Path
    tmp_path: Path
    existed_before: bool
    backup_bytes: bytes | None


def _stage_artifact(path: Path, content: str) -> _StagedArtifact:
    path.parent.mkdir(parents=True, exist_ok=True)

    existed_before = path.is_file()
    backup_bytes = path.read_bytes() if existed_before else None

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(tmp_path, 0o600)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise

    return _StagedArtifact(
        path=path,
        tmp_path=tmp_path,
        existed_before=existed_before,
        backup_bytes=backup_bytes,
    )


def _restore_staged(staged: _StagedArtifact) -> None:
    """Best-effort restore of one target path to its pre-commit state."""

    if not staged.existed_before:
        staged.path.unlink(missing_ok=True)
        return

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{staged.path.name}.restore.", dir=str(staged.path.parent)
    )
    restore_tmp = Path(tmp_name)

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(staged.backup_bytes or b"")
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(restore_tmp, 0o600)
        os.replace(restore_tmp, staged.path)
    finally:
        restore_tmp.unlink(missing_ok=True)


def _commit_artifact_pair(items: list[tuple[Path, str]]) -> None:
    """Commit the raw+report artifacts as one failure-safe unit.

    Both contents must already be fully rendered (pure string
    construction, no I/O) before this is called. Each is first staged
    as a temp file next to its final path; only once every temp file is
    written and fsynced does this function start swapping targets into
    place with `os.replace` (atomic per file). If a later swap fails,
    every target already committed in this call is rolled back to its
    exact pre-call state — restored from an in-memory backup if it
    existed before, or removed if it did not — so this occurrence can
    never leave a newly partial raw/report pair, and a valid
    pre-existing artifact is never destroyed.
    """

    staged: list[_StagedArtifact] = []

    try:
        for path, content in items:
            staged.append(_stage_artifact(path, content))
    except OSError as exc:
        for item in staged:
            item.tmp_path.unlink(missing_ok=True)
        raise ArtifactCommitError(
            "Failed to stage Daily Analytics artifacts for commit"
        ) from exc

    committed: list[_StagedArtifact] = []

    try:
        for item in staged:
            os.replace(item.tmp_path, item.path)
            committed.append(item)
    except OSError as exc:
        for item in reversed(committed):
            _restore_staged(item)

        for item in staged:
            item.tmp_path.unlink(missing_ok=True)

        raise ArtifactCommitError(
            "Failed to commit Daily Analytics artifacts; prior state restored"
        ) from exc

    for item in staged:
        if not item.path.is_file():
            raise ArtifactCommitError(
                f"Daily Analytics artifact missing immediately after commit: "
                f"{item.path}"
            )


def _has_reportable_data(
    insights: dict[str, Any],
    post_analytics: dict[str, Any],
) -> bool:
    total_interactions = metric_total(insights, "total_interactions")

    has_insight_data = (
        total_interactions is not None
        and total_interactions != NO_DATA_SENTINEL
    )
    has_post_data = bool(post_analytics.get("posts"))

    return has_insight_data or has_post_data


def _fmt_metric(value: int | float | None) -> str:
    return "unavailable" if value is None else str(value)


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
        "are not judged prematurely. A metric absent from its envelope "
        "means Zernio reported it unavailable, not zero."
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
        f"- reach: {_fmt_metric(metric_total(insights, 'reach'))}",
        f"- views: {_fmt_metric(metric_total(insights, 'views'))}",
        (
            "- accounts_engaged: "
            f"{_fmt_metric(metric_total(insights, 'accounts_engaged'))}"
        ),
        (
            "- total_interactions: "
            f"{_fmt_metric(metric_total(insights, 'total_interactions'))}"
        ),
        "",
        "## Post-level ratios (mature posts only, when reach is available)",
        "",
    ]

    posts = post_analytics.get("posts") or []

    if not posts:
        lines.append("No mature published-post analytics available yet.")
    else:
        for post in posts:
            post_metrics = post.get("analytics") or {}
            post_reach = post_metrics.get("reach")
            row = f"- post {post.get('_id')}: reach={_fmt_metric(post_reach)}"

            if isinstance(post_reach, (int, float)) and post_reach > 0:
                for metric in ("likes", "comments", "saves", "shares"):
                    value = post_metrics.get(metric)
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

    Required raw/report artifacts are rendered and validated in full
    before either is committed, and committed as one failure-safe unit
    (`_commit_artifact_pair`): a filesystem failure partway through
    never leaves a newly partial pair and never destroys a valid
    pre-existing artifact, and becomes domain FAILED rather than an
    uncaught or partial success.
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
        except AnalyticsCapabilityUnavailableError:
            # Must be caught before the parent ConnectorUnavailableError
            # clause below: documented `hasAnalyticsAccess: false` or
            # HTTP 402 (analytics_addon_required) is a capability
            # unavailability, not a bootstrap failure or a malformed
            # request — still BLOCKED, with a more specific reason.
            return _blocked(
                "ZERNIO_ANALYTICS_ADDON_REQUIRED",
                "Zernio analytics add-on is required but not enabled "
                "for this account.",
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

        raw_content = _render_raw_markdown(
            analytics_date=analytics_date,
            account=account,
            follower_history=follower_history,
            insights=insights,
            post_analytics=post_analytics,
        )
        report_content = _render_report_markdown(
            analytics_date=analytics_date,
            insights=insights,
            post_analytics=post_analytics,
        )

        try:
            _commit_artifact_pair(
                [
                    (artifact_root / raw_relative, raw_content),
                    (artifact_root / report_relative, report_content),
                ]
            )
        except ArtifactCommitError:
            return _failed(
                "ANALYTICS_ARTIFACT_COMMIT_FAILED",
                "Could not commit Daily Analytics artifacts; prior "
                "artifact state was preserved.",
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
