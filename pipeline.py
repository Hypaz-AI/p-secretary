"""Durable ingestion → analysis → outbox. No external effects inside DB transactions."""
import json
from datetime import datetime, time
from zoneinfo import ZoneInfo

import brain
import config
import db
import flows
import jobs
from connectors import gcal, telegram


def ingest(channel, sender, subject, body, state=None, attachments=None,
           account=None, uid=None, msg_date=None, source_id=None, metadata=None,
           message_id=None):
    with db._conn() as conn, conn.transaction():
        mid = message_id or db.message_store.store(conn, channel, sender, subject, body,
            msg_date, source_id, metadata, attachments)
        jobs.enqueue(conn, 'analysis', f'analysis:{mid}', {'message_id': mid})
        import task_reconciliation
        task_reconciliation.queue(conn, mid)
    return {'queued': True, 'message_id': mid}


def load_message(mid):
    with db._conn() as conn:
        row = conn.execute('SELECT channel,sender,subject,body,source_id FROM communications WHERE id=%s', (mid,)).fetchone()
        attachments = conn.execute('''SELECT a.filename,a.content_type,a.path,a.data,a.metadata,
            a.id,t.text FROM attachments a LEFT JOIN media_transcripts t ON t.attachment_id=a.id
            WHERE a.message_id=%s ORDER BY a.id''', (mid,)).fetchall()
    if not row:
        raise ValueError('Missing archived message')
    result = []
    for attachment in attachments:
        item = dict(zip(('filename','content_type','path','data'), attachment))
        metadata = attachment[4] if len(attachment) > 4 else None
        if isinstance(metadata, dict) and 'duration' in metadata:
            item['duration'] = metadata['duration']
        if len(attachment) > 5:
            item['id'] = attachment[5]
            item['transcript'] = attachment[6]
        result.append(item)
    return row, result


def merge_agenda(remote, local):
    ids = {a.get('event_id') for a in remote if a.get('event_id')}
    return sorted(remote + [a for a in local if not a.get('event_id') or a['event_id'] not in ids], key=lambda a:a['dt'])


def analyze(payload):
    mid = payload['message_id']
    (channel, sender, subject, body, source_id), attachments = load_message(mid)
    flow = 'email' if channel.startswith('email') else 'whatsapp' if channel == 'whatsapp' else 'telegram_bot'
    if not flows.enabled(flow):
        raise jobs.Deferred('Source paused')
    with db._conn() as conn:
        if conn.execute('SELECT 1 FROM message_analysis WHERE message_id=%s', (mid,)).fetchone():
            return
    if channel.startswith('telegram') or channel == 'whatsapp':
        from connectors import media_text
        # Archived media must be converted durably before semantic analysis.
        body = media_text.message_text(body, attachments)
    verdict = brain.normalize_verdict(brain.analyze(subject, sender, body, channel, attachments))
    if verdict['category'] == 'errore':
        raise RuntimeError('Analysis unavailable')
    from main import format_verdict
    notify = verdict['urgent'] or verdict['severity'] == 'alta' or config.priority_at_least(verdict['priority'], config.NOTIFY_PRIORITY_THRESHOLD)
    with db._conn() as conn, conn.transaction():
        # Serializes analysis commit, including concurrent lease recovery.
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (mid,))
        if conn.execute('SELECT 1 FROM message_analysis WHERE message_id=%s', (mid,)).fetchone():
            return
        db.message_store.analyze(conn, mid, channel, verdict, False, source_id)
        if notify:
            jobs.enqueue(conn, 'action', f'notify:{mid}', {'type':'notify', 'message_id':mid,
                'text':format_verdict(channel, sender, subject, verdict)})
        if verdict['needs_action'] and verdict['action'].strip():
            deadline = None
            try:
                deadline = datetime.fromisoformat(verdict.get('action_deadline') or '')
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=ZoneInfo(config.TIMEZONE))
            except (ValueError, TypeError):
                pass
            conn.execute('''INSERT INTO tasks(source_message,title,deadline) VALUES (%s,%s,%s)
                ON CONFLICT(source_message) DO NOTHING''', (mid, verdict['action'], deadline))
        for index, event in enumerate(verdict['appointments']):
            try:
                event = gcal.validate_event(event)
            except ValueError:
                continue
            # Sender labels are not authenticated identities. External auto-create
            # requires explicit configuration AND an allowlisted exact sender.
            trusted = config.get_bool('CALENDAR_EXTERNAL_AUTO') and sender in [s.strip() for s in config.get('CALENDAR_TRUSTED_SENDERS', '').split(',') if s.strip()]
            save_event(conn, event, f'message:{mid}:{index}', f'{channel}: {sender}', trusted)
        if verdict['is_invoice'] and attachments:
            jobs.enqueue(conn, 'action', f'invoice:{mid}', {'type':'invoice', 'message_id':mid, 'verdict':verdict})


