"""Versioned native message storage; only the configured PostgreSQL is used."""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import email_attachments

VERSION = 'native_messages_v1'
TABLES = {'email': 'email_messages', 'whatsapp': 'whatsapp_messages',
          'telegram': 'telegram_messages'}


def reconciliation_excluded(conn, message_id):
    """Read durable provenance, including for pipeline callers with supplied IDs.

    The communications view does not expose metadata. Call inside the enqueue
    transaction; shared row locks fence concurrent promotion by a live edit.
    Missing IDs are not classified as historical.
    """
    for table in TABLES.values():
        row = conn.execute(f"""SELECT metadata @> '{{"historical": true}}'::jsonb
            FROM {table} WHERE id=%s FOR SHARE""", (message_id,)).fetchone()
        if row:
            return row[0] is True
    return False


def merge_whatsapp_metadata(old, incoming):
    """Names only improve; technical IDs and message bodies remain native."""
    merged = dict(old)
    for key, value in incoming.items():
        if value is not None and value != '' and key not in (
            'chat_name', 'author_name', 'chat_name_rank', 'author_name_rank',
            'historical'):
            if key.endswith('_aliases'):
                merged[key] = list(dict.fromkeys(list(old.get(key) or []) + list(value)))
            else:
                merged[key] = value
    for key in ('chat_name', 'author_name'):
        def rank(meta):
            name = str(meta.get(key) or '').strip()
            if not name or re.fullmatch(r'[+\d\s().-]+', name) or re.search(
                    r'@(lid|c\.us|s\.whatsapp\.net|g\.us)$', name):
                return 0
            return int(meta.get(key + '_rank') or 1)
        if rank(incoming) >= rank(old) and rank(incoming) > 0:
            merged[key] = incoming[key]
            merged[key + '_rank'] = rank(incoming)
        elif key not in merged and incoming.get(key):
            merged[key] = incoming[key]
    return merged


def enrich_whatsapp(conn, row, meta, attachments):
    mid, previous, sender, subject = row
    merged = merge_whatsapp_metadata(previous, meta)
    if merged != previous:
        chat = merged.get('chat_name') or merged.get('chat_id') or '?'
        author = merged.get('author_name')
        if merged.get('direction') == 'out':
            sender = f'io → {chat}'
        elif str(merged.get('chat_id', '')).endswith('@g.us'):
            sender = f'{author} in {chat}' if author else f"{chat} · {merged.get('author') or '?'}"
        else:
            sender = f"{chat} ({merged.get('chat_id') or merged.get('author') or '?'})"
        conn.execute('UPDATE whatsapp_messages SET sender=%s,subject=%s,metadata=%s WHERE id=%s',
                     (sender, f'WhatsApp · {chat}', json.dumps(merged), mid))
    # Serialized by the message lock: retries/enrichment never duplicate media.
    for att in attachments or []:
        conn.execute('''INSERT INTO attachments(message_id,channel,filename,content_type,path,data,metadata)
            SELECT %s,'whatsapp',%s,%s,%s,%s,%s WHERE NOT EXISTS
            (SELECT 1 FROM attachments WHERE message_id=%s AND
             filename IS NOT DISTINCT FROM %s AND path IS NOT DISTINCT FROM %s)''',
            (mid, att.get('filename'), att.get('content_type'), att.get('path'),
             att.get('data'), json.dumps({k: v for k, v in att.items() if k not in ('data', 'path')}),
             mid, att.get('filename'), att.get('path')))
    from connectors import media_archive
    media_archive.enqueue_message(conn, mid)
    return mid


def native(channel):
    kind = channel.split(' ', 1)[0]
    if kind not in TABLES:
        raise ValueError('Unsupported message channel')
    return kind


