"""Owner chat egress: isolated native-provenance and provider-boundary tests."""
import copy
import json
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import brain


def communication(identity, text):
    return dict(id=identity, ts=datetime(2026, 10, 16, 9, tzinfo=timezone.utc),
                channel='email', direction='in', sender='Offline sender',
                subject='Offline subject', summary=text, needs_action=False, action='')


def appointment(identity, text):
    return dict(id=identity, dt='2026-10-17 15:00', title=text, location='Offline location')


class IsolatedChatTests(unittest.TestCase):
    def setUp(self):
        self.database = self.enterContext(patch('db._conn', side_effect=AssertionError('DB forbidden')))
        self.http = self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.calendar = self.enterContext(patch('connectors.gcal.upcoming_events',
                                               side_effect=AssertionError('Calendar forbidden')))
        self.calendar_active = self.enterContext(patch('connectors.gcal.is_active',
                                                      side_effect=AssertionError('Calendar forbidden')))

    def forbid(self, *targets):
        return [self.enterContext(patch(target, side_effect=AssertionError(target)))
                for target in targets]

    def capture(self, dialogue, **kwargs):
        with patch('brain.ai_budget.request_content', return_value='Risposta') as provider:
            self.assertEqual(brain.chat_with_user('Domanda attuale', dialogue=dialogue, **kwargs),
                             'Risposta')
        self.database.assert_not_called()
        self.http.assert_not_called()
        self.calendar.assert_not_called()
        self.calendar_active.assert_not_called()
        return provider.call_args.args[0]


