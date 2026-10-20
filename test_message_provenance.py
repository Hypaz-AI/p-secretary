"""Provenienza dello storico: il marchio «storico» non si perde né si falsifica.

Archiviazione su PostgreSQL di test (mai config.DATABASE_URL), nessuna rete né AI.
Lo storico resta leggibile dall'agente SOLO con il consenso ai_history e non è mai autorità operativa.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import db
import historical_import
import message_store
from connectors import telegram_account as tg, whatsapp
from test_agent_postgres import AgentPostgresCase


class ProvenanceStorageTests(AgentPostgresCase):
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

    def test_historical_duplicates_stay_historical_in_all_native_tables(self):
        for channel in ('email (personal)', 'whatsapp', 'telegram', 'telegram (account)'):
            with self.subTest(channel=channel):
                mid = self.store(channel, historical=True)
                self.assertTrue(self.metadata(mid, channel)['historical'])
                for _ in range(2):
                    self.assertEqual(self.store(channel), mid)
                self.assertTrue(self.metadata(mid, channel)['historical'])

    def test_equal_revision_with_different_offset_is_not_a_later_edit(self):
        mid = self.store(historical=True)
        self.store(revision='2026-10-14T11:00:00+02:00')
        self.assertTrue(self.metadata(mid)['historical'])

    def test_stale_live_revision_does_not_promote(self):
        mid = self.store(historical=True)
        self.store(revision='2026-10-14T08:59:59+00:00')
        self.assertTrue(self.metadata(mid)['historical'])

    def test_strictly_later_live_edit_clears_the_marker(self):
        mid = self.store(historical=True)
        self.store(revision='2026-10-14T12:00:00+02:00')
        self.assertNotIn('historical', self.metadata(mid))

    def test_later_historical_edit_preserves_the_marker(self):
        mid = self.store(historical=True)
        revision = '2026-10-14T10:00:00+00:00'
        self.store(revision=revision, historical=True)
        self.store(revision=revision)
        self.assertTrue(self.metadata(mid)['historical'])

    def test_default_false_is_not_historical(self):
        for channel in ('email (personal)', 'whatsapp', 'telegram', 'telegram (account)'):
            with self.subTest(channel=channel):
                mid = self.store(channel)
                self.assertNotIn('historical', self.metadata(mid, channel))

    def test_history_reimport_does_not_reclassify_a_live_identity(self):
        for channel in ('email (personal)', 'whatsapp', 'telegram', 'telegram (account)'):
            with self.subTest(channel=channel):
                mid = self.store(channel)
                self.store(channel, historical=True)
                self.assertNotIn('historical', self.metadata(mid, channel))

    def test_transport_metadata_cannot_claim_or_clear_the_marker(self):
        live = self.store('email (personal)', metadata={'historical': True})
        self.assertNotIn('historical', self.metadata(live, 'email (personal)'))
        old = self.store('whatsapp', historical=True)
        self.store('whatsapp', metadata={'historical': False})
        self.assertTrue(self.metadata(old, 'whatsapp')['historical'])

    def test_whatsapp_enrichment_cannot_clear_or_spoof_provenance(self):
        mid = self.store('whatsapp', historical=True, metadata={'chat_name': 'raw@lid'})
        for historical in (True, False):
            self.store('whatsapp', historical=historical,
                       metadata={'chat_name': 'Anna', 'chat_name_rank': 3, 'historical': False},
                       attachments=[{'filename': 'photo', 'path': '/offline/photo'}])
        meta = self.metadata(mid, 'whatsapp')
        self.assertTrue(meta['historical'])
        self.assertEqual(meta['chat_name'], 'Anna')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM attachments').fetchone()[0], 1)

    def test_history_is_not_readable_by_the_agent_without_its_own_consent(self):
        mid = self.store('email (personal)', historical=True)
        live = self.store('whatsapp')
        for channel in ('email', 'whatsapp'):
            self.consent(channel, 'ai_text')
        visible = {r[0] for r in self.conn.execute(f'SELECT id FROM {self.views}.messages')}
        self.assertEqual(visible & {mid, live}, {live})
        self.consent('email', 'ai_history')
        visible = {r[0] for r in self.conn.execute(f'SELECT id FROM {self.views}.messages')}
        self.assertEqual(visible & {mid, live}, {mid, live})

    def test_caller_rollback_removes_the_historical_insert(self):
        with self.assertRaisesRegex(RuntimeError, '^abort$'), self.conn.transaction():
            self.store(historical=True)
            raise RuntimeError('abort')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM telegram_messages').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 0)

    def test_failed_derived_work_rolls_back_promotion_and_attachment_changes(self):
        mid = self.store(historical=True, attachments=[{'filename': 'original', 'data': b'original'}])
        with patch.object(message_store, 'enqueue_derived', side_effect=RuntimeError('derived failed')):
            with self.assertRaisesRegex(RuntimeError, 'derived failed'):
                self.store(revision='2026-10-14T10:00:00+00:00', attachments=[{'filename': 'edit', 'data': b'edit'}])
        self.assertTrue(self.metadata(mid)['historical'])
        self.assertEqual(self.metadata(mid)['revision_date'], '2026-10-14T09:00:00+00:00')
        self.assertEqual(self.conn.execute('SELECT filename FROM attachments').fetchall(), [('original',)])


class ProvenanceCallerTests(unittest.TestCase):
    def test_db_forwards_explicit_history(self):
        conn = Mock()
        with patch.object(db, '_conn', return_value=nullcontext(conn)), \
             patch.object(message_store, 'store', return_value=42) as store:
            self.assertEqual(db.store_message('telegram', 's', '', '', historical=True), 42)
        self.assertTrue(store.call_args.kwargs['historical'])
        self.assertNotIn('reconcile', store.call_args.kwargs)

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
                await worker.archive(message, recovered=True, historical=historical,
                                     window=(now - timedelta(days=1), now + timedelta(days=1)))
                self.assertIs(store.call_args.kwargs['historical'], historical)
                self.assertNotIn('reconcile', store.call_args.kwargs)

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
            self.assertNotIn('reconcile', archive.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
