#!/usr/bin/env python3
"""Static capability-negative guards for the #59 application layer.

Mirrors `tests/test_story_workflow_capability_negative.py`'s method: proves
by source inspection (not merely by review) that the #59
`MorningWorkflow`/`AnalyticsWorkflow` application/domain modules have no
executable capability to shell out, invoke OpenClaw/Zernio/Claude, publish,
approve, schedule, create Zernio drafts, or read the Daily Analytics secret
environment variable name. Checks target concrete, executable identifiers
rather than bare architectural vocabulary, since these modules' own
docstrings legitimately *describe* the OpenClaw/Zernio/Claude boundary they
must not cross.

Infrastructure adapter files (Claude CLI invocation, the AnalyticsProvider
production-factory placeholder, and the OpenClaw scheduler edge) are
explicitly excluded from the "no subprocess/no provider names" checks --
that capability (or, for the factory placeholder, that *documentation* of
the pending secret name) is their entire purpose -- but are still checked
for the narrower "never publish/schedule/process approval callbacks"
guarantee every #59 module must uphold, and the factory placeholder is
separately checked for the stronger "never actually reads the secret"
guarantee.
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
    correctly documents."""

    stripped = _TRIPLE_QUOTED_RE.sub("", source)
    stripped = _LINE_COMMENT_RE.sub("", stripped)
    return stripped


# Application/domain layer -- must have zero subprocess/provider-transport
# capability at all, and must never reference the Analytics secret env
# var name.
APPLICATION_MODULES = (
    "nullone_morning_workflow.py",
    "nullone_analytics_workflow.py",
    "nullone_scheduled_workflow_support.py",
    "nullone_schedule_registry.py",
    "nullone_scheduled_occurrence_authority.py",
)

# Infrastructure adapters -- legitimately shell out / document the pending
# secret name (that is each one's entire purpose) -- checked separately,
# only for the narrower publish/schedule/approval-callback-processing
# guarantee (and, for the factory placeholder, the stronger "never actually
# reads the secret" guarantee below).
INFRASTRUCTURE_ADAPTER_MODULES = (
    "nullone_claude_editorial_provider.py",
    "nullone_analytics_provider_factory.py",
    "nullone_openclaw_scheduler_adapter.py",
    "nullone-scheduled-run.py",
    "nullone_scheduled_run_dispatch.py",
    "nullone-scheduled-wakeup.py",
)

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

# Concrete publisher-module identifiers -- never imported/referenced by the
# #59 application layer or its infrastructure adapters.
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

# The exact approval-callback-processing marker the approval agent sends to
# the publisher (agents/approval/AGENTS.md) -- #59 must never process or
# emit this.
FORBIDDEN_APPROVAL_CALLBACK_PROCESSING = ("PUBLISH_AUTHORIZED",)

# The Daily Analytics production secret's exact environment variable name --
# must never appear in the application layer at all (section 33/34).
ANALYTICS_SECRET_ENV_VAR = "ZERNIO_ANALYTICS_API_TOKEN"


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
            self.assertNotIn("OpenClawTelegramTransport", source)

    def test_no_zernio_mcp_tool_names(self):
        for filename in APPLICATION_MODULES:
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            for forbidden in FORBIDDEN_ZERNIO_TOOL_NAMES:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never reference Zernio tool {forbidden!r}",
                )

    def test_no_analytics_secret_env_var_name(self):
        """Analytics secret env name must remain absent from the
        application layer (section 33) -- checked against the FULL source
        (including docstrings), since even *mentioning* the exact secret
        name in application-layer prose is disallowed by design; only the
        dedicated #61 seam module and the #29 adapter itself may know it."""

        for filename in APPLICATION_MODULES:
            source = (SCRIPTS / filename).read_text(encoding="utf-8")
            self.assertNotIn(
                ANALYTICS_SECRET_ENV_VAR,
                source,
                msg=f"{filename} must never reference {ANALYTICS_SECRET_ENV_VAR!r}",
            )

    def test_no_cron_or_job_uuid_syntax(self):
        for filename in APPLICATION_MODULES:
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            for forbidden in ("import sched", "APScheduler", "crontab", "croniter", "openclaw_job_id"):
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never reference {forbidden!r}",
                )


