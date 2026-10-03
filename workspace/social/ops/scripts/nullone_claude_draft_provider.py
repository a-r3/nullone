#!/usr/bin/env python3
"""Bounded Claude reasoning with fully mediated Draft Factory production (issue #194).

The model has NO shell, NO file-write, and NO transport capability
at all: it reasons (Read/WebSearch/WebFetch/Glob only) and
returns structured per-phase results. Deterministic Python owns
EVERY mutation -- all file writes go to Python-derived destinations
and all helper invocations use Python-built exact argv. The five
reviewed production helpers are the only subprocesses ever spawned:

    python3 social/ops/scripts/nullone-packaging-evaluator.py evaluate ...
    python3 social/ops/scripts/nullone-packaging-render.py render ...
    python3 social/ops/scripts/nullone-manifest.py build ...
    python3 social/ops/scripts/nullone-draft-bridge.py execute ...
    python3 social/ops/scripts/nullone_telegram_review_delivery_adapter.py deliver ...

Consequential Zernio and Telegram behavior stays behind those
deterministic helpers. The consequential publish path is
unreachable: no argv the model can influence ever names it, and no
model-controlled write can reach it.

Why full mediation (enforcement gap, proven by disposable probe on
Claude Code 2.1.284, fake /tmp workspace, no production):

- patterned Bash allows (e.g. ``Bash(echo hi *)``) act only as
  pre-approvals: unmatched commands still execute under
  ``--permission-mode dontAsk``. Adding a broad ``Bash(*)`` deny
  removes Bash entirely (deny wins over allows), so an exact
  five-command Bash allowlist is NOT expressible.
- path-scoped Edit/Write grants are NOT honored in either
  ``--allowedTools`` or settings ``permissions.allow`` (both the
  matched and unmatched attempts are denied); bare grants are
  all-or-nothing. Path-specific write/edit rules are NOT
  expressible either.
- ``Grep(...)`` and ``Glob(...)`` deny rules are NOT honored even
  in broad form, so Grep (content-bearing) is dropped from the
  toolset fail-closed; Glob is kept filename-only with secret
  values protected by the proven Read denies.

Per the reviewed migration contract this provider therefore takes
the stronger mediation path instead of a broad Bash/Write grant
plus prompt instructions. A model that cannot write or execute
cannot publish, schedule, leak secrets to new files, or reach the
publish path however it is prompted.

Structured phases (each a bounded read-only Claude round; the full
Draft Factory prompt is executed every round, never a Morning /
Weekly / Radar prompt):

    SELECT   -> ranked candidates + verification + packaging signals
                + asset descriptors. Python writes request/asset
                files, runs the evaluator per candidate in ranked
                order, classifies receipts through the deterministic
                fallback helper (SKIP records and continues, POST
                FEED/CAROUSEL accepts exactly one, POST/STORY is a
                Factory-non-producible disposition that records and
                continues -- never accepted, rendered, or handed to
                StoryWorkflow -- and system failures BLOCK).
    PRODUCE  -> caption + render text args + carousel spec. Python
                writes caption/spec files, runs the render
                dispatcher, builds the manifest, and runs the local
                bridge exactly once.
    COMPLETE -> ledger record + production
                report. After bridge proof, Python flips the selected
                queue status before assembling the Telegram preview
                payload deterministically (caption text, hashed
                render outputs, exact approval buttons), validates
                it with the existing preview validator, runs the
                delivery helper exactly once (failure becomes NOTIFY_FAILED),
                appends the ledger line, and writes
                the report.

DRAFT_FIRST, exactly-one-draft, no-publication, packaging receipt
authority, and the deterministic bridge backstop (wrapper-owned)
are unchanged. Any transport failure, malformed structured output,
helper rejection, or mediation violation raises BridgeError
fail-closed; the wrapper maps it to BLOCKED.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nullone_bridge_common import BridgeError
from nullone_claude import run_structured
from nullone_draft_candidate_queue import (
    QUEUE_PATH, flip_ready_to_drafted, load_queue,
)
from nullone_packaging_policy import (
    AUDIENCE_VALUE_VALUES,
    CONTENT_SHAPES,
    CONTENT_TYPES as PACKAGING_CONTENT_TYPES,
    SOURCE_GROUNDING_VALUES,
    TIMELINESS_VALUES,
    VERIFICATION_VALUES,
)
from nullone_packaging_receipt import (
    ASSET_DESCRIPTOR_SCHEMA,
    FILE_BACKED_ASSET_KINDS,
    STYLE_TO_ASSET_KIND,
    VISUAL_REQUIREMENT_VALUES,
)

ALLOWED_TOOLS = ["Read", "WebSearch", "WebFetch", "Glob"]

# Read-only discovery only. Bash/Edit/Write/Grep are granted
# nowhere: every mutation below is deterministic Python (see module
# docstring for the probed enforcement gaps behind this decision).
# Grep is content-bearing but its deny rules proved unenforceable,
# so it is dropped fail-closed; Glob lists filenames only and its
# values stay protected by the proven Read denies.

SECRET_DENY_NAMES = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*.key",
    "**/*.key",
    "*.pem",
    "**/*.pem",
)

PRIVATE_DENY_SUFFIX = "social/ops/private/**"

# Reviewed Draft Factory production scopes. Python writes ONLY these
# destinations (plus deterministic helper-owned outputs under the
# same staging root and the canonical manifests dir); the model
# never supplies a path.
DRAFTS_SCOPE = "social/drafts/production/*"
PUBLISHER_SCOPE = "social/publisher/*-draft.md"
LEDGER_PATH = "social/state/topic-ledger.jsonl"

# Manifest-declared content vocabulary (mirrors the manifest build
# helper's --content-type choices; the helper remains authoritative).
CONTENT_TYPES = (
    "AZ_CONTEXT",
    "BREAKING",
    "COMPARISON",
    "EVERGREEN",
    "EXPLAINER",
    "NEWS",
    "PRACTICAL",
)

# Receipt FORMAT_DECISION -> manifest --format (mirrors the
# deterministic packaging receipt mapping; STORY/SKIP never reach
# manifest build from this cycle).
FORMAT_TO_MANIFEST_FORMAT = {
    "SINGLE_POST": "FEED",
    "CAROUSEL": "CAROUSEL",
}

MAX_RANKED = 5
MAX_TURNS_PER_ROUND = 30

# Per-helper wall-clock caps (seconds), further bounded by the cycle deadline.
HELPER_TIMEOUTS = {
    "evaluate": 120,
    "render": 300,
    "manifest": 120,
    "bridge": 180,
    "deliver": 120,
}
CYCLE_SAFETY_MARGIN_SECONDS = 30
MIN_ROUND_SECONDS = 60
MIN_CONSEQUENTIAL_HELPER_SECONDS = 60

SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(api[_-]?key|bearer|token|secret|password)\s*[:=]\s*\S+"
)

SCRIPTS_DIR = Path(__file__).resolve().parent

# Authoritative packaging signal contract for the SELECT boundary.
# Single source of truth lives in nullone_packaging_policy /
# nullone_packaging_receipt; referenced here by import, never
# redefined with an independent vocabulary.
PACKAGING_CANDIDATE_FIELDS = (
    "content_type",
    "content_shape",
    "timeliness",
    "verification_status",
    "source_grounding",
    "audience_value",
    "distinct_beat_count",
    "depicts_real_world_subject",
    "still_developing",
    "visual_requirement",
)

PACKAGING_ASSET_BOOL_FIELDS = (
    "has_official_or_source_image",
    "has_usable_screenshot",
    "image_on_topic",
    "image_quality_ok",
    "data_visualization_possible",
)

# Evaluator outputs: the model assesses raw signals only and must
# never submit these (mirrors MODEL_FORBIDDEN_REQUEST_FIELDS in the
# deterministic evaluator CLI; rejected here before any write).
MODEL_FORBIDDEN_CANDIDATE_FIELDS = frozenset(
    {"FORMAT_DECISION", "FORMAT_REASON", "VISUAL_STYLE", "slide_count_recommendation"}
)

# Asset kinds a SELECT descriptor may claim (the receipt's
# STYLE_TO_ASSET_KIND values; receipt/style matching authority stays
# downstream with the deterministic render validator).
SELECT_ASSET_KINDS = frozenset(STYLE_TO_ASSET_KIND.values())

SELECT_ASSET_DESCRIPTOR_REQUIRED = (
    "schema",
    "candidate_id",
    "asset_kind",
    "local_path",
    "provenance",
)

SELECT_ASSET_DESCRIPTOR_OPTIONAL = ("sha256", "source_url")

SELECT_FIELDS = ("decision", "ranked", "notes")
SELECT_DECISIONS = ("SELECT", "NO_ACTION")

SELECT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(SELECT_FIELDS),
    "properties": {
        "decision": {"type": "string", "enum": list(SELECT_DECISIONS)},
        "ranked": {
            "type": "array",
            "maxItems": MAX_RANKED,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": (
                    "candidate_id",
                    "topic",
                    "topic_cluster",
                    "content_type",
                    "packaging_request",
                    "asset",
                ),
                "properties": {
                    "candidate_id": {"type": "string", "minLength": 1},
                    "topic": {"type": "string", "minLength": 1},
                    "topic_cluster": {"type": "string", "minLength": 1},
                    "content_type": {"type": "string", "minLength": 1},
                    "packaging_request": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ("candidate", "assets"),
                        "properties": {
                            "candidate": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": list(PACKAGING_CANDIDATE_FIELDS),
                                "properties": {
                                    "content_type": {
                                        "type": "string",
                                        "enum": sorted(PACKAGING_CONTENT_TYPES),
                                    },
                                    "content_shape": {
                                        "type": "string",
                                        "enum": sorted(CONTENT_SHAPES),
                                    },
                                    "timeliness": {
                                        "type": "string",
                                        "enum": sorted(TIMELINESS_VALUES),
                                    },
                                    "verification_status": {
                                        "type": "string",
                                        "enum": sorted(VERIFICATION_VALUES),
                                    },
                                    "source_grounding": {
                                        "type": "string",
                                        "enum": sorted(SOURCE_GROUNDING_VALUES),
                                    },
                                    "audience_value": {
                                        "type": "string",
                                        "enum": sorted(AUDIENCE_VALUE_VALUES),
                                    },
                                    "distinct_beat_count": {
                                        "type": "integer",
                                        "minimum": 0,
                                    },
                                    "depicts_real_world_subject": {"type": "boolean"},
                                    "still_developing": {"type": "boolean"},
                                    "visual_requirement": {
                                        "type": "string",
                                        "enum": sorted(VISUAL_REQUIREMENT_VALUES),
                                    },
                                },
                            },
                            "assets": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": list(PACKAGING_ASSET_BOOL_FIELDS),
                                "properties": {
                                    field: {"type": "boolean"}
                                    for field in PACKAGING_ASSET_BOOL_FIELDS
                                },
                            },
                        },
                    },
                    "asset": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(SELECT_ASSET_DESCRIPTOR_REQUIRED),
                        "properties": {
                            "schema": {
                                "type": "string",
                                "enum": [ASSET_DESCRIPTOR_SCHEMA],
                            },
                            "candidate_id": {"type": "string", "minLength": 1},
                            "asset_kind": {
                                "type": "string",
                                "enum": sorted(SELECT_ASSET_KINDS),
                            },
                            "local_path": {"type": ["string", "null"]},
                            "provenance": {"type": ["string", "null"]},
                            "sha256": {"type": ["string", "null"]},
                            "source_url": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        },
        "notes": {"type": "string"},
    },
}

PRODUCE_FIELDS = ("caption", "render_text", "slides")
PRODUCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(PRODUCE_FIELDS),
    "properties": {
        "caption": {"type": "string", "minLength": 1},
        "render_text": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kicker": {"type": ["string", "null"]},
                "headline": {"type": ["string", "null"]},
                "stat": {"type": ["string", "null"]},
                "source_name": {"type": ["string", "null"]},
            },
        },
        "slides": {"anyOf": [{"type": "null"}, {"type": "object"}]},
    },
}

COMPLETE_FIELDS = ("ledger_record", "report_markdown")
COMPLETE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(COMPLETE_FIELDS),
    "properties": {
        "ledger_record": {"type": "object"},
        "report_markdown": {"type": "string", "minLength": 1},
    },
}

BAKU_ZONE = "Asia/Baku"

SELECT_APPENDIX = """
Transport note for this phase: you have read-only tools only (no
shell, no file writes). Return ONLY the structured SELECT result:
ranked candidates strongest-first (at most %(max_ranked)d) ONLY from
this deterministic eligible ID map: %(eligible)s. Each returned item
includes candidate_id, topic, topic_cluster, content_type, the
packaging signals object, and the asset descriptor object. Assess
signals; never decide formats. Deterministic Python writes every
file and runs every helper; never supply filesystem paths or
commands.
"""

PRODUCE_APPENDIX = """
Transport note for this phase: you have read-only tools only (no
shell, no file writes). The deterministic evaluator already decided
FORMAT %(format)s for candidate %(candidate_id)s -- conform to it,
never override it. Return ONLY the structured PRODUCE result: final
caption text, render text args, and (CAROUSEL only) the slide spec
object. Deterministic Python writes every file and runs render,
manifest, and bridge; never supply filesystem paths or commands.
"""

COMPLETE_APPENDIX = """
Transport note for this phase: you have read-only tools only (no
shell, no file writes). Draft Bridge reported %(bridge)s for
candidate %(candidate_id)s (review %(review)s, Telegram %(notify)s).
Return ONLY the structured COMPLETE result: the topic-ledger record
object (same convention as the existing ledger lines you read),
and the production report markdown (no secrets, no presigned URLs).
Deterministic Python validates and
writes everything, including the exact queue status flip; never supply
queue content, filesystem paths, or commands.
"""


def draft_security_settings(workspace: Path) -> dict:
    """Secret-deny rules anchored to this invocation's workspace.

    Read is the only content-bearing local tool granted, and its
    deny set is proven enforced (disposable probe). Glob lists
    filenames only, matching the reviewed OpenCode discovery
    posture; secret VALUES stay protected because reading them is
    denied. Grep/Edit/Write/Bash are granted nowhere, so no rules
    for them are needed.
    """
    root = workspace.resolve(strict=True)
    anchor = "//" + root.as_posix().lstrip("/")
    denied = []
    for name in SECRET_DENY_NAMES:
        denied.append(f"Read({anchor}/{name})")
    denied.append(f"Read({anchor}/{PRIVATE_DENY_SUFFIX})")
    return {"permissions": {"deny": denied}}


def _validated_select(result: object) -> dict:
    """Structural validation for the SELECT round (fail closed).

    Enforces the deterministic packaging evaluator input contract
    at the structured-output boundary, BEFORE any production file
    write: packaging_request must carry exactly the authoritative
    candidate + assets objects (no model-owned FORMAT_DECISION /
    FORMAT_REASON / VISUAL_STYLE / slide_count_recommendation), the
    nested candidate content_type must equal the item's top-level
    content_type (single authority chain with the queue check), and
    the asset descriptor must be structurally valid
    (`nullone.packaging-asset.v1`). Receipt/style semantic authority
    stays downstream with the deterministic render validator.
    """
    if not isinstance(result, dict) or set(result) != set(SELECT_FIELDS):
        raise BridgeError("Malformed Draft Factory SELECT result")
    if result["decision"] not in SELECT_DECISIONS:
        raise BridgeError("Malformed Draft Factory SELECT result")
    ranked = result["ranked"]
    if not isinstance(ranked, list) or len(ranked) > MAX_RANKED:
        raise BridgeError("Malformed Draft Factory SELECT result")
    if result["decision"] == "SELECT" and not ranked:
        raise BridgeError("Malformed Draft Factory SELECT result")
    if not isinstance(result["notes"], str):
        raise BridgeError("Malformed Draft Factory SELECT result")
    for item in ranked:
        if not isinstance(item, dict):
            raise BridgeError("Malformed Draft Factory SELECT result")
        for key in (
            "candidate_id",
            "topic",
            "topic_cluster",
            "content_type",
            "packaging_request",
            "asset",
        ):
            if key not in item:
                raise BridgeError("Malformed Draft Factory SELECT result")
        for key in ("candidate_id", "topic", "topic_cluster", "content_type"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise BridgeError("Malformed Draft Factory SELECT result")
        _validate_select_packaging_request(item)
        _validate_select_asset_descriptor(item)
    return result


def _validate_select_packaging_request(item: dict) -> None:
    """Validate one ranked item's packaging_request (fail closed)."""
    request = item["packaging_request"]
    if not isinstance(request, dict):
        raise BridgeError("Malformed Draft Factory SELECT result")
    if set(request) != {"candidate", "assets"}:
        raise BridgeError("Malformed Draft Factory SELECT result")
    candidate = request["candidate"]
    if not isinstance(candidate, dict):
        raise BridgeError("Malformed Draft Factory SELECT result")
    for forbidden in MODEL_FORBIDDEN_CANDIDATE_FIELDS:
        if forbidden in candidate:
            raise BridgeError("Malformed Draft Factory SELECT result")
    if set(candidate) != set(PACKAGING_CANDIDATE_FIELDS):
        raise BridgeError("Malformed Draft Factory SELECT result")
    enum_checks = (
        ("content_type", PACKAGING_CONTENT_TYPES),
        ("content_shape", CONTENT_SHAPES),
        ("timeliness", TIMELINESS_VALUES),
        ("verification_status", VERIFICATION_VALUES),
        ("source_grounding", SOURCE_GROUNDING_VALUES),
        ("audience_value", AUDIENCE_VALUE_VALUES),
        ("visual_requirement", VISUAL_REQUIREMENT_VALUES),
    )
    for field, allowed in enum_checks:
        value = candidate[field]
        if not isinstance(value, str) or value not in allowed:
            raise BridgeError("Malformed Draft Factory SELECT result")
    beats = candidate["distinct_beat_count"]
    if isinstance(beats, bool) or not isinstance(beats, int) or beats < 0:
        raise BridgeError("Malformed Draft Factory SELECT result")
    for field in ("depicts_real_world_subject", "still_developing"):
        if not isinstance(candidate[field], bool):
            raise BridgeError("Malformed Draft Factory SELECT result")
    # Single content_type authority: the nested packaging candidate
    # must agree with the SELECT item's top-level content_type (which
    # the later queue identity check binds to the eligible entry).
    # Otherwise the receipt would bind one type while the manifest
    # builds from another.
    if candidate["content_type"] != item["content_type"]:
        raise BridgeError("Malformed Draft Factory SELECT result")
    assets = request["assets"]
    if not isinstance(assets, dict):
        raise BridgeError("Malformed Draft Factory SELECT result")
    if set(assets) != set(PACKAGING_ASSET_BOOL_FIELDS):
        raise BridgeError("Malformed Draft Factory SELECT result")
    for field in PACKAGING_ASSET_BOOL_FIELDS:
        if not isinstance(assets[field], bool):
            raise BridgeError("Malformed Draft Factory SELECT result")


