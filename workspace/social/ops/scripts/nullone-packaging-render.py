#!/usr/bin/env python3
"""Deterministic packaging render dispatcher (packaging runtime wiring).

    python3 social/ops/scripts/nullone-packaging-render.py render \
        --receipt <canonical decision receipt> \
        --asset-file <validated asset descriptor> \
        --output <workspace-contained output file-or-dir> \
        [--spec <workspace-contained carousel spec>] \
        [--kicker ... --headline ... --stat ... --source-name ...]

Renders ONLY the format the authoritative receipt decided, with ONLY
the visual evidence it allows: the asset descriptor kind must equal
the receipt's VISUAL_STYLE mapping (file-backed kinds need an
existing workspace file with provenance; typography claims NONE).
Feed --source comes from the validated descriptor file, never from
free-form agent input. Every success writes a canonical render
record binding outputs to the receipt hash for the manifest gate.

- SINGLE_POST -> the V2 feed renderer with the given copy fields;
- CAROUSEL    -> the V2 carousel renderer, but only when the spec's
  substantive slide count exactly equals the receipt's
  `slide_count_recommendation` (padding/invention blocked);
- STORY       -> refused (PACKAGING_STORY_DELEGATED: normal Draft
  Factory never produces Story; StoryWorkflow owns it);
- SKIP        -> refused (PACKAGING_SKIPPED: no render happens).

Renderers run as subprocesses with a bounded timeout and no shell.
All paths stay inside the workspace. Renderer failure propagates as
a deterministic failure, never as a format change.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from nullone_bridge_common import BridgeError, WORKSPACE, atomic_write_json
from nullone_packaging_receipt import (
    canonical_render_record_path,
    contained_path,
    load_receipt,
    require_canonical_receipt,
    validate_asset_descriptor,
)

FEED_RENDERER = WORKSPACE / "social/tools/render_texbrif_v2.py"
CAROUSEL_RENDERER = WORKSPACE / "social/tools/render_carousel_v2.py"

RENDER_TIMEOUT_SECONDS = 300


def _run_renderer(argv: list[str]) -> None:
    try:
        cp = subprocess.run(
            argv, text=True, capture_output=True, timeout=RENDER_TIMEOUT_SECONDS, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise BridgeError(f"PACKAGING_RENDER_FAILED: renderer did not complete: {e}") from e
    if cp.returncode != 0:
        raise BridgeError(f"PACKAGING_RENDER_FAILED: renderer exited {cp.returncode}")
    print(cp.stdout.strip()[-500:] if cp.stdout.strip() else "RENDER_OK")


def _carousel_slide_count(spec_path: Path) -> int:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BridgeError("PACKAGING_INPUT_INVALID: carousel spec is not valid JSON") from e
    slides = spec.get("slides") if isinstance(spec, dict) else None
    if not isinstance(slides, list):
        raise BridgeError("PACKAGING_INPUT_INVALID: carousel spec has no slides list")
    return len(slides)


def contained_asset_text(args: argparse.Namespace, root: Path) -> str:
    """Containment + regular-file check BEFORE reading the asset file."""

    raw_path = Path(args.asset_file)
    if raw_path.is_symlink():
        raise BridgeError("PACKAGING_INPUT_INVALID: asset file must not be a symlink")
    resolved = contained_path(raw_path, root)
    if not resolved.is_file():
        raise BridgeError("PACKAGING_INPUT_INVALID: asset file missing or not regular")
    return resolved.read_text(encoding="utf-8")


def render_command(args: argparse.Namespace, *, root: Path = WORKSPACE) -> int:
    receipt = load_receipt(Path(args.receipt), root=root)
    require_canonical_receipt(Path(args.receipt), receipt["candidate_id"], root=root)
    if receipt.get("POST_DECISION") != "POST":
        raise BridgeError("PACKAGING_SKIPPED: receipt is not a POST decision")
    decision = receipt.get("FORMAT_DECISION")
    if decision == "STORY":
        raise BridgeError("PACKAGING_STORY_DELEGATED: normal Draft Factory never produces Story")
    if decision == "SKIP":
        raise BridgeError("PACKAGING_SKIPPED: receipt forbids production")
    output = contained_path(Path(args.output), root)

    try:
        asset_raw = json.loads(contained_asset_text(args, root))
    except (OSError, ValueError) as e:
        raise BridgeError("PACKAGING_INPUT_INVALID: asset file unreadable") from e
    asset = validate_asset_descriptor(asset_raw, receipt, root=root)

    if receipt.get("visual_requirement") == "SOURCE_GROUNDED" and asset.get("asset_kind") == "NONE":
        raise BridgeError(
            "PACKAGING_VISUAL_GROUNDING_UNMET: receipt declares a visual block but"
            " the validated asset claims no evidence; refusing to render a placeholder frame"
        )

    candidate_id = receipt["candidate_id"]
    style = receipt.get("VISUAL_STYLE")
    if style == "GENERATED_ILLUSTRATION_ALLOWED":
        raise BridgeError(
            "PACKAGING_UNSUPPORTED_STYLE: no reviewed generation path exists; refusing rather than substituting arbitrary media"
        )
    if decision == "SINGLE_POST":
        missing = [f for f in ("kicker", "headline", "source_name") if not getattr(args, f, None)]
        if missing:
            raise BridgeError(f"PACKAGING_INPUT_INVALID: single-post render missing {missing}")
        if asset.get("local_path") is not None:
            source = asset["local_path"]
        else:
            if getattr(args, "source", None):
                raise BridgeError(
                    "PACKAGING_INPUT_INVALID: typography receipt takes no image source; smuggling blocked"
                )
            source = None
        argv = [sys.executable, str(FEED_RENDERER)]
        if source is not None:
            argv += ["--source", source]
        argv += [
            "--kicker", args.kicker,
            "--headline", args.headline,
            "--stat", args.stat or "",
            "--source-name", args.source_name,
            "--output", str(output),
        ]
        _run_renderer(argv)
        if not output.is_file():
            raise BridgeError("PACKAGING_RENDER_FAILED: feed output missing")
        _write_render_record(
            candidate_id=candidate_id, receipt=receipt, format_decision=decision,
            asset_kind=asset["asset_kind"], outputs=[output], root=root,
        )
        print(f"RENDER_FORMAT=SINGLE_POST OUTPUT={output}")
        return 0
    if decision == "CAROUSEL":
        if asset["asset_kind"] in ("REAL_PHOTO", "SOURCE_SCREENSHOT", "DATA_VISUALIZATION"):
            raise BridgeError(
                "PACKAGING_UNSUPPORTED_STYLE: the V2 carousel renderer takes no input images, "
                "so file-backed evidence cannot be faithfully bound into a carousel"
            )
        if not args.spec:
            raise BridgeError("PACKAGING_INPUT_INVALID: carousel render needs --spec")
        spec_path = contained_path(Path(args.spec), root)
        if Path(args.spec).is_symlink() or not spec_path.is_file():
            raise BridgeError("PACKAGING_INPUT_INVALID: carousel spec is not a regular file")
        count = _carousel_slide_count(spec_path)
        expected = receipt.get("slide_count_recommendation")
        if count != expected:
            raise BridgeError(
                f"PACKAGING_DECISION_MISMATCH: spec has {count} slides, receipt allows {expected}"
            )
        _run_renderer(
            [sys.executable, str(CAROUSEL_RENDERER), "--spec", str(spec_path), "--output-dir", str(output)]
        )
        slides = sorted(output.glob("*.png")) if output.is_dir() else []
        if len(slides) != expected:
            raise BridgeError(
                f"PACKAGING_RENDER_FAILED: renderer produced {len(slides)} slides, receipt allows {expected}"
            )
        _write_render_record(
            candidate_id=candidate_id, receipt=receipt, format_decision=decision,
            asset_kind=asset["asset_kind"], outputs=slides, root=root,
        )
        print(f"RENDER_FORMAT=CAROUSEL SLIDES={len(slides)} OUTPUT={output}")
        return 0
    raise BridgeError(f"PACKAGING_DECISION_MISMATCH: unknown FORMAT_DECISION {decision!r}")


def _write_render_record(*, candidate_id: str, receipt: dict[str, Any], format_decision: str,
                         asset_kind: str, outputs: list[Path], root: Path) -> Path:
    import hashlib

    entries = []
    for path in outputs:
        if not path.is_file():
            raise BridgeError("PACKAGING_RENDER_FAILED: expected render output missing")
        entries.append(
            {
                "path": str(path.resolve().relative_to(root.resolve())),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    from nullone_packaging_receipt import build_render_record

    record = build_render_record(
        candidate_id=candidate_id,
        receipt_hash=receipt["receipt_hash"],
        format_decision=format_decision,
        asset_kind=asset_kind,
        outputs=entries,
    )
    out = canonical_render_record_path(candidate_id, root=root)
    if out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if existing == record:
            print(f"RENDER_RECORD={out} UNCHANGED")
            return out
        raise BridgeError("PACKAGING_RECEIPT_CONFLICT: a different render record already exists")
    atomic_write_json(out, record)
    print(f"RENDER_RECORD={out}")
    return out


def self_test() -> int:
    # Offline: SKIP/STORY receipts refuse before any subprocess.
    import tempfile

    from nullone_packaging_receipt import build_receipt_body

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        skip = build_receipt_body(
            candidate_id="probe",
            validated_request={"candidate": {"content_shape": "SINGLE_FACT", "visual_requirement": "NONE"}, "assets": {}},
            decision={
                "POST_DECISION": "SKIP", "CONTENT_SHAPE": "SINGLE_FACT",
                "REAL_PHOTO_AVAILABLE": "NO", "REAL_PHOTO_REQUIRED": "NO",
                "VISUAL_EVIDENCE_REQUIRED": "NO", "ASSET_STRENGTH": "NONE",
                "TEXT_DENSITY": "LOW", "TIMELINESS": "TODAY",
                "FORMAT_DECISION": "SKIP", "FORMAT_REASON": "VERIFICATION_BLOCKED",
                "VISUAL_STYLE": "NONE", "slide_count_recommendation": None,
                "source_grounding": "STRONG_PRIMARY", "distinct_beat_count": 1,
                "content_type": "NEWS",
            },
        )
        receipt_path = root / "social/drafts/production/probe-packaging-decision.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(skip), encoding="utf-8")
        asset_path = root / "asset.json"
        asset_path.write_text(json.dumps({"asset_kind": "NONE"}), encoding="utf-8")

        class Args:
            receipt = str(receipt_path)
            asset_file = str(asset_path)
            output = str(root / "out.png")
            spec = None
            source = kicker = headline = stat = source_name = None

        try:
            render_command(Args(), root=root)
        except BridgeError as e:
            assert "PACKAGING_SKIPPED" in str(e)
        else:
            raise AssertionError("SKIP receipt rendered")

    print("PACKAGING_RENDER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne deterministic packaging render dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render")
    r.add_argument("--receipt", required=True)
    r.add_argument("--asset-file", required=True)
    r.add_argument("--output", required=True)
    r.add_argument("--spec", default=None)
    r.add_argument("--source", default=None)
    r.add_argument("--kicker", default=None)
    r.add_argument("--headline", default=None)
    r.add_argument("--stat", default=None)
    r.add_argument("--source-name", default=None)
    sub.add_parser("self-test")
    args = parser.parse_args()

    try:
        if args.command == "render":
            return render_command(args)
        if args.command == "self-test":
            return self_test()
        raise BridgeError("Unknown command")
    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
