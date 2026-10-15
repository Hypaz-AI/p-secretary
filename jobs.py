"""PostgreSQL durable work. Connections are short-lived and never shared by threads.

Leases exceed bounded HTTP timeouts. A crashed worker is retried; external sends
are at-least-once, not exactly-once (a lost Telegram response is ambiguous).
"""
import json
import hashlib
import logging
import threading
import uuid

import db

VERSION = 'durable_work_v2'
logger = logging.getLogger(__name__)


class Deferred(RuntimeError):
    """Paused/configuration-blocked work does not consume retry attempts."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_jobs (
 id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL UNIQUE,
 payload JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 lease_until TIMESTAMPTZ, token TEXT, error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 finished_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS work_ready ON work_jobs(kind,available_at) WHERE status IN ('pending','running');
CREATE TABLE IF NOT EXISTS source_cursors (
 key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS tasks (
 id BIGSERIAL PRIMARY KEY, source_message BIGINT, title TEXT NOT NULL,
 deadline TIMESTAMPTZ, status TEXT NOT NULL DEFAULT 'open',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ,
 UNIQUE(source_message));
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS sync_key TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS event_id TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS sync_state TEXT NOT NULL DEFAULT 'local';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS event_data JSONB;
CREATE UNIQUE INDEX IF NOT EXISTS appointment_sync_key ON appointments(sync_key);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS content_hash TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS invoice_content_hash ON invoices(content_hash);
"""


def migrate(conn):
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return
        conn.execute(SCHEMA)
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))


def enqueue(conn, kind, key, payload):
    return conn.execute('''INSERT INTO work_jobs(kind,key,payload) VALUES (%s,%s,%s)
        ON CONFLICT(key) DO NOTHING RETURNING id''', (kind, key, json.dumps(payload))).fetchone()


def submit(kind, key, payload):
    with db._conn() as conn:
        return enqueue(conn, kind, key, payload)


def cursor(key, default=None):
    with db._conn() as conn:
        row = conn.execute('SELECT value FROM source_cursors WHERE key=%s', (key,)).fetchone()
        return row[0] if row else default


def set_cursor(key, value):
    with db._conn() as conn:
        conn.execute('''INSERT INTO source_cursors(key,value) VALUES (%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()''', (key, json.dumps(value)))


def claim(kind):
    token = uuid.uuid4().hex
    with db._conn() as conn:
        row = conn.execute('''UPDATE work_jobs SET status='running',token=%s,
            attempts=attempts+1,lease_until=now()+interval '15 minutes'
            WHERE id=(SELECT id FROM work_jobs WHERE kind=%s AND attempts<8 AND
            ((status='pending' AND available_at<=now()) OR (status='running' AND lease_until<now()))
            ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id,payload,attempts''', (token, kind)).fetchone()
        # An exhausted crashed lease must not remain invisibly running forever.
        conn.execute("UPDATE work_jobs SET status='failed',error='lease exhausted' WHERE status='running' AND lease_until<now() AND attempts>=8")
        return (row[0], row[1], row[2], token) if row else None


def finish(job, error=None):
    identity, _, attempts, token = job
    with db._conn() as conn:
        conn.execute('''UPDATE work_jobs SET status=%s,error=%s,token=NULL,lease_until=NULL,
            finished_at=CASE WHEN %s THEN now() ELSE NULL END,
            available_at=now()+(%s * interval '1 second') WHERE id=%s AND token=%s''',
            ('done' if error is None else 'failed' if attempts >= 8 else 'pending',
             error, error is None, min(3600, 15 * 2 ** (attempts-1)), identity, token))


def defer(job):
    with db._conn() as conn:
        conn.execute("""UPDATE work_jobs SET status='pending',attempts=GREATEST(0,attempts-1),
            token=NULL,lease_until=NULL,available_at=now()+interval '60 seconds'
            WHERE id=%s AND token=%s""", (job[0],job[3]))


def event_id(key):
    # Hex is a subset of Google's permitted base32hex alphabet.
    return hashlib.sha256(key.encode()).hexdigest()


class Worker:
    def __init__(self, kind, handler):
        self.stop_event = threading.Event()
        self.kind, self.handler = kind, handler
        self.thread = threading.Thread(target=self.run, name='jobs-'+kind, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def run(self):
        while not self.stop_event.is_set():
            job = None
            try:
                job = claim(self.kind)
                if job:
                    self.handler(job[1])
                    finish(job)
                    continue
            except Exception as exc:
                if job:
                    try:
                        if isinstance(exc, Deferred):
                            defer(job)
                        else:
                            logger.exception('Work job %s (%s) failed', job[0], self.kind)
                            finish(job, f'{type(exc).__name__}: {exc}'[:2000])
                    except Exception:
                        pass  # Lease expiry recovers database outages.
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()


def metrics():
    with db._conn() as conn:
        rows = conn.execute('''SELECT kind,status,count(*),
            EXTRACT(EPOCH FROM now()-min(created_at)),max(finished_at)
            FROM work_jobs GROUP BY kind,status ORDER BY kind,status''').fetchall()
        return [dict(zip(('kind','status','count','oldest_age_seconds','last_success'), r)) for r in rows]