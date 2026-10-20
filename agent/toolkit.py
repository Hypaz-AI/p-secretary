"""Gli strumenti dell'agente.

Lettura libera, scrittura tipizzata:

* LETTURA (db_query, read_message, fs_*, attachment_text, mail_*, web_search):
  l'agente interpreta e cerca come vuole, dentro i confini dei consensi.
* SCRITTURA: strumenti con argomenti tipizzati. Ogni scrittura (1) rivalida
  l'autorità nativa del proprietario o la provenienza del messaggio di origine,
  (2) richiede come prova una FRASE ESATTA del messaggio del proprietario o una
  citazione esatta del messaggio, (3) passa dal journal idempotente (un retry o
  una ripetizione non duplica l'effetto), (4) riusa i backend con ricevute e
  versioni attese già collaudati. L'identità del proprietario non è MAI un
  argomento del modello: sta nel contesto del turno, impostato dal server.

I risultati degli strumenti che contengono testo di terzi sono DATI non fidati.
"""
import asyncio
import json
import re
import threading
import time
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

import config
import db
import jobs
import policy
from agent import data, fsbox, journal, mail, outbox, owner, web

MAX_RESULT_CHARS = 30000
CATEGORIES = ('lavoro', 'personale', 'documenti', 'spam', 'newsletter', 'fatture', 'altro')


class ToolRefused(Exception):
    """Rifiuto esplicito e mostrabile al modello (nessun dato)."""


class TurnState:
    """Contesto di UN turno: stabilito dal server, mai dal modello."""

    def __init__(self, mode, context, turn_key, *, owner_mid=None, owner_text='', message_time=None,
                 batch=(), run_id=None):
        self.mode, self.context, self.turn_key = mode, context, turn_key
        self.owner_mid, self.owner_text, self.message_time = owner_mid, owner_text, message_time
        self.batch = tuple(batch)
        self.run_id = run_id
        self.calls = 0
        self.touched = set()      # messaggi letti (per sigillare le fonti del digest)
        self.failure = None       # lease perso / budget: ferma il turno
        self.lock = threading.Lock()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


# ------------------------------------------------------------------ parametri
class NoArgs(Strict):
    pass


class SqlArgs(Strict):
    sql: str = Field(description='Una singola SELECT PostgreSQL sulle viste dello schema agent, con LIMIT.')


class ReadMessageArgs(Strict):
    message_id: int = Field(description='ID del messaggio (colonna id di agent.messages).')
    offset: int = Field(0, description='Primo carattere del corpo da leggere (per testi lunghi).')
    max_chars: int = Field(20000, description='Caratteri massimi da restituire (200-60000).')


class AttachmentArgs(Strict):
    attachment_id: int = Field(description='ID dell\'allegato (agent.attachments.id).')
    offset: int = 0
    max_chars: int = 20000


class ListArgs(Strict):
    path: str = Field('', description='Cartella relativa. Vuoto = radice. Esempi: workspace, email_attachments, invoices.')


class FindArgs(Strict):
    pattern: str = Field(description='Nome con caratteri jolly, es. *fattura*.pdf')
    path: str = Field('', description='Cartella da cui cercare (vuoto = tutte le cartelle consentite).')


class FsReadArgs(Strict):
    path: str = Field(description='Percorso relativo di un file, es. workspace/report.md o email_attachments/<nome>.')
    offset: int = 0
    max_chars: int = 20000


class FsWriteArgs(Strict):
    path: str = Field(description='Solo dentro workspace/, es. workspace/fatture/riepilogo.md. Nomi ASCII semplici.')
    content: str = Field(description='Testo del file.')
    mode: Literal['create', 'append'] = Field('create', description='create = nuovo file (fallisce se esiste); append = accoda.')


class MkdirArgs(Strict):
    path: str = Field(description='Cartella da creare dentro workspace/.')


class MailSearchArgs(Strict):
    account: str = Field(description='Indirizzo dell\'account (vedi mail_accounts).')
    sender: Optional[str] = Field(None, description='Mittente (parte dell\'indirizzo o del nome, ASCII).')
    to: Optional[str] = None
    subject: Optional[str] = None
    text: Optional[str] = Field(None, description='Parola da cercare nel testo (ASCII).')
    since: Optional[str] = Field(None, description='Data minima YYYY-MM-DD.')
    before: Optional[str] = Field(None, description='Data massima YYYY-MM-DD (esclusa).')
    folder: str = 'INBOX'
    limit: int = 15


