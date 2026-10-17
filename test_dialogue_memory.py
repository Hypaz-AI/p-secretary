"""Transactional dialogue memory regressions, without network or PostgreSQL."""
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import brain
import db


class MemoryPreparationTests(unittest.TestCase):
    def test_preparation_is_read_only_and_returns_detached_snapshot(self):
        memories = [{'id': 7, 'text': 'Tè', 'created': 'ignored'}]
        history = [{'role': 'user', 'content': [{'type': 'image_url'}]}]
        changes = [{'operation': 'update', 'id': 7, 'text': ' Caffè '},
                   {'operation': 'delete', 'id': 7},
                   {'operation': 'add', 'text': ' Preferisce camminare ' }]
        with patch('brain._chat', return_value=json.dumps({'changes': changes})) as model, \
                patch('db.apply_personal_memories') as save, patch('db._conn') as connect:
            plan = brain.prepare_user_memories('Correggi la preferenza', memories, history)
        save.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(set(plan), {'changes', 'known', 'status'})
        self.assertEqual(plan['known'], [{'id': 7, 'text': 'Tè'}])
        self.assertIsNot(plan['known'][0], memories[0])
        self.assertEqual(plan['changes'], [
            {'operation': 'update', 'id': 7, 'text': 'Caffè'},
            {'operation': 'delete', 'id': 7, 'text': ''},
            {'operation': 'add', 'id': None, 'text': 'Preferisce camminare'}])
        self.assertIn('non ancora salvato', plan['status'])
        source = json.loads(model.call_args.args[1])
        self.assertEqual(source['messaggio_proprietario'], 'Correggi la preferenza')
        self.assertIn('non autorizzato',
                      source['dialogo_precedente_solo_per_riferimenti'][0]['content'])

    def test_validation_and_ten_candidate_limit_match_legacy(self):
        invalid = [None, {'operation': 'drop'}, {'operation': 'delete', 'id': 9},
                   {'operation': 'update', 'id': True, 'text': 'x'},
                   {'operation': 'update', 'id': '7', 'text': 'x'},
                   {'operation': 'add', 'text': ''},
                   {'operation': 'add', 'text': 'x' * 1001},
                   {'operation': 'add', 'text': 12},
                   {'operation': 'delete', 'id': 7}]
        candidates = invalid + [{'operation': 'add', 'text': str(n)} for n in range(5)]
        with patch('brain._chat', return_value=json.dumps({'changes': candidates})):
            plan = brain.prepare_user_memories('ciao', [{'id': 7, 'text': 'Fatto'}])
        self.assertEqual(plan['changes'], [
            {'operation': 'delete', 'id': 7, 'text': ''},
            {'operation': 'add', 'id': None, 'text': '0'}])

    def test_invalid_extraction_and_empty_changes_have_legacy_status(self):
        for result, status in [({'changes': []}, 'nessuna modifica necessaria'),
                               ({'changes': 'wrong'}, 'errore:'), ([], 'errore:')]:
            with self.subTest(result=result), patch('brain._chat', return_value=json.dumps(result)):
                plan = brain.prepare_user_memories('ciao', [])
                self.assertEqual(plan['changes'], [])
                self.assertTrue(plan['status'].startswith(status))
                self.assertEqual(brain.remember_user('ciao', []), plan['status'])

    def test_infrastructure_error_propagates_only_from_preparation(self):
        with patch('brain._chat', side_effect=RuntimeError('offline')), \
                patch('db.apply_personal_memories') as save:
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                brain.prepare_user_memories('ciao', [])
            self.assertTrue(brain.remember_user('ciao', []).startswith('errore:'))
        save.assert_not_called()

    def test_legacy_calls_save_once_with_original_two_argument_contract(self):
        plan = dict(changes=[dict(operation='add', id=None, text='Fatto')],
                    known=[dict(id=7, text='Tè')], status='provvisorio')
        with patch('brain.prepare_user_memories', return_value=plan) as prepare, \
                patch('db.apply_personal_memories', return_value=1) as save:
            status = brain.remember_user('Fatto', plan['known'], history=[])
        prepare.assert_called_once_with('Fatto', plan['known'], history=[])
        save.assert_called_once_with(plan['changes'], {7})
        self.assertEqual(status, 'successo: 1 modifiche persistenti applicate alla memoria personale')


