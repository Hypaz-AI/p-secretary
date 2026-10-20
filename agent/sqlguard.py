"""Guardia per le query SQL libere dell'agente: una sola SELECT, solo viste `agent.*`.

L'agente scrive SQL liberamente, ma ogni query attraversa DUE barriere
indipendenti:

1. Questa validazione sull'AST di PostgreSQL (pglast): una sola istruzione
   SELECT/WITH, nessuna scrittura (nemmeno dentro una CTE), nessun INTO o
   lock, SOLO relazioni qualificate dello schema delle viste (o CTE definite
   nella stessa query), SOLO funzioni in allowlist, nessun cast a reg*.
2. L'esecuzione in transazione READ ONLY con timeout, search_path ristretto
   e, quando il DBA ha creato il ruolo, ``SET LOCAL ROLE`` su un ruolo che
   può leggere solo le viste (vedi agent/schema.py e deploy/agent_ro.sql).

Le viste applicano i consensi (data_policies) a OGNI query: la revoca vale
subito. Nessun testo, prompt o risultato viene mai scritto nei log di questo
modulo.
"""
from decimal import Decimal
import datetime as _dt
import re

from pglast import parse_sql
from pglast.visitors import Visitor

MAX_SQL_CHARS = 8000
MAX_ROWS = 200
MAX_CELL_CHARS = 400
MAX_RESULT_CHARS = 16000

# Funzioni di sola lettura, deterministiche e senza effetti collaterali.
ALLOWED_FUNCTIONS = frozenset('''
count sum avg min max bool_and bool_or every string_agg array_agg json_agg jsonb_agg
jsonb_object_agg json_object_agg percentile_cont percentile_disc mode
row_number rank dense_rank ntile lag lead first_value last_value nth_value
cume_dist percent_rank
lower upper initcap length char_length octet_length substr substring left right
trim ltrim rtrim btrim lpad rpad replace translate split_part regexp_replace
regexp_match regexp_matches regexp_split_to_array regexp_split_to_table
position strpos concat concat_ws format repeat reverse starts_with md5 ascii chr
to_char to_date to_timestamp to_number
now clock_timestamp statement_timestamp timezone date_trunc date_part extract
date_bin age make_date make_time make_timestamp make_timestamptz make_interval
justify_days justify_hours isfinite
abs ceil ceiling floor round trunc sign sqrt power exp ln log mod div greatest least
nullif coalesce
to_tsvector to_tsquery plainto_tsquery phraseto_tsquery websearch_to_tsquery
ts_rank ts_rank_cd ts_headline setweight strip numnode querytree
array_length cardinality array_to_string string_to_array array_position
array_positions array_remove array_cat array_append array_prepend array_dims
unnest generate_series generate_subscripts
to_jsonb to_json jsonb_build_object json_build_object jsonb_build_array
json_build_array jsonb_array_elements jsonb_array_elements_text jsonb_each
jsonb_each_text jsonb_object_keys jsonb_typeof jsonb_array_length
jsonb_extract_path jsonb_extract_path_text jsonb_path_exists jsonb_path_query
jsonb_path_query_array jsonb_pretty jsonb_strip_nulls row_to_json
'''.split())

# Tipi che permetterebbero di risolvere nomi di oggetti di sistema.
FORBIDDEN_TYPES = frozenset('''
regclass regproc regprocedure regoper regoperator regtype regrole regnamespace
regconfig regdictionary oid xid xid8 cid tid pg_lsn
'''.split())


class SqlRejected(ValueError):
    """La query non è ammessa: il messaggio è mostrato al modello (nessun dato)."""


