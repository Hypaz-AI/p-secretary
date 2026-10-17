# Operational reference (advanced)

For a new installation, start with the Italian [quick start](README.md) and
[Docker guide](docker/README.md). The host/virtualenv deployment section below
is an alternative for operators, not an additional step after Docker setup.

## Integrated bounded secretary and dashboard boundary

The dashboard consumes `secretary.snapshot/resolve_approval`,
`knowledge.snapshot/search/open_source`, `capabilities.OPTIONAL` and the existing
`flows.settings/set_enabled` store. The optional runtime snapshot is projected to
displayable review/proposal fields, never prepared inputs/model prompts. Missing
modules report 501; unavailable schemas report 503 (knowledge may explicitly
return available=false). HTTP reads never bootstrap/migrate/start workers.

`main()` calls `db.init()` **before** flows and workers. The current startup is
wired as follows (migration steps 1–2 share advisory lock 73190421 and one
transaction; flows/workers start afterwards):

1. Captures legacy installation provenance before base/native migration creates
   its receipt table; it does not infer provenance from newly created timestamps.
2. Applies native messages/attachments, jobs, task reconciliation, media and owner
   dialogue migrations, then capabilities (with captured legacy provenance),
   policy, secretary, knowledge, secretary runtime, note provenance, execution
   and AI budget. `execution.migrate` and `ai_budget.migrate` are already wired.
3. Initialises flows preserving explicit flags. Native storage/revisions enqueue
   knowledge and live observations; periodic scheduling calls due_reviews and
   runtime backlog recovery. No startup consent grants or inferred delegations.
4. Starts eight durable workers, one job at a time per kind: `analysis`, `media`,
   `reconcile`, `action`, `owner`, `knowledge`, `secretary_review` and
   `secretary_email_execution`. Named periodic activities have at most one live
   launcher thread each; these are additional to the eight job workers and their
   lease heartbeat threads. Run one scheduler/owner instance per deployment.

This is an integrated **bounded review secretary**: review, local drafts and
owner-decision proposals with version/source/lease checks, plus separately
authorized effects. It is not arbitrary workflow/dependency execution. Source
checks on integrated paths are not certification of every direct/legacy call;
in particular, persistent AI accounting applies only in Worker job contexts.
Verify upgrades on an isolated clone, not on live accounts or user data.

Public APIs still needed:
trusted dashboard responsibility stop with CAS/audit/dependent invalidation, and
policy list/upsert with allowlisted channel/effect and CAS. Until those exist,
bounded trusted-only SQL helpers in dashboard.py implement those operations.
They accept neither table/column identifiers nor SQL from HTTP. They must not be
reused as arbitrary model tools. The configured owner is asserted server-side
only after login/CSRF; dashboard credentials grant owner-level control.

### Provenance and historical context

New practice/responsibility owner plans use schema v2. Before inference the
server resolves **all actual exported inputs**, including snapshot/history, to
flattened native `input_sources`; `input_versions` records snapshot references.
Applied immutable plans and bound receipts preserve those dependencies across
partial updates. Read-egress resolution also follows prior applied runtime review
inputs. Missing/cyclic/unverifiable manifests deny derived context; legacy v1
replay is not proof of permission to export its derived text to AI.

The shared owner-context/task-reconciliation resolver checks task source/owner
origins and every applied `task_changes.message_id`, not only the latest
`last_evidence_id`. Pending/rejected changes are excluded; missing/denied applied
evidence blocks that context. These checks do **not** supply full model-input
manifests for legacy task/personal-memory write plans or a complete typed-memory
conflict/provenance model.

`ai_text`, `ai_media` and `ai_history` are separate, default-deny consents per
email/Telegram/WhatsApp channel (Telegram bot and account share consent).
`policy.can_read_context` permits historical read-only AI context only with both
text and history consent; media/document sources additionally need media consent.
`policy.can_operate` continues to reject historical operations. Granting history
consent never enables command/task/effect replay or historical media conversion.
Read-only historical context support is not a promise that every consumer, such
as the operational digest, uses history.

### Dashboard link acceptance and attention acknowledgement

- POST `/api/secretary/runtime/links/<id>/accept`, body `expected_version` only:
   accepts the exact stored proposal version, evidence revision and practice/
   responsibility scope. The server rechecks native origins, creates the link,
   advances the practice version, invalidates dependent approvals and queues a
   review. Receipt replay is historical, not renewed permission. It cannot link
   arbitrary search results or accept replacement evidence/authority from JSON.
- POST `/api/secretary/runtime/practices/<id>/acknowledge`, body
   `expected_version,attention_hash`: acknowledges only the displayed practice
   version and SHA-256 attention snapshot. Unchanged attention is silenced and
   queued stale/acknowledged review notifications are suppressed at send checks.
   It neither completes the practice nor changes delegation or permissions.

