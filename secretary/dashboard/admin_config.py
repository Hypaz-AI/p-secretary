"""Allowlisted, staged dashboard configuration. No runtime mutation or shell API.

The env file is the sole commit point. Calendar keys are immutable content-addressed
private files: publishing the env pointer last makes a batch atomic, even on crash.
"""
import hashlib
import hmac
import importlib.util
import imaplib
import ipaddress
import json
import os
from pathlib import Path
import re
import smtplib
import socket
import ssl
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from secretary.core import config
from secretary.connectors import notion_agent
from secretary.connectors.storage import FileLock, atomic_write
from secretary.setup.installer import read_env, valid_preference

_lock = threading.Lock()
_revision_key = os.urandom(32)
_restart_at = None


def restart_requested():
    """Allow the HTTP response to finish before draining application workers."""
    with _lock:
        return _restart_at is not None and time.monotonic() >= _restart_at


def automatic_restart():
    return os.environ.get('P_SECRETARY_CONTAINER') == '1'

DEPLOYMENT = {
    'P_SECRETARY_CONFIG_FILE', 'DATA_DIR', 'INVOICE_DIR', 'TELEGRAM_USER_SESSION',
    'DASHBOARD_CREDENTIAL_FILE', 'DASHBOARD_BIND', 'DASHBOARD_PORT',
    'DASHBOARD_HOSTNAME', 'DASHBOARD_COOKIE_SECURE', 'WA_CHROMIUM_PATH',
    'GCAL_SERVICE_ACCOUNT_FILE',
}
INTERACTIVE = {
    'WHATSAPP_ENABLED': 'Autorizzazione QR: python -m secretary.setup.whatsapp nel terminale del deployment.',
    'TELEGRAM_USER_ENABLED': 'Sessione personale: python -m secretary.setup.telegram_account nel terminale del deployment.',
    'TELEGRAM_API_ID': 'Credenziali e login: configure / python -m secretary.setup.telegram_account nel terminale.',
    'TELEGRAM_API_HASH': 'Credenziali e login: configure / python -m secretary.setup.telegram_account nel terminale.',
    'TELEGRAM_PHONE': 'Credenziali e login: configure / python -m secretary.setup.telegram_account nel terminale.',
    'TELEGRAM_CHAT_ID': 'Autorizzazione proprietario: python -m secretary.setup.telegram nel terminale; non modificabile dal web.',
}

# SECRETARY_MODE e AI_BUDGET_DAILY_COST hanno consumatori reali (agent.outbox.sends_allowed,
# ai_budget._limits): sono quindi modificabili e il riavvio del container li applica.
SECRETARY_FIELDS = {
    'SECRETARY_MODE': 'Modalità operativa: observe = solo bozze, nessun invio di email a terzi; active = consente di inviare le bozze che approvi tu con /invia. Non è un interruttore di sicurezza: policy e approvazioni restano attive.',
    'EFFECT_AUDIT_RETENTION_DAYS': 'Giorni di retention del registro di audit delle policy (0–3650); default 30, 0 disattiva. Le ricevute degli effetti restano immutabili: si pota solo il registro bulk dei reason-code.',
}
AGENT_FIELDS = {
    'AGENT_MODEL': 'Modello OpenRouter dell\u2019agente (tool calling obbligatorio). Default openrouter/auto: OpenRouter sceglie il modello per ogni richiesta.',
    'AGENT_WEB_MODEL': 'Modello OpenRouter che esegue la ricerca web dell\u2019agente (plugin web); vuoto usa OPENROUTER_MODEL.',
    'AGENT_NOTIFY_MAX_PER_DAY': 'Massimo notifiche urgenti inviate dall\u2019agente per giorno locale (0–100); default 5. Le emergenze verso di te non superano mai questo tetto.',
}
BUDGET_FIELDS = {
    'AI_BUDGET_DAILY_CALLS': 'Tetto tentativi HTTP AI per giorno UTC nei Worker; default 200. Ogni retry conta.',
    'AI_BUDGET_DAILY_TOKENS': 'Tetto prenotazioni conservative/consumi AI per giorno UTC nei Worker; default 500000. Nessun rimborso delle prenotazioni.',
    'AI_BUDGET_MAX_INFLIGHT': 'Massimo richieste AI simultanee nei Worker; default 2. Slot incerti richiedono riconciliazione operatore.',
    'AI_BUDGET_MAX_OUTPUT_TOKENS': 'Massimo token output per tentativo AI nei Worker (1–65536); default 65536. Non è un limite monetario.',
    'AI_BUDGET_DAILY_COST': 'Tetto di spesa AI giornaliero opzionale, valuta del provider (es. 5.00); 0 o vuoto = nessun tetto monetario, restano i limiti chiamate/token. Il costo riportato è conservativo: la chiamata che sfora viene registrata, le successive bloccate.',
}


