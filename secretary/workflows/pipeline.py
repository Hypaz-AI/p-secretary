"""Durable ingestion → analysis → outbox. No external effects inside DB transactions."""
import json
import hashlib
import re
import uuid
from urllib.parse import quote
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from secretary.core import config, db, flows, jobs, capabilities, policy
from secretary.connectors import gcal, telegram


def ingest(channel, sender, subject, body, state=None, attachments=None,
           account=None, uid=None, msg_date=None, source_id=None, metadata=None,
           message_id=None):
    queued = False
    with db._conn() as conn, conn.transaction():
        mid = message_id or db.message_store.store(conn, channel, sender, subject, body,
            msg_date, source_id, metadata, attachments)
        jobs.assert_current(conn)
        # Native provenance is the central barrier, not ingress/model flags.
        if policy.can_operate(conn, mid, 'extract'):
            jobs.enqueue(conn, 'analysis', f'analysis:{mid}', {'message_id': mid})
            queued = True
        jobs.assert_current(conn)
    return {'queued': queued, 'message_id': mid}


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


def _event_origin(conn, key):
    match = re.fullmatch(r'(?:owner|message):(\d+):.*', key or '')
    if match:
        return int(match[1])
    match = re.fullmatch(r'task:(\d+)(?::v\d+)?', key or '')
    if match:
        row = conn.execute('SELECT COALESCE(owner_origin_message,source_message) FROM tasks WHERE id=%s',
                           (int(match[1]),)).fetchone()
        return row[0] if row else None
    return None


def save_event(conn, event, key, source, approved=True, calendar_intent=None):
    """Local proposal first; only a payload-bound CURRENT owner grant queues I/O.

    Natural owner adapter: pass approved=True ONLY for its current event plan,
    with calendar_intent = the intent flag classified at preparation time and
    persisted in the dialogue plan. /conferma and /annulla must call
    policy.grant_calendar on the existing row (explicit commands need no flag).
    Task/calendar origin alone (including a previously approved task) is never
    sufficient to authorize a replacement or cancellation.
    An invalid or unconfigured seal snapshot leaves the proposal local.
    """
    jobs.assert_current(conn)
    origin = _event_origin(conn, key)
    row = conn.execute('''INSERT INTO appointments(title,dt,location,notes,source,sync_key,event_id,sync_state,event_data)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(sync_key) DO UPDATE SET sync_key=excluded.sync_key RETURNING id''',
        (event['title'], event['dt'], event.get('location',''), event.get('notes',''), source,
         key, jobs.event_id(key), 'proposal', json.dumps(event))).fetchone()
    if (approved and origin and calendar_intent is True
            and re.fullmatch(rf'owner:{origin}:event:\d+', key)):
        try:
            expected = policy.calendar_event_hash(conn, row[0])
        except ValueError:
            return row[0]
        policy.grant_calendar(conn, row[0], expected, origin, calendar_intent=calendar_intent)
    return row[0]


class EffectUncertain(RuntimeError):
    """A non-idempotent attempt requires manual reconciliation, not a resend."""


def _effect(key, mid, effect, payload, callback, *, deterministic=False,
            authorization=None, system=None, scope=None):
    """Commit authorization/attempt BEFORE I/O; accepted is not verified delivery.

    Only deterministic Calendar IDs and content-addressed local invoice writes
    can obtain a fresh retry receipt. Telegram/ZIP timeouts never blindly resend.
    """
    key = key if len(key) <= 150 else 'effect:' + jobs.event_id(key)
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        def prepare(receipt_key):
            if system is not None:
                return policy.prepare_system_effect(conn, receipt_key, effect, payload, system=system, scope=scope)
            return policy.prepare_effect(conn, receipt_key, mid, effect, payload, authorization=authorization)
        allowed = prepare(key)
        if not allowed:
            row = conn.execute('SELECT status FROM effect_receipts WHERE key=%s', (key,)).fetchone()
            if row and row[0] in ('accepted', 'verified'):
                return True
            if row and row[0] in ('attempted', 'uncertain', 'failed'):
                if not deterministic:
                    raise EffectUncertain('Effect needs reconciliation')
                context = jobs.execution_context()
                key += ':retry:' + jobs.event_id(context.token if context else uuid.uuid4().hex)[:32]
                if not prepare(key):
                    raise EffectUncertain('Retry receipt is not executable')
            else:
                # A policy denial is monotonic: missing native provenance or a
                # revoked grant can never become consent by waiting. Fail the
                # job visibly instead of deferring forever.
                raise jobs.Permanent('Effect policy denied')
        if not policy.record_receipt(conn, key, 'attempted'):
            raise jobs.Deferred('Effect authorization changed')
        jobs.assert_current(conn)
    try:
        jobs.assert_current()
        # Recheck consent after committing the attempt, immediately before HTTP.
        with db._conn() as conn, conn.transaction():
            if not policy.effect_allowed(conn, mid, effect, payload, authorization=authorization):
                raise jobs.Permanent('Effect policy revoked')
        jobs.assert_current()
        if not callback():
            raise EffectUncertain('Remote outcome is not acknowledged')
    except Exception:
        with db._conn() as conn:
            policy.record_receipt(conn, key, 'uncertain')
        raise
    with db._conn() as conn:
        if not policy.record_receipt(conn, key, 'accepted'):
            raise EffectUncertain('Effect result could not be fenced')
    return True


