"""Diagnostica manuale della connessione Notion (Custom Agent, sola lettura).

Esegue le stesse chiamate del salvataggio in dashboard, così l'esito è quello reale.
Non stampa mai il token e non salva nulla.

Uso (dalla root del repository, con la stessa configurazione dell'app):
    python -m secretary.maintenance.debug_notion [--agent-id UUID]

Il token e l'ID vengono chiesti a terminale; invio mantiene il valore configurato in
NOTION_API_KEY / NOTION_AGENT_ID (env o .env). Il token non viene mai mostrato né salvato.
"""
import argparse
import getpass
import re
import sys
from urllib.error import HTTPError, URLError

from secretary.core import config
from secretary.dashboard import admin_config

_UUID = re.compile(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}')
MAX_ERROR_CHARS = 400


def _say(label, value=''):
    print(f'{label:<28} {value}'.rstrip())


def _prompt(label, current, secret=False):
    ask = getpass.getpass if secret else input
    hint = 'invio = valore configurato' if current else 'obbligatorio'
    return ask(f'{label} [{hint}]: ').strip() or current


def _describe_token(token):
    shape = 'prefisso ntn_' if token.startswith('ntn_') else (
        'prefisso secret_' if token.startswith('secret_') else 'prefisso non riconosciuto')
    issues = []
    if token != token.strip():
        issues.append('spazi ai bordi')
    if any(ch in token for ch in '"\''):
        issues.append('virgolette nel valore')
    _say('token', f'{len(token)} caratteri, {shape}' + (f'; ATTENZIONE: {", ".join(issues)}' if issues else ''))


def _http_error_detail(exc):
    try:
        raw = exc.read().decode('utf-8', 'replace')
    except Exception:
        raw = ''
    return raw[:MAX_ERROR_CHARS] if raw else '(corpo vuoto)'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Diagnostica connessione Notion (sola lettura).')
    parser.add_argument('--agent-id', help='UUID del Custom Agent: salta la domanda sull\'ID')
    args = parser.parse_args(argv)

    try:
        token = _prompt('NOTION_API_KEY (non salvato)', config.NOTION_API_KEY, secret=True)
        agent = args.agent_id or _prompt('NOTION_AGENT_ID', config.NOTION_AGENT_ID)
    except (KeyboardInterrupt, EOFError):
        print()
        return 130

    print('== 1. Valori di input ==')
    if not token:
        _say('token', 'MANCANTE')
        return 2
    _describe_token(token)
    _say('agent id', 'UUID valido' if _UUID.fullmatch(agent) else f'NON UUID ({agent!r})')
    if not _UUID.fullmatch(agent):
        return 2

    print('== 2. Chiamata /v1/agents/query (come in dashboard) ==')
    try:
        result = admin_config.notion_active_agents(token)
    except HTTPError as exc:
        _say('esito', f'HTTP {exc.code} {exc.reason}')
        _say('risposta Notion', _http_error_detail(exc))
        return 1
    except URLError as exc:
        _say('esito', f'rete/TLS: {exc.reason}')
        return 1
    except (TimeoutError, OSError, ValueError) as exc:
        _say('esito', f'errore: {type(exc).__name__}: {exc}')
        return 1

    agents = result.get('results') if isinstance(result, dict) else None
    if not isinstance(agents, list):
        _say('esito', f'risposta inattesa: chiavi {sorted(result) if isinstance(result, dict) else type(result).__name__}')
        return 1
    _say('esito', f'HTTP 200, {len(agents)} Custom Agent attivi visibili al token')
    for item in agents[:10]:
        if isinstance(item, dict):
            _say('  -', f"{item.get('id')}  type={item.get('agent_type')} status={item.get('status')}")
    if len(agents) > 10:
        _say('  ...', f'altri {len(agents) - 10} non mostrati')

    print('== 3. Verifica come al salvataggio (verify_notion) ==')
    try:
        admin_config.verify_notion({'NOTION_API_KEY': token, 'NOTION_AGENT_ID': agent})
    except ValueError as exc:
        _say('esito', f'FALLITA: {exc}')
        if not any(str(item.get('id', '')).lower() == agent.lower() for item in agents if isinstance(item, dict)):
            _say('motivo probabile', 'l\'ID non è tra gli agenti attivi della connessione: '
                                     'controllare "Agent access" della connessione interna')
        return 1
    _say('esito', 'OK: token e Custom Agent verificati')
    return 0


if __name__ == '__main__':
    sys.exit(main())
