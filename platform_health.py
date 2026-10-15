"""Explicit read-only probes. Never called by polling, startup or healthz."""
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
import imaplib
import json
import re
import ssl
import threading
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

import config
import flows

TIMEOUT = 4
DEADLINE = 12
COOLDOWN = 30
_workers = {}


def register_workers(whatsapp, personal):
    _workers.update(whatsapp=whatsapp, telegram_user=personal)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirect refused')


def remote(url, headers=None, data=None):
    # All URLs are constructed here from fixed provider roots, never request data.
    with build_opener(NoRedirect).open(Request(url, headers=headers or {}, data=data), timeout=TIMEOUT) as response:
        return json.loads(response.read(65537))


def database():
    import db
    if not config.DATABASE_URL:
        return 'unconfigured', 'Connessione DB assente.'
    with db.psycopg.connect(config.DATABASE_URL, connect_timeout=TIMEOUT,
                          options='-c default_transaction_read_only=on -c statement_timeout=4000') as conn:
        if conn.execute('SELECT 1').fetchone() != (1,):
            raise ValueError()
    return 'tested', 'SELECT 1 riuscito; nessuna scrittura.'


def email_check(account):
    from connectors import ms_oauth
    from connectors.email import _xoauth2
    # Match existing web SSRF policy: no arbitrary custom/private IMAP probes.
    if account['imap_host'] not in {p[0] for p in config._EMAIL_PRESETS.values() if p[0]} or account['imap_port'] != 993:
        return 'skipped', 'Host/porta personalizzati: verifica da terminale (protezione SSRF).'
    token = None
    if account.get('auth') == 'oauth2':
        entry = ms_oauth._load_tokens().get(account['address'].lower(), {})
        if not entry.get('access_token') or entry.get('expires_at', 0) <= time.time() + 30:
            return 'skipped', 'Token Microsoft assente/scaduto; nessun refresh o nuova autorizzazione.'
        token = entry['access_token']
    elif not account.get('password'):
        return 'unconfigured', 'Password assente.'
    conn = imaplib.IMAP4_SSL(account['imap_host'], 993, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
    try:
        if token:
            conn.authenticate('XOAUTH2', lambda _: _xoauth2(account['address'], token))
        else:
            conn.login(account['address'], account['password'])
        if conn.noop()[0] != 'OK':
            raise ValueError()
    finally:
        # Close transport without SELECT/FETCH/STORE or mailbox expunge.
        conn.shutdown()
    return 'tested', 'Autenticazione IMAP e NOOP riusciti; nessun messaggio letto. SMTP non testato.'


def bot():
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        return 'unconfigured', 'Token bot assente.'
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        return 'unconfigured', 'Formato token non valido.'
    result = remote('https://api.telegram.org/bot' + token + '/getMe')
    if not result.get('ok') or not result.get('result', {}).get('is_bot'):
        raise ValueError()
    return 'tested', 'getMe riuscito; invio, polling e autorizzazione proprietario non testati.'


def openrouter():
    if not config.OPENROUTER_API_KEY:
        return 'unconfigured', 'Chiave AI assente.'
    result = remote('https://openrouter.ai/api/v1/key', {'Authorization': 'Bearer ' + config.OPENROUTER_API_KEY})
    if not isinstance(result.get('data'), dict):
        raise ValueError()
    return 'tested', 'Credenziale verificata; nessuna inferenza o verifica del modello.'


def calendar():
    from connectors import gcal
    if not config.GCAL_ENABLED:
        return 'disabled', 'Calendar disabilitato nella configurazione.'
    sa = gcal._load_service_account()
    if not sa or gcal.readiness() != 'ready':
        return 'unconfigured', 'Service account o ID calendario mancanti/non validi.'
    now = int(time.time())
    claims = {'iss': sa['client_email'], 'scope': 'https://www.googleapis.com/auth/calendar.readonly',
              'aud': gcal.TOKEN_URL, 'iat': now, 'exp': now + 3600}
    segments = gcal._b64url(b'{"alg":"RS256","typ":"JWT"}') + '.' + gcal._b64url(json.dumps(claims).encode())
    n, d = gcal._parse_rsa_private_key(sa['private_key'])
    assertion = segments + '.' + gcal._b64url(gcal._rsa_sign_sha256(segments.encode(), n, d))
    token = remote(gcal.TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'},
                   urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion}).encode())['access_token']
    remote('https://www.googleapis.com/calendar/v3/calendars/' + quote(gcal.calendar_id(), safe=''),
           {'Authorization': 'Bearer ' + token})
    return 'tested', 'Lettura calendario con scope readonly; permessi di scrittura non testati. Cache runtime invariata.'


