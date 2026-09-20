#!/usr/bin/env python3
"""Provider-neutral transport adapter contract (issue #111).

Workflows invoke a TRANSPORT through this interface, never a vendor
module directly::

    profile = resolve_provider_profile(role)   # router: sole authority
    outcome = invoke_role_cycle(profile, prompt, workspace)
    writer = make_story_writer(profile)        # story role only

The caller supplies ONLY the invocation material it legitimately
owns (prompt/workspace/editorial context). Agent and timeout derive
exclusively from reviewed role authority:

- agent   <- role_agent(profile.role)      (router, pinned per role)
- timeout <- profile.timeout_seconds       (router, pinned per role)

There is no caller override: AdapterCall has no agent/timeout
fields (proven by test). Vendor-specific imports terminate HERE:
workflow/wrapper/factory layers import only this module (plus the
router and neutral helpers).

Normalized across adapters:

- invocation (one-shot, explicit agent/model/workspace/timeout);
- timeout vs reachability vs execution failure (existing semantics
  survive byte-for-byte: execution timeout is never reachability,
  non-retryable execution failure stays distinct; the Story
  OpenCode error family normalizes through the same contract);
- provider/model metadata (secret-free; reported == executed);
- capability declaration (from the profile; adapters grant nothing);
- result/outcome.

NO SILENT FALLBACK: the registry runs exactly the profile's
transport. If it fails, the classified error propagates and the
caller fails closed. A different transport is NEVER attempted
inside a run; fallback needs a separately reviewed policy.

No provider outage may change publication truth: adapters return
structured outcomes or raise; they never write publication state.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable

import nullone_opencode_binary as opencode_binary
import nullone_opencode_role as opencode_role
from nullone_bridge_common import BridgeError
from nullone_editorial_runtime import (
    ProviderExecutionTimeoutError as EditorialTimeout,
    ProviderUnreachableError as EditorialUnreachable,
)
from nullone_opencode_binary import OpenCodeBinaryResolutionError
from nullone_opencode_role import (
    RoleExecutionTimeoutError as RoleTimeout,
    RoleUnreachableError as RoleUnreachable,
)
from nullone_provider_router import (
    ProviderProfile,
    ProviderRoutingError,
    TRANSPORT_CLAUDE,
    TRANSPORT_OPENCODE,
    role_agent,
)

TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
EXECUTION = "execution"


def classify_adapter_error(exc: BaseException) -> str:
    """Normalize any adapter failure to timeout/unreachable/execution.

    Whole-process wall-clock expiry is ALWAYS timeout (never proof
    of unreachability); reachability-pattern failures are ALWAYS
    unreachable (retryable per workflow policy); everything else --
    including binary resolution -- is a non-retryable execution
    failure. Covers the editorial family, the shared role family,
    AND the Story OpenCode family (same contract, unchanged
    fail-closed domain behavior).
    """

    from nullone_opencode_story_provider import (
        StoryWriterTimeoutError,
        StoryWriterUnreachableError,
    )

    if isinstance(exc, (EditorialTimeout, RoleTimeout, StoryWriterTimeoutError)):
        return TIMEOUT
    if isinstance(exc, (EditorialUnreachable, RoleUnreachable, StoryWriterUnreachableError)):
        return UNREACHABLE
    return EXECUTION


@dataclass(frozen=True)
class AdapterCall:
    """One normalized transport invocation (no secrets).

    The caller owns ONLY role/prompt/workspace. Agent and timeout
    are derived from reviewed role authority inside `invoke_adapter`
    and cannot be overridden by the caller (no such fields exist).
    """

    role: str
    prompt: str
    workspace: Path


CALLER_OWNED_FIELDS = ("role", "prompt", "workspace")


def _invoke_opencode_cycle(profile: ProviderProfile, call: AdapterCall) -> None:
    # PR116: production argv always carries the resolved absolute
    # binary; the pure builder default ("opencode") exists only so
    # offline tests stay filesystem-independent.
    cmd = build_adapter_command(
        profile,
        prompt=call.prompt,
        workspace=call.workspace,
        binary=opencode_binary.resolve_opencode_binary(),
    )
    opencode_role.run_opencode_cycle(
        cmd,
        cwd=call.workspace,
        timeout=profile.timeout_seconds,
        role=profile.role,
    )


def _invoke_claude_cycle(profile: ProviderProfile, call: AdapterCall) -> None:
    from nullone_claude_editorial_provider import default_invoke_provider

    # The Claude transport owns one reviewed cycle shape; the model
    # travels from the profile (rollback default "sonnet" preserves
    # the reviewed command when unpinned).
    default_invoke_provider(model=profile.model)


_ADAPTERS: dict[str, Callable[[ProviderProfile, AdapterCall], None]] = {
    TRANSPORT_OPENCODE: _invoke_opencode_cycle,
    TRANSPORT_CLAUDE: _invoke_claude_cycle,
}


def build_adapter_command(
    profile: ProviderProfile,
    *,
    prompt: str,
    workspace: Path | str,
    binary: str = "opencode",
) -> list[str]:
    """Build the deterministic transport argv for a profile (pure).

    Agent derives from reviewed role authority; model travels from
    the profile. Used by wrapper self-tests so the execution layer
    never imports vendor builders directly.
    """

    if profile.transport == TRANSPORT_OPENCODE:
        return opencode_role.build_opencode_command(
            prompt=prompt,
            workspace=workspace,
            agent=role_agent(profile.role),
            model=profile.model,
            binary=binary,
        )
    if profile.transport == TRANSPORT_CLAUDE:
        from nullone_claude_editorial_provider import build_claude_command

        return build_claude_command(prompt=prompt, model=profile.model)
    raise ProviderRoutingError("Provider routing misconfigured: transport")


def invoke_adapter(profile: ProviderProfile, call: AdapterCall) -> AdapterOutcome:
    """Run exactly the profile's transport; never fall back.

    Agent and timeout derive from reviewed role authority
    (role_agent / profile.timeout_seconds). Returns an outcome on
    success; raises the original classified transport error on
    failure (caller fails closed). Unknown transport fails closed
    here too (defense in depth behind the router, which already
    rejects it).
    """

    try:
        invoker = _ADAPTERS[profile.transport]
    except KeyError as e:
        raise ProviderRoutingError(
            "Provider routing misconfigured: transport"
        ) from e
    if call.role != profile.role:
        raise ProviderRoutingError("Provider routing misconfigured: role")
    if not call.prompt.strip():
        raise ProviderRoutingError("Provider routing misconfigured: call")
    invoker(profile, call)
    return AdapterOutcome(
        role=profile.role,
        transport=profile.transport,
        model=profile.model,
        outcome="COMPLETED",
    )


def invoke_role_cycle(
    profile: ProviderProfile, prompt: str, workspace: Path | str
) -> AdapterOutcome:
    """One normalized role cycle: the workflow's single entrypoint."""

    return invoke_adapter(
        profile,
        AdapterCall(
            role=profile.role,
            prompt=prompt,
            workspace=Path(workspace),
        ),
    )


