"""Test offline degli impegni del proprietario: validazione server e apply idempotente.

Il modello propone (strumenti dell'agente); qui si verifica il confine di
validazione (build_change/build_bulk) e gli effetti atomici di apply().
Database e ciclo di vita sono simulati.
"""
import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import owner_commitments as oc


ANCHOR = datetime(2025, 12, 31, 21, 30, tzinfo=timezone.utc)
TEXT = 'Devo inviare il contratto entro domani.'
TASK = dict(id=7, version=3, title='Inviare contratto', deadline=None,
            owner_deadline_date=None, owner_remind_at=None)


def change(operation='create', **overrides):
    result = dict(operation=operation, task_id=None if operation == 'create' else 7,
                  changes={'title': 'Inviare contratto', 'deadline': None,
                           'owner_deadline_date': '2026-01-01'} if operation == 'create' else {},
                  evidence=TEXT)
    result.update(overrides)
    return result


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.conn.execute.return_value.fetchone.return_value = None
        self.conn.execute.return_value.fetchall.return_value = []
        network = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        p = patch.object(oc.config, 'TIMEZONE', 'Europe/Rome')
        p.start()
        self.addCleanup(p.stop)

    def prepare(self, items=None, text=TEXT, tasks=None):
        """Come lo strumento dell'agente: una build_change per richiesta, poi un batch ad apply."""
        items = [change()] if items is None else items
        if len(items) > oc.MAX_CHANGES:
            raise ValueError('Too many changes')
        return [oc.build_change(text, ANCHOR, [] if tasks is None else tasks, item['operation'],
                                item['task_id'], item['changes'], item['evidence']) for item in items]

    def sql(self):
        return '\n'.join(c.args[0] for c in self.conn.execute.call_args_list)


