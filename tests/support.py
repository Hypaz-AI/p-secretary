"""Helper condivisi dai test (nessun TestCase qui: non viene mai scoperto né rieseguito).

* ``completion``: risposta finta di OpenRouter come context manager di urlopen.
* ``job_context``: contesto d'esecuzione di un job senza avviare un Worker.
* ``PostgreSQLFixture``: schema isolato per test su un database ``*_test``, con
  ``db.init()`` reale, senza rete né worker. Riusa AgentPostgresCase.
"""
import importlib
import json
import re
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from secretary.core import jobs

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / 'secretary' / 'dashboard' / 'static'


@contextmanager
def fake_module(dotted, module):
    """Sostituisce un modulo del pacchetto per la durata del blocco.

    `from pacchetto import modulo` legge prima l'attributo del pacchetto padre e solo poi
    ``sys.modules``: occorre quindi toccare entrambi.
    """
    parent_name, _, leaf = dotted.rpartition('.')
    parent = importlib.import_module(parent_name)
    missing = object()
    previous = getattr(parent, leaf, missing)
    with patch.dict(sys.modules, {dotted: module}):
        setattr(parent, leaf, module)
        try:
            yield module
        finally:
            if previous is missing:
                delattr(parent, leaf)
            else:
                setattr(parent, leaf, previous)


@contextmanager
def fake_modules(modules):
    """Come `fake_module`, per piu' moduli insieme (nome puntato -> sostituto)."""
    with ExitStack() as stack:
        for dotted, module in modules.items():
            stack.enter_context(fake_module(dotted, module))
        yield


def _dockerignore_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
        elif pattern.startswith('**', i):
            out.append('.*')
            i += 2
        elif pattern[i] == '*':
            out.append('[^/]*')
            i += 1
        elif pattern[i] == '?':
            out.append('[^/]')
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile(''.join(out))


def docker_context_includes(rules, path):
    """Modello di `.dockerignore`: vince l'ultima regola che corrisponde al percorso o a un suo genitore."""
    parts = path.split('/')
    candidates = ['/'.join(parts[:count]) for count in range(1, len(parts) + 1)]
    excluded = False
    for rule in rules:
        negate = rule.startswith('!')
        regex = _dockerignore_regex((rule[1:] if negate else rule).strip('/'))
        if any(regex.fullmatch(candidate) for candidate in candidates):
            excluded = not negate
    return not excluded


def completion(content, finish_reason='stop'):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        'choices': [{'finish_reason': finish_reason,
                     'message': {'content': content}}]
    }).encode()
    return response


@contextmanager
def job_context(job_id=1, payload=None, token='offline-token', kind='offline'):
    previous = jobs.execution_context()
    context = jobs.ExecutionContext(job_id, token, kind, payload or {}, time.monotonic() + 60)
    jobs._local.execution = context
    try:
        yield context
    finally:
        jobs._local.execution = previous


def postgres_fixture():
    """Restituisce la classe di base (importata solo al bisogno: evita doppia raccolta)."""
    from tests.agent import test_agent_postgres
    return test_agent_postgres.AgentPostgresCase
