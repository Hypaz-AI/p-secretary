"""Persistenza su Postgres: comunicazioni, appuntamenti, note, fatture, regole.

Tutto il 'contesto' del segretario vive qui: ogni comunicazione analizzata,
ogni regola che l'utente insegna, ogni fattura salvata. Lo schema viene
creato automaticamente al primo avvio (CREATE TABLE IF NOT EXISTS).
"""
from datetime import datetime

from secretary.core import config
from secretary.archive import message_store

try:
    import psycopg
except ImportError:  # il servizio parte comunque, solo senza memoria
    psycopg = None


# ---------------------------------------------------------------- schema ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    dt          TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM'
    location    TEXT,
    notes       TEXT,
    source      TEXT,                   -- comunicazione di origine
    done        BOOLEAN NOT NULL DEFAULT FALSE,
    created     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notes (
    id          BIGSERIAL PRIMARY KEY,
    text        TEXT NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS email_seen (
    id          BIGSERIAL PRIMARY KEY,
    account     TEXT NOT NULL,          -- indirizzo della casella
    uid         TEXT NOT NULL,          -- UID IMAP del messaggio
    seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account, uid)
);

CREATE TABLE IF NOT EXISTS invoices (
    id          BIGSERIAL PRIMARY KEY,
    entity      TEXT NOT NULL,          -- 'hypaz' | 'alessio' | ...
    month       TEXT NOT NULL,          -- 'YYYY-MM'
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL,
    sender      TEXT,
    subject     TEXT,
    source      TEXT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rules (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    pattern     TEXT NOT NULL,          -- es. 'mittente contiene amazon'
    instruction TEXT NOT NULL,          -- cosa fare quando matcha
    taught_by   TEXT NOT NULL DEFAULT 'utente'
);

CREATE TABLE IF NOT EXISTS monthly_zips (
    id          BIGSERIAL PRIMARY KEY,
    entity      TEXT NOT NULL,
    month       TEXT NOT NULL,          -- 'YYYY-MM'
    path        TEXT NOT NULL,
    sent        BOOLEAN NOT NULL DEFAULT FALSE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity, month)
);

CREATE INDEX IF NOT EXISTS idx_appt_dt        ON appointments (dt);
CREATE INDEX IF NOT EXISTS idx_inv_entity    ON invoices (entity, month);
"""


def available() -> bool:
    return psycopg is not None and bool(config.DATABASE_URL)


def _conn():
    # Transaction-pooling endpoints cannot safely reuse named prepared statements.
    return psycopg.connect(config.DATABASE_URL, autocommit=True, prepare_threshold=None,
                          connect_timeout=10, options='-c statement_timeout=15000 -c lock_timeout=5000')


def init(approved_reset: bool = False) -> int:
    """Crea lo schema se non esiste. Errore fatale se il DB non è raggiungibile."""
    if not available():
        raise RuntimeError(
            "DATABASE_URL non impostato o psycopg non installato "
            "(pip install 'psycopg[binary]')")
    with _conn() as conn:
        read_only, default_read_only, recovery = conn.execute(
            "SELECT current_setting('transaction_read_only'), "
            "current_setting('default_transaction_read_only'), pg_is_in_recovery()"
        ).fetchone()
        if read_only == 'on' or default_read_only == 'on' or recovery:
            raise RuntimeError(
                "PostgreSQL raggiungibile ma in sola lettura: "
                f"transaction_read_only={read_only}, "
                f"default_transaction_read_only={default_read_only}, "
                f"pg_is_in_recovery={recovery}. "
                "Il servizio richiede scritture; verificare endpoint e impostazioni "
                "del database. Nessuna protezione read-only è stata disabilitata.")
        if approved_reset:
            raise RuntimeError('Destructive history resets are no longer supported')
        with conn.transaction():
            # Capture installation provenance before the base migration creates
            # its receipt table. Serialize concurrent first starts as well.
            conn.execute('SELECT pg_advisory_xact_lock(73190421)')
            legacy = bool(conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0])
            result = message_store.migrate(conn, SCHEMA, False)
            from secretary.core import jobs
            jobs.migrate(conn)
            from secretary.workflows import task_reconciliation
            task_reconciliation.migrate(conn)
            from secretary.connectors import media_archive
            media_archive.migrate(conn)
            from secretary.agent import owner as agent_owner
            agent_owner.migrate(conn)
            from secretary.core import capabilities
            capabilities.migrate(conn, legacy=legacy)
            from secretary.core import policy
            policy.migrate(conn)
            from secretary.archive import knowledge
            knowledge.migrate(conn)
            from secretary.core import privacy
            privacy.migrate(conn)
            migrate_notes(conn)
            migrate_typed_notes(conn)
            from secretary.core import ai_budget
            ai_budget.migrate(conn)
            from secretary.agent import schema as agent_schema
            agent_schema.migrate(conn, config.AGENT_DB_SCHEMA)
            # Ruolo di sola lettura dell'agente: creato qui se l'utente applicativo può
            # (DB locale del compose), altrimenti lo segnala main() e lo crea il DBA.
            agent_schema.ensure_readonly_role(conn, config.AGENT_DB_SCHEMA, config.AGENT_DB_ROLE)
            return result


# ------------------------------------------------------- comunicazioni ----
def store_message(channel, sender, subject, body, ts=None, source_id=None,
                  metadata=None, attachments=None, update_existing=False, historical=False):
    with _conn() as conn:
        return message_store.store(conn, channel, sender, subject, body, ts,
                                   source_id, metadata, attachments, update_existing,
                                   **({'historical': True} if historical else {}))


def log_communication(channel, sender, subject, body, verdict, notified,
                      ts=None, source_id=None, metadata=None, attachments=None,
                      message_id=None):
    with _conn() as conn:
        mid = message_id or message_store.store(conn, channel, sender, subject,
                                               body, ts, source_id, metadata, attachments)
        return message_store.analyze(conn, mid, channel, verdict, notified, source_id)


def communication_seen(channel: str, source_id: str) -> bool:
    with _conn() as conn:
        return conn.execute(
            "SELECT 1 FROM message_dedup WHERE channel=%s AND source_id=%s",
            (channel, source_id)).fetchone() is not None


# ------------------------------------------------------- email già lette ----
def email_seen_uids(account: str) -> set:
    """Insieme degli UID IMAP già letti per una casella."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT uid FROM email_seen WHERE account = %(a)s",
            {"a": account.lower()}).fetchall()
        return {r[0] for r in rows}


