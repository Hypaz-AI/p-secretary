"""Carica la configurazione dal file .env (senza dipendenze esterne)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def set_env(key: str, value: str) -> None:
    """Aggiorna una variabile nel .env (usato dagli script di setup)."""
    import re
    path = BASE_DIR / ".env"
    if not path.exists():
        path.write_text(f"{key}={value}\n", encoding="utf-8")
        os.environ[key] = value
        return
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(f"{key}={value}", text)
    else:
        # assicura un newline prima di aggiungere, per non incollare
        # la nuova chiave in fondo all'ultima riga esistente
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"{key}={value}\n"
    path.write_text(text, encoding="utf-8")
    os.environ[key] = value


def get_bool(key: str, default: bool = False) -> bool:
    return get(key, str(default)).lower() in ("1", "true", "yes", "on")


# --- OpenRouter ----------------------------------------------
OPENROUTER_API_KEY = get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")

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
TELEGRAM_USER_SESSION = get('TELEGRAM_USER_SESSION', 'data/telegram_account/personal.session')
TELEGRAM_USER_DIALOG_LIMIT = int(get('TELEGRAM_USER_DIALOG_LIMIT', '100'))
TELEGRAM_USER_MESSAGE_LIMIT = int(get('TELEGRAM_USER_MESSAGE_LIMIT', '200'))
TELEGRAM_USER_RECOVERY_INTERVAL = int(get('TELEGRAM_USER_RECOVERY_INTERVAL', '900'))

# --- Google Calendar (OAuth2 device flow, vedi setup_gcal.py) ----
GCAL_ENABLED = get_bool("GCAL_ENABLED", False)
GCAL_CLIENT_ID = get("GCAL_CLIENT_ID")
GCAL_CLIENT_SECRET = get("GCAL_CLIENT_SECRET")
# calendario da usare: 'primary' (il principale) o un ID calendario
GCAL_CALENDAR_ID = get("GCAL_CALENDAR_ID", "primary")
# durata predefinita degli eventi fissati dall'agente (minuti)
GCAL_EVENT_DURATION = int(get("GCAL_EVENT_DURATION", "60"))

# --- Database Postgres (contesto / memoria del segretario) -----
# URI completo: postgresql://utente:password@host:porta/database
DATABASE_URL = get("DATABASE_URL")

# --- Fatture ----------------------------------------------------
# Cartella base dove salvare le fatture ricevute
INVOICE_DIR = Path(get("INVOICE_DIR", "/home/ale/invoices"))
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
# Ora locale (HH:MM) a cui inviare il digest della giornata
DIGEST_TIME = get("DIGEST_TIME", "21:00")
# Politica di notifica in tempo reale:
#   "mai"   -> nessuna notifica ordinaria immediata (resta il digest)
#   "alta"  -> solo priorità alta tra le notifiche ordinarie
#   "media"/"bassa" -> avvisa subito anche le cose meno importanti
# Gravità alta o urgent=true valutate dall'AI scavalcano SEMPRE la soglia, anche mai.
NOTIFY_PRIORITY_THRESHOLD = get("NOTIFY_PRIORITY_THRESHOLD", "media").lower()

# --- Varie ----------------------------------------------------
POLL_INTERVAL = int(get("POLL_INTERVAL", "300"))
TIMEZONE = get("TIMEZONE", "Europe/Rome")
LANGUAGE = get("LANGUAGE", "it")
DATA_DIR = Path(get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# File di stato
STATE_FILE = DATA_DIR / "state.json"
APPOINTMENTS_FILE = DATA_DIR / "appointments.json"
NOTES_FILE = DATA_DIR / "notes.json"
LOG_FILE = DATA_DIR / "secretary.log"

# Priorità ordinate: una notifica parte solo se >= soglia
_PRIORITY_ORDER = {"bassa": 0, "media": 1, "alta": 2}


def priority_at_least(priority: str, threshold: str) -> bool:
    """True se `priority` merita una notifica immediata secondo la soglia.

    threshold="mai" disabilita le notifiche ordinarie; le emergenze sono
    gestite separatamente da main.process_message.
    """
    if (threshold or "").strip().lower() == "mai":
        return False
    return _PRIORITY_ORDER.get(priority, 0) >= _PRIORITY_ORDER.get(threshold, 1)