#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from nullone_bridge_common import (
    BridgeError,
    atomic_write_json,
    load_manifest,
    now_iso,
    workspace_relative,
)
from nullone_publish_provider_factory import (
    build_production_publish_provider,
)
from nullone_secret_provider import SecretProvider
from nullone_state import (
    mark_queue_published_exact,
    record_publication_event,
)
from nullone_zernio_publish_adapter import (
    PROMOTE_AMBIGUOUS_REASON,
    PROMOTE_REJECTED_REASON,
    READBACK_FAILED_REASON,
    PublishAdapterError,
    PublishAmbiguousError,
    PublishConnectorUnauthorizedError,
    PublishConnectorUnavailableError,
    PublishPreflightBlockedError,
    PublishReadbackFailedError,
    ZernioPublishProvider,
    build_expected_snapshot,
    classify_readback_truth,
)

# Production provider construction seam (#90). The ONLY publication
# secret source is the in-memory provider installed here by the
# controller daemon at startup from the credential the plugin delivered
# over the authenticated private pipe. There is no environment fallback:
# with nothing installed the factory fails closed before any attempt.
# The deterministic controller calls execute_loaded IN-PROCESS; offline
# tests substitute a fake provider factory here without touching
# production wiring.
_installed_secret_provider: SecretProvider | None = None


def install_publish_secret_provider(
    provider: SecretProvider,
) -> None:
    """Install the single in-memory publication secret source.

    Called exactly once by the controller daemon at startup, and by
    offline tests with fakes. Anything without `get_required` is a
    programming defect (AttributeError on use, never a domain result).
    """
    global _installed_secret_provider
    _installed_secret_provider = provider


def provider_factory() -> ZernioPublishProvider:
    """Build the deterministic publisher from the installed secret source.

    Fails closed (BridgeError, attempts untouched) when controller
    startup did not deliver a credential -- notably for raw standalone
    CLI invocation, which can never publish.
    """
    if _installed_secret_provider is None:
        raise BridgeError(
            "Publication credential unavailable: controller startup "
            "did not deliver it over the private pipe"
        )
    return build_production_publish_provider(
        secret_provider=_installed_secret_provider
    )

# Fixed, generic failure text for a readback that proves the provider
# rejected the promoted post. Never echoes response bodies.
READBACK_PROVES_FAILED_REASON = (
    "Zernio reports the publication failed; retry forbidden."
)


def require_final_authorization(m: dict) -> None:
    review = m["review"]
    approval = m["approval"]
    pub = m["publication"]

    if review["state"] != "DRAFT_CREATED":
        raise BridgeError(
            "Review draft is not in DRAFT_CREATED state"
        )

    if not review.get("zernio_draft_id"):
        raise BridgeError(
            "Review draft ID missing"
        )

    if approval.get("first_stage") is not True:
        raise BridgeError(
            "First-stage human approval missing"
        )

    if approval.get("final_publish") is not True:
        raise BridgeError(
            "Final human publish authorization missing"
        )

    if approval.get("source") != "texbrif-approval":
        raise BridgeError(
            "Authorization source mismatch"
        )

    if approval.get("operator") != "Rauf":
        raise BridgeError(
            "Authorization operator mismatch"
        )

    if (
        approval.get("human_confirmation")
        != "two_step"
    ):
        raise BridgeError(
            "Two-step authorization proof missing"
        )

    if pub.get("attempts") != 0:
        raise BridgeError(
            "Publication attempt already consumed"
        )

    if pub.get("state") != "NOT_REQUESTED":
        raise BridgeError(
            f"Publication state is {pub.get('state')}"
        )

    for item in m["media"]:
        if not item.get("public_url"):
            raise BridgeError(
                "Approved media missing public URL"
            )


def _platform_post_url_from(body: Any) -> str | None:
    """Copy ONLY the documented platformPostUrl string when present.

    Never fabricate: anything absent, empty, or non-string becomes None.
    """
    if not isinstance(body, dict):
        return None
    post = body.get("post")
    if not isinstance(post, dict):
        return None
    url = post.get("platformPostUrl")
    if isinstance(url, str) and url:
        return url
    return None


