"""Helper condivisi dai test (nessun TestCase qui: non viene mai scoperto né rieseguito).

* ``completion``: risposta finta di OpenRouter come context manager di urlopen.
* ``job_context``: contesto d'esecuzione di un job senza avviare un Worker.
* ``PostgreSQLFixture``: schema isolato per test su un database ``*_test``, con
  ``db.init()`` reale, senza rete né worker. Riusa AgentPostgresCase.
"""
import json
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import jobs


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
    import test_agent_postgres
    return test_agent_postgres.AgentPostgresCase
