"""Accesso ai dati dell'agente: query libere, messaggi, allegati.

Tutto passa dalle viste `agent.*` (consensi inclusi). Nessuna funzione qui
scrive nel database. Gli errori sono tradotti in messaggi brevi e privi di
dati, pensati per essere letti dal modello e correggere la query.
"""
import threading
import time

import psycopg

import config
import jobs
from agent import fsbox, schema as agent_schema, sqlguard

MAX_BODY_CHARS = 60000
_role_lock = threading.Lock()
_role_state = {'checked': 0.0, 'usable': False, 'role': None}
CONTENT_GATED = ('email_attachments', 'tg_files', 'wa_media')


def connect():
    """Connessione NON autocommit: SET LOCAL vale solo per la singola transazione."""
    return psycopg.connect(config.DATABASE_URL, autocommit=False, prepare_threshold=None,
                           connect_timeout=10,
                           options='-c statement_timeout=15000 -c lock_timeout=5000 '
                                   '-c idle_in_transaction_session_timeout=20000')


def schema_name():
    return config.AGENT_DB_SCHEMA


def readonly_role():
    """Ruolo di sola lettura se esiste ed è assumibile (ricontrollato ogni 5 minuti)."""
    with _role_lock:
        # La cache vale per UN nome di ruolo: cambiarlo (config, test) la invalida.
        if _role_state['role'] != config.AGENT_DB_ROLE or time.monotonic() - _role_state['checked'] > 300:
            try:
                with connect() as conn:
                    usable = agent_schema.readonly_role_usable(conn, config.AGENT_DB_ROLE)
            except Exception:
                usable = False
            _role_state.update(checked=time.monotonic(), usable=usable, role=config.AGENT_DB_ROLE)
        return config.AGENT_DB_ROLE if _role_state['usable'] else None


def explain(exc):
    """Messaggio sicuro per il modello a partire da un errore del database."""
    if isinstance(exc, sqlguard.SqlRejected):
        return str(exc)
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return 'Query troppo lenta (oltre 10 s): aggiungi filtri su day/ts/source e un LIMIT'
    if isinstance(exc, (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable,
                        psycopg.errors.UndefinedFunction, psycopg.errors.SyntaxError,
                        psycopg.errors.AmbiguousColumn, psycopg.errors.DatatypeMismatch,
                        psycopg.errors.InvalidTextRepresentation, psycopg.errors.DivisionByZero,
                        psycopg.errors.GroupingError, psycopg.errors.InvalidDatetimeFormat)):
        primary = getattr(getattr(exc, 'diag', None), 'message_primary', None)
        return f'Errore SQL: {primary}' if primary else 'Errore SQL: controlla colonne e sintassi (db_schema)'
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return 'Relazione non accessibile: usa solo le viste dello schema ' + schema_name()
    return 'Errore del database: riprova con una query più semplice'


def db_schema():
    """Primer verificato + colonne reali delle viste (se il DB è raggiungibile)."""
    jobs.assert_current()
    info = dict(primer=agent_schema.primer(schema_name()))
    try:
        with connect() as conn:
            rows = conn.execute('''SELECT table_name, string_agg(column_name||' '||data_type, ', '
                ORDER BY ordinal_position) FROM information_schema.columns
                WHERE table_schema=%s GROUP BY table_name ORDER BY table_name''',
                (schema_name(),)).fetchall()
        info['views'] = {name: cols for name, cols in rows}
    except Exception:
        pass
    return info


def db_query(sql):
    jobs.assert_current()
    try:
        return sqlguard.execute(connect, sql, schema=schema_name(), role=readonly_role())
    except Exception as exc:
        if isinstance(exc, jobs.LeaseLost):
            raise
        return dict(error=explain(exc))


