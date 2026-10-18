"""Durable ingestion → analysis → outbox. No external effects inside DB transactions."""
import json
import hashlib
import re
import uuid
from urllib.parse import quote
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import brain
import config
import db
import flows
import jobs
import capabilities
import policy
from connectors import gcal, telegram


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
            import task_reconciliation
            task_reconciliation.queue(conn, mid)
            import secretary_runtime
            secretary_runtime.observe(conn, mid)
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


def analyze(payload):
    mid = payload['message_id']
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        if not policy.can_operate(conn, mid, 'extract'):
            return
    (channel, sender, subject, body, source_id), attachments = load_message(mid)
    flow = 'email' if channel.startswith('email') else 'whatsapp' if channel == 'whatsapp' else 'telegram_bot'
    if not flows.enabled(flow):
        raise jobs.Deferred('Source paused')
    with db._conn() as conn:
        if conn.execute('SELECT 1 FROM message_analysis WHERE message_id=%s', (mid,)).fetchone():
            return
        _analysis_permission(conn, mid, attachments)
    if channel.startswith('telegram') or channel == 'whatsapp':
        from connectors import media_text
        # Archived media must be converted durably before semantic analysis.
        body = media_text.message_text(body, attachments)
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        _analysis_permission(conn, mid, attachments)
    verdict = brain.normalize_verdict(brain.analyze(subject, sender, body, channel, attachments))
    if verdict['category'] == 'errore':
        raise RuntimeError('Analysis unavailable')
    from main import format_verdict
    notify = verdict['urgent'] or verdict['severity'] == 'alta' or config.priority_at_least(verdict['priority'], config.NOTIFY_PRIORITY_THRESHOLD)
    # A permanent owner instruction ("avvisami sempre quando scrive X") makes
    # this message urgent for the owner, bypassing quiet hours via the
    # existing emergency semantics. The condition is the owner's own words,
    # judged semantically by the model; the message content is untrusted data
    # and can never create or modify conditions.
    try:
        import owner_instructions
        matched = owner_instructions.evaluate_notify(sender, subject, body, channel)
        if matched:
            notify = True
            verdict = dict(verdict, urgent=True)
    except Exception:
        pass  # Missing table/migration must never break analysis.
    with db._conn() as conn, conn.transaction():
        # Serializes analysis commit, including concurrent lease recovery.
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (mid,))
        jobs.assert_current(conn)
        if not policy.can_operate(conn, mid, 'extract'):
            return
        _analysis_permission(conn, mid, attachments)
        if conn.execute('SELECT 1 FROM message_analysis WHERE message_id=%s', (mid,)).fetchone():
            return
        db.message_store.analyze(conn, mid, channel, verdict, False, source_id)
        if notify:
            import quiet_hours
            notification = {'type':'notify', 'message_id':mid,
                'text':format_verdict(channel, sender, subject, verdict),
                'urgent': bool(verdict['urgent']), 'severity': verdict['severity']}
            inserted = jobs.enqueue(conn, 'action', f'notify:{mid}', notification)
            if inserted and not quiet_hours.notification_ready(notification, datetime.now(timezone.utc)):
                # Fascia silenziosa globale: le notifiche ordinarie non urgenti
                # vengono rinviate all'uscita della fascia, mai scartate.
                deferred = quiet_hours.defer_until(datetime.now(timezone.utc))
                conn.execute('UPDATE work_jobs SET available_at=%s WHERE key=%s', (deferred, f'notify:{mid}'))
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
            # No display label, legacy allowlist or model flag grants authority.
            save_event(conn, event, f'message:{mid}:{index}', f'{channel}: {sender}', False)
        if verdict['is_invoice'] and attachments and capabilities.enabled('invoices', conn=conn):
            jobs.enqueue(conn, 'action', f'invoice:{mid}', {'type':'invoice', 'message_id':mid, 'verdict':verdict})
        import secretary_runtime
        secretary_runtime.observe(conn, mid)
        jobs.assert_current(conn)


