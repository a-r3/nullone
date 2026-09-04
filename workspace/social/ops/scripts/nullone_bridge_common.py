#!/usr/bin/env python3

from __future__ import annotations
import re

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


SCHEMA = "nullone.production.v1"
CANONICAL_ACCOUNT_ID = "6a982bbf77555aae01c28f21"

WORKSPACE = Path(__file__).resolve().parents[3]
MANIFEST_DIR = WORKSPACE / "social" / "ops" / "manifests"

ALLOWED_CONTENT_TYPES = {
    "NEWS",
    "BREAKING",
    "EXPLAINER",
    "PRACTICAL",
    "COMPARISON",
    "AZ_CONTEXT",
    "EVERGREEN",
}

ALLOWED_FORMATS = {
    "FEED",
    "CAROUSEL",
    "STORY",
}


class BridgeError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_workspace_path(value: str | Path) -> Path:
    path = Path(value).expanduser()

    if not path.is_absolute():
        path = WORKSPACE / path

    return path.resolve()


def workspace_relative(path: Path) -> str:
    path = path.resolve()

    try:
        return str(path.relative_to(WORKSPACE))
    except ValueError as e:
        raise BridgeError(
            f"Path must be inside workspace: {path}"
        ) from e


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )

    tmp = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
                sort_keys=False,
            )
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def expected_dimensions(fmt: str) -> tuple[int, int]:
    if fmt in {"FEED", "CAROUSEL"}:
        return 1080, 1350

    if fmt == "STORY":
        return 1080, 1920

    raise BridgeError(f"Unsupported format: {fmt}")


def validate_media_count(fmt: str, count: int) -> None:
    if fmt == "FEED" and count != 1:
        raise BridgeError("FEED requires exactly 1 media item")

    if fmt == "STORY" and count != 1:
        raise BridgeError("STORY requires exactly 1 media item")

    if fmt == "CAROUSEL" and not (2 <= count <= 10):
        raise BridgeError("CAROUSEL requires 2-10 media items")


def inspect_media(path: Path, fmt: str) -> dict[str, Any]:
    if not path.is_file():
        raise BridgeError(f"Media not found: {path}")

    expected_w, expected_h = expected_dimensions(fmt)

    try:
        with Image.open(path) as im:
            width, height = im.size
            image_format = (im.format or "").upper()

    except Exception as e:
        raise BridgeError(
            f"Could not inspect image: {path}: {e}"
        ) from e

    if (width, height) != (expected_w, expected_h):
        raise BridgeError(
            f"Wrong dimensions for {path}: "
            f"{width}x{height}, expected {expected_w}x{expected_h}"
        )

    suffix = path.suffix.lower()

    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix)

    if not content_type:
        raise BridgeError(
            f"Unsupported media extension: {path.suffix}"
        )

    return {
        "local_path": workspace_relative(path),
        "sha256": sha256_file(path),
        "content_type": content_type,
        "width": width,
        "height": height,
        "image_format": image_format,
        "public_url": None,
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != SCHEMA:
        raise BridgeError("Invalid manifest schema")

    if manifest.get("account_id") != CANONICAL_ACCOUNT_ID:
        raise BridgeError("Canonical account ID mismatch")

    if manifest.get("verification") != "PASS":
        raise BridgeError("Manifest verification is not PASS")

    fmt = manifest.get("format")

    if fmt not in ALLOWED_FORMATS:
        raise BridgeError(f"Invalid format: {fmt}")

    content_type = manifest.get("content_type")

    if content_type not in ALLOWED_CONTENT_TYPES:
        raise BridgeError(
            f"Invalid content_type: {content_type}"
        )

    topic_cluster = manifest.get("topic_cluster")

    if (
        not isinstance(topic_cluster, str)
        or not topic_cluster.strip()
    ):
        raise BridgeError("Manifest topic_cluster missing")

    caption_path = resolve_workspace_path(
        manifest["caption"]["file"]
    )

    # Public-brand safety guard.
    # Historical already-published manifests remain valid audit artifacts.
    publication_attempts = manifest.get("publication", {}).get("attempts", 0)

    if publication_attempts == 0:
        caption_text = caption_path.read_text(encoding="utf-8")

        if re.search(r"(?i)(?<![A-Za-z0-9_])#?texbrif(?![A-Za-z0-9_])", caption_text):
            raise BridgeError(
                "Public caption contains forbidden legacy public brand: Texbrif"
            )


    if not caption_path.is_file():
        raise BridgeError(
            f"Caption file missing: {caption_path}"
        )

    caption_bytes = caption_path.read_bytes()

    if sha256_bytes(caption_bytes) != manifest["caption"]["sha256"]:
        raise BridgeError(
            "Caption hash mismatch: approved content changed"
        )

    media = manifest.get("media") or []

    validate_media_count(fmt, len(media))

    expected_w, expected_h = expected_dimensions(fmt)

    for item in media:
        path = resolve_workspace_path(item["local_path"])

        if not path.is_file():
            raise BridgeError(
                f"Media file missing: {path}"
            )

        if sha256_file(path) != item["sha256"]:
            raise BridgeError(
                f"Media hash mismatch: {path}"
            )

        try:
            with Image.open(path) as im:
                actual = im.size
        except Exception as e:
            raise BridgeError(
                f"Cannot inspect media: {path}"
            ) from e

        if actual != (expected_w, expected_h):
            raise BridgeError(
                f"Media dimensions changed: "
                f"{path} -> {actual[0]}x{actual[1]}"
            )

        if (
            item.get("width") != expected_w
            or item.get("height") != expected_h
        ):
            raise BridgeError(
                f"Manifest dimension metadata mismatch: {path}"
            )

    review = manifest.get("review") or {}
    publication = manifest.get("publication") or {}

    review_attempts = review.get("create_attempts", 0)
    publication_attempts = publication.get("attempts", 0)

    if (
        not isinstance(review_attempts, int)
        or review_attempts < 0
        or review_attempts > 1
    ):
        raise BridgeError("Invalid review.create_attempts")

    if (
        not isinstance(publication_attempts, int)
        or publication_attempts < 0
        or publication_attempts > 1
    ):
        raise BridgeError("Invalid publication.attempts")


def load_manifest(path: str | Path) -> tuple[Path, dict[str, Any]]:
    p = resolve_workspace_path(path)

    if not p.is_file():
        raise BridgeError(f"Manifest missing: {p}")

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise BridgeError(
            f"Invalid manifest JSON: {p}: {e}"
        ) from e

    validate_manifest(data)

    return p, data


def find_manifest_by_review_post_id(
    review_post_id: str,
) -> tuple[Path, dict[str, Any]]:
    matches: list[tuple[Path, dict[str, Any]]] = []

    for path in MANIFEST_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if (
            data.get("review", {}).get("zernio_draft_id")
            == review_post_id
        ):
            matches.append((path, data))

    if not matches:
        raise BridgeError(
            f"No manifest for review post ID: {review_post_id}"
        )

    if len(matches) > 1:
        raise BridgeError(
            f"Multiple manifests for review post ID: {review_post_id}"
        )

    path, data = matches[0]
    validate_manifest(data)

    return path, data
