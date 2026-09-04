#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json

from nullone_bridge_common import (
    CANONICAL_ACCOUNT_ID,
    BridgeError,
    atomic_write_json,
    load_manifest,
    now_iso,
    resolve_workspace_path,
    workspace_relative,
)
from nullone_claude import run_structured
from nullone_state import (
    mark_queue_published_exact,
    record_publication_event,
)


PREFLIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["READY", "BLOCKED"],
        },
        "review_draft_ok": {"type": "boolean"},
        "account_ok": {"type": "boolean"},
        "media_ok": {"type": "boolean"},
        "error": {"type": "string"},
    },
    "required": [
        "status",
        "review_draft_ok",
        "account_ok",
        "media_ok",
        "error",
    ],
    "additionalProperties": False,
}


PUBLISH_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "PUBLISH_CALLED",
                "BLOCKED",
            ],
        },
        "live_zernio_post_id": {
            "type": "string"
        },
        "returned_status": {
            "type": "string"
        },
        "error": {"type": "string"},
    },
    "required": [
        "status",
        "live_zernio_post_id",
        "returned_status",
        "error",
    ],
    "additionalProperties": False,
}


READBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "CHECKED",
                "BLOCKED",
            ],
        },
        "live_status": {"type": "string"},
        "platform_status": {"type": "string"},
        "platform_post_id": {"type": "string"},
        "permalink": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": [
        "status",
        "live_status",
        "platform_status",
        "platform_post_id",
        "permalink",
        "error",
    ],
    "additionalProperties": False,
}


def require_final_authorization(m: dict) -> None:
    review = m["review"]
    approval = m["approval"]
    pub = m["publication"]

    if review["state"] != "DRAFT_CREATED":
        raise BridgeError(
            "Review draft is not in DRAFT_CREATED state"
        )

    if not review.get("zernio_draft_id"):
        raise BridgeError(
            "Review draft ID missing"
        )

    if approval.get("first_stage") is not True:
        raise BridgeError(
            "First-stage human approval missing"
        )

    if approval.get("final_publish") is not True:
        raise BridgeError(
            "Final human publish authorization missing"
        )

    if approval.get("source") != "texbrif-approval":
        raise BridgeError(
            "Authorization source mismatch"
        )

    if approval.get("operator") != "Rauf":
        raise BridgeError(
            "Authorization operator mismatch"
        )

    if (
        approval.get("human_confirmation")
        != "two_step"
    ):
        raise BridgeError(
            "Two-step authorization proof missing"
        )

    if pub.get("attempts") != 0:
        raise BridgeError(
            "Publication attempt already consumed"
        )

    if pub.get("state") != "NOT_REQUESTED":
        raise BridgeError(
            f"Publication state is {pub.get('state')}"
        )

    for item in m["media"]:
        if not item.get("public_url"):
            raise BridgeError(
                "Approved media missing public URL"
            )


def preflight_prompt(m: dict) -> str:
    review_id = m["review"][
        "zernio_draft_id"
    ]

    media_urls = [
        x["public_url"]
        for x in m["media"]
    ]

    return f"""
NULLONE PUBLISH BRIDGE — READ-ONLY FINAL PREFLIGHT.

Do not create, update, schedule, delete or publish anything.

Canonical Instagram account ID:
{CANONICAL_ACCOUNT_ID}

Review draft ID:
{review_id}

Verify:
1. canonical account exists;
2. review draft exists and status is draft;
3. review draft targets Instagram and canonical account ID;
4. every approved public media URL remains valid.

Approved media URLs:
{json.dumps(media_urls, ensure_ascii=False)}

A stale cached username such as texbrif is NOT an error
when the canonical account ID matches.

Return READY only when every invariant passes.
"""


def publish_prompt(m: dict) -> str:
    caption_path = resolve_workspace_path(
        m["caption"]["file"]
    )

    caption = caption_path.read_text(
        encoding="utf-8"
    )

    media_urls = [
        x["public_url"]
        for x in m["media"]
    ]

    story_instruction = (
        'Publish as Instagram Story contentType="story".'
        if m["format"] == "STORY"
        else
        "Publish as normal Instagram feed/carousel content."
    )

    return f"""
NULLONE PUBLISH BRIDGE — FINAL HUMAN-AUTHORIZED PUBLICATION.

This invocation is reached only after the local manifest validator
confirmed:

source = texbrif-approval
operator = Rauf
human_confirmation = two_step
first_stage = true
final_publish = true
publication.attempts = 0
verification = PASS

The second Telegram human publication confirmation is therefore present.

Publish the exact approved content NOW.

Use ONLY:
mcp__zernio__posts_publish_now

Call posts_publish_now MAXIMUM ONE TIME.

Canonical account ID:
{CANONICAL_ACCOUNT_ID}

FORMAT:
{m["format"]}

{story_instruction}

EXACT APPROVED CAPTION — verbatim:
{json.dumps(caption, ensure_ascii=False)}

EXACT APPROVED MEDIA URLS — preserve order:
{json.dumps(media_urls, ensure_ascii=False)}

Do not:
- rewrite caption
- replace media
- schedule
- create a draft
- retry
- call posts_publish_now twice

Return the new live Zernio post ID from that single call.

If the result is ambiguous, return the information you actually received.
Never make a second publish call.
"""


