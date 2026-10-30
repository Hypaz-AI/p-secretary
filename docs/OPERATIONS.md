# Operational reference (advanced)

For a new installation, start with the Italian [quick start](../README.md) and
[Docker guide](DOCKER.md). The host/virtualenv deployment section below
is an alternative for operators, not an additional step after Docker setup.
Flows and diagrams: [AGENT_FLOWS.md](AGENT_FLOWS.md). Settings: [CONFIGURATION.md](CONFIGURATION.md).

## Architecture in one page

One **agent** (GitHub Copilot SDK, LLM = OpenRouter) interprets owner requests and
incoming messages using typed tools. The model is never an authority: native
provenance, consents, leases and receipts decide what is allowed. There are no fixed
classification/plan pipelines; to add a capability, add a tool in `secretary/agent/toolkit.py`.

`main()` calls `db.init()` **before** flows and workers. Migrations are additive and
run under advisory locks in one transaction: native messages/attachments, jobs, task
lifecycle, media, capabilities (with legacy provenance captured **first**), policy,
knowledge, AI budget, the `agent_*` tables and the `agent` views (`secretary/agent/schema.py`).
It then starts six durable workers, one job at a time per kind: `analysis` (incoming
triage), `owner` (dialogue), `digest`, `action` (the only place with external effects),
`media` and `knowledge`. Named periodic activities have at most one live launcher thread
each, in addition to the workers and their lease-heartbeat threads. **Run one
scheduler/owner instance per deployment.** HTTP reads never bootstrap or migrate.

Verify upgrades on an isolated clone, not on live accounts or user data. No startup
step grants consents or infers delegations.

### The agent runtime

- One `CopilotClient` (a ~150 MB binary, about 190 MB of memory in use) runs in a dedicated thread,
  `mode='empty'` (no shell, file, network or git tools); any permission request other than
  the turn's own tools is denied. Sessions are **ephemeral**: the context is rebuilt from the
  database on every turn and the session is deleted at the end.
- The runtime talks only to a **loopback gateway** in the app process. The OpenRouter key
  never enters the runtime; each turn gets a one-shot bearer bound to its job, and every
  request is accounted in the ledger with an explicit `max_tokens`. Lost lease, exhausted
  budget or unavailable ledger refuse the request **before** contacting the provider.
- The runtime binary is **baked into the Docker image** (`COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime`,
  `COPILOT_SKIP_CLI_DOWNLOAD=1`) because the container's cache directory is a tmpfs; nothing is
  downloaded at start. Its version is fixed by the pinned `github-copilot-sdk` (arm64 and amd64
  builds exist; the runtime must match the host architecture (arm64 on a Raspberry Pi 4), so build the image **on the target host** or with
  a multi-arch builder).

### Read-only database role

The agent reads only the `agent` schema (views that embed the consents). Its second,
independent barrier is the `agent_ro` role: `NOLOGIN`, no attributes, `SELECT` on those views only.
**The app provisions it on every start** (`db.init` → `agent.schema.ensure_readonly_role`,
best effort, idempotent, atomic):

- **Local compose database** (the `secretary` user is a superuser) or any user with `CREATEROLE`:
  the role is created at the first start. No manual step.
- **External database, application user without `CREATEROLE`** (the production case): the DBA runs
  **once** PART 1 of `deploy/sql/agent_ro.sql` (`CREATE ROLE agent_ro NOLOGIN; GRANT agent_ro TO <app user>;`).
  `USAGE` on the schema and `SELECT` on the views are then granted by the app itself (it owns
  them), so the order of operations does not matter and views added by later versions are
  covered through default privileges. Restart the app after the DBA step.
- Without the role the app still works (AST guard + READ ONLY transaction), prints a warning at
  startup with the two statements, and the dashboard page **Agente** shows «Protezione database».
  `AGENT_DB_ROLE=` (empty) means "no role wanted".