class MailReadArgs(Strict):
    account: str
    uid: str = Field(description='uid restituito da mail_search.')
    folder: str = 'INBOX'
    max_chars: int = 20000


class WebArgs(Strict):
    query: str = Field(description='Cosa cercare, con parole tue (max 200 caratteri, senza URL né email).')
    max_results: int = 5


class TaskArgs(Strict):
    operation: Literal['create', 'update', 'complete', 'cancel']
    task_id: Optional[int] = Field(None, description='ID dell\'attività aperta (obbligatorio tranne per create).')
    title: Optional[str] = None
    deadline: Optional[str] = Field(None, description='Scadenza con orario: ISO 8601 con offset, es. 2026-10-21T15:00:00+02:00. '
                                    'Solo se il proprietario ha indicato un orario.')
    deadline_date: Optional[str] = Field(None, description='Scadenza solo-data YYYY-MM-DD (nessun orario inventato).')
    remind_at: Optional[str] = Field(None, description='Promemoria esplicitamente richiesto: ISO 8601 con offset.')
    evidence: str = Field(description='La frase ESATTA e completa del messaggio corrente del proprietario che motiva la richiesta.')


class BulkArgs(Strict):
    operation: Literal['complete', 'cancel']
    exclude_ids: list[int] = Field(description='ID delle attività da lasciare APERTE.')
    evidence: str = Field(description='La frase ESATTA del proprietario che chiede la chiusura di massa.')


class NoteArgs(Strict):
    text: str = Field(description='Fatto o preferenza da ricordare, autosufficiente.')
    topic: Optional[str] = Field(None, description='Etichetta breve: una nota viva per etichetta (sostituisce la precedente).')
    kind: Literal['personal', 'preference', 'fact', 'instruction', 'context'] = Field(
        'personal', description="instruction = istruzione permanente ('da ora in poi...').")
    evidence: str = Field(description='La frase ESATTA del proprietario che chiede di ricordarlo.')


class ForgetArgs(Strict):
    note_id: int
    evidence: str = Field(description='La frase ESATTA del proprietario che chiede di dimenticarlo.')


class CalendarArgs(Strict):
    title: str
    datetime: str = Field(description='YYYY-MM-DD HH:MM, ora locale. Mai inventare ora o data.')
    location: str = ''
    notes: str = ''
    duration_min: Optional[int] = None
    reminder_min: Optional[int] = None
    message_id: Optional[int] = Field(None, description='Solo nell\'elaborazione della posta: messaggio di origine.')


class EmailArgs(Strict):
    account: str = Field(description='Indirizzo dell\'account da cui scrivere (vedi mail_accounts).')
    to: str = Field(description='Un solo indirizzo email.')
    subject: str
    body: str


class TriageArgs(Strict):
    message_id: int
    summary: str = Field(description='Riassunto in 1-2 frasi che cita CHI scrive (nome) e DOVE (gruppo/account).')
    category: Literal['lavoro', 'personale', 'documenti', 'spam', 'newsletter', 'fatture', 'altro']
    priority: Literal['bassa', 'media', 'alta']
    needs_action: bool = Field(description='True solo se il proprietario deve fare qualcosa di concreto.')
    action: str = ''
    needs_reply: bool = False
    reply_hint: str = ''


class TaskFromMessageArgs(Strict):
    message_id: int
    title: str = Field(description='Azione concreta per il proprietario.')
    deadline: Optional[str] = Field(None, description='ISO 8601 con offset, solo se il messaggio dà una scadenza esplicita.')
    quote: str = Field(description='Citazione ESATTA dal messaggio che giustifica l\'attività.')


class UrgentArgs(Strict):
    message_id: int
    text: str = Field(description='Avviso breve (max 400 caratteri): chi, cosa, perché è urgente ora.')


class InvoiceArgs(Strict):
    message_id: int
    entity: Optional[str] = None
    date: Optional[str] = None
    description: Optional[str] = None
    document_number: Optional[str] = None


