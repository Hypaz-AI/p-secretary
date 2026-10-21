"""Il proprietario: autorità nativa, scambi durevoli, consegna e comandi slash.

L'unica autorità del proprietario è il TESTO nativo vivo: diretto, non
inoltrato, in chat privata dal bot (`policy.can_operate(..., owner_required)`).
Storico, inoltrati e contenuti di terzi sono solo evidenza, mai comando. Il
modello non decide chi è il proprietario: il server lo verifica dalla
provenienza archiviata e lo passa agli strumenti come contesto, mai come
argomento.

I comandi slash sono deterministici (nessun modello) e restano il "gesto
esplicito" per le azioni sensibili: grant Calendar (/conferma, /annulla) e
invio email (/invia, /scarta).
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from secretary.core import config, db, jobs, policy

VERSION = 'owner_exchanges_v1'
CHUNK = 3500
REUSE_MINUTES = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_exchanges (
 message_id BIGINT PRIMARY KEY, message_time TIMESTAMPTZ NOT NULL,
 user_text TEXT NOT NULL, has_images BOOLEAN NOT NULL DEFAULT FALSE,
 plan JSONB, response TEXT, completed_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_deadline_date DATE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_origin_message BIGINT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_operation_key TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_remind_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS owner_task_operation ON tasks(owner_operation_key);
"""


def migrate(conn):
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190427)')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return
        conn.execute(SCHEMA)
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))


# ---------------------------------------------------------------- autorità
def authorize(mid, *, ai=False, media=False, conn=None):
    """Autorità nativa e consenso dati esplicito della dashboard, mai flag nel payload."""
    if conn is None:
        with db._conn() as owned:
            return authorize(mid, ai=ai, media=media, conn=owned)
    jobs.assert_current(conn)
    if not policy.can_operate(conn, mid, 'owner_command', owner_required=True):
        raise PermissionError('Native owner authority required')
    effects = ('ai_text', 'ai_media') if media else ('ai_text',)
    for effect in effects if ai else ():
        if not policy.can_operate(conn, mid, effect, owner_required=True):
            raise jobs.Deferred('Owner AI data permission required in dashboard')


def current_text(conn, mid, text):
    """Solo il testo nativo attestato e corrente può autorizzare scritture."""
    meta = policy.metadata_for(conn, mid)
    native = meta.get('metadata') or {}
    row = conn.execute('SELECT body FROM telegram_messages WHERE id=%s FOR SHARE', (mid,)).fetchone()
    return bool(row and row[0] == text and native.get('content_origin') == 'direct_text'
                and native.get('is_forwarded') is False
                and not any(native.get(k) not in (None, False, '', {}) for k in (
                    'forward_origin', 'forward_date', 'forward_from', 'forward_from_chat',
                    'forward_sender_name', 'forwarded', 'quote', 'external_reply',
                    'via_bot', 'sender_chat', 'is_automatic_forward')))


