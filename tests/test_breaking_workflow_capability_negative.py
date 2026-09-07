#!/usr/bin/env python3
"""Static capability-negative guards for the #63 application layer."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

APPLICATION_MODULES = (
    "nullone_breaking_workflow.py",
    "nullone_breaking_workflow_input.py",
    "nullone_breaking_main_candidate_provider.py",
)
EDGE_MODULE = "nullone_breaking_radar_edge.py"
_TRIPLE_QUOTED_RE = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)
_COMMENT_RE = re.compile(r"#.*")


def code_only(filename):
    source = (SCRIPTS / filename).read_text(encoding="utf-8")
    return _COMMENT_RE.sub("", _TRIPLE_QUOTED_RE.sub("", source))


class BreakingWorkflowCapabilityNegativeTests(unittest.TestCase):
    def test_application_has_no_subprocess_or_openclaw_cli(self):
        for filename in APPLICATION_MODULES:
            source = code_only(filename)
            self.assertNotIn("subprocess", source)
            self.assertNotIn("import openclaw", source)
            self.assertNotIn("openclaw message send", source)

    def test_application_has_no_zernio_tools_or_endpoints(self):
        forbidden = (
            "mcp__zernio",
            "posts_create",
            "posts_publish_now",
            "posts_update",
            "posts_delete",
            "media_get_media_presigned_url",
            "/api/v1/posts",
        )
        for filename in APPLICATION_MODULES:
            source = code_only(filename)
            for value in forbidden:
                self.assertNotIn(value, source)

    def test_application_and_edge_have_no_publisher_import(self):
        for filename in (*APPLICATION_MODULES, EDGE_MODULE):
            source = code_only(filename)
            for value in (
                "nullone_publish_bridge",
                "nullone_publisher_run",
                "nullone-publish-bridge",
                "nullone-publisher-run",
            ):
                self.assertNotIn(value, source)

    def test_no_scheduling_framework_or_cron_capability(self):
        for filename in (*APPLICATION_MODULES, EDGE_MODULE):
            source = code_only(filename)
            for value in ("import sched", "APScheduler", "croniter", "crontab"):
                self.assertNotIn(value, source)

    def test_no_approval_or_callback_processing_capability(self):
        for filename in (*APPLICATION_MODULES, EDGE_MODULE):
            source = code_only(filename)
            for value in ("PUBLISH_AUTHORIZED", "callback_query", "approve:", "reject:", "revise:"):
                self.assertNotIn(value, source)

    def test_edge_contains_no_identity_routing_or_dispatch_business_calls(self):
        source = code_only(EDGE_MODULE)
        for value in (
            "load_repository_state",
            "evaluate_routing",
            "dispatch_draft_set",
            "collect_format_loads",
            "run_story_pipeline",
            "run_main_pipeline",
        ):
            self.assertNotIn(value, source)

    def test_runtime_import_does_not_load_publisher_modules(self):
        import nullone_breaking_main_candidate_provider  # noqa: F401
        import nullone_breaking_radar_edge  # noqa: F401
        import nullone_breaking_workflow  # noqa: F401
        import nullone_breaking_workflow_input  # noqa: F401

        modules = set(sys.modules)
        self.assertNotIn("nullone_publish_bridge", modules)
        self.assertNotIn("nullone_publisher_run", modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
