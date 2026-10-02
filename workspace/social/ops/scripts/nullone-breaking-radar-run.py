#!/usr/bin/env python3
"""Breaking Radar role runner behind the provider adapter (issue #111).

Command-payload entrypoint:

    python3 social/ops/scripts/nullone-breaking-radar-run.py execute

reads the reviewed `breaking-radar.md` prompt and runs exactly one
DELTA_MONITORING_ONLY cycle for the `breaking_radar` logical role.
Transport, model, agent, and timeout arrive from the role router's
ProviderProfile and execute through the provider adapter registry:
this module imports NO vendor transport module and never chooses
OpenCode or Claude itself. Discovery/research reasoning happens in
the model; handoff commits happen only through the deterministic
`nullone-breaking-scan.py` boundary. On the Claude path the model
has no shell or file-write capability: it returns one structured
result, and deterministic Python writes the report, stages
assessments, and commits through the scan helper. Radar
stops at human review preview: no drafts, no Telegram, no
publication path.

Transport budget (reviewed, transport-only, not domain policy):
600s, mirrored in the router (`ROLE_TIMEOUTS`); the two must stay
equal (proven offline).
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from nullone_bridge_common import BridgeError, WORKSPACE
import nullone_provider_adapter as provider_adapter
import nullone_provider_router as provider_router

try:
    from nullone_claude_radar_provider import (
        ALLOWED_RADAR_REASON_CODES,
        RadarStageError,
    )
except Exception:  # pragma: no cover - import safety net, fail-closed below
    ALLOWED_RADAR_REASON_CODES = frozenset({"UNKNOWN_RADAR_FAILURE"})

    class RadarStageError(BridgeError):  # type: ignore[no-redef]
        def __init__(self, reason_code: str = "UNKNOWN_RADAR_FAILURE", **kwargs):
            self.reason_code = "UNKNOWN_RADAR_FAILURE"
            self.exit_code = None
            super().__init__("Radar stage failure: UNKNOWN_RADAR_FAILURE")


def format_radar_stage_blocked(exc: BaseException) -> str:
    """Safe deterministic BLOCKED line for a RadarStageError.

    Exposes only the stable reason code and, for nonzero Claude
    exits, the numeric exit code. Never includes raw exception
    text, prompts, fetched content, URLs, or secrets.
    """
    code = getattr(exc, "reason_code", "UNKNOWN_RADAR_FAILURE")
    if not isinstance(code, str) or code not in ALLOWED_RADAR_REASON_CODES:
        code = "UNKNOWN_RADAR_FAILURE"
    exit_code = getattr(exc, "exit_code", None)
    if (
        code == "CLAUDE_EXIT_NONZERO"
        and isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
    ):
        return f"ROLE_OUTCOME=BLOCKED reason=RadarStageError code={code} exit={exit_code}"
    return f"ROLE_OUTCOME=BLOCKED reason=RadarStageError code={code}"

ROLE = "breaking-radar"
AGENT = "nullone-breaking-radar"
PROMPT_PATH = WORKSPACE / "social/ops/prompts/breaking-radar.md"

RADAR_TIMEOUT_SECONDS = 600

# Transport appendix: the working directory of the model invocation
# is the workspace root, so the scan helper runs workspace-relative
# (the checked-in prompt text shows the legacy automation-root
# form). Prompt domain behavior is unchanged.
#
# On transports without shell/file-write capability (Claude Radar),
# this appendix additionally overrides the prompt's file-writing and
# helper-invocation instructions: do NOT attempt helper commands and
# do NOT write any files. Author the Markdown report and any staged
# assessments ONLY as the structured result
# (`mode` / `report_markdown` / `assessments`); the deterministic
# wrapper writes the report, stages assessments, and commits through
# the scan helper. Never supply filesystem paths or commands.
TRANSPORT_APPENDIX = """

Transport note: this run executes with the workspace root as its
working directory. The scan helper path is
`social/ops/scripts/nullone-breaking-scan.py`
(workspace-relative); never write handoff files directly, and commit
only through the helper.

