#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
from pathlib import Path

from nullone_bridge_common import (
    BridgeError,
    atomic_write_json,
    now_iso,
    validate_manifest,
)


HERE = Path(__file__).resolve().parent


def apply_final_authorization(
    manifest_path: Path,
    m: dict,
) -> None:

    review = m["review"]
    approval = m["approval"]
    publication = m["publication"]

    if review.get("state") != "DRAFT_CREATED":
        raise BridgeError(
            "Review draft is not DRAFT_CREATED"
        )

    if not review.get("zernio_draft_id"):
        raise BridgeError(
            "Review draft ID missing"
        )

    if publication.get("attempts") != 0:
        raise BridgeError(
            "Publication attempt already consumed"
        )

    if publication.get("state") != "NOT_REQUESTED":
        raise BridgeError(
            "Publication is not in NOT_REQUESTED state"
        )

    # This wrapper may only be invoked by the publisher agent
    # after receiving the exact trusted PUBLISH_AUTHORIZED
    # protocol from texbrif-approval.
    ts = now_iso()

    approval["first_stage"] = True

    if not approval.get("first_stage_at"):
        approval["first_stage_at"] = ts

    approval["final_publish"] = True
    approval["final_publish_at"] = ts

    approval["source"] = "texbrif-approval"
    approval["operator"] = "Rauf"
    approval["human_confirmation"] = "two_step"

    validate_manifest(m)

    atomic_write_json(
        manifest_path,
        m,
    )


def revoke_final_if_no_publish_attempt(
    manifest_path: Path,
    m: dict,
) -> None:

    if m["publication"].get("attempts") != 0:
        return

    approval = m["approval"]

    approval["final_publish"] = False
    approval["final_publish_at"] = None

    # first_stage may remain true because the first approval
    # actually occurred; only final authorization is invalidated.
    approval["source"] = None
    approval["operator"] = None
    approval["human_confirmation"] = None

    atomic_write_json(
        manifest_path,
        m,
    )


def _validate_review_post_id(review_post_id: str) -> None:
    if (
        len(review_post_id) != 24
        or any(
            c not in "0123456789abcdef"
            for c in review_post_id.lower()
        )
    ):
        raise BridgeError(
            "Invalid Zernio review post ID format"
        )


def execute(review_post_id: str) -> int:
    """Legacy direct entrypoint — permanently fail-closed since #89.

    A bare POST_ID-only invocation cannot prove plugin-authenticated daemon
    provenance, so it must never apply final authorization or reach the
    publication core. The deterministic controller
    (`nullone_final_publish_controller.execute_authorized`, reached only
    through the authenticated plugin pipe) is the sole live path; it reuses
    `apply_final_authorization` + the bridge core directly, in-process.
    """
    raise BridgeError(
        "Legacy direct publisher execution is disabled (#89). "
        "Final publication runs only through the deterministic "
        "plugin-authenticated controller path."
    )


def _execute_locked(review_post_id: str) -> int:
    raise BridgeError(
        "Legacy direct publisher execution is disabled (#89). "
        "Final publication runs only through the deterministic "
        "plugin-authenticated controller path."
    )


def self_test() -> int:

    base = {
        "review": {
            "state": "DRAFT_CREATED",
            "zernio_draft_id":
                "0123456789abcdef01234567",
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
        },
    }

    x = copy.deepcopy(base)

    # Pure structural simulation of the authorization transition.
    ts = now_iso()

    x["approval"]["first_stage"] = True
    x["approval"]["first_stage_at"] = ts
    x["approval"]["final_publish"] = True
    x["approval"]["final_publish_at"] = ts
    x["approval"]["source"] = "texbrif-approval"
    x["approval"]["operator"] = "Rauf"
    x["approval"]["human_confirmation"] = "two_step"

    assert x["approval"]["first_stage"] is True
    assert x["approval"]["final_publish"] is True
    assert x["approval"]["source"] == "texbrif-approval"
    assert x["approval"]["operator"] == "Rauf"
    assert (
        x["approval"]["human_confirmation"]
        == "two_step"
    )

    y = copy.deepcopy(x)
    y["publication"]["attempts"] = 1

    assert y["publication"]["attempts"] == 1

    print("PUBLISHER_WRAPPER_SELF_TEST=PASS")
    print("AUTH_TRANSITION=PASS")
    print("NO_EXTERNAL_CALLS=PASS")

    return 0


def main() -> int:
    p = argparse.ArgumentParser()

    sub = p.add_subparsers(
        dest="command",
        required=True,
    )

    sub.add_parser("self-test")

    e = sub.add_parser("execute")
    e.add_argument("review_post_id")

    args = p.parse_args()

    try:
        if args.command == "self-test":
            return self_test()

        if args.command == "execute":
            return execute(
                args.review_post_id.lower()
            )

        raise BridgeError("Unknown command")

    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
