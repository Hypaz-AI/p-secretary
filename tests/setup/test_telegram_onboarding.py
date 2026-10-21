"""Offline onboarding regressions; no real session files or Telegram traffic."""
import asyncio
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors
from secretary.setup import telegram_account as setup


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        return SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
                               get_me=AsyncMock(return_value=None),
                               send_code_request=AsyncMock(), sign_in=AsyncMock(),
                               qr_login=AsyncMock())

    async def authorize(self, client, inputs, codes):
        with patch('telethon.TelegramClient', return_value=client), \
             patch.object(setup, 'api_credentials', return_value=(1, 'private')), \
             patch.object(setup, 'session_path', return_value='unused.session'), \
             patch.object(setup, 'secure_session'), \
             patch.object(setup.config, 'TELEGRAM_PHONE', '+391234567890'), \
             patch.object(setup.config, 'set_env'), \
             patch('builtins.input', side_effect=inputs), \
             patch.object(setup, 'login_code', side_effect=codes), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            user = SimpleNamespace(phone='391234567890', bot=False)
            client.get_me.side_effect = [None, user]
            await setup.authorize()
            return output.getvalue()

    async def test_resend_unavailable_keeps_code_flow_without_spam(self):
        client = self.client()
        sent = SimpleNamespace(type=SimpleNamespace(), next_type=SimpleNamespace(),
                               timeout=0, phone_code_hash='secret-hash')
        client.send_code_request.side_effect = [sent, errors.SendCodeUnavailableError(None)]
        client.sign_in.return_value = SimpleNamespace(phone='391234567890', bot=False)
        output = await self.authorize(client, ['', ''], ['r', 'r', '12345'])
        self.assertEqual(client.send_code_request.await_count, 2)
        self.assertIn('Telegram rifiuta ulteriori metodi', output)
        self.assertNotIn('Autorizzazione fallita', output)
        self.assertNotIn('secret-hash', output)
        client.sign_in.assert_awaited_once_with(phone='+391234567890', code='12345',
                                               phone_code_hash='secret-hash')
        client.disconnect.assert_awaited_once()

    async def test_initial_unavailable_can_switch_to_qr(self):
        client = self.client()
        client.send_code_request.side_effect = errors.SendCodeUnavailableError(None)
        user = SimpleNamespace(phone='391234567890', bot=False)
        with patch.object(setup, 'qr_login', AsyncMock(return_value=user)):
            await self.authorize(client, ['', ''], ['r', 'qr'])
        client.send_code_request.assert_awaited_once()

    async def test_no_next_method_disables_resend(self):
        client = self.client()
        client.send_code_request.return_value = SimpleNamespace(
            type=SimpleNamespace(), next_type=None, timeout=0, phone_code_hash='secret')
        client.sign_in.return_value = SimpleNamespace(phone='391234567890', bot=False)
        await self.authorize(client, ['', ''], ['r', '12345'])
        client.send_code_request.assert_awaited_once()

    async def test_qr_selected_never_requests_phone_code(self):
        client = self.client()
        with patch.object(setup, 'qr_login', AsyncMock(return_value=SimpleNamespace(
                phone='391234567890', bot=False))):
            await self.authorize(client, ['', 'qr'], [])
        client.send_code_request.assert_not_awaited()

    async def test_qr_wrong_account_rejected(self):
        client = self.client()
        with patch.object(setup, 'qr_login', AsyncMock(return_value=SimpleNamespace(
                phone='39999999999', bot=False))):
            with self.assertRaisesRegex(ValueError, 'altro account'):
                await self.authorize(client, ['', 'qr'], [])
        client.disconnect.assert_awaited_once()

    async def run_qr(self, client, answers, module):
        with patch.dict('sys.modules', qrcode=module), \
             patch('builtins.input', side_effect=answers), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            result = await setup.qr_login(client)
            self.assertNotIn('tg://login', output.getvalue())
            self.assertNotIn('secret-token', output.getvalue())
            return result

    async def test_qr_timeout_refresh_and_2fa(self):
        client = self.client()
        qr = SimpleNamespace(url='tg://login?token=secret-token',
                             wait=AsyncMock(side_effect=[asyncio.TimeoutError(),
                                                        errors.SessionPasswordNeededError(None)]),
                             recreate=AsyncMock())
        client.qr_login.return_value = qr
        user = SimpleNamespace(bot=False)
        client.sign_in.side_effect = [errors.PasswordHashInvalidError(None), user]
        renderer = Mock()
        with patch.object(setup.getpass, 'getpass', side_effect=['wrong', 'private-password']):
            result = await self.run_qr(client, ['s', 's'], renderer)
        self.assertIs(result, user)
        qr.recreate.assert_awaited_once()
        self.assertEqual(qr.wait.await_count, 2)
        renderer.QRCode.return_value.add_data.assert_called_with(qr.url)
        client.send_code_request.assert_not_awaited()

    async def test_qr_requires_consent(self):
        client = self.client()
        self.assertIsNone(await self.run_qr(client, ['n'], Mock()))
        client.qr_login.assert_not_awaited()

    async def test_changed_phone_not_stale_config_after_qr_2fa(self):
        client = self.client()
        user = SimpleNamespace(phone='398765432100', bot=False)
        client.get_me.side_effect = [None, user]
        qr = SimpleNamespace(url='tg://login?token=secret-token',
                             wait=AsyncMock(side_effect=errors.SessionPasswordNeededError(None)))
        client.qr_login.return_value = qr
        client.sign_in.return_value = user
        with patch('telethon.TelegramClient', return_value=client), \
             patch.object(setup, 'api_credentials', return_value=(1, 'private')), \
             patch.object(setup, 'session_path', return_value='unused.session'), \
             patch.object(setup, 'secure_session'), \
             patch.object(setup.config, 'TELEGRAM_PHONE', '+391234567890'), \
             patch.object(setup.config, 'set_env') as save, \
             patch.dict('sys.modules', qrcode=Mock()), \
             patch('builtins.input', side_effect=['+398765432100', 'qr', 's']), \
             patch.object(setup.getpass, 'getpass', return_value='private-password'), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            await setup.authorize()
        save.assert_called_once_with('TELEGRAM_PHONE', '+398765432100')
        client.sign_in.assert_awaited_once_with(password='private-password')
        client.send_code_request.assert_not_awaited()
        self.assertNotIn('391234567890', output.getvalue())
        client.disconnect.assert_awaited_once()

    async def test_real_telethon_password_branch_without_phone_or_code(self):
        from telethon.client.auth import AuthMethods
        from telethon import functions, types
        from telethon.tl.custom.qrlogin import QRLogin
        user = SimpleNamespace(phone='391234567890', bot=False)
        calls = []

        async def rpc(request):
            calls.append(type(request))
            if isinstance(request, functions.auth.ExportLoginTokenRequest):
                raise errors.SessionPasswordNeededError(request)
            if isinstance(request, functions.account.GetPasswordRequest):
                return 'mock-password-parameters'
            if isinstance(request, functions.auth.CheckPasswordRequest):
                return SimpleNamespace(user=user)
            raise AssertionError(type(request))

        client = Mock()
        client._phone = None
        client.get_me = AsyncMock(return_value=None)
        client._on_login = AsyncMock(return_value=user)
        client.side_effect = rpc
        client.sign_in = lambda **kw: AuthMethods.sign_in(client, **kw)
        qr = QRLogin(client, [])
        # Exercise real wait(), including handler registration before the scan.
        from datetime import datetime, timedelta, timezone
        qr._resp = SimpleNamespace(expires=datetime.now(timezone.utc) + timedelta(seconds=60), token=b'offline')
        def register(handler, event):
            asyncio.create_task(handler(None))
        client.add_event_handler.side_effect = register
        client.qr_login = AsyncMock(return_value=qr)
        with patch('telethon.client.auth.pwd_mod.compute_check', return_value='mock-srp'), \
             patch.object(setup.getpass, 'getpass', return_value='private-password'):
            self.assertIs(await self.run_qr(client, ['s'], Mock()), user)
        self.assertEqual(calls, [functions.auth.ExportLoginTokenRequest,
                                functions.account.GetPasswordRequest,
                                functions.auth.CheckPasswordRequest])
        client._parse_phone_and_hash.assert_not_called()
        client.send_code_request.assert_not_called()
        client.remove_event_handler.assert_called_once()

    async def test_empty_password_and_local_value_error_are_distinct(self):
        client = self.client()
        client.sign_in.side_effect = ValueError('private internal details')
        with patch.object(setup.getpass, 'getpass', side_effect=['', 'private-password']), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            with self.assertRaisesRegex(setup.AuthorizationFailure, 'Errore locale Telethon') as exc:
                await setup.password_login(client)
        client.sign_in.assert_awaited_once_with(password='private-password')
        self.assertIn('vuota', output.getvalue())
        self.assertNotIn('private', str(exc.exception))

    def test_identity_normalized_masked_and_fail_closed(self):
        setup.verify_identity(SimpleNamespace(phone='+391234567890', bot=False), '\"+391234567890\"')
        for user in (None, SimpleNamespace(phone=None, bot=False),
                     SimpleNamespace(phone='39999999999', bot=False),
                     SimpleNamespace(phone='391234567890', bot=True)):
            with self.assertRaises(setup.AuthorizationFailure) as exc:
                setup.verify_identity(user, '+391234567890')
            self.assertNotIn('391234567890', str(exc.exception))
            self.assertNotIn('39999999999', str(exc.exception))

    async def test_post_login_session_missing_rejected(self):
        client = self.client()
        client.get_me.side_effect = [None, None]
        user = SimpleNamespace(phone='391234567890', bot=False)
        with patch('telethon.TelegramClient', return_value=client), \
             patch.object(setup, 'api_credentials', return_value=(1, 'private')), \
             patch.object(setup, 'session_path', return_value='unused.session'), \
             patch.object(setup, 'secure_session'), \
             patch.object(setup.config, 'TELEGRAM_PHONE', '+391234567890'), \
             patch.object(setup.config, 'set_env'), \
             patch.object(setup, 'qr_login', AsyncMock(return_value=user)), \
             patch('builtins.input', side_effect=['', 'qr']), \
             patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaisesRegex(setup.AuthorizationFailure, 'Verifica sessione fallita'):
                await setup.authorize()
        client.disconnect.assert_awaited_once()

    async def test_main_specific_failure_never_enables_ingestion(self):
        failure = setup.AuthorizationFailure('Sessione di un altro account: +***890 / +***999')
        with patch.object(setup.sys.stdin, 'isatty', return_value=True), \
             patch.object(setup, 'guide'), patch.object(setup.os, 'umask'), \
             patch.object(setup, 'FileLock'), \
             patch.object(setup, 'session_path', return_value='unused.session'), \
             patch.object(setup, 'authorize', AsyncMock(side_effect=failure)), \
             patch.object(setup.config, 'set_env') as save, \
             patch('builtins.input') as consent, \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            # main owns its event loop, so invoke it outside this async test loop.
            result = await asyncio.to_thread(setup.main)
        self.assertEqual(result, 1)
        self.assertIn('Sessione di un altro account', output.getvalue())
        self.assertNotIn('Autorizzazione fallita (ValueError)', output.getvalue())
        save.assert_not_called()
        consent.assert_not_called()

    async def test_qr_refresh_is_bounded(self):
        client = self.client()
        qr = SimpleNamespace(url='tg://login?token=secret-token',
                             wait=AsyncMock(side_effect=asyncio.TimeoutError), recreate=AsyncMock())
        client.qr_login.return_value = qr
        self.assertIsNone(await self.run_qr(client, ['s', 's', 's'], Mock()))
        self.assertEqual(qr.wait.await_count, 3)
        self.assertEqual(qr.recreate.await_count, 2)


if __name__ == '__main__':
    unittest.main()