def mark_email_seen(account: str, uid: str) -> None:
    """Registra un UID come già letto (idempotente)."""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO email_seen (account, uid) VALUES (%(a)s, %(u)s)
               ON CONFLICT (account, uid) DO NOTHING""",
            {"a": account.lower(), "u": str(uid)})


def prune_email_seen(keep: int = 2000) -> None:
    """Tieni solo gli ultimi `keep` UID per casella (pulizia periodica)."""
    return  # Stable dedup IDs must survive history resets indefinitely.


def communications_for_day(day=None) -> list:
    """Tutte le comunicazioni di un giorno (default: oggi), ordinate per ora."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, ts, channel, sender, subject,
                                            concat_ws(E'\n\n', COALESCE(summary, LEFT(body, 2000)),
                                                (SELECT string_agg('[' || t.kind || '] ' || t.text, E'\n\n' ORDER BY a.id)
                                                 FROM attachments a JOIN media_transcripts t ON t.attachment_id=a.id
                                                 WHERE a.message_id=communications.id)) AS summary, category,
                      priority, needs_action, action, direction
               FROM communications
               WHERE day = COALESCE(%(day)s, (now() AT TIME ZONE %(tz)s)::date)
               ORDER BY ts""",
            {"day": day, "tz": config.TIMEZONE}).fetchall()
        cols = ["id", "ts", "channel", "sender", "subject", "summary",
                "category", "priority", "needs_action", "action", "direction"]
        return [dict(zip(cols, r)) for r in rows]

def communications_since(hours: int = 24) -> list:
    """Tutte le comunicazioni delle ultime `hours` ore (finestra mobile).

    Usata dal digest: l'invio non è legato al giorno di calendario ma alle
    ultime 24 ore esatte, così nessun messaggio resta fuori. Il confronto
    `ts >= now() - interval` lavora su istanti assoluti (timestamptz),
    quindi è corretto a prescindere dal fuso orario della sessione.
    """
    with _conn() as conn:
        rows = conn.execute(
            """SELECT ts, channel, sender, subject,
                                            concat_ws(E'\n\n', COALESCE(summary, LEFT(body, 2000)),
                                                (SELECT string_agg('[' || t.kind || '] ' || t.text, E'\n\n' ORDER BY a.id)
                                                 FROM attachments a JOIN media_transcripts t ON t.attachment_id=a.id
                                                 WHERE a.message_id=communications.id)) AS summary, category,
                      priority, needs_action, action, direction
               FROM communications
               WHERE ts >= now() - %(hours)s * interval '1 hour'
               ORDER BY ts""",
            {"hours": int(hours)}).fetchall()
        cols = ["ts", "channel", "sender", "subject", "summary", "category",
                "priority", "needs_action", "action", "direction"]
        return [dict(zip(cols, r)) for r in rows]

# ------------------------------------------------------------ appuntamenti ----
def pending_tasks(until: datetime) -> list:
    """Attività aperte: arretrate, entro l'orizzonte e senza scadenza."""
    from zoneinfo import ZoneInfo
    local_until = until.astimezone(ZoneInfo(config.TIMEZONE))
    with _conn() as conn:
        rows = conn.execute(
                 """WITH horizon AS (SELECT %s::timestamptz AS until)
                 SELECT id, title, deadline, source_message, owner_deadline_date, owner_remind_at,
                     owner_origin_message, owner_operation_key, version FROM tasks CROSS JOIN horizon
                 WHERE status='open' AND (deadline IS NULL OR deadline <= horizon.until
                                OR owner_remind_at <= horizon.until)
               ORDER BY deadline NULLS LAST, id""", (until,)).fetchall()
        tasks = [dict(zip(('id', 'title', 'deadline', 'source_message', 'owner_deadline_date',
                           'owner_remind_at', 'owner_origin_message', 'owner_operation_key', 'version'), row))
                 for row in rows]
        # DATE horizons use the configured local date, never the DB session zone.
        tasks = [t for t in tasks if t.get('owner_deadline_date') is None
                 or t['owner_deadline_date'] <= local_until.date()
                 or (t.get('owner_remind_at') is not None and t['owner_remind_at'] <= until)]
        def order(task):
            if task.get('owner_deadline_date') is not None:
                return (0, datetime.combine(task['owner_deadline_date'], datetime.min.time(),
                                            local_until.tzinfo), task['id'])
            if task.get('deadline') is not None:
                return (0, task['deadline'], task['id'])
            return (1, local_until, task['id'])
        return sorted(tasks, key=order)