def _budget_supported():
    return importlib.util.find_spec('secretary.core.ai_budget') is not None


def field_metadata(key):
    """Public, static UI hints: never derive defaults/help from saved credentials."""
    descriptions = {
        'AGENT_NAME': 'Nome con cui si presenta l’assistente.',
        'OPENROUTER_API_KEY': 'Chiave da openrouter.ai/keys; controllare credito e limiti di spesa.',
        'OPENROUTER_MODEL': 'Modello OpenRouter per descrivere le immagini e leggere i media archiviati: DEVE supportare nativamente input testo e immagini (image_url). Non è il modello dell\u2019agente (vedi AGENT_MODEL).',
        'OPENROUTER_AUDIO_MODEL': 'Modello OpenRouter che trascrive gli audio Telegram in testo.',
        'DATABASE_URL': 'URI PostgreSQL con credenziali percent-encoded. Verifica web solo host pubblici senza query; DB privati e opzioni TLS dal terminale. Nessuna migrazione automatica.',
        'TELEGRAM_BOT_TOKEN': 'Token BotFather per dialogo e report; non legge l’account personale. Un nuovo bot richiede autorizzazione da terminale.',
        'TELEGRAM_CHAT_ID': 'Chat privata del proprietario autorizzato con python -m secretary.setup.telegram.',
        'TELEGRAM_USER_ENABLED': 'Abilita la lettura dell’account personale dopo login MTProto da terminale.',
        'TELEGRAM_API_ID': 'ID numerico da my.telegram.org; distinto dal token del bot.',
        'TELEGRAM_API_HASH': 'API hash da my.telegram.org; non inserire codici login o password 2FA.',
        'TELEGRAM_PHONE': 'Numero dell’account personale con prefisso internazionale.',
        'TELEGRAM_USER_SESSION': 'File di sessione privato creato dal login; vuoto usa il percorso predefinito.',
        'TELEGRAM_USER_DIALOG_LIMIT': 'Numero massimo di chat per passata di recupero di ieri/oggi; non è un import storico completo.',
        'TELEGRAM_USER_MESSAGE_LIMIT': 'Numero massimo di messaggi per chat e passata di recupero di ieri/oggi.',
        'TELEGRAM_USER_RECOVERY_INTERVAL': 'Secondi tra i recuperi Telegram di ieri/oggi; rispetta i FloodWait.',
        'WHATSAPP_ENABLED': 'Abilita il ponte whatsapp-web.js; autorizzazione QR da terminale.',
        'WHATSAPP_EXCLUDED_CHATS': 'ID WhatsApp di chat o gruppi da escludere, separati da virgole (es. 120363…@g.us). Messaggi in entrata e uscita di queste chat non vengono archiviati né analizzati. Effetto dopo il riavvio; i messaggi già archiviati restano.',
        'TELEGRAM_EXCLUDED_CHATS': 'ID Telegram di chat, gruppi o canali dell’account personale da escludere, separati da virgole (es. -1001234567890). Messaggi in entrata e uscita di queste chat non vengono archiviati né analizzati. Effetto dopo il riavvio; i messaggi già archiviati restano.',
        'NOTION_ENABLED': 'Abilita lo strumento notion_ask: ricerche in sola lettura sul workspace Notion tramite il Custom Agent. Richiede token e ID verificati al salvataggio.',
        'NOTION_API_KEY': 'Token della connessione interna Notion con «View sessions and interact with agents» e solo il Custom Agent in Agent access; vuoto mantiene quello salvato.',
        'NOTION_AGENT_ID': 'ID del Custom Agent Notion (tipo custom_agent, stato active); verificato con Notion al salvataggio.',
        'GCAL_ENABLED': 'Abilita Google Calendar con service account IAM; nessun OAuth utente.',
        'GCAL_CALENDAR_ID': 'ID esplicito del calendario condiviso con client_email, permesso di modifica; non usare primary.',
        'GCAL_EVENT_DURATION': 'Durata predefinita degli appuntamenti, in minuti.',
        'GCAL_SERVICE_ACCOUNT_JSON': 'Contenuto JSON della chiave IAM, non un percorso. Salvato privatamente con permessi 600; vuoto mantiene la chiave.',
        'GCAL_SERVICE_ACCOUNT_FILE': 'Percorso della chiave IAM; vuoto usa il file predefinito nei dati persistenti.',
        'TIMEZONE': 'Fuso IANA per appuntamenti e riepilogo, ad esempio Europe/Rome (gestisce l’ora legale).',
        'LANGUAGE': 'Lingua dell’assistente, ad esempio it per italiano o en per inglese.',
        'DIGEST_TIME': 'Ora locale HH:MM del riepilogo delle ultime 24 ore e del piano delle prossime 48 ore dalle attività aperte; richiede il bot Telegram.',
        'POLL_INTERVAL': 'Secondi tra i controlli email; 300 corrisponde a 5 minuti.',
        'INVOICE_DIR': 'Cartella fatture: <entità>/<aaaa-mm>/<nome>.pdf; vuoto usa il percorso predefinito.',
        'DATA_DIR': 'Cartella dati persistenti per appuntamenti e note; vuoto usa il percorso predefinito.',
        'P_SECRETARY_CONFIG_FILE': 'Percorso del file di configurazione privato scelto dal deployment.',
        'DASHBOARD_CREDENTIAL_FILE': 'File privato delle credenziali dashboard; non contiene la password Linux.',
        'DASHBOARD_BIND': 'Indirizzo di ascolto; 0.0.0.0 espone sulla rete. HTTP solo su reti fidate.',
        'DASHBOARD_PORT': 'Porta HTTP dashboard; le porte privilegiate richiedono configurazione esterna.',
        'DASHBOARD_HOSTNAME': 'Nome host della dashboard, ad esempio p-secretary.local.',
        'DASHBOARD_COOKIE_SECURE': 'true solo con accesso HTTPS; false per HTTP locale.',
        'WA_CHROMIUM_PATH': 'Percorso del Chromium di sistema usato dal ponte WhatsApp.',
    }
    result = {'type': 'password' if secret(key) else 'text',
              'description': SECRETARY_FIELDS.get(key, AGENT_FIELDS.get(key, BUDGET_FIELDS.get(key, descriptions.get(key, ''))))}
    if key in SECRETARY_FIELDS:
        result.update(supported=True, status='supported')
    if key == 'SECRETARY_MODE':
        result.update(type='select', options=['observe', 'active'])
    if key == 'EFFECT_AUDIT_RETENTION_DAYS':
        result.update(type='number', min=0, max=3650, step=1, supported=True)
    if key == 'AGENT_NOTIFY_MAX_PER_DAY':
        result.update(type='number', min=0, max=100, step=1, supported=True)
    if key in BUDGET_FIELDS:
        result.update(type='number', min=1,
                      max=65536 if key == 'AI_BUDGET_MAX_OUTPUT_TOKENS' else 2147483647,
                      step=1, supported=_budget_supported())
    if key == 'AI_BUDGET_DAILY_COST':
        result.update(type='text', supported=_budget_supported())
    email = re.fullmatch(r'EMAIL\d+_(.+)', key)
    invoice = re.fullmatch(r'INVOICE_ENTITY\d+_(.+)', key)
    if email:
        suffix = email[1]
        result['description'] = {
            'NAME': 'Nome riconoscibile della casella (fino a 50 account).',
            'PROVIDER': 'Preset server: Gmail usa password per app; Outlook personale e Microsoft 365 richiedono OAuth da terminale; OVH usa la password casella; custom richiede gli host.',
            'ADDRESS': 'Indirizzo email completo della casella.',
            'PASSWORD': 'Password per app Gmail o password casella OVH/custom; Microsoft richiede OAuth, non password.',
            'AUTH': 'basic = password; oauth2 = autorizzazione Microsoft con python -m secretary.setup.ms_auth da terminale. Vuoto usa il preset provider.',
            'CLIENT_ID': 'ID applicazione OAuth Microsoft personalizzata, facoltativo; non un client secret.',
            'IMAP_HOST': 'Host IMAP senza https://; omettere per usare il preset. Host custom verificabili solo da terminale.',
            'SMTP_HOST': 'Host SMTP senza https://; omettere per usare il preset. Host custom verificabili solo da terminale.',
            'IMAP_PORT': 'Porta IMAP intera; preset 993 (IMAPS). Verifica web solo sulla porta 993.',
            'SMTP_PORT': 'Porta SMTP intera; preset 587 (STARTTLS). Verifica web su 465 TLS o 587 STARTTLS.',
        }[suffix]
        if suffix == 'ADDRESS':
            result['type'] = 'email'
        if suffix in ('PROVIDER', 'AUTH'):
            result.update(type='select', options=list(config._EMAIL_PRESETS) if suffix == 'PROVIDER' else ['basic', 'oauth2'])
    if invoice:
        result['description'] = {
            'NAME': 'Nome cartella usato dall’AI per intestare le fatture; solo lettere ASCII, cifre, trattini e underscore. Massimo 4 entità.',
            'VAT': 'Partita IVA come indizio per l’AI; formato consigliato IT + 11 cifre. È testo: conserva zeri iniziali e prefisso.',
            'DESC': 'Descrizione dell’entità per aiutare l’AI a classificare le fatture.',
        }[invoice[1]]
    if key in ('GCAL_ENABLED', 'WHATSAPP_ENABLED', 'TELEGRAM_USER_ENABLED', 'DASHBOARD_COOKIE_SECURE', 'NOTION_ENABLED'):
        result.update(type='select', options=['true', 'false'])
    if key.endswith(('_PORT', '_LIMIT', '_INTERVAL', '_DURATION')) or key == 'TELEGRAM_API_ID':
        result.update(type='number', min=1, max=65535, step=1)
        if key == 'TELEGRAM_API_ID':
            result.pop('max')
    if key == 'DIGEST_TIME':
        result['type'] = 'time'
    if key == 'GCAL_SERVICE_ACCOUNT_JSON':
        result['type'] = 'textarea'
    return result


