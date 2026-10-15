"""Offline candidate/config route tests. Never access real credentials or network."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import admin_config as admin
import config
import dashboard


class AdminConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'secretary.env'
        self.path.write_text('AGENT_NAME="Old"\nOPENROUTER_API_KEY="OLD-SECRET"\nTELEGRAM_CHAT_ID="123"\nTELEGRAM_BOT_TOKEN="123:old"\n')
        for name, value in [('CONFIG_FILE', self.path), ('DEPLOYMENT_ENV_KEYS', frozenset())]:
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = dashboard.create_app({'username': 'test', 'password': 'pass'}).test_client()
        self.headers = {'X-CSRF-Token': self.client.post('/api/login', json={'username': 'test', 'password': 'pass'}).json['csrf']}
        # Fail closed if any test accidentally reaches network.
        self.network = patch.object(admin, 'remote_json', side_effect=AssertionError('network forbidden')).start()
        self.addCleanup(patch.stopall)

    def payload(self, changes):
        return {'revision': self.client.get('/api/configuration').json['revision'], 'changes': changes}

    def post(self, changes):
        return self.client.post('/api/configuration', json=self.payload(changes), headers=self.headers)

    def test_auth_csrf_origin_host(self):
        client = dashboard.create_app({'username': 'test', 'password': 'pass'}).test_client()
        self.assertEqual(client.get('/api/configuration').status_code, 401)
        self.assertEqual(client.post('/api/configuration', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/configuration', json={}).status_code, 403)
        for headers in ({'Origin': 'https://evil.test'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.client.post('/api/configuration', json={}, headers={**self.headers, **headers}).status_code, 403)
        self.assertEqual(self.client.get('/api/configuration', headers={'Host': 'evil.test'}).status_code, 400)

    def test_schema_redaction_and_all_numbered_fields(self):
        response = self.client.get('/api/configuration')
        text = response.get_data(as_text=True)
        self.assertNotIn('OLD-SECRET', text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        fields = {f['key']: f for f in response.json['fields']}
        self.assertIsNone(fields['OPENROUTER_API_KEY']['value'])
        self.assertTrue(fields['OPENROUTER_API_KEY']['configured'])
        self.assertIn('EMAIL50_CLIENT_ID', fields)
        self.assertIn('INVOICE_ENTITY4_DESC', fields)

    def test_atomic_success_no_live_mutation_and_conflict(self):
        payload = self.payload({'AGENT_NAME': 'New', 'LANGUAGE': 'en'})
        before = dict(os.environ)
        live = config.AGENT_NAME
        response = self.client.post('/api/configuration', json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['applied_live'])
        self.assertTrue(response.json['restart_required'])
        self.assertEqual(config.AGENT_NAME, live)
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(admin.read_env(self.path)['AGENT_NAME'], 'New')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        saved = self.path.read_bytes()
        self.assertEqual(self.client.post('/api/configuration', json=payload, headers=self.headers).status_code, 409)
        self.assertEqual(saved, self.path.read_bytes())

    def test_failed_batch_keeps_file_environment_and_redacts_errors(self):
        before = self.path.read_bytes()
        response = self.post({'AGENT_NAME': 'New', 'OPENROUTER_API_KEY': 'CANDIDATE-SECRET'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(before, self.path.read_bytes())
        self.assertNotIn('CANDIDATE', response.get_data(as_text=True))
        self.assertNotIn('network forbidden', response.get_data(as_text=True))

    def test_allowlist_immutable_and_interactive(self):
        before = self.path.read_bytes()
        for key in ('PYTHONPATH', 'LD_PRELOAD', 'DATA_DIR', 'DASHBOARD_PORT', 'TELEGRAM_CHAT_ID',
                    'WHATSAPP_ENABLED', 'TELEGRAM_USER_ENABLED', 'TELEGRAM_API_HASH', 'EMAIL51_PASSWORD'):
            self.assertEqual(self.post({key: 'evil'}).status_code, 400, key)
        self.assertEqual(before, self.path.read_bytes())

    def test_injected_environment_never_overridden(self):
        with patch.object(config, 'DEPLOYMENT_ENV_KEYS', frozenset({'AGENT_NAME', 'OPENROUTER_API_KEY'})), \
             patch.dict(os.environ, {'AGENT_NAME': 'Docker', 'OPENROUTER_API_KEY': 'INJECTED-SECRET'}):
            fields = {f['key']: f for f in self.client.get('/api/configuration').json['fields']}
            self.assertEqual(fields['AGENT_NAME']['value'], 'Docker')
            self.assertTrue(fields['AGENT_NAME']['readonly'])
            self.assertIsNone(fields['OPENROUTER_API_KEY']['value'])
            self.assertEqual(self.post({'AGENT_NAME': 'Ignored'}).status_code, 400)

    def test_values_are_revalidated_and_control_chars_rejected(self):
        for changes in ({'TIMEZONE': 'invalid'}, {'DIGEST_TIME': '30:00'}, {'POLL_INTERVAL': '0'},
                        {'LANGUAGE': 'en\nLD_PRELOAD=x'}, {'INVOICE_ENTITY1_NAME': '../escape'},
                        {'GCAL_ENABLED': 'yes'}, {'EMAIL1_SMTP_PORT': 'abc'}, {'OPENROUTER_API_KEY': ''}):
            self.assertEqual(self.post(changes).status_code, 400)
        self.network.assert_not_called()

    def test_bot_only_getme_preserves_owner(self):
        self.network.side_effect = None
        self.network.return_value = {'ok': True, 'result': {'is_bot': True}}
        self.assertEqual(self.post({'TELEGRAM_BOT_TOKEN': '123:abc_DEF'}).status_code, 200)
        self.network.assert_called_once_with('https://api.telegram.org/bot123:abc_DEF/getMe')
        self.assertEqual(admin.read_env(self.path)['TELEGRAM_CHAT_ID'], '123')

    def test_ai_safe_endpoint(self):
        self.network.side_effect = None
        self.network.return_value = {'data': {}}
        self.assertEqual(self.post({'OPENROUTER_API_KEY': 'NEW-SECRET'}).status_code, 200)
        self.network.assert_called_once_with('https://openrouter.ai/api/v1/key', {'Authorization': 'Bearer NEW-SECRET'})

    def test_bot_identity_change_requires_pairing(self):
        self.network.side_effect = None
        self.network.return_value = {'ok': True, 'result': {'is_bot': True}}
        before = self.path.read_bytes()
        self.assertEqual(self.post({'TELEGRAM_BOT_TOKEN': '999:new'}).status_code, 400)
        self.assertEqual(before, self.path.read_bytes())

    def test_calendar_candidate_isolated_no_cache_or_live_config_change(self):
        from connectors import gcal
        candidate = {'client_email': 'test@example.invalid', 'private_key': 'PRIVATE-SECRET'}
        cache = dict(gcal._sa_token_cache)
        self.network.side_effect = [{'access_token': 'CANDIDATE-TOKEN'}, {'id': 'shared'}, {'accessRole': 'writer'}]
        with patch.object(gcal, 'validate_service_account', side_effect=lambda value: value), \
             patch.object(gcal, '_parse_rsa_private_key', return_value=(123, 456)), \
             patch.object(gcal, '_rsa_sign_sha256', return_value=b'signature'), \
             patch.object(gcal, '_service_account_token') as live_token:
            self.assertEqual(admin.verify_calendar({'GCAL_CALENDAR_ID': 'shared'}, json.dumps(candidate)), candidate)
            live_token.assert_not_called()
        self.assertEqual(cache, gcal._sa_token_cache)
        self.assertEqual(len(self.network.call_args_list), 3)
        self.assertTrue(self.network.call_args_list[0].args[0].startswith('https://oauth2.googleapis.com/'))
        self.assertTrue(self.network.call_args_list[2].args[0].endswith('/calendarList/shared'))

    def test_email_batch_failure_rolls_back_other_successful_checks(self):
        before = self.path.read_bytes()
        with patch.object(admin, 'verify_database') as db_check, \
             patch.object(admin, 'verify_email', side_effect=ValueError('private')):
            self.assertEqual(self.post({'DATABASE_URL': 'candidate', 'EMAIL1_PASSWORD': 'secret', 'AGENT_NAME': 'New'}).status_code, 400)
            db_check.assert_called_once_with('candidate')
        self.assertEqual(before, self.path.read_bytes())

    def test_database_readonly_real_candidate_select_contract(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchone.return_value = (1,)
        with patch('psycopg.connect', return_value=conn) as connect, patch.object(admin, 'public_host', return_value='8.8.8.8'):
            admin.verify_database('postgresql://user:pass@example.com/test')
        self.assertIn('default_transaction_read_only=on', connect.call_args.kwargs['options'])
        self.assertEqual(connect.call_args.kwargs['hostaddr'], '8.8.8.8')
        self.assertEqual(connect.call_args.kwargs['sslmode'], 'verify-full')
        conn.execute.assert_called_once_with('SELECT 1')

    def test_ssrf_reject_private_and_db_query(self):
        for host in ('127.0.0.1', '169.254.169.254', '10.0.0.1', '::1'):
            with patch('socket.getaddrinfo', return_value=[(None, None, None, None, (host, 5432))]):
                with self.assertRaises(ValueError):
                    admin.public_host(host, 5432)
        with self.assertRaises(ValueError):
            admin.verify_database('postgresql://user@public.example/db?host=localhost')
        with self.assertRaises(ValueError):
            admin.verify_email({'EMAIL1_ADDRESS': 'a@example.com', 'EMAIL1_PASSWORD': 'secret',
                                'EMAIL1_IMAP_HOST': 'localhost', 'EMAIL1_SMTP_HOST': 'localhost'}, 'EMAIL1_')

    def test_email_checks_both_providers_without_send_or_select(self):
        imap, smtp = MagicMock(), MagicMock()
        with patch('imaplib.IMAP4_SSL', return_value=imap), patch('smtplib.SMTP', return_value=smtp):
            admin.verify_email({'EMAIL1_PROVIDER': 'gmail', 'EMAIL1_ADDRESS': 'a@gmail.com', 'EMAIL1_PASSWORD': 'secret'}, 'EMAIL1_')
        imap.__enter__.return_value.login.assert_called_once_with('a@gmail.com', 'secret')
        imap.__enter__.return_value.select.assert_not_called()
        smtp.__enter__.return_value.login.assert_called_once_with('a@gmail.com', 'secret')
        smtp.__enter__.return_value.sendmail.assert_not_called()
        with self.assertRaises(ValueError):
            admin.verify_email({'EMAIL1_PROVIDER': 'm365'}, 'EMAIL1_')

    def test_calendar_private_publish_and_batch_failure(self):
        candidate = {'private_key': 'PRIVATE-SECRET', 'client_email': 'test@example.invalid'}
        changes = {'GCAL_SERVICE_ACCOUNT_JSON': json.dumps(candidate), 'GCAL_CALENDAR_ID': 'shared', 'AGENT_NAME': 'New'}
        with patch.object(admin, 'verify_calendar', return_value=candidate):
            self.assertEqual(self.post(changes).status_code, 200)
        keypath = Path(admin.read_env(self.path)['GCAL_SERVICE_ACCOUNT_FILE'])
        self.assertEqual(keypath.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('PRIVATE-SECRET', self.client.get('/api/configuration').get_data(as_text=True))
        before = self.path.read_bytes()
        with patch.object(admin, 'verify_calendar', side_effect=ValueError('PRIVATE-SECRET')):
            self.assertEqual(self.post(changes).status_code, 400)
        self.assertEqual(before, self.path.read_bytes())

    def test_failed_atomic_publish_removes_candidate_and_keeps_old_file(self):
        original = admin.atomic_write
        before = self.path.read_bytes()
        def failing(path, text):
            if path == self.path:
                raise OSError('PRIVATE-SECRET')
            original(path, text)
        with patch.object(admin, 'verify_calendar', return_value={'private_key': 'PRIVATE-SECRET'}), \
             patch.object(admin, 'atomic_write', side_effect=failing):
            self.assertEqual(self.post({'GCAL_SERVICE_ACCOUNT_JSON': '{}', 'GCAL_CALENDAR_ID': 'shared'}).status_code, 400)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(list(self.path.parent.glob('.calendar-keys/*.json')), [])

    def test_external_writer_conflict_after_validation(self):
        def writer(*args):
            self.path.write_text('AGENT_NAME="Other writer"\n')
        with patch.object(admin, 'validate', side_effect=writer):
            self.assertEqual(self.post({'AGENT_NAME': 'New'}).status_code, 409)
        self.assertEqual(admin.read_env(self.path)['AGENT_NAME'], 'Other writer')

    def test_malformed_and_bounded_requests(self):
        for value in ([], {}, {'revision': 'x', 'changes': []}):
            self.assertEqual(self.client.post('/api/configuration', json=value, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post('/api/configuration', data='x' * 200000,
                         content_type='application/json', headers=self.headers).status_code, 400)


if __name__ == '__main__':
    unittest.main()