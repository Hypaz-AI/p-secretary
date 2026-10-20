"""Contratti della dashboard: HTTP offline con mock, SQL solo su database *_test.

Nessuna credenziale di deployment, chiamata a provider, worker o migrazione di produzione.
"""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import admin_config
import ai_budget
import config
import dashboard
import jobs


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(dashboard.config, 'TELEGRAM_CHAT_ID', '101'))
        self.connection = MagicMock()
        self.enterContext(patch.object(dashboard.db, '_conn', return_value=self.connection))
        self.conn = self.connection.__enter__.return_value
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('No network')))
        self.client = dashboard.create_app({'username': 'owner', 'password': 'offline'}).test_client()
        self.csrf = self.client.post('/api/login', json={'username': 'owner', 'password': 'offline'}).json['csrf']
        self.headers = {'X-CSRF-Token': self.csrf}

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    # ------------------------------------------------------------ guardie
    def test_every_api_requires_authentication_and_same_origin(self):
        anonymous = dashboard.create_app({'username': 'owner', 'password': 'offline'}).test_client()
        for path in ('/api/agent', '/api/agent/runs/1', '/api/budget', '/api/budget/limits',
                     '/api/data-policies', '/api/capabilities', '/api/privacy/messages/1/export'):
            with self.subTest(path=path):
                self.assertEqual(anonymous.get(path).status_code, 401)
                self.assertEqual(self.client.get(path, headers={'Host': 'evil.invalid'}).status_code, 400)
        for path in ('/api/data-policies', '/api/budget/reconcile', '/api/jobs/requeue',
                     '/api/media/retry-blocked', '/api/privacy/messages/1/erase'):
            with self.subTest(path=path):
                self.assertEqual(anonymous.post(path, json={}).status_code, 401)
                self.assertEqual(self.client.post(path, json={}).status_code, 403)
                for extra in ({'Origin': 'https://evil.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
                    self.assertEqual(self.client.post(path, json={}, headers={**self.headers, **extra}).status_code, 403)
        self.connection.assert_not_called()

    def test_removed_surfaces_do_not_exist(self):
        for path in ('/api/secretary', '/api/secretary/emails', '/api/secretary/approvals/1',
                     '/api/secretary/runtime', '/api/knowledge', '/api/knowledge/search?q=x'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
                self.assertIn(self.client.post(path, json={}, headers=self.headers).status_code, (404, 405))

    # ------------------------------------------------------------ consensi
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

    def test_capabilities_use_existing_flow_store_without_policy_grant(self):
        with patch.object(dashboard.flows, 'settings', return_value={'invoices': False, 'monthly_zip': True}):
            result = self.client.get('/api/capabilities').json['capabilities']
        self.assertEqual(result, [dict(id='invoices', enabled=False, optional=True),
                                  dict(id='monthly_zip', enabled=True, optional=True)])
        with patch.object(dashboard.flows, 'set_enabled') as toggle:
            self.assertEqual(self.post('/api/flows/invoices', {'enabled': True}).status_code, 200)
            toggle.assert_called_once_with('invoices', True)
        self.conn.execute.assert_not_called()

    # --------------------------------------------------------- vista agente
    def test_agent_view_exposes_metadata_only_and_no_send_control(self):
        started = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        runs = [(5, 'owner', 'done', 3, None, 'openrouter/auto', started, started)]
        drafts = [(2, 'owner@example.test', 'to@example.test', 'Oggetto', 'Corpo', 'proposed',
                   started, None, None)]
        self.conn.execute.return_value.fetchall.side_effect = [runs, drafts]
        with patch('agent.data.readonly_role', return_value='agent_ro'), \
             patch.object(config, 'SECRETARY_MODE', 'observe'):
            response = self.client.get('/api/agent')
        self.assertEqual(response.status_code, 200)
        body = response.json
        self.assertEqual(body['model'], config.AGENT_MODEL)
        self.assertTrue(body['readonly_role'])
        self.assertFalse(body['sends_allowed'])
        self.assertEqual(body['runs'][0]['kind'], 'owner')
        self.assertEqual(body['emails'][0]['state'], 'proposed')
        self.assertEqual(set(body['runs'][0]), {'id', 'kind', 'state', 'tool_calls', 'error', 'model',
                                                'started_at', 'finished_at'})
        # Nessun prompt, risposta del modello, token o credenziale.
        for forbidden in ('prompt', 'api_key', 'token', 'password'):
            self.assertNotIn(forbidden, response.get_data(as_text=True).lower())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        # La dashboard non espone alcuna rotta per decidere o inviare una bozza.
        for path in ('/api/agent/emails', '/api/agent/emails/2/send', '/api/agent/send'):
            self.assertIn(self.client.post(path, json={}, headers=self.headers).status_code, (404, 405))

    def test_agent_run_detail_is_bounded_and_validates_the_id(self):
        at = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        self.conn.execute.return_value.fetchall.return_value = [(1, 'db_query', '{"sql":"select 1"}', True, '1 riga', 12, at)]
        response = self.client.get('/api/agent/runs/5')
        self.assertEqual(response.json['calls'][0]['tool'], 'db_query')
        self.assertIn('LIMIT 60', self.conn.execute.call_args.args[0])
        self.assertEqual(self.client.get('/api/agent/runs/0').status_code, 400)
        self.assertEqual(self.client.get('/api/agent/runs/5?x=1').status_code, 400)
        self.assertEqual(self.client.get('/api/agent/runs/abc').status_code, 404)

    # --------------------------------------------------------------- budget
    def test_budget_snapshot_aggregate_only_null_cost_and_real_caps(self):
        raw = dict(calls=2, reserved_tokens=16000, charged_tokens=17000, reported_tokens=12000,
                   reported_cost=None, cost_unreported_calls=2, inflight=1, prompt='PRIVATE', job_token='PRIVATE')
        with patch.object(ai_budget, 'usage', return_value=raw) as usage, \
             patch.object(ai_budget, 'orphans', return_value=[]) as orphans, \
             patch.object(ai_budget, '_limits', return_value=dict(DAILY_CALLS=200, DAILY_TOKENS=500000,
                    MAX_INFLIGHT=2, MAX_OUTPUT_TOKENS=65536, password='PRIVATE')):
            response = self.client.get('/api/budget')
        usage.assert_called_once_with(self.conn)
        orphans.assert_called_once_with(self.conn)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json['usage']['reported_cost'])
        self.assertEqual(response.json['limits']['DAILY_CALLS'], 200)
        self.assertFalse(response.json['money_cap_enforced'])
        self.assertEqual(response.json['orphaned'], [])
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))

    def test_budget_snapshot_exposes_optional_caps_for_saturation_flags(self):
        raw = dict(calls=2, reserved_tokens=16000, charged_tokens=17000, reported_tokens=12000,
                   reported_cost='3.50', cost_unreported_calls=0, inflight=1,
                   scopes={'agent_owner': 4, 'agent_inbox': 0, 'PRIVATE': 9})
        limits = dict(DAILY_CALLS=200, DAILY_TOKENS=500000, MAX_INFLIGHT=2,
                      MAX_OUTPUT_TOKENS=65536, DAILY_COST=Decimal('5.00'),
                      SCOPE_agent_owner=10, SCOPE_agent_inbox=0, password='PRIVATE')
        orphan = dict(id='abc', job_id=42, job_token='PRIVATE', reserved_tokens=8192,
                      created_at=datetime(2026, 10, 19, 15, 30, tzinfo=timezone.utc))
        with patch.object(ai_budget, 'usage', return_value=raw), \
             patch.object(ai_budget, 'orphans', return_value=[orphan]), \
             patch.object(ai_budget, '_limits', return_value=limits):
            response = self.client.get('/api/budget')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['money_cap_enforced'])
        self.assertEqual(response.json['daily_cost'], '5.00')
        # Solo scope server-dichiarati con tetto attivo, mai nomi arbitrari.
        self.assertEqual(response.json['scope_caps'], {'agent_owner': 10})
        self.assertEqual(response.json['scope_usage'], {'agent_owner': 4})
        # Gli orfani espongono solo job_id/tokens/timestamp, mai il token del job.
        self.assertEqual(response.json['orphaned'],
                         [dict(job_id=42, reserved_tokens=8192,
                               created_at='2026-10-19T15:30:00+00:00')])
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))

    def test_budget_reconcile_and_jobs_requeue_endpoints(self):
        with patch.object(ai_budget, 'reconcile_orphans', return_value=[('a',), ('b',)]) as reconcile:
            response = self.post('/api/budget/reconcile', {})
        reconcile.assert_called_once_with(self.conn)
        self.assertEqual(response.json, dict(available=True, freed=2))
        for kind in ('analysis', 'owner', 'digest'):
            with patch.object(jobs, 'requeue_failed', return_value=[7, 9]) as requeue:
                response = self.post('/api/jobs/requeue', {'kind': kind})
            requeue.assert_called_once_with(kind, 50)
            self.assertEqual(response.json, dict(available=True, kind=kind, requeued=[7, 9]))
        # Tipi sconosciuti (anche quelli rimossi) e campi extra sono rifiutati prima di ogni modifica.
        for data in ({'kind': 'knowledge'}, {'kind': 'reconcile'}, {'kind': 'secretary_review'},
                     {'kind': 'analysis', 'limit': 0}, {'kind': 'analysis', 'limit': 201}, {}):
            with self.subTest(data=data), patch.object(jobs, 'requeue_failed') as requeue:
                self.assertEqual(self.post('/api/jobs/requeue', data).status_code, 400)
                requeue.assert_not_called()

    def test_budget_config_fields_not_secrets_positive_and_restart_only(self):
        self.assertEqual(set(admin_config.BUDGET_FIELDS) | {'AI_BUDGET_DAILY_COST'},
                         {key for key in admin_config.schema() if key.startswith('AI_BUDGET_')})
        for key in admin_config.BUDGET_FIELDS:
            self.assertFalse(admin_config.secret(key))
            expected_type = 'text' if key == 'AI_BUDGET_DAILY_COST' else 'number'
            self.assertEqual(admin_config.field_metadata(key)['type'], expected_type)
            if key == 'AI_BUDGET_DAILY_COST':
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

    def test_budget_limits_endpoint_and_settings_group_names(self):
        with patch.object(ai_budget, '_limits', return_value=dict(
                DAILY_CALLS=10000, DAILY_TOKENS=500000000, MAX_INFLIGHT=2,
                MAX_OUTPUT_TOKENS=65536, DAILY_COST=0, password='PRIVATE')):
            response = self.client.get('/api/budget/limits')
        self.assertEqual(response.status_code, 200)
        fields = {field['key']: field['value'] for field in response.json['fields']}
        self.assertEqual(fields['AI_BUDGET_MAX_INFLIGHT'], '2')
        self.assertEqual(fields['AI_BUDGET_DAILY_COST'], '0')
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        schema = admin_config.schema()
        self.assertEqual(schema['AI_BUDGET_MAX_INFLIGHT']['group'],
                         'AI · limiti dei Worker (chiamate, token, concorrenza)')
        self.assertEqual(schema['AI_BUDGET_DAILY_COST']['group'],
                         'AI · tetto di spesa in denaro (opzionale)')

    def test_agent_settings_are_editable_and_validated(self):
        schema = admin_config.schema()
        for key in ('AGENT_MODEL', 'AGENT_WEB_MODEL', 'AGENT_NOTIFY_MAX_PER_DAY'):
            self.assertIn(key, schema)
            self.assertFalse(admin_config.readonly(key))
            self.assertTrue(schema[key]['description'])
        for key in ('NOTIFY_PRIORITY_THRESHOLD', 'RECONCILE_CONTEXT_CHARS', 'SECRETARY_REVIEW_MIN_MINUTES',
                    'OPENROUTER_MODEL_CHAT'):
            self.assertNotIn(key, schema)
        from installer import valid_preference
        self.assertTrue(valid_preference('AGENT_MODEL', 'openrouter/auto'))
        self.assertTrue(valid_preference('AGENT_WEB_MODEL', ''))
        for bad in ('', 'a b', '../x', 'model;rm', '-x'):
            self.assertFalse(valid_preference('AGENT_MODEL', bad))

    # ------------------------------------------------------------- privacy
    def test_erase_requires_a_key_and_the_configured_owner(self):
        for data in ({}, {'operation_key': 'x'}, {'operation_key': 'valid-operation-key', 'extra': 1}):
            with self.subTest(data=data):
                self.assertEqual(self.post('/api/privacy/messages/9/erase', data).status_code, 400)
        with patch.object(dashboard.config, 'TELEGRAM_CHAT_ID', ''):
            response = self.post('/api/privacy/messages/9/erase', {'operation_key': 'valid-operation-key'})
        self.assertEqual(response.status_code, 403)
        self.conn.execute.assert_not_called()

    # ------------------------------------------------------------ frontend
    def test_frontend_is_safe_text_only_and_has_no_send_surface(self):
        root = Path(__file__).parent
        script = (root / 'web/app.js').read_text()
        page = (root / 'web/index.html').read_text()
        for label in ('Agente', 'Permessi AI', 'Impostazioni', 'Archivio e file', 'Panoramica'):
            self.assertIn(label, page)
        for forbidden in ('innerHTML', 'localStorage', 'sessionStorage', 'eval('):
            self.assertNotIn(forbidden, script)
        for removed in ('/api/secretary', '/api/knowledge', 'dispatch', 'approvals', 'practice'):
            self.assertNotIn(removed, script)
        self.assertIn("/invia ${email.id}", script)  # la decisione passa dal gesto su Telegram
        for page_id in ('page-overview', 'page-agent', 'page-ai-permissions',
                        'page-integration-settings', 'page-archive'):
            self.assertIn(f'id="{page_id}"', page)
        self.assertIn("PAGES = ['overview', 'agent'", script)
        self.assertIn('function showPage(name)', script)
        self.assertIn("location.hash.slice(1)||'overview'", script)
        # Ogni id usato dallo script esiste nella pagina.
        import re
        for identity in set(re.findall(r"el\('([a-z-]+)'\)", script)):
            self.assertIn(f'id="{identity}"', page, identity)

    def test_budget_frontend_saturation_bars_and_thresholds(self):
        root = Path(__file__).parent
        script = (root / 'web/app.js').read_text()
        page = (root / 'web/index.html').read_text()
        style = (root / 'web/style.css').read_text()
        # Soglie: verde <60%, gialla 60–80%, arancione 80–100%, rossa >=100%.
        for text in ("if (ratio < 0.6) return 'ok'", "if (ratio < 0.8) return 'warn'",
                     "if (ratio < 1) return 'high'", "return 'full'",
                     'function budgetMeter(parent, label, used, limit, target)',
                     "fill.className = `budget-fill ${level}`",
                     "fill.setAttribute('role', 'progressbar')",
                     "const flag = node('span', 'SATURATO')",
                     "const edit = node('button', 'Modifica questo limite')",
                     'async function openSettingsGroup(group, key)',
                     "api('/api/budget')", "api('/api/budget/reconcile', {})",
                     "api('/api/jobs/requeue', {kind: 'analysis'})",
                     'result.money_cap_enforced', 'result.scope_caps', 'result.scope_usage',
                     "agent_owner: 'agente · tue richieste'"):
            self.assertIn(text, script)
        for text in ('.budget-fill.ok{background:#3ecf8e}', '.budget-fill.warn{background:#e6c34a}',
                     '.budget-fill.high{background:#e6913a}', '.budget-fill.full{background:#e5484d}'):
            self.assertIn(text, style)
        self.assertIn('Barre di saturazione', page)
        self.assertIn('Modifica i limiti AI', page)

    def test_packaging_and_ci_contracts(self):
        root = Path(__file__).parent
        image = (root / 'Dockerfile').read_text()
        self.assertIn('COPY agent/ ./agent/', image)
        self.assertIn('COPY connectors/ ./connectors/', image)
        # `.dockerignore` parte da `**`: una cartella copiata dal Dockerfile ma non riammessa
        # lì NON entra nel contesto di build ("/agent: not found" solo in CI, mai nei test).
        admitted = {line.strip() for line in (root / '.dockerignore').read_text().splitlines()}
        self.assertEqual(next(line.strip() for line in (root / '.dockerignore').read_text().splitlines() if line.strip()), '**')
        for line in image.splitlines():
            parts = line.split()
            if parts[:1] == ['COPY'] and not parts[1].startswith('--'):
                for source in parts[1:-1]:
                    top = source.split('/')[0]
                    if '/' in source and (root / top).is_dir():
                        self.assertIn(f'!{top}/', admitted, f'{top}/ è copiata dal Dockerfile ma esclusa da .dockerignore')
                    elif source.endswith('.py'):
                        self.assertIn('!*.py', admitted)
        self.assertIn('!agent/*.py', admitted)
        # Il runtime è incorporato nell'immagine: niente download all'avvio.
        self.assertIn('python -m copilot download-runtime', image)
        self.assertIn('COPILOT_SKIP_CLI_DOWNLOAD=1', image)
        requirements = (root / 'requirements.txt').read_text()
        for pin in ('github-copilot-sdk==', 'pydantic', 'pglast'):
            self.assertIn(pin, requirements)
        # Ogni modulo importato dall'applicazione deve essere nell'immagine.
        copied = set()
        for line in image.splitlines():
            if line.startswith('COPY ') and not line.startswith('COPY data/'):
                copied.update(token for token in line.split()[1:-1] if token.endswith('.py'))
        for path in sorted(root.glob('*.py')):
            if path.name.startswith(('test_', 'setup_')) or path.name in ('support.py', 'fix_communication_dates.py',
                                                                      'migrate_email_attachments.py'):
                continue
            self.assertIn(path.name, copied, f'{path.name} non è copiato nell\'immagine')
        for removed in ('secretary.py', 'secretary_runtime.py', 'execution.py', 'brain.py',
                        'owner_dialogue.py', 'research.py', 'owner_instructions.py', 'agent_tools.py'):
            self.assertNotIn(removed, image)
        workflow = (root / '.github/workflows/docker.yml').read_text()
        self.assertIn('python -m unittest discover -v', workflow)
        self.assertIn('PI_SECRETARY_CONFIG_FILE=/dev/null', workflow)
        self.assertIn('POSTGRES_DB: secretary_test', workflow)
        self.assertNotIn('${{ secrets.', workflow)