On PostgreSQL 16+ a role creator with `CREATEROLE` is only an *administrator* member and
`SET ROLE` is denied until an explicit `GRANT`; the app checks the `SET` privilege (not mere
membership) and grants it. Verification, as the application user: `SET LOCAL ROLE agent_ro;
SELECT count(*) FROM agent.messages;` works, and `SELECT count(*) FROM public.email_messages;` must fail.

### Retiring the old secretary tables (optional)

The previous secretary (practices, responsibilities, reviews, email execution, research,
owner dialogue/instructions, reconciliation) is gone from the code, but its tables remain
in the database, unread. After a backup, `deploy/sql/drop_legacy_tables.sql` removes them in
one transaction and aborts if a non-legacy table depends on them. It is **never run by
the application**.

## Provenance and consent

`ai_text`, `ai_media` and `ai_history` are separate default-deny consents per
email/Telegram/WhatsApp channel (the Telegram bot and personal account share consent).
They are embedded in the `agent.*` views and re-evaluated at every query, live mail read,
provider send and digest seal. Historical imports are read-only context
(`policy.can_read_context`) and never enable commands, tasks, effects or media conversion.
Native owner authority is only live, direct, non-forwarded text in the private bot chat.

### Explicit recovery of consent-blocked media

Denied live media conversion becomes terminal `blocked/media_consent_required`. Changing
consent or replaying storage does not reset it. In the dashboard (**Permessi AI** →
*Recupero media bloccati*) an explicit POST `/api/media/retry-blocked` with exactly
`message_id,expected_channel` (`telegram`, `telegram (account)` or `whatsapp`) re-queues it.
Both current text and media grants and unchanged live provenance are required. Only eligible
blocks with no recorded provider attempt and no transcript/active lease are reset;
historical sources and ambiguous prior provider attempts stay excluded. Limits: 20
attachments per request, three consent resets per attachment; each recovered job keeps the
normal eight-attempt bound. Recovery does not grant consent or call AI inline; the worker
rechecks before inference and publication.

Media that were **never persisted** (for example a WhatsApp download that kept failing) are
reported by `messages.media_missing` in the agent views and in the audit trail
(`MEDIA_ERRORS` in `secretary/connectors/whatsapp.py`); the bridge now retries the download with
backoff and records the outcome instead of silently dropping the attachment.

### Outgoing email

The agent only **drafts** (`email_propose` → `agent_email_outbox`, state `proposed`).
`/invia ID` (current native owner text, `SECRETARY_MODE=active`) approves it; the `action`
worker then sends one text-only message to one bare address from a configured account. An
attempt is committed before SMTP; a timeout, crash or false return is `uncertain` and is
**never** resent automatically. `accepted` means the SMTP server accepted the message, not
that it was delivered or read; revoking cannot recall a send already in flight. No
attachments, threads/Message-ID, display names, lists or custom headers.

### Real AI caps (not a money budget)

`ai_budget` enforces `AI_BUDGET_DAILY_CALLS=200`, `AI_BUDGET_DAILY_TOKENS=500000`,
`AI_BUDGET_MAX_INFLIGHT=2` and `AI_BUDGET_MAX_OUTPUT_TOKENS=65536` on every agent request
(through the gateway) and media conversion, per UTC day. Every HTTP attempt or retry consumes
one call plus a reservation of serialized input + maximum output + overhead tokens, without
refunds; higher provider usage is charged too. Each ledger row carries a server-derived
`scope` (`agent_owner`, `agent_inbox`, or `global` for media); the optional
`AI_BUDGET_AGENT_OWNER_DAILY_CALLS` / `AI_BUDGET_AGENT_INBOX_DAILY_CALLS` can only **narrow**
the global cap, 0 or absent disables the scope cap and invalid values fail closed. An
exhausted budget **defers** the job without consuming attempts. Workers fail closed when the
DB, ledger or lease is unavailable. Crash-reserved slots are not released automatically:
reconcile (`POST /api/budget/reconcile`) only after establishing that no request is live.
Reported cost NULL means unknown, not free. Web search has no dedicated budget (explicit
decision). Configure monetary limits separately at OpenRouter.