def save_event(conn, event, key, source, approved=True):
    row = conn.execute('''INSERT INTO appointments(title,dt,location,notes,source,sync_key,event_id,sync_state,event_data)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(sync_key) DO UPDATE SET sync_key=excluded.sync_key RETURNING id''',
        (event['title'], event['dt'], event.get('location',''), event.get('notes',''), source,
         key, jobs.event_id(key), 'pending' if approved else 'proposal', json.dumps(event))).fetchone()
    if approved:
        jobs.enqueue(conn, 'action', f'calendar:{key}', {'type':'calendar', 'id':row[0]})
    return row[0]


def action(payload):
    kind = payload['type']
    if kind == 'notify':
        if payload.get('flow') and not flows.enabled(payload['flow']):
            raise jobs.Deferred('Notification flow paused')
        if payload.get('owner_reply_mid') is not None:
            key = f"owner-reply:{payload['owner_reply_mid']}:{payload['owner_reply_chunk']}"
            with db._conn() as conn:
                # Include backoff/leased replies, but exclude this job and terminal failures.
                if conn.execute("""SELECT 1 FROM work_jobs WHERE kind='action' AND key LIKE %s
                    AND id < (SELECT id FROM work_jobs WHERE key=%s)
                    AND status IN ('pending','running') LIMIT 1""",
                    ('owner-reply:%', key)).fetchone():
                    raise jobs.Deferred('Earlier owner reply still delivering')
        if not telegram.notify(payload['text'], html=payload.get('html', True)):
            raise RuntimeError('Notification not acknowledged')
        if payload.get('message_id'):
            with db._conn() as conn:
                conn.execute('UPDATE message_analysis SET notified=TRUE WHERE message_id=%s', (payload['message_id'],))
        if payload.get('digest_day'):
            jobs.set_cursor('last_digest_sent_day', payload['digest_day'])
            flows.mark('digest','sent_today')
    elif kind == 'calendar':
        if not gcal.is_active():
            raise jobs.Deferred('Calendar paused or unavailable')
        with db._conn() as conn:
            row = conn.execute('SELECT event_id,event_data,sync_state FROM appointments WHERE id=%s', (payload['id'],)).fetchone()
        if not row or row[2] in ('proposal','synced','cancelled'):
            return
        if row[2] == 'cancel_pending':
            result = gcal.delete_event(row[0])
            state = 'cancelled'
        else:
            ev = row[1]
            result = gcal.create_event(ev['title'], ev['dt'], location=ev.get('location',''),
                notes=ev.get('notes',''), duration_min=ev.get('duration_min'),
                reminder_minutes=ev.get('reminder_minutes'), event_id=row[0])
            state = 'synced'
        if not result.get('ok'):
            raise RuntimeError('Calendar sync failed')
        with db._conn() as conn:
            conn.execute('UPDATE appointments SET sync_state=%s WHERE id=%s AND sync_state=%s', (state,payload['id'],row[2]))
    elif kind == 'invoice':
        if not flows.enabled('invoices'):
            raise jobs.Deferred('Invoices paused')
        (channel,sender,subject,_,_), attachments = load_message(payload['message_id'])
        import invoices
        info = payload['verdict'].get('invoice_info') or {}
        for att in attachments:
            data = att['data']
            if data is None and att['path']:
                from pathlib import Path
                data = Path(att['path']).read_bytes()
            if data is not None:
                invoices.save_invoice(info.get('entity'), att['filename'], bytes(data), sender,
                    subject, channel, info.get('date'), info.get('description'), info.get('document_number'))
    elif kind == 'reminder':
        import task_reconciliation
        with db._conn() as conn:
            if not task_reconciliation.reminder_is_current(conn, payload):
                return
            if payload.get('owner_reminder'):
                row = conn.execute("""SELECT title,deadline,owner_deadline_date,owner_remind_at FROM tasks
                    WHERE id=%s AND status='open' AND version=%s""",
                    (payload['id'], payload['version'])).fetchone()
            else:
                row = conn.execute("SELECT title FROM tasks WHERE id=%s AND status='open'", (payload['id'],)).fetchone()
        prefix = '⏰ Promemoria attività: ' if payload.get('owner_reminder') else '⏰ Attività in scadenza: '
        label = (' — ' + owner_task_schedule_label(dict(zip(
            ('title', 'deadline', 'owner_deadline_date', 'owner_remind_at'), row)))
                 if row and payload.get('owner_reminder') else '')
        if row and not telegram.notify(prefix+row[0]+label, html=False):
            raise RuntimeError('Reminder not acknowledged')
    elif kind == 'zip':
        if not flows.enabled('monthly_zip'):
            raise jobs.Deferred('ZIP paused')
        import invoices
        report = invoices.send_monthly_zips(payload['month'])
        if any('fallito' in value for value in report.values()):
            raise RuntimeError('ZIP send failed')
    else:
        raise ValueError('Unknown action')


