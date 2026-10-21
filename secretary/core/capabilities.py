"""Declarative capabilities, neutral defaults and an additive installation receipt.

Integration: call ``migrate(conn, legacy=was_installed_before_base_migrations)``
after the base schema, before ``flows.init()``. The caller must capture ``legacy``
BEFORE message_store.migrate: timestamps written by that migration cannot reliably
distinguish a fresh installation from an upgrade. No history is inferred/deleted.

``settings(conn=None)`` returns name -> bool; ``enabled(name, conn=None)`` reads
one switch. ``set_enabled(name, value, conn=None)`` changes optional switches.
For these settings APIs, a supplied connection is never closed/committed; the
caller owns its transaction. migrate uses a transaction (or nested savepoint).
Without one, reads fail closed for optional features when the DB is unavailable;
writes raise. Importing this module never imports/connects to the database.

flow_settings is the ONLY mutable switch store. capability_settings is a VIEW,
not a second enabled flag. secretary_profile records immutable installation
defaults/provenance, not current settings or authority to execute a tool.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType


VERSION = 'capabilities_v1'
PROFILE_VERSION = 1


@dataclass(frozen=True)
class Capability:
    """Names describe contracts, not executable callbacks or authorization."""
    tools: tuple[str, ...]
    requirements: tuple[str, ...] = ()
    optional: bool = False


REGISTRY = MappingProxyType({
    'extraction': Capability(('read_message', 'attachment_text', 'triage')),
    'search': Capability(('db_query', 'fs_find', 'mail_search', 'web_search')),
    'cataloguing': Capability(('attachment_text',)),
    'drafts': Capability(('email_propose',)),
    'memory': Capability(('note_save', 'note_forget')),
    'tasks': Capability(('task_change', 'tasks_close_bulk', 'task_from_message')),
    'calendar': Capability(('calendar_propose',)),
    'invoices': Capability(('archive_invoice',),
                          ('invoice_document', 'configured_storage'), optional=True),
    'monthly_zip': Capability(('prepare_monthly_zip', 'propose_zip_delivery'),
                             ('archived_invoices', 'delivery_authorization'), optional=True),
})
OPTIONAL = tuple(name for name, spec in REGISTRY.items() if spec.optional)

FLOW_SCHEMA = '''CREATE TABLE IF NOT EXISTS flow_settings (
    flow TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now())'''


@contextmanager
def _connection(conn=None):
    if conn is not None:
        yield conn
    else:
        from secretary.core import db
        with db._conn() as owned:
            yield owned


def flow_defaults(conn):
    """Optional defaults from the receipt; no profile means neutral/fail closed."""
    legacy = False
    if conn.execute("SELECT to_regclass('secretary_profile')").fetchone()[0]:
        row = conn.execute('SELECT provenance FROM secretary_profile WHERE id=1').fetchone()
        legacy = bool(row and row[0] == 'legacy')
    return {name: legacy for name in OPTIONAL}


def migrate(conn, legacy=False):
    """Atomically apply v1 once; return True if applied, False on receipt replay.

    Explicit existing flow rows (including False) always win. Missing optional
    rows default False for fresh DBs, True for legacy DBs. Replaying with another
    legacy value never rewrites the profile, switches, timestamps or receipt.
    """
    if type(legacy) is not bool:
        raise ValueError('legacy must be a boolean captured before base migrations')
    with conn.transaction():
        # Same lock as the base migration, including concurrent first starts.
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reset_rows BIGINT NOT NULL)''')
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (VERSION,)).fetchone():
            return False
        conn.execute('''CREATE TABLE IF NOT EXISTS secretary_profile (
            id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
            provenance TEXT NOT NULL CHECK(provenance IN ('fresh','legacy')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        conn.execute('''INSERT INTO secretary_profile(id,version,provenance)
            VALUES (1,%s,%s) ON CONFLICT(id) DO NOTHING''',
            (PROFILE_VERSION, 'legacy' if legacy else 'fresh'))
        conn.execute(FLOW_SCHEMA)
        for name, value in flow_defaults(conn).items():
            conn.execute('''INSERT INTO flow_settings(flow,enabled) VALUES (%s,%s)
                ON CONFLICT(flow) DO NOTHING''', (name, value))
        conn.execute('''CREATE OR REPLACE VIEW capability_settings AS
            SELECT flow AS capability,enabled,updated_at FROM flow_settings
            WHERE flow IN ('invoices','monthly_zip')''')
        conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (VERSION,))
    return True


def _settings(conn):
    values = {name: not spec.optional for name, spec in REGISTRY.items()}
    values.update(flow_defaults(conn))
    if conn.execute("SELECT to_regclass('flow_settings')").fetchone()[0]:
        rows = conn.execute('SELECT flow,enabled FROM flow_settings').fetchall()
        values.update({name: value for name, value in rows
                       if name in OPTIONAL and type(value) is bool})
    return values


def settings(conn=None):
    """Core contracts are available; optional DB read failures never opt in."""
    if conn is not None:
        return _settings(conn)
    fallback = {name: not spec.optional for name, spec in REGISTRY.items()}
    from secretary.core import db
    if not db.available():
        return fallback
    try:
        with _connection() as owned:
            return _settings(owned)
    except Exception:
        return fallback


def enabled(name, conn=None):
    if name not in REGISTRY:
        raise ValueError('Unknown capability')
    return settings(conn=conn)[name]


def set_enabled(name, value, conn=None):
    """Set an optional flow flag; no migration, policy grant or external effect."""
    if name not in OPTIONAL or type(value) is not bool:
        raise ValueError('Invalid optional capability setting')
    with _connection(conn) as current:
        current.execute('''INSERT INTO flow_settings(flow,enabled) VALUES (%s,%s)
            ON CONFLICT(flow) DO UPDATE SET enabled=excluded.enabled,updated_at=now()''',
            (name, value))