"""Owner-only natural language tasks, not calendar events.

Integration contract:
* Authenticate the owner BEFORE calling apply; this module is not an ingress
  authenticator. message_id is a positive archived owner-message ID, message_time
  is its original aware datetime (or ISO string), never the processing time.
* The agent proposes a change through its task tools; the SERVER validates it
  here (exact evidence sentence, canonical fields, grounded dates, snapshot
  versions) and builds the prepared change. The model never reaches apply with
  unchecked fields or its own authority.
* apply accepts ONLY a validated prepared change, uses an atomic transaction/savepoint
  and never commits an enclosing caller transaction. Its confirmations are provisional
  until that transaction commits. Do not send a reply before committing.
* Every call carries its own idempotency prefix, so several task tool calls in one
  turn never collide and a retried turn replays instead of duplicating.

Required migration (NOT performed here): tasks.owner_deadline_date DATE,
owner_origin_message BIGINT, owner_operation_key TEXT UNIQUE, owner_remind_at
TIMESTAMPTZ, plus task_reconciliation.migrate's version/audit/outbox schema.
source_message is always NULL for new owner tasks. Lifecycle evidence is stored
in task_changes with the owner-message ID and the stable operation key. Extra
date/reminder fields are mentioned in the audit reason, updated under the same
lock/version boundary. No scheduler or calendar integration is installed here.
The scheduler must consume the date/reminder columns, fence deliveries against
current task state/version, and describe date-only morning reminders as defaults,
not as an hour requested by the owner.

Retry with the SAME prepared batch. Keys are {prefix}:{index} (zero based), by default
owner-commitment:{message_id}. Lifecycle receipts reject a changed batch on retry. Creation
retries compare stored fields and fail closed if the task was subsequently edited;
there is no separate immutable creation receipt or receipt for empty batches.
"""

import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db
import task_reconciliation as lifecycle


MAX_CHANGES = 8
MAX_TASKS = 200
MAX_BULK = 60


class Clarification(ValueError):
    """An entire batch must remain unapplied pending owner clarification."""


class StaleBatch(Clarification):
    pass


def _timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Original message_time requires an explicit UTC offset')
    return value


def _tasks(conn):
    names = ('id', 'version', 'title', 'deadline', 'owner_deadline_date', 'owner_remind_at')
    rows = conn.execute("""SELECT id,version,title,deadline,owner_deadline_date,owner_remind_at
        FROM tasks WHERE status='open' ORDER BY id LIMIT 201""").fetchall()
    if len(rows) > MAX_TASKS:
        raise Clarification('Troppe attività candidate: specifica l’ID e usa il comando esplicito.')
    return [dict(zip(names, row)) for row in rows]


def _snapshot():
    with db._conn() as conn:
        return _tasks(conn)


def sentence(text, evidence):
    """Fail closed on quoted, reported or uncertain commands.

    Structural guard only: the evidence must be an exact, unquoted, current
    owner sentence, not a question or hypothesis. Intent (create/update/
    complete/cancel, remember, standing instruction) is judged by the agent from
    natural language; this guard never demands specific verbs and never rejects
    negated formulations that can be legitimate ('non c'è più nulla da fare' = complete).
    """
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 4000:
        raise Clarification('Quale impegno personale vuoi registrare o modificare?')
    lines, fenced, forwarded = [], False, False
    for line in text.splitlines():
        if re.search(r'forwarded message|messaggio inoltrato|begin forwarded|original message|messaggio originale', line, re.I):
            forwarded = True
        if forwarded:
            continue
        if re.match(r'^\s*(```|~~~)', line):
            fenced = not fenced
            continue
        if fenced or re.match(r'^\s*(>|["“«]|(?:assistant|assistente|system|user|utente)\s*:)', line, re.I):
            continue
        if re.search(r'\b(dice|ha detto|scrive|ha scritto|said|says|wrote|forwarded|inoltrato)\b', line, re.I):
            continue
        # Inline quoted instructions do not become evidence. Conservative even
        # for task titles in quotes: the caller can ask the owner to rephrase.
        line = re.sub(r'"[^"\n]*"|“[^”\n]*”|«[^»\n]*»|(?<!\w)\x27[^\x27\n]+\x27', '', line)
        lines.extend(re.split(r'(?<=[.!?;])\s+', line))
    canonical = lambda s: s.strip().rstrip('.!;').strip()
    if not any(canonical(evidence) == canonical(line) for line in lines):
        raise Clarification('Conferma la richiesta con una frase tua, non citata.')
    uncertain = r'\?|\b(forse|magari|se|if|maybe|perhaps|would|potrei|potresti|dovrei|should)\b'
    if re.search(uncertain, evidence, re.I):
        raise Clarification('Vuoi davvero applicare questa richiesta? La frase è una domanda o un’ipotesi.')


