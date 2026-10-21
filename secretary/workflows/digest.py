"""Riepilogo serale: l'agente lo scrive, il server lo sigilla e lo consegna.

Una volta al giorno (all'ora DIGEST_TIME) `maybe_send_digest` accoda UN job
durevole `digest`. Il worker lo esegue con l'agente (strumenti di sola lettura:
nessuna scrittura, nessuna notifica) e poi il server sigilla testo e fonti in
UNA transazione prima di accodare l'unica notifica Telegram.

Il modello non decide nulla di operativo: il consenso è nelle viste `agent.*`
(rivalutato a ogni query) e, al momento dell'invio, `policy._system_allowed`
riverifica che i consensi con cui il testo è stato prodotto siano ancora attivi.
"""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from secretary.core import config, db, flows, jobs, policy

DIGEST_WINDOW_HOURS = 24
PLANNING_WINDOW_HOURS = 48
NO_SOURCE_TEXT = 'Nessuna fonte autorizzata per il riepilogo AI.'


def plan_pending_tasks(tasks: list, now: datetime, until: datetime) -> None:
    """Pianifica scadenze note; nessuna data inventata, chiavi stabili fra digest."""
    from secretary.workflows import task_reconciliation
    from secretary.workflows.pipeline import save_event, is_owner_task, enqueue_owner_reminder
    from secretary.connectors import gcal

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
                    continue  # Le attività del proprietario non entrano nel pianificatore automatico.
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
            # Una scadenza, anche del proprietario, NON è un'intenzione di calendario.
            save_event(conn, event, key, f"task:{task['id']}", approved=False)


def _parse_digest_time() -> (int, int):
    try:
        hh, mm = config.DIGEST_TIME.split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        return 21, 0  # default 21:00


# ------------------------------------------------------------------ fonti
def _schema():
    from secretary.agent import schema
    return schema._valid(config.AGENT_DB_SCHEMA)


def witnesses(conn):
    """Un messaggio leggibile per ogni (canale, storico) e uno con allegati per ogni canale.

    L'agente vede SOLO ciò che passa dalle viste `agent.*`, che applicano gli
    stessi consensi di `policy.can_read_context`. Sigillare questi testimoni
    significa che la revoca di un consenso usato per scrivere il riepilogo
    blocca l'invio, senza dover indovinare quali righe il modello abbia letto.
    """
    schema = _schema()
    text = [row[0] for row in conn.execute(f'''SELECT DISTINCT ON (source, historical) id
        FROM {schema}.messages ORDER BY source, historical, ts DESC, id DESC''').fetchall()]
    media = [row[0] for row in conn.execute(f'''SELECT DISTINCT ON (m.source, m.historical) a.message_id
        FROM {schema}.attachments a JOIN {schema}.messages m ON m.id = a.message_id
        ORDER BY m.source, m.historical, a.message_id DESC''').fetchall()]
    return text, media


def _seal_sources(conn, before, after, touched):
    """Fonti sigillate = testimoni prima/dopo la generazione + messaggi letti per id.

    Un messaggio letto che nel frattempo non è più consentito rende il testo
    non pubblicabile: il job viene rieseguito, non inviato.
    """
    sources = set(before[0]) | set(after[0])
    media = set(before[1]) | set(after[1])
    for mid in touched:
        if policy.metadata_for(conn, mid):
            if not policy.can_read_context(conn, mid, 'ai_text'):
                raise jobs.Deferred('Digest data policy changed')
            sources.add(mid)
            if conn.execute('SELECT 1 FROM attachments WHERE message_id=%s LIMIT 1', (mid,)).fetchone():
                if policy.can_read_context(conn, mid, 'ai_media'):
                    media.add(mid)
    # Un testimone che non è più leggibile (consenso revocato durante la generazione).
    if not _sources_current(conn, sorted(set(before[0])), sorted(set(before[1]))):
        raise jobs.Deferred('Digest data policy changed')
    return sorted(sources), sorted(media & sources)


def _sources_current(conn, sources, media_sources=()):
    media_sources = set(media_sources)
    return all(policy.can_read_context(conn, mid, 'ai_text')
               and (mid not in media_sources or policy.can_read_context(conn, mid, 'ai_media'))
               for mid in sources)