def make_story_writer(profile: ProviderProfile) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Build the Story writer the profile selects (vendor imports end here).

    OpenCode writer executes exactly profile.model with the
    profile timeout; Claude writer executes exactly profile.model
    (Haiku default "haiku" when the router leaves it unpinned --
    reported == executed, never faked).
    """

    if profile.role != "story_writer":
        raise ProviderRoutingError("Provider routing misconfigured: role")
    if profile.transport == TRANSPORT_OPENCODE:
        from nullone_opencode_story_provider import OpenCodeStoryWriter

        return OpenCodeStoryWriter(
            model=profile.model, timeout=profile.timeout_seconds
        )
    if profile.transport == TRANSPORT_CLAUDE:
        from nullone_story_pipeline import HaikuStoryWriter

        return HaikuStoryWriter(model=profile.model)
    raise ProviderRoutingError("Provider routing misconfigured: transport")


@dataclass(frozen=True)
class AdapterOutcome:
    """Secret-free record of exactly what executed."""

    role: str
    transport: str
    model: str
    outcome: str
    error_kind: str = ""


def self_test() -> int:
    from nullone_provider_router import (
        ROLE_DRAFT_FACTORY,
        ROLE_MORNING_EDITORIAL,
        resolve_provider_profile,
    )

    # Registry shape only; no subprocess, no model calls.
    assert set(_ADAPTERS) == {TRANSPORT_OPENCODE, TRANSPORT_CLAUDE}

    # Caller-owned surface is exactly role/prompt/workspace: no
    # agent/timeout override is representable.
    assert tuple(f.name for f in fields(AdapterCall)) == CALLER_OWNED_FIELDS

    # Classification parity across all three error families.
    from nullone_opencode_story_provider import (
        StoryWriterTimeoutError,
        StoryWriterUnreachableError,
    )

    assert classify_adapter_error(EditorialTimeout("t")) == TIMEOUT
    assert classify_adapter_error(RoleTimeout("t")) == TIMEOUT
    assert classify_adapter_error(StoryWriterTimeoutError("t")) == TIMEOUT
    assert classify_adapter_error(EditorialUnreachable("u")) == UNREACHABLE
    assert classify_adapter_error(RoleUnreachable("u")) == UNREACHABLE
    assert classify_adapter_error(StoryWriterUnreachableError("u")) == UNREACHABLE
    assert classify_adapter_error(BridgeError("e")) == EXECUTION
    assert classify_adapter_error(OpenCodeBinaryResolutionError("b")) == EXECUTION
    assert classify_adapter_error(ValueError("v")) == EXECUTION

    # Unknown transport fails closed (defense in depth).
    profile = resolve_provider_profile(ROLE_MORNING_EDITORIAL, env={})
    bad = ProviderProfile(
        role=profile.role,
        transport="openclaw",
        model=profile.model,
        capabilities=profile.capabilities,
        timeout_seconds=profile.timeout_seconds,
    )
    try:
        invoke_adapter(
            bad,
            AdapterCall(role=bad.role, prompt="p", workspace=Path("/tmp")),
        )
    except ProviderRoutingError:
        pass
    else:
        raise AssertionError("unknown transport did not fail closed")

    # Blank prompt fails closed before any transport runs.
    calls: list[str] = []
    _ADAPTERS["__probe__"] = lambda _p, _c: calls.append("ran")  # type: ignore[assignment]
    try:
        probe = ProviderProfile(
            role=ROLE_DRAFT_FACTORY,
            transport="__probe__",
            model="probe/model",
            capabilities=(),
            timeout_seconds=1,
        )
        try:
            invoke_adapter(
                probe,
                AdapterCall(role=probe.role, prompt=" ", workspace=Path("/tmp")),
            )
        except ProviderRoutingError:
            pass
        else:
            raise AssertionError("blank prompt did not fail closed")
        assert calls == []
    finally:
        del _ADAPTERS["__probe__"]

    # Role/call mismatch fails closed.
    try:
        invoke_adapter(
            profile,
            AdapterCall(role=ROLE_DRAFT_FACTORY, prompt="p", workspace=Path("/tmp")),
        )
    except ProviderRoutingError:
        pass
    else:
        raise AssertionError("role mismatch did not fail closed")

    print("PROVIDER_ADAPTER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne provider adapter contract")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
