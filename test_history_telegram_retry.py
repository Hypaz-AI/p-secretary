"""Offline historical FloodWait regression tests; no Telegram or database I/O."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from connectors import telegram_account as tg
from connectors.storage import FileLock


START = datetime(2020, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(days=1)


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__('secret phone session api_hash')
        self.seconds = seconds


def message(identifier=1):
    return SimpleNamespace(
        id=identifier, chat_id=7, sender_id=8, date=START, edit_date=None,
        action=None, out=False, is_private=True, is_group=False, media=None,
        raw_text='private body', sender=SimpleNamespace(first_name='Sender'),
        chat=SimpleNamespace(first_name='Chat'))


class HistoryRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'personal.session'
        self.path.touch()
        self.client = Mock()
        self.client.connect = AsyncMock()
        self.client.disconnect = AsyncMock()
        self.client.is_user_authorized = AsyncMock(return_value=True)
        self.client.get_me = AsyncMock(return_value=SimpleNamespace(id=9, bot=False))
        module = ModuleType('telethon')
        module.TelegramClient = Mock(return_value=self.client)
        errors = ModuleType('telethon.errors')
        errors.FloodWaitError = FloodWaitError
        self.store = Mock()
        self.sleep = AsyncMock()
        self.output = Mock()
        self.completed = Mock()
        self.scan_start = Mock()
        for p in (
                patch.dict(sys.modules, {'telethon': module, 'telethon.errors': errors}),
                patch.object(tg, 'session_path', return_value=self.path),
                patch.object(tg, 'api_credentials', return_value=(1, 'offline')),
                patch.object(tg.db, 'store_message', self.store),
                patch.object(tg.asyncio, 'sleep', self.sleep),
                patch('builtins.print', self.output),
                patch('recovery.completed', self.completed),
                patch('recovery.scan_start', self.scan_start)):
            p.start()
            self.addCleanup(p.stop)

        async def dialogs(**kwargs):
            self.assertEqual(kwargs, {'limit': None})
            yield SimpleNamespace(entity=7)

        async def messages(entity, **kwargs):
            self.assertEqual(kwargs, {'limit': None, 'offset_date': END})
            yield message()

        self.client.iter_dialogs.side_effect = dialogs
        self.client.iter_messages.side_effect = messages

    def assert_cleaned_up(self):
        self.client.disconnect.assert_awaited_once()
        with FileLock(str(self.path) + '.owner.lock', blocking=False):
            pass
        self.completed.assert_not_called()
        self.scan_start.assert_not_called()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_setup_requests_retry_after_cooldown(self):
        for name in ('connect', 'is_user_authorized', 'get_me'):
            with self.subTest(request=name):
                operation = getattr(self.client, name)
                result = operation.return_value
                operation.side_effect = [FloodWaitError(2), result]
                self.assertEqual(await tg.import_history(START, END), 1)
                self.sleep.assert_awaited_with(2)
                operation.side_effect = None
        self.assertEqual(self.sleep.await_count, 3)

    async def test_partial_message_iteration_restarts_without_duplicates(self):
        attempts = 0

        async def messages(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            yield message()
            if attempts == 1:
                raise FloodWaitError(7)
            yield message(2)

        self.client.iter_messages.side_effect = messages

        async def cooldown(seconds):
            self.assertEqual(seconds, 7)
            self.assertEqual(attempts, 1)
            self.assertEqual(self.client.iter_dialogs.call_count, 1)
            self.client.disconnect.assert_not_awaited()
            with self.assertRaises(OSError):
                with FileLock(str(self.path) + '.owner.lock', blocking=False):
                    pass

        self.sleep.side_effect = cooldown
        self.assertEqual(await tg.import_history(START, END), 2)
        self.assertEqual(self.store.call_count, 2)
        self.assertEqual(attempts, 2)
        self.assertIn('7s', str(self.output.call_args_list))
        self.assertNotIn('secret', str(self.output.call_args_list))
        self.assert_cleaned_up()

    async def test_partial_dialog_iteration_retries_full_scan(self):
        attempts = 0

        async def dialogs(**kwargs):
            nonlocal attempts
            attempts += 1
            yield SimpleNamespace(entity=7)
            if attempts == 1:
                raise FloodWaitError(3)

        self.client.iter_dialogs.side_effect = dialogs
        self.assertEqual(await tg.import_history(START, END), 1)
        self.assertEqual(self.store.call_count, 1)
        self.sleep.assert_awaited_once_with(3)
        self.assert_cleaned_up()

    async def test_long_default_wait_fails_without_sleep_or_retry(self):
        self.client.get_me.side_effect = FloodWaitError(301)
        with self.assertRaisesRegex(RuntimeError, '301s.*300s.*Retry later') as caught:
            await tg.import_history(START, END)
        self.assertNotIn('secret', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.sleep.assert_not_awaited()
        self.client.get_me.assert_awaited_once()
        self.client.iter_dialogs.assert_not_called()
        self.assert_cleaned_up()

    async def test_budget_is_cumulative_across_setup_and_scans(self):
        self.client.connect.side_effect = [FloodWaitError(2), None]

        async def dialogs(**kwargs):
            raise FloodWaitError(2)
            yield

        self.client.iter_dialogs.side_effect = dialogs
        with self.assertRaisesRegex(RuntimeError, 'after 2s'):
            await tg.import_history(START, END, max_flood_wait=3)
        self.sleep.assert_awaited_once_with(2)
        self.client.iter_dialogs.assert_called_once()
        self.assert_cleaned_up()

    async def test_exact_budget_allowed_then_repeated_wait_stops(self):
        self.client.get_me.side_effect = FloodWaitError(2)
        with self.assertRaises(RuntimeError):
            await tg.import_history(START, END, max_flood_wait=2)
        self.sleep.assert_awaited_once_with(2)
        self.assertEqual(self.client.get_me.await_count, 2)
        self.assert_cleaned_up()

    async def test_cancel_during_cooldown_disconnects_and_unlocks(self):
        self.client.get_me.side_effect = FloodWaitError(1)
        self.sleep.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await tg.import_history(START, END)
        self.client.get_me.assert_awaited_once()
        self.assert_cleaned_up()

    async def test_invalid_budget_before_client_requests(self):
        for budget in (-1, float('inf'), float('nan'), '300', None):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                await tg.import_history(START, END, max_flood_wait=budget)
        self.client.connect.assert_not_awaited()

    async def test_unrelated_failure_propagates_without_retry(self):
        self.client.get_me.side_effect = ValueError('offline failure')
        with self.assertRaisesRegex(ValueError, 'offline failure'):
            await tg.import_history(START, END)
        self.sleep.assert_not_awaited()
        self.assert_cleaned_up()


if __name__ == '__main__':
    unittest.main()