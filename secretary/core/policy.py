"""Deterministic F1 boundaries. Native provenance, never model authority flags.

Call migrate from startup, can_operate before preparation AND execution. AI data
egress defaults to denied until an explicit data_policies row permits its channel
and effect. can_read_context is ONLY for read-only source egress: historical
sources additionally require ai_history consent, never operational replay.
Calendar grants bind native CURRENT owner text to one event/state/destination/time.
System notifications are sealed by digest's scheduler, never by model output.

Owner adapter integration (same transaction as its local response):
* /conferma ID: grant_calendar(conn, ID, calendar_event_hash(conn, ID), mid).
* /annulla ID: use operation='cancel' on BOTH calls above.
* Natural create: save_event(..., f'owner:{mid}:event:{index}', ..., approved=True).
    save_event first inserts a proposal; only a successful grant queues Calendar.
* grant_calendar queues the action itself and returns a token or None. Do not
    separately update sync_state or enqueue an unsealed action. General
    can_operate(..., 'calendar_write') deliberately remains False. DashboardOwner
    and AI plans are NOT native owner messages and cannot mint these grants.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from secretary.core import config

VERSION = 'secretary_effect_grants_v3'
NATIVE_TABLES = ('telegram_messages', 'whatsapp_messages', 'email_messages')
SCHEMA = """
CREATE TABLE IF NOT EXISTS data_policies (
 channel TEXT NOT NULL, effect TEXT NOT NULL, allowed BOOLEAN NOT NULL DEFAULT FALSE,
 version BIGINT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(channel,effect));
