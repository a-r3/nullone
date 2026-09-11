# NullOne Restore Drill Report (fixture-only, issue #10)

Status: empirical fixture proof for the ADR-01 recovery contract.
Repository-only; sanitized fixtures in temp directories. No production,
network, provider, secret, or Telegram contact of any kind.

Oracle: `docs/adr/ADR-01-recovery-and-artifact-durability.md`,
`docs/operations/recovery-contract.md`, `ops/recovery-policy.json`.
Harness: `scripts/recovery/restore_drill.py`.
Fixtures: `tests/fixtures/recovery/` (`source_kind=SANITIZED_FIXTURE`).

## What the fixture drill proves

- A complete sanitized snapshot restores into a fresh temp root with
  checksums, record counts, referential bindings, exact media bytes, and
  complete critical history all validating (scenario A: SUCCESS).
- Publisher stays DISABLED in every scenario, including the complete one
  — completeness never auto-enables publication. No code path performs
  publication (`FAKE_PROVIDER_PUBLISH_COUNT=0`,
  `FAKE_PROVIDER_WRITE_COUNT=0`, `FAKE_TELEGRAM_SEND_COUNT=0` always).
- Stale `attempts=0` + provider PUBLISHED reconciles forward to PUBLISHED
  in the disposable root with `retry_performed: false` (scenario D).
- Stale `attempts=0` + remote DRAFT + missing history → CHECK_REQUIRED,
  no retry (scenario H). Remote DRAFT proves nothing.
- Partial/corrupt critical history → CHECK_REQUIRED, publisher disabled
  (scenarios C, K). Provider unreachable → UNKNOWN/CHECK_REQUIRED (G).
- Missing notifier history forbids auto-resend (no NOTIFIER certainty
  fabricated). Expired-reference-without-bytes → MEDIA_RECOVERY_GAP (E).
- Truncated counts, missing files, and checksum mismatches fail closed
  (I, J). Interrupted restores never become READY and revalidation
  reports INCOMPLETE (L). Encrypted-backup-without-key → BLOCKED with
  `BACKUP_USABLE=false` and no crypto theater (B). Backup outage
  degrades ordinary availability without changing critical safety (F).
- Every run writes `restore-report.json` with measured
  `FIXTURE_RESTORE_DURATION_MS` (single-digit milliseconds for fixtures).

## What it does NOT prove (explicit gaps)

- PRIVATE_SNAPSHOT_VALIDATION=NOT_EXERCISED — real production snapshots
  were never read; that mode requires separate explicit authorization:
  PRIVATE_SNAPSHOT_AUTHORIZATION=REQUIRED_RAUF_ALIZADA.
- REAL_CREDENTIAL_RECOVERY=NOT_EXERCISED — no secret, OAuth, session, or
  token recovery was attempted or inspected.
- REAL_OPENCLAW_CONFIG_RECOVERY=NOT_EXERCISED — live automation config
  untouched.
- REAL_EXTERNAL_BACKUP=NOT_PROVISIONED — no backup destination exists.
- REAL_ENCRYPTION_PROOF=NOT_EXERCISED — no encrypt/decrypt cycle proven;
  only the key-loss decision rule (scenario B).
- SEPARATE_FAILURE_DOMAIN_COPY=NOT_PROVISIONED.
- PRODUCTION_RPO_PROVEN=NO — fixture milliseconds prove nothing about the
  accepted 900s design target in production.
- PRODUCTION_RTO_PROVEN=NO — same for the accepted 14400s design target.

## Scenario table

| Scenario | Setup | Expected | Publisher |
|---|---|---|---|
| A complete fixture | intact snapshot | SUCCESS | DISABLED |
| B key lost | require key, no key | BLOCKED, BACKUP_USABLE=false | DISABLED |
| C partial history | receipt removed | CHECK_REQUIRED | DISABLED |
| D stale + PUBLISHED | provider PUBLISHED | SUCCESS + forward reconcile, NO RETRY | DISABLED |
| E URL expired | bytes removed | MEDIA_RECOVERY_GAP | DISABLED |
| F backup outage | remote unavailable flag | SUCCESS + degradation gap | DISABLED |
| G unreachable | provider UNREACHABLE | UNKNOWN / CHECK_REQUIRED | DISABLED |
| H attempts=0 + DRAFT + missing | receipt removed, DRAFT | CHECK_REQUIRED, NO RETRY | DISABLED |
| I truncated JSONL | hash/count mismatch | FAILED | DISABLED |
| J missing file | manifest declares absent file | FAILED | DISABLED |
| K corrupt receipt | invalid JSON receipt | CHECK_REQUIRED | DISABLED |
| L interrupted | stop before finalize | INTERRUPTED, never READY | DISABLED |

Determinism claim: STRUCTURAL (same checks, same verdicts) — never
cross-host byte identity, never production RPO/RTO proof.