def _evidence(text, evidence, operation):
    """Task evidence: structural sentence guard plus the appointment heuristic."""
    sentence(text, evidence)
    appointment = r'\b(appuntamento|incontro|riunione|meeting|appointment|visita|dentista|cena|pranzo|vedere|vedrò|incontrare|incontrerò)\b'
    specified = r'\d{1,2}[:.]\d{2}|\b(alle|ore|at|presso|in ufficio|al bar|a casa|ristorante|studio)\b'
    if re.search(appointment, evidence, re.I) and re.search(specified, evidence, re.I):
        raise Clarification('Questo sembra un appuntamento: usa il percorso calendario, non un’attività.')


def _fields(operation, fields, evidence):
    allowed = {'title', 'deadline', 'owner_deadline_date', 'owner_remind_at'}
    if type(fields) is not dict or set(fields) - allowed:
        raise ValueError('Invalid task fields')
    if operation in ('complete', 'cancel'):
        if fields:
            raise ValueError('Closing operations cannot change fields')
        return {}
    if not fields or (operation == 'create' and 'title' not in fields):
        raise ValueError('Missing task fields')
    result = fields.copy()
    if 'title' in result:
        title = result['title']
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError('Invalid title')
        result['title'] = title.strip()
    for name in ('deadline', 'owner_remind_at'):
        value = result.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})', value):
            raise ValueError('Invalid aware task timestamp')
        parsed = _timestamp(value)
        local = parsed.astimezone(ZoneInfo(config.TIMEZONE))
        if local.replace(tzinfo=None) != parsed.replace(tzinfo=None):
            raise ValueError('Timestamp offset must match configured timezone/DST')
        if not re.search(r'\d{1,2}[:.]\d{2}|\b(?:alle|ore|at)\s+\d{1,2}\b|\b(mezzogiorno|mezzanotte|noon|midnight)\b|\b(?:tra|fra|in)\s+(?:\d+|un[ao]?|due|tre|one|two|three)\s+(?:or[ae]|minut[oi]|hours?|minutes?)\b', evidence, re.I):
            raise Clarification('Hai indicato una data, ma non un orario: conferma senza inventare l’ora.')
        if name == 'owner_remind_at' and not re.search(r'\b(ricordam\w*|promemoria|avvisam\w*|remind\w*|reminder|notify)\b', evidence, re.I):
            raise Clarification('Vuoi anche un promemoria a un orario preciso?')
        result[name] = parsed.isoformat()
    if result.get('owner_deadline_date') is not None:
        value = result['owner_deadline_date']
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Invalid date-only deadline')
        date.fromisoformat(value)
        if result.get('deadline') is not None:
            raise ValueError('Date-only and timed deadlines are mutually exclusive')
        result['deadline'] = None
    elif result.get('deadline') is not None:
        result['owner_deadline_date'] = None
    # An explicit removal clears both deadline representations.
    elif 'deadline' in result and 'owner_deadline_date' not in result:
        result['owner_deadline_date'] = None
    elif 'owner_deadline_date' in result and 'deadline' not in result:
        result['deadline'] = None
    return result