def _queue_notification(conn, day, text, source_ids, media_source_ids=()):
    """Unico scrittore fidato (scheduler). Sigilla testo e job in UNA transazione.

    Non è uno strumento del modello né un endpoint: nessun payload di azione può
    creare queste righe. Giorno e destinatario sono write-once, anche se l'invio
    risulta incerto.
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
            # Un job estraneo/preesistente non deve mai essere promosso a lavoro fidato.
            raise ValueError('Digest job identity conflict')
        conn.execute('''INSERT INTO system_notifications
            (token,system,scope,recipient,effect_key,job_id,payload,payload_hash,source_ids,media_source_ids)
            VALUES (%s,'digest',%s::date,%s,%s,%s,%s,%s,%s,%s)''',
            (token, day, recipient, key, queued[0], json.dumps(effect_payload), digest, sources, media))
        jobs.assert_current(conn)
        return key


# ------------------------------------------------------------- esecuzione
def _owner_lines(tasks, now):
    """Sezione deterministica: le scadenze del proprietario non dipendono dal modello."""
    from secretary.workflows.pipeline import is_owner_task, owner_task_schedule_label
    return [f"#{t['id']} {t['title']} — {owner_task_schedule_label(t, now)}"
            for t in tasks if is_owner_task(t)]


def run(payload):
    """Handler del worker 'digest': piano delle scadenze, testo dell'agente, sigillo."""
    day = payload.get('day') if isinstance(payload, dict) else None
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise jobs.Permanent('Digest payload invalid')
    datetime.strptime(day, '%Y-%m-%d')
    if not flows.enabled('digest'):
        raise jobs.Deferred('Digest flow paused')
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    window_start = (now.astimezone(timezone.utc) - timedelta(hours=DIGEST_WINDOW_HOURS)).astimezone(now.tzinfo)
    until = (now.astimezone(timezone.utc) + timedelta(hours=PLANNING_WINDOW_HOURS)).astimezone(now.tzinfo)

    tasks = db.pending_tasks(until)
    plan_pending_tasks(tasks, now, until)
    with db._conn() as conn:
        jobs.assert_current(conn)
        before = witnesses(conn)
    appointments = db.appointments_between(now, until)
    if not (before[0] or tasks or appointments):
        text, touched = NO_SOURCE_TEXT, []
    else:
        from secretary.agent import turns
        text, touched = turns.digest_text(window_start, now, until, day)
        if not text:
            raise RuntimeError('Digest vuoto')
    owner_lines = _owner_lines(tasks, now)
    if owner_lines:
        text += '\n\nAttività personali (date e promemoria):\n' + '\n'.join(owner_lines)
    header = ("🌙 Riassunto delle ultime 24 ore\n"
              f"da {window_start.strftime('%d/%m %H:%M')} a {now.strftime('%d/%m %H:%M')}\n"
              f"📅 Piano fino al {until.strftime('%d/%m %H:%M')} ({config.TIMEZONE})\n\n")
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        after = witnesses(conn)
        sources, media = _seal_sources(conn, before, after, touched)
        _queue_notification(conn, day, header + text, sources, media)


def enqueue_digest(now=None) -> bool:
    """Accoda il job del giorno (idempotente). Accodato non significa consegnato."""
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    day = now.strftime('%Y-%m-%d')
    jobs.submit('digest', f'digest:{day}', {'day': day})
    return True


def maybe_send_digest(state: dict) -> bool:
    """Chiamato a ogni ciclo: accoda il digest solo all'ora giusta, una volta al giorno."""
    hh, mm = _parse_digest_time()
    if not flows.enabled('digest'):
        flows.mark('digest', 'paused')
        return False
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    today_key = now.strftime("%Y-%m-%d")
    already_sent = jobs.cursor('last_digest_day') == today_key

    if already_sent:
        flows.mark('digest', 'sent_today' if jobs.cursor('last_digest_sent_day') == today_key else 'queued_today')
        return False
    if (now.hour, now.minute) < (hh, mm):
        flows.mark('digest', 'scheduled')
        return False

    print("[digest] ora del riassunto giornaliero!")
    try:
        if enqueue_digest(now):
            state["last_digest_day"] = today_key
            jobs.set_cursor('last_digest_day', today_key)
            flows.mark('digest', 'queued_today')
            return True
        flows.mark('digest', 'not_sent')
    except Exception as exc:  # noqa: BLE001
        flows.mark('digest', 'failed')
        print(f"[digest] errore: {exc}")
    return False
