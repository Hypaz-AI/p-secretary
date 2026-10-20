"""Carica la configurazione dal file .env (senza dipendenze esterne)."""
import os
import json
import re
from pathlib import Path
from connectors.storage import FileLock, atomic_write

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get('PI_SECRETARY_CONFIG_FILE') or BASE_DIR / '.env').expanduser().resolve()
# Preserve provenance before dotenv fills the process environment. Dashboard
# edits must never claim to override values injected by the deployment.
DEPLOYMENT_ENV_KEYS = frozenset(os.environ)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_dotenv(CONFIG_FILE)
DATA_DIR = Path(os.environ.get('DATA_DIR') or str(BASE_DIR / 'data')).expanduser()


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def set_env(key: str, value: str) -> None:
    """Aggiorna una variabile nel .env (usato dagli script di setup)."""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
        raise ValueError('Invalid environment key')
    if any(c in value for c in ('\n', '\r', '\x00')):
        raise ValueError('Multiline environment values are not supported')
    path = CONFIG_FILE
    with FileLock(str(path) + '.lock'):
        text = path.read_text(encoding='utf-8') if path.exists() else ''
        lines = [line for line in text.splitlines()
                 if not re.match(rf'^\s*{re.escape(key)}\s*=', line)]
        lines.append(key + '=' + json.dumps(value, ensure_ascii=False))
        atomic_write(path, '\n'.join(lines) + '\n')
    os.environ[key] = value


def get_bool(key: str, default: bool = False) -> bool:
    return get(key, str(default)).lower() in ("1", "true", "yes", "on")


# --- OpenRouter ----------------------------------------------
AGENT_NAME = get("AGENT_NAME", "Pi Secretary") or "Pi Secretary"
OPENROUTER_API_KEY = get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OPENROUTER_AUDIO_MODEL = get("OPENROUTER_AUDIO_MODEL", "google/gemini-2.5-flash")


def _bounded_int(key, default, low, high):
    try:
        value = int(get(key, str(default)))
    except ValueError:
        return default
    return value if low <= value <= high else default


# --- Agente (core agentico su Copilot SDK, LLM = OpenRouter) ------------------
# openrouter/auto sceglie il modello in base alla complessità della richiesta.
AGENT_MODEL = get("AGENT_MODEL", "openrouter/auto") or "openrouter/auto"
# Modello che esegue la sola ricerca web (plugin web di OpenRouter).
AGENT_WEB_MODEL = get("AGENT_WEB_MODEL", "") or OPENROUTER_MODEL
AGENT_MAX_TOOL_CALLS = _bounded_int("AGENT_MAX_TOOL_CALLS", 30, 1, 200)
AGENT_OWNER_TIMEOUT = _bounded_int("AGENT_OWNER_TIMEOUT", 240, 30, 1800)
AGENT_BACKGROUND_TIMEOUT = _bounded_int("AGENT_BACKGROUND_TIMEOUT", 900, 60, 3300)
AGENT_LLM_TIMEOUT = _bounded_int("AGENT_LLM_TIMEOUT", 180, 20, 600)
AGENT_MAX_OUTPUT_TOKENS = _bounded_int("AGENT_MAX_OUTPUT_TOKENS", 16384, 1024, 65536)
# Lotto massimo di messaggi in ingresso valutati insieme dall'agente.
AGENT_INBOX_BATCH = _bounded_int("AGENT_INBOX_BATCH", 8, 1, 25)
# Tetto di notifiche Telegram autonome (urgenze) per giorno: argine al rumore.
AGENT_NOTIFY_MAX_PER_DAY = _bounded_int("AGENT_NOTIFY_MAX_PER_DAY", 5, 0, 100)
# Le risposte dell'assistente più vecchie di così tanti minuti non rientrano nel contesto:
# la provenienza dei loro dati non è registrata e i consensi possono essere cambiati.
AGENT_HISTORY_RESPONSE_MINUTES = _bounded_int("AGENT_HISTORY_RESPONSE_MINUTES", 20, 0, 240)
AGENT_DB_SCHEMA = get("AGENT_DB_SCHEMA", "agent") or "agent"
# Ruolo PostgreSQL di sola lettura (vedi deploy/agent_ro.sql); se assente la
# validazione AST resta l'unica barriera e l'app lo segnala all'avvio.
AGENT_DB_ROLE = get("AGENT_DB_ROLE", "agent_ro")

# --- Email ----------------------------------------------------
# Preset per provider: (host IMAP, porta IMAP, host SMTP, porta SMTP, auth)
# auth = "basic" (password / password per app) oppure "oauth2" (Microsoft).
_EMAIL_PRESETS = {
    "gmail":      ("imap.gmail.com", 993, "smtp.gmail.com", 587, "basic"),
    "outlook":    ("outlook.office365.com", 993, "smtp.office365.com", 587, "oauth2"),
    "m365":       ("outlook.office365.com", 993, "smtp.office365.com", 587, "oauth2"),
    "zimbra_ovh": ("ssl0.ovh.net", 993, "ssl0.ovh.net", 587, "basic"),
    "custom":     ("", 993, "", 587, "basic"),
}

