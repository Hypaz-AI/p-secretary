"""Durable, derived media text. Native bodies and attachments remain unchanged."""
import json

import config
import db
import jobs
from connectors import media_text

VERSION = 'media_transcripts_v1'
RETRY_CHANNELS = ('telegram', 'telegram (account)', 'whatsapp')
MAX_RETRY_ATTACHMENTS = 20
MAX_CONSENT_RETRIES = 3


class Conflict(ValueError):
    """The selected native source is no longer eligible for consent recovery."""


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


def _media_allowed(conn, mid):
    """Check native provenance and BOTH explicit data grants, never payload flags.

    Lock existing grants until commit, so concurrent revocation is serialized
    with publication. Current policy denies history even with channel consent;
    archive_only is not authority to bypass that boundary.
    """
    import policy
    meta = policy.metadata_for(conn, mid)
    channel = str(meta.get('channel') or '').split(' ', 1)[0]
    conn.execute('''SELECT channel,effect FROM data_policies
        WHERE channel=%s AND effect IN ('ai_text','ai_media') FOR SHARE''', (channel,))
    media = policy.can_operate(conn, mid, 'ai_media')
    text = policy.can_operate(conn, mid, 'ai_text')
    return media and text


def _blocked(conn, identity):
    """Terminal policy outcome. Replayed archival must not create a retry loop."""
    jobs.assert_current(conn)
    current = jobs.execution_context()
    conn.execute('''UPDATE work_jobs SET status='blocked',error='media_consent_required',
        finished_at=now(),token=NULL,lease_until=NULL WHERE kind='media' AND key=%s
        AND ((%s::bigint IS NULL AND status IN ('pending','running'))
             OR (id=%s AND token=%s AND status='running'))''',
        (f'media:{identity}', current.job_id if current else None,
         current.job_id if current else None, current.token if current else None))


def retry_blocked(conn, mid, *, expected_channel):
    """Explicit local recovery, never a consent-grant/enqueue/worker side effect.

    Lock native provenance, BOTH grants, attachments and job snapshots until
    commit. Only exact consent blocks without a recorded provider attempt can
    be reset. Payload counters bound lifetime resets as well as each request;
    jobs.claim still bounds each recovered run to its normal eight attempts.
    Existing legacy consent blocks have no counter and start at zero.
    """
    if (type(mid) is not int or not 0 < mid <= 9223372036854775807
            or type(expected_channel) is not str or expected_channel not in RETRY_CHANNELS):
        raise ValueError('Invalid native media source')
    import policy
    with conn.transaction():
        jobs.assert_current(conn)
        meta = policy.metadata_for(conn, mid)
        if (meta.get('channel') != expected_channel
                or meta.get('metadata', {}).get('historical', False) is not False):
            raise Conflict('Native media source changed or historical')
        if not _media_allowed(conn, mid):
            raise Conflict('Both current media and text consent required')
        channel = expected_channel.split(' ', 1)[0]
        rows = conn.execute('''SELECT a.id,a.content_type,j.id,j.payload,j.attempts,j.finished_at
            FROM attachments a JOIN work_jobs j ON j.key='media:' || a.id
            WHERE a.message_id=%s AND a.channel=%s
              AND (a.path IS NOT NULL OR a.data IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM media_transcripts t WHERE t.attachment_id=a.id)
              AND j.kind='media' AND j.status='blocked' AND j.error='media_consent_required'
              AND j.token IS NULL AND j.lease_until IS NULL
            ORDER BY a.id LIMIT %s FOR SHARE OF a FOR UPDATE OF j''',
            (mid, channel, MAX_RETRY_ATTACHMENTS + 1)).fetchall()
        if len(rows) > MAX_RETRY_ATTACHMENTS:
            raise Conflict('Too many blocked attachments for one explicit recovery')
        queued = 0
        for identity, mime, job_id, payload, attempts, finished_at in rows:
            if (not media_text.kind({'content_type': mime or ''})
                    or type(payload) is not dict
                    or set(payload) - {'attachment_id', 'media_consent_retries'}
                    or type(payload.get('attachment_id')) is not int
                    or payload['attachment_id'] != identity
                    or not 0 <= attempts <= 8):
                continue
            retries = payload.get('media_consent_retries', 0)
            if type(retries) is not int or not 0 <= retries < MAX_CONSENT_RETRIES:
                continue
            updated = dict(payload, media_consent_retries=retries + 1)
            row = conn.execute('''UPDATE work_jobs SET status='pending',attempts=0,
                available_at=now(),finished_at=NULL,error=NULL,token=NULL,lease_until=NULL,payload=%s
                WHERE id=%s AND kind='media' AND key=%s AND status='blocked'
                  AND error='media_consent_required' AND token IS NULL AND lease_until IS NULL
                  AND attempts=%s AND payload=%s AND finished_at IS NOT DISTINCT FROM %s
                  AND NOT EXISTS (SELECT 1 FROM media_transcripts WHERE attachment_id=%s)
                RETURNING id''', (json.dumps(updated), job_id, f'media:{identity}', attempts,
                                  json.dumps(payload), finished_at, identity)).fetchone()
            if row:
                queued += 1
                conn.execute('''INSERT INTO effect_audit(message_id,effect,allowed,reason)
                    VALUES (%s,'ai_media',true,'media_consent_retry')''', (mid,))
        jobs.assert_current(conn)
        return dict(message_id=mid, channel=expected_channel, queued=queued,
                    automatic_retry=False, attachment_limit=MAX_RETRY_ATTACHMENTS,
                    retry_limit=MAX_CONSENT_RETRIES)


