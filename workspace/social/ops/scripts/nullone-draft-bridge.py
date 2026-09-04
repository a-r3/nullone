#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nullone_bridge_common import (
    CANONICAL_ACCOUNT_ID,
    BridgeError,
    atomic_write_json,
    load_manifest,
    now_iso,
    resolve_workspace_path,
    validate_manifest,
    workspace_relative,
)
from nullone_claude import run_structured


PRESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["OK", "BLOCKED"],
        },
        "upload_url": {"type": "string"},
        "public_url": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": [
        "status",
        "upload_url",
        "public_url",
        "error",
    ],
    "additionalProperties": False,
}


VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["READY", "BLOCKED"],
        },
        "account_ok": {"type": "boolean"},
        "media_ok": {"type": "boolean"},
        "post_ok": {"type": "boolean"},
        "error": {"type": "string"},
    },
    "required": [
        "status",
        "account_ok",
        "media_ok",
        "post_ok",
        "error",
    ],
    "additionalProperties": False,
}


CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["DRAFT_CREATED", "BLOCKED"],
        },
        "review_post_id": {"type": "string"},
        "review_status": {"type": "string"},
        "account_id": {"type": "string"},
        "platform": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": [
        "status",
        "review_post_id",
        "review_status",
        "account_id",
        "platform",
        "error",
    ],
    "additionalProperties": False,
}


def require_not_created(m: dict) -> None:
    review = m["review"]

    if review["create_attempts"] != 0:
        raise BridgeError(
            "Review draft create attempt already consumed"
        )

    if review["state"] != "NOT_CREATED":
        raise BridgeError(
            f"Review state is {review['state']}, "
            "expected NOT_CREATED"
        )

    if review["zernio_draft_id"]:
        raise BridgeError(
            "Manifest already has review draft ID"
        )


def presign_one(item: dict) -> tuple[str, str]:
    local_path = resolve_workspace_path(
        item["local_path"]
    )

    prompt = f"""
NULLONE DRAFT BRIDGE — PRESIGN ONLY.

This is a deterministic transport operation.

Use Zernio call_tool exactly ONCE.
The only permitted dynamic target is:

media_get_media_presigned_url

Arguments:
filename = {json.dumps(local_path.name)}
content_type = {json.dumps(item["content_type"])}
size = {local_path.stat().st_size}

Do not create, update, schedule, delete or publish any post.
Do not invoke any other dynamic target.

Return the temporary upload URL and public URL exactly.
If the presign cannot be obtained, return BLOCKED.
"""

    result = run_structured(
        prompt=prompt,
        allowed_tools=[
            "mcp__zernio__call_tool",
        ],
        schema=PRESIGN_SCHEMA,
        max_turns=4,
    )

    if result.get("status") != "OK":
        raise BridgeError(
            "Zernio media presign was blocked"
        )

    upload_url = result.get("upload_url", "")
    public_url = result.get("public_url", "")

    if not upload_url.startswith("http"):
        raise BridgeError(
            "Presign result missing upload URL"
        )

    if not public_url.startswith("http"):
        raise BridgeError(
            "Presign result missing public URL"
        )

    return upload_url, public_url


def upload_one(
    *,
    upload_url: str,
    item: dict,
) -> None:
    path = resolve_workspace_path(
        item["local_path"]
    )

    data = path.read_bytes()

    request = Request(
        upload_url,
        data=data,
        method="PUT",
        headers={
            "Content-Type": item["content_type"],
            "Content-Length": str(len(data)),
        },
    )

    try:
        with urlopen(
            request,
            timeout=120,
        ) as response:
            status = getattr(response, "status", None)

    except HTTPError as e:
        raise BridgeError(
            f"Media upload HTTP error: {e.code}"
        ) from e

    except URLError as e:
        raise BridgeError(
            "Media upload network error"
        ) from e

    if status not in (200, 201, 204):
        raise BridgeError(
            f"Unexpected upload HTTP status: {status}"
        )


def ensure_public_media(
    manifest_path,
    m: dict,
) -> None:

    changed = False

    for item in m["media"]:
        if item.get("public_url"):
            continue

        upload_url, public_url = presign_one(item)

        # upload_url stays memory-only.
        upload_one(
            upload_url=upload_url,
            item=item,
        )

        item["public_url"] = public_url
        changed = True

        # Persist only public URL, never signed upload URL.
        atomic_write_json(
            manifest_path,
            m,
        )

    if changed:
        validate_manifest(m)


def validation_prompt(m: dict) -> str:
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

    return f"""
NULLONE DRAFT BRIDGE — READ-ONLY PREFLIGHT.

Do not create, update, schedule, delete or publish anything.

Canonical Instagram account ID:
{CANONICAL_ACCOUNT_ID}

Verify through Zernio:

1. The canonical account exists.
2. Every media URL below passes media validation.
3. This exact post payload passes post validation.

FORMAT:
{m["format"]}

CONTENT TYPE:
{m["content_type"]}

EXACT CAPTION:
{json.dumps(caption, ensure_ascii=False)}

MEDIA URLS:
{json.dumps(media_urls, ensure_ascii=False)}

For STORY, validate it as Instagram Story content.
For CAROUSEL, preserve media URL order exactly.

Return READY only if account, media and complete post payload
all validate successfully.
"""