def owner_reminder_time(task):
    """Explicit reminder wins; otherwise default to local 09:00 or the deadline."""
    if task.get('owner_remind_at') is not None:
        return task['owner_remind_at']
    if task.get('owner_deadline_date') is not None:
        return datetime.combine(task['owner_deadline_date'], time(9), ZoneInfo(config.TIMEZONE))
    return task.get('deadline')


def is_owner_task(task):
    return any(task.get(field) is not None for field in (
        'owner_origin_message', 'owner_operation_key', 'owner_deadline_date', 'owner_remind_at'))


def enqueue_owner_reminder(conn, task):
    """Caller holds the open task row lock; never rearm a delivered version."""
    remind_at = owner_reminder_time(task)
    if remind_at is None:
        return
    key = f"owner-reminder:{task['id']}:v{task['version']}"
    payload = {'type': 'reminder', 'id': task['id'], 'version': task['version'],
               'owner_reminder': True, 'remind_at': remind_at.isoformat()}
    if jobs.enqueue(conn, 'action', key, payload):
        conn.execute('UPDATE work_jobs SET available_at=GREATEST(now(),%s::timestamptz) WHERE key=%s',
                     (remind_at, key))


def owner_task_schedule_label(task, now=None):
    """Presentation only: a default reminder is not a timed deadline."""
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    parts = []
    day = task.get('owner_deadline_date')
    if day is not None:
        parts.append(f'{day.isoformat()} (solo data)'
                     + (' — arretrata' if day < now.astimezone(ZoneInfo(config.TIMEZONE)).date() else ''))
    elif task.get('deadline') is not None:
        parts.append(task['deadline'].astimezone(ZoneInfo(config.TIMEZONE)).isoformat())
    else:
        parts.append('senza scadenza')
    reminder = owner_reminder_time(task)
    if reminder is not None:
        if task.get('owner_remind_at') is not None:
            label = 'promemoria richiesto'
        elif day is not None:
            label = 'promemoria predefinito (default 09:00 locale)'
        else:
            label = 'promemoria predefinito (default alla scadenza)'
        parts.append(f'{label}: {reminder.astimezone(ZoneInfo(config.TIMEZONE)).isoformat()}')
    return '; '.join(parts)


