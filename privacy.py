"""Coordinated privacy erase/export across DB, derived text and files.

Oblio (erase): given a native message, removes EVERYWHERE the personal data
derived from it, in one transaction where possible:
* knowledge entries/occurrences/runs (derived text and index)
* message analysis verdicts
* personal notes with that provenance
* task changes referencing the message (evidence rows, not the tasks themselves)
* secretary links using the message as evidence
* the native message row and its attachments (DB bytes; file occurrences are
  reported for manual/orchestrated deletion because DB+filesystem cannot
  share one transaction)

Esportazione (export): bounded JSON snapshot of everything the system holds
about one message, for portability requests. Read-only, no side effects.

Both APIs are TRUSTED OWNER APIs: the caller (dashboard) must authenticate the
configured owner and verify CSRF before calling. They never grant permissions.
Filesystem deletion is returned as a report, never executed blindly: the caller
must verify paths against the storage roots before unlinking (fail-safe).
"""
import json

import config
import db
import secretary as sec


def _message_row(conn, message_id):
    sec._positive(message_id, 'message_id')
    row = conn.execute('''SELECT channel FROM email_messages WHERE id=%s
        UNION ALL SELECT channel FROM telegram_messages WHERE id=%s
        UNION ALL SELECT channel FROM whatsapp_messages WHERE id=%s''',
        (message_id, message_id, message_id)).fetchone()
    if not row:
        raise sec.Conflict('Unknown native message')
    return row[0].split(' ', 1)[0]


def _is_owner_authority(conn, message_id, channel):
    """True se il messaggio è il flusso di comandi owner vivo: non erasabile.

    L'oblio riguarda le comunicazioni osservate, non l'autorità stessa: cancellare
    la provenienza di una pratica/delega romperebbe i fence di autorità.
    """
    if channel != 'telegram':
        return False
    row = conn.execute('''SELECT account, chat_id, author, direction, metadata
        FROM telegram_messages WHERE id=%s''', (message_id,)).fetchone()
    if not row:
        return False
    account, chat, author, direction, meta = row
    owner = str(config.TELEGRAM_CHAT_ID or '').strip()
    bot = str(config.TELEGRAM_BOT_TOKEN or '').split(':', 1)[0]
    return (account == 'bot:' + bot and str(chat) == owner and str(author) == owner
            and direction == 'in' and isinstance(meta, dict)
            and meta.get('content_origin') == 'direct_text')


