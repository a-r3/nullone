#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_run_outcome import make_run_id  # noqa: E402
from nullone_editorial_runtime import (  # noqa: E402
    ProviderUnreachableError,
    run_morning_editorial,
)


class UnreachableStub:
    """Always raises the confirmed ENOTFOUND/unreachable failure."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        raise ProviderUnreachableError(
            "API Error: Can't reach the API server — ENOTFOUND"
        )


class ImmediateSuccessStub:
    def __init__(self, board_path: Path) -> None:
        self.calls = 0
        self.board_path = board_path

    def __call__(self) -> None:
        self.calls += 1
        self.board_path.parent.mkdir(parents=True, exist_ok=True)
        self.board_path.write_text("# Editorial board\n", encoding="utf-8")


class MorningEditorialRuntimeTests(unittest.TestCase):
    def test_enotfound_bounded_retry_ends_in_final_domain_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sleeps: list[float] = []
            provider = UnreachableStub()

            result = run_morning_editorial(
                occurrence_id="2026-09-05T08:30:00+04:00",
                board_date="2026-09-05",
                invoke_provider=provider,
                sleep=sleeps.append,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["reason_code"], "PROVIDER_UNREACHABLE")
            self.assertNotIn("\n", result["reason_text"])
            self.assertEqual(provider.calls, 3)
            self.assertEqual(len(sleeps), 2)

            persisted = (
                root / "run-outcomes" / f"{result['run_id']}.json"
            )
            self.assertTrue(persisted.is_file())

    def test_transient_failure_then_success_no_duplicate_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            board = root / "social/research/daily/2026-09-05-editorial-board.md"
            calls: list[int] = []
            mutation_count = {"n": 0}

            def straggler_then_visible() -> None:
                calls.append(1)
                # Simulates the confirmed pattern: the provider actually
                # completed the board write, but the call itself was
                # then reported as an unreachable/timeout failure.
                board.parent.mkdir(parents=True, exist_ok=True)
                board.write_text("# Editorial board\n", encoding="utf-8")
                mutation_count["n"] += 1
                raise ProviderUnreachableError(
                    "API Error: Can't reach the API server — ENOTFOUND"
                )

            result = run_morning_editorial(
                occurrence_id="2026-09-05T08:30:00+04:00",
                board_date="2026-09-05",
                invoke_provider=straggler_then_visible,
                sleep=lambda _s: None,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "SUCCEEDED")
            # The provider was consulted once; the retry loop's artifact
            # check short-circuited the second attempt, so the board was
            # never written twice and no second mutation occurred.
            self.assertEqual(len(calls), 1)
            self.assertEqual(mutation_count["n"], 1)

    def test_failed_occurrence_then_later_distinct_healthy_occurrence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_root = root / "run-outcomes"

            failed = run_morning_editorial(
                occurrence_id="2026-09-05T08:30:00+04:00",
                board_date="2026-09-05",
                invoke_provider=UnreachableStub(),
                sleep=lambda _s: None,
                artifact_root=root,
                output_root=output_root,
            )
            self.assertEqual(failed["domain_outcome"], "FAILED")

            later_board = (
                root / "social/research/daily/2026-09-06-editorial-board.md"
            )
            later_provider = ImmediateSuccessStub(later_board)

            healthy = run_morning_editorial(
                occurrence_id="2026-09-06T08:30:00+04:00",
                board_date="2026-09-06",
                invoke_provider=later_provider,
                sleep=lambda _s: None,
                artifact_root=root,
                output_root=output_root,
            )

            self.assertEqual(healthy["domain_outcome"], "SUCCEEDED")
            self.assertEqual(later_provider.calls, 1)
            self.assertNotEqual(healthy["run_id"], failed["run_id"])

            # No catch-up duplication: the earlier failed occurrence's
            # board was never synthesized, and its stored result is
            # unchanged by the later healthy run.
            earlier_board = (
                root / "social/research/daily/2026-09-05-editorial-board.md"
            )
            self.assertFalse(earlier_board.exists())

            still_failed = (output_root / f"{failed['run_id']}.json")
            self.assertTrue(still_failed.is_file())

    def test_retries_and_reentry_preserve_occurrence_run_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_root = root / "run-outcomes"
            occurrence_id = "2026-09-05T08:30:00+04:00"

            expected_run_id = make_run_id(
                workflow_id="morning-editorial",
                occurrence_id=occurrence_id,
            )

            first = run_morning_editorial(
                occurrence_id=occurrence_id,
                board_date="2026-09-05",
                invoke_provider=UnreachableStub(),
                sleep=lambda _s: None,
                artifact_root=root,
                output_root=output_root,
            )
            self.assertEqual(first["run_id"], expected_run_id)

            def must_not_be_called() -> None:
                raise AssertionError(
                    "provider must not be re-invoked for a "
                    "terminal occurrence"
                )

            second = run_morning_editorial(
                occurrence_id=occurrence_id,
                board_date="2026-09-05",
                invoke_provider=must_not_be_called,
                sleep=lambda _s: None,
                artifact_root=root,
                output_root=output_root,
            )

            self.assertEqual(second["run_id"], expected_run_id)
            self.assertEqual(second, first)

    def test_non_reachability_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calls: list[int] = []

            def broken() -> None:
                calls.append(1)
                raise RuntimeError("schema validation failed")

            result = run_morning_editorial(
                occurrence_id="2026-09-05T08:30:00+04:00",
                board_date="2026-09-05",
                invoke_provider=broken,
                sleep=lambda _s: None,
                artifact_root=root,
                output_root=root / "run-outcomes",
            )

            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["reason_code"], "EDITORIAL_PROVIDER_ERROR")
            self.assertEqual(len(calls), 1)

    def test_publication_is_never_referenced_by_the_retry_module(self):
        for filename in (
            "nullone_editorial_runtime.py",
            "nullone-morning-editorial-run.py",
        ):
            source = (SCRIPTS / filename).read_text(encoding="utf-8").lower()
            self.assertNotIn("publish", source)
            self.assertNotIn("zernio", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
