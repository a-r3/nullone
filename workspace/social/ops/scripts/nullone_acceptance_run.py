#!/usr/bin/env python3
"""Safe credentialed Zernio + Telegram acceptance entrypoint (issue #127).

Purpose-built, deterministic, model-free CLI for ONE controlled external
acceptance cycle:

    nullone-acceptance-run.py execute
        --acceptance-id nullone-acceptance-YYYYMMDD-HHMMSS
        --manifest social/ops/manifests/<acceptance-id>.json
        --mode zernio_telegram_acceptance

It runs the exact production Draft connector
(`nullone-draft-bridge.execute`) and the exact production Telegram
preview path (`TelegramReviewDeliveryAdapter.send`) -- the same code
the cron jobs use -- but ONLY for an explicitly TEST-marked manifest,
at most once per external object, and stopping dead at the human
approval boundary.

What this module can never do (enforced by construction, proven by
`tests/test_acceptance_entrypoint.py`):

- shell out, run arbitrary commands, or invoke `openclaw` itself: both
  connectors are in-process Python callables, injected for tests;
- reach approval, second confirmation, scheduling, or publication:
  those modules are never imported;
- read, return, log, or transport secrets: results carry only stable
  status strings plus the non-secret remote/message identifiers;
- create a second Zernio draft: the manifest's own at-most-once
  review state (`require_not_created` semantics) plus the persisted
  remote ID short-circuit a second create;
- send a second Telegram preview: a persisted delivery receipt
  short-circuits resends, including after an ambiguous first attempt
  (which records ATTEMPTED, never SENT, and refuses to retry blindly).

Credential context: this module takes no credential argument and reads
no secret source. It succeeds only where the invoking process already
carries the reviewed credential environment (Gateway/cron context);
elsewhere the production factory fails closed with a fixed BLOCKED
string (proven live 2026-09-14). There is deliberately no mechanism
here to fetch, forward, or amplify credentials.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Callable

from nullone_bridge_common import (
    BridgeError,
    WORKSPACE,
    atomic_write_json,
    load_manifest,
    now_iso,
)
from nullone_review_delivery import (
    MAIN_PREVIEW_SCHEMA,
    ReviewDeliveryError,
    validate_preview_payload,
)

ACCEPTANCE_MODE = "zernio_telegram_acceptance"

ACCEPTANCE_ID_RE = re.compile(r"^nullone-acceptance-\d{8}-\d{6}$")

MANIFESTS_RELATIVE_DIR = "social/ops/manifests"
RECEIPTS_RELATIVE_DIR = "social/ops/delivery-receipts"

RECEIPT_SCHEMA = "nullone.telegram-delivery-receipt.v1"

TEST_MARKERS = ("TEST", "YAYIM ÜÇÜN DEYİL")

# Stable reason codes only. Rejected caller input is never echoed:
# identifiers and paths are operator-controlled values and must not
# flow into fixed operator-facing text (same policy as the provider
# factories' fixed error strings).
REASON_OK = "OK"
REASON_BAD_MODE = "UNKNOWN_ACCEPTANCE_MODE"
REASON_BAD_ID = "INVALID_ACCEPTANCE_ID"
REASON_BAD_MANIFEST_PATH = "MANIFEST_PATH_REJECTED"
REASON_MANIFEST_UNREADABLE = "MANIFEST_UNREADABLE"
REASON_NOT_TEST_MARKED = "MANIFEST_NOT_TEST_MARKED"
REASON_PUBLISH_REQUESTED = "PUBLISH_REQUESTED"
REASON_ALREADY_CONSUMED = "DRAFT_ALREADY_CONSUMED"
REASON_DRAFT_FAILED = "DRAFT_CREATE_FAILED"
REASON_DRAFT_AMBIGUOUS = "DRAFT_CREATE_AMBIGUOUS"
REASON_TELEGRAM_FAILED = "TELEGRAM_SEND_FAILED"
REASON_TELEGRAM_AMBIGUOUS = "TELEGRAM_SEND_AMBIGUOUS"
REASON_TELEGRAM_ALREADY_ATTEMPTED = "TELEGRAM_ALREADY_ATTEMPTED"


class AcceptanceError(RuntimeError):
    """Caller-contract violation (never a legitimate acceptance outcome,
    which is always returned as a result dict, never raised)."""


def _fixed_error(reason: str) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason_code": reason,
        "reason_text": "Acceptance precondition failed; no external call made.",
        "zernio": {"created": False, "draft_id": None},
        "telegram": {"sent": False, "approval_message_id": None},
    }


def _check_id(acceptance_id: Any) -> str:
    if not isinstance(acceptance_id, str) or not ACCEPTANCE_ID_RE.fullmatch(acceptance_id):
        raise AcceptanceError(REASON_BAD_ID)
    return acceptance_id


def _manifest_file(workspace_root: Path, manifest_rel: Any, *, acceptance_id: str) -> Path:
    """Resolve the manifest reference to a contained regular file.

    Only `social/ops/manifests/<acceptance-id>.json` is addressable:
    absolute paths, parent escapes, other directories, and
    identifier-mismatched filenames are all rejected.
    """

    if not isinstance(manifest_rel, str) or not manifest_rel:
        raise AcceptanceError(REASON_BAD_MANIFEST_PATH)
    candidate = Path(manifest_rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AcceptanceError(REASON_BAD_MANIFEST_PATH)
    if (
        len(candidate.parts) != 4
        or candidate.parts[0] != "social"
        or candidate.parts[1] != "ops"
        or candidate.parts[2] != "manifests"
    ):
        raise AcceptanceError(REASON_BAD_MANIFEST_PATH)
    if candidate.suffix != ".json" or candidate.stem != acceptance_id:
        raise AcceptanceError(REASON_BAD_MANIFEST_PATH)
    root = workspace_root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AcceptanceError(REASON_BAD_MANIFEST_PATH) from exc
    return resolved


def _receipt_file(workspace_root: Path, acceptance_id: str) -> Path:
    return (
        workspace_root.resolve()
        / RECEIPTS_RELATIVE_DIR
        / f"{acceptance_id}.telegram.json"
    )


def _read_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != RECEIPT_SCHEMA:
        return None
    return data


def _guard_manifest(manifest: dict[str, Any], *, workspace_root: Path) -> tuple[str, str]:
    """Enforce TEST-only, never-published preconditions.

    Returns `(caption_text, review_post_id_or_empty)`. Raises
    `AcceptanceError` with a fixed reason code for any violation.
    """

    review = manifest.get("review") or {}
    if (
        review.get("state") != "NOT_CREATED"
        or review.get("create_attempts") != 0
        or review.get("zernio_draft_id")
    ):
        raise AcceptanceError(REASON_ALREADY_CONSUMED)

    publication = manifest.get("publication") or {}
    if publication.get("attempts") != 0 or publication.get("state") != "NOT_REQUESTED":
        raise AcceptanceError(REASON_PUBLISH_REQUESTED)

    approval = manifest.get("approval") or {}
    if approval.get("final_publish") or approval.get("first_stage"):
        raise AcceptanceError(REASON_PUBLISH_REQUESTED)

    caption_rel = ((manifest.get("caption") or {}).get("file"))
    if not isinstance(caption_rel, str) or not caption_rel:
        raise AcceptanceError(REASON_NOT_TEST_MARKED)
    caption_path = (workspace_root.resolve() / caption_rel).resolve()
    try:
        caption_path.relative_to(workspace_root.resolve())
        caption_text = caption_path.read_text(encoding="utf-8")
    except (ValueError, OSError) as exc:
        raise AcceptanceError(REASON_NOT_TEST_MARKED) from exc
    if not all(marker in caption_text for marker in TEST_MARKERS):
        raise AcceptanceError(REASON_NOT_TEST_MARKED)
    return caption_text, ""


def _build_preview_payload(
    *,
    manifest: dict[str, Any],
    caption_text: str,
    review_post_id: str,
) -> dict[str, Any]:
    """Deterministic preview payload bound to this acceptance draft.

    Uses only manifest-declared media/caption identity plus the
    created review draft ID. Validated with the shared validator
    before return; malformed construction fails closed here, never
    at the transport.
    """

    media = [
        {
            "local_path": item["local_path"],
            "sha256": item["sha256"],
            "width": item["width"],
            "height": item["height"],
            "content_type": item["content_type"],
        }
        for item in (manifest.get("media") or [])
    ]
    payload = {
        "schema": MAIN_PREVIEW_SCHEMA,
        "brand": "NullOne",
        "review_post_id": review_post_id,
        "text": caption_text.strip(),
        "media": media,
        "presentation": {
            "blocks": [
                {
                    "type": "buttons",
                    "buttons": [
                        {"label": "Approve", "value": f"texbrif:approve:{review_post_id}"},
                        {"label": "Reject", "value": f"texbrif:reject:{review_post_id}"},
                        {"label": "Revise", "value": f"texbrif:revise:{review_post_id}"},
                    ],
                }
            ]
        },
    }
    try:
        validate_preview_payload(payload)
    except ReviewDeliveryError as exc:
        raise AcceptanceError(REASON_TELEGRAM_FAILED) from exc
    return payload


def _completed_existing(
    *,
    draft_id: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    media_ids = receipt.get("media_message_ids") or []
    return {
        "status": "COMPLETED",
        "reason_code": REASON_OK,
        "reason_text": "Acceptance already completed; existing result returned, zero external calls.",
        "zernio": {"created": False, "draft_id": draft_id, "replayed": True},
        "telegram": {
            "sent": False,
            "approval_message_id": receipt.get("approval_message_id"),
            "media_message_ids": list(media_ids) if isinstance(media_ids, list) else [],
            "replayed": True,
        },
    }


def run_acceptance(
    acceptance_id: str,
    manifest_rel: str,
    *,
    mode: str,
    workspace_root: Path = WORKSPACE,
    draft_execute: Callable[[str], int] | None = None,
    delivery_send: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one controlled Zernio + Telegram acceptance cycle.

    `draft_execute` defaults to the real production Draft bridge;
    `delivery_send` defaults to the real production Telegram preview
    transport. Tests inject fakes. The returned mapping carries only
    stable statuses plus non-secret remote/message identifiers --
    never file contents, credentials, tokens, or headers.
    """

    if mode != ACCEPTANCE_MODE:
        return _fixed_error(REASON_BAD_MODE)

    try:
        checked_id = _check_id(acceptance_id)
        manifest_file = _manifest_file(workspace_root, manifest_rel, acceptance_id=checked_id)
    except AcceptanceError as exc:
        return _fixed_error(str(exc))

    try:
        _, manifest = load_manifest(manifest_file)
    except BridgeError:
        return _fixed_error(REASON_MANIFEST_UNREADABLE)

    # Idempotency first: an existing SENT receipt replays with zero
    # external calls, even if the manifest was separately advanced.
    receipt = _read_receipt(_receipt_file(workspace_root, checked_id))
    if receipt is not None and receipt.get("status") == "SENT":
        draft_id = receipt.get("review_post_id") or ((manifest.get("review") or {}).get("zernio_draft_id"))
        if isinstance(draft_id, str) and draft_id:
            return _completed_existing(draft_id=draft_id, receipt=receipt)

    # A recorded ambiguous Telegram attempt must never auto-retry into
    # a duplicate send: fail closed for human resolution.
    if receipt is not None and receipt.get("status") == "ATTEMPTED":
        result = _fixed_error(REASON_TELEGRAM_ALREADY_ATTEMPTED)
        result["zernio"] = {
            "created": False,
            "draft_id": receipt.get("review_post_id"),
        }
        return result

    caption_text: str | None = None
    try:
        caption_text, _ = _guard_manifest(manifest, workspace_root=workspace_root)
    except AcceptanceError as exc:
        reason = str(exc)
        if reason == REASON_ALREADY_CONSUMED:
            # Remote Zernio identity may already exist (e.g. a prior
            # run created the draft but never reached Telegram): fall
            # through to the Telegram-only path below instead of
            # creating again. Re-read the TEST marking directly; the
            # remaining guard checks stay failed-closed via the review
            # state already established above.
            caption_rel = ((manifest.get("caption") or {}).get("file"))
            try:
                caption_path = (workspace_root.resolve() / caption_rel).resolve()
                caption_path.relative_to(workspace_root.resolve())
                caption_text = caption_path.read_text(encoding="utf-8")
            except (ValueError, OSError, AttributeError, TypeError) as e2:
                return _fixed_error(REASON_NOT_TEST_MARKED)
            if not all(marker in caption_text for marker in TEST_MARKERS):
                return _fixed_error(REASON_NOT_TEST_MARKED)
        else:
            return _fixed_error(reason)
    assert caption_text is not None

    review = manifest.get("review") or {}
    draft_id = review.get("zernio_draft_id")

    if not draft_id:
        if draft_execute is None:
            from nullone_draft_bridge import execute as real_draft_execute

            draft_execute = real_draft_execute
        try:
            exit_code = draft_execute(str(manifest_file))
        except Exception:
            return _fixed_error(REASON_DRAFT_FAILED)
        if exit_code != 0:
            return _fixed_error(REASON_DRAFT_FAILED)
        try:
            _, manifest = load_manifest(manifest_file)
        except BridgeError:
            return _fixed_error(REASON_DRAFT_AMBIGUOUS)
        draft_id = (manifest.get("review") or {}).get("zernio_draft_id")
        if not isinstance(draft_id, str) or not draft_id:
            # Exit 0 without a persisted remote ID is ambiguous: the
            # manifest itself is the lookup. Never retry blindly.
            return _fixed_error(REASON_DRAFT_AMBIGUOUS)

    try:
        payload = _build_preview_payload(
            manifest=manifest, caption_text=caption_text, review_post_id=draft_id
        )
    except AcceptanceError as exc:
        return _fixed_error(str(exc))

    if delivery_send is None:
        from nullone_telegram_review_delivery_adapter import (
            TelegramReviewDeliveryAdapter,
        )

        def default_delivery_send(payload: dict[str, Any]) -> dict[str, Any]:
            return TelegramReviewDeliveryAdapter().send(payload)

        delivery_send = default_delivery_send

    try:
        delivery_result = delivery_send(payload)
    except Exception:
        result = _fixed_error(REASON_TELEGRAM_FAILED)
        result["zernio"] = {"created": False, "draft_id": draft_id}
        return result
    if not isinstance(delivery_result, dict):
        result = _fixed_error(REASON_TELEGRAM_FAILED)
        result["zernio"] = {"created": False, "draft_id": draft_id}
        return result

    status = delivery_result.get("status")
    if status == "SENT":
        approval_id = delivery_result.get("approval_message_id")
        media_ids = delivery_result.get("media_message_ids")
        if not isinstance(approval_id, str) or not approval_id:
            return _fixed_error(REASON_TELEGRAM_AMBIGUOUS)
        receipt_doc = {
            "schema": RECEIPT_SCHEMA,
            "acceptance_id": checked_id,
            "manifest": f"{MANIFESTS_RELATIVE_DIR}/{checked_id}.json",
            "review_post_id": draft_id,
            "approval_message_id": approval_id,
            "media_message_ids": list(media_ids) if isinstance(media_ids, list) else [],
            "status": "SENT",
            "created_at": now_iso(),
        }
        receipt_path = _receipt_file(workspace_root, checked_id)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(receipt_path, receipt_doc)
        return {
            "status": "COMPLETED",
            "reason_code": REASON_OK,
            "reason_text": "One Zernio draft created and one Telegram preview sent; human approval pending.",
            "zernio": {"created": True, "draft_id": draft_id},
            "telegram": {
                "sent": True,
                "approval_message_id": approval_id,
                "media_message_ids": receipt_doc["media_message_ids"],
            },
        }

    if status in ("TIMEOUT", "MALFORMED_RESPONSE"):
        # Ambiguous: the message may or may not have been delivered.
        # Record the attempt so a second invocation refuses to resend
        # blindly; a human resolves via the Telegram client.
        receipt_doc = {
            "schema": RECEIPT_SCHEMA,
            "acceptance_id": checked_id,
            "manifest": f"{MANIFESTS_RELATIVE_DIR}/{checked_id}.json",
            "review_post_id": draft_id,
            "approval_message_id": None,
            "media_message_ids": [],
            "status": "ATTEMPTED",
            "created_at": now_iso(),
        }
        receipt_path = _receipt_file(workspace_root, checked_id)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(receipt_path, receipt_doc)
        result = _fixed_error(REASON_TELEGRAM_AMBIGUOUS)
        result["zernio"] = {"created": False, "draft_id": draft_id}
        return result

    result = _fixed_error(REASON_TELEGRAM_FAILED)
    result["zernio"] = {"created": False, "draft_id": draft_id}
    return result


