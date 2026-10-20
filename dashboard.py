"""LAN dashboard: bounded owner APIs; approval and explicit email dispatch differ."""
import hmac
import importlib
import json
import os
from pathlib import Path
import secrets
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from waitress import create_server

import config
import db
import flows

CREDENTIAL_FILE = Path.home() / '.config' / 'pi-secretary' / 'dashboard.json'

# Finite server contracts. No module, table, path or SQL arrives from the browser.
DATA_CHANNELS = ('email', 'telegram', 'whatsapp')
DATA_EFFECTS = ('ai_text', 'ai_media', 'ai_history')


class Unsupported(RuntimeError):
    pass


def _service(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        raise Unsupported('Optional service not installed') from exc


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError('Invalid request schema')
    return value


def _positive(value, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 9223372036854775807:
        raise ValueError('Invalid bounded integer')
    return value


def _owner():
    # Called ONLY inside guarded mutation routes. Never accept caller flags/ID.
    sec = _service('secretary')
    owner = str(config.TELEGRAM_CHAT_ID or '').strip()
    if not owner.isdecimal() or int(owner) <= 0:
        raise PermissionError('Configured owner required')
    return sec.DashboardOwner(owner_id=owner, authenticated=True, csrf_verified=True)


def secretary_view():
    """Project public service data; never export plans, prompts or raw config."""
    raw = _service('secretary').snapshot()
    fields = {
        'practices': ('id', 'objective', 'state', 'version', 'next_action', 'waiting_for',
                      'blocked_reason', 'next_review_at', 'completion_criteria', 'responsibility_id'),
        'responsibilities': ('id', 'type', 'objective', 'scope', 'triggers', 'allowed_actions',
                             'state', 'version', 'stop_condition'),
        'approvals': ('id', 'practice_id', 'practice_version', 'responsibility_id',
                      'responsibility_version', 'payload', 'payload_hash', 'expires_at',
                      'state', 'effective_state', 'version'),
        'receipts': ('operation_key', 'receipt', 'created_at'),
        'decisions': ('id', 'event', 'operation_key', 'created_at'),
        'links': ('id', 'practice_id', 'email_message_id', 'telegram_message_id',
                  'whatsapp_message_id', 'document_id', 'evidence'),
        'wakeups': ('practice_id', 'practice_version', 'reason', 'created_at'),
    }
    return dict(available=True, **{name: [{key: row[key] for key in keys if key in row}
                 for row in raw.get(name, [])[:24]] for name, keys in fields.items()},
                truncated={name: bool(raw.get('truncated', {}).get(name)) for name in fields},
                limits={'per_collection': 24}, external_execution=False)


def data_policy_list():
    """policy v1 has no management API. Missing rows are DENIED, never seeded."""
    _service('policy')
    with db._conn() as conn:
        rows = conn.execute('''SELECT channel,effect,allowed,version FROM data_policies
            WHERE channel IN (%s,%s,%s) AND effect IN (%s,%s,%s) LIMIT 9''',
            (*DATA_CHANNELS, *DATA_EFFECTS)).fetchall()
    saved = {(channel, effect): (allowed, version) for channel, effect, allowed, version in rows}
    return {'policies': [dict(channel=channel, effect=effect,
                 allowed=saved.get((channel, effect), (False, 0))[0] is True,
                 version=saved.get((channel, effect), (False, 0))[1])
             for channel in DATA_CHANNELS for effect in DATA_EFFECTS],
            'scope': 'channel/effect; telegram includes bot and personal account',
            'default_allowed': False}


def runtime_view():
    """Do not export prepared inputs, source bodies or internal model prompts."""
    raw = _service('secretary_runtime').snapshot(limit=20)
    proposal_fields = ('action', 'summary', 'evidence', 'question', 'draft', 'next_action', 'citations')
    reviews = []
    for row in raw.get('reviews', [])[:20]:
        item = {key: row[key] for key in ('operation_key', 'practice_id', 'practice_version',
                'status', 'created_at', 'updated_at') if key in row}
        plan = row.get('plan') or {}
        item['proposal'] = {key: plan[key] for key in proposal_fields if key in plan}
        reviews.append(item)
    fields = ('id', 'practice_id', 'practice_version', 'message_id', 'entry_id',
              'responsibility_id', 'responsibility_version', 'version',
              'revision', 'evidence', 'method', 'state', 'created_at')
    attention = []
    for row in raw.get('attention', [])[:20]:
        item = {key: row[key] for key in ('practice_id', 'practice_version', 'last_hash',
                'last_notified_at', 'acknowledged_at', 'review_operation_key') if key in row}
        item['proposal'] = {key: value for key, value in (row.get('proposal') or {}).items()
                            if key in proposal_fields}
        attention.append(item)
    return dict(reviews=reviews, link_proposals=[{key: row[key] for key in fields if key in row}
                for row in raw.get('link_proposals', [])[:20]], attention=attention, external_sends_supported=False,
                arbitrary_tools_supported=False, limit=20)


EMAIL_FIELDS = ('approval_id', 'expected_version', 'account_address', 'subject',
                'payload', 'payload_hash', 'state', 'expires_at', 'documents_supported')


def _email_snapshot(conn, identity):
    raw = _service('execution').email_snapshot(conn, identity)
    return {key: raw[key] for key in EMAIL_FIELDS if key in raw}


def execution_view():
    """Finite sealed proposals/outcomes, never SMTP configuration or job tokens."""
    gateway = _service('execution')
    with db._conn() as conn:
        rows = conn.execute('''SELECT p.approval_id,d.created_at,a.state,a.finished_at
            FROM secretary_email_proposals p
            LEFT JOIN secretary_email_dispatches d ON d.approval_id=p.approval_id
            LEFT JOIN secretary_email_attempts a ON a.approval_id=p.approval_id
            ORDER BY p.approval_id DESC LIMIT 25''').fetchall()
        proposals = []
        for identity, dispatched_at, state, finished_at in rows[:24]:
            item = _email_snapshot(conn, identity)
            item.update(dispatch_state=state if state in ('attempted', 'accepted', 'uncertain')
                        else 'queued' if dispatched_at else 'not_requested',
                        dispatched_at=dispatched_at, finished_at=finished_at,
                        delivered=False, automatic_retry=False)
            proposals.append(item)
    return dict(available=True, proposals=proposals, limit=24, truncated=len(rows) > 24,
                accounts=[dict(account_address=item['address'])
                          for item in config.EMAIL_ACCOUNTS[:50] if item.get('address')],
                subject=gateway.SUBJECT, documents_supported=True,
                delivery_verification_supported=False)


def budget_view():
    """Aggregate UTC-day accounting only: no prompts, credentials or lease IDs."""
    budget = _service('ai_budget')
    limits = budget._limits()
    with db._conn() as conn:
        raw = budget.usage(conn)
    counters = ('calls', 'reserved_tokens', 'charged_tokens', 'reported_tokens',
                'cost_unreported_calls', 'inflight')
    result = {key: raw.get(key) for key in counters}
    cost = raw.get('reported_cost')
    result['reported_cost'] = str(cost) if cost is not None else None
    # Optional enforceable caps are surfaced so the FE can flag saturation.
    # Only server-declared scope names and numeric limits leave this view.
    daily_cost = limits.get('DAILY_COST', 0)
    scopes = raw.get('scopes') or {}
    scope_caps = {name: limits['SCOPE_' + name] for name in budget.SCOPE_CALLS
                  if limits.get('SCOPE_' + name, 0) > 0}
    with db._conn() as conn:
        orphaned = budget.orphans(conn)
    return dict(available=True, usage=result,
                limits={key: limits[key] for key in ('DAILY_CALLS', 'DAILY_TOKENS',
                        'MAX_INFLIGHT', 'MAX_OUTPUT_TOKENS')},
                daily_cost=str(daily_cost), money_cap_enforced=daily_cost > 0,
                scope_caps=scope_caps,
                scope_usage={name: scopes.get(name, 0) for name in scope_caps},
                orphaned=[dict(job_id=item['job_id'], reserved_tokens=item['reserved_tokens'],
                               created_at=item['created_at'].isoformat()) for item in orphaned],
                day_timezone='UTC', scope='jobs execution context only',
                reservations_refunded=False)


def budget_limits_view():
    """Editable Worker budget caps only: no usage, prompts or credentials."""
    budget = _service('ai_budget')
    limits = budget._limits()
    fields = [dict(key='AI_BUDGET_' + name, value=str(limits[name]))
              for name in ('DAILY_CALLS', 'DAILY_TOKENS', 'MAX_INFLIGHT', 'MAX_OUTPUT_TOKENS')]
    fields.append(dict(key='AI_BUDGET_DAILY_COST', value=str(limits['DAILY_COST'])))
    return dict(available=True, fields=fields)


def _json_object(keys):
    """Reject duplicate keys/nonfinite JSON before any service or DB mutation."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate JSON key')
            value[key] = item
        return value

    def constant(value):
        raise ValueError('Nonfinite JSON')

    if not request.is_json:
        raise ValueError('JSON content type required')
    return _object(json.loads(request.get_data(), object_pairs_hook=pairs,
                              parse_constant=constant), keys)


def _email_data(keys):
    data = _json_object(keys)
    _positive(data['expected_version'])
    gateway = _service('execution')
    gateway._payload(data['payload'])
    gateway._address(data['account_address'])
    return data


def _server_data_policy(data):
    """Trusted dashboard-only CAS, no wildcard, bulk grant or implicit opt-in."""
    _object(data, ('channel', 'effect', 'allowed', 'expected_version'))
    channel, effect, allowed = data['channel'], data['effect'], data['allowed']
    if channel not in DATA_CHANNELS or effect not in DATA_EFFECTS or type(allowed) is not bool:
        raise ValueError('Invalid consent')
    version = _positive(data['expected_version'], zero=True)
    sec = _service('secretary')
    _service('policy')
    with db._conn() as conn, conn.transaction():
        if version == 0:
            row = conn.execute('''INSERT INTO data_policies(channel,effect,allowed)
                VALUES (%s,%s,%s) ON CONFLICT(channel,effect) DO NOTHING RETURNING version''',
                (channel, effect, allowed)).fetchone()
        else:
            row = conn.execute('''UPDATE data_policies SET allowed=%s,version=version+1,updated_at=now()
                WHERE channel=%s AND effect=%s AND version=%s RETURNING version''',
                (allowed, channel, effect, version)).fetchone()
        if not row:
            raise sec.Conflict('Consent changed; reload')
    return dict(channel=channel, effect=effect, allowed=allowed, version=row[0])


def _server_stop_responsibility(identity, data, caller):
    """Bounded fallback until secretary exposes a trusted owner stop API.

    Same writer lock, CAS, immutable receipt/audit and dependent invalidations as
    secretary.apply. Only restrictive transitions; no creation/resume/tool/send.
    """
    _positive(identity)
    _object(data, ('action', 'expected_version'))
    version = _positive(data['expected_version'])
    action = data['action']
    if action not in ('suspend', 'revoke'):
        raise ValueError('Only suspension/revocation supported')
    sec = _service('secretary')
    if (type(caller) is not sec.DashboardOwner or caller.authenticated is not True
            or caller.csrf_verified is not True or caller.owner_id != str(config.TELEGRAM_CHAT_ID or '').strip()
            or not caller.owner_id.isdecimal() or int(caller.owner_id) <= 0):
        raise PermissionError('Trusted owner required')
    key = f'dashboard-responsibility:{identity}:v{version}:{action}'
    payload = dict(id=identity, action=action, expected_version=version, owner_id=caller.owner_id)
    digest = sec.payload_hash(payload)
    state = 'suspended' if action == 'suspend' else 'revoked'
    with db._conn() as conn, conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(%s)', (sec.WRITE_LOCK,))
        previous = conn.execute('SELECT request_hash,receipt FROM secretary_receipts WHERE operation_key=%s', (key,)).fetchone()
        if previous:
            if previous[0] != digest:
                raise sec.Conflict('Changed retry')
            return dict(receipt=previous[1])
        row = conn.execute('''UPDATE secretary_responsibilities SET state=%s,version=version+1,updated_at=now()
            WHERE id=%s AND version=%s AND state IN ('active','suspended') RETURNING version''',
            (state, identity, version)).fetchone()
        if not row:
            raise sec.Conflict('Responsibility changed or revoked')
        invalidated = conn.execute('''UPDATE secretary_approvals SET state='revoked',version=version+1
            WHERE state IN ('pending','approved') AND (responsibility_id=%s OR practice_id IN
                (SELECT id FROM secretary_practices WHERE responsibility_id=%s)) RETURNING id,version''',
            (identity, identity)).fetchall()
        for approval, approval_version in invalidated:
            conn.execute('INSERT INTO secretary_decisions(operation_key,event,data) VALUES (%s,%s,%s)',
                         (f'{key}:invalidate:{approval}', 'approval_invalidated',
                          json.dumps(dict(approval_id=approval, version=approval_version, cause=key))))
        receipt = f'Responsabilità #{identity}: {state}, versione {row[0]}. Nessuna azione esterna eseguita.'
        result = dict(id=identity, version=row[0], state=state)
        conn.execute('''INSERT INTO secretary_receipts(operation_key,request_hash,receipt,result)
            VALUES (%s,%s,%s,%s)''', (key, digest, receipt, json.dumps(result)))
        conn.execute('INSERT INTO secretary_decisions(operation_key,event,data) VALUES (%s,%s,%s)',
                     (key, 'receipt', json.dumps(dict(request=payload, result=result))))
    return dict(receipt=receipt)


def credential(path=None):
    if path is None:
        configured = Path(config.get('DASHBOARD_CREDENTIAL_FILE', str(CREDENTIAL_FILE)))
        path = CREDENTIAL_FILE if not configured.exists() and CREDENTIAL_FILE.exists() else configured
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        value = {'username': 'admin', 'password': secrets.token_urlsafe(32)}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
    path.chmod(0o600)
    return json.loads(path.read_text())


def snapshot():
    with db._conn() as conn:
        counts = conn.execute('''SELECT day,channel,
            COALESCE(direction,'unknown'),count(*),
            count(*) FILTER (WHERE raw_verdict IS NOT NULL),
            count(*) FILTER (WHERE needs_action)
            FROM communications WHERE day BETWEEN
            (now() AT TIME ZONE %s)::date - 1 AND (now() AT TIME ZONE %s)::date
            GROUP BY 1,2,3 ORDER BY 1,2,3''', (config.TIMEZONE, config.TIMEZONE)).fetchall()
        recent = conn.execute('''SELECT channel,direction,ts,
            CASE WHEN raw_verdict IS NULL THEN 'archived' ELSE 'analyzed' END
            FROM communications WHERE day BETWEEN
            (now() AT TIME ZONE %s)::date - 1 AND (now() AT TIME ZONE %s)::date
            ORDER BY acquired_at DESC,id DESC LIMIT 20''', (config.TIMEZONE, config.TIMEZONE)).fetchall()
        today = conn.execute('SELECT (now() AT TIME ZONE %s)::date', (config.TIMEZONE,)).fetchone()[0].isoformat()
        invoices = conn.execute('SELECT entity,month,count(*) FROM invoices GROUP BY 1,2 ORDER BY 2 DESC LIMIT 24').fetchall()
        appointments = conn.execute('SELECT count(*) FROM appointments WHERE NOT done').fetchone()[0]
        zips = conn.execute('SELECT entity,month,sent FROM monthly_zips ORDER BY month DESC LIMIT 12').fetchall()
        pending_calendar = conn.execute("SELECT id,title,dt,sync_state FROM appointments WHERE sync_state NOT IN ('synced','cancelled') AND NOT done ORDER BY dt LIMIT 50").fetchall()
        sent = conn.execute('SELECT count(*) FROM message_analysis WHERE notified').fetchone()[0]
        task_counts = conn.execute('SELECT status,count(*) FROM tasks GROUP BY status').fetchall()
    settings = flows.settings()
    from connectors.telegram_account import readiness
    personal_readiness = readiness()
    from connectors.gcal import readiness as gcal_readiness
    calendar_readiness = gcal_readiness()
    configuration = {
        'telegram_user': personal_readiness, 'gcal': calendar_readiness,
        'email': 'configured' if config.EMAIL_ACCOUNTS else 'missing_configuration',
        'telegram_bot': 'configured' if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID else 'missing_configuration',
        'whatsapp': 'configured' if config.WHATSAPP_ENABLED else 'disabled_in_config',
        'digest': 'configured' if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID else 'missing_configuration',
        'invoices': 'configured',
        'monthly_zip': 'configured' if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID else 'missing_configuration',
    }
    counts = [[r[0].isoformat(), *r[1:]] for r in counts]
    recent = [[r[0], r[1], r[2].isoformat() if r[2] else None, r[3]] for r in recent]
    import jobs
    return {'agent_name': config.AGENT_NAME, 'timezone': config.TIMEZONE, 'today': today, 'counts': counts, 'recent': recent,
            'jobs': jobs.metrics(), 'calendar_pending':pending_calendar, 'notifications_sent':sent,
            'tasks':task_counts,
            'invoices': invoices, 'appointments': appointments, 'zips': zips,
            'flows': [{'id': k, 'label': v, 'enabled': settings[k],
                       'configuration': configuration[k],
                       'health': ({'status': 'paused'} if not settings[k] else
                                  {'status': personal_readiness} if k == 'telegram_user' and personal_readiness != 'ready' else
                                  {'status': calendar_readiness} if k == 'gcal' and calendar_readiness != 'ready' else
                                  flows.observed().get(k, {'status': 'not_observed'}))}
                      for k, v in flows.LABELS.items()]}


def create_app(auth=None):
    auth = auth or credential()
    password_hash = generate_password_hash(auth['password'])
    username = auth['username']
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 4096
    import file_browser
    file_browser.register(app)
    import admin_config
    admin_config.register(app)
    register_secretary(app)
    sessions, attempts = {}, {}
    lock = threading.Lock()
    from platform_health import HealthRunner
    health_runner = HealthRunner()

    @app.post('/api/platform-health')
    def platform_health():
        result = health_runner.run()
        if result is None:
            return jsonify(error='Verifica in corso o attesa di sicurezza: riprovare tra 30 secondi.'), 429, {'Retry-After': '30'}
        return jsonify(result)

    @app.before_request
    def guard():
        host = request.host.rsplit(':', 1)[0] if request.host.count(':') == 1 else request.host
        if host not in {'127.0.0.1', 'localhost', '[::1]', config.get('DASHBOARD_HOSTNAME', 'pi-secretary.local'), config.get('DASHBOARD_BIND', '127.0.0.1')}:
            return jsonify(error='Host not allowed'), 400
        if request.method != 'GET':
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return jsonify(error='Cross-site request refused'), 403
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return jsonify(error='Origin refused'), 403
        if request.path in ('/', '/app.js', '/style.css', '/api/login', '/healthz'):
            return
        with lock:
            now = time.time()
            for key in list(sessions):
                if sessions[key]['expires'] < now:
                    del sessions[key]
            session = sessions.get(request.cookies.get('secretary_session'))
        if not session:
            return jsonify(error='Authentication required'), 401
        if request.method != 'GET' and not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']):
            return jsonify(error='CSRF refused'), 403
        request.dashboard_session = session
        if request.path == '/api/configuration' and request.method == 'POST':
            request.max_content_length = 196608
        if request.path == '/api/files/upload' and request.method == 'POST':
            request.max_content_length = file_browser.MAX_UPLOAD
        if request.method == 'POST' and (request.path.startswith('/api/secretary/approvals/')
            or request.path == '/api/secretary/emails'
            or request.path.startswith('/api/secretary/emails/')):
            request.max_content_length = 49152

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.get('/')
    def index():
        return send_from_directory(config.BASE_DIR / 'web', 'index.html')

    @app.get('/healthz')
    def healthz():
        # Only the serving app process: no DB, connector or external checks.
        return jsonify(status='started')

    @app.get('/<name>')
    def asset(name):
        if name not in ('app.js', 'style.css'):
            return '', 404
        return send_from_directory(config.BASE_DIR / 'web', name)

    @app.post('/api/login')
    def login():
        now = time.time()
        with lock:
            # Global limit also bounds memory and password-hashing CPU.
            recent = [t for t in attempts.get('all', []) if now-t < 300]
            attempts['all'] = recent
            if len(recent) >= 10:
                return jsonify(error='Troppi tentativi; attendere 5 minuti'), 429
            recent.append(now)
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify(error='Invalid request'), 400
        if data.get('username') != username or not check_password_hash(password_hash, str(data.get('password', ''))):
            return jsonify(error='Credenziali non valide'), 401
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with lock:
            if len(sessions) >= 64:
                sessions.clear()
            sessions[token] = {'csrf': csrf, 'expires': now+3600}
        response = jsonify(csrf=csrf)
        response.set_cookie('secretary_session', token, httponly=True, samesite='Strict',
                            secure=config.get_bool('DASHBOARD_COOKIE_SECURE'), max_age=3600)
        return response

    @app.get('/api/session')
    def session():
        return jsonify(csrf=request.dashboard_session['csrf'])

    @app.post('/api/logout')
    def logout():
        with lock:
            sessions.pop(request.cookies.get('secretary_session'), None)
        response = jsonify(ok=True)
        response.delete_cookie('secretary_session')
        return response

    @app.get('/api/dashboard')
    def overview():
        try:
            return jsonify(snapshot())
        except Exception:
            return jsonify(error='Database non disponibile'), 503

    @app.post('/api/flows/<flow>')
    def toggle(flow):
        data = request.get_json(silent=True)
        value = data.get('enabled') if isinstance(data, dict) else None
        if flow not in flows.LABELS or type(value) is not bool:
            return jsonify(error='Invalid flow setting'), 400
        try:
            flows.set_enabled(flow, value)
        except Exception:
            return jsonify(error='Database non disponibile'), 503
        return jsonify(ok=True)

    return app


def register_secretary(app):
    """All handlers inherit the app's authentication, CSRF and same-origin guard."""
    from functools import wraps
    from werkzeug.exceptions import HTTPException

    def bounded(callback):
        @wraps(callback)
        def handle(*args, **kwargs):
            try:
                return jsonify(callback(*args, **kwargs))
            except HTTPException:
                raise
            except Unsupported:
                return jsonify(available=False, error='Servizio opzionale non installato'), 501
            except PermissionError:
                return jsonify(error='Proprietario configurato richiesto'), 403
            except (ValueError, TypeError) as exc:
                status = 409 if type(exc).__name__ == 'Conflict' else 400
                return jsonify(error='Versione o payload cambiato: ricaricare' if status == 409 else 'Dati non validi'), status
            except Exception:
                return jsonify(available=False, error='Servizio o schema non disponibile'), 503
        return handle

    @app.get('/api/secretary')
    @bounded
    def secretary_snapshot():
        return secretary_view()

    @app.get('/api/secretary/support')
    @bounded
    def secretary_support():
        import admin_config
        return admin_config.secretary_support()

    @app.get('/api/secretary/runtime')
    @bounded
    def secretary_runtime():
        return runtime_view()

    @app.post('/api/secretary/runtime/links/<int:identity>/accept')
    @bounded
    def accept_link(identity):
        _positive(identity)
        if request.args:
            raise ValueError('Unknown parameter')
        data = _json_object(('expected_version',))
        _positive(data['expected_version'])
        caller = _owner()
        # The proposal version pins stored evidence and both scope versions.
        # Never accept replacement evidence, metadata or permissions from JSON.
        with db._conn() as conn, conn.transaction():
            result = _service('secretary').accept_link(conn, proposal_id=identity,
                expected_version=data['expected_version'], caller=caller)
        return dict(link=result, external_execution=False)

    @app.post('/api/secretary/runtime/practices/<int:identity>/acknowledge')
    @bounded
    def acknowledge_attention(identity):
        _positive(identity)
        if request.args:
            raise ValueError('Unknown parameter')
        data = _json_object(('expected_version', 'attention_hash'))
        _positive(data['expected_version'])
        caller = _owner()
        with db._conn() as conn, conn.transaction():
            result = _service('secretary_runtime').acknowledge_dashboard(
                conn, identity, caller=caller, **data)
        return dict(acknowledgement=result, external_execution=False)

    @app.post('/api/secretary/practices/<int:identity>/links')
    @bounded
    def link_evidence(identity):
        """Owner collega un messaggio nativo esistente come evidenza della pratica.

        Il payload richiede la versione della pratica letta, l'ID del messaggio
        nativo e un estratto ESATTO del suo snapshot indicizzato: nessun testo
        arbitrario, metadati o permessi dal browser. Nessuna azione esterna.
        """
        _positive(identity)
        if request.args:
            raise ValueError('Unknown parameter')
        data = _json_object(('expected_version', 'message_id', 'evidence', 'operation_key'))
        _positive(data['expected_version'])
        _positive(data['message_id'])
        caller = _owner()
        with db._conn() as conn, conn.transaction():
            result = _service('secretary').link_evidence(conn, practice_id=identity,
                expected_version=data['expected_version'], message_id=data['message_id'],
                evidence=data['evidence'], caller=caller,
                operation_key=data['operation_key'])
        return dict(link=result, external_execution=False)

    @app.get('/api/privacy/messages/<int:identity>/export')
    @bounded
    def export_message(identity):
        """Esportazione JSON di tutto ciò che il sistema conserva di un messaggio."""
        _positive(identity)
        if request.args:
            raise ValueError('Unknown parameter')
        caller = _owner()
        with db._conn() as conn, conn.transaction():
            import privacy
            return privacy.export_message(conn, identity, caller)

    @app.post('/api/privacy/messages/<int:identity>/erase')
    @bounded
    def erase_message(identity):
        """Oblio coordinato: DB atomico + report dei file da rimuovere verificati."""
        _positive(identity)
        if request.args:
            raise ValueError('Unknown parameter')
        data = _json_object(('operation_key',))
        caller = _owner()
        with db._conn() as conn, conn.transaction():
            import privacy
            report = privacy.erase_message(conn, identity, caller,
                                           operation_key=data['operation_key'])
        return dict(erase=report, files_pending_verification=report['files_to_delete'])

    @app.get('/api/secretary/execution')
    @bounded
    def secretary_execution():
        return execution_view()

    @app.get('/api/secretary/budget')
    @bounded
    def secretary_budget():
        return budget_view()

    @app.get('/api/secretary/budget/limits')
    @bounded
    def secretary_budget_limits():
        return budget_limits_view()

    @app.post('/api/secretary/budget/reconcile')
    @bounded
    def secretary_budget_reconcile():
        """Operator-initiated release of provably dead in-flight slots."""
        _owner()
        if request.args:
            raise ValueError('Unknown parameter')
        _json_object(())
        budget = _service('ai_budget')
        with db._conn() as conn, conn.transaction():
            freed = budget.reconcile_orphans(conn)
        return dict(available=True, freed=len(freed))

    @app.post('/api/secretary/jobs/requeue')
    @bounded
    def secretary_jobs_requeue():
        """Operator-initiated retry of failed jobs (analysis/reconcile/owner)."""
        _owner()
        data = _json_object(('kind',))
        kind = data['kind']
        if kind not in ('analysis', 'reconcile', 'owner'):
            raise ValueError('Unknown job kind')
        limit = data.get('limit', 50)
        if type(limit) is not int or not 0 < limit <= 200:
            raise ValueError('Invalid limit')
        requeued = _service('jobs').requeue_failed(kind, limit)
        return dict(available=True, kind=kind, requeued=requeued)

    @app.post('/api/secretary/emails')
    @bounded
    def request_email():
        data = _email_data(('practice_id', 'expected_version', 'payload',
                            'account_address', 'expires_at', 'operation_key'))
        _positive(data['practice_id'])
        sec = _service('secretary')
        sec._operation_key(data['operation_key'])
        sec._time(data['expires_at'])
        _owner()
        with db._conn() as conn, conn.transaction():
            raw = _service('execution').request_email(conn, **data)
            result = {key: raw[key] for key in EMAIL_FIELDS if key in raw}
        return dict(email=result, external_execution=False)

    @app.get('/api/secretary/emails/<int:identity>')
    @bounded
    def email_snapshot(identity):
        _positive(identity)
        if request.args:
            raise ValueError('Unknown parameter')
        with db._conn() as conn:
            return _email_snapshot(conn, identity)

    @app.post('/api/secretary/emails/<int:identity>/dispatch')
    @bounded
    def dispatch_email(identity):
        _positive(identity)
        data = _email_data(('expected_version', 'account_address', 'subject', 'payload',
                            'approval_operation_key', 'operation_key'))
        sec = _service('secretary')
        sec._operation_key(data['approval_operation_key'])
        sec._string(data['operation_key'], 180)
        if not data['operation_key'].startswith('secretary-email-dispatch:'):
            raise ValueError('Explicit dispatch key required')
        gateway = _service('execution')
        if data.pop('subject') != gateway.SUBJECT:
            raise sec.Conflict('Fixed subject changed')
        caller = _owner()
        with db._conn() as conn, conn.transaction():
            result = gateway.dispatch_email(conn, approval_id=identity, caller=caller, **data)
        return dict(dispatch={key: result[key] for key in ('approval_id', 'dispatch_key')},
                    queued=True, delivered=False, automatic_retry=False)

    @app.get('/api/knowledge')
    @bounded
    def knowledge_snapshot():
        return _service('knowledge').snapshot(limit=20)

    @app.get('/api/knowledge/search')
    @bounded
    def knowledge_search():
        if set(request.args) - {'q', 'limit', 'channel'}:
            raise ValueError('Unknown parameter')
        query = request.args.get('q', '')
        channel = request.args.get('channel')
        limit = int(request.args.get('limit', '8'))
        if not query.strip() or len(query) > 512 or '\x00' in query or not 1 <= limit <= 50:
            raise ValueError('Invalid search')
        if channel is not None and (not channel or len(channel) > 80 or '\x00' in channel):
            raise ValueError('Invalid channel')
        return dict(results=_service('knowledge').search(query, limit=limit, channel=channel))

    @app.get('/api/knowledge/open/<source_type>/<int:identity>')
    @bounded
    def knowledge_open(source_type, identity):
        if source_type not in ('message', 'document', 'entry', 'note', 'practice') or request.args:
            raise ValueError('Invalid source')
        _positive(identity)
        return _service('knowledge').open_source(source_type, identity)

    @app.get('/api/data-policies')
    @bounded
    def consents():
        return data_policy_list()

    @app.post('/api/data-policies')
    @bounded
    def consent_update():
        return _server_data_policy(request.get_json(silent=True))

    @app.post('/api/media/retry-blocked')
    @bounded
    def retry_media_consent():
        if request.args:
            raise ValueError('Unknown parameter')
        data = _json_object(('message_id', 'expected_channel'))
        mid = _positive(data['message_id'])
        channel = data['expected_channel']
        if type(channel) is not str or channel not in ('telegram', 'telegram (account)', 'whatsapp'):
            raise ValueError('Invalid native media channel')
        with db._conn() as conn:
            return _service('connectors.media_archive').retry_blocked(conn, mid, expected_channel=channel)

    @app.get('/api/capabilities')
    @bounded
    def capability_snapshot():
        module = _service('capabilities')
        settings = flows.settings()
        return dict(capabilities=[dict(id=name, enabled=bool(settings.get(name, False)), optional=True)
                                  for name in module.OPTIONAL])

    @app.post('/api/secretary/responsibilities/<int:identity>')
    @bounded
    def stop_responsibility(identity):
        return _server_stop_responsibility(identity, request.get_json(silent=True), _owner())

    @app.post('/api/secretary/approvals/<int:identity>')
    @bounded
    def resolve_approval(identity):
        data = _object(request.get_json(silent=True), ('payload', 'decision', 'expected_version', 'operation_key'))
        _positive(identity)
        _positive(data['expected_version'])
        sec = _service('secretary')
        caller = _owner()
        with db._conn() as conn, conn.transaction():
            receipt = sec.resolve_approval(conn, identity, data['payload'], decision=data['decision'],
                expected_version=data['expected_version'], operation_key=data['operation_key'], caller=caller)
        return dict(receipt=receipt, operation_key=data['operation_key'], external_execution=False)


def start():
    server = create_server(create_app(), host=config.get('DASHBOARD_BIND', '127.0.0.1'),
                           port=int(config.get('DASHBOARD_PORT', '8080')), threads=4)
    threading.Thread(target=server.run, name='dashboard', daemon=True).start()
    return server