def schema():
    groups = {
        'AI · limiti dei Worker (chiamate, token, concorrenza)': [key for key in BUDGET_FIELDS if key != 'AI_BUDGET_DAILY_COST'],
        'AI · tetto di spesa in denaro (opzionale)': ['AI_BUDGET_DAILY_COST'],
        'Nome assistente e modelli AI': ['AGENT_NAME', 'OPENROUTER_API_KEY', 'AGENT_MODEL', 'AGENT_WEB_MODEL',
                  'AGENT_NOTIFY_MAX_PER_DAY', 'OPENROUTER_MODEL', 'OPENROUTER_AUDIO_MODEL'],
        'Database': ['DATABASE_URL'],
        'Telegram e WhatsApp': ['TELEGRAM_BOT_TOKEN', *INTERACTIVE,
            'TELEGRAM_USER_DIALOG_LIMIT', 'TELEGRAM_USER_MESSAGE_LIMIT', 'TELEGRAM_USER_RECOVERY_INTERVAL',
            'TELEGRAM_EXCLUDED_CHATS', 'WHATSAPP_EXCLUDED_CHATS'],
        'Calendar': ['GCAL_ENABLED', 'GCAL_CALENDAR_ID', 'GCAL_EVENT_DURATION', 'GCAL_SERVICE_ACCOUNT_JSON'],
        'Notion (sola lettura)': ['NOTION_ENABLED', 'NOTION_API_KEY', 'NOTION_AGENT_ID'],
        'Preferenze': ['TIMEZONE', 'LANGUAGE', 'DIGEST_TIME', 'POLL_INTERVAL'],
        'Segretario · modalità e retention': list(SECRETARY_FIELDS),
        'Deployment (sola lettura)': sorted(DEPLOYMENT),
    }
    for i in range(1, 51):
        groups[f'Email {i}'] = [f'EMAIL{i}_{s}' for s in (
            'NAME', 'PROVIDER', 'ADDRESS', 'PASSWORD', 'AUTH', 'CLIENT_ID',
            'IMAP_HOST', 'IMAP_PORT', 'SMTP_HOST', 'SMTP_PORT')]
    for i in range(1, 5):
        groups[f'Entità fatture {i}'] = [f'INVOICE_ENTITY{i}_{s}' for s in ('NAME', 'VAT', 'DESC')]
    return {key: {'group': group, 'secret': secret(key), **field_metadata(key)}
            for group, keys in groups.items() for key in keys}