def _validate_select_asset_descriptor(item: dict) -> None:
    """Validate one ranked item's asset descriptor shape (fail closed).

    Structural only: authoritative `nullone.packaging-asset.v1`
    field set, schema literal, candidate binding, asset-kind
    vocabulary, and file-backed vs NONE path/provenance shape.
    Receipt/style matching authority stays downstream.
    """
    descriptor = item["asset"]
    if not isinstance(descriptor, dict):
        raise BridgeError("Malformed Draft Factory SELECT result")
    allowed_keys = set(SELECT_ASSET_DESCRIPTOR_REQUIRED) | set(
        SELECT_ASSET_DESCRIPTOR_OPTIONAL
    )
    if not set(SELECT_ASSET_DESCRIPTOR_REQUIRED) <= set(descriptor) <= allowed_keys:
        raise BridgeError("Malformed Draft Factory SELECT result")
    if descriptor["schema"] != ASSET_DESCRIPTOR_SCHEMA:
        raise BridgeError("Malformed Draft Factory SELECT result")
    if (
        not isinstance(descriptor["candidate_id"], str)
        or not descriptor["candidate_id"].strip()
        or descriptor["candidate_id"] != item["candidate_id"]
    ):
        raise BridgeError("Malformed Draft Factory SELECT result")
    kind = descriptor["asset_kind"]
    if kind not in SELECT_ASSET_KINDS:
        raise BridgeError("Malformed Draft Factory SELECT result")
    local_path = descriptor["local_path"]
    provenance = descriptor["provenance"]
    if kind in FILE_BACKED_ASSET_KINDS:
        if not isinstance(local_path, str) or not local_path.strip():
            raise BridgeError("Malformed Draft Factory SELECT result")
        if not isinstance(provenance, str) or not provenance.strip():
            raise BridgeError("Malformed Draft Factory SELECT result")
    elif local_path is not None:
        raise BridgeError("Malformed Draft Factory SELECT result")
    elif provenance is not None and not isinstance(provenance, str):
        raise BridgeError("Malformed Draft Factory SELECT result")
    for field in SELECT_ASSET_DESCRIPTOR_OPTIONAL:
        value = descriptor.get(field)
        if value is not None and not isinstance(value, str):
            raise BridgeError("Malformed Draft Factory SELECT result")


