"""Pipeline 1: riassunto giornaliero via Telegram.

Una volta al giorno (all'ora DIGEST_TIME del .env) il segretario raccoglie
le comunicazioni delle ULTIME 24 ORE (finestra mobile, non il giorno di
calendario) dal DB, chiede all'AI un riassunto e lo manda via bot. Così
l'invio all'ora scelta non lascia fuori nulla: copre esattamente le 24 ore
precedenti. Include il piano delle prossime 48 ore dalle attività aperte,
con scadenze, appuntamenti e promemoria persistenti.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import brain
import config
import db
import flows
import jobs
import json
import policy
import uuid

DIGEST_WINDOW_HOURS = 24
PLANNING_WINDOW_HOURS = 48


def plan_pending_tasks(tasks: list, now: datetime, until: datetime) -> None:
    """Pianifica scadenze note; nessuna data inventata, chiavi stabili fra digest."""
    import jobs
    import task_reconciliation
    from pipeline import save_event, is_owner_task, enqueue_owner_reminder
    from connectors import gcal

    with db._conn() as conn, conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190423)')
        for task in sorted(tasks, key=lambda item: item['id']):
            # Rileggi sotto lock: /fatto può essere arrivato durante la generazione.
            row = conn.execute(
                """SELECT deadline,version,title,source_message,owner_deadline_date,owner_remind_at,
                    owner_origin_message,owner_operation_key FROM tasks WHERE id=%s AND status='open' FOR UPDATE""",
                (task['id'],)).fetchone()
            if row:
                current = dict(zip(('deadline', 'version', 'title', 'source_message', 'owner_deadline_date',
                                    'owner_remind_at', 'owner_origin_message', 'owner_operation_key'), row))
                if is_owner_task(current):
                    enqueue_owner_reminder(conn, dict(current, id=task['id']))
                    continue  # Owner tasks never enter the legacy reminder/calendar planner.
            if not row or row[0] is None:
                continue
            deadline, version, title, source_message = row[:4]
            if deadline > until:
                continue
            reminder_key = f"task-reminder:{task['id']}"
            inserted = jobs.enqueue(conn, 'action', reminder_key,
                                    {'type': 'reminder', 'id': task['id'],
                                     'deadline': deadline.isoformat()})
            if inserted:
                conn.execute(
                    'UPDATE work_jobs SET available_at=%s WHERE key=%s',
                    (max(now, deadline - timedelta(hours=24)), reminder_key))
            if deadline <= now:
                continue  # segnalare gli arretrati, non spostarli arbitrariamente
            key = task_reconciliation.calendar_key(task['id'], version)
            existing = conn.execute(
                """SELECT id FROM appointments WHERE sync_key=%s
                   OR (%s::bigint IS NOT NULL AND sync_key LIKE %s) LIMIT 1""",
                (key, source_message,
                 f"message:{source_message}:%")).fetchone()
            if existing:
                continue
            try:
                event = gcal.validate_event({
                    'title': title, 'dt': deadline.isoformat(),
                    'duration_min': 15, 'reminder_minutes': 30,
                    'notes': f"Scadenza attività #{task['id']}",
                })
            except ValueError:
                continue
            # A deadline, even owner-originated, is NOT a current Calendar intent.
            save_event(conn, event, key, f"task:{task['id']}",
                       approved=False)


def planning_agenda(now: datetime, until: datetime) -> list:
    from connectors import gcal
    from pipeline import merge_agenda

    appointments = db.appointments_between(now, until)
    try:
        if gcal.is_active():
            remote = []
            for event in gcal.upcoming_events(days=3):
                start = datetime.fromisoformat(event['dt'])
                if start.tzinfo is None:
                    start = start.replace(tzinfo=ZoneInfo(config.TIMEZONE))
                if (event.get('all_day') and now.date() <= start.date() <= until.date()
                        or now <= start <= until):
                    remote.append(event)
            appointments = merge_agenda(remote, appointments)
    except Exception as exc:  # fallback locale, senza bloccare il digest
        print(f'[digest] agenda remota non disponibile: {type(exc).__name__}')
    return appointments


def _parse_digest_time() -> (int, int):
    try:
        hh, mm = config.DIGEST_TIME.split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        return 21, 0  # default 21:00


def _permitted_sources(conn, comm_rows, tasks, appointments):
    """Gate EVERY native source before model egress, not display labels/summary.

    Unknown provenance (legacy tasks and remote Calendar) is omitted from AI.
    Never reuse message_analysis summaries: those may contain denied attachments.
    No image bytes/paths or transcripts are loaded, so no media egress is needed.
    """
    sources = set()
    decisions = {}

    def allowed(mid):
        if type(mid) is not int or mid <= 0:
            return False
        if mid not in decisions:
            decisions[mid] = policy.can_operate(conn, mid, 'ai_text')
        if decisions[mid]:
            sources.add(mid)
        return decisions[mid]

    communications = []
    for item in comm_rows:
        if allowed(item['id']):
            communications.append(dict(ts=item['ts'], channel=item['channel'], sender=item['sender'],
                subject=item['subject'], summary=item['body'] or '', category='altro',
                priority='normale', needs_action=False, action='', direction=item['direction']))
    safe_tasks = []
    for task in tasks:
        # Both origins matter when an owner updates a task inferred from email.
        origins = {task.get(field) for field in ('source_message', 'owner_origin_message')
                   if task.get(field) is not None}
        if origins and all(allowed(mid) for mid in origins):
            safe_tasks.append(task)
    safe_appointments = []
    for item in appointments:
        row = conn.execute('SELECT sync_key FROM appointments WHERE id=%s', (item['id'],)).fetchone()
        mid = None
        if row:
            from pipeline import _event_origin
            mid = _event_origin(conn, row[0])
        if allowed(mid):
            # Event notes/title can derive from an attachment. Only the display
            # fields below reach summarize_day; media-derived events are omitted
            # unless the source also permits ai_media.
            attached = conn.execute('SELECT 1 FROM attachments WHERE message_id=%s LIMIT 1', (mid,)).fetchone()
            if attached and not policy.can_operate(conn, mid, 'ai_media'):
                continue
            safe_appointments.append(item)
    return communications, safe_tasks, safe_appointments, sorted(sources)


def _sources_current(conn, sources, media_sources=()):
    return all(policy.can_operate(conn, mid, 'ai_text')
               and (mid not in media_sources or policy.can_operate(conn, mid, 'ai_media'))
               for mid in sources)


def _queue_notification(conn, day, text, source_ids, media_source_ids=()):
    """Trusted scheduler writer ONLY. Seal exact text + job in ONE transaction.

    Not a model tool/public request endpoint. Nothing in action payloads can
    create these rows. A date/recipient is write-once, including uncertain sends.
    """
    recipient = str(config.TELEGRAM_CHAT_ID or '').strip()
    if not recipient.isdecimal() or int(recipient) <= 0:
        raise jobs.Deferred('Digest recipient unavailable')
    datetime.strptime(day, '%Y-%m-%d')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Empty digest')
    sources = sorted(set(source_ids))
    media = sorted(set(media_source_ids))
    with conn.transaction():
        jobs.assert_current(conn)
        conn.execute('SELECT pg_advisory_xact_lock(73190429)')
        flow = conn.execute("SELECT enabled FROM flow_settings WHERE flow='digest' FOR SHARE").fetchone()
        if not flow or flow[0] is not True or not set(media).issubset(sources):
            raise jobs.Deferred('Digest flow paused')
        if not _sources_current(conn, sources, media):
            raise jobs.Deferred('Digest data policy changed')
        existing = conn.execute('''SELECT effect_key FROM system_notifications
            WHERE system='digest' AND scope=%s::date AND recipient=%s FOR UPDATE''', (day, recipient)).fetchone()
        if existing:
            return existing[0]
        effect_payload = dict(text=text, html=False, recipient=recipient)
        digest = policy.payload_hash(effect_payload)
        key = f'digest:{day}:{digest}'
        token = 'digest:' + uuid.uuid4().hex
        payload = dict(type='notify', text=text, html=False, flow='digest', digest_day=day,
                       system='digest', system_authorization=token)
        queued = jobs.enqueue(conn, 'action', key, payload)
        if not queued:
            # A foreign/preexisting job must never be promoted to trusted work.
            raise ValueError('Digest job identity conflict')
        conn.execute('''INSERT INTO system_notifications
            (token,system,scope,recipient,effect_key,job_id,payload,payload_hash,source_ids,media_source_ids)
            VALUES (%s,'digest',%s::date,%s,%s,%s,%s,%s,%s,%s)''',
            (token, day, recipient, key, queued[0], json.dumps(effect_payload), digest, sources, media))
        jobs.assert_current(conn)
        return key


def send_daily_digest() -> bool:
    """Accoda recap delle ultime 24 ore e piano delle successive 48 ore.

    La finestra è mobile: comprende tutto ciò che ha un timestamp nelle
    ultime DIGEST_WINDOW_HOURS ore (email, Telegram, WhatsApp), così tra un
    invio all'ora DIGEST_TIME e il successivo non resta scoperto nulla.
    """
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    window_start = (now.astimezone(timezone.utc) - timedelta(hours=DIGEST_WINDOW_HOURS)).astimezone(now.tzinfo)
    until = (now.astimezone(timezone.utc) + timedelta(hours=PLANNING_WINDOW_HOURS)).astimezone(now.tzinfo)

    tasks = db.pending_tasks(until)
    plan_pending_tasks(tasks, now, until)
    # Native IDs and RAW text only. The legacy query concatenates unconditionally
    # media transcripts and previously generated AI summaries, so cannot be used.
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        rows = conn.execute('''SELECT id,ts,channel,sender,subject,LEFT(body,2000),direction
            FROM communications WHERE ts>=%s AND ts<=%s ORDER BY ts,id''', (window_start, now)).fetchall()
        comm_rows = [dict(zip(('id', 'ts', 'channel', 'sender', 'subject', 'body', 'direction'), row))
                     for row in rows]
        # Remote Calendar has no native consent source; do not send it to AI.
        appointments = db.appointments_between(now, until)
        comm_rows, tasks, appointments, sources = _permitted_sources(conn, comm_rows, tasks, appointments)
        media_sources = [mid for mid in sources if conn.execute(
            'SELECT 1 FROM attachments WHERE message_id=%s LIMIT 1', (mid,)).fetchone()]
        # Derived task titles can include attachment content as well.
        if media_sources and not all(policy.can_operate(conn, mid, 'ai_media') for mid in media_sources):
            # Raw message text remains independently permitted; omit all derived
            # tasks/events for denied media rather than infer from old summaries.
            denied = {mid for mid in media_sources if not policy.can_operate(conn, mid, 'ai_media')}
            tasks = [t for t in tasks if not denied.intersection(
                (t.get('source_message'), t.get('owner_origin_message')))]
            appointments = []
            media_sources = [mid for mid in media_sources if mid not in denied]

    # brain's legacy formatter only knows timestamp deadlines. Supply an explicit
    # label in its input, and retain a deterministic section even if AI omits it.
    from pipeline import is_owner_task, owner_task_schedule_label
    owner_lines = [f"#{t['id']} {t['title']} — {owner_task_schedule_label(t, now)}"
                   for t in tasks if is_owner_task(t)]
    summary_tasks = [dict(t, title=f"{t['title']} — {owner_task_schedule_label(t, now)}")
                     if is_owner_task(t) else t for t in tasks]

    # testo generato dall'AI: senza parse_mode HTML (caratteri < > & rompono il parse)
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        if not _sources_current(conn, sources, media_sources):
            raise jobs.Deferred('Digest data policy changed')
    # With no consented native input, use a local neutral recap, not an AI call.
    summary = ((brain.summarize_day(comm_rows, appointments, tasks=summary_tasks,
                                   planning_start=now, planning_end=until) or '').strip()
               if comm_rows or appointments or summary_tasks else 'Nessuna fonte autorizzata per il riepilogo AI.')
    if not summary:
        raise RuntimeError('Digest vuoto')
    if owner_lines:
        summary += '\n\nAttività personali (date e promemoria):\n' + '\n'.join(owner_lines)

    # UN SOLO messaggio: header + riassunto, tutto in plain text
    header = ("🌙 Riassunto delle ultime 24 ore\n"
              f"da {window_start.strftime('%d/%m %H:%M')} a {now.strftime('%d/%m %H:%M')}\n"
              f"📅 Piano fino al {until.strftime('%d/%m %H:%M')} ({config.TIMEZONE})\n\n")
    day = now.strftime('%Y-%m-%d')
    with db._conn() as conn, conn.transaction():
        _queue_notification(conn, day, header + summary, sources, media_sources)
    return True  # queued, not delivered; the outbox records actual completion


def maybe_send_digest(state: dict) -> bool:
    """Chiama questo a ogni ciclo: manda il digest solo all'ora giusta."""
    hh, mm = _parse_digest_time()
    if not flows.enabled('digest'):
        flows.mark('digest', 'paused')
        return False
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    today_key = now.strftime("%Y-%m-%d")
    import jobs
    already_sent = jobs.cursor('last_digest_day') == today_key

    if already_sent:
        flows.mark('digest', 'sent_today' if jobs.cursor('last_digest_sent_day') == today_key else 'queued_today')
        return False
    if (now.hour, now.minute) < (hh, mm):
        flows.mark('digest', 'scheduled')
        return False

    print("[digest] ora del riassunto giornaliero!")
    try:
        if send_daily_digest():
            state["last_digest_day"] = today_key
            jobs.set_cursor('last_digest_day', today_key)
            flows.mark('digest', 'queued_today')
        else:
            flows.mark('digest', 'not_sent')
    except Exception as exc:  # noqa: BLE001
        flows.mark('digest', 'failed')
        print(f"[digest] errore: {exc}")
    return True