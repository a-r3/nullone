# 61 — Secure Analytics Secret Injection

Status: **IMPLEMENTED & REVIEWED — NOT DEPLOYED**. This document describes the
reviewed runtime boundary and the production injection mechanism proven on
this host. Nothing here has been applied to production: no credential has
been provisioned, no systemd change has been made, and no OpenClaw job has
been created, edited, or run.

Referenced baseline: PR branch `feature/n61-secure-analytics-secret-wiring`
at `9edeb52` (`9edeb529bddd7709cc697068bd703026e88a44aa`). Verified
`2026-09-08` (read-only host inspection + official Zernio docs).

## 1. What changed (#61)

The fail-closed placeholder (`PROVIDER_SECRET_WIRING_PENDING_61` /
`ProviderSecretWiringPendingError`) is gone. In its place:

- `workspace/social/ops/scripts/nullone_secret_provider.py` — the reviewed
  secret boundary. Owns the only mapping from the logical secret id to a
  runtime source, wraps every value in a non-loggable `SecretValue`, and
  classifies runtime state without ever rendering the value.
- `workspace/social/ops/scripts/nullone_analytics_provider_factory.py` —
  `build_production_analytics_provider(*, secret_provider=None)` now
  constructs the #29 `ZernioReadOnlyAnalyticsConnector` with the canonical
  account id (`6a982bbf77555aae01c28f21`) behind that boundary.
- `workspace/social/ops/scripts/nullone_zernio_analytics_adapter.py` — the
  transport now receives an explicit `SecretValue` token via
  `build_authenticated_transport(*, token=...)`; the adapter no longer reads
  any environment variable and its transport repr never renders the token.
- `workspace/social/ops/scripts/nullone-daily-analytics-run.py` —
  `_default_build_connector()` delegates to the reviewed factory.

Domain semantics are unchanged: missing/blank credential and rejected
credential both surface as `ConnectorUnauthorizedError` → `BLOCKED`
(`ZERNIO_ANALYTICS_UNAUTHORIZED`); secret-source unavailability surfaces as
`ConnectorUnavailableError` → `BLOCKED`. Neither is ever a crash, and no Zernio
bootstrap attempt is fabricated.

## 2. Secret identity and mapping

| Logical secret id | Runtime source | Syntax in source |
| --- | --- | --- |
| `zernio.analytics.bearer` | inherited process environment variable | `ZERNIO_ANALYTICS_API_TOKEN` |

- The exact environment-variable name is bound **only** in
  `nullone_secret_provider.EnvironmentSecretProvider`. Neither the Zernio
  adapter nor the factory references it (enforced by
  `tests/test_scheduled_workflows_capability_negative.py` →
  `ReviewedSecretBoundaryGuardTests`).
- The previously rejected legacy key alias is **not** introduced anywhere in
  new code or documentation.
- The canonical account id is not a secret and stays in
  `nullone_bridge_common.CANONICAL_ACCOUNT_ID`.

## 3. Least-privilege Zernio credential policy

Configured on the Zernio admin surface (`docs.zernio.com/api-keys/create-api-key`,
verified 2026-09-08):

