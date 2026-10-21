"""Stato locale minimo del loop principale (offset/ultimo digest); il resto vive in Postgres."""
import json

from secretary.core import config


def load_state() -> dict:
    if config.STATE_FILE.exists():
        try:
            return json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"email_uids": {}, "telegram_offset": 0, "last_digest_day": None}


def save_state(state: dict) -> None:
    config.STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