def _validated_produce(result: object, *, carousel: bool) -> dict:
    """Structural validation for the PRODUCE round (fail closed)."""
    if not isinstance(result, dict) or set(result) != set(PRODUCE_FIELDS):
        raise BridgeError("Malformed Draft Factory PRODUCE result")
    caption = result["caption"]
    if not isinstance(caption, str) or not caption.strip():
        raise BridgeError("Malformed Draft Factory PRODUCE result")
    render_text = result["render_text"]
    if not isinstance(render_text, dict):
        raise BridgeError("Malformed Draft Factory PRODUCE result")
    for key, value in render_text.items():
        if key not in ("kicker", "headline", "stat", "source_name"):
            raise BridgeError("Malformed Draft Factory PRODUCE result")
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise BridgeError("Malformed Draft Factory PRODUCE result")
    slides = result["slides"]
    if carousel:
        if not isinstance(slides, dict) or not isinstance(
            slides.get("slides"), list
        ):
            raise BridgeError("Malformed Draft Factory PRODUCE result")
    elif slides is not None:
        raise BridgeError("Malformed Draft Factory PRODUCE result")
    return result


def _validated_complete(result: object) -> dict:
    """Structural validation for the COMPLETE round (fail closed)."""
    if not isinstance(result, dict) or set(result) != set(COMPLETE_FIELDS):
        raise BridgeError("Malformed Draft Factory COMPLETE result")
    if not isinstance(result["ledger_record"], dict):
        raise BridgeError("Malformed Draft Factory COMPLETE result")
    report = result["report_markdown"]
    if not isinstance(report, str) or not report.strip():
        raise BridgeError("Malformed Draft Factory COMPLETE result")
    return result


