"""Durable ingestion → analysis → outbox. No external effects inside DB transactions."""
import json
from datetime import datetime
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
    return {'queued': True, 'message_id': mid}


def load_message(mid):
    with db._conn() as conn:
        row = conn.execute('SELECT channel,sender,subject,body,source_id FROM communications WHERE id=%s', (mid,)).fetchone()
        attachments = conn.execute('SELECT filename,content_type,path,data FROM attachments WHERE message_id=%s', (mid,)).fetchall()
    if not row:
        raise ValueError('Missing archived message')
    return row, [dict(zip(('filename','content_type','path','data'), a)) for a in attachments]


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
    if channel.startswith('telegram'):
        from connectors import media_text
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
        with db._conn() as conn:
            row = conn.execute("SELECT title FROM tasks WHERE id=%s AND status='open'", (payload['id'],)).fetchone()
        if row and not telegram.notify('⏰ Attività in scadenza: '+row[0], html=False):
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


def schedule():
    import invoices
    current = datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%Y-%m')
    if flows.enabled('monthly_zip'):
        for month in sorted({m['month'] for m in db.months_with_invoices() if m['month'] < current}):
            jobs.submit('action', f'zip:{month}', {'type':'zip','month':month})
    with db._conn() as conn, conn.transaction():
        for row in conn.execute("SELECT id FROM tasks WHERE status='open' AND deadline<=now()+interval '24 hours'").fetchall():
            jobs.enqueue(conn, 'action', f'task-reminder:{row[0]}', {'type':'reminder','id':row[0]})


def owner_commands(text):
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
            rows = conn.execute("SELECT id,title,deadline,source_message FROM tasks WHERE status='open' ORDER BY deadline NULLS LAST,id LIMIT 50").fetchall()
            reply = '\n'.join(f'#{r[0]} {r[1]} — {r[2] or "senza scadenza"} (fonte #{r[3]})' for r in rows) or 'Nessuna attività aperta.'
        else:
            if len(parts)<2 or not parts[1].isdigit():
                reply = 'Specificare un ID numerico.'
            elif command == '/fatto':
                row = conn.execute("UPDATE tasks SET status='done',completed_at=now() WHERE id=%s AND status='open' RETURNING id", (int(parts[1]),)).fetchone()
                reply = 'Attività completata.' if row else 'Attività non aperta.'
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