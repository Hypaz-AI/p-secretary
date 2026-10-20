"""Gateway LLM: chiave mai esposta, ledger fail-closed, parametri normalizzati."""
import http.client
import json
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import ai_budget
import config
import jobs
from agent import gateway


def context(job_id=7):
    return jobs.ExecutionContext(job_id, 'tok', 'owner', {'message_id': 5}, time.monotonic() + 60)


def body(**extra):
    payload = {'model': 'openrouter/auto', 'messages': [{'role': 'user', 'content': 'ciao'}],
               'temperature': 0.7, 'top_p': 1, 'frequency_penalty': 0, 'presence_penalty': 0,
               'tools': [{'type': 'function', 'function': {'name': 'x', 'parameters': {}}}]}
    payload.update(extra)
    return json.dumps(payload).encode()


OK = json.dumps({'model': 'vendor/m', 'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'cost': 0.0001}}).encode()


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.ledger = [], []
        self.status, self.data = 200, OK

        def upstream(payload):
            self.calls.append(payload)
            return self.status, self.data
        self.gw = gateway.Gateway(upstream=upstream)
        self.addCleanup(self.gw.stop)

        @contextmanager
        def accounted(payload, *, scope=None):
            outcome = {'response': None}
            self.ledger.append(dict(scope=scope, max_tokens=payload['max_tokens'],
                                    bound=jobs.execution_context()))
            yield outcome
            self.ledger[-1]['response'] = outcome['response']
        self.enterContext(patch.object(ai_budget, 'accounted', accounted))

    def test_unknown_or_closed_bearer_is_rejected_before_any_provider_call(self):
        self.assertEqual(self.gw.handle('nope', body())[0], 401)
        turn = self.gw.open_turn(context(), 'agent_owner')
        self.gw.close_turn(turn)
        self.assertEqual(self.gw.handle(turn.token, body())[0], 401)
        self.assertEqual(self.calls, [])

    def test_request_is_normalized_accounted_under_the_job_lease_and_forwarded(self):
        ctx = context()
        turn = self.gw.open_turn(ctx, 'agent_owner')
        status, data = self.gw.handle(turn.token, body())
        self.assertEqual(status, 200)
        self.assertEqual(data, OK)
        sent = self.calls[0]
        for dropped in ('temperature', 'top_p', 'frequency_penalty', 'presence_penalty', 'stream'):
            self.assertNotIn(dropped, sent)
        self.assertEqual(sent['max_tokens'], config.AGENT_MAX_OUTPUT_TOKENS)
        self.assertEqual(sent['model'], 'openrouter/auto')
        self.assertEqual(len(sent['tools']), 1)
        entry = self.ledger[0]
        self.assertEqual(entry['scope'], 'agent_owner')
        self.assertIs(entry['bound'], ctx)
        self.assertEqual(entry['response']['usage']['cost'], 0.0001)
        self.assertEqual(turn.model_calls, 1)
        self.assertIsNone(jobs.execution_context())  # il bind non trapela nel thread chiamante

    def test_output_budget_is_clamped_never_raised(self):
        turn = self.gw.open_turn(context(), 'agent_inbox')
        self.gw.handle(turn.token, body(max_tokens=10**9))
        self.gw.handle(turn.token, body(max_completion_tokens=100))
        self.gw.handle(turn.token, body(max_tokens='lots'))
        self.assertEqual([c['max_tokens'] for c in self.calls],
                         [config.AGENT_MAX_OUTPUT_TOKENS, 100, config.AGENT_MAX_OUTPUT_TOKENS])

    def test_client_cannot_inject_plugins_provider_routing_or_streaming(self):
        turn = self.gw.open_turn(context(), 'agent_owner')
        self.gw.handle(turn.token, body(plugins=[{'id': 'web'}], provider={'order': ['x']}, stream=True))
        sent = self.calls[0]
        for key in ('plugins', 'provider', 'stream'):
            self.assertNotIn(key, sent)

    def test_budget_exhausted_is_refused_without_calling_the_provider(self):
        for exc, code in ((ai_budget.BudgetExceeded('x'), 429), (ai_budget.BudgetUnavailable('x'), 503),
                          (jobs.LeaseLost('x'), 409)):
            @contextmanager
            def refuse(payload, *, scope=None, exc=exc):
                raise exc
                yield
            with patch.object(ai_budget, 'accounted', refuse):
                turn = self.gw.open_turn(context(), 'agent_owner')
                status, data = self.gw.handle(turn.token, body())
            self.assertEqual(status, code)
            self.assertIs(turn.failure, exc)
            self.assertNotIn(b'tok', data)
        self.assertEqual(self.calls, [])

    def test_provider_errors_are_passed_through_without_charging_a_result(self):
        self.status, self.data = 429, b'{"error":{"message":"rate limited"}}'
        turn = self.gw.open_turn(context(), 'agent_owner')
        status, data = self.gw.handle(turn.token, body())
        self.assertEqual((status, data), (429, self.data))
        self.assertIsNone(self.ledger[0]['response'])

    def test_network_failure_becomes_502_and_is_recorded(self):
        def broken(payload):
            raise OSError('connection reset by peer, key sk-secret')
        gw = gateway.Gateway(upstream=broken)
        turn = gw.open_turn(context(), 'agent_owner')
        status, data = gw.handle(turn.token, body())
        self.assertEqual(status, 502)
        self.assertNotIn(b'sk-secret', data)
        self.assertIsInstance(turn.failure, OSError)

    def test_malformed_requests_are_rejected(self):
        turn = self.gw.open_turn(context(), 'agent_owner')
        for raw in (b'not json', b'[]', b'{"messages":"x"}', b'{}'):
            self.assertEqual(self.gw.handle(turn.token, raw)[0], 400)
        self.assertEqual(self.calls, [])

    def test_scope_must_be_a_declared_agent_scope(self):
        with self.assertRaises(ValueError):
            self.gw.open_turn(context(), 'global')

    def test_loopback_server_enforces_path_and_bearer(self):
        url = self.gw.start()
        port = int(url.rsplit(':', 1)[1].split('/')[0])
        turn = self.gw.open_turn(context(), 'agent_owner')

        def post(path, token):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            headers = {'Content-Type': 'application/json'}
            if token is not None:
                headers['Authorization'] = 'Bearer ' + token
            conn.request('POST', path, body(), headers)
            response = conn.getresponse()
            result = response.status, response.read()
            conn.close()
            return result
        self.assertEqual(post('/v1/chat/completions', turn.token), (200, OK))
        self.assertEqual(post('/v1/chat/completions', 'wrong')[0], 401)
        self.assertEqual(post('/v1/chat/completions', None)[0], 401)
        self.assertEqual(post('/v1/other', turn.token)[0], 404)
        self.assertTrue(url.startswith('http://127.0.0.1:'))

    def test_concurrent_turns_are_isolated(self):
        results = {}

        def run(name, job_id):
            turn = self.gw.open_turn(context(job_id), 'agent_inbox')
            self.gw.handle(turn.token, body())
            results[name] = turn
        threads = [threading.Thread(target=run, args=(n, i)) for i, n in enumerate('abc', 1)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual({t.model_calls for t in results.values()}, {1})
        self.assertEqual(len({t.token for t in results.values()}), 3)
        self.assertEqual(sorted(e['bound'].job_id for e in self.ledger), [1, 2, 3])


if __name__ == '__main__':
    unittest.main()
