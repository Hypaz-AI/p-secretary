# Durable processing and operation

## Architecture

`pipeline.ingest` commits native message + analysis job in one transaction.
Email UID seen flags and WhatsApp spool acknowledgement occur only after that
commit. One analysis worker commits the verdict, actionable task, Calendar
proposal and outbox jobs together. An independent action worker executes effects
outside transactions. A separate serialized owner worker preserves dialogue
history without waiting for email or analysis. Each periodic source has at most
one background thread; job payloads remain in PostgreSQL, not an unbounded RAM
executor queue. All DB connections are thread-local/short-lived with connection,
statement and lock timeouts.

Jobs use SKIP LOCKED, token fencing, 15-minute crash leases, exponential retry
(15s to 1h), maximum eight attempts, then visible `failed` state. No automatic
deletion of jobs/messages/dedup identities is performed. Plan storage capacity;
retention policy must archive source files and referenced rows together.
An operator can requeue a failed job by setting status=pending, attempts=0,
available_at=now(), token=NULL, lease_until=NULL after reviewing its effect.
Do not blindly replay ambiguous sends.

Notifications become `notified=true` only after Telegram acknowledges success.
Telegram cannot guarantee exactly-once: success followed by lost response or a
crash before the DB commit can cause a duplicate on retry. Owner replies likewise
can repeat; owner event keys are stable per input. Digest generation is periodic,
with a persistent day cursor and idempotent outbox key, not a durable AI-generation
job. The dashboard reports actual queue states, oldest job age, last completion,
confirmed notifications, open tasks and pending/proposed Calendar entries.

## Calendar and tasks

External events default to `proposal`. `/appuntamenti` displays local IDs and sync
state alongside remote events, deduplicated by Google event ID. `/conferma ID`
queues a proposal; `/annulla ID` queues cancellation. Owner dialogue saves locally
and explicitly says Calendar is pending, never claims remote success in advance.
SHA256 keys produce valid deterministic Google IDs; 409 is reconciled by GET.
Google IDs/sync state/event data persist. Existing legacy events are not reconciled
or retroactively assigned IDs. Updating existing events through natural language
is **not implemented**: cancel and create a new event instead.

`CALENDAR_EXTERNAL_AUTO=true` plus comma-separated `CALENDAR_TRUSTED_SENDERS`
allows exact sender labels to auto-create. Sender labels can be spoofed by email;
this is a convenience allowlist, NOT cryptographic sender authentication. Leave
disabled for safety. Archived content stays outside the system prompt, but prompt
injection cannot be completely eliminated by role separation alone.

`/task titolo | YYYY-MM-DDTHH:MM+02:00` creates an owner task (deadline optional).
`/attivita` lists open tasks with source message ID and optional deadline;
`/fatto ID` completes one. AI must explicitly supply action_deadline; absent or
invalid dates remain unset. A single persistent reminder is queued within 24h of
deadline (including overdue tasks). No arbitrary deadline/time is invented.

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

Startup applies additive `durable_work_v2` under the schema advisory lock. The
legacy native migration refuses an unapproved legacy history conversion; resets
are now rejected by db.init. Back up and test migration on a clone first.
No production migration/restart was executed during this implementation.

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
   workers. Google deterministic IDs reconcile creation; Telegram sends may
   duplicate. Verify Calendar target identity before enabling sync.
5. Switch the service only after verification; retain the previous database and
   filesystem snapshot for rollback. Do not roll back only the DB while retaining
   a newer spool/media tree without reconciliation.

Offline validation: run unittest discovery and compileall in the virtualenv.
PostgreSQL tests are skipped unless PI_SECRETARY_TEST_DATABASE_URL is explicitly
set to a disposable database whose name ends in `_test`; they create/remove only
a random test schema. They never fall back to DATABASE_URL.