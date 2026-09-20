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
  per-role agents/prompts. Story writer owns no tool capability.

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
Migration M1: the no-env default moved from the legacy shims
(`claude`) to the checked-in mapping (`opencode` + Muse Spark),
matching live production. Live sets explicit env values, so live
behavior is unchanged.

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
- Claude transport remains the explicit rollback adapter (Morning
  cycle via `claude -p`, Story via `HaikuStoryWriter`), model from
  the profile with truthful transport defaults when unpinned
  (`sonnet` cycle / `haiku` writer -- the exact executed values, so
  reported == executed). Claude supports `morning_editorial` +
  `story_writer` only; any other role x claude fails closed at
  resolve time.

## Observability

Every model-backed run prints a secret-free line:

```
ROLE=<role> TRANSPORT=<transport> PROVIDER_MODEL=<model> OUTCOME=<outcome>
```

plus the role profile in scheduled result contexts
(`provider_profile: role/transport/model`).

## Activation

Merging this PR changes NO production behavior: the checked-in
mapping equals the current live mapping, and live env overrides
resolve identically. Any future provider/model switch is an
explicit routing-config change activated under Issue #37
(preflight -> controlled config -> natural-run validation ->
rollback proof), never a side effect of this merge. Rollback =
previous mapping (Git revert or explicit override); no workflow
code involved either way.
