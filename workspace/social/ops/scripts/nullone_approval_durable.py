#!/usr/bin/env python3
"""Durable first-stage Telegram approval state (P0: issue-durable-approval).

The pure decision logic in ``nullone_approval_controller`` is authoritative
for WHAT a callback means, but it is caller-supplied ``current_stage`` and
caller-owned ``seen_keys`` -- by itself it proves nothing about durability.
The live plugin path (``plugins/nullone-final-publish``) previously supplied
``current_stage`` from a per-process in-memory ``Map``, which a Gateway
restart or process stall silently forgets (a real REJECT for
6ab46980b85359fa0c9305c8 / WeatherNext 3 was received but never left
DRAFT_CREATED in the manifest, ledger, or Zernio -- traced 2026-09-24).

This module makes the per-post manifest the durable source of truth for the
first-stage decision, reusing existing NullOne infrastructure rather than
inventing a second state machine:

- ``find_manifest_by_review_post_id`` (nullone_bridge_common) is ITEM_FOUND.
- ``review_post_lock`` (nullone_story_supersession) is the SAME per-post
  lock the second-stage publish controller already holds, so first- and
  second-stage decisions for one post can never race each other.
- ``atomic_write_json`` (nullone_bridge_common) is the durable write.
- ``manifest_baku_date`` / ``is_effectively_expired``
  (nullone_review_lifecycle) is the existing P0 #140 stale-review contract,
  now actually wired into the live callback path.
- ``append_jsonl_once`` (nullone_state) is the existing idempotent-append
  ledger primitive, reused for the approval-decision audit trail.

The durable record lives at ``manifest["approval"]``:

    stage           DRAFT_READY | AWAITING_PUBLISH_CONFIRMATION |
                    REJECTED | REVISION_REQUESTED (absent == DRAFT_READY,
                    for backward compatibility with manifests written before
                    this field existed)
    first_stage     True once any first-stage decision has been recorded
    first_stage_at  ISO-8601 UTC timestamp of that decision
    operator        the authorized Telegram sender_id that made the decision
    source          "telegram"

NO network. NO Zernio. NO model. NO secrets. Stdlib + existing NullOne
bridge helpers only.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from nullone_approval_controller import (
    OUTCOME_TRANSITIONED,
    POST_ID_RE,
    STAGE_DRAFT_READY,
    STAGES,
    handle_approval_callback,
)
from nullone_bridge_common import (
    BridgeError,
    atomic_write_json,
    find_manifest_by_review_post_id,
    now_iso,
)
from nullone_review_lifecycle import baku_zone, is_effectively_expired, manifest_baku_date
from nullone_state import record_approval_decision
from nullone_story_supersession import review_post_lock

# Outcomes beyond the pure controller's vocabulary: these are properties of
# DURABILITY, not of the callback itself, so they belong here rather than in
# the dependency-free pure module.
OUTCOME_REJECTED_NOT_FOUND = "REJECTED_NOT_FOUND"
OUTCOME_REJECTED_STATE_CORRUPT = "REJECTED_STATE_CORRUPT"
OUTCOME_PERSISTENCE_FAILED = "PERSISTENCE_FAILED"

TEXT_NOT_FOUND = "⛔ Bu draft tapılmadı. Heç nə dəyişmədi."
TEXT_STATE_CORRUPT = (
    "⛔ Draft vəziyyəti oxuna bilmədi. Heç nə dəyişmədi."
)
TEXT_PERSISTENCE_FAILED = (
    "⚠️ Nəticə yadda saxlanılmadı. Yenidən cəhd et."
)


def _closed(outcome: str, text: str, stage: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "from_stage": stage,
        "to_stage": stage,
        "reply": {"text": text, "buttons": None},
        "receipt": {
            "outcome": outcome,
            "publish_authorized": False,
            "zernio_calls": 0,
        },
        "publish_authorized": False,
        "zernio_calls": 0,
    }


def approval_stage(manifest: dict[str, Any]) -> str:
    """Durable stage for one manifest. Absent field == DRAFT_READY.

    Raises BridgeError on a present-but-invalid value: a corrupted or
    hand-edited field must never be silently coerced into a live stage.
    """
    approval = manifest.get("approval")
    if not isinstance(approval, dict) or "stage" not in approval:
        return STAGE_DRAFT_READY
    stage = approval.get("stage")
    if stage not in STAGES:
        raise BridgeError(f"Invalid durable approval stage: {stage!r}")
    return stage


def _persist_transition(
    manifest_path: Path,
    manifest: dict[str, Any],
    result: dict[str, Any],
    sender_id: Any,
) -> None:
    approval = dict(manifest.get("approval") or {})
    approval["stage"] = result["to_stage"]
    approval["first_stage"] = True
    approval["first_stage_at"] = now_iso()
    approval["operator"] = str(sender_id) if isinstance(sender_id, (str, int)) else None
    approval["source"] = "telegram"
    manifest = {**manifest, "approval": approval}
    atomic_write_json(manifest_path, manifest)
    record_approval_decision(manifest, result)


def handle_durable_approval_callback(
    *,
    action: object,
    review_post_id: object,
    authorized: object,
    message_id: object,
    chat_id: object,
    account_id: object,
    sender_id: object,
    today: date | None = None,
) -> dict[str, Any]:
    """Handle one first-stage callback with a durable, locked stage read.

    Unauthorized or syntactically-malformed input is delegated straight to
    the pure decision (no filesystem I/O at all, matching the existing
    fail-closed-before-I/O posture). Otherwise the per-post manifest is the
    single durable authority: its current ``approval.stage`` (never an
    in-memory cache) is read fresh UNDER THE SAME LOCK the second-stage
    publish controller uses, and only an ``OUTCOME_TRANSITIONED`` result is
    ever written back -- a converged/duplicate/rejected outcome never
    rewrites the manifest or appends a ledger row, which is what makes
    replayed/duplicate callbacks idempotent by construction.
    """
    if (
        authorized is not True
        or not isinstance(review_post_id, str)
        or not POST_ID_RE.match(review_post_id)
    ):
        return handle_approval_callback(
            action=action,
            post_id=review_post_id,
            current_stage=STAGE_DRAFT_READY,
            authorized=authorized,
            message_id=message_id,
            chat_id=chat_id,
            account_id=account_id,
            sender_id=sender_id,
            seen_keys=None,
        )

    post_id = review_post_id.lower()
    with review_post_lock(post_id):
        try:
            manifest_path, manifest = find_manifest_by_review_post_id(post_id)
        except BridgeError:
            return _closed(OUTCOME_REJECTED_NOT_FOUND, TEXT_NOT_FOUND, STAGE_DRAFT_READY)

        try:
            current_stage = approval_stage(manifest)
        except BridgeError:
            return _closed(
                OUTCOME_REJECTED_STATE_CORRUPT, TEXT_STATE_CORRUPT, STAGE_DRAFT_READY
            )

        expired = is_effectively_expired(
            manifest_date=manifest_baku_date(manifest),
            today=today if today is not None else datetime.now(baku_zone()).date(),
        )

        result = handle_approval_callback(
            action=action,
            post_id=post_id,
            current_stage=current_stage,
            authorized=True,
            message_id=message_id,
            chat_id=chat_id,
            account_id=account_id,
            sender_id=sender_id,
            seen_keys=None,
            review_expired=expired,
        )

        if result["outcome"] == OUTCOME_TRANSITIONED:
            try:
                _persist_transition(manifest_path, manifest, result, sender_id)
            except Exception:
                # The in-memory decision was computed but never durably
                # stuck: report as if nothing changed (fail closed) so the
                # human sees an honest "try again", never a false success.
                return _closed(
                    OUTCOME_PERSISTENCE_FAILED,
                    TEXT_PERSISTENCE_FAILED,
                    current_stage,
                )

        return result


def self_test() -> None:
    """Offline deterministic self-test against a temp-dir workspace."""
    import shutil
    import tempfile

    import nullone_bridge_common as common

    post = "0123456789abcdef01234567"
    ids = {
        "message_id": 424242,
        "chat_id": "770011",
        "account_id": "test-bot-account",
        "sender_id": "990022",
    }

    tmp = tempfile.mkdtemp()
    old_workspace = common.WORKSPACE
    old_manifest_dir = common.MANIFEST_DIR
    try:
        root = Path(tmp)
        common.WORKSPACE = root
        common.MANIFEST_DIR = root / "social/ops/manifests"

        from PIL import Image

        caption_path = root / "social/drafts/durable-caption.txt"
        caption_path.parent.mkdir(parents=True, exist_ok=True)
        caption_path.write_text("Durable approval fixture.\n", encoding="utf-8")
        media_path = root / "social/drafts/durable-0.png"
        Image.new("RGB", (1080, 1350), (10, 20, 30)).save(media_path, "PNG")
        media = common.inspect_media(media_path, "FEED")

        def make_manifest(stage_fields: dict | None = None, created_at: str | None = None):
            manifest = {
                "schema": common.SCHEMA,
                "manifest_id": "durable-manifest",
                "created_at": created_at or common.now_iso(),
                "candidate_id": "candidate-durable",
                "topic": "Durable approval",
                "topic_cluster": "durable-approval",
                "content_type": "NEWS",
                "format": "FEED",
                "verification": "PASS",
                "account_id": common.CANONICAL_ACCOUNT_ID,
                "caption": {
                    "file": common.workspace_relative(caption_path),
                    "sha256": common.sha256_file(caption_path),
                },
                "media": [media],
                "review": {
                    "create_attempts": 1,
                    "state": "DRAFT_CREATED",
                    "zernio_draft_id": post,
                    "created_at": common.now_iso(),
                },
                "approval": {
                    "first_stage": False,
                    "first_stage_at": None,
                    "final_publish": False,
                    "final_publish_at": None,
                    "source": None,
                    "operator": None,
                    "human_confirmation": None,
                    **(stage_fields or {}),
                },
                "publication": {
                    "attempts": 0,
                    "state": "NOT_REQUESTED",
                    "live_zernio_post_id": None,
                    "platform_post_id": None,
                    "permalink": None,
                    "last_checked_at": None,
                    "error": None,
                },
            }
            path = common.MANIFEST_DIR / "durable-manifest.json"
            common.atomic_write_json(path, manifest)
            return path

        # 1) REJECT persists durably.
        make_manifest()
        result = handle_durable_approval_callback(
            action="reject", review_post_id=post, authorized=True, **ids
        )
        assert result["outcome"] == OUTCOME_TRANSITIONED, result
        assert result["to_stage"] == "REJECTED", result
        _, reloaded = common.load_manifest(common.MANIFEST_DIR / "durable-manifest.json")
        assert reloaded["approval"]["stage"] == "REJECTED", reloaded
        assert reloaded["approval"]["first_stage"] is True
        assert reloaded["approval"]["operator"] == "990022"

        # 2) Restart recovery: fresh read (no process/cache involved at all)
        # still sees REJECTED, and a stale APPROVE cannot resurrect it.
        again = handle_durable_approval_callback(
            action="approve", review_post_id=post, authorized=True, **ids
        )
        assert again["outcome"] == "REJECTED_WRONG_STATE", again
        _, reloaded2 = common.load_manifest(common.MANIFEST_DIR / "durable-manifest.json")
        assert reloaded2["approval"]["stage"] == "REJECTED", reloaded2

        # 3) Duplicate reject converges, no second ledger row.
        ledger_path = common.WORKSPACE / "social/state/topic-ledger.jsonl"
        from nullone_state import read_jsonl

        rows_before = len(read_jsonl(ledger_path))
        dup = handle_durable_approval_callback(
            action="reject", review_post_id=post, authorized=True, **ids
        )
        assert dup["outcome"] == "CONVERGED", dup
        rows_after = len(read_jsonl(ledger_path))
        assert rows_after == rows_before, (rows_before, rows_after)

        # 4) Item not found fails closed.
        missing = handle_durable_approval_callback(
            action="reject",
            review_post_id="aaaaaaaaaaaaaaaaaaaaaaaa",
            authorized=True,
            **ids,
        )
        assert missing["outcome"] == OUTCOME_REJECTED_NOT_FOUND, missing

        # 5) Stale review fails closed regardless of action.
        old_iso = "2020-01-01T00:00:00+00:00"
        make_manifest(created_at=old_iso)
        stale = handle_durable_approval_callback(
            action="approve", review_post_id=post, authorized=True, **ids
        )
        assert stale["outcome"] == "REJECTED_EXPIRED", stale
        _, still_draft = common.load_manifest(common.MANIFEST_DIR / "durable-manifest.json")
        assert still_draft["approval"].get("stage") in (None, "DRAFT_READY", False) or (
            "stage" not in still_draft["approval"]
        ), still_draft

        print("APPROVAL_DURABLE_SELF_TEST=PASS")
    finally:
        common.WORKSPACE = old_workspace
        common.MANIFEST_DIR = old_manifest_dir
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["self-test"])
    args = parser.parse_args(argv)
    if args.command == "self-test":
        self_test()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