# ------------------------------------------------------------------ guardie
def _require_owner(state):
    """Autorità nativa rivalutata a OGNI scrittura (la revoca vale subito)."""
    if state.mode != 'owner' or state.owner_mid is None:
        raise ToolRefused('Strumento disponibile solo quando risponde a una richiesta diretta del proprietario')
    with db._conn() as conn:
        owner.authorize(state.owner_mid, conn=conn)
        if not owner.current_text(conn, state.owner_mid, state.owner_text):
            raise ToolRefused('Per modificare dati serve un messaggio diretto di testo del proprietario '
                              '(non inoltrato, non una trascrizione o un allegato)')


def _in_batch(state, message_id):
    if state.mode != 'inbox' or message_id not in state.batch:
        raise ToolRefused('Messaggio non incluso nell\'elaborazione corrente')
    with db._conn() as conn:
        if not policy.can_operate(conn, message_id, 'ai_text'):
            raise ToolRefused('Consenso dati non attivo per questo messaggio')


def _journaled(state, tool, args, apply):
    """Esegue una scrittura una sola volta per (turno, strumento, argomenti)."""
    with db._conn() as conn:
        result, replayed = journal.once(conn, state.turn_key, tool, args, apply)
    return dict(result, replayed=True) if replayed else result


def _clarify(call):
    """Traduce i rifiuti di validazione in risposte che il modello può usare."""
    import owner_commitments
    try:
        return call()
    except owner_commitments.Clarification as exc:
        return dict(error=str(exc), needs_clarification=True)
    except (ValueError, outbox.OutboxError) as exc:
        return dict(error=str(exc))


def _short(value, limit):
    return value if len(value) <= limit else value[:limit]


# ----------------------------------------------------------------- lettura
def t_db_schema(state, p):
    return data.db_schema()


def t_db_query(state, p):
    return data.db_query(p.sql)


def t_read_message(state, p):
    result = data.read_message(p.message_id, p.offset, p.max_chars)
    if 'error' not in result:
        state.touched.add(p.message_id)
    return result


def t_attachment_text(state, p):
    result = data.attachment_text(p.attachment_id, p.offset, p.max_chars)
    if result.get('message_id'):
        state.touched.add(result['message_id'])
    return result


def _fs(call):
    try:
        return call()
    except fsbox.FsError as exc:
        return dict(error=str(exc))


def t_fs_list(state, p):
    return _fs(lambda: dict(path=p.path, entries=fsbox.listing(p.path)))


def t_fs_find(state, p):
    return _fs(lambda: dict(pattern=p.pattern, results=fsbox.find(p.pattern, p.path)))


def t_fs_read(state, p):
    def run():
        data.gate_path(p.path)
        return fsbox.read_text(p.path, offset=p.offset, max_chars=p.max_chars)
    return _fs(run)


def t_mail_accounts(state, p):
    return dict(accounts=mail.accounts())


def _mail(call):
    try:
        return call()
    except mail.MailError as exc:
        return dict(error=str(exc))
    except Exception as exc:
        if isinstance(exc, jobs.LeaseLost):
            raise
        return dict(error='Casella email non raggiungibile in questo momento')


def t_mail_search(state, p):
    return _mail(lambda: mail.search(p.account, sender=p.sender, to=p.to, subject=p.subject, text=p.text,
                                     since=p.since, before=p.before, folder=p.folder, limit=p.limit))


def t_mail_read(state, p):
    return _mail(lambda: mail.read(p.account, p.uid, folder=p.folder, max_chars=p.max_chars))


def t_web_search(state, p):
    try:
        return web.search(p.query, p.max_results)
    except web.WebError as exc:
        return dict(error=str(exc))


# ------------------------------------------------------- scritture: proprietario
def t_fs_write(state, p):
    _require_owner(state)
    return _fs(lambda: fsbox.write(p.path, p.content, p.mode))


def t_fs_mkdir(state, p):
    _require_owner(state)
    return _fs(lambda: fsbox.mkdir(p.path))


def t_task_change(state, p):
    import owner_commitments
    _require_owner(state)
    args = p.model_dump(exclude_none=True)
    fields = {k: v for k, v in (('title', p.title), ('deadline', p.deadline),
                                ('owner_deadline_date', p.deadline_date),
                                ('owner_remind_at', p.remind_at)) if v is not None}

    def apply(conn):
        tasks = owner_commitments._tasks(conn)
        change = owner_commitments.build_change(state.owner_text, state.message_time, tasks, p.operation,
                                                p.task_id, fields, p.evidence)
        prefix = f'owner-commitment:{state.owner_mid}:{journal.effect_key("task_change", args)[:20]}'
        result = owner_commitments.apply(conn, [change], state.owner_mid, prefix=prefix)
        return dict(status=result['status'], receipt=' '.join(result['confirmations']))
    return _clarify(lambda: _journaled(state, 'task_change', args, apply))


