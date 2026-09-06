#!/usr/bin/env python3
"""Lightweight review-only FEED/CAROUSEL main draft production core (#36).

Repository inspection for issue #36 confirmed: a Carousel renderer
(`social/tools/render_carousel_v2.py`), a Feed renderer
(`social/tools/render_texbrif_v2.py`) and a format-agnostic Production
Bridge/draft bridge (`nullone_bridge_common.py`, `nullone-draft-bridge.py`)
already exist and already pass FEED/CAROUSEL through generically -- but no
reusable *programmatic* pipeline wires candidate -> render -> immutable
manifest -> at most one review-draft attempt -> Telegram preview for a
main (FEED/CAROUSEL) target, the way `nullone_story_pipeline.py` already
does for Story (issue #33). This module is the smallest safe review-only
core closing that gap, mirroring only the safety concepts #33 already
proved: candidate PASS, deterministic request/version identity,
deterministic render, exact media dimensions, one immutable manifest, at
most one review-draft attempt, Telegram human review. No second editorial
system, no publication capability anywhere in this module.

This module never publishes, deletes, schedules or bypasses human
approval. It reuses `nullone_bridge_common` (manifest schema/validator),
`nullone_story_pipeline.NulloneDraftBridgeConnector` (the existing
format-agnostic real draft connector -- not duplicated here) and the
existing `render_texbrif_v2.py`/`render_carousel_v2.py` renderers
verbatim.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from nullone_bridge_common import (
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

SCHEMA = "nullone.main-draft-spec.v1"
CONTRACT_VERSION = "1.0.0"

ALLOWED_MAIN_FORMATS = frozenset({"FEED", "CAROUSEL"})

REQUIRED_CANDIDATE_FIELDS = (
    "candidate_id",
    "topic",
    "topic_cluster",
    "content_type",
    "format",
    "verification",
    "evidence_refs",
    "source_attribution",
)

CAROUSEL_SLIDE_TYPES = frozenset(
    {"cover", "stat", "explainer", "comparison", "limitation", "final"}
)

MAIN_PIPELINE_OUTCOMES = frozenset(
    {
        "DRAFT_CREATED",
        "PREVIEW_DELIVERY_FAILED",
        "CANDIDATE_NOT_ELIGIBLE",
        "VERIFIER_FAILED",
        "VERIFICATION_BLOCKED",
        "MAIN_SPEC_BLOCKED",
        "MAIN_SPEC_CONFLICT",
        "RENDER_FAILED",
        "MANIFEST_BLOCKED",
        "REVIEW_DRAFT_ALREADY_CONSUMED",
        "REVIEW_DRAFT_BLOCKED_BEFORE_ATTEMPT",
        "REVIEW_DRAFT_AMBIGUOUS",
    }
)


class MainPipelineError(RuntimeError):
    """Base class for all #36 main draft pipeline errors."""


class MainCandidateNotEligible(MainPipelineError):
    pass


class MainSpecBlocked(MainPipelineError):
    """A persisted/finalized main spec is missing, malformed or inconsistent."""


class MainSpecConflict(MainPipelineError):
    """Incoming same-request content conflicts with the persisted finalized spec.

    A retry is not a revision: the persisted spec (candidate admission PASS
    -> exact spec -> separate final verifier PASS -> immutable persistence)
    is authoritative for `main_request_id`. Changed wording under the same
    request never silently mints a new version.
    """


class MainRenderFailed(MainPipelineError):
    pass


class MainManifestBlocked(MainPipelineError):
    pass


@dataclass
class MainPipelineResult:
    outcome: str
    reason_code: str
    reason_text: str
    manifest_id: str | None = None
    main_request_id: str | None = None
    main_version_id: str | None = None
    review_post_id: str | None = None
    manifest_path: str | None = None
    main_spec_path: str | None = None
    preview_payload: dict[str, Any] | None = None
    preview_delivery: dict[str, Any] | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome not in MAIN_PIPELINE_OUTCOMES:
            raise MainPipelineError(f"Unknown main pipeline outcome: {self.outcome!r}")


def _result(outcome: str, reason_text: str, **kwargs: Any) -> MainPipelineResult:
    return MainPipelineResult(outcome=outcome, reason_code=outcome, reason_text=reason_text, **kwargs)


# ---------------------------------------------------------------------------
# Candidate input
# ---------------------------------------------------------------------------


