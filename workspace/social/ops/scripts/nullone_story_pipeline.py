#!/usr/bin/env python3
"""Lightweight Story draft production pipeline for issue #33.

Consumes one already-selected, already-verified candidate and produces at
most one Story review draft, ending at human review. This module never
publishes anything and has no import path to the publisher/publish-bridge
scripts.

Pipeline (see docs/contracts/cadence-contract-v1.md and the #33 issue for
the full contract this implements):

    trigger adapter (cadence PREPARE_STORY, or a future #36 adapter)
    -> validate_candidate (VERIFICATION: PASS required)
    -> writer (Haiku or a fake, given only minimal editorial context)
    -> separate final verifier (writer output can never self-certify PASS)
    -> deterministic story_version_id
    -> render_story_v2.py (reused, unchanged) + Production Bridge media
       inspection (dimension/hash authority reused from
       nullone_bridge_common, not reinvented)
    -> nullone.production.v1 manifest (reused schema/validator, immutable
       once written -- a retry for the same logical version reuses the
       existing manifest instead of minting a second one)
    -> at most one review-draft attempt, delegated to an injected
       DraftConnector (the real one shells out to
       nullone-draft-bridge.py; tests inject a fake -- no network)
    -> deterministic Telegram preview payload (legacy `texbrif:` callback
       values, unchanged approval boundary)

The core `run_story_pipeline()` function takes a plain candidate dict and
has no #32-specific knowledge; `prepare_story_from_cadence_recommendation()`
is the thin, separate trigger adapter that validates a
nullone.cadence-contract.v1 PREPARE_STORY response before calling the core.
A future #36 breaking-trigger adapter can be added the same way without
touching the core, the writer, the verifier, the renderer, the manifest
logic, the draft connector, or the Telegram preview builder.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from nullone_bridge_common import (
    ALLOWED_CONTENT_TYPES,
    CANONICAL_ACCOUNT_ID,
    BridgeError,
    atomic_write_json,
    inspect_media,
    load_manifest,
    now_iso,
    resolve_workspace_path,
    sha256_bytes,
    validate_manifest,
    workspace_relative,
)
from nullone_bridge_common import SCHEMA as PRODUCTION_MANIFEST_SCHEMA

try:
    from nullone_claude import run_structured
except ImportError:  # pragma: no cover - claude CLI wrapper always present in-repo
    run_structured = None  # type: ignore[assignment]


SCHEMA = "nullone.story-spec.v1"
CONTRACT_VERSION = "1.0.0"

MANIFEST_FORMAT = "STORY"

ALLOWED_LAYOUTS = frozenset({"breaking", "big-stat", "explainer", "comparison"})

ACCEPTED_CADENCE_SCHEMA = "nullone.cadence-contract.v1"
ACCEPTED_RECOMMENDATION = "PREPARE_STORY"
ACCEPTED_REASON_CODES = frozenset({"STORY_GAP"})
ACCEPTED_PERMITTED_ACTION = "CANDIDATE_SEARCH_AND_PREPARE"

REQUIRED_CANDIDATE_FIELDS = (
    "candidate_id",
    "topic",
    "topic_cluster",
    "content_type",
    "verification",
    "evidence_refs",
    "source_attribution",
)

# Fields that may be sent to the Haiku writer. Deliberately excludes
# cadence arithmetic, publication state, hashes, filesystem paths, render
# dimensions, manifest fields and Zernio/Telegram mechanics (issue #33,
# "Haiku boundary").
WRITER_CONTEXT_CANDIDATE_FIELDS = (
    "topic",
    "topic_cluster",
    "content_type",
    "claims",
    "limitations",
    "product_version_region",
    "source_attribution",
    "evidence_refs",
    "factual_inputs",
    "source_image",
    "operator_revision_instruction",
)

STORY_PIPELINE_OUTCOMES = frozenset(
    {
        "DRAFT_CREATED",
        "CANDIDATE_NOT_ELIGIBLE",
        "WRITER_OUTPUT_INVALID",
        "VERIFICATION_BLOCKED",
        "RENDER_FAILED",
        "MANIFEST_BLOCKED",
        "REVIEW_DRAFT_ALREADY_CONSUMED",
        "REVIEW_DRAFT_PREFLIGHT_BLOCKED",
        "REVIEW_DRAFT_AMBIGUOUS",
    }
)

# nullone-draft-bridge.py review states that mean "an attempt was already
# consumed and it is unsafe to retry automatically" (issue #33 section 13).
_UNSAFE_TO_RETRY_REVIEW_STATES = frozenset(
    {"CREATE_IN_FLIGHT", "DRAFT_CREATED", "REVIEW_UNKNOWN"}
)


class StoryPipelineError(RuntimeError):
    """Base class for all #33 Story pipeline errors."""


