# NullOne offline regression tests

Run the complete repository-local safety suite with:

```bash
python3 tests/run_offline.py
```

The behavioral suite uses only synthetic data and temporary filesystem paths.
Provider, notifier and network behavior is replaced with fakes or fail-closed
sentinels. It must not call real Claude, Zernio, Telegram or production state.

Covered behavior includes:

- verification and exact-content hash tampering guards;
- missing approval and consumed publication-attempt guards;
- ambiguous publication -> `UNKNOWN` with no automatic second attempt;
- accepted publication + readback failure -> unsafe-to-repeat;
- ambiguous review-draft creation -> `REVIEW_UNKNOWN`;
- notification failure with publication attempt unchanged;
- serial append-only duplicate suppression;
- workflow/domain completion semantics for Analytics;
- fail-closed detection if an isolated test unexpectedly reaches a real
  subprocess or network seam.

Not covered / not upgraded to implemented guarantees:

- authenticated callback provenance, sender/chat/message/stage/expiry/replay;
- deterministic prevention of a second live publish tool call inside one
  model session;
- concurrent transactional publication authorization;
- production domain-outcome emission and durable run identity.

Those remain downstream implementation work. A green offline suite does not
replace the active production reliability proof.
