"""Provenance regressions: disposable socket-only PostgreSQL, no network/AI.

Storage tests start an isolated cluster when PostgreSQL binaries are installed;
they never connect to config.DATABASE_URL or reuse an existing database.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import db
import historical_import
import message_store
import task_reconciliation
from connectors import media_archive, telegram_account as tg, whatsapp


class ProvenanceStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        candidates = sorted(Path('/usr/lib/postgresql').glob('*/bin/initdb'))
        initdb = shutil.which('initdb') or (str(candidates[-1]) if candidates else None)
        if not initdb or os.geteuid() == 0:
            raise unittest.SkipTest('Local PostgreSQL binaries and non-root user required')
        directory = cls.enterClassContext(tempfile.TemporaryDirectory(prefix='provenance-'))
        cls.pg_ctl = str(Path(initdb).with_name('pg_ctl'))
        cls.data = str(Path(directory) / 'cluster')
        subprocess.run([initdb, '-D', cls.data, '-A', 'trust', '--no-locale', '-E', 'UTF8'],
                       check=True, capture_output=True)
        subprocess.run([cls.pg_ctl, '-D', cls.data, '-l', directory + '/postgres.log',
                        '-o', f"-F -c listen_addresses='' -k {directory}", '-w', 'start'],
                       check=True, capture_output=True)
        cls.addClassCleanup(subprocess.run,
                            [cls.pg_ctl, '-D', cls.data, '-m', 'immediate', '-w', 'stop'],
                            check=True, capture_output=True)
        cls.conn = psycopg.connect(host=directory, dbname='postgres', autocommit=True)
        cls.addClassCleanup(cls.conn.close)
        message_store.migrate(cls.conn, '')
        cls.conn.execute('CREATE TABLE queued (mid BIGINT, revision TEXT)')

    def setUp(self):
        self.conn.execute('TRUNCATE email_messages, whatsapp_messages, telegram_messages, '
                          'attachments, queued')
        self.enterContext(patch.object(media_archive, 'enqueue_message'))
        self.queue = self.enterContext(patch.object(task_reconciliation, 'queue',
            side_effect=lambda conn, mid, revision='': conn.execute(
                'INSERT INTO queued VALUES (%s,%s)', (mid, revision))))

    def store(self, channel='telegram (account)', revision=None, **kwargs):
        meta = dict(kwargs.pop('metadata', {}))
        if channel == 'telegram (account)':
            meta['revision_date'] = revision or '2026-10-14T09:00:00+00:00'
            kwargs.setdefault('update_existing', True)
        return message_store.store(self.conn, channel, 'sender', 'subject', 'body',
                                   source_id='native', metadata=meta, **kwargs)

    def metadata(self, mid, channel='telegram (account)'):
        return self.conn.execute(
            f'SELECT metadata FROM {message_store.TABLES[message_store.native(channel)]} '
            'WHERE id=%s', (mid,)).fetchone()[0]

    def queued(self):
        return self.conn.execute('SELECT mid,revision FROM queued').fetchall()

    def test_historical_duplicates_never_queue_in_all_native_tables(self):
        for channel in ('email (personal)', 'whatsapp', 'telegram', 'telegram (account)'):
            with self.subTest(channel=channel):
                mid = self.store(channel, historical=True, reconcile=True)
                self.assertTrue(self.metadata(mid, channel)['historical'])
                for _ in range(2):
                    self.assertEqual(self.store(channel, reconcile=True), mid)
                self.assertTrue(message_store.reconciliation_excluded(self.conn, mid))
                self.assertEqual(self.queued(), [])

    def test_equal_revision_with_different_offset_is_not_a_later_edit(self):
        mid = self.store(historical=True)
        self.store(revision='2026-10-14T11:00:00+02:00', reconcile=True)
        self.assertTrue(self.metadata(mid)['historical'])
        self.assertEqual(self.queued(), [])

    def test_stale_live_revision_does_not_promote(self):
        mid = self.store(historical=True)
        self.store(revision='2026-10-14T08:59:59+00:00', reconcile=True)
        self.assertTrue(self.metadata(mid)['historical'])
        self.assertEqual(self.queued(), [])

    def test_strictly_later_live_edit_clears_marker_and_queues_revision(self):
        mid = self.store(historical=True)
        self.store(revision='2026-10-14T12:00:00+02:00', reconcile=True)
        self.assertNotIn('historical', self.metadata(mid))
        self.assertFalse(message_store.reconciliation_excluded(self.conn, mid))
        self.assertEqual(self.queued(), [(mid, '2026-10-14T10:00:00+00:00')])

    def test_later_historical_edit_preserves_exclusion(self):
        mid = self.store(historical=True)
        revision = '2026-10-14T10:00:00+00:00'
        self.store(revision=revision, historical=True, reconcile=True)
        self.store(revision=revision, reconcile=True)
        self.assertTrue(self.metadata(mid)['historical'])
        self.assertEqual(self.queued(), [])

    def test_default_false_is_not_historical_and_pipeline_duplicate_can_queue(self):
        for channel in ('email (personal)', 'whatsapp', 'telegram', 'telegram (account)'):
            with self.subTest(channel=channel):
                mid = self.store(channel)
                self.assertNotIn('historical', self.metadata(mid, channel))
                self.assertFalse(message_store.reconciliation_excluded(self.conn, mid))
                before = len(self.queued())
                self.store(channel, reconcile=True)
                self.assertEqual(len(self.queued()), before + 1)

    def test_history_reimport_does_not_reclassify_live_identity(self):
        for channel in ('email (personal)', 'whatsapp', 'telegram', 'telegram (account)'):
            with self.subTest(channel=channel):
                mid = self.store(channel)
                self.store(channel, historical=True)
                self.assertNotIn('historical', self.metadata(mid, channel))

    def test_whatsapp_enrichment_cannot_clear_or_spoof_provenance(self):
        mid = self.store('whatsapp', historical=True,
                         metadata={'chat_name': 'raw@lid'})
        for historical in (True, False):
            self.store('whatsapp', historical=historical, reconcile=True,
                       metadata={'chat_name': 'Anna', 'chat_name_rank': 3,
                                 'historical': False},
                       attachments=[{'filename': 'photo', 'path': '/offline/photo'}])
        meta = self.metadata(mid, 'whatsapp')
        self.assertTrue(meta['historical'])
        self.assertEqual(meta['chat_name'], 'Anna')
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM attachments').fetchone()[0], 1)

    def test_queue_failure_rolls_back_promotion_and_attachment_changes(self):
        mid = self.store(historical=True,
                         attachments=[{'filename': 'original', 'data': b'original'}])
        self.queue.side_effect = RuntimeError('queue failed')
        with self.assertRaisesRegex(RuntimeError, 'queue failed'):
            self.store(revision='2026-10-14T10:00:00+00:00', reconcile=True,
                       attachments=[{'filename': 'edit', 'data': b'edit'}])
        self.assertTrue(self.metadata(mid)['historical'])
        self.assertEqual(self.metadata(mid)['revision_date'], '2026-10-14T09:00:00+00:00')
        self.assertEqual(self.conn.execute('SELECT filename FROM attachments').fetchall(),
                         [('original',)])
        self.assertEqual(self.queued(), [])

    def test_caller_rollback_removes_historical_insert(self):
        with self.assertRaises(RuntimeError), self.conn.transaction():
            self.store(historical=True)
            raise RuntimeError('abort')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM telegram_messages').fetchone()[0], 0)

    def test_supplied_id_helper_uses_native_metadata_not_view(self):
        mid = self.store('email (personal)', historical=True)
        columns = [c.name for c in self.conn.execute('SELECT * FROM communications LIMIT 0').description]
        self.assertNotIn('metadata', columns)
        self.assertTrue(message_store.reconciliation_excluded(self.conn, mid))
        self.assertFalse(message_store.reconciliation_excluded(self.conn, 999999))


class ProvenanceCallerTests(unittest.TestCase):
    def test_db_forwards_explicit_history(self):
        conn = Mock()
        with patch.object(db, '_conn', return_value=nullcontext(conn)), \
             patch.object(message_store, 'store', return_value=42) as store:
            self.assertEqual(db.store_message('telegram', 's', '', '', historical=True), 42)
        self.assertTrue(store.call_args.kwargs['historical'])

    def test_email_import_marks_history(self):
        from connectors import email
        message = dict(ts='2026-10-14T09:00:00+00:00', source_id='native',
                       metadata={}, attachments=[], subject='', body='', **{'from': 'sender'})
        with patch.object(db, 'init'), patch.object(historical_import, 'print', create=True), \
             patch('flows.init'), patch('flows.settings', return_value={'email': True}), \
             patch('config.EMAIL_ACCOUNTS', [{'name': 'test'}]), \
             patch.object(email, 'iter_history_messages', return_value=[message]), \
             patch.object(db, 'store_message') as store:
            results = historical_import.run_import(1)
        self.assertEqual(results[0]['status'], 'ok')
        self.assertTrue(store.call_args.kwargs['historical'])

    def test_whatsapp_history_storage_explicitly_opts_in(self):
        # Verify the actual storage call, without launching Chromium or a bridge.
        import ast
        tree = ast.parse(Path(whatsapp.__file__).read_text())
        history = next(node for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == 'import_history')
        stores = [node for node in ast.walk(history) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == 'store_message']
        self.assertEqual(len(stores), 1)
        self.assertIs(next(kw.value.value for kw in stores[0].keywords
                           if kw.arg == 'historical'), True)


class TelegramProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_false_does_not_imply_history(self):
        now = datetime.now(timezone.utc)
        message = SimpleNamespace(id=42, chat_id=7, sender_id=8, date=now, edit_date=None,
            action=None, out=False, is_private=True, is_group=False, media=None,
            raw_text='body', sender=SimpleNamespace(first_name='Anna'),
            chat=SimpleNamespace(first_name='Anna'))
        worker = tg.TelegramAccountWorker()
        worker.account = 'user:9'
        with patch.object(worker, 'media_attachments', AsyncMock(return_value=[])), \
             patch.object(db, 'store_message') as store:
            for historical in (False, True):
                await worker.archive(message, recovered=True, reconcile=False,
                    historical=historical, window=(now - timedelta(days=1), now + timedelta(days=1)))
                self.assertIs(store.call_args.kwargs['historical'], historical)

    async def test_history_scan_passes_explicit_provenance(self):
        now = datetime.now(timezone.utc)
        client = Mock(connect=AsyncMock(), disconnect=AsyncMock(),
                      is_user_authorized=AsyncMock(return_value=True),
                      get_me=AsyncMock(return_value=SimpleNamespace(id=9, bot=False)))

        async def dialogs(**kwargs):
            yield SimpleNamespace(entity=7)

        async def messages(*args, **kwargs):
            yield SimpleNamespace(date=now, chat_id=7, id=42, edit_date=None)

        client.iter_dialogs.side_effect = dialogs
        client.iter_messages.side_effect = messages
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'personal.session'
            path.touch()
            with patch.dict(sys.modules, telethon=SimpleNamespace(TelegramClient=Mock(return_value=client))), \
                 patch.object(tg, 'session_path', return_value=path), \
                 patch.object(tg, 'api_credentials', return_value=(1, 'offline')), \
                 patch.object(tg.TelegramAccountWorker, 'archive', AsyncMock(return_value=True)) as archive:
                self.assertEqual(await tg.import_history(now - timedelta(days=1),
                                                         now + timedelta(days=1)), 1)
            self.assertTrue(archive.call_args.kwargs['historical'])
            self.assertFalse(archive.call_args.kwargs['reconcile'])


if __name__ == '__main__':
    unittest.main()