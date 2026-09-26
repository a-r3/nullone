#!/usr/bin/env python3
"""GET-only post-publish reconciliation (issue #169).

Background: `nullone-publish-bridge.py` performs exactly ONE consequential
PUT, then exactly ONE read-only GET readback, then settles whatever
`classify_readback_truth` says -- including `PUBLISHING`, which is a
terminal, settled receipt state by design (see
`nullone_final_publish_controller._settle_from_manifest`). This is
deliberate: it prevents a second PUT under any circumstance. But Zernio's
actual platform delivery is asynchronous, so a readback that lands before
delivery finishes settles PUBLISHING forever -- nothing in the codebase
ever re-checks. Confirmed real instance: manifest
`2026-09-25-amazon-seller-assistant-claude` (Zernio id
`6ab5c068f1c2af7c30f89020`), frozen at PUBLISHING while Zernio's own
status is `published`.

This module is the missing later check, and ONLY that:

- structurally incapable of publishing -- it only ever constructs a
  `ZernioPublishReadOnlyReconciler` (issue #169,
  `nullone_zernio_publish_adapter.py`), which has no PUT/POST-capable
  method at all, via a DISTINCT credential
  (`zernio.publish.reconcile.bearer`) from the daemon-owned publish
  bearer;
- reuses `build_expected_snapshot` / `classify_readback_truth` /
  `_persist_state` / `record_publication_event` /
  `mark_queue_published_exact` verbatim -- the SAME functions the
  primary bridge already uses for the SAME purposes, so there is no
  second parallel definition of "published";
- narrowly eligible: `publication.attempts == 1` AND
  `publication.state == "PUBLISHING"` only (see `RECONCILABLE_STATES`
  docstring below for why READBACK_FAILED/CHECK_REQUIRED are not
  included yet);
- idempotent: a manifest already `PUBLISHED` short-circuits to
  `NOOP_ALREADY_PUBLISHED` before any external call;
- lock-disciplined: acquires the existing `review_post_lock` and
  re-reads the manifest under the lock before ever acting on it.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from nullone_bridge_common import (  # noqa: E402
    BridgeError,
    find_manifest_by_review_post_id,
    workspace_relative,
)
from nullone_publish_reconcile_provider_factory import (  # noqa: E402
    build_production_reconcile_provider,
)
from nullone_state import mark_queue_published_exact, record_publication_event  # noqa: E402
from nullone_story_supersession import review_post_lock  # noqa: E402
from nullone_zernio_publish_adapter import (  # noqa: E402
    PublishConnectorUnauthorizedError,
    PublishConnectorUnavailableError,
    PublishPreflightBlockedError,
    PublishReadbackFailedError,
    ZernioPublishReadOnlyReconciler,
    build_expected_snapshot,
    classify_readback_truth,
)

import nullone_publish_receipt as receipts  # noqa: E402

# Fixed, generic reason text mirroring the primary bridge's own constant
# (nullone-publish-bridge.py: READBACK_PROVES_FAILED_REASON). Duplicated
# as a literal string rather than imported because the source file uses
# dashes and is not import-statement-addressable; `_bridge_module()`
# below loads it by file location when the real persistence helper is
# needed, and this string is asserted equal to the source constant in
# the offline self-test so the two can never silently drift.
READBACK_PROVES_FAILED_REASON = (
    "Zernio reports the publication failed; retry forbidden."
)

# Narrowest safe eligibility scope (issue #169, audited in the PR body).
#
# `READBACK_FAILED` and `CHECK_REQUIRED` are structurally similar to
# `PUBLISHING` in the one way that matters for safety: all three are
# terminal-by-design local states that arose from exactly one PUT having
# already happened, and a reconciliation GET can never turn any of them
# into a second PUT regardless of which one it started from. But each
# has DIFFERENT receipt-terminal-state semantics
# (SETTLED_READBACK_FAILED / SETTLED_CHECK_REQUIRED are distinct receipt
# states from SETTLED_PUBLISHING) and no real-world instance exists yet
# to validate against -- so this PR does not broaden past the one
# confirmed case. Broadening is a follow-up issue, not a same-PR
# speculative change.
RECONCILABLE_STATES = frozenset({"PUBLISHING"})

# Explicit non-goals, named so a future edit has to consciously remove
# them rather than accidentally fall through:
NEVER_RECONCILE_STATES = frozenset(
    {
        "NOT_REQUESTED",
        "PUBLISH_IN_FLIGHT",
        "PUBLISHED",
        "FAILED",
        "UNKNOWN",
        "READBACK_FAILED",
        "CHECK_REQUIRED",
    }
)

_SCRIPT_MODULES: dict[str, Any] = {}


def _load_script(name: str, filename: str) -> Any:
    """Load a dash-named sibling script by file location (same technique
    as `nullone_final_publish_controller._load_script`)."""
    if name not in _SCRIPT_MODULES:
        spec = importlib.util.spec_from_file_location(name, _HERE / filename)
        if spec is None or spec.loader is None:
            raise BridgeError(f"Cannot load {filename}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        _SCRIPT_MODULES[name] = module
    return _SCRIPT_MODULES[name]


def _bridge_module() -> Any:
    return _load_script(
        "nullone_publish_reconcile_bridge", "nullone-publish-bridge.py"
    )


def workspace_root() -> Path:
    """Resolve the served workspace, honoring the same override the rest
    of this codebase's dynamic-workspace tests use."""
    override = os.environ.get("NULLONE_WORKSPACE")
    if override:
        return Path(override)
    import nullone_bridge_common as common

    return Path(common.WORKSPACE)


