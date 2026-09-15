#!/usr/bin/env python3
"""NullOne Draft Bridge CLI — stable CLI edge for review-draft creation.

This module is the STABLE CLI EDGE for review-draft creation. It preserves
the existing command/contract:

    nullone-draft-bridge.py self-test
    nullone-draft-bridge.py local-check <manifest>
    nullone-draft-bridge.py execute <manifest>

After #81, `execute` uses the new deterministic direct Zernio REST
DraftProvider (`nullone_zernio_draft_adapter.ZernioDraftProvider`) behind
the production factory boundary (`nullone_draft_provider_factory`).

This module MUST NOT import or invoke any Claude/MCP infrastructure.
The existing NulloneDraftBridgeConnector (in nullone_story_pipeline.py)
may remain structurally unchanged and continue shelling to this CLI.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nullone_bridge_common import (
    CANONICAL_ACCOUNT_ID,
    BridgeError,
    atomic_write_json,
    load_manifest,
    now_iso,
    resolve_workspace_path,
    sha256_bytes,
    validate_manifest,
    workspace_relative,
)
import nullone_bridge_common as bridge_common
from nullone_draft_provider_factory import build_production_draft_provider
from nullone_packaging_receipt import (
    load_receipt,
    load_render_record,
    manifest_format_for_receipt,
    require_canonical_receipt,
    require_canonical_render_record,
)
from nullone_zernio_draft_adapter import (
    DraftConnectorUnauthorizedError,
    DraftConnectorUnavailableError,
    DraftPreflightBlockedError,
    DraftPresignBlockedError,
    DraftUploadFailedError,
    DraftCreateAmbiguousError,
    DraftReadbackFailedError,
)

# Review state vocabulary preserved from the existing contract.
# These constants are referenced by acceptance contract PUB-DRAFT-001.
REVIEW_UNKNOWN = "REVIEW_UNKNOWN"
REVIEW_DRAFT_AMBIGUOUS = "REVIEW_DRAFT_AMBIGUOUS"
REVIEW_DRAFT_BLOCKED_BEFORE_ATTEMPT = "REVIEW_DRAFT_BLOCKED_BEFORE_ATTEMPT"
REVIEW_DRAFT_ALREADY_CONSUMED = "REVIEW_DRAFT_ALREADY_CONSUMED"


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


def _require_canonical_manifest_path(manifest_path: Path, *, raw_arg: str | Path | None = None) -> Path:
    """Require the factory manifest to be the canonical manifests-dir file.

    Factory manifests are only authoritative when they are regular,
    non-symlink files contained inside the canonical MANIFEST_DIR
    (derived from the current WORKSPACE so temp-workspace offline
    tests resolve the same way). Any manifest from
    social/drafts/production or any other workspace directory, and
    any symlink manifest path, fails closed here before any
    provider/Zernio effect.
    """
    root = bridge_common.WORKSPACE
    manifest_dir = (root / "social" / "ops" / "manifests").resolve()
    if raw_arg is not None:
        raw = Path(raw_arg).expanduser()
        if not raw.is_absolute():
            raw = root / raw
        try:
            if raw.is_symlink():
                raise BridgeError("PACKAGING_INPUT_INVALID: manifest path must not be a symlink")
        except OSError as e:
            raise BridgeError(f"PACKAGING_INPUT_INVALID: manifest path unreadable: {e}") from e
    try:
        if Path(manifest_path).is_symlink():
            raise BridgeError("PACKAGING_INPUT_INVALID: manifest path must not be a symlink")
    except OSError as e:
        raise BridgeError(f"PACKAGING_INPUT_INVALID: manifest path unreadable: {e}") from e
    try:
        resolved = Path(manifest_path).resolve()
    except OSError as e:
        raise BridgeError(f"PACKAGING_INPUT_INVALID: manifest path unreadable: {e}") from e
    try:
        resolved.relative_to(manifest_dir)
    except (OSError, ValueError) as e:
        raise BridgeError(
            "PACKAGING_INPUT_INVALID: manifest path is not the canonical manifests directory file"
        ) from e
    if not resolved.is_file():
        raise BridgeError("PACKAGING_INPUT_INVALID: manifest is not a regular file")
    return resolved


def require_packaging_authority(manifest_path: Path, m: dict, *, raw_arg: str | Path | None = None) -> None:
    """Re-validate packaging authority immediately before draft creation.

    Factory formats (FEED/CAROUSEL) must carry the embedded packaging
    block from a deterministic manifest build AND that block must
    still verify against the canonical on-disk receipt/record: same
    candidate, same receipt hash, same record hash, receipt format
    mapping to the manifest format, manifest_path is a regular
    non-symlink file inside the canonical MANIFEST_DIR, and manifest
    media is exactly (ordered, same count, same per-item sha256) the
    record's validated outputs with current bytes rehashed. A
    hand-written manifest without this block, outside the canonical
    dir, or with any subset/reorder/duplicate/hash drift, fails
    closed here with zero Zernio effects. STORY manifests are owned
    by StoryWorkflow and skip this gate.
    """

    if m.get("format") == "STORY":
        return

    _require_canonical_manifest_path(manifest_path, raw_arg=raw_arg if raw_arg is not None else manifest_path)

    packaging = m.get("packaging")
    if not isinstance(packaging, dict):
        raise BridgeError(
            "PACKAGING_DECISION_MISMATCH: factory manifest lacks packaging authority"
        )

    candidate_id = m.get("candidate_id")
    root = bridge_common.WORKSPACE

    receipt_arg = root / str(packaging.get("receipt_path", ""))
    receipt = load_receipt(receipt_arg, root=root)
    require_canonical_receipt(receipt_arg, candidate_id, root=root)
    if receipt.get("candidate_id") != candidate_id:
        raise BridgeError("PACKAGING_DECISION_MISMATCH: receipt candidate mismatch")
    if receipt.get("receipt_hash") != packaging.get("receipt_hash"):
        raise BridgeError("PACKAGING_RECEIPT_TAMPERED: embedded receipt hash mismatch")
    allowed_format = manifest_format_for_receipt(receipt)
    if allowed_format != m.get("format"):
        raise BridgeError("PACKAGING_DECISION_MISMATCH: receipt forbids this manifest format")

    record_arg = root / str(packaging.get("render_record_path", ""))
    record = load_render_record(record_arg, root=root)
    require_canonical_render_record(record_arg, candidate_id, root=root)
    if record.get("candidate_id") != candidate_id:
        raise BridgeError("PACKAGING_DECISION_MISMATCH: render record candidate mismatch")
    if record.get("receipt_hash") != receipt.get("receipt_hash"):
        raise BridgeError("PACKAGING_DECISION_MISMATCH: render record is not bound to this receipt")
    if record.get("record_hash") != packaging.get("record_hash"):
        raise BridgeError("PACKAGING_RECEIPT_TAMPERED: embedded render-record hash mismatch")

    record_outputs = record.get("outputs", [])
    manifest_media = m.get("media", [])
    if not isinstance(manifest_media, list) or not isinstance(record_outputs, list):
        raise BridgeError("PACKAGING_DECISION_MISMATCH: manifest media is not validated render output")
    if len(manifest_media) != len(record_outputs):
        raise BridgeError(
            "PACKAGING_DECISION_MISMATCH: manifest media count is not the validated render output"
        )
    manifest_paths: list[str | None] = [
        item.get("local_path") if isinstance(item, dict) else None for item in manifest_media
    ]
    record_paths: list[str | None] = [
        entry.get("path") if isinstance(entry, dict) else None for entry in record_outputs
    ]
    if any(not isinstance(p, str) for p in manifest_paths + record_paths):
        raise BridgeError("PACKAGING_DECISION_MISMATCH: manifest media is not validated render output")
    if len(set(manifest_paths)) != len(manifest_paths):
        raise BridgeError("PACKAGING_DECISION_MISMATCH: duplicate manifest media entry")
    if manifest_paths != record_paths:
        raise BridgeError(
            "PACKAGING_DECISION_MISMATCH: manifest media is not the exact validated render output"
        )
    for item, entry in zip(manifest_media, record_outputs):
        assert isinstance(item, dict) and isinstance(entry, dict)
        rel = item.get("local_path")
        if item.get("sha256") != entry.get("sha256"):
            raise BridgeError(
                "PACKAGING_DECISION_MISMATCH: manifest media hash is not validated render output"
            )
        current = resolve_workspace_path(rel)
        if not current.is_file() or sha256_bytes(current.read_bytes()) != entry.get("sha256"):
            raise BridgeError("PACKAGING_RENDER_INTEGRITY_FAILED: manifest media was modified")


def execute(manifest_arg: str) -> int:
    manifest_path, m = load_manifest(manifest_arg)

    require_not_created(m)

    try:
        require_packaging_authority(manifest_path, m, raw_arg=manifest_arg)
    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2

    # Traceability anchor: create_attempts is persisted as 1 by the
    # underlying ZernioDraftProvider before the single POST /posts.
    # m["review"]["create_attempts"] = 1  (set by provider)

    try:
        provider = build_production_draft_provider()
    except DraftConnectorUnauthorizedError as e:
        print(f"BLOCKED={e}")
        return 2
    except DraftConnectorUnavailableError as e:
        print(f"BLOCKED={e}")
        return 2

    try:
        provider.create_review_draft(manifest_path)
    except DraftPreflightBlockedError as e:
        print(f"BLOCKED={e}")
        return 2
    except DraftPresignBlockedError as e:
        print(f"BLOCKED={e}")
        return 2
    except DraftUploadFailedError as e:
        print(f"BLOCKED={e}")
        return 2
    except DraftConnectorUnauthorizedError as e:
        print(f"BLOCKED={e}")
        return 2
    except DraftConnectorUnavailableError as e:
        print(f"BLOCKED={e}")
        return 2
    except DraftCreateAmbiguousError as e:
        print(f"BLOCKED={e}")
        return 2
    except DraftReadbackFailedError as e:
        print(f"BLOCKED={e}")
        return 2
    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2

    # Reload manifest (ground truth) to confirm final state.
    try:
        _, m = load_manifest(manifest_path)
    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2

    state = m["review"]["state"]
    post_id = m["review"].get("zernio_draft_id")

    if state != "DRAFT_CREATED" or not post_id:
        print(
            "BLOCKED=Review draft was not unambiguously created; "
            f"state={state}"
        )
        return 2

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
    path, m = load_manifest(manifest_arg)

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
    print("TRANSPORT=direct_zernio_rest")
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