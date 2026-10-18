"""Offline contracts for the read-only agent tool registry.

No production database, network, workers or model calls. Consent gates run for
real against a strict native-policy SQL double, reusing the proven fixture
shape from test_history_context_policy (metadata_for + data_policies FOR SHARE).
"""
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

with tempfile.TemporaryDirectory(prefix='agent-tools-') as _bootstrap:
    with patch.dict(os.environ, {'PI_SECRETARY_CONFIG_FILE': '/dev/null',
                                 'DATA_DIR': _bootstrap, 'DATABASE_URL': ''}):
        import agent_tools
        import brain
        import db
        import jobs
        import knowledge
        import owner_dialogue as dialogue
        import policy
        import secretary
        import test_history_context_policy as policy_helpers


CANARY = 'AGENT_TOOLS_CANARY_51c7'


class AgentToolTests(unittest.TestCase):
    """Real consent gates over the strict SQL double; no PostgreSQL needed."""

    message = policy_helpers.HistoryContextPolicyTests.message
    consent = policy_helpers.HistoryContextPolicyTests.consent

    def setUp(self):
        # The fixture's setUp binds conn.execute to self.execute, so define the
        # SQL double BEFORE delegating; unknown SQL must fail loudly, not pass.
        self.messages = {table: {} for table in policy.NATIVE_TABLES}
        self.grants = {}
        self.audits = []
        self.conn = MagicMock()
        self.conn.execute.side_effect = self.execute
        self.enterContext(patch.object(policy.config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value = self.conn
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch.object(brain.ai_budget, 'request_content',
                                       side_effect=AssertionError('Model forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        self.mid = self.message('email', mid=42)
        self.other = self.message('whatsapp', mid=43)
        self.consent('email', 'ai_text')

    def execute(self, sql, params):
        if sql.startswith('SELECT ' + ','.join(policy_helpers.FIELDS) + ' FROM '):
            self.assertIn('FOR SHARE', sql)
            table = sql.split(' FROM ', 1)[1].split()[0]
            row = self.messages[table].get(params[0])
        elif sql.startswith('SELECT allowed FROM data_policies '):
            self.assertIn('FOR SHARE', sql)
            row = (self.grants[params],) if params in self.grants else None
        elif sql.startswith('SELECT 1 FROM attachments'):
            row = None
        elif sql.startswith('INSERT INTO effect_audit('):
            self.audits.append(params)
            row = None
        else:
            raise AssertionError('Unexpected SQL: ' + sql)
        return SimpleNamespace(fetchone=lambda: row)

    def source(self, mid=42, channel='email'):
        return dict(source_type='message', source_id=mid, citation=f'message:{mid}@1',
                    historical=False, channel=channel, text='Tetto ' + CANARY,
                    entry_id=None, documents=[])

    def patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result


class RegistryContractTests(AgentToolTests):
    def test_registry_is_read_only_and_declared(self):
        self.assertEqual(agent_tools.TOOL_NAMES,
                         ('appointments', 'open', 'practices', 'search', 'tasks'))
        for name in ('send', 'email', 'calendar', 'notify', 'grant', 'write', 'sql'):
            self.assertNotIn(name, agent_tools.REGISTRY)

    def test_unknown_and_malformed_requests_fail_typed(self):
        for request in (None, 'search', [], {}, dict(tool='grant'),
                        dict(tool='send_email'), dict(tool='search'),
                        dict(tool='search', query='x', extra=1),
                        dict(tool='open', source_type='message'),
                        dict(tool='open', source_type='message', source_id='42'),
                        dict(tool='open', source_type='message', source_id=True),
                        dict(tool='open', source_type='sql', source_id=1),
                        dict(tool='search', query=''),
                        dict(tool='search', query='x' * 301),
                        dict(tool='tasks', extra=1)):
            with self.subTest(request=request):
                with self.assertRaises(agent_tools.ToolError):
                    agent_tools.run(request)

    def test_search_filters_results_through_real_consent_gate(self):
        allowed = self.source(42)
        denied = self.source(43, channel='whatsapp')
        self.patch('knowledge.search', return_value=[allowed, denied])
        result = agent_tools.run(dict(tool='search', query='tetto'))
        # Only the consented email source survives; the whatsapp one is dropped.
        self.assertEqual([r['source_id'] for r in result['results']], [42])
        self.assertEqual(len(result['results']), 1)

    def test_open_denied_source_yields_excluded_placeholder(self):
        denied = self.source(43, channel='whatsapp')
        self.patch('knowledge.open_source', return_value=denied)
        result = agent_tools.run(dict(tool='open', source_type='message', source_id=43))
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertEqual(result['source'].get('excluded'), dialogue.CONTEXT_OMITTED)

    def test_open_allowed_source_returns_filtered_text(self):
        allowed = self.source(42)
        self.patch('knowledge.open_source', return_value=allowed)
        result = agent_tools.run(dict(tool='open', source_type='message', source_id=42))
        self.assertEqual(result['source']['source_id'], 42)
        self.assertIn(CANARY, result['source']['text'])

    def test_tasks_and_appointments_filter_rows_per_call(self):
        task = dict(id=7, title='Pagare ' + CANARY)
        self.patch('db.pending_tasks', return_value=[task])
        self.patch('owner_dialogue.filter_context_rows',
                   side_effect=lambda rows, kind: list(rows) if kind == 'task' else [])
        result = agent_tools.run(dict(tool='tasks'))
        self.assertEqual(result['tasks'], [task])
        appt = dict(id=3, title='Dentista')
        self.patch('db.upcoming_appointments', return_value=[appt])
        self.patch('owner_dialogue.filter_context_rows',
                   side_effect=lambda rows, kind: list(rows) if kind == 'appointment' else [])
        result = agent_tools.run(dict(tool='appointments'))
        self.assertEqual(result['appointments'], [appt])

    def test_practices_snapshot_is_filtered(self):
        snapshot = dict(practices=[dict(id=1, objective='Tetto')],
                       responsibilities=[], excluded=dialogue.CONTEXT_OMITTED)
        self.patch('secretary.context', return_value=dict(practices=[], responsibilities=[],
                                                          truncated={}))
        self.patch('owner_dialogue.filtered_practices', return_value=snapshot)
        result = agent_tools.run(dict(tool='practices'))
        self.assertEqual(result['practices'], [dict(id=1, objective='Tetto')])

    def test_oversized_result_fails_typed(self):
        # 500 consented sources exceed the result budget: fail typed, not silent.
        huge = [self.source(42) for _ in range(500)]
        self.patch('knowledge.search', return_value=huge)
        with self.assertRaises(agent_tools.ToolError):
            agent_tools.run(dict(tool='search', query='tetto'))


class PlannerIntegrationTests(AgentToolTests):
    """retrieve() drives the registry; open stays allowlisted to surfaced IDs."""

    def setUp(self):
        super().setUp()
        self.patch('owner_dialogue.authorize')
        self.patch('owner_dialogue._current_sources', return_value=[])
        self.patch('knowledge.context', return_value=dict(query='', citations=[],
                                                          snippets=[], untrusted_sources=True))
        self.patch('secretary.context', return_value=dict(practices=[], responsibilities=[],
                                                          truncated={}))
        self.patch('owner_dialogue.filtered_practices',
                   return_value=dict(practices=[], responsibilities=[], excluded=''))
        self.search = self.patch('knowledge.search', return_value=[self.source(42)])
        self.opened = self.patch('knowledge.open_source', return_value=self.source(42))

    def retrieve(self, requests):
        with patch.object(brain, '_chat',
                          side_effect=[json.dumps(r) for r in requests]) as model:
            return dialogue.retrieve('Tetto', 101, []), model

    def test_new_tools_reachable_and_results_flow_to_planner_context(self):
        task = dict(id=7, title='Pagare il tetto')
        self.patch('db.pending_tasks', return_value=[task])
        self.patch('owner_dialogue.filter_context_rows',
                   side_effect=lambda rows, kind: list(rows) if kind == 'task' else [])
        (retrieved, _), model = self.retrieve([
            dict(tool='tasks'), dict(tool='search', query='tetto'), dict(tool='done')])
        self.assertEqual(len(retrieved['rounds']), 2)
        self.assertEqual(retrieved['rounds'][0]['result']['tasks'], [task])
        self.assertEqual(retrieved['rounds'][1]['result']['results'][0]['source_id'], 42)
        self.assertIn('untrusted DATA', model.call_args_list[0].args[0])

    def test_open_outside_surfaced_ids_stops_the_loop(self):
        (retrieved, _), _ = self.retrieve([
            dict(tool='open', source_type='message', source_id=999)])
        self.assertEqual(retrieved['rounds'], [])
        self.opened.assert_not_called()

    def test_open_of_surfaced_id_runs_and_is_recorded(self):
        (retrieved, _), _ = self.retrieve([
            dict(tool='search', query='tetto'),
            dict(tool='open', source_type='message', source_id=42),
            dict(tool='done')])
        self.assertEqual(len(retrieved['rounds']), 2)
        self.opened.assert_called_once_with('message', 42)

    def test_unknown_tool_request_stops_the_loop(self):
        (retrieved, _), _ = self.retrieve([dict(tool='grant', effect='ai_history',
                                                 allowed=True)])
        self.assertEqual(retrieved['rounds'], [])
        self.search.assert_not_called()


if __name__ == '__main__':
    unittest.main()
