#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/acceptance_contracts.json"
DOC = ROOT / "docs/contracts/acceptance.md"

ALLOWED_ENFORCEMENT = {"ENFORCED_TODAY", "MISSING"}
ALLOWED_PROOF = {"EXERCISED", "NOT_EXERCISED"}

FORBIDDEN_LITERAL_PATTERNS = [
    re.compile(r"6a982bbf77555aae01c28f21", re.I),  # production account ID
    re.compile(r"api[_-]?key\s*[:=]", re.I),
    re.compile(r"authorization\s*[:=]", re.I),
    re.compile(r"bearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"oauth[_-]?(token|secret)\s*[:=]", re.I),
    re.compile(r"presigned", re.I),
]

URL_RE = re.compile(r"https?://[^\s\"']+")


def fail(message: str) -> None:
    raise AssertionError(message)


def main() -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert data["schema"] == "nullone.acceptance-contracts.v1"

    contracts = data.get("contracts")
    assert isinstance(contracts, list) and contracts

    ids: set[str] = set()
    enforced = 0
    missing = 0
    not_exercised = 0

    required = {
        "id",
        "invariant",
        "enforcement",
        "proof",
        "owner",
        "positive",
        "negative",
        "observable",
    }

    for c in contracts:
        absent = required - set(c)
        if absent:
            fail(f"{c.get('id', '<unknown>')}: missing fields {sorted(absent)}")

        cid = c["id"]
        if cid in ids:
            fail(f"duplicate contract ID: {cid}")
        ids.add(cid)

        if not re.fullmatch(r"[A-Z]+(?:-[A-Z]+)*-\d{3}", cid):
            fail(f"unstable/invalid contract ID format: {cid}")

        if c["enforcement"] not in ALLOWED_ENFORCEMENT:
            fail(f"{cid}: invalid enforcement {c['enforcement']}")

        if c["proof"] not in ALLOWED_PROOF:
            fail(f"{cid}: invalid proof {c['proof']}")

        if c["proof"] == "NOT_EXERCISED":
            not_exercised += 1

        for scenario_name in ("positive", "negative"):
            scenario = c[scenario_name]
            if not isinstance(scenario, dict):
                fail(f"{cid}: {scenario_name} must be an object")
            if not str(scenario.get("given", "")).strip():
                fail(f"{cid}: {scenario_name}.given missing")
            if not str(scenario.get("expect", "")).strip():
                fail(f"{cid}: {scenario_name}.expect missing")

        if c["enforcement"] == "ENFORCED_TODAY":
            enforced += 1
            traceability = c.get("traceability")
            if not isinstance(traceability, list) or not traceability:
                fail(f"{cid}: enforced contract lacks traceability")

            for trace in traceability:
                rel = trace.get("path")
                needle = trace.get("contains")
                if not rel or not needle:
                    fail(f"{cid}: invalid traceability entry")
                path = ROOT / rel
                if not path.is_file():
                    fail(f"{cid}: traceability path missing: {rel}")
                text = path.read_text(encoding="utf-8")
                if needle not in text:
                    fail(f"{cid}: traceability anchor missing in {rel}: {needle!r}")

        else:
            missing += 1
            if not str(c.get("gap_evidence", "")).strip():
                fail(f"{cid}: missing guarantee lacks gap_evidence")

    if enforced == 0 or missing == 0 or not_exercised == 0:
        fail("matrix must distinguish enforced, missing and not-exercised states")

    raw_fixture = FIXTURE.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_LITERAL_PATTERNS:
        if pattern.search(raw_fixture):
            fail(f"fixture hygiene violation: {pattern.pattern}")

    for url in URL_RE.findall(raw_fixture):
        if not url.startswith("https://example.invalid/"):
            fail(f"fixture contains non-synthetic URL: {url}")

    doc = DOC.read_text(encoding="utf-8")
    for cid in ids:
        if f"`{cid}`" not in doc:
            fail(f"contract missing from documentation: {cid}")

    for required_phrase in (
        "must **not** claim provider-side exactly-once publication",
        "forged wrapper metadata",
        "scheduler/process success is distinct from domain/business success",
        "Closing #4 does **not** close #5, #27, or the active reliability proof.",
    ):
        if required_phrase not in doc:
            fail(f"required contract statement missing: {required_phrase}")

    print(f"ACCEPTANCE_CONTRACTS=PASS count={len(contracts)}")
    print(f"ENFORCED_TODAY={enforced}")
    print(f"MISSING={missing}")
    print(f"NOT_EXERCISED={not_exercised}")
    print("FIXTURE_HYGIENE=PASS")
    print("TRACEABILITY=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