Both require login, same-origin/CSRF and backend-constructed DashboardOwner, reject
unknown/duplicate JSON keys and return `external_execution=false`. Neither sends
externally; accepting a link can schedule a later consent-gated review.

### Explicit recovery of consent-blocked media

Denied live media conversion becomes terminal `blocked/media_consent_required`.
Changing consent or replaying storage does not automatically reset it. From an
opened current source, the dashboard offers an explicit POST
`/api/media/retry-blocked` with exactly `message_id,expected_channel`; native
channels are `telegram`, `telegram (account)` or `whatsapp`.
Both current text/media grants and unchanged live provenance are required.
Only eligible blocks with no recorded provider attempt and no transcript/active
lease are reset; historical sources and ambiguous prior provider attempts remain
excluded. Limits: 20 attachments per request, three consent resets per attachment;
each recovered job retains the normal eight-attempt bound. Recovery does not
grant consent or call AI inline; the worker rechecks before inference/publication.

### Explicit text-only email gateway

The dashboard integrates `execution.request_email/email_snapshot/dispatch_email`.
After startup migrations, `main()` starts
`jobs.Worker(execution.KIND, execution.execute_approved)`. No worker is started by
a dashboard request; a queued dispatch still does not prove worker liveness or
successful sending.

1. **Prepara invio**: choose an existing active/waiting practice and its displayed
   version, one bare recipient address, an explicit configured `account_address`,
   exact text and expiry with timezone. Runtime drafts are shown literally and
   can populate text only; they cannot infer sender/recipient or authorize sends.
2. **Approva**: `secretary.resolve_approval` checks the exact displayed payload,
   current version and stable key. It never dispatches, enqueues or sends email.
   The response includes `operation_key`; the UI retains it as
   `approval_operation_key` in the DOM across manual snapshot refreshes, never
   browser storage. Logout/full reload clears it; absent receipt disables send.
3. **Invio esplicito**: a separate owner click posts the exact sender, subject,
   payload, approved version, approval receipt key and stable dispatch key.
   The gateway commits an identifier-only job; worker validation rechecks native
   authority, versions, expiry, SMTP binding, receipt, document bytes and lease.

Subject is fixed to **Messaggio approvato** and displayed before approval/send.
The connector supports **text only, no attachments**. Nonempty approved document
lists are displayed and explicitly rejected, never silently dropped. No default
account, arbitrary SMTP/URL/path payload, remote draft or generic model tool exists
in this gateway. Threaded replies/Message-ID metadata are not supported.
One committed attempt precedes SMTP; timeout/crash/false is uncertain, never
automatically resent. `accepted` means connector accepted, not delivered/read.
Revocation cannot recall SMTP already in flight. Never blindly replay a send.

Authenticated APIs: POST `/api/secretary/emails` (request_email argument fields),
GET `/api/secretary/emails/<id>`, POST `/api/secretary/emails/<id>/dispatch`
(`expected_version,account_address,subject,payload,approval_operation_key,operation_key`).
Mutations require same-origin and CSRF; request/dispatch bodies are capped at
49152 bytes. Unknown/duplicate keys, nonfinite JSON, header/list recipients,
spoofed owner flags and invalid bounds fail before writes. Owner identity is
constructed only by the backend. GET `/api/secretary/execution` shows at most 24
sealed proposals/outcomes and 50 sender addresses, never SMTP secrets/job tokens.

Upgrade checklist: back up DB plus private config/media first; test the
additive migrations on a new clone with external I/O disabled. Preserve explicit
legacy flags, including false. Fresh/unknown profiles pause optional invoice/ZIP
flows; do not rewrite existing responsibilities, backfill them from old tasks or
auto-allow AI. Historical invoices/ZIP remain accessible in the collapsed archive
without re-enabling processing. Review ambiguous old effects before restart.

Configuration: global observe/active mode and daily monetary budget remain
planned, not enforced; `SECRETARY_MODE` has no runtime consumer and stays read-only.
Review-only quiet hours are implemented by secretary_runtime via
SECRETARY_REVIEW_QUIET_START/END, default 22/8 local; they do not silence all
notifications or stop AI spending. The support endpoint distinguishes this from
global quiet hours.

### Real Worker AI caps (not a money budget)