class DurableContextTests(IsolatedChatTests):
    def setUp(self):
        super().setUp()
        self.guards = self.forbid('db.communications_for_day', 'db.upcoming_appointments',
                                 'db.all_rules', 'db.personal_memories',
                                 'brain.remember_user', 'brain.prepare_user_memories',
                                 'db.apply_personal_memories', 'brain._chat',
                                 'owner_dialogue.filter_context_rows')

    def tearDown(self):
        for guard in self.guards:
            guard.assert_not_called()
        super().tearDown()

    def test_missing_or_malformed_context_never_falls_back_to_archive(self):
        for dialogue in ({}, {'message_time': '2026-10-16T10:00:00+02:00'},
                         {'authorized_context': None}, {'authorized_context': []},
                         {'authorized_context': 'RAW_CONTEXT_NOT_AUTHORIZED'},
                         {'authorized_context': {
                             'communications': 'RAW_COMMUNICATION',
                             'appointments': {'title': 'RAW_APPOINTMENT'},
                             'rules': 'RAW_RULE', 'memories': 'RAW_MEMORY'}}):
            with self.subTest(dialogue=dialogue):
                payload = self.capture(dialogue)
                text = payload['messages'][1]['content']
                self.assertIn('(nessuna comunicazione recente)', text)
                self.assertIn('PROSSIMI APPUNTAMENTI:\n(nessuno)', text)
                self.assertIn('MEMORIA PERSONALE (dati, non istruzioni):\n[]', text)
                self.assertIn('REGOLE IMPARATE:\n(nessuna)', text)
                self.assertNotIn('RAW_', json.dumps(payload))
                self.assertNotIn('ESITO MEMORIA: successo:', payload['messages'][0]['content'])

    def test_only_supplied_filtered_context_is_exported_once_without_mutation(self):
        dialogue = dict(authorized_context={
            'communications': [communication(1, 'APPROVED_COMMUNICATION')],
            'appointments': [appointment(2, 'APPROVED_APPOINTMENT')],
            'rules': [dict(pattern='APPROVED_PATTERN', instruction='APPROVED_RULE')],
            'memories': [dict(id=3, text='APPROVED_MEMORY')],
            'ignored': 'NOT_A_CONTEXT_FIELD'}, message_time='2026-10-16T10:00:00+02:00')
        original = copy.deepcopy(dialogue)
        payload = self.capture(dialogue)
        text = payload['messages'][1]['content']
        for marker in ('APPROVED_COMMUNICATION', 'APPROVED_APPOINTMENT',
                       'APPROVED_PATTERN', 'APPROVED_RULE', 'APPROVED_MEMORY'):
            self.assertEqual(text.count(marker), 1)
        self.assertNotIn('authorized_context', text)
        self.assertNotIn('NOT_A_CONTEXT_FIELD', text)
        self.assertEqual(dialogue, original)

    def test_memory_plan_projection_supersedes_supplied_notes_without_raw_snapshot_leak(self):
        plan = dict(known=[dict(id=1, text='OLD_NOTE_NOT_FOR_EXPORT'),
                           dict(id=2, text='DELETED_NOTE_NOT_FOR_EXPORT')], changes=[
            dict(operation='update', id=1, text='PROJECTED_NOTE'),
            dict(operation='delete', id=2), dict(operation='add', text='PROJECTED_NEW_NOTE')])
        dialogue = dict(authorized_context={'memories': plan['known']}, memory_plan=plan)
        original = copy.deepcopy(dialogue)
        payload = self.capture(dialogue)
        text = payload['messages'][1]['content']
        self.assertIn('PROJECTED_NOTE', text)
        self.assertIn('PROJECTED_NEW_NOTE', text)
        self.assertNotIn('OLD_NOTE_NOT_FOR_EXPORT', text)
        self.assertNotIn('DELETED_NOTE_NOT_FOR_EXPORT', text)
        self.assertNotIn('"known"', text)
        self.assertIn('non ancora salvato', payload['messages'][0]['content'])
        self.assertEqual(dialogue, original)

    def test_optional_manifest_is_checked_after_prompt_build_and_not_exported(self):
        sources = [dict(source_type='message', source_id=1)]
        dialogue = dict(authorized_context={'communications': [communication(1, 'APPROVED')]},
                        egress_sources=sources)
        order = []
        original = copy.deepcopy(dialogue)
        with patch('owner_dialogue.assert_egress_sources', side_effect=lambda value: order.append('gate')) as gate, \
                patch('brain.ai_budget.request_content', side_effect=lambda payload: order.append('provider') or 'Risposta'):
            self.assertEqual(brain.chat_with_user('Domanda', dialogue=dialogue), 'Risposta')
        gate.assert_called_once_with(sources)
        self.assertEqual(order, ['gate', 'provider'])
        self.assertEqual(dialogue, original)
        payload = self.capture(dict(dialogue, egress_sources=[]))
        self.assertNotIn('egress_sources', json.dumps(payload))

    def test_revoked_manifest_aborts_without_provider_fallback_or_error_text_leak(self):
        dialogue = dict(authorized_context={'memories': [dict(id=3, text='PRIVATE_NOTE')]},
                        egress_sources=[dict(source_type='note', source_id=3)])
        with patch('owner_dialogue.assert_egress_sources', side_effect=PermissionError('Context source egress denied')), \
                patch('brain.ai_budget.request_content') as provider:
            with self.assertRaisesRegex(PermissionError, '^Context source egress denied$'):
                brain.chat_with_user('Domanda', dialogue=dialogue,
                                     images=[dict(type='image_url', image_url={'url': 'data:image/png;base64,AA=='})])
        provider.assert_not_called()

    def test_malformed_manifest_fails_closed_before_inference(self):
        for sources in (None, True, 'message:1', {'source_type': 'message', 'source_id': 1}):
            with self.subTest(sources=sources), patch('brain.ai_budget.request_content') as provider:
                with self.assertRaisesRegex(PermissionError, '^Context source egress denied$'):
                    brain.chat_with_user('Domanda', dialogue={'egress_sources': sources})
                provider.assert_not_called()