class _Checker(Visitor):
    def __init__(self, schema):
        super().__init__()
        self.schema = schema
        self.ctes = set()
        self.relations = []
        self.problem = None

    def _fail(self, message):
        if self.problem is None:
            self.problem = message

    def visit_CommonTableExpr(self, ancestors, node):
        self.ctes.add(node.ctename.lower())

    def visit_RangeVar(self, ancestors, node):
        self.relations.append((node.schemaname, node.relname))

    def visit_FuncCall(self, ancestors, node):
        names = [part.sval.lower() for part in node.funcname]
        if len(names) > 2 or (len(names) == 2 and names[0] != 'pg_catalog'):
            self._fail('Funzione con schema non ammesso')
        elif names[-1] not in ALLOWED_FUNCTIONS:
            self._fail(f'Funzione non ammessa: {names[-1]}')
        if node.agg_filter is not None or node.over is not None or node.agg_order is not None:
            pass  # FILTER/OVER/ORDER BY negli aggregati sono sola lettura

    def visit_TypeCast(self, ancestors, node):
        name = node.typeName.names[-1].sval.lower() if node.typeName.names else ''
        if name in FORBIDDEN_TYPES:
            self._fail(f'Tipo non ammesso: {name}')

    def visit_ParamRef(self, ancestors, node):
        self._fail('Parametri ($n) non ammessi')

    def _no_writes(self, ancestors, node):
        self._fail('Sono ammesse solo SELECT in sola lettura')

    visit_InsertStmt = visit_UpdateStmt = visit_DeleteStmt = visit_MergeStmt = _no_writes

    def visit_SelectStmt(self, ancestors, node):
        if node.intoClause is not None:
            self._fail('SELECT ... INTO non ammesso')
        if node.lockingClause:
            self._fail('Clausole di lock non ammesse')


def validate(sql, schema='agent'):
    """Ritorna la query normalizzata (senza ';' finale) o alza SqlRejected."""
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected('Query vuota')
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRejected('Query troppo lunga')
    text = sql.strip().rstrip(';').strip()
    if '\x00' in text:
        raise SqlRejected('Carattere non valido')
    try:
        tree = parse_sql(text)
    except Exception:
        raise SqlRejected('SQL non valido: correggi la sintassi PostgreSQL') from None
    if len(tree) != 1:
        raise SqlRejected('Una sola istruzione per volta')
    # Si esegue SOLO il testo dell'istruzione validata, mai code o commenti.
    start = tree[0].stmt_location or 0
    end = start + tree[0].stmt_len if tree[0].stmt_len else len(text)
    text = text[start:end].strip().rstrip(';').strip()
    if type(tree[0].stmt).__name__ != 'SelectStmt':
        raise SqlRejected('Sono ammesse solo SELECT in sola lettura')
    checker = _Checker(schema)
    checker(tree)
    if checker.problem:
        raise SqlRejected(checker.problem)
    for relation_schema, name in checker.relations:
        if relation_schema is None and name.lower() in checker.ctes:
            continue
        if relation_schema != schema:
            raise SqlRejected(f'Usa solo le viste {schema}.* (non {relation_schema + "." if relation_schema else ""}{name})')
    return text


def _cell(value):
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f'<binario {len(value)} byte>'
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    text = value if isinstance(value, str) else str(value)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', text)
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS] + f'…[+{len(text) - MAX_CELL_CHARS} caratteri: usa read_message]'
    return text


def shape(columns, rows, *, limit=MAX_ROWS):
    """Risultato limitato in righe, celle e dimensione totale."""
    out, used, truncated = [], 0, len(rows) > limit
    for row in rows[:limit]:
        cells = [_cell(v) for v in row]
        used += sum(len(str(c)) + 2 for c in cells)
        if used > MAX_RESULT_CHARS:
            truncated = True
            break
        out.append(cells)
    return dict(columns=list(columns), rows=out, row_count=len(out), truncated=truncated)


def execute(connect, sql, *, schema='agent', role=None, timeout_ms=10000):
    """Esegue una query già validata in transazione READ ONLY.

    ``connect`` è una funzione che ritorna una connessione psycopg NON autocommit
    (così SET LOCAL vale solo per questa transazione).
    """
    query = validate(sql, schema)
    with connect() as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute(f'SET LOCAL statement_timeout = {int(timeout_ms)}')
            conn.execute('SET LOCAL lock_timeout = 2000')
            conn.execute(f'SET LOCAL search_path = "{schema}", public, pg_catalog')
            if role:
                conn.execute(f'SET LOCAL ROLE "{role}"')
            with conn.cursor(name='agent_q') as cursor:
                cursor.execute(query)
                columns = [d.name for d in cursor.description or []]
                rows = cursor.fetchmany(MAX_ROWS + 1)
    return shape(columns, rows)
