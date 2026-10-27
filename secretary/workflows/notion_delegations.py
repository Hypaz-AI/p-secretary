"""Deleghe al Custom Agent Notion (R1: SOLA LETTURA).

Flusso: il proprietario chiede a Jarvis, Jarvis formula UNA domanda
autosufficiente e la passa allo specialista Notion configurato lato server.

Garanzie:
* attempt-before-I/O: la riga `notion_delegations` (chiave turno+richiesta) è
  committata PRIMA della POST di creazione sessione; un esito ambiguo marca
  'uncertain' e NON viene mai rigiocato (niente sessioni duplicate);
* nessuna transazione SQL aperta durante le chiamate HTTP;
* il completamento lento è un job durevole (`notion`) che fa polling e avvisa
  il proprietario UNA volta (chiave `notion-result:<id>` + ricevute);
* `requires_action` non è mai approvato da qui: in sola lettura la delega
  viene rifiutata e segnalata.
"""
import time
import uuid

from secretary.core import config, db, jobs
from secretary.agent import journal
from secretary.connectors import notion_agent

VERSION = 'notion_delegations_v1'
VERSION_V2 = 'notion_delegations_v2'
TERMINAL = ('completed', 'failed', 'uncertain', 'refused')
NOTIFY_ANSWER_CHARS = 2500

SCHEMA = """
CREATE TABLE IF NOT EXISTS notion_delegations (
    id                  UUID PRIMARY KEY,
    turn_key            TEXT NOT NULL,
    owner_message_id    BIGINT NOT NULL,
    request_hash        TEXT NOT NULL,
    notion_session_id   TEXT,
    status              TEXT NOT NULL DEFAULT 'submitting',
    answer              TEXT,
    last_event_sequence BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (turn_key, request_hash)
);
"""


def migrate(conn):
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            conn.execute(SCHEMA)
            conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))
        if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION_V2,)).fetchone():
            # Installazioni create con la v1: aggiunge la colonna del cursore eventi.
            conn.execute('ALTER TABLE notion_delegations '
                         'ADD COLUMN IF NOT EXISTS last_event_sequence BIGINT NOT NULL DEFAULT 0')
            conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION_V2,))


def _mark(delegation_id, status, answer=None, last_sequence=None):
    with db._conn() as conn, conn.transaction():
        conn.execute('''UPDATE notion_delegations SET status=%s,
            answer=COALESCE(%s,answer),
            last_event_sequence=GREATEST(last_event_sequence, COALESCE(%s,last_event_sequence)),
            updated_at=now() WHERE id=%s''',
                     (status, answer, last_sequence, str(delegation_id)))


def _receipt(delegation_id, status, answer=None):
    """Risposta del tool per una delega già registrata (ripetizione del modello o retry del turno)."""
    if status == 'completed':
        return dict(status='completed', delegation_id=str(delegation_id), answer=answer or '',
                    receipt='Ricerca Notion già completata (nessuna nuova richiesta inviata).')
    if status in ('failed', 'refused'):
        return dict(status=status, delegation_id=str(delegation_id),
                    receipt='Questa identica richiesta a Notion è già stata gestita; non la ripeto.')
    if status in ('submitting', 'uncertain'):
        return dict(status='uncertain', delegation_id=str(delegation_id),
                    receipt="L'invio di questa richiesta a Notion non è verificabile: non la ripeto "
                            'per evitare duplicati. Serve un controllo manuale.')
    return dict(status='queued', delegation_id=str(delegation_id),
                receipt='Questa richiesta a Notion è già in corso: ti avviso quando è pronta.')


