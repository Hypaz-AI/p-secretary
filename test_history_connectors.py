"""Offline historical connector contracts; never use real sessions or networks."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from connectors import email as mail, telegram_account as tg
from connectors.storage import FileLock


START = datetime(2020, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(days=1)
ACCOUNT = {'name': 'offline', 'address': 'Test@example.invalid'}


def raw(date):
    msg = EmailMessage()
    msg['Date'] = date
    msg['From'] = 'Sender <sender@example.invalid>'
    msg['Subject'] = 'History'
    msg.set_content('body')
    return msg.as_bytes()


def message(date=START, **changes):
    values = dict(id=42, chat_id=7, sender_id=8, date=date, edit_date=None,
                  action=None, out=False, is_private=True, is_group=False,
                  media=None, raw_text='history',
                  sender=SimpleNamespace(first_name='Sender'),
                  chat=SimpleNamespace(first_name='Chat'))
    values.update(changes)
    return SimpleNamespace(**values)


class EmailHistoryTests(unittest.TestCase):
    def setUp(self):
        self.imap = Mock()
        self.imap.list.return_value = ('OK', [b'(\\Sent) "/" "Custom Sent"'])
        self.imap.select.return_value = ('OK', [b'1'])
        self.imap.response.return_value = ('UIDVALIDITY', [b'99'])
        self.payloads = [raw('Wed, 01 Jan 2020 01:00:00 +0100')]

        def uid(command, *args):
            if command == 'search':
                return 'OK', [b' '.join(str(i).encode() for i in range(len(self.payloads)))]
            return 'OK', [(b'BODY[]', self.payloads[int(args[0])]), b')']
        self.imap.uid.side_effect = uid
        p = patch.object(mail, '_connect', return_value=self.imap)
        self.connect = p.start()
        self.addCleanup(p.stop)

    def test_exact_window_both_folders_read_only_and_identity(self):
        self.payloads += [raw('Thu, 02 Jan 2020 00:00:00 +0000'),
                          raw('Tue, 31 Dec 2019 23:59:59 +0000')]
        rows = list(mail.iter_history_messages(ACCOUNT, START, END))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['direction'] for r in rows], ['in', 'out'])
        self.assertEqual(datetime.fromisoformat(rows[0]['ts']), START)
        self.assertEqual(rows[1]['metadata']['folder'], '"Custom Sent"')
        self.assertNotEqual(rows[0]['source_id'], rows[1]['source_id'])
        self.assertEqual(rows[0]['metadata']['uidvalidity'], '99')
        for call in self.imap.select.call_args_list:
            self.assertEqual(call.kwargs, {'readonly': True})
        for call in self.imap.uid.call_args_list:
            self.assertIn(call.args[0], ('search', 'fetch'))
            if call.args[0] == 'fetch':
                self.assertEqual(call.args[-1], '(BODY.PEEK[])')
        self.imap.logout.assert_called_once()

    def test_no_hundred_message_cap(self):
        self.payloads *= 125
        self.assertEqual(len(list(mail.iter_history_messages(ACCOUNT, START, END))), 250)

    def test_missing_sent_explicit_and_cleanup(self):
        self.imap.select.return_value = ('NO', [])
        with self.assertRaisesRegex(RuntimeError, 'Sent folder unavailable'):
            list(mail.iter_history_messages(ACCOUNT, START, END))
        self.imap.logout.assert_called_once()

    def test_protocol_errors_propagate_and_cleanup(self):
        for command in ('search', 'fetch'):
            original = self.imap.uid.side_effect
            def fail(op, *args):
                return ('NO', []) if op == command else original(op, *args)
            with self.subTest(command=command):
                self.imap.uid.side_effect = fail
                with self.assertRaises(RuntimeError):
                    list(mail.iter_history_messages(ACCOUNT, START, END))
                self.imap.uid.side_effect = original
        self.assertEqual(self.imap.logout.call_count, 2)

    def test_bad_date_is_not_silently_skipped(self):
        self.payloads = [raw('Wed, 01 Jan 2020 00:00:00 -0000')]
        with self.assertRaises(ValueError):
            list(mail.iter_history_messages(ACCOUNT, START, END))
        self.imap.logout.assert_called_once()

    def test_generator_close_logs_out(self):
        iterator = mail.iter_history_messages(ACCOUNT, START, END)
        next(iterator)
        iterator.close()
        self.imap.logout.assert_called_once()

    def test_invalid_window_before_connect(self):
        for start, end in ((START.replace(tzinfo=None), END), (END, START), (START, START)):
            with self.assertRaises(ValueError):
                list(mail.iter_history_messages(ACCOUNT, start, end))
        self.connect.assert_not_called()


class TelegramHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'private' / 'personal.session'
        self.path.parent.mkdir()
        self.path.touch()
        self.client = Mock()
        self.client.connect = AsyncMock()
        self.client.disconnect = AsyncMock()
        self.client.is_user_authorized = AsyncMock(return_value=True)
        self.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9, bot=False))
        self.module = SimpleNamespace(TelegramClient=Mock(return_value=self.client))
        for p in (patch.dict(sys.modules, telethon=self.module),
                  patch.object(tg, 'session_path', return_value=self.path),
                  patch.object(tg, 'api_credentials', return_value=(1, 'offline'))):
            p.start()
            self.addCleanup(p.stop)

    async def test_all_dialogs_unlimited_exact_window_no_watermarks(self):
        async def dialogs(**kwargs):
            self.assertEqual(kwargs, {'limit': None})
            for i in range(105):
                yield SimpleNamespace(entity=i)

        async def messages(entity, **kwargs):
            self.assertEqual(kwargs, {'limit': None, 'offset_date': END})
            yield message(END)
            for i in range(105):
                yield message(START, id=i, chat_id=entity)
            yield message(START - timedelta(seconds=1))
            raise AssertionError('must stop at cutoff')

        self.client.iter_dialogs.side_effect = dialogs
        self.client.iter_messages.side_effect = messages
        with patch.object(tg.db, 'store_message') as store, \
             patch('recovery.completed') as completed, \
             patch('recovery.scan_start') as scan, \
             patch.object(tg, 'history_window', side_effect=AssertionError('live window used')):
            self.assertEqual(await tg.import_history(START, END), 105 * 105)
        self.assertEqual(store.call_count, 105 * 105)
        self.assertEqual(store.call_args.kwargs['ts'], START.isoformat())
        self.assertTrue(store.call_args.kwargs['metadata']['recovered'])
        completed.assert_not_called()
        scan.assert_not_called()
        self.module.TelegramClient.assert_called_once_with(
            str(self.path), 1, 'offline', receive_updates=False, flood_sleep_threshold=0)
        for name in ('start', 'sign_in', 'add_event_handler', 'send_message', 'send_read_acknowledge'):
            getattr(self.client, name).assert_not_called()
        self.client.disconnect.assert_awaited_once()

    async def test_missing_or_busy_session_never_constructs_client(self):
        with FileLock(str(self.path) + '.owner.lock', blocking=False):
            with self.assertRaises(OSError):
                await tg.import_history(START, END)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            await tg.import_history(START, END)
        self.module.TelegramClient.assert_not_called()

    async def test_refuses_unauthorized_and_bot(self):
        for authorized, bot in ((False, False), (True, True)):
            self.client.is_user_authorized.return_value = authorized
            self.client.get_me.return_value.bot = bot
            with self.assertRaises(PermissionError):
                await tg.import_history(START, END)
        self.assertEqual(self.client.disconnect.await_count, 2)
        self.client.iter_dialogs.assert_not_called()
        self.client.start.assert_not_called()

    async def test_scan_failure_disconnects_and_releases_lock(self):
        async def dialogs(**kwargs):
            raise RuntimeError('offline scan failure')
            yield
        self.client.iter_dialogs.side_effect = dialogs
        with self.assertRaisesRegex(RuntimeError, 'offline scan failure'):
            await tg.import_history(START, END)
        self.client.disconnect.assert_awaited_once()
        with FileLock(str(self.path) + '.owner.lock', blocking=False):
            pass

    async def test_invalid_window_before_client(self):
        for start, end in ((START.replace(tzinfo=None), END), (END, START), (START, START)):
            with self.assertRaises(ValueError):
                await tg.import_history(start, end)
        self.module.TelegramClient.assert_not_called()


if __name__ == '__main__':
    unittest.main()