class BuildTests(OfflineTests):
    def test_date_only_does_not_invent_time_and_past_is_allowed(self):
        for value in ('2026-01-01', '2020-01-01'):
            text = f'Devo inviare il contratto il {value}.'
            plan = self.prepare([change(evidence=text, changes={'title': 'Contratto', 'owner_deadline_date': value})], text)
            self.assertIsNone(plan[0]['changes']['deadline'])
            self.assertEqual(plan[0]['changes']['owner_deadline_date'], value)

    def test_server_fixes_expected_version_and_owner_text(self):
        text = 'Ho inviato il contratto.'
        plan = self.prepare([change('complete', evidence=text)], text, [TASK])
        self.assertEqual(plan[0]['expected_version'], 3)
        self.assertEqual(plan[0]['owner_text'], text)

    def test_datetime_requires_original_offset(self):
        for value in (None, '2026-01-01', datetime(2026, 1, 1), True):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                oc.build_change(TEXT, value, [], 'create', None, change()['changes'], TEXT)

    def test_exact_schema_and_types(self):
        for item in (change(operation='delete'), change(changes={'title': ''}),
                     change(changes={'title': 'x', 'status': 'done'}), change(changes=[]),
                     change(changes={}), change('complete', changes={'title': 'x'})):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.prepare([item], tasks=[TASK])
        for item in (change(evidence=123), change(evidence='')):
            with self.subTest(item=item), self.assertRaises(oc.Clarification):
                self.prepare([item])
        with self.assertRaises(ValueError):
            self.prepare([change()] * (oc.MAX_CHANGES + 1))
        with self.assertRaises(ValueError):
            oc.build_change(TEXT, ANCHOR, [], 'create', 7, change()['changes'], TEXT)  # creation targets no ID

    def test_only_existing_unique_ids(self):
        text = 'Ho inviato il contratto.'
        for task_id in (True, 99, None, '7'):
            with self.subTest(task_id=task_id), self.assertRaises(oc.Clarification):
                self.prepare([change('complete', task_id=task_id, evidence=text)], text, [TASK])

    def test_duplicate_creation_title_is_not_new_task(self):
        with self.assertRaises(oc.Clarification):
            self.prepare(tasks=[TASK])

    def test_quotes_forwarding_role_spoofing_history_and_substrings_not_authority(self):
        for text in ('> ' + TEXT, '"' + TEXT + '"', 'Mario dice: ' + TEXT,
                     'assistant: ' + TEXT, '```\n' + TEXT + '\n```',
                     'Messaggio inoltrato\n' + TEXT, 'Ti cito «' + TEXT + '»',
                     'Una domanda senza impegni.'):
            with self.subTest(text=text), self.assertRaises(oc.Clarification):
                self.prepare(text=text)
        # L'evidenza deve essere una frase INTERA del messaggio corrente, non una sottostringa.
        with self.assertRaises(oc.Clarification):
            self.prepare([change(evidence='inviare il contratto')], 'Devo inviare il contratto entro domani.')

    def test_uncertain_sentences_rejected(self):
        for text in ('Forse devo inviare il contratto.', 'Se devo inviare il contratto.',
                     'Devo inviare il contratto?'):
            with self.subTest(text=text), self.assertRaises(oc.Clarification):
                self.prepare([change(evidence=text)], text)
        # Terza persona o negazione: giudizio del modello, non un divieto lessicale.
        for text in ('Mario invierà il contratto.', 'Non devo inviare il contratto.'):
            with self.subTest(text=text):
                self.prepare([change(evidence=text)], text)

    def test_negated_formulations_are_not_lexically_rejected(self):
        for text in ('Non devo più inviare il contratto.', 'Non c’è più nulla da fare per il contratto.'):
            with self.subTest(text=text):
                plan = self.prepare([change('cancel', evidence=text)], text, [TASK])
                self.assertEqual(plan[0]['operation'], 'cancel')

    def test_appointments_with_time_or_place_never_become_tasks(self):
        for text in ('Ricordami la riunione alle 15.', 'Devo andare al meeting in ufficio.',
                     'Ricordami la visita dal dentista presso lo studio.'):
            with self.subTest(text=text), self.assertRaises(oc.Clarification):
                self.prepare([change(evidence=text)], text)

    def test_explicit_owner_cancellation_allowed(self):
        text = 'Annulla il contratto.'
        self.assertEqual(self.prepare([change('cancel', evidence=text)], text, [TASK])[0]['operation'], 'cancel')

    def test_explicit_reminder_separate_from_deadline_and_correct_dst(self):
        text = 'Ricordami di pagare il 2026-07-01 alle 15:00.'
        fields = dict(title='Pagare', owner_remind_at='2026-07-01T15:00:00+02:00')
        plan = self.prepare([change(changes=fields, evidence=text)], text)
        self.assertNotIn('deadline', plan[0]['changes'])
        self.assertEqual(plan[0]['changes']['owner_remind_at'], fields['owner_remind_at'])
        for timestamp in ('2026-07-01T15:00:00+01:00', '2026-07-01T15:00:00', 'bad', True):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                self.prepare([change(changes=dict(fields, owner_remind_at=timestamp), evidence=text)], text)

    def test_no_invented_hour_or_reminder_without_request(self):
        with self.assertRaises(oc.Clarification):
            self.prepare([change(changes=dict(title='Inviare', deadline='2026-01-01T09:00:00+01:00'))])
        text = 'Devo inviare il contratto alle 15:00.'
        with self.assertRaises(oc.Clarification):
            self.prepare([change(evidence=text, changes=dict(title='Inviare', owner_remind_at='2026-01-01T15:00:00+01:00'))], text)

    def test_date_and_timed_deadline_exclusive_and_date_valid(self):
        text = 'Devo inviare alle 15:00.'
        for fields in (dict(title='x', owner_deadline_date='2026-02-30'),
                       dict(title='x', owner_deadline_date='2026-01-01', deadline='2026-01-01T15:00:00+01:00')):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.prepare([change(evidence=text, changes=fields)], text)
        result = self.prepare([change(evidence=text, changes=dict(title='Inviare', deadline='2026-01-01T15:00:00+01:00'))], text)
        self.assertIsNone(result[0]['changes']['owner_deadline_date'])

    def test_candidate_overflow_is_clarification_not_truncation(self):
        with self.assertRaises(oc.Clarification):
            self.prepare(tasks=[TASK] * (oc.MAX_TASKS + 1))