def appointments_between(start: datetime, end: datetime) -> list:
    """Agenda interna nell'intervallo assoluto, incluse proposte da confermare."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, title, dt, location, notes, event_id, sync_state
               FROM appointments WHERE NOT done
                 AND sync_state NOT IN ('cancelled', 'cancel_pending')
                 AND (dt::timestamp AT TIME ZONE %(tz)s) >= %(start)s
                 AND (dt::timestamp AT TIME ZONE %(tz)s) <= %(end)s
               ORDER BY dt""",
            {'tz': config.TIMEZONE, 'start': start, 'end': end}).fetchall()
        return [dict(zip(('id', 'title', 'dt', 'location', 'notes', 'event_id', 'sync_state'), row))
                for row in rows]


def add_appointment(title: str, dt: str, location: str = "", notes: str = "",
                    source: str = "") -> int:
    with _conn() as conn:
        row = conn.execute(
            """INSERT INTO appointments (title, dt, location, notes, source)
               VALUES (%(t)s, %(d)s, %(l)s, %(n)s, %(s)s)
               RETURNING id""",
            {"t": title, "d": dt, "l": location, "n": notes, "s": source}).fetchone()
        return row[0]


def upcoming_appointments(days: int = 7) -> list:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, title, dt, location, notes, event_id, sync_state
               FROM appointments
               WHERE done = FALSE
                 AND (dt::timestamp AT TIME ZONE %(tz)s) >= now()
                 AND (dt::timestamp AT TIME ZONE %(tz)s) <= now() + %(days)s * interval '1 day'
               ORDER BY dt""",
            {"days": days, "tz": config.TIMEZONE}).fetchall()
        return [dict(zip(["id", "title", "dt", "location", "notes", "event_id", "sync_state"], r)) for r in rows]


# ------------------------------------------------------------------- note ----
NOTES_VERSION = 'personal_memory_provenance_v1'
TYPED_NOTES_VERSION = 'personal_memory_typed_v1'
NOTE_TYPES = frozenset(('personal', 'preference', 'fact', 'instruction', 'context'))
MAX_TOPIC_CHARS = 120


def migrate_notes(conn):
    """Add provenance without rewriting existing notes or granting authority."""
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s',
                        (NOTES_VERSION,)).fetchone():
            return
        conn.execute('''ALTER TABLE notes ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'personal';
            ALTER TABLE notes ADD COLUMN IF NOT EXISTS source_message BIGINT;
            ALTER TABLE notes ADD COLUMN IF NOT EXISTS source_revision TEXT;
            ALTER TABLE notes ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1''')
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)',
                     (NOTES_VERSION,))


def migrate_typed_notes(conn):
    """Typed memory topics: one live note per topic, replaced on conflict.

    Additive column only; existing notes keep topic NULL and behave exactly as
    before (additive, deduplicated by text). A topic is a short normalized label
    (e.g. 'caffe', 'fatturazione') chosen by the model but VALIDATED server-side:
    lowercase, no control chars, bounded length. The topic never grants
    authority; it only keys the replace-on-conflict behaviour.
    """
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s',
                        (TYPED_NOTES_VERSION,)).fetchone():
            return
        conn.execute("ALTER TABLE notes ADD COLUMN IF NOT EXISTS topic TEXT")
        conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS notes_live_topic
            ON notes(lower(topic)) WHERE topic IS NOT NULL''')
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)',
                     (TYPED_NOTES_VERSION,))


def normalize_topic(value):
    """Server-side topic normalization; None/invalid means untyped note."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Invalid memory topic')
    topic = ' '.join(value.lower().split())
    if not topic or len(topic) > MAX_TOPIC_CHARS:
        raise ValueError('Invalid memory topic')
    return topic


