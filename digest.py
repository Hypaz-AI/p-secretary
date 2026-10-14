"""Pipeline 1: riassunto giornaliero via Telegram.

Una volta al giorno (all'ora DIGEST_TIME del .env) il segretario raccoglie
tutte le comunicazioni della giornata dal DB, chiede all'AI un riassunto
e lo manda via bot. Se è l'ultimo giorno del mese, manda anche gli zip
delle fatture del mese appena concluso (pipeline 5).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import brain
import config
import db
import flows
from connectors import telegram


def _parse_digest_time() -> (int, int):
    try:
        hh, mm = config.DIGEST_TIME.split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        return 21, 0  # default 21:00


def send_daily_digest() -> bool:
    """Prepara e invia il riassunto della giornata. Ritorna True se inviato.

    RILEGGE dalla casella tutte le email del giorno (ricevute E inviate),
    così il digest è completo anche per quelle non analizzate in tempo reale.
    """
    # Use native archive, including unanalysed source rows. Do not bypass
    # ingestion switches with a second live IMAP import during the digest.
    day_emails = []

    # 2) comunicazioni già analizzate oggi (email + telegram + whatsapp)
    comm_rows = db.communications_for_day()

    # 3) appuntamenti in agenda
    appointments = db.upcoming_appointments(days=7)

    if not day_emails and not comm_rows:
        print("[digest] nessuna comunicazione oggi, niente da mandare")
        return False

    # testo generato dall'AI: senza parse_mode HTML (caratteri < > & rompono il parse)
    summary = (brain.summarize_day(comm_rows, appointments,
                                   day_emails=day_emails) or "").strip()

    # UN SOLO messaggio: header + riassunto, tutto in plain text
    header = f"🌙 Riassunto della giornata — {datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%d/%m/%Y')}\n\n"
    return telegram.notify(header + summary, html=False)


def maybe_send_digest(state: dict) -> bool:
    """Chiama questo a ogni ciclo: manda il digest solo all'ora giusta."""
    hh, mm = _parse_digest_time()
    if not flows.enabled('digest'):
        flows.mark('digest', 'paused')
        return False
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    today_key = now.strftime("%Y-%m-%d")
    already_sent = state.get("last_digest_day") == today_key

    if already_sent:
        flows.mark('digest', 'sent_today')
        return False
    if (now.hour, now.minute) < (hh, mm):
        flows.mark('digest', 'scheduled')
        return False

    print("[digest] ora del riassunto giornaliero!")
    try:
        if send_daily_digest():
            state["last_digest_day"] = today_key
            flows.mark('digest', 'sent_today')
        else:
            flows.mark('digest', 'not_sent')
    except Exception as exc:  # noqa: BLE001
        flows.mark('digest', 'failed')
        print(f"[digest] errore: {exc}")
    return True