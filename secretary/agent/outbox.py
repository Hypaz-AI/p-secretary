"""Email in uscita: l'agente PROPONE, il proprietario APPROVA con un comando.

Tre gesti distinti, come per ogni azione verso terzi:
1. `propose` (agente): salva una bozza VERA nella cartella Bozze della casella
   (IMAP APPEND \\Draft), così il proprietario la vede e la modifica dal suo
   mail client, e la annuncia su Telegram. Nessun invio è possibile da qui.
2. `decide` (comando slash nativo `/invia N` o `/scarta N`): solo testo diretto
   del proprietario, verificato dal server; rifiutato in modalità `observe`.
   `/scarta` rimuove anche la copia remota, se è ancora una bozza.
3. `pipeline.action` (kind 'email'): invio SMTP con tentativo committato PRIMA
   dell'I/O e ricevuta; un esito incerto non viene mai rinviato alla cieca.

Un solo destinatario, indirizzo nudo, account dalla configurazione: nessuna
lista, nome visualizzato, intestazione o allegato. Il thread (In-Reply-To /
References) è ammesso solo se deriva da un messaggio archiviato, mai dal modello.
"""
import re

from secretary.core import config, db, jobs, policy

MAX_SUBJECT = 200
MAX_BODY = 20000
MAX_REFERENCES = 2000
ADDRESS = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
                     r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                     r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
MESSAGE_ID = re.compile(r'<[^<>\s\x00-\x1f]{1,900}>')


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


def _thread(conn, source_message, recipient):
    """In-Reply-To/References dal messaggio archiviato a cui si risponde.

    Il modello non fornisce mai intestazioni: il server le ricava dal messaggio
    nativo, dopo aver verificato il consenso e che il destinatario sia il suo
    mittente. Un messaggio senza Message-ID valido resta senza thread.
    """
    if source_message is None:
        return None, None
    if type(source_message) is not int or not 0 < source_message <= 9223372036854775807:
        raise OutboxError('reply_to_message_id non valido')
    if not policy.can_read_context(conn, source_message, 'ai_text'):
        raise OutboxError('Consenso dati email non attivo per il messaggio a cui rispondere')
    row = conn.execute('SELECT sender,direction,metadata FROM email_messages WHERE id=%s',
                       (source_message,)).fetchone()
    if not row:
        raise OutboxError('Messaggio di origine non trovato: deve essere una email archiviata')
    sender, direction, metadata = row
    if direction != 'in':
        raise OutboxError('Si risponde solo a email ricevute')
    from email.utils import parseaddr
    sender_address = parseaddr(sender or '')[1].lower()
    if sender_address != recipient.lower():
        raise OutboxError(f'Il destinatario deve essere il mittente del messaggio: '
                          f'{sender_address or "sconosciuto"}')
    metadata = metadata if isinstance(metadata, dict) else {}
    message_id = metadata.get('message_id')
    if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
        return None, None
    references = metadata.get('references')
    references = ' '.join(references.split())[:MAX_REFERENCES] if isinstance(references, str) else ''
    return message_id, (references + ' ' + message_id).strip()[:MAX_REFERENCES]


def propose(conn, account, recipient, subject, body, proposed_by_message, *, source_message=None):
    """Registra la bozza (nessuna I/O qui) e restituisce il suo ID.

    Il salvataggio nella cartella Bozze avviene dopo il commit, in
    `publish_draft`: la riga è l'attempt committato prima dell'effetto esterno.
    """
    account_for(account)
    address(recipient)
    subject, body = _clean(subject, body)
    in_reply_to, references = _thread(conn, source_message, recipient)
    row = conn.execute('''INSERT INTO agent_email_outbox(account,recipient,subject,body,proposed_by_message,
        source_message,in_reply_to,references_header,draft_state)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING id''',
        (account, recipient, subject, body, proposed_by_message, source_message,
         in_reply_to, references)).fetchone()
    identity = row[0]
    return dict(draft_id=identity, state='proposed', draft_state='pending',
                note='Bozza registrata: la salvo nella cartella Bozze della casella e ti avviso')


def _draft_text(identity, account, recipient, subject, body, outcome, error):
    preview = body if len(body) <= 1500 else body[:1500] + '…'
    if outcome == 'saved':
        where = f'📥 La trovi nella cartella Bozze di {account}: aprila dal tuo client per modificarla o inviarla.'
    else:
        where = (f'⚠️ Non sono riuscito a salvarla nella cartella Bozze ({error}). '
                 f'La bozza resta qui e nella dashboard.')
    return (f'✉️ Bozza email #{identity}\nDa: {account}\nA: {recipient}\nOggetto: {subject}\n---\n{preview}\n---\n'
            f'{where}\n/invia {identity} per farla inviare da me · /scarta {identity} per eliminarla')


