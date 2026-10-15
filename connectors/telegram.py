"""Connettore Telegram: invia notifiche e file, legge i messaggi in arrivo."""
import html as html_mod
import json
import mimetypes
import urllib.request
from pathlib import Path

import config

API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
_poll_lock = None


def acquire_poll_lock():
    """Keep ownership for process lifetime, not just a single getUpdates call."""
    global _poll_lock
    if _poll_lock is None:
        from connectors.storage import FileLock, bot_lock_path
        lock = FileLock(bot_lock_path(config.TELEGRAM_BOT_TOKEN), blocking=False)
        lock.__enter__()
        _poll_lock = lock


def _call(method: str, params: dict) -> dict:
    payload = json.dumps(params).encode()
    req = urllib.request.Request(
        f"{API}/{method}", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _download(file_id: str, dest: Path) -> Path:
    """Scarica un file dal bot e lo salva in dest."""
    info = _call("getFile", {"file_id": file_id})
    path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    return dest


def notify(text: str, html: bool = True) -> bool:
    """Invia un messaggio all'utente (chat configurata).

    Con html=True i tag <b> <i> <code> sono interpretati; il testo dinamico
    va escapato con escape_html(). Con testo generato dall'AI usa html=False.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[telegram] non configurato, notifica saltata")
        return False
    params = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text[:4000],  # limite Telegram
    }
    if html:
        params["parse_mode"] = "HTML"
    try:
        _call("sendMessage", params)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[telegram] invio fallito: {exc}")
        return False


def escape_html(text: str) -> str:
    """Escape dei caratteri speciali per il parse_mode HTML di Telegram."""
    return html_mod.escape(str(text or ""))


def send_document(path, caption: str = "") -> bool:
    """Invia un file (es. zip di fatture) all'utente via bot."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[telegram] non configurato, invio file saltato")
        return False
    path = Path(path)
    if not path.exists():
        print(f"[telegram] file inesistente: {path}")
        return False
    try:
        import uuid
        boundary = f"----pi-secretary-{uuid.uuid4().hex}"
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        parts = []
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
            f"{config.TELEGRAM_CHAT_ID}\r\n".encode())
        if caption:
            parts.append(
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"caption\"\r\n\r\n"
                f"{caption}\r\n".encode())
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"document\"; "
            f"filename=\"{path.name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n".encode()
            + path.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            f"{API}/sendDocument", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            json.loads(resp.read().decode())
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[telegram] invio file fallito: {exc}")
        return False


def _full_name(user: dict) -> str:
    """Nome visualizzabile di un utente Telegram: nome + cognome, con
    fallback su username e infine sull'ID numerico."""
    if not user:
        return "?"
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    if name:
        username = user.get("username")
        return f"{name} (@{username})" if username else name
    if user.get("username"):
        return f"@{user['username']}"
    return str(user.get("id") or "?")


def _chat_name(chat: dict) -> str:
    """Nome visualizzabile di una chat Telegram (privata, gruppo o canale)."""
    if not chat:
        return ""
    for key in ("title", "first_name", "last_name", "username"):
        if chat.get(key):
            return str(chat[key])
    return str(chat.get("id") or "")


def fetch_new_messages(offset: int, download_dir=None) -> tuple:
    """Legge i nuovi update. Ritorna (lista messaggi, nuovo offset).

    Ogni messaggio può avere 'document' (file ricevuto dall'utente, es. una
    fattura in PDF): viene scaricato in download_dir e riportato come
    allegato con percorso locale.
    """
    messages = []
    if not config.TELEGRAM_BOT_TOKEN:
        return messages, offset
    try:
        acquire_poll_lock()
    except OSError:
        print('[telegram] polling occupato; nessun update consumato')
        return messages, offset
    # solo la chat configurata può dialogare col segretario:
    # chiunque altro scriva al bot viene ignorato (anti prompt-injection)
    authorized = str(config.TELEGRAM_CHAT_ID) if config.TELEGRAM_CHAT_ID else None
    try:
        result = _call("getUpdates", {
            "offset": offset,
            "timeout": 0,
            "allowed_updates": ["message"],
        })
        for update in result.get("result", []):
            offset = max(offset, update["update_id"] + 1)
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(config.TIMEZONE))
            start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(1,min(90,int(config.get('RECOVERY_DAYS','14')))))
            dt = datetime.fromtimestamp(msg['date'], ZoneInfo(config.TIMEZONE)) if msg.get('date') else None
            if dt and not start <= dt < now + timedelta(minutes=5):
                continue
            chat_id = str(msg["chat"]["id"])
            chat_name = _chat_name(msg.get("chat"))
            native = {'msg_date': dt.isoformat() if dt else None,
                'source_id': json.dumps(['bot', config.TELEGRAM_BOT_TOKEN.split(':')[0], chat_id, msg.get('message_id')]),
                'metadata': {'account': 'bot:' + config.TELEGRAM_BOT_TOKEN.split(':')[0],
                             'chat_id': chat_id, 'message_id': msg.get('message_id'),
                             'author': msg.get('from', {}).get('id'), 'direction': 'in',
                             'chat_name': chat_name}}
            if not authorized or chat_id != authorized:
                who = _full_name(msg.get("from"))
                username = (msg.get("from", {}).get("username") or "")
                print(f"[telegram] ⚠️ messaggio da chat non autorizzata "
                      f"{chat_id} ({who}): {str(msg.get('text', ''))[:60]}")
                # NON scartare: loggalo come comunicazione in arrivo da altri
                # (il segretario legge tutto; solo il dialogo è riservato a te)
                entry = {
                    **native,
                    "chat_id": chat_id,
                    "from": who,
                    "text": msg.get("text", ""),
                    "attachments": [],
                    "unauthorized": True,
                }
                doc = msg.get("document")
                if doc and download_dir:
                    dest = Path(download_dir) / f"{doc['file_unique_id']}_{doc['file_name']}"
                    try:
                        _download(doc["file_id"], dest)
                        entry["attachments"].append({
                            "filename": doc["file_name"],
                            "content_type": doc.get("mime_type", "application/octet-stream"),
                            "path": str(dest),
                        })
                        entry["text"] = entry["text"] or f"(file: {doc['file_name']})"
                    except Exception as exc:  # noqa: BLE001
                        print(f"[telegram] download allegato fallito: {exc}")
                if entry["text"] or entry["attachments"]:
                    messages.append(entry)
                continue
            entry = {
                **native,
                "chat_id": chat_id,
                "from": _full_name(msg.get("from")),
                "text": msg.get("text", ""),
                "attachments": [],
            }
            doc = msg.get("document")
            if doc and download_dir:
                dest = Path(download_dir) / f"{doc['file_unique_id']}_{doc['file_name']}"
                try:
                    _download(doc["file_id"], dest)
                    entry["attachments"].append({
                        "filename": doc["file_name"],
                        "content_type": doc.get("mime_type", "application/octet-stream"),
                        "path": str(dest),
                    })
                    entry["text"] = entry["text"] or f"(file: {doc['file_name']})"
                except Exception as exc:  # noqa: BLE001
                    print(f"[telegram] download allegato fallito: {exc}")
            if not entry["text"] and not entry["attachments"]:
                continue
            messages.append(entry)
    except Exception as exc:  # noqa: BLE001
        print(f"[telegram] lettura fallita: {exc}")
        import flows
        flows.mark('telegram_bot', 'failed')
    return messages, offset
