"""Evidence-gated task lifecycle. AI runs outside transactions; effects use the outbox.

Live ingestion opts in explicitly; historical imports remain archive-only.
Model proposals are validated against versioned snapshots before any effects.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import brain
import db
import documents
import flows
import jobs
import pipeline
import policy
from connectors import media_text


def migrate(conn):
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190424)')
        conn.execute("""ALTER TABLE tasks ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;
            CREATE TABLE IF NOT EXISTS task_changes (
                id BIGSERIAL PRIMARY KEY, task_id BIGINT NOT NULL REFERENCES tasks(id),
                message_id BIGINT, evidence_key TEXT NOT NULL,
                operation TEXT NOT NULL CHECK(operation IN ('complete','update','cancel')),
                changes JSONB NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
                expected_version BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','applied','rejected','stale')),
                UNIQUE(evidence_key,task_id));
            CREATE TABLE IF NOT EXISTS task_reconciliation_runs (key TEXT PRIMARY KEY);""")
        conn.execute("""ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_evidence_id BIGINT;
            ALTER TABLE task_changes ADD COLUMN IF NOT EXISTS evidence_revision TEXT NOT NULL DEFAULT ''""")
        # Existing installations scheduled reminders before deadline fencing.
        # Upgrade only unfinished legacy payloads; never rearm delivered jobs.
        conn.execute("""UPDATE work_jobs j SET payload=j.payload ||
            jsonb_build_object('deadline',t.deadline)
            FROM tasks t WHERE j.kind='action' AND j.payload->>'type'='reminder'
            AND j.payload->>'id'=t.id::text AND NOT (j.payload ? 'deadline')
            AND j.status IN ('pending','running') AND t.status='open'
            AND t.deadline IS NOT NULL""")


def _key(mid, revision=''):
    if type(mid) is not int or mid <= 0 or not isinstance(revision, str):
        raise ValueError('Invalid reconciliation identity')
    return 'reconcile:' + hashlib.sha256(json.dumps([mid, revision]).encode()).hexdigest()


def queue(conn, mid, revision=''):
    if db.message_store.reconciliation_excluded(conn, mid):
        return None
    return jobs.enqueue(conn, 'reconcile', _key(mid, revision),
                        {'message_id': mid, 'revision': revision})


def _fields(operation, changes):
    if operation not in ('complete', 'update', 'cancel') or type(changes) is not dict:
        raise ValueError('Invalid task operation')
    if set(changes) - {'title', 'deadline'} or (operation != 'update' and changes):
        raise ValueError('Forbidden task fields')
    if operation == 'update' and not changes:
        raise ValueError('Empty update')
    if 'title' in changes and (not isinstance(changes['title'], str) or not changes['title'].strip()):
        raise ValueError('Empty title')
    if changes.get('deadline') is not None:
        if not isinstance(changes['deadline'], str):
            raise ValueError('Invalid deadline')
        deadline = datetime.fromisoformat(changes['deadline'])
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError('Deadline requires UTC offset')
    return changes.copy()


def _explicit_quote(evidence, texts):
    """A substring (e.g. 'paid' in 'not paid') is not sufficient evidence."""
    uncertain = r'\b(not|no|non|mai|never|perhaps|maybe|forse|if|se|would|potrebbe)\b|\?'
    return any(evidence.strip().rstrip('.!;') == sentence.strip().rstrip('.!;')
               and not re.search(uncertain, sentence, re.IGNORECASE)
               for text in texts for sentence in re.split(r'[\n.!;]+', text or ''))


def parse_changes(result, tasks, body, extracted='', *, allow_visual_evidence=False):
    """Reject malformed batches atomically; missing evidence is a proposal, not failure."""
    if type(result) is not dict or type(result.get('changes')) is not list:
        raise ValueError('Invalid reconciliation result')
    candidates = {t['id']: t for t in tasks}
    parsed, seen = [], set()
    for item in result['changes']:
        if type(item) is not dict or set(item) - {
                'task_id', 'operation', 'changes', 'reason', 'evidence', 'certainty', 'unique_match'}:
            raise ValueError('Invalid change fields')
        identity = item.get('task_id')
        if type(identity) is not int or identity not in candidates or identity in seen:
            raise ValueError('Unknown or duplicate task')
        seen.add(identity)
        operation = item.get('operation')
        changes = _fields(operation, item.get('changes', {}))
        evidence, reason = item.get('evidence', ''), item.get('reason', '')
        certainty, unique = item.get('certainty', 'uncertain'), item.get('unique_match', False)
        if not isinstance(evidence, str) or not isinstance(reason, str):
            raise ValueError('Invalid evidence/reason')
        if not isinstance(certainty, str) or type(unique) is not bool:
            raise ValueError('Invalid certainty/unique_match')
        quoted = bool(evidence.strip()) and any(evidence in text for text in (body or '', extracted or ''))
        if evidence and (not evidence.strip() or (not quoted and not allow_visual_evidence)):
            raise ValueError('Evidence must quote current message or document')
        parsed.append(dict(task_id=identity, operation=operation, changes=changes,
                           expected_version=candidates[identity]['version'], reason=reason,
                           evidence=evidence, auto=quoted and certainty == 'certain'
                           and unique and len(tasks) <= 40 and operation != 'cancel'
                           and _explicit_quote(evidence, (body, extracted))))
    return parsed


def _notify(conn, key, text, evidence_mid):
    """Queue proposal notifications with their native evidence provenance.

    evidence_message_id is the message the proposal was derived from: it is
    the native authority for the notify effect (policy.can_operate), while the
    'message_id' key stays reserved for verdict notifications (which mark
    message_analysis.notified). Never truncate Telegram's plain-text limit.
    """
    for index, start in enumerate(range(0, len(text), 3500)):
        jobs.enqueue(conn, 'action', f'{key}:{index}',
                     {'type': 'notify', 'text': text[start:start + 3500], 'html': False,
                      'evidence_message_id': evidence_mid})


def _allows_candidate(conn, identity, *, expected_version=None):
    # Lazy import: owner dialogue also calls the task lifecycle. Resolve native
    # origins without adding private provenance/receipts to the exported task.
    from owner_dialogue import allows_source
    if not allows_source(conn, dict(source_type='task', source_id=identity)):
        return False
    # last_evidence_id is only the latest input. Earlier applied changes may
    # still contribute to the title/deadline; pending/rejected proposals do not.
    # A legacy applied change without a native source cannot authorize egress.
    rows = conn.execute('''SELECT DISTINCT message_id FROM task_changes
        WHERE task_id=%s AND status='applied' ''', (identity,)).fetchall()
    if not all(allows_source(conn, dict(source_type='message', source_id=row[0]))
               for row in rows):
        return False
    # Authorize the snapshot that will actually leave the machine, not newer
    # provenance after a concurrent task update/close. No lock spans inference.
    if expected_version is not None:
        return conn.execute('SELECT status,version FROM tasks WHERE id=%s',
                            (identity,)).fetchone() == ('open', expected_version)
    return True


def reconcile(payload):
    mid, revision = payload['message_id'], payload.get('revision', '')
    key = _key(mid, revision)
    (channel, sender, subject, body, _), attachments = pipeline.load_message(mid)
    flow = ('email' if channel.startswith('email') else 'whatsapp' if channel == 'whatsapp'
            else 'telegram_user' if channel in ('telegram_user', 'telegram_account', 'telegram (account)')
            else 'telegram_bot')
    if not flows.enabled(flow):
        raise jobs.Deferred('Source paused')
    with db._conn() as conn:
        jobs.assert_current(conn)
        if not policy.can_operate(conn, mid, 'reconcile'):
            return
        if conn.execute('SELECT 1 FROM task_reconciliation_runs WHERE key=%s', (key,)).fetchone():
            return
        if not policy.can_operate(conn, mid, 'ai_text'):
            raise jobs.Deferred('Reconciliation data policy blocked')
        if attachments and not policy.can_operate(conn, mid, 'ai_media'):
            raise jobs.Deferred('Reconciliation media policy blocked')
        rows = conn.execute("""SELECT t.id,t.title,t.deadline,t.version,t.source_message,
                c.sender,c.subject,c.body FROM tasks t
                LEFT JOIN communications c ON c.id=t.source_message
                WHERE t.status='open' AND t.source_message IS DISTINCT FROM %s
                AND (CASE WHEN t.source_message IS NULL THEN t.created_at
                          ELSE c.acquired_at END)
                    <= (SELECT acquired_at FROM communications WHERE id=%s)
                AND (CASE WHEN t.source_message IS NULL THEN t.created_at ELSE c.ts END IS NULL
                     OR (CASE WHEN t.source_message IS NULL THEN t.created_at ELSE c.ts END)
                         <= (SELECT COALESCE(ts,acquired_at) FROM communications WHERE id=%s))
                ORDER BY t.id LIMIT 200""", (mid, mid, mid)).fetchall()
        # Task fields and source text both leave the machine. NULL source_message
        # is not consent: owner origins and all tracked derived inputs must pass
        # the shared native read-only text/history/media gate independently.
        rows = [row for row in rows if _allows_candidate(conn, row[0])]
    # Acquisition order admits tasks analyzed late; native message time also
    # prevents newly imported historical messages from changing newer tasks.
    original_body = body
    body = media_text.message_text(body, attachments) if rows else body
    tasks = [dict(zip(('id', 'title', 'deadline', 'version', 'source_message',
                      'sender', 'subject', 'body'), row)) for row in rows]
    extracted, changes = documents.attachment_text(attachments), []
    for start in range(0, len(tasks), 40):
        batch = tasks[start:start + 40]
        # Long multi-batch runs must respect lease loss and data-policy revocation
        # between calls, not only at the beginning of the handler.
        with db._conn() as conn:
            jobs.assert_current(conn)
            if not policy.can_operate(conn, mid, 'ai_text'):
                raise jobs.Deferred('Reconciliation data policy blocked')
            if attachments and not policy.can_operate(conn, mid, 'ai_media'):
                raise jobs.Deferred('Reconciliation media policy blocked')
            if any(not _allows_candidate(conn, t['id'], expected_version=t['version']) for t in batch):
                raise jobs.Deferred('Candidate data policy blocked')
        jobs.assert_current()
        result = brain.reconcile_tasks(subject, sender, body, channel, attachments, batch)
        # Image descriptions are generated inferences, not quoted native text.
        evidence_body = original_body if any(media_text.kind(a) == 'image' for a in attachments) else body
        parsed = parse_changes(result, batch, evidence_body, extracted,
                               allow_visual_evidence=any(media_text.kind(a) == 'image' for a in attachments))
        # Relative dates in delayed evidence need explicit owner confirmation.
        for change in parsed:
            if 'deadline' in change['changes']:
                change['auto'] = False
        # The model has not seen the other batches: its uniqueness claim is local.
        if len(tasks) > 40:
            for change in parsed:
                change['auto'] = False
        changes.extend(parsed)
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        if not policy.can_operate(conn, mid, 'reconcile'):
            return
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (mid,))
        if revision and not conn.execute("""SELECT id FROM telegram_messages
            WHERE id=%s AND (metadata->>'revision_date')::timestamptz=%s::timestamptz
            FOR SHARE""", (mid, revision)).fetchone():
            # An edit accepted while AI was running invalidates its old evidence.
            return
        if not conn.execute('INSERT INTO task_reconciliation_runs(key) VALUES (%s) '
                            'ON CONFLICT DO NOTHING RETURNING key', (key,)).fetchone():
            return
        for change in sorted(changes, key=lambda item: item['task_id']):
            # Lock before inserting the FK-backed audit row, in stable task order.
            task = conn.execute('SELECT status,version FROM tasks WHERE id=%s FOR UPDATE',
                                (change['task_id'],)).fetchone()
            change.update(message_id=mid, evidence_key=key, evidence_revision=revision)
            identity = _record(conn, change)
            if identity is None:
                continue
            # Certainty/unique_match describe evidence, not permission. External
            # statements (including own outgoing not strongly authenticated)
            # remain proposals. Native private non-forwarded owner input only.
            if (change['auto'] and change['evidence'] in (original_body or '')
                    and _explicit_quote(change['evidence'], (original_body,))
                    and policy.can_operate(conn, mid, 'task_apply')):
                if apply_change(conn, identity) == 'applied':
                    _notify(conn, f'task-auto:{identity}',
                            'Modifica automatica applicata\n' + _describe(change, tasks), mid)
            else:
                # Recheck snapshots even for proposals: do not advertise stale changes.
                if not task or task != ('open', change['expected_version']):
                    conn.execute("UPDATE task_changes SET status='stale' WHERE id=%s", (identity,))
                    continue
                _notify(conn, f'task-proposal:{identity}',
                        f"Proposta #{identity} — in attesa di conferma\n{_describe(change, tasks)}\n"
                        f"/conferma_attivita {identity}\n/rifiuta_attivita {identity}", mid)


def _describe(change, tasks):
    task = next(t for t in tasks if t['id'] == change['task_id'])
    operation = {'complete': 'Completa', 'cancel': 'Annulla', 'update': 'Aggiorna'}[change['operation']]
    lines = [f"{operation} attività #{task['id']}: {task['title']}"]
    for field, label in (('title', 'Titolo'), ('deadline', 'Scadenza')):
        if field in change['changes']:
            lines.append(f"{label}: {change['changes'][field] or 'nessuna'}")
    lines.extend([change.get('reason') or 'Motivo non specificato.',
                  'Evidenza: ' + (change.get('evidence') or 'nessuna citazione testuale verificabile')])
    return '\n'.join(lines)


def _record(conn, data):
    row = conn.execute('''INSERT INTO task_changes
        (task_id,message_id,evidence_key,operation,changes,reason,evidence,expected_version,evidence_revision)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(evidence_key,task_id) DO NOTHING RETURNING id''',
        (data['task_id'], data.get('message_id'), data['evidence_key'], data['operation'],
         json.dumps(data['changes']), data.get('reason', ''), data.get('evidence', ''),
         data['expected_version'], data.get('evidence_revision', ''))).fetchone()
    return row[0] if row else None


def calendar_key(identity, version):
    """Digest must use this key and reread version under the task row lock."""
    return f'task:{identity}:v{version}'


def reminder_is_current(conn, payload):
    """Delivery gate: owner jobs require current version and exact due time.

    Legacy jobs without a deadline fail closed. Call immediately before sending:
    a claimed old job can outlive cancellation or a deadline replacement.
    """
    if payload.get('owner_reminder'):
        if type(payload.get('version')) is not int:
            return False
        try:
            remind_at = datetime.fromisoformat(payload['remind_at'])
            if remind_at.tzinfo is None or remind_at.utcoffset() is None:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        row = conn.execute("""SELECT version,owner_deadline_date,owner_remind_at,deadline FROM tasks
            WHERE id=%s AND status='open'""", (payload['id'],)).fetchone()
        return bool(row and row[0] == payload['version']
                    and pipeline.owner_reminder_time(dict(zip(
                        ('owner_deadline_date', 'owner_remind_at', 'deadline'), row[1:]))) == remind_at
                    and remind_at <= datetime.now(timezone.utc))
    try:
        deadline = datetime.fromisoformat(payload['deadline'])
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    row = conn.execute("""SELECT deadline FROM tasks WHERE id=%s AND status='open'
        AND owner_origin_message IS NULL AND owner_operation_key IS NULL
        AND owner_deadline_date IS NULL AND owner_remind_at IS NULL""",
                       (payload['id'],)).fetchone()
    return bool(row and row[0] == deadline
                and deadline <= datetime.now(timezone.utc) + timedelta(hours=24))


def _derived(conn, identity, version, operation, changes, deadline, title=None):
    if operation != 'update' or changes:
        rows = conn.execute("""UPDATE appointments SET sync_key=NULL,done=TRUE,
                sync_state=CASE WHEN sync_state IN ('pending','synced','cancel_pending')
                    THEN 'cancel_pending' ELSE 'cancelled' END
                WHERE sync_key=%s OR sync_key LIKE %s RETURNING id,sync_state,event_data""",
                (f'task:{identity}', f'task:{identity}:v%')).fetchall()
        for appointment, state, _ in rows:
            if state == 'cancel_pending':
                jobs.enqueue(conn, 'action', f'task-calendar:{identity}:{version}:{appointment}',
                             {'type': 'calendar', 'id': appointment})
        # Reusing the old key would reuse Google's deterministic ID of the
        # deleted event, as well as an already completed calendar outbox job.
        if (operation == 'update' and rows and deadline is not None
                and deadline > datetime.now(timezone.utc)):
            event = dict(rows[0][2] or {})
            event.update(title=title or changes.get('title') or event.get('title'),
                         dt=deadline.isoformat())
            event = pipeline.gcal.validate_event(event)
            pipeline.save_event(conn, event, calendar_key(identity, version), f'task:{identity}',
                                approved=any(state == 'cancel_pending' for _, state, _ in rows))
    reminder = f'task-reminder:{identity}'
    if operation != 'update' or ('deadline' in changes and deadline is None):
        conn.execute("""UPDATE work_jobs SET status='done',finished_at=now(),token=NULL,
            lease_until=NULL WHERE key=%s""", (reminder,))
    elif 'deadline' in changes:
        jobs.enqueue(conn, 'action', reminder,
                     {'type': 'reminder', 'id': identity, 'deadline': deadline.isoformat()})
        conn.execute("""UPDATE work_jobs SET status='pending',attempts=0,token=NULL,
            lease_until=NULL,error=NULL,finished_at=NULL,
            payload=%s,available_at=GREATEST(now(),%s::timestamptz-interval '24 hours')
            WHERE key=%s""", (json.dumps({'type': 'reminder', 'id': identity,
                                          'deadline': deadline.isoformat()}), deadline, reminder))


def apply_change(conn, proposal, *, authority_message_id=None):
    """Apply a proposal ID or trusted audit data, atomically. Returns applied/stale/etc.

    Dict callers must supply task_id, expected_version, evidence_key, operation,
    changes. Without message evidence/context this retains the trusted internal
    legacy API. Worker/owner callers are checked using native message IDs only;
    authority_message_id is the current confirmation, never a model boolean.
    """
    with conn.transaction():
        jobs.assert_current(conn)
        context = jobs.execution_context()
        authority = authority_message_id
        if authority is None and context and context.kind == 'owner':
            authority = context.message_id
        if authority is not None and not policy.can_operate(
                conn, authority, 'owner_command', owner_required=True):
            return 'blocked'
        if context and context.kind == 'owner' and authority is None:
            return 'blocked'
        if context and authority is not None and (
                context.kind != 'owner' or authority != context.message_id):
            return 'blocked'  # An old owner ID in a model/background payload is not current authority.
        if type(proposal) is dict:
            _fields(proposal.get('operation'), proposal.get('changes'))
            if (type(proposal.get('task_id')) is not int
                    or type(proposal.get('expected_version')) is not int
                    or not isinstance(proposal.get('evidence_key'), str) or not proposal['evidence_key']):
                raise ValueError('Invalid audit identity')
            conn.execute('SELECT id FROM tasks WHERE id=%s FOR UPDATE', (proposal['task_id'],))
            proposal = _record(conn, proposal)
            if proposal is None:
                return 'duplicate'
        if type(proposal) is not int:
            raise ValueError('Invalid proposal ID')
        row = conn.execute('SELECT task_id FROM task_changes WHERE id=%s', (proposal,)).fetchone()
        if not row:
            return 'missing'
        task_id = row[0]
        # All lifecycle paths lock task first, then proposals: avoid confirmation deadlocks.
        task = conn.execute('SELECT status,version,title,deadline FROM tasks WHERE id=%s FOR UPDATE',
                            (task_id,)).fetchone()
        row = conn.execute('''SELECT operation,changes,expected_version,status FROM task_changes
                              WHERE id=%s FOR UPDATE''', (proposal,)).fetchone()
        operation, changes, version, status = row
        if status != 'pending':
            return status
        evidence = conn.execute('''SELECT message_id,evidence_revision FROM task_changes
                                   WHERE id=%s''', (proposal,)).fetchone()
        if evidence and evidence[0] is not None:
            mid, revision = evidence
            # Recheck execution-time provenance even for already queued changes.
            # A current owner may confirm a live third-party proposal, never history.
            if not policy.can_operate(conn, mid, 'task_propose'):
                return 'blocked'
            if authority is None and not policy.can_operate(conn, mid, 'task_apply'):
                return 'blocked'
            if revision and not conn.execute('''SELECT id FROM telegram_messages
                    WHERE id=%s AND (metadata->>'revision_date')::timestamptz=%s::timestamptz
                    FOR SHARE''', (mid, revision)).fetchone():
                conn.execute("UPDATE task_changes SET status='stale' WHERE id=%s", (proposal,))
                return 'stale'
            newer = conn.execute('''SELECT 1 FROM tasks t
                JOIN communications previous ON previous.id=t.last_evidence_id
                JOIN communications current ON current.id=%s
                WHERE t.id=%s AND previous.id<>current.id AND
                  (COALESCE(previous.ts,previous.acquired_at),previous.acquired_at,previous.id) >
                  (COALESCE(current.ts,current.acquired_at),current.acquired_at,current.id)''',
                (mid, task_id)).fetchone()
            if newer:
                conn.execute("UPDATE task_changes SET status='stale' WHERE id=%s", (proposal,))
                return 'stale'
        elif context and authority is None:
            return 'blocked'  # Background/model payloads cannot use the legacy bypass.
        if not task or task[0] != 'open' or task[1] != version:
            conn.execute("UPDATE task_changes SET status='stale' WHERE id=%s", (proposal,))
            return 'stale'
        changes = _fields(operation, changes)
        title, deadline = changes.get('title', task[2]), changes.get('deadline', task[3])
        if isinstance(deadline, str):
            deadline = datetime.fromisoformat(deadline)
        target = {'complete': 'done', 'cancel': 'cancelled', 'update': 'open'}[operation]
        conn.execute('''UPDATE tasks SET title=%s,deadline=%s,status=%s,version=version+1,
            completed_at=CASE WHEN %s='open' THEN NULL ELSE now() END
            WHERE id=%s AND status='open' AND version=%s''',
            (title, deadline, target, target, task_id, version))
        conn.execute("UPDATE task_changes SET status='applied' WHERE id=%s", (proposal,))
        if evidence and evidence[0] is not None:
            conn.execute('UPDATE tasks SET last_evidence_id=%s WHERE id=%s', (evidence[0], task_id))
        conn.execute("""UPDATE task_changes SET status='stale'
                        WHERE task_id=%s AND id<>%s AND status='pending'""", (task_id, proposal))
        _derived(conn, task_id, version + 1, operation, changes, deadline, title)
        return 'applied'


def close_task(conn, identity):
    """Manual /fatto, including versioning, audit and derived work cleanup."""
    if type(identity) is not int:
        raise ValueError('Invalid task ID')
    with conn.transaction():
        row = conn.execute('SELECT status,version FROM tasks WHERE id=%s FOR UPDATE', (identity,)).fetchone()
        if not row or row[0] != 'open':
            return False
        return apply_change(conn, dict(task_id=identity, expected_version=row[1],
            evidence_key='manual:' + uuid.uuid4().hex, operation='complete', changes={},
            reason='Completamento manuale')) == 'applied'


def owner_commands(text, *, message_id=None):
    parts = text.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ''
    if command not in ('/conferma_attivita', '/rifiuta_attivita', '/proposte'):
        return False
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        context = jobs.execution_context()
        authority = message_id if message_id is not None else (
            context.message_id if context else None)
        if (context or message_id is not None) and not policy.can_operate(
                conn, authority, 'owner_command', owner_required=True):
            return True  # Recognized but not authorized: no effects or replies.
        if command == '/proposte':
            rows = conn.execute("""SELECT p.id,p.task_id,p.operation,p.changes,p.reason,p.evidence,t.title
                FROM task_changes p JOIN tasks t ON t.id=p.task_id
                WHERE p.status='pending' AND t.status='open' AND t.version=p.expected_version
                ORDER BY p.id""").fetchall()
            reply = '\n\n'.join(
                f'Proposta #{r[0]} — in attesa di conferma\n' +
                _describe(dict(task_id=r[1], operation=r[2], changes=r[3], reason=r[4], evidence=r[5]),
                          [dict(id=r[1], title=r[6])]) +
                f'\n/conferma_attivita {r[0]} /rifiuta_attivita {r[0]}' for r in rows)
            reply = reply or 'Nessuna proposta pendente.'
        elif len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
            reply = 'Specificare un ID numerico.'
        elif command == '/conferma_attivita':
            if authority is None:
                reply = 'Esito proposta: ' + apply_change(conn, int(parts[1]))
            else:
                reply = 'Esito proposta: ' + apply_change(
                    conn, int(parts[1]), authority_message_id=authority)
        else:
            row = conn.execute("""UPDATE task_changes SET status='rejected'
                WHERE id=%s AND status='pending' RETURNING id""", (int(parts[1]),)).fetchone()
            reply = 'Proposta rifiutata.' if row else 'Proposta non pendente.'
        _notify(conn, 'task-command:' + uuid.uuid4().hex, reply, authority)
    return True