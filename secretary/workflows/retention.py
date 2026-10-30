"""Retention: potatura periodica di storia, file e registri operativi.

Una passata al giorno (timbro in source_cursors, ripresa se rimasta incompleta).
Ogni lotto è una transazione; i file si rimuovono solo DOPO il commit del DB e
solo se nessuna riga ancora presente li referenzia e sono più vecchi della grazia.

Cosa viene potato:
* messaggi nativi scaduti per data di acquisizione, con allegati, analisi,
  derivati di conoscenza (testi, voci, occorrenze) e dialoghi col proprietario.
  Un messaggio resta se è ancora referenziato da lavoro vivo (vedi _protected);
* file degli allegati non più referenziati (email_attachments, tg_files,
  wa_media) e gli orfani di email_attachments più vecchi della grazia;
* job conclusi: si svuota il payload, la riga e la chiave restano. Le chiavi sono
  identità di deduplica (analysis:<id>, task-reminder:<id>, digest:<data>...):
  cancellarle farebbe ri-accodare lavoro già eseguito;
* outbox email inviata/rifiutata e ledger AI completato oltre la finestra.

Cosa NON viene potato: identità di deduplica (message_dedup, email_seen),
ricevute degli effetti, job falliti (da riesaminare), dati dell'owner (note,
attività, appuntamenti, regole, fatture, zip), registro privacy, grant calendar,
ledger AI in attesa o incerto. Nessuna cancellazione è decisa dal modello.
"""
import json
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from secretary.core import config, db

VERSION = 'retention_v1'
STAMP = 'retention_pass'
LOCK = 73190461
TABLES = ('email_messages', 'whatsapp_messages', 'telegram_messages')
# Relazioni legacy con FK verso il contenuto: la potatura si ferma finché esistono
# (vedi deploy/sql/drop_legacy_tables.sql). Non le tocca da sola.
LEGACY = ('secretary_links', 'secretary_runtime_link_proposals')
MEDIA_DIRS = ('email_attachments', 'tg_files', 'wa_media')
ORPHAN_DIRS = ('email_attachments',)  # content-addressed, nessun altro produttore
KEY_RE = r'((?:email_attachments|tg_files|wa_media)/[^/]+)$'
# Grazia sulla data di modifica: un file riusato dalla deduplica viene "toccato" in
# persist(), quindi non è mai cancellato mentre il suo riferimento è in commit.
FILE_GRACE_SECONDS = 30 * 86400
BATCH = 500
MAX_BATCHES = 20
NON_TERMINAL = "('pending', 'running', 'blocked')"


def _protected(alias):
    """Predicato: il messaggio `alias` può essere potato solo se nulla lo usa ancora.

    Lavoro vivo, evidenza dell'owner e provenienza delle attività restano; una
    riga chiusa (ricevuta, job concluso) non trattiene il messaggio.
    """
    return f"""
  NOT EXISTS (SELECT 1 FROM tasks t WHERE t.source_message = {alias}.id OR t.owner_origin_message = {alias}.id)
  AND NOT EXISTS (SELECT 1 FROM task_changes c WHERE c.message_id = {alias}.id)
  AND NOT EXISTS (SELECT 1 FROM notes n WHERE n.source_message = {alias}.id)
  AND NOT EXISTS (SELECT 1 FROM effect_receipts r WHERE r.message_id = {alias}.id
                  AND r.status IN ('authorized', 'attempted', 'uncertain'))
  AND NOT EXISTS (SELECT 1 FROM agent_email_outbox o
                  WHERE (o.proposed_by_message = {alias}.id OR o.approved_by_message = {alias}.id
                         OR o.source_message = {alias}.id)
                    AND o.state IN ('proposed', 'approved', 'uncertain'))
  AND NOT EXISTS (SELECT 1 FROM calendar_effect_grants g
                  WHERE g.owner_message_id = {alias}.id AND NOT g.revoked AND g.expires_at > now())
  AND NOT EXISTS (SELECT 1 FROM system_notifications s WHERE s.expires_at > now()
                  AND ({alias}.id = ANY(s.source_ids) OR {alias}.id = ANY(s.media_source_ids)))
  AND NOT EXISTS (SELECT 1 FROM work_jobs j WHERE j.status IN {NON_TERMINAL}
                  AND (j.payload->>'message_id' = {alias}.id::text
                       OR j.payload->>'evidence_message_id' = {alias}.id::text))
  AND NOT EXISTS (SELECT 1 FROM work_jobs j JOIN attachments a ON j.payload->>'attachment_id' = a.id::text
                  WHERE a.message_id = {alias}.id AND j.status IN {NON_TERMINAL})"""


def migrate(conn):
    """Indici additivi per la potatura. Nessun dato viene modificato."""
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return
        for table in TABLES:
            conn.execute(f'CREATE INDEX IF NOT EXISTS {table}_acquired ON {table}(acquired_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS effect_receipts_message ON effect_receipts(message_id)')
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))