If this transport grants no shell or file-write capability, the
helper-invocation and file-writing instructions above do not apply:
return the Markdown report and any staged assessments ONLY as the
structured result object, and never supply filesystem paths or
commands. The deterministic wrapper owns all destinations and
commits.
"""


def build_command(
    *,
    workspace: Path | str | None = None,
    model: str | None = None,
    binary: str = "opencode",
) -> list[str]:
    """Build the deterministic Radar argv (pure, no I/O).

    Delegates to the provider adapter so the reviewed argv shape is
    owned in exactly one place; `model=None` resolves through the
    role router (never a vendor default). Structured-invocation
    transports (Claude Radar) have no flat argv and raise
    ProviderRoutingError here; execution still flows through
    `invoke_role_cycle`.
    """

    resolved_workspace = Path(workspace) if workspace is not None else WORKSPACE
    prompt = PROMPT_PATH.read_text(encoding="utf-8") + TRANSPORT_APPENDIX
    profile = provider_router.resolve_provider_profile(provider_router.ROLE_BREAKING_RADAR)
    if model is not None:
        profile = provider_router.ProviderProfile(
            role=profile.role,
            transport=profile.transport,
            model=model,
            capabilities=profile.capabilities,
            timeout_seconds=profile.timeout_seconds,
            fallback_policy=profile.fallback_policy,
        )
    return provider_adapter.build_adapter_command(
        profile, prompt=prompt, workspace=resolved_workspace, binary=binary
    )


def find_fresh_reports(*, workspace: Path, since_epoch: float) -> list[Path]:
    """Breaking reports created during this invocation (issue #124).

    Pure workspace scan (no slot/date math, no timezone logic): any
    `*-breaking-*.md` under `social/research/daily/` with mtime at or
    after `since_epoch` counts. A transport success that produced no
    report is a hollow COMPLETED and must fail closed downstream.
    """

    daily = workspace / "social/research/daily"
    if not daily.is_dir():
        return []
    fresh: list[Path] = []
    for path in sorted(daily.glob("*-breaking-*.md")):
        try:
            if path.is_file() and path.stat().st_mtime >= since_epoch:
                fresh.append(path)
        except OSError:
            continue
    return fresh


def execute() -> int:
    # Issue #111: the profile (transport/model/timeout) arrives from
    # the role router and executes through the adapter registry.
    # Reviewed values equal today's constants, so production
    # behavior is unchanged.
    try:
        profile = provider_router.resolve_provider_profile(provider_router.ROLE_BREAKING_RADAR)
    except BridgeError as e:
        print(f"ROLE_OUTCOME=BLOCKED reason={type(e).__name__}")
        return 1
    print(provider_router.describe_profile(profile))
    print(provider_router.format_routing_metadata(profile, "STARTED"))
    prompt = PROMPT_PATH.read_text(encoding="utf-8") + TRANSPORT_APPENDIX
    started = time.time()
    try:
        outcome = provider_adapter.invoke_role_cycle(profile, prompt, WORKSPACE)
    except RadarStageError as e:
        print(format_radar_stage_blocked(e))
        print(provider_router.format_routing_metadata(profile, "BLOCKED"))
        return 1
    except BridgeError as e:
        print(f"ROLE_OUTCOME=BLOCKED reason={type(e).__name__}")
        print(provider_router.format_routing_metadata(profile, "BLOCKED"))
        return 1
    print(provider_router.format_routing_metadata(profile, outcome.outcome))
    if not find_fresh_reports(workspace=WORKSPACE, since_epoch=started):
        print("ROLE_OUTCOME=BLOCKED reason=MissingRadarReport")
        print(provider_router.format_routing_metadata(profile, "BLOCKED"))
        return 1
    print("ROLE_OUTCOME=COMPLETED")
    return 0


def self_test() -> int:
    profile = provider_router.resolve_provider_profile(provider_router.ROLE_BREAKING_RADAR)
    assert profile.transport == provider_router.TRANSPORT_CLAUDE, profile
    assert profile.model == provider_router.HAIKU_DEFAULT_MODEL, profile
    assert profile.timeout_seconds == RADAR_TIMEOUT_SECONDS == 600, profile
    assert profile.fallback_policy == provider_router.FALLBACK_NONE, profile
    # Structured invocation has no flat argv: the adapter refuses to
    # build one for Claude Radar (same contract as Weekly).
    try:
        build_command(workspace=Path("/tmp/nullone-radar-self-test"))
    except provider_adapter.ProviderRoutingError:
        pass
    else:
        raise AssertionError("Claude Radar must not expose a flat argv")
    assert PROMPT_PATH.name == "breaking-radar.md"

    print("BREAKING_RADAR_RUN_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="NullOne Breaking Radar role")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("execute")
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "execute":
        return execute()
    if args.command == "self-test":
        return self_test()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
