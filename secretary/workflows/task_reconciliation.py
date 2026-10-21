"""Ciclo di vita delle attività: versioni, audit delle modifiche e lavoro derivato.

Nessun modello qui: `apply_change` è l'unico scrittore di modifiche a un'attività
esistente, chiamato dagli strumenti dell'agente (agent.toolkit via
owner_commitments) e dal comando /fatto con autorità nativa del proprietario.
Ogni modifica è confrontata con la versione (CAS), registrata in `task_changes`
e ripulisce promemoria ed eventi calendario derivati.
"""
import json
from datetime import datetime, timedelta, timezone

from secretary.core import jobs
from secretary.workflows import pipeline
from secretary.core import policy


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
                UNIQUE(evidence_key,task_id));""")
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