def _converge_receipts(
    workspace: Path, review_post_id: str, final_state: str
) -> list[str]:
    """Best-effort receipt convergence for every receipt of this post.

    Never blocks or reverses the manifest-level result: the manifest is
    the authoritative settlement (same principle the daemon already
    applies -- attempts-based manifest truth always wins over receipt
    state). A missing receipt, an already-converged receipt, or a
    receipt in some other terminal state is reported, never raised.
    """
    converged: list[str] = []
    try:
        all_receipts = receipts.scan_receipts(workspace)
    except Exception:
        return converged

    for post_id, instance_id, record in all_receipts:
        if post_id != review_post_id:
            continue
        try:
            if final_state == "PUBLISHED":
                result = receipts.converge_publishing_to_published(
                    workspace,
                    review_post_id,
                    instance_id,
                    {"outcome": "PUBLISHED", "code": 0},
                )
            elif final_state == "FAILED":
                result = receipts.converge_publishing_to_failed(
                    workspace,
                    review_post_id,
                    instance_id,
                    {"outcome": "FAILED", "code": 0},
                )
            else:
                continue
        except BridgeError:
            continue
        if result is not None:
            converged.append(instance_id)
    return converged


def _reconcile_locked(
    review_post_id: str,
    provider_factory: Callable[[], ZernioPublishReadOnlyReconciler],
) -> dict[str, Any]:
    workspace = workspace_root()

    try:
        manifest_path, m = find_manifest_by_review_post_id(review_post_id)
    except BridgeError as exc:
        return {
            "outcome": "BLOCKED",
            "review_post_id": review_post_id,
            "reason": f"INVALID_MANIFEST: {exc}",
        }

    pub = m.get("publication", {})
    state = pub.get("state")
    attempts = pub.get("attempts", 0)

    if state == "PUBLISHED":
        return {
            "outcome": "NOOP_ALREADY_PUBLISHED",
            "review_post_id": review_post_id,
            "manifest_id": m.get("manifest_id"),
        }

    if not isinstance(attempts, int) or attempts != 1:
        return {
            "outcome": "BLOCKED",
            "review_post_id": review_post_id,
            "reason": f"INELIGIBLE_ATTEMPTS: attempts={attempts!r}",
        }

    if state not in RECONCILABLE_STATES:
        return {
            "outcome": "BLOCKED",
            "review_post_id": review_post_id,
            "reason": f"INELIGIBLE_STATE: state={state!r}",
        }

    try:
        expected = build_expected_snapshot(m)
    except PublishPreflightBlockedError as exc:
        return {
            "outcome": "BLOCKED",
            "review_post_id": review_post_id,
            "reason": f"CANNOT_DERIVE_EXPECTATIONS: {exc}",
        }

    post_id = expected["post_id"]

    try:
        provider = provider_factory()
    except (
        PublishConnectorUnauthorizedError,
        PublishConnectorUnavailableError,
    ) as exc:
        return {
            "outcome": "CHECK_REQUIRED",
            "review_post_id": review_post_id,
            "reason": f"CREDENTIAL: {exc}",
        }

    try:
        truth = provider.readback(post_id, expected)
    except (
        PublishConnectorUnauthorizedError,
        PublishReadbackFailedError,
    ) as exc:
        return {
            "outcome": "CHECK_REQUIRED",
            "review_post_id": review_post_id,
            "reason": f"READBACK: {exc}",
        }
    except Exception as exc:
        return {
            "outcome": "CHECK_REQUIRED",
            "review_post_id": review_post_id,
            "reason": f"READBACK_AMBIGUOUS: {exc}",
        }

    classified = classify_readback_truth(truth)
    permalink = truth.get("platform_post_url")

    if classified == "PUBLISHING":
        return {
            "outcome": "STILL_PENDING",
            "review_post_id": review_post_id,
            "manifest_id": m.get("manifest_id"),
        }

    if classified == "CHECK_REQUIRED":
        return {
            "outcome": "CHECK_REQUIRED",
            "review_post_id": review_post_id,
            "reason": "readback truth unprovable; never fabricated",
        }

    if classified == "FAILED":
        final_state = "FAILED"
        error: str | None = READBACK_PROVES_FAILED_REASON
    elif classified == "PUBLISHED":
        final_state = "PUBLISHED"
        error = None
    else:
        return {
            "outcome": "CHECK_REQUIRED",
            "review_post_id": review_post_id,
            "reason": f"unrecognized classification: {classified!r}",
        }

    bridge = _bridge_module()
    bridge._persist_state(
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

    converged_receipts = _converge_receipts(workspace, post_id, final_state)

    return {
        "outcome": f"RECONCILED_{final_state}",
        "review_post_id": post_id,
        "manifest_id": m.get("manifest_id"),
        "manifest_path": workspace_relative(manifest_path),
        "final_state": final_state,
        "converged_receipts": converged_receipts,
    }


def reconcile_one(
    review_post_id: str,
    *,
    provider_factory: Callable[[], ZernioPublishReadOnlyReconciler] | None = None,
) -> dict[str, Any]:
    """Reconcile exactly one review_post_id. Safe to call any number of
    times: eligibility gating + the manifest re-read under the lock make
    every call after the first a NOOP once truth is settled."""
    factory = provider_factory or build_production_reconcile_provider
    with review_post_lock(review_post_id):
        return _reconcile_locked(review_post_id, factory)


def scan_eligible(
    *,
    max_items: int = 5,
    min_stale_seconds: float = 900.0,
    provider_factory: Callable[[], ZernioPublishReadOnlyReconciler] | None = None,
) -> list[dict[str, Any]]:
    """Bounded scan over stale PUBLISHING manifests. NOT wired to any
    scheduler by this PR (see issue #169 / PR body section 10) -- calling
    this function is always an explicit, manual, bounded action.

    Deterministic bounds: `max_items` caps how many manifests this one
    call will touch; `min_stale_seconds` requires
    `publication.last_checked_at` to be at least this old (skips a
    manifest whose original readback just happened, giving Zernio's own
    async delivery a chance to finish first); ties broken oldest
    `last_checked_at` first (the longest-stuck item is reconciled first).
    """
    from datetime import datetime, timezone
    import json

    workspace = workspace_root()
    manifest_dir = workspace / "social/ops/manifests"

    candidates: list[tuple[float, str]] = []
    now = datetime.now(timezone.utc)

    for path in sorted(manifest_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        pub = data.get("publication", {})
        if pub.get("attempts") != 1 or pub.get("state") not in RECONCILABLE_STATES:
            continue
        review_post_id = data.get("review", {}).get("zernio_draft_id")
        if not review_post_id:
            continue
        last_checked = pub.get("last_checked_at")
        try:
            checked_at = datetime.fromisoformat(str(last_checked).replace("Z", "+00:00"))
        except Exception:
            continue
        age_seconds = (now - checked_at).total_seconds()
        if age_seconds < min_stale_seconds:
            continue
        candidates.append((age_seconds, review_post_id))

    candidates.sort(key=lambda pair: pair[0], reverse=True)  # oldest/most-stale first
    bounded = candidates[: max(0, int(max_items))]

    results = []
    for _age, review_post_id in bounded:
        results.append(
            reconcile_one(review_post_id, provider_factory=provider_factory)
        )
    return results


def _print_result(result: dict[str, Any]) -> None:
    for key, value in result.items():
        print(f"{key.upper()}={value}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GET-only post-publish reconciliation (issue #169)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("reconcile-one")
    one.add_argument("--review-post-id", required=True)

    scan = sub.add_parser("scan")
    scan.add_argument("--max-items", type=int, default=5)
    scan.add_argument("--min-stale-seconds", type=float, default=900.0)

    sub.add_parser("self-test")

    args = parser.parse_args()

    if args.command == "reconcile-one":
        result = reconcile_one(args.review_post_id)
        _print_result(result)
        return 0

    if args.command == "scan":
        results = scan_eligible(
            max_items=args.max_items, min_stale_seconds=args.min_stale_seconds
        )
        print(f"SCANNED={len(results)}")
        for result in results:
            _print_result(result)
        return 0

    if args.command == "self-test":
        return self_test()

    return 2


def self_test() -> int:
    """Offline self-test: no network, no real workspace, no credential."""
    bridge = _bridge_module()
    assert READBACK_PROVES_FAILED_REASON == bridge.READBACK_PROVES_FAILED_REASON

    assert RECONCILABLE_STATES == {"PUBLISHING"}
    assert "PUBLISHED" in NEVER_RECONCILE_STATES
    assert "NOT_REQUESTED" in NEVER_RECONCILE_STATES
    assert "PUBLISH_IN_FLIGHT" in NEVER_RECONCILE_STATES
    assert RECONCILABLE_STATES.isdisjoint(NEVER_RECONCILE_STATES)

    print("PUBLISH_RECONCILE_SELF_TEST=PASS")
    print("NO_NETWORK=TRUE")
    print("NO_MODEL_TRANSPORT=TRUE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
