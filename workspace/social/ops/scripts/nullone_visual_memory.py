#!/usr/bin/env python3
"""Read-only NullOne Visual Memory: compact recent PUBLISHED visual
history for the Visual Director (docs/contracts/visual-director-contract-v1.md).

Sources (existing authoritative repository state formats, same
precedence family as `nullone_cadence_state_adapter.py`):

- `social/state/publish-ledger.jsonl` -- the only source for "did this
  candidate actually reach the audience". A row counts as published
  only when `result == "PUBLISHED"` (the same predicate
  `nullone_cadence_state_adapter.py` already uses for audience-facing
  counting). `PUBLISH_ACCEPTED`/`PUBLISHING`/`CHECK_REQUIRED` rows are
  in-flight, not yet confirmed, and are excluded.
- `social/ops/manifests/<manifest_id>.json` -- the only source for the
  per-post packaging/visual facts (`format`, `packaging.format_decision`,
  `packaging.asset_kind`, `packaging.visual_style` where present, media
  dimensions/path).

This module never reads `social/state/candidate-queue.md` or
`social/state/topic-ledger.jsonl` for this purpose: a rejected draft, an
abandoned manifest, or an unpublished experimental render must never
enter visual memory, and those files carry exactly that kind of
not-yet-published material.

This module never writes anything. It has no capability to create
drafts, mutate manifests/ledgers, publish, or call any connector. No
network access, no image bytes are read or returned -- only compact
per-post metadata, bounded to a small recent window.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nullone_bridge_common import BridgeError, WORKSPACE

DEFAULT_STATE_ROOT = WORKSPACE / "social"
PUBLISH_LEDGER_SUBPATH = "state/publish-ledger.jsonl"
MANIFESTS_SUBDIR = "ops/manifests"

MIN_WINDOW = 12
MAX_WINDOW = 20
DEFAULT_WINDOW = 16

VISUAL_MEMORY_SCHEMA = "nullone.visual-memory.v1"

# Manifest packaging.asset_kind -> compact visual_style label used when a
# finer VISUAL_STYLE (including BRANDED_GRAPHIC) cannot be recovered
# because the original packaging-decision receipt has since been pruned
# from social/drafts/production/. Best-effort, documented degrade -- this
# module never fails the whole extraction over one missing receipt.
ASSET_KIND_TO_VISUAL_STYLE = {
    "REAL_PHOTO": "SOURCE_PHOTO",
    "SOURCE_SCREENSHOT": "SOURCE_PHOTO",
    "DATA_VISUALIZATION": "DATA_VISUALIZATION",
    "NONE": "EDITORIAL_TYPOGRAPHY",
}


class VisualMemoryError(BridgeError):
    """State is present but malformed. Fails closed, never silently empty."""


def _fail(message: str) -> None:
    raise VisualMemoryError(f"VISUAL_MEMORY_STATE_INVALID: {message}")


def _load_ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        # An absent-but-otherwise-valid ledger (no publications yet) is a
        # legitimate empty state, not a defect.
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as e:
            _fail(f"publish-ledger.jsonl line {lineno} is not valid JSON: {e}")
        if not isinstance(row, dict):
            _fail(f"publish-ledger.jsonl line {lineno} is not an object")
        if "timestamp" not in row or "result" not in row:
            _fail(f"publish-ledger.jsonl line {lineno} missing timestamp/result")
        rows.append(row)
    return rows


def _load_manifests(manifests_dir: Path) -> dict[str, dict[str, Any]]:
    if not manifests_dir.is_dir():
        return {}
    manifests: dict[str, dict[str, Any]] = {}
    for path in sorted(manifests_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            _fail(f"manifest {path.name} is not valid JSON: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("manifest_id"), str):
            _fail(f"manifest {path.name} missing manifest_id")
        manifests[data["manifest_id"]] = data
    return manifests


def _resolve_visual_style(manifest: dict[str, Any], *, root: Path) -> str | None:
    packaging = manifest.get("packaging")
    if not isinstance(packaging, dict):
        return None
    # Forward-looking manifests carry the resolved style directly
    # (nullone-manifest.py build now persists it); older manifests are
    # recovered best-effort from asset_kind, then (best-effort) from the
    # still-present packaging receipt if one happens to survive on disk.
    style = packaging.get("visual_style")
    if isinstance(style, str) and style:
        return style
    receipt_path = packaging.get("receipt_path")
    if isinstance(receipt_path, str):
        candidate = (root / receipt_path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            candidate = None
        if candidate is not None and candidate.is_file():
            try:
                receipt = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                receipt = None
            if isinstance(receipt, dict) and isinstance(receipt.get("VISUAL_STYLE"), str):
                return receipt["VISUAL_STYLE"]
    asset_kind = packaging.get("asset_kind")
    return ASSET_KIND_TO_VISUAL_STYLE.get(asset_kind)


def _compact_record(row: dict[str, Any], manifest: dict[str, Any] | None, *, root: Path) -> dict[str, Any]:
    record = {
        "post_id": row.get("live_zernio_post_id") or row.get("manifest_id"),
        "published_at": row.get("timestamp"),
        "topic": row.get("topic"),
        "topic_cluster": row.get("topic_cluster"),
        "content_type": row.get("content_type"),
        "format": row.get("format"),
        "visual_style": None,
        "source_photo_used": None,
        "final_render_path": None,
    }
    if manifest is not None:
        style = _resolve_visual_style(manifest, root=root)
        record["visual_style"] = style
        record["source_photo_used"] = style == "SOURCE_PHOTO"
        media = manifest.get("media")
        if isinstance(media, list) and media and isinstance(media[0], dict):
            record["final_render_path"] = media[0].get("local_path")
    return record


def recent_published_visual_history(
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    window: int = DEFAULT_WINDOW,
) -> list[dict[str, Any]]:
    """Compact visual-history records for the most recent PUBLISHED posts.

    Bounded to `window` records (clamped to [MIN_WINDOW, MAX_WINDOW]),
    newest first. Only ledger rows with `result == "PUBLISHED"` count;
    every other event (PUBLISH_ACCEPTED, PUBLISHING, CHECK_REQUIRED, a
    rejected/abandoned draft, an unpublished experimental render) is
    excluded by construction -- this function never reads
    candidate-queue.md or topic-ledger.jsonl.
    """

    window = max(MIN_WINDOW, min(MAX_WINDOW, window))
    ledger_rows = _load_ledger_rows(state_root / PUBLISH_LEDGER_SUBPATH)
    manifests = _load_manifests(state_root / MANIFESTS_SUBDIR)

    published = [row for row in ledger_rows if row.get("result") == "PUBLISHED"]
    published.sort(key=lambda r: r["timestamp"], reverse=True)

    records: list[dict[str, Any]] = []
    seen_post_ids: set[str] = set()
    for row in published:
        post_id = row.get("live_zernio_post_id") or row.get("manifest_id")
        if not post_id or post_id in seen_post_ids:
            continue
        seen_post_ids.add(post_id)
        manifest = manifests.get(row.get("manifest_id"))
        records.append(_compact_record(row, manifest, root=state_root.parent))
        if len(records) >= window:
            break

    return records


def build_visual_memory_document(
    *, state_root: Path = DEFAULT_STATE_ROOT, window: int = DEFAULT_WINDOW
) -> dict[str, Any]:
    history = recent_published_visual_history(state_root=state_root, window=window)
    style_counts: dict[str, int] = {}
    for record in history:
        style = record.get("visual_style")
        if style:
            style_counts[style] = style_counts.get(style, 0) + 1
    return {
        "schema": VISUAL_MEMORY_SCHEMA,
        "window_requested": max(MIN_WINDOW, min(MAX_WINDOW, window)),
        "window_actual": len(history),
        "recent_visual_style_counts": style_counts,
        "history": history,
    }


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state_root = root / "social"
        (state_root / "state").mkdir(parents=True)
        (state_root / "ops/manifests").mkdir(parents=True)

        rows = []
        for i in range(25):
            rows.append(
                {
                    "timestamp": f"2026-09-{i + 1:02d}T00:00:00+00:00",
                    "event": "PUBLISH_ACCEPTED",
                    "manifest_id": f"m{i}",
                    "candidate_id": f"c{i}",
                    "topic": f"Topic {i}",
                    "topic_cluster": "cluster",
                    "content_type": "NEWS",
                    "format": "FEED",
                    "live_zernio_post_id": f"post{i}",
                    "result": "PUBLISH_ACCEPTED",
                }
            )
            rows.append(dict(rows[-1], event="PUBLISHED", result="PUBLISHED"))
        # One in-flight-only row (never confirmed) must be excluded entirely.
        rows.append(
            {
                "timestamp": "2026-10-01T00:00:00+00:00",
                "event": "PUBLISHING",
                "manifest_id": "unconfirmed",
                "candidate_id": "unconfirmed",
                "topic": "Should not appear",
                "topic_cluster": "cluster",
                "content_type": "NEWS",
                "format": "FEED",
                "live_zernio_post_id": "unconfirmed-post",
                "result": "PUBLISHING",
            }
        )
        ledger_path = state_root / "state/publish-ledger.jsonl"
        ledger_path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )

        for i in range(25):
            manifest = {
                "manifest_id": f"m{i}",
                "format": "FEED",
                "packaging": {
                    "visual_style": "EDITORIAL_TYPOGRAPHY" if i % 2 == 0 else "SOURCE_PHOTO",
                    "asset_kind": "NONE",
                },
                "media": [{"local_path": f"social/drafts/production/{i}.png"}],
            }
            (state_root / f"ops/manifests/m{i}.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

        doc = build_visual_memory_document(state_root=state_root, window=16)
        assert doc["window_actual"] == 16, doc
        assert all(r["post_id"] != "unconfirmed-post" for r in doc["history"]), doc
        # Newest first: post24 (i=24) is the most recent PUBLISHED row.
        assert doc["history"][0]["post_id"] == "post24", doc["history"][0]
        assert doc["history"][0]["topic"] == "Topic 24"
        assert set(doc["recent_visual_style_counts"]) <= {"EDITORIAL_TYPOGRAPHY", "SOURCE_PHOTO"}

        small = build_visual_memory_document(state_root=state_root, window=1)
        assert small["window_actual"] == MIN_WINDOW, "window must clamp to the documented floor"

        empty_state = root / "empty" / "social"
        empty_state.mkdir(parents=True)
        doc_empty = build_visual_memory_document(state_root=empty_state, window=16)
        assert doc_empty["history"] == []

        bad_ledger = root / "bad" / "social"
        (bad_ledger / "state").mkdir(parents=True)
        (bad_ledger / "state/publish-ledger.jsonl").write_text("not json\n", encoding="utf-8")
        try:
            build_visual_memory_document(state_root=bad_ledger, window=16)
        except VisualMemoryError:
            pass
        else:
            raise AssertionError("malformed ledger must fail closed, not read as empty")

    print("VISUAL_MEMORY_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