class StoryTriggerRejected(StoryPipelineError):
    """The cadence (or other) trigger is not an accepted PREPARE_STORY signal.

    Raised directly to the caller -- unlike the domain-blocked conditions
    below, an invalid/malformed trigger is a caller contract violation, not
    a legitimate pipeline outcome.
    """


class StoryCandidateNotEligible(StoryPipelineError):
    """Candidate is missing required fields or is not VERIFICATION: PASS."""


class StoryWriterOutputInvalid(StoryPipelineError):
    """Writer output does not match the nullone.story-spec.v1 writer shape."""


class StoryVerificationBlocked(StoryPipelineError):
    """The separate final verifier did not return PASS for the exact wording."""


class StoryRenderFailed(StoryPipelineError):
    """render_story_v2.py failed, or its output failed media inspection."""


class StoryManifestBlocked(StoryPipelineError):
    """Manifest construction/validation failed (nullone.production.v1)."""


# ---------------------------------------------------------------------------
# Typed pipeline result
# ---------------------------------------------------------------------------


@dataclass
class StoryPipelineResult:
    outcome: str
    reason_code: str
    reason_text: str
    manifest_id: str | None = None
    story_version_id: str | None = None
    review_post_id: str | None = None
    manifest_path: str | None = None
    preview_payload: dict[str, Any] | None = None
    preview_delivery: dict[str, Any] | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome not in STORY_PIPELINE_OUTCOMES:
            raise StoryPipelineError(f"Unknown Story pipeline outcome: {self.outcome!r}")