# ----------------------------------------------------------- messaggi ----
def _prune_messages(conn, table, days, limit):
    """Un lotto in una transazione: potatura di messaggi e dipendenze.

    Ritorna (numero di messaggi, chiavi dei file rilasciati). Il predicato di
    protezione è valutato nello stesso statement della DELETE.
    """
    rows = conn.execute(f"""WITH victims AS (
        SELECT m.id FROM {table} m
        WHERE m.acquired_at < now() - (%s * interval '1 day') AND {_protected('m')}
        ORDER BY m.id LIMIT %s FOR UPDATE SKIP LOCKED)
      DELETE FROM {table} x USING victims v WHERE x.id = v.id RETURNING x.id""",
        (days, limit)).fetchall()
    ids = [row[0] for row in rows]
    if not ids:
        return 0, set()
    runs = 'SELECT id FROM knowledge_runs WHERE message_id = ANY(%s)'
    docs = [row[0] for row in conn.execute(
        f'SELECT DISTINCT document_id FROM knowledge_occurrences '
        f'WHERE run_id IN ({runs}) AND document_id IS NOT NULL', (ids,)).fetchall()]
    conn.execute(f'DELETE FROM knowledge_entries WHERE run_id IN ({runs})', (ids,))
    conn.execute(f'DELETE FROM knowledge_occurrences WHERE run_id IN ({runs})', (ids,))
    conn.execute('DELETE FROM knowledge_runs WHERE message_id = ANY(%s)', (ids,))
    if docs:
        # I documenti condivisi (stesso sha256) restano finché un'altra occorrenza li usa.
        conn.execute('''DELETE FROM knowledge_extractions e WHERE e.document_id = ANY(%s)
            AND NOT EXISTS (SELECT 1 FROM knowledge_occurrences o WHERE o.document_id = e.document_id)''',
            (docs,))
        conn.execute('''DELETE FROM knowledge_documents d WHERE d.id = ANY(%s)
            AND NOT EXISTS (SELECT 1 FROM knowledge_occurrences o WHERE o.document_id = d.id)
            AND NOT EXISTS (SELECT 1 FROM knowledge_extractions e WHERE e.document_id = d.id)''', (docs,))
    conn.execute('DELETE FROM message_analysis WHERE message_id = ANY(%s)', (ids,))
    conn.execute('DELETE FROM owner_exchanges WHERE message_id = ANY(%s)', (ids,))
    # media_transcripts cade in cascata. message_dedup ed email_seen restano: sono identità.
    paths = conn.execute('DELETE FROM attachments WHERE message_id = ANY(%s) RETURNING path',
                         (ids,)).fetchall()
    released = {key for key in (_key(path) for (path,) in paths if path) if key}
    return len(ids), released


def _referenced(conn, only=None):
    """Chiavi relative (cartella/nome) ancora referenziate da qualunque riga.

    La chiave non dipende dal percorso assoluto: un DB migrato da un altro host
    (path diversi, stesso file) continua a proteggere il file.
    """
    params = {'re': KEY_RE, 'only': sorted(only) if only is not None else None}
    parts = []
    for table, column in (('attachments', 'path'), ('knowledge_occurrences', 'original_path'),
                          ('invoices', 'path')):
        expr = f'substring({column} FROM %(re)s::text)'
        clause = f'{column} ~ %(re)s::text'
        if only is not None:
            clause += f' AND {expr} = ANY(%(only)s::text[])'
        parts.append(f'SELECT {expr} FROM {table} WHERE {clause}')
    return {row[0] for row in conn.execute(' UNION '.join(parts), params).fetchall() if row[0]}


# ------------------------------------------------------------------ file ----
_KEY = re.compile(KEY_RE)


def _key(path):
    match = _KEY.search(str(path or ''))
    return match.group(1) if match else None


def _media_file(key):
    folder, _, name = key.partition('/')
    if folder not in MEDIA_DIRS or name in ('', '.', '..') or '/' in name:
        return None
    return Path(config.DATA_DIR) / folder / name


