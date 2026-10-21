"""Gateway LLM locale tra il runtime Copilot e OpenRouter.

Perché esiste: l'SDK parla con il provider BYOK in modo opaco. Il gateway
(HTTP su loopback, nello stesso processo dell'app) rende OpenRouter l'UNICO
LLM e mantiene tutte le garanzie del progetto:

* la chiave OpenRouter non lascia mai questo processo: il runtime usa un
  bearer monouso per sessione che identifica il job (e il suo lease);
* ogni richiesta passa dal ledger `secretary_ai_usage` (fail-closed), con
  token e costo REALI letti da OpenRouter (l'SDK non li riporta in BYOK);
* `max_tokens` esplicito e parametri di campionamento normalizzati
  (l'SDK non invia max_tokens, che il ledger richiede);
* lease perso, budget esaurito o ledger non disponibile = richiesta rifiutata
  PRIMA di contattare il provider; il motivo è conservato per il chiamante.

Nessun prompt, risposta o credenziale viene scritto nei log.
"""
import http.server
import json
import secrets
import threading
import urllib.error
import urllib.request

from secretary.core import ai_budget, config, jobs

UPSTREAM = 'https://openrouter.ai/api/v1/chat/completions'
MAX_BODY = 12 * 1024 * 1024
# Parametri che l'SDK invia sempre e che molti modelli (soprattutto a
# ragionamento) rifiutano o che restringono inutilmente la scelta dei provider.
DROPPED = ('temperature', 'top_p', 'frequency_penalty', 'presence_penalty',
           'stream', 'stream_options')


class Turn:
    """Contesto di una sessione dell'agente, associato al suo bearer."""

    def __init__(self, context, scope):
        self.context, self.scope = context, scope
        self.token = secrets.token_urlsafe(32)
        self.failure = None          # eccezione che ha fermato il turno (budget, lease…)
        self.model_calls = 0
        self.lock = threading.Lock()


def _upstream(payload):
    """Chiamata HTTPS a OpenRouter. Isolata per poterla sostituire nei test."""
    request = urllib.request.Request(
        UPSTREAM, data=json.dumps(payload).encode(), method='POST', headers={
            'Authorization': f'Bearer {config.OPENROUTER_API_KEY}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'https://p-secretary.local', 'X-Title': 'p-secretary'})
    try:
        with urllib.request.urlopen(request, timeout=config.AGENT_LLM_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _error(status, message):
    return status, json.dumps({'error': {'message': message, 'type': 'gateway_error'}}).encode()


class Gateway:
    def __init__(self, upstream=None):
        self._turns = {}
        self._lock = threading.Lock()
        self._server = None
        self.upstream = upstream or _upstream

    # ------------------------------------------------------------- turni
    def open_turn(self, context, scope):
        if scope not in ai_budget.AGENT_SCOPES:
            raise ValueError('Unknown agent scope')
        turn = Turn(context, scope)
        with self._lock:
            self._turns[turn.token] = turn
        return turn

    def close_turn(self, turn):
        with self._lock:
            self._turns.pop(turn.token, None)

    # ----------------------------------------------------------- richieste
    def handle(self, token, body):
        """Ritorna (status, bytes). Mai solleva: ogni esito è una risposta HTTP."""
        with self._lock:
            turn = self._turns.get(token)
        if turn is None:
            return _error(401, 'Sessione dell\'agente non riconosciuta')
        try:
            payload = json.loads(body)
            if type(payload) is not dict or type(payload.get('messages')) is not list:
                raise ValueError
        except (ValueError, RecursionError):
            return _error(400, 'Richiesta non valida')
        for name in DROPPED:
            payload.pop(name, None)
        payload.pop('plugins', None)
        payload.pop('provider', None)
        limit = config.AGENT_MAX_OUTPUT_TOKENS
        requested = payload.pop('max_completion_tokens', None)
        asked = payload.get('max_tokens', requested)
        payload['max_tokens'] = min(asked, limit) if type(asked) is int and asked > 0 else limit
        payload['usage'] = {'include': True}
        try:
            with jobs.bind(turn.context):
                with ai_budget.accounted(payload, scope=turn.scope) as accounting:
                    status, data = self.upstream(payload)
                    if accounting is not None and status == 200:
                        try:
                            accounting['response'] = json.loads(data)
                        except ValueError:
                            accounting['response'] = {}
            with turn.lock:
                turn.model_calls += 1
            return status, data
        except ai_budget.BudgetExceeded as exc:
            turn.failure = exc
            return _error(429, 'Budget AI esaurito')
        except ai_budget.BudgetUnavailable as exc:
            turn.failure = exc
            return _error(503, 'Contabilita AI non disponibile')
        except jobs.LeaseLost as exc:
            turn.failure = exc
            return _error(409, 'Esecuzione non piu corrente')
        except Exception as exc:  # rete, TLS, timeout verso il provider
            turn.failure = exc
            return _error(502, 'Provider AI non raggiungibile')

    # -------------------------------------------------------------- server
    def start(self):
        with self._lock:
            if self._server is not None:
                return self.url
            gateway = self

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = 'HTTP/1.1'

                def log_message(self, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get('content-length') or 0)
                    if self.path.rstrip('/') != '/v1/chat/completions' or not 0 < length <= MAX_BODY:
                        status, data = _error(404 if length else 400, 'Percorso o corpo non ammesso')
                    else:
                        auth = self.headers.get('authorization', '')
                        token = auth[7:] if auth.lower().startswith('bearer ') else ''
                        status, data = gateway.handle(token, self.rfile.read(length))
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            class Server(http.server.ThreadingHTTPServer):
                daemon_threads = True

            self._server = Server(('127.0.0.1', 0), Handler)
            threading.Thread(target=self._server.serve_forever, name='agent-gateway', daemon=True).start()
            return self.url

    @property
    def url(self):
        return f'http://127.0.0.1:{self._server.server_address[1]}/v1'

    def stop(self):
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()


_default = None
_default_lock = threading.Lock()


def default():
    global _default
    with _default_lock:
        if _default is None:
            _default = Gateway()
        return _default
