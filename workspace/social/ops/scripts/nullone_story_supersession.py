#!/usr/bin/env python3
"""Durable Story supersession guard shared by revision and publication.

The production manifest remains canonical publication state. This module
stores only the narrow fact that one immutable Story review version was
superseded by one logical operator-revision request. It performs no network
or provider calls and has no publication capability.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from nullone_bridge_common import (
    BridgeError,
    atomic_write_json,
    now_iso,
    resolve_workspace_path,
)


SCHEMA = "nullone.story-supersession.v1"
REASON = "OPERATOR_REVISION"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StorySupersessionError(BridgeError):
    """Supersession state is malformed, inaccessible or contradictory."""


class StorySupersessionConflict(StorySupersessionError):
    """A parent Story is already bound to a different revision request."""


def _require_safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise StorySupersessionError(f"Invalid {label}")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorySupersessionError(f"Invalid {label}")
    return value


def review_post_lock_path(review_post_id: str) -> Path:
    safe_id = _require_safe_id(review_post_id, "review post ID")
    return resolve_workspace_path(f"social/ops/locks/review/{safe_id}.lock")


@contextmanager
def review_post_lock(review_post_id: str) -> Iterator[None]:
    """Serialize revision and publication decisions for one review post."""

    lock_path = review_post_lock_path(review_post_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def supersession_path(parent_manifest_id: str) -> Path:
    safe_id = _require_safe_id(parent_manifest_id, "parent manifest ID")
    return resolve_workspace_path(
        f"social/drafts/production/story/superseded/{safe_id}.json"
    )


def revision_instruction_fingerprint(instruction: str) -> str:
    if not isinstance(instruction, str) or not instruction.strip():
        raise StorySupersessionError("Operator revision instruction is required")
    return hashlib.sha256(instruction.encode("utf-8")).hexdigest()


def validate_supersession_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise StorySupersessionError("Invalid Story supersession record")

    expected_fields = {
        "schema",
        "parent_manifest_id",
        "parent_review_post_id",
        "candidate_id",
        "superseded_by_story_request_id",
        "revision_instruction_sha256",
        "created_at",
        "reason",
    }
    if set(record) != expected_fields or record.get("schema") != SCHEMA:
        raise StorySupersessionError("Invalid Story supersession record")
    if record.get("reason") != REASON:
        raise StorySupersessionError("Invalid Story supersession reason")

    _require_safe_id(record.get("parent_manifest_id"), "parent manifest ID")
    _require_safe_id(record.get("parent_review_post_id"), "parent review post ID")
    _require_text(record.get("candidate_id"), "candidate ID")
    _require_safe_id(
        record.get("superseded_by_story_request_id"),
        "superseding Story request ID",
    )
    if not _SHA256_RE.fullmatch(str(record.get("revision_instruction_sha256", ""))):
        raise StorySupersessionError("Invalid revision instruction fingerprint")
    if not isinstance(record.get("created_at"), str) or not record["created_at"]:
        raise StorySupersessionError("Invalid Story supersession timestamp")
    return record


def load_story_supersession(
    parent_manifest_id: str,
    parent_review_post_id: str,
) -> dict[str, Any] | None:
    """Load exact supersession state, failing closed on malformed state."""

    path = supersession_path(parent_manifest_id)
    if not path.exists():
        return None
    try:
        record = validate_supersession_record(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError) as e:
        raise StorySupersessionError("Invalid Story supersession record") from e

    if (
        record["parent_manifest_id"] != parent_manifest_id
        or record["parent_review_post_id"] != parent_review_post_id
    ):
        raise StorySupersessionError("Story supersession identity mismatch")
    return record


def mark_story_superseded(
    *,
    parent_manifest_id: str,
    parent_review_post_id: str,
    candidate_id: str,
    superseded_by_story_request_id: str,
    operator_instruction: str,
) -> tuple[dict[str, Any], bool]:
    """Create one immutable marker, or idempotently reuse the exact marker.

    The caller must hold ``review_post_lock(parent_review_post_id)`` so this
    decision is atomic with authoritative parent-manifest validation.
    Returns ``(record, created)``.
    """

    identity = {
        "parent_manifest_id": _require_safe_id(
            parent_manifest_id, "parent manifest ID"
        ),
        "parent_review_post_id": _require_safe_id(
            parent_review_post_id, "parent review post ID"
        ),
        "candidate_id": _require_text(candidate_id, "candidate ID"),
        "superseded_by_story_request_id": _require_safe_id(
            superseded_by_story_request_id, "superseding Story request ID"
        ),
        "revision_instruction_sha256": revision_instruction_fingerprint(
            operator_instruction
        ),
    }

    existing = load_story_supersession(parent_manifest_id, parent_review_post_id)
    if existing is not None:
        if all(existing[key] == value for key, value in identity.items()):
            return existing, False
        raise StorySupersessionConflict(
            "Story parent is already superseded by a different revision request"
        )

    record = {
        "schema": SCHEMA,
        **identity,
        "created_at": now_iso(),
        "reason": REASON,
    }
    validate_supersession_record(record)
    atomic_write_json(supersession_path(parent_manifest_id), record)
    return record, True


def require_story_not_superseded(
    parent_manifest_id: str,
    parent_review_post_id: str,
) -> None:
    """Block publication when exact durable Story supersession exists."""

    if load_story_supersession(parent_manifest_id, parent_review_post_id) is not None:
        raise StorySupersessionError("STORY_VERSION_SUPERSEDED")
