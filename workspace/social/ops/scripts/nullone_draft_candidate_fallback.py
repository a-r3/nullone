#!/usr/bin/env python3
"""Deterministic Draft Factory candidate-fallback boundary (P0 #140).

Incident 2026-09-16: one candidate-local packaging SKIP
(REAL_PHOTO_REQUIRED_NO_FALLBACK, correct) terminated the entire Draft
cycle instead of continuing through the remaining ranked eligible
candidates.

This module owns the fallback decision as a pure, stdlib-only state
machine over the ALREADY-LOADED ranked candidate set:

    ranked eligible ids (order frozen) + attempt ledger
    -> NEXT candidate | ALL_SKIPPED (legitimate NO_ACTION) | STOP (fail closed)

Hard rules (enforced here, not by prompt text):

- The ranked order is frozen at first record: no reshuffle, no manual
  injection, no re-ordering mid-cycle.
- Each candidate is attempted AT MOST ONCE; a second record for the
  same id raises (fail closed).
- Only a legitimate candidate-local policy SKIP continues: a
  well-formed receipt with POST_DECISION == SKIP,
  FORMAT_DECISION == SKIP, and FORMAT_REASON in the authoritative
  packaging REASON_CODES (typed contract, never fragile string
  matching by callers).
- EVERYTHING else fails closed and STOPS the cycle: malformed
  receipt, unknown decision/reason values, evaluator exception,
  provider execution failure, hash/integrity failure, unexpected
  state. Those are raised as DraftFallbackError, never converted
  into "try next".
- Exactly one accepted candidate maximum per cycle: accept() after an
  acceptance raises.

Side-effect discipline: this module performs NO render, NO manifest,
NO Zernio, NO Telegram, NO provider calls. It only decides WHICH
candidate the existing pipeline may attempt next. A skipped candidate
therefore has zero consequential effects by construction (the
downstream render/manifest/bridge gates independently refuse SKIP
receipts).

The attempt ledger is serializable so the Draft Factory run can
persist it alongside packaging receipts as the truthful audit trail:
candidates considered, ordered attempts, each SKIP reason, final
selection.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nullone_packaging_policy import (
    FORMAT_DECISIONS,
    POST_DECISIONS,
    REASON_CODES,
)

SCHEMA = "nullone.draft-fallback-ledger.v1"

DECISION_NEXT = "NEXT"
DECISION_ALL_SKIPPED = "ALL_SKIPPED"

# Typed packaging outcomes that may continue to the next candidate:
# a well-formed candidate-local policy SKIP. POST means the candidate
# proceeds through the normal pipeline (no fallback needed).
SKIP_POST_DECISION = "SKIP"
SKIP_FORMAT_DECISION = "SKIP"
PROCEED_POST_DECISION = "POST"


class DraftFallbackError(ValueError):
    """System failure: STOP the cycle, never fall back to next candidate."""


def _require_candidate_id(value: Any, *, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DraftFallbackError(f"{what} must be a non-empty string")
    if "/" in value or "\\" in value or value.strip() != value:
        raise DraftFallbackError(f"{what} is not a plain candidate id: {value!r}")
    return value


def classify_packaging_receipt(receipt: Any) -> tuple[str, str | None]:
    """Classify a packaging receipt dict into a fallback decision.

    Returns ("SKIP_FALLBACK", reason_code) for a legitimate
    candidate-local policy SKIP, ("PROCEED", None) for a POST decision.

    Raises DraftFallbackError for ANYTHING else: malformed receipt,
    missing/unknown decision fields, unknown reason code, or a receipt
    shape the contract does not define. Callers must STOP on that
    exception, never try the next candidate.
    """
    if not isinstance(receipt, dict):
        raise DraftFallbackError("packaging receipt must be an object")
    post = receipt.get("POST_DECISION")
    fmt = receipt.get("FORMAT_DECISION")
    reason = receipt.get("FORMAT_REASON")
    if post not in POST_DECISIONS:
        raise DraftFallbackError(
            f"packaging receipt has unknown POST_DECISION: {post!r}"
        )
    if fmt not in FORMAT_DECISIONS:
        raise DraftFallbackError(
            f"packaging receipt has unknown FORMAT_DECISION: {fmt!r}"
        )
    if post == PROCEED_POST_DECISION:
        return ("PROCEED", None)
    # SKIP path: the reason code is load-bearing, validate it strictly.
    if fmt != SKIP_FORMAT_DECISION:
        raise DraftFallbackError(
            "packaging receipt is inconsistent: "
            f"POST_DECISION=SKIP with FORMAT_DECISION={fmt!r}"
        )
    if reason not in REASON_CODES:
        raise DraftFallbackError(
            f"packaging SKIP receipt has unknown FORMAT_REASON: {reason!r}"
        )
    return ("SKIP_FALLBACK", reason)


def new_ledger(
    *,
    editorial_date: str,
    ranked_candidate_ids: list[str],
) -> dict[str, Any]:
    """Create a frozen cycle ledger over the already-loaded ranked set."""
    if not isinstance(editorial_date, str) or not editorial_date.strip():
        raise DraftFallbackError("editorial_date must be a non-empty string")
    if not isinstance(ranked_candidate_ids, list) or not ranked_candidate_ids:
        raise DraftFallbackError("ranked_candidate_ids must be a non-empty list")
    ranked = [_require_candidate_id(c, what="ranked_candidate_ids[]")
              for c in ranked_candidate_ids]
    if len(set(ranked)) != len(ranked):
        raise DraftFallbackError("ranked_candidate_ids contains duplicates")
    return {
        "schema": SCHEMA,
        "editorial_date": editorial_date,
        "ranked_candidate_ids": ranked,
        "attempts": [],
        "accepted_candidate_id": None,
    }


def _require_ledger(ledger: Any) -> dict[str, Any]:
    if not isinstance(ledger, dict):
        raise DraftFallbackError("fallback ledger must be an object")
    if ledger.get("schema") != SCHEMA:
        raise DraftFallbackError("fallback ledger has unknown schema")
    ranked = ledger.get("ranked_candidate_ids")
    attempts = ledger.get("attempts")
    if not isinstance(ranked, list) or not all(
        isinstance(c, str) for c in ranked
    ):
        raise DraftFallbackError("fallback ledger has malformed ranked set")
    if not isinstance(attempts, list):
        raise DraftFallbackError("fallback ledger has malformed attempts")
    return ledger


def record_skip(
    ledger: dict[str, Any],
    *,
    candidate_id: str,
    skip_reason: str,
) -> dict[str, Any]:
    """Record one candidate-local SKIP attempt. Mutates and returns ledger.

    Raises on: unknown candidate (not in frozen ranked set = injection),
    duplicate attempt, skip reason outside the typed REASON_CODES, or a
    cycle that already accepted a candidate.
    """
    ledger = _require_ledger(ledger)
    candidate_id = _require_candidate_id(candidate_id, what="candidate_id")
    if ledger.get("accepted_candidate_id") is not None:
        raise DraftFallbackError("cycle already accepted a candidate")
    if candidate_id not in ledger["ranked_candidate_ids"]:
        raise DraftFallbackError(
            f"candidate {candidate_id!r} is not in the frozen ranked set"
        )
    if any(a["candidate_id"] == candidate_id for a in ledger["attempts"]):
        raise DraftFallbackError(
            f"candidate {candidate_id!r} was already attempted once"
        )
    if skip_reason not in REASON_CODES:
        raise DraftFallbackError(
            f"skip reason is not a typed packaging reason: {skip_reason!r}"
        )
    ledger["attempts"].append(
        {
            "candidate_id": candidate_id,
            "skip_reason": skip_reason,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return ledger


def record_acceptance(
    ledger: dict[str, Any],
    *,
    candidate_id: str,
) -> dict[str, Any]:
    """Record the single accepted candidate. Raises if one already exists."""
    ledger = _require_ledger(ledger)
    candidate_id = _require_candidate_id(candidate_id, what="candidate_id")
    if ledger.get("accepted_candidate_id") is not None:
        raise DraftFallbackError(
            "cycle already accepted "
            f"{ledger['accepted_candidate_id']!r}: at most one accepted candidate"
        )
    if candidate_id not in ledger["ranked_candidate_ids"]:
        raise DraftFallbackError(
            f"candidate {candidate_id!r} is not in the frozen ranked set"
        )
    if any(a["candidate_id"] == candidate_id for a in ledger["attempts"]):
        raise DraftFallbackError(
            f"candidate {candidate_id!r} was already skipped; "
            "a skipped candidate cannot later be accepted"
        )
    ledger["accepted_candidate_id"] = candidate_id
    return ledger


def next_candidate(ledger: dict[str, Any]) -> dict[str, Any]:
    """Next fallback decision: NEXT (+candidate) or ALL_SKIPPED (+reasons).

    Order-preserving: first ranked id with no recorded attempt.
    """
    ledger = _require_ledger(ledger)
    if ledger.get("accepted_candidate_id") is not None:
        raise DraftFallbackError("cycle already accepted a candidate")
    attempted = {a["candidate_id"] for a in ledger["attempts"]}
    for candidate_id in ledger["ranked_candidate_ids"]:
        if candidate_id not in attempted:
            return {"decision": DECISION_NEXT, "candidate_id": candidate_id}
    return {
        "decision": DECISION_ALL_SKIPPED,
        "candidate_id": None,
        "domain_outcome": "NO_ACTION",
        "reason": aggregate_exhaustion_reason(ledger),
        "skipped": [
            {
                "candidate_id": a["candidate_id"],
                "skip_reason": a["skip_reason"],
            }
            for a in ledger["attempts"]
        ],
    }


def aggregate_exhaustion_reason(ledger: dict[str, Any]) -> str:
    """Deterministic aggregate NO_ACTION reason for an exhausted set."""
    ledger = _require_ledger(ledger)
    parts = [
        f"{a['candidate_id']}:{a['skip_reason']}" for a in ledger["attempts"]
    ]
    return (
        f"ALL_ELIGIBLE_SKIPPED "
        f"count={len(parts)} attempts=[{', '.join(parts)}]"
    )


def ledger_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    """Truthful run-receipt view: considered, attempts, selection."""
    ledger = _require_ledger(ledger)
    accepted = ledger.get("accepted_candidate_id")
    if accepted is not None:
        terminal = {"decision": "ACCEPTED", "candidate_id": accepted}
    else:
        terminal = next_candidate(ledger)
    return {
        "schema": SCHEMA,
        "editorial_date": ledger["editorial_date"],
        "candidates_considered": list(ledger["ranked_candidate_ids"]),
        "attempts": [
            {
                "candidate_id": a["candidate_id"],
                "skip_reason": a["skip_reason"],
            }
            for a in ledger["attempts"]
        ],
        "final_selected_candidate_id": accepted,
        "terminal_decision": terminal["decision"],
    }


def load_ledger(path: Path) -> dict[str, Any]:
    """Load and validate a persisted ledger file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DraftFallbackError(f"cannot read ledger file: {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DraftFallbackError(
            f"malformed ledger JSON: {path}: {exc}"
        ) from exc
    return _require_ledger(data)


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    """Atomic persist (tmp + rename). Validates before writing."""
    _require_ledger(ledger)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
    except OSError as exc:
        raise DraftFallbackError(
            f"cannot persist ledger file: {path}: {exc}"
        ) from exc


