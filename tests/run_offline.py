#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMMANDS = [
    [sys.executable, "tests/test_acceptance_contracts.py"],
    [sys.executable, "tests/test_breaking_routing_contract.py"],
    [sys.executable, "tests/test_breaking_identity.py"],
    [sys.executable, "tests/test_cadence_contract_fixture.py"],
    [sys.executable, "tests/test_editorial_cadence_v2_contract.py"],
    [sys.executable, "tests/test_scheduler_invocation_contract_fixture.py"],
    [sys.executable, "tests/test_cadence_controller.py"],
    [sys.executable, "tests/test_cadence_state_adapter.py"],
    [sys.executable, "tests/test_story_pipeline.py"],
    [sys.executable, "tests/test_story_supersession.py"],
    [sys.executable, "tests/test_review_delivery.py"],
    [sys.executable, "tests/test_story_workflow.py"],
    [sys.executable, "tests/test_story_workflow_capability_negative.py"],
    [sys.executable, "tests/test_editorial_candidate_handoff.py"],
    [sys.executable, "tests/test_story_production_provider.py"],
    [sys.executable, "tests/test_story_scheduled_workflow.py"],
    [sys.executable, "tests/test_story_production_capability_negative.py"],
    [sys.executable, "tests/test_breaking_router.py"],
    [sys.executable, "tests/test_main_draft_pipeline.py"],
    [sys.executable, "tests/test_breaking_dispatch.py"],
    [sys.executable, "tests/test_breaking_workflow.py"],
    [sys.executable, "tests/test_breaking_workflow_capability_negative.py"],
    [sys.executable, "tests/test_breaking_scan_authority.py"],
    [sys.executable, "tests/test_breaking_scan_commit.py"],
    [sys.executable, "tests/test_breaking_candidate_runner.py"],
    [sys.executable, "tests/test_breaking_consume.py"],
    [sys.executable, "tests/test_breaking_production_capability_negative.py"],
    [sys.executable, "tests/test_run_outcomes.py"],
    [sys.executable, "tests/test_behavioral_regressions.py"],
    [sys.executable, "tests/test_approval_publication_instruction_safety.py"],
    [sys.executable, "tests/test_morning_editorial.py"],
    [sys.executable, "tests/test_daily_analytics.py"],
    [sys.executable, "tests/test_failure_notify.py"],
    [sys.executable, "tests/test_openclaw_scheduler_adapter.py"],
    [sys.executable, "tests/test_morning_workflow.py"],
    [sys.executable, "tests/test_analytics_workflow.py"],
    [sys.executable, "tests/test_analytics_provider_factory.py"],
    [sys.executable, "tests/test_secret_provider.py"],
    [sys.executable, "tests/test_scheduled_workflows_capability_negative.py"],
    [sys.executable, "tests/test_zernio_draft_adapter.py"],
    [sys.executable, "tests/test_draft_provider_factory.py"],
    [sys.executable, "tests/test_scheduled_run_cli.py"],
    [sys.executable, "tests/test_schedule_registry.py"],
    [sys.executable, "tests/test_scheduled_occurrence_authority.py"],
    [sys.executable, "tests/test_scheduled_run_dispatch.py"],
    [sys.executable, "tests/test_scheduled_wakeup_cli.py"],
    [sys.executable, "tests/test_publish_ipc.py"],
    [sys.executable, "tests/test_publish_receipt.py"],
    [sys.executable, "tests/test_final_publish_controller.py"],
    [sys.executable, "tests/test_zernio_publish_adapter.py"],
    [sys.executable, "tests/test_publish_secret_pipe.py"],
    [sys.executable, "tests/test_plugin_routing.py"],
    [sys.executable, "tests/test_release_cli.py"],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_breaking_identity.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_breaking_router.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_main_draft_pipeline.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_breaking_dispatch.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-manifest.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-draft-bridge.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-publish-bridge.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-publisher-run.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_final_publish_controller.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-morning-editorial-run.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-daily-analytics-run.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-failure-notify-run.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_scheduler_invocation.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_openclaw_scheduler_adapter.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_schedule_registry.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_scheduled_occurrence_authority.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_secret_provider.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_zernio_publish_adapter.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_publish_provider_factory.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_analytics_provider_factory.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_morning_workflow.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_analytics_workflow.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-scheduled-run.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-scheduled-wakeup.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_review_delivery.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_telegram_review_delivery_adapter.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_story_candidate_provider.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_story_workflow.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_editorial_candidate_handoff.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_story_production_provider.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_story_workflow.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_breaking_scan_authority.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-breaking-scan.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone_breaking_candidate_runner.py",
        "self-test",
    ],
    [
        sys.executable,
        "workspace/social/ops/scripts/nullone-breaking-consume.py",
        "self-test",
    ],
]


def main() -> int:
    for cmd in COMMANDS:
        print("+", " ".join(cmd), flush=True)
        cp = subprocess.run(cmd, cwd=ROOT, check=False)
        if cp.returncode != 0:
            return cp.returncode

    print("OFFLINE_REGRESSION_SUITE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