def migrate(conn, base_schema, approved=False):
    """DDL and reset are atomic. RESTRICT refuses external dependencies."""
    # Normal startup must not issue DDL for an already installed version.
    # Recheck under the migration lock below to handle concurrent first starts.
    if conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]:
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return 0
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(73190421)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), reset_rows BIGINT NOT NULL)")
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return 0
        kind = conn.execute("SELECT relkind FROM pg_class WHERE oid=to_regclass('communications')").fetchone()
        if kind and kind[0] != 'r':
            raise RuntimeError('Unexpected communications relation; migration refused')
        if kind:
            raise RuntimeError('Legacy communications table requires an offline additive conversion; history resets are forbidden')
        conn.execute(base_schema)
        conn.execute("""CREATE TABLE IF NOT EXISTS message_dedup (
            channel TEXT NOT NULL, source_id TEXT NOT NULL,
            seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(channel, source_id));
            CREATE SEQUENCE IF NOT EXISTS message_ids;
            CREATE TABLE IF NOT EXISTS conversations (
                id BIGSERIAL PRIMARY KEY, channel TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
                external_id TEXT NOT NULL, UNIQUE(channel, account, external_id));""")
        count = 0
        for table in TABLES.values():
            conn.execute(f"""CREATE TABLE {table} (
                id BIGINT PRIMARY KEY DEFAULT nextval('message_ids'),
                ts TIMESTAMPTZ, day DATE, acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                channel TEXT NOT NULL, sender TEXT, subject TEXT, body TEXT,
                source_id TEXT, account TEXT, folder TEXT, uidvalidity TEXT, uid TEXT,
                message_id TEXT, chat_id TEXT, author TEXT, direction TEXT,
                conversation_id BIGINT REFERENCES conversations(id), metadata JSONB NOT NULL,
                UNIQUE(channel, source_id));
                CREATE INDEX ON {table}(day);""")
        conn.execute("""CREATE TABLE message_analysis (
            message_id BIGINT PRIMARY KEY, channel TEXT NOT NULL,
            summary TEXT, category TEXT, priority TEXT,
            needs_action BOOLEAN NOT NULL DEFAULT FALSE, action TEXT,
            needs_reply BOOLEAN NOT NULL DEFAULT FALSE, reply_hint TEXT,
            notified BOOLEAN NOT NULL DEFAULT FALSE, raw_verdict JSONB,
            analyzed_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE attachments (
                id BIGSERIAL PRIMARY KEY, message_id BIGINT NOT NULL, channel TEXT NOT NULL,
                filename TEXT, content_type TEXT, path TEXT, data BYTEA,
                metadata JSONB NOT NULL DEFAULT '{}');""")
        union = ' UNION ALL '.join(f'SELECT * FROM {t}' for t in TABLES.values())
        conn.execute(f"""CREATE VIEW communications AS
            SELECT m.id, m.ts, m.day, m.channel, m.sender, m.subject, m.body,
                   a.summary, a.category, a.priority,
                   COALESCE(a.needs_action,FALSE) AS needs_action, a.action,
                   COALESCE(a.needs_reply,FALSE) AS needs_reply, a.reply_hint,
                   COALESCE(a.notified,FALSE) AS notified, a.raw_verdict,
                   m.source_id, m.acquired_at, m.account, m.folder, m.uidvalidity,
                   m.uid, m.message_id, m.chat_id, m.author, m.direction, m.conversation_id
            FROM ({union}) m LEFT JOIN message_analysis a ON a.message_id=m.id""")
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,%s)', (VERSION, count))
        return count