class NoPublisherOrApprovalCapabilityAnywhereTests(unittest.TestCase):
    """Applies to BOTH the application layer and the infrastructure
    adapters: neither may ever gain publish/schedule/approval-processing
    capability."""

    def test_no_publisher_references(self):
        for filename in (*APPLICATION_MODULES, *INFRASTRUCTURE_ADAPTER_MODULES):
            source = (SCRIPTS / filename).read_text(encoding="utf-8")
            for forbidden in FORBIDDEN_PUBLISHER_REFERENCES:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never reference {forbidden!r}",
                )

    def test_no_approval_callback_processing(self):
        for filename in (*APPLICATION_MODULES, *INFRASTRUCTURE_ADAPTER_MODULES):
            source = (SCRIPTS / filename).read_text(encoding="utf-8")
            for forbidden in FORBIDDEN_APPROVAL_CALLBACK_PROCESSING:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never process/emit {forbidden!r}",
                )

    def test_no_zernio_draft_creation_anywhere(self):
        for filename in (*APPLICATION_MODULES, *INFRASTRUCTURE_ADAPTER_MODULES):
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            self.assertNotIn("create_review_draft", source)
            self.assertNotIn("DraftProvider(", source)

    def test_no_module_imports_publisher_at_runtime(self):
        import nullone_analytics_workflow  # noqa: F401
        import nullone_morning_workflow  # noqa: F401
        import nullone_schedule_registry  # noqa: F401
        import nullone_scheduled_occurrence_authority  # noqa: F401
        import nullone_scheduled_workflow_support  # noqa: F401

        module_names = set(sys.modules.keys())
        for forbidden in (
            "nullone-publish-bridge",
            "nullone-publisher-run",
            "nullone_publish_bridge",
            "nullone_publisher_run",
        ):
            self.assertNotIn(forbidden, module_names)


class ProviderSecretPlaceholderNeverReadsSecretTests(unittest.TestCase):
    """The #61 seam placeholder must document, but never actually read, the
    real secret -- checked at both source and runtime-behavior level."""

    def test_factory_module_never_calls_os_environ(self):
        source = code_only(
            (SCRIPTS / "nullone_analytics_provider_factory.py").read_text(encoding="utf-8")
        )
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)

    def test_factory_placeholder_raises_without_reading_environment(self):
        import os
        from unittest import mock

        import nullone_analytics_provider_factory as factory

        with mock.patch.dict(os.environ, {"ZERNIO_ANALYTICS_API_TOKEN": "should-never-be-read"}):
            with self.assertRaises(factory.ProviderSecretWiringPendingError) as ctx:
                factory.build_production_analytics_provider()
            self.assertIn("PROVIDER_SECRET_WIRING_PENDING_61", str(ctx.exception))
            self.assertNotIn("should-never-be-read", str(ctx.exception))


class NoScheduleFrameworkTests(unittest.TestCase):
    """#59 must not introduce a scheduler framework -- only the narrow
    scheduler-invocation value contract and the narrow OpenClaw mapping
    edge."""

    def test_openclaw_adapter_has_no_scheduling_capability(self):
        source = code_only(
            (SCRIPTS / "nullone_openclaw_scheduler_adapter.py").read_text(encoding="utf-8")
        )
        for forbidden in ("import sched", "APScheduler", "crontab", "croniter", "subprocess"):
            self.assertNotIn(
                forbidden,
                source,
                msg=f"OpenClaw scheduler edge adapter must never reference {forbidden!r}",
            )


# Modules the #59 remaining-scope occurrence-authority work introduced.
# Neither may hold any persistence/database/queue capability of its own --
# #28/#29's existing per-occurrence lock and persisted #27 result already
# make replay/concurrency safe once these compute a `scheduled_for`; see
# each module's own docstring "non-goals" section.
OCCURRENCE_AUTHORITY_MODULES = (
    "nullone_schedule_registry.py",
    "nullone_scheduled_occurrence_authority.py",
)

FORBIDDEN_PERSISTENCE_TOKENS = (
    "sqlite3",
    "psycopg2",
    "pymongo",
    "redis",
    "CREATE TABLE",
    "INSERT INTO",
    "fcntl",
    "shelve",
    "pickle",
)


class NoNewSchedulerLedgerOrStateTests(unittest.TestCase):
    """Regression for #59 remaining-scope section 33/9: the M0 schedule
    registry and occurrence authority are repo-owned desired-state
    code/config, not a new mutable runtime persistence layer -- proven
    both by absence of any persistence-capability token and by these
    modules never opening a file at all."""

    def test_no_persistence_capability_tokens(self):
        for filename in OCCURRENCE_AUTHORITY_MODULES:
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            for forbidden in FORBIDDEN_PERSISTENCE_TOKENS:
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"{filename} must never reference {forbidden!r} -- no new scheduler ledger",
                )

    def test_no_filesystem_open_capability(self):
        for filename in OCCURRENCE_AUTHORITY_MODULES:
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            self.assertNotIn(
                "open(",
                source,
                msg=f"{filename} must have zero file I/O capability -- pure function/config only",
            )

    def test_no_subprocess_or_network(self):
        for filename in OCCURRENCE_AUTHORITY_MODULES:
            source = code_only((SCRIPTS / filename).read_text(encoding="utf-8"))
            for forbidden in ("subprocess", "socket", "requests.", "urllib", "http.client"):
                self.assertNotIn(forbidden, source, msg=f"{filename} must never reference {forbidden!r}")

    def test_occurrence_authority_is_a_pure_function_of_its_own_arguments(self):
        """Deterministic replay proof at the static-capability level: the
        resolver takes no ambient/ledger state, only its own keyword
        arguments and the immutable schedule registry."""

        import inspect

        import nullone_scheduled_occurrence_authority as authority

        signature = inspect.signature(authority.resolve_scheduled_occurrence)
        self.assertEqual(set(signature.parameters), {"workflow_id", "source", "triggered_at"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
