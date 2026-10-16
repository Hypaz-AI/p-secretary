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


def _download(file_id: str, dest: Path, max_bytes=None) -> Path:
    """Scarica un file dal bot e lo salva in dest."""
    info = _call("getFile", {"file_id": file_id})
    path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if max_bytes is None:
        urllib.request.urlretrieve(url, dest)
    else:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError('Media oltre il limite di download')
        dest.write_bytes(data)
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


def _media_attachments(msg, download_dir):
    """Download native voice/photo media locally; never send token-bearing URLs to AI."""
    if not download_dir:
        return []
    media = msg.get('voice') or msg.get('audio')
    mime = (media or {}).get('mime_type', 'audio/ogg')
    suffix = mimetypes.guess_extension(mime) or '.ogg'
    if not media and msg.get('photo'):
        media = max(msg['photo'], key=lambda p: p.get('width', 0) * p.get('height', 0))
        mime, suffix = 'image/jpeg', '.jpg'
    if not media:
        return []
    from connectors.media_text import MAX_BYTES
    if media.get('file_size', 0) > MAX_BYTES:
        # Preserve a visible explanation without repeatedly downloading oversized media.
        return []
    filename = f"{Path(media['file_unique_id']).name}{suffix}"
    dest = Path(download_dir) / filename
    _download(media['file_id'], dest, max_bytes=MAX_BYTES)
    attachment = {'filename': filename, 'content_type': mime, 'path': str(dest)}
    if 'duration' in media:
        attachment['duration'] = media['duration']
    return [attachment]


def is_owner_chat(chat_id, chat_type) -> bool:
    """Fail closed: only the configured owner's private bot chat is allowed."""
    owner = str(config.TELEGRAM_CHAT_ID or '').strip()
    return bool(owner and owner.isdecimal() and int(owner) > 0
                and str(chat_id) == owner and chat_type == 'private')


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
            chat = msg.get('chat') or {}
            if not is_owner_chat(chat.get('id'), chat.get('type')):
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
            try:
                media_attachments = _media_attachments(msg, download_dir)
            except Exception:
                offset = update['update_id']
                raise
            text = msg.get('text') or msg.get('caption') or ''
            if (msg.get('voice') or msg.get('audio') or msg.get('photo')) and not media_attachments:
                text += '\n[Media non scaricato: limite 10 MiB o download non disponibile]'
            native = {'msg_date': dt.isoformat() if dt else None,
                'source_id': json.dumps(['bot', config.TELEGRAM_BOT_TOKEN.split(':')[0], chat_id, msg.get('message_id')]),
                'metadata': {'account': 'bot:' + config.TELEGRAM_BOT_TOKEN.split(':')[0],
                             'chat_id': chat_id, 'chat_type': chat['type'],
                             'message_id': msg.get('message_id'),
                             'author': msg.get('from', {}).get('id'), 'direction': 'in',
                             'chat_name': chat_name}}
            entry = {
                **native,
                "chat_id": chat_id,
                "chat_type": chat['type'],
                "from": _full_name(msg.get("from")),
                "text": text,
                "attachments": list(media_attachments),
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