def self_test() -> None:
    """Offline deterministic self-test (no network, no model)."""
    # Typed classification: SKIP continues, POST proceeds, junk stops.
    decision, reason = classify_packaging_receipt(
        {
            "POST_DECISION": "SKIP",
            "FORMAT_DECISION": "SKIP",
            "FORMAT_REASON": "REAL_PHOTO_REQUIRED_NO_FALLBACK",
        }
    )
    assert (decision, reason) == (
        "SKIP_FALLBACK",
        "REAL_PHOTO_REQUIRED_NO_FALLBACK",
    )
    assert classify_packaging_receipt(
        {"POST_DECISION": "POST", "FORMAT_DECISION": "SINGLE_POST"}
    ) == ("PROCEED", None)
    for bad in (
        {},
        {"POST_DECISION": "SKIP"},
        {"POST_DECISION": "SKIP", "FORMAT_DECISION": "SKIP",
         "FORMAT_REASON": "INVENTED_REASON"},
        {"POST_DECISION": "SKIP", "FORMAT_DECISION": "SINGLE_POST",
         "FORMAT_REASON": "REAL_PHOTO_REQUIRED_NO_FALLBACK"},
        {"POST_DECISION": "MAYBE", "FORMAT_DECISION": "SKIP"},
        "not-a-dict",
    ):
        try:
            classify_packaging_receipt(bad)
        except DraftFallbackError:
            pass
        else:
            raise AssertionError(f"bad receipt did not fail closed: {bad!r}")

    # Order preserved, at-most-once, exactly-one acceptance.
    ledger = new_ledger(
        editorial_date="2026-09-16",
        ranked_candidate_ids=["rank1", "rank2", "rank3"],
    )
    assert next_candidate(ledger)["candidate_id"] == "rank1"
    record_skip(
        ledger, candidate_id="rank1",
        skip_reason="REAL_PHOTO_REQUIRED_NO_FALLBACK",
    )
    assert next_candidate(ledger)["candidate_id"] == "rank2"
    try:
        record_skip(
            ledger, candidate_id="rank1",
            skip_reason="REAL_PHOTO_REQUIRED_NO_FALLBACK",
        )
    except DraftFallbackError:
        pass
    else:
        raise AssertionError("double attempt did not fail closed")
    try:
        record_skip(ledger, candidate_id="injected", skip_reason="LOW_AUDIENCE_VALUE")
    except DraftFallbackError:
        pass
    else:
        raise AssertionError("injected candidate did not fail closed")
    record_acceptance(ledger, candidate_id="rank2")
    try:
        record_acceptance(ledger, candidate_id="rank3")
    except DraftFallbackError:
        pass
    else:
        raise AssertionError("second acceptance did not fail closed")

    # Exhaustion aggregates deterministically.
    full = new_ledger(
        editorial_date="2026-09-16",
        ranked_candidate_ids=["a", "b"],
    )
    record_skip(full, candidate_id="a", skip_reason="LOW_AUDIENCE_VALUE")
    record_skip(full, candidate_id="b", skip_reason="WEAK_SOURCE_GROUNDING")
    terminal = next_candidate(full)
    assert terminal["decision"] == DECISION_ALL_SKIPPED, terminal
    assert terminal["domain_outcome"] == "NO_ACTION", terminal
    assert "a:LOW_AUDIENCE_VALUE" in terminal["reason"], terminal
    assert "b:WEAK_SOURCE_GROUNDING" in terminal["reason"], terminal

    print("DRAFT_CANDIDATE_FALLBACK_SELF_TEST=PASS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")

    p_next = sub.add_parser("next")
    p_next.add_argument("--ledger", required=True)
    p_next.add_argument("--editorial-date", required=True)
    p_next.add_argument("--ranked", required=True,
                        help="comma-separated ranked candidate ids (frozen)")

    p_record = sub.add_parser("record-skip")
    p_record.add_argument("--ledger", required=True)
    p_record.add_argument("--candidate", required=True)
    p_record.add_argument("--reason", required=True)

    p_accept = sub.add_parser("record-accept")
    p_accept.add_argument("--ledger", required=True)
    p_accept.add_argument("--candidate", required=True)

    p_status = sub.add_parser("status")
    p_status.add_argument("--ledger", required=True)

    args = parser.parse_args(argv)
    if args.command == "self-test":
        self_test()
        return 0
    ledger_path = Path(args.ledger)
    if args.command == "next":
        ranked = [c.strip() for c in args.ranked.split(",") if c.strip()]
        if ledger_path.exists():
            ledger = load_ledger(ledger_path)
            if ledger["ranked_candidate_ids"] != ranked:
                print("FALLBACK_RANK_RESHUFFLE_REFUSED", file=sys.stderr)
                return 2
            if ledger.get("editorial_date") != args.editorial_date:
                print("FALLBACK_DATE_MISMATCH", file=sys.stderr)
                return 2
        else:
            ledger = new_ledger(
                editorial_date=args.editorial_date, ranked_candidate_ids=ranked
            )
            save_ledger(ledger_path, ledger)
        print(json.dumps(next_candidate(ledger), indent=2, sort_keys=True))
        return 0
    if args.command == "record-skip":
        ledger = load_ledger(ledger_path)
        record_skip(
            ledger, candidate_id=args.candidate, skip_reason=args.reason
        )
        save_ledger(ledger_path, ledger)
        print(json.dumps(ledger_summary(ledger), indent=2, sort_keys=True))
        return 0
    if args.command == "record-accept":
        ledger = load_ledger(ledger_path)
        record_acceptance(ledger, candidate_id=args.candidate)
        save_ledger(ledger_path, ledger)
        print(json.dumps(ledger_summary(ledger), indent=2, sort_keys=True))
        return 0
    if args.command == "status":
        print(json.dumps(ledger_summary(load_ledger(ledger_path)),
                         indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
