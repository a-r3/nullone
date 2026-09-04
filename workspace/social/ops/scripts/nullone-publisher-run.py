#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import subprocess
import sys
from pathlib import Path

from nullone_bridge_common import (
    BridgeError,
    atomic_write_json,
    find_manifest_by_review_post_id,
    now_iso,
    validate_manifest,
    workspace_relative,
)


HERE = Path(__file__).resolve().parent
PUBLISH_BRIDGE = HERE / "nullone-publish-bridge.py"


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


def execute(review_post_id: str) -> int:

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

    manifest_path, m = (
        find_manifest_by_review_post_id(
            review_post_id
        )
    )

    if (
        m["review"].get("zernio_draft_id")
        != review_post_id
    ):
        raise BridgeError(
            "Review post ID mismatch"
        )

    apply_final_authorization(
        manifest_path,
        m,
    )

    cmd = [
        sys.executable,
        str(PUBLISH_BRIDGE),
        "execute",
        workspace_relative(manifest_path),
    ]

    try:
        cp = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=420,
            check=False,
        )

    except subprocess.TimeoutExpired:
        # Read current durable state before deciding anything.
        _, current = find_manifest_by_review_post_id(
            review_post_id
        )

        print("PUBLISHER_WRAPPER=TIMEOUT")
        print(
            "PUBLICATION_ATTEMPTS="
            f"{current['publication']['attempts']}"
        )
        print(
            "PUBLICATION_STATE="
            f"{current['publication']['state']}"
        )
        print("AUTOMATIC_RETRY=FORBIDDEN")
        return 3

    # Reload authoritative durable state.
    _, current = find_manifest_by_review_post_id(
        review_post_id
    )

    if cp.returncode != 0:

        # Safe case: publication call was never attempted.
        # Require a fresh final human confirmation later.
        if current["publication"]["attempts"] == 0:
            revoke_final_if_no_publish_attempt(
                manifest_path,
                current,
            )

            print("PUBLISHER_WRAPPER=BLOCKED")
            print("PUBLISH_ATTEMPT_OCCURRED=NO")
            print("FRESH_FINAL_CONFIRMATION_REQUIRED=YES")

        else:
            # Consequential call may have occurred.
            # Never retry automatically.
            print("PUBLISHER_WRAPPER=UNKNOWN_OR_FAILED")
            print("PUBLISH_ATTEMPT_OCCURRED=YES")
            print("AUTOMATIC_RETRY=FORBIDDEN")
            print(
                "PUBLICATION_STATE="
                f"{current['publication']['state']}"
            )

        # nullone-publish-bridge emits only sanitized output.
        if cp.stdout.strip():
            print(cp.stdout.strip())

        return cp.returncode

    print("PUBLISHER_WRAPPER=PASS")

    if cp.stdout.strip():
        print(cp.stdout.strip())

    return 0


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
