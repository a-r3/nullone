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
        "MAIN_SPEC_BLOCKED",
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


def compute_main_version_id(candidate: dict[str, Any], request_id: str) -> str:
    payload = {
        "main_request_id": request_id,
        "candidate_id": candidate["candidate_id"],
        "format": candidate["format"],
        "caption_text": candidate["caption_text"],
        "feed": candidate.get("feed"),
        "carousel": candidate.get("carousel"),
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
    media: list[dict[str, Any]],
    manifest_id: str,
    main_request_id: str,
    main_version_id: str,
) -> dict[str, Any]:
    manifest_path = resolve_workspace_path(f"social/ops/manifests/{manifest_id}.json")
    if manifest_path.exists():
        raise MainManifestBlocked(f"Manifest already exists: {manifest_path}")

    caption_path = resolve_workspace_path(
        f"social/drafts/production/main/{manifest_id}-caption.txt"
    )
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_text = candidate["caption_text"]
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
        "format": candidate["format"],
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
        "caption_excerpt": candidate["caption_text"][:120],
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
    draft_connector: DraftConnector,
    telegram_sender: TelegramPreviewSender | None = None,
) -> MainPipelineResult:
    """Produce at most one FEED/CAROUSEL review draft for one candidate.

    Never publishes, deletes, defers to a future send time, or bypasses
    human approval. Ends at a Zernio review draft plus a Telegram preview
    payload.
    """

    try:
        validate_candidate(candidate)
    except MainCandidateNotEligible as e:
        return _result("CANDIDATE_NOT_ELIGIBLE", str(e))

    main_request_id = compute_main_request_id(candidate)
    main_version_id = compute_main_version_id(candidate, main_request_id)
    manifest_id = _manifest_id_for_version(candidate["candidate_id"], candidate["format"], main_version_id)
    manifest_path = resolve_workspace_path(f"social/ops/manifests/{manifest_id}.json")

    with _main_request_lock(main_request_id):
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
                )
            if (
                manifest.get("main_request_id") != main_request_id
                or manifest.get("main_version_id") != main_version_id
            ):
                return _result(
                    "MANIFEST_BLOCKED",
                    "Existing manifest does not match the main request/version.",
                    manifest_id=manifest_id,
                    main_request_id=main_request_id,
                    main_version_id=main_version_id,
                )
        else:
            try:
                media = render_main_asset(candidate, manifest_id)
            except MainRenderFailed as e:
                return _result(
                    "RENDER_FAILED",
                    str(e),
                    main_request_id=main_request_id,
                    main_version_id=main_version_id,
                )
            try:
                manifest = build_main_manifest(
                    candidate, media, manifest_id, main_request_id, main_version_id
                )
            except (MainManifestBlocked, OSError) as e:
                return _result(
                    "MANIFEST_BLOCKED",
                    str(e),
                    main_request_id=main_request_id,
                    main_version_id=main_version_id,
                    manifest_id=manifest_id,
                )

        common = {
            "manifest_id": manifest_id,
            "main_request_id": main_request_id,
            "main_version_id": main_version_id,
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
            preview_payload = build_main_preview_payload(candidate, manifest, draft_id)
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