def validate_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise MainCandidateNotEligible("candidate must be an object")

    missing = [f for f in REQUIRED_CANDIDATE_FIELDS if not candidate.get(f)]
    if missing:
        raise MainCandidateNotEligible(f"candidate missing required field(s): {missing}")

    if candidate["format"] not in ALLOWED_MAIN_FORMATS:
        raise MainCandidateNotEligible(f"Unsupported main format: {candidate['format']!r}")

    if not isinstance(candidate["evidence_refs"], list) or not candidate["evidence_refs"]:
        raise MainCandidateNotEligible("candidate.evidence_refs must be a non-empty list")

    if candidate["verification"] != "PASS":
        raise MainCandidateNotEligible(
            f"Candidate is not VERIFICATION: PASS: {candidate['verification']!r}"
        )

    fmt = candidate["format"]
    if fmt == "FEED":
        feed = candidate.get("feed")
        if not isinstance(feed, dict):
            raise MainCandidateNotEligible("FEED candidate requires a 'feed' object")
        for key in ("source_image", "kicker", "headline", "source_name"):
            if not isinstance(feed.get(key), str) or not feed[key].strip():
                raise MainCandidateNotEligible(f"feed.{key} is required")
    else:
        carousel = candidate.get("carousel")
        if not isinstance(carousel, dict):
            raise MainCandidateNotEligible("CAROUSEL candidate requires a 'carousel' object")
        slides = carousel.get("slides")
        if not isinstance(slides, list) or not (2 <= len(slides) <= 10):
            raise MainCandidateNotEligible("carousel.slides must contain 2-10 slides")
        for slide in slides:
            if not isinstance(slide, dict) or slide.get("type") not in CAROUSEL_SLIDE_TYPES:
                raise MainCandidateNotEligible(f"Unsupported/missing carousel slide type: {slide!r}")
        if not carousel.get("meaningful_multi_slide_value"):
            raise MainCandidateNotEligible(
                "carousel.meaningful_multi_slide_value must be true -- no one-slide Carousel"
            )

    caption = candidate.get("caption_text")
    if not isinstance(caption, str) or not caption.strip():
        raise MainCandidateNotEligible("candidate.caption_text is required")

    return candidate


# ---------------------------------------------------------------------------
# Request/version identity and immutable finalized spec
# ---------------------------------------------------------------------------


def _canonical_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_main_request_id(candidate: dict[str, Any]) -> str:
    """Identify one logical main (FEED/CAROUSEL) production request.

    Binds candidate identity, target format and any upstream request
    lineage (e.g. the #36 draft_set_id). Time and generated content never
    participate.
    """

    lineage = {
        "candidate_id": candidate["candidate_id"],
        "format": candidate["format"],
        "candidate_version": candidate.get("candidate_version"),
        "request_lineage": candidate.get("request_lineage"),
    }
    return f"main-request-{_canonical_hash(lineage)[:32]}"


_FINAL_MAIN_FIELDS = ("format", "caption_text", "feed", "carousel")


def compute_main_version_id(candidate: dict[str, Any], spec: dict[str, Any]) -> str:
    """Deterministic fingerprint of the exact finalized main content.

    Depends only on candidate identity/lineage, the request identity, and
    the *persisted spec's* finalized content fields -- never the raw,
    possibly-still-mutable caller-supplied candidate fields directly.
    Retry stability comes from reusing the persisted finalized spec (see
    `_load_or_persist_main_spec`) rather than recomputing from whatever
    content a retry happens to supply.
    """

    payload = {
        "main_request_id": spec.get("main_request_id") or compute_main_request_id(candidate),
        "candidate_id": candidate["candidate_id"],
        "candidate_version": candidate.get("candidate_version"),
        "request_lineage": candidate.get("request_lineage"),
        "spec": {field_name: spec.get(field_name) for field_name in _FINAL_MAIN_FIELDS},
    }
    return _canonical_hash(payload)