def _poll_once(delegation_id, session_id, after_sequence=0):
    """Un controllo di stato. Ritorna il risultato se la sessione è conclusa, altrimenti None."""
    session = notion_agent.retrieve_session(session_id)
    status = str(session.get('status') or '')
    if status in ('queued', 'in_progress'):
        return None
    if status == 'completed':
        # Solo i messaggi del turno appena inviato: mai la risposta di un turno precedente.
        answer, last, links = notion_agent.agent_messages(session_id, after_sequence=after_sequence)
        answer = answer.strip() or '(lo specialista Notion non ha lasciato un messaggio finale)'
        _mark(delegation_id, 'completed', answer, last)
        return dict(status='completed', delegation_id=str(delegation_id), answer=answer, links=links,
                    receipt='Ricerca Notion completata; nessuna modifica effettuata.')
    if status == 'requires_action':
        _mark(delegation_id, 'refused')
        return dict(status='refused', delegation_id=str(delegation_id),
                    receipt="Notion chiede un'approvazione: l'integrazione è in sola lettura e non la concede. "
                            'Nessuna azione è stata autorizzata da qui.')
    _mark(delegation_id, 'failed')
    return dict(status='failed', delegation_id=str(delegation_id),
                receipt=f'La richiesta a Notion non è stata completata (stato: {status or "sconosciuto"}). '
                        'Nessuna modifica effettuata.')


def _enqueue_monitor(conn, delegation_id):
    return jobs.enqueue(conn, 'notion', f'notion-poll:{delegation_id}',
                        dict(delegation_id=str(delegation_id)))


def _notify(conn, delegation_id, owner_message_id, text):
    """Notifica Telegram deduplicata: la chiave stabile la rende una sola anche dopo i retry."""
    jobs.enqueue(conn, 'action', f'notion-result:{delegation_id}',
                 dict(type='notify', text=text, html=False, evidence_message_id=owner_message_id))


def _notify_text(status, answer=None, receipt=None):
    if status == 'completed':
        body = (answer or '').strip() or '(nessun messaggio finale dallo specialista)'
        return '📚 Notion: ' + body[:NOTIFY_ANSWER_CHARS]
    return '📚 Notion: ' + (receipt or 'richiesta non completata; nessuna modifica effettuata.')


def delegate(state, question, continue_previous=False):
    """Invia UNA domanda di lettura allo specialista Notion (solo tool notion_ask, modo owner).

    Con continue_previous=True il server (mai il modello) sceglie l'ultima sessione
    conclusa e la prosegue: un turno in volo per sessione, nessun ID dal modello.
    """
    question = question.strip()
    request_hash = journal.effect_key('notion_ask', dict(question=question, follow=bool(continue_previous)))
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        row = conn.execute('''SELECT id,status,answer,notion_session_id FROM notion_delegations
            WHERE turn_key=%s AND request_hash=%s''', (state.turn_key, request_hash)).fetchone()
        if row:
            delegation_id, status, answer, session_id = row
            if status == 'running' and session_id:
                # Retry del turno o ripetizione del modello: riallaccia il monitor
                # (enqueue è idempotente sulla chiave) senza una seconda POST.
                _enqueue_monitor(conn, delegation_id)
            return _receipt(delegation_id, status, answer)
        session_id, after_sequence = None, 0
        if continue_previous:
            prev = conn.execute('''SELECT notion_session_id,status,last_event_sequence
                FROM notion_delegations WHERE notion_session_id IS NOT NULL
                ORDER BY created_at DESC LIMIT 1''').fetchone()
            if prev and prev[1] in ('submitting', 'running'):
                return dict(status='queued', error='La ricerca Notion precedente è ancora in corso: '
                            'aspetta il suo risultato prima di insistere, non serve una nuova domanda.')
            if prev and prev[1] == 'completed':
                session_id, after_sequence = prev[0], prev[2]
            # failed/uncertain/refused o nessuna precedente: si apre una sessione nuova.
        delegation_id = uuid.uuid4()
        # Attempt committato PRIMA dell'I/O: se il processo muore durante la
        # POST la delega resta 'submitting' e non viene mai ri-inviata.
        conn.execute('''INSERT INTO notion_delegations
            (id,turn_key,owner_message_id,request_hash,notion_session_id,last_event_sequence)
            VALUES (%s,%s,%s,%s,%s,%s)''',
                     (str(delegation_id), state.turn_key, state.owner_mid, request_hash,
                      session_id, after_sequence))
    try:
        if session_id:
            session = notion_agent.follow_up(session_id, question)
        else:
            session = notion_agent.create_session(question)
    except notion_agent.NotionAmbiguous:
        _mark(delegation_id, 'uncertain')
        return dict(status='uncertain', delegation_id=str(delegation_id),
                    receipt='La connessione a Notion è caduta durante l\'invio: non posso verificare se la '
                            'richiesta sia partita e NON la ripeto per evitare duplicati. Serve un controllo manuale.')
    except notion_agent.NotionError as exc:
        if 400 <= exc.status < 500:
            _mark(delegation_id, 'failed')
            return dict(status='failed', delegation_id=str(delegation_id),
                        error=f'Notion ha rifiutato la richiesta ({exc.code})',
                        receipt='Notion ha rifiutato la richiesta; nessuna sessione creata.')
        _mark(delegation_id, 'uncertain')  # 5xx/429: la sessione potrebbe esistere comunque
        return dict(status='uncertain', delegation_id=str(delegation_id),
                    receipt='Risposta di Notion non verificabile: richiesta NON ripetuta per evitare duplicati.')
    session_id = session['id']
    with db._conn() as conn, conn.transaction():
        conn.execute('''UPDATE notion_delegations SET notion_session_id=%s, status='running',
            updated_at=now() WHERE id=%s''', (session_id, str(delegation_id)))
    # Fast path: le letture semplici si chiudono spesso in pochi secondi.
    deadline = time.monotonic() + config.NOTION_FAST_WAIT_SECONDS
    while True:
        try:
            result = _poll_once(delegation_id, session_id, after_sequence)
        except Exception:
            break  # il monitor durevole riprenderà il polling
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            break
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        _enqueue_monitor(conn, delegation_id)
    return dict(status='queued', delegation_id=str(delegation_id),
                receipt='Richiesta inviata allo specialista Notion: la ricerca è in corso, '
                        'ti avviso qui appena è pronta.')


