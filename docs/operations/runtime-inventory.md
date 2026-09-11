# NullOne Runtime Inventory (secret-free)

Status: repository operations reference for issue #8. Read-only inventory;
no production effect. Captured 2026-09-11. No secret values are recorded
here — environment variables and SecretRefs appear by NAME only.

Reproducibility status values: PINNED | CAPTURED_NOT_PINNED |
HOST_PROVIDED | EXTERNAL_CONTROLLED | UNRESOLVED.

Covered workload components: Morning/editorial scripts, Radar, Analytics
(Daily), Story, Draft Factory, renderer (FEED, CAROUSEL, STORY),
deterministic final publication scripts, release CLI and offline tests.

Truthfulness rule: an entry is PINNED only if an exact version is declared
in a reviewed manifest. Anything true merely because "the current laptop
happens to have it" is HOST_PROVIDED or CAPTURED_NOT_PINNED, never PINNED.

## A. Python interpreter

| COMPONENT | DEPENDENCY | TYPE | VERSION | SOURCE_OF_TRUTH | WHY_REQUIRED | BUILD/RUNTIME | RECOVERY_OWNER | SECRET_FREE | STATUS |
|---|---|---|---|---|---|---|---|---|---|
| All scripts/tests | Python | interpreter | 3.12 (CI pins 3.12; zoneinfo/tzdata behavior assumed) | `.github/workflows/ci.yml` | Runtime for every script, renderer, test | RUNTIME | system/package manager | yes | CAPTURED_NOT_PINNED |

## B. Python packages

| COMPONENT | DEPENDENCY | TYPE | VERSION | SOURCE_OF_TRUTH | WHY_REQUIRED | BUILD/RUNTIME | RECOVERY_OWNER | SECRET_FREE | STATUS |
|---|---|---|---|---|---|---|---|---|---|
| Renderers (`render_*_v2.py`), manifest/main-draft/bridge image validation | Pillow | PyPI package | ==10.2.0 (captured; NOT upgraded by this change) | `requirements-runtime.txt` | Image render/compose/validate | RUNTIME | dependency manifest (`pip install -r requirements-runtime.txt`) | yes | PINNED |
| Everything else (workflows, adapters, release CLI, offline tests) | Python stdlib only | stdlib | 3.12 stdlib | source imports | No third-party imports anywhere outside Pillow users | RUNTIME | system Python | yes | PINNED |

There is no `requests`, no `dateutil`, no test-only third-party package:
`tests/` use stdlib `unittest`; `node` is optional (JS `--check` skipped
when absent). `requirements-dev.txt` documents this explicitly.

## C. Pillow API assumptions

Renderers use stable Pillow APIs only: `Image`, `ImageDraw`, `ImageFont`,
`ImageFilter`, `ImageOps` (`ImageOps.fit`, `GaussianBlur`,
`Image.Resampling.LANCZOS`, `draw.textbbox`, `truetype`). Rasterization
parity additionally depends on the FreeType build behind Pillow, which is
why rendering determinism is claimed as STRUCTURAL_DETERMINISM (sizes,
counts, format, glyph coverage, input hashes, same-host pipeline byte
stability) and NOT as cross-host BIT_IDENTICAL. See
`tests/test_offline_render_reproducibility.py`.

## D. Fonts

