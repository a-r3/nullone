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

    stage               DRAFT_READY | AWAITING_PUBLISH_CONFIRMATION |
                        REJECTED | REVISION_REQUESTED. Absent (a manifest
                        written before this field existed) derives a safe
                        fallback from the EXISTING publication/approval
                        fields instead of blindly defaulting to
                        DRAFT_READY -- see ``approval_stage``.
    first_stage         True once any first-stage decision has been recorded
    first_stage_at      ISO-8601 UTC timestamp of that decision
    operator            the authorized Telegram sender_id that decided
    source              "telegram"
    pending_ledger_event  durable outbox marker: non-null only between a
                        committed stage transition and its audit-ledger row
                        actually landing -- see ``_persist_transition``.

Two correctness properties proven by review of this module (PR #162):

1. Once ``approval.stage`` changes on disk, the human is NEVER told the
   transition didn't happen, even if the audit ledger append fails
   afterward (a durable ``pending_ledger_event`` outbox marker recovers it
   later -- opportunistically on the next access, or via an explicit
   ``recover_pending_ledger_events()`` sweep).
2. A legacy manifest with no ``approval.stage`` is NEVER treated as fresh
   DRAFT_READY if it already reached second-stage publication, or if its
   pre-existing fields make its true first-stage decision unrecoverable --
   both fail closed instead of guessing.

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
    TEXT_WRONG_STATE,
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
OUTCOME_REJECTED_LEGACY_UNSAFE = "REJECTED_LEGACY_UNSAFE"
OUTCOME_PERSISTENCE_FAILED = "PERSISTENCE_FAILED"

TEXT_NOT_FOUND = "⛔ Bu draft tapılmadı. Heç nə dəyişmədi."
TEXT_STATE_CORRUPT = (
    "⛔ Draft vəziyyəti oxuna bilmədi. Heç nə dəyişmədi."
)
# Reuses the existing wrong-state wording (nullone_approval_controller):
# from the human's point of view "this legacy item can't be safely
# actioned" and "this request doesn't match the current stage" are the
# same situation -- no new vocabulary needed.
TEXT_LEGACY_UNSAFE = TEXT_WRONG_STATE
TEXT_PERSISTENCE_FAILED = (
    "⚠️ Nəticə yadda saxlanılmadı. Yenidən cəhd et."
)


class LegacyStageUnsafeError(BridgeError):
    """A legacy (pre-approval.stage) manifest cannot be safely defaulted to
    DRAFT_READY -- covers both proven-terminal (already published/attempted)
    and ambiguous (some first-stage signal exists but which decision it was
    is unrecoverable) legacy manifests. Both fail closed identically."""


def _closed(
    outcome: str, text: str, stage: str, *, ledger_sync: str | None = None
) -> dict[str, Any]:
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
        # None where no manifest was ever examined (not-found/unauthorized/
        # malformed) or a write failure leaves its true on-disk state
        # unconfirmed; "pending"/"flushed" wherever a manifest WAS read.
        "ledger_sync": ledger_sync,
    }


def _ledger_sync_state(manifest: dict[str, Any]) -> str:
    """"pending" if this manifest still carries an unflushed audit-ledger
    outbox marker, "flushed" otherwise. Observability only -- never
    influences the human-facing outcome/reply."""
    pending = (manifest.get("approval") or {}).get("pending_ledger_event")
    return "pending" if pending else "flushed"


def approval_stage(manifest: dict[str, Any]) -> str:
    """Durable stage for one manifest.

    A manifest written by this fix carries an explicit ``approval.stage``
    and that value alone is authoritative -- present-but-invalid (corrupted
    or hand-edited) raises BridgeError rather than being silently coerced.

    A manifest written BEFORE this fix existed has no ``approval.stage`` at
    all. Defaulting that blindly to DRAFT_READY is only safe for a manifest
    that was genuinely never actioned. Using the EXISTING durable fields
    already in the schema (no new meanings invented):

    - ``publication.attempts >= 1`` or ``publication.state`` not in
      (None, "NOT_REQUESTED") -- this manifest already went through the
      second-stage publish flow. Proven terminal: fail closed
      (LegacyStageUnsafeError), never DRAFT_READY.
    - ``approval.final_publish is True`` or ``approval.first_stage is
      True`` -- some first-stage/second-stage signal was recorded under
      the old (pre-``stage``) mechanism, but WHICH decision it was
      (approve vs reject vs revise) is not recoverable from these two
      booleans alone. Ambiguous: fail closed (LegacyStageUnsafeError)
      rather than guess.
    - Otherwise -- first_stage is False/absent AND publication was never
      attempted AND final_publish is False/absent -- this item was
      genuinely never actioned. Safe: DRAFT_READY.
    """
    approval = manifest.get("approval") or {}
    if "stage" in approval:
        stage = approval.get("stage")
        if stage not in STAGES:
            raise BridgeError(f"Invalid durable approval stage: {stage!r}")
        return stage

    publication = manifest.get("publication") or {}
    attempts = publication.get("attempts", 0)
    pub_state = publication.get("state")
    if (isinstance(attempts, int) and attempts >= 1) or (
        pub_state not in (None, "NOT_REQUESTED")
    ):
        raise LegacyStageUnsafeError(
            "legacy manifest already reached second-stage publication "
            f"(attempts={attempts!r}, state={pub_state!r})"
        )
    if approval.get("final_publish") is True or approval.get("first_stage") is True:
        raise LegacyStageUnsafeError(
            "legacy manifest carries an unrecoverable pre-stage approval signal"
        )
    return STAGE_DRAFT_READY