def monitor(payload):
    """Job durevole: segue UNA delega fino alla conclusione e avvisa il proprietario."""
    delegation_id = payload['delegation_id']
    with db._conn() as conn:
        row = conn.execute('''SELECT notion_session_id,status,answer,owner_message_id,last_event_sequence,
            (now() - created_at) > %s * interval '1 second' FROM notion_delegations WHERE id=%s''',
                           (config.NOTION_MAX_RUN_SECONDS, str(delegation_id))).fetchone()
    if not row:
        return
    session_id, status, answer, owner_mid, after_sequence, expired = row
    if status in TERMINAL:
        if status in ('completed', 'failed', 'refused'):
            # Crash tra la conclusione e l'accodamento della notifica: la chiave
            # stabile garantisce che al proprietario arrivi UNA sola notifica.
            with db._conn() as conn, conn.transaction():
                jobs.assert_current(conn)
                _notify(conn, delegation_id, owner_mid, _notify_text(status, answer))
        return
    if not session_id:
        _mark(delegation_id, 'uncertain')
        return  # attempt senza session_id: mai ri-inviare la POST alla cieca
    if expired:
        _mark(delegation_id, 'failed')
        with db._conn() as conn, conn.transaction():
            jobs.assert_current(conn)
            _notify(conn, delegation_id, owner_mid,
                    _notify_text('failed', receipt='la ricerca non si è conclusa in tempo utile; '
                                 'nessuna modifica effettuata.'))
        return
    try:
        result = _poll_once(delegation_id, session_id, after_sequence)
    except notion_agent.NotionError as exc:
        if not exc.retryable:
            _mark(delegation_id, 'failed')
            with db._conn() as conn, conn.transaction():
                jobs.assert_current(conn)
                _notify(conn, delegation_id, owner_mid,
                        _notify_text('failed', receipt=f'richiesta non completata ({exc.code}). '
                                     'Nessuna modifica effettuata.'))
            return
        raise jobs.Deferred('Notion non raggiungibile: nuovo controllo più tardi') from exc
    except notion_agent.NotionAmbiguous as exc:
        raise jobs.Deferred('Controllo di stato Notion non riuscito: riprovo') from exc
    if result is None:
        raise jobs.Deferred('Sessione Notion ancora in corso')
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        _notify(conn, delegation_id, owner_mid, _notify_text(result['status'], result.get('answer'),
                                                             result.get('receipt')))