def readback_prompt(
    live_post_id: str,
) -> str:
    return f"""
NULLONE PUBLISH BRIDGE — READ-ONLY RESULT CHECK.

Check this exact Zernio post ID once:

{live_post_id}

Use posts_get only.

Do not create, update, schedule, delete or publish anything.

Return:
- exact Zernio status
- Instagram platform status
- platform post ID when available
- exact permalink when available
- actual platform error if present
"""


def execute(manifest_arg: str) -> int:
    manifest_path, m = load_manifest(
        manifest_arg
    )

    require_final_authorization(m)

    preflight = run_structured(
        prompt=preflight_prompt(m),
        allowed_tools=[
            "mcp__zernio__accounts_list",
            "mcp__zernio__posts_get",
            "mcp__zernio__validate_media",
        ],
        schema=PREFLIGHT_SCHEMA,
        max_turns=8,
    )

    if (
        preflight.get("status") != "READY"
        or not preflight.get("review_draft_ok")
        or not preflight.get("account_ok")
        or not preflight.get("media_ok")
    ):
        raise BridgeError(
            "Final read-only publication preflight failed"
        )

    # Critical idempotency boundary.
    # From here on publication MUST NEVER be automatically retried.
    m["publication"]["attempts"] = 1
    m["publication"]["state"] = "PUBLISH_IN_FLIGHT"
    m["publication"]["last_checked_at"] = now_iso()

    atomic_write_json(
        manifest_path,
        m,
    )

    try:
        result = run_structured(
            prompt=publish_prompt(m),
            allowed_tools=[
                "mcp__zernio__posts_publish_now",
            ],
            schema=PUBLISH_SCHEMA,
            max_turns=4,
        )

    except Exception:
        m["publication"]["state"] = "UNKNOWN"
        m["publication"]["error"] = (
            "Publish invocation ended ambiguously"
        )

        atomic_write_json(
            manifest_path,
            m,
        )
        raise

    if result.get("status") != "PUBLISH_CALLED":
        m["publication"]["state"] = "UNKNOWN"
        m["publication"]["error"] = (
            "Publish result was not unambiguously accepted"
        )

        atomic_write_json(
            manifest_path,
            m,
        )

        raise BridgeError(
            "Publication result ambiguous; retry forbidden"
        )

    live_id = result.get(
        "live_zernio_post_id",
        "",
    ).strip()

    if not live_id:
        m["publication"]["state"] = "UNKNOWN"
        m["publication"]["error"] = (
            "Publish call returned no live post ID"
        )

        atomic_write_json(
            manifest_path,
            m,
        )

        raise BridgeError(
            "Live post ID missing; retry forbidden"
        )

    m["publication"][
        "live_zernio_post_id"
    ] = live_id

    m["publication"]["state"] = "PUBLISH_ACCEPTED"

    atomic_write_json(
        manifest_path,
        m,
    )

    # The consequential live call has been accepted.
    # Write authoritative state BEFORE readback so even a later
    # readback failure cannot allow a duplicate publication.
    record_publication_event(
        m,
        "PUBLISH_ACCEPTED",
    )

    # Read-only check after the one and only live write.
    try:
        checked = run_structured(
            prompt=readback_prompt(live_id),
            allowed_tools=[
                "mcp__zernio__posts_get",
            ],
            schema=READBACK_SCHEMA,
            max_turns=4,
        )

    except Exception:
        m["publication"]["state"] = "READBACK_FAILED"
        m["publication"]["error"] = (
            "Publish accepted but readback failed; "
            "do not retry publication"
        )

        atomic_write_json(
            manifest_path,
            m,
        )
        raise

    if checked.get("status") != "CHECKED":
        m["publication"]["state"] = "READBACK_FAILED"
        m["publication"]["error"] = (
            "Post readback was not conclusive; "
            "do not retry publication"
        )

        atomic_write_json(
            manifest_path,
            m,
        )

        raise BridgeError(
            "Readback inconclusive; publication retry forbidden"
        )

    live_status = checked.get(
        "live_status",
        "",
    ).lower()

    platform_status = checked.get(
        "platform_status",
        "",
    ).lower()

    if (
        live_status == "published"
        and platform_status == "published"
    ):
        final_state = "PUBLISHED"

    elif live_status in {
        "publishing",
        "processing",
        "queued",
    }:
        final_state = "PUBLISHING"

    elif live_status in {
        "failed",
        "error",
    }:
        final_state = "FAILED"

    else:
        final_state = "CHECK_REQUIRED"

    m["publication"]["state"] = final_state
    m["publication"]["platform_post_id"] = (
        checked.get("platform_post_id") or None
    )
    m["publication"]["permalink"] = (
        checked.get("permalink") or None
    )
    m["publication"]["last_checked_at"] = now_iso()
    m["publication"]["error"] = (
        checked.get("error") or None
    )

    atomic_write_json(
        manifest_path,
        m,
    )

    record_publication_event(
        m,
        final_state,
    )

    if final_state == "PUBLISHED":
        mark_queue_published_exact(
            topic=m["topic"],
            review_post_id=m["review"]["zernio_draft_id"],
            live_post_id=m["publication"]["live_zernio_post_id"],
            platform_post_id=m["publication"]["platform_post_id"],
            permalink=m["publication"]["permalink"],
        )

    print("PUBLISH_BRIDGE=COMPLETE")
    print(
        f"MANIFEST={workspace_relative(manifest_path)}"
    )
    print(
        f"LIVE_ZERNIO_POST_ID={live_id}"
    )
    print(
        f"PUBLICATION_STATE={final_state}"
    )

    if m["publication"]["platform_post_id"]:
        print(
            "PLATFORM_POST_ID="
            + m["publication"]["platform_post_id"]
        )

    if m["publication"]["permalink"]:
        print(
            "PERMALINK="
            + m["publication"]["permalink"]
        )

    return 0


