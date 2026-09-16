#!/usr/bin/env python3
"""Deterministic human-review lifecycle helper (P0 #140).

Owns ONE narrow, pure, stdlib-only semantic shared by the cadence
pending-state scan and the approval-callback guard:

    an unapproved human-review object from an EARLIER Asia/Baku
    editorial date is EFFECTIVELY EXPIRED for blocking purposes.

The stored object is never mutated here: manifests stay byte-identical
audit evidence (``stored_state`` preserved). Callers report
``stored_state=<original> / effective_state=EXPIRED /
expiry_reason=EDITORIAL_DATE_ELAPSED`` on their own receipts.

Fail-closed rules (never guess):

- Only a PROVEN earlier Baku calendar date expires. Missing,
  non-string, naive, or unparsable timestamps prove nothing and stay
  blocking.
- A manifest dated today (Asia/Baku) stays blocking: same-day
  duplicate suppression is preserved.
- A manifest dated in the future (clock skew) stays blocking.

NO model. NO network. NO Zernio. NO Telegram. NO secrets. NO writes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIMEZONE_NAME = "Asia/Baku"

EFFECTIVE_CURRENT = "CURRENT"
EFFECTIVE_EXPIRED = "EXPIRED"

EXPIRY_REASON_DATE_ELAPSED = "EDITORIAL_DATE_ELAPSED"
EXPIRY_REASON_UNPROVEN_DATE = "UNPROVEN_DATE"


def baku_zone() -> ZoneInfo:
    """Asia/Baku zone. Raises RuntimeError (fail closed) if unavailable."""
    try:
        return ZoneInfo(TIMEZONE_NAME)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown timezone: {TIMEZONE_NAME!r}: {exc}") from exc


def parse_baku_date(value: Any) -> date | None:
    """Baku calendar date of an ISO-8601 timestamp, or None if unproven.

    Returns None (never raises) for missing, non-string, naive, or
    unparsable input so callers can fail closed to "still blocking".
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed.astimezone(baku_zone()).date()


def manifest_baku_date(manifest: Any) -> date | None:
    """Authoritative editorial date of one manifest, or None if unproven.

    Reads only existing authoritative timestamp fields, in strict
    preference order: top-level ``created_at``, then
    ``review.created_at``. Never infers from filenames.
    """
    if not isinstance(manifest, dict):
        return None
    top = manifest.get("created_at")
    if isinstance(top, str):
        parsed = parse_baku_date(top)
        if parsed is not None:
            return parsed
    review = manifest.get("review")
    if isinstance(review, dict):
        return parse_baku_date(review.get("created_at"))
    return None


def is_effectively_expired(
    *,
    manifest_date: date | None,
    today: date,
) -> bool:
    """True only when an earlier Baku date is PROVEN.

    ``manifest_date=None`` (unproven), same-day, and future dates all
    return False (stay blocking). Strict ``<`` comparison only.
    """
    if manifest_date is None:
        return False
    return manifest_date < today


def effective_blocking_state(
    *,
    stored_pending: bool,
    manifest: Any,
    today: date,
) -> dict[str, Any]:
    """Derived (stored, effective) blocking state for one manifest.

    Pure read of immutable metadata. Never mutates the manifest.
    """
    if not stored_pending:
        return {
            "stored_pending": False,
            "blocking": False,
            "effective_state": EFFECTIVE_CURRENT,
            "expiry_reason": None,
        }
    manifest_date = manifest_baku_date(manifest)
    if is_effectively_expired(manifest_date=manifest_date, today=today):
        return {
            "stored_pending": True,
            "blocking": False,
            "effective_state": EFFECTIVE_EXPIRED,
            "expiry_reason": EXPIRY_REASON_DATE_ELAPSED,
        }
    return {
        "stored_pending": True,
        "blocking": True,
        "effective_state": EFFECTIVE_CURRENT,
        "expiry_reason": (
            None if manifest_date is not None else EXPIRY_REASON_UNPROVEN_DATE
        ),
    }


def self_test() -> None:
    """Offline deterministic self-test (no network, no model)."""
    zone = baku_zone()
    today = date(2026, 9, 16)

    # Proven earlier date expires.
    assert parse_baku_date("2026-09-15T15:30:11.905290+00:00") == date(2026, 9, 15)
    # UTC instant just after Baku midnight belongs to the new day.
    assert parse_baku_date("2026-09-15T20:30:00+00:00") == date(2026, 9, 16)
    # Unproven inputs never parse.
    for bad in (None, "", 123, "not-a-date", "2026-09-16T10:00:00", "2026-13-01"):
        assert parse_baku_date(bad) is None, bad

    old = {"created_at": "2026-09-15T09:30:54.139355+00:00"}
    assert manifest_baku_date(old) == date(2026, 9, 15)
    review_only = {"review": {"created_at": "2026-09-15T09:30:54+00:00"}}
    assert manifest_baku_date(review_only) == date(2026, 9, 15)
    assert manifest_baku_date({}) is None
    assert manifest_baku_date(None) is None

    assert is_effectively_expired(manifest_date=date(2026, 9, 15), today=today) is True
    assert is_effectively_expired(manifest_date=date(2026, 9, 16), today=today) is False
    assert is_effectively_expired(manifest_date=date(2026, 9, 17), today=today) is False
    assert is_effectively_expired(manifest_date=None, today=today) is False

    expired = effective_blocking_state(
        stored_pending=True, manifest=old, today=today
    )
    assert expired["blocking"] is False, expired
    assert expired["effective_state"] == EFFECTIVE_EXPIRED, expired
    assert expired["expiry_reason"] == EXPIRY_REASON_DATE_ELAPSED, expired

    same_day = effective_blocking_state(
        stored_pending=True,
        manifest={"created_at": "2026-09-16T06:30:00+00:00"},
        today=today,
    )
    assert same_day["blocking"] is True, same_day
    assert same_day["effective_state"] == EFFECTIVE_CURRENT, same_day

    unproven = effective_blocking_state(
        stored_pending=True, manifest={}, today=today
    )
    assert unproven["blocking"] is True, unproven
    assert unproven["expiry_reason"] == EXPIRY_REASON_UNPROVEN_DATE, unproven

    not_pending = effective_blocking_state(
        stored_pending=False, manifest=old, today=today
    )
    assert not_pending["blocking"] is False, not_pending

    assert zone.key == TIMEZONE_NAME
    print("REVIEW_LIFECYCLE_SELF_TEST=PASS")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["self-test"])
    args = parser.parse_args(argv)
    if args.command == "self-test":
        self_test()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
