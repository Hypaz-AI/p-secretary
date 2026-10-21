"""Pipeline fatture: salvataggio organizzato e zip mensili via Telegram.

Ogni fattura che arriva (email o messaggio) viene salvata in:
    INVOICE_DIR / <entità> / <YYYY-MM> / <nome-appropriato>.<est>

Con entità = 'hypaz' o 'alessio' (le due P.IVA dell'utente).
L'ultimo giorno del mese il segretario prepara uno zip per entità con
tutte le fatture del mese appena concluso e lo invia via Telegram bot.
"""
import re
import hashlib
import zipfile
from datetime import datetime
from pathlib import Path

from secretary.core import config, db, flows
from secretary.connectors import telegram

# Estensioni considerate documenti di fattura (non immagini del profilo ecc.)
INVOICE_EXTENSIONS = {".pdf", ".xml", ".zip", ".p7m", ".png", ".jpg", ".jpeg"}


def _slug(text: str) -> str:
    """Rende una stringa sicura come nome file: 'Fattura 2026 n.12/A' -> 'fattura_2026_n_12_a'."""
    # l'AI può restituire null nei campi opzionali: normalizza a stringa
    if not text or not isinstance(text, str):
        return ""
    text = text.lower().strip()
    text = re.sub(r"[àáâä]", "a", text)
    text = re.sub(r"[èéêë]", "e", text)
    text = re.sub(r"[ìíîï]", "i", text)
    text = re.sub(r"[òóôö]", "o", text)
    text = re.sub(r"[ùúûü]", "u", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")[:80]


def _entity_dir(entity: str, sender: str = "") -> Path:
    name = (entity or "sconosciuto").lower()
    # accetta anche varianti: 'hypaz srl' -> 'hypaz'
    for known in config.INVOICE_ENTITIES:
        if name.startswith(known) or known.startswith(name.split()[0]):
            name = known
            break
    return config.INVOICE_DIR / (_slug(name) or 'sconosciuto')


def save_invoice(entity: str, filename: str, data: bytes, sender: str = "",
                  subject: str = "", source: str = "", date: str = "",
                  description: str = "", number: str = "") -> Path:
    """Salva un allegato fattura nella struttura <entità>/<aaaa-mm>/<nome>.

    Il nome del file è costruito per essere ordinabile e leggibile:
        <aaaa-mm-gg>_<mittente>_<descrizione>[_<numero>].<estensione>
    """
    # l'AI può restituire null nei campi opzionali: normalizza a stringa
    sender = sender or ""
    subject = subject or ""
    source = source or ""
    date = date or ""
    description = description or ""
    number = number or ""

    ext = Path(filename).suffix.lower()
    if ext and ext not in INVOICE_EXTENSIONS:
        ext = ""
    if not ext:
        ext = '.bin'
    if ext == '.pdf' and not data.startswith(b'%PDF-'):
        ext = '.bin'
    digest = hashlib.sha256(data).hexdigest()

    # data della fattura (o di oggi se sconosciuta)
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except (ValueError, TypeError):
        dt = datetime.now()

    month = dt.strftime("%Y-%m")
    day = dt.strftime("%Y-%m-%d")

    # l'entità deve essere una stringa non vuota per il DB
    entity = (entity or "sconosciuto").strip().lower() or "sconosciuto"

    parts = [day]
    if sender:
        parts.append(_slug(sender))
    if description:
        parts.append(_slug(description))
    elif subject:
        parts.append(_slug(subject))
    if number:
        parts.append(_slug(number))
    name = "_".join(parts) + ext

    folder = _entity_dir(entity, sender) / month
    folder.mkdir(parents=True, exist_ok=True)

    dest = folder / (Path(name).stem+'_'+digest[:16]+ext)
    with db._conn() as conn, conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (int(digest[:15],16),))
        existing = conn.execute('SELECT path FROM invoices WHERE content_hash=%s', (digest,)).fetchone()
        if existing:
            return Path(existing[0])
        temporary = dest.with_suffix(dest.suffix+'.tmp')
        temporary.write_bytes(data)
        temporary.replace(dest)
        conn.execute('''INSERT INTO invoices(entity,month,filename,path,sender,subject,source,content_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
            (entity,month,dest.name,str(dest),sender,subject,source,digest))
    print(f"[invoices] 💾 fattura salvata: {dest}")
    return dest


def save_invoice_from_file(entity: str, path, sender: str = "", subject: str = "",
                           source: str = "", date: str = "", description: str = "",
                           number: str = "") -> Path:
    """Versione che legge il contenuto da un file su disco (Telegram/WhatsApp)."""
    path = Path(path)
    return save_invoice(entity, path.name, path.read_bytes(), sender, subject,
                         source, date, description, number)


def month_to_zip(month: str) -> str:
    """Il mese successivo a 'YYYY-MM' (per zippare il mese appena finito)."""
    y, m = month.split("-")
    y, m = int(y), int(m)
    if m == 12:
        return f"{y + 1}-01"
    return f"{y}-{m + 1:02d}"


def previous_month() -> str:
    """Il mese precedente a quello odierno, formato 'YYYY-MM'."""
    now = datetime.now()
    if now.month == 1:
        return f"{now.year - 1}-12"
    return f"{now.year}-{now.month - 1:02d}"


def build_monthly_zip(entity: str, month: str) -> Path | None:
    """Crea lo zip con tutte le fatture di un'entità per un mese."""
    invoices = db.invoices_for_month(entity, month)
    if not invoices:
        return None
    zip_dir = config.DATA_DIR / "zips"
    zip_dir.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError('Invalid invoice month')
    zip_path = zip_dir / f"fatture_{_slug(entity)}_{month}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for inv in invoices:
            p = Path(inv["path"])
            if not p.is_file():
                raise FileNotFoundError('Invoice archive is incomplete')
            zf.write(p, arcname=f"{month}/{p.name}")
    return zip_path


def send_monthly_zips(month: str = None) -> dict:
    """Pipeline 5: ultimo giorno del mese, invia via Telegram gli zip del mese.

    Ritorna un report {entità: esito} per il log/digest.
    """
    month = month or previous_month()
    report = {}
    entities = set(config.INVOICE_ENTITIES) | {m['entity'] for m in db.months_with_invoices() if m['month'] == month}
    for entity in sorted(entities):
        if not flows.enabled('monthly_zip'):
            flows.mark('monthly_zip', 'paused')
            raise RuntimeError('Monthly ZIP flow paused')
        if db.zip_sent(entity, month):
            print(f"[invoices] zip {entity} {month} già inviato, salto")
            report[entity] = "già inviato"
            continue
        zip_path = build_monthly_zip(entity, month)
        if not zip_path:
            print(f"[invoices] nessuna fattura {entity} {month}")
            report[entity] = "nessuna fattura"
            continue
        ok = telegram.send_document(
            zip_path,
            caption=f"📦 Fatture {entity} — {month} ({zip_path.stat().st_size // 1024} KB)",
        )
        if ok:
            db.mark_zip_sent(entity, month, str(zip_path))
            report[entity] = f"inviato ({zip_path.name})"
        else:
            flows.mark('monthly_zip', 'failed')
            report[entity] = "invio fallito, riprovo più tardi"
    return report


def is_month_end() -> bool:
    """True se oggi è l'ultimo giorno del mese."""
    import calendar
    from datetime import date
    today = date.today()
    return today.day == calendar.monthrange(today.year, today.month)[1]