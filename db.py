"""Persistenza su Postgres: comunicazioni, appuntamenti, note, fatture, regole.

Tutto il 'contesto' del segretario vive qui: ogni comunicazione analizzata,
ogni regola che l'utente insegna, ogni fattura salvata. Lo schema viene
creato automaticamente al primo avvio (CREATE TABLE IF NOT EXISTS).
"""
import json
from datetime import datetime

import config
import message_store

try:
    import psycopg
    from psycopg import sql as pg_sql
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
        result = message_store.migrate(conn, SCHEMA, False)
        import jobs
        jobs.migrate(conn)
        return result


# ------------------------------------------------------- comunicazioni ----
def store_message(channel, sender, subject, body, ts=None, source_id=None,
                  metadata=None, attachments=None, update_existing=False):
    with _conn() as conn:
        return message_store.store(conn, channel, sender, subject, body, ts,
                                   source_id, metadata, attachments, update_existing)


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
            """SELECT ts, channel, sender, subject,
                      COALESCE(summary, LEFT(body, 2000)) AS summary, category,
                      priority, needs_action, action, direction
               FROM communications
               WHERE day = COALESCE(%(day)s, (now() AT TIME ZONE %(tz)s)::date)
               ORDER BY ts""",
            {"day": day, "tz": config.TIMEZONE}).fetchall()
        cols = ["ts", "channel", "sender", "subject", "summary",
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
                      COALESCE(summary, LEFT(body, 2000)) AS summary, category,
                      priority, needs_action, action, direction
               FROM communications
               WHERE ts >= now() - %(hours)s * interval '1 hour'
               ORDER BY ts""",
            {"hours": int(hours)}).fetchall()
        cols = ["ts", "channel", "sender", "subject", "summary", "category",
                "priority", "needs_action", "action", "direction"]
        return [dict(zip(cols, r)) for r in rows]

# ------------------------------------------------------------ appuntamenti ----
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
def add_note(text: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO notes (text) VALUES (%(t)s) RETURNING id",
            {"t": text}).fetchone()
        return row[0]


def recent_notes(limit: int = 5) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT text FROM notes ORDER BY id DESC LIMIT %(l)s",
            {"l": limit}).fetchall()
        return [r[0] for r in rows]


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