`ai_budget` enforces `AI_BUDGET_DAILY_CALLS=200`,
`AI_BUDGET_DAILY_TOKENS=500000`, `AI_BUDGET_MAX_INFLIGHT=2` and
`AI_BUDGET_MAX_OUTPUT_TOKENS=65536` inside jobs execution contexts. Every HTTP
attempt/retry consumes one call and serialized-input-byte + maximum-output +
overhead token reservation, without refunds, on a UTC day. Higher provider usage
is charged too; this cannot predict exact multimodal tokenization or prices.
Direct/local legacy calls and periodic digest generation outside a Worker are
explicitly outside persistent enforcement. Eight job workers do not mean eight
simultaneous accounted AI calls: the default shared in-flight cap is two.
Workers fail closed if DB/ledger/fence is unavailable. Crash-reserved slots are
not automatically released: reconcile only after establishing no request is live.
GET `/api/secretary/budget` exposes aggregate counters and finite limits only;
reported cost NULL means unknown, not free. No prompts, bodies, secrets or lease
IDs are returned. Configure monetary limits separately at OpenRouter.
Web-editable caps are positive integers up to 2147483647 (output 65536), applied
only after restart; inherited deployment values remain read-only.

## Validation and CI isolation

`.github/workflows/docker.yml` installs requirements and runs full offline
`unittest discover` with `env -i`, no deployment dotenv, a temporary data directory
and no inherited provider/DB credentials. A separate job repeats discovery against
a disposable PostgreSQL service with an explicit `secretary_test` database and
random test schemas. Existing auto-PG tests may create their own Unix-socket-only
temporary clusters. Neither job starts main.py or connects to production.
CI explicitly runs dashboard/execution/budget contracts before full discovery;
the isolated PostgreSQL full-discovery job also discovers their SQL tests.
These are workflow definitions, not a claim of current green CI. Full-validation
results are supplied separately by the parent run; this documentation update
runs no tests/terminal commands and reads no user data.

`test_secretary_dashboard.py` covers login/CSRF/origin/Host, bounds/validation,
missing support, redaction, consent defaults/CAS, exact approval API invocation,
restrictive stop/invalidation, UI text rendering and packaging. Its real SQL
class is opt-in via PI_SECRETARY_TEST_DATABASE_URL, insists on an explicit `_test`
dbname without libpq service, uses a random schema and never falls back to
DATABASE_URL. It tests actual payload mismatch/retry, consent CAS, stop and
rollback on audit failure, then actual email request/approval/dispatch receipts,
tamper/revocation/document rejection and mocked connector acceptance. No real
provider calls occur. Do not set the test URL to any production cluster.

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
run `python migrate_email_attachments.py --apply --limit 100` using the application
environment. Omit `--limit` for all email blobs. Without `--apply`, no database
connection is made. Each row is locked, bytes/SHA256 verified on disk, then path
and `data=NULL` updated in one transaction. It is resumable; failures preserve
the current blob and previously committed rows remain migrated. No VACUUM or
blob/schema deletion is performed. Non-email attachments are unchanged.

# Durable processing and operation

## Architecture

`pipeline.ingest` commits native message + analysis job in one transaction.
Email UID seen flags and WhatsApp spool acknowledgement occur only after that
commit. The analysis path uses native policy gates and durable local/outbox
commits; indexing and bounded secretary observation/review have separate workers.
An independent action worker executes effects outside transactions. A serialized
owner worker preserves dialogue history without waiting for email or analysis;
email execution has its own worker. Job payloads remain in PostgreSQL, not an
unbounded RAM executor queue. Connections are thread-local/short-lived with
connection, statement and lock timeouts.

Jobs use SKIP LOCKED, token fencing, 15-minute crash leases, heartbeat every
30 seconds and a one-hour execution deadline. Ordinary failures retry with
exponential backoff (15s to 1h), maximum eight attempts, then visible `failed`.
Deferred paused work does not consume attempts; consent-blocked media remains
terminal until eligible explicit recovery. A lease cannot recall in-flight I/O.
No automatic deletion of jobs/messages/dedup identities is performed. Plan storage capacity;
retention policy must archive source files and referenced rows together.
Do not reset job rows as a generic recovery procedure. Review native authority,
leases and effect receipts first; resetting a job does not renew an authorization
or make an attempted/uncertain send safe. Use the bounded media recovery API for
eligible consent blocks, not bulk SQL resets.

Integrated pipeline notifications/owner replies/ZIP sends and the email gateway
commit an attempt before remote I/O. Attempted/uncertain non-idempotent effects
stop for reconciliation; retries do not blindly resend after timeout, lost reply
or a crash. Acknowledged acceptance is not proof of delivery/read, and no global
exactly-once guarantee covers legacy/direct paths. Deterministic Calendar IDs and
content-addressed local writes allow narrower safe reconciliation/retry.
`notified=true` follows acknowledged Telegram acceptance. Digest generation is
periodic, with a persistent day cursor and outbox identity, not a durable
AI-generation job. Dashboard metrics report queue states, oldest age, completion,
acknowledged notifications, tasks and pending/proposed Calendar entries.