def store(conn, channel, sender, subject, body, ts=None, source_id=None,
        metadata=None, attachments=None, update_existing=False, reconcile=False,
        historical=False):
    """Archive atomically; explicit history stays excluded across live retries.

    reconcile=False alone is not historical provenance (pipeline storage uses
    that default too). Only a strictly newer live personal-Telegram revision
    can promote an explicitly historical identity.
    """
    meta = dict(metadata or {})
    # This reserved provenance field is controlled by the explicit store option,
    # not by transport metadata. Duplicate/enrichment paths preserve stored state.
    meta.pop('historical', None)
    if historical:
      meta['historical'] = True
    table = TABLES[native(channel)]
    dt = datetime.fromisoformat(ts) if ts else None
    if dt and dt.tzinfo is None:
        raise ValueError('Native timestamp must include timezone')
    day = dt.astimezone(ZoneInfo(config.TIMEZONE)).date() if dt else None
    with conn.transaction():
        if channel == 'whatsapp' and source_id:
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                         ('whatsapp:' + source_id,))
            existing = conn.execute('''SELECT id,metadata,sender,subject FROM whatsapp_messages
                WHERE channel=%s AND source_id=%s FOR UPDATE''', (channel, source_id)).fetchone()
            if existing:
                mid = enrich_whatsapp(conn, existing, meta, attachments)
                if reconcile and not historical and existing[1].get('historical') is not True:
                    import task_reconciliation
                    task_reconciliation.queue(conn, mid)
                return mid
        conversation = None
        external = meta.get('chat_id')  # email Message-ID is NOT a thread ID
        if external is not None:
            conversation = conn.execute("""INSERT INTO conversations(channel,account,external_id)
                VALUES (%s,%s,%s) ON CONFLICT(channel,account,external_id)
                DO UPDATE SET external_id=excluded.external_id RETURNING id""",
                (native(channel), meta.get('account') or '', str(external))).fetchone()[0]
        fields = ['account', 'folder', 'uidvalidity', 'uid', 'message_id', 'chat_id', 'author', 'direction']
        values = [str(meta[k]) if meta.get(k) is not None else None for k in fields]
        conflict = 'DO NOTHING'
        if update_existing:
            if channel != 'telegram (account)' or not source_id or not meta.get('revision_date'):
                raise ValueError('Updates require a scoped personal Telegram identity and revision')
            conflict = f'''DO UPDATE SET body=excluded.body, sender=excluded.sender,
                subject=excluded.subject,
                metadata=(excluded.metadata - 'historical') || CASE
                    WHEN {table}.metadata @> '{{"historical": true}}'::jsonb
                    AND ((excluded.metadata @> '{{"historical": true}}'::jsonb)
                         OR ({table}.metadata->>'revision_date') IS NULL
                         OR (excluded.metadata->>'revision_date')::timestamptz <=
                            ({table}.metadata->>'revision_date')::timestamptz)
                    THEN '{{"historical": true}}'::jsonb ELSE '{{}}'::jsonb END,
                author=excluded.author, direction=excluded.direction
                WHERE ({table}.metadata->>'revision_date') IS NULL OR
                (excluded.metadata->>'revision_date')::timestamptz >=
                ({table}.metadata->>'revision_date')::timestamptz'''
        row = conn.execute(f"""INSERT INTO {table}
            (ts,day,channel,sender,subject,body,source_id,conversation_id,metadata,{','.join(fields)})
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,{','.join(['%s'] * len(fields))})
            ON CONFLICT(channel,source_id) {conflict}
            RETURNING id, metadata @> '{{"historical": true}}'::jsonb""",
            (dt, day, channel, sender, subject, body, source_id, conversation,
             json.dumps(meta), *values)).fetchone()
        if not row:
            existing = conn.execute(f'''SELECT id, metadata @> '{{"historical": true}}'::jsonb
                FROM {table} WHERE channel=%s AND source_id=%s FOR SHARE''',
                (channel, source_id)).fetchone()
            mid = existing[0]
            from connectors import media_archive
            media_archive.enqueue_message(conn, mid)
            # A conditional personal-Telegram upsert returns no row for an
            # older revision. Never give that rejected edit a reconciliation key.
            if (reconcile and not historical and not update_existing
                    and not (len(existing) > 1 and existing[1] is True)):
                import task_reconciliation
                task_reconciliation.queue(conn, mid)
            return mid
        if update_existing:
            # An accepted edit can replace/remove the media. Never reuse the old
            # transcript for a different photo/voice on the same message identity.
            conn.execute('''DELETE FROM attachments WHERE message_id=%s
                AND NOT (filename=ANY(%s))''',
                (row[0], [att.get('filename') for att in attachments or []]))
        for att in attachments or []:
            path, data = att.get('path'), att.get('data')
            if native(channel) == 'email' and data is not None:
                path = email_attachments.persist(data)
                data = None
            conn.execute("""INSERT INTO attachments(message_id,channel,filename,content_type,path,data,metadata)
                SELECT %s,%s,%s,%s,%s,%s,%s WHERE NOT EXISTS (
                    SELECT 1 FROM attachments WHERE message_id=%s
                    AND filename IS NOT DISTINCT FROM %s AND path IS NOT DISTINCT FROM %s
                    AND data IS NOT DISTINCT FROM %s)""", (row[0], native(channel), att.get('filename'),
                att.get('content_type'), path, data,
                json.dumps({k: v for k, v in att.items() if k not in ('data', 'path')}),
                row[0], att.get('filename'), path, data))
        from connectors import media_archive
        if native(channel) != 'email':
            media_archive.enqueue_message(conn, row[0])
        if reconcile and not historical and not (len(row) > 1 and row[1] is True):
            import task_reconciliation
            revision = (datetime.fromisoformat(meta['revision_date'])
                        .astimezone(timezone.utc).isoformat() if update_existing else '')
            task_reconciliation.queue(conn, row[0], revision=revision)
        return row[0]


def analyze(conn, message_id, channel, verdict, notified, source_id):
    with conn.transaction():
        row = conn.execute("""INSERT INTO message_analysis
            (message_id,channel,summary,category,priority,needs_action,action,needs_reply,reply_hint,notified,raw_verdict)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(message_id) DO NOTHING RETURNING message_id""",
            (message_id, native(channel), verdict.get('summary',''), verdict.get('category',''),
             verdict.get('priority',''), bool(verdict.get('needs_action')), verdict.get('action',''),
             bool(verdict.get('needs_reply')), verdict.get('reply_hint',''), notified,
             json.dumps(verdict))).fetchone()
        if source_id:
            conn.execute('INSERT INTO message_dedup(channel,source_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                         (channel, source_id))
        return row[0] if row else None