"""Lettura diretta e SOLA LETTURA delle caselle email (IMAP), oltre all'archivio.

Serve per ciò che non è ancora (o non sarà mai) nell'archivio: altre cartelle,
messaggi più vecchi della finestra di acquisizione, ricerche sul server. Mai
modifica la casella: `select(readonly=True)`, `BODY.PEEK`, nessun flag, nessuna
cancellazione, nessun invio. I criteri IMAP sono costruiti da parametri
strutturati e validati, mai da testo libero del modello. Il consenso `email /
ai_text` della dashboard è verificato a ogni chiamata.
"""
import email as stdlib_email
import re
from datetime import date

from secretary.core import config, db
from secretary.connectors import email as mailbox

MAX_RESULTS = 25
MAX_BODY = 60000
FOLDER = re.compile(r'[A-Za-z0-9 ._/\[\]-]{1,80}')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


class MailError(ValueError):
    pass


def accounts():
    """Account configurati, senza credenziali."""
    return [dict(address=a['address'], name=a['name'], provider=a['provider']) for a in config.EMAIL_ACCOUNTS]


def _account(address):
    matches = [a for a in config.EMAIL_ACCOUNTS if a['address'] == address]
    if len(matches) != 1:
        known = ', '.join(a['address'] for a in config.EMAIL_ACCOUNTS) or 'nessuno'
        raise MailError(f'Account sconosciuto. Disponibili: {known}')
    return matches[0]


def _consent():
    with db._conn() as conn:
        row = conn.execute("SELECT allowed FROM data_policies WHERE channel='email' AND effect='ai_text'").fetchone()
    if not row or row[0] is not True:
        raise MailError('Consenso dati email non attivo nella dashboard')


def _quote(value, field):
    if type(value) is not str or not value.strip() or len(value) > 120:
        raise MailError(f'{field}: testo non valido')
    if not value.isascii() or re.search(r'[\x00-\x1f\x7f]', value):
        raise MailError(f'{field}: usa solo caratteri ASCII semplici (per accenti usa una parte della parola)')
    return '"' + value.strip().replace('\\', '\\\\').replace('"', '\\"') + '"'


def _day(value, field):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise MailError(f'{field}: usa il formato YYYY-MM-DD') from None
    return f'{parsed.day:02d}-{MONTHS[parsed.month - 1]}-{parsed.year}'


def _folder(value):
    if type(value) is not str or not FOLDER.fullmatch(value) or '..' in value:
        raise MailError('Cartella non valida')
    return '"' + value + '"'


def _open(account, folder):
    imap = mailbox._connect(account)
    status, _ = imap.select(_folder(folder), readonly=True)
    if status != 'OK':
        imap.logout()
        raise MailError('Cartella non trovata')
    return imap


def search(address, *, sender=None, to=None, subject=None, text=None, since=None, before=None,
           folder='INBOX', limit=15):
    _consent()
    account = _account(address)
    criteria = []
    for keyword, value, label in (('FROM', sender, 'sender'), ('TO', to, 'to'),
                                  ('SUBJECT', subject, 'subject'), ('TEXT', text, 'text')):
        if value:
            criteria += [keyword, _quote(value, label)]
    if since:
        criteria += ['SINCE', _day(since, 'since')]
    if before:
        criteria += ['BEFORE', _day(before, 'before')]
    if not criteria:
        criteria = ['ALL']
    limit = max(1, min(int(limit), MAX_RESULTS)) if type(limit) is int else 15
    imap = _open(account, folder)
    try:
        status, found = imap.uid('search', None, *criteria)
        if status != 'OK':
            raise MailError('Ricerca rifiutata dal server')
        uids = found[0].split()[-limit:][::-1]
        results = []
        for uid in uids:
            status, data = imap.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] RFC822.SIZE)')
            if status != 'OK' or not data or not isinstance(data[0], tuple):
                continue
            headers = stdlib_email.message_from_bytes(data[0][1])
            size = re.search(rb'RFC822\.SIZE (\d+)', data[0][0])
            results.append(dict(uid=uid.decode(), sender=mailbox._decode(headers.get('From')),
                                subject=mailbox._decode(headers.get('Subject')),
                                date=mailbox.parse_email_datetime(headers.get('Date', '')) or headers.get('Date'),
                                size=int(size.group(1)) if size else None))
        return dict(account=address, folder=folder, total_matches=len(found[0].split()), results=results,
                    untrusted_content=True)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def read(address, uid, *, folder='INBOX', max_chars=20000):
    _consent()
    account = _account(address)
    if type(uid) is not str or not uid.isascii() or not uid.isdigit() or len(uid) > 12:
        raise MailError('uid non valido (usa quello restituito da mail_search)')
    max_chars = max(200, min(int(max_chars), MAX_BODY))
    imap = _open(account, folder)
    try:
        status, data = imap.uid('fetch', uid, '(BODY.PEEK[])')
        if status != 'OK' or not data or not isinstance(data[0], tuple):
            raise MailError('Messaggio non trovato')
        parsed = mailbox._parse_msg(data[0][1], account)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    body = parsed['body'] or ''
    return dict(account=address, folder=folder, uid=uid, sender=parsed['from'], subject=parsed['subject'],
                date=mailbox.parse_email_datetime(parsed['date']) or parsed['date'],
                to=parsed['recipients'].get('to', [])[:20], cc=parsed['recipients'].get('cc', [])[:20],
                body=body[:max_chars], body_chars=len(body), truncated=len(body) > max_chars,
                attachments=[dict(filename=a['filename'], content_type=a['content_type'], size=len(a['data']))
                             for a in parsed['attachments'][:30]],
                untrusted_content=True)
