"""Durable, derived media text. Native bodies and attachments remain unchanged."""
import config
import db
import jobs
from connectors import media_text

VERSION = 'media_transcripts_v1'


def migrate(conn):
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return
        conn.execute('''CREATE TABLE IF NOT EXISTS media_transcripts (
            attachment_id BIGINT PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, text TEXT NOT NULL, model TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE INDEX IF NOT EXISTS attachments_message_media ON attachments(message_id)''')
        # Queue archived media too; imports never call providers synchronously.
        conn.execute('''INSERT INTO work_jobs(kind,key,payload)
            SELECT 'media','media:' || a.id,
                   jsonb_build_object('attachment_id',a.id)
            FROM attachments a WHERE a.channel IN ('telegram','whatsapp')
              AND (a.path IS NOT NULL OR a.data IS NOT NULL)
                AND (lower(split_part(a.content_type,';',1)) LIKE 'image/%'
                    OR lower(split_part(a.content_type,';',1)) LIKE 'audio/%')
            ON CONFLICT(key) DO NOTHING''')
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))


def enqueue_message(conn, mid):
    """Called inside storage transactions, including enrichment/replayed messages."""
    rows = conn.execute('''SELECT a.id,a.content_type FROM attachments a
        LEFT JOIN media_transcripts t ON t.attachment_id=a.id
        WHERE a.message_id=%s AND a.channel IN ('telegram','whatsapp')
          AND t.attachment_id IS NULL AND (a.path IS NOT NULL OR a.data IS NOT NULL)''',
        (mid,)).fetchall()
    for identity, mime in rows:
        if media_text.kind({'content_type': mime or ''}):
            jobs.enqueue(conn, 'media', f'media:{identity}', {'attachment_id': identity})


def process(payload):
    identity = payload['attachment_id']
    with db._conn() as conn:
        if conn.execute('SELECT 1 FROM media_transcripts WHERE attachment_id=%s', (identity,)).fetchone():
            return
        row = conn.execute('''SELECT filename,content_type,path,data,metadata
            FROM attachments WHERE id=%s''', (identity,)).fetchone()
    if not row:
        return
    attachment = dict(zip(('filename', 'content_type', 'path', 'data', 'metadata'), row))
    metadata = attachment.pop('metadata') or {}
    attachment['duration'] = metadata.get('duration')
    kind = media_text.kind(attachment)
    if not kind:
        return
    if not config.OPENROUTER_API_KEY:
        raise jobs.Deferred('OpenRouter non configurato')
    # No DB transaction held during provider calls; failed requests are retried.
    text = (media_text.convert(attachment) if kind == 'audio'
            else media_text.describe_image(attachment)).strip()
    if not text:
        raise RuntimeError('Trascrizione media vuota')
    model = config.OPENROUTER_AUDIO_MODEL if kind == 'audio' else config.OPENROUTER_MODEL
    with db._conn() as conn:
        conn.execute('''INSERT INTO media_transcripts(attachment_id,kind,text,model)
            VALUES (%s,%s,%s,%s) ON CONFLICT(attachment_id) DO NOTHING''',
            (identity, kind, text, model))


def text_for_message(conn, mid):
    rows = conn.execute('''SELECT t.kind,t.text FROM media_transcripts t
        JOIN attachments a ON a.id=t.attachment_id WHERE a.message_id=%s ORDER BY a.id''',
        (mid,)).fetchall()
    return '\n\n'.join(f'[{"Trascrizione vocale" if kind == "audio" else "Immagine: testo e descrizione"}]\n{text}'
                       for kind, text in rows)