def _slug(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value[:40] or "candidate"


def _manifest_id_for_version(candidate_id: str, fmt: str, version_id: str) -> str:
    return f"main-{fmt.lower()}-{_slug(candidate_id)}-{version_id[:16]}"


def _main_request_lock_path(request_id: str) -> Path:
    return resolve_workspace_path(f"social/drafts/production/main/locks/{request_id}.lock")


@contextmanager
def _main_request_lock(request_id: str):
    lock_path = _main_request_lock_path(request_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Final main verification -- separate from candidate admission (#36
# hardening, accepted #34 rule: candidate verification PASS is only the
# admission gate; the exact finalized Feed/Carousel wording must
# independently pass exact verification before render/manifest/draft).
# Mirrors the #33 Story final-verifier boundary
# (`nullone_story_pipeline.StoryVerifier`) but matches the issue's own
# suggested shape: an object exposing `.verify(main_spec, evidence_refs)`.
# No real production verifier ships here (mirroring #33); a numeric-scope
# checker is one legitimate, deterministic, narrow example, and offline
# tests also exercise an unconditional fake.
# ---------------------------------------------------------------------------


class MainFinalVerifier(Protocol):
    def verify(self, main_spec: dict[str, Any], evidence_refs: list[str]) -> dict[str, Any]: ...


class _CallableMainVerifier:
    """Wraps a plain `(main_spec, evidence_refs) -> dict` function as a
    `MainFinalVerifier`, preserving a stable `__name__` for audit
    (`main_final_verification.verifier`)."""

    def __init__(self, fn, name: str | None = None) -> None:
        self._fn = fn
        self.__name__ = name or getattr(fn, "__name__", "main_verifier")

    def verify(self, main_spec: dict[str, Any], evidence_refs: list[str]) -> dict[str, Any]:
        return self._fn(main_spec, evidence_refs)


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")


def _numeric_scope_main_verifier_fn(main_spec: dict[str, Any], evidence_refs: list[str]) -> dict[str, Any]:
    """Deterministic, narrow verifier: every number in the exact finalized
    Feed/Carousel content must be verbatim-supported by the candidate's
    evidence text. Intentionally narrow (numbers only) -- a concrete
    example of the injectable final-verifier interface, not a substitute
    for real fact verification.
    """

    evidence_blob = " ".join(str(ref) for ref in evidence_refs)

    spec_text_parts = [str(main_spec.get("caption_text") or "")]
    feed = main_spec.get("feed") or {}
    if isinstance(feed, dict):
        spec_text_parts.extend(str(feed.get(k) or "") for k in ("headline", "stat", "kicker"))
    carousel = main_spec.get("carousel") or {}
    if isinstance(carousel, dict):
        for slide in carousel.get("slides") or []:
            if isinstance(slide, dict):
                spec_text_parts.append(json.dumps(slide, ensure_ascii=False))

    spec_text = " ".join(spec_text_parts)
    unsupported = sorted(
        {token for token in _NUMBER_RE.findall(spec_text) if token not in evidence_blob}
    )

    if unsupported:
        return {
            "status": "BLOCKED",
            "reason": f"Unsupported numeric claim(s) not found in evidence: {unsupported}",
        }
    return {"status": "PASS", "reason": "All numeric claims are evidence-supported."}


numeric_scope_main_verifier: MainFinalVerifier = _CallableMainVerifier(
    _numeric_scope_main_verifier_fn, name="numeric_scope_main_verifier"
)


def make_fake_main_verifier(status: str, reason: str = "fake verifier") -> MainFinalVerifier:
    """Test helper: an unconditional fake final verifier returning a fixed result."""

    def _verify(main_spec: dict[str, Any], evidence_refs: list[str]) -> dict[str, Any]:
        return {"status": status, "reason": reason}

    return _CallableMainVerifier(_verify, name=f"fake_main_verifier_{status.lower()}")


# ---------------------------------------------------------------------------
# Immutable finalized main spec (nullone.main-draft-spec.v1) -- persisted at
# social/drafts/production/main/specs/<main_request_id>.json BEFORE render/
# manifest/review work. Candidate admission PASS is only the entry gate;
# writer/editor/candidate state can never self-certify the exact finalized
# wording -- only this separately-verified, persisted spec is authoritative
# for `main_request_id`. A retry is not a revision: the same
# `main_request_id` reuses the exact persisted content/version; incoming
# same-request content that conflicts fails closed to MAIN_SPEC_CONFLICT
# rather than silently minting a new version or drifting wording.
# ---------------------------------------------------------------------------


def _main_spec_path(main_request_id: str) -> Path:
    return resolve_workspace_path(f"social/drafts/production/main/specs/{main_request_id}.json")


def _build_raw_main_spec(candidate: dict[str, Any], main_request_id: str) -> dict[str, Any]:
    return {
        "main_request_id": main_request_id,
        "candidate_id": candidate["candidate_id"],
        "candidate_version": candidate.get("candidate_version"),
        "request_lineage": candidate.get("request_lineage"),
        "format": candidate["format"],
        "caption_text": candidate["caption_text"],
        "feed": candidate.get("feed"),
        "carousel": candidate.get("carousel"),
        "evidence_refs": list(candidate["evidence_refs"]),
    }


def _spec_content_matches_candidate(spec: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return (
        spec.get("caption_text") == candidate.get("caption_text")
        and spec.get("feed") == candidate.get("feed")
        and spec.get("carousel") == candidate.get("carousel")
    )


def _validate_finalized_main_spec(
    spec: Any,
    candidate: dict[str, Any],
    main_request_id: str,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise MainSpecBlocked("Persisted main spec must be an object")
    if spec.get("schema") != SCHEMA or spec.get("contract_version") != CONTRACT_VERSION:
        raise MainSpecBlocked("Persisted main spec schema/version mismatch")
    if spec.get("main_request_id") != main_request_id:
        raise MainSpecBlocked("Persisted main request identity mismatch")
    if spec.get("candidate_id") != candidate["candidate_id"]:
        raise MainSpecBlocked("Persisted main candidate identity mismatch")
    if spec.get("candidate_version") != candidate.get("candidate_version"):
        raise MainSpecBlocked("Persisted main candidate version mismatch")
    if spec.get("request_lineage") != candidate.get("request_lineage"):
        raise MainSpecBlocked("Persisted main request lineage mismatch")
    if spec.get("format") != candidate["format"]:
        raise MainSpecBlocked("Persisted main format mismatch")
    if spec.get("evidence_refs") != list(candidate["evidence_refs"]):
        raise MainSpecBlocked("Persisted main evidence references do not match candidate")

    verification = spec.get("final_verification")
    if not isinstance(verification, dict) or verification.get("status") != "PASS":
        raise MainSpecBlocked("Persisted main final verification is not PASS")
    if not isinstance(verification.get("reason"), str):
        raise MainSpecBlocked("Persisted main verification reason is invalid")
    if not isinstance(verification.get("verifier"), str) or not verification["verifier"]:
        raise MainSpecBlocked("Persisted main verifier identity is invalid")
    if not isinstance(verification.get("checked_at"), str) or not verification["checked_at"]:
        raise MainSpecBlocked("Persisted main verification timestamp is invalid")

    expected_version = compute_main_version_id(candidate, spec)
    if spec.get("main_version_id") != expected_version:
        raise MainSpecBlocked("Persisted main spec content/version identity mismatch")
    return spec


def _load_or_persist_main_spec(
    candidate: dict[str, Any],
    main_request_id: str,
    *,
    final_verifier: MainFinalVerifier,
) -> tuple[dict[str, Any] | None, "MainPipelineResult | None"]:
    """Reuse a valid persisted spec before verifying, or finalize+persist exactly once.

    Retry invariant: if a valid finalized spec already exists for this
    exact `main_request_id`, it is loaded and reused BEFORE the incoming
    candidate's content is trusted. If the incoming candidate's caption/
    feed/carousel content differs from the persisted spec (retry drift),
    this fails closed to MAIN_SPEC_CONFLICT rather than silently accepting
    the changed wording or minting a new version.
    """

    spec_path = _main_spec_path(main_request_id)

    if spec_path.exists():
        try:
            raw = json.loads(spec_path.read_text(encoding="utf-8"))
            spec = _validate_finalized_main_spec(raw, candidate, main_request_id)
        except (OSError, json.JSONDecodeError, MainSpecBlocked) as e:
            return None, _result(
                "MAIN_SPEC_BLOCKED",
                f"Persisted main spec is invalid: {e}",
                main_request_id=main_request_id,
                main_spec_path=workspace_relative(spec_path),
            )
        if not _spec_content_matches_candidate(spec, candidate):
            return None, _result(
                "MAIN_SPEC_CONFLICT",
                "Incoming content for this main_request_id conflicts with the "
                "persisted finalized spec; a retry is not a revision.",
                main_request_id=main_request_id,
                main_version_id=spec["main_version_id"],
                main_spec_path=workspace_relative(spec_path),
            )
        return spec, None

    raw_spec = _build_raw_main_spec(candidate, main_request_id)
    try:
        verify_result = final_verifier.verify(dict(raw_spec), list(candidate["evidence_refs"]))
    except Exception as e:
        return None, _result(
            "VERIFIER_FAILED",
            "Main final verifier failed.",
            main_request_id=main_request_id,
            context={"error_type": type(e).__name__},
        )
    if not isinstance(verify_result, dict) or verify_result.get("status") not in {"PASS", "BLOCKED"}:
        return None, _result(
            "VERIFIER_FAILED",
            "Main final verifier returned an invalid result.",
            main_request_id=main_request_id,
        )
    if verify_result["status"] != "PASS":
        return None, _result(
            "VERIFICATION_BLOCKED",
            str(verify_result.get("reason") or "Final verification blocked"),
            main_request_id=main_request_id,
        )

    spec = dict(raw_spec)
    spec["schema"] = SCHEMA
    spec["contract_version"] = CONTRACT_VERSION
    spec["final_verification"] = {
        "status": "PASS",
        "reason": str(verify_result.get("reason") or ""),
        "verifier": getattr(final_verifier, "__name__", type(final_verifier).__name__),
        "checked_at": now_iso(),
    }
    spec["main_version_id"] = compute_main_version_id(candidate, spec)

    try:
        _validate_finalized_main_spec(spec, candidate, main_request_id)
        if spec_path.exists():
            raise MainSpecBlocked("Main spec appeared during locked creation")
        atomic_write_json(spec_path, spec)
    except (BridgeError, OSError, MainSpecBlocked) as e:
        return None, _result(
            "MAIN_SPEC_BLOCKED",
            f"Finalized main spec could not be persisted: {e}",
            main_request_id=main_request_id,
            main_version_id=spec["main_version_id"],
            main_spec_path=workspace_relative(spec_path),
        )
    return spec, None


# ---------------------------------------------------------------------------
# Renderer adapters -- reuse render_texbrif_v2.py / render_carousel_v2.py
# and Production Bridge media inspection verbatim. No new dimension/hash
# authority.
# ---------------------------------------------------------------------------

_FEED_RENDERER_SCRIPT = "social/tools/render_texbrif_v2.py"
_CAROUSEL_RENDERER_SCRIPT = "social/tools/render_carousel_v2.py"


def render_feed_asset(candidate: dict[str, Any], manifest_id: str) -> dict[str, Any]:
    feed = candidate["feed"]
    output_path = resolve_workspace_path(f"social/drafts/production/main/{manifest_id}.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    source_path = resolve_workspace_path(feed["source_image"])
    if not source_path.is_file():
        raise MainRenderFailed(f"Feed source image not found: {source_path}")

    renderer_path = resolve_workspace_path(_FEED_RENDERER_SCRIPT)
    argv = [
        sys.executable,
        str(renderer_path),
        "--source",
        str(source_path),
        "--kicker",
        feed["kicker"],
        "--headline",
        feed["headline"],
        "--source-name",
        feed["source_name"],
        "--output",
        str(output_path),
    ]
    if feed.get("stat"):
        argv.extend(["--stat", feed["stat"]])

    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise MainRenderFailed(f"render_texbrif_v2.py invocation failed: {e}") from e

    if completed.returncode != 0:
        raise MainRenderFailed(
            f"render_texbrif_v2.py exited {completed.returncode}: {completed.stderr.strip()}"
        )
    if not output_path.is_file():
        raise MainRenderFailed(f"render_texbrif_v2.py did not produce output: {output_path}")

    try:
        return inspect_media(output_path, "FEED")
    except BridgeError as e:
        raise MainRenderFailed(str(e)) from e


def render_carousel_assets(candidate: dict[str, Any], manifest_id: str) -> list[dict[str, Any]]:
    carousel = candidate["carousel"]
    out_dir = resolve_workspace_path(f"social/drafts/production/main/{manifest_id}-slides")
    out_dir.mkdir(parents=True, exist_ok=True)

    spec_path = resolve_workspace_path(
        f"social/drafts/production/main/{manifest_id}-carousel-spec.json"
    )
    spec_path.write_text(
        json.dumps({"slides": carousel["slides"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    renderer_path = resolve_workspace_path(_CAROUSEL_RENDERER_SCRIPT)
    argv = [
        sys.executable,
        str(renderer_path),
        "--spec",
        str(spec_path),
        "--output-dir",
        str(out_dir),
    ]

    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise MainRenderFailed(f"render_carousel_v2.py invocation failed: {e}") from e

    if completed.returncode != 0:
        raise MainRenderFailed(
            f"render_carousel_v2.py exited {completed.returncode}: {completed.stderr.strip()}"
        )

    slide_count = len(carousel["slides"])
    media: list[dict[str, Any]] = []
    for i in range(1, slide_count + 1):
        slide_path = out_dir / f"{i:02d}.png"
        if not slide_path.is_file():
            raise MainRenderFailed(f"render_carousel_v2.py did not produce slide: {slide_path}")
        try:
            media.append(inspect_media(slide_path, "CAROUSEL"))
        except BridgeError as e:
            raise MainRenderFailed(str(e)) from e

    return media


def render_main_asset(candidate: dict[str, Any], manifest_id: str) -> list[dict[str, Any]]:
    """Render the exact media set for this main candidate, deterministically.

    Returns an ordered media list (single item for FEED, 2-10 for
    CAROUSEL, order preserved). Raises MainRenderFailed on any renderer
    failure or dimension/hash validation failure. No fallback to altered
    dimensions.
    """

    if candidate["format"] == "FEED":
        return [render_feed_asset(candidate, manifest_id)]
    return render_carousel_assets(candidate, manifest_id)


# ---------------------------------------------------------------------------
# Manifest -- reuses nullone.production.v1 / nullone_bridge_common exactly.
# ---------------------------------------------------------------------------


def build_main_manifest(
    candidate: dict[str, Any],
    spec: dict[str, Any],
    media: list[dict[str, Any]],
    manifest_id: str,
    main_request_id: str,
    main_version_id: str,
    spec_path: Path,
) -> dict[str, Any]:
    """Build and persist a new immutable nullone.production.v1 main manifest.

    Deterministically binds to the persisted, separately-verified main
    spec (`main_spec.file`/`sha256`), never to raw caller-supplied
    candidate content -- `verification: PASS` is written here only because
    `spec["final_verification"]` already independently proved PASS for
    this exact finalized content (see `_load_or_persist_main_spec`);
    writer/editor/candidate admission state can never self-certify it.
    """

    manifest_path = resolve_workspace_path(f"social/ops/manifests/{manifest_id}.json")
    if manifest_path.exists():
        raise MainManifestBlocked(f"Manifest already exists: {manifest_path}")

    caption_path = resolve_workspace_path(
        f"social/drafts/production/main/{manifest_id}-caption.txt"
    )
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_text = spec["caption_text"]
    if not caption_text.endswith("\n"):
        caption_text += "\n"
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
        "format": spec["format"],
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
        "main_request_id": main_request_id,
        "main_version_id": main_version_id,
        "main_spec": {
            "file": workspace_relative(spec_path),
            "sha256": sha256_bytes(spec_path.read_bytes()),
        },
        "main_final_verification": spec["final_verification"],
    }

    try:
        validate_manifest(manifest)
    except BridgeError as e:
        raise MainManifestBlocked(str(e)) from e

    atomic_write_json(manifest_path, manifest)
    return manifest


# ---------------------------------------------------------------------------
# Review-draft connector -- Protocol only; the real connector is the
# existing format-agnostic nullone_story_pipeline.NulloneDraftBridgeConnector
# (delegates to nullone-draft-bridge.py). No publisher capability anywhere
# in this module: it never imports publish-bridge or publisher-run.
# ---------------------------------------------------------------------------


class DraftConnector(Protocol):
    def create_review_draft(self, manifest_path: Path) -> None: ...


def _review_state(manifest: dict[str, Any]) -> tuple[int, str, str | None]:
    review = manifest["review"]
    return review["create_attempts"], review["state"], review.get("zernio_draft_id")


def _review_is_untouched(manifest: dict[str, Any]) -> bool:
    attempts, state, draft_id = _review_state(manifest)
    return attempts == 0 and state == "NOT_CREATED" and not draft_id


# ---------------------------------------------------------------------------
# Telegram preview -- deterministic payload + narrow sender interface.
# ---------------------------------------------------------------------------


class TelegramPreviewSender(Protocol):
    def send(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def build_main_preview_payload(
    candidate: dict[str, Any],
    spec: dict[str, Any],
    manifest: dict[str, Any],
    review_post_id: str,
) -> dict[str, Any]:
    fmt = manifest["format"]
    return {
        "schema": "nullone.main-preview.v1",
        "brand": "NullOne",
        "format": fmt,
        "topic": candidate["topic"],
        "candidate_id": candidate["candidate_id"],
        "main_request_id": manifest["main_request_id"],
        "main_version_id": manifest["main_version_id"],
        "manifest_id": manifest["manifest_id"],
        "review_post_id": review_post_id,
        "media": [
            {
                "local_path": item["local_path"],
                "sha256": item["sha256"],
                "width": item["width"],
                "height": item["height"],
                "content_type": item["content_type"],
            }
            for item in manifest["media"]
        ],
        # Sourced from the persisted, separately-verified spec -- never the
        # raw caller-supplied candidate, which is authoritative only up to
        # admission and may legitimately differ on a conflicting retry.
        "caption_excerpt": spec["caption_text"][:120],
        "text": (
            f"📰 Yeni NullOne {fmt.title()} draft\n\n"
            f"Mövzu: {candidate['topic']}\n"
            f"Format: {fmt}\n"
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
# Core pipeline
# ---------------------------------------------------------------------------


def run_main_pipeline(
    candidate: dict[str, Any],
    *,
    final_verifier: MainFinalVerifier,
    draft_connector: DraftConnector,
    telegram_sender: TelegramPreviewSender | None = None,
) -> MainPipelineResult:
    """Produce at most one FEED/CAROUSEL review draft for one candidate.

    Candidate admission (`validate_candidate`, requiring
    `verification: PASS`) is only the entry gate -- it never self-certifies
    the exact finalized Feed caption/headline/stat/kicker or every factual
    Carousel slide. `final_verifier` independently proves PASS for the
    exact finalized content before it is persisted as the immutable
    `nullone.main-draft-spec.v1` and rendered; a `VERIFIER_FAILED`/
    `VERIFICATION_BLOCKED` outcome never renders, never writes a manifest,
    and never creates a review draft (see `_load_or_persist_main_spec`).

    Never publishes, deletes, defers to a future send time, or bypasses
    human approval. Ends at a Zernio review draft plus a Telegram preview
    payload.
    """

    try:
        validate_candidate(candidate)
    except MainCandidateNotEligible as e:
        return _result("CANDIDATE_NOT_ELIGIBLE", str(e))

    main_request_id = compute_main_request_id(candidate)
    spec_path = _main_spec_path(main_request_id)

    with _main_request_lock(main_request_id):
        spec, blocked = _load_or_persist_main_spec(candidate, main_request_id, final_verifier=final_verifier)
        if blocked is not None:
            return blocked
        assert spec is not None

        main_version_id = spec["main_version_id"]
        manifest_id = _manifest_id_for_version(candidate["candidate_id"], spec["format"], main_version_id)
        manifest_path = resolve_workspace_path(f"social/ops/manifests/{manifest_id}.json")

        if manifest_path.is_file():
            try:
                _, manifest = load_manifest(manifest_path)
            except BridgeError as e:
                return _result(
                    "MANIFEST_BLOCKED",
                    str(e),
                    manifest_id=manifest_id,
                    main_request_id=main_request_id,
                    main_version_id=main_version_id,
                    main_spec_path=workspace_relative(spec_path),
                )
            if (
                manifest.get("main_request_id") != main_request_id
                or manifest.get("main_version_id") != main_version_id
                or (manifest.get("main_spec") or {}).get("file") != workspace_relative(spec_path)
                or (manifest.get("main_spec") or {}).get("sha256") != sha256_bytes(spec_path.read_bytes())
            ):
                return _result(
                    "MANIFEST_BLOCKED",
                    "Existing manifest does not match the main request/version/spec.",
                    manifest_id=manifest_id,
                    main_request_id=main_request_id,
                    main_version_id=main_version_id,
                    main_spec_path=workspace_relative(spec_path),
                )
        else:
            try:
                media = render_main_asset(spec, manifest_id)
            except MainRenderFailed as e:
                return _result(
                    "RENDER_FAILED",
                    str(e),
                    main_request_id=main_request_id,
                    main_version_id=main_version_id,
                    main_spec_path=workspace_relative(spec_path),
                )
            try:
                manifest = build_main_manifest(
                    candidate, spec, media, manifest_id, main_request_id, main_version_id, spec_path
                )
            except (MainManifestBlocked, OSError) as e:
                return _result(
                    "MANIFEST_BLOCKED",
                    str(e),
                    main_request_id=main_request_id,
                    main_version_id=main_version_id,
                    manifest_id=manifest_id,
                    main_spec_path=workspace_relative(spec_path),
                )

        common = {
            "manifest_id": manifest_id,
            "main_request_id": main_request_id,
            "main_version_id": main_version_id,
            "main_spec_path": workspace_relative(spec_path),
            "manifest_path": workspace_relative(manifest_path),
        }

        if not _review_is_untouched(manifest):
            attempts, state, draft_id = _review_state(manifest)
            if state == "DRAFT_CREATED" and draft_id:
                return _result(
                    "REVIEW_DRAFT_ALREADY_CONSUMED",
                    "A review draft already exists for this main request.",
                    review_post_id=draft_id,
                    **common,
                )
            return _result(
                "REVIEW_DRAFT_ALREADY_CONSUMED",
                f"Review create attempt already consumed (attempts={attempts}, state={state}).",
                **common,
            )

        try:
            draft_connector.create_review_draft(manifest_path)
        except Exception:
            pass

        try:
            _, manifest = load_manifest(manifest_path)
        except BridgeError as e:
            return _result(
                "REVIEW_DRAFT_AMBIGUOUS",
                f"Review manifest could not be validated after connector call: {e}",
                **common,
            )
        attempts, state, draft_id = _review_state(manifest)

        if state == "DRAFT_CREATED" and draft_id:
            preview_payload = build_main_preview_payload(candidate, spec, manifest, draft_id)
            preview_delivery = None
            outcome = "DRAFT_CREATED"
            reason = "Main review draft created."
            if telegram_sender is not None:
                try:
                    sender_result = telegram_sender.send(preview_payload)
                    if not isinstance(sender_result, dict) or sender_result.get("status") != "SENT":
                        preview_delivery = {
                            "status": "FAILED",
                            "sender_status": (
                                sender_result.get("status") if isinstance(sender_result, dict) else None
                            ),
                            "error": (
                                str(sender_result.get("error") or "Sender did not return SENT")
                                if isinstance(sender_result, dict)
                                else "Sender returned an invalid result"
                            ),
                        }
                        outcome = "PREVIEW_DELIVERY_FAILED"
                        reason = "Main draft exists, but Telegram preview delivery failed."
                    else:
                        preview_delivery = dict(sender_result)
                except Exception as e:
                    preview_delivery = {
                        "status": "FAILED",
                        "error": "Telegram preview sender raised an exception.",
                        "error_type": type(e).__name__,
                    }
                    outcome = "PREVIEW_DELIVERY_FAILED"
                    reason = "Main draft exists, but Telegram preview delivery failed."
            return _result(
                outcome,
                reason,
                review_post_id=draft_id,
                preview_payload=preview_payload,
                preview_delivery=preview_delivery,
                **common,
            )

        if attempts == 0 and state == "NOT_CREATED":
            return _result(
                "REVIEW_DRAFT_BLOCKED_BEFORE_ATTEMPT",
                "Review draft connector was blocked before a create attempt was consumed.",
                **common,
            )

        return _result(
            "REVIEW_DRAFT_AMBIGUOUS",
            f"Review draft outcome is ambiguous (attempts={attempts}, state={state}). "
            "Manual reconciliation required; no automatic retry.",
            **common,
        )


# ---------------------------------------------------------------------------
# Self-test (offline, no fixtures, no network) -- registered in run_offline.py
# ---------------------------------------------------------------------------


def self_test() -> int:
    import shutil
    import tempfile
    import nullone_bridge_common as bridge_common

    with tempfile.TemporaryDirectory() as td:
        original_workspace = bridge_common.WORKSPACE
        bridge_common.WORKSPACE = Path(td)
        try:
            source = Path(td) / "social/source-assets/source.png"
            source.parent.mkdir(parents=True, exist_ok=True)
            from PIL import Image

            Image.new("RGB", (1600, 900), (10, 20, 30)).save(source, "PNG")

            real_workspace_root = Path(__file__).resolve().parents[3]
            tools_dir = Path(td) / "social/tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(
                real_workspace_root / "social/tools/render_texbrif_v2.py",
                tools_dir / "render_texbrif_v2.py",
            )

            candidate = {
                "candidate_id": "self-test-candidate",
                "topic": "Self-test topic",
                "topic_cluster": "self-test",
                "content_type": "NEWS",
                "format": "FEED",
                "verification": "PASS",
                "evidence_refs": ["Self-test evidence."],
                "source_attribution": "Self-test source",
                "caption_text": "Self-test caption.",
                "feed": {
                    "source_image": bridge_common.workspace_relative(source),
                    "kicker": "TEST",
                    "headline": "Self-test headline",
                    "source_name": "Self-test",
                },
            }

            try:
                validate_candidate(candidate)
            except MainCandidateNotEligible:
                raise AssertionError("valid FEED candidate rejected")

            request_id_1 = compute_main_request_id(candidate)
            request_id_2 = compute_main_request_id(candidate)
            assert request_id_1 == request_id_2, "non-deterministic request id"

            bad = dict(candidate)
            bad["format"] = "REEL"
            try:
                validate_candidate(bad)
                raise AssertionError("unsupported format was not rejected")
            except MainCandidateNotEligible:
                pass

            carousel_candidate = dict(candidate)
            carousel_candidate["format"] = "CAROUSEL"
            del carousel_candidate["feed"]
            carousel_candidate["carousel"] = {"slides": [{"type": "cover"}]}
            try:
                validate_candidate(carousel_candidate)
                raise AssertionError("one-slide Carousel was not rejected")
            except MainCandidateNotEligible:
                pass

            class _FakeConnector:
                def create_review_draft(self, manifest_path: Path) -> None:
                    _, m = load_manifest(manifest_path)
                    m["review"]["create_attempts"] = 1
                    m["review"]["state"] = "DRAFT_CREATED"
                    m["review"]["zernio_draft_id"] = "self-test-draft-1"
                    m["review"]["created_at"] = now_iso()
                    atomic_write_json(manifest_path, m)

            # Admission PASS + final verifier BLOCKED -> no render/manifest/draft.
            blocked = run_main_pipeline(
                candidate,
                final_verifier=make_fake_main_verifier("BLOCKED", "self-test block"),
                draft_connector=_FakeConnector(),
            )
            assert blocked.outcome == "VERIFICATION_BLOCKED", blocked.outcome
            assert not _main_spec_path(compute_main_request_id(candidate)).exists()

            # Final verifier PASS -> spec persisted, manifest written, draft created.
            ok = run_main_pipeline(
                candidate,
                final_verifier=make_fake_main_verifier("PASS"),
                draft_connector=_FakeConnector(),
            )
            assert ok.outcome == "DRAFT_CREATED", ok.outcome
            assert _main_spec_path(ok.main_request_id).exists()

            # Retry with drifted content for the SAME request_id -> fails
            # closed to MAIN_SPEC_CONFLICT; never silently mints a new
            # version from the changed wording.
            drifted = dict(candidate)
            drifted["caption_text"] = "Changed caption after the fact."
            conflict = run_main_pipeline(
                drifted,
                final_verifier=make_fake_main_verifier("PASS"),
                draft_connector=_FakeConnector(),
            )
            assert conflict.outcome == "MAIN_SPEC_CONFLICT", conflict.outcome
        finally:
            bridge_common.WORKSPACE = original_workspace

    print("MAIN_DRAFT_PIPELINE_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_PUBLISH_CAPABILITY=TRUE")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne Main (Feed/Carousel) Draft Pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