def secret(key):
    if key in BUDGET_FIELDS:
        return False
    return key in ('DATABASE_URL', 'GCAL_SERVICE_ACCOUNT_JSON') or any(s in key for s in ('PASSWORD', 'TOKEN', 'API_KEY', 'API_HASH'))


def readonly(key):
    if key in BUDGET_FIELDS and not _budget_supported():
        return 'Modulo ai_budget non installato; impostazione non applicabile.'
    if key in DEPLOYMENT or key in config.DEPLOYMENT_ENV_KEYS:
        return 'Imposto dal deployment/ambiente: modificare esternamente e ricreare il container; il file non prevale su Docker.'
    return INTERACTIVE.get(key, '')


def revision(raw):
    return hmac.new(_revision_key, raw, hashlib.sha256).hexdigest()


def raw_file():
    return config.CONFIG_FILE.read_bytes() if config.CONFIG_FILE.exists() else b''


def effective(saved):
    # os.environ contains the startup dotenv too; only inherited keys override
    # the latest file, otherwise a second edit would validate stale credentials.
    return {**saved, **{k: os.environ[k] for k in config.DEPLOYMENT_ENV_KEYS if k in os.environ}}


def describe():
    with _lock, FileLock(str(config.CONFIG_FILE) + '.lock'):
        raw = raw_file()
        values = effective(read_env(config.CONFIG_FILE))
        fields = []
        for key, spec in schema().items():
            value = values.get(key, '')
            if key == 'GCAL_SERVICE_ACCOUNT_JSON':
                from secretary.connectors import gcal
                value = 'configured' if Path(values.get('GCAL_SERVICE_ACCOUNT_FILE') or gcal.service_account_file()).is_file() else ''
            fields.append(dict(spec, key=key, configured=bool(value),
                               value=None if spec['secret'] else value, readonly=readonly(key)))
        return {'revision': revision(raw), 'fields': fields,
                'apply': ('Dopo il salvataggio il container si riavvia automaticamente. '
                          if automatic_restart() else 'Runtime non Docker: riavvio manuale necessario. ')
                         + 'Nessuna migrazione DB. Autorizzazioni QR/OTP/OAuth solo da terminale.'}


