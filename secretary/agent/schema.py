"""Schema `agent`: le UNICHE relazioni che l'agente può leggere con SQL libero.

Ogni vista incorpora i consensi dashboard (`data_policies`) e li rivaluta a
OGNI query: revocare un consenso nasconde subito i dati, anche a una sessione
già aperta. Le viste NON espongono token, grant, ricevute, credenziali né i
byte degli allegati. Le tabelle `agent_*` (audit, journal degli effetti,
outbox email) sono scritte solo dal codice del server, mai dal modello.

Le viste appartengono al ruolo applicativo (security_invoker disattivo): un
ruolo di sola lettura che abbia SELECT soltanto su di esse non può toccare le
tabelle sottostanti (vedi deploy/sql/agent_ro.sql, da eseguire a cura del DBA).
"""
import re

VERSION = 'agent_core_v1'
VERSION_V2 = 'agent_core_v2'
DEFAULT_SCHEMA = 'agent'
MEDIA_DIRS = 'email_attachments|tg_files|wa_media|uploads|invoices'

TABLES = """
CREATE TABLE IF NOT EXISTS agent_runs (
 id BIGSERIAL PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('owner','inbox','digest')),
 turn_key TEXT NOT NULL, job_id BIGINT, owner_message BIGINT, model TEXT,
 state TEXT NOT NULL DEFAULT 'running' CHECK(state IN ('running','done','failed')),
 tool_calls INTEGER NOT NULL DEFAULT 0, error TEXT,
 started_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS agent_runs_started ON agent_runs(started_at DESC);
CREATE TABLE IF NOT EXISTS agent_tool_calls (
 id BIGSERIAL PRIMARY KEY, run_id BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
 seq INTEGER NOT NULL, tool TEXT NOT NULL, args TEXT, ok BOOLEAN NOT NULL, summary TEXT,
 duration_ms INTEGER, at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS agent_tool_calls_run ON agent_tool_calls(run_id, seq);
CREATE TABLE IF NOT EXISTS agent_effects (
 turn_key TEXT NOT NULL, effect_key TEXT NOT NULL, tool TEXT NOT NULL, result JSONB,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(turn_key, effect_key));
CREATE TABLE IF NOT EXISTS agent_email_outbox (
 id BIGSERIAL PRIMARY KEY, account TEXT NOT NULL, recipient TEXT NOT NULL,
 subject TEXT NOT NULL, body TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'proposed'
  CHECK(state IN ('proposed','approved','rejected','sent','failed','uncertain')),
 proposed_by_message BIGINT, approved_by_message BIGINT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), decided_at TIMESTAMPTZ, sent_at TIMESTAMPTZ);
"""

# v2: la bozza vive anche nella cartella Bozze della casella (IMAP APPEND \Draft).
# `draft_state` dice se il salvataggio remoto è avvenuto; `draft_folder`/`draft_uid`
# identificano la copia remota per poterla rimuovere su /scarta. `in_reply_to` e
# `references` conservano il thread del messaggio a cui si risponde.
DRAFT_COLUMNS = """
ALTER TABLE agent_email_outbox ADD COLUMN IF NOT EXISTS draft_state TEXT NOT NULL DEFAULT 'none';
ALTER TABLE agent_email_outbox ADD COLUMN IF NOT EXISTS draft_folder TEXT;
ALTER TABLE agent_email_outbox ADD COLUMN IF NOT EXISTS draft_uid TEXT;
ALTER TABLE agent_email_outbox ADD COLUMN IF NOT EXISTS draft_message_id TEXT;
ALTER TABLE agent_email_outbox ADD COLUMN IF NOT EXISTS draft_error TEXT;
ALTER TABLE agent_email_outbox ADD COLUMN IF NOT EXISTS in_reply_to TEXT;
ALTER TABLE agent_email_outbox ADD COLUMN IF NOT EXISTS references_header TEXT;
ALTER TABLE agent_email_outbox ADD COLUMN IF NOT EXISTS source_message BIGINT;
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS email_messages_fts ON email_messages USING gin
 (to_tsvector('italian', coalesce(subject,'') || ' ' || coalesce(body,'')));
CREATE INDEX IF NOT EXISTS whatsapp_messages_fts ON whatsapp_messages USING gin
 (to_tsvector('italian', coalesce(subject,'') || ' ' || coalesce(body,'')));
CREATE INDEX IF NOT EXISTS telegram_messages_fts ON telegram_messages USING gin
 (to_tsvector('italian', coalesce(subject,'') || ' ' || coalesce(body,'')));
"""