## Validation and CI isolation

`.github/workflows/docker.yml` has an offline job (explicit agent/dashboard/budget contract
modules, then full `unittest discover` with `env -i`, no deployment dotenv, temporary data
directory and no inherited credentials, plus `sh -n`, `node --check` and an image build) and
a PostgreSQL job that repeats discovery against a disposable service with an explicit
`secretary_test` database and Unix-socket URL. Neither job starts `python -m secretary` or touches
production, and the real-runtime scenarios are skipped there unless
`P_SECRETARY_TEST_AGENT_RUNTIME` points to a runtime binary. These are workflow definitions,
not a claim of a green CI run.

Opt-in SQL classes need `P_SECRETARY_TEST_DATABASE_URL` naming a disposable `_test` database
(no libpq service, random schemas, never `DATABASE_URL`); some need a Unix socket. Some
`*PostgresTests` classes bootstrap their own temporary cluster even without the URL: run them
by explicit class. Never point the test URL at a production cluster. See `AGENTS.md` for the
exact commands.

Frontend: `tests/dashboard/test_web_session_safety.py` runs the real `charts.js` + `app.js` in a Node VM with
a synthetic DOM (no browser): session fencing, confirmations, budget levels, configuration lifecycle, the
communications list/detail and the overview. The fake DOM rejects `append(false/null)`, which a real browser
would render as text. To look at the
interface with fictitious data, create a disposable database whose name ends in `_demo` and run
`P_SECRETARY_DEMO_DATABASE_URL="host=... dbname=secretary_demo user=... password=..." python -m tests.demo_dashboard`
(login `demo` / `demo`, port 8099). The runner refuses any other database name, uses placeholder tokens and
never touches the network.

## Email attachment filesystem storage details

New email attachments (PDFs and all other binary attachments, including empty
files) are stored under `DATA_DIR/email_attachments/<sha256>` with atomic
publication and file/directory fsync. PostgreSQL retains filename, content type,
metadata and absolute path, with `data` NULL. Existing BYTEA rows remain readable;
no schema change or automatic migration is required. Back up this directory
alongside PostgreSQL; moving DATA_DIR requires updating existing absolute paths.
File failures abort message storage and its durable queue transaction. A DB
rollback can leave harmless unreferenced files; do not delete files automatically.

Optional existing-blob migration: after backing up the database and data folder,
run `python -m secretary.maintenance.migrate_email_attachments --apply --limit 100` using the application
environment. Omit `--limit` for all email blobs. Without `--apply`, no database
connection is made. Each row is locked, bytes/SHA256 verified on disk, then path
and `data=NULL` updated in one transaction. It is resumable; failures preserve
the current blob and previously committed rows remain migrated. No VACUUM or
blob/schema deletion is performed. Non-email attachments are unchanged.

# Durable processing and operation

## Architecture

`pipeline.ingest` commits the native message **and** one `analysis` job in a single
transaction; email UID seen flags and the WhatsApp spool acknowledgement happen only after
that commit. Job payloads live in PostgreSQL, not in an unbounded in-memory queue, and
connections are thread-local and short-lived with connection, statement and lock timeouts.

Jobs use `SKIP LOCKED`, token fencing, 15-minute crash leases, a heartbeat every 30 seconds
and a one-hour execution deadline. Ordinary failures retry with exponential backoff (15 s to
1 h), at most eight attempts, then a visible `failed` state. `Deferred` work (paused flow,
missing consent, exhausted budget) does not consume attempts. A lease cannot recall in-flight
I/O. `jobs.assert_current` rechecks the lease before every effect. Use `POST /api/jobs/requeue`
(`analysis`, `owner`, `digest`) for failed jobs, and the media recovery API for consent
blocks. **Do not reset job rows with bulk SQL**: a reset renews no authorization and does not
make an attempted/uncertain send safe; review native authority, leases and effect receipts first.

Retention (see the section below) is the only automatic deletion: one daily pass prunes expired history, its files and
operational data. Job rows and deduplication identities are never deleted. Plan storage capacity accordingly.

