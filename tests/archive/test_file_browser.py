"""Offline file API tests: temporary content and mocked DB only."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import DEFAULT, MagicMock, patch

from secretary.dashboard import dashboard
from secretary.archive import file_browser as browser


class FileBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.data, self.invoices = self.base / 'data', self.base / 'invoices'
        self.data.mkdir(); self.invoices.mkdir()
        for name in browser.CONTENT_DIRS:
            (self.data / name).mkdir()
        for key, value in [('DATA_DIR', self.data), ('INVOICE_DIR', self.invoices)]:
            p = patch.object(browser.config, key, value); p.start(); self.addCleanup(p.stop)
        self.client = dashboard.create_app({'username':'test', 'password':'offline'}).test_client()
        self.csrf = self.client.post('/api/login', json={'username':'test', 'password':'offline'}).json['csrf']
        self.headers = {'X-CSRF-Token':self.csrf}

    def db(self, paths=()):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [(p,) for p in paths]
        # Come PostgreSQL in autocommit: LOCK TABLE fuori da una transazione e' un errore.
        state = {'open': False}

        def transaction():
            manager = MagicMock()
            manager.__enter__.side_effect = lambda: state.update(open=True)
            manager.__exit__.side_effect = lambda *args: state.update(open=False) or False
            return manager

        def execute(sql, *args):
            if sql.lstrip().upper().startswith('LOCK TABLE') and not state['open']:
                raise RuntimeError('LOCK TABLE can only be used in transaction blocks')
            return DEFAULT
        conn.transaction.side_effect = transaction
        conn.execute.side_effect = execute
        context = MagicMock(); context.__enter__.return_value = conn
        return patch.object(browser.db, '_conn', return_value=context)

    def test_auth_csrf_origin(self):
        other = dashboard.create_app({'username':'test', 'password':'offline'}).test_client()
        self.assertEqual(other.get('/api/files').status_code, 401)
        self.assertEqual(self.client.post('/api/files/upload?name=x', data=b'x').status_code, 403)
        self.assertEqual(self.client.delete('/api/files?path=uploads/x', headers={**self.headers, 'Origin':'https://evil.test'}).status_code, 403)

    def test_allowlist_and_secrets(self):
        (self.data / 'state.json').write_text('not exposed')
        (self.data / 'uploads' / 'ms_tokens.json').write_text('not exposed')
        names = {e['name'] for e in self.client.get('/api/files').json['entries']}
        self.assertEqual(names, browser.CONTENT_DIRS)
        self.assertEqual(self.client.get('/api/files?path=uploads').json['entries'], [])
        for path in ['../invoices', '/etc', 'uploads//x', 'wa_session', 'uploads/ms_tokens.json', 'uploads/.env']:
            response = self.client.get('/api/files/download', query_string={'path':path})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(str(self.base), response.get_data(as_text=True))

    def test_upload_download_no_overwrite_and_limit(self):
        url = '/api/files/upload?name=report.pdf'
        self.assertEqual(self.client.post(url, data=b'hello', headers=self.headers).status_code, 201)
        self.assertEqual(self.client.post(url, data=b'other', headers=self.headers).status_code, 409)
        with self.client.get('/api/files/download?path=uploads/report.pdf') as response:
            self.assertEqual(response.data, b'hello')
            self.assertIn('attachment', response.headers['Content-Disposition'])
        with self.assertRaises(browser.FileError) as error:
            browser.upload('large.bin', io.BytesIO(b'123456'), limit=5)
        self.assertEqual(error.exception.status, 413)
        self.assertFalse((self.data / 'uploads' / 'large.bin').exists())
        self.assertEqual(self.client.post('/api/files/upload?name=../x', data=b'x', headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post('/api/files/upload?name=big.bin', data=b'x'*8192, headers=self.headers).status_code, 201)
        with patch.object(browser, 'MAX_UPLOAD', 5):
            self.assertEqual(self.client.post('/api/files/upload?name=too-big.bin', data=b'123456', headers=self.headers).status_code, 413)
        self.assertFalse((self.data / 'uploads' / 'too-big.bin').exists())

    def test_symlink_and_hardlink(self):
        outside = self.base / 'outside'; outside.write_text('private')
        (self.data / 'uploads' / 'link').symlink_to(outside)
        (self.invoices / 'escape').symlink_to(self.base, target_is_directory=True)
        os.link(outside, self.data / 'uploads' / 'hard')
        self.assertEqual(self.client.get('/api/files?path=uploads').json['entries'], [])
        for root, path in [('data','uploads/link'), ('data','uploads/hard'), ('invoices','escape/outside')]:
            self.assertEqual(self.client.get('/api/files/download', query_string={'root':root,'path':path}).status_code, 400)

    def test_delete_reference_db_unavailable_directory(self):
        file = self.invoices / 'invoice.pdf'; file.write_bytes(b'pdf')
        url = '/api/files?root=invoices&path=invoice.pdf'
        for table in ['attachments', 'invoices', 'monthly_zips']:
            with self.subTest(table=table), self.db([str(file)]):
                self.assertEqual(self.client.delete(url, headers=self.headers).status_code, 409)
                self.assertTrue(file.exists())
        with patch.object(browser.db, '_conn', side_effect=RuntimeError('private path')):
            response = self.client.delete(url, headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private path', response.get_data(as_text=True))
            self.assertTrue(file.exists())
        with self.db():
            self.assertEqual(self.client.delete(url, headers=self.headers).status_code, 200)
        self.assertFalse(file.exists())
        self.assertEqual(self.client.delete('/api/files?path=uploads', headers=self.headers).status_code, 400)

    def test_custom_credential_hidden(self):
        file = self.data / 'uploads' / 'innocent.json'; file.write_text('private')
        original = browser.config.get
        with patch.object(browser.config, 'get', side_effect=lambda key, default='': str(file) if key == 'DASHBOARD_CREDENTIAL_FILE' else original(key, default)):
            self.assertEqual(self.client.get('/api/files?path=uploads').json['entries'], [])
            self.assertEqual(self.client.get('/api/files/download?path=uploads/innocent.json').status_code, 400)

    def test_invoice_navigation_and_symlink_root(self):
        (self.invoices / 'company').mkdir()
        (self.invoices / 'company' / 'receipt.xml').write_text('content')
        response = self.client.get('/api/files?root=invoices&path=company')
        self.assertEqual(response.json['entries'][0]['name'], 'receipt.xml')
        link = self.base / 'rootlink'; link.symlink_to(self.invoices, target_is_directory=True)
        with patch.object(browser.config, 'INVOICE_DIR', link):
            self.assertEqual(self.client.get('/api/files?root=invoices').status_code, 400)

    def test_credentials_config_and_legacy_fallback(self):
        legacy = self.base / 'legacy' / 'dashboard.json'
        configured = self.base / 'custom' / 'dashboard.json'
        with patch.object(dashboard, 'CREDENTIAL_FILE', legacy), patch.object(dashboard.config, 'get', return_value=str(configured)):
            first = dashboard.credential()
            self.assertTrue(configured.exists())
            self.assertFalse(legacy.exists())
            self.assertEqual(dashboard.credential(), first)
            configured.unlink()
            old = dashboard.credential(legacy)
            self.assertEqual(dashboard.credential(), old)
            self.assertFalse(configured.exists())


if __name__ == '__main__':
    unittest.main()