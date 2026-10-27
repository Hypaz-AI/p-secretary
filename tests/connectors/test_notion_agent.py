"""Client Notion Agent APIs testato offline: trasporto finto, nessuna rete.

Si verificano il contratto degli endpoint, la distinzione tra errore definitivo
(risposta HTTP) ed esito ambiguo (timeout/rete) e l'estrazione dei soli eventi
`agent.message` nuovi, con paginazione.
"""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from secretary.core import config
from secretary.connectors import notion_agent

SID = '123e4567-e89b-42d3-a456-426614174000'


def http_response(payload, status=200):
    resp = io.BytesIO(json.dumps(payload).encode())
    resp.status = status
    return resp


def http_error(status, payload=None):
    body = json.dumps(payload or {'error': {'code': 'unauthorized'}}).encode()
    return urllib.error.HTTPError('https://api.notion.com/v1/x', status, 'err', {}, io.BytesIO(body))


class FakeTransport:
    """urlopen finto: risponde in sequenza e registra le richieste."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(config, 'NOTION_API_KEY', 'ntn_test'))
        self.enterContext(patch.object(config, 'NOTION_AGENT_ID', SID))
        self.enterContext(patch.object(config, 'NOTION_HTTP_TIMEOUT_SECONDS', 5))

    def test_create_session_posts_agent_and_message_with_beta_headers(self):
        transport = FakeTransport(http_response({'id': SID, 'status': 'queued'}))
        result = notion_agent.create_session('Cerca la polizza casa', opener=transport)
        req = transport.requests[0]
        self.assertEqual(req.full_url, 'https://api.notion.com/v1/sessions')
        self.assertEqual(req.headers['Notion-version'], '2026-03-11')
        self.assertEqual(req.headers['Authorization'], 'Bearer ntn_test')
        self.assertEqual(json.loads(req.data), {'agent_id': SID, 'message': 'Cerca la polizza casa'})
        self.assertEqual(result['status'], 'queued')

    def test_create_session_rejects_an_unverifiable_session_id(self):
        transport = FakeTransport(http_response({'id': '../../etc/passwd', 'status': 'queued'}))
        with self.assertRaises(notion_agent.NotionAmbiguous):
            notion_agent.create_session('x', opener=transport)

    def test_follow_up_posts_to_the_same_session(self):
        transport = FakeTransport(http_response({'id': SID, 'status': 'queued'}))
        notion_agent.follow_up(SID, 'Intendo il contratto di affitto', opener=transport)
        req = transport.requests[0]
        self.assertEqual(req.full_url, 'https://api.notion.com/v1/sessions')
        self.assertEqual(json.loads(req.data),
                         {'session_id': SID, 'message': 'Intendo il contratto di affitto'})

    def test_follow_up_validates_the_session_id_before_any_request(self):
        transport = FakeTransport(http_response({'id': SID}))
        with self.assertRaises(notion_agent.NotionAmbiguous):
            notion_agent.follow_up('not-a-session', 'x', opener=transport)
        self.assertEqual(transport.requests, [])

    def test_http_4xx_is_a_definitive_error(self):
        transport = FakeTransport(http_error(401))
        with self.assertRaises(notion_agent.NotionError) as ctx:
            notion_agent.create_session('x', opener=transport)
        self.assertEqual(ctx.exception.status, 401)
        self.assertFalse(ctx.exception.retryable)

    def test_http_429_is_retryable(self):
        transport = FakeTransport(http_error(429, {'error': {'code': 'rate_limited'}}))
        with self.assertRaises(notion_agent.NotionError) as ctx:
            notion_agent.retrieve_session(SID, opener=transport)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.code, 'rate_limited')

    def test_network_failure_is_ambiguous_never_retried_by_transport(self):
        transport = FakeTransport(urllib.error.URLError('boom'))
        with self.assertRaises(notion_agent.NotionAmbiguous):
            notion_agent.create_session('x', opener=transport)
        self.assertEqual(len(transport.requests), 1)

    def test_retrieve_session_validates_the_id_before_building_the_url(self):
        transport = FakeTransport(http_response({'status': 'completed'}))
        with self.assertRaises(notion_agent.NotionAmbiguous):
            notion_agent.retrieve_session('1 OR 1=1', opener=transport)
        self.assertEqual(transport.requests, [])

    def test_agent_messages_paginates_and_keeps_only_new_agent_messages(self):
        page1 = {'results': [
            {'type': 'agent.thinking', 'sequence': 1, 'content': [{'type': 'text', 'text': 'penso'}]},
            {'type': 'agent.message', 'sequence': 2,
             'content': [{'type': 'text', 'text': 'Prima risposta'}]},
        ], 'has_more': True, 'next_cursor': 'c2'}
        page2 = {'results': [
            {'type': 'agent.message', 'sequence': 5,
             'content': [{'type': 'text', 'text': 'Trovata: '},
                         {'type': 'text', 'text': 'https://www.notion.so/Pagina-abc'}]},
        ], 'has_more': False}
        transport = FakeTransport(http_response(page1), http_response(page2))
        answer, last, links = notion_agent.agent_messages(SID, after_sequence=2, opener=transport)
        self.assertEqual(answer, 'Trovata: https://www.notion.so/Pagina-abc')
        self.assertEqual(last, 5)
        self.assertEqual(links, ['https://www.notion.so/Pagina-abc'])
        body2 = json.loads(transport.requests[1].data)
        self.assertEqual(body2['start_cursor'], 'c2')
        self.assertEqual(body2['filter']['event_type'], {'equals': 'agent.message'})

    def test_agent_messages_caps_pagination(self):
        page = {'results': [], 'has_more': True, 'next_cursor': 'x'}
        transport = FakeTransport(*(http_response(page) for _ in range(10)))
        notion_agent.agent_messages(SID, opener=transport)
        self.assertEqual(len(transport.requests), notion_agent.MAX_EVENT_PAGES)


if __name__ == '__main__':
    unittest.main()
