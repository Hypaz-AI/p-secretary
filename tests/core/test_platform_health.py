"""Offline only: no credentials or live connector requests."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from secretary.dashboard import dashboard
from secretary.core import platform_health as health


class HealthTests(unittest.TestCase):
    def test_security_and_manual_only(self):
        client = dashboard.create_app({'username': 'test', 'password': 'test'}).test_client()
        with patch.object(health.HealthRunner, 'run', return_value={'results': []}) as run:
            self.assertEqual(client.post('/api/platform-health').status_code, 401)
            csrf = client.post('/api/login', json={'username': 'test', 'password': 'test'}).json['csrf']
            self.assertEqual(client.post('/api/platform-health').status_code, 403)
            headers = {'X-CSRF-Token': csrf}
            self.assertEqual(client.post('/api/platform-health', headers={**headers, 'Origin': 'https://evil.test'}).status_code, 403)
            self.assertEqual(client.post('/api/platform-health', headers={**headers, 'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
            self.assertEqual(client.post('/api/platform-health', headers={**headers, 'Host': 'evil.test'}).status_code, 400)
            client.get('/healthz')
            with client.get('/') as response:
                self.assertEqual(response.status_code, 200)
            run.assert_not_called()
            self.assertEqual(client.post('/api/platform-health', headers=headers).status_code, 200)
            run.return_value = None
            response = client.post('/api/platform-health', headers=headers)
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['Retry-After'], '30')

    def test_runner_sanitizes_and_cools_down(self):
        runner = health.HealthRunner()
        with patch.object(health, 'probes', return_value=[('x', 'X', Mock(side_effect=RuntimeError('secret token')))]):
            result = runner.run()
            self.assertEqual(result['results'][0]['status'], 'failed')
            self.assertNotIn('secret', str(result))
            self.assertIsNone(runner.run())
        runner.lock.acquire()
        try:
            self.assertIsNone(runner.run())
        finally:
            runner.lock.release()

    def test_deadline_blocks_overlap_even_after_cooldown(self):
        gate = threading.Event()
        runner = health.HealthRunner()
        def slow():
            gate.wait(2)
            return 'tested', 'ok'
        try:
            with patch.object(health, 'DEADLINE', .01), patch.object(health, 'probes', return_value=[('x', 'X', slow)]):
                self.assertEqual(runner.run()['results'][0]['status'], 'timeout')
                runner.next_run = 0
                self.assertIsNone(runner.run())
        finally:
            gate.set()

    def test_imap_read_only_and_microsoft_no_refresh(self):
        account = dict(imap_host='imap.gmail.com', imap_port=993, address='test@example.com', password='fake', auth='basic')
        conn = Mock()
        conn.noop.return_value = ('OK', [])
        with patch.object(health.imaplib, 'IMAP4_SSL', return_value=conn), patch('secretary.connectors.ms_oauth.get_access_token') as refresh:
            self.assertEqual(health.email_check(account)[0], 'tested')
            conn.select.assert_not_called()
            conn.uid.assert_not_called()
            conn.store.assert_not_called()
            conn.shutdown.assert_called_once()
            account['auth'] = 'oauth2'
            with patch('secretary.connectors.ms_oauth._load_tokens', return_value={}):
                self.assertEqual(health.email_check(account)[0], 'skipped')
            with patch('secretary.connectors.ms_oauth._load_tokens', return_value={'test@example.com': {'access_token': 'fake', 'expires_at': time.time()+1000}}):
                self.assertEqual(health.email_check(account)[0], 'tested')
            refresh.assert_not_called()
        account['imap_host'] = '127.0.0.1'
        with patch.object(health.imaplib, 'IMAP4_SSL') as connect:
            self.assertEqual(health.email_check(account)[0], 'skipped')
            connect.assert_not_called()

    def test_worker_observation_never_connects(self):
        wa, personal = Mock(), Mock()
        wa.proc.poll.return_value = None
        wa.ready = True
        with patch.dict(health._workers, whatsapp=wa, telegram_user=personal), patch.object(health.config, 'WHATSAPP_ENABLED', True), patch.object(health.config, 'TELEGRAM_USER_ENABLED', True):
            self.assertEqual(health.worker_check('whatsapp')[0], 'observed')
            self.assertEqual(health.worker_check('telegram_user')[0], 'observed')
            wa.start.assert_not_called()
            personal.start.assert_not_called()
            personal.client.connect.assert_not_called()
            wa.proc.poll.return_value = 1
            self.assertIn('non connesso', health.worker_check('whatsapp')[1])

    def test_fixed_provider_read_operations(self):
        with patch.object(health.config, 'TELEGRAM_BOT_TOKEN', '123:fake'), patch.object(health.config, 'OPENROUTER_API_KEY', 'fake'), patch.object(health, 'remote') as remote:
            remote.return_value = {'ok': True, 'result': {'is_bot': True}}
            self.assertEqual(health.bot()[0], 'tested')
            self.assertTrue(remote.call_args.args[0].endswith('/getMe'))
            remote.return_value = {'data': {}}
            self.assertEqual(health.openrouter()[0], 'tested')
            self.assertEqual(remote.call_args.args[0], 'https://openrouter.ai/api/v1/key')

    def test_calendar_isolated_readonly(self):
        from secretary.connectors import gcal
        with patch.object(health.config, 'GCAL_ENABLED', True), patch.object(gcal, '_load_service_account', return_value={'client_email':'fake', 'private_key':'fake'}), patch.object(gcal, 'readiness', return_value='ready'), patch.object(gcal, 'calendar_id', return_value='calendar/example'), patch.object(gcal, '_parse_rsa_private_key', return_value=(1, 1)), patch.object(gcal, '_rsa_sign_sha256', return_value=b'fake'), patch.object(health, 'remote', side_effect=[{'access_token':'fake'}, {}]) as remote:
            before = dict(gcal._sa_token_cache)
            self.assertEqual(health.calendar()[0], 'tested')
            self.assertEqual(gcal._sa_token_cache, before)
            self.assertEqual(remote.call_count, 2)
            self.assertIn('calendar%2Fexample', remote.call_args.args[0])
            import base64
            from urllib.parse import parse_qs
            jwt = parse_qs(remote.call_args_list[0].args[2].decode())['assertion'][0]
            claims = base64.urlsafe_b64decode(jwt.split('.')[1] + '==').decode()
            self.assertIn('calendar.readonly', claims)

    def test_database_select_only(self):
        from secretary.core import db
        with patch.object(health.config, 'DATABASE_URL', 'postgresql://fake'), patch.object(db.psycopg, 'connect') as connect:
            conn = connect.return_value.__enter__.return_value
            conn.execute.return_value.fetchone.return_value = (1,)
            self.assertEqual(health.database()[0], 'tested')
            conn.execute.assert_called_once_with('SELECT 1')
            self.assertIn('default_transaction_read_only=on', connect.call_args.kwargs['options'])


if __name__ == '__main__':
    unittest.main()