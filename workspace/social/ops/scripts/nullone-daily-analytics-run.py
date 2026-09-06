#!/usr/bin/env python3
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from nullone_bridge_common import BridgeError, CANONICAL_ACCOUNT_ID
from nullone_analytics_runtime import run_daily_analytics
from nullone_zernio_analytics_adapter import (
    ZernioReadOnlyAnalyticsConnector,
    build_default_transport,
)


def _default_build_connector() -> ZernioReadOnlyAnalyticsConnector:
    """Build the real read-only Zernio analytics connector.

    Not exercised by any test in this repository: tests inject a fake
    `build_connector` into `run_daily_analytics` instead. Production
    wiring of this default path — and any real scheduled invocation of
    this script — has not been deployed; controlled production
    activation remains a separate, later step (issue #37).
    """

    transport = build_default_transport()
    return ZernioReadOnlyAnalyticsConnector(transport, account_id=CANONICAL_ACCOUNT_ID)


def execute(occurrence_id: str, analytics_date: str | None) -> int:
    resolved_date = analytics_date or occurrence_id[:10]

    result = run_daily_analytics(
        occurrence_id=occurrence_id,
        analytics_date=resolved_date,
        build_connector=_default_build_connector,
    )

    print(f"RUN_ID={result['run_id']}")
    print(f"DOMAIN_OUTCOME={result['domain_outcome']}")

    if result["domain_outcome"] != "SUCCEEDED":
        print(f"REASON_CODE={result['reason_code']}")
        print(f"REASON_TEXT={result['reason_text']}")
        return 1

    if result.get("empty_success"):
        print(f"EMPTY_SUCCESS={result['empty_success']}")

    return 0


def self_test() -> int:
    def _insights_envelope(**metrics: int) -> dict:
        return {
            "success": True,
            "accountId": CANONICAL_ACCOUNT_ID,
            "platform": "instagram",
            "metricType": "total_value",
            "metrics": {
                name: {"total": value} for name, value in metrics.items()
            },
        }

    class _FakeSuccessTransport:
        def get(self, path, *, params=None):
            if path == "/analytics/instagram/follower-history":
                return 200, _insights_envelope(
                    follower_count=100, followers_gained=5, followers_lost=1
                )
            if path == "/analytics/instagram/account-insights":
                return 200, _insights_envelope(
                    reach=500,
                    views=900,
                    accounts_engaged=80,
                    total_interactions=42,
                    comments=3,
                    likes=30,
                    saves=5,
                    shares=4,
                    profile_links_taps=2,
                )
            if path == "/analytics":
                return 200, {
                    "posts": [
                        {
                            "_id": "p1",
                            "analytics": {
                                "reach": 400,
                                "likes": 20,
                                "comments": 2,
                                "saves": 3,
                                "shares": 1,
                            },
                        }
                    ],
                    "pagination": {"page": 1, "limit": 25, "total": 1, "pages": 1},
                }
            if path == "/accounts":
                return 200, {
                    "accounts": [
                        {
                            "_id": CANONICAL_ACCOUNT_ID,
                            "platform": "instagram",
                            "username": "nullone.az",
                            "isActive": True,
                        }
                    ],
                    "hasAnalyticsAccess": True,
                }
            raise AssertionError(f"unexpected path in self-test: {path}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        def build_connector() -> ZernioReadOnlyAnalyticsConnector:
            return ZernioReadOnlyAnalyticsConnector(
                _FakeSuccessTransport(), account_id=CANONICAL_ACCOUNT_ID
            )

        result = run_daily_analytics(
            occurrence_id="2026-01-01T03:20:00+04:00",
            analytics_date="2026-01-01",
            build_connector=build_connector,
            artifact_root=root,
            output_root=root / "run-outcomes",
        )

        assert result["domain_outcome"] == "SUCCEEDED"
        assert (root / "social/analytics/raw/2026-01-01.md").is_file()
        assert (root / "social/analytics/reports/2026-01-01.md").is_file()

    print("DAILY_ANALYTICS_RUNNER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")

    e = sub.add_parser("execute")
    e.add_argument("--occurrence-id", required=True)
    e.add_argument("--analytics-date", default=None)

    args = p.parse_args()

    try:
        if args.command == "self-test":
            return self_test()

        if args.command == "execute":
            return execute(args.occurrence_id, args.analytics_date)

        raise BridgeError("Unknown command")

    except BridgeError as e:
        print(f"BLOCKED={e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
