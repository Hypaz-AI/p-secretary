"""I tre modi in cui lavora l'agente: dialogo, posta in arrivo, riepilogo serale.

Nessun flusso fisso: cambiano solo prompt, strumenti ammessi e cosa si fa con
il testo finale. Ogni turno è un job durevole (lease, retry, backoff).
"""
import logging
from datetime import timezone

import config
import db
import flows
import jobs
import policy
from agent import journal, owner, prompts, runtime as runtime_module, toolkit

logger = logging.getLogger(__name__)
BODY_EXCERPT = 1500


class _Audit:
    """Registra l'esecuzione (agent_runs) senza mai far fallire il turno."""

    def __init__(self, kind, turn_key, owner_mid=None):
        context = jobs.execution_context()
        self.run_id = None
        try:
            self.run_id = journal.start_run(kind, turn_key, context.job_id if context else None,
                                            owner_mid, config.AGENT_MODEL)
        except Exception:
            logger.warning('Agent audit unavailable', exc_info=True)

    def done(self, state, error=None):
        if self.run_id is None:
            return
        try:
            journal.finish_run(self.run_id, 'failed' if error else 'done', state.calls,
                               type(error).__name__ if error else None)
        except Exception:
            pass


def _attachments_note(attachments):
    lines = [f"[Allegato #{a['id']}: {a.get('filename') or 'senza nome'} ({a.get('content_type') or 'tipo ignoto'})]"
             for a in attachments if a.get('id') is not None]
    return '\n'.join(lines)


# --------------------------------------------------------------- proprietario
def owner_turn(mid, *, runtime=None):
    """Una richiesta diretta del proprietario: interpreta, usa gli strumenti, risponde."""
    import pipeline
    from connectors import media_text
    runtime = runtime or runtime_module.default()
    row, attachments = pipeline.load_message(mid)
    body = row[3]
    message_time, previous = owner.begin(mid, body, any(media_text.kind(a) == 'image' for a in attachments))
    if previous is not None:
        # Già risposto (o richiesta identica appena evasa): rilancia solo la consegna.
        with db._conn() as conn, conn.transaction():
            jobs.assert_current(conn)
            owner.authorize(mid, conn=conn)
            conn.execute('''UPDATE owner_exchanges SET response=%s,completed_at=COALESCE(completed_at,now())
                WHERE message_id=%s AND response IS NULL''', (previous, mid))
            owner.deliver(conn, mid, previous)
        return previous
    owner.authorize(mid, ai=True, media=bool(attachments))
    spoken = media_text.message_text(body, [a for a in attachments if media_text.kind(a)])
    note = _attachments_note(attachments)
    turn_key = f'owner:{mid}'
    state = toolkit.TurnState('owner', jobs.execution_context(), turn_key, owner_mid=mid, owner_text=body,
                              message_time=message_time)
    audit = _Audit('owner', turn_key, mid)
    state.run_id = audit.run_id
    history = owner.recent_history(mid)
    lines = ['[Conversazione recente]'] if history else []
    for entry in history:
        lines.append(f"{entry['when']} Proprietario: {entry['owner']}")
        if entry['assistant']:
            lines.append(f"{entry['when']} Assistente: {entry['assistant']}")
    local = message_time.astimezone(prompts.ZoneInfo(config.TIMEZONE)).strftime('%d/%m/%Y %H:%M')
    lines += [f'[Messaggio del proprietario, {local}]', spoken or '(messaggio senza testo)']
    if note:
        lines.append(note)
    system = prompts.system_prompt('owner', effects=journal.executed(turn_key))
    error = None
    try:
        text = runtime.run(state, system, '\n'.join(lines), 'agent_owner', config.AGENT_OWNER_TIMEOUT)
    except BaseException as exc:
        error = exc
        raise
    finally:
        audit.done(state, error)
    receipts = [(e.get('result') or {}).get('receipt') for e in journal.executed(turn_key)]
    receipts = [r for r in receipts if r]
    response = text + ('\n\n' + '\n'.join(receipts) if receipts else '')
    return owner.complete(mid, response)


# ------------------------------------------------------------ posta in arrivo
def _flow(channel):
    return 'email' if channel.startswith('email') else 'whatsapp' if channel == 'whatsapp' else 'telegram_bot'


def _candidates(primary, limit):
    """Il messaggio del job più altri in attesa, per elaborarli insieme (un solo contesto)."""
    with db._conn() as conn:
        rows = conn.execute('''SELECT (payload->>'message_id')::bigint FROM work_jobs
            WHERE kind='analysis' AND status='pending' AND available_at<=now() AND key<>%s
            ORDER BY id LIMIT %s''', (f'analysis:{primary}', limit * 3)).fetchall()
    return [primary] + [r[0] for r in rows if r[0] != primary]