def _attachment(conn, identity):
    return conn.execute('''SELECT filename,content_type,path,data,metadata,message_id
        FROM attachments WHERE id=%s FOR SHARE''', (identity,)).fetchone()


def process(payload):
    identity = payload['attachment_id']
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        if conn.execute('SELECT 1 FROM media_transcripts WHERE attachment_id=%s', (identity,)).fetchone():
            return
        row = _attachment(conn, identity)
        if not row:
            return
        attachment = dict(zip(('filename', 'content_type', 'path', 'data', 'metadata'), row[:5]))
        metadata = attachment.pop('metadata') or {}
        attachment['duration'] = metadata.get('duration')
        kind = media_text.kind(attachment)
        if not kind:
            return
        if not _media_allowed(conn, row[5]):
            _blocked(conn, identity)
            return
        if not config.OPENROUTER_API_KEY:
            raise jobs.Deferred('OpenRouter non configurato')
        jobs.assert_current(conn)
        current = jobs.execution_context()
        if current is not None:
            # Commit before I/O: a later consent block must not erase evidence
            # of a possibly attempted provider call, including ambiguous failures.
            marked = conn.execute('''UPDATE work_jobs SET payload=payload ||
                '{"media_provider_attempted":true}'::jsonb WHERE id=%s AND token=%s
                AND kind='media' AND key=%s AND status='running'
                AND lease_until>clock_timestamp() RETURNING id''',
                (current.job_id, current.token, f'media:{identity}')).fetchone()
            if not marked:
                raise jobs.LeaseLost('Media execution no longer current')
    # No DB transaction held during provider calls; failed requests are retried.
    text = (media_text.convert(attachment) if kind == 'audio'
            else media_text.describe_image(attachment)).strip()
    if not text:
        raise RuntimeError('Trascrizione media vuota')
    model = config.OPENROUTER_AUDIO_MODEL if kind == 'audio' else config.OPENROUTER_MODEL
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        current = _attachment(conn, identity)
        if current is None:
            return  # A live edit removed/replaced this attachment during inference.
        if current != row:
            raise jobs.Deferred('Media modificato durante la conversione')
        if not _media_allowed(conn, row[5]):
            _blocked(conn, identity)
            return
        conn.execute('''INSERT INTO media_transcripts(attachment_id,kind,text,model)
            VALUES (%s,%s,%s,%s) ON CONFLICT(attachment_id) DO NOTHING''',
            (identity, kind, text, model))
        import message_store
        message_store.enqueue_derived(conn, row[5])
        jobs.assert_current(conn)


def text_for_message(conn, mid):
    rows = conn.execute('''SELECT t.kind,t.text FROM media_transcripts t
        JOIN attachments a ON a.id=t.attachment_id WHERE a.message_id=%s ORDER BY a.id''',
        (mid,)).fetchall()
    return '\n\n'.join(f'[{"Trascrizione vocale" if kind == "audio" else "Immagine: testo e descrizione"}]\n{text}'
                       for kind, text in rows)