class Conflict(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirect refused')


def remote_json(url, headers=None, data=None):
    # Fixed vendor HTTPS URLs only. Never follow redirects carrying credentials
    # and never use ambient HTTP proxies for credential verification.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(Request(url, headers=headers or {}, data=data), timeout=10) as response:
        return json.loads(response.read(262145))


def public_host(host, port):
    if not host or not re.fullmatch(r'[A-Za-z0-9.:-]+', host):
        raise ValueError('Host non valido')
    addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
        raise ValueError('La verifica web consente solo host pubblici; destinazioni locali/private richiedono il terminale.')
    return sorted(addresses)[0]


def verify_database(uri):
    import psycopg
    parsed = urlsplit(uri)
    if parsed.scheme not in ('postgres', 'postgresql') or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError('Usare URI PostgreSQL senza opzioni query; DB privati/opzioni TLS avanzate dal terminale.')
    # Pin the vetted IP; libpq must not resolve the hostname again (DNS rebinding).
    address = public_host(parsed.hostname, parsed.port or 5432)
    with psycopg.connect(uri, hostaddr=address, connect_timeout=8,
                         options='-c default_transaction_read_only=on -c statement_timeout=5000',
                         sslmode='verify-full', sslrootcert='/etc/ssl/certs/ca-certificates.crt') as conn:
        if conn.execute('SELECT 1').fetchone() != (1,):
            raise ValueError('Database non verificato')


def verify_email(values, prefix):
    provider = values.get(prefix + 'PROVIDER', 'custom')
    if provider not in config._EMAIL_PRESETS:
        raise ValueError('Provider email non valido')
    ih, ip, sh, sp, auth = config._EMAIL_PRESETS[provider]
    auth = values.get(prefix + 'AUTH') or auth
    if auth != 'basic':
        raise ValueError('Email OAuth richiede python -m secretary.setup.ms_auth nel terminale; nessuna autorizzazione simulata.')
    address, password = values.get(prefix + 'ADDRESS', ''), values.get(prefix + 'PASSWORD', '')
    if not address or not password:
        raise ValueError('Indirizzo e password email obbligatori')
    ih, sh = values.get(prefix + 'IMAP_HOST') or ih, values.get(prefix + 'SMTP_HOST') or sh
    ip, sp = int(values.get(prefix + 'IMAP_PORT') or ip), int(values.get(prefix + 'SMTP_PORT') or sp)
    if ip != 993 or sp not in (465, 587):
        raise ValueError('Verifica email solo IMAPS 993 e SMTP TLS 465/587')
    # Only fixed provider endpoints: custom host probes cannot safely pin IMAP
    # and SMTP DNS using stdlib without replacing transport internals.
    if (ih, sh) not in {(p[0], p[2]) for p in config._EMAIL_PRESETS.values() if p[0]}:
        raise ValueError('Host email personalizzati: verifica dal terminale (protezione SSRF).')
    context = ssl.create_default_context()
    with imaplib.IMAP4_SSL(ih, ip, ssl_context=context, timeout=10) as conn:
        conn.login(address, password)
        conn.logout()
    client = smtplib.SMTP_SSL(sh, sp, timeout=10, context=context) if sp == 465 else smtplib.SMTP(sh, sp, timeout=10)
    with client as conn:
        if sp == 587:
            conn.ehlo()
            conn.starttls(context=context)
            conn.ehlo()
        conn.login(address, password)


def verify_calendar(values, raw):
    from secretary.connectors import gcal
    import time
    if raw:
        sa = gcal.validate_service_account(json.loads(raw))
    else:
        path = Path(values.get('GCAL_SERVICE_ACCOUNT_FILE') or gcal.service_account_file())
        sa = gcal.validate_service_account(json.loads(path.read_text()))
    calendar = values.get('GCAL_CALENDAR_ID', '')
    if not calendar or calendar.lower() == 'primary':
        raise ValueError('Serve ID calendario esplicito')
    # Isolated candidate signing: do NOT touch the live gcal token cache or config.
    now = int(time.time())
    claims = {'iss': sa['client_email'], 'scope': gcal.SCOPES, 'aud': gcal.TOKEN_URL, 'iat': now, 'exp': now + 3600}
    segments = gcal._b64url(b'{"alg":"RS256","typ":"JWT"}') + '.' + gcal._b64url(json.dumps(claims).encode())
    n, d = gcal._parse_rsa_private_key(sa['private_key'])
    assertion = segments + '.' + gcal._b64url(gcal._rsa_sign_sha256(segments.encode(), n, d))
    token = remote_json('https://oauth2.googleapis.com/token',
                        {'Content-Type': 'application/x-www-form-urlencoded'},
                        urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion}).encode())['access_token']
    headers = {'Authorization': 'Bearer ' + token}
    remote_json('https://www.googleapis.com/calendar/v3/calendars/' + quote(calendar, safe=''), headers)
    # CalendarList exposes accessRole without creating events or changing ACLs.
    entry = remote_json('https://www.googleapis.com/calendar/v3/users/me/calendarList/' + quote(calendar, safe=''), headers)
    if entry.get('accessRole') not in ('writer', 'owner'):
        raise ValueError('Calendar richiede accesso writer/owner e presenza in CalendarList')
    return sa