| Logical name | File | Source | License | Required glyphs | Used in | Status |
|---|---|---|---|---|---|---|
| DejaVu Sans Regular | `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | OS font package (Debian `fonts-dejavu-core`) | Bitstream Vera / Arev — freely redistributable (see OS package) | Latin + Azerbaijani ƏəĞğİıÖöÜüŞşÇç + digits/currency | All three renderers (body) | HOST_PROVIDED |
| DejaVu Sans Bold | `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf` | OS font package | Same as above | Same as above | All three renderers (headlines) | HOST_PROVIDED |

No font binaries are committed to this repository. No container font files
are exposed here. If either path is absent, renderers fail loudly
(`OSError` from `ImageFont.truetype`) — tested. Azerbaijani coverage is
proven offline by the glyph check (tofu-vs-`.notdef` bitmap comparison),
not assumed.

## E. Filesystem / path assumptions

- Repository-relative reads: scripts resolve inputs relative to the repo
  checkout or `NULLONE_WORKSPACE` env (name only; never a value here).
- Renderer font paths are absolute host paths (see D) — HOST_PROVIDED.
- Renderers accept local-file image sources offline; `http(s)` sources
  require network and are NEVER used by offline validation.
- `zoneinfo` reads the host zoneinfo database (`Asia/Baku` scheduling);
  database presence is HOST_PROVIDED.
- Writable runtime directories (production): `social/ops/run-outcomes/`,
  `social/ops/notifications/`, `social/ops/manifests/`,
  `social/ops/publish-callback-receipts/`, `social/analytics/`,
  `deploy-state/` (release CLI). All are MUTABLE PRODUCTION STATE and must
  never enter any reproducibility bundle (enforced by exclusion tests).

## F. External executables

| Executable | Required by | How invoked | Status |
|---|---|---|---|
| `claude` (`-p`, bounded turns/timeout, `--permission-mode dontAsk`) | Editorial/research provider transport (Morning etc.) | Subprocess with finite timeout; never in offline tests (fakes injected) | EXTERNAL_CONTROLLED |
| `openclaw message send` | Telegram notifier transports | Subprocess; never in offline tests | EXTERNAL_CONTROLLED |
| `node --check` | Release-CLI JS validation hook | Optional; skipped when absent | HOST_PROVIDED |
| `gh api` | Release-CLI NullOne-CI proof | Read-only; offline tests patch the adapter explicitly | EXTERNAL_CONTROLLED |
| `git` | Release CLI (exact-SHA extraction) | Local repository operations | HOST_PROVIDED |

## G/H. OpenClaw and Claude Code versions

| COMPONENT | VERSION (captured) | SOURCE_OF_TRUTH | STATUS |
|---|---|---|---|
| OpenClaw | 2026.8.2 (commit `0965053`) | install receipt / deployment docs | EXTERNAL_CONTROLLED |
| Claude Code | 2.1.268 (captured) | `claude --version` | EXTERNAL_CONTROLLED |

Pinned by `scripts/capture-runtime-versions.py` output at capture time;
upgrading is out of scope (explicit non-goal of #8).

## I. Node runtime

`node` v22.23.2 captured on this host; required ONLY as an optional
validation hook. Not a runtime dependency. HOST_PROVIDED.

## J. Image/media fixtures

Offline validation generates its source fixture deterministically with
Pillow itself (solid/pattern PNG in a temp dir) — no binary fixture is
committed and no network/Zernio/Telegram/model is touched. Fixture spec
JSONs live under `tests/fixtures/` (text only).

## K. Environment variables / SecretRefs (NAMES ONLY)

| Name | Kind | Consumer |
|---|---|---|
| `NULLONE_WORKSPACE` | workspace root override | scripts/tests |
| `ZERNIO_ANALYTICS_API_TOKEN` | env secret (never a value here) | analytics provider factory |
| `ZERNIO_DRAFT_API_TOKEN` | env secret (never a value here) | draft provider factory |
| `zernio.publish.bearer` / store entry `ZERNIO_PUBLISH_API_TOKEN` | OpenClaw SecretRef | publish provider factory |
| `NULONE_ALLOW_REAL_PROD`, `NULONE_NO_FETCH` | release-CLI dev guards | release CLI only |

No values, tokens, OAuth state, sessions, presigned URLs, or Telegram
owner IDs appear in any reproducibility artifact (enforced by tests).

## L/M/N. Writable dirs / immutable inputs / excluded state

- Immutable repository inputs (hashed in `ops/runtime-inputs.json`):
  renderer sources, `visual-rules.md`, `style-profile.md`, fixture JSONs,
  dependency manifests, capture/validator scripts.
- NEVER hashed or bundled: secrets, production manifests, candidate queue,
  ledgers, receipts, deploy-state backups, logs, sessions/auth files,
  `social/state/**`.

## Release-CLI boundary (no duplication)

- Release CLI guarantees: exact reviewed Git bytes for managed workspace
  files. It does not install host packages, fonts, or OpenClaw.
- Runtime inventory guarantees: required host/runtime dependencies are
  known, with recovery owners.
- Future integration (not implemented): bootstrap preflight of dependency
  versions. Documented here as future work; no deployment behavior changed.
