"""Offline MTProto tests: no Telegram connection, login or sends."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import sys

import config
import message_store
from connectors import telegram_account as tg


def message(**changes):
    m = dict(id=42, chat_id=-100123, sender_id=8, out=False,
             date=datetime.now(timezone.utc), edit_date=None, raw_text='/note',
             media=None, action=None, is_private=False, is_group=False,
             sender=None, chat=None)
    m.update(changes)
    return SimpleNamespace(**m)


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_native_ingestion_and_edit(self):
        worker = tg.TelegramAccountWorker()
        worker.account = 'user:7'
        with patch.object(tg.db, 'store_message', return_value=12) as store:
            await worker.archive(message())
            first = store.call_args
            self.assertEqual(first.args[0], 'telegram (account)')
            self.assertEqual(first.kwargs['source_id'], '["mtproto","user:7","-100123",42]')
            self.assertEqual(first.kwargs['metadata']['account'], 'user:7')
            self.assertEqual(first.kwargs['metadata']['chat_type'], 'channel')
            await worker.archive(message(out=True, raw_text='edited',
                                         edit_date=datetime.now(timezone.utc)))
            self.assertEqual(first.kwargs['source_id'], store.call_args.kwargs['source_id'])
            self.assertEqual(store.call_args.kwargs['metadata']['direction'], 'out')
            self.assertTrue(store.call_args.kwargs['update_existing'])
            worker.account = 'user:9'
            await worker.archive(message(is_private=True))
            self.assertNotEqual(first.kwargs['source_id'], store.call_args.kwargs['source_id'])

    async def test_readable_sender_and_chat_names(self):
        worker = tg.TelegramAccountWorker()
        worker.account = 'user:7'
        group = SimpleNamespace(title='Gruppo Calcetto')
        person = SimpleNamespace(first_name='Marco', last_name='Rossi', username='mrossi')
        with patch.object(tg.db, 'store_message') as store:
            # gruppo: autore e chat risolti dalla cache (nessuna RPC)
            await worker.archive(message(is_group=True, sender=person, chat=group))
            self.assertEqual(store.call_args.args[1], 'Marco Rossi (@mrossi) in Gruppo Calcetto')
            self.assertEqual(store.call_args.args[2], 'Gruppo Calcetto')
            self.assertEqual(store.call_args.kwargs['metadata']['sender_name'],
                             'Marco Rossi (@mrossi)')
            self.assertEqual(store.call_args.kwargs['metadata']['chat_name'], 'Gruppo Calcetto')
            # uscita: destinazione leggibile, non un ID
            await worker.archive(message(out=True, sender=person, chat=group))
            self.assertEqual(store.call_args.args[1], 'io → Gruppo Calcetto')
            # privata senza entità in cache: fallback bounded, mai ID nudo
            fallback = message(is_private=True, chat_id=77, sender=None, chat=None)
            anna = SimpleNamespace(first_name='Anna', last_name=None, username='anna')
            fallback.get_sender = AsyncMock(return_value=anna)
            fallback.get_chat = AsyncMock(return_value=anna)
            await worker.archive(fallback)
            self.assertEqual(store.call_args.args[1], 'Anna (@anna)')

    async def test_display_name_formats(self):
        self.assertEqual(tg.display_name(None), '')
        self.assertEqual(tg.display_name(SimpleNamespace(title='Canale News')), 'Canale News')
        self.assertEqual(tg.display_name(
            SimpleNamespace(first_name='Anna', last_name=None, username='anna_x')),
            'Anna (@anna_x)')
        self.assertEqual(tg.display_name(
            SimpleNamespace(first_name=None, last_name=None, username='solo')), '@solo')
        self.assertEqual(tg.display_name(SimpleNamespace(first_name='Bo')), 'Bo')

    async def test_window_rejects_old_future_and_service(self):
        worker = tg.TelegramAccountWorker()
        start, end = tg.history_window()
        with patch.object(tg.db, 'store_message') as store:
            self.assertFalse(await worker.archive(message(date=start - timedelta(seconds=1))))
            self.assertFalse(await worker.archive(message(date=end)))
            self.assertFalse(await worker.archive(message(action=object())))
            store.assert_not_called()

    async def test_recovery_limits_and_early_stop(self):
        worker = tg.TelegramAccountWorker()
        worker.account = 'user:7'
        calls = []
        async def dialogs(**kwargs):
            calls.append(('dialogs', kwargs))
            yield SimpleNamespace(entity='group')
        async def messages(entity, **kwargs):
            calls.append(('messages', kwargs))
            yield message(is_group=True)
            yield message(date=tg.history_window()[0] - timedelta(seconds=1))
            raise AssertionError('old history paged')
        worker.client = SimpleNamespace(iter_dialogs=dialogs, iter_messages=messages)
        with patch.object(tg.db, 'store_message') as store:
            await worker.recover()
        store.assert_called_once()
        self.assertEqual(calls[0][1]['limit'], config.TELEGRAM_USER_DIALOG_LIMIT)
        self.assertEqual(calls[1][1]['limit'], config.TELEGRAM_USER_MESSAGE_LIMIT)
        self.assertTrue(store.call_args.kwargs['metadata']['recovered'])

    async def test_flood_wait_defers_entire_pass(self):
        from telethon.errors import FloodWaitError

        for flood_in_dialogs in (False, True):
            with self.subTest(flood_in_dialogs=flood_in_dialogs):
                worker = tg.TelegramAccountWorker()
                calls = []

                async def dialogs(**kwargs):
                    calls.append('dialogs')
                    if flood_in_dialogs:
                        raise FloodWaitError(request=None, capture=120)
                    yield SimpleNamespace(entity='first')
                    yield SimpleNamespace(entity='must-not-request')

                async def messages(entity, **kwargs):
                    calls.append(entity)
                    raise FloodWaitError(request=None, capture=120)
                    yield  # async iterator

                worker.client = SimpleNamespace(iter_dialogs=dialogs, iter_messages=messages,
                                                disconnect=AsyncMock())
                worker.archive = AsyncMock()
                with patch.object(tg.time, 'monotonic', return_value=1000) as clock, \
                     patch('builtins.print') as log:
                    await worker.recover()
                    expected = ['dialogs'] if flood_in_dialogs else ['dialogs', 'first']
                    self.assertEqual(calls, expected)
                    self.assertEqual(worker.recovery_not_before, 1120)
                    clock.return_value = 1119
                    await worker.recover()
                    self.assertEqual(calls, expected)
                    log.assert_called_once_with(
                        '[telegram-account] recovery rate limited; deferred for 120s; live archive remains active')
                    # Live events are still processed during the recovery cooldown.
                    await worker._on_message(SimpleNamespace(message=message()))
                    worker.archive.assert_awaited_once()
                    clock.return_value = 1120
                    await worker.recover()
                    self.assertEqual(calls, expected + expected)
                worker.client.disconnect.assert_not_awaited()

    async def test_unauthorized_no_login(self):
        client = Mock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=False)
        module = SimpleNamespace(TelegramClient=Mock(return_value=client), events=Mock())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private' / 'personal.session'
            path.parent.mkdir()
            path.touch()
            with patch.dict(sys.modules, telethon=module), \
                 patch.object(config, 'TELEGRAM_USER_SESSION', path), \
                 patch.object(tg, 'api_credentials', return_value=(1, 'mock')):
                await tg.TelegramAccountWorker()._serve()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        client.start.assert_not_called()
        client.sign_in.assert_not_called()
        client.send_message.assert_not_called()
        client.disconnect.assert_awaited_once()

    async def test_live_handlers_and_shutdown(self):
        client = Mock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_me = AsyncMock(return_value=SimpleNamespace(id=7, bot=False))
        client.is_connected.return_value = True
        module = SimpleNamespace(TelegramClient=Mock(return_value=client), events=Mock())
        worker = tg.TelegramAccountWorker()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private' / 'personal.session'
            path.parent.mkdir()
            path.touch()
            with patch.dict(sys.modules, telethon=module), \
                 patch.object(config, 'TELEGRAM_USER_SESSION', path), \
                 patch.object(tg, 'api_credentials', return_value=(1, 'mock')), \
                 patch.object(worker, 'recover', AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await worker._serve()
        self.assertEqual(client.add_event_handler.call_count, 2)
        module.events.NewMessage.assert_called_once()
        module.events.MessageEdited.assert_called_once()
        client.disconnect.assert_awaited_once()
        client.start.assert_not_called()
        client.send_message.assert_not_called()

    def test_disabled_no_thread(self):
        with patch.object(config, 'TELEGRAM_USER_ENABLED', False):
            worker = tg.TelegramAccountWorker()
            worker.start()
            self.assertIsNone(worker.thread)

    def test_missing_credentials_no_thread(self):
        with patch.object(config, 'TELEGRAM_USER_ENABLED', True), \
             patch.object(config, 'TELEGRAM_API_ID', ''), \
             patch.object(tg.flows, 'mark') as mark:
            worker = tg.TelegramAccountWorker()
            worker.start()
            self.assertIsNone(worker.thread)
            mark.assert_called_once_with('telegram_user', 'credentials_missing')

    async def test_media_placeholder_original_timestamp(self):
        worker = tg.TelegramAccountWorker()
        worker.account = 'user:7'
        original = datetime.now(timezone.utc) - timedelta(minutes=5)
        with patch.object(tg.db, 'store_message') as store:
            await worker.archive(message(raw_text='', media=object(), date=original))
        self.assertEqual(store.call_args.args[3], '[Telegram media]')
        self.assertEqual(store.call_args.kwargs['ts'], original.isoformat())
        self.assertTrue(store.call_args.kwargs['metadata']['has_media'])

    def test_timezone_dst_window(self):
        with patch.object(config, 'TIMEZONE', 'Europe/Rome'):
            start, end = tg.history_window(datetime(2026, 10, 25, 12, tzinfo=timezone.utc))
        self.assertEqual(start.day, 24)
        self.assertEqual(end.day, 26)
        self.assertEqual((end.timestamp() - start.timestamp()) / 3600, 49)

    def test_native_edit_sql_no_schema_reset(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (12,)
        message_store.store(conn, 'telegram (account)', '8', '', 'edited',
                            ts='2026-10-14T10:00:00+00:00', source_id='scoped',
                            metadata={'account': 'user:7', 'chat_id': '-100123',
                                      'revision_date': '2026-10-14T11:00:00+00:00'},
                            update_existing=True)
        sql = '\n'.join(c.args[0] for c in conn.execute.call_args_list)
        self.assertIn('INSERT INTO telegram_messages', sql)
        self.assertIn('DO UPDATE SET body=excluded.body', sql)
        self.assertIn('::timestamptz >=', sql)
        self.assertNotIn('DROP', sql)


if __name__ == '__main__':
    unittest.main()