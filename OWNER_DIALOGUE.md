# Durable owner dialogue

## In breve

Il dialogo naturale del proprietario archiviato integra storico persistente,
memoria personale, attività e appuntamenti, con conferme dopo il commit e risposte
in coda durevole ordinata. Il rollout è automatico e additivo: al prossimo avvio
`db.init` applica le migrazioni necessarie senza azzerare lo storico.
Serve riavviare il servizio; **il riavvio in produzione non è stato eseguito
nell'ambito di questo aggiornamento**.

## Integrated route and trust boundary

`main._owner_job` scopes the archived message ID to the worker, clearing it in
`finally`. For natural-language chat with that ID, `main.handle_user_chat` calls
`owner_dialogue.respond` and returns without a duplicate legacy send/event path.
`brain.chat_with_user(..., dialogue=...)` consumes durable context and the supplied
history without its legacy ten-message slice. Authentication precedes this
boundary: `respond` is not an owner authenticator.

Explicit slash commands retain their handlers. Manual chat without an archived
owner ID retains `_chat_history`, direct replies and legacy immediate memory
writes, outside the coordinator's transactional guarantees.

For media turns, only the owner's caption and audio transcript become current
owner text. Image OCR remains archive/visual data, never task or memory authority;
media contents do not become slash commands or learned rules. Images, quoted or
forwarded material, recalled exchanges and summaries are untrusted data, not
instructions. Assistant proposals are not owner approval. Prompt-level trust
separation does not prove correct model interpretation in every case.

## Durable plan, commit and replay

`owner_exchanges` stores original text/time, image presence, prepared JSON plan,
final reply and completion time, not photo base64 or Telegram download URLs.
`owner_dialogue_state` holds the summary/checkpoint and a tokenized twelve-minute
lease. Earlier pending/running owner jobs block later turns by **job ID**, not
timestamp. Original time is `communications.ts`, falling back to `acquired_at`,
never the processing/retry clock.

1. Acquire the lease and load context. Task extraction, memory preparation and
  dialogue generation run outside database transactions.
2. Persist a fenced plan containing validated task changes, prepared memory
  changes and their original note snapshot, parsed event candidates, reply and
  preparation errors. This is a durable decision, not an effects receipt.
3. In one fenced transaction, apply tasks and memory changes, validate/save
  events, save the final reply/completion and enqueue reply chunks. The memory
  helper uses the caller's connection without committing it independently.
4. After commit, compact older dialogue. Outbox workers deliver separately;
  `respond` never sends Telegram messages directly.

Memory update/delete uses compare-and-set (CAS): lock all targeted notes and
compare original text against the prepared snapshot **before any memory write**.
A mismatch skips the memory batch and adds a reconfirmation question; valid task
and event effects may still commit. Adds deduplicate case-insensitive trimmed
text. The chat model sees provisional projected memory and does not write it.
Memory preparation failure is reported without falling back to legacy writes.

Task application uses a savepoint and version/status checks. A stale task batch
rolls back its task effects and commits a **durable clarification reply**, with no
memory or event effects for that turn. Preparation-time task ambiguity also blocks
events, but does not itself suppress the memory plan. Task extraction failure is
reported and does not universally block calendar candidates.

Effect confirmations come from tool results, not speculative model prose;
informational prose can accompany receipts. Calendar receipts say sync is queued,
not confirmed. Invalid event candidates receive failure explanations. Unexpected
database errors roll back the effects/reply transaction together.

A saved plan skips task/memory extraction and chat generation on retry. Completed
reply replay bypasses context/effects and only re-enqueues existing reply chunks.
Keys are `owner-commitment:{message_id}:{index}` for tasks,
`owner:{message_id}:event:{index}` for events and
`owner-reply:{message_id}:{index}` for replies; outbox keys deduplicate enqueueing.

Effects transactions lock and verify the live lease token. Summary updates also
require the unchanged checkpoint; release only clears the worker's own token.
There is no heartbeat during model calls: work exceeding twelve minutes can be
fenced out and retried. A crash before plan persistence can yield a fresh model
decision; stable decision reuse begins only after persistence.

## Reply ordering and external delivery

Replies use 3,500-Python-character notification chunks. `pipeline.action` defers a
chunk while an earlier owner-reply job is pending/running, including backoff or
leased jobs. This orders chunks and turns by outbox job ID, not lexical key or
message ID. Done/terminally failed jobs do not block; unrelated notifications and
Calendar work are not held by the reply gate.