Notifications, owner replies, ZIP sends and outgoing email commit an attempt **before** remote
I/O. Attempted/uncertain non-idempotent effects stop for reconciliation: retries never blindly
resend after a timeout, a lost reply or a crash. Acknowledged acceptance is not proof of
delivery, and there is no global exactly-once guarantee; deterministic Calendar IDs and
content-addressed local writes allow narrower safe reconciliation. `notified=true` follows
acknowledged Telegram acceptance. The daily digest is a durable job (`digest:YYYY-MM-DD`),
queued idempotently at `DIGEST_TIME`; without readable sources it does not call the model.
Dashboard metrics report queue states, oldest age, completion, acknowledged notifications,
tasks and pending/proposed Calendar entries.

## Calendar and tasks

The agent never writes to Calendar: `calendar_propose` stores a **local proposal**. External
events also default to `proposal`. `/appuntamenti` lists local IDs and sync state next to remote
events, deduplicated by Google event ID. `/conferma ID` requests a sealed create grant and
`/annulla ID` a separate cancel grant. Only current direct native owner text can grant; task
creation, deadlines and model output cannot. Invalid or unconfigured seals leave a local
proposal. SHA-256 keys produce deterministic Google IDs; a create 409 is reconciled by GET
against the expected fields.

`secretary_effect_grants_v3` binds event payload/state and the current native owner text to the
explicit configured Calendar ID, timezone and normalized start/end/duration/reminder values.
Execution rechecks target, configuration and expiry before I/O and uses the sealed target.
Expired grants, or legacy grants with a NULL target, fail closed; a replay cannot extend expiry
or bind an old command to today's destination: obtain fresh owner authorization rather than
backfilling a target. Credentials alone are not permission. Sender-label auto-create
(`CALENDAR_EXTERNAL_AUTO` / `CALENDAR_TRUSTED_SENDERS`) is not active. Updating existing events,
availability planning and remote drift repair are **not implemented**: cancel/create needs fresh
explicit authorization. Source text is evidence, not authority.

`/task title | YYYY-MM-DDTHH:MM+02:00` creates an owner task (deadline optional); `/attivita`
lists open tasks; `/fatto ID` completes one. Date-only deadlines stay date-only (default
reminder 09:00 local); timed deadlines default to a reminder at the deadline unless an explicit
reminder overrides it. Tasks created from incoming messages keep a reminder identity within 24 h
of the deadline, overdue included. In dialogue the agent can create, edit, complete or cancel
tasks, even in bulk, only on an exact owner phrase in the current message (verified against the
archived text); incoming messages from third parties can only create **new** tasks with an exact
quote and can never modify existing ones. Every change is versioned with its evidence.

## Remaining operational scope

Not done: Calendar update, availability and remote drift; outgoing email in threads and with
attachments; erase inside backups (erase covers the database and reports the files to delete);
a full restored-backup drill; qualitative evaluation of the agent on the real archive; unit
tests of `secretary/agent/mail.py` against a real IMAP server and of `secretary/agent/web.py` against the network (both are covered offline with fakes).

## Recovery, invoices and delivery limits

`RECOVERY_DAYS=14` (clamped 1–90) bounds email, WhatsApp and Telegram recovery.
Email uses durable account/folder/UIDVALIDITY/UID identities and overlapping scans,
100 unseen messages per poll. Personal Telegram stores per-dialog scan watermarks
with one-day overlap; only uncapped scans advance. Existing dialog/message limits
still apply; very busy dialogs may exceed caps and require explicit operator
backfill. WhatsApp recovery fetches at most 200 messages in each of 100 chats;
unavailable web history cannot be guaranteed. Bot API server retention also limits
recovery. No connector imports unlimited history.

WhatsApp has one JS handler and an in-flight guard. Python waits for enrichment
and acknowledges only matching spool versions. A crash during enrichment can
leave an incomplete file; inspect/remove that file to allow source recovery.

