"""PostgreSQL integration tests for the coordinated privacy erase/export.

Real schema, real triggers, real FKs: no mocks. Exercises the full db.init()
bootstrap, then verifies that erase_message removes every derived row and
respects the append-only audit trail, and that export is a faithful snapshot.
Requires PI_SECRETARY_TEST_DATABASE_URL (explicit *_test DB, Unix socket).
"""
import json
import os
import unittest
import uuid

from unittest.mock import patch

from secretary.core import config, db, privacy, dashboard_owner as owners


def _params():
    url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
    if not url:
        raise unittest.SkipTest('Set PI_SECRETARY_TEST_DATABASE_URL to opt in')
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(url)
    if (not params.get('dbname', '').endswith('_test') or 'service' in params
            or any(char in params['dbname'] for char in '/:= \t\r\n')):
        raise ValueError('An explicit database ending in _test is required')
    return params


class PrivacyErasePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.params = _params()
        import psycopg
        cls.psycopg = psycopg

    def setUp(self):
        from psycopg import sql
        self.conn = self.psycopg.connect(**self.params, autocommit=True,
                                         prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.schema = 'privacy_erase_test_' + uuid.uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self._drop)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='15s'")
        self.conn.execute("SET lock_timeout='10s'")
        self.views = 'agent_priv_' + uuid.uuid4().hex[:12]
        self.addCleanup(lambda: self.conn.execute(f'DROP SCHEMA IF EXISTS {self.views} CASCADE'))
        self.enterContext(patch.object(config, 'AGENT_DB_SCHEMA', self.views))
        with patch.object(db, 'available', return_value=True), \
             patch.object(db, '_conn', side_effect=lambda: _NullContext(self.conn)):
            self.assertEqual(db.init(), 0)
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:not-a-secret'))
        self.owner = owners.DashboardOwner('101', True, True)

    def _drop(self):
        from psycopg import sql
        with self.conn.transaction():
            self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def _owner_message(self, text='organizza la riparazione'):
        """Insert a live direct owner bot message with the required attestations."""
        return self.conn.execute('''INSERT INTO telegram_messages
            (channel, body, account, chat_id, author, direction, source_id, metadata, ts)
            VALUES ('telegram', %s, 'bot:777', '101', '101', 'in', %s,
            jsonb_build_object('chat_type','private','historical',false,
                'content_origin','direct_text','is_forwarded',false,
                'account','bot:777','chat_id','101','author','101','direction','in'),
            now()) RETURNING id''', (text, 'src-' + uuid.uuid4().hex)).fetchone()[0]

    def _third_party_message(self, channel='telegram'):
        """A non-owner message: evidence, never authority."""
        table = {'telegram': 'telegram_messages', 'email': 'email_messages',
                 'whatsapp': 'whatsapp_messages'}[channel]
        if channel == 'telegram':
            return self.conn.execute('''INSERT INTO telegram_messages
                (channel, body, account, chat_id, author, direction, source_id, metadata, ts)
                VALUES ('telegram', 'preventivo del fornitore: 300 euro', 'bot:777', '101',
                '202', 'in', %s, jsonb_build_object('chat_type','private','historical',false,
                'content_origin','direct_text','is_forwarded',false), now()) RETURNING id''',
                ('src-' + uuid.uuid4().hex,)).fetchone()[0]
        if channel == 'email':
            return self.conn.execute('''INSERT INTO email_messages
                (channel, body, account, chat_id, author, direction, source_id, metadata, ts)
                VALUES ('email', 'preventivo email', 'acct', 'chat', 'fornitore@example.test',
                'in', %s, jsonb_build_object('historical',false), now()) RETURNING id''',
                ('src-' + uuid.uuid4().hex,)).fetchone()[0]
        return self.conn.execute('''INSERT INTO whatsapp_messages
            (channel, body, account, chat_id, author, direction, source_id, metadata, ts)
            VALUES ('whatsapp', 'preventivo whatsapp', 'acct', 'chat', 'fornitore',
            'in', %s, jsonb_build_object('historical',false), now()) RETURNING id''',
            ('src-' + uuid.uuid4().hex,)).fetchone()[0]

    def _seed_everything(self, mid, channel='telegram'):
        """Seed every derived artifact that references the message."""
        # Analysis verdict
        self.conn.execute('''INSERT INTO message_analysis(message_id, channel, summary, category)
            VALUES (%s, %s, 'riepilogo privato', 'casa')''', (mid, channel))
        # Personal note with provenance
        self.conn.execute('''INSERT INTO notes(text, kind, source_message)
            VALUES ('nota privata dal messaggio', 'personal', %s)''', (mid,))
        # Task + applied evidence change
        task = self.conn.execute('''INSERT INTO tasks(source_message, title)
            VALUES (%s, 'confermare preventivo') RETURNING id''', (mid,)).fetchone()[0]
        self.conn.execute('''INSERT INTO task_changes(task_id, message_id, evidence_key,
            operation, changes, reason, evidence, expected_version, status)
            VALUES (%s, %s, %s, 'update', '{}', 'test', 'evidenza privata', 0, 'applied')''',
            (task, mid, 'ev-' + uuid.uuid4().hex))
        # Knowledge run + entry (derived text)
        run = self.conn.execute('''INSERT INTO knowledge_runs(message_id, revision, state,
            attachment_count) VALUES (%s, %s, 'done', 0) RETURNING id''',
            (mid, 'rev-' + uuid.uuid4().hex[:8])).fetchone()[0]
        entry = self.conn.execute('''INSERT INTO knowledge_entries(run_id, source_type,
            source_key, message_id, channel, title, text, historical, status, source_ref)
            VALUES (%s, 'message', %s, %s, %s, 'titolo', 'testo privato indicizzato',
            false, 'ready', '{}'::jsonb) RETURNING id''',
            (run, mid, mid, channel)).fetchone()[0]
        # Attachment with DB bytes
        self.conn.execute('''INSERT INTO attachments(message_id, channel, filename,
            content_type, data, metadata) VALUES (%s, %s, 'doc.txt', 'text/plain',
            %s, '{}'::jsonb)''', (mid, channel, b'private bytes'))
        return dict(task=task, run=run, entry=entry)

    def test_erase_removes_every_derived_row_and_keeps_audit(self):
        mid = self._third_party_message()
        seeded = self._seed_everything(mid)
        # Audit dell'agente che cita il messaggio (e uno che non lo cita).
        run = self.conn.execute("INSERT INTO agent_runs(kind,turn_key) VALUES ('inbox','inbox:test') RETURNING id").fetchone()[0]
        for tool, args in (('triage', json.dumps(dict(message_id=mid, summary='riepilogo privato'))),
                           ('read_message', json.dumps(dict(message_id=mid + 1000)))):
            self.conn.execute('''INSERT INTO agent_tool_calls(run_id,seq,tool,args,ok,summary)
                VALUES (%s,1,%s,%s,true,'ok')''', (run, tool, args))
        # Export BEFORE erase: everything must be visible.
        exported = privacy.export_message(self.conn, mid, self.owner)
        self.assertEqual(exported['message_id'], mid)
        self.assertEqual(len(exported['analysis']), 1)
        self.assertEqual(len(exported['notes']), 1)
        self.assertEqual(len(exported['task_evidence']), 1)
        self.assertEqual(len(exported['knowledge']), 1)
        self.assertEqual(len(exported['agent_tool_calls']), 1)
        self.assertEqual(len(exported['attachments']), 1)
        self.assertNotIn('data', exported['attachments'][0])  # no raw bytes in export
        # Erase.
        report = privacy.erase_message(self.conn, mid, self.owner,
                                       operation_key='privacy-erase:' + uuid.uuid4().hex)
        self.assertTrue(report['db_erased'])
        self.assertEqual(report['message_id'], mid)
        # Every derived row is gone.
        self.assertIsNone(self.conn.execute(
            'SELECT 1 FROM message_analysis WHERE message_id=%s', (mid,)).fetchone())
        self.assertIsNone(self.conn.execute(
            'SELECT 1 FROM notes WHERE source_message=%s', (mid,)).fetchone())
        self.assertIsNone(self.conn.execute(
            'SELECT 1 FROM task_changes WHERE message_id=%s', (mid,)).fetchone())
        self.assertIsNone(self.conn.execute(
            'SELECT 1 FROM knowledge_entries WHERE message_id=%s', (mid,)).fetchone())
        self.assertIsNone(self.conn.execute(
            'SELECT 1 FROM knowledge_runs WHERE message_id=%s', (mid,)).fetchone())
        self.assertIsNone(self.conn.execute(
            'SELECT 1 FROM attachments WHERE message_id=%s', (mid,)).fetchone())
        # Le chiamate dell'agente per QUESTO messaggio spariscono; le altre restano (id che inizia uguale).
        self.assertEqual(self.conn.execute('SELECT tool FROM agent_tool_calls').fetchall(), [('read_message',)])
        self.assertIsNone(self.conn.execute(
            'SELECT 1 FROM telegram_messages WHERE id=%s', (mid,)).fetchone())
        # The task itself survives (it is the owner's work, not the evidence).
        self.assertIsNotNone(self.conn.execute(
            'SELECT 1 FROM tasks WHERE id=%s', (seeded['task'],)).fetchone())
        # The erase receipt is kept as the audit trail.
        receipt = self.conn.execute('''SELECT result FROM privacy_receipts
            WHERE operation_key LIKE 'privacy-erase:%'
            ORDER BY created_at DESC LIMIT 1''').fetchone()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt[0]['message_id'], mid)

    def test_erase_succeeds_on_a_database_that_still_has_the_legacy_secretary_tables(self):
        """Produzione ha ancora le tabelle del vecchio segretario (FK + trigger append-only)."""
        mid = self._third_party_message()
        seeded = self._seed_everything(mid)
        owner_mid = self._owner_message()
        self.conn.execute('''CREATE TABLE secretary_practices (id BIGSERIAL PRIMARY KEY, objective TEXT);
            CREATE TABLE secretary_links (
              id BIGSERIAL PRIMARY KEY, practice_id BIGINT REFERENCES secretary_practices(id),
              email_message_id BIGINT REFERENCES email_messages(id),
              telegram_message_id BIGINT REFERENCES telegram_messages(id),
              whatsapp_message_id BIGINT REFERENCES whatsapp_messages(id),
              evidence TEXT, origin_message BIGINT REFERENCES telegram_messages(id));
            CREATE TABLE secretary_runtime_link_proposals (
              id BIGSERIAL PRIMARY KEY, practice_id BIGINT, message_id BIGINT NOT NULL,
              entry_id BIGINT NOT NULL REFERENCES knowledge_entries(id), evidence TEXT);
            CREATE FUNCTION secretary_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
              BEGIN RAISE EXCEPTION 'secretary append-only relation'; END $$;
            CREATE TRIGGER secretary_no_mutation BEFORE UPDATE OR DELETE ON secretary_links
              FOR EACH ROW EXECUTE FUNCTION secretary_immutable()''')
        practice = self.conn.execute("INSERT INTO secretary_practices(objective) VALUES ('casa') RETURNING id").fetchone()[0]
        self.conn.execute("INSERT INTO secretary_links(practice_id,telegram_message_id,evidence,origin_message) VALUES (%s,%s,'privato',%s)",
                          (practice, mid, owner_mid))
        self.conn.execute("INSERT INTO secretary_runtime_link_proposals(practice_id,message_id,entry_id,evidence) VALUES (%s,%s,%s,'privato')",
                          (practice, mid, seeded['entry']))
        report = privacy.erase_message(self.conn, mid, self.owner, operation_key='privacy-erase:legacy-tables')
        self.assertTrue(report['db_erased'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_links').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_runtime_link_proposals').fetchone()[0], 0)
        # Il trigger append-only è ripristinato dopo l'oblio.
        self.assertEqual(self.conn.execute("SELECT tgenabled FROM pg_trigger WHERE tgname='secretary_no_mutation'").fetchone()[0], 'O')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_practices').fetchone()[0], 1)

    def test_erase_requires_a_key_and_is_bound_to_its_request(self):
        mid = self._third_party_message()
        other = self._third_party_message(channel='email')
        for key in (None, '', 'x', 'k' * 181, 'privacy\x00key', 7):
            with self.subTest(key=key), self.assertRaises(ValueError):
                privacy.erase_message(self.conn, mid, self.owner, operation_key=key)
        privacy.erase_message(self.conn, mid, self.owner, operation_key='privacy-erase:same-key')
        with self.assertRaises(owners.Conflict):  # la stessa chiave non può cancellare un altro messaggio
            privacy.erase_message(self.conn, other, self.owner, operation_key='privacy-erase:same-key')
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM email_messages WHERE id=%s', (other,)).fetchone())


    def test_erase_is_idempotent_via_receipt_replay(self):
        mid = self._third_party_message(channel='email')
        self._seed_everything(mid, channel='email')
        key = 'privacy-erase:' + uuid.uuid4().hex
        first = privacy.erase_message(self.conn, mid, self.owner, operation_key=key)
        second = privacy.erase_message(self.conn, mid, self.owner, operation_key=key)
        self.assertEqual(first, second)  # receipt replay returns the original report

    def test_erase_requires_authenticated_owner(self):
        mid = self._third_party_message()
        for bad in (owners.DashboardOwner('202', True, True),      # wrong owner
                    owners.DashboardOwner('101', False, True),    # not authenticated
                    owners.DashboardOwner('101', True, False),     # no CSRF
                    None, 'owner', {}):
            with self.subTest(caller=bad), self.assertRaises(owners.Unauthorized):
                privacy.erase_message(self.conn, mid, bad,
                                     operation_key='privacy-erase:valid-key')
        # Nothing was erased.
        self.assertIsNotNone(self.conn.execute(
            'SELECT 1 FROM telegram_messages WHERE id=%s', (mid,)).fetchone())

    def test_erase_unknown_message_fails_closed(self):
        with self.assertRaises(owners.Conflict):
            privacy.erase_message(self.conn, 999999, self.owner,
                                 operation_key='privacy-erase:unknown')

    def test_erase_owner_authority_message_is_refused(self):
        """The live owner command stream is not erasable via this API."""
        mid = self._owner_message()
        with self.assertRaises(owners.Conflict):
            privacy.erase_message(self.conn, mid, self.owner,
                                 operation_key='privacy-erase:owner')


class _NullContext:
    def __init__(self, conn):
        self.conn = conn
    def __enter__(self):
        return self.conn
    def __exit__(self, *exc):
        return False


if __name__ == '__main__':
    unittest.main()