def _ground_dates(fields, evidence, message_time):
    """Check supported calendar dates, not clocks or model-only expressions.

    Scope is the item's authenticated evidence, never the whole owner message.
    A separate reminder clause is not evidence for the deadline's date. Unknown
    expressions (e.g. weekdays) deliberately remain the extractor's responsibility.
    """
    deadline = fields.get('owner_deadline_date') or fields.get('deadline')
    reminder = fields.get('owner_remind_at')
    if deadline is None and reminder is None:
        return
    pattern = (r"\b(?:l['’]altro\s+ieri|dopodomani|domani|oggi|ieri)\b"
               r'|\b(?:tra|fra)\s+\d+\s+giorni\b'
               r'|\b\d{4}-\d{2}-\d{2}\b|\b\d{2}/\d{2}/\d{4}\b')
    scope = evidence
    reminder_request = re.search(
        r'\b(?:ricordam\w*|promemoria|avvisam\w*|remind\w*|reminder|notify)\b', evidence, re.I)
    deadline_request = re.search(r'\b(?:entro|scadenza|scade)\b', evidence, re.I)
    if deadline is not None:
        value = deadline
        if (reminder_request and reminder_request.start() > 0
                and (re.search(pattern, evidence[:reminder_request.start()], re.I)
                     or (deadline_request and deadline_request.start() < reminder_request.start()))):
            scope = evidence[:reminder_request.start()]
        elif reminder_request and deadline_request:
            scope = evidence[deadline_request.start():]
    else:
        # A deadline and its reminder can have different dates, including when
        # the deadline expression is unsupported ("scadenza venerdì").
        if deadline_request:
            return
        value = reminder
        if reminder_request:
            scope = evidence[reminder_request.end():]

    anchor = _timestamp(message_time).astimezone(ZoneInfo(config.TIMEZONE)).date()
    dates = set()
    for match in re.finditer(pattern, scope, re.I):
        reference = match.group().casefold().replace('’', "'")
        try:
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', reference):
                resolved = date.fromisoformat(reference)
            elif '/' in reference:
                resolved = datetime.strptime(reference, '%d/%m/%Y').date()
            else:
                offsets = {"l'altro ieri": -2, 'ieri': -1, 'oggi': 0,
                           'domani': 1, 'dopodomani': 2}
                relative = re.search(r'\d+', reference)
                offset = int(relative.group()) if relative else offsets[re.sub(r'\s+', ' ', reference)]
                resolved = anchor + timedelta(days=offset)
        except (ValueError, OverflowError):
            raise Clarification('Quale data valida intendi per questa attività?') from None
        dates.add(resolved)
    if len(dates) > 1:
        raise Clarification('Quale delle date indicate si riferisce a questa attività?')
    if dates and date.fromisoformat(value[:10]) not in dates:
        raise Clarification('La data estratta non corrisponde alla richiesta: conferma la data dell’attività.')


def build_change(text, message_time, tasks, operation, task_id, fields, evidence):
    """One validated per-task change from the agent's structured request; no writes.

    Raises Clarification when the request is not safe to apply as stated. The
    result is exactly what apply() accepts: the server, not the model, decides
    the expected version from the CURRENT snapshot.
    """
    if len(tasks) > MAX_TASKS:
        raise Clarification('Specifica l’ID dell’attività da modificare.')
    if operation not in ('create', 'update', 'complete', 'cancel'):
        raise ValueError('Invalid operation')
    candidates = {task['id']: task for task in tasks}
    version = None
    if operation == 'create':
        if task_id is not None:
            raise ValueError('Creation must not target an ID')
    else:
        if type(task_id) is not int or task_id not in candidates:
            raise Clarification('Indica un’attività aperta esistente (ID).')
        version = candidates[task_id]['version']
        if type(version) is not int or version < 0:
            raise ValueError('Invalid task snapshot version')
    _evidence(text, evidence, operation)
    fields = _fields(operation, fields, evidence)
    _ground_dates(fields, evidence, message_time)
    if operation == 'create':
        title = fields['title'].casefold()
        if any(t['title'].strip().casefold() == title for t in tasks):
            raise Clarification('Questa attività esiste già: vuoi modificarla?')
    return dict(operation=operation, task_id=task_id, expected_version=version,
                changes=fields, evidence=evidence, owner_text=text)