def _ledger_record(manifest: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """The exact ledger row `record_approval_decision` would build -- kept
    as a pure function so the SAME record can be embedded as a durable
    outbox marker on the manifest and later replayed byte-identically."""
    review = manifest.get("review") or {}
    return {
        "timestamp": now_iso(),
        "event": "APPROVAL_DECISION",
        "manifest_id": manifest.get("manifest_id"),
        "candidate_id": manifest.get("candidate_id"),
        "topic": manifest.get("topic"),
        "topic_cluster": manifest.get("topic_cluster"),
        "review_post_id": review.get("zernio_draft_id"),
        "action": result.get("receipt", {}).get("action"),
        "from_stage": result.get("from_stage"),
        "to_stage": result.get("to_stage"),
        "outcome": result.get("outcome"),
    }


def _flush_pending_ledger_event(
    manifest_path: Path, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Best-effort: if this manifest carries a pending (unflushed) ledger
    event, retry appending it and clear the marker on success.

    Idempotent and safe to call unconditionally on every access to this
    manifest (opportunistic self-healing) or from an explicit restart-time
    sweep (``recover_pending_ledger_events``). Never touches
    ``approval.stage`` -- this only catches up the AUDIT trail, which lags
    behind the (already-authoritative) manifest by construction. Must be
    called while holding ``review_post_lock(post_id)``.

    Returns the manifest as it now stands on disk (unchanged if there was
    nothing pending, or if the flush attempt itself failed again).
    """
    approval = manifest.get("approval") or {}
    pending = approval.get("pending_ledger_event")
    if not pending:
        return manifest
    # Deliberately broad and never re-raised past this function: a failed
    # (or partially failed) catch-up attempt must never turn an already-
    # committed transition into a reported failure. Worst case on any
    # exception here is exactly the pre-flush state -- marker stays
    # pending, picked up by the next opportunistic access or an explicit
    # recover_pending_ledger_events() sweep. append_jsonl_once is
    # idempotent by content, so retrying a half-finished attempt (ledger
    # row already written, marker-clear write failed) never duplicates
    # the row.
    try:
        record_approval_decision(manifest, pending)
        cleared_approval = dict(approval)
        cleared_approval["pending_ledger_event"] = None
        cleared_manifest = {**manifest, "approval": cleared_approval}
        atomic_write_json(manifest_path, cleared_manifest)
    except Exception:
        # Return the manifest exactly as it still stands on disk (marker
        # still pending) -- never claim a clear that didn't durably commit.
        return manifest
    return cleared_manifest


def recover_pending_ledger_events(review_post_id: str | None = None) -> list[str]:
    """Explicit restart/reboot recovery sweep: flush every manifest's
    pending ledger event.

    Safe to call at Gateway boot, from a periodic reconciliation job (the
    existing pattern -- see ``nullone_state.reconcile``), or directly in
    tests. Scans a single post when ``review_post_id`` is given, otherwise
    every manifest in the workspace. Returns the review_post_ids that had a
    pending event flushed.

    ``pending_ledger_event`` records ARE the durable, recoverable marker
    this scan needs: it never depends on any process-local queue.
    """
    import nullone_bridge_common as _common

    flushed: list[str] = []
    if review_post_id is not None:
        try:
            manifest_path, manifest = find_manifest_by_review_post_id(
                review_post_id.lower()
            )
        except BridgeError:
            return flushed
        candidates = [(manifest_path, manifest, review_post_id.lower())]
    else:
        candidates = []
        for manifest_path in sorted(_common.MANIFEST_DIR.glob("*.json")):
            try:
                _, manifest = _common.load_manifest(manifest_path)
            except Exception:
                continue
            post_id = (manifest.get("review") or {}).get("zernio_draft_id")
            if isinstance(post_id, str) and POST_ID_RE.match(post_id):
                candidates.append((manifest_path, manifest, post_id.lower()))

    for manifest_path, manifest, post_id in candidates:
        approval = manifest.get("approval") or {}
        if not approval.get("pending_ledger_event"):
            continue
        with review_post_lock(post_id):
            # Re-read under the lock: the marker may have already been
            # flushed by another caller between the scan above and here.
            try:
                manifest_path, manifest = find_manifest_by_review_post_id(post_id)
            except BridgeError:
                continue
            before = (manifest.get("approval") or {}).get("pending_ledger_event")
            if not before:
                continue
            after = _flush_pending_ledger_event(manifest_path, manifest)
            if not (after.get("approval") or {}).get("pending_ledger_event"):
                flushed.append(post_id)
    return flushed


def _persist_transition(
    manifest_path: Path,
    manifest: dict[str, Any],
    result: dict[str, Any],
    sender_id: Any,
) -> dict[str, Any]:
    """Commit a real stage transition durably, then catch the audit ledger
    up to it -- in that exact order, and never the other way around.

    Ordering (this is the fix for the manifest/ledger split-brain a prior
    review found):

    1. Build the ledger record.
    2. ONE atomic write: the new ``approval.stage`` AND the ledger record
       as ``approval.pending_ledger_event`` land in the SAME manifest
       write. The instant this write is durable, the transition is
       committed AND the audit event is durably queued -- there is no
       window where the stage changed but the outbox marker doesn't exist
       yet, and no window where the marker exists without the stage having
       changed.
    3. Best-effort: append the ledger row and clear the marker
       (``_flush_pending_ledger_event``). If this fails (or the process
       dies before it runs at all), the marker stays on the manifest and
       is recovered later -- by the next callback for this post
       (opportunistic) or by ``recover_pending_ledger_events`` (explicit
       sweep) -- never lost, never duplicated (``append_jsonl_once`` is
       idempotent by content).

    Only step 2 can produce ``NO_FALSE_SUCCESS_IF_AUTHORITATIVE_MANIFEST_
    WRITE_FAILS``-relevant PERSISTENCE_FAILED: if it raises, the caller
    (below) still reports failure and current_stage is genuinely
    unchanged. Step 3 never raises and never changes what gets reported to
    the human -- the transition already stands once step 2 committed.
    """
    record = _ledger_record(manifest, result)
    approval = dict(manifest.get("approval") or {})
    approval["stage"] = result["to_stage"]
    approval["first_stage"] = True
    approval["first_stage_at"] = now_iso()
    approval["operator"] = str(sender_id) if isinstance(sender_id, (str, int)) else None
    approval["source"] = "telegram"
    approval["pending_ledger_event"] = record
    manifest = {**manifest, "approval": approval}
    atomic_write_json(manifest_path, manifest)  # <-- transition is now durable

    return _flush_pending_ledger_event(manifest_path, manifest)


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

        # Opportunistic self-healing: catch up any audit ledger row left
        # pending by a prior transition (e.g. the process died between the
        # manifest write and the ledger append) before doing anything else.
        # Never touches approval.stage; safe on every access.
        manifest = _flush_pending_ledger_event(manifest_path, manifest)

        try:
            current_stage = approval_stage(manifest)
        except LegacyStageUnsafeError:
            return _closed(
                OUTCOME_REJECTED_LEGACY_UNSAFE,
                TEXT_LEGACY_UNSAFE,
                STAGE_DRAFT_READY,
                ledger_sync=_ledger_sync_state(manifest),
            )
        except BridgeError:
            return _closed(
                OUTCOME_REJECTED_STATE_CORRUPT,
                TEXT_STATE_CORRUPT,
                STAGE_DRAFT_READY,
                ledger_sync=_ledger_sync_state(manifest),
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
                manifest = _persist_transition(manifest_path, manifest, result, sender_id)
            except Exception:
                # The in-memory decision was computed but never durably
                # stuck: report as if nothing changed (fail closed) so the
                # human sees an honest "try again", never a false success.
                return _closed(
                    OUTCOME_PERSISTENCE_FAILED,
                    TEXT_PERSISTENCE_FAILED,
                    current_stage,
                    ledger_sync=None,
                )

        # Observability (PR #162 review): whether the audit ledger has
        # actually caught up to the manifest's current durable state, or a
        # pending_ledger_event outbox marker is still waiting on recovery.
        # Never changes the human-facing outcome/reply -- that was already
        # decided above from the manifest transition alone.
        return {**result, "ledger_sync": _ledger_sync_state(manifest)}


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
    parser.add_argument("command", choices=["self-test", "recover-pending"])
    parser.add_argument(
        "--review-post-id",
        default=None,
        help="Recover only this post; default scans every manifest.",
    )
    args = parser.parse_args(argv)
    if args.command == "self-test":
        self_test()
        return 0
    if args.command == "recover-pending":
        flushed = recover_pending_ledger_events(args.review_post_id)
        print(f"RECOVERED_COUNT={len(flushed)}")
        for post_id in flushed:
            print(f"RECOVERED_REVIEW_POST_ID={post_id}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