External delivery remains **at-least-once**, not exactly-once: Telegram acceptance
followed by a lost acknowledgment can cause duplicate delivery. Database keys and
atomic local commits do not make remote delivery/synchronization atomic.

## Dates, tasks and reminders

`owner_commitments.prepare` validates explicit current-owner task evidence and
task identity/version. Historical assistant suggestions and quoted requests are
not fresh authorization. Relative dates use original message time in
`config.TIMEZONE`; past deadlines remain overdue, not silently moved forward.

Supported evidence dates are checked deterministically: `oggi`, `ieri`,
`l'altro ieri`, `domani`, `dopodomani`, `tra/fra N giorni`, `YYYY-MM-DD` and
`DD/MM/YYYY`. Checks use each item's evidence and separate deadline/reminder
clauses where supported; conflicting or mismatched dates ask for clarification.
This is not a general date parser: weekdays/unsupported expressions, clocks and
intent still depend on the model. Calendar candidates use `gcal.validate_event`,
not this task-date grounding routine.

A date-only deadline uses `owner_deadline_date` without an invented deadline hour;
its default reminder is **09:00 local** that day. A timed owner-task deadline
defaults to a reminder **at the deadline**, not 24 hours earlier. An explicitly
requested `owner_remind_at` takes precedence over both defaults. Timed task values
require an explicit offset matching the configured zone/DST. Presentation labels
requested times versus scheduler defaults. Reminder jobs are keyed by task/version
and recheck open status, version and schedule before sending; edits, completion
and cancellation invalidate obsolete reminders.

## Working context and real limitations

| Component | Current limit |
| --- | --- |
| Prior completed exchanges loaded | 100 |
| Recent history | 12,000 estimated tokens; complete chronological pairs |
| Lexical recall | 8 ranked records; 5,000 estimated tokens total |
| Recall owner/assistant excerpts | 4,500 / 2,000 characters per record |
| Summary batch; owner/assistant excerpts | 20 exchanges; 12,000 / 6,000 characters each |
| Summary output; accepted text | 2,500 tokens; nonblank, at most 6,500 characters |
| Open task context; photo reconstruction | 200 rows; last two selected photo turns |

Text tokens are estimated as UTF-8 bytes divided by three, rounded up, plus eight;
images cost 1,800 estimated tokens. Oversized newest pairs use labeled,
UTF-8-byte-bounded excerpts. Photos are restored only if budget permits; unavailable
images do not justify invented visual details. These are component targets,
**not a proven total provider-context cap**. Summary batching permits its first
excerpt above target and excludes previous-summary/JSON overhead; other chat
context adds cost too.

Recall is parameterized PostgreSQL Italian full-text **lexical search**, not
embeddings or exhaustive semantic memory. Sparse queries borrow recent owner-text
terms, not assistant/image data. Recall excludes incomplete, current/later and
selected recent exchanges, preserves absolute dates and stops at its estimated
budget. Synonyms and ambiguous follow-ups can be missed.

The summary is advisory; structured tasks, agenda and notes remain authoritative.
Checkpoint advancement covers only included exchanges and requires a live token.
Failed/invalid compaction leaves originals available; the initial batch-read error
can propagate after effects/reply commit, with completed replay still safe.
There is **no automatic bootstrap of old transcripts**: communications, imports
and `_chat_history` are not converted into completed exchanges or replayed for
effects. Model semantics, summary quality and provider capacity, latency and
availability remain dependencies; guards and transactions do not prove correctness.

## Startup migration and verification scope

`db.init` already invokes `owner_dialogue.migrate` after prerequisite migrations.
The additive migration uses an advisory transaction lock and `owner_dialogue_v1`
receipt, creates dialogue tables/search index and adds owner task date/provenance/
reminder columns and operation-key uniqueness. Receipted repeats are no-ops; the
existing task/migration tables are required. Deploy compatible files and restart
normally, without a reset or destructive rebuild; backup/lock precautions apply.

Offline regressions cover context, Unicode budgets, transactional memory, dates,
reminders, integration and reply ordering. Opt-in `test_owner_dialogue_postgres.py`
exercises real SQL contracts with external calls mocked, using only
`PI_SECRETARY_TEST_DATABASE_URL`, an explicit database ending in `_test`, and
isolated schemas. Mocks alone cannot prove PostgreSQL rollback/concurrency;
neither suite proves live model or remote-service quality. This documentation
update asserts neither a test count nor a completed production rollout.