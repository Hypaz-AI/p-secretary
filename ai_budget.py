"""Bounded completions and opt-in persistent Worker accounting.

Bootstrap must call migrate(conn) after the native/jobs migrations. No lazy DDL
or startup hook is installed here. Every request_content call inside a jobs
execution context fails CLOSED if the ledger/DB/fence is unavailable. Legacy
direct/local calls retain their existing behaviour and are NOT budget enforced.

Configuration (positive integers): AI_BUDGET_DAILY_CALLS=200,
AI_BUDGET_DAILY_TOKENS=500000, AI_BUDGET_MAX_INFLIGHT=2,
AI_BUDGET_MAX_OUTPUT_TOKENS=65536. Days are UTC. Each HTTP attempt, including a
truncation retry, consumes a call and a conservative serialized-input-byte +
max-output reservation. Reservations are never refunded, even on timeout.
Reported usage above that estimate is charged too; multimodal tokenization and
provider accounting cannot be predicted exactly. usage() exposes both values.
Provider cost is recorded when present, NOT a guaranteed/enforced money budget.

In-flight slots are process-thread locked AND reserved under a global DB lock.
A crashed process leaves a reserved slot occupied: only an operator who has
established that its request is no longer live may reconcile it as uncertain.
There is deliberately no automatic timeout release or retry of uncertain I/O.
No prompts, response text, credentials or provider exception text are persisted.
"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import json
import threading
import urllib.request
import uuid

import config

ANALYSIS_TOKENS = 8192
MEMORY_TOKENS = 4096
DIGEST_TOKENS = 8192
CHAT_TOKENS = 16384
MAX_RETRY_TOKENS = 65536
VERSION = 'secretary_ai_budget_v1'
WRITE_LOCK = 73190447
_lock = threading.Lock()
_inflight = 0

SCHEMA = """
CREATE TABLE secretary_ai_usage (
 id TEXT PRIMARY KEY, usage_day DATE NOT NULL,
 job_id BIGINT NOT NULL, job_token TEXT NOT NULL,
 reserved_tokens BIGINT NOT NULL CHECK(reserved_tokens>0),
 charged_tokens BIGINT NOT NULL CHECK(charged_tokens>=reserved_tokens),
 state TEXT NOT NULL CHECK(state IN ('reserved','completed','uncertain')),
 prompt_tokens BIGINT, completion_tokens BIGINT, total_tokens BIGINT,
 provider_cost NUMERIC CHECK(provider_cost>=0),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ);
