"""Offline HTTP security and flow tests: no live integrations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from secretary.dashboard import dashboard
from secretary.core import flows


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.client = dashboard.create_app({'username': 'test', 'password': 'test-only'}).test_client()

    def login(self):
        return self.client.post('/api/login', json={'username': 'test', 'password': 'test-only'}).json['csrf']

    def test_protected_api_and_assets(self):
        self.assertEqual(self.client.get('/api/dashboard').status_code, 401)
        for path in ('/', '/app.js', '/style.css'):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/', headers={'Host': 'evil.test'}).status_code, 400)

    def test_csrf_origin_whitelist_and_toggle(self):
        csrf = self.login()
        self.assertEqual(self.client.post('/api/flows/email', json={'enabled': False}).status_code, 403)
        headers = {'X-CSRF-Token': csrf}
        self.assertEqual(self.client.post('/api/flows/email', json={'enabled': False},
                         headers={**headers, 'Origin': 'https://evil.test'}).status_code, 403)
        with patch.object(flows, 'set_enabled') as change:
            for flow, value in [('arbitrary', True), ('email', 'false')]:
                self.assertEqual(self.client.post('/api/flows/'+flow, json={'enabled': value}, headers=headers).status_code, 400)
            change.assert_not_called()
            self.assertEqual(self.client.post('/api/flows/email', json={'enabled': False}, headers=headers).status_code, 200)
            change.assert_called_once_with('email', False)
        self.assertEqual(self.client.post('/api/logout', json={}, headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/api/session').status_code, 401)

    def test_login_cookie_and_rate_limit(self):
        response = self.client.post('/api/login', json={'username':'test','password':'test-only'})
        self.assertIn('HttpOnly', response.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', response.headers['Set-Cookie'])
        for _ in range(9):
            self.client.post('/api/login', json={})
        self.assertEqual(self.client.post('/api/login', json={}).status_code, 429)

    def test_credential_mode_and_stability(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'private'/'auth.json'
            first = dashboard.credential(path)
            self.assertEqual(first, dashboard.credential(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_paused_flow_no_work(self):
        with patch.object(flows, 'enabled', return_value=False), patch.object(flows, 'mark') as mark:
            callback = unittest.mock.Mock()
            flows.run('email', callback)
            callback.assert_not_called()
            mark.assert_called_with('email', 'paused')

    def test_database_error_sanitized(self):
        self.login()
        with patch.object(dashboard, 'snapshot', side_effect=RuntimeError('private database secret')):
            result = self.client.get('/api/dashboard')
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('secret', result.get_data(as_text=True))

    def test_malformed_json_and_toggle_database_error(self):
        self.assertEqual(self.client.post('/api/login', json=['invalid']).status_code, 400)
        csrf = self.login()
        headers = {'X-CSRF-Token': csrf}
        self.assertEqual(self.client.post('/api/flows/email', json=[True], headers=headers).status_code, 400)
        with patch.object(flows, 'set_enabled', side_effect=RuntimeError('private secret')):
            result = self.client.post('/api/flows/email', json={'enabled': False}, headers=headers)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('secret', result.get_data(as_text=True))

    def test_runtime_worker_enable_disable(self):
        from secretary import main
        wa, personal = unittest.mock.Mock(), unittest.mock.Mock()
        wa.proc = None
        wa.ready = False
        personal.thread = None
        with patch.object(main.config, 'WHATSAPP_ENABLED', True), \
             patch.object(main.config, 'TELEGRAM_USER_ENABLED', True), \
             patch.object(flows, 'enabled', return_value=False):
            main.sync_workers(wa, personal)
            wa.stop.assert_called_once()
            personal.stop.assert_called_once()
            wa.start.assert_not_called()
        with patch.object(main.config, 'WHATSAPP_ENABLED', True), \
             patch.object(main.config, 'TELEGRAM_USER_ENABLED', True), \
             patch.object(flows, 'enabled', return_value=True):
            main.sync_workers(wa, personal)
            wa.start.assert_called_once()
            personal.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()