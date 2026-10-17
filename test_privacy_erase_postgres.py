"""PostgreSQL integration tests for the coordinated privacy erase/export.

Real schema, real triggers, real FKs: no mocks. Exercises the full db.init()
bootstrap, then verifies that erase_message removes every derived row and
respects the append-only audit trail, and that export is a faithful snapshot.
Requires PI_SECRETARY_TEST_DATABASE_URL (explicit *_test DB, Unix socket).
"""
import os
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import config
import db
import privacy
import secretary as sec


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
        with patch.object(db, 'available', return_value=True), \
             patch.object(db, '_conn', side_effect=lambda: _NullContext(self.conn)):
            self.assertEqual(db.init(), 0)
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:not-a-secret'))
        self.owner = sec.DashboardOwner('101', True, True)

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
        # A practice linked to this message as evidence (owner origin required).
        owner_mid = self._owner_message()
        practice = self.conn.execute('''INSERT INTO secretary_practices
            (objective, state, version, next_action, waiting_for, blocked_reason,
             next_review_at, completion_criteria, origin_message, operation_key)
            VALUES ('riparazione casa', 'active', 1, 'chiamare', '', '', NULL,
            'fatto', %s, %s) RETURNING id''',
            (owner_mid, 'op-' + uuid.uuid4().hex[:8])).fetchone()[0]
        column = {'telegram': 'telegram_message_id', 'email': 'email_message_id',
                  'whatsapp': 'whatsapp_message_id'}['telegram']
        self.conn.execute(f'''INSERT INTO secretary_links(practice_id, {column},
            evidence, origin_message) VALUES (%s, %s, 'testo privato', %s)''',
            (practice, mid, owner_mid))
        # Export BEFORE erase: everything must be visible.
        exported = privacy.export_message(self.conn, mid, self.owner)
        self.assertEqual(exported['message_id'], mid)
        self.assertEqual(len(exported['analysis']), 1)
        self.assertEqual(len(exported['notes']), 1)
        self.assertEqual(len(exported['task_evidence']), 1)
        self.assertEqual(len(exported['knowledge']), 1)
        self.assertEqual(len(exported['practice_links']), 1)
        self.assertEqual(len(exported['attachments']), 1)
        self.assertNotIn('data', exported['attachments'][0])  # no raw bytes in export
        # Erase.
        report = privacy.erase_message(self.conn, mid, self.owner,
                                       operation_key='secretary-privacy-erase:' + uuid.uuid4().hex)
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
        self.assertIsNone(self.conn.execute(
            f'SELECT 1 FROM secretary_links WHERE {column}=%s', (mid,)).fetchone())
        self.assertIsNone(self.conn.execute(
            'SELECT 1 FROM telegram_messages WHERE id=%s', (mid,)).fetchone())
        # The task itself survives (it is the owner's work, not the evidence).
        self.assertIsNotNone(self.conn.execute(
            'SELECT 1 FROM tasks WHERE id=%s', (seeded['task'],)).fetchone())
        # The practice and its owner origin survive untouched.
        self.assertIsNotNone(self.conn.execute(
            'SELECT 1 FROM secretary_practices WHERE id=%s', (practice,)).fetchone())
        # The erase receipt is in the append-only audit trail.
        receipt = self.conn.execute('''SELECT result FROM secretary_receipts
            WHERE operation_key LIKE 'secretary-privacy-erase:%'
            ORDER BY created_at DESC LIMIT 1''').fetchone()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt[0]['message_id'], mid)

    def test_erase_is_idempotent_via_receipt_replay(self):
        mid = self._third_party_message(channel='email')
        self._seed_everything(mid, channel='email')
        key = 'secretary-privacy-erase:' + uuid.uuid4().hex
        first = privacy.erase_message(self.conn, mid, self.owner, operation_key=key)
        second = privacy.erase_message(self.conn, mid, self.owner, operation_key=key)
        self.assertEqual(first, second)  # receipt replay returns the original report

    def test_erase_requires_authenticated_owner(self):
        mid = self._third_party_message()
        for bad in (sec.DashboardOwner('202', True, True),      # wrong owner
                    sec.DashboardOwner('101', False, True),    # not authenticated
                    sec.DashboardOwner('101', True, False),     # no CSRF
                    None, 'owner', {}):
            with self.subTest(caller=bad), self.assertRaises(sec.Unauthorized):
                privacy.erase_message(self.conn, mid, bad,
                                     operation_key='secretary-privacy-erase:x')
        # Nothing was erased.
        self.assertIsNotNone(self.conn.execute(
            'SELECT 1 FROM telegram_messages WHERE id=%s', (mid,)).fetchone())

    def test_erase_unknown_message_fails_closed(self):
        with self.assertRaises(sec.Conflict):
            privacy.erase_message(self.conn, 999999, self.owner,
                                 operation_key='secretary-privacy-erase:unknown')

    def test_erase_owner_authority_message_is_refused(self):
        """The live owner command stream is not erasable via this API."""
        mid = self._owner_message()
        with self.assertRaises(sec.Conflict):
            privacy.erase_message(self.conn, mid, self.owner,
                                 operation_key='secretary-privacy-erase:owner')


class _NullContext:
    def __init__(self, conn):
        self.conn = conn
    def __enter__(self):
        return self.conn
    def __exit__(self, *exc):
        return False


if __name__ == '__main__':
    unittest.main()