class ApplyingTests(OfflineTests):
    def test_creation_provenance_key_transaction_no_calendar_or_lifecycle(self):
        plan = self.prepare()
        self.conn.execute.return_value.fetchone.side_effect = [None, None, (41,)]
        with patch.object(oc.lifecycle, 'apply_change') as lifecycle:
            result = oc.apply(self.conn, plan, 101)
        self.assertEqual(result['status'], 'applied')
        self.assertIn('solo data', result['confirmations'][0])
        self.assertNotIn('09:00', result['confirmations'][0])
        call = self.conn.execute.call_args_list[-1]
        self.assertIn('source_message', call.args[0])
        self.assertIn('NULL', call.args[0])
        self.assertEqual(call.args[1], ('Inviare contratto', None, '2026-01-01', 101, 'owner-commitment:101:0', None))
        self.conn.transaction.assert_called_once()
        self.conn.commit.assert_not_called()
        lifecycle.assert_not_called()
        self.assertNotIn('appointments', self.sql())

    def test_creation_retry_duplicate_no_insert(self):
        plan = self.prepare()
        self.conn.execute.return_value.fetchone.side_effect = [(41, 'Inviare contratto', None, '2026-01-01', None), None]
        result = oc.apply(self.conn, plan, 101)
        self.assertEqual(result['status'], 'duplicate')
        self.assertNotIn('INSERT', self.sql())

    def test_retry_creation_changed_fields_fails_closed(self):
        self.conn.execute.return_value.fetchone.side_effect = [(41, 'Different', None, None, None), None]
        with self.assertRaises(oc.StaleBatch):
            oc.apply(self.conn, self.prepare(), 101)

    def test_retry_cannot_extend_or_truncate_previously_committed_batch(self):
        for keys in (['owner-commitment:101:0', 'owner-commitment:101:1'],
                     ['owner-commitment:101:1']):
            with self.subTest(keys=keys):
                self.conn.execute.return_value.fetchall.return_value = [(key,) for key in keys]
                with self.assertRaises(oc.StaleBatch):
                    oc.apply(self.conn, self.prepare(), 101)
        self.assertNotIn('INSERT', self.sql())

    def lifecycle_plan(self, operation='complete', fields=None):
        text = {'complete': 'Ho inviato il contratto.', 'cancel': 'Annulla il contratto.',
                'update': 'Sposta il contratto a domani.'}[operation]
        return self.prepare([change(operation, evidence=text, changes=fields or {})], text, [TASK])

    def test_lifecycle_operations_use_trusted_api_with_owner_evidence(self):
        for operation in ('update', 'complete', 'cancel'):
            with self.subTest(operation=operation):
                self.conn.reset_mock()
                self.conn.execute.return_value.fetchone.side_effect = [('open', 3), None, None]
                plan = self.lifecycle_plan(operation, {'owner_deadline_date': '2026-01-01'} if operation == 'update' else {})
                with patch.object(oc.lifecycle, 'apply_change', return_value='applied') as lifecycle:
                    result = oc.apply(self.conn, plan, 101)
                self.assertEqual(result['status'], 'applied')
                data = lifecycle.call_args.args[1]
                self.assertEqual(data['message_id'], 101)
                self.assertEqual(data['evidence_key'], 'owner-commitment:101:0')
                self.assertEqual(data['expected_version'], 3)
                self.assertEqual(data['evidence'], plan[0]['evidence'])
                self.assertEqual(data['operation'], operation)
                self.assertEqual(data['changes'], {'deadline': None} if operation == 'update' else {})
                self.assertIn('owner_deadline_date', data['reason'] if operation == 'update' else self.sql())

    def test_sidecar_only_edit_still_versions_through_lifecycle(self):
        text = 'Modifica il promemoria del contratto: ricordami alle 15:00.'
        plan = self.prepare([change('update', evidence=text, changes={'owner_remind_at': '2026-01-01T15:00:00+01:00'})], text, [TASK])
        self.conn.execute.return_value.fetchone.side_effect = [('open', 3), None, None, ('Inviare contratto',)]
        with patch.object(oc.lifecycle, 'apply_change', return_value='applied') as lifecycle:
            oc.apply(self.conn, plan, 101)
        self.assertEqual(lifecycle.call_args.args[1]['changes'], {'title': 'Inviare contratto'})
        self.assertIn('SET owner_remind_at=%s', self.sql())

    def test_stale_missing_or_closed_task_never_calls_lifecycle(self):
        plan = self.lifecycle_plan()
        for row in (None, ('done', 3), ('open', 4)):
            with self.subTest(row=row), patch.object(oc.lifecycle, 'apply_change') as lifecycle:
                self.conn.execute.return_value.fetchone.side_effect = [row, None, None]
                with self.assertRaises(oc.StaleBatch):
                    oc.apply(self.conn, plan, 101)
                lifecycle.assert_not_called()

    def test_lifecycle_nonapplied_rolls_back(self):
        self.conn.execute.return_value.fetchone.side_effect = [('open', 3), None, None]
        with patch.object(oc.lifecycle, 'apply_change', return_value='stale'), self.assertRaises(oc.StaleBatch):
            oc.apply(self.conn, self.lifecycle_plan(), 101)
        self.assertIs(self.conn.transaction.return_value.__exit__.call_args.args[0], oc.StaleBatch)

    def test_lifecycle_receipt_idempotence_even_after_task_closed(self):
        plan = self.lifecycle_plan()
        item = plan[0]
        receipt = json.dumps(dict(operation=item['operation'], task_id=item['task_id'],
                                  changes=item['changes'], evidence=item['evidence']), sort_keys=True, ensure_ascii=False)
        self.conn.execute.return_value.fetchone.side_effect = [('done', 4), None, (7, 'complete', 'applied', receipt)]
        with patch.object(oc.lifecycle, 'apply_change') as lifecycle:
            self.assertEqual(oc.apply(self.conn, plan, 101)['status'], 'duplicate')
        lifecycle.assert_not_called()

    def test_wrong_or_rejected_receipt_is_not_success(self):
        self.conn.execute.return_value.fetchone.side_effect = [('open', 3), None, (7, 'complete', 'rejected', 'other')]
        with self.assertRaises(oc.StaleBatch):
            oc.apply(self.conn, self.lifecycle_plan(), 101)

    def test_unvalidated_input_or_bad_message_id_never_writes(self):
        plan = self.prepare()
        for batch, identity in (([change()], 101), (plan, True), (plan, 0), (plan, '101')):
            self.conn.reset_mock()
            with self.subTest(batch=batch, identity=identity), self.assertRaises(ValueError):
                oc.apply(self.conn, batch, identity)
            self.conn.execute.assert_not_called()
        tampered = copy.deepcopy(plan)
        tampered[0]['changes']['status'] = 'done'
        with self.assertRaises(ValueError):
            oc.apply(self.conn, tampered, 101)

    def test_empty_batch_no_transaction(self):
        self.assertEqual(oc.apply(self.conn, [], 101), dict(status='noop', confirmations=[]))
        self.conn.transaction.assert_not_called()

    def test_all_fields_validated_before_first_sql_even_in_late_item(self):
        plan = self.prepare()
        invalid = copy.deepcopy(plan[0])
        invalid['changes']['title'] = ''
        with self.assertRaises(ValueError):
            oc.apply(self.conn, plan + [invalid], 101)
        self.conn.execute.assert_not_called()