def worker_check(key):
    enabled = config.WHATSAPP_ENABLED if key == 'whatsapp' else config.TELEGRAM_USER_ENABLED
    if not enabled:
        return 'disabled', 'Disabilitato nella configurazione.'
    worker = _workers.get(key)
    observed = flows.observed().get(key, {})
    if key == 'whatsapp':
        active = bool(worker and worker.proc and worker.proc.poll() is None and worker.ready)
    else:
        active = bool(worker and worker.thread and worker.thread.is_alive() and worker.client and worker.client.is_connected())
    observed_at = (datetime.fromtimestamp(observed['observed_at'], timezone.utc).isoformat()
                   if observed.get('observed_at') else 'mai')
    return 'observed', ('Worker attivo/connesso osservato; non è una verifica remota.' if active else
                        'Worker non connesso o non disponibile; nessuna seconda sessione avviata.') + ' Stato flusso: ' + str(observed.get('status', 'non osservato')) + ' · ultima osservazione: ' + observed_at


def probes():
    checks = [('database', 'Database', database), ('telegram_bot', 'Telegram bot', bot),
              ('telegram_user', 'Telegram personale', lambda: worker_check('telegram_user')),
              ('whatsapp', 'WhatsApp', lambda: worker_check('whatsapp')),
              ('gcal', 'Google Calendar', calendar), ('openrouter', 'OpenRouter', openrouter)]
    for i, account in enumerate(config.EMAIL_ACCOUNTS, 1):
        checks.append((f'email_{i}', f'Email · account {i}', lambda a=dict(account): email_check(a)))
    if not config.EMAIL_ACCOUNTS:
        checks.append(('email', 'Email', lambda: ('unconfigured', 'Nessun account configurato.')))
    return checks


def stamp():
    return datetime.now(timezone.utc).isoformat()


def execute(key, label, callback):
    start = time.monotonic()
    try:
        status, explanation = callback()
    except Exception:
        status, explanation = 'failed', 'Verifica non riuscita: controllare rete, credenziali e permessi. Dettagli remoti omessi.'
    return dict(id=key, label=label, status=status, explanation=explanation,
                checked_at=stamp(), latency_ms=round((time.monotonic() - start) * 1000))


class HealthRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.next_run = 0
        self.pending = []

    def run(self):
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if time.monotonic() < self.next_run or any(not f.done() for f in self.pending):
                return None
            self.next_run = time.monotonic() + COOLDOWN
            started = stamp()
            pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix='health')
            checks = probes()
            futures = [pool.submit(execute, *check) for check in checks]
            self.pending = futures
            done, _ = wait(futures, timeout=DEADLINE)
            results = [future.result() if future in done else dict(id=check[0], label=check[1],
                       status='timeout', explanation='Tempo massimo superato; risultato non verificato.',
                       checked_at=stamp(), latency_ms=DEADLINE * 1000) for check, future in zip(checks, futures)]
            pool.shutdown(wait=False, cancel_futures=True)
            return dict(started_at=started, completed_at=stamp(), cooldown_seconds=COOLDOWN, results=results)
        finally:
            self.lock.release()