XML extraction rejects DTD/entity declarations including UTF-16/32; PDF uses
pypdf with input/page/output/content limits. These are defensive application
limits, not a hard CPU/memory sandbox for hostile compressed PDF streams. No OCR,
P7M verification, ZIP extraction or accounting/tax validation is provided.
Invoices are SHA256-deduplicated going forward; old rows are not hashed/backfilled.
Unknown types use .bin, not a fake .pdf. Filesystem and DB cannot commit atomically;
a crash can leave an orphan content-addressed file, safely reused on retry.
Closed months are queued from the first day of the next month, including catchup
and unknown entities. A late invoice after a month's ZIP was already sent does
not automatically resend that ZIP; review/reissue manually.

## Installation and deployment (operator only)

Use a dedicated Python 3.11+ virtualenv; install `requirements.txt` and freeze the resolved
platform-specific versions into a reviewed lock artifact (the repository pins ranges, plus an
exact `github-copilot-sdk`). **Download the agent runtime once**, as the service user:
`COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime python -m copilot download-runtime`, and run the
service with `COPILOT_CLI_EXTRACT_DIR` set to the same directory and `COPILOT_SKIP_CLI_DOWNLOAD=1`.
The runtime must match the host architecture (arm64 on a Raspberry Pi 4). WhatsApp requires Node + Chromium and
`npm ci` using the reviewed `deploy/whatsapp/package-lock.json`; do not mix architectures (x86 Chromium on an arm64 host, or vice versa).

`deploy/systemd/p-secretary.service` is a template; adjust user/paths before installing. Keep secrets
and session files private (0700 directories, 0600 files). Do not expose the HTTP dashboard on the
Internet: bind loopback:8080, terminate HTTPS with Caddy/nginx using a valid certificate, set
`DASHBOARD_COOKIE_SECURE=true` and `DASHBOARD_HOSTNAME` to the public hostname, and configure
proxy headers deliberately (never trust arbitrary `X-Forwarded-*`). Existing port-80 overrides
must be removed or reviewed separately. Run one scheduler/owner instance per deployment.

Startup applies the additive migrations under the schema advisory lock, including
`durable_work_v2` and its runtime upgrade. The legacy native migration refuses an unapproved
history conversion and `db.init` rejects resets. Back up and test the migration on a clone first.

### Upgrading from the previous secretary

1. Back up the database and the private config/media; test on a clone with external I/O disabled.
2. Run `sh ./p-secretary up`: it rebuilds the image and recreates the container (the Dockerfile copies modules **by name**).
3. The new `agent` schema and `agent_*` tables are created at startup. If the application user
   cannot create roles, ask the DBA to run PART 1 of `deploy/sql/agent_ro.sql` (two statements) and
   restart. Existing flags (including `false`) and consents are preserved; nothing
   is auto-allowed, and optional invoice/ZIP flows of unknown provenance stay paused.
4. Review ambiguous old effects (attempted/uncertain receipts) before enabling `SECRETARY_MODE=active`.
5. Only after a backup, optionally run `deploy/sql/drop_legacy_tables.sql`.
6. Attachments stored with the absolute path of a previous host keep working for the agent: the
   `agent.attachments.rel_path` view column keeps only the part from the media directory name
   (`email_attachments/`, `tg_files/`, ...) onward and the file box resolves it under `/data`.
   Files that are genuinely missing on disk are **considered lost by decision of the owner**: no
   repair or reconciliation is attempted and no rows are rewritten; `stored` and `media_missing`
   in the views tell which ones they are, so the agent says so instead of inventing content.

## Retention

One daily pass (`secretary/workflows/retention.py`, started by the main loop). If a backlog remains, the
next periodic tick resumes the same day. Each batch is one transaction; files are removed only **after**
the commit. Settings: `RETENTION_MESSAGES_DAYS` (default 730), `RETENTION_OPERATIONAL_DAYS` (90),
`RETENTION_AI_USAGE_DAYS` (365); `0` disables a class, out-of-range values fall back to the default.