def build_bulk(text, tasks, operation, exclude_ids, evidence):
    """One validated bulk close (all open tasks except exclude_ids); no writes."""
    if operation not in ('complete', 'cancel'):
        raise ValueError('Invalid bulk operation')
    candidates = {task['id'] for task in tasks}
    if (type(exclude_ids) is not list or len(exclude_ids) > MAX_TASKS
            or any(type(i) is not int or i not in candidates for i in exclude_ids)
            or len(set(exclude_ids)) != len(exclude_ids)):
        raise Clarification('Indica chiaramente quali attività restano aperte.')
    _evidence(text, evidence, operation)
    return dict(operation=operation, exclude_ids=sorted(exclude_ids), evidence=evidence, owner_text=text)


def _checked(changes):
    """Recheck the trusted prepared boundary before any SQL mutation.

    Returns the bulk dict for a bulk close plan, or None for a per-task list.
    """
    if isinstance(changes, dict) and set(changes) == {'operation', 'exclude_ids',
                                                      'evidence', 'owner_text'}:
        if changes['operation'] not in ('complete', 'cancel'):
            raise ValueError('Invalid bulk operation')
        if (type(changes['exclude_ids']) is not list or len(changes['exclude_ids']) > MAX_TASKS
                or any(type(i) is not int or i <= 0 for i in changes['exclude_ids'])):
            raise ValueError('Invalid bulk exclusions')
        if not isinstance(changes['evidence'], str) or not isinstance(changes['owner_text'], str):
            raise ValueError('Invalid bulk evidence')
        return changes
    if type(changes) is not list or len(changes) > MAX_CHANGES:
        raise ValueError('Invalid prepared batch')
    seen = set()
    for item in changes:
        if type(item) is not dict or set(item) != {
                'operation', 'task_id', 'expected_version', 'changes', 'evidence', 'owner_text'}:
            raise ValueError('Use build_change before apply')
        operation = item['operation']
        if operation not in ('create', 'update', 'complete', 'cancel'):
            raise ValueError('Invalid prepared operation')
        if not isinstance(item['owner_text'], str):
            raise ValueError('Missing current owner evidence')
        _evidence(item['owner_text'], item['evidence'], operation)
        if _fields(operation, item['changes'], item['evidence']) != item['changes']:
            raise ValueError('Noncanonical prepared fields')
        if operation == 'create':
            if item['task_id'] is not None or item['expected_version'] is not None:
                raise ValueError('Invalid prepared creation')
        else:
            identity, version = item['task_id'], item['expected_version']
            if type(identity) is not int or identity <= 0 or type(version) is not int or version < 0 or identity in seen:
                raise ValueError('Invalid prepared lifecycle identity')
            seen.add(identity)


def _confirmation(identity, operation, fields, duplicate=False):
    action = {'create': 'salvata', 'update': 'aggiornata', 'complete': 'completata', 'cancel': 'annullata'}[operation]
    result = f'Attività #{identity} ' + ('già registrata.' if duplicate else action + '.')
    if fields.get('owner_deadline_date'):
        result += f" Scadenza: {fields['owner_deadline_date']} (solo data; nessun orario richiesto)."
    elif fields.get('deadline'):
        result += f" Scadenza: {fields['deadline']}."
    if fields.get('owner_remind_at'):
        result += f" Orario promemoria richiesto: {fields['owner_remind_at']} (invio affidato allo scheduler)."
    return result