def _result(outcome: str, reason_text: str, **kwargs: Any) -> StoryPipelineResult:
    return StoryPipelineResult(
        outcome=outcome,
        reason_code=outcome,
        reason_text=reason_text,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Trigger adapter -- #32 cadence recommendation only. Kept separate from
# the core so a future #36 breaking-trigger adapter can be added without
# touching run_story_pipeline() or anything it calls.
# ---------------------------------------------------------------------------


def validate_cadence_trigger(cadence_response: Any) -> dict[str, Any]:
    """Accept only a well-formed nullone.cadence-contract.v1 PREPARE_STORY response.

    Rejects NO_ACTION, PREPARE_MAIN_CANDIDATE and any malformed/incompatible
    response. Never recomputes cadence -- #32 owns that arithmetic.
    """

    if not isinstance(cadence_response, dict):
        raise StoryTriggerRejected("cadence_response must be an object")

    if cadence_response.get("schema") != ACCEPTED_CADENCE_SCHEMA:
        raise StoryTriggerRejected(
            f"Unsupported cadence schema: {cadence_response.get('schema')!r}"
        )

    recommendation = cadence_response.get("recommendation")
    if recommendation != ACCEPTED_RECOMMENDATION:
        raise StoryTriggerRejected(
            f"Cadence recommendation is not PREPARE_STORY: {recommendation!r}"
        )

    reason_code = cadence_response.get("reason_code")
    if reason_code not in ACCEPTED_REASON_CODES:
        raise StoryTriggerRejected(
            f"Cadence reason_code incompatible with PREPARE_STORY: {reason_code!r}"
        )

    permitted_action = cadence_response.get("permitted_action")
    if permitted_action != ACCEPTED_PERMITTED_ACTION:
        raise StoryTriggerRejected(
            f"Cadence permitted_action is not {ACCEPTED_PERMITTED_ACTION!r}: "
            f"{permitted_action!r}"
        )

    return cadence_response


def prepare_story_from_cadence_recommendation(
    cadence_response: dict[str, Any],
    candidate: dict[str, Any],
    **kwargs: Any,
) -> StoryPipelineResult:
    """#32 trigger adapter: validate the cadence signal, then run the core."""

    validate_cadence_trigger(cadence_response)
    return run_story_pipeline(candidate, **kwargs)


# ---------------------------------------------------------------------------
# Candidate input
# ---------------------------------------------------------------------------


def validate_candidate(candidate: Any) -> dict[str, Any]:
    """Require a structurally complete, already-verified candidate.

    This pipeline is production, not discovery: it consumes exactly one
    already-selected candidate and never scans, browses or rescopes a queue.
    """

    if not isinstance(candidate, dict):
        raise StoryCandidateNotEligible("candidate must be an object")

    missing = [f for f in REQUIRED_CANDIDATE_FIELDS if not candidate.get(f)]
    if missing:
        raise StoryCandidateNotEligible(
            f"candidate missing required field(s): {missing}"
        )

    if candidate["content_type"] not in ALLOWED_CONTENT_TYPES:
        raise StoryCandidateNotEligible(
            f"Unsupported candidate content_type: {candidate['content_type']!r}"
        )

    if not isinstance(candidate["evidence_refs"], list) or not candidate["evidence_refs"]:
        raise StoryCandidateNotEligible("candidate.evidence_refs must be a non-empty list")

    if candidate["verification"] != "PASS":
        raise StoryCandidateNotEligible(
            f"Candidate is not VERIFICATION: PASS: {candidate['verification']!r}"
        )

    return candidate


def build_writer_context(candidate: dict[str, Any]) -> dict[str, Any]:
    """Minimum editorial context sent to the writer -- the Haiku boundary.

    Deliberately excludes cadence arithmetic, publication state, hashes,
    filesystem mechanics, render dimensions, manifest construction,
    Telegram/Zernio mechanics and unrelated research history.
    """

    context = {
        field_name: candidate.get(field_name)
        for field_name in WRITER_CONTEXT_CANDIDATE_FIELDS
        if candidate.get(field_name) is not None
    }
    context["allowed_layouts"] = sorted(ALLOWED_LAYOUTS)
    context["scope_rule"] = (
        "Preserve claim scope exactly as supported by the evidence. Never "
        "broaden availability, rollout, plan eligibility, geography, "
        "benchmark, price, capability, comparison, number, unit, "
        "population, period or performance claims. If concise wording "
        "cannot preserve scope, prefer omitting the detail over "
        "broadening it."
    )
    return context


# ---------------------------------------------------------------------------
# Story spec -- nullone.story-spec.v1
# ---------------------------------------------------------------------------

_WRITER_SPEC_FIELDS = (
    "layout",
    "headline",
    "body",
    "stat",
    "source_name",
    "source_image",
    "cta",
    "left_stat",
    "right_stat",
    "left_label",
    "right_label",
)

# Fields a writer output must never be trusted to set; the pipeline strips
# and recomputes them itself so a writer can never self-certify PASS.
_SELF_CERTIFICATION_FIELDS = (
    "verification",
    "final_verification",
    "story_version_id",
    "schema",
)


def _strip_self_certification(raw_spec: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in raw_spec.items()
        if key not in _SELF_CERTIFICATION_FIELDS
    }


def validate_story_spec_shape(raw_spec: Any) -> dict[str, Any]:
    if not isinstance(raw_spec, dict):
        raise StoryWriterOutputInvalid("writer output must be an object")

    layout = raw_spec.get("layout")
    if layout not in ALLOWED_LAYOUTS:
        raise StoryWriterOutputInvalid(f"Unsupported/missing layout: {layout!r}")

    headline = raw_spec.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        raise StoryWriterOutputInvalid("writer output missing non-empty headline")

    for optional_field in _WRITER_SPEC_FIELDS[2:]:
        value = raw_spec.get(optional_field)
        if value is not None and not isinstance(value, str):
            raise StoryWriterOutputInvalid(
                f"writer field {optional_field!r} must be a string or absent"
            )

    return raw_spec


# ---------------------------------------------------------------------------
# Writer -- Haiku boundary
# ---------------------------------------------------------------------------

WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "layout": {"type": "string", "enum": sorted(ALLOWED_LAYOUTS)},
        "headline": {"type": "string"},
        "body": {"type": "string"},
        "stat": {"type": "string"},
        "source_name": {"type": "string"},
        "cta": {"type": "string"},
        "left_stat": {"type": "string"},
        "right_stat": {"type": "string"},
        "left_label": {"type": "string"},
        "right_label": {"type": "string"},
    },
    "required": [
        "layout",
        "headline",
        "body",
        "stat",
        "source_name",
        "cta",
        "left_stat",
        "right_stat",
        "left_label",
        "right_label",
    ],
    "additionalProperties": False,
}


