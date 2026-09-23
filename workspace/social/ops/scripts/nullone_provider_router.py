#!/usr/bin/env python3
"""Deterministic logical-role -> provider-profile router (issue #111).

NullOne workflows depend on LOGICAL ROLES, never on vendor names.
This module is the SOLE authority mapping a role to its provider
profile::

    workflow ("I need role X")
        -> resolve_provider_profile(role)
        -> ProviderProfile(role, transport, model, capabilities,
                            timeout_seconds, fallback_policy)
        -> transport adapter (opencode | claude)
        -> provider/model endpoint

Concept separation (see docs/architecture/provider-role-routing.md):

- ROLE: a reviewed NullOne workflow capability
  (``morning_editorial`` etc.).
- TRANSPORT: the CLI/API vehicle (``opencode`` | ``claude``).
  OpenCode is a TRANSPORT, not the final LLM provider.
- MODEL/PROVIDER ENDPOINT: the ``provider/model`` identifier the
  transport is told to use (``opencode/muse-spark-...``,
  ``openrouter/...``, ``anthropic/...`` ...). Non-secret.
- CAPABILITIES: frozen least-privilege labels per role. Routing a
  different model NEVER widens them; enforcement stays in the
  reviewed per-role agents/prompts.

Configuration (no secrets, ever):

1. Checked-in ``workspace/social/ops/provider-routing.json``
   (schema ``nullone.provider-routing.v1``) carries transport+model
   per role. Unknown role keys, unknown transports, blank/invalid
   models all FAIL CLOSED at load.
2. Explicit per-role environment override (deployment switch
   without workflow rewrites):
   ``NULLONE_ROLE_<ROLE>_TRANSPORT`` / ``NULLONE_ROLE_<ROLE>_MODEL``.
3. Deprecated compatibility (documented, tested, reviewed for
   removal): ``NULLONE_EDITORIAL_PROVIDER`` (morning transport),
   ``NULLONE_STORY_PROVIDER`` (story transport),
   ``NULLONE_OPENCODE_MODEL`` (model for opencode-transport roles).

Precedence: per-role env > legacy env > checked-in JSON.
Production override is therefore always explicit and visible.

Timeouts and capabilities are pinned in code (not JSON) so the
mapping file cannot silently change execution budgets or privilege:
timeouts equal the reviewed per-role constants; any drift fails the
offline suite.

``fallback_policy`` is always ``"none"``: if a role says
transport=opencode and OpenCode fails, the run fails -- Claude (or
anything else) is NEVER silently called. Fallback needs a
separately reviewed policy, which does not exist.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from nullone_bridge_common import BridgeError

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE.parent / "provider-routing.json"

CONFIG_SCHEMA = "nullone.provider-routing.v1"

ROLE_MORNING_EDITORIAL = "morning_editorial"
ROLE_DRAFT_FACTORY = "draft_factory"
ROLE_STORY_WRITER = "story_writer"
ROLE_BREAKING_RADAR = "breaking_radar"
ROLE_WEEKLY_STRATEGY = "weekly_strategy"

LOGICAL_ROLES = (
    ROLE_MORNING_EDITORIAL,
    ROLE_DRAFT_FACTORY,
    ROLE_STORY_WRITER,
    ROLE_BREAKING_RADAR,
    ROLE_WEEKLY_STRATEGY,
)

TRANSPORT_OPENCODE = "opencode"
TRANSPORT_CLAUDE = "claude"

KNOWN_TRANSPORTS = (TRANSPORT_OPENCODE, TRANSPORT_CLAUDE)

# Claude transport exists only where a reviewed Claude implementation
# exists: the Morning cycle (`claude -p`) and the Story writer
# (`HaikuStoryWriter`). Any other role x claude combination fails
# closed at resolve time instead of dispatching nowhere.
CLAUDE_SUPPORTED_ROLES = (ROLE_MORNING_EDITORIAL, ROLE_STORY_WRITER)

FALLBACK_NONE = "none"

# Default model for the Claude transport cycle when the operator
# selects transport=claude without pinning a model: preserves the
# long-reviewed `claude -p --model sonnet` command byte-for-byte.
CLAUDE_DEFAULT_MODEL = "sonnet"

# Exact model the Claude Story writer executes when the operator
# selects transport=claude for story_writer without pinning a model:
# `HaikuStoryWriter` passes its model verbatim to
# `nullone_claude.run_structured` (whose own default is "haiku").
# The profile reports exactly this value (Blocker C observability
# truth: reported == executed, never faked).
HAIKU_DEFAULT_MODEL = "haiku"

# Bare (slash-less) model values are transport-local legacy
# identifiers, valid ONLY for the Claude transport, which passes
# them verbatim to the Claude CLI. OpenCode transport models must
# always be `provider/model` endpoints.
TRANSPORT_LOCAL_MODELS = (CLAUDE_DEFAULT_MODEL, HAIKU_DEFAULT_MODEL)

# Model identifiers are `provider/model` endpoints (non-secret).
# The shape check rejects blanks and vendor-less values without
# executing anything. A single provider-defined `:suffix` (e.g.
# OpenRouter `:free` variants) is accepted after the model path;
# the provider segment itself never contains `:`.
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*/[A-Za-z0-9_./\-]+(?::[A-Za-z0-9_\-]+)?$")

# Least-privilege capability labels per role. Frozen: routing a new
# model never adds shell/Git/Zernio/Telegram/publish/approval/secret
# capability. Enforcement lives in the reviewed per-role agents and
# prompts; these labels are the observable contract.
ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    ROLE_MORNING_EDITORIAL: ("editorial-reasoning", "morning-artifacts", "web-research"),
    ROLE_DRAFT_FACTORY: ("editorial-reasoning", "packaging", "draft-artifacts", "web-research"),
    ROLE_STORY_WRITER: ("story-reasoning",),
    ROLE_BREAKING_RADAR: ("radar-reasoning", "radar-artifacts", "web-research"),
    ROLE_WEEKLY_STRATEGY: ("strategy-reasoning", "strategy-artifacts", "web-research"),
}

# Reviewed per-role execution budgets (seconds). Must equal the
# timeout constants in the role wrappers/pipelines:
# morning 600 (PROVIDER_CALL_TIMEOUT_SECONDS), draft 900, story 300
# (STORY_WRITER_TIMEOUT_SECONDS), radar 600, weekly 600.
ROLE_TIMEOUTS: dict[str, int] = {
    ROLE_MORNING_EDITORIAL: 600,
    ROLE_DRAFT_FACTORY: 900,
    ROLE_STORY_WRITER: 300,
    ROLE_BREAKING_RADAR: 600,
    ROLE_WEEKLY_STRATEGY: 600,
}

# Reviewed per-role OpenCode agent names. Pinned here so a routing
# change can never redirect a role at a different agent.
ROLE_AGENTS: dict[str, str] = {
    ROLE_MORNING_EDITORIAL: "nullone-editorial",
    ROLE_DRAFT_FACTORY: "nullone-draft-factory",
    ROLE_STORY_WRITER: "nullone-story-writer",
    ROLE_BREAKING_RADAR: "nullone-breaking-radar",
    ROLE_WEEKLY_STRATEGY: "nullone-weekly-strategy",
}

LEGACY_MORNING_TRANSPORT_ENV_VAR = "NULLONE_EDITORIAL_PROVIDER"
LEGACY_STORY_TRANSPORT_ENV_VAR = "NULLONE_STORY_PROVIDER"
LEGACY_OPENCODE_MODEL_ENV_VAR = "NULLONE_OPENCODE_MODEL"

ROLE_ENV_PREFIX = "NULLONE_ROLE_"


class ProviderRoutingError(BridgeError):
    """Any routing/config failure. Always fail closed, fixed message."""


@dataclass(frozen=True)
class ProviderProfile:
    """Typed provider profile for one logical role.

    No credentials inside: ``model`` is a non-secret
    ``provider/model`` identifier and the secret material stays
    entirely with the transport/provider outside Git.
    """

    role: str
    transport: str
    model: str
    capabilities: tuple[str, ...]
    timeout_seconds: int
    fallback_policy: str = FALLBACK_NONE


def _fail(reason: str) -> ProviderRoutingError:
    # Fixed message: never echo rejected values (operator config).
    return ProviderRoutingError(f"Provider routing misconfigured: {reason}")


def _model_is_valid(model: str, transport: str) -> bool:
    if MODEL_PATTERN.match(model):
        return True
    # Bare legacy identifiers are valid only for the Claude
    # transport, which executes them verbatim.
    return transport == TRANSPORT_CLAUDE and model in TRANSPORT_LOCAL_MODELS


def _transport_default_model(transport: str, role: str) -> str | None:
    """Truthful model default when a transport is selected unpinned.

    Returns None when the transport has no safe default (OpenCode:
    the model MUST come from explicit configuration). Claude
    defaults are the exact values the Claude adapters execute
    (sonnet cycle / haiku writer) so reported == executed.
    """

    if transport == TRANSPORT_CLAUDE:
        if role == ROLE_STORY_WRITER:
            return HAIKU_DEFAULT_MODEL
        return CLAUDE_DEFAULT_MODEL
    return None


def _validate_mapping(mapping: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Validate a role mapping (checked-in file or injected)."""

    validated: dict[str, dict[str, str]] = {}
    for role, entry in mapping.items():
        if role not in LOGICAL_ROLES:
            raise _fail("unknown role")
        if not isinstance(entry, dict):
            raise _fail("entry")
        transport = entry.get("transport")
        model = entry.get("model")
        if transport not in KNOWN_TRANSPORTS:
            raise _fail("transport")
        if not isinstance(model, str):
            raise _fail("model")
        model = model.strip()
        if not _model_is_valid(model, transport):
            raise _fail("model")
        validated[role] = {"transport": transport, "model": model}
    missing = [role for role in LOGICAL_ROLES if role not in validated]
    if missing:
        raise _fail("incomplete")
    return validated


