"""Offline tests: no Docker daemon, credentials, auth or migrations."""
import json
from pathlib import Path
import tempfile
import unittest
import os
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, unquote

import installer


class InstallerTests(unittest.TestCase):
    def fake_config(self, directory):
        path = Path(directory) / 'secretary.env'
        def save(key, value):
            values = installer.read_env(path)
            values[key] = value
            installer.private_write(path, ''.join(k + '=' + json.dumps(v) + '\n' for k, v in values.items()))
        return SimpleNamespace(CONFIG_FILE=path, get=lambda key: installer.read_env(path).get(key, ''), set_env=save)

    def test_prompt_reprompts_empty_with_default(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.fake_config(temp)
            config.set_env('LANGUAGE', '')
            with patch('builtins.input', return_value='') as prompt:
                installer.prompt_missing(config, 'LANGUAGE', 'Lingua')
                prompt.assert_called_once()
            self.assertEqual(config.get('LANGUAGE'), 'it')

    def test_custom_email_hidden_password_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.fake_config(temp)
            with patch('installer.confirm', side_effect=[True, False]), patch('builtins.input', side_effect=[
                    'custom', 'Lavoro', 'a@example.invalid', 'imap.example.invalid', 'smtp.example.invalid']), \
                    patch('installer.getpass.getpass', return_value='private') as hidden:
                installer.configure_email(config)
                hidden.assert_called_once()
            before = config.CONFIG_FILE.read_text()
            with patch('installer.confirm', return_value=False), patch('builtins.input') as prompt, \
                    patch('installer.getpass.getpass') as hidden:
                installer.configure_email(config)
                prompt.assert_not_called()
                hidden.assert_not_called()
            self.assertEqual(config.CONFIG_FILE.read_text(), before)

    def test_email_limit_and_sparse_existing(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.fake_config(temp)
            config.set_env('EMAIL50_PROVIDER', 'outlook')
            with patch('installer.confirm', return_value=False), patch('builtins.input', side_effect=['Ultimo', 'last@example.invalid']), \
                    patch('installer.getpass.getpass') as hidden:
                installer.configure_email(config)
                hidden.assert_not_called()
            self.assertEqual(config.get('EMAIL50_ADDRESS'), 'last@example.invalid')
            self.assertFalse(any(k.startswith('EMAIL51_') for k in installer.read_env(config.CONFIG_FILE)))

    def test_preferences_and_entities_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.fake_config(temp)
            with patch('builtins.input', side_effect=['Europe/Rome', 'it', 'model', '21:00', 'azienda', '123', 'descrizione']), \
                    patch('installer.confirm', side_effect=[True, False]):
                installer.configure_preferences(config)
            with patch('builtins.input') as prompt, patch('installer.confirm', return_value=False):
                installer.configure_preferences(config)
                prompt.assert_not_called()

    def test_launcher_exports_actual_identity_without_build(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PI_SECRETARY_HOME': temp}), \
                patch('installer.subprocess.run') as run:
            installer.launch(SimpleNamespace(command='status'))
            self.assertEqual(os.environ['PI_SECRETARY_UID'], str(os.getuid()))
            self.assertEqual(os.environ['PI_SECRETARY_GID'], str(os.getgid()))
            self.assertFalse(any('build' in call.args[0] for call in run.call_args_list))

    def test_entrypoint_creates_home_for_current_identity(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'HOME': temp + '/home'}), \
                patch.object(sys, 'argv', ['entrypoint.py', 'python', 'installer.py', '_wizard']), \
                patch('os.execvp') as execute:
            runpy.run_path(str(installer.ROOT / 'docker/entrypoint.py'))
            self.assertTrue(Path(temp + '/home').is_dir())
            self.assertEqual(Path(temp + '/home').stat().st_mode & 0o777, 0o700)
            execute.assert_called_once()

    def test_telegram_credentials_saved_before_setup(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.fake_config(temp)
            config.DATABASE_URL = 'postgresql://user:secret@example.invalid/db'
            def setup(*args, **kwargs):
                self.assertEqual(config.get('TELEGRAM_API_ID'), '12345')
                self.assertEqual(config.get('TELEGRAM_API_HASH'), 'hidden-hash')
                self.assertEqual(config.get('TELEGRAM_PHONE'), '+391234')
                return SimpleNamespace(returncode=0)
            with patch.dict(sys.modules, {'config': config}), \
                    patch('installer.configure_preferences'), patch('installer.configure_email'), \
                    patch('installer.confirm', side_effect=[False, True, False, False, False, True]), \
                    patch('builtins.input', side_effect=['Agent', '12345', '+391234']), \
                    patch('installer.getpass.getpass', return_value='hidden-hash'), \
                    patch('installer.subprocess.run', side_effect=setup) as run:
                self.assertEqual(installer.wizard(), 0)
                run.assert_called_once()
            self.assertNotIn('hidden-hash', (Path(temp) / 'progress.json').read_text())

    def test_local_password_is_private_and_stable(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            uri, mode = installer.provision_database(directory, '')
            self.assertEqual(mode, 'local')
            self.assertEqual(urlsplit(uri).hostname, 'db')
            self.assertEqual((directory / 'postgres.env').stat().st_mode & 0o777, 0o600)
            self.assertEqual(installer.provision_database(directory, '')[0], uri)
            self.assertGreater(len(urlsplit(uri).password), 32)

    def test_password_percent_encoded(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            password = 'a:@/ %?#'
            installer.private_write(directory / 'postgres.env', 'POSTGRES_PASSWORD=' + json.dumps(password) + '\n')
            uri, _ = installer.provision_database(directory, '')
            self.assertEqual(unquote(urlsplit(uri).password), password)

    def test_existing_external_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            uri = 'postgresql://user:secret@example.invalid/database'
            self.assertEqual(installer.provision_database(directory, uri), (uri, 'external'))
            self.assertFalse((directory / 'postgres.env').exists())

    def test_invalid_external_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                installer.provision_database(Path(temp), '', 'https://example.invalid')

    def test_empty_existing_password_not_rotated(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            installer.private_write(directory / 'postgres.env', '')
            with self.assertRaises(ValueError):
                installer.provision_database(directory, '')
            self.assertEqual((directory / 'postgres.env').read_text(), '')

    def test_progress_contains_no_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'progress.json'
            self.assertEqual(installer.progress(path), {})
            installer.private_write(path, json.dumps({'complete': False, 'database': 'local'}))
            self.assertFalse(installer.progress(path)['complete'])

    def test_consent_defaults_no(self):
        with patch('builtins.input', return_value=''):
            self.assertFalse(installer.confirm('Login?'))

    def test_image_allowlist_and_no_host_db_port(self):
        dockerfile = (installer.ROOT / 'Dockerfile').read_text()
        self.assertNotIn('COPY . ', dockerfile)
        self.assertIn('npm ci', dockerfile)
        compose = (installer.ROOT / 'compose.yaml').read_text()
        self.assertIn('127.0.0.1:8080:8080', compose)
        self.assertNotIn('5432:5432', compose)
        self.assertNotIn('latest', compose)
        self.assertIn('${PI_SECRETARY_UID:-1000}:${PI_SECRETARY_GID:-1000}', compose)
        self.assertIn('/tmp:uid=${PI_SECRETARY_UID:-1000},gid=${PI_SECRETARY_GID:-1000}', compose)


if __name__ == '__main__':
    unittest.main()