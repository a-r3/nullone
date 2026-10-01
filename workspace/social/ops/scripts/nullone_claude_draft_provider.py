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
                accepts exactly one, STORY delegates, system
                failures BLOCK).
    PRODUCE  -> caption + render text args + carousel spec. Python
                writes caption/spec files, runs the render
                dispatcher, builds the manifest, and runs the local
                bridge exactly once.
    COMPLETE -> ledger record + queue file content + production
                report. Python assembles the Telegram preview
                payload deterministically (caption text, hashed
                render outputs, exact approval buttons), validates
                it with the existing preview validator, runs the
                delivery helper (one retry, then NOTIFY_FAILED),
                constrains the queue edit to the single candidate
                status flip, appends the ledger line, and writes
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
QUEUE_PATH = "social/state/candidate-queue.md"
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

# Per-helper wall-clock budgets (seconds); the overall profile
# timeout (900) bounds the whole cycle via deadline tracking.
HELPER_TIMEOUTS = {
    "evaluate": 120,
    "render": 300,
    "manifest": 120,
    "bridge": 180,
    "deliver": 120,
}

SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(api[_-]?key|bearer|token|secret|password)\s*[:=]\s*\S+"
)

SCRIPTS_DIR = Path(__file__).resolve().parent

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
                    "packaging_request": {"type": "object"},
                    "asset": {"type": "object"},
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

COMPLETE_FIELDS = ("ledger_record", "queue_content", "report_markdown")
COMPLETE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(COMPLETE_FIELDS),
    "properties": {
        "ledger_record": {"type": "object"},
        "queue_content": {"type": "string", "minLength": 1},
        "report_markdown": {"type": "string", "minLength": 1},
    },
}

BAKU_ZONE = "Asia/Baku"