class BulkTests(OfflineTests):
    TEXT = 'Tutte le attività a parte la call con Omar delle 13 di domani sono da considerarsi chiuse.'
    OMAR = dict(id=35, version=0, title='Call Omar', deadline=None, owner_deadline_date=None, owner_remind_at=None)

    def test_bulk_close_with_exclusion_is_valid(self):
        plan = oc.build_bulk(self.TEXT, [TASK, self.OMAR], 'complete', [35], self.TEXT)
        self.assertEqual(plan['operation'], 'complete')
        self.assertEqual(plan['exclude_ids'], [35])
        self.assertEqual(plan['owner_text'], self.TEXT)

    def test_bulk_rejects_unknown_exclusions_and_bad_schema(self):
        for exclusions in ([99], [7, 7], 'x', [True], [7.0]):
            with self.subTest(exclusions=exclusions), self.assertRaises(oc.Clarification):
                oc.build_bulk(self.TEXT, [TASK], 'complete', exclusions, self.TEXT)
        with self.assertRaises(ValueError):
            oc.build_bulk(self.TEXT, [TASK], 'update', [], self.TEXT)
        plan = oc.build_bulk(self.TEXT, [TASK], 'complete', [7], self.TEXT)
        self.assertEqual(plan['exclude_ids'], [7])

    def test_bulk_evidence_must_be_current_owner_sentence(self):
        with self.assertRaises(oc.Clarification):
            oc.build_bulk('Tutte le attività tranne una sono chiuse.', [TASK], 'complete', [],
                          'Frase assente dal messaggio.')

    def test_bulk_apply_closes_all_but_excluded(self):
        plan = dict(operation='complete', exclude_ids=[35], evidence='Tutte chiuse tranne la 35.',
                    owner_text='Tutte chiuse tranne la 35.')
        conn = MagicMock()

        def execute(sql, *args):
            row = MagicMock()
            if 'FROM task_changes' in sql:
                row.fetchone.return_value = None
            elif 'status=\'open\'\n            ORDER BY id' in sql:
                row.fetchall.return_value = [(7, 3), (35, 0), (51, 1)]
            else:
                row.fetchone.return_value = (1,)
            return row
        conn.execute.side_effect = execute
        applied = []
        with patch.object(oc.lifecycle, 'apply_change', side_effect=lambda c, item: applied.append(item) or 'applied') as lc:
            result = oc.apply(conn, plan, 101)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(sorted(item['task_id'] for item in applied), [7, 51])
        self.assertTrue(all(item['evidence_key'] == 'owner-commitment:101:bulk' for item in applied))
        self.assertIn('2 attività completate', result['confirmations'][0])

    def test_bulk_apply_retry_is_idempotent(self):
        plan = dict(operation='complete', exclude_ids=[35], evidence='Tutte chiuse tranne la 35.',
                    owner_text='Tutte chiuse tranne la 35.')
        receipt = json.dumps(dict(operation='complete', exclude_ids=[35]), sort_keys=True, ensure_ascii=False)
        conn = MagicMock()

        def execute(sql, *args):
            if 'FROM task_changes' in sql and 'evidence_key=%s' in sql:
                row = MagicMock()
                row.fetchone.return_value = (7, 'complete', 'applied', receipt)
                return row
            row = MagicMock()
            row.fetchone.return_value = (1,)
            return row
        conn.execute.side_effect = execute
        with patch.object(oc.lifecycle, 'apply_change') as lc:
            result = oc.apply(conn, plan, 101)
        self.assertEqual(result['status'], 'duplicate')
        lc.assert_not_called()

    def test_bulk_apply_retry_with_different_exclusions_fails_closed(self):
        plan = dict(operation='complete', exclude_ids=[35], evidence='Tutte chiuse tranne la 35.',
                    owner_text='Tutte chiuse tranne la 35.')
        receipt = json.dumps(dict(operation='complete', exclude_ids=[36]), sort_keys=True, ensure_ascii=False)
        conn = MagicMock()

        def execute(sql, *args):
            if 'FROM task_changes' in sql and 'evidence_key=%s' in sql:
                row = MagicMock()
                row.fetchone.return_value = (7, 'complete', 'applied', receipt)
                return row
            row = MagicMock()
            row.fetchone.return_value = (1,)
            return row
        conn.execute.side_effect = execute
        with patch.object(oc.lifecycle, 'apply_change') as lc, self.assertRaises(oc.StaleBatch):
            oc.apply(conn, plan, 101)
        lc.assert_not_called()

    def test_bulk_apply_noop_when_all_excluded(self):
        plan = dict(operation='complete', exclude_ids=[7], evidence='Chiudi tutto tranne la 7.',
                    owner_text='Chiudi tutto tranne la 7.')
        conn = MagicMock()

        def execute(sql, *args):
            row = MagicMock()
            if 'FROM task_changes' in sql:
                row.fetchone.return_value = None
            elif 'status=\'open\'\n            ORDER BY id' in sql:
                row.fetchall.return_value = [(7, 3)]
            else:
                row.fetchone.return_value = (1,)
            return row
        conn.execute.side_effect = execute
        with patch.object(oc.lifecycle, 'apply_change') as lc:
            result = oc.apply(conn, plan, 101)
        self.assertEqual(result['status'], 'noop')
        lc.assert_not_called()