def _today(root: Path) -> str:
    return datetime.now(ZoneInfo(BAKU_ZONE)).strftime("%Y-%m-%d")


def _check_candidate_id(candidate_id: str) -> str:
    from nullone_packaging_receipt import check_candidate_id

    try:
        return check_candidate_id(candidate_id)
    except BridgeError:
        raise
    except Exception as exc:
        raise BridgeError(f"Draft candidate id rejected: {exc}") from exc


def remaining_budget(deadline: float) -> int:
    """Safe whole-cycle seconds available before the scheduler boundary."""
    return math.floor(deadline - time.monotonic() - CYCLE_SAFETY_MARGIN_SECONDS)


def bounded_timeout(deadline: float, *, helper_cap: int | None = None,
                    minimum: int = 1) -> int:
    remaining = remaining_budget(deadline)
    if remaining < minimum:
        raise BridgeError("Draft cycle budget exhausted")
    return min(remaining, helper_cap) if helper_cap is not None else remaining


def _run_helper(
    argv: list[str], *, workspace_root: Path, timeout: int, marker: str
) -> str:
    """Run one exact reviewed helper CLI (fail closed).

    The argv is always built by deterministic Python from constants
    and validated round outputs -- never from model-supplied command
    text. Non-zero exit, timeout, or a missing stdout marker is a
    BridgeError; stdout is returned for marker parsing by the caller.
    """
    try:
        cp = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=workspace_root,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"Draft helper timed out: {argv[1]}") from exc
    except OSError as exc:
        raise BridgeError(f"Draft helper failed to start: {argv[1]}") from exc
    if cp.returncode != 0:
        raise BridgeError(f"Draft helper rejected: {argv[1]} (exit={cp.returncode})")
    if marker not in (cp.stdout or ""):
        raise BridgeError(f"Draft helper missing proof marker: {argv[1]}")
    return cp.stdout