def _base(message_id, prefix):
    if prefix is None:
        return f'owner-commitment:{message_id}'
    if (not isinstance(prefix, str) or not prefix.startswith(f'owner-commitment:{message_id}:')
            or len(prefix) > 120 or any(c in prefix for c in ('%', '_', '\x00', '\n'))):
        raise ValueError('Invalid idempotency prefix')
    return prefix


def apply(conn, changes, message_id, *, prefix=None):
    """Atomic effects, including existing lifecycle cleanup/audit. No AI calls.

    The caller is the authenticated-owner authority. Never pass raw model output
    to this method or to lifecycle.apply_change's trusted dict interface.
    A bulk close dict closes every open task except exclude_ids, with one receipt.
    prefix (owner-commitment:{message_id}:...) makes each tool call idempotent on
    its own; without it keys are owner-commitment:{message_id}:{index}.
    """
    if type(message_id) is not int or message_id <= 0:
        raise ValueError('Invalid archived owner message ID')
    base = _base(message_id, prefix)
    checked = _checked(changes)
    if isinstance(checked, dict):
        return _apply_bulk(conn, checked, message_id, base)
    if not changes:
        return dict(status='noop', confirmations=[])
    confirmations, applied = [], False
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (base,))
        # An earlier batch commits all keys together. Never extend, truncate or
        # reinterpret it just because a retry produced a different list.
        used = conn.execute('''SELECT owner_operation_key FROM tasks
            WHERE owner_operation_key LIKE %s UNION
            SELECT evidence_key FROM task_changes WHERE evidence_key LIKE %s''',
            (f'{base}:%', f'{base}:%')).fetchall()
        if used and {row[0] for row in used} != {f'{base}:{i}' for i in range(len(changes))}:
            raise StaleBatch('Richiesta già elaborata con un elenco diverso: controlla le attività.')
        # Stable lock ordering matches the lifecycle API; preflight is atomic.
        locked = {}
        for identity in sorted({c['task_id'] for c in changes if c['task_id'] is not None}):
            locked[identity] = conn.execute('SELECT status,version FROM tasks WHERE id=%s FOR UPDATE',
                                            (identity,)).fetchone()
        for index, item in enumerate(changes):
            key = f'{base}:{index}'
            operation, fields, identity = item['operation'], item['changes'], item['task_id']
            created = conn.execute('SELECT id,title,deadline,owner_deadline_date,owner_remind_at '
                                   'FROM tasks WHERE owner_operation_key=%s', (key,)).fetchone()
            audit = conn.execute('SELECT task_id,operation,status,reason FROM task_changes '
                                 'WHERE evidence_key=%s', (key,)).fetchone()
            # A complete canonical receipt also covers sidecar-only updates.
            receipt = json.dumps(dict(operation=operation, task_id=identity, changes=fields,
                                      evidence=item['evidence']), sort_keys=True, ensure_ascii=False)
            if created or audit:
                if operation == 'create' and created and not audit:
                    expected = (fields['title'], fields.get('deadline'), fields.get('owner_deadline_date'), fields.get('owner_remind_at'))
                    actual = tuple(v.isoformat() if isinstance(v, (datetime, date)) else v for v in created[1:])
                    if actual != expected:
                        raise StaleBatch('Richiesta già elaborata con dati diversi: controlla le attività.')
                    identity = created[0]
                elif operation != 'create' and not created and audit == (identity, operation, 'applied', receipt):
                    pass
                else:
                    raise StaleBatch('Richiesta già elaborata: non posso reinterpretarla automaticamente.')
                confirmations.append(_confirmation(identity, operation, fields, duplicate=True))
                continue
            if operation == 'create':
                identity = conn.execute('''INSERT INTO tasks
                    (title,deadline,source_message,owner_deadline_date,owner_origin_message,
                     owner_operation_key,owner_remind_at) VALUES (%s,%s,NULL,%s,%s,%s,%s) RETURNING id''',
                    (fields['title'], fields.get('deadline'), fields.get('owner_deadline_date'),
                     message_id, key, fields.get('owner_remind_at'))).fetchone()[0]
            else:
                if locked[identity] != ('open', item['expected_version']):
                    raise StaleBatch('L’attività è cambiata nel frattempo: conferma nuovamente la modifica.')
                core = {k: v for k, v in fields.items() if k in ('title', 'deadline')}
                # Sidecar-only edits still need lifecycle versioning/invalidation,
                # without disturbing the existing deadline or creating an event.
                if operation == 'update' and not core:
                    core['title'] = conn.execute('SELECT title FROM tasks WHERE id=%s', (identity,)).fetchone()[0]
                result = lifecycle.apply_change(conn, dict(
                    task_id=identity, expected_version=item['expected_version'], evidence_key=key,
                    operation=operation, changes=core, message_id=message_id,
                    evidence=item['evidence'], reason=receipt))
                if result != 'applied':
                    raise StaleBatch('Modifica non applicata: verifica lo stato dell’attività.')
                for name in ('owner_deadline_date', 'owner_remind_at'):
                    if name in fields:
                        # Column names are a fixed internal allowlist, never model SQL.
                        conn.execute(f'UPDATE tasks SET {name}=%s WHERE id=%s', (fields[name], identity))
                if operation in ('complete', 'cancel'):
                    conn.execute('UPDATE tasks SET owner_remind_at=NULL WHERE id=%s', (identity,))
            applied = True
            confirmations.append(_confirmation(identity, operation, fields))
    return dict(status='applied' if applied else 'duplicate', confirmations=confirmations)