def notion_active_agents(key):
    """Custom Agent attivi visibili al token. Usata da verify_notion e dalla diagnostica manuale."""
    body = {'filter': {'and': [{'property': 'agent_type', 'string': {'equals': 'custom_agent'}},
                               {'property': 'status', 'status': {'in': ['active']}}]},
            'page_size': 100}
    headers = {'Authorization': 'Bearer ' + key, 'Notion-Version': notion_agent.API_VERSION,
               'Content-Type': 'application/json'}
    return remote_json(notion_agent.API_ROOT + '/agents/query', headers, json.dumps(body).encode())


def verify_notion(values):
    key, agent = values.get('NOTION_API_KEY', ''), values.get('NOTION_AGENT_ID', '')
    if not key or not agent:
        raise ValueError('Notion richiede token e ID del Custom Agent')
    try:
        result = notion_active_agents(key)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError('Token Notion non accettato: verificare la connessione interna') from None
        raise ValueError(f'Notion ha rifiutato la verifica (HTTP {exc.code})') from None
    agents = result.get('results') if isinstance(result, dict) else None
    if not isinstance(agents, list) or not any(
            isinstance(item, dict) and str(item.get('id', '')).lower() == agent.lower() for item in agents):
        raise ValueError('Custom Agent Notion non trovato tra gli agenti attivi della connessione')


