#!/usr/bin/env python3
"""Deterministic Telegram approval/control state machine (P0, issue #132).

Authoritative pure logic for first-stage NullOne approval callbacks:

    texbrif:approve:<POST_ID> / reject / revise / back

Callback -> authenticated validation -> current-stage validation ->
deterministic state transition -> deterministic reply card -> STOP.

NO model. NO network. NO Zernio. NO secrets. NO child processes. Stdlib only.

Design notes (emergency stabilization, not the final authority model):

- Authorization comes from the caller's ``authorized`` flag, which the
  plugin sets ONLY from trusted runtime data
  (``handlerCtx.auth.isAuthorizedSender``). A wrapper-written
  ``source="approval"`` string is never accepted as proof.
- Per-post stage is caller-supplied (``current_stage``). Unknown posts
  start at DRAFT_READY. Payload-integrity / content-digest binding stays
  at the final publish gate (``require_final_authorization`` +
  expected snapshot); this layer binds callback -> post -> stage and
  guarantees no publication is ever authorized here
  (``publish_authorized`` is always False, ``zernio_calls`` always 0).
- ``texbrif:publish:*`` is NEVER handled here: it is owned exclusively
  by the deterministic final-publish controller path (#89).
- Duplicate delivery of the same callback identity is idempotent: same
  reply, no state change. Redelivery after a transition converges to the
  same safe card instead of erroring (double-tap safe).
- Terminal stages (REJECTED / REVISION_REQUESTED) only converge on the
  same action; any other consequential action is a wrong-state rejection.
  Editorial revision itself runs later through Draft/OpenCode, outside
  this control transition.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Closed vocabularies. Anything outside is malformed, never authorized.
STAGE_DRAFT_READY = "DRAFT_READY"
STAGE_AWAITING = "AWAITING_PUBLISH_CONFIRMATION"
STAGE_REJECTED = "REJECTED"
STAGE_REVISION = "REVISION_REQUESTED"

STAGES = frozenset(
    {STAGE_DRAFT_READY, STAGE_AWAITING, STAGE_REJECTED, STAGE_REVISION}
)

ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_REVISE = "revise"
ACTION_BACK = "back"

ACTIONS = frozenset(
    {ACTION_APPROVE, ACTION_REJECT, ACTION_REVISE, ACTION_BACK}
)

POST_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
MAX_ID_LEN = 128

# Outcomes. Only TRANSITIONED / CONVERGED / DUPLICATE mutate or affirm;
# every REJECTED_* outcome changes nothing and authorizes nothing.
OUTCOME_TRANSITIONED = "TRANSITIONED"
OUTCOME_CONVERGED = "CONVERGED"
OUTCOME_DUPLICATE = "DUPLICATE"
OUTCOME_REJECTED_UNAUTHORIZED = "REJECTED_UNAUTHORIZED"
OUTCOME_REJECTED_MALFORMED = "REJECTED_MALFORMED"
OUTCOME_REJECTED_WRONG_STATE = "REJECTED_WRONG_STATE"
# P0 #140: callback for a review object whose editorial date has elapsed
# (caller-proven via nullone_review_lifecycle). Fail closed: no Zernio
# mutation, no publication, no second confirmation, no model call, and
# the stale card is never resurrected into a live approval flow.
OUTCOME_REJECTED_EXPIRED = "REJECTED_EXPIRED"

# Deterministic bounded reply texts (Azerbaijani, matching existing tone).
TEXT_APPROVE_CARD = (
    "Rauf, bu NullOne draft\u0131 son t\u0259sdiqd\u0259n sonra "
    "Instagram-da yay\u0131mlanacaq."
)
TEXT_REJECTED = "\u274c \u0130mtina edildi. He\u00e7 n\u0259 yay\u0131mlanmad\u0131."
TEXT_REVISE = "Rauf, hans\u0131 d\u0259yi\u015fikliyi ist\u0259yirs\u0259n?"
TEXT_BACK_SAFE = (
    "Yay\u0131m l\u0259\u011fv edildi. Draft d\u0259yi\u015fm\u0259d\u0259n saxlan\u0131ld\u0131."
)
TEXT_WRONG_STATE = (
    "\u26d4 Bu sor\u011fu cari m\u0259rh\u0259l\u0259 \u00fc\u00e7\u00fcn ke\u00e7\u0259rli deyil. "
    "He\u00e7 n\u0259 d\u0259yi\u015fm\u0259di."
)
TEXT_MALFORMED = (
    "\u26d4 Sor\u011fu q\u0259bul edilm\u0259di. He\u00e7 n\u0259 d\u0259yi\u015fm\u0259di."
)
TEXT_UNAUTHORIZED = "\u26d4 \u0130caz\u0259siz sor\u011fu."
TEXT_EXPIRED = (
    "\u23f3 Bu sor\u011funun redaksiya g\u00fcn\u00fc ke\u00e7ib. "
    "He\u00e7 n\u0259 yay\u0131mlanmad\u0131."
)

# (stage, action) -> (to_stage, converged_only). Missing entries are
# wrong-state rejections. ``converged_only`` marks transitions that must
# not move state (safe-view / idempotent affirmations).
_TRANSITIONS: dict[tuple[str, str], tuple[str, bool]] = {
    (STAGE_DRAFT_READY, ACTION_APPROVE): (STAGE_AWAITING, False),
    (STAGE_DRAFT_READY, ACTION_REJECT): (STAGE_REJECTED, False),
    (STAGE_DRAFT_READY, ACTION_REVISE): (STAGE_REVISION, False),
    (STAGE_DRAFT_READY, ACTION_BACK): (STAGE_DRAFT_READY, True),
    (STAGE_AWAITING, ACTION_APPROVE): (STAGE_AWAITING, True),
    (STAGE_AWAITING, ACTION_REJECT): (STAGE_REJECTED, False),
    (STAGE_AWAITING, ACTION_REVISE): (STAGE_REVISION, False),
    (STAGE_AWAITING, ACTION_BACK): (STAGE_DRAFT_READY, False),
    (STAGE_REJECTED, ACTION_REJECT): (STAGE_REJECTED, True),
    (STAGE_REJECTED, ACTION_BACK): (STAGE_REJECTED, True),
    (STAGE_REVISION, ACTION_REVISE): (STAGE_REVISION, True),
    (STAGE_REVISION, ACTION_BACK): (STAGE_REVISION, True),
}


def _is_non_empty_id(value: object) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= MAX_ID_LEN
    )


def _is_positive_message_id(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str) and value.isdigit():
        try:
            return int(value) > 0
        except Exception:
            return False
    return False


def callback_key(
    *,
    action: str,
    post_id: str,
    message_id: object,
    chat_id: object,
    sender_id: object,
) -> str:
    """Stable replay/duplicate identity for one callback delivery."""
    return "|".join(
        [action, post_id.lower(), str(message_id), str(chat_id), str(sender_id)]
    )


def reply_for(action: str, post_id: str) -> dict:
    """Deterministic reply card for an accepted control callback."""
    post = post_id.lower()
    if action == ACTION_APPROVE:
        return {
            "text": TEXT_APPROVE_CARD,
            "buttons": [
                {
                    "label": "\U0001f680 Payla\u015f",
                    "value": f"texbrif:publish:{post}",
                    "style": "success",
                },
                {"label": "\u21a9\ufe0f Geri", "value": f"texbrif:back:{post}"},
            ],
        }
    if action == ACTION_REJECT:
        return {"text": TEXT_REJECTED, "buttons": None}
    if action == ACTION_REVISE:
        return {"text": TEXT_REVISE, "buttons": None}
    return {"text": TEXT_BACK_SAFE, "buttons": None}


def handle_approval_callback(
    *,
    action: object,
    post_id: object,
    current_stage: object,
    authorized: object,
    message_id: object,
    chat_id: object,
    account_id: object,
    sender_id: object,
    seen_keys: set[str] | None = None,
    review_expired: object = False,
) -> dict:
    """Handle one first-stage approval callback deterministically.

    ``seen_keys`` is caller-owned idempotency storage (mutated only on
    accepted callbacks). ``review_expired`` is caller-proven staleness
    (P0 #140: the review object's Asia/Baku editorial date elapsed
    before the current date, per nullone_review_lifecycle): when True
    the callback fails closed with REJECTED_EXPIRED -- no transition,
    no seen_keys mutation, no second confirmation -- regardless of
    action or stage. Defaults False, preserving all existing behavior.

    Returns a result dict with ``outcome``,
    ``from_stage``/``to_stage``, ``reply`` (``{"text", "buttons"}``),
    ``receipt``, and invariant ``publish_authorized=False`` /
    ``zernio_calls=0``.
    """
    base_receipt = {
        "action": action if isinstance(action, str) else None,
        "post_id": post_id.lower()
        if isinstance(post_id, str)
        else None,
        "publish_authorized": False,
        "zernio_calls": 0,
    }

    def rejected(outcome: str, text: str, stage: object) -> dict:
        return {
            "outcome": outcome,
            "from_stage": stage,
            "to_stage": stage,
            "reply": {"text": text, "buttons": None},
            "receipt": {**base_receipt, "outcome": outcome},
            "publish_authorized": False,
            "zernio_calls": 0,
        }

    if authorized is not True:
        return rejected(
            OUTCOME_REJECTED_UNAUTHORIZED, TEXT_UNAUTHORIZED, current_stage
        )
    if review_expired is True:
        # P0 #140: stale card from a prior editorial date. Fail closed
        # before duplicate tracking so an expired callback can never
        # converge into, or resurrect, a live approval flow. seen_keys
        # is intentionally NOT mutated.
        return rejected(
            OUTCOME_REJECTED_EXPIRED, TEXT_EXPIRED, current_stage
        )
    if not isinstance(action, str) or action not in ACTIONS:
        return rejected(
            OUTCOME_REJECTED_MALFORMED, TEXT_MALFORMED, current_stage
        )
    if (
        not isinstance(post_id, str)
        or not POST_ID_RE.match(post_id)
        or not isinstance(current_stage, str)
        or current_stage not in STAGES
        or not _is_positive_message_id(message_id)
        or not _is_non_empty_id(chat_id)
        or not _is_non_empty_id(account_id)
        or not _is_non_empty_id(sender_id)
    ):
        return rejected(
            OUTCOME_REJECTED_MALFORMED, TEXT_MALFORMED, current_stage
        )

    post = post_id.lower()
    key = callback_key(
        action=action,
        post_id=post,
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
    )
    if seen_keys is not None and key in seen_keys:
        reply = reply_for(action, post)
        return {
            "outcome": OUTCOME_DUPLICATE,
            "from_stage": current_stage,
            "to_stage": current_stage,
            "reply": reply,
            "receipt": {
                **base_receipt,
                "post_id": post,
                "outcome": OUTCOME_DUPLICATE,
            },
            "publish_authorized": False,
            "zernio_calls": 0,
        }

    transition = _TRANSITIONS.get((current_stage, action))
    if transition is None:
        return rejected(
            OUTCOME_REJECTED_WRONG_STATE, TEXT_WRONG_STATE, current_stage
        )
    to_stage, converged = transition
    outcome = OUTCOME_CONVERGED if converged else OUTCOME_TRANSITIONED
    if seen_keys is not None:
        seen_keys.add(key)
    reply = reply_for(action, post)
    return {
        "outcome": outcome,
        "from_stage": current_stage,
        "to_stage": to_stage,
        "reply": reply,
        "receipt": {
            **base_receipt,
            "post_id": post,
            "outcome": outcome,
            "from_stage": current_stage,
            "to_stage": to_stage,
        },
        "publish_authorized": False,
        "zernio_calls": 0,
    }


def self_test() -> None:
    """Offline deterministic self-test (no network, no model)."""
    post = "0123456789abcdef01234567"
    ids = {
        "message_id": 424242,
        "chat_id": "770011",
        "account_id": "test-bot-account",
        "sender_id": "990022",
    }

    def call(**kw):
        args = {
            "authorized": True,
            "post_id": post,
            "current_stage": STAGE_DRAFT_READY,
            **ids,
            **kw,
        }
        return handle_approval_callback(**args)

    # Valid Approve -> second-confirmation card with legacy button values.
    seen: set[str] = set()
    res = call(action="approve", seen_keys=seen)
    assert res["outcome"] == OUTCOME_TRANSITIONED, res
    assert res["to_stage"] == STAGE_AWAITING, res
    assert res["publish_authorized"] is False and res["zernio_calls"] == 0
    assert res["reply"]["text"] == TEXT_APPROVE_CARD, res
    buttons = res["reply"]["buttons"]
    assert buttons and buttons[0]["value"] == f"texbrif:publish:{post}"
    assert buttons[1]["value"] == f"texbrif:back:{post}"

    # Duplicate Approve (same identity) -> idempotent, same card.
    dup = call(action="approve", seen_keys=seen)
    assert dup["outcome"] == OUTCOME_DUPLICATE, dup
    assert dup["reply"] == res["reply"], (dup, res)

    # Converged Approve on AWAITING (new card identity) -> same card.
    conv = handle_approval_callback(
        authorized=True,
        action="approve",
        post_id=post,
        current_stage=STAGE_AWAITING,
        seen_keys=set(),
        **ids,
    )
    assert conv["outcome"] == OUTCOME_CONVERGED, conv
    assert conv["reply"] == res["reply"], (conv, res)

    # Reject / Revise from DRAFT_READY.
    rej = call(action="reject")
    assert (rej["outcome"], rej["to_stage"]) == (
        OUTCOME_TRANSITIONED,
        STAGE_REJECTED,
    ), rej
    rev = call(action="revise")
    assert (rev["outcome"], rev["to_stage"]) == (
        OUTCOME_TRANSITIONED,
        STAGE_REVISION,
    ), rev

    # Back from AWAITING returns to the safe view.
    back = handle_approval_callback(
        authorized=True,
        action="back",
        post_id=post,
        current_stage=STAGE_AWAITING,
        **ids,
    )
    assert (back["outcome"], back["to_stage"]) == (
        OUTCOME_TRANSITIONED,
        STAGE_DRAFT_READY,
    ), back

    # Wrong stage: approve after terminal REJECTED.
    wrong = handle_approval_callback(
        authorized=True,
        action="approve",
        post_id=post,
        current_stage=STAGE_REJECTED,
        **ids,
    )
    assert wrong["outcome"] == OUTCOME_REJECTED_WRONG_STATE, wrong
    assert wrong["to_stage"] == STAGE_REJECTED, wrong

    # Wrong post reference: malformed ids never transition.
    for bad in ("ZZZ", "short", "", f"{post}:extra", post[:-1]):
        bad_res = call(action="approve", post_id=bad)
        assert bad_res["outcome"] == OUTCOME_REJECTED_MALFORMED, (bad, bad_res)

    # Unauthorized (wrapper text is not proof) + bad identity fields.
    unauth = call(action="approve", authorized=False)
    assert unauth["outcome"] == OUTCOME_REJECTED_UNAUTHORIZED, unauth
    for field in ("message_id", "chat_id", "account_id", "sender_id"):
        bad_ids = dict(ids)
        bad_ids[field] = 0 if field == "message_id" else ""
        bad_res = handle_approval_callback(
            authorized=True,
            action="approve",
            post_id=post,
            current_stage=STAGE_DRAFT_READY,
            **bad_ids,
        )
        assert bad_res["outcome"] == OUTCOME_REJECTED_MALFORMED, (
            field,
            bad_res,
        )

    # Unknown stage + publish action (never owned here).
    unk = call(action="approve", current_stage="PUBLISHED")
    assert unk["outcome"] == OUTCOME_REJECTED_MALFORMED, unk
    pub = call(action="publish")
    assert pub["outcome"] == OUTCOME_REJECTED_MALFORMED, pub

    # Full transition-table sweep: every (stage, action) resolves and
    # never authorizes publication or touches Zernio.
    for stage in STAGES:
        for action in (
            ACTION_APPROVE,
            ACTION_REJECT,
            ACTION_REVISE,
            ACTION_BACK,
        ):
            sweep = handle_approval_callback(
                authorized=True,
                action=action,
                post_id=post,
                current_stage=stage,
                **ids,
            )
            assert sweep["publish_authorized"] is False, (stage, action)
            assert sweep["zernio_calls"] == 0, (stage, action)
            assert sweep["receipt"]["publish_authorized"] is False

    # P0 #140: expired review callbacks fail closed on every stage and
    # action: no transition, no idempotency mutation, no publish, no
    # Zernio, expired reply card.
    for stage in STAGES:
        for action in (
            ACTION_APPROVE,
            ACTION_REJECT,
            ACTION_REVISE,
            ACTION_BACK,
        ):
            seen_expired: set[str] = set()
            stale = handle_approval_callback(
                authorized=True,
                action=action,
                post_id=post,
                current_stage=stage,
                seen_keys=seen_expired,
                review_expired=True,
                **ids,
            )
            assert stale["outcome"] == OUTCOME_REJECTED_EXPIRED, (
                stage,
                action,
                stale,
            )
            assert stale["to_stage"] == stage, (stage, action, stale)
            assert stale["publish_authorized"] is False, (stage, action)
            assert stale["zernio_calls"] == 0, (stage, action)
            assert stale["receipt"]["publish_authorized"] is False
            assert stale["reply"]["buttons"] is None, (stage, action)
            assert seen_expired == set(), (stage, action)

    print("APPROVAL_CONTROLLER_SELF_TEST=PASS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["self-test"])
    args = parser.parse_args(argv)
    if args.command == "self-test":
        self_test()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
