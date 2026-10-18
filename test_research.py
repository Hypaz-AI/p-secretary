"""Offline contracts for the research decomposition worker.

No production database, network, workers or provider calls. The tool registry
runs for real with the strict SQL double; model outputs are scripted JSON.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

with tempfile.TemporaryDirectory(prefix='research-') as _bootstrap:
    with patch.dict(os.environ, {'PI_SECRETARY_CONFIG_FILE': '/dev/null',
                                 'DATA_DIR': _bootstrap, 'DATABASE_URL': ''}):
        import agent_tools
        import brain
        import db
        import jobs
        import research
        import test_history_context_policy as policy_helpers


CANARY = 'RESEARCH_CANARY_88f2'


class ResearchTests(unittest.TestCase):
    """Strict SQL double over the research tables; model calls are scripted."""

    def setUp(self):
        self.requests = {}
        self.findings = {}
        self.enqueued = []
        self.next_id = [1]
        self.conn = MagicMock(name='connection')
        self.conn.execute.side_effect = self.execute
        self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value = self.conn
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch.object(brain.ai_budget, 'request_content',
                                       side_effect=AssertionError('Provider forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        self.enterContext(patch.object(jobs, 'assert_current'))
        self.enterContext(patch.object(jobs, 'enqueue', side_effect=self._enqueue))

    def _enqueue(self, conn, kind, key, payload, **kwargs):
        self.enqueued.append((kind, key, payload))
        return SimpleNamespace(id=len(self.enqueued))

    def execute(self, sql, params=None):
        params = params or ()
        if sql.startswith('INSERT INTO research_requests'):
            identity = self.next_id[0]
            self.next_id[0] += 1
            self.requests[identity] = dict(id=identity, origin_message=params[0],
                                            question=params[1], status='pending',
                                            subtasks_total=0, report=None)
            return SimpleNamespace(fetchone=lambda: (identity,))
        if sql.startswith('SELECT question,status FROM research_requests'):
            row = self.requests.get(params[0])
            value = (row['question'], row['status']) if row else None
            return SimpleNamespace(fetchone=lambda: value, fetchall=lambda: [])
        if sql.startswith('SELECT status FROM research_requests'):
            row = self.requests.get(params[0])
            return SimpleNamespace(fetchone=lambda: (row['status'],) if row else None)
        if sql.startswith('SELECT status,report,question FROM research_requests'):
            row = self.requests.get(params[0])
            value = (row['status'], row['report'], row['question']) if row else None
            return SimpleNamespace(fetchone=lambda: value)
        if sql.startswith('SELECT subtasks_total FROM research_requests'):
            row = self.requests.get(params[0])
            return SimpleNamespace(fetchone=lambda: (row['subtasks_total'],) if row else None)
        if sql.startswith('UPDATE research_requests'):
            row = self.requests.get(params[-1] if isinstance(params[-1], int) else params[0])
            if row:
                if 'subtasks_total' in sql:
                    row['subtasks_total'] = params[0]
                elif 'report' in sql:
                    row['status'], row['report'] = 'done', params[0]
                elif 'failed' in sql:
                    row['status'] = 'failed'
                else:
                    row['status'] = params[0] if isinstance(params[0], str) else 'running'
            return SimpleNamespace(fetchone=lambda: None, rowcount=1)
        if sql.startswith('INSERT INTO research_findings'):
            identity, index, subtask, finding, citations = params
            self.findings[(identity, index)] = dict(finding=finding, citations=json.loads(citations))
            return SimpleNamespace(fetchone=lambda: None)
        if sql.startswith('SELECT count(*) FROM research_findings'):
            count = sum(1 for (rid, _) in self.findings if rid == params[0])
            return SimpleNamespace(fetchone=lambda: (count,))
        if sql.startswith('SELECT subtask,finding,citations FROM research_findings'):
            rows = [(f'q{i}', self.findings[(params[0], i)]['finding'],
                     self.findings[(params[0], i)]['citations'])
                    for i in range(6) if (params[0], i) in self.findings]
            return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: None)
        if sql.startswith('SELECT id,question,status,subtasks_total FROM research_requests'):
            return SimpleNamespace(fetchall=lambda: [])
        raise AssertionError('Unexpected SQL: ' + sql)

    def chat(self, *responses):
        return patch.object(brain, '_chat', side_effect=list(responses))

    def tool_result(self, mid=42):
        return dict(tool='search', query='tetto', results=[
            dict(source_type='message', source_id=mid, citation=f'message:{mid}@1',
                 historical=False, channel='email', text='Tetto ' + CANARY,
                 entry_id=None, documents=[])])


class RequestTests(ResearchTests):
    def test_request_queues_decompose_and_validates_inputs(self):
        identity = research.request(self.conn, 101, 'Quanto costerà il tetto?')
        self.assertEqual(identity, 1)
        self.assertEqual(self.enqueued[0][1], 'research-decompose:1')
        for bad in ('', 'x' * 2001, None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                research.request(self.conn, 101, bad)
        with self.assertRaises(ValueError):
            research.request(self.conn, '101', 'Domanda')

    def test_decompose_validates_model_output_and_queues_subtasks(self):
        self.requests[1] = dict(id=1, origin_message=101, question='Tetto?',
                                status='pending', subtasks_total=0, report=None)
        good = json.dumps(dict(subtasks=['Preventivi tetto', 'Fornitori tetto']))
        with self.chat(good):
            result = research.execute(dict(request_id=1, phase='decompose'))
        self.assertEqual(result['subtasks'], 2)
        self.assertEqual(self.requests[1]['status'], 'running')
        self.assertEqual(self.requests[1]['subtasks_total'], 2)
        keys = [key for _, key, _ in self.enqueued]
        self.assertIn('research-subtask:1:0', keys)
        self.assertIn('research-subtask:1:1', keys)
        for bad in ('{"subtasks":[]}', '{"subtasks":"x"}', '{"subtasks":["x"]*7}',
                    '{"other":1}', 'not json', '{"subtasks":["", "  "]}'):
            with self.subTest(bad=bad):
                self.requests[1]['status'] = 'pending'
                with self.chat(bad), self.assertRaises(ValueError):
                    research.execute(dict(request_id=1, phase='decompose'))

    def test_decompose_stops_when_request_not_pending(self):
        self.requests[1] = dict(id=1, origin_message=101, question='Tetto?',
                                status='done', subtasks_total=0, report='x')
        result = research.execute(dict(request_id=1, phase='decompose'))
        self.assertEqual(result['status'], 'already_processed')


class SubtaskTests(ResearchTests):
    def setUp(self):
        super().setUp()
        self.requests[1] = dict(id=1, origin_message=101, question='Tetto?',
                                status='running', subtasks_total=2, report=None)

    def test_subtask_runs_tools_and_persists_finding_with_real_citations(self):
        tool = json.dumps(dict(tool='search', query='tetto'))
        finding = json.dumps(dict(finding='Il preventivo è di 5000 euro.',
                                  citations=['message:42@1']))
        with self.chat(tool, finding, finding, finding), patch.object(agent_tools, 'run',
                                                     return_value=self.tool_result()):
            result = research.execute(dict(request_id=1, phase='subtask',
                                           subtask_index=0, subtask='Preventivi tetto'))
        self.assertEqual(result['status'], 'found')
        self.assertEqual(self.findings[(1, 0)]['finding'], 'Il preventivo è di 5000 euro.')
        self.assertEqual(self.findings[(1, 0)]['citations'], ['message:42@1'])

    def test_forged_citations_are_dropped_not_trusted(self):
        tool = json.dumps(dict(tool='search', query='tetto'))
        finding = json.dumps(dict(finding='Risposta.',
                                  citations=['message:999@1', 'invented:1@1']))
        with self.chat(tool, finding, finding, finding), patch.object(agent_tools, 'run',
                                                     return_value=self.tool_result()):
            research.execute(dict(request_id=1, phase='subtask',
                                  subtask_index=0, subtask='Preventivi tetto'))
        self.assertEqual(self.findings[(1, 0)]['citations'], [])

    def test_open_outside_surfaced_ids_stops_the_loop(self):
        request = json.dumps(dict(tool='open', source_type='message', source_id=999))
        finding = json.dumps(dict(finding='Nessuna evidenza.', citations=[]))
        with self.chat(request, finding, finding, finding), patch.object(agent_tools, 'run',
                                                       side_effect=AssertionError('Must not run')):
            result = research.execute(dict(request_id=1, phase='subtask',
                                           subtask_index=0, subtask='Preventivi tetto'))
        self.assertEqual(result['status'], 'found')

    def test_synthesize_queues_only_when_all_findings_arrived(self):
        finding = json.dumps(dict(finding='F1.', citations=[]))
        with self.chat(finding, finding, finding, finding), patch.object(agent_tools, 'run', return_value=self.tool_result()):
            research.execute(dict(request_id=1, phase='subtask',
                                  subtask_index=0, subtask='q0'))
        self.assertEqual([k for _, k, _ in self.enqueued], [])
        with self.chat(finding, finding, finding, finding), patch.object(agent_tools, 'run', return_value=self.tool_result()):
            research.execute(dict(request_id=1, phase='subtask',
                                  subtask_index=1, subtask='q1'))
        self.assertIn('research-synthesize:1', [k for _, k, _ in self.enqueued])

    def test_subtask_stops_when_request_not_running(self):
        self.requests[1]['status'] = 'stopped'
        result = research.execute(dict(request_id=1, phase='subtask',
                                       subtask_index=0, subtask='q0'))
        self.assertEqual(result['status'], 'stopped')


class SynthesizeTests(ResearchTests):
    def setUp(self):
        super().setUp()
        self.requests[1] = dict(id=1, origin_message=101, question='Tetto?',
                                status='running', subtasks_total=1, report=None)
        self.findings[(1, 0)] = dict(finding='Il preventivo è di 5000 euro.',
                                     citations=['message:42@1'])

    def test_synthesize_persists_bounded_report(self):
        report = json.dumps(dict(report='Il tetto costerà 5000 euro secondo il preventivo.'))
        with self.chat(report):
            result = research.execute(dict(request_id=1, phase='synthesize'))
        self.assertEqual(result['status'], 'done')
        self.assertEqual(self.requests[1]['status'], 'done')
        self.assertIn('5000', self.requests[1]['report'])

    def test_synthesize_rejects_malformed_reports(self):
        for bad in ('{"report":""}', '{"report":5}', '{"other":"x"}', 'no json'):
            with self.subTest(bad=bad), self.chat(bad), self.assertRaises(ValueError):
                research.execute(dict(request_id=1, phase='synthesize'))
        self.assertEqual(self.requests[1]['status'], 'running')

    def test_synthesize_without_findings_fails_explicitly(self):
        self.findings.clear()
        result = research.execute(dict(request_id=1, phase='synthesize'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.requests[1]['status'], 'failed')

    def test_unknown_phase_fails_typed(self):
        with self.assertRaises(ValueError):
            research.execute(dict(request_id=1, phase='grant', allowed=True))


if __name__ == '__main__':
    unittest.main()