def _analysis_permission(conn, mid, attachments):
    # data_policies is dashboard-managed. No environment default grants consent.
    if not policy.can_operate(conn, mid, 'ai_text'):
        raise jobs.Deferred('AI text data policy denied')
    if attachments and not policy.can_operate(conn, mid, 'ai_media'):
        raise jobs.Deferred('AI media data policy denied')


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


def save_event(conn, event, key, source, approved=True):
    """Local proposal first; only a payload-bound CURRENT owner grant queues I/O.

    Natural owner adapter: pass approved=True ONLY for its current event plan.
    /conferma and /annulla must call policy.grant_calendar on the existing row.
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
    if approved and origin and re.fullmatch(rf'owner:{origin}:event:\d+', key):
        try:
            expected = policy.calendar_event_hash(conn, row[0])
        except ValueError:
            return row[0]
        policy.grant_calendar(conn, row[0], expected, origin)
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
                raise jobs.Deferred('Effect policy denied')
        if not policy.record_receipt(conn, key, 'attempted'):
            raise jobs.Deferred('Effect authorization changed')
        jobs.assert_current(conn)
    try:
        jobs.assert_current()
        # Recheck consent after committing the attempt, immediately before HTTP.
        with db._conn() as conn, conn.transaction():
            if not policy.effect_allowed(conn, mid, effect, payload, authorization=authorization):
                raise jobs.Deferred('Effect policy revoked')
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
        mid = payload.get('owner_reply_mid') or payload.get('message_id')
        if payload.get('secretary_review'):
            import secretary_runtime
            try:
                secretary_runtime.assert_notification_current(payload)
            except secretary_runtime.ReviewStopped:
                return  # Do not defer revoked/completed/acknowledged work forever.
        def deliver():
            # Review notification fences are re-read at the actual send, not
            # merely when the review was queued.
            if payload.get('secretary_review'):
                import secretary_runtime
                secretary_runtime.assert_notification_current(payload)
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
        import invoices
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
            _effect(_notification_key(payload), origin[0] if origin else None, 'notify',
                    dict(text=prefix+row[0]+label, html=False, recipient=str(config.TELEGRAM_CHAT_ID)), remind)
    elif kind == 'zip':
        if not capabilities.enabled('monthly_zip') or not flows.enabled('monthly_zip'):
            raise jobs.Deferred('ZIP paused')
        import invoices
        # Per-entity receipts prevent a partial batch retry from resending the
        # first archive. Current policy deliberately does not delegate zip_send.
        month = payload['month']
        entities = set(config.INVOICE_ENTITIES) | {m['entity'] for m in db.months_with_invoices() if m['month'] == month}
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
            _effect(f'zip:{month}:{entity}', payload.get('message_id'), 'zip',
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


def schedule():
    import secretary
    import secretary_runtime
    current = datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%Y-%m')
    if capabilities.enabled('monthly_zip') and flows.enabled('monthly_zip'):
        for month in sorted({m['month'] for m in db.months_with_invoices() if m['month'] < current}):
            jobs.submit('action', f'zip:{month}', {'type':'zip','month':month})
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        secretary.due_reviews(conn)
        secretary_runtime.backlog(conn)
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


def owner_commands(text):
    import task_reconciliation
    jobs.assert_current()
    context = jobs.execution_context()
    if context is not None:
        # Durable owner commands use the fenced receipt/outbox coordinator,
        # never the legacy direct-Telegram compatibility path below.
        import owner_dialogue
        return owner_dialogue.command(text, context.message_id)
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
                context = jobs.execution_context()
                authority = context.message_id if context else None
                if not authority or not policy.can_operate(conn, authority, 'owner_command', owner_required=True):
                    raise jobs.Deferred('Calendar command requires native owner provenance')
                jobs.assert_current(conn)
                row = conn.execute("UPDATE appointments SET sync_state=%s,done=%s WHERE id=%s AND sync_state=ANY(%s) RETURNING id", (target, command=='/annulla', identity,list(allowed))).fetchone()
                if row:
                    jobs.enqueue(conn, 'action', f'{command}:{identity}:{authority}',
                                 {'type':'calendar','id':identity, 'authority_message_id':authority})
                reply = 'Operazione in coda.' if row else 'Appuntamento non trovato.'
    telegram.notify(reply, html=False)
    return True