def schedule():
    import invoices
    current = datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%Y-%m')
    if flows.enabled('monthly_zip'):
        for month in sorted({m['month'] for m in db.months_with_invoices() if m['month'] < current}):
            jobs.submit('action', f'zip:{month}', {'type':'zip','month':month})
    with db._conn() as conn, conn.transaction():
        for row in conn.execute("""SELECT id,version,owner_deadline_date,owner_remind_at,deadline FROM tasks
            WHERE status='open' AND (owner_deadline_date IS NOT NULL OR owner_remind_at IS NOT NULL
                OR (deadline IS NOT NULL AND
                    (owner_origin_message IS NOT NULL OR owner_operation_key IS NOT NULL)))
            FOR UPDATE""").fetchall():
            enqueue_owner_reminder(conn, dict(zip(
            ('id', 'version', 'owner_deadline_date', 'owner_remind_at', 'deadline'), row)))
        for row in conn.execute("""SELECT id,deadline FROM tasks WHERE status='open'
            AND owner_origin_message IS NULL AND owner_operation_key IS NULL
            AND owner_deadline_date IS NULL AND owner_remind_at IS NULL
            AND deadline<=now()+interval '24 hours' FOR UPDATE""").fetchall():
            jobs.enqueue(conn, 'action', f'task-reminder:{row[0]}',
                         {'type':'reminder','id':row[0], 'deadline':row[1].isoformat()})


def owner_commands(text):
    import task_reconciliation
    if task_reconciliation.owner_commands(text):
        return True
    parts = text.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ''
    if command not in ('/attivita','/fatto','/conferma','/annulla','/task'):
        return False
    with db._conn() as conn, conn.transaction():
        if command == '/task':
            # Explicit syntax, no AI guessing: /task title | ISO timestamp
            title, _, raw_deadline = (parts[1] if len(parts)>1 else '').partition('|')
            deadline = None
            try:
                if not title.strip():
                    raise ValueError('Missing title')
                if raw_deadline.strip():
                    deadline = datetime.fromisoformat(raw_deadline.strip())
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=ZoneInfo(config.TIMEZONE))
                row = conn.execute('INSERT INTO tasks(title,deadline) VALUES (%s,%s) RETURNING id', (title.strip(),deadline)).fetchone()
                reply = f'Attività #{row[0]} salvata.'
            except ValueError:
                reply = 'Usare /task titolo | YYYY-MM-DDTHH:MM+02:00 (scadenza facoltativa).'
        elif command == '/attivita':
            rows = conn.execute("""SELECT id,title,deadline,source_message,owner_deadline_date,owner_remind_at,
                owner_origin_message FROM tasks WHERE status='open'
                ORDER BY COALESCE(owner_deadline_date, (deadline AT TIME ZONE %s)::date) NULLS LAST,id LIMIT 50""",
                (config.TIMEZONE,)).fetchall()
            tasks = [dict(zip(('id', 'title', 'deadline', 'source_message', 'owner_deadline_date',
                              'owner_remind_at', 'owner_origin_message'), row)) for row in rows]
            reply = '\n'.join(f"#{t['id']} {t['title']} — {owner_task_schedule_label(t)} "
                              f"(fonte #{t.get('owner_origin_message') or t['source_message']})"
                              for t in tasks) or 'Nessuna attività aperta.'
        else:
            if len(parts)<2 or not parts[1].isdigit():
                reply = 'Specificare un ID numerico.'
            elif command == '/fatto':
                closed = task_reconciliation.close_task(conn, int(parts[1]))
                reply = 'Attività completata.' if closed else 'Attività non aperta.'
            else:
                identity = int(parts[1])
                target = 'pending' if command == '/conferma' else 'cancel_pending'
                allowed = ('proposal',) if command == '/conferma' else ('proposal','local','pending','synced')
                row = conn.execute("UPDATE appointments SET sync_state=%s,done=%s WHERE id=%s AND sync_state=ANY(%s) RETURNING id", (target, command=='/annulla', identity,list(allowed))).fetchone()
                if row:
                    jobs.enqueue(conn, 'action', f'{command}:{identity}', {'type':'calendar','id':identity})
                reply = 'Operazione in coda.' if row else 'Appuntamento non trovato.'
    telegram.notify(reply, html=False)
    return True