#!/usr/bin/env python3
"""NullOne Visual Memory CLI (docs/contracts/visual-director-contract-v1.md).

    python3 social/ops/scripts/nullone-visual-memory.py history \
        --candidate-id <CANDIDATE_ID> [--window 16]

Runs the pure, read-only `nullone_visual_memory.recent_published_visual_
history` (see that module for the exact source precedence and the
PUBLISHED-only, published-only-through-a-manifest-link filtering rules)
and writes the compact result to the ONE canonical path derived from the
candidate id:

    social/drafts/production/<candidate-id>-visual-memory.json

This is Visual Director context only, never an authority receipt: unlike
the packaging/visual-decision receipts, it carries no hash and is freely
re-derivable at any time (it is a read-only summary of already-durable
state, not a decision anything downstream is bound to). Re-running
overwrites it with the current state.
"""
from __future__ import annotations

import argparse
import json

from nullone_bridge_common import BridgeError, WORKSPACE, atomic_write_json
from nullone_packaging_receipt import check_candidate_id
from nullone_visual_memory import DEFAULT_WINDOW, build_visual_memory_document


def canonical_visual_memory_path(candidate_id: str):
    check_candidate_id(candidate_id)
    return WORKSPACE / "social/drafts/production" / f"{candidate_id}-visual-memory.json"


def history_command(args: argparse.Namespace) -> int:
    candidate_id = check_candidate_id(args.candidate_id)
    doc = build_visual_memory_document(window=args.window)
    out = canonical_visual_memory_path(candidate_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out, doc)
    print(f"VISUAL_MEMORY_PATH={out}")
    print(f"WINDOW_ACTUAL={doc['window_actual']}")
    return 0


def self_test() -> int:
    import tempfile
    from pathlib import Path

    from nullone_visual_memory import build_visual_memory_document as _build

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "social/drafts/production").mkdir(parents=True)
        (root / "social/state").mkdir(parents=True)
        doc = _build(state_root=root / "social", window=16)
        out = root / "social/drafts/production/probe-visual-memory.json"
        out.write_text(json.dumps(doc), encoding="utf-8")
        assert out.is_file()

    print("VISUAL_MEMORY_CLI_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne Visual Memory CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("history")
    h.add_argument("--candidate-id", required=True)
    h.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    sub.add_parser("self-test")
    args = parser.parse_args()

    try:
        if args.command == "history":
            return history_command(args)
        if args.command == "self-test":
            return self_test()
        raise BridgeError("Unknown command")
    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
