"""Catalogo locale e versionato del testo estratto da documenti e allegati.

Opt-in: migrate(conn), enqueue(conn, message_id), poi jobs.Worker('knowledge',
index_message). Nulla qui invia, chiama l'AI, scopre cartelle, copia originali o
concede autorità al testo delle fonti. Gli ID dei documenti identificano le
*occorrenze*, non il contenuto deduplicato. L'agente legge il risultato dalla
vista `agent.documents` (consensi rivalutati a ogni query).
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from secretary.core import config, db
from secretary.archive import documents
from secretary.core import jobs

VERSION = 'knowledge_v2'
EXTRACTOR_VERSION = 'documents_pdf_xml_v2'
MAX_TEXT = 24000
MAX_ATTACHMENTS = 20
MAX_BYTES = documents.MAX_BYTES
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_PDF_PAGES = 10
TABLES = ('email_messages', 'whatsapp_messages', 'telegram_messages')
CONTENT_DIRS = {'email_attachments', 'wa_media', 'tg_files', 'uploads', 'invoices'}
SECRET_NAME = re.compile(
    r'credential|secret|session|token|password|\.env|\.pem$|\.key$|config|state|spool|bridge', re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_documents (
 id BIGSERIAL PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
 byte_size BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS knowledge_extractions (
 id BIGSERIAL PRIMARY KEY,
 document_id BIGINT NOT NULL REFERENCES knowledge_documents(id),
 extractor_version TEXT NOT NULL, format TEXT NOT NULL,
 status TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', error_code TEXT,
 truncated BOOLEAN NOT NULL DEFAULT FALSE, attempts INTEGER NOT NULL DEFAULT 1,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(document_id,extractor_version,format));
ALTER TABLE knowledge_extractions ADD COLUMN IF NOT EXISTS limits JSONB NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS knowledge_runs (
 id BIGSERIAL PRIMARY KEY, message_id BIGINT NOT NULL, revision TEXT NOT NULL,
 state TEXT NOT NULL, error_code TEXT, attachment_count INTEGER NOT NULL,
 indexed_attachment_count INTEGER NOT NULL DEFAULT 0,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ,
 UNIQUE(message_id,revision));
CREATE TABLE IF NOT EXISTS knowledge_occurrences (
 id BIGSERIAL PRIMARY KEY, run_id BIGINT NOT NULL REFERENCES knowledge_runs(id),
 attachment_id BIGINT NOT NULL,
 document_id BIGINT REFERENCES knowledge_documents(id),
 filename TEXT NOT NULL, content_type TEXT NOT NULL, storage TEXT NOT NULL,
 original_path TEXT, availability TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}',
 UNIQUE(run_id,attachment_id));
CREATE TABLE IF NOT EXISTS knowledge_entries (
 id BIGSERIAL PRIMARY KEY, run_id BIGINT NOT NULL REFERENCES knowledge_runs(id),
 occurrence_id BIGINT REFERENCES knowledge_occurrences(id),
 extraction_id BIGINT REFERENCES knowledge_extractions(id),
 source_type TEXT NOT NULL CHECK (source_type IN ('message','document')),
 source_key BIGINT NOT NULL, message_id BIGINT NOT NULL, channel TEXT NOT NULL,
 title TEXT NOT NULL, text TEXT NOT NULL, ts TIMESTAMPTZ, acquired_at TIMESTAMPTZ,
 historical BOOLEAN NOT NULL, status TEXT NOT NULL,
 source_ref JSONB NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
 search_vector TSVECTOR GENERATED ALWAYS AS
 (to_tsvector('italian'::regconfig, title || E'\n' || text)) STORED,
 UNIQUE(run_id,source_type,source_key));
CREATE INDEX IF NOT EXISTS knowledge_fts ON knowledge_entries USING gin(search_vector) WHERE active;
CREATE INDEX IF NOT EXISTS knowledge_message ON knowledge_entries(message_id) WHERE active;
CREATE INDEX IF NOT EXISTS knowledge_recent ON knowledge_entries(ts DESC,id DESC) WHERE active;
CREATE INDEX IF NOT EXISTS knowledge_runs_message ON knowledge_runs(message_id,id DESC);
CREATE INDEX IF NOT EXISTS knowledge_occurrences_document ON knowledge_occurrences(document_id);
CREATE INDEX IF NOT EXISTS knowledge_attachment_message ON attachments(message_id,id);
"""


