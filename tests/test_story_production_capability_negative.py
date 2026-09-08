#!/usr/bin/env python3
"""Static capability-negative guards for the #79 Story production layer.

Mirrors `tests/test_scheduled_workflows_capability_negative.py`'s method:
proves by source inspection that the new #79 application modules and the
#79 production entrypoint wiring have no executable capability to publish,
approve, schedule, invoke OpenClaw/Zernio/Claude/Telegram, shell out from
the application layer, or steer Story selection by parsing Markdown
(board/queue/ledger). Infrastructure wiring (`run_story_trigger` in
`nullone_scheduled_run_dispatch.py`) legitimately references the reviewed
concrete adapters -- and is checked only for the narrower
never-publish/schedule/approval-callback guarantee -- exactly like the
existing #59 dispatch wiring.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "workspace/social/ops/scripts"
sys.path.insert(0, str(SCRIPTS))

_TRIPLE_QUOTED_RE = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"#.*")
_SELF_TEST_RE = re.compile(r"\ndef self_test\(.*", re.DOTALL)
_MAIN_RE = re.compile(r"\ndef main\(.*", re.DOTALL)


def production_code_only(source: str) -> str:
    """Strip docstrings, comments, and embedded self-test/main data.

    Self-tests legitimately contain fixture strings (board paths, fake
    provider CamelCase names); the capability under test is what the
    production code paths can execute.
    """

    stripped = _TRIPLE_QUOTED_RE.sub("", source)
    stripped = _LINE_COMMENT_RE.sub("", stripped)
    stripped = _SELF_TEST_RE.split(stripped)[0]
    stripped = _MAIN_RE.split(stripped)[0]
    return stripped


# #79 application modules: no transport/subprocess/publication capability.
STORY_APPLICATION_MODULES = (
    "nullone_editorial_candidate_handoff.py",
    "nullone_story_production_provider.py",
    "nullone_story_scheduled_workflow.py",
)

# #79 production entrypoint wiring (mirrors the #59 dispatch pattern).
STORY_WIRING_MODULES = ("nullone_scheduled_run_dispatch.py",)

FORBIDDEN_TRANSPORT_TOKENS = (
    "subprocess",
    "openclaw",
    "call_tool(",
    "mcp__zernio",
    "posts_create",
    "posts_publish_now",
    "posts_update",
    "posts_delete",
    "posts_unpublish_post",
    "posts_get",
    "media_get_media_presigned_url",
)

FORBIDDEN_PUBLISH_TOKENS = (
    "nullone-publish-bridge",
    "nullone-publisher-run",
    "nullone_publish_bridge",
    "nullone_publisher_run",
    "publish_now",
    "PUBLISH_AUTHORIZED",
    "final_publish",
)

# Concrete scheduling-job capability markers (never bare architectural
# words like "scheduled_for", which the invocation contract requires).
FORBIDDEN_SCHEDULING_TOKENS = (
    "cron",
    "automations",
    "apscheduler",
    "threading.Timer",
)

# Markdown source markers the production Story path must never read.
FORBIDDEN_MARKDOWN_SOURCES = (
    "candidate-queue",
    "topic-ledger",
    "editorial-board.md",
)


class StoryApplicationHasNoTransportCapabilityTests(unittest.TestCase):
    def test_no_transport_or_subprocess_tokens(self):
        for filename in STORY_APPLICATION_MODULES:
            source = production_code_only(
                (SCRIPTS / filename).read_text(encoding="utf-8")
            )
            for forbidden in FORBIDDEN_TRANSPORT_TOKENS:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never contain {forbidden!r}",
                )

    def test_no_publication_approval_or_scheduling_capability(self):
        for filename in (*STORY_APPLICATION_MODULES, *STORY_WIRING_MODULES):
            source = production_code_only(
                (SCRIPTS / filename).read_text(encoding="utf-8")
            )
            for forbidden in (
                *FORBIDDEN_PUBLISH_TOKENS,
                *FORBIDDEN_SCHEDULING_TOKENS,
            ):
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never contain {forbidden!r}",
                )

    def test_no_markdown_source_reads(self):
        for filename in STORY_APPLICATION_MODULES:
            source = production_code_only(
                (SCRIPTS / filename).read_text(encoding="utf-8")
            )
            for forbidden in FORBIDDEN_MARKDOWN_SOURCES:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never read {forbidden!r}",
                )

    def test_no_secret_environment_access(self):
        for filename in STORY_APPLICATION_MODULES:
            source = production_code_only(
                (SCRIPTS / filename).read_text(encoding="utf-8")
            )
            self.assertNotIn("os.environ", source)
            self.assertNotIn("ZERNIO_ANALYTICS_API_TOKEN", source)

    def test_no_module_imports_publisher_or_transport_at_runtime(self):
        import nullone_editorial_candidate_handoff  # noqa: F401
        import nullone_story_production_provider  # noqa: F401
        import nullone_story_scheduled_workflow  # noqa: F401

        module_names = set(sys.modules.keys())
        for forbidden in (
            "nullone-publish-bridge",
            "nullone-publisher-run",
            "nullone_publish_bridge",
            "nullone_publisher_run",
            "nullone_telegram_review_delivery_adapter",
            "nullone_zernio_analytics_adapter",
        ):
            self.assertNotIn(forbidden, module_names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