def _write_new_file(path: Path, content: str, *, workspace_root: Path) -> Path:
    """Write a deterministic output file (idempotent-or-blocked).

    The destination always derives from the candidate/date in
    Python; identical reruns reuse the bytes, but an existing file
    with different content BLOCKS instead of silently overwriting
    production state. Symlink/escape attempts fail closed.
    """
    root = workspace_root.resolve(strict=True)
    try:
        candidate = (workspace_root / path).resolve()
        candidate.relative_to(root)
    except (ValueError, OSError) as exc:
        raise BridgeError("Draft output path escapes workspace") from exc
    if path.is_symlink():
        raise BridgeError("Draft output path is a symlink")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_text(encoding="utf-8") == content:
            return path
        raise BridgeError(f"Draft output already exists: {path.name}")
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".draft-tmp-")
    import os as _os

    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        _os.replace(tmp_name, path)
    except BaseException:
        try:
            _os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def _scan_for_secrets(text: str, *, what: str) -> None:
    if SECRET_VALUE_PATTERN.search(text):
        raise BridgeError(f"Draft {what} looks secret-bearing; refusing write")


def _media_entries(
    record_outputs: list[dict[str, Any]], *, workspace_root: Path
) -> list[dict[str, Any]]:
    """Build validated preview media entries from render outputs."""
    from PIL import Image

    entries = []
    for output in record_outputs:
        rel = output.get("path")
        sha = output.get("sha256")
        if not isinstance(rel, str) or not isinstance(sha, str):
            raise BridgeError("Draft render record output malformed")
        local = (workspace_root / rel).resolve()
        try:
            local.relative_to(workspace_root.resolve(strict=True))
        except (ValueError, OSError) as exc:
            raise BridgeError("Draft render output escapes workspace") from exc
        if not local.is_file():
            raise BridgeError("Draft render output missing")
        if hashlib.sha256(local.read_bytes()).hexdigest() != sha:
            raise BridgeError("Draft render output hash mismatch")
        content_type = {".png": "image/png", ".jpg": "image/jpeg"}.get(
            local.suffix.lower()
        )
        if content_type is None:
            raise BridgeError("Draft render output has unknown media type")
        try:
            with Image.open(local) as image:
                width, height = image.size
        except Exception as exc:
            raise BridgeError("Draft render output unreadable") from exc
        entries.append(
            {
                "local_path": rel,
                "sha256": sha,
                "width": width,
                "height": height,
                "content_type": content_type,
            }
        )
    return entries


def _approval_blocks(review_post_id: str) -> dict[str, Any]:
    """Exact legacy approval-card blocks bound to one review post."""
    return {
        "blocks": [
            {
                "type": "buttons",
                "buttons": [
                    {
                        "label": "\u2705 T\u0259sdiq et",
                        "value": f"texbrif:approve:{review_post_id}",
                        "style": "success",
                    },
                    {
                        "label": "\u274c \u0130mtina et",
                        "value": f"texbrif:reject:{review_post_id}",
                        "style": "danger",
                    },
                    {
                        "label": "\U0001f4dd D\u0259yi\u015fiklik ist\u0259",
                        "value": f"texbrif:revise:{review_post_id}",
                    },
                ],
            }
        ]
    }


