"""Client HTTP per le Notion Agent APIs (public beta), usato in SOLA LETTURA (R1).

Jarvis non parla con la Data API: delega una domanda autosufficiente al Custom
Agent configurato nel workspace e ne legge l'esito. Solo urllib: nessuna
dipendenza nuova. Il token vive nella configurazione del processo e non finisce
mai in log, journal o argomenti degli strumenti.

Regole del trasporto:
* host fisso api.notion.com, HTTPS, niente redirect;
* la POST di creazione sessione NON si ripete mai automaticamente: un esito
  ambiguo (timeout, connessione caduta, 5xx) solleva NotionAmbiguous e il
  chiamante marca la delega 'uncertain' invece di creare una seconda sessione;
* GET e query eventi sono idempotenti: il workflow può ritentarli;
* il session_id restituito dal provider è validato prima di comporre gli URL.
"""
import json
import re
import urllib.error
import urllib.request

from secretary.core import config

API_ROOT = 'https://api.notion.com/v1'
API_VERSION = '2026-03-11'
MAX_EVENT_PAGES = 5
MAX_ANSWER_CHARS = 16000
MAX_LINKS = 10
_SESSION_ID = re.compile(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}')
_NOTION_URL = re.compile(r'https://(?:www\.)?notion\.so/\S+')


class NotionError(RuntimeError):
    """Errore dell'API con risposta HTTP ricevuta (codice bounded, mai payload)."""

    def __init__(self, status, code):
        super().__init__(f'Notion API: HTTP {status} {code}')
        self.status, self.code = status, code
        self.retryable = status in (429, 500, 502, 503, 504, 529)


class NotionAmbiguous(RuntimeError):
    """Esito non verificabile (timeout/rete/risposta malformata): la richiesta PUÒ essere arrivata."""


def _request(method, path, body=None, *, opener=None):
    opener = opener or urllib.request.urlopen
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API_ROOT + path, data=data, method=method, headers={
        'Authorization': f'Bearer {config.NOTION_API_KEY}',
        'Notion-Version': API_VERSION,
        'Content-Type': 'application/json',
    })
    try:
        with opener(req, timeout=config.NOTION_HTTP_TIMEOUT_SECONDS) as resp:
            text = resp.read().decode()
            payload = json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as exc:
        code = 'http_error'
        try:
            detail = json.loads(exc.read().decode())
            code = str((detail.get('error') or {}).get('code') or detail.get('code') or code)[:80]
        except Exception:
            pass
        raise NotionError(exc.code, code) from None
    except Exception as exc:
        raise NotionAmbiguous(type(exc).__name__) from None
    if not isinstance(payload, dict):
        raise NotionAmbiguous('UnexpectedPayload')
    return payload


def _check_id(session_id):
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise NotionAmbiguous('InvalidSessionId')


def create_session(question, *, opener=None):
    """Nuova sessione del Custom Agent. MAI ritentata dal trasporto."""
    result = _request('POST', '/sessions',
                      {'agent_id': config.NOTION_AGENT_ID, 'message': question}, opener=opener)
    _check_id(result.get('id'))
    return result


def follow_up(session_id, question, *, opener=None):
    """Messaggio successivo dentro una sessione esistente. MAI ritentato dal trasporto.

    Lo sceglie il server quale sessione proseguire: mai un ID fornito dal modello.
    """
    _check_id(session_id)
    result = _request('POST', '/sessions',
                      {'session_id': session_id, 'message': question}, opener=opener)
    _check_id(result.get('id'))
    return result


def retrieve_session(session_id, *, opener=None):
    """Stato della sessione (GET idempotente: il workflow può riprovarla)."""
    _check_id(session_id)
    return _request('GET', f'/sessions/{session_id}', opener=opener)


def agent_messages(session_id, *, after_sequence=0, opener=None):
    """Testo dei messaggi `agent.message` nuovi (sequence > after_sequence).

    Ritorna (testo concatenato e bounded, ultima sequence vista, link Notion
    trovati nel testo). Gli eventi `agent.thinking` e i tool output interni
    non vengono mai restituiti.
    """
    _check_id(session_id)
    cursor, events = None, []
    for _ in range(MAX_EVENT_PAGES):
        body = {'filter': {'property': 'type', 'event_type': {'equals': 'agent.message'}},
                'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        page = _request('POST', f'/sessions/{session_id}/events/query', body, opener=opener)
        events.extend(e for e in (page.get('results') or []) if isinstance(e, dict))
        if not page.get('has_more'):
            break
        cursor = page.get('next_cursor')
        if not cursor:
            break
    texts, last = [], after_sequence
    for event in sorted(events, key=lambda e: e.get('sequence') or 0):
        sequence = event.get('sequence') or 0
        if event.get('type') != 'agent.message' or sequence <= after_sequence:
            continue
        parts = event.get('content')
        if isinstance(parts, list):
            texts.append(''.join(str(p.get('text') or '') for p in parts
                                 if isinstance(p, dict) and p.get('type') == 'text'))
        last = max(last, sequence)
    answer = '\n'.join(t for t in texts if t.strip())[:MAX_ANSWER_CHARS]
    links = list(dict.fromkeys(_NOTION_URL.findall(answer)))[:MAX_LINKS]
    return answer, last, links
