"""Pipeline 1: riassunto giornaliero via Telegram.

Una volta al giorno (all'ora DIGEST_TIME del .env) il segretario raccoglie
le comunicazioni delle ULTIME 24 ORE (finestra mobile, non il giorno di
calendario) dal DB, chiede all'AI un riassunto e lo manda via bot. Così
l'invio all'ora scelta non lascia fuori nulla: copre esattamente le 24 ore
precedenti. Se è l'ultimo giorno del mese, manda anche gli zip delle
fatture del mese appena concluso (pipeline 5).
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import brain
import config
import db
import flows
from connectors import telegram

DIGEST_WINDOW_HOURS = 24


def _parse_digest_time() -> (int, int):
    try:
        hh, mm = config.DIGEST_TIME.split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        return 21, 0  # default 21:00


def send_daily_digest() -> bool:
    """Prepara e invia il riassunto delle ultime 24 ore. True se inviato.

    La finestra è mobile: comprende tutto ciò che ha un timestamp nelle
    ultime DIGEST_WINDOW_HOURS ore (email, Telegram, WhatsApp), così tra un
    invio all'ora DIGEST_TIME e il successivo non resta scoperto nulla.
    """
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    window_start = now - timedelta(hours=DIGEST_WINDOW_HOURS)

    # comunicazioni delle ultime 24 ore (finestra mobile, non giorno di calendario)
    comm_rows = db.communications_since(hours=DIGEST_WINDOW_HOURS)

    # appuntamenti in agenda
    appointments = db.upcoming_appointments(days=7)

    if not comm_rows:
        print("[digest] nessuna comunicazione nelle ultime 24 ore, niente da mandare")
        return False

    # testo generato dall'AI: senza parse_mode HTML (caratteri < > & rompono il parse)
    summary = (brain.summarize_day(comm_rows, appointments) or "").strip()

    # UN SOLO messaggio: header + riassunto, tutto in plain text
    header = ("🌙 Riassunto delle ultime 24 ore\n"
              f"da {window_start.strftime('%d/%m %H:%M')} a {now.strftime('%d/%m %H:%M')}\n\n")
    import jobs
    day = datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%Y-%m-%d')
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