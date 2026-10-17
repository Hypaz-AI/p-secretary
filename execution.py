"""Integrated opt-in email gateway: proposal != approval != explicit dispatch.

Current application wiring (importing this module alone starts nothing):
* db.init calls execution.migrate after jobs/secretary prerequisites, and installs
    ai_budget in the same startup migration transaction before workers accept work.
* main starts jobs.Worker(KIND, execute_approved); jobs contain identifiers only.
* Authenticated POST /api/secretary/emails calls request_email with an existing
    practice/version, exact payload, EXPLICIT account_address, expiry and stable
    operation key. GET /api/secretary/emails/<id> returns the sealed snapshot.
* POST /api/secretary/approvals/<id> resolves the exact displayed payload/version;
    approval alone NEVER sends or enqueues. Its operation/receipt key is retained
    only in the dashboard DOM, not browser storage.
* A separate owner click on POST /api/secretary/emails/<id>/dispatch checks the
    displayed sender, fixed subject, payload, approved version and approval receipt
    before queuing dispatch. Mutations require same-origin/CSRF; DashboardOwner is
    constructed by the backend, NEVER deserialized from client JSON.
* GET /api/secretary/execution projects bounded proposals/outcomes, not delivery
    proof. GET /api/secretary/budget exposes aggregate Worker AI accounting only.

Sending is NOT AI egress; ai_text/ai_media consent is neither required nor granted
by this API. Any AI used to construct a draft MUST independently authorize EACH
source with policy before model egress. Native live owner origin, current versions,
concrete approval receipt, dashboard dispatch grant and lease are checked here.

Connector ownership is external. email.send(account,to,subject,body) supports NO
attachments or Message-ID: document bytes are guarded/hashed, but dispatch of a
nonempty document list is explicitly unsupported (never silently omit them).
Subject is fixed and displayed. No custom SMTP implementation or hidden default
account. One committed attempt BEFORE SMTP, even across crashes and job retries.
True means connector accepted, NOT delivery/read confirmation. False, timeout,
crash or failed result persistence is uncertain and NEVER automatically resent.
Revocation fences the authorization transaction, not SMTP already in flight.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

import config
import db
import jobs
import knowledge
import secretary as sec

VERSION = 'secretary_email_execution_v1'
KIND = 'secretary_email_execution'
SUBJECT = 'Messaggio approvato'
MAX_DOCUMENT_BYTES = min(knowledge.MAX_BYTES, 8 * 1024 * 1024)
MAX_TOTAL_BYTES = 16 * 1024 * 1024

SCHEMA = """
CREATE TABLE secretary_email_proposals (
 approval_id BIGINT PRIMARY KEY REFERENCES secretary_approvals(id),
 account_address TEXT NOT NULL, account_hash TEXT NOT NULL,
 request_hash TEXT NOT NULL, payload_hash TEXT NOT NULL,
 subject TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE secretary_email_dispatches (
 approval_id BIGINT PRIMARY KEY REFERENCES secretary_email_proposals(approval_id),
 operation_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
 approval_version BIGINT NOT NULL, approval_operation_key TEXT NOT NULL,
 owner_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE secretary_email_attempts (
 approval_id BIGINT PRIMARY KEY REFERENCES secretary_email_dispatches(approval_id),
 job_id BIGINT NOT NULL, job_token TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('attempted','accepted','uncertain')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ);
CREATE FUNCTION secretary_email_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN RAISE EXCEPTION 'email execution append-only relation'; END $$;
"""


class UnsupportedEmail(sec.InvalidPlan):
    """Connector cannot send the complete sealed payload; nothing was sent."""


def migrate(conn):
    """Additive migration, caller transaction respected, no network or jobs."""
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reset_rows BIGINT NOT NULL)''')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return
        conn.execute(SCHEMA)
        for table in ('secretary_email_proposals', 'secretary_email_dispatches'):
            conn.execute(f'''CREATE TRIGGER secretary_email_no_mutation BEFORE UPDATE OR DELETE
                ON {table} FOR EACH ROW EXECUTE FUNCTION secretary_email_immutable()''')
            conn.execute(f'''CREATE TRIGGER secretary_email_no_truncate BEFORE TRUNCATE
                ON {table} FOR EACH STATEMENT EXECUTE FUNCTION secretary_email_immutable()''')
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))


def _address(value):
    # Deliberately narrower than RFC 5322: no lists, display names, quoted local
    # parts, Unicode/header injection, comments, groups, whitespace or literals.
    if type(value) is not str or len(value) > 254 or not value.isascii():
        raise sec.InvalidPlan('One explicit bare email address required')
    if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
                        r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                        r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value):
        raise sec.InvalidPlan('One explicit bare email address required')
    if len(value.split('@', 1)[0]) > 64:
        raise sec.InvalidPlan('Email local part too long')
    return value


def _payload(payload):
    sec._approval_payload(payload)
    if payload['channel'] != 'email':
        raise sec.InvalidPlan('Email only')
    _address(payload['recipient'])
    return sec.payload_hash(payload)


def _account(address):
    _address(address)
    # Address is the stable identity, NEVER an array index or display name.
    matches = [dict(item) for item in config.EMAIL_ACCOUNTS
               if str(item.get('address', '')).casefold() == address.casefold()]
    if len(matches) != 1 or matches[0]['address'] != address:
        raise sec.Conflict('Explicit unique configured account required')
    account = matches[0]
    if (not account.get('smtp_host') or type(account.get('smtp_port')) is not int
            or not 1 <= account['smtp_port'] <= 65535 or account.get('auth') not in ('basic', 'oauth2')):
        raise sec.InvalidPlan('Configured SMTP account required')
    # Changing the delivery endpoint/auth identity requires a NEW proposal.
    fingerprint = sec.payload_hash({key: account.get(key) for key in (
        'address', 'smtp_host', 'smtp_port', 'auth', 'client_id', 'provider')})
    return account, fingerprint


def _documents(conn, practice_id, documents):
    """Read native attachment occurrences only; no URL/path supplied by clients."""
    total = 0
    for document in documents:
        row = conn.execute('''SELECT a.path,octet_length(a.data),
            CASE WHEN octet_length(a.data)<=%s THEN a.data END
            FROM attachments a WHERE a.id=%s AND EXISTS (
                SELECT 1 FROM secretary_links l WHERE l.practice_id=%s AND l.document_id=a.id)
            FOR SHARE OF a''', (MAX_DOCUMENT_BYTES, document['id'], practice_id)).fetchone()
        if not row:
            raise sec.Conflict('Document missing or not linked to practice')
        path, size, data = row
        if size is not None and (size > MAX_DOCUMENT_BYTES or size > MAX_TOTAL_BYTES - total):
            raise sec.InvalidPlan('Document byte budget exceeded')
        try:
            if size is None:
                # The catalogue's descriptor-relative guard rejects symlinks,
                # hardlinks, secret paths, devices and traversal outside roots.
                with knowledge._file(path) as stream:
                    data = stream.read(min(MAX_DOCUMENT_BYTES, MAX_TOTAL_BYTES - total) + 1)
            else:
                data = bytes(data)
        except (OSError, TypeError, knowledge.SourceError):
            raise sec.Conflict('Document unavailable or unsafe') from None
        total += len(data)
        if len(data) > MAX_DOCUMENT_BYTES or total > MAX_TOTAL_BYTES:
            raise sec.InvalidPlan('Document byte budget exceeded')
        if hashlib.sha256(data).hexdigest() != document['sha256']:
            raise sec.Conflict('Document bytes changed')


def _native(conn, practice, responsibility=None):
    sec._trusted_message(conn, practice['origin_message'])
    if responsibility is not None:
        sec._trusted_message(conn, responsibility['origin_message'])


def _unexpired(approval):
    if sec._time(approval['expires_at']) <= datetime.now(timezone.utc):
        raise sec.Conflict('Email approval expired')


def request_email(conn, *, practice_id, expected_version, payload,
                  account_address, expires_at, operation_key):
    """Create inert approval + immutable sender binding; NEVER enqueue/send/AI.

    No account fallback, including when exactly one account is configured. The
    payload is copied through finite JSON, so caller mutations cannot change it.
    """
    digest = _payload(payload)
    payload = json.loads(sec._dump(payload))
    sec._positive(practice_id)
    sec._positive(expected_version, 'expected_version')
    _, account_hash = _account(account_address)
    request_hash = sec.payload_hash(dict(practice_id=practice_id, expected_version=expected_version,
        payload=payload, account_address=account_address, account_hash=account_hash,
        subject=SUBJECT, expires_at=sec._time(expires_at).isoformat()))
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (sec.WRITE_LOCK,))
        practice = sec._fetch(conn, sec.TABLES['practice'], practice_id, lock=True)
        responsibility = (sec._check_responsibility(conn, practice['responsibility_id'])
                          if practice['responsibility_id'] is not None else None)
        _native(conn, practice, responsibility)
        _documents(conn, practice_id, payload['documents'])
        identity = sec.request_approval(conn, practice_id=practice_id, expected_version=expected_version,
            payload=payload, expires_at=expires_at, operation_key=operation_key)
        row = conn.execute('SELECT request_hash FROM secretary_email_proposals WHERE approval_id=%s',
                           (identity,)).fetchone()
        if row:
            if row[0] != request_hash:
                raise sec.Conflict('Proposal sender or configuration changed')
        else:
            conn.execute('''INSERT INTO secretary_email_proposals
                (approval_id,account_address,account_hash,request_hash,payload_hash,subject)
                VALUES (%s,%s,%s,%s,%s,%s)''',
                (identity, account_address, account_hash, request_hash, digest, SUBJECT))
        return email_snapshot(conn, identity)


def _proposal(conn, identity):
    row = conn.execute('''SELECT account_address,account_hash,request_hash,payload_hash,subject
        FROM secretary_email_proposals WHERE approval_id=%s FOR SHARE''', (identity,)).fetchone()
    if not row:
        raise sec.Conflict('No sealed email proposal')
    return dict(zip(('account_address', 'account_hash', 'request_hash', 'payload_hash', 'subject'), row))


def email_snapshot(conn, approval_id):
    """Trusted authenticated dashboard read API; sender MUST be displayed."""
    sec._positive(approval_id)
    proposal = _proposal(conn, approval_id)
    approval = sec._fetch(conn, 'secretary_approvals', approval_id)
    return dict(approval_id=approval_id, expected_version=approval['version'],
        account_address=proposal['account_address'], subject=proposal['subject'],
        payload=approval['payload'], payload_hash=proposal['payload_hash'], state=approval['state'],
        expires_at=approval['expires_at'], documents_supported=False)


def _validate(conn, approval_id, version, address, payload):
    digest = _payload(payload)
    approval = sec._fetch(conn, 'secretary_approvals', approval_id, lock=True)
    proposal = _proposal(conn, approval_id)
    if (approval['state'] != 'approved' or approval['version'] != version
            or sec._time(approval['expires_at']) <= datetime.now(timezone.utc)):
        raise sec.Conflict('Approval expired, revoked, stale or not approved')
    if (approval['payload'] != payload or approval['payload_hash'] != digest
            or proposal['payload_hash'] != digest or proposal['subject'] != SUBJECT
            or proposal['account_address'] != address):
        raise sec.Conflict('Sealed email changed')
    account, fingerprint = _account(address)
    if fingerprint != proposal['account_hash']:
        raise sec.Conflict('SMTP account configuration changed')
    practice = sec._fetch(conn, sec.TABLES['practice'], approval['practice_id'], lock=True)
    if (practice['version'] != approval['practice_version'] or practice['state'] not in ('active', 'waiting')
            or practice['responsibility_id'] != approval['responsibility_id']):
        raise sec.Conflict('Practice changed')
    responsibility = None
    if approval['responsibility_id'] is not None:
        responsibility = sec._check_responsibility(conn, approval['responsibility_id'])
        if responsibility['version'] != approval['responsibility_version']:
            raise sec.Conflict('Responsibility changed')
    _native(conn, practice, responsibility)
    _documents(conn, practice['id'], payload['documents'])
    _unexpired(approval)  # File/row locks may have consumed the remaining TTL.
    return account


def _approval_receipt(conn, identity, version, payload, owner_id, key):
    sec._operation_key(key)
    request = dict(operation='approve', approval_id=identity, payload=payload,
                   expected_version=version - 1, owner_id=owner_id)
    receipt = sec._receipt(conn, key, request)
    if not receipt or receipt[1] != dict(id=identity, version=version, state='approved'):
        raise sec.Unauthorized('Concrete dashboard approval receipt required')


def dispatch_email(conn, *, approval_id, expected_version, account_address, payload,
                   approval_operation_key, operation_key, caller):
    """Second explicit owner dashboard action. Atomic grant + outbox, no SMTP.

    Caller must have displayed email_snapshot sender/subject/payload. Never expose
    this API as an approval side effect or a generic model tool. Returns IDs only.
    """
    sec._caller(caller)
    sec._positive(approval_id)
    sec._positive(expected_version, 'expected_version')
    sec._string(operation_key, 180)
    if not operation_key.startswith('secretary-email-dispatch:'):
        raise sec.InvalidPlan('Explicit dispatch operation key required')
    _payload(payload)
    payload = json.loads(sec._dump(payload))
    request = dict(approval_id=approval_id, expected_version=expected_version,
        account_address=account_address, payload=payload, subject=SUBJECT,
        approval_operation_key=approval_operation_key, owner_id=caller.owner_id)
    digest = sec.payload_hash(request)
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (sec.WRITE_LOCK,))
        previous = conn.execute('''SELECT operation_key,request_hash FROM secretary_email_dispatches
            WHERE approval_id=%s OR operation_key=%s FOR SHARE''', (approval_id, operation_key)).fetchall()
        if previous:
            if previous != [(operation_key, digest)]:
                raise sec.Conflict('Dispatch already bound to another request')
            return dict(approval_id=approval_id, dispatch_key=operation_key)
        _validate(conn, approval_id, expected_version, account_address, payload)
        _approval_receipt(conn, approval_id, expected_version, payload, caller.owner_id, approval_operation_key)
        if payload['documents']:
            raise UnsupportedEmail('Current email connector cannot send approved attachments')
        conn.execute('''INSERT INTO secretary_email_dispatches
            (approval_id,operation_key,request_hash,approval_version,approval_operation_key,owner_id)
            VALUES (%s,%s,%s,%s,%s,%s)''',
            (approval_id, operation_key, digest, expected_version, approval_operation_key, caller.owner_id))
        work = dict(approval_id=approval_id, dispatch_key=operation_key)
        jobs.enqueue(conn, KIND, 'secretary-email:' + str(approval_id), work)
        sec._record(conn, operation_key, request,
                    f'Email #{approval_id}: invio esplicitamente richiesto, non ancora accettato.', work)
        return work


def _result(identity, state):
    return dict(approval_id=identity, state=state, delivered=False, automatic_retry=False)


def _finish(identity, context, state):
    # Accounting is tied to the committed attempt, not a new execution authority.
    # If this write fails, 'attempted' remains durable and forbids another send.
    with db._conn() as conn, conn.transaction():
        row = conn.execute('''UPDATE secretary_email_attempts SET state=%s,finished_at=now()
            WHERE approval_id=%s AND job_id=%s AND job_token=%s AND state='attempted'
            RETURNING state''', (state, identity, context.job_id, context.token)).fetchone()
        if row != (state,):
            raise RuntimeError('Email attempt result was not persisted')


def execute_approved(payload):
    """jobs.Worker handler; requires durable dashboard grant, never direct calls."""
    sec._exact(payload, ('approval_id', 'dispatch_key'))
    identity = sec._positive(payload['approval_id'])
    sec._string(payload['dispatch_key'], 180)
    context = jobs.execution_context()
    if context is None or context.kind != KIND or context.payload != payload:
        raise sec.Unauthorized('Current email execution Worker required')
    with db._conn() as conn, conn.transaction():
        jobs.assert_current(conn)
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (sec.WRITE_LOCK,))
        grant = conn.execute('''SELECT operation_key,approval_version,approval_operation_key,owner_id,request_hash
            FROM secretary_email_dispatches WHERE approval_id=%s FOR SHARE''', (identity,)).fetchone()
        if not grant or grant[0] != payload['dispatch_key']:
            raise sec.Unauthorized('Explicit dashboard dispatch grant required')
        previous = conn.execute('SELECT state FROM secretary_email_attempts WHERE approval_id=%s',
                                (identity,)).fetchone()
        if previous:
            return _result(identity, 'uncertain' if previous[0] == 'attempted' else previous[0])
        # Read immutable server proposal and approved data, never job-supplied
        # recipient, account index, prompt or document path.
        proposal = _proposal(conn, identity)
        approval = sec._fetch(conn, 'secretary_approvals', identity, lock=True)
        approved = approval['payload']
        sec._caller(sec.DashboardOwner(grant[3], True, True))
        account = _validate(conn, identity, grant[1], proposal['account_address'], approved)
        _approval_receipt(conn, identity, grant[1], approved, grant[3], grant[2])
        request = dict(approval_id=identity, expected_version=grant[1], account_address=proposal['account_address'],
                       payload=approved, subject=SUBJECT, approval_operation_key=grant[2], owner_id=grant[3])
        if grant[4] != sec.payload_hash(request):
            raise sec.Conflict('Dispatch seal changed')
        if approved['documents']:
            raise UnsupportedEmail('Current email connector cannot send approved attachments')
        jobs.assert_current(conn)
        conn.execute('''INSERT INTO secretary_email_attempts(approval_id,job_id,job_token,state)
            VALUES (%s,%s,%s,'attempted')''', (identity, context.job_id, context.token))
    # This function owns its fresh autocommit connection: attempt is committed
    # before any remote call. No request is ever sent within caller savepoints.
    try:
        from connectors import email
        # Reservation is not continuing authority. Re-read under the same fence
        # as revocation, comparing CURRENT approval data to the sealed payload.
        # Release all locks/transactions before SMTP; denial still consumes the
        # committed attempt, so neither worker retries nor replays can resend.
        with db._conn() as conn, conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (sec.WRITE_LOCK,))
            approval = sec._fetch(conn, 'secretary_approvals', identity, lock=True)
            account = _validate(conn, identity, grant[1], proposal['account_address'], approved)
            jobs.assert_current(conn)
        # Recheck lease and the fresh expiry after any validation/commit delay.
        jobs.assert_current()
        _unexpired(approval)
        outcome = email.send(account, approved['recipient'], SUBJECT, approved['text'])
        state = 'accepted' if outcome is True else 'uncertain'
    except Exception:
        _finish(identity, context, 'uncertain')
        raise  # Worker persists sanitized class only; its retry cannot send again.
    _finish(identity, context, state)
    return _result(identity, state)