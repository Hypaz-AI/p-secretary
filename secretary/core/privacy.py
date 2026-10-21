"""Oblio ed esportazione coordinati: DB, testi derivati e file.

Oblio (erase): dato un messaggio nativo, rimuove OVUNQUE i dati personali che ne
derivano, in una sola transazione dove possibile:
* testi derivati e indice (knowledge entries/occurrences/runs)
* verdetti di analisi (message_analysis)
* note personali con quella provenienza
* evidenze di modifica delle attività (task_changes; le attività restano)
* l'audit degli strumenti dell'agente che citano il messaggio (agent_tool_calls)
* eventuali relazioni legacy (pratiche del vecchio segretario) se presenti
* la riga nativa e gli allegati (byte nel DB; i file sono ELENCATI per una
  rimozione verificata, perché DB e filesystem non condividono una transazione)

Esportazione (export): snapshot JSON limitato di ciò che il sistema conserva su
un messaggio, per le richieste di portabilità. Sola lettura, nessun effetto.

Entrambe sono API fidate del proprietario: il chiamante (dashboard) autentica il
proprietario configurato e verifica il CSRF. Non concedono mai permessi. La
cancellazione dei file è restituita come rapporto, mai eseguita alla cieca.
"""
import json

from secretary.core import config, dashboard_owner as owners, policy

VERSION = 'privacy_receipts_v1'
WRITE_LOCK = 73190431
LEGACY_LINKS = 'secretary_links'
# Gli argomenti degli strumenti sono salvati come JSON (message_id sempre intero, mai troncato prima).
CALLS_ABOUT = r'"message_id": {}(?![0-9])'

SCHEMA = """
CREATE TABLE IF NOT EXISTS privacy_receipts (
 operation_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, receipt TEXT NOT NULL,
 result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""
NATIVE = {'email': 'email_messages', 'telegram': 'telegram_messages', 'whatsapp': 'whatsapp_messages'}


def migrate(conn):
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return
        conn.execute(SCHEMA)
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))


def _message_row(conn, message_id):
    owners.positive(message_id, 'message_id')
    row = conn.execute('''SELECT channel FROM email_messages WHERE id=%s
        UNION ALL SELECT channel FROM telegram_messages WHERE id=%s
        UNION ALL SELECT channel FROM whatsapp_messages WHERE id=%s''',
        (message_id, message_id, message_id)).fetchone()
    if not row:
        raise owners.Conflict('Unknown native message')
    return row[0].split(' ', 1)[0]


def _is_owner_authority(conn, message_id, channel):
    """True se il messaggio è il flusso di comandi owner vivo: non erasabile.

    L'oblio riguarda le comunicazioni osservate, non l'autorità stessa: cancellare
    la provenienza di un'azione del proprietario romperebbe i fence di autorità.
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


def _receipt(conn, key, request):
    row = conn.execute('SELECT request_hash,receipt,result FROM privacy_receipts WHERE operation_key=%s',
                       (key,)).fetchone()
    if row and row[0] != policy.payload_hash(request):
        raise owners.Conflict('Operation key already bound to another payload')
    return (row[1], row[2]) if row else None


def _legacy_links(conn, message_id):
    """Relazioni del vecchio segretario con FK verso il messaggio, se le tabelle esistono ancora.

    Sono dati di una versione precedente (vedi deploy/sql/drop_legacy_tables.sql): su
    una installazione nuova non esistono. Append-only per trigger: l'oblio è
    l'unica eccezione voluta, sotto lock e con ricevuta; il trigger viene sospeso
    per questa sola DELETE e ripristinato.
    """
    if conn.execute('SELECT to_regclass(%s)', ('secretary_runtime_link_proposals',)).fetchone()[0]:
        # Hanno una FK verso le voci di conoscenza che stiamo per eliminare.
        conn.execute('DELETE FROM secretary_runtime_link_proposals WHERE message_id=%s', (message_id,))
    if not conn.execute('SELECT to_regclass(%s)', (LEGACY_LINKS,)).fetchone()[0]:
        return
    triggers = {row[0] for row in conn.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid=%s::regclass AND NOT tgisinternal",
        (LEGACY_LINKS,)).fetchall()}
    guard = 'secretary_no_mutation' if 'secretary_no_mutation' in triggers else None
    if guard:
        conn.execute(f'ALTER TABLE {LEGACY_LINKS} DISABLE TRIGGER {guard}')
    try:
        for column in ('email_message_id', 'telegram_message_id', 'whatsapp_message_id'):
            conn.execute(f'DELETE FROM {LEGACY_LINKS} WHERE {column}=%s', (message_id,))
    finally:
        if guard:
            conn.execute(f'ALTER TABLE {LEGACY_LINKS} ENABLE TRIGGER {guard}')