def create_prompt(m: dict) -> str:
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
        'platformSpecificData.contentType must be "story".'
        if m["format"] == "STORY"
        else
        "Do not set Story content type."
    )

    return f"""
NULLONE DRAFT BRIDGE — CREATE REVIEW DRAFT.

The exact content was already independently validated.

Create exactly ONE Zernio review draft.

Account ID:
{CANONICAL_ACCOUNT_ID}

Platform:
instagram

FORMAT:
{m["format"]}

{story_instruction}

EXACT CAPTION — use verbatim:
{json.dumps(caption, ensure_ascii=False)}

MEDIA URLS — preserve exact order:
{json.dumps(media_urls, ensure_ascii=False)}

Requirements:
- is_draft = true
- do NOT publish
- do NOT publish_now
- do NOT schedule
- no scheduled_for
- do not rewrite caption
- do not replace media
- call posts_create maximum ONE time

After successful creation:
call posts_get exactly once for the returned post ID.

Require:
- status = draft
- Instagram platform
- canonical account ID

Return DRAFT_CREATED only when readback confirms the draft.
"""


def execute(manifest_arg: str) -> int:
    manifest_path, m = load_manifest(
        manifest_arg
    )

    require_not_created(m)

    ensure_public_media(
        manifest_path,
        m,
    )

    # Read-only validation happens BEFORE consuming create attempt.
    validation = run_structured(
        prompt=validation_prompt(m),
        allowed_tools=[
            "mcp__zernio__accounts_list",
            "mcp__zernio__validate_media",
            "mcp__zernio__validate_post",
        ],
        schema=VALIDATION_SCHEMA,
        max_turns=8,
    )

    if (
        validation.get("status") != "READY"
        or not validation.get("account_ok")
        or not validation.get("media_ok")
        or not validation.get("post_ok")
    ):
        raise BridgeError(
            "Zernio draft preflight validation failed"
        )

    # From this point on, duplicate draft creation is forbidden.
    m["review"]["create_attempts"] = 1
    m["review"]["state"] = "CREATE_IN_FLIGHT"

    atomic_write_json(
        manifest_path,
        m,
    )

    try:
        result = run_structured(
            prompt=create_prompt(m),
            allowed_tools=[
                "mcp__zernio__posts_create",
                "mcp__zernio__posts_get",
            ],
            schema=CREATE_SCHEMA,
            max_turns=6,
        )
    except Exception:
        m["review"]["state"] = "REVIEW_UNKNOWN"
        atomic_write_json(
            manifest_path,
            m,
        )
        raise

    if result.get("status") != "DRAFT_CREATED":
        m["review"]["state"] = "REVIEW_UNKNOWN"
        atomic_write_json(
            manifest_path,
            m,
        )
        raise BridgeError(
            "Draft create result was not unambiguously successful"
        )

    post_id = result.get(
        "review_post_id",
        "",
    ).strip()

    if not post_id:
        m["review"]["state"] = "REVIEW_UNKNOWN"
        atomic_write_json(
            manifest_path,
            m,
        )
        raise BridgeError(
            "Draft result missing review post ID"
        )

    if result.get("account_id") != CANONICAL_ACCOUNT_ID:
        m["review"]["state"] = "REVIEW_UNKNOWN"
        atomic_write_json(
            manifest_path,
            m,
        )
        raise BridgeError(
            "Draft readback account ID mismatch"
        )

    if result.get("platform", "").lower() != "instagram":
        m["review"]["state"] = "REVIEW_UNKNOWN"
        atomic_write_json(
            manifest_path,
            m,
        )
        raise BridgeError(
            "Draft readback platform mismatch"
        )

    if result.get("review_status", "").lower() != "draft":
        m["review"]["state"] = "REVIEW_UNKNOWN"
        atomic_write_json(
            manifest_path,
            m,
        )
        raise BridgeError(
            "Draft readback did not confirm draft status"
        )

    m["review"]["state"] = "DRAFT_CREATED"
    m["review"]["zernio_draft_id"] = post_id
    m["review"]["created_at"] = now_iso()

    atomic_write_json(
        manifest_path,
        m,
    )

    print("DRAFT_BRIDGE=PASS")
    print(
        f"MANIFEST={workspace_relative(manifest_path)}"
    )
    print(
        f"REVIEW_POST_ID={post_id}"
    )
    print("REVIEW_STATE=DRAFT_CREATED")

    return 0


def local_check(manifest_arg: str) -> int:
    path, m = load_manifest(
        manifest_arg
    )

    print("LOCAL_CHECK=PASS")
    print(
        f"MANIFEST={workspace_relative(path)}"
    )
    print(
        f"REVIEW_STATE={m['review']['state']}"
    )
    print(
        f"CREATE_ATTEMPTS="
        f"{m['review']['create_attempts']}"
    )

    return 0


def self_test() -> int:
    # External services intentionally not called.
    print("DRAFT_BRIDGE_SELF_TEST=PASS")
    print("EXTERNAL_CALLS=0")
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
