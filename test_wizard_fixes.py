"""Offline wizard regressions: no provider requests or real session changes."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import installer
import setup_telegram_account as telegram
import setup_whatsapp as whatsapp
from connectors.storage import FileLock


class WizardFixTests(unittest.TestCase):
    def test_authorized_telegram_never_requests_code(self):
        client = SimpleNamespace(connect=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(bot=False, phone='391234567')),
                                 disconnect=AsyncMock(), send_code_request=AsyncMock())
        with patch('telethon.TelegramClient', return_value=client), patch.object(telegram, 'api_credentials', return_value=(1, 'hash')), \
             patch.object(telegram, 'session_path', return_value=Path('/unused')), patch.object(telegram, 'secure_session'), \
             patch.object(telegram.config, 'TELEGRAM_PHONE', '+391234567'):
            asyncio.run(telegram.authorize())
        client.send_code_request.assert_not_called()
        client.disconnect.assert_awaited_once()

    def test_telegram_expired_invalid_and_2fa(self):
        from telethon import errors
        sent = SimpleNamespace(type=type('SentCodeTypeApp', (), {})(), timeout=0, phone_code_hash='secret-hash')
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), get_me=AsyncMock(return_value=None),
                                 send_code_request=AsyncMock(return_value=sent), sign_in=AsyncMock(side_effect=[
                                     errors.PhoneCodeInvalidError(None), errors.PhoneCodeExpiredError(None),
                                     errors.SessionPasswordNeededError(None), SimpleNamespace(bot=False)]))
        with patch('telethon.TelegramClient', return_value=client), patch.object(telegram, 'api_credentials', return_value=(1, 'hash')), \
             patch.object(telegram, 'session_path', return_value=Path('/unused')), patch.object(telegram, 'secure_session'), \
             patch.object(telegram.config, 'TELEGRAM_PHONE', '+391234567'), patch.object(telegram.config, 'set_env'), \
             patch('builtins.input', return_value=''), patch.object(telegram, 'login_code', side_effect=['bad', 'old', 'r', 'new']), \
             patch.object(telegram.getpass, 'getpass', return_value='password'):
            asyncio.run(telegram.authorize())
        self.assertEqual(client.send_code_request.await_count, 2)
        self.assertEqual(client.sign_in.await_count, 4)

    def test_delivery_metadata_does_not_print_hash(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            telegram.delivery_feedback(SimpleNamespace(type=type('SentCodeTypeEmailCode', (), {})(), timeout=20, phone_code_hash='SECRET'))
        self.assertIn('email', output.getvalue())
        self.assertNotIn('SECRET', output.getvalue())

    def test_whatsapp_reset_scoped_and_lock_preserved(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(whatsapp.config, 'DATA_DIR', Path(temp)):
            root = Path(temp) / 'wa_session'
            (root / 'session').mkdir(parents=True)
            (root / 'session/token').write_text('secret')
            (Path(temp) / 'keep').write_text('keep')
            with FileLock(root / '.owner.lock', blocking=False):
                inode = (root / '.owner.lock').stat().st_ino
                self.assertTrue(whatsapp.reset_session())
                self.assertEqual((root / '.owner.lock').stat().st_ino, inode)
                with self.assertRaises(BlockingIOError):
                    with FileLock(root / '.owner.lock', blocking=False):
                        pass
            self.assertTrue((Path(temp) / 'keep').exists())

    def test_whatsapp_refuses_foreign_or_active_owner(self):
        for owner in ('foreign-504', f'{socket.gethostname()}-{os.getpid()}'):
            with tempfile.TemporaryDirectory() as temp, patch.object(whatsapp.config, 'DATA_DIR', Path(temp)):
                profile = Path(temp) / 'wa_session/session'
                profile.mkdir(parents=True)
                (profile / 'SingletonLock').symlink_to(owner)
                self.assertFalse(whatsapp.reset_session())
                self.assertTrue((profile / 'SingletonLock').is_symlink())

    def test_retired_container_requires_no_shared_running_mount(self):
        owner = {'Id': 'old', 'Config': {'Hostname': 'oldhost'}, 'State': {'Running': False},
                 'Mounts': [{'Source': '/fake/data'}]}
        active = dict(owner, Id='active', Config={'Hostname': 'active'}, State={'Running': True})
        for containers, allowed in (([owner], True), ([owner, active], False), ([], False)):
            with patch.object(Path, 'exists', return_value=False), patch.object(whatsapp.subprocess, 'run', side_effect=[
                    SimpleNamespace(stdout='old'), SimpleNamespace(stdout=json.dumps(containers))]):
                self.assertEqual(whatsapp.retired_container_owner('oldhost', Path('/fake/data/wa_session')), allowed)

    def test_telegram_cancel_disconnects_without_request(self):
        client = SimpleNamespace(connect=AsyncMock(), get_me=AsyncMock(return_value=None), disconnect=AsyncMock(), send_code_request=AsyncMock())
        with patch('telethon.TelegramClient', return_value=client), patch.object(telegram, 'api_credentials', return_value=(1, 'hash')), \
             patch.object(telegram, 'session_path', return_value=Path('/unused')), patch.object(telegram, 'secure_session'), \
             patch('builtins.input', return_value='q'):
            with self.assertRaises(KeyboardInterrupt):
                asyncio.run(telegram.authorize())
        client.disconnect.assert_awaited_once()
        client.send_code_request.assert_not_called()

    def test_launcher_selected_db_automatic_app_still_opt_in(self):
        for hostname in ('db', 'external.invalid'):
            with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PI_SECRETARY_HOME': temp}):
                directory = Path(temp) / 'config'
                directory.mkdir()
                installer.private_write(directory / 'secretary.env', 'DATABASE_URL=' + json.dumps(f'postgresql://u:p@{hostname}/db') + '\n')
                with patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                     patch.object(installer, 'confirm', return_value=False) as consent:
                    self.assertEqual(installer.launch(SimpleNamespace(command='setup')), 0)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(any(command[-1] == 'db' for command in commands), hostname == 'db')
                self.assertTrue(any('_probe' in command for command in commands))
                self.assertFalse(any('--force-recreate' in command for command in commands))
                consent.assert_called_once_with('Avviare ora app?')

    def test_microsoft_inline_before_optional_setups(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'secretary.env'
            installer.private_write(path, 'EMAIL1_PROVIDER=outlook\n')
            config = SimpleNamespace(CONFIG_FILE=path, DATABASE_URL='postgresql://u:p@external/db', set_env=Mock())
            order = []
            with patch.dict(sys.modules, {'config': config}), patch.object(installer, 'configure_email', side_effect=lambda *a, **k: order.append('email')), \
                 patch.object(installer, 'configure_preferences'), patch.object(installer, 'prompt_missing'), \
                 patch.object(installer, 'run_setup', side_effect=lambda script: (order.append(script) or SimpleNamespace(returncode=0))), \
                 patch.object(installer, 'confirm', side_effect=[False, False, False, False, True]):
                self.assertEqual(installer.wizard(), 0)
            self.assertEqual(order, ['email', 'setup_ms_auth.py'])