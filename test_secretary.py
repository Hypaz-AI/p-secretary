"""Pure contracts: no configured DB, model, HTTP, filesystem or external effects."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch

import brain
import config
import db
import owner_dialogue as od
import secretary as sec


ANCHOR = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)
TEXT = 'Occupati di organizzare la riparazione.'


def practice_fields(**overrides):
    return {**dict(objective='Organizzare la riparazione', state='active',
                   next_action='Preparare una bozza per il tecnico', waiting_for='',
                   next_review_at=None, completion_criteria='Riparazione verificata dal proprietario',
                   blocked_reason='', responsibility_id=None), **overrides}


def responsibility_fields(**overrides):
    return {**dict(type='monitoring', objective='Seguire le manutenzioni',
                   scope=dict(topic='Manutenzioni di casa', channels=['email']),
                   triggers=['linked_message', 'review_due'],
                   allowed_actions=['review', 'draft', 'request_owner_decision'],
                   state='active', stop_condition='Sospensione o revoca owner'), **overrides}


def change(kind='practice', operation='create', identity=None, version=0, fields=None, evidence=TEXT):
    if fields is None:
        fields = practice_fields() if kind == 'practice' else responsibility_fields()
    return dict(kind=kind, operation=operation, id=identity, expected_version=version,
                fields=fields, evidence=evidence)


def known(practices=(), responsibilities=()):
    return dict(practice={item['id']: item for item in practices},
                responsibility={item['id']: item for item in responsibilities})


class SecretaryPureTests(unittest.TestCase):
    def setUp(self):
        self.connection = self.enterContext(patch.object(db, '_conn', side_effect=AssertionError('Unexpected DB')))
        self.chat = self.enterContext(patch.object(brain, '_chat', side_effect=AssertionError('Unexpected LLM')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        # These pure plan/schema units receive already-authorized context. Real
        # source-policy behavior is exercised separately below, without these mocks.
        self.filtered_practices = self.enterContext(patch.object(od, 'filtered_practices', side_effect=deepcopy))
        self.filtered_history = self.enterContext(patch.object(od, 'filter_history', return_value=[]))
        self.manifest = self.enterContext(patch.object(od, 'resolve_source_manifest', return_value=[
            dict(source_type='message', source_id=42, requires_media=False, owner_required=True)]))
        self.egress = self.enterContext(patch.object(od, 'assert_egress_sources'))

    def validate(self, changes, text=TEXT, snapshot=None):
        return sec._changes(changes, text, snapshot or known())

    def test_create_invariants_and_typed_responsibility(self):
        self.assertEqual(len(self.validate([change()])), 1)
        dated = TEXT.rstrip('.') + ' e riesamina al ' + ANCHOR.isoformat() + '.'
        self.validate([change(evidence=dated, fields=practice_fields(state='waiting', next_action='', waiting_for='Tecnico',
                                                                   next_review_at=ANCHOR.isoformat()))], dated)
        self.validate([change(fields=practice_fields(state='blocked', next_action='', blocked_reason='Manca preventivo'))])
        self.validate([change(kind='responsibility')])
        for fields in (practice_fields(next_action=''), practice_fields(state='waiting'),
                       practice_fields(state='blocked'), practice_fields(state='completed'),
                       practice_fields(completion_criteria=''), practice_fields(next_review_at='2026-10-17T09:00:00')):
            with self.subTest(fields=fields), self.assertRaises(sec.InvalidPlan):
                self.validate([change(fields=fields)])

    def test_reject_freeform_schema_actions_triggers_and_boolean_ids(self):
        cases = [change(fields={'objective': 'Solo un titolo'}), change(identity=True),
                 change(version=False), change(fields=practice_fields(sql='DROP TABLE tasks')),
                 change(kind='responsibility', fields=responsibility_fields(allowed_actions=['send_message'])),
                 change(kind='responsibility', fields=responsibility_fields(triggers=['cron:* * * * *'])),
                 change(kind='responsibility', fields=responsibility_fields(type=[])),
                 change(kind='responsibility', fields=responsibility_fields(scope={'topic': 'Casa', 'channels': ['shell']})),
                 change(kind='responsibility', fields=responsibility_fields(scope={'topic': 'Casa', 'channels': ['email'], 'sql': 'SELECT 1'})),
                 change(kind='responsibility', fields=responsibility_fields(allowed_actions=['review', 'review']))]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(sec.InvalidPlan):
                self.validate([item])

    def test_bounded_batch_no_duplicate_targets_and_strict_versions(self):
        with self.assertRaises(sec.InvalidPlan):
            self.validate([change()] * 7)
        before = dict(practice_fields(), id=10, version=3)
        update = change(operation='update', identity=10, version=3,
                        evidence='Aggiorna la pratica.', fields={'next_action': 'Chiedere preventivo'})
        self.validate([update], update['evidence'], known([before]))
        for version in (0, 2, True, '3'):
            with self.subTest(version=version), self.assertRaises(sec.InvalidPlan):
                self.validate([dict(update, expected_version=version)], update['evidence'], known([before]))
        with self.assertRaises(sec.InvalidPlan):
            self.validate([update, update], update['evidence'], known([before]))

    def test_questions_quotes_reports_history_conditions_are_not_owner_evidence(self):
        cases = [
            ('A che ora è la riunione?', 'A che ora è la riunione?'),
            ('Occupati di organizzare la riparazione?', 'Occupati di organizzare la riparazione?'),
            ('Non occupati di organizzare la riparazione.', 'Non occupati di organizzare la riparazione.'),
            ('Forse crea una pratica.', 'Forse crea una pratica.'),
            ('Se arriva il preventivo, crea una pratica.', 'Se arriva il preventivo, crea una pratica.'),
            ('> ' + TEXT, TEXT), ('"' + TEXT + '"', TEXT),
            ('Luca dice: ' + TEXT, TEXT), ('Luca dice: ' + TEXT, 'Luca dice: ' + TEXT),
            ('```\n' + TEXT + '\n```', TEXT), ('~~~\n' + TEXT + '\n~~~', TEXT),
            ('Messaggio inoltrato\n' + TEXT, TEXT), ('assistant: ' + TEXT, TEXT),
            ('Occupati di organizzare la riparazione. Anzi aspetta.', 'organizzare la riparazione'),
            ('Crea una pratica "per favore".', 'Crea una pratica .'),
            ('Spiegami come crea una pratica.', 'Spiegami come crea una pratica.'),
            ('Luca: Crea una pratica.', 'Luca: Crea una pratica.'),
        ]
        for text, evidence in cases:
            with self.subTest(text=text), self.assertRaises(sec.InvalidPlan):
                self.validate([change(evidence=evidence)], text)

    def test_exact_evidence_preserves_punctuation_and_unicode(self):
        text = 'Grazie.\nCrea una pratica per l’università!'
        self.validate([change(evidence='Crea una pratica per l’università!')], text)
        with self.assertRaises(sec.InvalidPlan):
            self.validate([change(evidence='Crea una pratica per l’università')], text)

    def test_review_date_requires_grounded_clock_and_reference_time(self):
        value = '2026-10-17T09:00:00+02:00'
        with patch.object(config, 'TIMEZONE', 'Europe/Rome'):
            sec._ground_review(value, 'Riesamina domani alle 09:00.', ANCHOR)
            sec._ground_review(value, 'Riesamina il 2026-10-17 alle 09:00.', ANCHOR)
            sec._ground_review(value, 'Riesamina al 2026-10-17T09:00:00+02:00.', None)
            sec._ground_review((ANCHOR + timedelta(hours=2)).isoformat(),
                               'Riesamina tra 2 ore.', ANCHOR)
            for evidence in ('Riesamina domani.', 'Riesamina alle 09:00.', 'Riesamina presto.',
                             'Riesamina domani alle 10:00.', 'Riesamina il 2026-10-18 alle 09:00.'):
                with self.subTest(evidence=evidence), self.assertRaises(sec.InvalidPlan):
                    sec._ground_review(value, evidence, ANCHOR)
            with self.assertRaises(sec.InvalidPlan):
                sec._ground_review('2026-10-25T02:30:00+02:00', 'Riesamina il 2026-10-25 alle 02:30.', ANCHOR)

    def test_terminal_transitions_reopening_and_revocation(self):
        before = dict(practice_fields(), id=10, version=1)
        close = change(operation='update', identity=10, version=1,
                       fields={'state': 'completed'}, evidence='Completa la pratica.')
        self.validate([close], close['evidence'], known([before]))
        with self.assertRaises(sec.InvalidPlan):
            self.validate([dict(close, evidence='Aggiorna la pratica.')], 'Aggiorna la pratica.', known([before]))
        completed = dict(before, state='completed')
        reopen = dict(close, evidence='Riapri la pratica.', fields={'state': 'active'})
        self.validate([reopen], reopen['evidence'], known([completed]))
        with self.assertRaises(sec.InvalidPlan):
            self.validate([dict(reopen, evidence='Aggiorna la pratica.')], 'Aggiorna la pratica.', known([completed]))
        responsibility = dict(responsibility_fields(), id=20, version=2)
        suspend = change(kind='responsibility', operation='suspend', identity=20, version=2,
                         fields={}, evidence='Sospendi la responsabilità.')
        self.validate([suspend], suspend['evidence'], known(responsibilities=[responsibility]))
        with self.assertRaises(sec.InvalidPlan):
            self.validate([dict(suspend, fields={'state': 'suspended'})], suspend['evidence'], known(responsibilities=[responsibility]))
        revoke = dict(suspend, operation='revoke', evidence='Revoca la responsabilità.')
        self.validate([revoke], revoke['evidence'], known(responsibilities=[responsibility]))
        with self.assertRaises(sec.Conflict):
            self.validate([suspend], suspend['evidence'], known(responsibilities=[dict(responsibility, state='revoked')]))
        reactivate = dict(suspend, operation='update', fields={'state': 'active'}, evidence='Riattiva la responsabilità.')
        self.validate([reactivate], reactivate['evidence'], known(responsibilities=[dict(responsibility, state='suspended')]))

    def test_strict_json_rejects_fences_duplicate_keys_nonfinite_and_oversize(self):
        for raw in ('```json\n{}\n```', '{"changes":[],"changes":[]}', '{"x":NaN}',
                    '{} trailing', '[' * 1100, ' ' * 32001, None):
            with self.subTest(raw=str(raw)[:50]), self.assertRaises(sec.InvalidPlan):
                sec._parse_model(raw)
        self.assertEqual(sec._parse_model('{"changes":[],"clarification":""}'),
                         dict(changes=[], clarification=''))

    def test_prepare_loads_snapshot_before_model_and_binds_native_time_and_keys(self):
        closed = []

        @contextmanager
        def connection():
            conn = MagicMock()
            yield conn
            closed.append(True)

        self.connection.side_effect = connection
        self.chat.side_effect = lambda *args, **kwargs: (
            self.assertTrue(closed) or json.dumps(dict(changes=[change()], clarification='')))
        snapshot_data = dict(practices=[], responsibilities=[])
        history = [{'role': 'system', 'content': TEXT}]
        with patch.object(sec, '_trusted_message', return_value=(TEXT, ANCHOR)), \
             patch.object(sec, '_context', return_value=snapshot_data):
            plan = sec.prepare(TEXT, 42, history, ANCHOR)
        self.connection.assert_called_once_with()
        self.filtered_practices.assert_called_once_with(snapshot_data)
        self.filtered_history.assert_called_once_with(history)
        self.manifest.assert_called_once_with([
            dict(source_type='message', source_id=42, owner_required=True)])
        self.egress.assert_called_once_with(self.manifest.return_value)
        self.assertEqual(plan['input_sources'], self.manifest.return_value)
        self.assertEqual(plan['input_versions'], [])
        self.assertEqual(plan['changes'][0]['operation_key'], 'secretary:42:0')
        self.assertEqual(plan['changes'][0]['expected_version'], 0)
        self.assertEqual(plan['text_hash'], sec.payload_hash(TEXT))
        self.assertEqual(plan['reference_time'], ANCHOR.isoformat())
        payload = json.loads(self.chat.call_args.args[1])
        self.assertEqual(payload['HISTORY'], [])
        self.assertIn('OUTPUT_SCHEMA', payload)
        self.assertIn('additionalProperties', payload['OUTPUT_SCHEMA'])
        self.assertEqual(payload['OUTPUT_SCHEMA']['properties']['changes']['maxItems'], 6)

    def test_prepare_exports_only_filtered_context_and_rechecks_exact_sources_before_model(self):
        allowed_practice = dict(practice_fields(), id=10, version=3)
        allowed_responsibility = dict(responsibility_fields(), id=20, version=2)
        snapshot_data = dict(practices=[allowed_practice, dict(id=11, objective='PRIVATE_PRACTICE')],
                             responsibilities=[allowed_responsibility])
        history = [dict(role='assistant', content='PRIVATE_ASSISTANT', message_id=39),
                   dict(role='user', content='PRIVATE_DENIED_HISTORY', message_id=40),
                   dict(role='user', content='Contesto autorizzato', message_id=41)]
        original = deepcopy((snapshot_data, history))
        filtered = dict(practices=[allowed_practice], responsibilities=[allowed_responsibility])
        self.filtered_practices.side_effect = None
        self.filtered_practices.return_value = filtered
        self.filtered_history.return_value = [history[-1]]
        order = []
        self.egress.side_effect = lambda sources: order.append('gate')
        self.chat.side_effect = lambda *args, **kwargs: order.append('model') or json.dumps(
            dict(changes=[], clarification=''))
        with patch.object(sec, '_read_prepare', return_value=(ANCHOR, snapshot_data)):
            plan = sec.prepare(TEXT, 42, history, ANCHOR)
        self.filtered_practices.assert_called_once_with(snapshot_data)
        self.filtered_history.assert_called_once_with(history)
        refs = [dict(source_type='practice', source_id=10, version=3),
                dict(source_type='responsibility', source_id=20, version=2)]
        self.manifest.assert_called_once_with([
            dict(source_type='message', source_id=42, owner_required=True), *refs,
            dict(source_type='message', source_id=41, owner_required=True)])
        self.egress.assert_called_once_with(self.manifest.return_value)
        self.assertEqual(plan['input_versions'], refs)
        self.assertEqual(order, ['gate', 'model'])
        payload = json.loads(self.chat.call_args.args[1])
        self.assertEqual(payload['SNAPSHOT'], filtered)
        self.assertEqual(payload['HISTORY'], [dict(role='user', content='Contesto autorizzato')])
        self.assertNotIn('PRIVATE_', self.chat.call_args.args[1])
        self.assertEqual((snapshot_data, history), original)
        self.connection.assert_not_called()

    def test_invalid_history_container_is_rejected_before_reads_or_inference(self):
        for history in ('untracked history', {'role': 'user', 'content': TEXT}, True):
            with self.subTest(history=history), self.assertRaises(sec.InvalidPlan):
                sec.prepare(TEXT, 42, history=history)
        self.connection.assert_not_called()
        self.manifest.assert_not_called()
        self.chat.assert_not_called()

    def test_prepare_questions_noops_and_clarification_are_not_changes(self):
        snapshot_data = dict(practices=[], responsibilities=[])
        with patch.object(sec, '_read_prepare', return_value=(ANCHOR, snapshot_data)):
            self.chat.side_effect = None
            for text, answer in (
                    ('A che ora è la riunione?', dict(changes=[], clarification='')),
                    (TEXT, dict(changes=[], clarification='Quale riparazione?'))):
                self.chat.return_value = json.dumps(answer)
                result = sec.prepare(text, 42)
                self.assertEqual(result['changes'], [])
                self.assertEqual(result['clarification'], answer['clarification'])
            for answer in (dict(changes=[change()], clarification='Quale?'),
                           dict(changes=[], clarification='', sql='SELECT 1')):
                self.chat.return_value = json.dumps(answer)
                with self.assertRaises(sec.InvalidPlan):
                    sec.prepare(TEXT, 42)
            self.chat.reset_mock()
            with self.assertRaises(sec.InvalidPlan):
                sec.prepare(TEXT, 42, reference_time='2026-10-17T10:00:00+00:00')
            self.chat.assert_not_called()

    def test_snapshot_and_context_are_bounded_and_literal(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [(dict(id=i),) for i in range(25)]
        result = sec._context(conn, '%_SQL', 100000)
        self.assertEqual(len(result['practices']), sec.MAX_CONTEXT)
        self.assertTrue(result['truncated']['responsibilities'])
        self.assertEqual(conn.execute.call_args.args[1], ('%_SQL', '%_SQL', sec.MAX_CONTEXT + 1))
        self.assertIn('position(lower(%s)', conn.execute.call_args.args[0])
        for limit in (False, 0, -1, '12'):
            with self.assertRaises(sec.InvalidPlan):
                sec._context(conn, '', limit)

    def test_payload_hash_canonical_and_approval_strict(self):
        first = dict(action='send_message', channel='email', recipient='tecnico@example.test',
                     text='Chiedo un preventivo', documents=[])
        self.assertEqual(sec._approval_payload(first), sec.payload_hash(dict(reversed(list(first.items())))))
        self.assertNotEqual(sec.payload_hash(first), sec.payload_hash(dict(first, text='Testo diverso')))
        for payload in (dict(first, recipient=''), dict(first, shell='echo x'),
                        dict(first, documents=[dict(id=True, sha256='a' * 64)]),
                        dict(first, documents=[dict(id=1, sha256='no')])):
            with self.assertRaises(sec.InvalidPlan):
                sec._approval_payload(payload)
        with self.assertRaises(sec.InvalidPlan):
            sec.payload_hash(float('nan'))

    def test_dashboard_caller_is_explicit_not_client_json(self):
        with patch.object(config, 'TELEGRAM_CHAT_ID', '101'):
            sec._caller(sec.DashboardOwner('101', True, True))
            for caller in (None, dict(owner_id='101', authenticated=True, csrf_verified=True),
                           sec.DashboardOwner('102', True, True), sec.DashboardOwner('101', False, True),
                           sec.DashboardOwner('101', True, False), sec.DashboardOwner('101', 1, True)):
                with self.subTest(caller=caller), self.assertRaises(sec.Unauthorized):
                    sec._caller(caller)

    def test_native_metadata_is_authority_not_display_name(self):
        meta = dict(account='bot:777', chat_id='101', author=101, direction='in',
                    chat_type='private', content_origin='direct_text', is_forwarded=False,
                    message_id=5)
        row = ('telegram', TEXT, 'bot:777', '101', '101', 'in',
               json.dumps(['bot', '777', '101', 5]), meta, ANCHOR)
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = row
        with patch.object(config, 'TELEGRAM_CHAT_ID', '101'), patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:not-a-secret'):
            self.assertEqual(sec._trusted_message(conn, 1), (TEXT, ANCHOR))
            for override in ({'historical': True}, {'is_forwarded': True}, {'is_forwarded': None},
                             {'content_origin': 'caption'}, {'forward_origin': {'type': 'user'}},
                             {'quote': {'text': TEXT}}, {'chat_type': 'group'}, {'author': 102},
                             {'via_bot': {'id': 3}}, {'forward_date': 123}, {'sender_chat': {'id': 3}}):
                conn.execute.return_value.fetchone.return_value = (*row[:7], dict(meta, **override), ANCHOR)
                with self.subTest(override=override), self.assertRaises(sec.Unauthorized):
                    sec._trusted_message(conn, 1)
            legacy = deepcopy(meta)
            del legacy['is_forwarded']
            conn.execute.return_value.fetchone.return_value = (*row[:7], legacy, ANCHOR)
            with self.assertRaises(sec.Unauthorized):
                sec._trusted_message(conn, 1)


class SecretaryPreparePrivacyTests(unittest.TestCase):
    def test_prepare_default_denies_native_source_without_ai_consent_before_model(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        conn.execute.return_value.fetchall.return_value = []
        meta = dict(account='bot:777', chat_id='101', author=101, direction='in',
                    chat_type='private', content_origin='direct_text', is_forwarded=False,
                    message_id=5)
        source_id = json.dumps(['bot', '777', '101', 5])
        native = ('telegram', TEXT, 'bot:777', '101', '101', 'in', source_id, meta, ANCHOR)
        provenance = ('telegram', meta, ANCHOR, ANCHOR, source_id, 'bot:777', '101', '101', 'in', 5)
        cursor = conn.execute.return_value

        def execute(sql, *args):
            if sql.startswith('SELECT channel,body,account'):
                return MagicMock(**{'fetchone.return_value': native})
            if sql.startswith('SELECT channel,metadata,ts'):
                return MagicMock(**{'fetchone.return_value': provenance})
            if ('SELECT to_jsonb(t)' in sql or 'FROM data_policies' in sql
                    or sql.startswith('INSERT INTO effect_audit')):
                return cursor
            raise AssertionError('Unexpected SQL')

        conn.execute.side_effect = execute
        with patch.object(db, '_conn') as connection, \
                patch.object(config, 'TELEGRAM_CHAT_ID', '101'), \
                patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:not-a-secret'), \
                patch.object(brain, '_chat') as model, \
                patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')) as http:
            connection.return_value.__enter__.return_value = conn
            with self.assertRaisesRegex(PermissionError, '^Context source egress denied$'):
                sec.prepare(TEXT, 42, reference_time=ANCHOR)
        model.assert_not_called()
        http.assert_not_called()
        statements = [c.args for c in conn.execute.call_args_list]
        policies = [s for s in statements if 'FROM data_policies' in s[0]]
        self.assertEqual(len(policies), 1)
        self.assertEqual(policies[0][1], ('telegram', 'ai_text'))
        self.assertIn(('INSERT INTO effect_audit(message_id,effect,allowed,reason) VALUES (%s,%s,%s,%s)',
                       (42, 'ai_text', False, 'context_data_denied')), statements)


if __name__ == '__main__':
    unittest.main()