def _note_provenance(kind, source_message, source_revision):
    if kind not in NOTE_TYPES:
        raise ValueError('Invalid memory kind')
    if source_message is not None and (type(source_message) is not int or source_message <= 0):
        raise ValueError('Invalid memory source')
    if source_revision is not None and (not isinstance(source_revision, str)
            or not source_revision or len(source_revision) > 200 or source_message is None):
        raise ValueError('Invalid memory source revision')


def add_note(text: str, *, kind='personal', source_message=None, source_revision=None) -> int:
    _note_provenance(kind, source_message, source_revision)
    with _conn() as conn, conn.transaction():
        row = conn.execute(
            '''INSERT INTO notes (text,kind,source_message,source_revision)
               VALUES (%s,%s,%s,%s) RETURNING id''',
            (text, kind, source_message, source_revision)).fetchone()
        return row[0]


def recent_notes(limit: int = 5) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT text FROM notes ORDER BY id DESC LIMIT %(l)s",
            {"l": limit}).fetchall()
        return [r[0] for r in rows]


def personal_memories(*, provenance=False) -> list:
    """All notes; the default id/text return contract remains unchanged."""
    with _conn() as conn:
        fields = ('id', 'text', 'kind', 'source_message', 'source_revision', 'version', 'topic') if provenance else ('id', 'text')
        rows = conn.execute(f"SELECT {', '.join(fields)} FROM notes ORDER BY id").fetchall()
        return [dict(zip(fields, row)) for row in rows]