def _unlink_if_old(path, cutoff):
    """Cancella solo file regolari, non collegamenti, con data di modifica prima della soglia."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def _release(keys, cutoff):
    """Rimuove i file rilasciati dalla potatura che nessuna riga referenzia più."""
    if not keys:
        return 0
    with db._conn() as conn:
        still = _referenced(conn, only=keys)
    removed = 0
    for key in sorted(set(keys) - still):
        path = _media_file(key)
        if path is not None and _unlink_if_old(path, cutoff):
            removed += 1
    return removed


def _sweep_orphans(cutoff):
    """Orfani di email_attachments: file senza riga (crash o rollback) e vecchi."""
    candidates = {}
    for folder in ORPHAN_DIRS:
        root = Path(config.DATA_DIR) / folder
        if not root.is_dir() or root.is_symlink():
            continue
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    candidates[f'{folder}/{entry.name}'] = Path(entry.path)
    if not candidates:
        return 0
    with db._conn() as conn:
        still = _referenced(conn, only=set(candidates))
    return sum(1 for key, path in candidates.items()
               if key not in still and _unlink_if_old(path, cutoff))


# ------------------------------------------------------ operativo e ledger ----
def _compact_jobs(conn, days, limit):
    """Job conclusi: svuota il payload (può contenere testo), conserva riga e chiave."""
    return len(conn.execute("""WITH batch AS (SELECT id FROM work_jobs
        WHERE status='done' AND payload <> '{}'::jsonb
          AND COALESCE(finished_at, created_at) < now() - (%s * interval '1 day')
        ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)
      UPDATE work_jobs j SET payload='{}'::jsonb FROM batch WHERE j.id = batch.id
      RETURNING j.id""", (days, limit)).fetchall())


def _prune_outbox(conn, days, limit):
    """Email terminali (inviate o rifiutate). Le ricevute degli effetti restano."""
    return conn.execute("""DELETE FROM agent_email_outbox WHERE id IN (
        SELECT id FROM agent_email_outbox WHERE state IN ('sent', 'rejected')
          AND COALESCE(sent_at, decided_at, created_at) < now() - (%s * interval '1 day')
        ORDER BY id LIMIT %s)""", (days, limit)).rowcount


def _prune_ai_usage(conn, days):
    """Solo consumi completati: prenotazioni in corso e incerte restano per la riconciliazione."""
    return conn.execute("""DELETE FROM secretary_ai_usage WHERE state = 'completed'
        AND usage_day < (now() AT TIME ZONE 'UTC')::date - %s""", (days,)).rowcount


# ------------------------------------------------------------- passata ----
def _batch(step, *args):
    with db._conn() as conn, conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
        return step(conn, *args)


def _drain(step, *args):
    """Ripete un passo a lotti; ritorna (totale, completato)."""
    total = 0
    for _ in range(MAX_BATCHES):
        count = _batch(step, *args, BATCH)
        total += count
        if count < BATCH:
            return total, True
    return total, False


def _legacy_present(conn):
    return any(conn.execute('SELECT to_regclass(%s)', (name,)).fetchone()[0] for name in LEGACY)


def run():
    """Una passata completa. `complete` è False se restano lotti da potare."""
    cutoff = time.time() - FILE_GRACE_SECONDS
    report = dict(messages=0, files=0, job_payloads=0, outbox=0, ai_usage=0, complete=True, skipped=[])
    days = config.RETENTION_MESSAGES_DAYS
    if days:
        with db._conn() as conn:
            legacy = _legacy_present(conn)
        if legacy:
            report['skipped'].append('legacy_tables')
        else:
            released = set()
            for table in TABLES:
                complete = False
                for _ in range(MAX_BATCHES):
                    count, keys = _batch(_prune_messages, table, days, BATCH)
                    report['messages'] += count
                    released |= keys
                    if count < BATCH:
                        complete = True
                        break
                report['complete'] = report['complete'] and complete
            report['files'] += _release(released, cutoff)
            report['files'] += _sweep_orphans(cutoff)
    if config.RETENTION_OPERATIONAL_DAYS:
        report['job_payloads'], complete = _drain(_compact_jobs, config.RETENTION_OPERATIONAL_DAYS)
        report['complete'] = report['complete'] and complete
        report['outbox'], complete = _drain(_prune_outbox, config.RETENTION_OPERATIONAL_DAYS)
        report['complete'] = report['complete'] and complete
    if config.RETENTION_AI_USAGE_DAYS:
        report['ai_usage'] = _batch(_prune_ai_usage, config.RETENTION_AI_USAGE_DAYS)
    return report


def run_scheduled():
    """Chiamata periodica da main: una passata al giorno, ripresa se incompleta."""
    if not db.available() or not (config.RETENTION_MESSAGES_DAYS or config.RETENTION_OPERATIONAL_DAYS
                                  or config.RETENTION_AI_USAGE_DAYS):
        return None
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
    with db._conn() as conn:
        row = conn.execute('SELECT value FROM source_cursors WHERE key=%s', (STAMP,)).fetchone()
    stamp = row[0] if row and isinstance(row[0], dict) else {}
    if stamp.get('day') == today and stamp.get('complete') is True:
        return None
    report = run()
    with db._conn() as conn, conn.transaction():
        conn.execute('''INSERT INTO source_cursors(key,value) VALUES (%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()''',
            (STAMP, json.dumps({'day': today, 'complete': report['complete']})))
    counts = {k: report[k] for k in ('messages', 'files', 'job_payloads', 'outbox', 'ai_usage')}
    if any(counts.values()) or report['skipped'] or not report['complete']:
        # Solo conteggi: nessun contenuto di messaggi o percorsi nei log.
        print('[retention] ' + ' '.join(f'{k}={v}' for k, v in counts.items())
              + (f" saltato={','.join(report['skipped'])}" if report['skipped'] else '')
              + ('' if report['complete'] else ' (incompleto: prosegue alla prossima passata)'))
    return report