CREATE INDEX secretary_ai_usage_day ON secretary_ai_usage(usage_day);
CREATE INDEX secretary_ai_usage_inflight ON secretary_ai_usage(state) WHERE state='reserved';
"""


class BudgetExceeded(RuntimeError):
    """No provider request was started: finite call/token/concurrency cap."""


class BudgetUnavailable(RuntimeError):
    """Worker accounting is unavailable; no unaccounted response may be used."""


def migrate(conn):
    """Additive, transactional migration. Caller commits; never connects itself."""
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reset_rows BIGINT NOT NULL)''')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return
        conn.execute(SCHEMA)
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))


def _limits():
    values = {}
    for name, default in (('DAILY_CALLS', 200), ('DAILY_TOKENS', 500000),
                          ('MAX_INFLIGHT', 2), ('MAX_OUTPUT_TOKENS', MAX_RETRY_TOKENS)):
        try:
            raw = config.get('AI_BUDGET_' + name, str(default))
            value = int(raw)
            if not 0 < value <= 9223372036854775807:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise BudgetUnavailable('Invalid AI budget configuration') from None
        values[name] = value
    values['MAX_OUTPUT_TOKENS'] = min(values['MAX_OUTPUT_TOKENS'], MAX_RETRY_TOKENS)
    return values


def _tokens(value):
    return value if type(value) is int and 0 <= value <= 9223372036854775807 else None


def _reported(response):
    value = response.get('usage') if type(response) is dict else None
    value = value if type(value) is dict else {}
    prompt, output, total = (_tokens(value.get(key)) for key in (
        'prompt_tokens', 'completion_tokens', 'total_tokens'))
    if prompt is not None and output is not None:
        total = max(total or 0, prompt + output)
    cost = value.get('cost')
    try:
        cost = Decimal(str(cost)) if type(cost) in (int, float, str) else None
        if cost is not None and (not cost.is_finite() or cost < 0 or cost > Decimal('1e18')):
            cost = None
    except (InvalidOperation, ValueError):
        cost = None
    return prompt, output, total, cost


def _reserve(context, limits, count):
    # Lazy imports are essential: brain -> ai_budget -> jobs/db -> brain can
    # otherwise cycle during startup. No DB connection is held over HTTP.
    import db
    import jobs
    identity = uuid.uuid4().hex
    try:
        if not db.available():
            raise BudgetUnavailable('AI budget database required for Workers')
        with db._conn() as conn, conn.transaction():
            jobs.assert_current(conn)
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (WRITE_LOCK,))
            day = conn.execute("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date").fetchone()[0]
            calls, tokens = conn.execute('''SELECT count(*),coalesce(sum(charged_tokens),0)
                FROM secretary_ai_usage WHERE usage_day=%s''', (day,)).fetchone()
            active = conn.execute("SELECT count(*) FROM secretary_ai_usage WHERE state='reserved'").fetchone()[0]
            if (calls >= limits['DAILY_CALLS'] or tokens + count > limits['DAILY_TOKENS']
                    or active >= limits['MAX_INFLIGHT']):
                raise BudgetExceeded('AI daily or in-flight budget exhausted')
            conn.execute('''INSERT INTO secretary_ai_usage
                (id,usage_day,job_id,job_token,reserved_tokens,charged_tokens,state)
                VALUES (%s,%s,%s,%s,%s,%s,'reserved')''',
                (identity, day, context.job_id, context.token, count, count))
    except BudgetExceeded:
        raise
    except Exception as exc:
        # Do not mask a lost execution lease as an accounting outage.
        if isinstance(exc, jobs.LeaseLost):
            raise
        raise BudgetUnavailable('AI budget reservation unavailable') from None
    return identity


def _settle(identity, context, response):
    import db
    prompt, output, total, cost = _reported(response)
    try:
        # Record the outcome even after lease loss. This is accounting only,
        # token-bound to this attempt; it grants no authority to use its result.
        with db._conn() as conn, conn.transaction():
            row = conn.execute('''UPDATE secretary_ai_usage SET state=%s,
                charged_tokens=greatest(reserved_tokens,%s),prompt_tokens=%s,
                completion_tokens=%s,total_tokens=%s,provider_cost=%s,finished_at=now()
                WHERE id=%s AND job_id=%s AND job_token=%s AND state='reserved' RETURNING id''',
                ('completed' if response is not None else 'uncertain', total or 0,
                 prompt, output, total, cost, identity, context.job_id, context.token)).fetchone()
            if not row:
                raise BudgetUnavailable('Unknown AI reservation')
    except Exception:
        raise BudgetUnavailable('AI budget settlement unavailable') from None


@contextmanager
def _attempt(payload):
    import jobs
    context = jobs.execution_context()
    if context is None:
        yield None
        return
    limits = _limits()
    output = payload.get('max_tokens')
    if type(output) is not int or output <= 0:
        raise BudgetUnavailable('Explicit positive AI output budget required')
    payload['max_tokens'] = min(output, limits['MAX_OUTPUT_TOKENS'])
    # Serialized bytes intentionally overestimate ordinary text token counts.
    count = len(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()) + payload['max_tokens'] + 1024
    global _inflight
    with _lock:
        if _inflight >= limits['MAX_INFLIGHT']:
            raise BudgetExceeded('AI process in-flight budget exhausted')
        _inflight += 1
    try:
        identity = _reserve(context, limits, count)
        outcome = {'response': None, 'output_ceiling': limits['MAX_OUTPUT_TOKENS']}
        try:
            jobs.assert_current()
            yield outcome
        finally:
            _settle(identity, context, outcome['response'])
        jobs.assert_current()
    finally:
        with _lock:
            _inflight -= 1


def usage(conn, day=None):
    """Bounded counters only; NULL cost means unreported, never 'free'."""
    row = conn.execute('''SELECT count(*),coalesce(sum(reserved_tokens),0),
        coalesce(sum(charged_tokens),0),sum(total_tokens),sum(provider_cost),
        count(*) FILTER (WHERE provider_cost IS NULL)
        FROM secretary_ai_usage WHERE usage_day=coalesce(%s::date,
            (clock_timestamp() AT TIME ZONE 'UTC')::date)''', (day,)).fetchone()
    return dict(zip(('calls', 'reserved_tokens', 'charged_tokens', 'reported_tokens',
                     'reported_cost', 'cost_unreported_calls'), row),
                inflight=conn.execute("SELECT count(*) FROM secretary_ai_usage WHERE state='reserved'").fetchone()[0])


class TruncatedCompletion(RuntimeError):
    """No partial completion may reach a parser or execute side effects."""


def request_content(payload, timeout=90):
    """Regenerate once from original messages, doubling the output budget.

    Only token exhaustion is retried here; transport failures stay with the
    caller/job queue. Never expose reasoning as a substitute for final content.
    """
    payload = dict(payload)
    for attempt in range(2):
        with _attempt(payload) as accounting:
            request = urllib.request.Request(
                'https://openrouter.ai/api/v1/chat/completions',
                data=json.dumps(payload).encode(), headers={
                    'Authorization': f'Bearer {config.OPENROUTER_API_KEY}',
                    'Content-Type': 'application/json',
                    'HTTP-Referer': 'https://pi-secretary.local',
                    'X-Title': 'Pi Secretary'}, method='POST')
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode())
            if accounting is not None:
                accounting['response'] = result
            choice = result['choices'][0]
        if (choice.get('finish_reason') in ('length', 'max_tokens')
                or choice.get('native_finish_reason') in ('MAX_TOKENS', 'max_tokens')):
            budget = payload['max_tokens']
            ceiling = accounting['output_ceiling'] if accounting is not None else MAX_RETRY_TOKENS
            if attempt == 0 and budget < ceiling:
                payload['max_tokens'] = min(budget * 2, ceiling)
                continue
            raise TruncatedCompletion('Risposta AI troncata: nessun contenuto parziale accettato')
        content = choice['message'].get('content')
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError('Risposta AI senza testo finale; riprovare')
        return content.strip()