def publish_draft(identity):
    """Salva la bozza nella casella (I/O fuori transazione) e avvisa il proprietario.

    Idempotente: se la riga non è più `pending` non fa nulla. Un crash tra il
    commit e l'IMAP lascia `pending` e viene recuperato da `retry_pending`.
    """
    from secretary.connectors import email as mailbox
    jobs.assert_current()
    with db._conn() as conn:
        row = conn.execute('''SELECT account,recipient,subject,body,draft_state,draft_folder,draft_uid,
            in_reply_to,references_header,proposed_by_message FROM agent_email_outbox WHERE id=%s''',
            (identity,)).fetchone()
    if not row:
        return dict(error='Bozza inesistente')
    (account, recipient, subject, body, draft_state, folder, uid, in_reply_to, references,
     proposed_by) = row
    if draft_state != 'pending':
        return dict(draft_id=identity, draft_state=draft_state, folder=folder, uid=uid)
    saved, outcome, error = {}, 'saved', None
    try:
        saved = mailbox.save_draft(account_for(account), recipient, subject, body,
                                   in_reply_to=in_reply_to, references=references)
    except Exception as exc:  # noqa: BLE001
        outcome = 'failed'
        error = str(exc)[:200] if isinstance(exc, mailbox.DraftError) else type(exc).__name__
    with db._conn() as conn, conn.transaction():
        conn.execute('''UPDATE agent_email_outbox SET draft_state=%s,draft_folder=%s,draft_uid=%s,
            draft_message_id=%s,draft_error=%s WHERE id=%s AND draft_state='pending' ''',
            (outcome, saved.get('folder'), saved.get('uid'), saved.get('message_id'), error, identity))
        # evidence_message_id (non message_id): una notifica di proposta non deve
        # marcare come «notificata» l'analisi di un messaggio. Senza provenienza
        # nativa la notifica non sarebbe autorizzabile: non la si accoda affatto.
        if proposed_by is not None:
            jobs.enqueue(conn, 'action', f'email-draft:{identity}',
                         dict(type='notify', html=False, evidence_message_id=proposed_by,
                              text=_draft_text(identity, account, recipient, subject, body, outcome, error)))
    return dict(draft_id=identity, draft_state=outcome, folder=saved.get('folder'), uid=saved.get('uid'))


def retry_pending(limit=5):
    """Recupera i salvataggi interrotti da un crash (righe `pending` più vecchie di 5 minuti)."""
    with db._conn() as conn:
        rows = conn.execute("""SELECT id FROM agent_email_outbox WHERE draft_state='pending'
            AND created_at < now() - interval '5 minutes' ORDER BY id LIMIT %s""", (limit,)).fetchall()
    return [publish_draft(row[0]) for row in rows]


def decide(conn, identity, owner_message, *, approve):
    """Gesto esplicito del proprietario (comando nativo). Ritorna il testo di risposta."""
    row = conn.execute('''SELECT state,account,proposed_by_message,draft_folder,draft_uid
        FROM agent_email_outbox WHERE id=%s FOR UPDATE''', (identity,)).fetchone()
    if not row:
        return 'Bozza non trovata.'
    if row[0] != 'proposed':
        return f'Bozza #{identity} già in stato «{row[0]}»: nessuna modifica.'
    if not approve:
        conn.execute("UPDATE agent_email_outbox SET state='rejected',decided_at=now() WHERE id=%s", (identity,))
        if row[3] and row[4]:
            jobs.enqueue(conn, 'action', f'email-draft-cleanup:{identity}',
                         dict(type='email_draft_cleanup', id=identity, evidence_message_id=row[2]))
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


def draft_guard(identity):
    """Esito del controllo sulla copia remota prima dell'invio.

    * ``True``  — nessuna copia remota, oppure è ancora una bozza: si può inviare;
    * ``False`` — la copia non è più una bozza (già inviata/cancellata dal client):
      inviare di nuovo duplicherebbe, quindi l'invio va fermato;
    * ``None``  — non verificabile (errore IMAP): incerto, non si invia alla cieca
      ma il job può essere ritentato più tardi.
    """
    from secretary.connectors import email as mailbox
    with db._conn() as conn:
        row = conn.execute('''SELECT account,draft_state,draft_folder,draft_uid
            FROM agent_email_outbox WHERE id=%s''', (identity,)).fetchone()
    if not row:
        return False
    account, state, folder, uid = row
    if state != 'saved' or not folder or not uid:
        return True
    try:
        return mailbox.draft_exists(account_for(account), folder, uid)
    except Exception:  # noqa: BLE001
        return None


def cleanup_remote(identity):
    """Rimuove la copia remota di una bozza terminale (scartata o inviata).

    Ritorna True se non c'era nulla da rimuovere (già inviata/cancellata dal
    client) o se la rimozione è riuscita; False solo su errore IMAP.
    """
    from secretary.connectors import email as mailbox
    with db._conn() as conn:
        row = conn.execute('''SELECT account,draft_folder,draft_uid FROM agent_email_outbox
            WHERE id=%s AND state IN ('rejected','sent')''', (identity,)).fetchone()
    if not row or not row[1] or not row[2]:
        return True
    try:
        mailbox.delete_draft(account_for(row[0]), row[1], row[2])
        return True
    except Exception:  # noqa: BLE001
        return False


def mark(conn, identity, state):
    if state not in ('sent', 'failed', 'uncertain'):
        raise ValueError('Invalid outbox state')
    conn.execute('''UPDATE agent_email_outbox SET state=%s,sent_at=CASE WHEN %s='sent' THEN now() END
        WHERE id=%s AND state IN ('approved','uncertain')''', (state, state, identity))