def consent(channel_sql, effect):
    """Predicato SQL: consenso dashboard attivo per canale ed effetto."""
    return (f"EXISTS (SELECT 1 FROM data_policies p WHERE p.channel = split_part({channel_sql},' ',1) "
            f"AND p.effect = '{effect}' AND p.allowed)")


def views(schema):
    """Definizioni delle viste. Ordine: prima `messages`, da cui le altre dipendono."""
    S = schema
    union = ' UNION ALL '.join(
        f"SELECT id,ts,day,channel,account,sender,subject,body,direction,chat_id,author,metadata FROM {t}"
        for t in ('email_messages', 'whatsapp_messages', 'telegram_messages'))
    historical = "COALESCE(m.metadata->>'historical','false') = 'true'"
    return [
        ('messages', f"""
SELECT m.id, m.ts, m.day, m.channel, split_part(m.channel,' ',1) AS source, m.account,
       m.sender, m.subject, m.body, m.direction, m.chat_id, m.author,
       ({historical}) AS historical,
       a.summary, a.category, a.priority, COALESCE(a.needs_action,false) AS needs_action,
       a.action, COALESCE(a.needs_reply,false) AS needs_reply, a.reply_hint, a.analyzed_at,
       (COALESCE(m.metadata->>'has_media','false') = 'true'
        AND NOT EXISTS (SELECT 1 FROM attachments x WHERE x.message_id = m.id)) AS media_missing
FROM ({union}) m LEFT JOIN message_analysis a ON a.message_id = m.id
WHERE {consent('m.channel', 'ai_text')}
  AND (NOT ({historical}) OR {consent('m.channel', 'ai_history')})"""),
        ('attachments', f"""
SELECT a.id, a.message_id, a.channel, a.filename, a.content_type,
       COALESCE(length(a.data), 0) AS db_bytes, (a.path IS NOT NULL OR a.data IS NOT NULL) AS stored,
       CASE WHEN a.path ~ '/({MEDIA_DIRS})/'
            THEN regexp_replace(a.path, '^.*/(({MEDIA_DIRS})/.*)$', '\\1') END AS rel_path
FROM attachments a JOIN {S}.messages m ON m.id = a.message_id
WHERE {consent('a.channel', 'ai_media')}"""),
        ('documents', f"""
SELECT k.id AS entry_id, k.message_id, k.source_type, k.title, k.text, k.ts, k.status
FROM knowledge_entries k JOIN {S}.messages m ON m.id = k.message_id
WHERE k.active AND k.source_type = 'document' AND {consent('k.channel', 'ai_media')}
UNION ALL
SELECT t.attachment_id, a.message_id, 'media_' || t.kind, a.filename, t.text, t.created_at, 'ok'
FROM media_transcripts t JOIN attachments a ON a.id = t.attachment_id
JOIN {S}.messages m ON m.id = a.message_id WHERE {consent('a.channel', 'ai_media')}"""),
        ('tasks', f"""
SELECT t.id, t.title, t.status, t.deadline, t.owner_deadline_date, t.owner_remind_at,
       t.created_at, t.completed_at, t.version, t.source_message, t.owner_origin_message
FROM tasks t WHERE t.source_message IS NULL
   OR EXISTS (SELECT 1 FROM {S}.messages m WHERE m.id = t.source_message)"""),
        ('appointments', """
SELECT id, title, dt, location, notes, source, done, sync_state FROM appointments"""),
        ('notes', """
SELECT id, text, kind, topic, created, version FROM notes"""),
        ('rules', """
SELECT id, pattern, instruction FROM rules"""),
        ('invoices', """
SELECT id, entity, month, filename, sender, subject, source, ts FROM invoices"""),
        ('owner_exchanges', f"""
SELECT e.message_id, e.message_time, e.user_text, e.response, e.completed_at
FROM owner_exchanges e JOIN {S}.messages m ON m.id = e.message_id
WHERE e.completed_at IS NOT NULL"""),
    ]