def invoke_draft(
    *,
    prompt: str,
    workspace: Path | str,
    model: str,
    timeout: int,
) -> dict[str, Any]:
    """Run one bounded Claude Draft Factory cycle with full mediation.

    Returns a summary dict (status, candidate_id, manifest/review
    identifiers). Raises BridgeError fail-closed on transport
    failure, malformed structured output, any helper rejection, or
    any mediation violation. The model performs read-only reasoning
    only; deterministic Python performs every write and every helper
    invocation through the exact reviewed commands.
    """
    root = Path(workspace)
    if not prompt.strip():
        raise BridgeError("Draft prompt must not be blank")
    if not model.strip():
        raise BridgeError("Draft model must not be blank")
    if timeout <= 0:
        raise BridgeError("Draft timeout must be positive")

    from nullone_draft_candidate_fallback import (
        DraftFallbackError,
        classify_packaging_receipt,
        new_ledger,
        next_candidate,
        record_acceptance,
        record_skip,
        record_story_disposition,
        save_ledger,
    )
    from nullone_packaging_receipt import (
        canonical_receipt_path,
        load_receipt,
        load_render_record,
    )
    from nullone_review_delivery import validate_preview_payload

    deadline = time.monotonic() + timeout

    def helper_timeout(name: str) -> int:
        minimum = MIN_CONSEQUENTIAL_HELPER_SECONDS if name in ("bridge", "deliver") else 1
        return bounded_timeout(deadline, helper_cap=HELPER_TIMEOUTS[name], minimum=minimum)

    def structured(schema: dict[str, Any], phase_prompt: str) -> dict[str, Any]:
        return run_structured(
            prompt=phase_prompt,
            allowed_tools=ALLOWED_TOOLS,
            schema=schema,
            model=model,
            max_turns=MAX_TURNS_PER_ROUND,
            timeout=bounded_timeout(deadline, minimum=MIN_ROUND_SECONDS),
            workspace=root,
            weekly_security_settings=draft_security_settings(root),
        )

    today = _today(root)
    drafts = root / "social/drafts/production"

    # ---- SELECT ----
    queue_snapshot = load_queue(root)
    eligible = queue_snapshot.eligible()
    eligible_prompt = [
        {"candidate_id": candidate_id, "topic": entry.topic,
         "topic_cluster": entry.fields["topic_cluster"],
         "content_type": entry.fields["content_type"]}
        for candidate_id, entry in eligible.items()
    ]
    selected = _validated_select(
        structured(SELECT_SCHEMA, prompt + SELECT_APPENDIX % {
            "max_ranked": MAX_RANKED,
            "eligible": json.dumps(eligible_prompt, ensure_ascii=False),
        })
    )
    if selected["decision"] == "NO_ACTION" or not selected["ranked"]:
        return {"status": "NO_ACTION", "candidate_id": None}
    for item in selected["ranked"]:
        _check_candidate_id(item["candidate_id"])
        entry = eligible.get(item["candidate_id"])
        if entry is None:
            raise BridgeError("Draft SELECT candidate is not eligible")
        if item["topic"] != entry.topic or item["topic_cluster"] != entry.fields["topic_cluster"] or item["content_type"] != entry.fields["content_type"]:
            raise BridgeError("Draft SELECT identity disagrees with queue")
        if entry.fields["content_type"] not in CONTENT_TYPES:
            raise BridgeError("Draft candidate content_type not reviewable")
    ranked_ids = [item["candidate_id"] for item in selected["ranked"]]
    if len(set(ranked_ids)) != len(ranked_ids):
        raise BridgeError("Draft ranked candidates contain duplicates")
    by_id = {item["candidate_id"]: item for item in selected["ranked"]}

    try:
        ledger = new_ledger(editorial_date=today, ranked_candidate_ids=ranked_ids)
    except DraftFallbackError as exc:
        raise BridgeError(f"Draft fallback ledger refused: {exc}") from exc
    ledger_path = drafts / f"{today}-draft-fallback-ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    accepted_id: str | None = None
    receipt: dict[str, Any] | None = None
    while True:
        try:
            step = next_candidate(ledger)
        except DraftFallbackError as exc:
            raise BridgeError(f"Draft fallback refused: {exc}") from exc
        if step.get("decision") == "ALL_SKIPPED":
            save_ledger(ledger_path, ledger)
            return {"status": "NO_ACTION", "candidate_id": None}
        candidate_id = step.get("candidate_id")
        item = by_id.get(candidate_id or "")
        if item is None:
            raise BridgeError("Draft fallback returned an unranked candidate")
        # A canonical receipt that already exists is authoritative and is
        # verified BEFORE any request/asset write or evaluator run. A
        # malformed/tampered one fails closed (BridgeError from
        # load_receipt). An existing STORY decision is handled
        # deterministically without re-evaluating changed model bytes.
        receipt_file = canonical_receipt_path(candidate_id, root=root)
        receipt = None
        kind = reason = None
        if receipt_file.exists() or receipt_file.is_symlink():
            try:
                receipt = load_receipt(receipt_file, root=root)
                if receipt.get("candidate_id") != candidate_id:
                    raise BridgeError("Draft receipt candidate mismatch")
                kind, reason = classify_packaging_receipt(receipt)
            except BridgeError:
                raise
            except Exception as exc:
                raise BridgeError(f"Draft receipt classification failed: {exc}") from exc
            if kind != "STORY_FALLBACK":
                # Existing SKIP/POST receipts keep the idempotent
                # evaluator path below (same bytes or CONFLICT).
                receipt = None
                kind = reason = None
        if receipt is None:
            request_path = drafts / f"{today}-{candidate_id}-packaging-request.json"
            asset_path = drafts / f"{today}-{candidate_id}-packaging-asset.json"
            _write_new_file(
                request_path,
                json.dumps(item["packaging_request"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                workspace_root=root,
            )
            _write_new_file(
                asset_path,
                json.dumps(item["asset"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                workspace_root=root,
            )
            _run_helper(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "nullone-packaging-evaluator.py"),
                    "evaluate",
                    "--candidate-id",
                    candidate_id,
                    "--request-file",
                    str(request_path),
                ],
                workspace_root=root,
                timeout=helper_timeout("evaluate"),
                marker="RECEIPT_PATH=",
            )
            try:
                receipt = load_receipt(receipt_file, root=root)
                kind, reason = classify_packaging_receipt(receipt)
            except BridgeError:
                raise
            except Exception as exc:
                raise BridgeError(f"Draft receipt classification failed: {exc}") from exc
        if kind == "SKIP_FALLBACK":
            try:
                ledger = record_skip(
                    ledger, candidate_id=candidate_id, skip_reason=reason or ""
                )
                save_ledger(ledger_path, ledger)
            except DraftFallbackError as exc:
                raise BridgeError(f"Draft fallback refused: {exc}") from exc
            continue
        if kind == "STORY_FALLBACK":
            # Draft Factory is FEED/CAROUSEL-only. StoryWorkflow owns
            # normal Story from its own Morning handoff; this is NOT a
            # handoff, acceptance, or render. Record and continue.
            try:
                ledger = record_story_disposition(
                    ledger, candidate_id=candidate_id, format_reason=reason or ""
                )
                save_ledger(ledger_path, ledger)
            except DraftFallbackError as exc:
                raise BridgeError(f"Draft fallback refused: {exc}") from exc
            continue
        try:
            ledger = record_acceptance(ledger, candidate_id=candidate_id)
            save_ledger(ledger_path, ledger)
        except DraftFallbackError as exc:
            raise BridgeError(f"Draft fallback refused: {exc}") from exc
        accepted_id = candidate_id
        break

    assert accepted_id is not None and receipt is not None
    format_decision = receipt.get("FORMAT_DECISION")
    manifest_format = FORMAT_TO_MANIFEST_FORMAT.get(format_decision or "")
    if manifest_format is None:
        raise BridgeError(f"Draft receipt format not producible: {format_decision!r}")
    carousel = manifest_format == "CAROUSEL"
    # ---- PRODUCE ----
    produced = _validated_produce(
        structured(
            PRODUCE_SCHEMA,
            prompt
            + PRODUCE_APPENDIX
            % {"format": format_decision, "candidate_id": accepted_id},
        ),
        carousel=carousel,
    )
    caption_path = drafts / f"{today}-{accepted_id}-caption.txt"
    _write_new_file(
        caption_path, produced["caption"].strip() + "\n", workspace_root=root
    )
    from nullone_packaging_receipt import (
        canonical_receipt_path,
        canonical_render_record_path,
    )

    render_argv = [
        sys.executable,
        str(SCRIPTS_DIR / "nullone-packaging-render.py"),
        "render",
        "--receipt",
        str(canonical_receipt_path(accepted_id, root=root)),
        "--asset-file",
        str(asset_path_for(accepted_id, today, root)),
    ]
    if carousel:
        spec_path = drafts / f"{today}-{accepted_id}-carousel-spec.json"
        _write_new_file(
            spec_path,
            json.dumps(produced["slides"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            workspace_root=root,
        )
        render_argv += ["--spec", str(spec_path)]
        output_path = drafts / f"{today}-{accepted_id}-carousel-slides"
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        for key in ("kicker", "headline", "stat", "source_name"):
            value = produced["render_text"].get(key)
            if value:
                render_argv += [f"--{key.replace('_', '-')}", value]
        output_path = drafts / f"{today}-{accepted_id}-feed.png"
    render_argv += ["--output", str(output_path)]
    render_out = _run_helper(
        render_argv,
        workspace_root=root,
        timeout=helper_timeout("render"),
        marker="RENDER_FORMAT=",
    )
    _ = render_out

    from nullone_packaging_receipt import canonical_render_record_path

    record = load_render_record(
        canonical_render_record_path(accepted_id, root=root), root=root
    )
    media_rels = []
    for entry in record.get("outputs", []):
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            raise BridgeError("Draft render record output malformed")
        media_rels.append(str((root / rel).resolve()))
    manifest_id = f"{today}-{accepted_id}"
    manifest_argv = [
        sys.executable,
        str(SCRIPTS_DIR / "nullone-manifest.py"),
        "build",
        "--candidate-id",
        accepted_id,
        "--topic",
        eligible[accepted_id].topic,
        "--topic-cluster",
        eligible[accepted_id].fields["topic_cluster"],
        "--content-type",
        eligible[accepted_id].fields["content_type"],
        "--format",
        manifest_format,
        "--caption-file",
        str(caption_path),
        "--manifest-id",
        manifest_id,
    ]
    for media in media_rels:
        manifest_argv += ["--media", media]
    manifest_argv += [
        "--packaging-receipt",
        str(canonical_receipt_path(accepted_id, root=root)),
        "--render-record",
        str(canonical_render_record_path(accepted_id, root=root)),
    ]
    _run_helper(
        manifest_argv,
        workspace_root=root,
        timeout=helper_timeout("manifest"),
        marker="MANIFEST_CREATED=",
    )
    manifest_path = root / "social/ops/manifests" / f"{manifest_id}.json"
    if not manifest_path.is_file():
        raise BridgeError("Draft manifest missing after build")

    # ---- BRIDGE ----
    load_queue(root).ready_entry(accepted_id, baseline=eligible[accepted_id])
    bridge_out = _run_helper(
        [
            sys.executable,
            str(SCRIPTS_DIR / "nullone-draft-bridge.py"),
            "execute",
            str(manifest_path),
        ],
        workspace_root=root,
        timeout=helper_timeout("bridge"),
        marker="DRAFT_BRIDGE=PASS",
    )
    review_post_id = None
    for line in bridge_out.splitlines():
        if line.startswith("REVIEW_POST_ID="):
            review_post_id = line.split("=", 1)[1].strip()
    if not review_post_id or "REVIEW_STATE=DRAFT_CREATED" not in bridge_out.splitlines():
        raise BridgeError("Draft bridge proof missing created review post")
    # The review draft now exists. A later delivery or COMPLETE failure must
    # never leave this candidate eligible for another scheduled draft.
    flip_ready_to_drafted(root, accepted_id, eligible[accepted_id])

    # ---- PAYLOAD (deterministic assembly) ----
    media_entries = _media_entries(
        record.get("outputs", []), workspace_root=root
    )
    payload = {
        "schema": "nullone.main-preview.v1",
        "brand": "NullOne",
        "review_post_id": review_post_id,
        "text": produced["caption"].strip(),
        "media": media_entries,
        "presentation": _approval_blocks(review_post_id),
    }
    try:
        validate_preview_payload(payload)
    except Exception as exc:
        raise BridgeError(f"Draft preview payload invalid: {exc}") from exc
    payload_path = drafts / f"{today}-{accepted_id}-preview-payload.json"
    _write_new_file(
        payload_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        workspace_root=root,
    )
    delivery_timeout = helper_timeout("deliver")
    notify_state = "SENT"
    try:
        delivery_out = _run_helper(
            [
                sys.executable,
                str(SCRIPTS_DIR / "nullone_telegram_review_delivery_adapter.py"),
                "deliver",
                "--payload-file",
                str(payload_path),
            ],
            workspace_root=root,
            timeout=delivery_timeout,
            marker="DELIVERY_STATUS=SENT",
        )
        if "DELIVERY_STATUS=SENT" not in delivery_out.splitlines():
            raise BridgeError("Draft delivery did not prove SENT")
    except BridgeError:
        # An ambiguous or partial send must never be duplicated.
        notify_state = "NOTIFY_FAILED"

    # ---- COMPLETE ----
    completed = _validated_complete(
        structured(
            COMPLETE_SCHEMA,
            prompt
            + COMPLETE_APPENDIX
            % {
                "bridge": "DRAFT_CREATED",
                "candidate_id": accepted_id,
                "review": review_post_id,
                "notify": notify_state,
            },
        )
    )
    ledger_record = completed["ledger_record"]
    if ledger_record.get("candidate_id") != accepted_id:
        raise BridgeError("Draft ledger record names a different candidate")
    _scan_for_secrets(
        json.dumps(ledger_record, ensure_ascii=False), what="ledger record"
    )
    _scan_for_secrets(completed["report_markdown"], what="production report")
    ledger_file = root / LEDGER_PATH
    try:
        with ledger_file.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(ledger_record, ensure_ascii=False, sort_keys=True) + "\n"
            )
    except OSError as exc:
        raise BridgeError("Draft topic ledger unwritable") from exc
    report_path = root / f"social/publisher/{today}-{accepted_id}-draft.md"
    _write_new_file(
        report_path, completed["report_markdown"].strip() + "\n", workspace_root=root
    )

    return {
        "status": "DRAFT_CREATED",
        "candidate_id": accepted_id,
        "manifest_path": str(manifest_path.relative_to(root)),
        "review_post_id": review_post_id,
        "notify_state": notify_state,
    }


def asset_path_for(candidate_id: str, today: str, root: Path) -> Path:
    """Deterministic asset-descriptor path for one candidate/day."""
    return root / f"social/drafts/production/{today}-{candidate_id}-packaging-asset.json"


def self_test() -> int:
    if set(SELECT_FIELDS) != {"decision", "ranked", "notes"}:
        raise AssertionError("draft select schema fields drifted")
    if set(PRODUCE_FIELDS) != {"caption", "render_text", "slides"}:
        raise AssertionError("draft produce schema fields drifted")
    if set(COMPLETE_FIELDS) != {"ledger_record", "report_markdown"}:
        raise AssertionError("draft complete schema fields drifted")
    # SELECT packaging contract binding: the schema and validator
    # share the authoritative policy/receipt vocabularies, so a
    # contract evolution fails loudly here instead of drifting.
    if set(PACKAGING_CANDIDATE_FIELDS) != {
        "content_type",
        "content_shape",
        "timeliness",
        "verification_status",
        "source_grounding",
        "audience_value",
        "distinct_beat_count",
        "depicts_real_world_subject",
        "still_developing",
        "visual_requirement",
    }:
        raise AssertionError("draft select candidate fields drifted")
    if set(PACKAGING_ASSET_BOOL_FIELDS) != {
        "has_official_or_source_image",
        "has_usable_screenshot",
        "image_on_topic",
        "image_quality_ok",
        "data_visualization_possible",
    }:
        raise AssertionError("draft select asset fields drifted")
    if SELECT_ASSET_KINDS != frozenset(STYLE_TO_ASSET_KIND.values()):
        raise AssertionError("draft select asset kinds drifted")
    if VISUAL_REQUIREMENT_VALUES != frozenset({"NONE", "SOURCE_GROUNDED"}):
        raise AssertionError("draft select visual_requirement drifted")
    for bad in (None, {}, {"decision": "SELECT", "ranked": [], "notes": "n"}):
        try:
            _validated_select(bad)
        except BridgeError:
            pass
        else:
            raise AssertionError(f"malformed select accepted: {bad!r}")
    for bad in (None, {}, {"caption": " ", "render_text": {}, "slides": None}):
        try:
            _validated_produce(bad, carousel=False)
        except BridgeError:
            pass
        else:
            raise AssertionError(f"malformed produce accepted: {bad!r}")
    # Model tool authority must stay read-only: Bash/Edit/Write/Agent
    # are granted nowhere (every mutation below is deterministic
    # Python). Only the five reviewed helper scripts may ever appear
    # as executed subprocesses.
    if ALLOWED_TOOLS != ["Read", "WebSearch", "WebFetch", "Glob"]:
        raise AssertionError(f"draft model tools drifted: {ALLOWED_TOOLS}")
    source = Path(__file__).read_text(encoding="utf-8")
    for script in (
        "nullone-packaging-evaluator.py",
        "nullone-packaging-render.py",
        "nullone-manifest.py",
        "nullone-draft-bridge.py",
        "nullone_telegram_review_delivery_adapter.py",
    ):
        if script not in source:
            raise AssertionError(f"reviewed helper missing: {script}")
    print("DRAFT_CLAUDE_PROVIDER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
