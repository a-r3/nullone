#!/usr/bin/env python3
"""Static capability-negative guards for the #80 Breaking production layer.

Mirrors the #79 method: proves by source inspection that the new #80
runtime modules have no executable capability to publish, approve,
schedule, message users, mutate the publication ledger, send Telegram
outside ReviewDelivery, or publish via Zernio. Breaking stops at human
review preview.
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
    stripped = _TRIPLE_QUOTED_RE.sub("", source)
    stripped = _LINE_COMMENT_RE.sub("", stripped)
    stripped = _SELF_TEST_RE.split(stripped)[0]
    stripped = _MAIN_RE.split(stripped)[0]
    return stripped


# #80 application modules: no transport/subprocess capability at all.
BREAKING_APPLICATION_MODULES = (
    "nullone_breaking_scan_authority.py",
    "nullone_breaking_candidate_runner.py",
)

# #80 infrastructure wiring (agent commit edge + spool consumer): these
# legitimately reference the reviewed concrete adapters -- their entire
# purpose, exactly like the #59 dispatch wiring -- and are checked only
# for the narrower never-publish/schedule/approval/ledger guarantee.
BREAKING_INFRASTRUCTURE_MODULES = (
    "nullone-breaking-scan.py",
    "nullone-breaking-consume.py",
)

FORBIDDEN_TRANSPORT_TOKENS = (
    "subprocess",
    "import openclaw",
    "openclaw message send",
    "OpenClawTelegramTransport",
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
    "send_message",
    "answer_comment",
)

FORBIDDEN_SCHEDULING_TOKENS = (
    "import sched",
    "APScheduler",
    "crontab",
    "croniter",
    "threading.Timer",
)


class BreakingProductionHasNoPublishCapabilityTests(unittest.TestCase):
    def _application_sources(self):
        for filename in BREAKING_APPLICATION_MODULES:
            yield filename, production_code_only(
                (SCRIPTS / filename).read_text(encoding="utf-8")
            )

    def _all_sources(self):
        for filename in (*BREAKING_APPLICATION_MODULES, *BREAKING_INFRASTRUCTURE_MODULES):
            yield filename, production_code_only(
                (SCRIPTS / filename).read_text(encoding="utf-8")
            )

    def test_no_transport_or_subprocess_tokens(self):
        for filename, source in self._application_sources():
            for forbidden in FORBIDDEN_TRANSPORT_TOKENS:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never contain {forbidden!r}",
                )

    def test_no_publication_messaging_or_scheduling_capability(self):
        for filename, source in self._all_sources():
            for forbidden in (
                *FORBIDDEN_PUBLISH_TOKENS,
                *FORBIDDEN_SCHEDULING_TOKENS,
            ):
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never contain {forbidden!r}",
                )

    def test_no_ledger_mutation_capability(self):
        for filename, source in self._all_sources():
            self.assertNotIn("publish-ledger", source)
            self.assertNotIn("append_ledger", source)
            self.assertNotIn("record_published", source)

    def test_no_markdown_source_reads(self):
        for filename in BREAKING_APPLICATION_MODULES:
            source = production_code_only(
                (SCRIPTS / filename).read_text(encoding="utf-8")
            )
            self.assertNotIn("candidate-queue", source)
            self.assertNotIn("topic-ledger", source)
            for lineno, line in enumerate(source.splitlines(), start=1):
                if "read_text" in line or "read_bytes" in line or "open(" in line:
                    self.assertNotIn(
                        ".md",
                        line,
                        msg=f"{filename}:{lineno} must never open a Markdown file",
                    )

    def test_no_module_imports_publisher_or_transport_at_runtime(self):
        import nullone_breaking_candidate_runner  # noqa: F401
        import nullone_breaking_scan_authority  # noqa: F401

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
