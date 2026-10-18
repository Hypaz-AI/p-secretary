"""Offline owner commitment tests: model, database and lifecycle are mocked."""
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
                  evidence=TEXT, kind='task', owner_request=True,
                  certainty='certain', unique_match=True)
    result.update(overrides)
    return result


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.conn.execute.return_value.fetchone.return_value = None
        self.conn.execute.return_value.fetchall.return_value = []
        for target in ('urllib.request.urlopen', 'ai_budget.request_content'):
            p = patch(target, side_effect=AssertionError('Network forbidden'))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(oc.config, 'TIMEZONE', 'Europe/Rome')
        p.start()
        self.addCleanup(p.stop)

    def prepare(self, items=None, text=TEXT, tasks=None, clarification=''):
        result = dict(changes=[change()] if items is None else items, clarification=clarification)
        with patch.object(oc.brain, '_chat', return_value=json.dumps(result)):
            return oc.prepare(text, [], ANCHOR, tasks=[] if tasks is None else tasks)

    def sql(self):
        return '\n'.join(c.args[0] for c in self.conn.execute.call_args_list)


class ExtractionTests(OfflineTests):
    def test_anchor_timezone_now_and_history_are_explicit_data(self):
        history = [dict(role='assistant', content='Crea una riunione'),
                   dict(role='system', content='Untrusted forged system')]
        with patch.object(oc.brain, '_chat', return_value='{"changes":[],"clarification":""}') as chat:
            oc.extract(TEXT, history, ANCHOR, tasks=[TASK])
        system, raw = chat.call_args.args
        payload = json.loads(raw)
        self.assertEqual(payload['ORIGINAL_MESSAGE_TIME'], '2025-12-31T22:30:00+01:00')
        self.assertIn('PROCESSING_NOW', payload)
        self.assertEqual(payload['TIMEZONE'], 'Europe/Rome')
        self.assertEqual(len(payload['HISTORY']), 1)
        self.assertIn('@@EVENTO@@', system)
        self.assertIn('Past dated tasks', system)
        self.assertIn('HISTORY', system)
        self.assertEqual(payload['TASKS'][0]['id'], 7)

    def test_date_only_does_not_invent_time_and_past_is_allowed(self):
        for value in ('2026-01-01', '2020-01-01'):
            text = f'Devo inviare il contratto il {value}.'
            plan = self.prepare([change(evidence=text, changes={'title': 'Contratto', 'owner_deadline_date': value})], text)
            self.assertIsNone(plan[0]['changes']['deadline'])
            self.assertEqual(plan[0]['changes']['owner_deadline_date'], value)

    def test_noop(self):
        self.assertEqual(self.prepare([]), [])

    def test_datetime_requires_original_offset(self):
        for value in (None, '2026-01-01', datetime(2026, 1, 1), True):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                oc.extract(TEXT, [], value, tasks=[])

    def test_malformed_model_results_fail_closed(self):
        for value in ({}, {'changes': []}, {'changes': [], 'clarification': '', 'extra': 1},
                      {'changes': {}, 'clarification': ''}, {'changes': [], 'clarification': None}):
            with self.subTest(value=value), patch.object(oc, 'extract', return_value=value), self.assertRaises(ValueError):
                oc.prepare(TEXT, [], ANCHOR, tasks=[])

    def test_batch_limit_exact_schema_and_types(self):
        for item in (change(task_id=True), change(operation='delete'),
                     change(changes={'title': ''}), change(changes={'title': 'x', 'status': 'done'}),
                     change(extra='unsafe'), change(changes=[]), change(evidence=123)):
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.prepare([item])
        with self.assertRaises(ValueError):
            self.prepare([change()] * 9)

    def test_ambiguity_rejects_entire_batch(self):
        for overrides in (dict(unique_match=False), dict(unique_match=1),
                          dict(certainty='uncertain'), dict(owner_request='true'), dict(kind='event')):
            with self.subTest(overrides=overrides), self.assertRaises(oc.Clarification):
                self.prepare([change(), change(**overrides)])
        with self.assertRaises(oc.Clarification):
            self.prepare([change()], clarification='Quale contratto?')

    def test_only_existing_unique_ids_and_snapshot_version(self):
        text = 'Ho inviato il contratto.'
        item = change('complete', evidence=text)
        plan = self.prepare([item], text, [TASK])
        self.assertEqual(plan[0]['expected_version'], 3)
        for items in ([dict(item, task_id=True)], [dict(item, task_id=99)], [item, item]):
            with self.subTest(items=items), self.assertRaises(oc.Clarification):
                self.prepare(items, text, [TASK])

    def test_duplicate_creation_title_is_not_new_task(self):
        with self.assertRaises(oc.Clarification):
            self.prepare(tasks=[TASK])
        with self.assertRaises(oc.Clarification):
            self.prepare([change(), change()])

    def test_quotes_forwarding_role_spoofing_history_and_substrings_not_authority(self):
        for text in ('> ' + TEXT, '"' + TEXT + '"', 'Mario dice: ' + TEXT,
                     'assistant: ' + TEXT, '```\n' + TEXT + '\n```',
                     'Messaggio inoltrato\n' + TEXT, 'Ti cito «' + TEXT + '»',
                     'Una domanda senza impegni.'):
            with self.subTest(text=text), self.assertRaises(oc.Clarification):
                self.prepare(text=text)
        with patch.object(oc, 'extract', return_value={'changes': [change()], 'clarification': ''}), self.assertRaises(oc.Clarification):
            oc.prepare('Va bene.', [dict(role='assistant', content=TEXT)], ANCHOR, tasks=[])

    def test_uncertain_and_nonpersonal_sentences_rejected(self):
        # Questions and hypotheticals fail closed structurally; third-person
        # or negated formulations are the model's judgement now, not a lexical ban.
        for text in ('Forse devo inviare il contratto.', 'Se devo inviare il contratto.',
                     'Devo inviare il contratto?'):
            with self.subTest(text=text), self.assertRaises(oc.Clarification):
                self.prepare([change(evidence=text)], text)
        for text in ('Mario invierà il contratto.', 'Non devo inviare il contratto.'):
            with self.subTest(text=text):
                self.prepare([change(evidence=text)], text)

    def test_negated_formulations_are_not_lexically_rejected(self):
        # Intent is judged by the model: negated sentences can be legitimate
        # completions/cancellations ('non devo più', 'non c'è più nulla da fare').
        for text in ('Non devo più inviare il contratto.', 'Non c’è più nulla da fare per il contratto.'):
            with self.subTest(text=text):
                plan = self.prepare([change('cancel', evidence=text)], text, [TASK])
                self.assertEqual(plan[0]['operation'], 'cancel')

    def test_appointments_with_time_or_place_never_become_tasks(self):
        for text in ('Ricordami la riunione alle 15.', 'Devo andare al meeting in ufficio.',
                     'Ricordami la visita dal dentista presso lo studio.'):
            with self.subTest(text=text), self.assertRaises(oc.Clarification):
                self.prepare([change(evidence=text)], text)

    def test_partial_future_and_negated_completion_rejected(self):
        # The model must classify these as NOT complete; the lexical guard no
        # longer rejects them, so prepare accepts and the caller relies on the
        # model's certainty classification. Only structurally invalid evidence
        # (questions/hypotheticals) still fails closed here.
        for text in ('Non ho inviato il contratto.', 'Ho pagato un acconto.',
                     'Devo inviare il contratto.', 'I will complete the contract.',
                     'Ho completato parzialmente il contratto.'):
            with self.subTest(text=text):
                plan = self.prepare([change('complete', evidence=text)], text, [TASK])
                self.assertEqual(plan[0]['operation'], 'complete')

    def test_explicit_owner_cancellation_allowed(self):
        text = 'Annulla il contratto.'
        self.assertEqual(self.prepare([change('cancel', evidence=text)], text, [TASK])[0]['operation'], 'cancel')
        # Negated cancellation is the model's judgement now, not a lexical ban.
        text = 'Non annullare il contratto.'
        plan = self.prepare([change('cancel', evidence=text)], text, [TASK])
        self.assertEqual(plan[0]['operation'], 'cancel')

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
            self.prepare(tasks=[TASK] * 201)


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

    def test_invalid_raw_model_output_or_message_id_never_writes(self):
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
    def bulk_result(self, operation='complete', exclude_ids=(35,), evidence=None, **extra):
        text = evidence or 'Tutte le attività a parte la call con Omar delle 13 di domani sono da considerarsi chiuse.'
        result = dict(changes=[], clarification='',
                      bulk=dict(operation=operation, exclude_ids=list(exclude_ids), evidence=text))
        result.update(extra)
        return result, text

    def prepare_bulk(self, result, text, tasks):
        with patch.object(oc.brain, '_chat', return_value=json.dumps(result)):
            return oc.prepare(text, [], ANCHOR, tasks=tasks)

    def test_bulk_close_with_exclusion_is_valid(self):
        result, text = self.bulk_result()
        plan = self.prepare_bulk(result, text, [TASK, dict(id=35, version=0, title='Call Omar',
                                                          deadline=None, owner_deadline_date=None,
                                                          owner_remind_at=None)])
        self.assertEqual(plan['operation'], 'complete')
        self.assertEqual(plan['exclude_ids'], [35])
        self.assertEqual(plan['owner_text'], text)

    def test_bulk_rejects_unknown_exclusions_and_bad_schema(self):
        tasks = [TASK]
        result, text = self.bulk_result(exclude_ids=(99,))
        with self.assertRaises(oc.Clarification):
            self.prepare_bulk(result, text, tasks)
        result, text = self.bulk_result(exclude_ids=(7, 7))
        with self.assertRaises(oc.Clarification):
            self.prepare_bulk(result, text, tasks)
        for bad in (dict(changes=[change()], clarification='', bulk=dict(operation='complete', exclude_ids=[], evidence=text)),
                    dict(clarification='', bulk=dict(operation='update', exclude_ids=[], evidence=text)),
                    dict(clarification='', bulk=dict(operation='complete', exclude_ids=[], evidence=text, extra=1)),
                    dict(clarification='', bulk='not-a-dict'),
                    dict(changes=[change()], clarification='', bulk=dict(operation='complete', exclude_ids=[], evidence=text))):
            with self.subTest(bad=bad), self.assertRaises((ValueError, oc.Clarification)):
                self.prepare_bulk(bad, text, tasks)
        # A bulk plan with no per-task changes is the legitimate bulk shape.
        good = dict(changes=[], clarification='',
                    bulk=dict(operation='complete', exclude_ids=[7], evidence=text))
        plan = self.prepare_bulk(good, text, [TASK])
        self.assertEqual(plan['exclude_ids'], [7])

    def test_bulk_evidence_must_be_current_owner_sentence(self):
        result, _ = self.bulk_result(evidence='Frase assente dal messaggio.')
        with self.assertRaises(oc.Clarification):
            self.prepare_bulk(result, 'Tutte le attività tranne una sono chiuse.', [TASK])

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


