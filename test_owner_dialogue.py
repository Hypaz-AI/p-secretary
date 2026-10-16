"""Offline durable-dialogue contracts; no production database or model calls."""
import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import db
import jobs
import owner_commitments
import owner_dialogue as od
import pipeline
from connectors import media_text


ANCHOR = datetime(2025, 12, 31, 21, 30, tzinfo=timezone.utc)
MID = 101
TOKEN = 'lease-token'


def exchange(mid, user='Domanda', response='Risposta', images=False):
    return dict(message_id=mid, message_time=ANCHOR, user_text=user,
                response=response, has_images=images)


def context(**overrides):
    result = dict(history=[], recent_ids=[], summary='Riepilogo precedente',
                  summarized_through=5, recalled=[], tasks=[])
    result.update(overrides)
    return result


def plan(**overrides):
    result = dict(reply='Risposta salvata', events=[], commitments=[], task_error='')
    result.update(overrides)
    return result


class OfflineTests(unittest.TestCase):
    def setUp(self):
        # A missed patch must fail locally rather than use DATABASE_URL or HTTP.
        self.conn = MagicMock(name='connection')
        self.conn.execute.return_value.fetchone.return_value = None
        self.conn.execute.return_value.fetchall.return_value = []
        self.connect = self.mock('db._conn')
        self.connect.return_value.__enter__.return_value = self.conn
        self.mock('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden'))
        self.mock('ai_budget.request_content', side_effect=AssertionError('Model forbidden'))
        if db.psycopg is not None:
            self.mock('db.psycopg.connect', side_effect=AssertionError('PostgreSQL forbidden'))
        self.mock('config.TIMEZONE', 'Europe/Rome')

    def mock(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def statements(self):
        return [c.args for c in self.conn.execute.call_args_list]


class WindowTests(OfflineTests):
    def test_empty_and_zero_budget(self):
        self.assertEqual(od.recent_window([]), ([], []))
        self.assertEqual(od.recent_window([exchange(1)], budget=0), ([], []))

    def test_complete_pairs_newest_selection_chronological_output(self):
        rows = [exchange(i, f'Domanda {i}', f'Risposta {i}') for i in range(1, 5)]
        full, _ = od.recent_window(rows)
        budget = sum(od.tokens(m['content']) for m in full[-4:])
        history, ids = od.recent_window(rows, budget=budget)
        self.assertEqual(ids, [3, 4])
        self.assertEqual([m['role'] for m in history], ['user', 'assistant'] * 2)
        self.assertEqual(history[-1]['content'], 'Risposta 4')
        self.assertIn(str(ANCHOR), history[0]['content'])
        self.assertLessEqual(sum(od.tokens(m['content']) for m in history), budget)

    def test_normal_unicode_pairs_fit_estimated_budget_without_mutating_originals(self):
        rows = [exchange(i, 'Perché caffè ☕ 日本語', 'Sì 👍') for i in range(1, 10)]
        original = copy.deepcopy(rows)
        history, ids = od.recent_window(rows, budget=250)
        self.assertTrue(ids)
        self.assertEqual(len(history), len(ids) * 2)
        self.assertLessEqual(sum(od.tokens(m['content']) for m in history), 250)
        self.assertEqual(rows, original)

    def test_oversized_latest_is_explicit_complete_excerpt_not_older_substitute(self):
        rows = [exchange(1), exchange(2, 'x' * 20000, 'y' * 20000)]
        original = copy.deepcopy(rows)
        history, ids = od.recent_window(rows, budget=400)
        self.assertEqual(ids, [2])
        self.assertEqual([m['role'] for m in history], ['user', 'assistant'])
        self.assertTrue(all('[Estratto: originale nello storico]' in m['content'] for m in history))
        self.assertLessEqual(sum(od.tokens(m['content']) for m in history), 400)
        self.assertEqual(rows, original)
        self.assertEqual(od.recent_window(rows, budget=199), ([], []))

    def test_oversized_unicode_excerpt_respects_estimated_budget(self):
        history, ids = od.recent_window([exchange(1, '😀' * 20000, '😀' * 20000)], budget=400)
        self.assertEqual(ids, [1])
        self.assertLessEqual(sum(od.tokens(m['content']) for m in history), 400)

    def test_token_estimator_counts_utf8_and_image_allowance(self):
        self.assertEqual(od.tokens(''), 8)
        self.assertEqual(od.tokens('abc'), 9)
        self.assertGreater(od.tokens('😀' * 10), od.tokens('a' * 10))
        parts = [dict(type='text', text='abc'), dict(type='image_url', image_url={'url': 'ignored'})]
        self.assertEqual(od.tokens(parts), 1800 + od.tokens('abc'))


class SearchTests(OfflineTests):
    def test_stopwords_empty_numeric_and_short_words(self):
        self.assertEqual(od.search_query('Come cosa quando dove ricordi oggi domani grazie? 123 a xy'), '')

    def test_unicode_case_deduplication_and_sixteen_term_cap(self):
        self.assertEqual(od.search_query('Caffè CAFFÈ contratto'), 'caffè OR contratto')
        text = ' '.join('parola' + chr(97 + i) for i in range(20))
        self.assertEqual(len(od.search_query(text).split(' OR ')), 16)

    def test_short_query_uses_newest_user_history_not_assistant_or_date_header(self):
        history = [dict(role='user', content='Vecchio archivio'),
                   dict(role='assistant', content='segreto assistente'),
                   dict(role='user', content='[Messaggio del 2025-12-31]\nContratto banca scadenza firma allegato')]
        self.assertEqual(od.search_query('Ricordi?', history),
                         'contratto OR banca OR scadenza OR firma OR allegato')

    def test_two_current_terms_do_not_pull_unrelated_history(self):
        history = [dict(role='user', content='Segreto storico')]
        self.assertEqual(od.search_query('Contratto banca', history), 'contratto OR banca')

    def test_multimodal_history_is_ignored_for_query_terms(self):
        self.assertEqual(od.search_query('Ricordi?', [dict(role='user', content=[dict(type='text', text='banca')])]), '')

    def test_empty_recall_never_queries_database(self):
        self.assertEqual(od.recall(self.conn, 'Grazie', [], MID, []), [])
        self.conn.execute.assert_not_called()

    def test_recall_parameterization_filters_ranking_and_original_dates(self):
        text = "contratto'); DROP TABLE owner_exchanges; --"
        query = od.search_query(text)
        self.conn.execute.return_value.fetchall.return_value = [(9, ANCHOR, 'Contratto originale', None)]
        result = od.recall(self.conn, text, [], MID, [98, 99])
        sql, params = self.conn.execute.call_args.args
        self.assertEqual(params, (query, MID, [98, 99]))
        self.assertNotIn(text, sql)
        self.assertNotIn(query, sql)
        for fragment in ('websearch_to_tsquery', 'completed_at IS NOT NULL', 'message_id < %s',
                         'ANY(%s::bigint[])', 'ts_rank_cd', 'message_id DESC LIMIT 8'):
            self.assertIn(fragment, sql)
        self.assertEqual(result, [dict(message_id=9, date=str(ANCHOR), owner='Contratto originale', assistant='')])

    def test_recall_excerpts_and_total_unicode_budget(self):
        self.conn.execute.return_value.fetchall.return_value = [
            (i, ANCHOR, 'à' * 8000, 'a' * 4000) for i in range(8)]
        result = od.recall(self.conn, 'contratto', [], MID, [])
        self.assertTrue(result)
        self.assertLess(len(result), 8)
        self.assertEqual(len(result[0]['owner']), 4500)
        self.assertEqual(len(result[0]['assistant']), 2000)
        self.assertLessEqual(sum(od.tokens(json.dumps(e, ensure_ascii=False)) for e in result), od.RECALL_TOKENS)

    def test_recall_stops_before_first_overbudget_entry(self):
        self.conn.execute.return_value.fetchall.return_value = [(1, ANCHOR, 'x', 'y')]
        with patch.object(od, 'RECALL_TOKENS', 1):
            self.assertEqual(od.recall(self.conn, 'contratto', [], MID, []), [])


class MigrationTests(OfflineTests):
    def test_first_migration_locks_and_records_version_in_transaction(self):
        od.migrate(self.conn)
        self.conn.transaction.assert_called_once_with()
        statements = self.statements()
        self.assertIn('pg_advisory_xact_lock', statements[0][0])
        self.assertEqual(statements[1][1], (od.VERSION,))
        self.assertEqual(statements[2], (od.SCHEMA,))
        self.assertEqual(statements[3][1], (od.VERSION,))
        self.conn.commit.assert_not_called()
        self.assertIn('ADD COLUMN IF NOT EXISTS owner_deadline_date DATE', od.SCHEMA)
        self.assertIn('CREATE UNIQUE INDEX IF NOT EXISTS owner_task_operation', od.SCHEMA)
        self.assertNotIn('DROP TABLE', od.SCHEMA)

    def test_second_migration_is_noop_after_version_check(self):
        self.conn.execute.return_value.fetchone.side_effect = [None, (1,)]
        od.migrate(self.conn)
        od.migrate(self.conn)
        self.assertEqual(sum(c.args == (od.SCHEMA,) for c in self.conn.execute.call_args_list), 1)
        self.assertEqual(len(self.statements()), 6)

    def test_schema_error_propagates_for_transaction_rollback(self):
        def execute(sql, *args):
            if sql == od.SCHEMA:
                raise RuntimeError('DDL failed')
            return MagicMock(**{'fetchone.return_value': None})
        self.conn.execute.side_effect = execute
        with self.assertRaisesRegex(RuntimeError, 'DDL failed'):
            od.migrate(self.conn)
        self.assertIs(self.conn.transaction.return_value.__exit__.call_args.args[0], RuntimeError)
        self.assertFalse(any('INSERT INTO schema_migrations' in s[0] for s in self.statements()))


class ContextTests(OfflineTests):
    def load(self, rows, tasks=()):
        self.conn.execute.return_value.fetchone.return_value = ('Summary', 5)
        self.conn.execute.return_value.fetchall.side_effect = [rows, list(tasks)]
        with patch.object(od, 'recall', return_value=[{'message_id': 1}]) as recall:
            result = od.load_context(MID, 'contratto')
        return result, recall

    def test_context_completed_prior_turns_chronological_tasks_and_summary(self):
        rows = [(9, ANCHOR, 'Nuovo', 'Risposta nuova', False),
                (8, ANCHOR, 'Vecchio', 'Risposta vecchia', False)]
        task = (7, 'Pagare', None, 'open', '2026-01-01', None, 3)
        result, recall = self.load(rows, [task])
        self.assertEqual(result['recent_ids'], [8, 9])
        self.assertIn('Vecchio', result['history'][0]['content'])
        self.assertEqual((result['summary'], result['summarized_through']), ('Summary', 5))
        self.assertEqual(result['tasks'][0]['version'], 3)
        self.assertEqual(result['tasks'][0]['owner_deadline_date'], '2026-01-01')
        recall.assert_called_once_with(self.conn, 'contratto', result['history'], MID, [8, 9])
        statements = self.statements()
        self.assertIn('completed_at IS NOT NULL AND message_id < %s', statements[1][0])
        self.assertIn('ORDER BY message_id DESC LIMIT 100', statements[1][0])
        self.assertEqual(statements[1][1], (MID,))
        self.assertIn("status='open'", statements[2][0])
        self.assertIn('LIMIT 200', statements[2][0])
        self.assertEqual(statements[2][1], ('Europe/Rome',))

    def test_empty_context(self):
        result, _ = self.load([])
        self.assertEqual(result['history'], [])
        self.assertEqual(result['tasks'], [])

    def test_only_last_two_recent_photos_are_rehydrated_without_persisting_urls(self):
        rows = [(i, ANCHOR, f'Foto {i}', 'Vista', True) for i in (3, 2, 1)]
        parts = [dict(type='image_url', image_url={'url': 'data:image/png;base64,FAKE'})]
        with patch.object(pipeline, 'load_message', return_value=(None, ['attachment'])) as load, \
                patch.object(media_text, 'image_parts', return_value=parts):
            result, _ = self.load(rows)
        self.assertEqual(load.call_args_list, [call(2), call(3)])
        self.assertIsInstance(result['history'][0]['content'], str)
        self.assertEqual(result['history'][2]['content'][1:], parts)
        self.assertFalse(any('UPDATE' in s[0] or 'INSERT' in s[0] for s in self.statements()))

    def test_unavailable_photo_does_not_discard_text(self):
        with patch.object(pipeline, 'load_message', side_effect=OSError('missing archive')), \
                self.assertLogs(od.logger, level='WARNING'):
            result, _ = self.load([(1, ANCHOR, 'Descrizione', 'Risposta', True)])
        self.assertIsInstance(result['history'][0]['content'], str)
        self.assertIn('Descrizione', result['history'][0]['content'])

    def test_photo_is_skipped_when_image_budget_would_overflow(self):
        parts = [dict(type='image_url')] * 10
        with patch.object(pipeline, 'load_message', return_value=(None, [])), \
                patch.object(media_text, 'image_parts', return_value=parts):
            result, _ = self.load([(1, ANCHOR, 'Foto', 'Risposta', True)])
        self.assertIsInstance(result['history'][0]['content'], str)


class LeaseTests(OfflineTests):
    def test_acquire_checks_earlier_jobs_before_lease_and_uses_archived_time(self):
        persisted = plan()
        self.conn.execute.return_value.fetchone.side_effect = [None, (1,), (ANCHOR,), (ANCHOR, persisted, None)]
        with patch.object(od.uuid, 'uuid4', return_value=MagicMock(hex=TOKEN)):
            token, row = od.acquire(MID, 'Originale', True)
        self.assertEqual((token, row), (TOKEN, (ANCHOR, persisted, None)))
        statements = self.statements()
        self.assertIn("status IN ('pending','running')", statements[0][0])
        self.assertIn('AND id <', statements[0][0])
        self.assertEqual(statements[0][1], ('owner:101',))
        self.assertIn("interval '12 minutes'", statements[1][0])
        self.assertIn('token IS NULL OR lease_until<now()', statements[1][0])
        self.assertEqual(statements[1][1], (TOKEN,))
        self.assertIn('COALESCE(ts,acquired_at)', statements[2][0])
        self.assertEqual(statements[3][1], (MID, ANCHOR, 'Originale', True))
        self.assertIn('ON CONFLICT(message_id) DO NOTHING', statements[3][0])
        self.conn.transaction.assert_called_once_with()

    def test_earlier_pending_turn_defers_without_acquiring_or_inserting(self):
        self.conn.execute.return_value.fetchone.return_value = (1,)
        with self.assertRaisesRegex(jobs.Deferred, 'Earlier'):
            od.acquire(MID, 'text', False)
        self.assertEqual(len(self.statements()), 1)

    def test_busy_lease_defers_without_reading_or_inserting_exchange(self):
        self.conn.execute.return_value.fetchone.side_effect = [None, None]
        with self.assertRaisesRegex(jobs.Deferred, 'busy'):
            od.acquire(MID, 'text', False)
        self.assertEqual(len(self.statements()), 2)

    def test_missing_archived_message_rolls_back_acquisition(self):
        self.conn.execute.return_value.fetchone.side_effect = [None, (1,), None]
        with self.assertRaisesRegex(ValueError, 'not archived'):
            od.acquire(MID, 'text', False)
        self.assertIs(self.conn.transaction.return_value.__exit__.call_args.args[0], ValueError)
        self.assertFalse(any('INSERT' in s[0] for s in self.statements()))

    def test_fence_requires_matching_live_token_and_locks_state(self):
        with self.assertRaisesRegex(jobs.Deferred, 'lease expired'):
            od._fence(self.conn, 'stale')
        sql, params = self.conn.execute.call_args.args
        self.assertEqual(params, ('stale',))
        self.assertIn('token=%s', sql)
        self.assertIn('lease_until>now() FOR UPDATE', sql)
        self.conn.execute.return_value.fetchone.return_value = (1,)
        od._fence(self.conn, TOKEN)


class RespondTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.acquire = self.mock('owner_dialogue.acquire', return_value=(TOKEN, (ANCHOR, None, None)))
        self.load = self.mock('owner_dialogue.load_context', return_value=context())
        self.real_compact = od.compact
        self.compact = self.mock('owner_dialogue.compact')
        self.prepare = self.mock('owner_commitments.prepare', return_value=[])
        self.memory_prepare = self.mock('brain.prepare_user_memories', return_value=dict(
            changes=[], known=[], status='nessuna modifica necessaria'))
        self.apply = self.mock('owner_commitments.apply', return_value=dict(confirmations=[]))
        self.chat = self.mock('brain.chat_with_user', return_value='Risposta modello')
        self.parse = self.mock('connectors.gcal.parse_markers', return_value=('Risposta modello', []))
        self.validate = self.mock('connectors.gcal.validate_event', side_effect=lambda event: event)
        self.save = self.mock('pipeline.save_event', return_value=42)
        self.enqueue = self.mock('jobs.enqueue')
        self.notify = self.mock('connectors.telegram.notify', side_effect=AssertionError('Direct send forbidden'))
        self.conn.execute.return_value.fetchone.return_value = (1,)

    def persisted(self, saved=None, response=None):
        self.acquire.return_value = (TOKEN, (ANCHOR, plan() if saved is None else saved, response))

    def assert_released(self):
        sql, params = self.conn.execute.call_args.args
        self.assertIn('SET token=NULL,lease_until=NULL', sql)
        self.assertIn('WHERE id=1 AND token=%s', sql)
        self.assertEqual(params, (TOKEN,))

    def test_persisted_plan_skips_ai_and_extraction(self):
        saved = plan()
        self.persisted(saved)
        self.assertEqual(od.respond('retry', MID), saved['reply'])
        self.chat.assert_not_called()
        self.prepare.assert_not_called()
        self.parse.assert_not_called()
        self.apply.assert_called_once_with(self.conn, saved['commitments'], MID)
        self.assertFalse(any('SET plan=' in s[0] for s in self.statements()))
        self.compact.assert_called_once_with(MID, self.load.return_value, TOKEN)
        self.assert_released()

    def test_completed_response_replay_only_enqueues_same_chunks(self):
        response = 'à' * 7001
        self.persisted(response=response)
        for _ in range(2):
            self.assertEqual(od.respond('retry', MID), response)
        for mock in (self.load, self.chat, self.prepare, self.apply, self.save, self.compact, self.notify):
            mock.assert_not_called()
        expected = [call(self.conn, 'action', f'owner-reply:{MID}:{i}',
                        dict(type='notify', text=response[start:start + 3500], html=False,
                            owner_reply_mid=MID, owner_reply_chunk=i))
                    for i, start in enumerate(range(0, len(response), 3500))]
        self.assertEqual(self.enqueue.call_args_list, expected * 2)
        self.assertFalse(any('UPDATE owner_exchanges' in s[0] for s in self.statements()))
        self.assert_released()

    def test_new_plan_receives_original_time_context_and_images_before_persist(self):
        images = [dict(type='image_url')]
        changes = [{'prepared': True}]
        self.prepare.return_value = changes
        self.assertEqual(od.respond('Richiesta', MID, images), 'Risposta modello')
        self.acquire.assert_called_once_with(MID, 'Richiesta', True)
        self.prepare.assert_called_once_with('Richiesta', [], ANCHOR)
        dialogue = self.chat.call_args.kwargs['dialogue']
        self.assertEqual(dialogue['message_time'], str(ANCHOR))
        self.assertEqual(dialogue['summary'], self.load.return_value['summary'])
        self.assertEqual(dialogue['commitments']['changes'], changes)
        self.assertEqual(self.chat.call_args.kwargs['images'], images)
        sql, params = next(s for s in self.statements() if 'SET plan=' in s[0])
        self.assertIn('AND plan IS NULL', sql)
        self.assertEqual(params[1], MID)
        self.assertEqual(json.loads(params[0])['commitments'], changes)
        self.assertEqual(self.conn.transaction.call_count, 2)
        self.notify.assert_not_called()

    def test_effects_reply_and_outbox_share_fenced_transaction_with_stable_event_keys(self):
        events = [dict(title='Visita', dt='2026-01-02 15:00'), dict(title='Riunione', dt='2026-01-03 10:00')]
        saved = plan(events=events, commitments=[{'prepared': True}])
        self.persisted(saved)
        self.apply.return_value = dict(confirmations=['Attività #7 salvata.'])
        trace = []
        tx = self.conn.transaction.return_value
        tx.__enter__.side_effect = lambda: trace.append('begin')
        tx.__exit__.side_effect = lambda *args: trace.append('end') or False
        self.apply.side_effect = lambda *args: trace.append('tasks') or dict(confirmations=['Attività #7 salvata.'])
        self.save.side_effect = lambda *args: trace.append('event') or 42
        self.enqueue.side_effect = lambda *args: trace.append('outbox')
        execute = self.conn.execute.return_value
        def sql(statement, *args):
            if 'FOR UPDATE' in statement:
                trace.append('fence')
            if 'SET response=' in statement:
                trace.append('response')
            return execute
        self.conn.execute.side_effect = sql
        response = od.respond('Richiesta', MID)
        self.assertEqual(trace, ['begin', 'fence', 'tasks', 'event', 'event', 'response', 'outbox', 'end'])
        self.assertEqual(self.save.call_args_list,
                         [call(self.conn, event, f'owner:{MID}:event:{i}', 'dialogo utente')
                          for i, event in enumerate(events)])
        self.assertIn('Attività #7 salvata.', response)
        self.assertIn('non ancora confermata', response)
        self.assertNotIn(saved['reply'], response)
        self.conn.commit.assert_not_called()

    def test_clarification_replaces_speculative_prose_and_blocks_all_events(self):
        self.prepare.side_effect = owner_commitments.Clarification('Quale contratto?')
        self.parse.return_value = ('Fatto!', [dict(title='Speculativo')])
        self.assertEqual(od.respond('Spostalo', MID), 'Quale contratto?')
        self.apply.assert_called_once_with(self.conn, [], MID)
        self.save.assert_not_called()
        self.validate.assert_not_called()
        saved = json.loads(next(s for s in self.statements() if 'SET plan=' in s[0])[1][0])
        self.assertEqual(saved['events'], [])
        self.assertEqual(saved['commitments'], [])

    def test_extraction_error_is_explicit_no_new_tasks_not_false_success(self):
        self.prepare.side_effect = RuntimeError('model unavailable')
        with self.assertLogs(od.logger, level='WARNING'):
            response = od.respond('Richiesta', MID)
        self.assertIn('nessuna nuova attività salvata', response)
        self.apply.assert_called_once_with(self.conn, [], MID)
        self.assertIn('non disponibile', self.chat.call_args.kwargs['dialogue']['commitments']['error'])

    def test_stale_task_plan_finishes_with_durable_clarification(self):
        self.persisted(plan(commitments=[{'prepared': True}], events=[dict(title='Do not create')]))
        self.apply.side_effect = owner_commitments.StaleBatch('L’attività è cambiata: confermi?')
        response = od.respond('Spostala', MID)
        self.assertEqual(response, 'L’attività è cambiata: confermi?')
        self.save.assert_not_called()
        self.assertTrue(any('SET response=' in s[0] for s in self.statements()))
        self.assertTrue(self.enqueue.called)
        self.assert_released()

    def test_memory_is_applied_only_inside_fenced_effects_transaction(self):
        memory = dict(known=[dict(id=3, text='Preferenza precedente')],
                      changes=[dict(operation='update', id=3, text='Nuova preferenza')])
        self.persisted(plan(memory_plan=memory))
        with patch('db.apply_personal_memories', return_value=1) as apply_memory:
            response = od.respond('Correggi la preferenza', MID)
        apply_memory.assert_called_once_with(memory['changes'], {3}, conn=self.conn,
                                             expected=memory['known'])
        self.assertIn('Memoria personale aggiornata', response)

    def test_stale_memory_is_not_confirmed(self):
        self.persisted(plan(memory_plan=dict(known=[], changes=[dict(operation='delete', id=3)])))
        with patch('db.apply_personal_memories', side_effect=ValueError('stale')):
            response = od.respond('Dimentica', MID)
        self.assertIn('confermare nuovamente', response)
        self.assertNotIn('Memoria personale aggiornata', response)

    def test_invalid_event_produces_failure_confirmation_without_save(self):
        self.persisted(plan(events=[dict(title='Invalid')]))
        self.validate.side_effect = ValueError('invalid date')
        self.assertIn('Appuntamento non creato: invalid date', od.respond('Richiesta', MID))
        self.save.assert_not_called()

    def test_blank_reply_gets_explicit_fallback(self):
        self.persisted(plan(reply='  '))
        self.assertIn('puoi precisare', od.respond('Richiesta', MID))

    def test_expired_token_prevents_prepared_plan_write_and_effects(self):
        self.conn.execute.return_value.fetchone.return_value = None
        with self.assertRaises(jobs.Deferred):
            od.respond('Richiesta', MID)
        self.apply.assert_not_called()
        self.enqueue.assert_not_called()
        self.assertFalse(any('UPDATE owner_exchanges' in s[0] for s in self.statements()))
        self.assert_released()

    def test_expiry_between_plan_and_effects_prevents_effects_but_preserves_plan(self):
        self.conn.execute.return_value.fetchone.side_effect = [(1,), None]
        with self.assertRaises(jobs.Deferred):
            od.respond('Richiesta', MID)
        self.assertTrue(any('SET plan=' in s[0] for s in self.statements()))
        self.assertFalse(any('SET response=' in s[0] for s in self.statements()))
        self.apply.assert_not_called()
        self.enqueue.assert_not_called()
        self.assert_released()

    def test_stale_replay_is_fenced_before_outbox(self):
        self.persisted(response='Already done')
        self.conn.execute.return_value.fetchone.return_value = None
        with self.assertRaises(jobs.Deferred):
            od.respond('retry', MID)
        self.enqueue.assert_not_called()
        self.assert_released()

    def test_effect_failure_rolls_back_response_and_delivery_and_releases(self):
        self.persisted(plan(events=[dict(title='Visita', dt='2026-01-02 15:00')]))
        self.save.side_effect = RuntimeError('save failed')
        with self.assertRaisesRegex(RuntimeError, 'save failed'):
            od.respond('Richiesta', MID)
        self.assertIs(self.conn.transaction.return_value.__exit__.call_args.args[0], RuntimeError)
        self.assertFalse(any('SET response=' in s[0] for s in self.statements()))
        self.enqueue.assert_not_called()
        self.compact.assert_not_called()
        self.assert_released()

    def test_outbox_failure_rolls_back_effect_transaction(self):
        self.persisted()
        self.enqueue.side_effect = RuntimeError('outbox unavailable')
        with self.assertRaisesRegex(RuntimeError, 'outbox unavailable'):
            od.respond('Richiesta', MID)
        self.assertIs(self.conn.transaction.return_value.__exit__.call_args.args[0], RuntimeError)
        self.compact.assert_not_called()
        self.assert_released()

    def test_chat_failure_never_persists_plan_or_effects_and_releases(self):
        self.chat.side_effect = RuntimeError('chat unavailable')
        with self.assertRaisesRegex(RuntimeError, 'chat unavailable'):
            od.respond('Richiesta', MID)
        self.apply.assert_not_called()
        self.enqueue.assert_not_called()
        self.assertFalse(any('UPDATE owner_exchanges' in s[0] for s in self.statements()))
        self.assert_released()

    def test_summary_model_failure_after_commit_keeps_reply_and_old_checkpoint(self):
        # Exercise the real compactor rather than a mocked successful checkpoint.
        self.persisted()
        self.compact.side_effect = self.real_compact
        self.conn.execute.return_value.fetchall.return_value = [(6, ANCHOR, 'Domanda', 'Risposta')]
        with patch('brain._chat', side_effect=RuntimeError('summary unavailable')), \
                self.assertLogs(od.logger, level='WARNING'):
            response = od.respond('Richiesta', MID)
        self.assertEqual(response, 'Risposta salvata')
        self.enqueue.assert_called_once()
        self.assertTrue(any('SET response=' in s[0] for s in self.statements()))
        self.assertFalse(any('SET summary=' in s[0] for s in self.statements()))
        self.assertEqual(self.load.return_value['summarized_through'], 5)
        self.assert_released()


class SummaryTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.chat = self.mock('brain._chat', return_value='  Nuovo riepilogo  ')
        self.ctx = context(recent_ids=[30, 31])
        self.conn.execute.return_value.fetchall.return_value = [(6, ANCHOR, 'Domanda', 'Risposta')]

    def updates(self):
        return [s for s in self.statements() if 'UPDATE owner_dialogue_state' in s[0]]

    def test_incremental_checkpoint_uses_only_unsummarized_complete_older_turns(self):
        od.compact(MID, self.ctx, TOKEN)
        sql, params = self.statements()[0]
        self.assertEqual(params, (5, 30))
        self.assertIn('completed_at IS NOT NULL', sql)
        self.assertIn('message_id>%s AND message_id<%s', sql)
        self.assertIn('ORDER BY message_id LIMIT 20', sql)
        self.assertEqual(self.chat.call_args.kwargs, dict(max_tokens=od.SUMMARY_TOKENS))
        payload = json.loads(self.chat.call_args.args[1])
        self.assertEqual(payload['previous'], self.ctx['summary'])
        self.assertEqual(payload['exchanges'][0][1], str(ANCHOR))
        update, args = self.updates()[0]
        self.assertEqual(args, ('Nuovo riepilogo', 6, TOKEN, 5))
        for fragment in ('token=%s', 'lease_until>now()', 'summarized_through=%s'):
            self.assertIn(fragment, update)

    def test_without_recent_ids_cutoff_is_current_message(self):
        od.compact(MID, context(), TOKEN)
        self.assertEqual(self.statements()[0][1], (5, MID))

    def test_no_rows_does_not_call_model_or_advance_checkpoint(self):
        self.conn.execute.return_value.fetchall.return_value = []
        od.compact(MID, self.ctx, TOKEN)
        self.chat.assert_not_called()
        self.assertEqual(self.updates(), [])

    def test_model_failure_preserves_previous_summary_and_checkpoint(self):
        self.chat.side_effect = RuntimeError('model unavailable')
        original = copy.deepcopy(self.ctx)
        with self.assertLogs(od.logger, level='WARNING'):
            od.compact(MID, self.ctx, TOKEN)
        self.assertEqual(self.updates(), [])
        self.assertEqual(self.ctx, original)

    def test_invalid_summary_does_not_advance_checkpoint(self):
        for value in (None, {}, '', '   ', 'x' * 6501):
            with self.subTest(value=type(value)):
                self.conn.reset_mock()
                self.chat.return_value = value
                od.compact(MID, self.ctx, TOKEN)
                self.assertEqual(self.updates(), [])

    def test_checkpoint_write_failure_is_deferred_not_fatal(self):
        cursor = self.conn.execute.return_value
        def execute(sql, *args):
            if 'UPDATE owner_dialogue_state' in sql:
                raise RuntimeError('write failed')
            return cursor
        self.conn.execute.side_effect = execute
        with self.assertLogs(od.logger, level='WARNING'):
            od.compact(MID, self.ctx, TOKEN)
        self.assertEqual(self.ctx['summarized_through'], 5)

    def test_stale_token_checkpoint_is_conditional_without_retry_or_unfenced_write(self):
        self.conn.execute.return_value.rowcount = 0
        od.compact(MID, self.ctx, 'expired-token')
        self.assertEqual(len(self.updates()), 1)
        self.assertEqual(self.updates()[0][1], ('Nuovo riepilogo', 6, 'expired-token', 5))
        self.assertEqual(self.ctx['summarized_through'], 5)

    def test_input_excerpts_batch_budget_and_checkpoint_tracks_last_included_row(self):
        self.conn.execute.return_value.fetchall.return_value = [
            (i, ANCHOR, 'a' * 20000, 'b' * 10000) for i in range(6, 26)]
        od.compact(MID, self.ctx, TOKEN)
        entries = json.loads(self.chat.call_args.args[1])['exchanges']
        self.assertEqual(len(entries), 1)
        self.assertEqual((len(entries[0][2]), len(entries[0][3])), (12000, 6000))
        self.assertLessEqual(sum(od.tokens(json.dumps(e, ensure_ascii=False)) for e in entries), od.RECENT_TOKENS)
        self.assertEqual(self.updates()[0][1][1], 6)


if __name__ == '__main__':
    unittest.main()