def _apply_bulk(conn, bulk, message_id, base):
    """Close every open task except the excluded IDs; one atomic receipt.

    Idempotent on retry: the receipt key is {base}:bulk.
    A retry with different exclusions fails closed like per-task batches.
    """
    operation, exclude = bulk['operation'], set(bulk['exclude_ids'])
    key = f'{base}:bulk'
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (base,))
        receipt = json.dumps(dict(operation=operation, exclude_ids=sorted(exclude)),
                             sort_keys=True, ensure_ascii=False)
        prior = conn.execute("""SELECT task_id,operation,status,reason FROM task_changes
            WHERE evidence_key=%s""", (key,)).fetchone()
        if prior:
            if prior[2] != 'applied' or prior[3] != receipt:
                raise StaleBatch('Richiesta già elaborata con esclusioni diverse: controlla le attività.')
            kept = conn.execute("""SELECT count(*) FROM tasks WHERE status='open'""").fetchone()[0]
            return dict(status='duplicate', confirmations=[
                f'Chiusura di massa già applicata in precedenza. Attività ancora aperte: {kept}.'])
        rows = conn.execute("""SELECT id,version FROM tasks WHERE status='open'
            ORDER BY id""").fetchall()
        targets = [(tid, version) for tid, version in rows if tid not in exclude]
        if not targets:
            return dict(status='noop', confirmations=[
                'Nessuna attività da chiudere: tutte quelle aperte erano escluse dalla richiesta.'])
        if len(targets) > MAX_BULK:
            raise Clarification('Troppe attività da chiudere insieme: rivedi la richiesta.')
        closed = 0
        for identity, version in targets:
            result = lifecycle.apply_change(conn, dict(
                task_id=identity, expected_version=version, evidence_key=key,
                operation=operation, changes={}, message_id=message_id,
                evidence=bulk['evidence'], reason=receipt))
            if result != 'applied':
                raise StaleBatch('Modifica non applicata: verifica lo stato dell’attività.')
            conn.execute('UPDATE tasks SET owner_remind_at=NULL WHERE id=%s', (identity,))
            closed += 1
        kept = conn.execute("""SELECT count(*) FROM tasks WHERE status='open'""").fetchone()[0]
        action = 'completate' if operation == 'complete' else 'annullate'
        return dict(status='applied', confirmations=[
            f'✅ Chiusura di massa applicata: {closed} attività {action}. Restano aperte: {kept}.'])