def load_routing_config(path: Path | str | None = None) -> dict[str, dict[str, str]]:
    """Load and schema-validate the checked-in role mapping.

    Returns ``{role: {"transport": ..., "model": ...}}``. Anything
    unexpected -- bad schema, unknown roles, unknown transports,
    blank/malformed models -- raises ``ProviderRoutingError``.
    """

    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise _fail("unreadable config") from e
    if not isinstance(raw, dict) or raw.get("schema") != CONFIG_SCHEMA:
        raise _fail("schema")
    roles = raw.get("roles")
    if not isinstance(roles, dict):
        raise _fail("roles")
    return _validate_mapping(roles)


def resolve_provider_profile(
    role: str,
    *,
    config: Mapping[str, Mapping[str, str]] | None = None,
    config_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> ProviderProfile:
    """Resolve the provider profile for one logical role.

    Unknown role, unknown transport, blank/invalid model, or an
    unsupported role x transport combination FAILS CLOSED. There is
    no silent vendor switching and no silent fallback: the returned
    profile names exactly one transport and one model.
    """

    if role not in LOGICAL_ROLES:
        raise _fail("unknown role")
    raw_mapping = dict(config) if config is not None else load_routing_config(config_path)
    mapping = _validate_mapping(raw_mapping)
    if role not in mapping:
        raise _fail("incomplete")
    environment = dict(os.environ) if env is None else dict(env)

    json_transport = mapping[role]["transport"]
    json_model = mapping[role]["model"]

    # Explicit per-role deployment override (highest precedence). A
    # key that is PRESENT but blank/invalid fails closed (explicit
    # misconfiguration); an absent key falls through. Legacy
    # compatibility keys stay lenient (blank == unset).
    transport_override = environment.get(f"{ROLE_ENV_PREFIX}{role.upper()}_TRANSPORT", None)
    model_override = environment.get(f"{ROLE_ENV_PREFIX}{role.upper()}_MODEL", None)
    if transport_override is not None:
        transport_override = transport_override.strip().lower()
        if transport_override not in KNOWN_TRANSPORTS:
            raise _fail("transport")
    if model_override is not None:
        model_override = model_override.strip()
        if not model_override:
            raise _fail("model")
    transport = transport_override
    model = model_override

    # Deprecated compatibility layer (documented, tested).
    if transport is None:
        if role == ROLE_MORNING_EDITORIAL:
            legacy = (environment.get(LEGACY_MORNING_TRANSPORT_ENV_VAR, "") or "").strip().lower()
            transport = legacy or None
        elif role == ROLE_STORY_WRITER:
            legacy = (environment.get(LEGACY_STORY_TRANSPORT_ENV_VAR, "") or "").strip().lower()
            transport = legacy or None
    if transport is None:
        transport = json_transport
    if transport not in KNOWN_TRANSPORTS:
        raise _fail("transport")

    if model is None:
        if transport == TRANSPORT_OPENCODE:
            legacy_model = (environment.get(LEGACY_OPENCODE_MODEL_ENV_VAR, "") or "").strip()
            if legacy_model:
                model = legacy_model
    if model is None:
        if transport == json_transport:
            model = json_model
        else:
            default = _transport_default_model(transport, role)
            if default is None:
                raise _fail("model")
            # Truthful transport default: the exact value the
            # transport's adapter executes when unpinned (Claude
            # sonnet cycle / haiku writer), so reported == executed.
            model = default
    if not _model_is_valid(model, transport):
        raise _fail("model")

    if transport == TRANSPORT_CLAUDE and role not in CLAUDE_SUPPORTED_ROLES:
        raise _fail("unsupported")

    return ProviderProfile(
        role=role,
        transport=transport,
        model=model,
        capabilities=ROLE_CAPABILITIES[role],
        timeout_seconds=ROLE_TIMEOUTS[role],
        fallback_policy=FALLBACK_NONE,
    )


def role_agent(role: str) -> str:
    """Reviewed OpenCode agent name for a role (fail closed)."""

    try:
        return ROLE_AGENTS[role]
    except KeyError as e:
        raise _fail("unknown role") from e


def format_routing_metadata(profile: ProviderProfile, outcome: str) -> str:
    """Secret-free `ROLE/TRANSPORT/PROVIDER_MODEL/OUTCOME` line."""

    return (
        f"ROLE={profile.role} TRANSPORT={profile.transport} "
        f"PROVIDER_MODEL={profile.model} OUTCOME={outcome}"
    )


def describe_profile(profile: ProviderProfile) -> str:
    """Secret-free one-line profile description for run logging."""

    return (
        f"role-provider-profile role={profile.role} "
        f"transport={profile.transport} model={profile.model} "
        f"timeout={profile.timeout_seconds} fallback={profile.fallback_policy}"
    )


def self_test() -> int:
    mapping = load_routing_config()
    # Reviewed per-role transports (issue #155): Morning runs the
    # validated Claude/Sonnet route; every other role stays on OpenCode.
    expected_transports = {
        ROLE_MORNING_EDITORIAL: TRANSPORT_CLAUDE,
        ROLE_DRAFT_FACTORY: TRANSPORT_OPENCODE,
        ROLE_STORY_WRITER: TRANSPORT_OPENCODE,
        ROLE_BREAKING_RADAR: TRANSPORT_OPENCODE,
        ROLE_WEEKLY_STRATEGY: TRANSPORT_OPENCODE,
    }
    for role in LOGICAL_ROLES:
        profile = resolve_provider_profile(role, config=mapping, env={})
        assert profile.transport == expected_transports[role], profile
        assert profile.model, profile
        assert profile.fallback_policy == FALLBACK_NONE, profile
        assert profile.timeout_seconds == ROLE_TIMEOUTS[role], profile
        assert profile.capabilities == ROLE_CAPABILITIES[role], profile
        line = format_routing_metadata(profile, "SELFTEST")
        assert f"ROLE={role}" in line
        assert f"TRANSPORT={expected_transports[role]}" in line
        assert "OUTCOME=SELFTEST" in line
    morning = resolve_provider_profile(
        ROLE_MORNING_EDITORIAL, config=mapping, env={}
    )
    assert morning.model == CLAUDE_DEFAULT_MODEL, morning

    for bad_role in ("morning", "analytics", "", "MORNING_EDITORIAL"):
        try:
            resolve_provider_profile(bad_role, config=mapping, env={})
        except ProviderRoutingError:
            pass
        else:
            raise AssertionError(f"role {bad_role!r} did not fail closed")

    print("PROVIDER_ROUTER_SELF_TEST=PASS")
    print("NO_EXTERNAL_CALLS=PASS")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="NullOne provider role router")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    show = sub.add_parser("show")
    show.add_argument("--role", required=True)
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()
    if args.command == "show":
        try:
            profile = resolve_provider_profile(args.role)
        except ProviderRoutingError as e:
            print(f"BLOCKED={e}")
            return 2
        print(describe_profile(profile))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
