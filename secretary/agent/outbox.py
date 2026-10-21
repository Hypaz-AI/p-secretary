"""Email in uscita: l'agente PROPONE, il proprietario APPROVA con un comando.

Tre gesti distinti, come per ogni azione verso terzi:
1. `propose` (agente): salva una bozza e la mostra al proprietario su Telegram.
   Nessun invio è possibile da qui.
2. `decide` (comando slash nativo `/invia N` o `/scarta N`): solo testo diretto
   del proprietario, verificato dal server; rifiutato in modalità `observe`.
3. `pipeline.action` (kind 'email'): invio SMTP con tentativo committato PRIMA
   dell'I/O e ricevuta; un esito incerto non viene mai rinviato alla cieca.

Un solo destinatario, indirizzo nudo, account dalla configurazione: nessuna
lista, nome visualizzato, intestazione o allegato.
"""
import re

from secretary.core import config, jobs, policy

MAX_SUBJECT = 200
MAX_BODY = 20000
ADDRESS = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
                     r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                     r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def sends_allowed():
    """True solo in modalità active: invio a terzi esplicitamente approvato dall'owner."""
    return config.SECRETARY_MODE == 'active'


class OutboxError(ValueError):
    """Errore mostrabile al modello o al proprietario."""


def address(value):
    if type(value) is not str or len(value) > 254 or not value.isascii() or not ADDRESS.fullmatch(value) \
            or len(value.split('@', 1)[0]) > 64:
        raise OutboxError('Serve un solo indirizzo email esplicito (senza nome, liste o spazi)')
    return value


def account_for(value):
    """Account SMTP dalla configurazione, per indirizzo esatto (mai per indice o nome)."""
    address(value)
    matches = [dict(a) for a in config.EMAIL_ACCOUNTS if str(a.get('address', '')) == value]
    if len(matches) != 1:
        known = ', '.join(sorted(str(a.get('address')) for a in config.EMAIL_ACCOUNTS))
        raise OutboxError(f'Account di invio sconosciuto. Disponibili: {known or "nessuno"}')
    account = matches[0]
    if (not account.get('smtp_host') or type(account.get('smtp_port')) is not int
            or account.get('auth') not in ('basic', 'oauth2')):
        raise OutboxError('Account senza SMTP configurato')
    return account


def _clean(subject, body):
    if type(subject) is not str or not subject.strip() or len(subject) > MAX_SUBJECT \
            or re.search(r'[\r\n\x00]', subject):
        raise OutboxError('Oggetto non valido (una riga, massimo 200 caratteri)')
    if type(body) is not str or not body.strip() or len(body) > MAX_BODY or '\x00' in body:
        raise OutboxError('Testo non valido (massimo 20000 caratteri)')
    return subject.strip(), body.strip()


def propose(conn, account, recipient, subject, body, proposed_by_message):
    """Salva la bozza e avvisa il proprietario. Nessun invio."""
    account_for(account)
    address(recipient)
    subject, body = _clean(subject, body)
    row = conn.execute('''INSERT INTO agent_email_outbox(account,recipient,subject,body,proposed_by_message)
        VALUES (%s,%s,%s,%s,%s) RETURNING id''',
        (account, recipient, subject, body, proposed_by_message)).fetchone()
    identity = row[0]
    preview = body if len(body) <= 1500 else body[:1500] + '…'
    text = (f'✉️ Bozza email #{identity}\nDa: {account}\nA: {recipient}\nOggetto: {subject}\n---\n{preview}\n---\n'
            f'/invia {identity} per inviarla · /scarta {identity} per scartarla')
    jobs.enqueue(conn, 'action', f'email-draft:{identity}',
                 dict(type='notify', text=text, html=False, message_id=proposed_by_message))
    return dict(draft_id=identity, state='proposed',
                note='Bozza salvata e mostrata al proprietario: l\'invio richiede il suo comando /invia')


def decide(conn, identity, owner_message, *, approve):
    """Gesto esplicito del proprietario (comando nativo). Ritorna il testo di risposta."""
    row = conn.execute('SELECT state,account FROM agent_email_outbox WHERE id=%s FOR UPDATE', (identity,)).fetchone()
    if not row:
        return 'Bozza non trovata.'
    if row[0] != 'proposed':
        return f'Bozza #{identity} già in stato «{row[0]}»: nessuna modifica.'
    if not approve:
        conn.execute("UPDATE agent_email_outbox SET state='rejected',decided_at=now() WHERE id=%s", (identity,))
        return f'Bozza #{identity} scartata.'
    if not sends_allowed():
        return ('Invio a terzi disattivato (modalità observe). La bozza resta salvata: attiva la modalità '
                'active dalla dashboard e ripeti /invia ' + str(identity) + '.')
    try:
        account_for(row[1])
    except OutboxError as exc:
        return str(exc)
    if not policy.can_operate(conn, owner_message, 'email_send', owner_required=True):
        raise PermissionError('Owner email permission required')
    conn.execute('''UPDATE agent_email_outbox SET state='approved',decided_at=now(),approved_by_message=%s
        WHERE id=%s''', (owner_message, identity))
    jobs.enqueue(conn, 'action', f'email-send:{identity}', dict(type='email', id=identity))
    return f'Invio della bozza #{identity} in coda. L\'accettazione da parte del server SMTP non prova la consegna.'


def load_approved(conn, identity):
    row = conn.execute('''SELECT account,recipient,subject,body,approved_by_message FROM agent_email_outbox
        WHERE id=%s AND state='approved' ''', (identity,)).fetchone()
    return dict(zip(('account', 'recipient', 'subject', 'body', 'approved_by_message'), row)) if row else None


def mark(conn, identity, state):
    if state not in ('sent', 'failed', 'uncertain'):
        raise ValueError('Invalid outbox state')
    conn.execute('''UPDATE agent_email_outbox SET state=%s,sent_at=CASE WHEN %s='sent' THEN now() END
        WHERE id=%s AND state IN ('approved','uncertain')''', (state, state, identity))
