"""Deleghe Notion: gating offline del tool e macchina a stati su PostgreSQL di test.

Offline: registrazione del tool (flag), rifiuto fuori dal modo owner, prompt.
Postgres (opt-in): attempt-before-I/O, dedupe, fast path, monitor durevole,
requires_action mai approvato, uncertain mai rigiocato, notifica unica.
"""
import unittest
from unittest.mock import patch

from secretary.core import config
from secretary.agent import prompts, toolkit
from secretary.connectors import notion_agent
from secretary.workflows import notion_delegations
from tests import support

SID = '123e4567-e89b-42d3-a456-426614174000'


def owner_state(text='Cerca la polizza casa su Notion'):
    return toolkit.TurnState('owner', None, 'owner:7', owner_mid=7, owner_text=text)


class ToolGatingTests(unittest.TestCase):
    """Nessun DB e nessuna rete: il tool esiste solo per l'owner e solo se configurato."""

    def notion_on(self):
        return patch.multiple(config, NOTION_ENABLED=True, NOTION_API_KEY='ntn_test', NOTION_AGENT_ID=SID)

    def test_tool_is_hidden_until_configured(self):
        with patch.multiple(config, NOTION_ENABLED=False, NOTION_API_KEY='', NOTION_AGENT_ID=''):
            self.assertNotIn('notion_ask', toolkit.names('owner'))
        with self.notion_on():
            self.assertIn('notion_ask', toolkit.names('owner'))

    def test_tool_is_never_available_to_inbox_or_digest(self):
        with self.notion_on():
            self.assertNotIn('notion_ask', toolkit.names('inbox'))
            self.assertNotIn('notion_ask', toolkit.names('digest'))

    def test_handler_refuses_non_owner_modes_before_any_io(self):
        state = toolkit.TurnState('inbox', None, 'inbox:1')
        with self.notion_on(), self.assertRaises(toolkit.ToolRefused):
            toolkit.t_notion_ask(state, toolkit.NotionAskArgs(question='cerca'))

    def test_owner_prompt_mentions_notion_only_when_enabled(self):
        conn = type('C', (), {'__enter__': lambda s: s, '__exit__': lambda s, *e: False,
                              'execute': lambda s, *a: type('R', (), {'fetchall': lambda r: []})()})()
        with patch.object(prompts.db, '_conn', return_value=conn), \
                patch.object(prompts.fsbox, 'read_text', side_effect=prompts.fsbox.FsError('assente')):
            with patch.multiple(config, NOTION_ENABLED=False, NOTION_API_KEY='', NOTION_AGENT_ID=''):
                self.assertNotIn('notion_ask', prompts.system_prompt('owner'))
            with self.notion_on():
                text = prompts.system_prompt('owner')
                self.assertIn('notion_ask', text)
                self.assertIn('SOLA LETTURA', text)