def execute_loaded(manifest_path: Path, m: dict) -> int:
    """Deterministic publication core, importable for in-process invocation.

    The #89 controller calls this IN-PROCESS while holding review_post_lock,
    so there is no parent/child process boundary between the authorization
    decision and the attempts=1 ownership transition. This function itself
    never acquires review_post_lock; the caller owns the lock exactly once.

    Consequential path (#90), zero model involvement after the final human
    click: local authorization gate -> remote-draft read-only preflight
    against the approved manifest -> attempts=1 + PUBLISH_IN_FLIGHT
    persisted BEFORE the single network write -> exactly ONE PUT
    /v1/posts/{reviewPostId} with the minimal promotion payload ->
    read-only readback that clarifies truth but can never authorize
    another PUT. Ambiguity -> UNKNOWN, no retry, ever.
    """
    require_final_authorization(m)

    # Exact remote-draft expectations derived from the approved immutable
    # manifest. No network yet; failure blocks with attempts untouched.
    try:
        expected = build_expected_snapshot(m)
    except PublishPreflightBlockedError as exc:
        raise BridgeError(
            "Approved manifest cannot supply exact publication expectations"
        ) from exc

    post_id = expected["post_id"]

    # Resolve the publication credential behind the reviewed secret
    # boundary (controller process memory only). Absent credential fails
    # BEFORE any attempt is consumed.
    try:
        provider = provider_factory()
    except (
        PublishConnectorUnauthorizedError,
        PublishConnectorUnavailableError,
    ) as exc:
        raise BridgeError(str(exc)) from None

    # Read-only remote draft preflight BEFORE publication.attempts
    # becomes 1. Any contradiction blocks with attempts 0 and zero PUT.
    # The remote draft is never modified to make it match.
    try:
        provider.preflight(post_id, expected)
    except PublishPreflightBlockedError as exc:
        raise BridgeError(
            "Final remote-draft publication preflight failed; "
            "fresh human confirmation required after remediation"
        ) from exc
    except (
        PublishConnectorUnauthorizedError,
        PublishConnectorUnavailableError,
    ) as exc:
        raise BridgeError(str(exc)) from None
    except PublishAdapterError as exc:
        # Any other transport-level deviation before the attempt
        # (redirect refused, oversize body, unexpected shape) blocks
        # with attempts untouched.
        raise BridgeError(
            "Final remote-draft publication preflight failed; "
            "fresh human confirmation required after remediation"
        ) from exc

    # Critical idempotency boundary.
    # From here on publication MUST NEVER be automatically retried.
    m["publication"]["attempts"] = 1
    m["publication"]["state"] = "PUBLISH_IN_FLIGHT"
    m["publication"]["last_checked_at"] = now_iso()

    atomic_write_json(
        manifest_path,
        m,
    )

    # Exactly ONE consequential PUT. The provider performs no retry; any
    # ambiguity surfaces as PublishAmbiguousError with the attempt
    # already consumed.
    try:
        disposition, _put_status, put_body = provider.promote_once(post_id)
    except PublishAmbiguousError:
        disposition, _put_status, put_body = ("AMBIGUOUS", 0, None)
    except Exception:
        _persist_unknown(
            manifest_path,
            m,
            PROMOTE_AMBIGUOUS_REASON,
        )
        raise BridgeError(
            "Publication result ambiguous; retry forbidden"
        ) from None

    put_post_url = _platform_post_url_from(put_body)

    if disposition == "REJECTED":
        # Documented definite request rejection: terminal FAILED. The
        # readback below can still adopt a truer state if the provider
        # contradicts the rejection, but it can never authorize a PUT.
        provisional = "FAILED"
    elif disposition == "NEEDS_READBACK":
        provisional = "PENDING_READBACK"
        # The consequential write was issued. Record the authoritative
        # event BEFORE readback so even a later readback failure cannot
        # allow a duplicate publication.
        m["publication"]["live_zernio_post_id"] = post_id
        atomic_write_json(manifest_path, m)
        record_publication_event(m, "PUBLISH_ACCEPTED")
    else:
        provisional = "UNKNOWN"

    # Read-only clarification AFTER the one PUT. Readback classifies
    # truth but NEVER authorizes another PUT.
    try:
        truth = provider.readback(post_id, expected)
    except Exception:
        if provisional == "FAILED":
            _persist_state(
                manifest_path,
                m,
                state="FAILED",
                error=PROMOTE_REJECTED_REASON,
                permalink=put_post_url,
                live_post_id=post_id,
            )
            record_publication_event(m, "FAILED")
            return 0
        if provisional == "PENDING_READBACK":
            _persist_state(
                manifest_path,
                m,
                state="READBACK_FAILED",
                error=READBACK_FAILED_REASON,
                permalink=put_post_url,
                live_post_id=post_id,
            )
            record_publication_event(m, "READBACK_FAILED")
            return 0
        _persist_unknown(manifest_path, m, PROMOTE_AMBIGUOUS_REASON)
        raise BridgeError(
            "Publication result ambiguous; retry forbidden"
        ) from None

    permalink = truth.get("platform_post_url") or put_post_url
    classified = classify_readback_truth(truth)
    if provisional == "FAILED" and classified not in (
        "PUBLISHED",
        "PUBLISHING",
    ):
        # A documented definite rejection stands: a readback that merely
        # confirms the un-published draft (or any other non-delivery
        # state) is consistent with the rejection, not a reason to
        # soften it. Only proven delivery overrides the rejection --
        # and even then no second PUT is ever issued.
        final_state = "FAILED"
        error: str | None = PROMOTE_REJECTED_REASON
    else:
        final_state = classified
        if final_state == "FAILED":
            error = READBACK_PROVES_FAILED_REASON
        else:
            error = None

    _persist_state(
        manifest_path,
        m,
        state=final_state,
        error=error,
        permalink=permalink,
        live_post_id=post_id,
    )

    record_publication_event(m, final_state)

    if final_state == "PUBLISHED":
        mark_queue_published_exact(
            topic=m["topic"],
            review_post_id=m["review"]["zernio_draft_id"],
            live_post_id=m["publication"]["live_zernio_post_id"],
            platform_post_id=m["publication"]["platform_post_id"],
            permalink=m["publication"]["permalink"],
        )

    print("PUBLISH_BRIDGE=COMPLETE")
    print(
        f"MANIFEST={workspace_relative(manifest_path)}"
    )
    print(
        f"LIVE_ZERNIO_POST_ID={post_id}"
    )
    print(
        f"PUBLICATION_STATE={final_state}"
    )

    if m["publication"]["platform_post_id"]:
        print(
            "PLATFORM_POST_ID="
            + m["publication"]["platform_post_id"]
        )

    if m["publication"]["permalink"]:
        print(
            "PERMALINK="
            + m["publication"]["permalink"]
        )

    return 0