def erase_message(conn, message_id, caller, *, operation_key):
    """API fidata: cancella un messaggio e tutti i dati derivati.

    Ritorna un rapporto: le cancellazioni DB sono atomiche; i percorsi dei file
    allegati sono ELENCATI perché il chiamante li elimini dopo verifica.
    Il replay di una ricevuta è idempotente e restituisce il rapporto originale.
    Il flusso di comandi del proprietario non è erasabile.
    """
    owners.require(caller)
    if type(operation_key) is not str or not 8 <= len(operation_key) <= 180 or '\x00' in operation_key:
        raise ValueError('Invalid operation key')
    request = dict(operation='erase_message', message_id=message_id, owner_id=caller.owner_id)
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (WRITE_LOCK,))
        # Il replay avviene PRIMA di ogni verifica: un oblio già eseguito non
        # deve fallire perché il messaggio non esiste più.
        previous = _receipt(conn, operation_key, request)
        if previous:
            return previous[1]
        channel = _message_row(conn, message_id)
        if _is_owner_authority(conn, message_id, channel):
            raise owners.Conflict('Il flusso di comandi owner non è erasabile')
        files = [row[0] for row in conn.execute('''SELECT a.path FROM attachments a
            WHERE a.message_id=%s AND a.path IS NOT NULL''', (message_id,)).fetchall()]
        # 0. Relazioni legacy che referenziano le voci di conoscenza (FK): prima di tutto.
        _legacy_links(conn, message_id)
        # 1. Testi derivati e indice. Le tabelle knowledge sono append-only via
        #    trigger: DELETE esplicito qui, sotto lock e con ricevuta. I documenti
        #    condivisi (stesso sha256) restano se altre occorrenze li referenziano.
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
        # 4. Evidenza delle attività (le attività restano; l'evidenza no).
        conn.execute('DELETE FROM task_changes WHERE message_id=%s', (message_id,))
        # 4b. Audit dell'agente: riepiloghi e citazioni passati agli strumenti per questo messaggio.
        conn.execute('DELETE FROM agent_tool_calls WHERE args ~ %s', (CALLS_ABOUT.format(message_id),))
        # 5. Messaggio nativo e allegati (byte DB). I file restano da eliminare.
        conn.execute('DELETE FROM attachments WHERE message_id=%s', (message_id,))
        conn.execute(f'DELETE FROM {NATIVE[channel]} WHERE id=%s', (message_id,))
        report = dict(message_id=message_id, channel=channel,
                      db_erased=True, files_to_delete=files,
                      receipt=f'Oblio del messaggio #{message_id} completato nel DB. '
                              f'{len(files)} file allegato/i elencati per la rimozione verificata.')
        conn.execute('''INSERT INTO privacy_receipts(operation_key,request_hash,receipt,result)
            VALUES (%s,%s,%s,%s)''', (operation_key, policy.payload_hash(request), report['receipt'],
                                      json.dumps(report)))
        return report


def export_message(conn, message_id, caller):
    """API fidata: esportazione JSON limitata di ciò che si conserva su un messaggio.

    Sola lettura. Include riga nativa, analisi, note, evidenze delle attività,
    voci di conoscenza e metadati degli allegati (mai i byte: i file originali
    sono già del proprietario).
    """
    owners.require(caller)
    channel = _message_row(conn, message_id)
    result = dict(message_id=message_id, channel=channel)
    row = conn.execute(f'SELECT to_jsonb(t) FROM {NATIVE[channel]} t WHERE id=%s', (message_id,)).fetchone()
    result['native'] = row[0] if row else None
    result['analysis'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(t) FROM message_analysis t WHERE message_id=%s', (message_id,)).fetchall()]
    result['notes'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(t) FROM notes t WHERE source_message=%s', (message_id,)).fetchall()]
    result['task_evidence'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(t) FROM task_changes t WHERE message_id=%s', (message_id,)).fetchall()]
    result['agent_tool_calls'] = [r[0] for r in conn.execute(
        'SELECT jsonb_build_object(\'tool\',tool,\'ok\',ok,\'summary\',summary,\'args\',args,\'at\',at) '
        'FROM agent_tool_calls WHERE args ~ %s ORDER BY id', (CALLS_ABOUT.format(message_id),)).fetchall()]
    result['knowledge'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(e) FROM knowledge_entries e JOIN knowledge_runs r ON r.id=e.run_id '
        'WHERE r.message_id=%s', (message_id,)).fetchall()]
    result['attachments'] = [r[0] for r in conn.execute(
        'SELECT to_jsonb(a) - \'data\' FROM attachments a WHERE message_id=%s', (message_id,)).fetchall()]
    return result