SELECT_APPENDIX = """
Transport note for this phase: you have read-only tools only (no
shell, no file writes). Return ONLY the structured SELECT result:
ranked READY candidates strongest-first (at most %(max_ranked)d),
each with candidate_id, topic, topic_cluster, content_type, the
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
the full updated candidate-queue.md content (only the %(candidate_id)s
line flips to DRAFTED), and the production report markdown (no
secrets, no presigned URLs). Deterministic Python validates and
writes everything; never supply filesystem paths or commands.
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
    """Structural validation for the SELECT round (fail closed)."""
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
        if not isinstance(item["packaging_request"], dict):
            raise BridgeError("Malformed Draft Factory SELECT result")
        if not isinstance(item["asset"], dict):
            raise BridgeError("Malformed Draft Factory SELECT result")
    return result


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
    for key in ("queue_content", "report_markdown"):
        value = result[key]
        if not isinstance(value, str) or not value.strip():
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


def _queue_flip(queue_text: str, new_content: str, *, candidate_id: str) -> str:
    """Constrain the queue edit to the single candidate status flip.

    The model's full-file content must equal the current queue
    except for exactly one line: the candidate's READY line becomes
    DRAFTED, byte-identical otherwise. Anything else BLOCKS.
    """
    old_lines = queue_text.splitlines()
    new_lines = new_content.splitlines()
    if len(old_lines) != len(new_lines):
        raise BridgeError("Draft queue update changes line count; refusing write")
    changed = [
        index
        for index, (old, new) in enumerate(zip(old_lines, new_lines))
        if old != new
    ]
    if len(changed) != 1:
        raise BridgeError("Draft queue update must touch exactly one line")
    old_line = old_lines[changed[0]]
    new_line = new_lines[changed[0]]
    if candidate_id not in old_line or "status=READY" not in old_line:
        raise BridgeError("Draft queue update must target the READY candidate line")
    if new_line != old_line.replace("status=READY", "status=DRAFTED"):
        raise BridgeError("Draft queue update must only flip READY to DRAFTED")
    return new_content


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
        save_ledger,
    )
    from nullone_packaging_receipt import load_receipt, load_render_record
    from nullone_review_delivery import validate_preview_payload

    deadline = time.monotonic() + timeout

    def round_timeout() -> int:
        remaining = int(deadline - time.monotonic())
        if remaining < 60:
            raise BridgeError("Draft cycle budget exhausted")
        return remaining

    def structured(schema: dict[str, Any], phase_prompt: str) -> dict[str, Any]:
        return run_structured(
            prompt=phase_prompt,
            allowed_tools=ALLOWED_TOOLS,
            schema=schema,
            model=model,
            max_turns=MAX_TURNS_PER_ROUND,
            timeout=round_timeout(),
            workspace=root,
            weekly_security_settings=draft_security_settings(root),
        )

    today = _today(root)
    drafts = root / "social/drafts/production"

    # ---- SELECT ----
    selected = _validated_select(
        structured(SELECT_SCHEMA, prompt + SELECT_APPENDIX % {"max_ranked": MAX_RANKED})
    )
    if selected["decision"] == "NO_ACTION" or not selected["ranked"]:
        return {"status": "NO_ACTION", "candidate_id": None}
    for item in selected["ranked"]:
        _check_candidate_id(item["candidate_id"])
        if item["content_type"] not in CONTENT_TYPES:
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
            timeout=HELPER_TIMEOUTS["evaluate"],
            marker="RECEIPT_PATH=",
        )
        try:
            from nullone_packaging_receipt import canonical_receipt_path

            receipt = load_receipt(
                canonical_receipt_path(candidate_id, root=root), root=root
            )
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
        try:
            ledger = record_acceptance(ledger, candidate_id=candidate_id)
            save_ledger(ledger_path, ledger)
        except DraftFallbackError as exc:
            raise BridgeError(f"Draft fallback refused: {exc}") from exc
        accepted_id = candidate_id
        break

    assert accepted_id is not None and receipt is not None
    format_decision = receipt.get("FORMAT_DECISION")
    if format_decision == "STORY":
        # Delegated to the StoryWorkflow path: accepted for cycle
        # accounting, but nothing is produced here.
        return {"status": "DELEGATED", "candidate_id": accepted_id}
    manifest_format = FORMAT_TO_MANIFEST_FORMAT.get(format_decision or "")
    if manifest_format is None:
        raise BridgeError(f"Draft receipt format not producible: {format_decision!r}")
    carousel = manifest_format == "CAROUSEL"
    item = by_id[accepted_id]

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
        timeout=HELPER_TIMEOUTS["render"],
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
        item["topic"],
        "--topic-cluster",
        item["topic_cluster"],
        "--content-type",
        item["content_type"],
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
        timeout=HELPER_TIMEOUTS["manifest"],
        marker="MANIFEST_CREATED=",
    )
    manifest_path = root / "social/ops/manifests" / f"{manifest_id}.json"
    if not manifest_path.is_file():
        raise BridgeError("Draft manifest missing after build")

    # ---- BRIDGE ----
    bridge_out = _run_helper(
        [
            sys.executable,
            str(SCRIPTS_DIR / "nullone-draft-bridge.py"),
            "execute",
            str(manifest_path),
        ],
        workspace_root=root,
        timeout=HELPER_TIMEOUTS["bridge"],
        marker="DRAFT_BRIDGE=PASS",
    )
    review_post_id = None
    for line in bridge_out.splitlines():
        if line.startswith("REVIEW_POST_ID="):
            review_post_id = line.split("=", 1)[1].strip()
    if not review_post_id:
        raise BridgeError("Draft bridge proof missing review post id")

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
    notify_state = "SENT"
    try:
        _run_helper(
            [
                sys.executable,
                str(SCRIPTS_DIR / "nullone_telegram_review_delivery_adapter.py"),
                "deliver",
                "--payload-file",
                str(payload_path),
            ],
            workspace_root=root,
            timeout=HELPER_TIMEOUTS["deliver"],
            marker="DELIVERY_STATUS=",
        )
    except BridgeError:
        # Retry notification at most once; a second failure keeps
        # the draft and manifest and records NOTIFY_FAILED.
        try:
            _run_helper(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "nullone_telegram_review_delivery_adapter.py"),
                    "deliver",
                    "--payload-file",
                    str(payload_path),
                ],
                workspace_root=root,
                timeout=HELPER_TIMEOUTS["deliver"],
                marker="DELIVERY_STATUS=",
            )
        except BridgeError:
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
    queue_file = root / QUEUE_PATH
    try:
        queue_text = queue_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise BridgeError("Draft candidate queue unreadable") from exc
    new_queue = _queue_flip(queue_text, completed["queue_content"], candidate_id=accepted_id)
    # Constrained overwrite only: _queue_flip above already proved the
    # new content differs by exactly the single candidate status flip.
    # Atomic temp+replace; never truncate-then-write.
    import os as _os

    fd, tmp_name = tempfile.mkstemp(
        dir=str(queue_file.parent), prefix=".draft-tmp-"
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_queue)
        _os.replace(tmp_name, queue_file)
    except BaseException:
        try:
            _os.unlink(tmp_name)
        except OSError:
            pass
        raise BridgeError("Draft candidate queue unwritable")
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
    if set(COMPLETE_FIELDS) != {"ledger_record", "queue_content", "report_markdown"}:
        raise AssertionError("draft complete schema fields drifted")
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
