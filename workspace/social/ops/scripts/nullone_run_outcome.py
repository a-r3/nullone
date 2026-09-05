#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "nullone.run-outcome.v1"

ALLOWED_OUTCOMES = {
    "SUCCEEDED",
    "BLOCKED",
    "FAILED",
    "UNKNOWN",
}

ALLOWED_EMPTY_SUCCESS = {
    "NO_ACTION",
    "NO_DATA",
}

REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

RESULT_FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "workflow_id",
        "occurrence_id",
        "scheduler_status",
        "domain_outcome",
        "health",
        "reason_code",
        "reason_text",
        "empty_success",
        "required_artifacts",
        "missing_artifacts",
    }
)


class CompletionContractError(ValueError):
    pass


def _required_text(
    value: Any,
    field: str,
    *,
    max_len: int = 240,
    single_line: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompletionContractError(f"{field} is required")

    value = value.strip()

    if len(value) > max_len:
        raise CompletionContractError(f"{field} is too long")

    if single_line and ("\n" in value or "\r" in value):
        raise CompletionContractError(f"{field} must be single-line")

    return value


def _reason_code(value: Any) -> str:
    value = _required_text(value, "reason_code", max_len=64)

    if not REASON_CODE_RE.fullmatch(value):
        raise CompletionContractError("invalid reason_code")

    return value


def make_run_id(
    *,
    workflow_id: str,
    occurrence_id: str,
) -> str:
    """Return a stable ID for one logical workflow occurrence.

    Retries of the same logical scheduled occurrence must reuse occurrence_id.
    A later scheduled occurrence must use a different occurrence_id.
    """

    workflow_id = _required_text(
        workflow_id,
        "workflow_id",
        max_len=128,
        single_line=True,
    )
    occurrence_id = _required_text(
        occurrence_id,
        "occurrence_id",
        max_len=160,
        single_line=True,
    )

    canonical = json.dumps(
        [workflow_id, occurrence_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return f"run_{digest}"


def _missing_artifacts(
    *,
    artifact_root: Path,
    required_artifacts: tuple[str, ...],
) -> list[str]:
    root = artifact_root.resolve()
    missing: list[str] = []

    for rel in required_artifacts:
        rel = _required_text(
            rel,
            "required_artifact",
            max_len=240,
            single_line=True,
        )

        rel_path = Path(rel)

        if rel_path.is_absolute():
            raise CompletionContractError(
                "artifact path must be relative"
            )

        path = (root / rel_path).resolve()

        try:
            path.relative_to(root)
        except ValueError as exc:
            raise CompletionContractError(
                "artifact path escapes artifact_root"
            ) from exc

        if not path.is_file() or path.stat().st_size == 0:
            missing.append(rel)

    return missing


def assess_run(
    *,
    workflow_id: str,
    occurrence_id: str,
    scheduler_status: str,
    domain_outcome: str,
    artifact_root: Path | None = None,
    required_artifacts: tuple[str, ...] = (),
    reason_code: str | None = None,
    reason_text: str | None = None,
    empty_success: str | None = None,
) -> dict[str, Any]:
    """Build the authoritative deterministic result for one workflow run."""

    workflow_id = _required_text(
        workflow_id,
        "workflow_id",
        max_len=128,
        single_line=True,
    )
    occurrence_id = _required_text(
        occurrence_id,
        "occurrence_id",
        max_len=160,
        single_line=True,
    )
    scheduler_status = _required_text(
        scheduler_status,
        "scheduler_status",
        max_len=64,
        single_line=True,
    )

    if domain_outcome not in ALLOWED_OUTCOMES:
        raise CompletionContractError("invalid domain_outcome")

    required_artifacts = tuple(required_artifacts)
    final_outcome = domain_outcome
    final_reason_code = reason_code
    final_reason_text = reason_text
    final_empty_success = empty_success
    missing_artifacts: list[str] = []

    if domain_outcome == "SUCCEEDED":
        if reason_code is not None or reason_text is not None:
            raise CompletionContractError(
                "successful outcome cannot include failure reason"
            )

        if empty_success is None and not required_artifacts:
            raise CompletionContractError(
                "successful outcome requires artifacts or explicit empty_success"
            )

        if empty_success is not None:
            if empty_success not in ALLOWED_EMPTY_SUCCESS:
                raise CompletionContractError("invalid empty_success")
        else:
            if required_artifacts and artifact_root is None:
                raise CompletionContractError(
                    "artifact_root required when artifacts are required"
                )

            if artifact_root is not None:
                missing_artifacts = _missing_artifacts(
                    artifact_root=artifact_root,
                    required_artifacts=required_artifacts,
                )

            if missing_artifacts:
                final_outcome = "FAILED"
                final_reason_code = "REQUIRED_ARTIFACT_MISSING"
                final_reason_text = (
                    "Required workflow artifact is missing: "
                    + ", ".join(missing_artifacts)
                )
                final_empty_success = None

    else:
        if empty_success is not None:
            raise CompletionContractError(
                "non-success outcome cannot include empty_success"
            )

        final_reason_code = _reason_code(reason_code)
        final_reason_text = _required_text(
            reason_text,
            "reason_text",
            max_len=240,
            single_line=True,
        )

    return {
        "schema": SCHEMA,
        "run_id": make_run_id(
            workflow_id=workflow_id,
            occurrence_id=occurrence_id,
        ),
        "workflow_id": workflow_id,
        "occurrence_id": occurrence_id,
        "scheduler_status": scheduler_status,
        "domain_outcome": final_outcome,
        "health": (
            "HEALTHY"
            if final_outcome == "SUCCEEDED"
            else "UNHEALTHY"
        ),
        "reason_code": final_reason_code,
        "reason_text": final_reason_text,
        "empty_success": final_empty_success,
        "required_artifacts": list(required_artifacts),
        "missing_artifacts": missing_artifacts,
    }


def validate_domain_completion(
    result: dict[str, Any],
    *,
    artifact_root: Path,
    required_artifacts: tuple[str, ...] = (),
) -> None:
    """Compatibility validator for the executable acceptance contract."""

    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise CompletionContractError("run_id is required")

    scheduler_status = result.get("scheduler_status")
    if not isinstance(scheduler_status, str) or not scheduler_status.strip():
        raise CompletionContractError("scheduler_status is required")

    outcome = result.get("domain_outcome")
    if outcome not in ALLOWED_OUTCOMES:
        raise CompletionContractError("invalid domain_outcome")

    reason_code = result.get("reason_code")

    if outcome != "SUCCEEDED":
        _reason_code(reason_code)
        _required_text(
            result.get("reason_text"),
            "reason_text",
            max_len=240,
            single_line=True,
        )
        return

    empty_success = result.get("empty_success")

    if empty_success is not None:
        if empty_success not in ALLOWED_EMPTY_SUCCESS:
            raise CompletionContractError("invalid empty_success")
        return

    if not required_artifacts:
        raise CompletionContractError(
            "successful outcome requires artifacts or explicit empty_success"
        )

    missing = _missing_artifacts(
        artifact_root=artifact_root,
        required_artifacts=required_artifacts,
    )

    if missing:
        raise CompletionContractError(
            "required artifacts missing: " + ", ".join(missing)
        )


def validate_result_record(
    result: dict[str, Any],
    *,
    artifact_root: Path | None = None,
) -> None:
    """Validate a machine-readable run outcome before durable persistence."""

    actual_fields = set(result)
    missing_fields = sorted(RESULT_FIELDS - actual_fields)
    unexpected_fields = sorted(actual_fields - RESULT_FIELDS)

    if missing_fields:
        raise CompletionContractError(
            "missing result fields: " + ", ".join(missing_fields)
        )

    if unexpected_fields:
        raise CompletionContractError(
            "unexpected result fields: " + ", ".join(unexpected_fields)
        )

    if result.get("schema") != SCHEMA:
        raise CompletionContractError("invalid run outcome schema")

    workflow_id = _required_text(
        result.get("workflow_id"),
        "workflow_id",
        max_len=128,
        single_line=True,
    )
    occurrence_id = _required_text(
        result.get("occurrence_id"),
        "occurrence_id",
        max_len=160,
        single_line=True,
    )
    _required_text(
        result.get("scheduler_status"),
        "scheduler_status",
        max_len=64,
        single_line=True,
    )

    expected_run_id = make_run_id(
        workflow_id=workflow_id,
        occurrence_id=occurrence_id,
    )

    if result.get("run_id") != expected_run_id:
        raise CompletionContractError("run_id does not match occurrence identity")

    outcome = result.get("domain_outcome")
    if outcome not in ALLOWED_OUTCOMES:
        raise CompletionContractError("invalid domain_outcome")

    expected_health = (
        "HEALTHY"
        if outcome == "SUCCEEDED"
        else "UNHEALTHY"
    )

    if result.get("health") != expected_health:
        raise CompletionContractError(
            "health does not match domain_outcome"
        )

    required_artifacts = result.get("required_artifacts")
    missing_artifacts = result.get("missing_artifacts")

    if not isinstance(required_artifacts, list):
        raise CompletionContractError(
            "required_artifacts must be a list"
        )

    if not isinstance(missing_artifacts, list):
        raise CompletionContractError(
            "missing_artifacts must be a list"
        )

    for rel in required_artifacts:
        _required_text(
            rel,
            "required_artifact",
            max_len=240,
            single_line=True,
        )

    for rel in missing_artifacts:
        _required_text(
            rel,
            "missing_artifact",
            max_len=240,
            single_line=True,
        )

    reason_code = result.get("reason_code")
    reason_text = result.get("reason_text")
    empty_success = result.get("empty_success")

    if outcome == "SUCCEEDED":
        if reason_code is not None or reason_text is not None:
            raise CompletionContractError(
                "successful outcome cannot include failure reason"
            )

        if missing_artifacts:
            raise CompletionContractError(
                "successful outcome cannot contain missing artifacts"
            )

        if empty_success is not None:
            if empty_success not in ALLOWED_EMPTY_SUCCESS:
                raise CompletionContractError("invalid empty_success")
        else:
            if not required_artifacts:
                raise CompletionContractError(
                    "successful outcome requires artifacts "
                    "or explicit empty_success"
                )

            if artifact_root is None:
                raise CompletionContractError(
                    "artifact_root required to persist artifact-backed success"
                )

            missing_on_disk = _missing_artifacts(
                artifact_root=artifact_root,
                required_artifacts=tuple(required_artifacts),
            )

            if missing_on_disk:
                raise CompletionContractError(
                    "required artifacts missing: "
                    + ", ".join(missing_on_disk)
                )

        return

    if empty_success is not None:
        raise CompletionContractError(
            "non-success outcome cannot include empty_success"
        )

    _reason_code(reason_code)
    _required_text(
        reason_text,
        "reason_text",
        max_len=240,
        single_line=True,
    )


def atomic_write_result(
    path: Path,
    result: dict[str, Any],
    *,
    artifact_root: Path | None = None,
) -> None:
    validate_result_record(
        result,
        artifact_root=artifact_root,
    )

    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                result,
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    finally:
        tmp.unlink(missing_ok=True)
