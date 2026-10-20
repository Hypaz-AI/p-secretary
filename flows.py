"""Persistent switches and process-local, observed worker health."""
import threading
import time

import db
import capabilities

LABELS = {
    'email': 'Email · acquisizione e analisi',
    'whatsapp': 'WhatsApp · acquisizione e analisi',
    'telegram_user': 'Telegram personale · archivio in/out',
    'telegram_bot': 'Telegram bot · analisi allegati e altre chat',
    'digest': 'Riassunto giornaliero',
    'invoices': 'Archiviazione fatture',
    'monthly_zip': 'Invio ZIP mensile',
    'gcal': 'Google Calendar · appuntamenti',
}
_health = {}
_lock = threading.Lock()


def init():
    with db._conn() as conn, conn.transaction():
        conn.execute(capabilities.FLOW_SCHEMA)
        defaults = capabilities.flow_defaults(conn)
        for flow in LABELS:
            conn.execute('''INSERT INTO flow_settings(flow,enabled) VALUES (%s,%s)
                ON CONFLICT DO NOTHING''', (flow, defaults.get(flow, True)))


def settings():
    with db._conn() as conn:
        rows = dict(conn.execute('SELECT flow,enabled FROM flow_settings').fetchall())
        defaults = capabilities.flow_defaults(conn)
        return {flow: rows.get(flow, defaults.get(flow, True)) for flow in LABELS}


def enabled(flow):
    if flow not in LABELS:
        raise ValueError('Unknown flow')
    return settings().get(flow, True)


def set_enabled(flow, value):
    if flow not in LABELS or type(value) is not bool:
        raise ValueError('Invalid flow setting')
    with db._conn() as conn:
        conn.execute('''INSERT INTO flow_settings(flow,enabled) VALUES (%s,%s)
            ON CONFLICT(flow) DO UPDATE SET enabled=excluded.enabled,updated_at=now()''',
            (flow, value))


def mark(flow, status):
    with _lock:
        _health[flow] = {'status': status, 'observed_at': time.time()}


def observed():
    with _lock:
        return {k: dict(v) for k, v in _health.items()}


def run(flow, callback):
    if not enabled(flow):
        mark(flow, 'paused')
        return
    mark(flow, 'running')
    try:
        result = callback()
        if observed().get(flow, {}).get('status') == 'running':
            mark(flow, 'idle')
        return result
    except Exception:
        mark(flow, 'failed')
        raise