def _notification_key(payload):
    if payload.get('system') == 'digest':
        return 'digest:' + str(payload.get('digest_day')) + ':' + policy.payload_hash(dict(
            text=payload['text'], html=payload.get('html', True), recipient=str(config.TELEGRAM_CHAT_ID)))
    if payload.get('owner_reply_mid') is not None:
        return f"owner-reply:{payload['owner_reply_mid']}:{payload['owner_reply_chunk']}"
    context = jobs.execution_context()
    return ('notification-job:' + str(context.job_id) if context else
            'notification:' + jobs.event_id(json.dumps(payload, sort_keys=True)))


def _calendar_io(payload, operation='create'):
    """Use ONLY the sealed destination/body, including conflict reconciliation.

    The legacy create/delete wrappers resolve mutable configuration internally,
    after our last fence (and again on 409). Do not use them for granted work.
    _api still owns authentication and HTTP, but cannot change this path/body.
    operation (create/cancel/update) è fornita dal chiamante sigillato, mai
    dedotta dal payload o dal modello.
    """
    target, event_id = payload['target'], payload['event_id']
    path = '/calendars/' + quote(target['calendar_id'], safe='') + '/events'
    event_path = path + '/' + quote(event_id, safe='')
    if payload['state'] == 'cancelled':
        try:
            gcal._api('DELETE', event_path)
        except RuntimeError as exc:
            if not str(exc).startswith(('Google Calendar API 404:', 'Google Calendar API 410:')):
                raise
        return True
    event = payload['event']
    body = dict(id=event_id, summary=event['title'],
        start=dict(dateTime=target['start'], timeZone=target['timezone']),
        end=dict(dateTime=target['end'], timeZone=target['timezone']),
        reminders=dict(useDefault=False, overrides=[dict(method='popup', minutes=target['reminder_minutes'])]))
    if event.get('location'):
        body['location'] = event['location']
    description = (event.get('notes') or '').strip()
    if description:
        body['description'] = description
    # update: l'evento esiste già (synced); PATCH con il corpo sigillato.
    # Il 404/410 qui è un fallimento, mai una ricreazione silenziosa.
    if operation == 'update':
        try:
            result = gcal._api('PATCH', event_path, body=body)
        except RuntimeError as exc:
            if str(exc).startswith(('Google Calendar API 404:', 'Google Calendar API 410:')):
                return False
            raise
        return result.get('id') == event_id
    try:
        result = gcal._api('POST', path, body=body)
    except RuntimeError as exc:
        if not str(exc).startswith('Google Calendar API 409:'):
            raise
        result = gcal._api('GET', event_path)
        # A deterministic ID alone does not prove this event's content. Never
        # acknowledge a cancelled, unrelated or modified event as this write.
        if (result.get('status') == 'cancelled'
                or any(result.get(key) != body[key] for key in ('id', 'summary', 'start', 'end', 'reminders'))
                or any((result.get(key) or '') != body.get(key, '') for key in ('location', 'description'))):
            return False
    return result.get('id') == event_id


