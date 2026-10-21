"""Offline tests: mocked database, AI, network and notification APIs."""
import json
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

from secretary.core import config, db
from secretary import main
from secretary.archive import message_store
from secretary.core import jobs
from secretary.connectors import email, telegram


class NativeTests(unittest.TestCase):
    def test_whatsapp_names_monotonic_and_aliases_additive(self):
        raw = {'chat_id': 'person@lid', 'chat_name': 'person@lid'}
        enriched = message_store.merge_whatsapp_metadata(raw, {
            'chat_name': 'Anna Rubrica', 'chat_name_rank': 3,
            'chat_aliases': ['person@lid', '123@c.us'], 'author_name': 'Anna'})
        for candidate in ({'chat_name': 'person@lid'},
                          {'chat_name': 'Push', 'chat_name_rank': 2},
                          {'chat_name': None, 'author_name': None}):
            self.assertEqual(message_store.merge_whatsapp_metadata(enriched, candidate)['chat_name'],
                             'Anna Rubrica')
        self.assertEqual(enriched['chat_aliases'], ['person@lid', '123@c.us'])

    def test_whatsapp_existing_row_updates_without_insert_or_analysis(self):
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = []
        conn.transaction.return_value.__enter__ = Mock()
        conn.transaction.return_value.__exit__ = Mock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (
            42, {'chat_id': 'person@lid', 'chat_name': 'person@lid', 'direction': 'out'},
            'io → person@lid', 'WhatsApp · person@lid')
        result = message_store.store(conn, 'whatsapp', 'io → Anna', 'WhatsApp · Anna',
            'unchanged', source_id='native', metadata={
                'chat_name': 'Anna', 'chat_name_rank': 3})
        self.assertEqual(result, 42)
        calls = conn.execute.call_args_list
        update = next(c for c in calls if c.args[0].startswith('UPDATE whatsapp_messages'))
        self.assertEqual(update.args[1][:2], ('io → Anna', 'WhatsApp · Anna'))
        self.assertFalse(any('INSERT' in c.args[0] for c in calls))
        self.assertFalse(any('message_analysis' in c.args[0] for c in calls))
        self.derived.assert_called_once_with(conn, 42, historical=False)

    def test_whatsapp_enrichment_media_is_guarded_and_body_not_updated(self):
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = []
        old = {'chat_id': 'g@g.us', 'chat_name': 'Team', 'chat_name_rank': 3,
               'author_name': 'Anna', 'author_name_rank': 3}
        message_store.enrich_whatsapp(conn, (42, old, 'Anna in Team', 'WhatsApp · Team'),
            {'author_name': '123@lid', 'chat_name': 'g@g.us'},
            [{'filename': 'a.pdf', 'path': '/offline/a.pdf'}])
        statements = [c.args[0] for c in conn.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn('INSERT INTO attachments', statements[0])
        self.assertIn('WHERE NOT EXISTS', statements[0])
        self.assertIn('SELECT a.id,a.content_type FROM attachments', statements[1])
        self.assertEqual(conn.execute.call_args.args[1], (42,))
        self.assertFalse(any('UPDATE whatsapp_messages' in sql for sql in statements))
        self.derived.assert_called_once_with(conn, 42, historical=False)

    def setUp(self):
        # Minimal native-storage fixtures stop at the derived integration boundary.
        self.derived = self.enterContext(patch.object(message_store, 'enqueue_derived'))
        for name in ('secretary.core.jobs.submit', 'secretary.core.jobs.set_cursor'):
            p = patch(name)
            p.start()
            self.addCleanup(p.stop)
        for name in ('secretary.core.db._conn', 'urllib.request.urlopen'):
            p = patch(name, side_effect=AssertionError('External I/O forbidden in offline tests'))
            p.start()
            self.addCleanup(p.stop)
        for name, value in (('TELEGRAM_BOT_TOKEN', '123:offline'), ('TELEGRAM_CHAT_ID', '7')):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        running = patch.object(main, 'running', True)
        running.start()
        self.addCleanup(running.stop)
        switch = patch.object(main.flows, 'enabled', return_value=True)
        switch.start()
        self.addCleanup(switch.stop)

    def _telegram_message(self):
        return {
            'update_id': 8, 'from': 'owner', 'text': '/start',
            'source_id': json.dumps(['bot', '123', '7', 42]),
            'chat_id': '7', 'chat_type': 'private',
            'msg_date': '2026-10-14T11:38:00+02:00',
            'metadata': {
                'account': 'bot:123', 'update_id': 8, 'message_id': 42,
                'chat_id': '7', 'chat_type': 'private', 'chat_name': 'owner',
                'author': 7, 'direction': 'in', 'content_origin': 'direct_text',
            },
        }

    def test_owner_command_persisted_before_routing(self):
        m = self._telegram_message()
        events = Mock()
        with (
            patch.object(telegram, 'fetch_new_messages', return_value=([m], 9)),
            patch.object(db, '_conn') as connect,
            patch.object(db.message_store, 'store', return_value=42) as store,
            # Only isolate routing here; native authority is covered by policy tests.
            patch('secretary.core.policy.can_operate', return_value=True) as can_operate,
            patch.object(jobs, 'enqueue') as enqueue,
            patch.object(db, 'communication_seen') as seen,
        ):
            conn = connect.return_value.__enter__.return_value
            transaction = conn.transaction.return_value
            for name, mocked in (('begin', transaction.__enter__), ('store', store),
                                 ('policy', can_operate), ('enqueue', enqueue),
                                 ('checkpoint', conn.execute), ('commit', transaction.__exit__)):
                events.attach_mock(mocked, name)
            state = {}
            main.check_telegram(state)
        self.assertEqual([c[0] for c in events.mock_calls],
                         ['begin', 'store', 'policy', 'enqueue', 'checkpoint', 'commit'])
        connect.assert_called_once_with()
        conn.transaction.assert_called_once_with()
        transaction.__exit__.assert_called_once_with(None, None, None)
        store.assert_called_once_with(
            conn, 'telegram', 'owner', 'Telegram · owner', '/start',
            ts=m['msg_date'], source_id=m['source_id'],
            metadata={**m['metadata'], 'is_forwarded': False}, attachments=[])
        can_operate.assert_called_once_with(conn, 42, 'owner_command', owner_required=True)
        enqueue.assert_called_once_with(conn, 'owner', 'owner:42',
                                       {'text': '/start', 'message_id': 42, 'media': False})
        conn.execute.assert_called_once()
        self.assertIn('INSERT INTO source_cursors', conn.execute.call_args.args[0])
        self.assertEqual(conn.execute.call_args.args[1], ('telegram_bot_offset', '9'))
        self.assertEqual(state['telegram_offset'], 9)
        seen.assert_not_called()
        jobs.submit.assert_not_called()
        jobs.set_cursor.assert_not_called()

    def test_thirdparty_never_routes_owner_command(self):
        m = self._telegram_message()
        m.update({'from': 'other', 'text': '/note', 'unauthorized': True, 'chat_id': '8',
                  'source_id': json.dumps(['bot', '123', '8', 42]),
                  'metadata': {**m['metadata'], 'chat_id': '8', 'author': 8}})
        with (
            patch.object(telegram, 'fetch_new_messages', return_value=([m], 9)),
            patch.object(db, '_conn') as connect,
            patch.object(db.message_store, 'store') as store,
            patch.object(db, 'communication_seen') as seen,
            patch.object(jobs, 'enqueue') as enqueue,
            patch.object(main, 'process_message') as process,
            patch.object(main, '_owner_job') as command,
            patch.object(main, '_analysis_job') as chat,
        ):
            state = {}
            main.check_telegram(state)
        conn = connect.return_value.__enter__.return_value
        conn.transaction.assert_called_once_with()
        conn.transaction.return_value.__exit__.assert_called_once_with(None, None, None)
        conn.execute.assert_called_once()
        self.assertIn('INSERT INTO source_cursors', conn.execute.call_args.args[0])
        self.assertEqual(conn.execute.call_args.args[1], ('telegram_bot_offset', '9'))
        self.assertEqual(state['telegram_offset'], 9)
        process.assert_not_called()
        store.assert_not_called()
        seen.assert_not_called()
        enqueue.assert_not_called()
        jobs.submit.assert_not_called()
        jobs.set_cursor.assert_not_called()
        command.assert_not_called()
        chat.assert_not_called()

    def test_bot_boundary_rejects_supplied_mismatched_or_missing_identity(self):
        valid = self._telegram_message()
        cases = [('7', {'unauthorized': True}), ('', {}), (None, {}),
                 ('7', {'chat_id': '8'}), ('7', {'chat_id': None}),
                 ('7', {'chat_type': 'group'}), ('7', {'chat_type': None}),
                 ('7', {'metadata': {'chat_id': '8', 'chat_type': 'private'}}),
                 ('7', {'metadata': {'chat_id': '7', 'chat_type': 'group'}}),
                 ('7', {'metadata': {}})]
        for owner, overrides in cases:
            with self.subTest(owner=owner, overrides=overrides), patch.object(
                    config, 'TELEGRAM_CHAT_ID', owner), patch.object(
                    telegram, 'fetch_new_messages', return_value=([{**valid, **overrides}], 9)), patch.object(
                    db, '_conn') as connect, patch.object(
                    db.message_store, 'store') as store, patch.object(
                    db, 'communication_seen') as seen, patch.object(
                    jobs, 'enqueue') as enqueue, patch.object(
                    main, 'process_message') as process, patch.object(
                    main, '_owner_job') as command, patch.object(
                    main, '_analysis_job') as chat, patch.object(
                    telegram, 'notify') as notify:
                state = {}
                main.check_telegram(state)
                self.assertEqual(state['telegram_offset'], 9)
                conn = connect.return_value.__enter__.return_value
                conn.transaction.assert_called_once_with()
                conn.transaction.return_value.__exit__.assert_called_once_with(None, None, None)
                conn.execute.assert_called_once()
                self.assertIn('INSERT INTO source_cursors', conn.execute.call_args.args[0])
                self.assertEqual(conn.execute.call_args.args[1], ('telegram_bot_offset', '9'))
                for call in (store, seen, process, notify, jobs.submit, jobs.set_cursor,
                             enqueue, command, chat):
                    call.assert_not_called()

    def test_timestamp_timezone_preserved(self):
        value = email.parse_email_datetime('Wed, 14 Oct 2026 10:00:00 +0200')
        self.assertEqual(datetime.fromisoformat(value).timestamp(),
                         datetime.fromisoformat('2026-10-14T08:00:00+00:00').timestamp())
        self.assertIsNone(email.parse_email_datetime('invalid'))

    def test_historical_requires_explicit_opt_in(self):
        with self.assertRaises(ValueError):
            email.fetch_day_messages({}, '2000-01-01')

    def test_identity_contains_native_scope(self):
        self.assertNotEqual(email.json_identity('a', 'INBOX', '1', '2'),
                            email.json_identity('a', 'INBOX', '3', '2'))

    def test_telegram_missing_owner_is_unauthorized(self):
        now = int(datetime.now(ZoneInfo(config.TIMEZONE)).timestamp())
        update = {'update_id': 5, 'message': {'message_id': 8, 'date': now,
                  'chat': {'id': 7, 'type': 'private', 'first_name': 'Other'},
                  'from': {'id': 7, 'first_name': 'Other', 'last_name': 'User',
                           'username': 'otheruser'}, 'text': 'quando x allora y'}}
        with (
            patch.object(config, 'TELEGRAM_BOT_TOKEN', 'mock'),
            patch.object(config, 'TELEGRAM_CHAT_ID', ''),
            patch.object(telegram, 'acquire_poll_lock'),
            patch.object(telegram, '_call', return_value={'result': [update]}),
        ):
            msgs, offset = telegram.fetch_new_messages(0)
        self.assertEqual(msgs, [])
        self.assertEqual(offset, 6)

    def test_telegram_full_name_and_chat_name(self):
        self.assertEqual(telegram._full_name(
            {'first_name': 'Luca', 'last_name': 'Bianchi', 'username': 'lucab'}),
            'Luca Bianchi (@lucab)')
        self.assertEqual(telegram._full_name({'first_name': 'Luca'}), 'Luca')
        self.assertEqual(telegram._full_name({'username': 'ghost'}), '@ghost')
        self.assertEqual(telegram._full_name({}), '?')
        self.assertEqual(telegram._chat_name(
            {'title': 'Gruppo Lavoro', 'id': -100}), 'Gruppo Lavoro')
        self.assertEqual(telegram._chat_name({'id': 42}), '42')
        self.assertEqual(telegram._chat_name({}), '')

    def test_versioned_migration_no_second_drop(self):
        conn = Mock()
        conn.transaction.return_value.__enter__ = Mock()
        conn.transaction.return_value.__exit__ = Mock(return_value=False)
        conn.execute.return_value.fetchone.return_value = (1,)
        self.assertEqual(message_store.migrate(conn, '', True), 0)
        self.assertTrue(all(call.args[0].startswith('SELECT')
                            for call in conn.execute.call_args_list))
        conn.transaction.assert_not_called()

    def test_concurrent_migration_rechecked_under_lock(self):
        conn = Mock()
        conn.transaction.return_value.__enter__ = Mock()
        conn.transaction.return_value.__exit__ = Mock(return_value=False)
        conn.execute.return_value.fetchone.side_effect = [(None,), (1,)]
        self.assertEqual(message_store.migrate(conn, '', False), 0)
        self.assertTrue(any('pg_advisory_xact_lock' in call.args[0]
                            for call in conn.execute.call_args_list))
        self.assertFalse(any('DROP' in call.args[0] for call in conn.execute.call_args_list))

    def test_init_refuses_read_only_without_migrating(self):
        for state in [('on', 'on', False), ('off', 'on', False), ('on', 'off', True)]:
            with self.subTest(state=state), patch.object(db, 'available', return_value=True), \
                 patch.object(db, '_conn') as connect, patch.object(message_store, 'migrate') as migrate:
                connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = state
                with self.assertRaisesRegex(RuntimeError, 'sola lettura'):
                    db.init()
                migrate.assert_not_called()

    def test_init_writable_runs_migration(self):
        with patch.object(db, 'available', return_value=True), patch.object(db, '_conn') as connect, \
             patch.object(message_store, 'migrate', return_value=0) as migrate:
            conn = connect.return_value.__enter__.return_value
            conn.execute.return_value.fetchone.return_value = ('off', 'off', False)
            self.assertEqual(db.init(), 0)
            migrate.assert_called_once_with(conn, db.SCHEMA, False)

    def test_no_implicit_reset(self):
        conn = Mock()
        conn.transaction.return_value.__enter__ = Mock()
        conn.transaction.return_value.__exit__ = Mock(return_value=False)
        conn.execute.return_value.fetchone.side_effect = [(None,), None, ('r',)]
        with self.assertRaises(RuntimeError):
            message_store.migrate(conn, '', False)
        self.assertFalse(any('DROP' in call.args[0] for call in conn.execute.call_args_list))


if __name__ == '__main__':
    unittest.main()