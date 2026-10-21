"""Dashboard LAN: API limitate del proprietario (sessione, CSRF, same-origin)."""
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

from secretary.core import config, dashboard_owner as owners, db, flows

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
    # Chiamata SOLO dentro le rotte protette. Mai accettare flag/ID dal chiamante.
    return owners.current()


def data_policy_list():
    """policy v1 has no management API. Missing rows are DENIED, never seeded."""
    _service('secretary.core.policy')
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


def budget_view():
    """Aggregate UTC-day accounting only: no prompts, credentials or lease IDs."""
    budget = _service('secretary.core.ai_budget')
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
    budget = _service('secretary.core.ai_budget')
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


def _server_data_policy(data):
    """Trusted dashboard-only CAS, no wildcard, bulk grant or implicit opt-in."""
    _object(data, ('channel', 'effect', 'allowed', 'expected_version'))
    channel, effect, allowed = data['channel'], data['effect'], data['allowed']
    if channel not in DATA_CHANNELS or effect not in DATA_EFFECTS or type(allowed) is not bool:
        raise ValueError('Invalid consent')
    version = _positive(data['expected_version'], zero=True)
    _service('secretary.core.policy')
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
            raise owners.Conflict('Consent changed; reload')
    return dict(channel=channel, effect=effect, allowed=allowed, version=row[0])


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
    from secretary.connectors.telegram_account import readiness
    personal_readiness = readiness()
    from secretary.connectors.gcal import readiness as gcal_readiness
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
    from secretary.core import jobs
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
    from secretary.archive import file_browser
    file_browser.register(app)
    from secretary.dashboard import admin_config
    admin_config.register(app)
    register_api(app)
    sessions, attempts = {}, {}
    lock = threading.Lock()
    from secretary.core.platform_health import HealthRunner
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

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.get('/')
    def index():
        return send_from_directory(config.STATIC_DIR, 'index.html')

    @app.get('/healthz')
    def healthz():
        # Only the serving app process: no DB, connector or external checks.
        return jsonify(status='started')

    @app.get('/<name>')
    def asset(name):
        if name not in ('app.js', 'style.css'):
            return '', 404
        return send_from_directory(config.STATIC_DIR, name)

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


def agent_view():
    """Cosa ha fatto l'agente: metadati limitati, mai prompt né risposte del modello."""
    from secretary.agent import data as agent_data
    with db._conn() as conn:
        runs = conn.execute('''SELECT id,kind,state,tool_calls,error,model,started_at,finished_at
            FROM agent_runs ORDER BY id DESC LIMIT 30''').fetchall()
        drafts = conn.execute('''SELECT id,account,recipient,subject,left(body,2000),state,
                created_at,decided_at,sent_at FROM agent_email_outbox ORDER BY id DESC LIMIT 20''').fetchall()
    return dict(
        available=True, model=config.AGENT_MODEL,
        readonly_role=agent_data.readonly_role() is not None,
        limits=dict(tool_calls=config.AGENT_MAX_TOOL_CALLS, inbox_batch=config.AGENT_INBOX_BATCH,
                    notify_per_day=config.AGENT_NOTIFY_MAX_PER_DAY),
        sends_allowed=config.SECRETARY_MODE == 'active',
        runs=[dict(id=r[0], kind=r[1], state=r[2], tool_calls=r[3], error=r[4], model=r[5],
                   started_at=r[6].isoformat(), finished_at=r[7].isoformat() if r[7] else None)
              for r in runs],
        emails=[dict(id=r[0], account=r[1], recipient=r[2], subject=r[3], body=r[4], state=r[5],
                     created_at=r[6].isoformat(), decided_at=r[7].isoformat() if r[7] else None,
                     sent_at=r[8].isoformat() if r[8] else None) for r in drafts])


def agent_run_view(identity):
    """Strumenti usati in una esecuzione: nome, esito e riepilogo troncato."""
    with db._conn() as conn:
        rows = conn.execute('''SELECT seq,tool,args,ok,summary,duration_ms,at
            FROM agent_tool_calls WHERE run_id=%s ORDER BY seq LIMIT 60''', (identity,)).fetchall()
    return dict(run_id=identity, calls=[dict(seq=r[0], tool=r[1], args=r[2], ok=r[3], summary=r[4],
                                             duration_ms=r[5], at=r[6].isoformat()) for r in rows])


def register_api(app):
    """Tutti i gestori ereditano autenticazione, CSRF e same-origin dell'app."""
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

    @app.get('/api/agent')
    @bounded
    def agent_snapshot():
        return agent_view()

    @app.get('/api/agent/runs/<int:identity>')
    @bounded
    def agent_run(identity):
        _positive(identity)
        if request.args:
            raise ValueError('Unknown parameter')
        return agent_run_view(identity)

    @app.get('/api/privacy/messages/<int:identity>/export')
    @bounded
    def export_message(identity):
        """Esportazione JSON di tutto ciò che il sistema conserva di un messaggio."""
        _positive(identity)
        if request.args:
            raise ValueError('Unknown parameter')
        caller = _owner()
        with db._conn() as conn, conn.transaction():
            from secretary.core import privacy
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
            from secretary.core import privacy
            report = privacy.erase_message(conn, identity, caller,
                                           operation_key=data['operation_key'])
        return dict(erase=report, files_pending_verification=report['files_to_delete'])

    @app.get('/api/budget')
    @bounded
    def ai_budget_snapshot():
        return budget_view()

    @app.get('/api/budget/limits')
    @bounded
    def ai_budget_limits():
        return budget_limits_view()

    @app.post('/api/budget/reconcile')
    @bounded
    def ai_budget_reconcile():
        """Rilascio, su richiesta dell'operatore, degli slot in volo dimostrabilmente morti."""
        _owner()
        if request.args:
            raise ValueError('Unknown parameter')
        _json_object(())
        budget = _service('secretary.core.ai_budget')
        with db._conn() as conn, conn.transaction():
            freed = budget.reconcile_orphans(conn)
        return dict(available=True, freed=len(freed))

    @app.post('/api/jobs/requeue')
    @bounded
    def jobs_requeue():
        """Nuovo tentativo, su richiesta dell'operatore, dei job falliti (posta/dialogo/riepilogo)."""
        _owner()
        data = _json_object(('kind',))
        kind = data['kind']
        if kind not in ('analysis', 'owner', 'digest'):
            raise ValueError('Unknown job kind')
        requeued = _service('secretary.core.jobs').requeue_failed(kind, 50)
        return dict(available=True, kind=kind, requeued=requeued)

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
            return _service('secretary.connectors.media_archive').retry_blocked(conn, mid, expected_channel=channel)

    @app.get('/api/capabilities')
    @bounded
    def capability_snapshot():
        module = _service('secretary.core.capabilities')
        settings = flows.settings()
        return dict(capabilities=[dict(id=name, enabled=bool(settings.get(name, False)), optional=True)
                                  for name in module.OPTIONAL])


def start():
    server = create_server(create_app(), host=config.get('DASHBOARD_BIND', '127.0.0.1'),
                           port=int(config.get('DASHBOARD_PORT', '8080')), threads=4)
    threading.Thread(target=server.run, name='dashboard', daemon=True).start()
    return server