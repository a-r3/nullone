#!/usr/bin/env python3
"""Shared `ReviewDelivery` application/provider port (#62, per #65 M0 ports).

`ReviewDelivery` delivers an already-built Story or main review preview
payload (`nullone.story-preview.v1` / `nullone.main-preview.v1`, produced
verbatim by `nullone_story_pipeline.build_story_preview_payload` /
`nullone_main_draft_pipeline.build_main_preview_payload`) and returns an
explicit `SENT` or a fail-closed non-success. It never reinterprets,
regenerates or paraphrases the payload's editorial content, media, request/
manifest identity, or button callback values.

This module is a pure application-layer port: the `ReviewDelivery`
protocol, a shared payload-shape validator usable by both preview schemas,
and injectable fakes for tests. It contains no subprocess call, no
`openclaw` invocation, and no owner-target file access -- the real
Telegram/OpenClaw transport lives only in
`nullone_telegram_review_delivery_adapter.py` (infrastructure adapter),
per the NullOne Application Runtime layering
(`docs/architecture/nullone-application-runtime.md`).

`ReviewDelivery.send()` is deliberately named `send`, not `deliver`, so
that any conforming implementation is structurally (not just nominally)
interchangeable with the existing narrow `TelegramPreviewSender` protocols
already defined locally in `nullone_story_pipeline.py` and
`nullone_main_draft_pipeline.py` -- no change to either #33/#36 core file
is required for them to accept it.
"""
from __future__ import annotations

from typing import Any, Protocol

STORY_PREVIEW_SCHEMA = "nullone.story-preview.v1"
MAIN_PREVIEW_SCHEMA = "nullone.main-preview.v1"

SUPPORTED_PREVIEW_SCHEMAS = frozenset({STORY_PREVIEW_SCHEMA, MAIN_PREVIEW_SCHEMA})

REQUIRED_BRAND = "NullOne"

# Legacy internal callback namespace -- preserved unchanged. Never rename;
# see docs/architecture/nullone-application-runtime.md and
# agents/approval/AGENTS.md, which key off these exact prefixes.
ALLOWED_CALLBACK_PREFIXES = ("texbrif:approve:", "texbrif:reject:", "texbrif:revise:")

SUCCESS_STATUS = "SENT"


class ReviewDeliveryError(RuntimeError):
    """The preview payload does not satisfy the shared ReviewDelivery shape."""