# Primer mostrato al modello: breve, esatto e verificato dai test contro il DB.
PRIMER = """Viste PostgreSQL (schema {s}); usa SEMPRE il prefisso {s}. e solo SELECT con LIMIT:
- {s}.messages(id, ts, day, channel, source, account, sender, subject, body, direction, chat_id, author, historical,
  summary, category, priority, needs_action, action, needs_reply, reply_hint, analyzed_at, media_missing)
  = TUTTE le comunicazioni (email, WhatsApp, Telegram) con la loro analisi. source = email|whatsapp|telegram;
  direction = in|out; channel include il nome dell'account (es. 'email (hypaz alessio)').
  media_missing = il messaggio aveva un allegato che NON è stato salvato (lo dirai al proprietario, senza inventarne il contenuto).
- {s}.attachments(id, message_id, channel, filename, content_type, db_bytes, stored, rel_path)
  rel_path e' la posizione del file; per leggerne il testo usa attachment_text con l'id dell'allegato.
- {s}.documents(entry_id, message_id, source_type, title, text, ts, status) = testo gia' estratto da documenti, immagini, vocali.
- {s}.tasks(id, title, status 'open'|'done'|'cancelled', deadline, owner_deadline_date, owner_remind_at, created_at, completed_at, version, source_message, owner_origin_message)
- {s}.appointments(id, title, dt, location, notes, source, done, sync_state)
- {s}.notes(id, text, kind, topic, created, version)  {s}.rules(id, pattern, instruction)
- {s}.invoices(id, entity, month, filename, sender, subject, source, ts)
- {s}.owner_exchanges(message_id, message_time, user_text, response) = i tuoi dialoghi col proprietario.
Ricerca nel testo: to_tsvector('italian', coalesce(subject,'')||' '||coalesce(body,'')) @@ websearch_to_tsquery('italian', 'parole'); oppure ILIKE.
Le celle lunghe sono troncate: per il testo completo usa read_message(id)."""


def primer(schema=DEFAULT_SCHEMA):
    return PRIMER.format(s=schema)


def _valid(schema):
    if not isinstance(schema, str) or not re.fullmatch(r'[a-z_][a-z0-9_]{0,40}', schema):
        raise ValueError('Invalid agent schema name')
    return schema


def migrate(conn, schema=DEFAULT_SCHEMA):
    """Additiva e transazionale. Il chiamante committa; non apre connessioni."""
    schema = _valid(schema)
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(73190421)')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            reset_rows BIGINT NOT NULL)''')
        version = f'{VERSION}:{schema}'
        if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (version,)).fetchone():
            conn.execute(TABLES)
            conn.execute(INDEXES)
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS {schema}')
            for name, body in views(schema):
                conn.execute(f'CREATE OR REPLACE VIEW {schema}.{name} AS {body}')
            conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (version,))
        # v2 additiva: vale anche per le installazioni già migrate alla v1.
        version_v2 = f'{VERSION_V2}:{schema}'
        if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (version_v2,)).fetchone():
            conn.execute(DRAFT_COLUMNS)
            conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (version_v2,))


