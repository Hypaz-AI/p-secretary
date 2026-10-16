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
from connectors import telegram

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
            # Le attività esplicite dell'utente sono autorizzate; le comunicazioni
            # esterne restano proposte, come nella pipeline di analisi.
            save_event(conn, event, key, f"task:{task['id']}",
                       approved=source_message is None)


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


def send_daily_digest() -> bool:
    """Accoda recap delle ultime 24 ore e piano delle successive 48 ore.

    La finestra è mobile: comprende tutto ciò che ha un timestamp nelle
    ultime DIGEST_WINDOW_HOURS ore (email, Telegram, WhatsApp), così tra un
    invio all'ora DIGEST_TIME e il successivo non resta scoperto nulla.
    """
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    window_start = (now.astimezone(timezone.utc) - timedelta(hours=DIGEST_WINDOW_HOURS)).astimezone(now.tzinfo)
    until = (now.astimezone(timezone.utc) + timedelta(hours=PLANNING_WINDOW_HOURS)).astimezone(now.tzinfo)

    # comunicazioni delle ultime 24 ore (finestra mobile, non giorno di calendario)
    comm_rows = db.communications_since(hours=DIGEST_WINDOW_HOURS)

    tasks = db.pending_tasks(until)
    plan_pending_tasks(tasks, now, until)
    appointments = planning_agenda(now, until)

    # brain's legacy formatter only knows timestamp deadlines. Supply an explicit
    # label in its input, and retain a deterministic section even if AI omits it.
    from pipeline import is_owner_task, owner_task_schedule_label
    owner_lines = [f"#{t['id']} {t['title']} — {owner_task_schedule_label(t, now)}"
                   for t in tasks if is_owner_task(t)]
    summary_tasks = [dict(t, title=f"{t['title']} — {owner_task_schedule_label(t, now)}")
                     if is_owner_task(t) else t for t in tasks]

    # testo generato dall'AI: senza parse_mode HTML (caratteri < > & rompono il parse)
    summary = (brain.summarize_day(comm_rows, appointments, tasks=summary_tasks,
                                   planning_start=now, planning_end=until) or "").strip()
    if not summary:
        raise RuntimeError('Digest vuoto')
    if owner_lines:
        summary += '\n\nAttività personali (date e promemoria):\n' + '\n'.join(owner_lines)

    # UN SOLO messaggio: header + riassunto, tutto in plain text
    header = ("🌙 Riassunto delle ultime 24 ore\n"
              f"da {window_start.strftime('%d/%m %H:%M')} a {now.strftime('%d/%m %H:%M')}\n"
              f"📅 Piano fino al {until.strftime('%d/%m %H:%M')} ({config.TIMEZONE})\n\n")
    import jobs
    day = now.strftime('%Y-%m-%d')
    jobs.submit('action', f'digest:{day}', {'type':'notify','text':header+summary,
        'html':False, 'flow':'digest', 'digest_day':day})
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