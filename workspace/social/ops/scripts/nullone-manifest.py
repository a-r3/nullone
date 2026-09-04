#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

from PIL import Image

from nullone_bridge_common import (
    ALLOWED_CONTENT_TYPES,
    ALLOWED_FORMATS,
    CANONICAL_ACCOUNT_ID,
    MANIFEST_DIR,
    SCHEMA,
    BridgeError,
    atomic_write_json,
    inspect_media,
    load_manifest,
    now_iso,
    resolve_workspace_path,
    sha256_bytes,
    validate_manifest,
    validate_media_count,
    workspace_relative,
)


def slug(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value[:48] or "candidate"


def build(args: argparse.Namespace) -> int:
    caption_path = resolve_workspace_path(args.caption_file)

    if not caption_path.is_file():
        raise BridgeError(
            f"Caption file missing: {caption_path}"
        )

    caption_bytes = caption_path.read_bytes()

    if not caption_bytes.strip():
        raise BridgeError("Caption file is empty")

    media_paths = [
        resolve_workspace_path(p)
        for p in args.media
    ]

    validate_media_count(args.format, len(media_paths))

    media = [
        inspect_media(path, args.format)
        for path in media_paths
    ]

    created = now_iso()

    manifest_id = (
        args.manifest_id
        or f"{created[:10]}-{slug(args.candidate_id)}"
    )

    out = (
        resolve_workspace_path(args.output)
        if args.output
        else MANIFEST_DIR / f"{manifest_id}.json"
    )

    if out.exists() and not args.force:
        raise BridgeError(
            f"Manifest already exists: {out}"
        )

    manifest = {
        "schema": SCHEMA,
        "manifest_id": manifest_id,
        "created_at": created,

        "candidate_id": args.candidate_id,
        "topic": args.topic,
        "topic_cluster": args.topic_cluster,
        "content_type": args.content_type,
        "format": args.format,

        "verification": "PASS",
        "account_id": CANONICAL_ACCOUNT_ID,

        "caption": {
            "file": workspace_relative(caption_path),
            "sha256": sha256_bytes(caption_bytes),
        },

        "media": media,

        "review": {
            "create_attempts": 0,
            "state": "NOT_CREATED",
            "zernio_draft_id": None,
            "created_at": None,
        },

        "approval": {
            "first_stage": False,
            "first_stage_at": None,
            "final_publish": False,
            "final_publish_at": None,
            "source": None,
            "operator": None,
            "human_confirmation": None,
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

    validate_manifest(manifest)
    atomic_write_json(out, manifest)

    print(f"MANIFEST_CREATED={workspace_relative(out)}")
    print(f"MANIFEST_ID={manifest_id}")
    print("VERIFICATION=PASS")
    print(f"FORMAT={args.format}")
    print(f"MEDIA_COUNT={len(media)}")

    return 0


def validate_cmd(args: argparse.Namespace) -> int:
    path, data = load_manifest(args.manifest)

    print("MANIFEST_VALID=PASS")
    print(f"PATH={workspace_relative(path)}")
    print(f"MANIFEST_ID={data['manifest_id']}")
    print(f"REVIEW_STATE={data['review']['state']}")
    print(
        f"PUBLICATION_STATE="
        f"{data['publication']['state']}"
    )

    return 0


def self_test() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        image_path = root / "test.png"
        caption_path = root / "caption.txt"

        Image.new(
            "RGB",
            (1080, 1350),
            (14, 14, 15),
        ).save(image_path)

        caption_path.write_text(
            "NullOne bridge self-test.",
            encoding="utf-8",
        )

        media = inspect_media_external_for_test(
            image_path,
            "FEED",
        )

        manifest = {
            "schema": SCHEMA,
            "manifest_id": "self-test",
            "created_at": now_iso(),
            "candidate_id": "self-test",
            "topic": "Self test",
            "topic_cluster": "self-test",
            "content_type": "EVERGREEN",
            "format": "FEED",
            "verification": "PASS",
            "account_id": CANONICAL_ACCOUNT_ID,
            "caption": {
                "file": "__self_test__",
                "sha256": sha256_bytes(
                    caption_path.read_bytes()
                ),
            },
            "media": [media],
            "review": {
                "create_attempts": 0,
                "state": "NOT_CREATED",
                "zernio_draft_id": None,
                "created_at": None,
            },
            "approval": {
                "first_stage": False,
                "first_stage_at": None,
                "final_publish": False,
                "final_publish_at": None,
                "source": None,
                "operator": None,
                "human_confirmation": None,
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

        # Structural assertions only; production validator intentionally
        # requires workspace-local files.
        assert manifest["schema"] == SCHEMA
        assert manifest["account_id"] == CANONICAL_ACCOUNT_ID
        assert manifest["verification"] == "PASS"
        assert manifest["media"][0]["width"] == 1080
        assert manifest["media"][0]["height"] == 1350
        assert manifest["publication"]["attempts"] == 0

    print("SELF_TEST=PASS")
    return 0


def inspect_media_external_for_test(
    path: Path,
    fmt: str,
) -> dict:
    with Image.open(path) as im:
        width, height = im.size

    expected = (1080, 1350)

    if fmt != "FEED" or (width, height) != expected:
        raise BridgeError("Self-test media mismatch")

    from nullone_bridge_common import sha256_file

    return {
        "local_path": "__self_test__",
        "sha256": sha256_file(path),
        "content_type": "image/png",
        "width": width,
        "height": height,
        "image_format": "PNG",
        "public_url": None,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="NullOne Production Manifest V1"
    )

    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build")

    b.add_argument(
        "--candidate-id",
        required=True,
    )

    b.add_argument(
        "--topic",
        required=True,
    )

    b.add_argument(
        "--topic-cluster",
        required=True,
    )

    b.add_argument(
        "--content-type",
        choices=sorted(ALLOWED_CONTENT_TYPES),
        required=True,
    )

    b.add_argument(
        "--format",
        choices=sorted(ALLOWED_FORMATS),
        required=True,
    )

    b.add_argument(
        "--caption-file",
        required=True,
    )

    b.add_argument(
        "--media",
        action="append",
        required=True,
        help="Repeat for carousel slides in exact order",
    )

    b.add_argument("--manifest-id")
    b.add_argument("--output")
    b.add_argument("--force", action="store_true")

    v = sub.add_parser("validate")
    v.add_argument("manifest")

    sub.add_parser("self-test")

    return p


def main() -> int:
    args = parser().parse_args()

    try:
        if args.command == "build":
            return build(args)

        if args.command == "validate":
            return validate_cmd(args)

        if args.command == "self-test":
            return self_test()

        raise BridgeError("Unknown command")

    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