# Assumere un ruolo (SET ROLE) richiede il privilegio SET su PostgreSQL 16+. Lì essere MEMBER
# non basta: chi crea un ruolo con CREATEROLE ne è membro solo come amministratore e SET ROLE
# gli viene negato finché non riceve un GRANT esplicito. Prima della 16 vale MEMBER.
CAN_ASSUME = ("pg_has_role(current_user, {oid}, CASE WHEN current_setting('server_version_num')::int >= 160000 "
              "THEN 'SET' ELSE 'MEMBER' END)")


def readonly_role_usable(conn, role):
    """True se il ruolo di sola lettura esiste e il ruolo corrente può assumerlo (SET ROLE)."""
    if not role:
        return False
    return bool(conn.execute(
        'SELECT 1 FROM pg_roles r WHERE r.rolname=%s AND ' + CAN_ASSUME.format(oid='r.oid'), (role,)).fetchone())


ROLE_NAME = re.compile(r'[a-z_][a-z0-9_]{0,40}')


def ensure_readonly_role(conn, schema=DEFAULT_SCHEMA, role='agent_ro'):
    """Predispone il ruolo di sola lettura dell'agente, per quanto i privilegi consentono.

    Idempotente e a massimo impegno: non solleva mai per mancanza di privilegi, perché
    senza il ruolo l'agente resta protetto dalla guardia AST + transazione READ ONLY.
    Ritorna uno stato:

    * ``disabled``  nome vuoto (``AGENT_DB_ROLE=``): nessun ruolo voluto;
    * ``ready``     ruolo assumibile e privilegi già corretti: nessuna scrittura;
    * ``created``   ruolo creato ora (serve CREATEROLE o superuser: es. il DB locale
                    del compose, dove l'utente applicativo è il proprietario);
    * ``granted``   il ruolo esisteva (creato dal DBA) e sono stati sistemati i privilegi;
    * ``needs_dba`` mancano privilegi: il DBA deve eseguire ``deploy/sql/agent_ro.sql``
                    (bastano ``CREATE ROLE`` e ``GRANT`` del ruolo all'utente applicativo:
                    i SELECT sulle viste li concede l'app, che ne è proprietaria);
    * ``error``     qualunque altro errore del database (la transazione è annullata).

    Il ruolo creato è NOLOGIN, senza alcun attributo e con il solo SELECT sulle viste.
    """
    from psycopg import sql
    schema = _valid(schema)
    if not role:
        return 'disabled'
    if not isinstance(role, str) or not ROLE_NAME.fullmatch(role):
        raise ValueError('Invalid agent role name')
    import psycopg
    schema_id, role_id = sql.Identifier(schema), sql.Identifier(role)
    try:
        with conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(73190421)')
            if conn.execute('''SELECT
                  ''' + CAN_ASSUME.format(oid='r.oid') + '''
                  AND has_schema_privilege(r.oid, %s::text, 'USAGE')
                  AND NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                      WHERE n.nspname=%s::text AND c.relkind IN ('v','r','m','f','p')
                        AND NOT has_table_privilege(r.oid, c.oid, 'SELECT'))
                FROM pg_roles r WHERE r.rolname=%s::text''', (schema, schema, role)).fetchone() == (True,):
                return 'ready'
            admin = conn.execute(
                'SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()[0]
            created = False
            if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                if not admin:
                    return 'needs_dba'
                conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                                     'NOREPLICATION NOBYPASSRLS').format(role_id))
                created = True
            if not readonly_role_usable(conn, role):
                conn.execute(sql.SQL('GRANT {} TO CURRENT_USER').format(role_id))
            conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(schema_id, role_id))
            conn.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(schema_id, role_id))
            # Le viste create dopo (nuove versioni dell'app) ereditano il SELECT.
            conn.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}')
                         .format(schema_id, role_id))
            return 'created' if created else 'granted'
    except psycopg.errors.InsufficientPrivilege:
        return 'needs_dba'
    except psycopg.Error:
        return 'error'


def prune(conn, days=30):
    """Retention dell'audit delle esecuzioni (le ricevute degli effetti restano)."""
    conn.execute("DELETE FROM agent_runs WHERE started_at < now() - (%s * interval '1 day')", (days,))
