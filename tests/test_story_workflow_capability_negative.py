#!/usr/bin/env python3
"""Static capability-negative guards for the #62 application layer.

Proves by source inspection (not merely by review) that the #62
application/domain modules have no executable capability to shell out,
invoke OpenClaw/Zernio, import the publisher/publish-bridge wrappers, or
process approval callbacks. Checks target concrete, executable identifiers
(an actual MCP tool name, an actual subprocess/import capability, an
actual publisher-module name) rather than bare architectural vocabulary --
these modules' own docstrings legitimately *describe* the OpenClaw/Zernio
boundary they must not cross (matching
`docs/architecture/nullone-application-runtime.md`'s own prose), so a
naive "must not contain the word openclaw" check would flag correct,
necessary documentation as a false violation.

Infrastructure adapter files (the Telegram/OpenClaw transport) are
explicitly excluded from the "no subprocess/no provider names" checks --
that capability is their entire purpose -- but are still checked for the
narrower "never publish/schedule/process approval callbacks" guarantee
every #62 module must uphold.
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


def code_only(source: str) -> str:
    """Strip docstrings/comments so capability checks scan executable code
    only, not architectural prose describing a boundary this same module
    correctly documents (e.g. "no subprocess call" in a docstring must not
    trip a "no subprocess" capability check)."""

    stripped = _TRIPLE_QUOTED_RE.sub("", source)
    stripped = _LINE_COMMENT_RE.sub("", stripped)
    return stripped

# Application/domain layer -- must have zero subprocess/provider-transport
# capability at all.
APPLICATION_MODULES = (
    "nullone_story_workflow.py",
    "nullone_story_candidate_provider.py",
    "nullone_review_delivery.py",
    "nullone_scheduler_invocation.py",
)

# Infrastructure adapter -- legitimately shells out to `openclaw` (that is
# its entire purpose) -- checked separately, only for the narrower
# publish/schedule/approval-callback-processing guarantee.
INFRASTRUCTURE_ADAPTER_MODULE = "nullone_telegram_review_delivery_adapter.py"

# Real Zernio MCP tool/endpoint names (concrete capability, never
# architectural prose) that must never appear in the application layer.
FORBIDDEN_ZERNIO_TOOL_NAMES = (
    "posts_create",
    "posts_publish_now",
    "posts_update",
    "posts_delete",
    "posts_unpublish_post",
    "posts_get",
    "media_get_media_presigned_url",
    "call_tool(",
    "mcp__zernio",
)

# Concrete publisher-module identifiers -- never imported/referenced by
# the #62 application layer or its infrastructure adapter.
FORBIDDEN_PUBLISHER_REFERENCES = (
    "nullone-publish-bridge",
    "nullone-publisher-run",
    "nullone_publish_bridge",
    "nullone_publisher_run",
    "posts_publish_now",
    "posts_delete",
    "posts_unpublish_post",
    "publish_now",
)

# The exact approval-callback-processing marker the approval agent sends
# to the publisher (agents/approval/AGENTS.md) -- #62 must never process
# or emit this; it only preserves button *values* verbatim inside an
# unopened payload, never interprets an inbound callback itself.
FORBIDDEN_APPROVAL_CALLBACK_PROCESSING = ("PUBLISH_AUTHORIZED",)


class ApplicationLayerHasNoTransportCapabilityTests(unittest.TestCase):
    """The application/domain layer must be unable to shell out or call a
    provider API at all -- not merely "chooses not to" in this instance."""

    def test_no_subprocess_import_or_usage(self):
        for filename in APPLICATION_MODULES:
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            self.assertNotIn(
                "subprocess",
                source,
                msg=f"{filename} must have no subprocess capability at all",
            )

    def test_no_openclaw_import_or_cli_invocation(self):
        for filename in APPLICATION_MODULES:
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            self.assertNotIn("import openclaw", source)
            self.assertNotIn("openclaw message send", source)

    def test_no_zernio_mcp_tool_names(self):
        for filename in APPLICATION_MODULES:
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            for forbidden in FORBIDDEN_ZERNIO_TOOL_NAMES:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never reference Zernio tool {forbidden!r}",
                )


class NoPublisherOrApprovalCapabilityAnywhereTests(unittest.TestCase):
    """Applies to BOTH the application layer and the infrastructure
    adapter: neither may ever gain publish/schedule/approval-processing
    capability, regardless of how much provider detail the adapter
    otherwise legitimately contains."""

    def test_no_publisher_references(self):
        for filename in (*APPLICATION_MODULES, INFRASTRUCTURE_ADAPTER_MODULE):
            source = (SCRIPTS / filename).read_text(encoding="utf-8")
            for forbidden in FORBIDDEN_PUBLISHER_REFERENCES:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never reference {forbidden!r}",
                )

    def test_no_approval_callback_processing(self):
        for filename in (*APPLICATION_MODULES, INFRASTRUCTURE_ADAPTER_MODULE):
            source = (SCRIPTS / filename).read_text(encoding="utf-8")
            for forbidden in FORBIDDEN_APPROVAL_CALLBACK_PROCESSING:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never process/emit {forbidden!r}",
                )

    def test_no_module_imports_publisher_at_runtime(self):
        import nullone_review_delivery  # noqa: F401
        import nullone_scheduler_invocation  # noqa: F401
        import nullone_story_candidate_provider  # noqa: F401
        import nullone_story_workflow  # noqa: F401
        import nullone_telegram_review_delivery_adapter  # noqa: F401

        module_names = set(sys.modules.keys())
        for forbidden in (
            "nullone-publish-bridge",
            "nullone-publisher-run",
            "nullone_publish_bridge",
            "nullone_publisher_run",
        ):
            self.assertNotIn(forbidden, module_names)


class NoScheduleFrameworkTests(unittest.TestCase):
    """#62 must not introduce a scheduler framework -- only the narrow
    scheduler-invocation value contract."""

    def test_scheduler_invocation_module_has_no_scheduling_capability(self):
        source = (SCRIPTS / "nullone_scheduler_invocation.py").read_text(encoding="utf-8")
        for forbidden in ("import sched", "APScheduler", "crontab", "croniter"):
            self.assertNotIn(
                forbidden,
                source,
                msg=f"scheduler-invocation module must never reference {forbidden!r}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