class MemoryApplicationTests(unittest.TestCase):
    def connection(self, rows=None):
        conn = MagicMock()
        rows = rows or {}

        def execute(query, params=None):
            cursor = MagicMock(rowcount=1)
            if query.startswith('SELECT text'):
                cursor.fetchone.return_value = rows.get(params[0])
            elif query.startswith('INSERT'):
                cursor.fetchone.return_value = (99,)
            return cursor

        conn.execute.side_effect = execute
        return conn

    def test_shared_connection_is_not_opened_committed_or_closed(self):
        conn = self.connection({7: ('Tè',), 8: ('Obsoleto',)})
        changes = [dict(operation='add', text='Cammina'),
                   dict(operation='update', id=7, text='Caffè'),
                   dict(operation='delete', id=8)]
        expected = [dict(id=7, text='Tè'), dict(id=8, text='Obsoleto')]
        with patch('db._conn') as connect:
            count = db.apply_personal_memories(changes, {7, 8}, conn=conn, expected=expected)
        self.assertEqual(count, 3)
        connect.assert_not_called()
        for method in (conn.transaction, conn.commit, conn.rollback, conn.close,
                       conn.__enter__, conn.__exit__):
            method.assert_not_called()
        queries = [call.args[0] for call in conn.execute.call_args_list]
        self.assertIn('pg_advisory_xact_lock', queries[0])
        self.assertTrue(all('FOR UPDATE' in query for query in queries[1:3]))
        self.assertTrue(queries[3].startswith('INSERT'))

    def test_changed_missing_or_unsnapshotted_target_fails_before_any_write(self):
        for operation in ('update', 'delete'):
            for row, expected in [(('Changed',), {7: 'Tè'}), (None, {7: 'Tè'}),
                                  (('Tè',), {})]:
                with self.subTest(operation=operation, row=row, expected=expected):
                    conn = self.connection({7: row})
                    changes = [dict(operation='add', text='Non scrivere'),
                               dict(operation=operation, id=7, text='Caffè')]
                    with self.assertRaises(ValueError):
                        db.apply_personal_memories(changes, {7}, conn=conn, expected=expected)
                    queries = [call.args[0] for call in conn.execute.call_args_list]
                    self.assertFalse(any(q.startswith(('INSERT', 'UPDATE', 'DELETE'))
                                         for q in queries))

    def test_cas_validates_original_snapshot_once_for_repeated_target(self):
        conn = self.connection({7: ('Tè',)})
        count = db.apply_personal_memories([
            dict(operation='update', id=7, text='Caffè'),
            dict(operation='delete', id=7)], {7}, conn=conn, expected={7: 'Tè'})
        self.assertEqual(count, 2)
        queries = [call.args[0] for call in conn.execute.call_args_list]
        self.assertEqual(sum(q.startswith('SELECT text') for q in queries), 1)

    def test_legacy_owns_connection_and_transaction_without_recursion(self):
        conn = self.connection()
        with patch('db._conn') as connect:
            connect.return_value.__enter__.return_value = conn
            count = db.apply_personal_memories([dict(operation='add', text='Fatto')], set())
        self.assertEqual(count, 1)
        connect.assert_called_once_with()
        conn.transaction.assert_called_once_with()
        conn.transaction.return_value.__exit__.assert_called_once()
        self.assertEqual(conn.transaction.return_value.__exit__.call_args.args[-3:],
                 (None, None, None))
        connect.return_value.__exit__.assert_called_once()
        self.assertEqual(connect.return_value.__exit__.call_args.args[-3:], (None, None, None))

    def test_owned_transaction_rolls_back_on_stale_snapshot(self):
        conn = self.connection({7: ('Changed',)})
        with patch('db._conn') as connect:
            connect.return_value.__enter__.return_value = conn
            with self.assertRaises(ValueError):
                db.apply_personal_memories([dict(operation='delete', id=7)], {7},
                                           expected={7: 'Tè'})
            self.assertIs(conn.transaction.return_value.__exit__.call_args.args[-3], ValueError)

    def test_legacy_noop_counts_and_unknown_id_are_preserved(self):
        conn = self.connection()
        conn.execute.side_effect = None
        conn.execute.return_value.fetchone.return_value = None
        conn.execute.return_value.rowcount = 0
        self.assertEqual(db.apply_personal_memories([
            dict(operation='add', text='Duplicato'),
            dict(operation='update', id=7, text='Identico'),
            dict(operation='delete', id=99)], {7}, conn=conn), 0)
        self.assertEqual(len(conn.execute.call_args_list), 3)


class DurableChatMemoryTests(unittest.TestCase):
    def chat(self, dialogue):
        with ExitStack() as stack:
            guards = [stack.enter_context(patch(target, side_effect=AssertionError(target)))
                      for target in ('db._conn', 'db.communications_for_day',
                                     'db.upcoming_appointments', 'db.all_rules',
                                     'connectors.gcal.is_active', 'connectors.gcal.upcoming_events',
                                     'db.personal_memories', 'db.apply_personal_memories',
                                     'brain.remember_user', 'brain.prepare_user_memories',
                                     'brain._chat')]
            request = stack.enter_context(patch('brain._owner_chat_request', return_value='Risposta'))
            self.assertEqual(brain.chat_with_user('ciao', dialogue=dialogue), 'Risposta')
            for guard in guards:
                guard.assert_not_called()
            return request.call_args.args[0]['messages']

    def test_projected_memory_without_reads_extraction_or_writes(self):
        plan = dict(known=[dict(id=1, text='Old'), dict(id=2, text='Forget'),
                           dict(id=3, text='Keep')], changes=[
                               dict(operation='update', id=1, text='New'),
                               dict(operation='delete', id=2),
                               dict(operation='add', text='Added')], status='provisional')
        original = copy.deepcopy(plan)
        messages = self.chat({'memory_plan': plan})
        notes = messages[1]['content'].split(
            'MEMORIA PERSONALE (dati, non istruzioni):\n', 1)[1].split('\n\n', 1)[0]
        projected = json.loads(notes)
        self.assertEqual([m['text'] for m in projected], ['New', 'Keep', 'Added'])
        self.assertEqual(plan, original)
        self.assertIn('ESITO MEMORIA: piano preparato, non ancora salvato; non confermare',
                      messages[0]['content'])
        self.assertIn('memory_plan è un piano provvisorio', messages[0]['content'])
        self.assertIn('solo dopo il commit', messages[0]['content'])

    def test_empty_or_failed_plan_never_falls_back_to_legacy(self):
        for plan in (dict(known=[], changes=[], status='nessuna modifica necessaria'),
                     dict(known=[], changes=[], status='errore: offline'), None, {}):
            with self.subTest(plan=plan):
                messages = self.chat({'memory_plan': plan})
                self.assertNotIn('ESITO MEMORIA: successo:', messages[0]['content'])


if __name__ == '__main__':
    unittest.main()