# --------------------------------------------------------- scambi e consegna
def begin(mid, text, has_images):
    """Registra lo scambio (idempotente). Ritorna (message_time, risposta già data)."""
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        # Le risposte escono nell'ordine delle richieste.
        if conn.execute("""SELECT 1 FROM work_jobs WHERE kind='owner'
            AND id < (SELECT id FROM work_jobs WHERE key=%s) AND status IN ('pending','running')
            LIMIT 1""", (f'owner:{mid}',)).fetchone():
            raise jobs.Deferred('Earlier owner message still processing')
        stamp = conn.execute('SELECT COALESCE(ts,acquired_at) FROM communications WHERE id=%s', (mid,)).fetchone()
        if not stamp:
            raise ValueError('Owner message not archived')
        conn.execute('''INSERT INTO owner_exchanges(message_id,message_time,user_text,has_images)
            VALUES (%s,%s,%s,%s) ON CONFLICT(message_id) DO NOTHING''', (mid, stamp[0], text, has_images))
        message_time, response = conn.execute(
            'SELECT message_time,response FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()
        if response is None:
            # Stessa richiesta ripetuta mentre la prima è in corso o appena conclusa:
            # una sola risposta, nessun secondo ciclo AI.
            twin = conn.execute("""SELECT e.response,j.status FROM owner_exchanges e
                LEFT JOIN work_jobs j ON j.key='owner:'||e.message_id::text
                WHERE e.user_text=%s AND e.message_id<>%s
                  AND e.message_time > now() - (%s * interval '1 minute')
                ORDER BY e.message_id LIMIT 1""", (text, mid, REUSE_MINUTES)).fetchone()
            if twin and twin[0] is not None:
                response = twin[0]
            elif twin and twin[1] in ('pending', 'running'):
                raise jobs.Deferred('Identical owner request still processing')
    return message_time, response


def deliver(conn, mid, response):
    for index, start in enumerate(range(0, len(response), CHUNK)):
        jobs.enqueue(conn, 'action', f'owner-reply:{mid}:{index}',
                     dict(type='notify', text=response[start:start + CHUNK], html=False,
                          message_id=mid, owner_reply_mid=mid, owner_reply_chunk=index))


def complete(mid, response):
    """Salva la risposta e la accoda alla consegna nella STESSA transazione."""
    response = (response or '').strip() or 'Non ho informazioni sufficienti: puoi precisare la richiesta?'
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        authorize(mid, conn=conn)
        conn.execute('''UPDATE owner_exchanges SET response=%s,completed_at=now()
            WHERE message_id=%s AND response IS NULL''', (response, mid))
        stored = conn.execute('SELECT response FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()[0]
        deliver(conn, mid, stored)
    return stored


def recent_history(mid, *, limit=12, max_chars=9000):
    """Ultimi scambi conclusi, dal più vecchio. Le vecchie risposte sono escluse oltre
    una finestra breve: la loro provenienza non è registrata e i consensi possono
    essere cambiati (il modello può rileggere i dati con gli strumenti)."""
    fresh = config.AGENT_HISTORY_RESPONSE_MINUTES
    with db._conn() as conn:
        rows = conn.execute(f'''SELECT e.message_id,e.message_time,e.user_text,e.response,
                e.completed_at > now() - (%s * interval '1 minute')
            FROM {config.AGENT_DB_SCHEMA}.owner_exchanges e WHERE e.message_id < %s
            ORDER BY e.message_id DESC LIMIT %s''', (fresh, mid, limit)).fetchall()
        rows = [r for r in rows if policy.can_operate(conn, r[0], 'owner_command', owner_required=True)]
    result, used = [], 0
    for _, when, user, response, recent in rows:
        entry = dict(when=when.astimezone(ZoneInfo(config.TIMEZONE)).strftime('%d/%m %H:%M'),
                     owner=user[:3000], assistant=(response or '')[:3000] if recent else None)
        used += len(entry['owner']) + len(entry['assistant'] or '')
        if result and used > max_chars:
            break
        result.append(entry)
    return list(reversed(result))


# ----------------------------------------------------------------- comandi
COMMANDS = frozenset(('/start', '/stop', '/appuntamenti', '/note', '/regole', '/fatture', '/task',
                      '/attivita', '/fatto', '/conferma', '/annulla', '/invia', '/scarta'))
RULE = re.compile(r'quando\s+(.+?)\s+allora\s+(.+)', re.I | re.S)


def command(text, mid):
    """Adapter deterministico: una transazione per scritture, ricevuta e consegna.

    Ritorna True se `text` è un comando (gestito o rifiutato), False altrimenti.
    owner_exchanges.response è la ricevuta locale legata al messaggio.
    """
    parts = text.strip().split(maxsplit=1)
    name = parts[0].lower() if parts else ''
    rule = RULE.fullmatch(text.strip())
    if name not in COMMANDS and not rule:
        return False
    authorize(mid)
    message_time, previous = begin(mid, text, False)
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        authorize(mid, conn=conn)
        if previous is not None:
            deliver(conn, mid, previous)
            return True
        if not current_text(conn, mid, text):
            raise PermissionError('Commands require attested current owner text')
        reply = _effect(conn, mid, name, parts[1] if len(parts) > 1 else '', rule)
        conn.execute('UPDATE owner_exchanges SET response=%s,completed_at=now() WHERE message_id=%s',
                     (reply, mid))
        deliver(conn, mid, reply)
    return True


def _effect(conn, mid, name, argument, rule):
    from secretary.workflows import pipeline, task_reconciliation as lifecycle
    if rule:
        conn.execute('INSERT INTO rules(pattern,instruction,taught_by) VALUES (%s,%s,%s)',
                     (rule[1].strip(), rule[2].strip(), 'utente'))
        return f'Regola imparata: SE {rule[1].strip()} ALLORA {rule[2].strip()}'
    if name in ('/start', '/stop'):
        return 'Segretario pronto.' if name == '/start' else 'Usa la dashboard per sospendere i flussi.'
    if name == '/task':
        title, _, raw = argument.partition('|')
        try:
            if not title.strip() or len(title.strip()) > 500:
                raise ValueError('Invalid title')
            deadline = datetime.fromisoformat(raw.strip()) if raw.strip() else None
            if deadline and deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=ZoneInfo(config.TIMEZONE))
        except ValueError:
            return 'Usare /task titolo | YYYY-MM-DDTHH:MM+02:00 (scadenza facoltativa).'
        if not policy.can_operate(conn, mid, 'task_create', owner_required=True):
            raise PermissionError('Owner task permission required')
        row = conn.execute('''INSERT INTO tasks(title,deadline,owner_origin_message,owner_operation_key)
            VALUES (%s,%s,%s,%s) RETURNING id,version''',
            (title.strip(), deadline, mid, f'owner-command:{mid}')).fetchone()
        pipeline.enqueue_owner_reminder(conn, dict(id=row[0], version=row[1], deadline=deadline))
        return f'Attività #{row[0]} salvata.'
    if name == '/attivita':
        rows = conn.execute("""SELECT id,title,deadline,owner_deadline_date,owner_remind_at
            FROM tasks WHERE status='open' ORDER BY id LIMIT 50""").fetchall()
        return '\n'.join(f"#{r[0]} {r[1]} — " + pipeline.owner_task_schedule_label(dict(zip(
            ('deadline', 'owner_deadline_date', 'owner_remind_at'), r[2:]))) for r in rows) or 'Nessuna attività aperta.'
    if name in ('/fatto', '/conferma', '/annulla', '/invia', '/scarta'):
        if not argument.isascii() or not argument.isdigit():
            return 'Specificare un ID numerico.'
        identity = int(argument)
        if not 0 < identity <= 9223372036854775807:
            return 'Elemento non trovato.'
        if name == '/fatto':
            row = conn.execute('SELECT status,version FROM tasks WHERE id=%s FOR UPDATE', (identity,)).fetchone()
            if not row or row[0] != 'open':
                return 'Attività non aperta.'
            result = lifecycle.apply_change(conn, dict(task_id=identity, expected_version=row[1],
                evidence_key=f'owner-command:{mid}', operation='complete', changes={},
                reason='Completamento owner'), authority_message_id=mid)
            return 'Attività completata.' if result == 'applied' else 'Attività non modificata: ' + result
        if name in ('/invia', '/scarta'):
            from secretary.agent import outbox
            return outbox.decide(conn, identity, mid, approve=(name == '/invia'))
        operation = 'create' if name == '/conferma' else 'cancel'
        try:
            expected = policy.calendar_event_hash(conn, identity, operation=operation)
        except ValueError:
            return 'Appuntamento non trovato o non valido per Calendar.'
        # La policy sigilla lo snapshot esatto e accoda da sola l'azione, atomicamente.
        grant = policy.grant_calendar(conn, identity, expected, mid, operation=operation)
        return ('Operazione Calendar in coda, non ancora confermata.' if grant else
                'Operazione Calendar non autorizzata: serve un’approvazione specifica.')
    if name == '/appuntamenti':
        rows = conn.execute('''SELECT id,dt,title,sync_state FROM appointments
            WHERE NOT done AND dt>=to_char(now(),'YYYY-MM-DD HH24:MI')
            ORDER BY dt,id LIMIT 50''').fetchall()
        return '\n'.join(f'#{r[0]} {r[1]} — {r[2]} [{r[3]}]' for r in rows) or 'Nessun appuntamento in agenda interna.'
    if name == '/note':
        rows = conn.execute('SELECT text FROM notes ORDER BY id DESC LIMIT 20').fetchall()
        return '\n'.join(r[0] for r in rows) or 'Nessuna nota.'
    if name == '/regole':
        rows = conn.execute('SELECT pattern,instruction FROM rules ORDER BY id DESC LIMIT 50').fetchall()
        return '\n'.join(f'SE {r[0]} ALLORA {r[1]}' for r in rows) or 'Nessuna regola.'
    rows = conn.execute('''SELECT entity,month,count(*) FROM invoices
        GROUP BY entity,month ORDER BY month DESC,entity LIMIT 50''').fetchall()
    return '\n'.join(f'{r[0]} {r[1]}: {r[2]} fatture' for r in rows) or 'Nessuna fattura archiviata.'