def apply_personal_memories(changes: list, known_ids: set, conn=None, expected=None, *,
                            kind='personal', source_message=None, source_revision=None) -> int:
    """Applica note; con conn usa la transazione del chiamante, senza chiuderla.

    expected è lo snapshot id/testo (mappa o lista di note) per il CAS di update/delete.
    Senza conn possiede connessione e transazione, come nel percorso legacy.
    """
    from contextlib import ExitStack

    # Provenance is caller-supplied, never a model boolean authorizing a write.
    _note_provenance(kind, source_message, source_revision)
    topics = {}
    for change in changes:
        topic = normalize_topic(change.get('topic')) if isinstance(change, dict) else None
        if topic is not None:
            topics[id(change)] = topic
    changed = 0
    with ExitStack() as stack:
        if conn is None:
            conn = stack.enter_context(_conn())
            stack.enter_context(conn.transaction())
        conn.execute('SELECT pg_advisory_xact_lock(73190422)')
        if expected is not None:
            snapshot = (expected if isinstance(expected, dict)
                        else {m['id']: m['text'] for m in expected})
            targets = sorted({c.get('id') for c in changes
                              if c['operation'] in ('update', 'delete')
                              and c.get('id') in known_ids})
            # Controlla tutti i bersagli prima di scrivere; i lock durano fino al commit.
            for identity in targets:
                row = conn.execute('SELECT text FROM notes WHERE id=%s FOR UPDATE',
                                   (identity,)).fetchone()
                if identity not in snapshot or row is None or row[0] != snapshot[identity]:
                    raise ValueError('Memoria personale cambiata dopo la preparazione')
        for change in changes:
            operation = change['operation']
            identity = change.get('id')
            text = change.get('text', '').strip()
            topic = topics.get(id(change))
            if operation == 'add':
                if topic is not None:
                    # Typed memory: one live note per topic. A new note on an
                    # existing topic REPLACES it (version bump, provenance
                    # updated) instead of accumulating duplicates. The unique
                    # partial index enforces the invariant at SQL level.
                    row = conn.execute('''UPDATE notes SET text=%s,version=version+1,
                        kind=%s,source_message=%s,source_revision=%s
                        WHERE id=(SELECT id FROM notes WHERE lower(topic)=%s
                                  AND topic IS NOT NULL FOR UPDATE LIMIT 1)
                          AND text<>%s RETURNING id''',
                        (text, kind, source_message, source_revision, topic, text)).fetchone()
                    if row:
                        changed += 1
                        continue
                    row = conn.execute(
                        '''INSERT INTO notes(text,kind,source_message,source_revision,topic)
                           SELECT %s,%s,%s,%s,%s WHERE NOT EXISTS
                           (SELECT 1 FROM notes WHERE lower(trim(text))=lower(trim(%s)))
                           RETURNING id''',
                        (text, kind, source_message, source_revision, topic, text)).fetchone()
                    changed += bool(row)
                    continue
                row = conn.execute(
                    '''INSERT INTO notes(text,kind,source_message,source_revision)
                       SELECT %s,%s,%s,%s WHERE NOT EXISTS
                       (SELECT 1 FROM notes WHERE lower(trim(text))=lower(trim(%s)))
                       RETURNING id''', (text, kind, source_message, source_revision, text)).fetchone()
                changed += bool(row)
            elif identity in known_ids:
                if operation == 'update':
                    result = conn.execute('''UPDATE notes SET text=%s,version=version+1,
                        source_message=%s,source_revision=%s WHERE id=%s AND text<>%s''',
                        (text, source_message, source_revision, identity, text))
                else:
                    result = conn.execute('DELETE FROM notes WHERE id=%s', (identity,))
                changed += result.rowcount
    return changed


# ------------------------------------------------------------------ regole ----
def add_rule(pattern: str, instruction: str, taught_by: str = "utente") -> int:
    """Regola insegnata dall'utente: pattern + istruzione."""
    with _conn() as conn:
        row = conn.execute(
            """INSERT INTO rules (pattern, instruction, taught_by)
               VALUES (%(p)s, %(i)s, %(b)s) RETURNING id""",
            {"p": pattern, "i": instruction, "b": taught_by}).fetchone()
        return row[0]


def all_rules() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, pattern, instruction FROM rules ORDER BY id").fetchall()
        return [dict(zip(["id", "pattern", "instruction"], r)) for r in rows]


# --------------------------------------------------------------- fatture ----
def add_invoice(entity: str, month: str, filename: str, path: str,
                sender: str = "", subject: str = "", source: str = "") -> int:
    with _conn() as conn:
        row = conn.execute(
            """INSERT INTO invoices (entity, month, filename, path, sender, subject, source)
               VALUES (%(e)s, %(m)s, %(f)s, %(p)s, %(s)s, %(sub)s, %(src)s)
               RETURNING id""",
            {"e": entity, "m": month, "f": filename, "p": str(path),
             "s": sender, "sub": subject, "src": source}).fetchone()
        return row[0]


def invoices_for_month(entity: str, month: str) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT path, filename FROM invoices WHERE entity=%(e)s AND month=%(m)s ORDER BY id",
            {"e": entity, "m": month}).fetchall()
        return [{"path": r[0], "filename": r[1]} for r in rows]


def months_with_invoices() -> list:
    """Tutti i mesi per cui esistono fatture, per entità."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT entity, month FROM invoices ORDER BY month DESC").fetchall()
        return [dict(zip(["entity", "month"], r)) for r in rows]


# ------------------------------------------------------------- zip mensili ----
def mark_zip_sent(entity: str, month: str, path: str) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO monthly_zips (entity, month, path, sent)
               VALUES (%(e)s, %(m)s, %(p)s, TRUE)
               ON CONFLICT (entity, month)
               DO UPDATE SET path = %(p)s, sent = TRUE, ts = now()""",
            {"e": entity, "m": month, "p": str(path)})


def zip_sent(entity: str, month: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT sent FROM monthly_zips WHERE entity=%(e)s AND month=%(m)s",
            {"e": entity, "m": month}).fetchone()
        return bool(row and row[0])