def _writer_prompt(editorial_context: dict[str, Any]) -> str:
    return f"""
NULLONE STORY WRITER — MINIMAL EDITORIAL CONTEXT ONLY.

Write a single Instagram Story spec in Azerbaijani for @nullone.az.

Choose exactly one layout from: breaking, big-stat, explainer, comparison.
Do not default to the same layout every time -- pick the one this exact
content actually fits.

You may use ONLY the exact supported facts below. Do not invent, broaden,
or add any claim, number, date, geography, price, capability or
comparison beyond what is given.

{json.dumps(editorial_context, ensure_ascii=False, indent=2)}

Leave any field that does not apply to the chosen layout as an empty
string. You do not decide verification -- a separate process verifies
your exact wording afterward.
"""


class HaikuStoryWriter:
    """Real writer adapter: one Haiku call, no tools, minimal context.

    Not exercised in offline tests (it shells out to the `claude` CLI,
    mirroring every other Haiku call in this repository) -- offline tests
    inject a fake writer instead.
    """

    model = "haiku"

    def __call__(
        self, candidate: dict[str, Any], editorial_context: dict[str, Any]
    ) -> dict[str, Any]:
        if run_structured is None:  # pragma: no cover
            raise StoryPipelineError("nullone_claude.run_structured is unavailable")

        result = run_structured(
            prompt=_writer_prompt(editorial_context),
            allowed_tools=[],
            schema=WRITER_SCHEMA,
            model=self.model,
            max_turns=2,
        )
        return {k: v for k, v in result.items() if v != ""}