def validate(changes, values):
    for key, value in changes.items():
        spec = field_metadata(key)
        if key in BUDGET_FIELDS and key != 'AI_BUDGET_DAILY_COST' and (not value.isascii() or not value.isdecimal()
                                    or not 1 <= int(value) <= spec['max']):
            raise ValueError('Limite AI fuori intervallo')
        if key == 'AI_BUDGET_DAILY_COST':
            from decimal import Decimal, InvalidOperation
            try:
                daily_cost = Decimal(value)
            except (InvalidOperation, ValueError):
                raise ValueError('Tetto di spesa non valido: usare un importo decimale, ad esempio 5.00') from None
            if not daily_cost.is_finite() or daily_cost < 0 or daily_cost > Decimal('1e9'):
                raise ValueError('Tetto di spesa fuori intervallo (0 – 1e9); 0 disattiva il tetto monetario')
        if key in config.CHAT_ID_PATTERNS:
            items = [item.strip() for item in value.split(',') if item.strip()]
            if len(items) > 200 or any(not config.CHAT_ID_PATTERNS[key].fullmatch(item) for item in items):
                raise ValueError('Elenco chat escluse non valido: ID separati da virgole, massimo 200')
        if key == 'AGENT_NOTIFY_MAX_PER_DAY' and (not value.isdecimal() or not 0 <= int(value) <= 100):
            raise ValueError('Tetto notifiche fuori intervallo 0–100')
        if key == 'NOTION_AGENT_ID' and value and not re.fullmatch(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}', value):
            raise ValueError('ID agente Notion non valido: deve essere un UUID')
        if key == 'EFFECT_AUDIT_RETENTION_DAYS' and (not value.isdecimal() or not 0 <= int(value) <= 3650):
            raise ValueError('Retention audit fuori intervallo 0–3650')
        if key == 'SECRETARY_MODE' and value not in ('observe', 'active'):
            raise ValueError('Modalità segretario non valida')
        if (spec['type'] == 'select' and value not in spec['options']
            and not (re.fullmatch(r'EMAIL\d+_AUTH', key) and value == '')):
            raise ValueError('Scelta non valida: ' + key)
        if not valid_preference(key, value):
            raise ValueError('Preferenza non valida: ' + key)
        if key in ('GCAL_ENABLED',) and value not in ('true', 'false'):
            raise ValueError('Booleano non valido')
        if key.endswith(('_PORT', '_LIMIT', '_INTERVAL', '_DURATION')) or key == 'POLL_INTERVAL':
            if not value.isdecimal() or not 1 <= int(value) <= 65535:
                raise ValueError('Intero fuori intervallo: ' + key)
        if re.fullmatch(r'INVOICE_ENTITY[1-4]_NAME', key) and value and not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise ValueError('Nome cartella entità non valido')
    if 'DATABASE_URL' in changes:
        verify_database(values['DATABASE_URL'])
    if 'OPENROUTER_API_KEY' in changes:
        result = remote_json('https://openrouter.ai/api/v1/key', {'Authorization': 'Bearer ' + values['OPENROUTER_API_KEY']})
        if not isinstance(result.get('data'), dict):
            raise ValueError('Chiave AI non verificata')
    if 'TELEGRAM_BOT_TOKEN' in changes:
        token = values['TELEGRAM_BOT_TOKEN']
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
            raise ValueError('Token bot non valido')
        # Owner authorization is immutable via web. Never getUpdates/sendMessage.
        if not values.get('TELEGRAM_CHAT_ID'):
            raise ValueError('Configurare prima il proprietario con python -m secretary.setup.telegram')
        result = remote_json('https://api.telegram.org/bot' + token + '/getMe')
        if not result.get('ok') or not result.get('result', {}).get('is_bot'):
            raise ValueError('Bot non verificato')
        # A different bot identity requires explicit pairing via the terminal.
        # getMe alone cannot establish ownership of a newly selected bot.
        previous = effective(read_env(config.CONFIG_FILE)).get('TELEGRAM_BOT_TOKEN', '')
        if not previous or previous.split(':', 1)[0] != token.split(':', 1)[0]:
            raise ValueError('Cambio identità bot: usare python -m secretary.setup.telegram per autorizzare il proprietario')
    for prefix in sorted({m.group(0) for key in changes if (m := re.match(r'EMAIL\d+_', key))}):
        verify_email(values, prefix)
    if values.get('NOTION_ENABLED') == 'true' and {'NOTION_ENABLED', 'NOTION_API_KEY', 'NOTION_AGENT_ID'} & changes.keys():
        verify_notion(values)
    if 'GCAL_SERVICE_ACCOUNT_JSON' in changes or 'GCAL_CALENDAR_ID' in changes or changes.get('GCAL_ENABLED') == 'true':
        return verify_calendar(values, changes.get('GCAL_SERVICE_ACCOUNT_JSON'))
    return None