def _eligible(conn, mid, attachments, *, primary):
    """Stesse barriere di sempre: provenienza nativa, consenso testo/media, flusso attivo."""
    if conn.execute('SELECT 1 FROM message_analysis WHERE message_id=%s', (mid,)).fetchone():
        return False
    if not policy.can_operate(conn, mid, 'extract'):
        return False
    if not policy.can_operate(conn, mid, 'ai_text'):
        if primary:
            raise jobs.Deferred('AI text data policy denied')
        return False
    if attachments and not policy.can_operate(conn, mid, 'ai_media'):
        if primary:
            raise jobs.Deferred('AI media data policy denied')
        return False
    return True


def inbox_job(payload, *, runtime=None):
    """Handler del worker 'analysis': valuta il messaggio (e altri in attesa) con l'agente."""
    import pipeline
    from connectors import media_text
    runtime = runtime or runtime_module.default()
    primary = payload['message_id']
    loaded = []
    for mid in _candidates(primary, config.AGENT_INBOX_BATCH - 1)[:config.AGENT_INBOX_BATCH]:
        is_primary = mid == primary
        try:
            (channel, sender, subject, body, source_id), attachments = pipeline.load_message(mid)
        except ValueError:
            continue
        with db._conn() as conn:
            jobs.assert_current(conn)
            if not _eligible(conn, mid, attachments, primary=is_primary):
                continue
        if not flows.enabled(_flow(channel)):
            if is_primary:
                raise jobs.Deferred('Source paused')
            continue
        try:
            text = media_text.message_text(body, [a for a in attachments if media_text.kind(a)]) \
                if channel.startswith(('telegram', 'whatsapp')) else body
        except jobs.Deferred:
            if is_primary:
                raise
            continue
        loaded.append(dict(id=mid, channel=channel, sender=sender, subject=subject, text=text or '',
                           attachments=attachments))
    if not loaded:
        return
    ids = [m['id'] for m in loaded]
    turn_key = f'inbox:{ids[0]}:{len(ids)}'
    state = toolkit.TurnState('inbox', jobs.execution_context(), turn_key, batch=ids)
    audit = _Audit('inbox', turn_key)
    state.run_id = audit.run_id
    blocks = []
    for item in loaded:
        text = item['text']
        clipped = text[:BODY_EXCERPT] + (f'… [+{len(text) - BODY_EXCERPT} caratteri: usa read_message]'
                                         if len(text) > BODY_EXCERPT else '')
        blocks.append(f"--- Messaggio id={item['id']} ---\nCanale: {item['channel']}\nDa: {item['sender']}\n"
                      f"Oggetto: {item['subject']}\n{_attachments_note(item['attachments'])}\n\n{clipped}")
    prompt = (f'Elabora questi {len(loaded)} messaggi in arrivo. Chiama triage per ciascuno '
              f'(id: {", ".join(map(str, ids))}).\n\n' + '\n\n'.join(blocks))
    system = prompts.system_prompt('inbox', effects=journal.executed(turn_key))
    error = None
    try:
        runtime.run(state, system, prompt, 'agent_inbox', config.AGENT_BACKGROUND_TIMEOUT)
    except BaseException as exc:
        error = exc
        raise
    finally:
        audit.done(state, error)
    with db._conn() as conn:
        missing = [mid for mid in ids if not conn.execute(
            'SELECT 1 FROM message_analysis WHERE message_id=%s', (mid,)).fetchone()]
    if primary in missing:
        raise RuntimeError('Triage incompleto: il messaggio non è stato registrato')


# ------------------------------------------------------------ riepilogo serale
def digest_text(window_start, now, until, day):
    """Testo del riepilogo (generato dall'agente) e messaggi letti, per sigillare le fonti."""
    runtime = runtime_module.default()
    turn_key = f'digest:{day}'
    state = toolkit.TurnState('digest', jobs.execution_context(), turn_key)
    audit = _Audit('digest', turn_key)
    state.run_id = audit.run_id
    fmt = lambda moment: moment.astimezone(prompts.ZoneInfo(config.TIMEZONE)).strftime('%d/%m %H:%M')
    system = prompts.system_prompt('digest', window=(fmt(window_start), fmt(now), fmt(until)))
    prompt = (f'Scrivi il riepilogo per il proprietario. Le comunicazioni vanno dal {window_start.astimezone(timezone.utc):%Y-%m-%d %H:%M} '
              f'al {now.astimezone(timezone.utc):%Y-%m-%d %H:%M} (UTC); il piano arriva a {until.astimezone(timezone.utc):%Y-%m-%d %H:%M} (UTC).')
    error = None
    try:
        text = runtime.run(state, system, prompt, 'agent_inbox', config.AGENT_BACKGROUND_TIMEOUT)
    except BaseException as exc:
        error = exc
        raise
    finally:
        audit.done(state, error)
    return text.strip(), sorted(state.touched)