## Calendar and tasks

External events default to `proposal`. `/appuntamenti` displays local IDs and sync
state alongside remote events, deduplicated by Google event ID. `/conferma ID`
requests a sealed create grant; `/annulla ID` requests a separate cancel grant.
Only current direct native owner text can grant, including an explicit natural
create intent; task creation/deadlines and model flags cannot. Invalid/unconfigured
seals leave a local proposal. Owner dialogue distinguishes local save/pending
from remote success. SHA256 keys produce deterministic Google IDs; create 409
is reconciled by GET against expected fields.

`secretary_effect_grants_v3` binds event payload/state and current native owner
text to the explicit configured Calendar ID, timezone, and normalized create
start/end/duration/reminder values. Execution rechecks target/configuration and
expiry before I/O and uses the sealed target. Expired grants or legacy grants
with NULL target fail closed; replay cannot extend expiry or bind an old command
to today's destination. Review and obtain fresh owner authorization rather than
backfilling a target. Credentials/configuration presence alone is not permission.

The earlier `CALENDAR_EXTERNAL_AUTO` / `CALENDAR_TRUSTED_SENDERS` sender-label
auto-create policy is **not active** in the integrated path. Task-derived events,
including deadlines of owner tasks where a proposal exists, never inherit an
automatic Calendar grant. Owner commitments use their own reminder schedule;
the digest's legacy deadline planner creates local proposals, not remote writes.
Updating existing events, availability planning and ongoing Calendar drift repair
are **not implemented**: cancel/create requires fresh explicit authorization.
Legacy events are not automatically backfilled/reconciled. Source text is evidence,
not authority; role separation alone is not a complete prompt-injection defense.

`/task titolo | YYYY-MM-DDTHH:MM+02:00` creates an owner task (deadline optional).
`/attivita` lists open tasks with source message ID and optional deadline;
`/fatto ID` completes one. Owner date-only deadlines stay date-only (default
reminder 09:00 local); timed deadlines default to a reminder at the deadline,
unless an explicit reminder overrides it. Legacy external-task reminders use a
persistent within-24h-of-deadline identity, including overdue tasks. No arbitrary
deadline/time or Calendar intent is inferred. Automatic spam/newsletter mailbox
mark-as-read exists only in the private legacy synchronous classifier, not
`pipeline.ingest` used by current runtime ingestion.

## Remaining operational scope

The integrated boundaries above do not finish the architecture roadmap. Remaining:
arbitrary workflows/dependency execution; typed-memory conflict handling and full
legacy task/memory model-input provenance; general existing-evidence practice
linking from retrieval; executable stop-condition rules; Calendar update,
availability and remote drift handling; email attachments/threaded sends;
global monetary cap/mode/global quiet hours; coordinated privacy erase/export;
and a full restored-backup production drill plus semantic/quality evaluation.
Bounded link acceptance, CAS, origin/applied-evidence checks and sealed effects
must not be described as those broader guarantees.

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

Use a dedicated Python 3.11+ virtualenv; install requirements.txt, then freeze the
resolved platform-specific versions into a reviewed deployment lock artifact.
The repository currently specifies compatible ranges, not a fully reproducible
cross-platform lock. WhatsApp requires Node + Chromium and `npm ci` using the
reviewed data/package-lock.json. Do not download x86 Chromium on ARM.

`deploy/pi-secretary.service` is a template; adjust user/paths before installing.
Keep secrets/session files private (0700 directories, 0600 files). Do not expose
the HTTP dashboard on the Internet: bind loopback:8080, terminate HTTPS using
Caddy/nginx with a valid certificate, set DASHBOARD_COOKIE_SECURE=true and
DASHBOARD_HOSTNAME to the public hostname. Configure proxy headers deliberately;
do not trust arbitrary X-Forwarded-* headers. Existing port-80 overrides must be
removed/reviewed separately. Run only one scheduler/owner instance per deployment.

Startup applies additive native/jobs/runtime/secretary/execution/budget migrations
under the schema advisory lock, including `durable_work_v2` and its runtime
upgrade. The legacy native migration refuses an unapproved legacy history
conversion; resets are rejected by db.init. Back up and test migration on a clone
first. No production migration/restart or user-data access is part of this
documentation update.

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
Opt-in PostgreSQL classes require PI_SECRETARY_TEST_DATABASE_URL naming a
disposable `_test` database and random test schemas, never DATABASE_URL fallback.
Some existing auto-PG classes instead create their own temporary local clusters;
absence of a test URL does not mean all SQL tests skip. Current full-validation
results belong to the parent run, not this documentation-only change.