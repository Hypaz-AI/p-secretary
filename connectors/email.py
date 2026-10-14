"""Connettore Email: legge nuove email via IMAP e invia via SMTP."""
import email
import imaplib
import smtplib
import ssl
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr

import config
from connectors import ms_oauth


def _xoauth2(address: str, token: str) -> bytes:
    """Stringa SASL XOAUTH2 per IMAP/SMTP (autenticazione Microsoft)."""
    return f"user={address}\x01auth=Bearer {token}\x01\x01".encode()


def _decode(value) -> str:
    if value is None:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _body_text(msg) -> str:
    """Estrae il corpo testo (preferendo il plain-text)."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
        return ""
    return msg.get_payload(decode=True).decode(
        msg.get_content_charset() or "utf-8", errors="replace"
    )


def _connect(account: dict):
    """Apre una connessione IMAP autenticata per l'account."""
    ctx = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(account["imap_host"], account["imap_port"],
                             ssl_context=ctx, timeout=30)
    if account.get("auth") == "oauth2":
        token = ms_oauth.get_access_token(account)
        imap.authenticate("XOAUTH2", lambda _: _xoauth2(account["address"], token))
    else:
        imap.login(account["address"], account["password"])
    return imap


def _parse_msg(raw: bytes, account: dict) -> dict:
    """Converte i byte RFC822 in dict con mittente, oggetto, corpo, allegati."""
    msg = email.message_from_bytes(raw)
    sender = _decode(msg.get("From"))
    subject = _decode(msg.get("Subject"))
    return {
        "uid": "",
        "account": account["name"],
        "account_address": account["address"],
        "from": parseaddr(sender)[1] or sender,
        "from_name": parseaddr(sender)[0],
        "subject": subject,
        "body": _body_text(msg),
        "attachments": _attachments(msg),
        "date": msg.get("Date", ""),
        "message_id": msg.get('Message-ID'),
    }


def fetch_new_messages(account: dict, seen_uids: set) -> list:
    """Scarica le email non ancora viste. Ritorna lista di dict."""
    messages = []
    try:
        imap = _connect(account)
        imap.select("INBOX", readonly=True)
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        cutoff = (datetime.now(ZoneInfo(config.TIMEZONE)) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        validity_data = imap.response('UIDVALIDITY')[1]
        validity = validity_data[0].decode() if validity_data and validity_data[0] else None
        _, data = imap.uid('search', None, 'SINCE', cutoff.strftime('%d-%b-%Y'))
        uids = data[0].split()
        for uid in uids:
            uid_str = uid.decode()
            identity = json_identity(account['address'].lower(), 'INBOX', validity, uid_str)
            if uid_str in seen_uids or identity in seen_uids:
                continue
            _, msg_data = imap.uid('fetch', uid, '(BODY.PEEK[])')
            raw = msg_data[0][1]
            entry = _parse_msg(raw, account)
            entry["uid"] = uid_str
            timestamp = parse_email_datetime(entry['date'])
            if timestamp and datetime.fromisoformat(timestamp) < cutoff:
                continue
            entry['source_id'] = identity
            entry['metadata'] = {'account': account['address'].lower(), 'folder': 'INBOX',
                                 'uidvalidity': validity, 'uid': uid_str,
                                 'message_id': entry['message_id'], 'direction': 'in'}
            messages.append(entry)
        imap.logout()
    except Exception as exc:  # noqa: BLE001
        print(f"[email] errore su {account['name']}: {exc}")
        import flows
        flows.mark('email', 'failed')
    return messages


def json_identity(account, folder, validity, uid):
    import json
    return json.dumps([account, folder, validity, uid], separators=(',', ':'))


def fetch_day_messages(account: dict, day: str, historical: bool = False) -> list:
    """Rilegge dalla casella TUTTE le email di un giorno (default: oggi),
    ricevute E inviate, per il digest serale. `day` = 'YYYY-MM-DD'."""
    import datetime as _dt
    # IMAP vuole la data in formato inglese: 14-Oct-2026
    d = _dt.datetime.strptime(day, "%Y-%m-%d")
    from zoneinfo import ZoneInfo
    today = _dt.datetime.now(ZoneInfo(config.TIMEZONE)).date()
    if not historical and d.date() not in (today, today - _dt.timedelta(days=1)):
        raise ValueError('Historical import requires historical=True')
    imap_day = d.strftime("%d-%b-%Y")
    # SINCE è inclusivo del giorno stesso; per escludere i successivi
    # filtreremo poi per data locale del messaggio
    day_start = d.strftime("%Y-%m-%d")

    messages = []
    try:
        imap = _connect(account)

        def _collect(folder_candidates: list, direction: str) -> None:
            for folder in folder_candidates:
                try:
                    status, _ = imap.select(folder, readonly=True)
                except Exception:  # noqa: BLE001
                    continue
                if status != "OK":
                    continue
                _, data = imap.uid('search', None, f'(SINCE "{imap_day}")')
                for uid in data[0].split():
                    try:
                        _, msg_data = imap.uid('fetch', uid, '(BODY.PEEK[])')
                        entry = _parse_msg(msg_data[0][1], account)
                    except Exception:  # noqa: BLE001
                        continue
                    # filtra: solo le email del giorno richiesto
                    msg_day = _parse_date_local(entry.get("date", ""), day_start)
                    if msg_day != day_start:
                        continue
                    entry["uid"] = uid.decode()
                    entry["direction"] = direction
                    validity_data = imap.response('UIDVALIDITY')[1]
                    validity = validity_data[0].decode() if validity_data and validity_data[0] else None
                    entry['metadata'] = {'account': account['address'].lower(), 'folder': folder,
                                         'uidvalidity': validity, 'uid': entry['uid'],
                                         'message_id': entry['message_id'], 'direction': direction}
                    entry['source_id'] = json_identity(account['address'].lower(), folder, validity, entry['uid'])
                    messages.append(entry)
                return  # la prima cartella che funziona basta

        # ricevute: INBOX
        _collect(["INBOX"], "in")
        # inviate: nomi comuni della cartella Sent nei vari provider
        _collect(['"[Gmail]/Sent Mail"', '"[Gmail]/Posta inviata"', "Sent", "Sent Items",
                  "Sent Messages", '"Posta inviata"'], "out")
        imap.logout()
    except Exception as exc:  # noqa: BLE001
        print(f"[email] fetch_day_messages errore su {account['name']}: {exc}")
    return messages


def _parse_date_local(date_header: str, fallback: str = "") -> str:
    """Converte l'header Date in 'YYYY-MM-DD' locale (o fallback se fallisce)."""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_header)
        return dt.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return fallback