def erase_message(conn, message_id, caller, *, operation_key):
    """Trusted owner API: erase one message and all its derived data.

    Returns a report: DB deletions are committed atomically; file paths of the
    attachments are LISTED for the caller to delete after verifying them.
    Receipt replay is idempotent and returns the original report. The live
    owner command stream is refused: erasing authority provenance would break
    the authorization fences of every practice/delegation derived from it.
    """
    sec._caller(caller)
    request = dict(operation='erase_message', message_id=message_id,
                   owner_id=caller.owner_id)
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (sec.WRITE_LOCK,))
        # Il replay della ricevuta avviene PRIMA di ogni verifica: un oblio già
        # eseguito non deve fallire perché il messaggio non esiste più.
        previous = sec._receipt(conn, operation_key, request)
        if previous:
            return previous[1]
        channel = _message_row(conn, message_id)
        if _is_owner_authority(conn, message_id, channel):
            raise sec.Conflict('Il flusso di comandi owner non è erasabile: '
                               'cancella le pratiche/deleghe che ne derivano prima')
        # 1. Testi derivati e indice: knowledge runs/occurrences/entries.
        #    Le tabelle knowledge sono append-only via trigger: usiamo DELETE
        #    esplicito qui perché l'oblio è l'unica eccezione voluta, sotto
        #    lock e con ricevuta. I documenti condivisi (stesso sha256) restano
        #    se altre occorrenze li referenziano.
        files = [row[0] for row in conn.execute('''SELECT a.path FROM attachments a
            WHERE a.message_id=%s AND a.path IS NOT NULL''', (message_id,)).fetchall()]
        conn.execute('''DELETE FROM knowledge_entries WHERE run_id IN
            (SELECT id FROM knowledge_runs WHERE message_id=%s)''', (message_id,))
        conn.execute('''DELETE FROM knowledge_occurrences WHERE run_id IN
            (SELECT id FROM knowledge_runs WHERE message_id=%s)''', (message_id,))
        conn.execute('''DELETE FROM knowledge_extractions WHERE document_id IN (
            SELECT DISTINCT o.document_id FROM knowledge_occurrences o
            JOIN knowledge_runs r ON r.id=o.run_id WHERE r.message_id=%s
              AND o.document_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM knowledge_occurrences o2
                  WHERE o2.document_id=o.document_id AND o2.run_id NOT IN
                      (SELECT id FROM knowledge_runs WHERE message_id=%s)))''',
            (message_id, message_id))
        conn.execute('DELETE FROM knowledge_runs WHERE message_id=%s', (message_id,))
        # 2. Analisi e verdetti.
        conn.execute('DELETE FROM message_analysis WHERE message_id=%s', (message_id,))
        # 3. Note personali con provenienza da questo messaggio.
        conn.execute('DELETE FROM notes WHERE source_message=%s', (message_id,))
        # 4. Evidenza task collegata (i task restano; l'evidenza no).
        conn.execute('DELETE FROM task_changes WHERE message_id=%s', (message_id,))
        # 5. Collegamenti pratiche che usano il messaggio come evidenza.
        #    secretary_links è append-only per progetto: l'oblio è l'unica
        #    eccezione voluta, quindi il trigger viene sospeso per questa sola
        #    DELETE sotto lock e con ricevuta, e ripristinato subito dopo.
        conn.execute('ALTER TABLE secretary_links DISABLE TRIGGER secretary_no_mutation')
        try:
            for column in ('email_message_id', 'telegram_message_id', 'whatsapp_message_id'):
                conn.execute(f'DELETE FROM secretary_links WHERE {column}=%s', (message_id,))
        finally:
            conn.execute('ALTER TABLE secretary_links ENABLE TRIGGER secretary_no_mutation')
        # 6. Messaggio nativo e allegati (bytes DB). I file restano da eliminare.
        conn.execute('DELETE FROM attachments WHERE message_id=%s', (message_id,))
        table = {'email': 'email_messages', 'telegram': 'telegram_messages',
                 'whatsapp': 'whatsapp_messages'}[channel]
        conn.execute(f'DELETE FROM {table} WHERE id=%s', (message_id,))
        report = dict(message_id=message_id, channel=channel,
                      db_erased=True, files_to_delete=files,
                      receipt=f'Oblio del messaggio #{message_id} completato nel DB. '
                              f'{len(files)} file allegato/i elencati per la rimozione verificata.')
        sec._record(conn, operation_key, request, report['receipt'], report)
        return report


def export_message(conn, message_id, caller):
    """Trusted owner API: bounded JSON export of everything held about a message.

    Read-only. Includes native row, analysis, notes, tasks evidence, practice
    links, knowledge entries and attachment metadata (never raw bytes: the
    export references files, the owner already owns the originals).
    """
    sec._caller(caller)
    channel = _message_row(conn, message_id)
    table = {'email': 'email_messages', 'telegram': 'telegram_messages',
             'whatsapp': 'whatsapp_messages'}[channel]
    result = dict(message_id=message_id, channel=channel)
    row = conn.execute(f'SELECT to_jsonb(t) FROM {table} t WHERE id=%s', (message_id,)).fetchone()
    result['native'] = row[0] if row else None
    result['analysis'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(t) FROM message_analysis t WHERE message_id=%s', (message_id,)).fetchall()]
    result['notes'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(t) FROM notes t WHERE source_message=%s', (message_id,)).fetchall()]
    result['task_evidence'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(t) FROM task_changes t WHERE message_id=%s', (message_id,)).fetchall()]
    result['practice_links'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(l) FROM secretary_links l WHERE %s IN '
        '(l.email_message_id,l.telegram_message_id,l.whatsapp_message_id)', (message_id,)).fetchall()]
    result['knowledge'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(e) FROM knowledge_entries e JOIN knowledge_runs r ON r.id=e.run_id '
        'WHERE r.message_id=%s', (message_id,)).fetchall()]
    result['attachments'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(a) - \'data\' FROM attachments a WHERE message_id=%s', (message_id,)).fetchall()]
    return result
