# NullOne Runtime Permissions Contract

Status: authoritative instruction/capability reference for issue #6.
Repository-only; no production effect. Complements
`docs/contracts/change-control.md` (lifecycle) and the per-role agent
files. The deterministic final publication path (#89) and Zernio publisher
connector (#90) are the only consequential publication authorities.

Enforcement legend — every capability below is classified exactly one way:

- **A. Required by design** — the role needs it to do its reviewed job.
- **B. Technically enforced by config/runtime** — code, schema, CLI flags,
  or fail-closed defaults prevent misuse (with the mechanism named).
- **C. Prompt/instruction only** — enforced by reviewed instruction text,
  not by machinery. Honest about the gap; covered by negative tests where
  stated.
- **D. Not permitted** — the role must never have or use it.

Do NOT claim B where only C exists.

## Roles

### MORNING EDITORIAL (research/planning cycle)

- Research/web access as required: A. The provider factory
  (`nullone_editorial_provider_factory.py`, `NULLONE_EDITORIAL_PROVIDER`
  = `opencode` | `claude`) selects one editorial transport per run with
  no silent fallback. The OpenCode transport injects the checked-in
  `nullone-editorial` agent (`workspace/.opencode/agents/`) via
  `nullone_opencode_editorial_provider.py`
  (`opencode run --agent nullone-editorial`, web research plus reads
  allowed; writes default-denied except the four Morning
  artifact/state paths; secret-bearing reads denied; shell, task
  delegation, skills, outside-worktree access denied; no `--auto`) (B). The Claude fallback transport injects
  `Read,Write,WebSearch,WebFetch` via
  `nullone_claude_editorial_provider.py` (`--allowedTools`,
  `--permission-mode dontAsk`); no Bash, no MCP tools (B).
- Read editorial state (queue, ledgers, strategy, references): A.
- Write board/handoff/state only where the current workflow requires
  (editorial board, candidate handoff, queue/ledger appends): A, bounded by
  #27 result/artifact validation and occurrence locks (B for identity and
  idempotence; content scope is C).
- NO publication authority: D. Prompt declares `EDITORIAL_PLANNING_ONLY`
  and `DO NOT publish/schedule/create a Zernio post` (C); the cycle has no
  Zernio/publish transport wired at all (B — there is no code path from
  this role to any publish adapter).

### RADAR (breaking research)

- Research/web access: A (same provider boundary as Morning where used).
- Structured candidate/state writes only (scan receipts, handoff envelopes
  under staging/spool containment): A, with symlink-escape fail-closed and
  receipt-authority commit rules (B).
- NO publication authority: D. Prompt lists explicit must-NOTs (publish,
  approve, drafts, Telegram, publisher, ledger mutation) (C); no publish
  transport exists on this path (B).

### DAILY ANALYTICS (read-only analytics)

- Analytics read capability (Zernio GET-only adapter, four read methods):
  A. The connector exposes no non-GET call by construction with
  capability-negative tests (B).
- Deterministic artifact/state writes (raw + report pair, atomic
  staging + ordered swap + call-scoped rollback): A (B for pair atomicity
  within a call; not a crash-proof filesystem transaction — documented).
- NO publication authority: D (B — no publish import or symbol; enforced
  by static capability-negative tests).

### WEEKLY STRATEGY (review)

- Read analytics/editorial evidence: A.
- Write strategy artifact where applicable (weekly report, durable memory
  findings only): A.
- NO publication authority: D. Prompt declares `DO NOT PUBLISH ANYTHING`
  (C); no publish transport wired (B).

### DRAFT FACTORY (review-draft creation)

- Read verified candidate: A.
- Render/create review draft (exactly one Zernio draft, DRAFT_FIRST): A.
- NO live publish capability: D. Prompt declares `It MUST NOT publish or
  schedule` and `DRAFT_FIRST remains mandatory` (C). Transport migration
  (MCP vs deterministic REST DraftProvider, #81) is owned by the #37
  deployment decision, not by this instruction file.

### APPROVAL (human authorization controller)

- Telegram approval UI/control only (first/second-stage buttons, reject/
  revise/back handling, exactly-once delivery discipline): A.
- NO Zernio publish/read/write capability: D by default. The agent file
  prohibits all Zernio MCP/REST calls including read-only `posts_get` (C,
  plus capability-negative instruction-safety tests). No read-only evidence
  exception is currently granted by any reviewed path; if a future path
  requires one, it must be named here explicitly — default deny stands.

### PUBLISHER COMPATIBILITY AGENT (de-escalated, #89)

- No consequential publication capability: D. Explicit MUST NOT list
  (wrappers, bridges, notifier, `sessions_send`, Zernio MCP/REST,
  caption/media reconstruction) (C); legacy direct execution is fail-closed
  in code (B).
- Read-only narration only: A, bounded to already-persisted deterministic
  state; never claim `published` unless durable manifest state is
  explicitly PUBLISHED (C).
- Legacy internal ID `texbrif-publisher` retained for compatibility; exec
  grant removal is a #37 deployment hardening step (not done in repo).

### DETERMINISTIC FINAL PUBLISH CONTROLLER (#89 core)

- Exact scoped publication capability: A — the ONLY role with final
  publish authority. Reached only via the HMAC-authenticated plugin pipe
  after the final `texbrif:publish:<POST_ID>` callback; singleton claim
  under `review_post_lock`; attempts=1 persisted; settle-from-manifest
  (B, all offline-tested).
- No LLM decision authority after the final human click (with #90 REST
  transport): D for models by architecture (B — provider transport is
  deterministic HTTP, not Claude/MCP, on the consequential path).

### NOTIFIER (result delivery)

- Result notification only (best-effort deterministic message; failure
  never retries publication): A (B — reserve-before-side-effect, timeout
  never auto-resent).
- No publication capability: D (B — notifier module has no publish import
  or symbol; static negative tests).

## Broad-permission audit (current main, repository scope)

| Occurrence | Classification | Rationale |
|---|---|---|
| `nullone_claude_editorial_provider.py --allowedTools Read,Write,WebSearch,WebFetch` | REQUIRED | Narrow research set for the Claude editorial fallback transport; no Bash/MCP/draft/notify tools. |
| `nullone_opencode_editorial_provider.py opencode run --agent nullone-editorial --model <provider/model> --format json` (no `--auto`, fresh session per run) | REQUIRED | Narrow research set for the OpenCode editorial transport. The checked-in agent defaults writes to deny and allows only the four Morning artifact/state paths (`**/social/research/daily/*-editorial-board.md`, `**/social/research/daily/*-editorial-candidates.json`, `**/social/state/candidate-queue.md`, `**/social/state/topic-ledger.jsonl`); secret-bearing reads (`.env`-family, keys) denied; shell, task delegation, skills, and outside-worktree access denied. Path scoping proven live against OpenCode 1.18.30 (allowed write succeeds, off-scope write and secret read blocked). |
| `nullone_claude.py run_structured(allowed_tools, ...)` with schema-bound JSON output, `--tools ""` default, no session persistence | REQUIRED | Callers pass minimal sets; default denies tool use. |
| `toolsAllow=['*']` in `docs/deployment/*` preflight records and context | DOCUMENTATION_ONLY | Historical evidence text, not executable config. |
| `toolsAllow=['*']` on live legacy Daily Analytics OpenClaw job | LEGACY, production-side | Lives only in live OpenClaw config (mutable production state, not in repo). Replacement by the deterministic wrapper invocation is a #37 deployment step; no repo change in this contract can fix it. |
| Publisher-agent `exec` grant | TOO_BROAD, deployment-gated | Documented in-agent; removal is the #37 deployment hardening step. Not removable from the repository alone. |
| Zernio access in non-publication roles (prompts) | NONE FOUND | Morning/Analytics/Strategy/Radar prompts contain no publish-capable Zernio instructions; Analytics uses the GET-only adapter; Draft Factory is DRAFT_FIRST with no live-publish instruction. |
| Wildcard MCP/tool access in repo code | NONE FOUND | Static capability-negative suites cover analytics/breaking/story/scheduler paths. |
| Generic shell authority for agents | NONE FOUND | Bash appears only in provider subprocess boundaries with bounded timeouts, never as an agent grant. |

Only concrete reachable repo problems were changed by the accompanying PR
(retired Main control-message protocol); the two production-side rows
above are explicitly #37 deployment work, not repository defects.

## Legacy production skill — source ownership decision

Decision: **EXTERNAL_CONTROLLED_COMPONENT**.

- Repository evidence shows no skill definition files anywhere in git; the
  "old production skill" is the set of legacy prompt-only OpenClaw
  `agentTurn` automations, which exist solely in live OpenClaw production
  config (read-only evidence in `docs/deployment/59-...`, `79-...`,
  `80-...`, and the 2026-09-07 preflight).
- Current owner/source of truth: live OpenClaw production config (mutable
  production state, never written from this repository).
- Future deployment may REPLACE it (never adopt it): reviewed deterministic
  wrapper invocations (`DESIRED / NOT DEPLOYED` in the deployment docs) cut
  over under #37 controlled deployment.
- Still reachable: yes, in production, until the #37 cutover — which is
  exactly why #37, not this contract, owns the activation.
- No production path is invented here; nothing was read from or written to
  production to record this decision.

## Stable identifiers (intentional, do not rename)

- Internal agent IDs: `texbrif-approval`, `texbrif-publisher`.
- Telegram callback namespace: `texbrif:`.
- Telegram internal account ID: `texbrif`.
- Public brand wording is always **NullOne** (never Texbrif in
  user-visible text); a stale cached username such as texbrif is not
  authoritative.

## Authority hierarchy pointer

The single override order lives in `workspace/AGENTS.md` (§ Instruction
authority hierarchy): change-control contract → this permissions contract
→ deterministic persisted state → workspace Main rules → task prompts →
narrow agent files. No other "highest priority override" layer may be
added without updating that hierarchy.
