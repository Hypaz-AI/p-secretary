"""LAN dashboard: opaque expiring sessions, CSRF, bounded API, no send actions."""
import hmac
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
    sessions, attempts = {}, {}
    lock = threading.Lock()

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


def start():
    server = create_server(create_app(), host=config.get('DASHBOARD_BIND', '127.0.0.1'),
                           port=int(config.get('DASHBOARD_PORT', '8080')), threads=4)
    threading.Thread(target=server.run, name='dashboard', daemon=True).start()
    return server