class DelegationTests(support.postgres_fixture()):
    """Macchina a stati reale sullo schema di test; client Notion sostituito."""

    def setUp(self):
        super().setUp()
        self.create = self.enterContext(patch.object(notion_agent, 'create_session'))
        self.follow = self.enterContext(patch.object(notion_agent, 'follow_up'))
        self.retrieve = self.enterContext(patch.object(notion_agent, 'retrieve_session'))
        self.messages = self.enterContext(patch.object(notion_agent, 'agent_messages'))
        self.create.return_value = {'id': SID, 'status': 'queued'}
        self.follow.return_value = {'id': SID, 'status': 'queued'}
        self.enterContext(patch.object(config, 'NOTION_FAST_WAIT_SECONDS', 0))
        self.enterContext(patch.object(notion_delegations.time, 'sleep', lambda s: None))

    def count_delegations(self):
        from secretary.core import db
        with db._conn() as conn:
            return conn.execute('SELECT count(*) FROM notion_delegations').fetchone()[0]

    def delegation(self, delegation_id):
        from secretary.core import db
        with db._conn() as conn:
            return conn.execute('SELECT status,answer,notion_session_id FROM notion_delegations WHERE id=%s',
                                (delegation_id,)).fetchone()

    def jobs(self, kind):
        from secretary.core import db
        with db._conn() as conn:
            return conn.execute('SELECT key,payload,status FROM work_jobs WHERE kind=%s', (kind,)).fetchall()

    def test_fast_path_completed_returns_the_answer(self):
        self.retrieve.return_value = {'id': SID, 'status': 'completed'}
        self.messages.return_value = ('La polizza è in Casa > Assicurazioni.', 4,
                                      ['https://www.notion.so/Polizza-abc'])
        result = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        self.assertEqual(result['status'], 'completed')
        self.assertIn('Polizza', result['links'][0])
        row = self.delegation(result['delegation_id'])
        self.assertEqual(row[0], 'completed')
        self.assertEqual(row[2], SID)
        self.assertEqual(self.jobs('notion'), [])  # conclusa nel turno: nessun monitor

    def test_slow_path_queues_a_durable_monitor(self):
        self.retrieve.return_value = {'id': SID, 'status': 'in_progress'}
        result = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        self.assertEqual(result['status'], 'queued')
        key, payload, _ = self.jobs('notion')[0]
        self.assertEqual(key, f"notion-poll:{result['delegation_id']}")
        self.assertEqual(payload['delegation_id'], result['delegation_id'])

    def test_monitor_completes_and_notifies_exactly_once(self):
        self.retrieve.return_value = {'id': SID, 'status': 'in_progress'}
        result = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        payload = {'delegation_id': result['delegation_id']}
        with self.assertRaises(notion_delegations.jobs.Deferred):
            notion_delegations.monitor(payload)  # ancora in corso: riprova più tardi
        self.retrieve.return_value = {'id': SID, 'status': 'completed'}
        self.messages.return_value = ('Trovata la polizza.', 9, [])
        notion_delegations.monitor(payload)
        self.assertEqual(self.delegation(result['delegation_id'])[0], 'completed')
        notify = self.jobs('action')
        self.assertEqual([n[0] for n in notify], [f"notion-result:{result['delegation_id']}"])
        self.assertIn('Trovata la polizza.', notify[0][1]['text'])
        self.assertEqual(notify[0][1]['evidence_message_id'], 7)
        notion_delegations.monitor(payload)  # retry dopo crash: la chiave stabile dedupica
        self.assertEqual(len(self.jobs('action')), 1)

    def test_repeated_identical_request_never_posts_twice(self):
        self.retrieve.return_value = {'id': SID, 'status': 'in_progress'}
        first = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        second = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        self.assertEqual(self.create.call_count, 1)
        self.assertEqual(first['delegation_id'], second['delegation_id'])
        self.assertEqual(second['status'], 'queued')
        self.assertEqual(len(self.jobs('notion')), 1)  # il monitor è riallacciato, non duplicato

    def test_ambiguous_submit_is_uncertain_and_never_resent(self):
        self.create.side_effect = notion_agent.NotionAmbiguous('TimeoutError')
        result = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(self.delegation(result['delegation_id'])[0], 'uncertain')
        again = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        self.assertEqual(again['status'], 'uncertain')
        self.assertEqual(self.create.call_count, 1)
        notion_delegations.monitor({'delegation_id': result['delegation_id']})
        self.assertEqual(self.jobs('action'), [])  # l'uncertain non notifica né riprova

    def test_definitive_4xx_fails_without_retry(self):
        self.create.side_effect = notion_agent.NotionError(404, 'object_not_found')
        result = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('object_not_found', result['error'])
        self.assertEqual(self.delegation(result['delegation_id'])[0], 'failed')
        self.assertEqual(self.create.call_count, 1)

    def test_requires_action_is_refused_never_approved(self):
        self.retrieve.return_value = {'id': SID, 'status': 'in_progress'}
        result = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        self.retrieve.return_value = {'id': SID, 'status': 'requires_action',
                                      'required_actions': [{'action_id': 'a1'}]}
        notion_delegations.monitor({'delegation_id': result['delegation_id']})
        self.assertEqual(self.delegation(result['delegation_id'])[0], 'refused')
        self.assertIn('sola lettura', self.jobs('action')[0][1]['text'])

    def test_expired_delegation_fails_and_notifies(self):
        self.retrieve.return_value = {'id': SID, 'status': 'in_progress'}
        result = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        with patch.object(config, 'NOTION_MAX_RUN_SECONDS', 0):
            notion_delegations.monitor({'delegation_id': result['delegation_id']})
        self.assertEqual(self.delegation(result['delegation_id'])[0], 'failed')
        self.assertIn('non si è conclusa in tempo', self.jobs('action')[0][1]['text'])

    def test_transient_poll_error_defers_without_failing(self):
        self.retrieve.return_value = {'id': SID, 'status': 'in_progress'}
        result = notion_delegations.delegate(owner_state(), 'Cerca la polizza casa')
        self.retrieve.side_effect = notion_agent.NotionError(429, 'rate_limited')
        with self.assertRaises(notion_delegations.jobs.Deferred):
            notion_delegations.monitor({'delegation_id': result['delegation_id']})
        self.assertEqual(self.delegation(result['delegation_id'])[0], 'running')
        self.retrieve.side_effect = notion_agent.NotionError(401, 'unauthorized')
        notion_delegations.monitor({'delegation_id': result['delegation_id']})
        self.assertEqual(self.delegation(result['delegation_id'])[0], 'failed')

    def test_continue_previous_resumes_the_last_completed_session(self):
        # Prima ricerca conclusa nel fast path: l'ultimo evento letto è il 4.
        self.retrieve.return_value = {'id': SID, 'status': 'completed'}
        self.messages.return_value = ('Prima risposta.', 4, [])
        first = notion_delegations.delegate(owner_state(), 'Cerca il contratto di X')
        self.assertEqual(first['status'], 'completed')
        self.create.reset_mock()
        self.messages.reset_mock()
        # Il proprietario insiste: il server riprende la STESSA sessione, dal turno 4.
        second = notion_delegations.delegate(owner_state('E quello di Y?'), 'E il contratto di Y?',
                                             continue_previous=True)
        self.assertEqual(second['status'], 'completed')
        self.create.assert_not_called()
        self.follow.assert_called_once_with(SID, 'E il contratto di Y?')
        self.assertEqual(self.messages.call_args.kwargs['after_sequence'], 4)
        self.assertEqual(self.count_delegations(), 2)

    def test_continue_previous_is_refused_while_the_previous_search_is_running(self):
        self.retrieve.return_value = {'id': SID, 'status': 'in_progress'}
        first = notion_delegations.delegate(owner_state(), 'Cerca il contratto di X')
        self.assertEqual(first['status'], 'queued')
        second = notion_delegations.delegate(owner_state('E quello di Y?'), 'E il contratto di Y?',
                                             continue_previous=True)
        self.assertEqual(second['status'], 'queued')
        self.assertIn('ancora in corso', second['error'])
        self.follow.assert_not_called()
        self.assertEqual(self.create.call_count, 1)  # nessuna seconda sessione
        self.assertEqual(self.count_delegations(), 1)

    def test_continue_previous_without_history_opens_a_new_session(self):
        self.retrieve.return_value = {'id': SID, 'status': 'completed'}
        self.messages.return_value = ('Ok.', 1, [])
        result = notion_delegations.delegate(owner_state(), 'Cerca il contratto di X',
                                             continue_previous=True)
        self.assertEqual(result['status'], 'completed')
        self.create.assert_called_once()
        self.follow.assert_not_called()


if __name__ == '__main__':
    unittest.main()