def _persist_state(
    manifest_path: Path,
    m: dict,
    *,
    state: str,
    error: str | None,
    permalink: str | None,
    live_post_id: str,
) -> None:
    """Persist a terminal publication state.

    Metadata rule: live_zernio_post_id is the promoted review post (the
    PUT promotes the SAME post, it never mints a new one). permalink is
    copied ONLY from a documented platformPostUrl string when actually
    present. platform_post_id is never fabricated and stays empty unless
    a documented provider field supplies it -- no such field exists in
    the current contract, so it is always None here.
    """
    m["publication"]["live_zernio_post_id"] = live_post_id
    m["publication"]["state"] = state
    m["publication"]["platform_post_id"] = None
    m["publication"]["permalink"] = permalink
    m["publication"]["last_checked_at"] = now_iso()
    m["publication"]["error"] = error

    atomic_write_json(
        manifest_path,
        m,
    )


def _persist_unknown(
    manifest_path: Path,
    m: dict,
    reason: str,
) -> None:
    m["publication"]["state"] = "UNKNOWN"
    m["publication"]["error"] = reason
    m["publication"]["last_checked_at"] = now_iso()

    atomic_write_json(
        manifest_path,
        m,
    )


def execute(manifest_arg: str) -> int:
    """Thin CLI compatibility wrapper: load, then run the in-process core."""
    manifest_path, m = load_manifest(
        manifest_arg
    )

    return execute_loaded(
        manifest_path,
        m,
    )


def self_test() -> int:
    good = {
        "review": {
            "state": "DRAFT_CREATED",
            "zernio_draft_id": "test-review",
        },
        "approval": {
            "first_stage": True,
            "final_publish": True,
            "source": "texbrif-approval",
            "operator": "Rauf",
            "human_confirmation": "two_step",
        },
        "publication": {
            "attempts": 0,
            "state": "NOT_REQUESTED",
        },
        "media": [
            {
                "public_url":
                "https://example.invalid/media.png"
            }
        ],
    }

    require_final_authorization(good)

    bad = json.loads(json.dumps(good))
    bad["publication"]["attempts"] = 1

    try:
        require_final_authorization(bad)
    except BridgeError:
        pass
    else:
        raise BridgeError(
            "Idempotency self-test failed"
        )

    bad = json.loads(json.dumps(good))
    bad["approval"]["final_publish"] = False

    try:
        require_final_authorization(bad)
    except BridgeError:
        pass
    else:
        raise BridgeError(
            "Human authorization self-test failed"
        )

    # Deterministic transport seam: the minimal promotion payload is
    # exact, and the bridge module carries no model transport.
    from nullone_zernio_publish_adapter import build_promote_payload

    assert build_promote_payload() == {
        "isDraft": False,
        "publishNow": True,
    }

    print("PUBLISH_BRIDGE_SELF_TEST=PASS")
    print("IDEMPOTENCY_GUARD=PASS")
    print("TWO_STEP_AUTH_GUARD=PASS")
    print("DETERMINISTIC_TRANSPORT=PASS")
    print("EXTERNAL_CALLS=0")

    return 0


def local_check(manifest_arg: str) -> int:
    path, m = load_manifest(
        manifest_arg
    )

    require_final_authorization(m)

    print("PUBLISH_LOCAL_CHECK=PASS")
    print(
        f"MANIFEST={workspace_relative(path)}"
    )
    print(
        "REVIEW_POST_ID="
        + m["review"]["zernio_draft_id"]
    )
    print("FINAL_AUTHORIZATION=PASS")
    print("PUBLICATION_ATTEMPTS=0")

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