def parse_email_datetime(date_header: str) -> str | None:
    """Converte l'header Date dell'email in timestamp locale
    'YYYY-MM-DD HH:MM:SS' (None se non parsabile)."""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_header)
        from zoneinfo import ZoneInfo
        if dt.tzinfo is None:
            return None  # unknown timezone is not a true timestamp
        return dt.astimezone(ZoneInfo(config.TIMEZONE)).isoformat()
    except Exception:  # noqa: BLE001
        return None


def _attachments(msg) -> list:
    """Estrae gli allegati (nome, tipo, bytes). Salta le parti inline del corpo."""
    out = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        out.append({
            "filename": _decode(filename),
            "content_type": part.get_content_type(),
            "data": payload,
        })
    return out


def mark_as_read(account: dict, uid: str) -> bool:
    r"""Segna un messaggio come letto (\Seen) senza aprirlo davvero."""
    ctx = ssl.create_default_context()
    try:
        imap = imaplib.IMAP4_SSL(account["imap_host"], account["imap_port"],
                                 ssl_context=ctx, timeout=30)
        if account.get("auth") == "oauth2":
            token = ms_oauth.get_access_token(account)
            imap.authenticate("XOAUTH2", lambda _: _xoauth2(account["address"], token))
        else:
            imap.login(account["address"], account["password"])
        imap.select("INBOX")
        imap.uid('store', uid, '+FLAGS', '\\Seen')
        imap.logout()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[email] mark_as_read fallito su {account['name']}: {exc}")
        return False


def send(account: dict, to: str, subject: str, body: str) -> bool:
    """Invia una email dall'account configurato."""
    try:
        msg = EmailMessage()
        msg["From"] = account["address"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(account["smtp_host"], account["smtp_port"], timeout=30) as smtp:
            smtp.starttls(context=ctx)
            if account.get("auth") == "oauth2":
                token = ms_oauth.get_access_token(account)
                # l'argomento di default rende la lambda compatibile con tutte
                # le versioni di smtplib (con o senza challenge iniziale)
                smtp.auth(account["address"], token, "XOAUTH2",
                          lambda _=b"": _xoauth2(account["address"], token))
            else:
                smtp.login(account["address"], account["password"])
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[email] invio fallito: {exc}")
        return False