def t_tasks_close_bulk(state, p):
    import owner_commitments
    _require_owner(state)
    args = p.model_dump()

    def apply(conn):
        tasks = owner_commitments._tasks(conn)
        bulk = owner_commitments.build_bulk(state.owner_text, tasks, p.operation, p.exclude_ids, p.evidence)
        prefix = f'owner-commitment:{state.owner_mid}:{journal.effect_key("tasks_close_bulk", args)[:20]}'
        result = owner_commitments.apply(conn, bulk, state.owner_mid, prefix=prefix)
        return dict(status=result['status'], receipt=' '.join(result['confirmations']))
    return _clarify(lambda: _journaled(state, 'tasks_close_bulk', args, apply))


def t_note_save(state, p):
    import owner_commitments
    _require_owner(state)
    text = p.text.strip()
    if not text or len(text) > 2000:
        return dict(error='Testo della nota vuoto o oltre 2000 caratteri')
    args = p.model_dump(exclude_none=True)

    def apply(conn):
        owner_commitments.sentence(state.owner_text, p.evidence)
        if not policy.can_operate(conn, state.owner_mid, 'memory_write', owner_required=True):
            raise PermissionError('Owner memory permission required')
        topic = db.normalize_topic(p.topic)
        count = db.apply_personal_memories([dict(operation='add', text=text, topic=topic)], set(), conn=conn,
                                           kind=p.kind, source_message=state.owner_mid)
        label = 'Istruzione permanente' if p.kind == 'instruction' else 'Memoria'
        return dict(saved=bool(count), receipt=f'📝 {label} salvata.' if count else f'📝 {label} già presente.')
    return _clarify(lambda: _journaled(state, 'note_save', args, apply))


def t_note_forget(state, p):
    import owner_commitments
    _require_owner(state)
    args = p.model_dump()

    def apply(conn):
        owner_commitments.sentence(state.owner_text, p.evidence)
        row = conn.execute('SELECT text FROM notes WHERE id=%s', (p.note_id,)).fetchone()
        if not row:
            raise ValueError('Nota inesistente')
        db.apply_personal_memories([dict(operation='delete', id=p.note_id)], {p.note_id}, conn=conn,
                                   expected={p.note_id: row[0]}, source_message=state.owner_mid)
        return dict(deleted=p.note_id, receipt=f'📝 Nota #{p.note_id} eliminata.')
    return _clarify(lambda: _journaled(state, 'note_forget', args, apply))


def _calendar(state, p, source, key_origin):
    import pipeline
    from connectors import gcal
    event = gcal.validate_event(dict(title=p.title, dt=p.datetime, location=p.location, notes=p.notes,
                                     duration_min=p.duration_min, reminder_minutes=p.reminder_min))
    args = p.model_dump(exclude_none=True)
    key = f'{key_origin}:event:{journal.effect_key("calendar", args)[:16]}'

    def apply(conn):
        identity = pipeline.save_event(conn, event, key, source, approved=False)
        return dict(appointment_id=identity, state='proposal',
                    receipt=f'📅 Proposta #{identity} «{event["title"]}» per {event["dt"]}: '
                            f'per metterla in calendario rispondi /conferma {identity}')
    return _journaled(state, 'calendar_propose', args, apply)


def t_calendar_propose(state, p):
    if state.mode == 'owner':
        _require_owner(state)
        return _clarify(lambda: _calendar(state, p, 'dialogo utente', f'owner:{state.owner_mid}'))
    if p.message_id is None:
        return dict(error='Indica message_id del messaggio da cui deriva l\'evento')
    _in_batch(state, p.message_id)
    return _clarify(lambda: _calendar(state, p, f'posta: messaggio {p.message_id}', f'message:{p.message_id}'))


def t_email_propose(state, p):
    _require_owner(state)
    args = p.model_dump()

    def apply(conn):
        result = outbox.propose(conn, p.account, p.to, p.subject, p.body, state.owner_mid)
        return dict(result, receipt=f'✉️ Bozza #{result["draft_id"]} pronta: rispondi /invia {result["draft_id"]} per inviarla')
    return _clarify(lambda: _journaled(state, 'email_propose', args, apply))