| Class | Clock | Removed | Always kept |
| --- | --- | --- | --- |
| Messages | `acquired_at` (archive time, not message date) | Native rows of email/WhatsApp/Telegram, attachment rows, analysis, knowledge text/entries, owner dialogue rows | Anything still in use: task source/origin, task changes, notes, receipts `authorized`/`attempted`/`uncertain`, pending/running/blocked jobs, undecided or unsent email drafts, live Calendar grants, unexpired digest sources |
| Files | Follow their rows | `email_attachments`, `tg_files`, `wa_media` files no longer referenced by any row, plus `email_attachments` orphans | Invoices, workspace, ZIPs, WhatsApp/Telegram spools and sessions are never swept. A file must be older than 30 days (modification time; deduplication reuse refreshes it) |
| Finished jobs | `finished_at` | The `payload` of `done` jobs | The job row and its key: keys are deduplication identities (`analysis:<id>`, `task-reminder:<id>`, `digest:<day>`). Failed jobs stay for review |
| Email outbox | `sent_at`/`decided_at` | `sent` and `rejected` drafts | `proposed`, `approved`, `uncertain`; effect receipts |
| AI ledger | `usage_day` (UTC) | `completed` usage | `reserved` and `uncertain` entries |

File references are matched by relative key (`email_attachments/<sha256>`, `tg_files/<name>`), not by absolute
path, so a database migrated from another host still protects its files. A file is never removed while any row
references that key.

Never deleted by retention: `message_dedup`, `email_seen`, `effect_receipts`, `privacy_receipts`, notes, tasks,
appointments, rules, invoices, monthly ZIP records, Calendar grants.

Operational notes:

- Take a database and data-folder backup before the first pass after lowering a window: removal is irreversible.
- While `secretary_links` or `secretary_runtime_link_proposals` exist (legacy schema), message pruning is skipped and
  logged as `saltato=legacy_tables`. Drop them with `deploy/sql/drop_legacy_tables.sql` after a backup.
- A pruned message cannot be re-ingested by the connectors: its deduplication identity remains. Do not re-run a
  historical import over a period that was already pruned, because it would archive those messages again.
- Owner-requested erasure (`privacy.erase_message`) stays the explicit path with receipts; retention writes no receipt per message.
- Backups are not pruned by this pass. Their retention remains an operator decision (see the drill below).
- Logs contain only counters (`[retention] messages=… files=…`), never message content or paths.

## Backup and restore drill

1. Stop ingestion/sends in a maintenance window; wait for workers to stop.
2. Take a PostgreSQL custom-format pg_dump of the dedicated database and a
   consistent filesystem backup of invoices, data media/spools, OAuth/session
   files, configuration and dashboard credentials. Encrypt backups and restrict
   access; keep copies off-device with a documented retention schedule.
3. Restore pg_dump with pg_restore into a NEW isolated database, restore paths
   and permissions, and point a staging config at it with all network sends
   disabled. Verify source counts, attachments, invoice hashes, job/task states,
   migrations and a sample ZIP. Never test restore against production.
4. Review running/failed outbox jobs for ambiguous delivery before enabling
   workers. Reconcile attempted/uncertain Telegram/email/ZIP receipts, never
   blindly resend. Google deterministic IDs reconcile bounded creation; verify
   sealed Calendar target/time and grant expiry before enabling sync. Restored
   legacy/unbound grants must not be upgraded into fresh authority.
5. Switch the service only after verification; retain the previous database and
   filesystem snapshot for rollback. Do not roll back only the DB while retaining
   a newer spool/media tree without reconciliation.

This is a drill procedure, not a record of a completed full restore or production
readiness. Record actual restore evidence and semantic checks separately.

Validation targets: isolated unittest discovery, compileall and diagnostics.
Opt-in PostgreSQL classes require P_SECRETARY_TEST_DATABASE_URL naming a
disposable `_test` database and random test schemas, never DATABASE_URL fallback.
Some existing auto-PG classes instead create their own temporary local clusters;
absence of a test URL does not mean all SQL tests skip. Current full-validation
results belong to the parent run, not this documentation-only change.