def self_test() -> int:
    good = {
        "review": {
            "state": "DRAFT_CREATED",
            "zernio_draft_id": "test-review",
        },
        "approval": {
            "first_stage": True,
            "final_publish": True,
            "source": "texbrif-approval",
            "operator": "Rauf",
            "human_confirmation": "two_step",
        },
        "publication": {
            "attempts": 0,
            "state": "NOT_REQUESTED",
        },
        "media": [
            {
                "public_url":
                "https://example.invalid/media.png"
            }
        ],
    }

    require_final_authorization(good)

    bad = json.loads(json.dumps(good))
    bad["publication"]["attempts"] = 1

    try:
        require_final_authorization(bad)
    except BridgeError:
        pass
    else:
        raise BridgeError(
            "Idempotency self-test failed"
        )

    bad = json.loads(json.dumps(good))
    bad["approval"]["final_publish"] = False

    try:
        require_final_authorization(bad)
    except BridgeError:
        pass
    else:
        raise BridgeError(
            "Human authorization self-test failed"
        )

    print("PUBLISH_BRIDGE_SELF_TEST=PASS")
    print("IDEMPOTENCY_GUARD=PASS")
    print("TWO_STEP_AUTH_GUARD=PASS")
    print("EXTERNAL_CALLS=0")

    return 0


def local_check(manifest_arg: str) -> int:
    path, m = load_manifest(
        manifest_arg
    )

    require_final_authorization(m)

    print("PUBLISH_LOCAL_CHECK=PASS")
    print(
        f"MANIFEST={workspace_relative(path)}"
    )
    print(
        "REVIEW_POST_ID="
        + m["review"]["zernio_draft_id"]
    )
    print("FINAL_AUTHORIZATION=PASS")
    print("PUBLICATION_ATTEMPTS=0")

    return 0


def main() -> int:
    p = argparse.ArgumentParser()

    sub = p.add_subparsers(
        dest="command",
        required=True,
    )

    sub.add_parser("self-test")

    c = sub.add_parser("local-check")
    c.add_argument("manifest")

    e = sub.add_parser("execute")
    e.add_argument("manifest")

    args = p.parse_args()

    try:
        if args.command == "self-test":
            return self_test()

        if args.command == "local-check":
            return local_check(
                args.manifest
            )

        if args.command == "execute":
            return execute(
                args.manifest
            )

        raise BridgeError("Unknown command")

    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