def read_message(message_id, offset=0, max_chars=20000):
    """Testo completo di un messaggio visibile (consensi inclusi) e dei suoi allegati."""
    jobs.assert_current()
    if type(message_id) is not int or not 0 < message_id <= 9223372036854775807:
        return dict(error='message_id non valido')
    max_chars = max(200, min(int(max_chars), MAX_BODY_CHARS))
    offset = max(0, int(offset))
    s = schema_name()
    try:
        with connect() as conn, conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            row = conn.execute(f'''SELECT id,ts,channel,sender,subject,body,direction,account,chat_id,
                summary,category,priority,needs_action,action,needs_reply,reply_hint
                FROM {s}.messages WHERE id=%s''', (message_id,)).fetchone()
            if row is None:
                return dict(error='Messaggio inesistente o non accessibile (consenso dati non attivo)')
            names = ('id', 'ts', 'channel', 'sender', 'subject', 'body', 'direction', 'account', 'chat_id',
                     'summary', 'category', 'priority', 'needs_action', 'action', 'needs_reply', 'reply_hint')
            message = dict(zip(names, row))
            attachments = conn.execute(f'''SELECT id,filename,content_type,rel_path,stored
                FROM {s}.attachments WHERE message_id=%s ORDER BY id LIMIT 30''', (message_id,)).fetchall()
    except Exception as exc:
        return dict(error=explain(exc))
    body = message.pop('body') or ''
    message['ts'] = message['ts'].isoformat() if message['ts'] else None
    message.update(body=body[offset:offset + max_chars], body_chars=len(body), offset=offset,
                   truncated=offset + max_chars < len(body),
                   attachments=[dict(zip(('id', 'filename', 'content_type', 'path', 'stored'), a))
                                for a in attachments])
    return message


def _visible_attachment(conn, attachment_id):
    row = conn.execute(f'''SELECT id,message_id,filename,content_type,rel_path
        FROM {schema_name()}.attachments WHERE id=%s''', (attachment_id,)).fetchone()
    return dict(zip(('id', 'message_id', 'filename', 'content_type', 'rel_path'), row)) if row else None


def attachment_text(attachment_id, offset=0, max_chars=20000):
    """Testo di un allegato: trascrizione/descrizione già estratta, altrimenti dal file."""
    jobs.assert_current()
    if type(attachment_id) is not int or attachment_id <= 0:
        return dict(error='attachment_id non valido')
    max_chars = max(200, min(int(max_chars), MAX_BODY_CHARS))
    offset = max(0, int(offset))
    try:
        with connect() as conn, conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            info = _visible_attachment(conn, attachment_id)
            if info is None:
                return dict(error='Allegato inesistente o non accessibile (consenso media non attivo)')
            transcript = conn.execute(f'''SELECT text FROM {schema_name()}.documents
                WHERE entry_id=%s AND source_type LIKE 'media\\_%%' LIMIT 1''', (attachment_id,)).fetchone()
            raw = None
            if not transcript:
                raw = conn.execute('SELECT data FROM attachments WHERE id=%s', (attachment_id,)).fetchone()
    except Exception as exc:
        return dict(error=explain(exc))
    text, source = None, None
    if transcript and transcript[0]:
        text, source = transcript[0], 'trascrizione'
    elif raw and raw[0] is not None:
        text, source = fsbox.extract_text(bytes(raw[0]), info['filename'] or ''), 'database'
    elif info['rel_path']:
        try:
            data, _ = fsbox.read_bytes(info['rel_path'])
            text, source = fsbox.extract_text(data, info['filename'] or info['rel_path']), 'file'
        except fsbox.FsError:
            return dict(error='File dell\'allegato non più disponibile nell\'archivio',
                        attachment_id=attachment_id, filename=info['filename'])
    else:
        return dict(error='Allegato senza contenuto archiviato', attachment_id=attachment_id,
                    filename=info['filename'])
    return dict(attachment_id=attachment_id, message_id=info['message_id'], filename=info['filename'],
                content_type=info['content_type'], source=source, chars=len(text), offset=offset,
                text=text[offset:offset + max_chars], truncated=offset + max_chars < len(text))


def gate_path(path):
    """Un file allegato è leggibile solo se un allegato VISIBILE (consensi) lo referenzia."""
    segments = [s for s in str(path).strip('/').split('/') if s]
    if len(segments) >= 2 and segments[0] in CONTENT_GATED:
        rel = '/'.join(segments[:2])
        with connect() as conn, conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            if not conn.execute(f'SELECT 1 FROM {schema_name()}.attachments WHERE rel_path=%s LIMIT 1',
                                (rel,)).fetchone():
                raise fsbox.FsError('File non accessibile: nessun allegato visibile lo referenzia '
                                    '(consenso media non attivo o riferimento mancante)')