# ------------------------------------------------------- scritture: posta in arrivo
def t_triage(state, p):
    _in_batch(state, p.message_id)
    args = p.model_dump()
    if not p.summary.strip():
        return dict(error='Il riassunto è obbligatorio')

    def apply(conn):
        row = conn.execute('SELECT channel,source_id,direction FROM communications WHERE id=%s',
                           (p.message_id,)).fetchone()
        if not row:
            raise ValueError('Messaggio inesistente')
        verdict = dict(summary=_short(p.summary.strip(), 600), category=p.category, priority=p.priority,
                       needs_action=p.needs_action, action=_short(p.action.strip(), 400),
                       needs_reply=p.needs_reply, reply_hint=_short(p.reply_hint.strip(), 800),
                       urgent=False, severity='bassa', agent=True)
        db.message_store.analyze(conn, p.message_id, row[0], verdict, False, row[1])
        return dict(triaged=p.message_id)
    return _clarify(lambda: _journaled(state, 'triage', args, apply))


def t_task_from_message(state, p):
    _in_batch(state, p.message_id)
    args = p.model_dump(exclude_none=True)
    title = p.title.strip()
    if not title or len(title) > 500:
        return dict(error='Titolo non valido (1-500 caratteri)')
    deadline = None
    if p.deadline:
        try:
            deadline = datetime.fromisoformat(p.deadline)
        except ValueError:
            return dict(error='Scadenza non valida: ISO 8601 con offset')
        if deadline.tzinfo is None:
            return dict(error='La scadenza richiede l\'offset di fuso, es. +02:00')

    def apply(conn):
        row = conn.execute('SELECT body,direction FROM communications WHERE id=%s', (p.message_id,)).fetchone()
        if not row:
            raise ValueError('Messaggio inesistente')
        if row[1] == 'out':
            raise ValueError('Dai messaggi inviati dal proprietario non si creano attività')
        if not p.quote.strip() or p.quote not in (row[0] or ''):
            raise ValueError('La citazione deve essere copiata ESATTAMENTE dal messaggio')
        existing = conn.execute('SELECT id FROM tasks WHERE source_message=%s', (p.message_id,)).fetchone()
        if existing:
            return dict(task_id=existing[0], created=False, note='Esiste già un\'attività per questo messaggio')
        created = conn.execute('''INSERT INTO tasks(source_message,title,deadline) VALUES (%s,%s,%s)
            ON CONFLICT(source_message) DO NOTHING RETURNING id''', (p.message_id, title, deadline)).fetchone()
        return dict(task_id=created[0], created=True) if created else dict(created=False)
    return _clarify(lambda: _journaled(state, 'task_from_message', args, apply))


def t_notify_urgent(state, p):
    _in_batch(state, p.message_id)
    text = re.sub(r'https?://\S+|www\.\S+', '[link]', p.text.strip())
    if not text or len(text) > 400:
        return dict(error='Avviso vuoto o oltre 400 caratteri')
    args = dict(message_id=p.message_id, text=text)

    def apply(conn):
        row = conn.execute('SELECT direction FROM communications WHERE id=%s', (p.message_id,)).fetchone()
        if not row or row[0] == 'out':
            raise ValueError('Nessun avviso per messaggi inviati dal proprietario')
        # Le chiamate dello stesso turno possono arrivare in parallelo: senza lock
        # due avvisi leggerebbero lo stesso conteggio e supererebbero il tetto.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('agent-notify-cap'))")
        sent = conn.execute("""SELECT count(*) FROM work_jobs WHERE kind='action' AND key LIKE 'agent-notify:%%'
            AND created_at >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s""",
                            (config.TIMEZONE, config.TIMEZONE)).fetchone()[0]
        if sent >= config.AGENT_NOTIFY_MAX_PER_DAY:
            raise ValueError('Tetto giornaliero di avvisi autonomi raggiunto: la segnalazione resta nel riepilogo serale')
        queued = jobs.enqueue(conn, 'action', f'agent-notify:{p.message_id}',
                              dict(type='notify', message_id=p.message_id, urgent=True, severity='alta',
                                   html=False, text='🔔 Urgente (valutazione automatica)\n' + text))
        return dict(queued=bool(queued), receipt='avviso accodato')
    return _clarify(lambda: _journaled(state, 'notify_urgent', args, apply))