def _limit(value, default, maximum):
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _id(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,18}', str(value)):
        raise ValueError('A positive source ID is required')
    identity = int(value)
    if identity > 9223372036854775807:
        raise ValueError('Source ID out of range')
    return identity


def _text(value, limit=MAX_TEXT):
    return str(value or '').replace('\x00', '')[:limit]


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                    ensure_ascii=True).encode()).hexdigest()


def _exists(conn, table):
    return bool(conn.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0])


def migrate(conn):
    """Additive, transactional migration; no scans/backfill or original writes."""
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations
            (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
             reset_rows BIGINT NOT NULL)''')
        applied = not bool(conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s',
                                       (VERSION,)).fetchone())
        if applied:
            conn.execute(SCHEMA)
            conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))
    return dict(version=VERSION, applied=applied, reset_rows=0)


def _snapshot(conn, message_id, data=False):
    """Bounded native snapshot. Hash uncapped text to detect edits beyond caps.

    Caller owns a short transaction; native row locks fence normal store edits.
    Enqueue checks safe path stat signatures, never reads file content. Binary
    payloads are fetched only for the worker, within a total byte budget.
    """
    message = None
    for table in TABLES:
        row = conn.execute(f'''SELECT id,channel,left(sender,240),left(subject,500),
            left(body,%s),ts,acquired_at,source_id,conversation_id,direction,
            metadata->>'revision_date',coalesce(metadata @> '{{"historical":true}}',false),
            encode(sha256(convert_to(coalesce(body,''),'UTF8')),'hex')
            FROM {table} WHERE id=%s FOR SHARE''', (MAX_TEXT + 1, message_id)).fetchone()
        if row:
            message = dict(zip(('id', 'channel', 'sender', 'subject', 'body', 'ts', 'acquired_at',
                                'source_id', 'conversation_id', 'direction', 'native_revision',
                                'historical', 'body_hash'), row))
            break
    if message is None:
        return None
    has_transcripts = _exists(conn, 'media_transcripts')
    transcript = ("left(t.text,%s),encode(sha256(convert_to(t.text,'UTF8')),'hex'),"
                  "left(t.kind,40),left(t.model,160),t.created_at"
                  if has_transcripts else 'NULL::text,NULL::text,NULL::text,NULL::text,NULL::timestamptz')
    rows = conn.execute(f'''SELECT a.id,left(a.filename,240),left(a.content_type,160),a.path,
        octet_length(a.data),
        CASE WHEN octet_length(a.data)<=%s THEN encode(sha256(a.data),'hex') END,
        NULL::bytea,
        a.metadata->'duration', {transcript},right(a.filename,32),
        encode(sha256(convert_to(coalesce(a.filename,''),'UTF8')),'hex')
        FROM attachments a {'LEFT JOIN media_transcripts t ON t.attachment_id=a.id' if has_transcripts else ''}
        WHERE a.message_id=%s ORDER BY a.id LIMIT %s FOR SHARE OF a''',
        # Placeholders for transcript come after the binary caps in SELECT.
        ([MAX_BYTES] + ([MAX_TEXT + 1] if has_transcripts else [])
         + [message_id, MAX_ATTACHMENTS])).fetchall()
    attachments = [dict(zip(('id', 'filename', 'content_type', 'path', 'byte_size',
                            'byte_hash', 'data', 'duration', 'transcript', 'transcript_hash',
                            'transcript_kind', 'transcript_model', 'transcript_created_at',
                            'filename_tail', 'filename_hash'), row)) for row in rows]
    remaining = MAX_MESSAGE_BYTES
    for attachment in attachments:
        if attachment['byte_size'] is None and attachment['path']:
            attachment['path_revision'] = _path_revision(attachment['path'])
        if data and attachment['byte_size'] is not None and attachment['byte_size'] <= MAX_BYTES:
            if attachment['byte_size'] > remaining:
                attachment['budget_exceeded'] = True
            else:
                binary = conn.execute('''SELECT data FROM attachments
                    WHERE id=%s AND octet_length(data)<=%s FOR SHARE''',
                    (attachment['id'], MAX_BYTES)).fetchone()
                attachment['data'] = binary[0] if binary else None
                remaining -= attachment['byte_size']
    count = conn.execute('SELECT count(*) FROM attachments WHERE message_id=%s',
                         (message_id,)).fetchone()[0]
    fingerprint = dict(version=VERSION, extractor=EXTRACTOR_VERSION,
                       message={k: v for k, v in message.items() if k != 'body'},
                       attachments=[{k: v for k, v in att.items()
                                     if k not in ('data', 'transcript', 'budget_exceeded')}
                                    for att in attachments], count=count)
    return dict(message=message, attachments=attachments, attachment_count=count,
                revision=_hash(fingerprint))


def _enqueue_snapshot(conn, snapshot):
    mid, revision = snapshot['message']['id'], snapshot['revision']
    row = jobs.enqueue(conn, 'knowledge', f'knowledge:{mid}:{revision}',
                       dict(message_id=mid, revision=revision, archive_only=True))
    if row is None:
        # A -> B -> A must reactivate A even if its original job is already done.
        # Also retry transient extraction failures on explicit enqueue/backfill.
        # A stale job may be done without any run (A -> B before A was parsed).
        # Never reset running/failed work, or rearm permanent extraction limits.
        row = conn.execute('''UPDATE work_jobs SET status='pending',attempts=0,
            available_at=now(),finished_at=NULL,error=NULL
            WHERE kind='knowledge' AND key=%s AND status='done'
            AND (NOT EXISTS(SELECT 1 FROM knowledge_entries e JOIN knowledge_runs r ON r.id=e.run_id
                WHERE r.message_id=%s AND r.revision=%s AND e.active)
                OR EXISTS(SELECT 1 FROM knowledge_entries e JOIN knowledge_runs r ON r.id=e.run_id
                    WHERE r.message_id=%s AND r.revision=%s AND e.active
                    AND e.source_ref->>'extraction_status' IN ('failed','changed')))
            RETURNING id''', (f'knowledge:{mid}:{revision}', mid, revision, mid, revision)).fetchone()
    return dict(message_id=mid, revision=revision, job_id=row[0] if row else None,
                queued=bool(row), archive_only=True)


def enqueue(conn, message_id):
    """Enqueue a native revision (including transcript changes), caller-atomic."""
    message_id = _id(message_id)
    with conn.transaction():
        snapshot = _snapshot(conn, message_id)
        if snapshot is None:
            return dict(message_id=message_id, queued=False, status='missing', archive_only=True)
        return _enqueue_snapshot(conn, snapshot)


class SourceError(Exception):
    """Sanitized extraction outcome, never a provider exception or physical path."""


def _selected_path(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or '\x00' in value:
        raise SourceError('blocked')
    path = Path(value)
    if '..' in path.parts:
        raise SourceError('blocked')
    path = path if path.is_absolute() else Path(config.BASE_DIR) / path
    # Do not resolve(): doing so would hide symlinks in the configured root.
    path = Path(os.path.abspath(path))
    for root, restricted in ((Path(config.INVOICE_DIR), False),
                              (Path(config.DATA_DIR), True)):
        root = root if root.is_absolute() else Path(config.BASE_DIR) / root
        if '..' in root.parts:
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not relative.parts or (restricted and relative.parts[0] not in CONTENT_DIRS):
            continue
        if any(part.startswith('.') or SECRET_NAME.search(part) for part in relative.parts):
            raise SourceError('blocked')
        return path
    raise SourceError('blocked')


@contextmanager
def _file(value):
    """Descriptor-relative traversal rejects symlinks, including root ancestors."""
    path = _selected_path(value)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                        dir_fd=fd)
        try:
            info = os.fstat(child)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SourceError('blocked')
            if info.st_size > MAX_BYTES:
                raise SourceError('too_large')
            with os.fdopen(os.dup(child), 'rb') as stream:
                yield stream
        finally:
            os.close(child)
    finally:
        os.close(fd)


def _stat_revision(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _bytes(attachment, remaining=MAX_MESSAGE_BYTES):
    if attachment.get('budget_exceeded'):
        raise SourceError('budget_exceeded')
    if attachment['byte_size'] is not None:
        if attachment['byte_size'] > MAX_BYTES:
            raise SourceError('too_large')
        if attachment['byte_size'] > remaining:
            raise SourceError('budget_exceeded')
        if attachment.get('data') is None:
            raise SourceError('missing')
        data = bytes(attachment['data'])
    elif attachment.get('path'):
        try:
            with _file(attachment['path']) as stream:
                before = _stat_revision(os.fstat(stream.fileno()))
                expected = attachment.get('path_revision')
                if expected is not None and before != expected:
                    raise SourceError('changed')
                if before[2] > remaining:
                    raise SourceError('budget_exceeded')
                data = stream.read(min(MAX_BYTES, remaining) + 1)
                if before != _stat_revision(os.fstat(stream.fileno())):
                    raise SourceError('changed')
        except SourceError:
            raise
        except FileNotFoundError:
            raise SourceError('missing') from None
        except OSError:
            raise SourceError('blocked') from None
    else:
        raise SourceError('missing')
    if len(data) > MAX_BYTES:
        raise SourceError('too_large')
    if len(data) > remaining:
        raise SourceError('budget_exceeded')
    return data


def _path_revision(value):
    try:
        with _file(value) as stream:
            info = os.fstat(stream.fileno())
            return _stat_revision(info)
    except SourceError as exc:
        return str(exc)
    except FileNotFoundError:
        return 'missing'
    except OSError:
        return 'blocked'


def _cached_extraction(digest, suffix):
    with db._conn() as conn:
        row = conn.execute('''SELECT x.text,x.status,x.error_code,x.truncated,x.limits
            FROM knowledge_extractions x JOIN knowledge_documents d ON d.id=x.document_id
            WHERE d.sha256=%s AND x.extractor_version=%s AND x.format=%s
            AND x.status IN ('ready','empty','unsupported') LIMIT 1''',
            (digest, EXTRACTOR_VERSION, suffix)).fetchone()
    return dict(zip(('text', 'status', 'error_code', 'truncated', 'limits'), row)) if row else None


def _document_text(data, filename, suffix):
    """Report parser coverage, not just the length of its final text.

    PDF parsing remains in-process (not a hard memory/CPU sandbox). No OCR,
    decryption, archive expansion or provider calls are attempted here.
    """
    limits = []
    if suffix == '.pdf':
        if not data.startswith(b'%PDF-'):
            raise ValueError('Invalid PDF')
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            return dict(text='', status='encrypted', error_code='encrypted',
                        truncated=False, limits=[])
        if len(reader.pages) > MAX_PDF_PAGES:
            limits.append('page_limit')
        text = ''
        for page in reader.pages[:MAX_PDF_PAGES]:
            contents = page.get_contents()
            if contents and len(contents.get_data()) > MAX_BYTES:
                limits.append('page_bytes_limit')
                break
            extracted = page.extract_text() or ''
            remaining = min(MAX_TEXT, documents.MAX_TEXT) - len(text)
            text += extracted[:remaining]
            if len(extracted) >= remaining:
                limits.append('text_limit')
                break
    else:
        text = documents.extract(data, filename)
        if len(text) >= min(MAX_TEXT, documents.MAX_TEXT):
            limits.append('text_limit')
    return dict(text=_text(text), status='ready' if text else 'empty',
                error_code=None, truncated=bool(limits), limits=limits)


def _extract(attachment, cache, budget, lookup=None):
    result = dict(attachment=attachment, sha256=None, size=0, text='', status='missing',
                  error_code=None, truncated=False, limits=[], format='')
    try:
        if budget[0] >= MAX_MESSAGE_BYTES:
            raise SourceError('budget_exceeded')
        data = _bytes(attachment, MAX_MESSAGE_BYTES - budget[0])
        budget[0] += len(data)
        if budget[0] > MAX_MESSAGE_BYTES:
            raise SourceError('budget_exceeded')
        digest = hashlib.sha256(data).hexdigest()
        if attachment.get('byte_hash') and attachment['byte_hash'] != digest:
            raise SourceError('changed')
        suffix = Path(attachment.get('filename_tail', attachment.get('filename')) or '').suffix.lower()
        result.update(sha256=digest, size=len(data), format=suffix)
        key = (digest, suffix)
        if key not in cache:
            cached = lookup(digest, suffix) if lookup else None
            if cached is not None:
                pass
            elif suffix not in ('.pdf', '.xml'):
                cached = dict(text='', status='unsupported', error_code=None, truncated=False)
            else:
                try:
                    cached = _document_text(data, attachment.get('filename') or '', suffix)
                except Exception:
                    cached = dict(text='', status='failed', error_code='extraction_failed', truncated=False)
            cache[key] = cached
        result.update(cache[key])
    except SourceError as exc:
        result.update(status=str(exc), error_code=str(exc))
    result['extraction_truncated'] = result['truncated']
    result['extraction_limits'] = list(result['limits'])
    # Already-persisted transcripts are occurrence-specific, never reused by hash.
    transcript = _text(attachment.get('transcript'))
    if transcript:
        combined = '\n\n'.join(t for t in (result['text'], transcript) if t)
        result['entry_text'] = combined[:MAX_TEXT]
        result['entry_status'] = 'partial' if result['status'] not in ('ready', 'unsupported', 'empty') else 'ready'
        result['truncated'] |= len(combined) > MAX_TEXT or len(attachment['transcript']) > MAX_TEXT
    else:
        result['entry_text'] = result['text']
        mime = (attachment.get('content_type') or '').lower()
        result['entry_status'] = ('pending_transcript' if result['status'] == 'unsupported'
                                  and mime.startswith(('image/', 'audio/')) else result['status'])
    result['text_parts'] = []
    extracted_chars = min(len(result['text']), len(result['entry_text']))
    if extracted_chars:
        result['text_parts'].append(dict(kind='extraction', start=0, end=extracted_chars))
    transcript_start = extracted_chars + (2 if extracted_chars and transcript else 0)
    if transcript and transcript_start < len(result['entry_text']):
        result['text_parts'].append(dict(kind='transcript', start=transcript_start,
                                        end=len(result['entry_text'])))
    return result


def _source_ref(message):
    return dict(message_id=message['id'], native_source_id=_text(message['source_id'], 500),
                conversation_id=message['conversation_id'], sender=_text(message['sender'], 240),
                direction=_text(message['direction'], 16),
                native_revision=_text(message['native_revision'], 100))


def _save_entry(conn, run_id, message, kind, key, title, text, status, ref,
                occurrence_id=None, extraction_id=None):
    conn.execute('''INSERT INTO knowledge_entries
        (run_id,occurrence_id,extraction_id,source_type,source_key,message_id,channel,
         title,text,ts,acquired_at,historical,status,source_ref)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(run_id,source_type,source_key) DO UPDATE SET
        occurrence_id=excluded.occurrence_id,extraction_id=excluded.extraction_id,
        title=excluded.title,text=excluded.text,status=excluded.status,
        source_ref=excluded.source_ref,active=true''',
        (run_id, occurrence_id, extraction_id, kind, key, message['id'], message['channel'],
         _text(title, 500), _text(text), message['ts'], message['acquired_at'],
         message['historical'], status, json.dumps(ref)))


def _publish(conn, snapshot, results):
    message, revision = snapshot['message'], snapshot['revision']
    mid = message['id']
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (f'knowledge:{mid}',))
    current = _snapshot(conn, mid)
    if current is None or current['revision'] != revision:
        if current:
            _enqueue_snapshot(conn, current)
        return dict(status='stale', message_id=mid, revision=revision)
    partial = (snapshot['attachment_count'] > len(results) or len(message['body'] or '') > MAX_TEXT
               or any(r['entry_status'] not in ('ready', 'empty', 'unsupported') or r['truncated'] for r in results))
    state = 'partial' if partial else 'indexed'
    run_id = conn.execute('''INSERT INTO knowledge_runs
        (message_id,revision,state,attachment_count,indexed_attachment_count,completed_at)
        VALUES (%s,%s,%s,%s,%s,now()) ON CONFLICT(message_id,revision) DO UPDATE SET
        state=excluded.state,error_code=NULL,indexed_attachment_count=excluded.indexed_attachment_count,
        completed_at=now() RETURNING id''',
        (mid, revision, state, snapshot['attachment_count'], len(results))).fetchone()[0]
    conn.execute('UPDATE knowledge_entries SET active=false WHERE message_id=%s AND active', (mid,))
    ref = _source_ref(message)
    _save_entry(conn, run_id, message, 'message', mid, message['subject'] or message['sender'] or 'Messaggio',
                message['body'], 'partial' if len(message['body'] or '') > MAX_TEXT else 'ready', ref)
    for result in results:
        attachment = result['attachment']
        document_id = extraction_id = None
        if result['sha256']:
            document_id = conn.execute('''INSERT INTO knowledge_documents(sha256,byte_size)
                VALUES (%s,%s) ON CONFLICT(sha256) DO UPDATE SET sha256=excluded.sha256 RETURNING id''',
                (result['sha256'], result['size'])).fetchone()[0]
            extraction_id = conn.execute('''INSERT INTO knowledge_extractions
                (document_id,extractor_version,format,status,text,error_code,truncated)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(document_id,extractor_version,format) DO UPDATE SET
                status=excluded.status,text=excluded.text,error_code=excluded.error_code,
                truncated=excluded.truncated,attempts=knowledge_extractions.attempts+1,updated_at=now()
                RETURNING id''',
                (document_id, EXTRACTOR_VERSION, result['format'], result['status'], result['text'],
                 result['error_code'], result['extraction_truncated'])).fetchone()[0]
        occurrence_id = conn.execute('''INSERT INTO knowledge_occurrences
            (run_id,attachment_id,document_id,filename,content_type,storage,original_path,availability,metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id,attachment_id) DO UPDATE SET
            document_id=excluded.document_id,availability=excluded.availability RETURNING id''',
            (run_id, attachment['id'], document_id, _text(attachment['filename'], 240),
             _text(attachment['content_type'], 160),
             'bytea' if attachment['byte_size'] is not None else 'path' if attachment['path'] else 'missing',
             attachment['path'] if result['sha256'] and attachment['byte_size'] is None else None,
             result['status'], json.dumps({'duration': attachment['duration']}
                                         if isinstance(attachment['duration'], (int, float)) else {}))).fetchone()[0]
        source = dict(ref, attachment_id=attachment['id'], content_id=document_id,
                      sha256=result['sha256'], extraction_version=EXTRACTOR_VERSION,
                      extraction_status=result['status'], error_code=result['error_code'],
                      content_type=_text(attachment['content_type'], 160),
                      byte_size=result['size'] if result['sha256'] else attachment['byte_size'],
                      transcript_revision=attachment['transcript_hash'],
                      transcript_kind=_text(attachment['transcript_kind'], 40),
                      truncated=result['truncated'])
        _save_entry(conn, run_id, message, 'document', attachment['id'], attachment['filename'] or 'Documento',
                    result['entry_text'], result['entry_status'], source, occurrence_id, extraction_id)
    return dict(status=state, message_id=mid, revision=revision, documents=len(results),
                omitted_attachments=max(0, snapshot['attachment_count'] - len(results)))


def index_message(payload):
    """Worker: bounded local extraction + atomic publication, no external calls.

    Payload text/path/authority is ignored. Stale jobs enqueue the current native
    revision instead of overwriting it. Missing/blocked/corrupt sources remain
    visible with explicit states; SQL failures roll back publication and retry.
    """
    mid = _id(payload.get('message_id'))
    with db._conn() as conn, conn.transaction():
        snapshot = _snapshot(conn, mid, data=True)
        if snapshot is None:
            conn.execute('UPDATE knowledge_entries SET active=false WHERE message_id=%s', (mid,))
            return dict(status='missing', message_id=mid)
        if payload.get('revision') and payload['revision'] != snapshot['revision']:
            _enqueue_snapshot(conn, snapshot)
            return dict(status='stale', message_id=mid, revision=payload['revision'])
        previous = conn.execute('''SELECT r.state,EXISTS(SELECT 1 FROM knowledge_entries e
            WHERE e.run_id=r.id AND e.active) FROM knowledge_runs r
            WHERE r.message_id=%s AND r.revision=%s''', (mid, snapshot['revision'])).fetchone()
        if previous and previous[0] == 'indexed' and previous[1]:
            return dict(status='duplicate', message_id=mid, revision=snapshot['revision'])
    # No transaction/connection is held while filesystem/parsers are running.
    cache, budget = {}, [0]
    results = [_extract(attachment, cache, budget, _cached_extraction)
               for attachment in snapshot['attachments']]
    with db._conn() as conn, conn.transaction():
        return _publish(conn, snapshot, results)


def backfill(batch=100, after_id=0):
    """Resume by returned after_id. Queue only; historical data stays historical."""
    batch = _limit(batch, 100, 500)
    if after_id != 0:
        after_id = _id(after_id)
    with db._conn() as conn, conn.transaction():
        rows = conn.execute('SELECT id FROM communications WHERE id>%s ORDER BY id LIMIT %s',
                            (after_id, batch + 1)).fetchall()
        selected = rows[:batch]
        queued = sum(bool(enqueue(conn, row[0])['queued']) for row in selected)
    return dict(scanned=len(selected), queued=queued, after_id=selected[-1][0] if selected else after_id,
                has_more=len(rows) > batch, archive_only=True)
