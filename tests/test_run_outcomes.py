#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

from nullone_run_outcome import (  # noqa: E402
    CompletionContractError,
    assess_run,
    atomic_write_result,
    health_decision,
    make_run_id,
    result_path,
    validate_result_record,
    validate_result_structure,
)


class RunOutcomeTests(unittest.TestCase):
    def test_run_id_is_stable_and_occurrence_scoped(self):
        first = make_run_id(
            workflow_id="daily-analytics",
            occurrence_id="2026-09-05T03:20:00+04:00",
        )
        second = make_run_id(
            workflow_id="daily-analytics",
            occurrence_id="2026-09-05T03:20:00+04:00",
        )
        later = make_run_id(
            workflow_id="daily-analytics",
            occurrence_id="2026-09-06T03:20:00+04:00",
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, later)

    def test_scheduler_success_cannot_hide_blocked_domain(self):
        result = assess_run(
            workflow_id="daily-analytics",
            occurrence_id="2026-09-05T03:20:00+04:00",
            scheduler_status="succeeded",
            domain_outcome="BLOCKED",
            reason_code="ZERNIO_ANALYTICS_UNAVAILABLE",
            reason_text="Zernio analytics capability is unavailable.",
        )

        self.assertEqual(result["scheduler_status"], "succeeded")
        self.assertEqual(result["domain_outcome"], "BLOCKED")
        self.assertEqual(result["health"], "UNHEALTHY")

    def test_success_with_required_artifact_is_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "analytics/daily.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text('{"status":"ok"}\n', encoding="utf-8")

            result = assess_run(
                workflow_id="daily-analytics",
                occurrence_id="2026-09-05T03:20:00+04:00",
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                artifact_root=root,
                required_artifacts=("analytics/daily.json",),
            )

            self.assertEqual(result["domain_outcome"], "SUCCEEDED")
            self.assertEqual(result["health"], "HEALTHY")
            self.assertEqual(result["missing_artifacts"], [])

    def test_missing_required_artifact_becomes_failed(self):
        with tempfile.TemporaryDirectory() as td:
            result = assess_run(
                workflow_id="daily-analytics",
                occurrence_id="2026-09-05T03:20:00+04:00",
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                artifact_root=Path(td),
                required_artifacts=("analytics/daily.json",),
            )

            self.assertEqual(result["scheduler_status"], "succeeded")
            self.assertEqual(result["domain_outcome"], "FAILED")
            self.assertEqual(result["health"], "UNHEALTHY")
            self.assertEqual(
                result["reason_code"],
                "REQUIRED_ARTIFACT_MISSING",
            )

    def test_success_without_evidence_contract_is_rejected(self):
        with self.assertRaisesRegex(
            CompletionContractError,
            "requires artifacts or explicit empty_success",
        ):
            assess_run(
                workflow_id="daily-analytics",
                occurrence_id="2026-09-05T03:20:00+04:00",
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
            )

    def test_explicit_no_data_is_valid_success(self):
        result = assess_run(
            workflow_id="daily-analytics",
            occurrence_id="2026-09-05T03:20:00+04:00",
            scheduler_status="succeeded",
            domain_outcome="SUCCEEDED",
            required_artifacts=("analytics/daily.json",),
            empty_success="NO_DATA",
        )

        self.assertEqual(result["domain_outcome"], "SUCCEEDED")
        self.assertEqual(result["health"], "HEALTHY")
        self.assertEqual(result["empty_success"], "NO_DATA")

    def test_non_success_requires_machine_and_operator_reason(self):
        with self.assertRaisesRegex(
            CompletionContractError,
            "reason_code is required",
        ):
            assess_run(
                workflow_id="morning-editorial",
                occurrence_id="2026-09-05T08:30:00+04:00",
                scheduler_status="error",
                domain_outcome="FAILED",
                reason_text="Provider is unreachable.",
            )

        with self.assertRaisesRegex(
            CompletionContractError,
            "reason_text is required",
        ):
            assess_run(
                workflow_id="morning-editorial",
                occurrence_id="2026-09-05T08:30:00+04:00",
                scheduler_status="error",
                domain_outcome="FAILED",
                reason_code="PROVIDER_UNREACHABLE",
            )

    def test_operator_reason_must_be_concise_single_line(self):
        with self.assertRaisesRegex(
            CompletionContractError,
            "single-line",
        ):
            assess_run(
                workflow_id="morning-editorial",
                occurrence_id="2026-09-05T08:30:00+04:00",
                scheduler_status="error",
                domain_outcome="FAILED",
                reason_code="PROVIDER_UNREACHABLE",
                reason_text="first line\nraw stack dump",
            )

    def test_record_validation_rejects_tampered_health(self):
        result = assess_run(
            workflow_id="daily-analytics",
            occurrence_id="2026-09-05T03:20:00+04:00",
            scheduler_status="succeeded",
            domain_outcome="BLOCKED",
            reason_code="ZERNIO_ANALYTICS_UNAVAILABLE",
            reason_text="Zernio analytics capability is unavailable.",
        )

        result["health"] = "HEALTHY"

        with self.assertRaisesRegex(
            CompletionContractError,
            "health does not match",
        ):
            validate_result_record(result)

    def test_record_validation_rejects_tampered_run_id(self):
        result = assess_run(
            workflow_id="morning-editorial",
            occurrence_id="2026-09-05T08:30:00+04:00",
            scheduler_status="error",
            domain_outcome="FAILED",
            reason_code="PROVIDER_UNREACHABLE",
            reason_text="Provider is unreachable.",
        )

        result["run_id"] = "run_" + ("0" * 24)

        with self.assertRaisesRegex(
            CompletionContractError,
            "run_id does not match",
        ):
            validate_result_record(result)

    def test_atomic_writer_rejects_malformed_record(self):
        result = assess_run(
            workflow_id="morning-editorial",
            occurrence_id="2026-09-05T08:30:00+04:00",
            scheduler_status="error",
            domain_outcome="UNKNOWN",
            reason_code="PROVIDER_RESULT_UNKNOWN",
            reason_text="Provider result could not be established.",
        )

        result["health"] = "HEALTHY"

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run-result.json"

            with self.assertRaisesRegex(
                CompletionContractError,
                "health does not match",
            ):
                atomic_write_result(path, result)

            self.assertFalse(path.exists())

    def test_artifact_paths_are_contained_and_relative(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            with self.assertRaisesRegex(
                CompletionContractError,
                "escapes artifact_root",
            ):
                assess_run(
                    workflow_id="daily-analytics",
                    occurrence_id="2026-09-05T03:20:00+04:00",
                    scheduler_status="succeeded",
                    domain_outcome="SUCCEEDED",
                    artifact_root=root,
                    required_artifacts=("../outside.json",),
                )

            absolute = str(root / "analytics/daily.json")

            with self.assertRaisesRegex(
                CompletionContractError,
                "must be relative",
            ):
                assess_run(
                    workflow_id="daily-analytics",
                    occurrence_id="2026-09-05T03:20:00+04:00",
                    scheduler_status="succeeded",
                    domain_outcome="SUCCEEDED",
                    artifact_root=root,
                    required_artifacts=(absolute,),
                )

    def test_persisted_success_rechecks_artifact_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            artifact = root / "analytics/daily.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                '{"status":"ok"}\n',
                encoding="utf-8",
            )

            result = assess_run(
                workflow_id="daily-analytics",
                occurrence_id="2026-09-05T03:20:00+04:00",
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                artifact_root=root,
                required_artifacts=("analytics/daily.json",),
            )

            output = root / "run-state/result.json"

            with self.assertRaisesRegex(
                CompletionContractError,
                "artifact_root required",
            ):
                atomic_write_result(output, result)

            atomic_write_result(
                output,
                result,
                artifact_root=root,
            )
            self.assertTrue(output.is_file())

            artifact.unlink()

            second = root / "run-state/result-after-delete.json"

            with self.assertRaisesRegex(
                CompletionContractError,
                "required artifacts missing",
            ):
                atomic_write_result(
                    second,
                    result,
                    artifact_root=root,
                )

            self.assertFalse(second.exists())

    def test_result_record_rejects_unexpected_fields(self):
        result = assess_run(
            workflow_id="morning-editorial",
            occurrence_id="2026-09-05T08:30:00+04:00",
            scheduler_status="error",
            domain_outcome="FAILED",
            reason_code="PROVIDER_UNREACHABLE",
            reason_text="Provider is unreachable.",
        )

        result["unexpected_private_value"] = "must-not-persist"

        with self.assertRaisesRegex(
            CompletionContractError,
            "unexpected result fields",
        ):
            validate_result_record(result)

    def test_result_path_is_canonical_and_rejects_forged_id(self):
        run_id = make_run_id(
            workflow_id="daily-analytics",
            occurrence_id="2026-09-05T03:20:00+04:00",
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = root.resolve() / f"{run_id}.json"

            self.assertEqual(
                result_path(root, run_id),
                expected,
            )

            with self.assertRaisesRegex(
                CompletionContractError,
                "invalid run_id format",
            ):
                result_path(root, "../escape")

    def test_health_decision_is_quiet_for_success(self):
        result = assess_run(
            workflow_id="daily-analytics",
            occurrence_id="2026-09-05T03:20:00+04:00",
            scheduler_status="succeeded",
            domain_outcome="SUCCEEDED",
            empty_success="NO_DATA",
        )

        decision = health_decision(result)

        self.assertEqual(decision["health"], "HEALTHY")
        self.assertFalse(decision["attention_required"])
        self.assertIsNone(decision["failure_identity"])

    def test_health_decision_exposes_stable_failure_identity(self):
        result = assess_run(
            workflow_id="daily-analytics",
            occurrence_id="2026-09-05T03:20:00+04:00",
            scheduler_status="succeeded",
            domain_outcome="BLOCKED",
            reason_code="ZERNIO_ANALYTICS_UNAVAILABLE",
            reason_text="Zernio analytics capability is unavailable.",
        )

        first = health_decision(result)
        second = health_decision(result)

        self.assertTrue(first["attention_required"])
        self.assertEqual(
            first["failure_identity"],
            result["run_id"]
            + ":ZERNIO_ANALYTICS_UNAVAILABLE",
        )
        self.assertEqual(first, second)

    def test_structure_validation_does_not_require_live_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "analytics/daily.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                '{"status":"ok"}\n',
                encoding="utf-8",
            )

            result = assess_run(
                workflow_id="daily-analytics",
                occurrence_id="2026-09-05T03:20:00+04:00",
                scheduler_status="succeeded",
                domain_outcome="SUCCEEDED",
                artifact_root=root,
                required_artifacts=("analytics/daily.json",),
            )

            artifact.unlink()

            # Consumer-side structural validation remains possible after
            # the original evidence-backed persistence decision.
            validate_result_structure(result)

            with self.assertRaisesRegex(
                CompletionContractError,
                "required artifacts missing",
            ):
                validate_result_record(
                    result,
                    artifact_root=root,
                )

    def test_atomic_result_write_round_trip(self):
        result = assess_run(
            workflow_id="morning-editorial",
            occurrence_id="2026-09-05T08:30:00+04:00",
            scheduler_status="error",
            domain_outcome="UNKNOWN",
            reason_code="PROVIDER_RESULT_UNKNOWN",
            reason_text="Provider result could not be established.",
        )

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run-result.json"
            atomic_write_result(path, result)

            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