class SemanticIntentTests(OfflineTests):
    def test_completion_without_verb_vocabulary_is_accepted(self):
        # Intent is judged by the model; the lexical guard must not demand
        # specific verbs like 'segna'/'completa'/'fatto'.
        for text in ('Il contratto è stato evaso.',
                     'Per quella pratica non c’è più nulla da fare.',
                     'La pratica del contratto è da ritenersi chiusa.'):
            with self.subTest(text=text):
                plan = self.prepare([change('complete', evidence=text)], text, [TASK])
                self.assertEqual(plan[0]['operation'], 'complete')

    def test_structural_guards_still_reject_ambiguity(self):
        for text in ('Forse il contratto è chiuso.', 'Se il contratto è chiuso.',
                     'Il contratto è chiuso?'):
            with self.subTest(text=text), self.assertRaises(oc.Clarification):
                self.prepare([change('complete', evidence=text)], text, [TASK])
        # Negated completions are the model's judgement now, not a lexical ban.
        plan = self.prepare([change('complete', evidence='Non ho chiuso il contratto.')],
                            'Non ho chiuso il contratto.', [TASK])
        self.assertEqual(plan[0]['operation'], 'complete')


class IdempotencyPrefixTests(OfflineTests):
    def test_each_tool_call_has_its_own_keys(self):
        plan = self.prepare()
        for prefix, expected in ((None, 'owner-commitment:101:0'),
                                 ('owner-commitment:101:t2', 'owner-commitment:101:t2:0')):
            with self.subTest(prefix=prefix):
                self.conn.reset_mock()
                self.conn.execute.return_value.fetchone.side_effect = [None, None, (41,)]
                oc.apply(self.conn, plan, 101, prefix=prefix)
                self.assertEqual(self.conn.execute.call_args_list[-1].args[1][4], expected)

    def test_prefix_must_belong_to_the_message_and_be_plain(self):
        plan = self.prepare()
        for prefix in ('owner-commitment:102:t1', 'other:101:t1', 'owner-commitment:101:a%b',
                       'owner-commitment:101:a_b', 'owner-commitment:101:' + 'x' * 200, 7):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                oc.apply(self.conn, plan, 101, prefix=prefix)
        self.conn.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()