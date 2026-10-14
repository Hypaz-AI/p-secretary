# Implementation checklist

- [x] Additive schema, durable analysis queue and action outbox with leases/retries
- [x] Bounded independent ingestion, analysis, action and owner-dialog workers
- [x] Calendar proposal policy, deterministic sync identities, confirmation/cancellation
- [x] Persistent actionable tasks, deadlines, completion and reminders
- [x] Closed-month ZIP catchup, safe document extraction and content deduplication
- [x] WhatsApp enrichment/ack race and bounded recovery
- [x] Operational metrics, deployment and recovery documentation
- [x] Offline regressions, isolated opt-in PostgreSQL tests, full validation

Validation: full unittest discovery, compileall and editor diagnostics. PostgreSQL
integration intentionally skipped without an explicit disposable test URL.
See OPERATIONS.md for scope limits: no natural-language event update, legacy
backfill, fully resolved dependency lock or hard PDF parser sandbox. The legacy
synchronous classifier remains private for compatibility tests; runtime routes
through pipeline.ingest exclusively.

No production migrations, service restarts or external sends performed by this implementation.