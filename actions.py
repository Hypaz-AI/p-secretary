"""Azioni concrete del segretario: proxy verso il DB Postgres.

Il vecchio file gestiva JSON su file: ora tutto vive in Postgres
(vedi db.py). Questo modulo mantiene le funzioni usate da main.py.
"""
import json

import config
import db


def load_state() -> dict:
    if config.STATE_FILE.exists():
        try:
            return json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"email_uids": {}, "telegram_offset": 0, "last_digest_day": None}


def save_state(state: dict) -> None:
    config.STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---- proxy verso il DB (mantenuti per compatibilità) --------------------
def add_appointment(title, dt, location="", notes="", source=""):
    return db.add_appointment(title, dt, location, notes, source)


def upcoming_appointments(days=7):
    return db.upcoming_appointments(days)


def add_note(text):
    return db.add_note(text)


def recent_notes(limit=5):
    return db.recent_notes(limit)


def add_rule(pattern, instruction, taught_by="utente"):
    return db.add_rule(pattern, instruction, taught_by)


def all_rules():
    return db.all_rules()
