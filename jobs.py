"""PostgreSQL durable work. Connections are short-lived and never shared by threads.

Leases exceed bounded HTTP timeouts. A crashed worker is retried; external sends
are at-least-once, not exactly-once (a lost Telegram response is ambiguous).
"""
import json
import hashlib
import builtins
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

import db

VERSION = 'durable_work_v2'
RUNTIME_VERSION = 'durable_work_runtime_v3'
logger = logging.getLogger(__name__)
_local = threading.local()
HEARTBEAT_SECONDS = 30
MAX_EXECUTION_SECONDS = 3600


class Deferred(RuntimeError):
    """Paused/configuration-blocked work does not consume retry attempts."""


class LeaseLost(RuntimeError):
    """Execution must stop: token expired, superseded, stopped or deadline passed."""


@dataclass
class ExecutionContext:
    job_id: int
    token: str
    kind: str
    payload: dict
    deadline: float
    lost: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)

    @property
    def message_id(self):
        value = self.payload.get('message_id')
        return value if type(value) is int and value > 0 else None


def execution_context():
    """Current thread's job context, or None for legacy/internal synchronous calls."""
    return getattr(_local, 'execution', None)


def assert_current(conn=None):
    """Recheck immediately before every effect/commit. No-op outside a Worker.

    A DB fence cannot undo a remote request already in flight. Never share a
    connection with the heartbeat thread, and never hold it across network I/O.
    """
    current = execution_context()
    if current is None:
        return True
    if current.lost.is_set() or current.stopped.is_set() or time.monotonic() >= current.deadline:
        current.lost.set()
        raise LeaseLost('Execution no longer current')
    if conn is None:
        with db._conn() as connection:
            return assert_current(connection)
    row = conn.execute("""SELECT id FROM work_jobs WHERE id=%s AND token=%s
        AND status='running' AND lease_until>now() AND lease_until>clock_timestamp()""",
        (current.job_id, current.token)).fetchone()
    if not row:
        current.lost.set()
        raise LeaseLost('Execution no longer current')
    return True


def renew(job):
    """Renew only an unexpired lease with the same token, on a fresh connection."""
    with db._conn() as conn:
        return bool(conn.execute("""UPDATE work_jobs SET lease_until=now()+interval '15 minutes'
            WHERE id=%s AND token=%s AND status='running' AND lease_until>now()
            RETURNING id""", (job[0], job[3])).fetchone())


def safe_error(error):
    """Persist/log a class code only, never arbitrary exception text or URLs."""
    code = type(error).__name__ if isinstance(error, BaseException) else str(error).split(':', 1)[0]
    known = getattr(builtins, code, None)
    return code if (isinstance(known, type) and issubclass(known, BaseException)
                    or code in ('Deferred', 'LeaseLost')) else 'JobError'

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
        if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            conn.execute(SCHEMA)
            conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))
        if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (RUNTIME_VERSION,)).fetchone():
            conn.execute("""ALTER TABLE work_jobs ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0;
                UPDATE work_jobs SET priority=CASE
                    WHEN kind='owner' THEN 100
                    WHEN kind='action' AND (payload ? 'owner_reply_mid' OR payload ? 'owner_reminder') THEN 90
                    WHEN kind='action' THEN 40 WHEN kind='analysis' THEN 20
                    WHEN kind='reconcile' THEN 10 ELSE 0 END
                WHERE status IN ('pending','running') AND priority=0;
                CREATE INDEX IF NOT EXISTS work_priority_ready ON work_jobs(kind,priority DESC,available_at,id)
                    WHERE status IN ('pending','running');""")
            conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (RUNTIME_VERSION,))


def enqueue(conn, kind, key, payload, *, priority=None):
    if priority is None:
        priority = (100 if kind == 'owner' else 90 if kind == 'action' and
                    ('owner_reply_mid' in payload or 'owner_reminder' in payload) else
                    {'action': 40, 'analysis': 20, 'reconcile': 10}.get(kind, 0))
    if type(priority) is not int or not -1000 <= priority <= 1000:
        raise ValueError('Invalid job priority')
    return conn.execute('''INSERT INTO work_jobs(kind,key,payload,priority) VALUES (%s,%s,%s,%s)
        ON CONFLICT(key) DO NOTHING RETURNING id''', (kind, key, json.dumps(payload), priority)).fetchone()


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
            ORDER BY priority DESC,available_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id,payload,attempts''', (token, kind)).fetchone()
        # An exhausted crashed lease must not remain invisibly running forever.
        conn.execute("UPDATE work_jobs SET status='failed',error='lease exhausted' WHERE status='running' AND lease_until<now() AND attempts>=8")
        return (row[0], row[1], row[2], token) if row else None


def finish(job, error=None):
    identity, _, attempts, token = job
    with db._conn() as conn:
        conn.execute('''UPDATE work_jobs SET status=%s,error=%s,token=NULL,lease_until=NULL,
            finished_at=CASE WHEN %s THEN now() ELSE NULL END,
            available_at=now()+(%s * interval '1 second') WHERE id=%s AND token=%s
            AND status='running' AND lease_until>now()''',
            ('done' if error is None else 'failed' if attempts >= 8 else 'pending',
             safe_error(error) if error is not None else None, error is None,
             min(3600, 15 * 2 ** (attempts-1)), identity, token))


def defer(job):
    with db._conn() as conn:
        conn.execute("""UPDATE work_jobs SET status='pending',attempts=GREATEST(0,attempts-1),
            token=NULL,lease_until=NULL,available_at=now()+interval '60 seconds'
            WHERE id=%s AND token=%s AND status='running' AND lease_until>now()""", (job[0],job[3]))


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

    @contextmanager
    def _execution(self, job):
        previous = execution_context()
        current = ExecutionContext(job[0], job[3], self.kind, job[1],
                                   time.monotonic() + MAX_EXECUTION_SECONDS)
        current.stopped = self.stop_event
        done = threading.Event()

        def heartbeat():
            while not done.wait(HEARTBEAT_SECONDS):
                if self.stop_event.is_set() or time.monotonic() >= current.deadline:
                    current.lost.set()
                    return
                try:
                    if renew(job):
                        continue
                except Exception:
                    pass
                current.lost.set()  # DB uncertainty also fails closed.
                return

        thread = threading.Thread(target=heartbeat, name='lease-' + str(job[0]), daemon=True)
        _local.execution = current
        thread.start()
        try:
            yield current
        finally:
            done.set()
            _local.execution = previous
            # A stalled DB request must not hold up handler cleanup indefinitely.
            thread.join(timeout=1)

    def run(self):
        while not self.stop_event.is_set():
            job = None
            try:
                job = claim(self.kind)
                if job:
                    with self._execution(job):
                        assert_current()
                        self.handler(job[1])
                        assert_current()
                        finish(job)
                    continue
            except Exception as exc:
                if job:
                    try:
                        if isinstance(exc, LeaseLost):
                            pass  # Never finalize a superseded execution.
                        elif isinstance(exc, Deferred):
                            defer(job)
                        else:
                            logger.warning('Work job %s failed (%s)', job[0], safe_error(exc))
                            finish(job, safe_error(exc))
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