"""Offline onboarding regressions, temporary config only."""
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from secretary.setup import installer
from tests.core.test_config_portability import ConfigPortabilityTests


class LauncherRegression(unittest.TestCase):
    def test_legacy_status_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'progress.json'
            path.write_text(json.dumps({'gcal': True, 'microsoft': False, 'complete': True}))
            self.assertEqual(installer.progress(path)['gcal'], 'ready')
            self.assertEqual(installer.progress(path)['microsoft'], 'failed')
            self.assertTrue(installer.progress(path)['complete'])
        for key, bad in [('TIMEZONE', 'Not/AZone'), ('DIGEST_TIME', '24:01'),
                         ('LANGUAGE', '<script>'), ('OPENROUTER_MODEL', 'bad model')]:
            self.assertFalse(installer.valid_preference(key, bad))

    def test_external_selection_reloaded_after_wizard(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'P_SECRETARY_HOME': tmp}):
            def execute(cmd, **kwargs):
                if '_wizard' in cmd:
                    (Path(tmp) / 'config/progress.json').write_text(json.dumps({'database': 'external', 'complete': True}))
                return SimpleNamespace(returncode=0)
            with patch('secretary.setup.installer.subprocess.run', side_effect=execute) as run, patch('secretary.setup.installer.confirm', side_effect=[False]):
                self.assertEqual(installer.launch(SimpleNamespace(command='setup')), 0)
            probe = next(c.args[0] for c in run.call_args_list if '_probe' in c.args[0])
            self.assertIn(str(installer.ROOT / 'compose.external.yaml'), probe)

    def test_missing_key_prevents_database_start(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'P_SECRETARY_HOME': tmp}):
            directory = Path(tmp) / 'config'
            directory.mkdir()
            (directory / 'progress.json').write_text('{"complete":true}')
            with patch('secretary.setup.installer.subprocess.run') as run:
                with self.assertRaisesRegex(ValueError, 'OPENROUTER'):
                    installer.launch(SimpleNamespace(command='up'))
                self.assertFalse(any('up' in c.args[0] for c in run.call_args_list))

    def test_up_rebuilds_image_before_probe_and_start(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'P_SECRETARY_HOME': tmp}):
            (Path(tmp) / 'config').mkdir()
            (Path(tmp) / 'config/progress.json').write_text('{"complete":true}')
            (Path(tmp) / 'config/secretary.env').write_text('OPENROUTER_API_KEY=test-key\n')
            with patch('secretary.setup.installer.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run, \
                    patch('secretary.connectors.whatsapp_profile.repair_docker_singletons', return_value=False):
                self.assertEqual(installer.launch(SimpleNamespace(command='up')), 0)
            commands = [c.args[0] for c in run.call_args_list]
            build = next(i for i, c in enumerate(commands) if c[-2:] == ['build', 'app'])
            probe = next(i for i, c in enumerate(commands) if '_probe' in c)
            start = next(i for i, c in enumerate(commands) if '--force-recreate' in c)
            self.assertLess(build, probe)
            self.assertLess(build, start)


class SetupRegression(ConfigPortabilityTests):
    def test_personal_consent_and_whatsapp_failure(self):
        self.run_code('''
from unittest.mock import patch
from secretary.setup import telegram_account as personal, whatsapp as wa
async def authorized(): pass
with patch.object(personal.sys.stdin, 'isatty', return_value=True), patch.object(personal, 'authorize', authorized), patch('builtins.input', return_value='n'), patch.object(personal.config, 'set_env') as save:
    assert personal.main() == 2
    save.assert_not_called()
with patch.object(personal.sys.stdin, 'isatty', return_value=True), patch.object(personal, 'authorize', authorized), patch('builtins.input', return_value='s'), patch.object(personal.config, 'set_env') as save:
    assert personal.main() == 0
    save.assert_called_once_with('TELEGRAM_USER_ENABLED', 'true')
with patch.object(wa, '_setup', return_value=1):
    assert wa.main() == 1
with patch.object(wa, '_setup', side_effect=RuntimeError('private')):
    assert wa.main() == 1
''')

    def test_setup_outcomes_and_health(self):
        self.run_code('''
from unittest.mock import patch
from secretary.setup import ms_auth as ms, gcal as cal, telegram as tg
with patch.object(ms.config, 'EMAIL_ACCOUNTS', []):
    assert ms.main() == 2
with patch.object(ms.config, 'EMAIL_ACCOUNTS', [{'auth':'oauth2','name':'Fake','address':'fake@example.invalid'}]), patch.object(ms.ms_oauth, 'authorize', side_effect=RuntimeError('private')):
    assert ms.main() == 1
with patch.object(cal.gcal, '_load_service_account', return_value=None), patch.object(cal, 'hidden_json', return_value=''):
    assert cal.main() == 2
with patch('getpass.getpass', return_value=''):
    assert tg.main() == 2
from secretary.dashboard import dashboard
app = dashboard.create_app({'username':'test','password':'test'})
with patch.object(dashboard.db, '_conn', side_effect=AssertionError('No DB')):
    response = app.test_client().get('/healthz', headers={'Host':'localhost'})
    assert response.status_code == 200 and response.json == {'status':'started'}
assert app.test_client().get('/healthz', headers={'Host':'evil.invalid'}).status_code == 400
''')