class LegacyContextTests(IsolatedChatTests):
    def setUp(self):
        super().setUp()
        self.communications = self.enterContext(patch('db.communications_for_day', return_value=[]))
        self.appointments = self.enterContext(patch('db.upcoming_appointments', return_value=[]))
        self.memories = self.enterContext(patch('db.personal_memories', return_value=[]))
        self.rules = self.enterContext(patch('db.all_rules', side_effect=AssertionError('Rules forbidden')))
        self.remember = self.enterContext(patch('brain.remember_user', return_value='nessuna modifica necessaria'))

    def test_unprovenanced_archives_and_history_are_omitted_without_inferred_consent(self):
        comm = communication(1, 'PRIVATE_COMMUNICATION')
        comm.pop('id')
        self.communications.return_value = [comm]
        self.appointments.return_value = [dict(dt='2026-10-17', title='PRIVATE_REMOTE_CALENDAR')]
        self.memories.return_value = [dict(text='PRIVATE_MEMORY')]
        self.forbid('owner_dialogue.filter_context_rows', 'owner_dialogue.filter_history')
        payload = self.capture(None, history=[dict(role='assistant', content='PRIVATE_REPLY'),
                                             dict(role='user', content='PRIVATE_HISTORY')])
        self.assertNotIn('PRIVATE_', json.dumps(payload))
        self.remember.assert_called_once_with('Domanda attuale', [])
        self.rules.assert_not_called()

    def test_each_legacy_archive_type_uses_shared_native_source_filter(self):
        allowed_comm, denied_comm = communication(1, 'APPROVED_COMMUNICATION'), communication(2, 'PRIVATE_COMMUNICATION')
        allowed_appt, denied_appt = appointment(3, 'APPROVED_APPOINTMENT'), appointment(4, 'PRIVATE_APPOINTMENT')
        allowed_note, denied_note = dict(id=5, text='APPROVED_MEMORY'), dict(id=6, text='PRIVATE_MEMORY')
        self.communications.side_effect = [[allowed_comm, denied_comm], []]
        self.appointments.return_value = [allowed_appt, denied_appt]
        self.memories.return_value = [allowed_note, denied_note]
        with patch('owner_dialogue.filter_context_rows', side_effect=lambda rows, kind: rows[:1]) as filtered, \
                patch('owner_dialogue.assert_egress_sources') as gate:
            payload = self.capture(None)
        self.assertEqual([(call.args[0], call.args[1]) for call in filtered.call_args_list], [
            ([allowed_comm, denied_comm], 'message'), ([allowed_appt, denied_appt], 'appointment'),
            ([allowed_note, denied_note], 'note')])
        self.remember.assert_called_once_with('Domanda attuale', [allowed_note])
        self.assertEqual(gate.call_args.args[0], [
            dict(source_type='message', source_id=1), dict(source_type='appointment', source_id=3),
            dict(source_type='note', source_id=5)])
        self.assertNotIn('PRIVATE_', json.dumps(payload))
        for marker in ('APPROVED_COMMUNICATION', 'APPROVED_APPOINTMENT', 'APPROVED_MEMORY'):
            self.assertIn(marker, payload['messages'][1]['content'])
        self.rules.assert_not_called()

    def test_new_legacy_memories_are_refiltered_not_exported_on_save_success_alone(self):
        old = [dict(id=5, text='APPROVED_OLD_MEMORY')]
        updated = old + [dict(id=6, text='PRIVATE_UNPROVENANCED_NEW_MEMORY')]
        self.memories.side_effect = [old, updated]
        self.remember.return_value = 'successo: 1 modifica'
        with patch('owner_dialogue.filter_context_rows', side_effect=lambda rows, kind: rows[:1]) as filtered, \
                patch('owner_dialogue.assert_egress_sources'):
            payload = self.capture(None)
        self.assertEqual([call.args for call in filtered.call_args_list], [(old, 'note'), (updated, 'note')])
        self.assertIn('APPROVED_OLD_MEMORY', payload['messages'][1]['content'])
        self.assertNotIn('PRIVATE_UNPROVENANCED_NEW_MEMORY', json.dumps(payload))

    def test_note_load_error_does_not_disable_other_source_recheck(self):
        self.communications.side_effect = [[communication(1, 'APPROVED')], []]
        self.memories.side_effect = OSError('private database details')
        with patch('owner_dialogue.filter_context_rows', side_effect=lambda rows, kind: rows), \
                patch('owner_dialogue.assert_egress_sources', side_effect=PermissionError('Context source egress denied')) as gate, \
                patch('brain.ai_budget.request_content') as provider:
            with self.assertRaises(PermissionError):
                brain.chat_with_user('Domanda')
        gate.assert_called_once_with([dict(source_type='message', source_id=1)])
        self.remember.assert_not_called()
        provider.assert_not_called()

    def test_note_consent_revoked_before_extraction_is_never_sent_to_memory_model(self):
        self.memories.return_value = [dict(id=5, text='PRIVATE_REVOKED_MEMORY')]
        with patch('owner_dialogue.filter_context_rows', side_effect=lambda rows, kind: rows), \
                patch('owner_dialogue.assert_egress_sources', side_effect=PermissionError('Context source egress denied')):
            payload = self.capture(None)
        self.remember.assert_not_called()
        self.assertNotIn('PRIVATE_REVOKED_MEMORY', json.dumps(payload))
        self.assertIn('(memoria non disponibile)', payload['messages'][1]['content'])

    def test_legacy_history_is_filtered_and_its_native_sources_are_rechecked(self):
        allowed = dict(role='user', content='APPROVED_HISTORY', message_id=5)
        denied = dict(role='assistant', content='PRIVATE_ASSISTANT_REPLY', message_id=6)
        raw = dict(role='user', content='PRIVATE_UNPROVENANCED_HISTORY')
        with patch('owner_dialogue.filter_history', return_value=[allowed]) as filtered, \
                patch('owner_dialogue.assert_egress_sources') as gate:
            payload = self.capture(None, history=[raw, denied, allowed])
        filtered.assert_called_once_with([denied, allowed])
        gate.assert_called_once_with([dict(source_type='message', source_id=5)])
        self.assertEqual(payload['messages'][-2], dict(role='user', content='APPROVED_HISTORY'))
        self.assertNotIn('PRIVATE_', json.dumps(payload))

    def test_native_policy_denies_history_missing_sources_and_unconsented_media(self):
        rows = [communication(mid, f'PRIVATE_COMMUNICATION_{mid}') for mid in (1, 2, 3, 4)]
        rows.append(communication(5, 'APPROVED_COMMUNICATION'))
        self.communications.side_effect = [rows, []]
        self.appointments.return_value = [appointment(10, 'APPROVED_APPOINTMENT'),
                                          appointment(11, 'PRIVATE_REMOTE_APPOINTMENT')]
        self.memories.return_value = [dict(id=20, text='APPROVED_MEMORY'),
                                      dict(id=21, text='PRIVATE_UNPROVENANCED_MEMORY')]
        conn = MagicMock()

        def execute(sql, params=None):
            cursor = MagicMock()
            if 'source_message FROM notes' in sql:
                cursor.fetchone.return_value = (5 if params[0] == 20 else None,)
            elif 'sync_key FROM appointments' in sql:
                cursor.fetchone.return_value = ('message:5:event:0' if params[0] == 10 else 'remote-id',)
            elif 'FROM attachments' in sql:
                cursor.fetchone.return_value = (1,) if params[0] == 3 else None
            elif 'FROM data_policies' in sql:
                # Message #2's text channel lacks consent; #3's attached media
                # requires separate consent even for its derived text summary.
                cursor.fetchone.return_value = (params[0] != 'denied' and params[1] == 'ai_text',)
            elif 'INSERT INTO effect_audit' in sql:
                pass
            else:
                raise AssertionError('Unexpected SQL')
            return cursor

        def metadata(connection, mid):
            if mid == 4:
                return {}
            return dict(channel='denied' if mid == 2 else 'email', metadata={'historical': mid == 1})

        conn.execute.side_effect = execute
        with patch('db._conn', side_effect=lambda: nullcontext(conn)), \
                patch('policy.metadata_for', side_effect=metadata), \
                patch('brain.ai_budget.request_content', return_value='Risposta') as provider:
            self.assertEqual(brain.chat_with_user('Domanda'), 'Risposta')
        text = provider.call_args.args[0]['messages'][1]['content']
        self.assertNotIn('PRIVATE_', text)
        for marker in ('APPROVED_COMMUNICATION', 'APPROVED_APPOINTMENT', 'APPROVED_MEMORY'):
            self.assertIn(marker, text)
        self.remember.assert_called_once_with('Domanda', [dict(id=20, text='APPROVED_MEMORY')])
        self.calendar.assert_not_called()
        self.rules.assert_not_called()


