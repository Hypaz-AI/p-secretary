"""Offline tests: mocked database, AI, network and notification APIs."""
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import db
import main
import message_store
from connectors import email, telegram


class NativeTests(unittest.TestCase):
    def setUp(self):
        switch = patch.object(main.flows, 'enabled', return_value=True)
        switch.start()
        self.addCleanup(switch.stop)

    def test_preserved_ledger_repairs_native_without_ai(self):
        calls = []
        with patch.object(db, 'store_message', side_effect=lambda *a, **k: calls.append('store') or 42), \
             patch.object(db, 'communication_seen', side_effect=lambda *a: calls.append('seen') or True), \
             patch.object(main.brain, 'analyze') as ai:
            main.process_message('whatsapp', 'sender', '', '', {}, source_id='native')
        self.assertEqual(calls, ['store', 'seen'])
        ai.assert_not_called()

    def test_owner_command_persisted_before_routing(self):
        m = {'from': 'owner', 'text': '/start', 'source_id': 'native',
             'msg_date': '2026-10-14T11:38:00+02:00', 'metadata': {'chat_id': '7'}}
        calls = []
        with patch.object(telegram, 'fetch_new_messages', return_value=([m], 9)), \
             patch.object(db, 'store_message', side_effect=lambda *a, **k: calls.append('store')), \
             patch.object(db, 'communication_seen', return_value=False), \
             patch.object(main, '_handle_user_command', side_effect=lambda *a: calls.append('command') or True), \
             patch.object(db, 'log_communication'):
            main.check_telegram({})
        self.assertEqual(calls, ['store', 'command'])

    def test_thirdparty_never_routes_owner_command(self):
        m = {'from': 'other', 'text': '/note', 'source_id': 'native', 'unauthorized': True}
        with patch.object(telegram, 'fetch_new_messages', return_value=([m], 9)), \
             patch.object(db, 'store_message'), patch.object(db, 'communication_seen', return_value=False), \
             patch.object(main, 'process_message') as process, \
             patch.object(main, '_handle_user_command') as command, \
             patch.object(main, 'handle_user_chat') as chat:
            main.check_telegram({})
        process.assert_called_once()
        command.assert_not_called()
        chat.assert_not_called()

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

    def test_source_before_analysis_failure(self):
        calls = []
        with patch.object(db, 'store_message', side_effect=lambda *a, **k: calls.append('stored') or 42), \
             patch.object(main.brain, 'analyze', side_effect=lambda *a: calls.append('ai') or (_ for _ in ()).throw(RuntimeError())), \
             patch.object(main.telegram, 'notify') as send:
            with self.assertRaises(RuntimeError):
                main.process_message('telegram', 'third party', '', 'hello', {},
                                     msg_date='2026-10-14T10:00:00+00:00')
            self.assertEqual(calls, ['stored', 'ai'])
            send.assert_not_called()

    def test_telegram_missing_owner_is_unauthorized(self):
        now = int(datetime.now(ZoneInfo(config.TIMEZONE)).timestamp())
        update = {'update_id': 5, 'message': {'message_id': 8, 'date': now,
                  'chat': {'id': 7, 'type': 'private', 'first_name': 'Other'},
                  'from': {'id': 7, 'first_name': 'Other', 'last_name': 'User',
                           'username': 'otheruser'}, 'text': 'quando x allora y'}}
        with patch.object(config, 'TELEGRAM_BOT_TOKEN', 'mock'), \
             patch.object(config, 'TELEGRAM_CHAT_ID', ''), \
             patch.object(telegram, '_call', return_value={'result': [update]}):
            msgs, offset = telegram.fetch_new_messages(0)
        self.assertTrue(msgs[0]['unauthorized'])
        self.assertEqual(msgs[0]['metadata']['message_id'], 8)
        self.assertEqual(msgs[0]['metadata']['chat_name'], 'Other')
        self.assertEqual(msgs[0]['from'], 'Other User (@otheruser)')
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