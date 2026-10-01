# Provider-Neutral Role Router (Issue #111)

NullOne workflows depend on LOGICAL ROLES, never on vendor names.

```
workflow ("I need role X")
  -> resolve_provider_profile(role)      # nullone_provider_router.py (sole authority)
  -> ProviderProfile(role, transport, model, capabilities, timeout, fallback_policy)
  -> transport adapter                   # nullone_provider_adapter.py registry
  -> provider/model endpoint
```

## Concepts (do not conflate)

- ROLE: a reviewed NullOne workflow capability. Exactly five:
  `morning_editorial`, `draft_factory`, `story_writer`,
  `breaking_radar`, `weekly_strategy`. Analytics and heartbeat stay
  deterministic and are deliberately NOT routed through any LLM.
- TRANSPORT: the CLI vehicle, `opencode` or `claude`. OpenCode is a
  TRANSPORT, not the final LLM provider.
- MODEL/PROVIDER ENDPOINT: the non-secret `provider/model`
  identifier handed to the transport (`opencode/...`,
  `openrouter/...`, `anthropic/...`, `openai/...`, `google/...`,
  `custom-provider/...`). Any value the transport supports can be
  configured without workflow rewrites.
- CAPABILITIES: frozen least-privilege labels per role. Routing a
  new model NEVER widens them; enforcement stays in the reviewed
  per-role adapters, agents, and prompts. Story writer owns no tool capability.

## Configuration

One authoritative mapping: `workspace/social/ops/provider-routing.json`
(schema `nullone.provider-routing.v1`, transport+model per role, no
secrets). Schema validation fails closed on unknown roles,
unknown transports, blank/malformed models, and incomplete maps.

Explicit per-role deployment override (no workflow rewrite):
`NULLONE_ROLE_<ROLE>_TRANSPORT` / `NULLONE_ROLE_<ROLE>_MODEL`.
A present-but-blank/invalid override fails closed; an absent key
falls through. Precedence: per-role env > legacy env > JSON.

Deprecated compatibility (tested, removal needs review):
`NULLONE_EDITORIAL_PROVIDER` (morning transport),
`NULLONE_STORY_PROVIDER` (story transport),
`NULLONE_OPENCODE_MODEL` (model for opencode-transport roles only).
The checked-in mapping selects Claude/Sonnet for Morning, Draft
Factory, and Weekly, Claude/Haiku for Story and Breaking Radar, and
OpenCode for no role. Production activation remains a separate
controlled deployment.

Timeouts and capabilities are pinned in router code (equal to the
reviewed per-role constants); the JSON file cannot change execution
budgets or privilege. `fallback_policy` is always `"none"`.

## Adapters

One contract (`nullone_provider_adapter.py`): normalized invocation,
timeout vs reachability vs execution failure (existing semantics
survive: timeout is never reachability; binary resolution is
execution failure, never reachability; the Story OpenCode error
family normalizes through the same contract), secret-free metadata,
capability declaration, outcome. The adapter registry is the SOLE
execution path: covered wrappers and factories import no vendor
transport module and call `invoke_role_cycle` / `make_story_writer`
only. `invoke_adapter` runs exactly the profile's transport -- a
failure propagates, never falls back.

The caller owns ONLY role/prompt/workspace (`AdapterCall` has no
agent/timeout fields by construction): agent derives from reviewed
role authority (`role_agent`), timeout from the profile. A
transport/model switch routes through the registry with zero
workflow changes; unsupported role x transport fails at the
router/adapter boundary.

- OpenCode transport accepts the model from the profile for every
  role; binary resolution (PR116 absolute path), isolated session,
  explicit agent/model, `--format json`, exact `--dir`, no `--auto`/
  continuation, reviewed agents, timeouts, and fixed-string failures
  preserved.
- Claude transport has reviewed Morning (`claude -p`), Draft
  Factory (fully mediated structured rounds), Story
  (`HaikuStoryWriter`), Weekly structured-result, and Radar
  structured-result paths, model from
  the profile with truthful transport defaults when unpinned
  (`sonnet` cycle / `haiku` writer -- the exact executed values, so
  reported == executed). Weekly passes the actual workflow prompt and
  workspace to `nullone_claude.run_structured`, grants only Read,
  WebSearch, and WebFetch, and persists a validated result to the
  deterministic ISO-week report path and optional `MEMORY.md` update.
  It grants no model filesystem writes, shell, MCP, or publication tools.
  Draft Factory executes the actual Draft Factory prompt in three
  read-only structured rounds (SELECT/PRODUCE/COMPLETE); the model
  holds no shell/file authority at all, and deterministic Python
  performs every write and the five exact reviewed helper
  invocations (packaging evaluate/render, manifest build, bridge
  execute, review delivery). Claude supports all five roles; no
  role x claude route fails as unsupported.

## Observability

Every model-backed run prints a secret-free line:

```
ROLE=<role> TRANSPORT=<transport> PROVIDER_MODEL=<model> OUTCOME=<outcome>
```

plus the role profile in scheduled result contexts
(`provider_profile: role/transport/model`).

## Activation

This repository change does not deploy or alter the existing Weekly
schedule. Production routing changes only through a separate controlled
deployment and natural-run validation under Issue #37. The reviewed
OpenCode Weekly agent remains available through an explicit route override.
