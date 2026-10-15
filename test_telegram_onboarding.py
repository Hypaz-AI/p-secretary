"""Offline onboarding regressions; no real session files or Telegram traffic."""
import asyncio
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors
import setup_telegram_account as setup


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