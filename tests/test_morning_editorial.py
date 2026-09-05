#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_run_outcome import make_run_id  # noqa: E402
from nullone_editorial_runtime import (  # noqa: E402
    MAX_ATTEMPTS,
    OCCURRENCE_FAILURE_BUDGET_SECONDS,
    PROVIDER_CALL_TIMEOUT_SECONDS,
    RETRY_BACKOFF_SECONDS,
    ProviderUnreachableError,
    UnsafeRetryPolicyError,
    run_morning_editorial,
    validate_occurrence_policy,
    worst_case_occurrence_seconds,
)


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_script(
    "nullone_morning_editorial_run_test",
    "nullone-morning-editorial-run.py",
)

# Confirmed 2026-09-05 evidence (issue #28): failed Morning Editorial
# occurrences were spaced as little as this many seconds apart. The
# bounded retry policy's worst case must stay under this window so a
# persistent reachability failure cannot still be running when the next
# scheduled occurrence starts.
OBSERVED_MIN_OCCURRENCE_SPACING_SECONDS = 600

# Verified real Morning Editorial run on 2026-09-04: completed in
# ~118s and produced a valid editorial board. The provider timeout
# must leave comfortable headroom above this healthy baseline.
VERIFIED_HEALTHY_RUN_SECONDS = 118


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
            self.assertEqual(provider.calls, MAX_ATTEMPTS)
            self.assertEqual(len(sleeps), MAX_ATTEMPTS - 1)

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

    def test_configured_worst_case_is_exactly_480_seconds(self):
        # No sleeping and no real provider calls: this is pure arithmetic
        # over the configured default policy constants.
        computed = worst_case_occurrence_seconds(
            max_attempts=MAX_ATTEMPTS,
            provider_call_timeout_seconds=PROVIDER_CALL_TIMEOUT_SECONDS,
            backoff_seconds=RETRY_BACKOFF_SECONDS,
        )

        self.assertEqual(MAX_ATTEMPTS, 2)
        self.assertEqual(PROVIDER_CALL_TIMEOUT_SECONDS, 210)
        self.assertEqual(RETRY_BACKOFF_SECONDS, (60,))
        self.assertEqual(computed, 480)
        self.assertEqual(OCCURRENCE_FAILURE_BUDGET_SECONDS, 480)
        self.assertEqual(computed, OCCURRENCE_FAILURE_BUDGET_SECONDS)

    def test_declared_budget_is_under_observed_occurrence_spacing(self):
        self.assertLess(
            OCCURRENCE_FAILURE_BUDGET_SECONDS,
            OBSERVED_MIN_OCCURRENCE_SPACING_SECONDS,
        )

    def test_provider_timeout_has_comfortable_headroom_over_healthy_baseline(
        self,
    ):
        headroom = PROVIDER_CALL_TIMEOUT_SECONDS - VERIFIED_HEALTHY_RUN_SECONDS

        self.assertGreater(
            PROVIDER_CALL_TIMEOUT_SECONDS,
            VERIFIED_HEALTHY_RUN_SECONDS,
        )
        self.assertEqual(headroom, 92)
        self.assertGreaterEqual(headroom, 60)

    def test_validator_accepts_the_configured_default_policy(self):
        # Does not raise: this is the same call made at module import time.
        validate_occurrence_policy(
            max_attempts=MAX_ATTEMPTS,
            provider_call_timeout_seconds=PROVIDER_CALL_TIMEOUT_SECONDS,
            backoff_seconds=RETRY_BACKOFF_SECONDS,
            budget_seconds=OCCURRENCE_FAILURE_BUDGET_SECONDS,
        )

    def test_validator_rejects_a_policy_that_would_overrun_the_budget(self):
        # The pre-fix policy (3 attempts, 900s timeout, (30, 90) backoff)
        # must be actually rejected by the validator, not merely shown to
        # be numerically larger.
        with self.assertRaises(UnsafeRetryPolicyError):
            validate_occurrence_policy(
                max_attempts=3,
                provider_call_timeout_seconds=900,
                backoff_seconds=(30, 90),
                budget_seconds=OCCURRENCE_FAILURE_BUDGET_SECONDS,
            )

    def test_validator_rejects_a_budget_at_or_above_observed_spacing(self):
        with self.assertRaises(UnsafeRetryPolicyError):
            validate_occurrence_policy(
                max_attempts=MAX_ATTEMPTS,
                provider_call_timeout_seconds=PROVIDER_CALL_TIMEOUT_SECONDS,
                backoff_seconds=RETRY_BACKOFF_SECONDS,
                budget_seconds=OBSERVED_MIN_OCCURRENCE_SPACING_SECONDS,
            )

    def test_cli_wrapper_passes_provider_timeout_to_subprocess(self):
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(runner.subprocess, "run", side_effect=fake_run):
            runner._default_invoke_provider()

        self.assertEqual(
            captured["kwargs"]["timeout"],
            PROVIDER_CALL_TIMEOUT_SECONDS,
        )

    def test_concurrent_same_occurrence_serializes_to_one_provider_call(self):
        # Two threads calling run_morning_editorial() with distinct
        # os.open() file descriptors on the same lock file are still
        # serialized by fcntl.flock, because flock locks are attached to
        # the open file description, not the process. No sleeps: the
        # second caller is only started once the first is deterministically
        # known to be inside invoke_provider() (and therefore already
        # holding the occurrence lock), so if the lock did not serialize
        # execution the second caller would race it there.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_root = root / "run-outcomes"
            board = root / "social/research/daily/2026-09-07-editorial-board.md"

            call_count = {"n": 0}
            count_lock = threading.Lock()
            entered = threading.Event()
            release = threading.Event()

            def invoke() -> None:
                with count_lock:
                    call_count["n"] += 1
                entered.set()
                self.assertTrue(
                    release.wait(timeout=5),
                    "release was never signaled",
                )
                board.parent.mkdir(parents=True, exist_ok=True)
                board.write_text("# Editorial board\n", encoding="utf-8")

            results: list[dict | None] = [None, None]

            def worker(idx: int) -> None:
                results[idx] = run_morning_editorial(
                    occurrence_id="2026-09-07T08:30:00+04:00",
                    board_date="2026-09-07",
                    invoke_provider=invoke,
                    sleep=lambda _s: None,
                    artifact_root=root,
                    output_root=output_root,
                )

            first = threading.Thread(target=worker, args=(0,))
            first.start()

            self.assertTrue(
                entered.wait(timeout=5),
                "first caller never entered the provider",
            )

            # At this point the first caller holds the occurrence lock
            # and is blocked inside invoke_provider(). A second caller
            # for the same occurrence must block on the lock rather than
            # entering invoke_provider() concurrently.
            second = threading.Thread(target=worker, args=(1,))
            second.start()

            release.set()

            first.join(timeout=5)
            second.join(timeout=5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())

            self.assertEqual(call_count["n"], 1)
            self.assertIsNotNone(results[0])
            self.assertIsNotNone(results[1])
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0]["domain_outcome"], "SUCCEEDED")

    def test_different_occurrence_lock_is_independent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_root = root / "run-outcomes"

            entered = threading.Event()
            release = threading.Event()

            def blocking_invoke() -> None:
                entered.set()
                self.assertTrue(
                    release.wait(timeout=5),
                    "release was never signaled",
                )
                board = (
                    root
                    / "social/research/daily/2026-09-07-editorial-board.md"
                )
                board.parent.mkdir(parents=True, exist_ok=True)
                board.write_text("# Editorial board\n", encoding="utf-8")

            holder = threading.Thread(
                target=lambda: run_morning_editorial(
                    occurrence_id="2026-09-07T08:30:00+04:00",
                    board_date="2026-09-07",
                    invoke_provider=blocking_invoke,
                    sleep=lambda _s: None,
                    artifact_root=root,
                    output_root=output_root,
                )
            )
            holder.start()

            self.assertTrue(
                entered.wait(timeout=5),
                "holder never entered the provider",
            )

            other_board = (
                root / "social/research/daily/2026-09-08-editorial-board.md"
            )

            def immediate_success() -> None:
                other_board.parent.mkdir(parents=True, exist_ok=True)
                other_board.write_text(
                    "# Editorial board\n", encoding="utf-8"
                )

            other_result: list[dict | None] = [None]

            def other_worker() -> None:
                other_result[0] = run_morning_editorial(
                    occurrence_id="2026-09-08T08:30:00+04:00",
                    board_date="2026-09-08",
                    invoke_provider=immediate_success,
                    sleep=lambda _s: None,
                    artifact_root=root,
                    output_root=output_root,
                )

            # A different occurrence_id must not block on the lock held
            # for the first occurrence, so this must finish promptly
            # even while `holder` is still inside its provider call.
            other = threading.Thread(target=other_worker)
            other.start()
            other.join(timeout=5)

            self.assertFalse(
                other.is_alive(),
                "a distinct occurrence was blocked by an unrelated lock",
            )
            self.assertIsNotNone(other_result[0])
            self.assertEqual(other_result[0]["domain_outcome"], "SUCCEEDED")

            release.set()
            holder.join(timeout=5)
            self.assertFalse(holder.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