class StoryWriter(Protocol):
    def __call__(
        self, candidate: dict[str, Any], editorial_context: dict[str, Any]
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Final verification -- separate from the writer. A narrow, injectable
# interface. No real production verifier ships in #33 (see PR description);
# a numeric-scope checker is provided as one legitimate, deterministic,
# narrow verifier, and offline tests also exercise an unconditional fake.
# ---------------------------------------------------------------------------


class StoryVerifier(Protocol):
    def __call__(self, spec: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]: ...


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")


def numeric_scope_verifier(spec: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Deterministic, narrow verifier: every number in the spec must be
    verbatim-supported by the candidate's evidence text.

    This is intentionally narrow (numbers only) -- it is not a substitute
    for real fact verification, only a concrete example of the injectable
    verifier interface the issue requires.
    """

    evidence_blob = " ".join(
        str(item) for item in candidate.get("evidence_refs", [])
    )
    evidence_blob += " " + json.dumps(candidate.get("factual_inputs", {}), ensure_ascii=False)

    spec_text = " ".join(
        str(spec.get(field_name, ""))
        for field_name in ("headline", "body", "stat", "left_stat", "right_stat")
    )

    unsupported = sorted(
        {
            token
            for token in _NUMBER_RE.findall(spec_text)
            if token not in evidence_blob
        }
    )

    if unsupported:
        return {
            "status": "BLOCKED",
            "reason": f"Unsupported numeric claim(s) not found in evidence: {unsupported}",
        }

    return {"status": "PASS", "reason": "All numeric claims are evidence-supported."}


def make_fake_verifier(status: str, reason: str = "fake verifier") -> StoryVerifier:
    """Test helper: an unconditional fake verifier returning a fixed result."""

    def _verify(spec: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        return {"status": status, "reason": reason}

    _verify.__name__ = f"fake_verifier_{status.lower()}"
    return _verify


# ---------------------------------------------------------------------------
# Story version identity
# ---------------------------------------------------------------------------


def compute_story_version_id(candidate: dict[str, Any], spec: dict[str, Any]) -> str:
    """Deterministic fingerprint of one logical Story version.

    Depends only on the candidate identity/version, any revision linkage,
    and the finalized (writer + verifier passed) spec content -- never on
    wall-clock time or a random UUID, so a retry with the same inputs
    resolves to the same version.
    """

    payload = {
        "candidate_id": candidate["candidate_id"],
        "candidate_version": candidate.get("candidate_version"),
        "revision_of": candidate.get("revision_of"),
        "spec": {field_name: spec.get(field_name) for field_name in _WRITER_SPEC_FIELDS},
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value[:40] or "candidate"


def _manifest_id_for_version(candidate_id: str, story_version_id: str) -> str:
    return f"story-{_slug(candidate_id)}-{story_version_id[:16]}"


# ---------------------------------------------------------------------------
# Renderer adapter -- reuses render_story_v2.py and Production Bridge media
# inspection verbatim. No new dimension/hash authority.
# ---------------------------------------------------------------------------

_RENDERER_SCRIPT = "social/tools/render_story_v2.py"


def _renderer_args(spec: dict[str, Any], output_path: Path) -> list[str]:
    args = [
        "--layout",
        spec["layout"],
        "--headline",
        spec["headline"],
        "--output",
        str(output_path),
    ]

    optional_flags = {
        "stat": "--stat",
        "body": "--body",
        "source_name": "--source-name",
        "cta": "--cta",
        "left_stat": "--left-stat",
        "right_stat": "--right-stat",
        "left_label": "--left-label",
        "right_label": "--right-label",
    }

    for field_name, flag in optional_flags.items():
        value = spec.get(field_name)
        if value:
            args.extend([flag, value])

    if spec.get("source_image"):
        args.extend(["--source-image", str(resolve_workspace_path(spec["source_image"]))])

    return args


def render_story_asset(spec: dict[str, Any], manifest_id: str) -> dict[str, Any]:
    """Render exactly one 1080x1920 Story asset and validate it deterministically.

    Raises StoryRenderFailed on any renderer failure, missing output, or
    dimension/hash validation failure. No fallback to altered dimensions.
    """

    output_path = resolve_workspace_path(f"social/drafts/production/story/{manifest_id}.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    renderer_path = resolve_workspace_path(_RENDERER_SCRIPT)
    argv = [sys.executable, str(renderer_path), *_renderer_args(spec, output_path)]

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise StoryRenderFailed(f"render_story_v2.py invocation failed: {e}") from e

    if completed.returncode != 0:
        raise StoryRenderFailed(
            f"render_story_v2.py exited {completed.returncode}: {completed.stderr.strip()}"
        )

    if not output_path.is_file():
        raise StoryRenderFailed(f"render_story_v2.py did not produce output: {output_path}")

    try:
        media = inspect_media(output_path, MANIFEST_FORMAT)
    except BridgeError as e:
        raise StoryRenderFailed(str(e)) from e

    return media


# ---------------------------------------------------------------------------
# Manifest -- reuses nullone.production.v1 / nullone_bridge_common exactly.
# No second manifest schema.
# ---------------------------------------------------------------------------


def _story_caption_text(spec: dict[str, Any]) -> str:
    lines = [spec["headline"]]
    if spec.get("source_name"):
        lines.append(f"Mənbə: {spec['source_name']}")
    if spec.get("cta"):
        lines.append(spec["cta"])
    return "\n".join(lines) + "\n"


def build_story_manifest(
    candidate: dict[str, Any],
    spec: dict[str, Any],
    media: dict[str, Any],
    manifest_id: str,
) -> dict[str, Any]:
    """Build and persist a new immutable nullone.production.v1 STORY manifest.

    Never overwrites an existing manifest (no --force equivalent). Callers
    must check for an existing manifest for this story_version_id first and
    reuse it instead of calling this function again.
    """

    manifest_path = resolve_workspace_path(f"social/ops/manifests/{manifest_id}.json")

    if manifest_path.exists():
        raise StoryManifestBlocked(f"Manifest already exists: {manifest_path}")

    caption_path = resolve_workspace_path(
        f"social/drafts/production/story/{manifest_id}-caption.txt"
    )
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_text = _story_caption_text(spec)
    caption_path.write_text(caption_text, encoding="utf-8")
    caption_bytes = caption_path.read_bytes()

    manifest = {
        "schema": PRODUCTION_MANIFEST_SCHEMA,
        "manifest_id": manifest_id,
        "created_at": now_iso(),
        "candidate_id": candidate["candidate_id"],
        "topic": candidate["topic"],
        "topic_cluster": candidate["topic_cluster"],
        "content_type": candidate["content_type"],
        "format": MANIFEST_FORMAT,
        "verification": "PASS",
        "account_id": CANONICAL_ACCOUNT_ID,
        "caption": {
            "file": workspace_relative(caption_path),
            "sha256": sha256_bytes(caption_bytes),
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
        "story_version_id": spec["story_version_id"],
        "story_final_verification": spec["final_verification"],
    }

    try:
        validate_manifest(manifest)
    except BridgeError as e:
        raise StoryManifestBlocked(str(e)) from e

    atomic_write_json(manifest_path, manifest)
    return manifest


# ---------------------------------------------------------------------------
# Review-draft connector -- delegates to nullone-draft-bridge.py only.
# No publisher capability anywhere in this module.
# ---------------------------------------------------------------------------


class DraftConnector(Protocol):
    def create_review_draft(self, manifest_path: Path) -> None:
        """Attempt to create exactly one review draft for this manifest.

        Implementations MUST mutate the manifest file on disk to reflect
        the true resulting review state (mirroring
        nullone-draft-bridge.py's own contract) -- the pipeline always
        reloads the manifest afterward and treats it as ground truth,
        regardless of this method's return value or whether it raises.
        """


_DRAFT_BRIDGE_SCRIPT = "social/ops/scripts/nullone-draft-bridge.py"


class NulloneDraftBridgeConnector:
    """Real connector: delegates to nullone-draft-bridge.py execute only.

    Has no publisher capability -- it never imports or invokes the
    publish-bridge or publisher-run scripts (see the capability-negative
    test in tests/test_story_pipeline.py).
    """

    def create_review_draft(self, manifest_path: Path) -> None:
        script_path = resolve_workspace_path(_DRAFT_BRIDGE_SCRIPT)
        subprocess.run(
            [sys.executable, str(script_path), "execute", str(manifest_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        # No further interpretation here: the pipeline reloads the
        # manifest file (the draft-bridge script's own source of truth)
        # to determine the actual outcome.


def _review_state(manifest: dict[str, Any]) -> tuple[int, str, str | None]:
    review = manifest["review"]
    return review["create_attempts"], review["state"], review.get("zernio_draft_id")


def _review_is_untouched(manifest: dict[str, Any]) -> bool:
    attempts, state, draft_id = _review_state(manifest)
    return attempts == 0 and state == "NOT_CREATED" and not draft_id


# ---------------------------------------------------------------------------
# Telegram preview -- deterministic payload + narrow sender interface.
# No production Telegram execution wired into the core.
# ---------------------------------------------------------------------------


class TelegramPreviewSender(Protocol):
    def send(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def build_story_preview_payload(
    candidate: dict[str, Any],
    spec: dict[str, Any],
    manifest: dict[str, Any],
    review_post_id: str,
) -> dict[str, Any]:
    """Deterministic first-stage Story preview. Legacy `texbrif:` callback
    values only -- no typed action.callback objects, per the installed
    OpenClaw compatibility rule. No secrets (never a presigned upload URL).
    """

    media = manifest["media"][0]

    return {
        "schema": "nullone.story-preview.v1",
        "brand": "NullOne",
        "format": "STORY",
        "topic": candidate["topic"],
        "candidate_id": candidate["candidate_id"],
        "story_version_id": spec["story_version_id"],
        "manifest_id": manifest["manifest_id"],
        "review_post_id": review_post_id,
        "media_fingerprint": media["sha256"],
        "media_dimensions": f"{media['width']}x{media['height']}",
        "caption_excerpt": spec["headline"],
        "text": (
            "📰 Yeni NullOne Story draft\n\n"
            f"Mövzu: {candidate['topic']}\n"
            "Format: STORY\n"
            "Verification: PASS\n"
            f"Post ID: {review_post_id}\n\n"
            "Yayımlama qərarını seç:"
        ),
        "presentation": {
            "blocks": [
                {
                    "type": "buttons",
                    "buttons": [
                        {
                            "label": "✅ Təsdiq et",
                            "value": f"texbrif:approve:{review_post_id}",
                            "style": "success",
                        },
                        {
                            "label": "❌ İmtina et",
                            "value": f"texbrif:reject:{review_post_id}",
                            "style": "danger",
                        },
                        {
                            "label": "📝 Dəyişiklik istə",
                            "value": f"texbrif:revise:{review_post_id}",
                        },
                    ],
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# Core pipeline -- trigger-agnostic, reusable by #36 later.
# ---------------------------------------------------------------------------


def run_story_pipeline(
    candidate: dict[str, Any],
    *,
    writer: StoryWriter,
    verifier: StoryVerifier,
    draft_connector: DraftConnector,
    telegram_sender: TelegramPreviewSender | None = None,
) -> StoryPipelineResult:
    """Produce at most one Story review draft for one already-eligible candidate.

    This function never publishes, deletes, defers to a future send time,
    or bypasses human approval. It ends at a Zernio review draft plus a
    Telegram preview
    payload; approval/publication remain owned by the existing human
    boundary (agents/approval/AGENTS.md).
    """

    try:
        validate_candidate(candidate)
    except StoryCandidateNotEligible as e:
        return _result("CANDIDATE_NOT_ELIGIBLE", str(e))

    editorial_context = build_writer_context(candidate)

    try:
        raw_spec = writer(candidate, editorial_context)
        raw_spec = _strip_self_certification(raw_spec)
        validate_story_spec_shape(raw_spec)
    except StoryWriterOutputInvalid as e:
        return _result("WRITER_OUTPUT_INVALID", str(e))

    verify_result = verifier(raw_spec, candidate)
    if not isinstance(verify_result, dict) or verify_result.get("status") != "PASS":
        reason = (verify_result or {}).get("reason", "Final verification did not return PASS")
        return _result("VERIFICATION_BLOCKED", reason)

    spec = dict(raw_spec)
    spec["schema"] = SCHEMA
    spec["candidate_id"] = candidate["candidate_id"]
    spec["evidence_refs"] = list(candidate.get("evidence_refs", []))
    spec["final_verification"] = {
        "status": "PASS",
        "reason": verify_result.get("reason", ""),
        "verifier": getattr(verifier, "__name__", type(verifier).__name__),
        "checked_at": now_iso(),
    }

    story_version_id = compute_story_version_id(candidate, spec)
    spec["story_version_id"] = story_version_id

    manifest_id = _manifest_id_for_version(candidate["candidate_id"], story_version_id)
    manifest_path = resolve_workspace_path(f"social/ops/manifests/{manifest_id}.json")

    if manifest_path.is_file():
        # Same logical Story version already has a manifest: reuse it
        # rather than re-rendering or minting a second one.
        _, manifest = load_manifest(manifest_path)
    else:
        try:
            media = render_story_asset(spec, manifest_id)
        except StoryRenderFailed as e:
            return _result("RENDER_FAILED", str(e), story_version_id=story_version_id)

        try:
            manifest = build_story_manifest(candidate, spec, media, manifest_id)
        except StoryManifestBlocked as e:
            return _result(
                "MANIFEST_BLOCKED",
                str(e),
                story_version_id=story_version_id,
                manifest_id=manifest_id,
            )

    if not _review_is_untouched(manifest):
        attempts, state, draft_id = _review_state(manifest)
        if state == "DRAFT_CREATED" and draft_id:
            # Idempotent re-entry after a prior successful run: report the
            # existing draft, but never send a duplicate preview here --
            # preview delivery is only attempted on the call that actually
            # created the draft.
            return _result(
                "REVIEW_DRAFT_ALREADY_CONSUMED",
                "A review draft already exists for this Story version.",
                manifest_id=manifest_id,
                story_version_id=story_version_id,
                review_post_id=draft_id,
                manifest_path=workspace_relative(manifest_path),
            )
        return _result(
            "REVIEW_DRAFT_ALREADY_CONSUMED",
            f"Review create attempt already consumed (attempts={attempts}, state={state}).",
            manifest_id=manifest_id,
            story_version_id=story_version_id,
            manifest_path=workspace_relative(manifest_path),
        )

    try:
        draft_connector.create_review_draft(manifest_path)
    except Exception:
        # Ambiguity is resolved below purely from the reloaded manifest
        # state -- a connector-level exception never causes a retry here.
        pass

    _, manifest = load_manifest(manifest_path)
    attempts, state, draft_id = _review_state(manifest)

    if state == "DRAFT_CREATED" and draft_id:
        preview_payload = build_story_preview_payload(candidate, spec, manifest, draft_id)
        preview_delivery = None
        if telegram_sender is not None:
            try:
                preview_delivery = telegram_sender.send(preview_payload)
            except Exception as e:
                preview_delivery = {"status": "FAILED", "error": str(e)}

        return _result(
            "DRAFT_CREATED",
            "Story review draft created.",
            manifest_id=manifest_id,
            story_version_id=story_version_id,
            review_post_id=draft_id,
            manifest_path=workspace_relative(manifest_path),
            preview_payload=preview_payload,
            preview_delivery=preview_delivery,
        )

    if attempts == 0 and state == "NOT_CREATED":
        # Preflight validation blocked before the create attempt was
        # consumed: a clean, non-ambiguous failure. Safe to retry later.
        return _result(
            "REVIEW_DRAFT_PREFLIGHT_BLOCKED",
            "Review draft preflight validation blocked before create attempt.",
            manifest_id=manifest_id,
            story_version_id=story_version_id,
            manifest_path=workspace_relative(manifest_path),
        )

    # attempts >= 1 and state in {CREATE_IN_FLIGHT, REVIEW_UNKNOWN, ...}:
    # the attempt is consumed and the outcome is unconfirmed. Never retry.
    return _result(
        "REVIEW_DRAFT_AMBIGUOUS",
        f"Review draft outcome is ambiguous (attempts={attempts}, state={state}). "
        "Manual reconciliation required; no automatic retry.",
        manifest_id=manifest_id,
        story_version_id=story_version_id,
        manifest_path=workspace_relative(manifest_path),
    )


# ---------------------------------------------------------------------------
# Revision -- composed from the same core, not a second state machine.
# ---------------------------------------------------------------------------


def build_revision_candidate(
    original_candidate: dict[str, Any],
    *,
    operator_instruction: str,
    parent_manifest_id: str,
    parent_review_post_id: str,
) -> dict[str, Any]:
    """Derive a revision candidate from an approved candidate + an exact
    operator instruction. The evidence/claim scope is carried over
    unchanged; only the instruction is added to the writer context.

    Passing the result to run_story_pipeline() naturally produces a new
    story_version_id (different content => different hash), a new
    immutable manifest, fresh approval/publication-attempt fields, and at
    most one new review-draft attempt -- with no copied approval state and
    no mutation of the prior manifest/media/draft.
    """

    revised = dict(original_candidate)
    revised["revision_of"] = {
        "parent_manifest_id": parent_manifest_id,
        "parent_review_post_id": parent_review_post_id,
        "operator_instruction": operator_instruction,
    }
    revised["claims"] = list(original_candidate.get("claims", []))
    revised["limitations"] = list(original_candidate.get("limitations", []))
    revised["operator_revision_instruction"] = operator_instruction
    return revised