- `permission` = `read` (never `read-write`)
- `scope` = `profiles`
- `profileIds` = `[<NULLONE_ZERNIO_PROFILE_ID>]` (replace placeholder with the
  NullOne profile id at #37 live validation time)

`GET /v1/auth/verify` (docs.zernio.com/api-keys/verify-credential) is
available as an optional **#37** validation tool. It proves the token is
valid and reports `valid`/`userId`/`authType`/`scope`; it does **not** prove
the read-only or profile restriction above, so it never replaces manual
least-privilege review.

Analytics is served over the direct, GET-only, read-only adapter
(`nullone_zernio_analytics_adapter.py`) — not over MCP (`bundle-mcp`), per
#29's root-cause finding.

## 4. Production injection mechanism (verified, branch A)

Chain proven by read-only inspection of the production host (2026-09-08):

1. `openclaw-gateway.service` is a **systemd user unit** (user `oem`):
   `systemctl --user`, `FragmentPath=/home/oem/.config/systemd/user/openclaw-gateway.service`,
   `DropInPaths` empty, `Type=simple`, `Enabled`, `MainPID=2272`
   (node `.../openclaw/dist/index.js gateway --port 18789`).
2. The unit has **no** `EnvironmentFile=` today, and `~/.config/nullone` does
   not exist yet.
3. A systemd user drop-in can extend the unit without editing it:

   ```ini
   # /home/oem/.config/systemd/user/openclaw-gateway.service.d/61-nullone-analytics-secret.conf
   [Service]
   EnvironmentFile=%h/.config/nullone/secrets/zernio-analytics.env
   ```

   `%h` resolves to the user home (`/home/oem`). Lines in an env file have the
   form `KEY=VALUE` (no `export` needed for systemd).
4. OpenClaw 2026.8.2 scheduled `--command` jobs inherit the Gateway process
   environment: `dist/server-cron-DtqkVgKM.js#runCronCommandJob` passes only
   `options.env` (none in our payload), and
   `dist/exec-XYRTg4oR.js#resolveCommandEnv` uses
   `baseEnv = params.baseEnv ?? process.env` → child processes receive the
   Gateway's environment. Therefore the token reaches the analytics
   executable via `ZERNIO_ANALYTICS_API_TOKEN` in `os.environ`.

After `systemctl --user daemon-reload` + `systemctl --user restart
openclaw-gateway`, the readback below reports `PRESENT_READABLE`.

## 5. Secret file layout (deployment-edge validation)

| Item | Expected value |
| --- | --- |
| Path | `/home/oem/.config/nullone/secrets/zernio-analytics.env` |
| Directory | `0700`, owned by `oem`, not a symlink |
| File | `0600`, regular file, owned by `oem`, not a symlink |
| Contents | `ZERNIO_ANALYTICS_API_TOKEN=<token>` (one line; no quotes; no trailing spaces) |

Metadata-only validation (never reads the value):

```bash
python3 workspace/social/ops/scripts/nullone_secret_provider.py \
  file-check --secret-file /home/oem/.config/nullone/secrets/zernio-analytics.env
```

## 6. Readback / operator validation

```bash
python3 workspace/social/ops/scripts/nullone_secret_provider.py list
python3 workspace/social/ops/scripts/nullone_secret_provider.py \
  readback --secret-id zernio.analytics.bearer
```

Output never contains the value; it reports the status
(`PRESENT_READABLE` / `MISSING` / `BLANK` / `UNAVAILABLE`) and always prints
`VALUE_REDACTED=TRUE`. Exit code: 0 for `PRESENT_READABLE`, 1 for
`MISSING`/`BLANK`, 2 for `UNAVAILABLE`/unknown id.

## 7. Behavior matrix (post-deployment, before/after activation)

| Condition | Connector outcome | Domain outcome | reason_code | scheduled CLI exit |
| --- | --- | --- | --- | --- |
| env var absent | `ConnectorUnauthorizedError` | `BLOCKED` | `ZERNIO_ANALYTICS_UNAUTHORIZED` | 0 (orchestration completed) |
| env var empty/whitespace | `ConnectorUnauthorizedError` | `BLOCKED` | `ZERNIO_ANALYTICS_UNAUTHORIZED` | 0 |
| secret source unreadable | `ConnectorUnavailableError` | `BLOCKED` | `ZERNIO_ANALYTICS_UNAVAILABLE` | 0 |
| Zernio returns 401/403 | `ConnectorUnauthorizedError` | `BLOCKED` | `ZERNIO_ANALYTICS_UNAUTHORIZED` | 0 |
| Zernio unreachable/5xx | `ConnectorUnavailableError` | `BLOCKED` | `ZERNIO_ANALYTICS_UNAVAILABLE` | 0 |
| unexpected programming defect | propagates | (none) | `RUNTIME_CRASHED` | non-zero |

The desired wake-up command remains static and carries no token in argv,
payload, cron, or env values:

```bash
python3 <repo>/workspace/social/ops/scripts/nullone-scheduled-wakeup.py \
  analytics --source openclaw
```

(`<repo>` is the production checkout root; created only during #37, never by
this change.) The legacy runner keeps its own exit convention
(`domain_outcome != SUCCEEDED → exit 1`).

## 8. Controlled #37 activation steps (documented only — not performed)

1. Provision a least-privilege token (section 3); keep it out of the repo.
2. `mkdir -p /home/oem/.config/nullone/secrets` (0700), write the env file
   (0600), owner `oem`, then run `file-check` (section 5).
3. Add the systemd user drop-in (section 4); `systemctl --user daemon-reload`;
   `systemctl --user restart openclaw-gateway`.
4. `readback` → `PRESENT_READABLE` on the host.
5. Optional: `GET /v1/auth/verify` via `curl` to confirm the token's
   `valid`/`scope`, then manually review the created policy.
6. Create the desired OpenClaw wake-up job; verify one in-window run yields
   `domain_outcome=SUCCEEDED` and a real analytics artifact; then re-run the
   full `OFFLINE_REGRESSION_SUITE`.

Rollback: remove the drop-in, `daemon-reload`/`restart`, delete the secrets
file and directory. No default behavior depends on this file.

## 9. Repository enforcement

- `tests/test_secret_provider.py` — `SecretValue` render safety, presence
  classification, typed missing/unavailable failures, metadata-only file
  validation, readback CLI.
- `tests/test_analytics_provider_factory.py` — factory construction, missing/
  unavailable sanitization, GET-only surface, keyword-only injection,
  workflow-seam `BLOCKED` proof, programming-defect propagation.
- `tests/test_scheduled_workflows_capability_negative.py` — env-var name bound
  in exactly one module; no legacy key alias; factory reads via boundary only;
  fake secret never rendered.
- `python3 tests/run_offline.py` → `OFFLINE_REGRESSION_SUITE=PASS` (no
  network, no real credential).