def t_archive_invoice(state, p):
    _in_batch(state, p.message_id)
    args = p.model_dump(exclude_none=True)

    def apply(conn):
        import capabilities
        if not capabilities.enabled('invoices', conn=conn):
            raise ValueError('Archiviazione fatture non attiva')
        info = {k: v for k, v in (('entity', p.entity), ('date', p.date), ('description', p.description),
                                  ('document_number', p.document_number)) if v}
        queued = jobs.enqueue(conn, 'action', f'invoice:{p.message_id}',
                              dict(type='invoice', message_id=p.message_id, verdict=dict(invoice_info=info)))
        return dict(queued=bool(queued))
    return _clarify(lambda: _journaled(state, 'archive_invoice', args, apply))


# ------------------------------------------------------------------ catalogo
class Spec:
    def __init__(self, name, description, params, handler, modes, write=False, enabled=None):
        self.name, self.description, self.params, self.handler = name, description, params, handler
        self.modes, self.write, self.enabled = frozenset(modes), write, enabled


ALL = ('owner', 'inbox', 'digest')
READ = ('owner', 'inbox', 'digest')

SPECS = [
    Spec('db_schema', 'Mostra le viste SQL disponibili con le loro colonne. Usalo se non sei sicuro dei nomi.',
         NoArgs, t_db_schema, ALL),
    Spec('db_query', 'Esegue una SELECT PostgreSQL in sola lettura sulle viste dello schema agent (messaggi, '
         'allegati, documenti, attività, appuntamenti, note, fatture, dialoghi). Per ogni numero che riporti '
         'esegui una query: non contare a mente. Le celle lunghe sono troncate (usa read_message).',
         SqlArgs, t_db_query, ALL),
    Spec('read_message', 'Legge il testo COMPLETO di un messaggio (email/WhatsApp/Telegram) e l\'elenco dei suoi allegati.',
         ReadMessageArgs, t_read_message, ALL),
    Spec('attachment_text', 'Legge il testo di un allegato (PDF, Word, Excel, immagini e vocali già trascritti).',
         AttachmentArgs, t_attachment_text, ALL),
    Spec('fs_list', 'Elenca file e cartelle (workspace, allegati, fatture, upload).', ListArgs, t_fs_list, ('owner',)),
    Spec('fs_find', 'Cerca file per nome nelle cartelle consentite.', FindArgs, t_fs_find, ('owner',)),
    Spec('fs_read', 'Legge un file di testo, PDF, Word o Excel dalle cartelle consentite.', FsReadArgs, t_fs_read, ('owner',)),
    Spec('fs_write', 'Crea o accoda un file di testo nella cartella workspace/ (visibile al proprietario dalla dashboard). '
         'Non si possono sovrascrivere né cancellare file.', FsWriteArgs, t_fs_write, ('owner',), write=True),
    Spec('fs_mkdir', 'Crea una cartella dentro workspace/.', MkdirArgs, t_fs_mkdir, ('owner',), write=True),
    Spec('mail_accounts', 'Elenca le caselle email configurate.', NoArgs, t_mail_accounts, ('owner',)),
    Spec('mail_search', 'Cerca direttamente nella casella email sul server (sola lettura): per cartelle o periodi '
         'non presenti nell\'archivio.', MailSearchArgs, t_mail_search, ('owner',)),
    Spec('mail_read', 'Legge un messaggio direttamente dalla casella email (sola lettura).', MailReadArgs, t_mail_read, ('owner',)),
    Spec('web_search', 'Cerca sul web e riassume con le fonti. Usalo quando l\'informazione non è nei dati del proprietario.',
         WebArgs, t_web_search, ('owner',)),
    Spec('task_change', 'Crea, aggiorna, completa o annulla UNA attività del proprietario. Richiede la frase esatta '
         'del proprietario come evidence. Non creare duplicati: controlla prima agent.tasks.',
         TaskArgs, t_task_change, ('owner',), write=True),
    Spec('tasks_close_bulk', 'Chiude (o annulla) TUTTE le attività aperte tranne quelle elencate in exclude_ids.',
         BulkArgs, t_tasks_close_bulk, ('owner',), write=True),
    Spec('note_save', 'Salva una memoria o un\'istruzione permanente del proprietario (kind=instruction per '
         '"da ora in poi..."). Richiede la frase esatta come evidence.', NoteArgs, t_note_save, ('owner',), write=True),
    Spec('note_forget', 'Elimina una nota su richiesta esplicita del proprietario.', ForgetArgs, t_note_forget,
         ('owner',), write=True),
    Spec('calendar_propose', 'Propone un evento in agenda: resta una PROPOSTA locale finché il proprietario non '
         'risponde /conferma ID. Non inventare ora o data.', CalendarArgs, t_calendar_propose, ('owner', 'inbox'), write=True),
    Spec('email_propose', 'Prepara la bozza di una email: NON la invia. Il proprietario la invia con /invia ID.',
         EmailArgs, t_email_propose, ('owner',), write=True),
    Spec('triage', 'Registra l\'esito dell\'analisi di UN messaggio in arrivo. Va chiamato per ogni messaggio.',
         TriageArgs, t_triage, ('inbox',), write=True),
    Spec('task_from_message', 'Crea un\'attività per il proprietario derivata da un messaggio in arrivo. Solo per azioni '
         'concrete richieste al proprietario. Non avvisa nessuno.', TaskFromMessageArgs, t_task_from_message,
         ('inbox',), write=True),
    Spec('notify_urgent', 'Invia un avviso Telegram al proprietario SOLO per vere emergenze (danno concreto o scadenza '
         'inderogabile entro poche ore). C\'è un tetto giornaliero: tutto il resto va nel riepilogo serale.',
         UrgentArgs, t_notify_urgent, ('inbox',), write=True),
    Spec('archive_invoice', 'Archivia le fatture allegate a un messaggio.', InvoiceArgs, t_archive_invoice,
         ('inbox',), write=True, enabled=lambda: _invoices_on()),
]
BY_NAME = {spec.name: spec for spec in SPECS}


