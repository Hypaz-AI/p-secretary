"""Dashboard contracts: offline HTTP/mocks, optional isolated _test SQL only.

No deployment credentials, provider calls, workers or production migrations.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

import admin_config
import ai_budget
import config
import dashboard
import execution
import jobs
import secretary


PAYLOAD = dict(action='send_message', channel='email', recipient='test@example.invalid',
               text='Bozza da approvare, non inviata.', documents=[])
ACCOUNT = dict(address='owner@example.test', smtp_host='smtp.example.test', smtp_port=587,
               auth='basic', password='PRIVATE', provider='custom')


class SecretaryDashboardTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(dashboard.config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch.object(dashboard.config, 'EMAIL_ACCOUNTS', [deepcopy(ACCOUNT)]))
        self.connection = MagicMock()
        self.enterContext(patch.object(dashboard.db, '_conn', return_value=self.connection))
        self.conn = self.connection.__enter__.return_value
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('No network')))
        self.client = dashboard.create_app({'username': 'owner', 'password': 'offline'}).test_client()
        self.csrf = self.client.post('/api/login', json={'username': 'owner', 'password': 'offline'}).json['csrf']
        self.headers = {'X-CSRF-Token': self.csrf}

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def test_every_new_api_requires_authentication_and_same_origin(self):
        anonymous = dashboard.create_app({'username': 'owner', 'password': 'offline'}).test_client()
        for path in ('/api/secretary', '/api/secretary/support', '/api/secretary/runtime', '/api/knowledge',
                     '/api/secretary/execution', '/api/secretary/budget', '/api/secretary/emails/1',
                     '/api/knowledge/search?q=contratto', '/api/knowledge/open/message/1',
                     '/api/data-policies', '/api/capabilities'):
            with self.subTest(path=path):
                self.assertEqual(anonymous.get(path).status_code, 401)
                self.assertEqual(self.client.get(path, headers={'Host': 'evil.invalid'}).status_code, 400)
        for path in ('/api/data-policies', '/api/secretary/responsibilities/1', '/api/secretary/approvals/1',
                 '/api/secretary/emails', '/api/secretary/emails/1/dispatch'):
            self.assertEqual(anonymous.post(path, json={}).status_code, 401)
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
            for extra in ({'Origin': 'https://evil.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
                self.assertEqual(self.client.post(path, json={}, headers={**self.headers, **extra}).status_code, 403)
        self.connection.assert_not_called()

    def test_snapshot_filters_private_plans_and_audit_payload(self):
        raw = dict(practices=[dict(id=1, objective='Riparazione', state='active', version=2,
                                  private_config='PRIVATE')], plans=[dict(payload='PRIVATE')],
                   decisions=[dict(id=1, event='receipt', data={'prompt': 'PRIVATE'})],
                   receipts=[dict(receipt='Nessuna azione esterna', request_hash='PRIVATE', result='PRIVATE')],
                   truncated={'practices': True})
        with patch.object(secretary, 'snapshot', return_value=raw):
            response = self.client.get('/api/secretary')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        self.assertNotIn('plans', response.json)
        self.assertTrue(response.json['truncated']['practices'])
        self.assertFalse(response.json['external_execution'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])

    def test_snapshot_collection_bound(self):
        with patch.object(secretary, 'snapshot', return_value={'practices': [{'id': n} for n in range(70)]}):
            response = self.client.get('/api/secretary')
        self.assertEqual(len(response.json['practices']), 24)

    def test_optional_service_missing_and_failure_sanitized(self):
        for error, expected in ((dashboard.Unsupported('PRIVATE'), 501), (RuntimeError('PRIVATE'), 503)):
            with patch.object(dashboard, '_service', side_effect=error):
                response = self.client.get('/api/knowledge')
            self.assertEqual(response.status_code, expected)
            self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        with patch.object(dashboard.importlib, 'import_module', side_effect=ModuleNotFoundError(name='knowledge')):
            with self.assertRaises(dashboard.Unsupported):
                dashboard._service('knowledge')
        with patch.object(dashboard.importlib, 'import_module', side_effect=ModuleNotFoundError(name='dependency')):
            with self.assertRaises(ModuleNotFoundError):
                dashboard._service('knowledge')

    def test_knowledge_snapshot_search_open_public_contract(self):
        import knowledge
        with patch.object(knowledge, 'snapshot', return_value={'available': False, 'documents': []}) as snap:
            self.assertFalse(self.client.get('/api/knowledge').json['available'])
            snap.assert_called_once_with(limit=20)
        with patch.object(knowledge, 'search', return_value=[{'citation': 'message:1@v1'}]) as search:
            result = self.client.get('/api/knowledge/search?q=contratto&limit=12&channel=email')
            self.assertEqual(result.status_code, 200)
            search.assert_called_once_with('contratto', limit=12, channel='email')
        with patch.object(knowledge, 'open_source', return_value={'status': 'not_found'}) as opened:
            self.assertEqual(self.client.get('/api/knowledge/open/document/9').json['status'], 'not_found')
            opened.assert_called_once_with('document', 9)

    def test_invalid_search_and_source_never_call_service(self):
        with patch.object(dashboard, '_service') as service:
            for query in ('', '?q=', '?q=a&limit=0', '?q=a&limit=51', '?q=a&limit=x',
                          '?q=' + 'a' * 513, '?q=a%00b', '?q=a&path=/etc/passwd', '?q=a&channel='):
                self.assertEqual(self.client.get('/api/knowledge/search' + query).status_code, 400, query)
            for path in ('file/1', 'url/1', 'message/0', 'entry/9223372036854775808', 'message/1?path=/etc/passwd'):
                self.assertEqual(self.client.get('/api/knowledge/open/' + path).status_code, 400, path)
            service.assert_not_called()

    def test_consent_absent_means_false_without_writes(self):
        self.conn.execute.return_value.fetchall.return_value = []
        result = self.client.get('/api/data-policies')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json['policies']), 9)
        self.assertTrue(all(p['allowed'] is False and p['version'] == 0 for p in result.json['policies']))
        statement, parameters = self.conn.execute.call_args.args
        self.assertTrue(statement.startswith('SELECT'))
        self.assertIn('LIMIT 9', statement)
        self.assertEqual(len(parameters), 6)

    def test_consent_exact_source_effect_and_boolean_version(self):
        valid = dict(channel='email', effect='ai_text', allowed=True, expected_version=0)
        invalid = [[], {}, {**valid, 'channel': '*'}, {**valid, 'effect': 'send_message'},
                   {**valid, 'allowed': 1}, {**valid, 'allowed': 'false'},
                   {**valid, 'expected_version': True}, {**valid, 'expected_version': -1},
                   {**valid, 'owner_id': '101'}, {**valid, 'channel': "email'; DROP TABLE data_policies;--"}]
        for data in invalid:
            with self.subTest(data=data):
                self.assertEqual(self.post('/api/data-policies', data).status_code, 400)
        self.conn.execute.assert_not_called()
        self.conn.execute.return_value.fetchone.return_value = (1,)
        response = self.post('/api/data-policies', valid)
        self.assertEqual(response.json['version'], 1)
        statement, parameters = self.conn.execute.call_args.args
        self.assertIn('ON CONFLICT(channel,effect) DO NOTHING', statement)
        self.assertEqual(parameters, ('email', 'ai_text', True))

    def test_consent_update_cas_conflict_no_overwrite(self):
        data = dict(channel='whatsapp', effect='ai_media', allowed=False, expected_version=4)
        self.conn.execute.return_value.fetchone.return_value = None
        self.assertEqual(self.post('/api/data-policies', data).status_code, 409)
        self.assertIn('version=%s', self.conn.execute.call_args.args[0])
        self.assertEqual(self.conn.execute.call_args.args[1], (False, 'whatsapp', 'ai_media', 4))

    def approval_data(self):
        return dict(payload=deepcopy(PAYLOAD), decision='approve', expected_version=2,
                    operation_key='secretary-approval:dashboard:3:v2:approve')

    def test_approval_backend_assertion_and_exact_payload_forwarding(self):
        data = self.approval_data()
        with patch.object(secretary, 'resolve_approval', return_value='Ricevuta locale') as resolve:
            response = self.post('/api/secretary/approvals/3', data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['external_execution'])
        self.assertEqual(response.json['operation_key'], data['operation_key'])
        self.assertEqual(resolve.call_args.args, (self.conn, 3, PAYLOAD))
        caller = resolve.call_args.kwargs['caller']
        self.assertIs(type(caller), secretary.DashboardOwner)
        self.assertEqual(caller.owner_id, '101')
        self.assertTrue(caller.authenticated and caller.csrf_verified)
        self.assertEqual(resolve.call_args.kwargs['operation_key'], data['operation_key'])
        self.assertIsNotNone(self.conn.transaction.call_args)

    def test_approval_conflict_and_spoofed_caller_rejected(self):
        data = self.approval_data()
        with patch.object(secretary, 'resolve_approval', side_effect=secretary.Conflict('PRIVATE')) as resolve:
            response = self.post('/api/secretary/approvals/3', data)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('PRIVATE', response.get_data(as_text=True))
            resolve.reset_mock()
            self.assertEqual(self.post('/api/secretary/approvals/3', {**data, 'caller': {'authenticated': True}}).status_code, 400)
            resolve.assert_not_called()
        with patch.object(dashboard.config, 'TELEGRAM_CHAT_ID', ''):
            self.assertEqual(self.post('/api/secretary/approvals/3', data).status_code, 403)

    def test_approval_full_unicode_payload_and_body_bound(self):
        data = self.approval_data()
        data['payload']['text'] = 'à' * 8000
        # Browser sends UTF-8 JSON rather than ASCII escape expansion.
        with patch.object(secretary, 'resolve_approval', return_value='ok'):
            result = self.client.post('/api/secretary/approvals/3', data=json.dumps(data, ensure_ascii=False),
                content_type='application/json', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.post('/api/secretary/approvals/3', data='x' * 50000,
                         content_type='application/json', headers=self.headers).status_code, 413)

    def test_stop_only_restrictive_and_cas(self):
        for data in ({'action': 'resume', 'expected_version': 1}, {'action': 'suspend', 'expected_version': True},
                     {'action': 'revoke', 'expected_version': 1, 'scope': '*'}):
            self.assertEqual(self.post('/api/secretary/responsibilities/7', data).status_code, 400)
        self.conn.execute.assert_not_called()
        self.conn.execute.return_value.fetchone.side_effect = [None, (2,)]
        self.conn.execute.return_value.fetchall.return_value = [(9, 4)]
        result = self.post('/api/secretary/responsibilities/7', {'action': 'suspend', 'expected_version': 1})
        self.assertEqual(result.status_code, 200)
        calls = self.conn.execute.call_args_list
        self.assertEqual(calls[0].args[1], (secretary.WRITE_LOCK,))
        self.assertIn("state IN ('active','suspended')", calls[2].args[0])
        self.assertEqual(calls[2].args[1], ('suspended', 7, 1))
        self.assertIn('practice_id IN', calls[3].args[0])
        self.assertIn('approval_invalidated', str(calls[4]))
        self.assertIn('INSERT INTO secretary_receipts', calls[5].args[0])

    def test_stop_stale_and_matching_replay(self):
        data = dict(action='revoke', expected_version=2)
        self.conn.execute.return_value.fetchone.side_effect = [None, None]
        self.assertEqual(self.post('/api/secretary/responsibilities/7', data).status_code, 409)
        self.conn.execute.reset_mock()
        digest = secretary.payload_hash(dict(id=7, action='revoke', expected_version=2, owner_id='101'))
        self.conn.execute.return_value.fetchone.side_effect = None
        self.conn.execute.return_value.fetchone.return_value = (digest, 'Ricevuta precedente')
        response = self.post('/api/secretary/responsibilities/7', data)
        self.assertEqual(response.json['receipt'], 'Ricevuta precedente')
        self.assertEqual(self.conn.execute.call_count, 2)

    def test_stop_rejects_untrusted_backend_object(self):
        with self.assertRaises(PermissionError):
            dashboard._server_stop_responsibility(1, dict(action='suspend', expected_version=1),
                                                  {'authenticated': True, 'owner_id': '101'})
        self.conn.execute.assert_not_called()

    def test_capabilities_use_existing_flow_store_without_policy_grant(self):
        with patch.object(dashboard.flows, 'settings', return_value={'invoices': False, 'monthly_zip': True}):
            result = self.client.get('/api/capabilities').json['capabilities']
        self.assertEqual(result, [dict(id='invoices', enabled=False, optional=True),
                                  dict(id='monthly_zip', enabled=True, optional=True)])
        with patch.object(dashboard.flows, 'set_enabled') as toggle:
            self.assertEqual(self.post('/api/flows/invoices', {'enabled': True}).status_code, 200)
            toggle.assert_called_once_with('invoices', True)
        self.conn.execute.assert_not_called()

    def test_planned_settings_never_claim_runtime_enforcement(self):
        with patch.object(admin_config, '_review_runtime_supported', return_value=True):
            result = self.client.get('/api/secretary/support').json
        statuses = {item['id']: item['supported'] for item in result['features']}
        self.assertEqual(statuses, dict(secretary_mode=True, quiet_hours=True,
                                      ai_daily_budget=True, output_token_budget=True))
        # SECRETARY_MODE, QUIET_HOURS e AI_BUDGET_DAILY_COST hanno consumer
        # runtime reali (quiet_hours, execution, ai_budget): modificabili.
        for key in admin_config.SECRETARY_FIELDS:
            self.assertFalse(admin_config.readonly(key))
            self.assertTrue(admin_config.field_metadata(key)['supported'])
            self.assertIn(key, admin_config.schema())
        self.assertIn('AI_BUDGET_DAILY_COST', admin_config.schema())
        self.assertFalse(admin_config.readonly('AI_BUDGET_DAILY_COST'))

    def test_runtime_snapshot_projects_only_displayable_proposals(self):
        module = SimpleNamespace(snapshot=lambda limit: dict(reviews=[dict(practice_id=1,
            input={'source': 'PRIVATE'}, plan={'prompt': 'PRIVATE', 'draft': 'Bozza locale'},
            status='prepared')], attention=[{'private': 'PRIVATE'}]))
        with patch.object(dashboard, '_service', return_value=module):
            response = self.client.get('/api/secretary/runtime')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        self.assertEqual(response.json['reviews'][0]['proposal'], {'draft': 'Bozza locale'})

    def test_review_config_validation_and_missing_runtime(self):
        from installer import valid_preference
        for key in admin_config.REVIEW_FIELDS:
            value = '22' if key.endswith(('_START', '_END')) else '60'
            self.assertTrue(valid_preference(key, value))
            for invalid in ('-1', '10081', 'true', ''):
                self.assertFalse(valid_preference(key, invalid))
            with patch.object(admin_config, '_review_runtime_supported', return_value=False):
                self.assertTrue(admin_config.readonly(key))
        with patch.object(admin_config, '_review_runtime_supported', return_value=True):
            admin_config.validate({'SECRETARY_REVIEW_QUIET_START': '0',
                                   'SECRETARY_REVIEW_QUIET_END': '23',
                                   'SECRETARY_REVIEW_MIN_MINUTES': '5',
                                   'SECRETARY_REVIEW_ACK_MINUTES': '10080'}, {})
            for key, value in (('SECRETARY_REVIEW_QUIET_START', '24'),
                               ('SECRETARY_REVIEW_QUIET_END', '-1'),
                               ('SECRETARY_REVIEW_MIN_MINUTES', '4'),
                               ('SECRETARY_REVIEW_ACK_MINUTES', '0')):
                with self.assertRaises(ValueError):
                    admin_config.validate({key: value}, {})

    def test_frontend_safe_text_historical_access_and_exact_approvals(self):
        root = Path(__file__).parent
        script = (root / 'web/app.js').read_text()
        page = (root / 'web/index.html').read_text()
        for label in ('Cosa seguo per te', 'Documenti e ricerca', 'Permessi AI', 'Esiti'):
            self.assertIn(label, page)
        for forbidden in ('innerHTML', 'localStorage', 'sessionStorage', 'eval('):
            self.assertNotIn(forbidden, script)
        self.assertIn('payload:approval.payload', script)
        self.assertIn('expected_version:approval.version', script)
        self.assertIn('secretary-approval:dashboard:', script)
        self.assertIn('id="invoice-history"', page)
        self.assertIn("el('invoice-history').open = invoiceActive", script)
        self.assertIn("// secretary document IDs are attachments", script)
        # Navigazione multi-pagina: pagine esclusive e pulsanti di navigazione.
        for page_id in ('page-overview', 'page-following', 'page-knowledge', 'page-ai-permissions',
                        'page-outcomes', 'page-integration-settings', 'page-archive'):
            self.assertIn(f'id="{page_id}"', page)
        self.assertIn('data-page="overview"', page)
        self.assertIn("PAGES = ['overview'", script)
        self.assertIn('function showPage(name)', script)
        self.assertIn("location.hash.slice(1)||'overview'", script)

    def test_packaging_and_ci_contracts(self):
        root = Path(__file__).parent
        image = (root / 'Dockerfile').read_text()
        self.assertIn('COPY policy.py capabilities.py knowledge.py secretary.py secretary_runtime.py ./', image)
        # I moduli runtime del segretario viaggiano insieme: execution e i
        # nuovi gate/privacy non devono mai restare fuori dall'immagine.
        self.assertIn('COPY execution.py quiet_hours.py privacy.py ./', image)
        workflow = (root / '.github/workflows/docker.yml').read_text()
        self.assertIn('python -m unittest discover -v', workflow)
        self.assertIn('PI_SECRETARY_CONFIG_FILE=/dev/null', workflow)
        self.assertIn('POSTGRES_DB: secretary_test', workflow)
        self.assertNotIn('${{ secrets.', workflow)
        self.assertIn('test_secretary_dashboard test_secretary_execution test_secretary_budget', workflow)

    def email_data(self):
        return dict(practice_id=7, expected_version=2, payload=deepcopy(PAYLOAD),
                    account_address=ACCOUNT['address'],
                    expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    operation_key='secretary-approval:email:dashboard:offline')

    def email_snapshot(self):
        return dict(approval_id=3, expected_version=2, account_address=ACCOUNT['address'],
                    payload=deepcopy(PAYLOAD), payload_hash=secretary.payload_hash(PAYLOAD),
                    subject=execution.SUBJECT, expires_at=self.email_data()['expires_at'],
                    state='approved', documents_supported=False)

    def dispatch_data(self):
        return dict(expected_version=2, account_address=ACCOUNT['address'], subject=execution.SUBJECT,
                    payload=deepcopy(PAYLOAD), approval_operation_key=self.approval_data()['operation_key'],
                    operation_key='secretary-email-dispatch:dashboard:3:v2')

    def test_request_email_exact_sender_expiry_and_no_dispatch_side_effect(self):
        data = self.email_data()
        with patch.object(execution, 'request_email', return_value=self.email_snapshot()) as proposed, \
             patch.object(execution, 'dispatch_email') as dispatch:
            response = self.post('/api/secretary/emails', data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(proposed.call_args.args, (self.conn,))
        self.assertEqual(proposed.call_args.kwargs, data)
        self.assertEqual(response.json['email']['subject'], execution.SUBJECT)
        self.assertFalse(response.json['external_execution'])
        dispatch.assert_not_called()

    def test_email_snapshot_projection_and_explicit_dispatch_forward_exact_fields(self):
        raw = dict(self.email_snapshot(), smtp_password='PRIVATE', job_token='PRIVATE')
        with patch.object(execution, 'email_snapshot', return_value=raw) as read:
            response = self.client.get('/api/secretary/emails/3')
        read.assert_called_once_with(self.conn, 3)
        self.assertEqual(response.json['account_address'], ACCOUNT['address'])
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        with patch.object(execution, 'dispatch_email', return_value=dict(approval_id=3, dispatch_key='key')) as dispatch:
            response = self.post('/api/secretary/emails/3/dispatch', self.dispatch_data())
        self.assertEqual(response.status_code, 200)
        args = dispatch.call_args.kwargs
        self.assertEqual(args['payload'], PAYLOAD)
        self.assertEqual(args['account_address'], ACCOUNT['address'])
        self.assertEqual(args['approval_operation_key'], self.approval_data()['operation_key'])
        self.assertIs(type(args['caller']), secretary.DashboardOwner)
        self.assertTrue(args['caller'].authenticated and args['caller'].csrf_verified)
        self.assertNotIn('subject', args)
        self.assertTrue(response.json['queued'])
        self.assertFalse(response.json['delivered'])

    def test_approval_never_dispatches_enqueues_or_sends(self):
        from connectors import email
        import jobs
        with patch.object(secretary, 'resolve_approval', return_value='Approvata') as approve, \
             patch.object(execution, 'dispatch_email') as dispatch, \
             patch.object(jobs, 'enqueue') as enqueue, patch.object(email, 'send') as send:
            response = self.post('/api/secretary/approvals/3', self.approval_data())
        self.assertEqual(response.status_code, 200)
        approve.assert_called_once()
        dispatch.assert_not_called()
        enqueue.assert_not_called()
        send.assert_not_called()

    def test_email_finite_validation_spoofing_and_header_injection_before_writes(self):
        valid = self.email_data()
        invalid = [[], {}, dict(valid, practice_id=True), dict(valid, expected_version=0),
                   dict(valid, caller={'authenticated': True}), dict(valid, account_address=''),
                   dict(valid, account_address='A <owner@example.test>'),
                   dict(valid, expires_at='2030-01-01T09:00:00'), dict(valid, operation_key='any'),
                   dict(valid, payload=dict(PAYLOAD, recipient='a@example.test\r\nBcc: b@example.test')),
                   dict(valid, payload=dict(PAYLOAD, recipient='a@example.test,b@example.test')),
                   dict(valid, payload=dict(PAYLOAD, channel='telegram')),
                   dict(valid, payload=dict(PAYLOAD, text='x' * 8001)),
                   dict(valid, payload=dict(PAYLOAD, documents=[dict(id=True, sha256='a' * 64)]))]
        with patch.object(execution, 'request_email') as propose, patch.object(execution, 'dispatch_email') as dispatch:
            for data in invalid:
                with self.subTest(data=str(data)[:90]):
                    self.assertEqual(self.post('/api/secretary/emails', data).status_code, 400)
            for data in (dict(self.dispatch_data(), caller={}),
                         dict(self.dispatch_data(), approval_operation_key=''),
                         dict(self.dispatch_data(), operation_key='secretary-approval:wrong')):
                self.assertEqual(self.post('/api/secretary/emails/3/dispatch', data).status_code, 400)
            self.assertEqual(self.post('/api/secretary/emails/3/dispatch',
                dict(self.dispatch_data(), subject='Injected subject')).status_code, 409)
            for text in ('{"expected_version":NaN}', '{"expected_version":Infinity}',
                         '{"expected_version":1,"expected_version":2}', 'null'):
                self.assertEqual(self.client.post('/api/secretary/emails', data=text,
                    content_type='application/json', headers=self.headers).status_code, 400)
            propose.assert_not_called()
            dispatch.assert_not_called()
        self.connection.assert_not_called()

    def test_email_request_bound_unicode_and_guard_precedes_body_processing(self):
        for path, data, method in (('/api/secretary/emails', self.email_data(), 'request_email'),
                                  ('/api/secretary/emails/3/dispatch', self.dispatch_data(), 'dispatch_email')):
            data['payload']['text'] = 'à' * 8000
            returned = self.email_snapshot() if method == 'request_email' else dict(approval_id=3, dispatch_key='key')
            with patch.object(execution, method, return_value=returned):
                response = self.client.post(path, data=json.dumps(data, ensure_ascii=False),
                    content_type='application/json', headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.post(path, data='x' * 49153,
                content_type='application/json', headers=self.headers).status_code, 413)
            self.assertEqual(self.client.post(path, data='x' * 49153,
                content_type='application/json').status_code, 403)

    def test_email_service_conflicts_missing_receipt_unsupported_docs_and_missing_schema(self):
        for error, status in ((secretary.Conflict('PRIVATE'), 409), (secretary.Unauthorized('PRIVATE'), 403),
                              (execution.UnsupportedEmail('PRIVATE'), 400), (RuntimeError('PRIVATE'), 503)):
            with patch.object(execution, 'dispatch_email', side_effect=error):
                response = self.post('/api/secretary/emails/3/dispatch', self.dispatch_data())
            self.assertEqual(response.status_code, status)
            self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        with patch.object(dashboard.config, 'TELEGRAM_CHAT_ID', ''):
            self.assertEqual(self.post('/api/secretary/emails', self.email_data()).status_code, 403)

    def test_execution_snapshot_bounded_no_smtp_secrets_and_no_delivery_promise(self):
        self.conn.execute.return_value.fetchall.return_value = [(n, None, None, None) for n in range(25)]
        raw = dict(self.email_snapshot(), account_hash='PRIVATE', smtp_password='PRIVATE', job_token='PRIVATE')
        with patch.object(execution, 'email_snapshot', return_value=raw) as read:
            response = self.client.get('/api/secretary/execution')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['proposals']), 24)
        self.assertEqual(read.call_count, 24)
        self.assertTrue(response.json['truncated'])
        self.assertEqual(response.json['accounts'], [dict(account_address=ACCOUNT['address'])])
        self.assertFalse(response.json['delivery_verification_supported'])
        self.assertTrue(response.json['documents_supported'])
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        self.assertIn('LIMIT 25', self.conn.execute.call_args.args[0])

    def test_budget_snapshot_aggregate_only_null_cost_and_real_caps(self):
        raw = dict(calls=2, reserved_tokens=16000, charged_tokens=17000, reported_tokens=12000,
                   reported_cost=None, cost_unreported_calls=2, inflight=1, prompt='PRIVATE', job_token='PRIVATE')
        with patch.object(ai_budget, 'usage', return_value=raw) as usage, \
             patch.object(ai_budget, '_limits', return_value=dict(DAILY_CALLS=200, DAILY_TOKENS=500000,
                    MAX_INFLIGHT=2, MAX_OUTPUT_TOKENS=65536, password='PRIVATE')):
            response = self.client.get('/api/secretary/budget')
        usage.assert_called_once_with(self.conn)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json['usage']['reported_cost'])
        self.assertEqual(response.json['limits']['DAILY_CALLS'], 200)
        self.assertFalse(response.json['money_cap_enforced'])
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))

    def test_budget_snapshot_exposes_optional_caps_for_saturation_flags(self):
        raw = dict(calls=2, reserved_tokens=16000, charged_tokens=17000, reported_tokens=12000,
                   reported_cost='3.50', cost_unreported_calls=0, inflight=1,
                   scopes={'dialogue': 4, 'review': 0, 'PRIVATE': 9})
        limits = dict(DAILY_CALLS=200, DAILY_TOKENS=500000, MAX_INFLIGHT=2,
                      MAX_OUTPUT_TOKENS=65536, DAILY_COST=Decimal('5.00'),
                      SCOPE_dialogue=10, SCOPE_review=0, SCOPE_research=0,
                      SCOPE_analysis=0, password='PRIVATE')
        with patch.object(ai_budget, 'usage', return_value=raw), \
             patch.object(ai_budget, '_limits', return_value=limits):
            response = self.client.get('/api/secretary/budget')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['money_cap_enforced'])
        self.assertEqual(response.json['daily_cost'], '5.00')
        # Solo scope server-dichiarati con tetto attivo, mai nomi arbitrari.
        self.assertEqual(response.json['scope_caps'], {'dialogue': 10})
        self.assertEqual(response.json['scope_usage'], {'dialogue': 4})
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))

    def test_budget_config_fields_not_secrets_positive_and_restart_only(self):
        self.assertEqual(set(admin_config.BUDGET_FIELDS) | {'AI_BUDGET_DAILY_COST'},
                         {key for key in admin_config.schema() if key.startswith('AI_BUDGET_')})
        for key in admin_config.BUDGET_FIELDS:
            self.assertFalse(admin_config.secret(key))
            expected_type = 'text' if key == 'AI_BUDGET_DAILY_COST' else 'number'
            self.assertEqual(admin_config.field_metadata(key)['type'], expected_type)
            if key == 'AI_BUDGET_DAILY_COST':
                # Tetto monetario: decimale >= 0, 0 = disattivato.
                for value in ('-1', 'true', 'abc', 'NaN', 'inf'):
                    with self.assertRaises(ValueError):
                        admin_config.validate({key: value}, {})
                admin_config.validate({key: '0'}, {})
                admin_config.validate({key: '5.00'}, {})
                continue
            for value in ('0', '-1', 'true', '1.5', '2147483648', '９'):
                with self.assertRaises(ValueError):
                    admin_config.validate({key: value}, {})
            admin_config.validate({key: '1'}, {})
        with self.assertRaises(ValueError):
            admin_config.validate({'AI_BUDGET_MAX_OUTPUT_TOKENS': '65537'}, {})

    def test_email_frontend_separate_actions_receipt_dom_and_exact_display_contract(self):
        root = Path(__file__).parent
        script = (root / 'web/app.js').read_text()
        page = (root / 'web/index.html').read_text()
        for text in ('Prepara invio', 'Invio esplicito', 'account_address:email.account_address',
                     'subject:email.subject', 'payload:email.payload',
                     'approval_operation_key:receipt.dataset.approvalOperationKey',
                     'receipt.dataset.approvalOperationKey = saved.operation_key',
                     "el('email-text').value = draft.textContent", 'option.dataset.version',
                     "'email-approval-receipts'", 'documents:[]'):
            self.assertIn(text, script)
        for identity in ('email-compose', 'email-practice', 'email-account', 'email-recipient',
                         'email-expires', 'email-text', 'email-approval-receipts', 'ai-budget'):
            self.assertIn(f'id="{identity}"', page)
        approve = script[script.index('const saved = await api(`/api/secretary/approvals/'):]
        approve = approve[:approve.index('}; controls.append(button);')]
        self.assertNotIn('/dispatch', approve)
        self.assertIn('rememberApproval(approval, saved)', approve)
        self.assertIn('Documenti/allegati NON supportati', page)
        self.assertIn('crypto.getRandomValues(new Uint8Array(16))', script)
        self.assertNotIn('crypto.randomUUID()', script)

    def test_budget_frontend_saturation_bars_and_thresholds(self):
        root = Path(__file__).parent
        script = (root / 'web/app.js').read_text()
        page = (root / 'web/index.html').read_text()
        style = (root / 'web/style.css').read_text()
        # Soglie richieste: verde <60%, gialla 60–80%, arancione 80–100%, rossa >=100%.
        for text in ("if (ratio < 0.6) return 'ok'", "if (ratio < 0.8) return 'warn'",
                     "if (ratio < 1) return 'high'", "return 'full'",
                     'function budgetMeter(parent, label, used, limit)',
                     "fill.className = `budget-fill ${level}`",
                     "fill.style.width = `${Number.isFinite(ratio) ? Math.min(100, Math.max(0, ratio * 100)) : 100}%`",
                     "fill.setAttribute('role', 'progressbar')",
                     "fill.setAttribute('aria-valuenow', String(used))",
                     "const flag = node('span', 'SATURATO')",
                     "budgetMeter(el('ai-budget'), 'Tentativi HTTP oggi (UTC)', usage.calls, limits.DAILY_CALLS)",
                     "budgetMeter(el('ai-budget'), 'Token addebitati conservativamente', usage.charged_tokens, limits.DAILY_TOKENS)",
                     "budgetMeter(el('ai-budget'), 'Slot prenotati / massimo simultanei', usage.inflight, limits.MAX_INFLIGHT)",
                     'result.money_cap_enforced', 'result.scope_caps', 'result.scope_usage'):
            self.assertIn(text, script)
        for text in ('.budget-fill.ok{background:#3ecf8e}', '.budget-fill.warn{background:#e6c34a}',
                     '.budget-fill.high{background:#e6913a}', '.budget-fill.full{background:#e5484d}',
                     '.budget-track{', '.budget-flag{'):
            self.assertIn(text, style)
        self.assertIn('Barre di saturazione', page)
        self.assertIn('SATURATO', page)


class SecretaryDashboardPostgresTests(unittest.TestCase):
    """Opt-in real SQL, random schema and explicit _test DB, no runtime startup."""
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
        if not cls.url:
            raise unittest.SkipTest('Set PI_SECRETARY_TEST_DATABASE_URL to opt in')
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(cls.url)
        if not params.get('dbname', '').endswith('_test') or params.get('service'):
            raise ValueError('Explicit disposable _test DB without service required')

    def setUp(self):
        import psycopg
        from psycopg import sql
        self.conn = psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.assertTrue(self.conn.execute('SELECT current_database()').fetchone()[0].endswith('_test'))
        self.schema = 'dashboard_test_' + uuid.uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET timezone='UTC'")
        self.conn.execute("SET statement_timeout='15s'")
        self.conn.execute("SET lock_timeout='10s'")
        self.enterContext(patch.object(dashboard.db, '_conn', self.connection))
        self.enterContext(patch.object(dashboard.db, 'available', return_value=True))
        self.enterContext(patch.object(dashboard.config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch.object(dashboard.config, 'TELEGRAM_BOT_TOKEN', '777:offline'))
        self.enterContext(patch.object(dashboard.config, 'EMAIL_ACCOUNTS', [deepcopy(ACCOUNT)]))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        # Il dispatch email esplicito richiede la modalità active: il default
        # observe blocca gli invii a terzi per costruzione.
        self.enterContext(patch.object(config, 'SECRETARY_MODE', 'active'))
        dashboard.db.init()
        metadata = dict(account='bot:777', chat_id='101', author='101', direction='in',
                        chat_type='private', content_origin='direct_text', is_forwarded=False, message_id=1)
        self.conn.execute('''INSERT INTO telegram_messages
            (id,channel,body,account,chat_id,author,direction,message_id,source_id,metadata,ts)
            VALUES (1,'telegram','Segui la riparazione','bot:777','101','101','in','1',%s,%s,now())''',
            (json.dumps(['bot', '777', '101', 1]), json.dumps(metadata)))
        self.responsibility = self.conn.execute('''INSERT INTO secretary_responsibilities
            (type,objective,scope,triggers,allowed_actions,stop_condition,state,origin_message,operation_key)
            VALUES ('monitoring','Riparazione','{}','[]','[]','Stop esplicito','active',1,'r:1') RETURNING id''').fetchone()[0]
        self.practice = self.conn.execute('''INSERT INTO secretary_practices
            (objective,state,next_action,waiting_for,blocked_reason,completion_criteria,responsibility_id,origin_message,operation_key)
            VALUES ('Riparazione','active','Raccogliere documenti','','','Conferma proprietario',%s,1,'p:1') RETURNING id''',
            (self.responsibility,)).fetchone()[0]
        self.client = dashboard.create_app({'username': 'owner', 'password': 'offline'}).test_client()
        csrf = self.client.post('/api/login', json={'username': 'owner', 'password': 'offline'}).json['csrf']
        self.headers = {'X-CSRF-Token': csrf}

    @contextmanager
    def connection(self):
        yield self.conn

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def proposal(self, key):
        return secretary.request_approval(self.conn, practice_id=self.practice, expected_version=1,
            payload=deepcopy(PAYLOAD), expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            operation_key='secretary-approval:' + key)

    def test_real_consent_insert_update_stale_no_implicit_grant(self):
        policies = self.client.get('/api/data-policies').json['policies']
        self.assertTrue(all(not p['allowed'] for p in policies))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone()[0], 0)
        data = dict(channel='email', effect='ai_text', allowed=True, expected_version=0)
        self.assertEqual(self.post('/api/data-policies', data).json['version'], 1)
        self.assertEqual(self.post('/api/data-policies', data).status_code, 409)
        self.assertEqual(self.post('/api/data-policies', {**data, 'allowed': False, 'expected_version': 1}).json['version'], 2)
        self.assertEqual(self.conn.execute('SELECT allowed,version FROM data_policies').fetchall(), [(False, 2)])

    def test_exact_payload_approval_retry_and_revoke(self):
        identity = self.proposal('exact')
        path = f'/api/secretary/approvals/{identity}'
        data = dict(payload=deepcopy(PAYLOAD), decision='approve', expected_version=1,
                    operation_key='secretary-approval:dashboard:exact')
        for field, value in (('text', 'Altro'), ('recipient', 'other@example.invalid'), ('channel', 'telegram')):
            changed = deepcopy(data)
            changed['payload'][field] = value
            self.assertEqual(self.post(path, changed).status_code, 409)
        first = self.post(path, data)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.post(path, data).json, first.json)
        data.update(decision='revoke', expected_version=2, operation_key='secretary-approval:dashboard:revoke')
        self.assertEqual(self.post(path, data).status_code, 200)
        self.assertEqual(self.conn.execute('SELECT state,version FROM secretary_approvals WHERE id=%s', (identity,)).fetchone(), ('revoked', 3))

    def test_suspend_cas_invalidates_pending_and_approved_replay_is_stable(self):
        pending, approved = self.proposal('pending'), self.proposal('approved')
        self.post(f'/api/secretary/approvals/{approved}', dict(payload=PAYLOAD, decision='approve',
            expected_version=1, operation_key='secretary-approval:approved'))
        path = f'/api/secretary/responsibilities/{self.responsibility}'
        data = dict(action='suspend', expected_version=1)
        first = self.post(path, data)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.post(path, data).json, first.json)
        self.assertEqual(self.conn.execute('SELECT state,version FROM secretary_responsibilities').fetchone(), ('suspended', 2))
        self.assertTrue(all(state == 'revoked' for state, in self.conn.execute('SELECT state FROM secretary_approvals').fetchall()))
        self.assertEqual(self.post(path, dict(action='revoke', expected_version=1)).status_code, 409)
        self.assertEqual(self.post(path, dict(action='revoke', expected_version=2)).status_code, 200)
        self.assertEqual(self.post(path, dict(action='suspend', expected_version=3)).status_code, 409)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM secretary_decisions WHERE event='approval_invalidated'").fetchone()[0], 2)

    def test_stop_transaction_rolls_back_if_audit_fails(self):
        self.conn.execute('''CREATE FUNCTION reject_dashboard_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'offline injected failure'; END $$''')
        self.conn.execute('''CREATE TRIGGER reject_audit BEFORE INSERT ON secretary_decisions
            FOR EACH ROW EXECUTE FUNCTION reject_dashboard_audit()''')
        response = self.post(f'/api/secretary/responsibilities/{self.responsibility}', dict(action='revoke', expected_version=1))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.conn.execute('SELECT state,version FROM secretary_responsibilities').fetchone(), ('active', 1))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_receipts').fetchone()[0], 0)

    def email_proposal(self, documents=None):
        data = dict(practice_id=self.practice, expected_version=1,
                    payload=dict(PAYLOAD, documents=documents or []), account_address=ACCOUNT['address'],
                    expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    operation_key='secretary-approval:email:dashboard:' + uuid.uuid4().hex)
        response = self.post('/api/secretary/emails', data)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['email'], data

    def approve_email(self, email):
        data = dict(payload=email['payload'], decision='approve', expected_version=email['expected_version'],
                    operation_key='secretary-approval:dashboard:approve:' + uuid.uuid4().hex)
        response = self.post(f"/api/secretary/approvals/{email['approval_id']}", data)
        self.assertEqual(response.status_code, 200, response.json)
        return dict(expected_version=email['expected_version'] + 1, account_address=email['account_address'],
                    subject=email['subject'], payload=email['payload'],
                    approval_operation_key=response.json['operation_key'],
                    operation_key='secretary-email-dispatch:dashboard:' + uuid.uuid4().hex)

    def test_real_email_security_approval_receipt_then_separate_exact_dispatch(self):
        from connectors import email as connector
        import jobs
        from test_secretary_execution import job_context
        # execute_approved invia con allegati: send_with_attachments è il
        # confine reale del connector; send() è solo un wrapper legacy.
        with patch.object(connector, 'send_with_attachments', return_value=True) as send:
            email, data = self.email_proposal()
            identity = email['approval_id']
            self.assertEqual(self.post('/api/secretary/emails', data).json['email'], email)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 0)
            dispatch = self.approve_email(email)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 0)
            send.assert_not_called()
            path = f'/api/secretary/emails/{identity}/dispatch'
            self.assertEqual(self.post(path, dict(dispatch, approval_operation_key='secretary-approval:forged')).status_code, 403)
            for changed in (dict(dispatch, subject='Custom subject'),
                            dict(dispatch, account_address='other@example.test'),
                            dict(dispatch, payload=dict(PAYLOAD, recipient='other@example.test')),
                            dict(dispatch, payload=dict(PAYLOAD, text='Changed')), dict(dispatch, expected_version=1)):
                self.assertEqual(self.post(path, changed).status_code, 409)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 0)
            accepted = self.post(path, dispatch)
            self.assertEqual(accepted.status_code, 200, accepted.json)
            self.assertFalse(accepted.json['delivered'])
            self.assertEqual(self.post(path, dispatch).json, accepted.json)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 1)
            send.assert_not_called()
            job = jobs.claim(execution.KIND)
            with job_context(job[0], job[1], job[3]):
                self.assertEqual(execution.execute_approved(job[1])['state'], 'accepted')
            send.assert_called_once_with(ACCOUNT, PAYLOAD['recipient'], execution.SUBJECT, PAYLOAD['text'], [])
            result = self.client.get('/api/secretary/execution')
            self.assertEqual(result.json['proposals'][0]['dispatch_state'], 'accepted')
            self.assertFalse(result.json['proposals'][0]['delivered'])
            self.assertNotIn('PRIVATE', result.get_data(as_text=True))
            self.assertEqual(self.client.get('/api/secretary/budget').json['usage']['calls'], 0)

    def test_real_email_documents_dispatch_and_revoked_never_queue(self):
        import hashlib
        from connectors import email as connector
        from test_secretary_execution import job_context
        self.conn.execute("INSERT INTO attachments(id,message_id,channel,data) VALUES (9,1,'telegram',%s)", (b'document',))
        self.conn.execute('''INSERT INTO secretary_links(practice_id,document_id,evidence,origin_message)
            VALUES (%s,9,'Documento esplicito',1)''', (self.practice,))
        email, _ = self.email_proposal([dict(id=9, sha256=hashlib.sha256(b'document').hexdigest())])
        dispatch = self.approve_email(email)
        path = f"/api/secretary/emails/{email['approval_id']}/dispatch"
        # Gli allegati verificati ora vengono inviati col connector reale.
        with patch.object(connector, 'send_with_attachments', return_value=True) as send:
            accepted = self.post(path, dispatch)
            self.assertEqual(accepted.status_code, 200, accepted.json)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 1)
            import jobs
            job = jobs.claim(execution.KIND)
            with job_context(job[0], job[1], job[3]):
                self.assertEqual(execution.execute_approved(job[1])['state'], 'accepted')
            send.assert_called_once_with(ACCOUNT, PAYLOAD['recipient'], execution.SUBJECT,
                                         PAYLOAD['text'], [dict(filename='allegato', data=b'document',
                                                               content_type='application/octet-stream')])
        email, _ = self.email_proposal()
        dispatch = self.approve_email(email)
        response = self.post(f"/api/secretary/approvals/{email['approval_id']}",
            dict(payload=email['payload'], decision='revoke', expected_version=2,
                 operation_key='secretary-approval:dashboard:revoke:' + uuid.uuid4().hex))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post(f"/api/secretary/emails/{email['approval_id']}/dispatch", dispatch).status_code, 409)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()