class ReviewDelivery(Protocol):
    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Attempt delivery of one already-built preview payload.

        Must return a mapping whose `status` is exactly `"SENT"` only when
        the underlying transport proves delivery; any other status (or a
        malformed/non-mapping return) is treated by the caller (#33's
        `run_story_pipeline` / #36's `run_main_pipeline`) as non-success.
        Must never raise to signal a normal transport failure -- return a
        non-SENT mapping instead; raising is reserved for a payload that
        fails `validate_preview_payload` before any transport attempt.
        """


def validate_preview_payload(payload: Any) -> dict[str, Any]:
    """Shared shape validation for either supported preview schema.

    Deliberately narrow: it checks only the invariants a ReviewDelivery
    transport itself must rely on (schema, public brand wording, a non-
    empty review_post_id, non-empty outbound text, and a well-formed
    buttons block whose callback `value`s use the unchanged legacy
    `texbrif:` namespace) -- it does not re-validate editorial content,
    media, or manifest/request identity, which remain the producing
    pipeline's exclusive responsibility.
    """

    if not isinstance(payload, dict):
        raise ReviewDeliveryError("preview payload must be an object")

    schema = payload.get("schema")
    if schema not in SUPPORTED_PREVIEW_SCHEMAS:
        raise ReviewDeliveryError(f"unsupported preview schema: {schema!r}")

    if payload.get("brand") != REQUIRED_BRAND:
        raise ReviewDeliveryError(
            f"preview payload must use public brand {REQUIRED_BRAND!r}, "
            f"got {payload.get('brand')!r}"
        )

    review_post_id = payload.get("review_post_id")
    if not isinstance(review_post_id, str) or not review_post_id.strip():
        raise ReviewDeliveryError("preview payload missing non-empty review_post_id")

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ReviewDeliveryError("preview payload missing non-empty text")

    presentation = payload.get("presentation")
    if not isinstance(presentation, dict):
        raise ReviewDeliveryError("preview payload missing presentation")

    blocks = presentation.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ReviewDeliveryError("preview payload presentation has no blocks")

    buttons = None
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "buttons":
            buttons = block.get("buttons")
            break

    if not isinstance(buttons, list) or not buttons:
        raise ReviewDeliveryError("preview payload has no buttons block")

    for button in buttons:
        if not isinstance(button, dict):
            raise ReviewDeliveryError("preview payload button is not an object")
        label = button.get("label")
        value = button.get("value")
        if not isinstance(label, str) or not label.strip():
            raise ReviewDeliveryError("preview payload button missing label")
        if not isinstance(value, str) or not value.startswith(ALLOWED_CALLBACK_PREFIXES):
            raise ReviewDeliveryError(
                "preview payload button value must use the legacy texbrif: "
                f"callback namespace, got {value!r}"
            )
        if review_post_id not in value:
            raise ReviewDeliveryError(
                "preview payload button value does not carry this review_post_id"
            )

    return payload


class FakeReviewDelivery:
    """Test double: deterministic fixed response, no transport.

    Records every payload it was asked to send (for assertion) and, when
    `validate_payload=True` (the default), still runs the shared shape
    validator first -- exactly like a real adapter would -- so tests
    exercise the same failure surface a production adapter enforces.
    """

    def __init__(
        self,
        *,
        status: str = SUCCESS_STATUS,
        error: str | None = None,
        validate_payload: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.validate_payload = validate_payload
        self.extra = dict(extra or {})
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.validate_payload:
            validate_preview_payload(payload)
        self.sent.append(payload)
        result: dict[str, Any] = {"status": self.status, **self.extra}
        if self.error is not None:
            result["error"] = self.error
        return result


def make_fake_review_delivery(status: str, error: str | None = None) -> FakeReviewDelivery:
    """Test helper mirroring `nullone_story_pipeline.make_fake_verifier`'s shape."""

    return FakeReviewDelivery(status=status, error=error)


def self_test() -> int:
    story_payload = {
        "schema": STORY_PREVIEW_SCHEMA,
        "brand": "NullOne",
        "review_post_id": "review-1",
        "text": "preview text",
        "presentation": {
            "blocks": [
                {
                    "type": "buttons",
                    "buttons": [
                        {"label": "Approve", "value": "texbrif:approve:review-1"},
                        {"label": "Reject", "value": "texbrif:reject:review-1"},
                        {"label": "Revise", "value": "texbrif:revise:review-1"},
                    ],
                }
            ]
        },
    }
    main_payload = dict(story_payload, schema=MAIN_PREVIEW_SCHEMA)

    validate_preview_payload(story_payload)
    validate_preview_payload(main_payload)

    sender = FakeReviewDelivery(status="SENT")
    assert sender.send(story_payload) == {"status": "SENT"}
    assert sender.send(main_payload) == {"status": "SENT"}
    assert len(sender.sent) == 2

    bad = dict(story_payload, brand="Texbrif")
    try:
        validate_preview_payload(bad)
        raise AssertionError("non-NullOne brand was not rejected")
    except ReviewDeliveryError:
        pass

    bad_callback = {
        **story_payload,
        "presentation": {
            "blocks": [
                {
                    "type": "buttons",
                    "buttons": [{"label": "Approve", "value": "custom:approve:review-1"}],
                }
            ]
        },
    }
    try:
        validate_preview_payload(bad_callback)
        raise AssertionError("non-texbrif callback namespace was not rejected")
    except ReviewDeliveryError:
        pass

    print("REVIEW_DELIVERY_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_TRANSPORT=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne shared ReviewDelivery port")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
