"""Integrazione su PostgreSQL di test (mai produzione): viste, consensi, query, journal.

Opt-in: PI_SECRETARY_TEST_DATABASE_URL con dbname che finisce in _test.
"""
import json
import os
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from secretary.core import config, db, jobs
from secretary.agent import data, fsbox, journal, schema as agent_schema, sqlguard

URL = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL', '')


@unittest.skipUnless(URL, 'Set PI_SECRETARY_TEST_DATABASE_URL to opt in')
class AgentPostgresCase(unittest.TestCase):
    """Schema isolato per test; db.init() reale; nessuna rete."""

    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(URL)
        if not params.get('dbname', '').endswith('_test') or params.get('service'):
            raise ValueError('An explicit database ending in _test (no service) is required')

    def setUp(self):
        from psycopg import sql
        self.schema = 'agent_base_' + uuid.uuid4().hex[:16]
        self.views = 'agent_v_' + uuid.uuid4().hex[:12]
        self.conn = db.psycopg.connect(URL, autocommit=True, prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop)
        self.configure(self.conn)
        self.main = threading.get_ident()
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch.object(db, 'available', return_value=True))
        self.enterContext(patch.object(data, 'connect', self.plain))
        self.enterContext(patch.object(config, 'AGENT_DB_SCHEMA', self.views))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        db.init()

    def configure(self, conn):
        from psycopg import sql
        conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        conn.execute("SET timezone='UTC'")

    def drop(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(self.views)))
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    @contextmanager
    def fresh_connection(self):
        """Una connessione indipendente nello stesso schema di test (altro processo/thread)."""
        fresh = db.psycopg.connect(URL, autocommit=True, prepare_threshold=None, connect_timeout=5)
        try:
            self.configure(fresh)
            yield fresh
        finally:
            fresh.close()

    @contextmanager
    def connection(self):
        if threading.get_ident() == self.main:
            yield self.conn
        else:
            with self.fresh_connection() as fresh:
                yield fresh

    def plain(self):
        """Connessione NON autocommit, come data.connect in produzione."""
        conn = db.psycopg.connect(URL, autocommit=False, prepare_threshold=None, connect_timeout=5)
        with conn.cursor() as cursor:
            cursor.execute('SET search_path TO "%s"' % self.schema)
        conn.commit()
        return conn

    # ---------------------------------------------------------------- dati
    def message(self, table='email_messages', *, channel='email (test)', body='corpo', subject='oggetto',
                sender='mario@example.test', historical=False, direction='in'):
        row = self.conn.execute(f'''INSERT INTO {table}(ts,day,channel,sender,subject,body,source_id,account,
            direction,metadata) VALUES (now(),current_date,%s,%s,%s,%s,%s,'acc',%s,%s) RETURNING id''',
            (channel, sender, subject, body, uuid.uuid4().hex, direction,
             json.dumps({'historical': True} if historical else {}))).fetchone()
        return row[0]

    def consent(self, channel, effect, allowed=True):
        self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed) VALUES (%s,%s,%s)
            ON CONFLICT(channel,effect) DO UPDATE SET allowed=excluded.allowed''', (channel, effect, allowed))

    def attachment(self, mid, *, channel='email', filename='doc.pdf', path=None, data_bytes=None,
                   content_type='application/pdf'):
        return self.conn.execute('''INSERT INTO attachments(message_id,channel,filename,content_type,path,data)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''',
            (mid, channel, filename, content_type, path, data_bytes)).fetchone()[0]

    def ids(self, sql):
        return [r[0] for r in data.db_query(sql)['rows']]


class SchemaAndConsentTests(AgentPostgresCase):
    def test_migration_is_idempotent_and_creates_every_view(self):
        agent_schema.migrate(self.conn, self.views)
        count = self.conn.execute('SELECT count(*) FROM schema_migrations WHERE version=%s',
                                  (f'{agent_schema.VERSION}:{self.views}',)).fetchone()[0]
        self.assertEqual(count, 1)
        names = {r[0] for r in self.conn.execute(
            'SELECT table_name FROM information_schema.views WHERE table_schema=%s', (self.views,))}
        self.assertEqual(names, {n for n, _ in agent_schema.views(self.views)})

    def test_without_any_consent_nothing_is_visible(self):
        self.message()
        self.assertEqual(data.db_query(f'SELECT id FROM {self.views}.messages')['rows'], [])

    def test_consent_is_per_channel_and_revocation_is_immediate(self):
        mail, wa = self.message(channel='email (a)'), self.message('whatsapp_messages', channel='whatsapp')
        self.consent('email', 'ai_text')
        self.assertEqual(self.ids(f'SELECT id FROM {self.views}.messages'), [mail])
        self.consent('whatsapp', 'ai_text')
        self.assertCountEqual(self.ids(f'SELECT id FROM {self.views}.messages'), [mail, wa])
        self.consent('email', 'ai_text', False)  # revoca a sessione "aperta"
        self.assertEqual(self.ids(f'SELECT id FROM {self.views}.messages'), [wa])

    def test_historical_messages_also_need_ai_history(self):
        live = self.message()
        old = self.message(historical=True)
        self.consent('email', 'ai_text')
        self.assertEqual(self.ids(f'SELECT id FROM {self.views}.messages'), [live])
        self.consent('email', 'ai_history')
        self.assertCountEqual(self.ids(f'SELECT id FROM {self.views}.messages'), [live, old])

    def test_account_suffix_in_channel_maps_to_the_base_channel_consent(self):
        mid = self.message('telegram_messages', channel='telegram (account)')
        self.consent('telegram', 'ai_text')
        self.assertEqual(self.ids(f'SELECT id FROM {self.views}.messages'), [mid])

    def test_attachments_need_media_consent_and_never_expose_bytes_or_physical_paths(self):
        mid = self.message()
        self.consent('email', 'ai_text')
        att = self.attachment(mid, path='/home/ale/pi-secretary/data/email_attachments/abc123',
                              data_bytes=b'segreto')
        self.assertEqual(data.db_query(f'SELECT id FROM {self.views}.attachments')['rows'], [])
        self.consent('email', 'ai_media')
        rows = data.db_query(f'SELECT id, rel_path, db_bytes, stored FROM {self.views}.attachments')['rows']
        self.assertEqual(rows, [[att, 'email_attachments/abc123', 7, True]])
        columns = {r[0] for r in self.conn.execute('''SELECT column_name FROM information_schema.columns
            WHERE table_schema=%s AND table_name='attachments' ''', (self.views,))}
        self.assertNotIn('data', columns)
        self.assertNotIn('path', columns)

    def test_a_whatsapp_message_whose_media_was_not_saved_is_flagged_for_the_agent(self):
        """Il proprietario deve poter sapere che un allegato è andato perso, senza che l'agente lo inventi."""
        self.consent('whatsapp', 'ai_text')
        self.consent('whatsapp', 'ai_media')
        lost = self.message('whatsapp_messages', channel='whatsapp', body='guarda questa fattura')
        saved = self.message('whatsapp_messages', channel='whatsapp', body='ecco il file')
        plain = self.message('whatsapp_messages', channel='whatsapp', body='ciao')
        for mid, error in ((lost, 'unavailable'), (saved, False)):
            self.conn.execute("UPDATE whatsapp_messages SET metadata=%s WHERE id=%s",
                              (json.dumps({'has_media': True, 'media_error': error}), mid))
        self.attachment(saved, channel='whatsapp', filename='fattura.pdf', path='/x/wa_media/s_fattura.pdf')
        flags = dict(self.conn.execute(f'SELECT id,media_missing FROM {self.views}.messages WHERE id=ANY(%s)',
                                       ([lost, saved, plain],)).fetchall())
        self.assertEqual(flags, {lost: True, saved: False, plain: False})
        # L'agente lo vede anche da SQL libero, senza metadati grezzi.
        rows = data.db_query(f'SELECT id FROM {self.views}.messages WHERE media_missing')['rows']
        self.assertEqual([r[0] for r in rows], [lost])

    def test_no_view_exposes_credentials_or_job_tokens(self):
        forbidden = {'token', 'password', 'secret', 'authorization', 'payload', 'lease_until', 'metadata',
                     'data', 'path', 'source_id', 'raw_verdict'}
        for name, _ in agent_schema.views(self.views):
            columns = {r[0] for r in self.conn.execute('''SELECT column_name FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s''', (self.views, name))}
            self.assertFalse(columns & forbidden, f'{name} espone {columns & forbidden}')

    def test_internal_tables_are_not_reachable_through_db_query(self):
        for relation in ('work_jobs', 'effect_receipts', 'calendar_effect_grants', 'data_policies',
                         'email_messages', 'attachments', 'secretary_profile'):
            result = data.db_query(f'SELECT * FROM {relation}')
            self.assertIn('error', result, relation)
            result = data.db_query(f'SELECT * FROM public.{relation}')
            self.assertIn('error', result, relation)

    def test_tasks_hide_those_derived_from_messages_without_consent(self):
        hidden = self.message('whatsapp_messages', channel='whatsapp')
        shown = self.message()
        self.consent('email', 'ai_text')
        self.conn.execute('INSERT INTO tasks(source_message,title) VALUES (%s,%s),(%s,%s),(NULL,%s)',
                          (hidden, 'nascosta', shown, 'visibile', 'senza origine'))
        titles = sorted(r[0] for r in data.db_query(f'SELECT title FROM {self.views}.tasks')['rows'])
        self.assertEqual(titles, ['senza origine', 'visibile'])


class QueryAndReadTests(AgentPostgresCase):
    def setUp(self):
        super().setUp()
        for channel in ('email', 'whatsapp', 'telegram'):
            for effect in ('ai_text', 'ai_media'):
                self.consent(channel, effect)
        self.mid = self.message(subject='Fattura ottobre', body='Gentile cliente, la fattura numero 42 è scaduta. ' * 10)
        self.message(subject='Auguri', body='Buon compleanno!')
        self.message('whatsapp_messages', channel='whatsapp', body='ci vediamo domani')

    def test_free_sql_aggregates_and_full_text_search(self):
        count = data.db_query(f"SELECT source, count(*) FROM {self.views}.messages GROUP BY 1 ORDER BY 1")
        self.assertEqual(count['rows'], [['email', 2], ['whatsapp', 1]])
        found = data.db_query(f"""SELECT id FROM {self.views}.messages WHERE
            to_tsvector('italian', coalesce(subject,'')||' '||coalesce(body,'')) @@ websearch_to_tsquery('italian','fattura')""")
        self.assertEqual(found['rows'], [[self.mid]])

    def test_errors_are_explained_without_leaking_data(self):
        self.assertIn('colonna', data.db_query(f'SELECT nonexistent FROM {self.views}.messages')['error'].lower()
                      + 'colonna')
        self.assertIn('Una sola istruzione', data.db_query('SELECT 1; SELECT 2')['error'])
        self.assertIn('solo le viste', data.db_query('SELECT * FROM tasks')['error'])

    def test_slow_queries_are_cancelled_and_the_connection_survives(self):
        with self.assertRaises(Exception) as raised:
            sqlguard.execute(data.connect, 'SELECT count(*) FROM generate_series(1, 400000000)',
                             schema=self.views, timeout_ms=300)
        self.assertIn('Query troppo lenta', data.explain(raised.exception))
        self.assertEqual(data.db_query(f'SELECT count(*) FROM {self.views}.messages')['rows'], [[3]])

    def test_query_runs_in_a_read_only_transaction(self):
        # Anche se la guardia fosse aggirata, PostgreSQL rifiuta la scrittura.
        with data.connect() as conn:
            with conn.transaction():
                conn.execute('SET TRANSACTION READ ONLY')
                with self.assertRaises(Exception):
                    conn.execute('INSERT INTO notes(text) VALUES (%s)', ('x',))

    def test_read_message_returns_full_text_with_paging_and_attachments(self):
        att = self.attachment(self.mid, path='/var/lib/pi-secretary/email_attachments/aaa')
        result = data.read_message(self.mid, max_chars=200)
        self.assertEqual(result['subject'], 'Fattura ottobre')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['attachments'][0]['id'], att)
        self.assertEqual(result['attachments'][0]['path'], 'email_attachments/aaa')
        more = data.read_message(self.mid, offset=result['body_chars'] - 10)
        self.assertFalse(more['truncated'])
        self.assertEqual(len(more['body']), 10)

    def test_read_message_respects_consent_and_bad_ids(self):
        self.consent('email', 'ai_text', False)
        self.assertIn('error', data.read_message(self.mid))
        for bad in (0, -1, True and 'x', None, 10**30):
            self.assertIn('error', data.read_message(bad))

    def test_attachment_text_prefers_transcripts_then_db_bytes_then_file(self):
        voice = self.attachment(self.mid, channel='email', filename='voce.ogg', content_type='audio/ogg',
                                data_bytes=b'\x00\x01')
        self.conn.execute("INSERT INTO media_transcripts(attachment_id,kind,text,model) VALUES (%s,'audio',%s,'m')",
                          (voice, 'Buongiorno, richiamami'))
        self.assertEqual(data.attachment_text(voice)['text'], 'Buongiorno, richiamami')
        inline = self.attachment(self.mid, filename='nota.txt', content_type='text/plain',
                                 data_bytes='Testo nel database'.encode())
        self.assertEqual(data.attachment_text(inline)['source'], 'database')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'email_attachments').mkdir()
            (root / 'email_attachments' / 'abc').write_bytes(b'Totale fattura 100')
            legacy = self.attachment(self.mid, filename='fattura.txt', content_type='text/plain',
                                     path='/home/ale/pi-secretary/data/email_attachments/abc')
            missing = self.attachment(self.mid, filename='perso.pdf',
                                      path='/home/ale/pi-secretary/data/email_attachments/zzz')
            with patch.object(config, 'DATA_DIR', root), patch.object(config, 'INVOICE_DIR', root / 'invoices'):
                found = data.attachment_text(legacy)
                self.assertEqual((found['text'], found['source']), ('Totale fattura 100', 'file'))
                self.assertIn('non più disponibile', data.attachment_text(missing)['error'])

    def test_attachment_text_requires_media_consent(self):
        att = self.attachment(self.mid, data_bytes=b'x', filename='a.txt', content_type='text/plain')
        self.consent('email', 'ai_media', False)
        self.assertIn('error', data.attachment_text(att))

    def test_gate_path_requires_a_visible_attachment_reference(self):
        self.attachment(self.mid, path='/x/email_attachments/known')
        data.gate_path('email_attachments/known')
        with self.assertRaises(fsbox.FsError):
            data.gate_path('email_attachments/unknown')
        with self.assertRaises(fsbox.FsError):
            data.gate_path('tg_files/anything.ogg')
        data.gate_path('workspace/report.md')  # area dell'owner: nessun vincolo di allegato
        self.consent('email', 'ai_media', False)
        with self.assertRaises(fsbox.FsError):
            data.gate_path('email_attachments/known')

    def test_readonly_role_absent_means_ast_only(self):
        with patch.object(config, 'AGENT_DB_ROLE', 'agent_ro_does_not_exist'):
            data._role_state.update(checked=0.0, usable=False)
            self.assertIsNone(data.readonly_role())
        self.assertFalse(agent_schema.readonly_role_usable(self.conn, ''))


class JournalTests(AgentPostgresCase):
    def test_effect_runs_once_and_replays_the_stored_result(self):
        calls = []

        def apply(conn):
            calls.append(1)
            return dict(id=conn.execute("INSERT INTO notes(text) VALUES ('uno') RETURNING id").fetchone()[0])
        args = dict(text='uno')
        first, replayed = journal.once(self.conn, 'owner:1', 'note_save', args, apply)
        second, replayed_again = journal.once(self.conn, 'owner:1', 'note_save', args, apply)
        self.assertEqual((replayed, replayed_again), (False, True))
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM notes').fetchone()[0], 1)
        third, _ = journal.once(self.conn, 'owner:1', 'note_save', dict(text='due'), apply)
        self.assertNotEqual(third, first)
        other, _ = journal.once(self.conn, 'owner:2', 'note_save', args, apply)
        self.assertNotEqual(other, first)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM notes').fetchone()[0], 3)

    def test_failed_effect_leaves_neither_effect_nor_journal_row(self):
        def boom(conn):
            conn.execute("INSERT INTO notes(text) VALUES ('parziale')")
            raise ValueError('rifiutato')
        with self.assertRaises(ValueError):
            journal.once(self.conn, 'owner:9', 'note_save', dict(x=1), boom)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM notes').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM agent_effects').fetchone()[0], 0)
        result, replayed = journal.once(self.conn, 'owner:9', 'note_save', dict(x=1),
                                        lambda conn: dict(ok=True))  # un retry può riuscire
        self.assertEqual((result, replayed), (dict(ok=True), False))

    def test_argument_key_is_order_independent(self):
        self.assertEqual(journal.effect_key('t', {'a': 1, 'b': 2}), journal.effect_key('t', {'b': 2, 'a': 1}))
        self.assertNotEqual(journal.effect_key('t', {'a': 1}), journal.effect_key('u', {'a': 1}))

    def test_run_audit_is_bounded_and_survives_logging_failures(self):
        run = journal.start_run('owner', 'owner:5', 1, 5, 'openrouter/auto')
        journal.log_tool(run, 1, 'db_query', 'SELECT ' + 'x' * 5000, True, 'y' * 2000, 12)
        journal.finish_run(run, 'done', 1)
        args, summary = self.conn.execute('SELECT args,summary FROM agent_tool_calls WHERE run_id=%s', (run,)).fetchone()
        self.assertLessEqual(len(args), 2001)
        self.assertLessEqual(len(summary), 501)
        with patch.object(db, '_conn', side_effect=RuntimeError('db down')):
            journal.log_tool(run, 2, 'db_query', 'x', True, 'y', 1)  # non solleva
        self.assertEqual(journal.executed('owner:none'), [])



class ReadonlyRoleIntegrationTests(AgentPostgresCase):
    """db.init() predispone il ruolo e le query libere girano davvero con quel ruolo."""

    def test_init_provisions_the_role_and_free_queries_run_under_it(self):
        role = config.AGENT_DB_ROLE
        data._role_state.update(checked=0.0, usable=False, role=None)
        self.assertEqual(data.readonly_role(), role)
        self.consent('email', 'ai_text')
        self.message()
        with patch.object(data.sqlguard, 'execute', wraps=data.sqlguard.execute) as spy:
            rows = data.db_query(f'SELECT id FROM {self.views}.messages')['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(spy.call_args.kwargs['role'], role)

    def test_the_role_cannot_touch_base_tables_even_without_the_sql_guard(self):
        """Seconda barriera, indipendente dall'AST: il ruolo vede solo le viste."""
        import psycopg
        role = config.AGENT_DB_ROLE
        self.message()
        for relation in (f'"{self.schema}".email_messages', f'"{self.schema}".data_policies',
                         f'"{self.schema}".secretary_ai_usage'):
            conn = self.plain()
            try:
                conn.execute(f'SET LOCAL ROLE "{role}"')
                with self.subTest(relation=relation), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(f'SELECT count(*) FROM {relation}')
            finally:
                conn.rollback()
                conn.close()
        conn = self.plain()
        try:
            conn.execute(f'SET LOCAL ROLE "{role}"')
            conn.execute('SET TRANSACTION READ ONLY')
            self.assertEqual(conn.execute(f'SELECT count(*) FROM "{self.views}".messages').fetchone()[0], 0)
        finally:
            conn.rollback()
            conn.close()

    def test_revoking_a_consent_still_hides_data_from_the_role_immediately(self):
        data._role_state.update(checked=0.0, usable=False, role=None)
        self.assertEqual(data.readonly_role(), config.AGENT_DB_ROLE)
        self.consent('email', 'ai_text')
        self.message()
        self.assertEqual(len(data.db_query(f'SELECT id FROM {self.views}.messages')['rows']), 1)
        self.consent('email', 'ai_text', False)
        self.assertEqual(data.db_query(f'SELECT id FROM {self.views}.messages')['rows'], [])

    def test_the_role_cache_follows_the_configured_name(self):
        data._role_state.update(checked=0.0, usable=False, role=None)
        self.assertEqual(data.readonly_role(), config.AGENT_DB_ROLE)
        with patch.object(config, 'AGENT_DB_ROLE', 'agent_ro_does_not_exist'):
            self.assertIsNone(data.readonly_role())  # senza attendere la scadenza della cache
        self.assertEqual(data.readonly_role(), config.AGENT_DB_ROLE)


@unittest.skipUnless(URL, 'Set PI_SECRETARY_TEST_DATABASE_URL to opt in')
class ReadonlyRoleProvisioningTests(unittest.TestCase):
    """Chi può creare il ruolo lo crea; chi non può lo segnala senza rompere l'avvio.

    Ruoli di login e schemi hanno nomi unici e vengono rimossi: il database di test è
    condiviso. Serve un utente di test con CREATEROLE o superuser.
    """

    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(URL)
        if not params.get('dbname', '').endswith('_test') or params.get('service'):
            raise ValueError('An explicit database ending in _test (no service) is required')

    def setUp(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        self.psycopg, self.make_conninfo = psycopg, make_conninfo
        self.admin = psycopg.connect(URL, autocommit=True, connect_timeout=5)
        self.addCleanup(self.admin.close)
        privileged = self.admin.execute(
            'SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()[0]
        if not privileged:
            self.skipTest('Serve un utente di test con CREATEROLE per creare ruoli temporanei')
        self.tag = uuid.uuid4().hex[:10]
        self.role = 'agent_ro_t_' + self.tag
        self.schema = 'agent_t_' + self.tag
        self.base = 'base_t_' + self.tag
        self.logins, self.opened = [], []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for conn in self.opened:
            conn.close()
        self.admin.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        self.admin.execute(f'DROP SCHEMA IF EXISTS "{self.base}" CASCADE')
        for name in [self.role, *self.logins]:
            if self.admin.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (name,)).fetchone():
                self.admin.execute(f'DROP OWNED BY "{name}"')
                self.admin.execute(f'DROP ROLE "{name}"')

    def login(self, name, attributes=''):
        """Utente applicativo di prova con i soli privilegi indicati; ritorna la sua connessione."""
        name = f'app_t_{name}_{self.tag}'
        self.admin.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD 'test-only' {attributes}")
        self.logins.append(name)
        for schema in (self.schema, self.base):
            self.admin.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            self.admin.execute(f'ALTER SCHEMA "{schema}" OWNER TO "{name}"')
        conn = self.psycopg.connect(self.make_conninfo(URL, user=name, password='test-only'),
                                    autocommit=True, connect_timeout=5)
        self.opened.append(conn)
        conn.execute(f'CREATE TABLE "{self.base}".secrets(id int)')
        conn.execute(f'CREATE TABLE "{self.base}".facts(id int)')
        conn.execute(f'CREATE VIEW "{self.schema}".messages AS SELECT id FROM "{self.base}".facts')
        return conn

    def ensure(self, conn, role=None):
        return agent_schema.ensure_readonly_role(conn, self.schema, self.role if role is None else role)

    def reads(self, conn, relation):
        """True se il ruolo legge la relazione, False se gli viene negata.

        Un errore in SET ROLE NON significa «non legge»: significa che il ruolo non è assumibile
        (bug diverso, più grave), quindi si propaga e fa fallire il test.
        """
        with conn.transaction():
            conn.execute(f'SET LOCAL ROLE "{self.role}"')
            try:
                with conn.transaction():
                    conn.execute(f'SELECT count(*) FROM {relation}')
                return True
            except self.psycopg.errors.InsufficientPrivilege:
                return False

    def role_row(self):
        return self.admin.execute(
            'SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls '
            'FROM pg_roles WHERE rolname=%s', (self.role,)).fetchone()

    def test_an_admin_user_creates_a_least_privilege_role_that_only_sees_the_views(self):
        app = self.login('admin', 'CREATEROLE')
        self.assertEqual(self.ensure(app), 'created')
        self.assertEqual(self.role_row(), (False,) * 6)  # nessun login, nessun attributo
        self.assertTrue(self.reads(app, f'"{self.schema}".messages'))
        self.assertFalse(self.reads(app, f'"{self.base}".secrets'))
        self.assertFalse(self.reads(app, f'"{self.base}".facts'))  # le tabelle sotto la vista restano chiuse
        app.execute(f'CREATE VIEW "{self.schema}".later AS SELECT id FROM "{self.base}".facts')
        self.assertTrue(self.reads(app, f'"{self.schema}".later'))  # viste future: SELECT ereditato
        self.assertEqual(self.ensure(app), 'ready')

    def test_membership_as_mere_administrator_is_not_enough_to_assume_the_role(self):
        """PostgreSQL 16+: chi crea un ruolo ne è membro solo come amministratore e SET ROLE gli è negato."""
        app = self.login('admin', 'CREATEROLE')
        if app.execute("SELECT current_setting('server_version_num')::int").fetchone()[0] < 160000:
            self.skipTest('Il privilegio SET dei ruoli esiste da PostgreSQL 16')
        app.execute(f'CREATE ROLE "{self.role}" NOLOGIN')
        self.assertTrue(app.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", (self.role,)).fetchone()[0])
        self.assertFalse(agent_schema.readonly_role_usable(app, self.role))  # MEMBER sì, ma niente SET ROLE
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            app.execute(f'SET ROLE "{self.role}"')
        self.assertEqual(self.ensure(app), 'granted')  # il GRANT esplicito lo rende assumibile
        self.assertTrue(agent_schema.readonly_role_usable(app, self.role))
        self.assertTrue(self.reads(app, f'"{self.schema}".messages'))

    def test_a_role_created_by_the_dba_only_needs_membership_the_app_does_the_grants(self):
        app = self.login('plain')
        self.admin.execute(f'CREATE ROLE "{self.role}" NOLOGIN')
        self.admin.execute(f'GRANT "{self.role}" TO "{self.logins[-1]}"')
        self.assertFalse(self.reads(app, f'"{self.schema}".messages'))  # ancora nessun SELECT
        self.assertEqual(self.ensure(app), 'granted')
        self.assertTrue(self.reads(app, f'"{self.schema}".messages'))
        self.assertFalse(self.reads(app, f'"{self.base}".facts'))
        app.execute(f'CREATE VIEW "{self.schema}".later AS SELECT id FROM "{self.base}".facts')
        self.assertTrue(self.reads(app, f'"{self.schema}".later'))
        self.assertEqual(self.ensure(app), 'ready')

    def test_the_dba_may_run_every_grant_first_and_the_order_does_not_matter(self):
        app = self.login('plain')
        self.admin.execute(f'CREATE ROLE "{self.role}" NOLOGIN')
        self.admin.execute(f'GRANT "{self.role}" TO "{self.logins[-1]}"')
        self.admin.execute(f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{self.role}"')
        self.admin.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{self.schema}" TO "{self.role}"')
        self.assertEqual(self.ensure(app), 'ready')
        self.assertTrue(self.reads(app, f'"{self.schema}".messages'))

    def test_without_privileges_the_app_reports_needs_dba_and_creates_nothing(self):
        app = self.login('plain')
        self.assertEqual(self.ensure(app), 'needs_dba')
        self.assertIsNone(self.admin.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (self.role,)).fetchone())

    def test_an_existing_role_the_app_cannot_assume_is_reported_not_forced(self):
        app = self.login('plain')
        self.admin.execute(f'CREATE ROLE "{self.role}" NOLOGIN')  # nessun GRANT all'utente applicativo
        self.assertEqual(self.ensure(app), 'needs_dba')
        self.assertFalse(self.admin.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER')", (self.logins[-1], self.role)).fetchone()[0])

    def test_a_failure_rolls_back_so_no_half_configured_role_is_left(self):
        app = self.login('admin', 'CREATEROLE')
        self.assertEqual(agent_schema.ensure_readonly_role(app, self.schema + '_missing', self.role), 'error')
        self.assertIsNone(self.admin.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (self.role,)).fetchone())

    def test_an_empty_role_name_disables_provisioning_and_bad_names_are_rejected(self):
        app = self.login('admin', 'CREATEROLE')
        self.assertEqual(self.ensure(app, role=''), 'disabled')
        for bad in ('Agent_RO', 'ro; DROP ROLE x', 'a' * 60, 'ro-1', 7):
            with self.subTest(role=bad), self.assertRaises(ValueError):
                agent_schema.ensure_readonly_role(app, self.schema, bad)
        for bad in ('Bad', 'x; y', ''):
            with self.subTest(schema=bad), self.assertRaises(ValueError):
                agent_schema.ensure_readonly_role(app, bad, self.role)


if __name__ == '__main__':
    unittest.main()