def action(payload):
    jobs.assert_current()
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
        mid = (payload.get('owner_reply_mid') or payload.get('evidence_message_id')
               or payload.get('message_id'))
        def deliver():
            jobs.assert_current()
            return telegram.notify(payload['text'], html=payload.get('html', True))
        _effect(_notification_key(payload), mid, 'notify',
                dict(text=payload['text'], html=payload.get('html', True),
                 recipient=str(config.TELEGRAM_CHAT_ID)), deliver,
            authorization=payload.get('system_authorization'), system=payload.get('system'),
            scope=payload.get('digest_day'))
        if payload.get('message_id'):
            with db._conn() as conn, conn.transaction():
                jobs.assert_current(conn)
                conn.execute('UPDATE message_analysis SET notified=TRUE WHERE message_id=%s', (payload['message_id'],))
        elif payload.get('evidence_message_id'):
            # Proposal notifications carry their evidence provenance separately:
            # they never mark a verdict notification as delivered.
            pass
        if payload.get('digest_day'):
            jobs.assert_current()
            jobs.set_cursor('last_digest_sent_day', payload['digest_day'])
            flows.mark('digest','sent_today')
    elif kind == 'calendar':
        with db._conn() as conn, conn.transaction():
            jobs.assert_current(conn)
            row = conn.execute('SELECT event_id,event_data,sync_state,sync_key FROM appointments WHERE id=%s', (payload['id'],)).fetchone()
            origin = payload.get('authority_message_id')
            authorization = payload.get('authorization')
        if not row or row[2] in ('proposal','synced','cancelled'):
            return
        if not row[0] or not re.fullmatch('[0-9a-f]{64}', row[0]):
            raise jobs.Deferred('Calendar requires a deterministic event ID')
        # cancel_pending=annulla; update SOLO se dichiarato dal job (un evento
        # già synced che riceve un nuovo grant pending lo riporta a pending);
        # altrimenti create. Il grant sigilla l'operazione esatta: mai dedotta
        # dal solo sync_state. Un evento già synced non è più lavorabile: il
        # retry dopo il successo resta un no-op silenzioso.
        if row[2] == 'cancel_pending':
            operation = 'cancel'
        elif payload.get('operation') == 'update' and row[2] == 'pending':
            operation = 'update'
        else:
            operation = 'create'
        try:
            effect_payload = policy.calendar_payload(row, operation)
        except ValueError:
            raise jobs.Deferred('Calendar target unavailable') from None
        # operation viaggia separato dal payload sigillato: il digest resta
        # quello del payload calendar, l'operazione è letta dal job payload.
        calendar_operation = operation
        # Deny changed/legacy/expired/native-revoked grants before readiness:
        # readiness resolves credentials, while authorization is entirely local.
        with db._conn() as conn, conn.transaction():
            if not policy.effect_allowed(conn, origin, 'calendar', effect_payload,
                                         authorization=authorization):
                raise jobs.Deferred('Calendar authorization changed')
        def assert_target():
            try:
                sealed = policy.calendar_payload(row, operation)
                if all(effect_payload.get(key) == sealed.get(key) for key in sealed):
                    return
            except ValueError:
                pass
            raise jobs.Deferred('Calendar target changed')
        def sync():
            assert_target()
            if not gcal.is_active():
                raise jobs.Deferred('Calendar paused or unavailable')
            with db._conn() as conn, conn.transaction():
                jobs.assert_current(conn)
                current = conn.execute('SELECT event_id,event_data,sync_state,sync_key FROM appointments WHERE id=%s',
                                       (payload['id'],)).fetchone()
                if current != row or not policy.effect_allowed(conn, origin, 'calendar',
                    effect_payload, authorization=authorization):
                    raise jobs.Deferred('Calendar proposal changed')
            # Re-read local config after DB/lease checks, at the connector edge.
            jobs.assert_current()
            assert_target()
            return _calendar_io(effect_payload, calendar_operation)
        state = effect_payload['state']
        # Receipts belong to this sealed grant, not another destination's
        # identical deterministic event ID (or an unbound pre-v3 receipt).
        _effect(f'{authorization}:{state}', origin, 'calendar',
            effect_payload, sync, deterministic=True, authorization=authorization)
        with db._conn() as conn, conn.transaction():
            jobs.assert_current(conn)
            # A new approval can supersede this grant while HTTP is in flight,
            # even with identical event JSON/state but a different destination.
            # Lock/recheck the original grant before publishing local success.
            if not policy.effect_allowed(conn, origin, 'calendar', effect_payload,
                                         authorization=authorization):
                raise jobs.Deferred('Calendar authorization changed after I/O')
            conn.execute('''UPDATE appointments SET sync_state=%s WHERE id=%s AND sync_state=%s
                AND event_id=%s AND event_data=%s::jsonb AND sync_key IS NOT DISTINCT FROM %s''',
                (state, payload['id'], row[2], row[0], json.dumps(row[1]), row[3]))
    elif kind == 'invoice':
        if not capabilities.enabled('invoices') or not flows.enabled('invoices'):
            raise jobs.Deferred('Invoices paused')
        with db._conn() as conn, conn.transaction():
            jobs.assert_current(conn)
            if not policy.can_operate(conn, payload['message_id'], 'invoice'):
                return
        (channel,sender,subject,_,_), attachments = load_message(payload['message_id'])
        from secretary.workflows import invoices
        info = payload['verdict'].get('invoice_info') or {}
        for att in attachments:
            data = att['data']
            if data is None and att['path']:
                from pathlib import Path
                data = Path(att['path']).read_bytes()
            if data is not None:
                data = bytes(data)
                digest = hashlib.sha256(data).hexdigest()
                def archive():
                    if not capabilities.enabled('invoices'):
                        raise jobs.Deferred('Invoices disabled')
                    jobs.assert_current()
                    return invoices.save_invoice(info.get('entity'), att['filename'], data, sender,
                        subject, channel, info.get('date'), info.get('description'), info.get('document_number'))
                _effect(f"invoice:{payload['message_id']}:{digest}", payload['message_id'], 'invoice',
                        dict(sha256=digest, filename=att['filename'], info=info), archive, deterministic=True)
    elif kind == 'reminder':
        from secretary.workflows import task_reconciliation
        with db._conn() as conn:
            if not task_reconciliation.reminder_is_current(conn, payload):
                return
            if payload.get('owner_reminder'):
                row = conn.execute("""SELECT title,deadline,owner_deadline_date,owner_remind_at FROM tasks
                    WHERE id=%s AND status='open' AND version=%s""",
                    (payload['id'], payload['version'])).fetchone()
            else:
                row = conn.execute("SELECT title FROM tasks WHERE id=%s AND status='open'", (payload['id'],)).fetchone()
            origin = conn.execute('SELECT COALESCE(owner_origin_message,source_message) FROM tasks WHERE id=%s',
                                  (payload['id'],)).fetchone() if row else None
        prefix = '⏰ Promemoria attività: ' if payload.get('owner_reminder') else '⏰ Attività in scadenza: '
        label = (' — ' + owner_task_schedule_label(dict(zip(
            ('title', 'deadline', 'owner_deadline_date', 'owner_remind_at'), row)))
                 if row and payload.get('owner_reminder') else '')
        if row:
            def remind():
                with db._conn() as conn, conn.transaction():
                    jobs.assert_current(conn)
                    if not task_reconciliation.reminder_is_current(conn, payload):
                        raise jobs.Deferred('Reminder changed')
                jobs.assert_current()
                return telegram.notify(prefix+row[0]+label, html=False)
            if not origin or not origin[0]:
                # A task without any native origin (e.g. /task-created) can never
                # pass the notify gate: fail visibly instead of looping forever.
                raise jobs.Permanent('Promemoria senza provenienza nativa notificabile')
            _effect(_notification_key(payload), origin[0], 'notify',
                    dict(text=prefix+row[0]+label, html=False, recipient=str(config.TELEGRAM_CHAT_ID)), remind)
    elif kind == 'email':
        from secretary.agent import outbox
        from secretary.connectors import email as mail
        with db._conn() as conn:
            jobs.assert_current(conn)
            draft = outbox.load_approved(conn, payload['id'])
        if draft is None:
            return  # già inviata, scartata o mai approvata: nessun effetto
        if not outbox.sends_allowed():
            raise jobs.Deferred('Invio a terzi disattivato (modalità observe)')
        account = outbox.account_for(draft['account'])
        effect_payload = dict(account=draft['account'], recipient=draft['recipient'],
                              subject=draft['subject'],
                              body_sha256=hashlib.sha256(draft['body'].encode()).hexdigest())

        def send():
            jobs.assert_current()
            return mail.send_with_attachments(account, draft['recipient'], draft['subject'], draft['body'])
        try:
            _effect(f"email-send:{payload['id']}", draft['approved_by_message'], 'email_send',
                    effect_payload, send)
        except EffectUncertain:
            with db._conn() as conn:
                outbox.mark(conn, payload['id'], 'uncertain')
            raise
        with db._conn() as conn:
            outbox.mark(conn, payload['id'], 'sent')
    elif kind == 'zip':
        if not capabilities.enabled('monthly_zip') or not flows.enabled('monthly_zip'):
            raise jobs.Deferred('ZIP paused')
        from secretary.workflows import invoices
        # Per-entity receipts prevent a partial batch retry from resending the
        # first archive. zip_send is delegated: each archive carries the native
        # provenance of an invoice message of that month (evidence_message_id),
        # resolved server-side; a month without native invoice provenance fails
        # permanently instead of looping.
        month = payload['month']
        entities = set(config.INVOICE_ENTITIES) | {m['entity'] for m in db.months_with_invoices() if m['month'] == month}
        with db._conn() as conn:
            evidence_mid = conn.execute('''SELECT (payload->>'message_id')::bigint FROM work_jobs
                WHERE kind='action' AND key=%s LIMIT 1''', (f'invoice:{month}',)).fetchone()
            if evidence_mid is None:
                # Fall back to any native message that archived an invoice of this month.
                evidence_mid = conn.execute('''SELECT i.message_id FROM invoices i
                    JOIN communications c ON c.id=i.message_id
                    WHERE i.month=%s ORDER BY i.id LIMIT 1''', (month,)).fetchone()
        evidence_mid = evidence_mid[0] if evidence_mid else None
        if evidence_mid is None:
            raise jobs.Permanent('Archivio ZIP senza provenienza nativa notificabile')
        for entity in sorted(entities):
            if db.zip_sent(entity, month):
                continue
            path = invoices.build_monthly_zip(entity, month)
            if not path:
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            def send_zip():
                if not capabilities.enabled('monthly_zip') or not flows.enabled('monthly_zip'):
                    raise jobs.Deferred('ZIP paused')
                jobs.assert_current()
                return telegram.send_document(path, caption=f'📦 Fatture {entity} — {month}')
            _effect(f'zip:{month}:{entity}', evidence_mid, 'zip',
                    dict(month=month, entity=entity, sha256=digest,
                         recipient=str(config.TELEGRAM_CHAT_ID)), send_zip)
            jobs.assert_current()
            db.mark_zip_sent(entity, month, str(path))
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


