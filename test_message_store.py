"""Offline tests: mocked database, AI, network and notification APIs."""
import unittest
from unittest.mock import Mock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import db
import main
import message_store
import jobs
from connectors import email, telegram


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

    def test_whatsapp_enrichment_media_is_guarded_and_body_not_updated(self):
        conn = Mock()
        old = {'chat_id': 'g@g.us', 'chat_name': 'Team', 'chat_name_rank': 3,
               'author_name': 'Anna', 'author_name_rank': 3}
        message_store.enrich_whatsapp(conn, (42, old, 'Anna in Team', 'WhatsApp · Team'),
            {'author_name': '123@lid', 'chat_name': 'g@g.us'},
            [{'filename': 'a.pdf', 'path': '/offline/a.pdf'}])
        statements = [c.args[0] for c in conn.execute.call_args_list]
        self.assertEqual(len(statements), 1)
        self.assertIn('WHERE NOT EXISTS', statements[0])
        self.assertNotIn('UPDATE whatsapp_messages', statements[0])

    def setUp(self):
        for name in ('jobs.submit', 'jobs.set_cursor'):
            p = patch(name)
            p.start()
            self.addCleanup(p.stop)
        switch = patch.object(main.flows, 'enabled', return_value=True)
        switch.start()
        self.addCleanup(switch.stop)

    def test_preserved_ledger_repairs_native_without_ai(self):
        calls = []
        with patch.object(db, 'store_message', side_effect=lambda *a, **k: calls.append('store') or 42), \
             patch.object(db, 'communication_seen', side_effect=lambda *a: calls.append('seen') or True), \
             patch.object(main.brain, 'analyze') as ai:
            main._legacy_process_message('whatsapp', 'sender', '', '', {}, source_id='native')
        self.assertEqual(calls, ['store', 'seen'])
        ai.assert_not_called()

    def test_owner_command_persisted_before_routing(self):
        m = {
            'from': 'owner', 'text': '/start', 'source_id': 'native',
            'chat_id': '7', 'chat_type': 'private',
            'msg_date': '2026-10-14T11:38:00+02:00',
            'metadata': {'chat_id': '7', 'chat_type': 'private'},
        }
        calls = []
        with (
            patch.object(config, 'TELEGRAM_CHAT_ID', '7'),
            patch.object(telegram, 'fetch_new_messages', return_value=([m], 9)),
            patch.object(db, 'store_message', side_effect=lambda *a, **k: calls.append('store')),
            patch.object(db, 'communication_seen', return_value=False),
        ):
            main.check_telegram({})
        self.assertEqual(calls, ['store'])
        jobs.submit.assert_called_once()

    def test_thirdparty_never_routes_owner_command(self):
        m = {'from': 'other', 'text': '/note', 'source_id': 'native', 'unauthorized': True}
        with patch.object(telegram, 'fetch_new_messages', return_value=([m], 9)), \
             patch.object(db, 'store_message') as store, patch.object(db, 'communication_seen', return_value=False), \
             patch.object(main, 'process_message') as process, \
             patch.object(main, '_handle_user_command') as command, \
             patch.object(main, 'handle_user_chat') as chat:
            main.check_telegram({})
        process.assert_not_called()
        store.assert_not_called()
        jobs.submit.assert_not_called()
        command.assert_not_called()
        chat.assert_not_called()

    def test_bot_boundary_rejects_supplied_mismatched_or_missing_identity(self):
        valid = {'from': 'owner', 'text': '/start', 'source_id': 'native',
                 'chat_id': '7', 'chat_type': 'private',
                 'metadata': {'chat_id': '7', 'chat_type': 'private'}}
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
                    db, 'store_message') as store, patch.object(
                    db, 'communication_seen') as seen, patch.object(
                    main, 'process_message') as process, patch.object(
                    telegram, 'notify') as notify, patch.object(main, 'running', True):
                state = {}
                main.check_telegram(state)
                self.assertEqual(state['telegram_offset'], 9)
                jobs.set_cursor.assert_called_with('telegram_bot_offset', 9)
                for call in (store, seen, process, notify, jobs.submit):
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

    def test_source_before_analysis_failure(self):
        calls = []
        with patch.object(db, 'store_message', side_effect=lambda *a, **k: calls.append('stored') or 42), \
             patch.object(main.brain, 'analyze', side_effect=lambda *a: calls.append('ai') or (_ for _ in ()).throw(RuntimeError())), \
             patch.object(main.telegram, 'notify') as send:
            with self.assertRaises(RuntimeError):
                main._legacy_process_message('telegram', 'third party', '', 'hello', {},
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
               patch.object(telegram, 'acquire_poll_lock'), \
             patch.object(telegram, '_call', return_value={'result': [update]}):
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