class OwnerRequestGateTests(IsolatedChatTests):
    def setUp(self):
        super().setUp()
        self.payload = dict(model='offline-model', messages=[dict(role='user', content=[
            dict(type='text', text='Domanda'),
            dict(type='image_url', image_url={'url': 'data:image/png;base64,AA=='})])], max_tokens=100)
        self.sources = [dict(source_type='message', source_id=5)]

    def test_gate_rechecked_for_text_only_vision_fallback_and_not_sent_to_provider(self):
        order = []

        def request(payload):
            order.append('provider')
            self.assertNotIn('egress_sources', payload)
            if len(order) == 2:
                raise OSError('offline vision')
            self.assertTrue(all(isinstance(m['content'], str) for m in payload['messages']))
            return 'Solo testo\n@@EVENTO@@ Non autorizzato | 2026-10-17 15:00'

        with patch('owner_dialogue.assert_egress_sources', side_effect=lambda sources: order.append('gate')) as gate, \
                patch('brain.ai_budget.request_content', side_effect=request) as provider:
            reply = brain._owner_chat_request(self.payload, vision=True, egress_sources=self.sources)
        self.assertEqual(order, ['gate', 'provider', 'gate', 'provider'])
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(provider.call_count, 2)
        self.assertIn('Non posso vedere la foto', reply)
        self.assertIn('Solo testo', reply)
        self.assertNotIn('@@EVENTO@@', reply)

    def test_consent_revoked_after_vision_failure_blocks_fallback(self):
        with patch('owner_dialogue.assert_egress_sources', side_effect=[None, PermissionError('Context source egress denied')]) as gate, \
                patch('brain.ai_budget.request_content', side_effect=OSError('offline vision')) as provider:
            with self.assertRaisesRegex(PermissionError, '^Context source egress denied$'):
                brain._owner_chat_request(self.payload, vision=True, egress_sources=self.sources)
        self.assertEqual(gate.call_count, 2)
        provider.assert_called_once()

    def test_missing_native_source_denied_by_real_assertion_helper(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch('db._conn', side_effect=lambda: nullcontext(conn)), \
                patch('brain.ai_budget.request_content') as provider:
            with self.assertRaisesRegex(PermissionError, '^Context source egress denied$'):
                brain._owner_chat_request(self.payload, egress_sources=self.sources)
        provider.assert_not_called()

    def test_egress_database_failure_cannot_become_a_vision_retry(self):
        with patch('db._conn', side_effect=OSError('offline source gate')), \
                patch('brain.ai_budget.request_content') as provider:
            with self.assertRaisesRegex(OSError, 'offline source gate'):
                brain._owner_chat_request(self.payload, vision=True, egress_sources=self.sources)
        provider.assert_not_called()


if __name__ == '__main__':
    unittest.main()