CREATE TABLE IF NOT EXISTS effect_audit (
 id BIGSERIAL PRIMARY KEY, message_id BIGINT, effect TEXT NOT NULL,
 allowed BOOLEAN NOT NULL, reason TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS effect_audit_message ON effect_audit(message_id,id);
CREATE TABLE IF NOT EXISTS effect_receipts (
 key TEXT PRIMARY KEY, message_id BIGINT NOT NULL, effect TEXT NOT NULL,
 payload_hash TEXT NOT NULL, owner_required BOOLEAN NOT NULL DEFAULT FALSE,
 status TEXT NOT NULL CHECK(status IN
 ('authorized','attempted','accepted','verified','failed','uncertain','blocked')),
 job_id BIGINT, token TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE effect_receipts ALTER COLUMN message_id DROP NOT NULL;
ALTER TABLE effect_receipts ADD COLUMN IF NOT EXISTS "authorization" TEXT;
CREATE TABLE IF NOT EXISTS calendar_effect_grants (
 token TEXT PRIMARY KEY, appointment_id BIGINT NOT NULL,
 owner_message_id BIGINT NOT NULL, authority_hash TEXT NOT NULL,
 payload_hash TEXT NOT NULL, sync_key TEXT, expected_state TEXT NOT NULL,
 expected_done BOOLEAN NOT NULL, revoked BOOLEAN NOT NULL DEFAULT FALSE,
 expires_at TIMESTAMPTZ NOT NULL DEFAULT (clock_timestamp()+interval '30 minutes'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(appointment_id,owner_message_id,payload_hash));
ALTER TABLE calendar_effect_grants ADD COLUMN IF NOT EXISTS connector_target JSONB;
CREATE TABLE IF NOT EXISTS system_notifications (
 token TEXT PRIMARY KEY, system TEXT NOT NULL CHECK(system='digest'),
 scope DATE NOT NULL, recipient TEXT NOT NULL, effect_key TEXT NOT NULL UNIQUE,
 job_id BIGINT NOT NULL UNIQUE REFERENCES work_jobs(id),
 payload JSONB NOT NULL, payload_hash TEXT NOT NULL,
 source_ids BIGINT[] NOT NULL, media_source_ids BIGINT[] NOT NULL,
 revoked BOOLEAN NOT NULL DEFAULT FALSE,
 expires_at TIMESTAMPTZ NOT NULL DEFAULT (clock_timestamp()+interval '36 hours'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(system,scope,recipient));
"""


def migrate(conn):
    """Additive, versioned; never reclassifies native messages or consent.

    Requires work_jobs(id)'s primary/unique key from jobs.migrate, not the rest
    of the secretary schema. Missing prerequisites roll back the whole migration;
    never omit the notification foreign key to accommodate a partial install.
    v3 also runs on installed v2 schemas. Legacy grants retain a NULL target:
    never backfill today's destination into yesterday's owner authorization.
    """
    with conn.transaction():
        # Share the native/bootstrap lock, including concurrent first installs.
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reset_rows BIGINT NOT NULL)''')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return
        conn.execute(SCHEMA)
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))


def metadata_for(conn, message_id):
    """Return flattened metadata plus authoritative native columns; {} if missing.

    Shared locks fence concurrent provenance edits in the caller's transaction.
    The communications view intentionally has no metadata and is not used here.
    """
    if type(message_id) is not int or not 0 < message_id <= 9223372036854775807:
        return {}
    fields = ('channel', 'metadata', 'ts', 'acquired_at', 'source_id', 'account',
              'chat_id', 'author', 'direction', 'message_id')
    for table in NATIVE_TABLES:
        row = conn.execute(f"SELECT {','.join(fields)} FROM {table} WHERE id=%s FOR SHARE",
                           (message_id,)).fetchone()
        if row is None:
            continue
        native = dict(zip(fields, row))
        metadata = native.get('metadata')
        if not isinstance(metadata, dict):
            return {}
        result = dict(metadata)
        result.update(native)
        result['id'] = message_id
        return result
    return {}


def _owner(meta):
    owner = str(config.TELEGRAM_CHAT_ID or '').strip()
    bot = str(config.TELEGRAM_BOT_TOKEN or '').split(':', 1)[0]
    native = meta.get('metadata')
    if (not owner.isdecimal() or int(owner) <= 0 or not bot.isdecimal() or int(bot) <= 0
            or type(native) is not dict or meta.get('channel') != 'telegram'
            or meta.get('account') != 'bot:' + bot or str(meta.get('chat_id')) != owner
            or str(meta.get('author')) != owner or meta.get('direction') != 'in'
            or native.get('chat_type') != 'private'
            or native.get('historical', False) is not False
            or native.get('content_origin') != 'direct_text'
            or native.get('is_forwarded') is not False):
        return False
    if any(native.get(key) is not None and native.get(key) is not False for key in (
            'forward_origin', 'forward_date', 'forward_from', 'forward_from_chat',
            'forward_sender_name', 'forwarded', 'quote', 'external_reply',
            'via_bot', 'sender_chat', 'is_automatic_forward')):
        return False
    for field, expected in (('account', 'bot:' + bot), ('chat_id', owner),
                            ('author', owner), ('direction', 'in')):
        if str(native.get(field)) != expected:
            return False
    try:
        source = json.loads(meta.get('source_id'))
    except (TypeError, ValueError, RecursionError):
        return False
    return bool(type(source) is list and len(source) == 4
                and source[:3] == ['bot', bot, owner]
                and type(source[3]) is int and 0 < source[3] <= 9223372036854775807
                and type(native.get('message_id')) is int and native['message_id'] == source[3]
                and str(meta.get('message_id')) == str(source[3]))


ALIASES = {'analysis': 'ai_text', 'ai': 'ai_text', 'media': 'ai_media',
           'task_reconcile': 'reconcile', 'owner': 'owner_command',
           'task_complete': 'task_apply', 'task_update': 'task_apply',
           'notification': 'notify', 'calendar': 'calendar_write',
           'zip': 'zip_send', 'invoice': 'extract'}
LOCAL_EFFECTS = frozenset(('archive', 'extract', 'reconcile', 'task_propose', 'notify', 'zip_send'))
OWNER_EFFECTS = frozenset(('owner_command', 'owner_dialogue', 'task_apply',
                           'task_create', 'reminder_create', 'memory_write', 'email_send'))
DATA_EFFECTS = frozenset(('ai_text', 'ai_media', 'ai_history'))


def can_read_context(conn, message_id, effect='ai_text', *, owner_required=False):
    """Read-only AI source egress, never authority to prepare/execute an effect.

    Resolve provenance natively, not from catalogue/model flags. Text consent is
    always required; exact native historical=True also needs separate channel
    ai_history consent, and media additionally needs ai_media. Missing rows and
    malformed historical flags deny. ai_history alone is not a readable effect.
    owner_required only restricts; history can never be native owner authority.
    Call again before inference, in the caller's transaction (FOR SHARE locks).
    Only safe effect/reason codes enter the audit, never source text or flags.
    """
    effect = (ALIASES.get(effect, effect)
              if isinstance(effect, str) and 0 < len(effect) <= 80 else None)
    allowed, reason = False, 'context_effect_not_delegated'
    if effect in ('ai_text', 'ai_media'):
        meta = metadata_for(conn, message_id)
        if not meta:
            reason = 'context_missing_message'
        elif owner_required and not _owner(meta):
            reason = 'context_owner_required'
        else:
            historical = meta['metadata'].get('historical', False)
            channel = meta.get('channel')
            channel = channel.split(' ', 1)[0] if isinstance(channel, str) else None
            if historical is not False and historical is not True:
                reason = 'context_historical_invalid'
            elif channel not in ('email', 'telegram', 'whatsapp'):
                reason = 'context_channel_invalid'
            else:
                required = ('ai_text',) + (('ai_history',) if historical else ())
                if effect == 'ai_media':
                    required += ('ai_media',)
                allowed, reason = True, 'context_allowed'
                for consent in required:
                    row = conn.execute('SELECT allowed FROM data_policies WHERE channel=%s AND effect=%s FOR SHARE',
                                       (channel, consent)).fetchone()
                    if not row or row[0] is not True:
                        allowed = False
                        reason = ('context_history_denied' if consent == 'ai_history'
                                  else 'context_data_denied')
                        break
    audit_id = (message_id if type(message_id) is int
                and 0 < message_id <= 9223372036854775807 else None)
    safe_effect = effect if effect in LOCAL_EFFECTS | OWNER_EFFECTS | DATA_EFFECTS else 'unsupported'
    conn.execute('INSERT INTO effect_audit(message_id,effect,allowed,reason) VALUES (%s,%s,%s,%s)',
                 (audit_id, safe_effect, allowed, reason))
    return allowed


def can_operate(conn, message_id, effect, owner_required=False):
    """False on missing/history/unknown effect; external evidence is never command.

    owner_required can only restrict: neither it nor a payload/model flag can
    grant authority. Own outgoing messages remain evidence/proposals for now.
    ai_history is a read-context consent modifier, never an operational effect.
    Audits contain safe reason codes, no message text, prompts or secrets.
    """
    if not isinstance(effect, str) or not effect or len(effect) > 80:
        return False
    effect = ALIASES.get(effect, effect)
    meta = metadata_for(conn, message_id)
    allowed, reason = False, 'missing_message'
    if meta:
        if meta.get('metadata', {}).get('historical', False) is not False:
            reason = 'historical'
        elif (owner_required or effect in OWNER_EFFECTS) and not _owner(meta):
            reason = 'owner_required'
        elif effect in DATA_EFFECTS and effect != 'ai_history':
            channel = meta['channel'].split(' ', 1)[0]
            row = conn.execute('SELECT allowed FROM data_policies WHERE channel=%s AND effect=%s FOR SHARE',
                               (channel, effect)).fetchone()
            allowed = bool(row and row[0] is True)
            reason = 'data_allowed' if allowed else 'data_denied'
        elif effect in LOCAL_EFFECTS or effect in OWNER_EFFECTS:
            allowed, reason = True, 'allowed'
        else:
            reason = 'effect_not_delegated'
    audit_id = message_id if type(message_id) is int and message_id > 0 else None
    # Unknown effect strings may themselves contain private model output.
    safe_effect = effect if effect in LOCAL_EFFECTS | OWNER_EFFECTS | DATA_EFFECTS else 'unsupported'
    conn.execute('INSERT INTO effect_audit(message_id,effect,allowed,reason) VALUES (%s,%s,%s,%s)',
                 (audit_id, safe_effect, allowed, reason))
    return allowed


def payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _appointment(conn, appointment_id):
    if type(appointment_id) is not int or appointment_id <= 0:
        return None
    return conn.execute('''SELECT event_id,event_data,sync_state,sync_key,done
        FROM appointments WHERE id=%s FOR UPDATE''', (appointment_id,)).fetchone()


def _calendar_target(event, operation):
    """Local connector configuration only: no readiness, credentials or API calls.

    Include resolved create times and defaults, not just a mutable wall clock.
    Mirror create_event's minute normalization before its final ZoneInfo attach;
    neither a caller/model destination nor a cached config constant is trusted.
    Cancellation still binds the configured timezone as well as the destination.
    """
    from secretary.connectors import gcal
    try:
        calendar_id = gcal.calendar_id()
        timezone = config.TIMEZONE
        if (not isinstance(calendar_id, str) or not calendar_id.strip()
                or calendar_id != calendar_id.strip() or calendar_id.lower() == 'primary'
                or any(c in calendar_id for c in ('\x00', '\r', '\n'))
                or not isinstance(timezone, str) or not timezone):
            raise ValueError
        tz = ZoneInfo(timezone)
        target = dict(calendar_id=calendar_id, timezone=timezone)
        if operation == 'create':
            # Preserve the connector's local validation (including DST, past
            # dates and text types) before sealing its normalized wire times.
            validated = gcal.validate_event(event)
            local = datetime.strptime(validated['dt'], '%Y-%m-%d %H:%M').replace(tzinfo=tz)
            for key in ('duration_min', 'reminder_minutes'):
                target[key] = validated[key]
            target.update(start=local.isoformat(),
                          end=(local + timedelta(minutes=target['duration_min'])).isoformat())
        return target
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
        raise ValueError('Invalid calendar target') from None


def calendar_payload(row, operation='create'):
    """Exact connector arguments, trusted destination/time and target state."""
    if operation not in ('create', 'cancel', 'update') or not row or not isinstance(row[1], dict):
        raise ValueError('Invalid calendar snapshot')
    if not isinstance(row[0], str) or not re.fullmatch(r'[0-9a-f]{64}', row[0]):
        raise ValueError('Deterministic Calendar ID required')
    return dict(event_id=row[0], event=row[1],
                state='cancelled' if operation == 'cancel' else 'synced',
                target=_calendar_target(row[1], 'create' if operation == 'update' else operation))


def calendar_event_hash(conn, appointment_id, *, operation='create'):
    """Read under lock; caller must use the same transaction for approval."""
    return payload_hash(calendar_payload(_appointment(conn, appointment_id), operation))


def _owner_text(conn, mid):
    if not can_operate(conn, mid, 'owner_command', owner_required=True):
        return None
    row = conn.execute('''SELECT body,metadata,
        EXISTS(SELECT 1 FROM attachments WHERE message_id=%s)
        FROM telegram_messages WHERE id=%s FOR SHARE''', (mid, mid)).fetchone()
    if (not row or not isinstance(row[0], str) or not isinstance(row[1], dict)
            or row[1].get('content_origin') != 'direct_text' or row[2]
            or not row[0].strip() or len(row[0]) > 16000 or '\x00' in row[0]):
        return None
    return row[0]


def grant_calendar(conn, appointment_id, expected_event_hash, owner_message_id, *, operation='create',
                      calendar_intent=None):
    """Seal one current native intent; no caller/model boolean grants authority.

    Replaying the same command returns its original token, NEVER renews expiry.
    Cancellation is a separate /annulla ID grant, including task derivatives.
    Denial returns None without promoting the local proposal or queuing I/O.
    calendar_intent: for natural-language approvals the caller passes the
    intent flag classified at PREPARATION time (persisted in the dialogue
    plan, under AI consent). This function NEVER calls a model: no inference
    inside write transactions or replays. None means explicit /conferma or
    /annulla, which needs no classification.
    """
    from secretary.core import jobs
    with conn.transaction():
        jobs.assert_current(conn)
        context = jobs.execution_context()
        if context and (context.kind != 'owner' or context.message_id != owner_message_id):
            return None
        row = _appointment(conn, appointment_id)
        if not row or operation not in ('create', 'cancel', 'update'):
            return None
        try:
            payload = calendar_payload(row, operation)
            digest = payload_hash(payload)
        except ValueError:
            return None
        if digest != expected_event_hash:
            return None
        text = _owner_text(conn, owner_message_id)
        if text is None:
            return None
        command = '/annulla' if operation == 'cancel' else '/conferma'
        explicit = bool(re.fullmatch(re.escape(command) + r'\s+' + str(appointment_id), text.strip(), re.I))
        if not explicit:
            # Natural intent cannot be borrowed to approve an unrelated proposal.
            # The flag comes from the persisted plan (classified in prepare),
            # never from a model call inside this transaction.
            if (operation not in ('create', 'update') or not re.fullmatch(
                    rf'owner:{owner_message_id}:event:\d+', row[3] or '')
                    or calendar_intent is not True):
                return None
        expected_state = 'cancel_pending' if operation == 'cancel' else 'pending'
        states = ('proposal', 'local', 'pending')
        if operation == 'cancel':
            states += ('synced', 'cancel_pending')
        elif operation == 'update':
            states += ('synced',)  # L'update richiede un evento già sincronizzato.
        if row[2] not in states or (operation == 'create' and row[4]):
            return None
        old = conn.execute('''SELECT token,NOT revoked AND expires_at>clock_timestamp(),
            payload_hash,connector_target
            FROM calendar_effect_grants WHERE appointment_id=%s AND owner_message_id=%s
            AND expected_state=%s FOR UPDATE''', (appointment_id, owner_message_id, expected_state)).fetchone()
        if old:
            # A replay cannot rebind an old command to a changed destination,
            # upgrade an unbound legacy grant, or renew an expired approval.
            return (old[0] if old[1] and row[2] == expected_state
                    and old[2] == digest and old[3] == payload['target'] else None)
        token = 'calendar:' + uuid.uuid4().hex
        conn.execute('UPDATE calendar_effect_grants SET revoked=TRUE WHERE appointment_id=%s', (appointment_id,))
        conn.execute('''INSERT INTO calendar_effect_grants
            (token,appointment_id,owner_message_id,authority_hash,payload_hash,sync_key,expected_state,expected_done,
             connector_target)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)''',
            (token, appointment_id, owner_message_id, payload_hash(text), digest, row[3],
             expected_state, operation == 'cancel', json.dumps(payload['target'])))
        conn.execute('UPDATE appointments SET sync_state=%s,done=%s WHERE id=%s',
                     (expected_state, operation == 'cancel', appointment_id))
        jobs.enqueue(conn, 'action', token, dict(type='calendar', id=appointment_id,
            authority_message_id=owner_message_id, authorization=token))
        jobs.assert_current(conn)
        return token


def authorize_calendar(conn, appointment_id, authority_message_id, caller=None, *,
                       expected_event_hash=None, operation='create'):
    """Native command adapter only; dashboard identities need their own grant API."""
    if caller is not None or expected_event_hash is None:
        return None
    return grant_calendar(conn, appointment_id, expected_event_hash, authority_message_id, operation=operation)


def _calendar_allowed(conn, mid, digest, token):
    # Grant writers lock appointment -> grant. Readers must use the same order
    # rather than holding a grant SHARE lock while waiting for its appointment.
    target = conn.execute('SELECT appointment_id FROM calendar_effect_grants WHERE token=%s',
                          (token,)).fetchone()
    if not target:
        return False
    row = _appointment(conn, target[0])
    grant = conn.execute('''SELECT appointment_id,owner_message_id,authority_hash,payload_hash,
        sync_key,expected_state,expected_done,connector_target FROM calendar_effect_grants
        WHERE token=%s AND NOT revoked AND expires_at>clock_timestamp() FOR SHARE''', (token,)).fetchone()
    if not grant or grant[0] != target[0] or grant[1] != mid or grant[3] != digest:
        return False
    if not row or (row[3], row[2], row[4]) != (grant[4], grant[5], grant[6]):
        return False
    # expected_state distingue le operazioni sigillate: cancel_pending=annulla,
    # pending su un evento già synced=update, pending altrimenti=create.
    if grant[5] == 'cancel_pending':
        operation = 'cancel'
    elif grant[5] == 'pending' and row[2] == 'synced':
        operation = 'update'
    else:
        operation = 'create'
    try:
        payload = calendar_payload(row, operation)
        if grant[7] != payload['target'] or payload_hash(payload) != digest:
            return False
    except ValueError:
        return False
    text = _owner_text(conn, mid)
    return text is not None and payload_hash(text) == grant[2]


def _system_allowed(conn, digest, token, *, key=None, scope=None):
    """Only a native, leased action matching a scheduler-sealed digest can send."""
    from secretary.core import jobs
    context = jobs.execution_context()
    if context is None or context.kind != 'action':
        return False
    row = conn.execute('''SELECT n.scope,n.recipient,n.effect_key,n.job_id,n.payload,
        n.payload_hash,n.source_ids,n.media_source_ids,j.kind,j.key,j.payload
        FROM system_notifications n JOIN work_jobs j ON j.id=n.job_id
        WHERE n.token=%s AND n.system='digest' AND NOT n.revoked
        AND n.expires_at>clock_timestamp() FOR SHARE OF n,j''', (token,)).fetchone()
    if not row:
        return False
    day, recipient, effect_key, job_id, payload, saved_hash, sources, media, kind, job_key, job_payload = row
    owner = str(config.TELEGRAM_CHAT_ID or '').strip()
    expected_job = dict(type='notify', text=payload.get('text'), html=False, flow='digest',
                        digest_day=str(day), system='digest', system_authorization=token)
    if (not owner.isdecimal() or int(owner) <= 0 or recipient != owner
            or payload != dict(text=payload.get('text'), html=False, recipient=owner)
            or payload_hash(payload) != digest or saved_hash != digest
            or kind != 'action' or job_key != effect_key or job_id != context.job_id
            or job_payload != expected_job or context.payload != job_payload
            or (key is not None and key != effect_key)
            or (scope is not None and scope != str(day))):
        return False
    flow = conn.execute("SELECT enabled FROM flow_settings WHERE flow='digest' FOR SHARE").fetchone()
    if not flow or flow[0] is not True or not set(media).issubset(sources):
        return False
    # can_read_context (non can_operate): le fonti possono essere storiche, ma solo con ai_history.
    return all(can_read_context(conn, mid, 'ai_text')
               and (mid not in media or can_read_context(conn, mid, 'ai_media'))
               for mid in sources)


def effect_allowed(conn, message_id, effect, payload, *, authorization=None, owner_required=False):
    """Recheck the same sealed effect at preparation, attempt and actual I/O."""
    if type(effect) is not str or not effect or len(effect) > 80:
        return False
    return _authorized(conn, message_id, ALIASES.get(effect, effect), payload_hash(payload),
                       authorization, owner_required)


def _authorized(conn, mid, effect, digest, authorization, owner_required=False):
    if authorization is None:
        return can_operate(conn, mid, effect, owner_required=owner_required)
    if not isinstance(authorization, str):
        return False
    if effect == 'calendar_write' and authorization.startswith('calendar:'):
        if type(mid) is not int or not 0 < mid <= 9223372036854775807:
            return False
        return _calendar_allowed(conn, mid, digest, authorization)
    if effect == 'notify' and mid is None and not owner_required and authorization.startswith('digest:'):
        return _system_allowed(conn, digest, authorization)
    return False


def prepare_system_effect(conn, key, effect, payload, *, system='digest', scope):
    """Not a global missing-message exemption; only digest's exact durable job."""
    from secretary.core import jobs
    jobs.assert_current(conn)
    context = jobs.execution_context()
    token = context.payload.get('system_authorization') if context else None
    if (system != 'digest' or type(effect) is not str or ALIASES.get(effect, effect) != 'notify'
            or not isinstance(token, str) or not token.startswith('digest:')
            or not _system_allowed(conn, payload_hash(payload), token, key=key, scope=scope)):
        return False
    return prepare_effect(conn, key, None, effect, payload, authorization=token)


def prepare_effect(conn, key, message_id, effect, payload, *, owner_required=False, authorization=None):
    """Reserve a concrete effect identity, without sending. Returns bool.

    Reusing a key with a different payload is an error, never a new approval.
    Attempted/uncertain receipts must be reconciled, not blindly retried.
    Caller keeps this in the same transaction as its local decision/outbox.
    """
    from secretary.core import jobs
    if (type(key) is not str or not key or key != key.strip() or len(key) > 200
            or any(char in key for char in ('\x00', '\r', '\n'))):
        raise ValueError('Invalid effect key')
    if type(effect) is not str or not effect or len(effect) > 80:
        return False
    effect = ALIASES.get(effect, effect)
    digest = payload_hash(payload)
    jobs.assert_current(conn)
    if not _authorized(conn, message_id, effect, digest, authorization, owner_required):
        return False
    context = jobs.execution_context()
    conn.execute('''INSERT INTO effect_receipts
        (key,message_id,effect,payload_hash,owner_required,status,job_id,token,"authorization")
        VALUES (%s,%s,%s,%s,%s,'authorized',%s,%s,%s) ON CONFLICT(key) DO NOTHING''',
        (key, message_id, effect, digest, bool(owner_required),
         context.job_id if context else None, context.token if context else None, authorization))
    row = conn.execute('''SELECT message_id,effect,payload_hash,owner_required,status,"authorization"
        FROM effect_receipts WHERE key=%s FOR UPDATE''', (key,)).fetchone()
    if row[:4] != (message_id, effect, digest, bool(owner_required)) or row[5] != authorization:
        raise ValueError('Effect identity conflict')
    return row[4] == 'authorized'


def record_receipt(conn, key, status):
    """Monotonic result states; accepted is not verified/read/exactly-once."""
    from secretary.core import jobs
    transitions = {'authorized': ('attempted', 'blocked'),
                   'attempted': ('accepted', 'failed', 'uncertain'),
                   'accepted': ('verified', 'uncertain')}
    with conn.transaction():
        jobs.assert_current(conn)
        row = conn.execute('''SELECT message_id,effect,owner_required,status,token,"authorization",payload_hash
            FROM effect_receipts WHERE key=%s FOR UPDATE''', (key,)).fetchone()
        if not row:
            return False
        context = jobs.execution_context()
        # Results belong to the attempt that started the remote operation.
        if row[3] != 'authorized' and row[4] != (context.token if context else None):
            return False
        if status == row[3]:
            # Recording a repeated result is harmless, but a repeated attempt
            # must never be interpreted as permission to send a second time.
            return status in ('accepted', 'verified', 'failed', 'uncertain', 'blocked')
        if type(status) is not str or status not in transitions.get(row[3], ()):
            return False
        if status == 'attempted' and not _authorized(conn, row[0], row[1], row[6], row[5], row[2]):
            status = 'blocked'
        conn.execute('''UPDATE effect_receipts SET status=%s,job_id=%s,token=%s,
            updated_at=now() WHERE key=%s''',
            (status, context.job_id if context else None, context.token if context else None, key))
        return status != 'blocked'