def _prune_effect_audit(conn):
    """Retention giornaliera: registro di audit delle policy e audit dell'agente.

    effect_receipts restano immutabili; si potano solo le righe ad alto volume
    (can_read_context inserisce ~una riga di audit per candidato) e le
    esecuzioni dell'agente (agent_runs, con i relativi strumenti).
    Un solo passaggio al giorno, protetto da un timbro in source_cursors.
    """
    days = config.EFFECT_AUDIT_RETENTION_DAYS
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    stamp = conn.execute("SELECT value FROM source_cursors WHERE key='effect_audit_retention_day'").fetchone()
    if stamp and stamp[0] and stamp[0].get('day') == today:
        return
    if days > 0:
        conn.execute('DELETE FROM effect_audit WHERE created_at < now() - (%s * interval \'1 day\')', (days,))
    from secretary.agent import schema as agent_schema
    agent_schema.prune(conn)
    conn.execute('''INSERT INTO source_cursors(key,value) VALUES ('effect_audit_retention_day',%s)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()''',
        (json.dumps({'day': today}),))


def schedule():
    current = datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%Y-%m')
    if capabilities.enabled('monthly_zip') and flows.enabled('monthly_zip'):
        for month in sorted({m['month'] for m in db.months_with_invoices() if m['month'] < current}):
            jobs.submit('action', f'zip:{month}', {'type':'zip','month':month})
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        _prune_effect_audit(conn)
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
        jobs.assert_current(conn)
