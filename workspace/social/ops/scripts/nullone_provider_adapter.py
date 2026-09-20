#!/usr/bin/env python3
"""Provider-neutral transport adapter contract (issue #111).

Workflows invoke a TRANSPORT through this interface, never a vendor
module directly::

    profile = resolve_provider_profile(role)   # router: sole authority
    outcome = invoke_adapter(profile, call)    # exactly one transport

Normalized across adapters:

- invocation (one-shot, explicit agent/model/workspace/timeout);
- timeout vs reachability vs execution failure (existing semantics
  survive byte-for-byte: execution timeout is never reachability,
  non-retryable execution failure stays distinct);
- provider/model metadata (secret-free);
- capability declaration (from the profile; adapters grant nothing);
- result/outcome.

Failure semantics preserved from both existing families:

- editorial family: ``ProviderExecutionTimeoutError`` (timeout),
  ``ProviderUnreachableError`` (reachability), ``BridgeError``
  (execution);
- role family: ``RoleExecutionTimeoutError`` (timeout),
  ``RoleUnreachableError`` (reachability), ``BridgeError``
  (execution);
- binary-resolution failures are execution failures, never
  reachability (PR116 semantics).

NO SILENT FALLBACK: ``invoke_adapter`` runs exactly the profile's
transport. If it fails, the classified error propagates and the
caller fails closed. A different transport is NEVER attempted
inside a run; fallback needs a separately reviewed policy.

No provider outage may change publication truth: adapters return
structured outcomes or raise; they never write publication state.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
    failure.
    """

    if isinstance(exc, (EditorialTimeout, RoleTimeout)):
        return TIMEOUT
    if isinstance(exc, (EditorialUnreachable, RoleUnreachable)):
        return UNREACHABLE
    return EXECUTION


@dataclass(frozen=True)
class AdapterCall:
    """One normalized transport invocation (no secrets)."""

    prompt: str
    workspace: Path
    agent: str
    timeout_seconds: int


@dataclass(frozen=True)
class AdapterOutcome:
    """Secret-free record of exactly what executed."""

    role: str
    transport: str
    model: str
    outcome: str
    error_kind: str = ""


def _invoke_opencode_cycle(profile: ProviderProfile, call: AdapterCall) -> None:
    from nullone_opencode_binary import resolve_opencode_binary
    from nullone_opencode_role import build_opencode_command, run_opencode_cycle

    cmd = build_opencode_command(
        prompt=call.prompt,
        workspace=call.workspace,
        agent=call.agent,
        model=profile.model,
        binary=resolve_opencode_binary(),
    )
    run_opencode_cycle(
        cmd, cwd=call.workspace, timeout=call.timeout_seconds, role=profile.role
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


def invoke_adapter(profile: ProviderProfile, call: AdapterCall) -> AdapterOutcome:
    """Run exactly the profile's transport; never fall back.

    Returns an outcome on success; raises the original classified
    transport error on failure (caller fails closed). Unknown
    transport fails closed here too (defense in depth behind the
    router, which already rejects it).
    """

    try:
        invoker = _ADAPTERS[profile.transport]
    except KeyError as e:
        raise ProviderRoutingError(
            "Provider routing misconfigured: transport"
        ) from e
    if not call.prompt.strip() or not call.agent.strip():
        raise ProviderRoutingError("Provider routing misconfigured: call")
    invoker(profile, call)
    return AdapterOutcome(
        role=profile.role,
        transport=profile.transport,
        model=profile.model,
        outcome="COMPLETED",
    )


def self_test() -> int:
    from nullone_provider_router import (
        ROLE_DRAFT_FACTORY,
        ROLE_MORNING_EDITORIAL,
        resolve_provider_profile,
    )

    # Registry shape only; no subprocess, no model calls.
    assert set(_ADAPTERS) == {TRANSPORT_OPENCODE, TRANSPORT_CLAUDE}

    # Classification parity across both existing error families.
    assert classify_adapter_error(EditorialTimeout("t")) == TIMEOUT
    assert classify_adapter_error(RoleTimeout("t")) == TIMEOUT
    assert classify_adapter_error(EditorialUnreachable("u")) == UNREACHABLE
    assert classify_adapter_error(RoleUnreachable("u")) == UNREACHABLE
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
            AdapterCall(prompt="p", workspace=Path("/tmp"), agent="a", timeout_seconds=1),
        )
    except ProviderRoutingError:
        pass
    else:
        raise AssertionError("unknown transport did not fail closed")

    # Blank prompt/agent fails closed before any transport runs.
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
                AdapterCall(prompt=" ", workspace=Path("/tmp"), agent="a", timeout_seconds=1),
            )
        except ProviderRoutingError:
            pass
        else:
            raise AssertionError("blank prompt did not fail closed")
        assert calls == []
    finally:
        del _ADAPTERS["__probe__"]

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