# Fino a 50 account: nel .env basta aggiungere blocchi EMAIL1_*, EMAIL2_*, ...
EMAIL_ACCOUNTS = []
for i in range(1, 51):
    addr = get(f"EMAIL{i}_ADDRESS")
    if not addr:
        continue
    provider = get(f"EMAIL{i}_PROVIDER", "custom").lower()
    if provider not in _EMAIL_PRESETS:
        print(f"[config] provider sconosciuto '{provider}' per EMAIL{i}, uso 'custom'")
        provider = "custom"
    imap_host, imap_port, smtp_host, smtp_port, auth = _EMAIL_PRESETS[provider]
    EMAIL_ACCOUNTS.append({
        "name": get(f"EMAIL{i}_NAME", f"Account {i}"),
        "address": addr,
        "password": get(f"EMAIL{i}_PASSWORD"),
        "provider": provider,
        "auth": get(f"EMAIL{i}_AUTH", auth),
        # client OAuth2 alternativo (opzionale, solo per account Microsoft)
        "client_id": get(f"EMAIL{i}_CLIENT_ID"),
        # override manuali (opzionali anche quando si usa un preset)
        "imap_host": get(f"EMAIL{i}_IMAP_HOST", imap_host),
        "imap_port": int(get(f"EMAIL{i}_IMAP_PORT", str(imap_port))),
        "smtp_host": get(f"EMAIL{i}_SMTP_HOST", smtp_host),
        "smtp_port": int(get(f"EMAIL{i}_SMTP_PORT", str(smtp_port))),
    })

# --- Telegram / WhatsApp --------------------------------------
TELEGRAM_BOT_TOKEN = get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = get("TELEGRAM_CHAT_ID")
WHATSAPP_ENABLED = get_bool("WHATSAPP_ENABLED", False)
TELEGRAM_USER_ENABLED = get_bool('TELEGRAM_USER_ENABLED', False)
TELEGRAM_API_ID = get('TELEGRAM_API_ID')
TELEGRAM_API_HASH = get('TELEGRAM_API_HASH')
TELEGRAM_PHONE = get('TELEGRAM_PHONE')
TELEGRAM_USER_SESSION = get('TELEGRAM_USER_SESSION', str(DATA_DIR.resolve() / 'telegram_account/personal.session'))
TELEGRAM_USER_DIALOG_LIMIT = int(get('TELEGRAM_USER_DIALOG_LIMIT', '100'))
TELEGRAM_USER_MESSAGE_LIMIT = int(get('TELEGRAM_USER_MESSAGE_LIMIT', '200'))
TELEGRAM_USER_RECOVERY_INTERVAL = int(get('TELEGRAM_USER_RECOVERY_INTERVAL', '900'))

# --- Google Calendar (solo service account IAM, vedi setup_gcal.py) ----
GCAL_ENABLED = get_bool("GCAL_ENABLED", False)
# ID esplicito del calendario condiviso; primary non è supportato.
GCAL_CALENDAR_ID = get("GCAL_CALENDAR_ID", "")
# durata predefinita degli eventi fissati dall'agente (minuti)
GCAL_EVENT_DURATION = int(get("GCAL_EVENT_DURATION", "60"))

# --- Database Postgres (contesto / memoria del segretario) -----
# URI completo: postgresql://utente:password@host:porta/database
DATABASE_URL = get("DATABASE_URL")

# --- Fatture ----------------------------------------------------
# Cartella base dove salvare le fatture ricevute
INVOICE_DIR = Path(get("INVOICE_DIR", str(DATA_DIR / 'invoices')))
# Le due partite IVA dell'utente: nome cartella -> descrizione
INVOICE_ENTITIES = {}
for _i in range(1, 5):
    _name = get(f"INVOICE_ENTITY{_i}_NAME")
    if _name:
        INVOICE_ENTITIES[_name.lower()] = {
            "name": _name,
            "vat": get(f"INVOICE_ENTITY{_i}_VAT"),
            "description": get(f"INVOICE_ENTITY{_i}_DESC"),
        }

# --- Riassunto giornaliero ---------------------------------------
# Ora locale (HH:MM) a cui inviare il digest delle ultime 24 ore
DIGEST_TIME = get("DIGEST_TIME", "21:00")
# --- Varie ----------------------------------------------------
POLL_INTERVAL = int(get("POLL_INTERVAL", "300"))
TIMEZONE = get("TIMEZONE", "Europe/Rome")
LANGUAGE = get("LANGUAGE", "it")

# --- Segretario: modalità operativa -----
# SECRETARY_MODE: "observe" (solo proposte/bozze, nessun invio a terzi) oppure
# "active" (permette gli invii esplicitamente approvati dal proprietario).
# Default: observe. Valori non riconosciuti ricadono su observe (fail-safe).
SECRETARY_MODE = get("SECRETARY_MODE", "observe").lower()
if SECRETARY_MODE not in ("observe", "active"):
    SECRETARY_MODE = "observe"
# Retention dell'audit degli effetti (EFFECT_AUDIT_RETENTION_DAYS giorni).
# 0 disattiva. Le ricevute (effect_receipts) restano immutabili: qui si
# pota solo il registro bulk dei reason-code di policy.
try:
    EFFECT_AUDIT_RETENTION_DAYS = int(get("EFFECT_AUDIT_RETENTION_DAYS", "30"))
except ValueError:
    EFFECT_AUDIT_RETENTION_DAYS = 30
if not 0 <= EFFECT_AUDIT_RETENTION_DAYS <= 3650:
    EFFECT_AUDIT_RETENTION_DAYS = 30


def _tz():
    """ZoneInfo della TIMEZONE configurata, con fallback UTC sicuro."""
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:
        from datetime import timezone
        return timezone.utc
DATA_DIR.mkdir(parents=True, exist_ok=True)
EMAIL_ATTACHMENTS_DIR = DATA_DIR / "email_attachments"

# File di stato
STATE_FILE = DATA_DIR / "state.json"
APPOINTMENTS_FILE = DATA_DIR / "appointments.json"
NOTES_FILE = DATA_DIR / "notes.json"
LOG_FILE = DATA_DIR / "secretary.log"
