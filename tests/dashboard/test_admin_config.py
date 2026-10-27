"""Offline candidate/config route tests. Never access real credentials or network."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

from secretary.dashboard import admin_config as admin
from secretary.core import config
from secretary.dashboard import dashboard


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
        patch.object(admin, '_restart_at', None).start()
        patch.object(admin, 'automatic_restart', return_value=False).start()

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

    def test_metadata_covers_every_field_and_dynamic_group(self):
        self.assertFalse(self.post({'LANGUAGE': 'it'}).json['restart_scheduled'])
        fields = {f['key']: f for f in self.client.get('/api/configuration').json['fields']}
        for key, field in fields.items():
            self.assertTrue(field['description'], key)
            self.assertIn(field['type'], ('text', 'password', 'textarea', 'select', 'number', 'time', 'email'), key)
            if field['secret']:
                self.assertIn(field['type'], ('password', 'textarea'))
                self.assertIsNone(field['value'])
        self.assertNotIn('NOTIFY_PRIORITY_THRESHOLD', fields)  # decide l'agente, con tetto giornaliero
        self.assertEqual((fields['AGENT_NOTIFY_MAX_PER_DAY']['min'], fields['AGENT_NOTIFY_MAX_PER_DAY']['max']), (0, 100))
        for key in ('GCAL_ENABLED', 'WHATSAPP_ENABLED', 'TELEGRAM_USER_ENABLED', 'DASHBOARD_COOKIE_SECURE'):
            self.assertEqual(fields[key]['options'], ['true', 'false'])
        self.assertEqual(fields['DIGEST_TIME']['type'], 'time')
        for i in range(1, 51):
            prefix = f'EMAIL{i}_'
            self.assertEqual(fields[prefix + 'PROVIDER']['options'], list(config._EMAIL_PRESETS))
            self.assertEqual(fields[prefix + 'AUTH']['options'], ['basic', 'oauth2'])
            self.assertEqual(fields[prefix + 'ADDRESS']['type'], 'email')
            for suffix in ('IMAP_PORT', 'SMTP_PORT'):
                self.assertEqual(fields[prefix + suffix]['type'], 'number')
                self.assertEqual(fields[prefix + suffix]['min'], 1)
                self.assertEqual(fields[prefix + suffix]['max'], 65535)
        for i in range(1, 5):
            self.assertEqual(fields[f'INVOICE_ENTITY{i}_VAT']['type'], 'text')
        for key in ('POLL_INTERVAL', 'GCAL_EVENT_DURATION', 'TELEGRAM_USER_DIALOG_LIMIT',
                    'TELEGRAM_USER_MESSAGE_LIMIT', 'TELEGRAM_USER_RECOVERY_INTERVAL'):
            self.assertEqual(fields[key]['type'], 'number')
            self.assertEqual(fields[key]['step'], 1)

    def test_docker_save_schedules_restart_after_commit_and_delay(self):
        with patch.object(admin, 'automatic_restart', return_value=True), \
             patch.object(admin.time, 'monotonic', return_value=100) as clock:
            response = self.post({'AGENT_NAME': 'Restarted'})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['restart_scheduled'])
            self.assertEqual(admin.read_env(self.path)['AGENT_NAME'], 'Restarted')
            self.assertFalse(admin.restart_requested())
            clock.return_value = 102
            self.assertTrue(admin.restart_requested())

    def test_failed_save_never_schedules_restart(self):
        with patch.object(admin, 'automatic_restart', return_value=True):
            self.assertEqual(self.post({'POLL_INTERVAL': '0'}).status_code, 400)
            self.assertIsNone(admin._restart_at)
            with patch.object(admin, 'atomic_write', side_effect=OSError('disk full')):
                self.assertEqual(self.post({'AGENT_NAME': 'New'}).status_code, 400)
            self.assertIsNone(admin._restart_at)

    def test_enum_validation_precedes_network_and_keeps_file(self):
        before = self.path.read_bytes()
        with patch.object(admin, 'verify_email') as verify:
            for key in ('EMAIL1_PROVIDER', 'EMAIL50_AUTH'):
                self.assertEqual(self.post({key: 'unsupported'}).status_code, 400)
            verify.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        for value in ('-1', '101', 'x'):
            self.assertEqual(self.post({'AGENT_NOTIFY_MAX_PER_DAY': value}).status_code, 400)
        self.assertEqual(self.post({'AGENT_NOTIFY_MAX_PER_DAY': '3'}).status_code, 200)
        with patch.object(admin, 'verify_email'):
            self.assertEqual(self.post({'EMAIL50_PROVIDER': 'm365', 'EMAIL50_AUTH': 'oauth2'}).status_code, 200)

    def test_failed_batch_keeps_file_environment_and_redacts_errors(self):
        before = self.path.read_bytes()
        response = self.post({'AGENT_NAME': 'New', 'OPENROUTER_API_KEY': 'CANDIDATE-SECRET'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(before, self.path.read_bytes())
        self.assertNotIn('CANDIDATE', response.get_data(as_text=True))
        self.assertNotIn('network forbidden', response.get_data(as_text=True))

    def test_notion_disabled_by_default_and_secret_redacted(self):
        fields = {f['key']: f for f in self.client.get('/api/configuration').json['fields']}
        self.assertEqual(fields['NOTION_ENABLED']['group'], 'Notion (sola lettura)')
        self.assertEqual(fields['NOTION_ENABLED']['options'], ['true', 'false'])
        self.assertEqual(fields['NOTION_API_KEY']['type'], 'password')
        self.assertTrue(fields['NOTION_API_KEY']['secret'])
        self.assertEqual(fields['NOTION_AGENT_ID']['type'], 'text')

    def test_notion_disable_needs_no_network(self):
        self.assertEqual(self.post({'NOTION_ENABLED': 'false'}).status_code, 200)
        self.network.assert_not_called()

    def test_notion_enable_verifies_active_custom_agent(self):
        agent = '1f0c7c06-781b-4987-9986-5c8dd3028013'
        self.network.side_effect = None
        self.network.return_value = {'results': [{'id': agent, 'agent_type': 'custom_agent', 'status': 'active'}]}
        response = self.post({'NOTION_ENABLED': 'true', 'NOTION_API_KEY': 'NOTION-SECRET', 'NOTION_AGENT_ID': agent})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('NOTION-SECRET', response.get_data(as_text=True))
        url, headers, body = self.network.call_args.args[0], self.network.call_args.args[1], self.network.call_args.args[2]
        self.assertEqual(url, 'https://api.notion.com/v1/agents/query')
        self.assertEqual(headers['Authorization'], 'Bearer NOTION-SECRET')
        self.assertEqual(headers['Notion-Version'], '2026-03-11')
        self.assertIn(b'custom_agent', body)
        saved = admin.read_env(self.path)
        self.assertEqual((saved['NOTION_ENABLED'], saved['NOTION_AGENT_ID']), ('true', agent))

    def test_notion_unknown_agent_or_rejected_token_keeps_file(self):
        agent = '1f0c7c06-781b-4987-9986-5c8dd3028013'
        before = self.path.read_bytes()
        self.network.side_effect = None
        self.network.return_value = {'results': [{'id': '00000000-0000-4000-8000-000000000000', 'agent_type': 'custom_agent', 'status': 'active'}]}
        self.assertEqual(self.post({'NOTION_ENABLED': 'true', 'NOTION_API_KEY': 'NOTION-SECRET', 'NOTION_AGENT_ID': agent}).status_code, 400)
        self.network.side_effect = HTTPError('https://api.notion.com/v1/agents/query', 401, 'Unauthorized', {}, None)
        response = self.post({'NOTION_ENABLED': 'true', 'NOTION_API_KEY': 'NOTION-SECRET', 'NOTION_AGENT_ID': agent})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('NOTION-SECRET', response.get_data(as_text=True))
        self.assertEqual(before, self.path.read_bytes())

    def test_notion_enable_requires_token_and_valid_agent_id_before_network(self):
        self.network.side_effect = None
        self.assertEqual(self.post({'NOTION_ENABLED': 'true', 'NOTION_AGENT_ID': '1f0c7c06-781b-4987-9986-5c8dd3028013'}).status_code, 400)
        self.assertEqual(self.post({'NOTION_ENABLED': 'true', 'NOTION_API_KEY': 'NOTION-SECRET', 'NOTION_AGENT_ID': 'not-a-uuid'}).status_code, 400)
        self.assertEqual(self.post({'NOTION_ENABLED': 'maybe'}).status_code, 400)
        self.network.assert_not_called()

    def test_notion_fields_are_saved_with_restart_and_no_live_mutation(self):
        self.network.side_effect = None
        self.network.return_value = {'results': [{'id': '1f0c7c06-781b-4987-9986-5c8dd3028013', 'agent_type': 'custom_agent', 'status': 'active'}]}
        with patch.object(admin, 'automatic_restart', return_value=True):
            response = self.post({'NOTION_ENABLED': 'true', 'NOTION_API_KEY': 'NOTION-SECRET', 'NOTION_AGENT_ID': '1f0c7c06-781b-4987-9986-5c8dd3028013'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['restart_scheduled'])
        self.assertFalse(getattr(config, 'NOTION_ENABLED', False))

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
        from secretary.connectors import gcal
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