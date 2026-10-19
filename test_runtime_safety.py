"""Mocked runtime/concurrent-ingress safety. No production configuration or IO."""
from contextlib import nullcontext
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import config
import jobs
from connectors import email as mail, telegram, whatsapp


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('db._conn', side_effect=AssertionError('Database forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(config, 'DATA_DIR', Path(self.directory)))


class LeaseTests(IsolatedTests):
    job = (42, {'message_id': 7}, 1, 'token')

    def test_context_scoped_threadlocal_and_cleaned_on_failure(self):
        worker = jobs.Worker('analysis', Mock())
        with self.assertRaises(ValueError):
            with worker._execution(self.job) as context:
                self.assertIs(jobs.execution_context(), context)
                self.assertEqual(context.message_id, 7)
                self.assertEqual(context.token, 'token')
                other = []
                thread = threading.Thread(target=lambda: other.append(jobs.execution_context()))
                thread.start()
                thread.join()
                self.assertEqual(other, [None])
                raise ValueError('failure')
        self.assertIsNone(jobs.execution_context())
        self.assertTrue(jobs.assert_current())  # Legacy synchronous API.

    def test_assert_checks_token_status_expiry_and_supersession(self):
        conn = MagicMock()
        worker = jobs.Worker('analysis', Mock())
        with worker._execution(self.job):
            conn.execute.return_value.fetchone.return_value = (42,)
            self.assertTrue(jobs.assert_current(conn))
            sql, params = conn.execute.call_args.args
            self.assertEqual(params, (42, 'token'))
            self.assertIn("status='running'", sql)
            self.assertIn('lease_until>', sql)
            conn.execute.return_value.fetchone.return_value = None
            with self.assertRaises(jobs.LeaseLost):
                jobs.assert_current(conn)
            conn.execute.return_value.fetchone.return_value = (42,)
            with self.assertRaises(jobs.LeaseLost):
                jobs.assert_current(conn)  # A lost attempt cannot revive.

    def test_execution_deadline_fails_before_db_or_effect(self):
        with jobs.Worker('analysis', Mock())._execution(self.job) as context:
            context.deadline = time.monotonic() - 1
            with self.assertRaises(jobs.LeaseLost):
                jobs.assert_current()

    def test_stop_immediately_revokes_context_without_waiting_for_heartbeat(self):
        worker = jobs.Worker('analysis', Mock())
        with worker._execution(self.job):
            worker.stop()
            with self.assertRaises(jobs.LeaseLost):
                jobs.assert_current()

    def test_arbitrary_error_text_or_custom_exception_name_is_not_persisted(self):
        self.assertEqual(jobs.safe_error('private_secret'), 'JobError')
        self.assertEqual(jobs.safe_error('https://private/token'), 'JobError')
        self.assertEqual(jobs.safe_error(ValueError('private body')), 'ValueError')

    def test_describe_error_keeps_explicit_message_but_redacts_secrets(self):
        self.assertEqual(jobs.describe_error(ValueError('Connection refused by provider')),
                         'Connection refused by provider')
        self.assertEqual(jobs.describe_error('Notification not acknowledged'),
                         'Notification not acknowledged')
        self.assertEqual(jobs.describe_error(ValueError('token=private-example')), '[redacted]')
        self.assertEqual(jobs.describe_error(ValueError('Authorization: Bearer private-token')),
                         '[redacted]')
        self.assertEqual(jobs.describe_error(ValueError('postgresql://secret@host/private body')),
                         '[redacted] body')
        self.assertNotIn('\n', jobs.describe_error(ValueError('line1\nline2')))
        self.assertLessEqual(len(jobs.describe_error(ValueError('x' * 2000))), 500)

    def test_heartbeat_renews_in_background_and_stops_on_lease_loss(self):
        renewed = threading.Event()
        worker = jobs.Worker('analysis', Mock())
        def renew(job):
            renewed.set()
            return False
        with patch.object(jobs, 'HEARTBEAT_SECONDS', 0.001), patch.object(jobs, 'renew', side_effect=renew):
            with worker._execution(self.job) as context:
                self.assertTrue(renewed.wait(2))
                self.assertTrue(context.lost.wait(2))
                with self.assertRaises(jobs.LeaseLost):
                    jobs.assert_current()

    def test_heartbeat_database_failure_is_fail_closed(self):
        with patch.object(jobs, 'HEARTBEAT_SECONDS', 0.001), patch.object(
                jobs, 'renew', side_effect=OSError('private db URL')):
            with jobs.Worker('analysis', Mock())._execution(self.job) as context:
                self.assertTrue(context.lost.wait(2))

    def test_renew_and_terminal_transitions_are_token_and_expiry_fenced(self):
        conn = MagicMock()
        with patch.object(jobs.db, '_conn', side_effect=lambda: nullcontext(conn)):
            self.assertTrue(jobs.renew(self.job))
            jobs.finish(self.job, RuntimeError('postgresql://secret@host/private body'))
            jobs.defer(self.job)
        for call in conn.execute.call_args_list:
            self.assertIn('token=%s', call.args[0])
            self.assertIn('lease_until>now()', call.args[0])
        error_call = conn.execute.call_args_list[1]
        self.assertEqual(error_call.args[1][1], 'RuntimeError')
        self.assertNotIn('secret', str(conn.execute.call_args_list))

    def test_worker_sanitizes_logs_and_stored_error(self):
        worker = jobs.Worker('analysis', Mock(side_effect=ValueError('token=private body')))
        with patch.object(jobs, 'claim', return_value=self.job), patch.object(
                jobs, 'assert_current'), patch.object(jobs, 'finish', side_effect=lambda *a: worker.stop()) as finish, \
                patch.object(jobs.logger, 'warning') as log:
            worker.run()
        finish.assert_called_once_with(self.job, worker.handler.side_effect)
        self.assertNotIn('private', str(log.call_args_list))
        self.assertNotIn('private', jobs.describe_error(worker.handler.side_effect))
        self.assertIsNone(jobs.execution_context())

    def test_worker_never_calls_handler_for_lost_lease(self):
        handler = Mock()
        worker = jobs.Worker('analysis', handler)
        def lost(*args):
            worker.stop()
            raise jobs.LeaseLost('expired')
        with patch.object(jobs, 'claim', return_value=self.job), patch.object(
                jobs, 'assert_current', side_effect=lost), patch.object(jobs, 'finish') as finish:
            worker.run()
        handler.assert_not_called()
        finish.assert_not_called()

    def test_priority_migration_runs_on_receipted_v2_without_rearming_jobs(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [(1,), None]
        jobs.migrate(conn)
        sql = '\n'.join(c.args[0] for c in conn.execute.call_args_list)
        self.assertNotIn('CREATE TABLE IF NOT EXISTS work_jobs', sql)
        self.assertIn('ADD COLUMN IF NOT EXISTS priority', sql)
        self.assertIn('priority DESC', sql)
        self.assertNotIn("status='pending'", sql)
        self.assertEqual(conn.execute.call_args.args[1], (jobs.RUNTIME_VERSION,))

    def test_enqueue_existing_identity_and_owner_priority_are_preserved(self):
        conn = MagicMock()
        jobs.enqueue(conn, 'action', 'owner-reply:7:0', {'owner_reply_mid': 7})
        self.assertEqual(conn.execute.call_args.args[1][-1], 90)
        self.assertIn('ON CONFLICT(key) DO NOTHING', conn.execute.call_args.args[0])
        jobs.enqueue(conn, 'owner', 'owner:7', {})
        self.assertEqual(conn.execute.call_args.args[1][-1], 100)
        jobs.enqueue(conn, 'analysis', 'analysis:7', {}, priority=5)
        self.assertEqual(conn.execute.call_args.args[1][-1], 5)
        with self.assertRaises(ValueError):
            jobs.enqueue(conn, 'analysis', 'bad', {}, priority=True)


class TelegramSafetyTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(telegram, 'acquire_poll_lock'))
        self.enterContext(patch('flows.mark'))

    def update(self, identity, **extra):
        return {'update_id': identity, 'message': {'message_id': identity,
                'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7},
                'date': int(datetime.now(timezone.utc).timestamp()), 'text': 'caption', **extra}}

    def test_document_failure_stops_offset_before_later_updates_and_logs_no_secrets(self):
        updates = [self.update(5), self.update(6, document={
            'file_id': 'doc', 'file_unique_id': 'safe', 'file_name': 'invoice.pdf'}), self.update(7)]
        with patch.object(telegram, '_call', return_value={'result': updates}), patch.object(
                telegram, '_download', side_effect=OSError('https://token/private body')), patch('builtins.print') as log:
            messages, offset = telegram.fetch_new_messages(5, self.directory)
        self.assertEqual(offset, 6)
        self.assertEqual([m['update_id'] for m in messages], [5])
        self.assertNotIn('token/private', str(log.call_args_list))

    def test_safe_document_filename_update_id_and_forward_origin_survive(self):
        origin = {'type': 'hidden_user', 'sender_user_name': 'external'}
        update = self.update(5, forward_origin=origin, document={
            'file_id': 'doc', 'file_unique_id': '../../id', 'file_name': '..\\..\\/../invoice.pdf'})
        with patch.object(telegram, '_call', return_value={'result': [update]}), patch.object(
                telegram, '_download') as download:
            messages, offset = telegram.fetch_new_messages(5, self.directory)
        self.assertEqual(offset, 6)
        self.assertEqual(messages[0]['metadata']['update_id'], 5)
        self.assertEqual(messages[0]['metadata']['forward_origin'], origin)
        path = download.call_args.args[1]
        self.assertEqual(path.parent, Path(self.directory))
        self.assertEqual(download.call_args.kwargs['max_bytes'], telegram.MAX_DOWNLOAD_BYTES)

    def test_oversized_document_is_visible_and_not_downloaded(self):
        update = self.update(5, document={'file_id': 'doc', 'file_unique_id': 'id',
            'file_name': 'invoice.pdf', 'file_size': telegram.MAX_DOWNLOAD_BYTES + 1})
        with patch.object(telegram, '_call', return_value={'result': [update]}), patch.object(
                telegram, '_download') as download:
            messages, offset = telegram.fetch_new_messages(5, self.directory)
        download.assert_not_called()
        self.assertEqual(offset, 6)
        self.assertIn('limite', messages[0]['text'])

    def test_bounded_download_is_atomic_and_failure_keeps_existing_file(self):
        dest = Path(self.directory) / 'invoice.pdf'
        dest.write_bytes(b'original')
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'oversize'
        with patch.object(telegram, '_call', return_value={'result': {'file_path': 'docs/file'}}), patch(
                'urllib.request.urlopen', return_value=response):
            with self.assertRaises(ValueError):
                telegram._download('id', dest, max_bytes=3)
            response.__enter__.return_value.read.assert_called_once_with(4)
            self.assertEqual(dest.read_bytes(), b'original')
            response.__enter__.return_value.read.return_value = b'new'
            telegram._download('id', dest, max_bytes=3)
            self.assertEqual(dest.read_bytes(), b'new')
        self.assertEqual(list(Path(self.directory).iterdir()), [dest])

    def test_telegram_getfile_path_cannot_redirect_or_traverse(self):
        for path in ('../file', '/absolute', 'docs/../../file', '..\\file'):
            with patch.object(telegram, '_call', return_value={'result': {'file_path': path}}):
                with self.assertRaises(ValueError):
                    telegram._download('id', Path(self.directory) / 'file')


class WhatsAppSafetyTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        self.bridge = whatsapp.WhatsAppBridge()

    def put(self, identity, **overrides):
        data = dict(id=identity, timestamp=datetime.now(timezone.utc).timestamp(),
                    chat_id='chat@c.us', author='sender@c.us', enriched=True,
                    text='body', version='v1')
        data.update(overrides)
        path = self.bridge.spool_dir / (identity.encode().hex() + '.json')
        path.write_text(json.dumps(data))
        return path

    def test_scan_skips_more_than_200_ineligible_without_starving_valid_message(self):
        for i in range(205):
            self.put(f'a{i:03d}', enriched=False)
        self.put('z-valid')
        self.assertEqual([m['id'] for m in self.bridge.fetch_new_messages()], ['z-valid'])

    def test_returns_at_most_200_eligible_and_ack_does_not_delete_newer_version(self):
        for i in range(201):
            self.put(f'm{i:03d}')
        self.assertEqual(len(self.bridge.fetch_new_messages()), 200)
        self.bridge.ack('m000', 'old')
        self.assertEqual(len(self.bridge.fetch_new_messages()), 200)
        self.bridge.ack('m000', 'v1')
        self.assertEqual(len(self.bridge.fetch_new_messages()), 200)
        self.assertEqual(self.bridge.fetch_new_messages()[0]['id'], 'm001')

    def test_old_invalid_incomplete_and_out_of_window_quarantined_not_deleted(self):
        paths = [self.put('incomplete', enriched=False), self.put('old', timestamp=1)]
        malformed = self.bridge.spool_dir / 'invalid.json'
        malformed.write_text('{invalid')
        paths.append(malformed)
        fresh = self.put('fresh', enriched=False)
        for path in paths:
            os.utime(path, (time.time() - 7200,) * 2)
        self.assertEqual(self.bridge.fetch_new_messages(), [])
        self.assertTrue(fresh.exists())
        self.assertEqual(len(list((self.bridge.spool_dir / 'quarantine').iterdir())), 3)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_filename_identity_mismatch_not_delivered_or_acknowledged(self):
        path = self.put('actual')
        path.rename(self.bridge.spool_dir / 'bad.json')
        self.assertEqual(self.bridge.fetch_new_messages(), [])

    def test_dead_bridge_restarts_and_closes_streams_and_releases_old_lock(self):
        dead, new = Mock(), Mock()
        dead.poll.return_value = 1
        new.poll.return_value = None
        lock = MagicMock()
        self.bridge.proc, self.bridge.session_lock = dead, lock
        self.bridge.ready, self.bridge.self_wid = True, 'old-self'
        with patch.object(config, 'WHATSAPP_ENABLED', True), patch.object(
                whatsapp, 'bridge_env', return_value={}), patch.object(
                whatsapp, '_find_system_chromium', return_value=''), patch.object(
                whatsapp, 'bridge_path', return_value='/tmp/offline.js'), patch.object(
                whatsapp.subprocess, 'Popen', return_value=new), patch.object(whatsapp.threading, 'Thread'):
            self.assertTrue(self.bridge.start())
        dead.stdin.close.assert_called_once()
        dead.stdout.close.assert_called_once()
        lock.__exit__.assert_called_once()
        self.assertIs(self.bridge.proc, new)
        self.assertFalse(self.bridge.ready)
        self.assertEqual(self.bridge.self_wid, '')
        self.bridge.session_lock.__exit__()
        self.bridge.session_lock = None


class EmailMetadataTests(IsolatedTests):
    def message(self):
        message = EmailMessage()
        message['From'] = 'Sender <sender@example.invalid>'
        message['To'] = 'Owner <owner@example.invalid>, Other <other@example.invalid>'
        message['Cc'] = 'Copy <copy@example.invalid>'
        message['Message-ID'] = '<native@example.invalid>'
        message['In-Reply-To'] = '<parent@example.invalid>'
        message['References'] = '<root@example.invalid> <parent@example.invalid>'
        message['Date'] = datetime.now(timezone.utc)
        message.set_content('body')
        return message

    def test_parse_preserves_native_thread_headers_and_recipients(self):
        entry = mail._parse_msg(self.message().as_bytes(), {'name': 'test', 'address': 'owner@example.invalid'})
        self.assertEqual(entry['message_id'], '<native@example.invalid>')
        self.assertEqual(entry['metadata']['in_reply_to'], '<parent@example.invalid>')
        self.assertEqual(entry['metadata']['references'], '<root@example.invalid> <parent@example.invalid>')
        self.assertEqual(entry['metadata']['recipients']['to'], ['owner@example.invalid', 'other@example.invalid'])
        self.assertEqual(entry['metadata']['recipients']['cc'], ['copy@example.invalid'])

    def test_live_metadata_merge_does_not_change_source_identity(self):
        imap = Mock()
        imap.response.return_value = ('UIDVALIDITY', [b'99'])
        imap.uid.side_effect = lambda command, *args: ('OK', [b'4']) if command == 'search' else (
            'OK', [(b'BODY', self.message().as_bytes())])
        with patch.object(mail, '_connect', return_value=imap):
            entries = mail.fetch_new_messages({'name': 'test', 'address': 'Owner@example.invalid'}, set())
        self.assertEqual(entries[0]['source_id'], mail.json_identity('owner@example.invalid', 'INBOX', '99', '4'))
        self.assertEqual(entries[0]['metadata']['in_reply_to'], '<parent@example.invalid>')
        self.assertEqual(entries[0]['metadata']['direction'], 'in')
        imap.logout.assert_called_once()


if __name__ == '__main__':
    unittest.main()