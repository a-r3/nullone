from __future__ import annotations

from pathlib import Path
from typing import Any

ALLOWED_OUTCOMES = {"SUCCEEDED", "BLOCKED", "FAILED", "UNKNOWN"}
ALLOWED_EMPTY_SUCCESS = {"NO_ACTION", "NO_DATA"}


class CompletionContractError(ValueError):
    pass


def validate_domain_completion(
    result: dict[str, Any],
    *,
    artifact_root: Path,
    required_artifacts: tuple[str, ...] = (),
) -> None:
    """Validate the issue #4/#27 domain-completion contract offline.

    This is a CI/reference validator only. It does not claim that current
    production scheduler runs already emit or enforce this structure.
    """

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
        if not isinstance(reason_code, str) or not reason_code.strip():
            raise CompletionContractError(
                "non-success outcome requires reason_code"
            )
        return

    empty_success = result.get("empty_success")
    if empty_success is not None:
        if empty_success not in ALLOWED_EMPTY_SUCCESS:
            raise CompletionContractError("invalid empty_success")
        return

    missing: list[str] = []
    for rel in required_artifacts:
        path = (artifact_root / rel).resolve()
        try:
            path.relative_to(artifact_root.resolve())
        except ValueError as exc:
            raise CompletionContractError(
                "artifact path escapes artifact_root"
            ) from exc

        if not path.is_file() or path.stat().st_size == 0:
            missing.append(rel)

    if missing:
        raise CompletionContractError(
            "required artifacts missing: " + ", ".join(missing)
        )