class DashboardPostgresTests(unittest.TestCase):
    """Consensi reali: nessuna concessione implicita, CAS sulla versione."""

    @classmethod
    def setUpClass(cls):
        import test_agent_postgres
        if not test_agent_postgres.URL:
            raise unittest.SkipTest('Set PI_SECRETARY_TEST_DATABASE_URL to opt in')

    def test_real_consent_insert_update_stale_no_implicit_grant(self):
        import test_agent_postgres as helper

        class Case(helper.AgentPostgresCase):
            def runTest(inner):
                self.assertEqual(inner.conn.execute('SELECT count(*) FROM data_policies').fetchone()[0], 0)
                with patch.object(dashboard.config, 'TELEGRAM_CHAT_ID', '101'):
                    first = dashboard._server_data_policy(dict(channel='email', effect='ai_text',
                                                               allowed=True, expected_version=0))
                    self.assertEqual(first['version'], 1)
                    second = dashboard._server_data_policy(dict(channel='email', effect='ai_text',
                                                                allowed=False, expected_version=1))
                    self.assertEqual((second['allowed'], second['version']), (False, 2))
                    for stale in (1, 0):
                        with self.assertRaises(dashboard.owners.Conflict):
                            dashboard._server_data_policy(dict(channel='email', effect='ai_text',
                                                               allowed=True, expected_version=stale))
                self.assertEqual(inner.conn.execute('SELECT count(*) FROM data_policies').fetchone()[0], 1)
                self.assertEqual(inner.conn.execute('SELECT allowed FROM data_policies').fetchone()[0], False)
        result = unittest.TextTestRunner(stream=open('/dev/null', 'w'), verbosity=0).run(Case())
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)


if __name__ == '__main__':
    unittest.main()