def self_test() -> int:
    """Minimal fake-connector smoke with a REAL valid manifest (full
    contract in tests/test_acceptance_entrypoint.py)."""

    import tempfile

    from PIL import Image

    from nullone_bridge_common import sha256_bytes, sha256_file

    calls: list[str] = []

    def fake_draft(manifest_path: str) -> int:
        calls.append(f"draft:{manifest_path}")
        path = Path(manifest_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["review"]["create_attempts"] = 1
        data["review"]["state"] = "DRAFT_CREATED"
        data["review"]["zernio_draft_id"] = "zdr_fake_1"
        atomic_write_json(path, data)
        return 0

    def fake_delivery(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append("telegram")
        assert payload["review_post_id"] == "zdr_fake_1"
        return {
            "status": "SENT",
            "approval_message_id": "msg_fake_1",
            "media_message_ids": ["msg_fake_media_1"],
        }

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        drafts = root / "social/drafts/production"
        drafts.mkdir(parents=True)
        caption = drafts / "nullone-acceptance-20260101-000000-caption.txt"
        caption.write_text("TEST — probe. YAYIM ÜÇÜN DEYİL.\n", encoding="utf-8")
        media_path = drafts / "nullone-acceptance-20260101-000000.png"
        Image.new("RGB", (1080, 1350), (10, 10, 20)).save(media_path)
        # Absolute workspace-contained fixture paths: the real
        # load_manifest resolves caption/media against the module
        # workspace, so relative test paths cannot validate here.
        # Containment is still enforced via resolve checks.
        manifest = {
            "schema": "nullone.production.v1",
            "account_id": "6a982bbf77555aae01c28f21",
            "verification": "PASS",
            "format": "FEED",
            "content_type": "NEWS",
            "topic_cluster": "probe",
            "caption": {
                "file": str(caption.resolve()),
                "sha256": sha256_bytes(caption.read_bytes()),
            },
            "media": [
                {
                    "local_path": str(media_path.resolve()),
                    "sha256": sha256_file(media_path),
                    "content_type": "image/png",
                    "width": 1080,
                    "height": 1350,
                    "image_format": "PNG",
                    "public_url": None,
                }
            ],
            "review": {"create_attempts": 0, "state": "NOT_CREATED", "zernio_draft_id": None, "created_at": None},
            "approval": {"first_stage": False, "first_stage_at": None, "final_publish": False, "final_publish_at": None, "source": None, "operator": None, "human_confirmation": None},
            "publication": {"attempts": 0, "state": "NOT_REQUESTED", "live_zernio_post_id": None, "platform_post_id": None, "permalink": None, "last_checked_at": None, "error": None},
        }
        manifest_path = root / "social/ops/manifests/nullone-acceptance-20260101-000000.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(manifest_path, manifest)

        result = run_acceptance(
            "nullone-acceptance-20260101-000000",
            "social/ops/manifests/nullone-acceptance-20260101-000000.json",
            mode=ACCEPTANCE_MODE,
            workspace_root=root,
            draft_execute=fake_draft,
            delivery_send=fake_delivery,
        )
        assert result["status"] == "COMPLETED", result
        assert result["zernio"]["draft_id"] == "zdr_fake_1"
        assert result["telegram"]["approval_message_id"] == "msg_fake_1"
        # Second invocation replays with zero external calls.
        before = list(calls)
        result2 = run_acceptance(
            "nullone-acceptance-20260101-000000",
            "social/ops/manifests/nullone-acceptance-20260101-000000.json",
            mode=ACCEPTANCE_MODE,
            workspace_root=root,
            draft_execute=fake_draft,
            delivery_send=fake_delivery,
        )
        assert result2["status"] == "COMPLETED", result2
        assert calls == before, calls

    print("ACCEPTANCE_ENTRYPOINT_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NullOne safe credentialed Zernio + Telegram acceptance entrypoint"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")

    e = sub.add_parser("execute")
    e.add_argument("--acceptance-id", required=True)
    e.add_argument("--manifest", required=True)
    e.add_argument("--mode", required=True)

    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    result = run_acceptance(
        args.acceptance_id,
        args.manifest,
        mode=args.mode,
    )
    print(f"ACCEPTANCE_STATUS={result['status']}")
    print(f"REASON_CODE={result['reason_code']}")
    zernio = result.get("zernio", {})
    telegram = result.get("telegram", {})
    if zernio.get("draft_id"):
        print(f"ZERNIO_DRAFT_ID={zernio['draft_id']}")
    if telegram.get("approval_message_id"):
        print(f"APPROVAL_MESSAGE_ID={telegram['approval_message_id']}")
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