def _invoices_on():
    try:
        import capabilities
        return capabilities.enabled('invoices')
    except Exception:
        return False


def names(mode):
    return [s.name for s in SPECS if mode in s.modes and (s.enabled is None or s.enabled())]


# --------------------------------------------------------- esecuzione e adattatore
def _summary(result):
    if not isinstance(result, dict):
        return 'ok'
    if 'error' in result:
        return 'errore: ' + str(result['error'])[:200]
    parts = [k for k in ('row_count', 'receipt', 'chars', 'task_id', 'triaged', 'draft_id') if k in result]
    return ', '.join(f'{k}={str(result[k])[:60]}' for k in parts) or ', '.join(list(result)[:6])


def call(spec, state, params):
    """Esegue UNA chiamata: lease, limite di chiamate, audit. Ritorna un dict JSON-serializzabile."""
    started = time.monotonic()
    with state.lock:
        state.calls += 1
        seq = state.calls
    if seq > config.AGENT_MAX_TOOL_CALLS:
        return dict(error='Limite di chiamate agli strumenti raggiunto: concludi con quanto raccolto')
    try:
        with jobs.bind(state.context):
            jobs.assert_current()
            result = spec.handler(state, params)
    except (jobs.LeaseLost, jobs.Deferred) as exc:
        state.failure = exc
        result = dict(error='Esecuzione interrotta')
    except ToolRefused as exc:
        result = dict(error=str(exc))
    except PermissionError:
        result = dict(error='Operazione non autorizzata dalla provenienza del messaggio')
    except Exception as exc:
        result = dict(error='Errore interno dello strumento')
        journal.log_tool(state.run_id, seq, spec.name, params.model_dump(), False,
                         f'eccezione {type(exc).__name__}', int((time.monotonic() - started) * 1000))
        return result
    journal.log_tool(state.run_id, seq, spec.name, params.model_dump(), 'error' not in result,
                     _summary(result), int((time.monotonic() - started) * 1000))
    return result


def _render(result):
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        return json.dumps(dict(error='Risultato troppo grande: restringi la richiesta', size=len(text)))
    return text


def sdk_tools(state):
    """Strumenti del turno nel formato del Copilot SDK (solo quelli ammessi dal modo)."""
    from copilot import define_tool
    tools = []
    for spec in SPECS:
        if state.mode not in spec.modes or (spec.enabled is not None and not spec.enabled()):
            continue

        def make(spec):
            async def handler(params, invocation):
                return _render(await asyncio.to_thread(call, spec, state, params))
            return handler
        tools.append(define_tool(spec.name, description=spec.description, handler=make(spec),
                                 params_type=spec.params, skip_permission=True))
    return tools