def save(payload):
    global _restart_at
    if not isinstance(payload, dict) or not isinstance(payload.get('changes'), dict) or not isinstance(payload.get('revision'), str):
        raise ValueError('Richiesta non valida')
    changes = payload['changes']
    allowed = schema()
    if not changes or len(changes) > 100:
        raise ValueError('Inviare da 1 a 100 campi modificati')
    for key, value in changes.items():
        if key not in allowed or readonly(key):
            raise ValueError('Campo non modificabile: ' + (key if key in allowed else 'sconosciuto'))
        if not isinstance(value, str) or len(value) > (131072 if key == 'GCAL_SERVICE_ACCOUNT_JSON' else 4096):
            raise ValueError('Valore non valido')
        if key != 'GCAL_SERVICE_ACCOUNT_JSON' and any(ord(c) < 32 for c in value):
            raise ValueError('Caratteri di controllo vietati')
        if secret(key) and not value:
            raise ValueError('Segreto vuoto: omettere il campo per mantenerlo')
    # Serializes admin requests and wizard writers throughout validation and commit.
    with _lock, FileLock(str(config.CONFIG_FILE) + '.lock'):
        before = raw_file()
        if not hmac.compare_digest(payload['revision'], revision(before)):
            raise Conflict('Configurazione cambiata: ricaricare prima di riprovare')
        saved = read_env(config.CONFIG_FILE)
        values = effective({**saved, **changes})
        sa = validate(changes, values)
        staged = {k: v for k, v in changes.items() if k != 'GCAL_SERVICE_ACCOUNT_JSON'}
        candidate_path = None
        created = False
        try:
            if sa is not None and 'GCAL_SERVICE_ACCOUNT_JSON' in changes:
                if 'GCAL_SERVICE_ACCOUNT_FILE' in config.DEPLOYMENT_ENV_KEYS:
                    raise ValueError('Percorso chiave Calendar imposto dal deployment')
                text = json.dumps(sa, sort_keys=True) + '\n'
                # Outside general file-browser roots: private config directory.
                directory = config.CONFIG_FILE.parent / '.calendar-keys'
                directory.mkdir(mode=0o700, exist_ok=True)
                if directory.is_symlink():
                    raise ValueError('Directory chiavi symlink vietata')
                directory.chmod(0o700)
                candidate_path = directory / (hashlib.sha256(text.encode()).hexdigest() + '.json')
                if candidate_path.is_symlink():
                    raise ValueError('Chiave symlink vietata')
                created = not candidate_path.exists()
                if created:
                    atomic_write(candidate_path, text)
                elif candidate_path.read_text() != text:
                    raise ValueError('Chiave esistente non corrispondente')
                candidate_path.chmod(0o600)
                staged['GCAL_SERVICE_ACCOUNT_FILE'] = str(candidate_path.resolve())
            if raw_file() != before:
                raise Conflict('Configurazione cambiata durante la verifica')
            lines = [line for line in before.decode().splitlines()
                     if line.strip().partition('=')[0].strip() not in staged]
            lines.extend(k + '=' + json.dumps(v, ensure_ascii=False) for k, v in staged.items())
            atomic_write(config.CONFIG_FILE, '\n'.join(lines) + '\n')
        except BaseException:
            if candidate_path and created:
                candidate_path.unlink(missing_ok=True)
            raise
        scheduled = automatic_restart()
        if scheduled and _restart_at is None:
            _restart_at = time.monotonic() + 2
        return {'ok': True, 'restart_required': True, 'applied_live': False,
            'restart_scheduled': scheduled}


def register(app):
    from flask import jsonify, request

    @app.get('/api/configuration')
    def configuration():
        try:
            return jsonify(describe())
        except Exception:
            return jsonify(error='Configurazione non disponibile'), 503

    @app.post('/api/configuration')
    def update():
        try:
            return jsonify(save(request.get_json(silent=True)))
        except Conflict:
            return jsonify(error='Conflitto: ricaricare configurazione e riprovare'), 409
        except Exception:
            # Neither provider error bodies, URLs, candidate values nor private
            # keys enter responses/logs, including exception tracebacks.
            return jsonify(error='Verifica/salvataggio fallito. Nessun valore sostituito. Controllare formato, provider, permessi o usare il setup da terminale per host privati/OAuth.'), 400