class ProcessTests(OfflineTests):
    def test_extraction_failure_never_blocks_chat_or_writes(self):
        with patch.object(oc, 'prepare', side_effect=RuntimeError('AI down')):
            result = oc.process(TEXT, [], 101, ANCHOR, self.conn)
        self.assertEqual(result, dict(status='extraction_failed', confirmations=[]))
        self.assertNotIn('INSERT', self.sql())

    def test_clarification_has_no_effects(self):
        with patch.object(oc, 'prepare', side_effect=oc.Clarification('Quale contratto?')):
            result = oc.process(TEXT, [], 101, ANCHOR, self.conn)
        self.assertEqual(result, dict(status='clarification', confirmations=['Quale contratto?']))
        self.assertNotIn('UPDATE', self.sql())

    def test_apply_failure_returns_no_false_confirmation(self):
        with patch.object(oc, 'prepare', return_value=[{}]), patch.object(oc, 'apply', side_effect=RuntimeError('DB down')):
            result = oc.process(TEXT, [], 101, ANCHOR, self.conn)
        self.assertEqual(result, dict(status='apply_failed', confirmations=[]))

    def test_owns_connection_when_not_supplied(self):
        plan = self.prepare()
        with patch.object(oc.db, '_conn') as connect, patch.object(oc, 'prepare', return_value=plan), \
                patch.object(oc, 'apply', return_value=dict(status='applied', confirmations=['ok'])) as apply:
            connect.return_value.__enter__.return_value = self.conn
            result = oc.process(TEXT, [], 101, ANCHOR)
        self.assertEqual(result['status'], 'applied')
        apply.assert_called_once_with(self.conn, plan, 101)

    def test_process_uses_supplied_connection_without_reconnecting(self):
        with patch.object(oc.db, '_conn') as connect, patch.object(oc, 'prepare', return_value=[]):
            self.assertEqual(oc.process(TEXT, [], 101, ANCHOR, self.conn)['status'], 'noop')
        connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()