"""Offline reconciliation ingress contracts: no network or PostgreSQL."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import db
import jobs
import main
import message_store
import task_reconciliation
from connectors import media_archive, telegram_account as tg


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.conn.execute.return_value.fetchone.return_value = (42,)
        self.queue = self.enterContext(patch.object(task_reconciliation, 'queue'))
        self.enterContext(patch.object(media_archive, 'enqueue_message'))
        # Keep base reconciliation contracts separate from bootstrap integration.
        self.enterContext(patch.object(message_store, 'enqueue_derived'))

    def store(self, channel='telegram', **kwargs):
        return message_store.store(self.conn, channel, 'sender', 'subject', 'body',
                                   source_id='native', **kwargs)

    def test_default_off_for_insert_and_duplicate(self):
        for rows in ([(42,)], [None, (42,)]):
            with self.subTest(rows=rows):
                self.conn.execute.return_value.fetchone.side_effect = rows
                self.assertEqual(self.store(), 42)
        self.queue.assert_not_called()

    def test_queue_insert_after_attachments_inside_transaction(self):
        events = []
        self.conn.transaction.return_value.__enter__.side_effect = lambda: events.append('begin')
        self.conn.transaction.return_value.__exit__.side_effect = lambda *args: events.append('end')
        self.conn.execute.side_effect = lambda *args: events.append('sql') or Mock(
            fetchone=Mock(return_value=(42,)))
        self.queue.side_effect = lambda *args, **kwargs: events.append('queue')
        self.assertEqual(self.store(reconcile=True, attachments=[{'filename': 'a.pdf'}]), 42)
        self.assertEqual(events[0], 'begin')
        self.assertEqual(events[-2:], ['queue', 'end'])
        self.queue.assert_called_once_with(self.conn, 42, revision='')

    def test_duplicate_is_queued(self):
        self.conn.execute.return_value.fetchone.side_effect = [None, (42,)]
        self.assertEqual(self.store(reconcile=True), 42)
        self.queue.assert_called_once_with(self.conn, 42)

    def test_whatsapp_enrichment_and_duplicate_are_queued(self):
        for old, incoming in (({'chat_name': 'raw@lid'}, {'chat_name': 'Anna'}),
                              ({'chat_name': 'Anna'}, {'chat_name': 'Anna'})):
            with self.subTest(old=old):
                self.queue.reset_mock()
                self.conn.execute.return_value.fetchone.return_value = (42, old, 's', 't')
                self.assertEqual(self.store('whatsapp', metadata=incoming, reconcile=True), 42)
                self.queue.assert_called_once_with(self.conn, 42)

    def test_accepted_edits_have_stable_revision_keys(self):
        for revision in ('2026-10-14T11:00:00+02:00', '2026-10-14T09:00:00+00:00'):
            with self.subTest(revision=revision):
                self.queue.reset_mock()
                self.assertEqual(self.store('telegram (account)', update_existing=True,
                    metadata={'revision_date': revision}, reconcile=True), 42)
                self.queue.assert_called_once_with(self.conn, 42,
                    revision='2026-10-14T09:00:00+00:00')

    def test_rejected_stale_edit_never_queues(self):
        self.conn.execute.return_value.fetchone.side_effect = [None, (42,)]
        self.assertEqual(self.store('telegram (account)', update_existing=True,
            metadata={'revision_date': '2026-10-14T09:00:00+00:00'}, reconcile=True), 42)
        self.queue.assert_not_called()
        sql = '\n'.join(c.args[0] for c in self.conn.execute.call_args_list)
        self.assertIn('::timestamptz >=', sql)
        self.assertNotIn('DELETE FROM attachments', sql)

    def test_queue_failure_aborts_storage_transaction(self):
        self.queue.side_effect = RuntimeError('queue unavailable')
        with self.assertRaisesRegex(RuntimeError, 'queue unavailable'):
            self.store(reconcile=True)
        self.assertIs(self.conn.transaction.return_value.__exit__.call_args.args[0], RuntimeError)

    def test_db_api_forwards_default_and_opt_in(self):
        with patch.object(db, '_conn') as connect, patch.object(message_store, 'store') as store:
            for enabled in (False, True):
                db.store_message('telegram', 's', '', '', **({'reconcile': True} if enabled else {}))
                self.assertEqual(store.call_args.kwargs, {'reconcile': enabled})
                self.assertIs(store.call_args.args[0], connect.return_value.__enter__.return_value)

    def test_migration_follows_jobs(self):
        order = []
        with patch.object(db, 'available', return_value=True), patch.object(db, '_conn') as connect, \
             patch.object(message_store, 'migrate', side_effect=lambda *a: order.append('native') or 0), \
             patch.object(jobs, 'migrate', side_effect=lambda *a: order.append('jobs')), \
             patch.object(task_reconciliation, 'migrate', side_effect=lambda *a: order.append('reconcile')), \
             patch.object(media_archive, 'migrate'):
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
                'off', 'off', False)
            self.assertEqual(db.init(), 0)
        self.assertEqual(order, ['native', 'jobs', 'reconcile'])


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(main, 'running', True))
        self.enterContext(patch.object(main.flows, 'enabled', return_value=True))
        self.enterContext(patch.object(main.flows, 'mark'))

    def test_authorized_bot_archive_queues_atomically_even_when_already_seen(self):
        m = {'update_id': 8, 'from': 'owner', 'text': '/task x',
             'source_id': json.dumps(['bot', '123', '7', 42]),
             'chat_id': '7', 'chat_type': 'private',
             'metadata': {'account': 'bot:123', 'update_id': 8, 'message_id': 42,
                          'chat_id': '7', 'chat_type': 'private', 'chat_name': 'owner',
                          'author': 7, 'direction': 'in', 'content_origin': 'direct_text'}}
        events = Mock()
        with patch.object(main.config, 'TELEGRAM_CHAT_ID', '7'), \
             patch.object(main.config, 'TELEGRAM_BOT_TOKEN', '123:offline'), \
             patch.object(main.telegram, 'fetch_new_messages', return_value=([m], 9)), \
             patch.object(db, '_conn') as connect, \
             patch.object(message_store, 'store', return_value=42) as store, \
             patch('policy.can_operate', return_value=True) as allowed, \
             patch.object(db, 'communication_seen', return_value=True) as seen, \
             patch.object(jobs, 'enqueue') as enqueue, \
             patch.object(jobs, 'set_cursor') as cursor, patch.object(jobs, 'submit') as submit:
            conn = connect.return_value.__enter__.return_value
            transaction = conn.transaction.return_value
            for name, mocked in (('begin', transaction.__enter__), ('store', store),
                                 ('policy', allowed), ('enqueue', enqueue),
                                 ('checkpoint', conn.execute), ('commit', transaction.__exit__)):
                events.attach_mock(mocked, name)
            state = {}
            main.check_telegram(state)
        self.assertEqual([c[0] for c in events.mock_calls],
                         ['begin', 'store', 'policy', 'enqueue', 'checkpoint', 'commit'])
        connect.assert_called_once_with()
        conn.transaction.assert_called_once_with()
        transaction.__exit__.assert_called_once_with(None, None, None)
        store.assert_called_once_with(conn, 'telegram', 'owner', 'Telegram · owner', '/task x',
            ts=None, source_id=m['source_id'],
            metadata={**m['metadata'], 'is_forwarded': False}, attachments=[])
        allowed.assert_called_once_with(conn, 42, 'owner_command', owner_required=True)
        enqueue.assert_called_once_with(conn, 'owner', 'owner:42',
                                       {'text': '/task x', 'message_id': 42, 'media': False})
        conn.execute.assert_called_once()
        self.assertIn('INSERT INTO source_cursors', conn.execute.call_args.args[0])
        self.assertEqual(conn.execute.call_args.args[1], ('telegram_bot_offset', '9'))
        self.assertEqual(state['telegram_offset'], 9)
        seen.assert_not_called()
        cursor.assert_not_called()
        submit.assert_not_called()

    def test_whatsapp_incoming_outgoing_and_seen_opt_in_before_ack(self):
        for outgoing, seen in ((False, False), (True, False), (False, True)):
            with self.subTest(outgoing=outgoing, seen=seen):
                bridge = Mock()
                bridge.fetch_new_messages.return_value = [
                    {'id': 'native', 'from': 'Anna', 'text': 'paid', 'from_me': outgoing}]
                order = []
                bridge.ack.side_effect = lambda *a: order.append('ack')
                with patch.object(db, 'store_message', side_effect=lambda *a, **k:
                        order.append('store') or 42) as store, \
                     patch.object(db, 'communication_seen', return_value=seen), \
                     patch.object(db, 'log_communication'), patch.object(main, 'process_message'):
                    main.check_whatsapp({}, bridge)
                self.assertTrue(store.call_args.kwargs['reconcile'])
                self.assertEqual(order, ['store', 'ack'])

    def test_worker_skips_superseded_revision(self):
        with patch.object(db, '_conn') as connect, \
             patch.object(task_reconciliation, 'reconcile') as handler:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
            main._reconcile_job({'message_id': 42, 'revision': '2026-10-14T09:00:00+00:00'})
        handler.assert_not_called()

    def test_worker_defers_until_earlier_analysis_finishes(self):
        with patch.object(db, '_conn') as connect, \
             patch.object(task_reconciliation, 'reconcile') as handler:
            result = connect.return_value.__enter__.return_value.execute.return_value.fetchone
            result.return_value = (1,)
            with self.assertRaises(jobs.Deferred):
                main._reconcile_job({'message_id': 42})
            handler.assert_not_called()
            result.return_value = None
            main._reconcile_job({'message_id': 42})
        handler.assert_called_once_with({'message_id': 42})

    def test_worker_registration(self):
        self.enterContext(patch.object(main, 'running', False))
        for target in ('signal.signal', 'db.init', 'actions.save_state', 'flows.init',
                       'platform_health.register_workers'):
            self.enterContext(patch(target))
        self.enterContext(patch.object(main.config, 'OPENROUTER_API_KEY', 'offline'))
        self.enterContext(patch.object(main.actions, 'load_state', return_value={}))
        self.enterContext(patch.object(jobs, 'cursor', return_value=0))
        self.enterContext(patch.object(main.dashboard, 'start'))
        self.enterContext(patch.object(main, 'WhatsAppBridge'))
        self.enterContext(patch.object(main, 'TelegramAccountWorker'))
        self.enterContext(patch.object(main.gcal, 'readiness', return_value='disabled'))
        with patch.object(jobs, 'Worker') as worker, patch('builtins.print'):
            main.main()
        self.assertIn(unittest.mock.call('reconcile', main._reconcile_job),
                      worker.call_args_list)


class PersonalTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_and_recovery_opt_in_history_opts_out(self):
        now = datetime.now(timezone.utc)
        message = SimpleNamespace(id=42, chat_id=7, sender_id=8, date=now, edit_date=None,
            action=None, out=False, is_private=True, is_group=False, media=None,
            raw_text='paid', sender=SimpleNamespace(first_name='Anna'),
            chat=SimpleNamespace(first_name='Anna'))
        worker = tg.TelegramAccountWorker()
        worker.account = 'user:9'
        with patch.object(tg.db, 'store_message') as store, \
             patch.object(worker, 'media_attachments', AsyncMock(return_value=[])):
            for recovered in (False, True):
                await worker.archive(message, recovered=recovered,
                                     window=(now - timedelta(days=1), now + timedelta(days=1)))
                self.assertTrue(store.call_args.kwargs['reconcile'])
            await worker.archive(message, recovered=True, reconcile=False,
                                 window=(now - timedelta(days=1), now + timedelta(days=1)))
            self.assertFalse(store.call_args.kwargs['reconcile'])

    async def test_explicit_import_passes_false_to_archive(self):
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
            self.assertFalse(archive.call_args.kwargs['reconcile'])
            self.assertTrue(archive.call_args.kwargs['recovered'])


if __name__ == '__main__':
    unittest.main()