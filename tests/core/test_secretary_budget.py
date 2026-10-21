"""No provider calls: bounded Worker ledger, SQL only with explicit *_test URL."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from secretary.core import ai_budget as budget, config, db, jobs
from tests.support import completion, job_context, postgres_fixture

PostgreSQLFixture = postgres_fixture()


PAYLOAD = dict(model='offline-model', max_tokens=32, messages=[dict(role='user', content='Private draft')])


def response(content='ok', finish='stop', usage=None):
    result = dict(choices=[dict(finish_reason=finish, message=dict(content=content))])
    if usage is not None:
        result['usage'] = usage
    mocked = MagicMock()
    mocked.__enter__.return_value.read.return_value = json.dumps(result).encode()
    return mocked


class BudgetPureTests(unittest.TestCase):
    def setUp(self):
        self.http = self.enterContext(patch('urllib.request.urlopen', return_value=completion('ok')))
        self.connection = self.enterContext(patch.object(db, '_conn', side_effect=AssertionError('No DB')))

    def test_direct_local_legacy_calls_do_not_connect_or_need_migration(self):
        with patch.object(db, 'available', return_value=False):
            self.assertEqual(budget.request_content(PAYLOAD), 'ok')
        self.connection.assert_not_called()
        self.http.assert_called_once()

    def test_worker_fails_closed_without_db_or_missing_ledger(self):
        with job_context():
            for available in (False, True):
                with patch.object(db, 'available', return_value=available), self.assertRaises(budget.BudgetUnavailable):
                    budget.request_content(PAYLOAD)
        self.assertEqual(budget._inflight, 0)
        self.http.assert_not_called()

    def test_invalid_limits_and_output_fail_before_provider(self):
        for value in ('0', '-1', 'unlimited', '1.5', '99999999999999999999999999'):
            with patch.dict('os.environ', AI_BUDGET_DAILY_CALLS=value), job_context():
                with self.assertRaises(budget.BudgetUnavailable):
                    budget.request_content(PAYLOAD)
        for value in (None, 0, -1, True, '20'):
            with job_context(), self.assertRaises(budget.BudgetUnavailable):
                budget.request_content(dict(PAYLOAD, max_tokens=value))
        self.http.assert_not_called()

    def test_finite_defaults(self):
        with patch.object(config, 'get', side_effect=lambda key, default: default):
            self.assertEqual(budget._limits(), dict(DAILY_CALLS=200, DAILY_TOKENS=500000,
                                                   MAX_INFLIGHT=2, MAX_OUTPUT_TOKENS=65536,
                                                   DAILY_COST=Decimal(0),
                                                   SCOPE_agent_owner=0, SCOPE_agent_inbox=0))

    def test_thread_cap_fails_before_reserve_and_releases_after_settlement_error(self):
        with patch.object(budget, '_inflight', 2), job_context():
            with patch.object(budget, '_reserve') as reserve, self.assertRaises(budget.BudgetExceeded):
                budget.request_content(PAYLOAD)
            reserve.assert_not_called()
        with patch.object(budget, '_reserve', return_value='reserved'), \
                patch.object(budget, '_settle', side_effect=budget.BudgetUnavailable('offline')), \
                patch.object(jobs, 'assert_current'), job_context():
            with self.assertRaises(budget.BudgetUnavailable):
                budget.request_content(PAYLOAD)
        self.assertEqual(budget._inflight, 0)

    def test_provider_usage_cost_sanitization_and_missing_cost_not_zero(self):
        self.assertEqual(budget._reported({}), (None, None, None, None))
        result = budget._reported(dict(usage=dict(prompt_tokens=10, completion_tokens=20,
                                                 total_tokens=1, cost='0.0025')))
        self.assertEqual(result, (10, 20, 30, Decimal('0.0025')))
        for value in ('NaN', 'Infinity', '-1', 'secret', True, float('inf')):
            self.assertIsNone(budget._reported(dict(usage=dict(cost=value)))[3])
        self.assertIsNone(budget._reported(dict(usage=dict(total_tokens=True)))[2])


class BudgetPostgresTests(PostgreSQLFixture):
    def setUp(self):
        super().setUp()
        budget.migrate(self.conn)
        self.enterContext(patch.object(db, 'available', return_value=True))
        self.http = self.enterContext(patch('urllib.request.urlopen', return_value=response()))
        self.settings = dict(AI_BUDGET_DAILY_CALLS='200', AI_BUDGET_DAILY_TOKENS='500000',
                             AI_BUDGET_MAX_INFLIGHT='2', AI_BUDGET_MAX_OUTPUT_TOKENS='65536')
        self.enterContext(patch.dict('os.environ', self.settings))

    def running(self):
        import uuid
        jobs.enqueue(self.conn, 'offline_budget', uuid.uuid4().hex, {})
        return jobs.claim('offline_budget')

    def request(self, job=None, payload=None):
        job = job or self.running()
        with job_context(job[0], job[1], job[3], kind='offline_budget'):
            return budget.request_content(payload or PAYLOAD)

    def test_migration_is_idempotent_and_rollback_respected(self):
        budget.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM schema_migrations WHERE version=%s',
                                           (budget.VERSION,)).fetchone()[0], 1)
        self.assertEqual(budget.usage(self.conn)['calls'], 0)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.conn.transaction():
                self.conn.execute('DROP TABLE secretary_ai_usage')
                self.conn.execute('DELETE FROM schema_migrations WHERE version=%s', (budget.VERSION,))
                budget.migrate(self.conn)
                raise RuntimeError('rollback')
        self.assertEqual(budget.usage(self.conn)['calls'], 0)

    def test_legacy_ledger_without_scope_column_is_backfilled(self):
        # Un ledger v1 esistente non ha 'scope': VERSION invariata non basta,
        # serve la migrazione runtime o ogni chiamata AI fallisce su quel DB.
        self.conn.execute('DELETE FROM schema_migrations WHERE version=%s', (budget.RUNTIME_VERSION,))
        self.conn.execute('ALTER TABLE secretary_ai_usage DROP COLUMN scope')
        with self.assertRaises(Exception):
            self.conn.execute('SELECT scope FROM secretary_ai_usage')
        budget.migrate(self.conn)
        self.assertEqual(self.conn.execute("""SELECT count(*) FROM information_schema.columns
            WHERE table_name='secretary_ai_usage' AND column_name='scope'""").fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM schema_migrations WHERE version=%s',
                                          (budget.RUNTIME_VERSION,)).fetchone()[0], 1)
        self.assertEqual(self.request(), 'ok')
        budget.migrate(self.conn)  # idempotente: la colonna esiste già

    def test_call_limit_survives_fresh_connection_and_module_local_counter_reset(self):
        with patch.dict('os.environ', AI_BUDGET_DAILY_CALLS='1'):
            self.assertEqual(self.request(), 'ok')
            with self.fresh_connection() as conn:
                self.assertEqual(budget.usage(conn)['calls'], 1)
            with patch.object(budget, '_inflight', 0), self.assertRaises(budget.BudgetExceeded):
                self.request()
        self.assertEqual(self.http.call_count, 1)

    def test_reserved_output_input_counts_toward_daily_token_limit(self):
        with patch.dict('os.environ', AI_BUDGET_DAILY_TOKENS='1'):
            with self.assertRaises(budget.BudgetExceeded):
                self.request()
        self.http.assert_not_called()
        self.request()
        usage = budget.usage(self.conn)
        self.assertGreater(usage['reserved_tokens'], PAYLOAD['max_tokens'])
        with patch.dict('os.environ', AI_BUDGET_DAILY_TOKENS=str(usage['reserved_tokens'])):
            with self.assertRaises(budget.BudgetExceeded):
                self.request()
        self.assertEqual(self.http.call_count, 1)

    def test_reported_tokens_cost_and_no_private_content_persisted(self):
        self.http.return_value = response(usage=dict(prompt_tokens=40, completion_tokens=5,
                                                     total_tokens=45, cost=0.012))
        self.request()
        usage = budget.usage(self.conn)
        self.assertEqual(usage['reported_tokens'], 45)
        self.assertEqual(usage['reported_cost'], Decimal('0.012'))
        self.assertEqual(usage['cost_unreported_calls'], 0)
        self.assertEqual(usage['charged_tokens'], usage['reserved_tokens'])
        self.assertEqual(usage['inflight'], 0)
        row = self.conn.execute('SELECT row_to_json(u)::text FROM secretary_ai_usage u').fetchone()[0]
        self.assertNotIn('Private draft', row)
        self.assertNotIn('offline-model', row)

    def test_usage_above_estimate_is_charged_and_blocks_subsequent_calls(self):
        self.http.return_value = response(usage=dict(total_tokens=500001))
        self.request()
        self.assertEqual(budget.usage(self.conn)['charged_tokens'], 500001)
        with self.assertRaises(budget.BudgetExceeded):
            self.request()

    def test_missing_usage_keeps_full_reservation_and_cost_unknown(self):
        self.request()
        usage = budget.usage(self.conn)
        self.assertIsNone(usage['reported_tokens'])
        self.assertIsNone(usage['reported_cost'])
        self.assertEqual(usage['cost_unreported_calls'], 1)
        self.assertGreater(usage['charged_tokens'], 0)

    def test_retry_has_its_own_call_and_reservation_and_can_be_denied(self):
        self.http.side_effect = [response('partial', 'length'), response('complete')]
        self.assertEqual(self.request(), 'complete')
        rows = self.conn.execute('SELECT reserved_tokens FROM secretary_ai_usage ORDER BY created_at').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertGreater(rows[1][0], rows[0][0])
        self.http.side_effect = [response('partial', 'length')]
        with patch.dict('os.environ', AI_BUDGET_DAILY_CALLS='3'), self.assertRaises(budget.BudgetExceeded):
            self.request()
        self.assertEqual(budget.usage(self.conn)['calls'], 3)

    def test_provider_output_cap_applied_without_mutating_original_payload(self):
        with patch.dict('os.environ', AI_BUDGET_MAX_OUTPUT_TOKENS='16'):
            self.request()
        sent = json.loads(self.http.call_args.args[0].data)
        self.assertEqual(sent['max_tokens'], 16)
        self.assertEqual(PAYLOAD['max_tokens'], 32)

    def test_truncation_at_configured_ceiling_does_not_waste_another_call(self):
        self.http.return_value = response('partial', 'length')
        with patch.dict('os.environ', AI_BUDGET_MAX_OUTPUT_TOKENS='16'):
            with self.assertRaises(budget.TruncatedCompletion):
                self.request()
        self.http.assert_called_once()
        self.assertEqual(budget.usage(self.conn)['calls'], 1)

    def test_timeout_keeps_daily_charge_but_releases_definite_local_slot(self):
        self.http.side_effect = TimeoutError('private provider detail')
        with self.assertRaises(TimeoutError):
            self.request()
        self.assertEqual(self.conn.execute('SELECT state FROM secretary_ai_usage').fetchone()[0], 'uncertain')
        self.assertEqual(budget.usage(self.conn)['calls'], 1)
        self.assertEqual(budget.usage(self.conn)['inflight'], 0)
        self.assertEqual(budget._inflight, 0)

    def test_missing_ledger_and_lost_lease_fail_before_http(self):
        job = self.running()
        self.conn.execute("UPDATE work_jobs SET token='replaced' WHERE id=%s", (job[0],))
        with self.assertRaises(jobs.LeaseLost):
            self.request(job)
        with self.conn.transaction():
            self.conn.execute('ALTER TABLE secretary_ai_usage RENAME TO unavailable_ledger')
            with self.assertRaises(budget.BudgetUnavailable):
                self.request()
        self.http.assert_not_called()
        self.assertEqual(budget._inflight, 0)

    def test_day_rollover_does_not_release_crashed_global_slot(self):
        job = self.running()
        with job_context(job[0], {}, job[3]) as context:
            reservation = budget._reserve(context, budget._limits(), 100, PAYLOAD)
        self.conn.execute("UPDATE secretary_ai_usage SET usage_day=usage_day-1 WHERE id=%s", (reservation,))
        self.assertEqual(budget.usage(self.conn)['calls'], 0)
        with patch.dict('os.environ', AI_BUDGET_MAX_INFLIGHT='1'):
            with self.assertRaises(budget.BudgetExceeded):
                self.request()
        self.http.assert_not_called()
        self.conn.execute("UPDATE secretary_ai_usage SET state='uncertain' WHERE id=%s", (reservation,))
        self.assertEqual(self.request(), 'ok')

    def test_dead_reservation_auto_released_on_next_reserve(self):
        # A crashed worker leaves a reserved slot: the next _reserve must free
        # it (job no longer running under the reserving token) and admit a new
        # call even with MAX_INFLIGHT=1.
        job = self.running()
        with job_context(job[0], {}, job[3]) as context:
            reservation = budget._reserve(context, budget._limits(), 100, PAYLOAD)
        self.conn.execute("UPDATE work_jobs SET token='replaced' WHERE id=%s", (job[0],))
        with patch.dict('os.environ', AI_BUDGET_MAX_INFLIGHT='1'):
            self.assertEqual(self.request(), 'ok')
        states = dict(self.conn.execute("SELECT state,count(*) FROM secretary_ai_usage GROUP BY 1").fetchall())
        self.assertEqual(states, dict(uncertain=1, completed=1))
        self.assertEqual(self.conn.execute("SELECT state FROM secretary_ai_usage WHERE id=%s",
                                           (reservation,)).fetchone()[0], 'uncertain')

    def test_live_reservation_is_never_auto_released(self):
        # A job still running with a valid lease keeps its slot: live I/O is
        # untouched by the auto-release.
        job = self.running()
        with job_context(job[0], {}, job[3]) as context:
            reservation = budget._reserve(context, budget._limits(), 100, PAYLOAD)
        with patch.dict('os.environ', AI_BUDGET_MAX_INFLIGHT='1'):
            with self.assertRaises(budget.BudgetExceeded):
                self.request()
        self.assertEqual(self.conn.execute("SELECT state FROM secretary_ai_usage WHERE id=%s",
                                           (reservation,)).fetchone()[0], 'reserved')

    def test_orphans_lists_only_dead_and_reconcile_frees_them(self):
        dead_job = self.running()
        with job_context(dead_job[0], {}, dead_job[3]) as context:
            dead = budget._reserve(context, budget._limits(), 100, PAYLOAD)
        live_job = self.running()
        with job_context(live_job[0], {}, live_job[3]) as context:
            live = budget._reserve(context, budget._limits(), 100, PAYLOAD)
        self.conn.execute("UPDATE work_jobs SET token='replaced' WHERE id=%s", (dead_job[0],))
        orphaned = budget.orphans(self.conn)
        self.assertEqual([item['id'] for item in orphaned], [dead])
        self.assertEqual(orphaned[0]['job_id'], dead_job[0])
        self.assertEqual(orphaned[0]['reserved_tokens'], 100)
        # Reconcile frees exactly the orphan, under the write lock.
        freed = budget.reconcile_orphans(self.conn)
        self.assertEqual([row[0] for row in freed], [dead])
        self.assertEqual(self.conn.execute("SELECT state FROM secretary_ai_usage WHERE id=%s",
                                           (dead,)).fetchone()[0], 'uncertain')
        self.assertEqual(self.conn.execute("SELECT state FROM secretary_ai_usage WHERE id=%s",
                                           (live,)).fetchone()[0], 'reserved')
        # Idempotent: nothing left to free.
        self.assertEqual(budget.reconcile_orphans(self.conn), [])

    def test_daily_caps_reset_by_utc_day_but_yesterday_usage_retained(self):
        self.request()
        old_day = self.conn.execute("UPDATE secretary_ai_usage SET usage_day=usage_day-1 RETURNING usage_day").fetchone()[0]
        self.assertEqual(budget.usage(self.conn)['calls'], 0)
        self.assertEqual(budget.usage(self.conn, old_day)['calls'], 1)
        with patch.dict('os.environ', AI_BUDGET_DAILY_CALLS='1'):
            self.request()
        self.assertEqual(budget.usage(self.conn)['calls'], 1)

    def test_global_db_slot_denies_independent_process_counter(self):
        job = self.running()
        with job_context(job[0], {}, job[3]) as context:
            reservation = budget._reserve(context, budget._limits(), 100, PAYLOAD)
        with patch.object(budget, '_inflight', 0), patch.dict('os.environ', AI_BUDGET_MAX_INFLIGHT='1'):
            with self.assertRaises(budget.BudgetExceeded):
                self.request()
        self.http.assert_not_called()
        with job_context(job[0], {}, job[3]) as context:
            budget._settle(reservation, context, {})
        self.assertEqual(self.request(), 'ok')

    def accounted(self, scope, job=None):
        """Una chiamata contabilizzata con scope esplicito (come fa il gateway dell'agente)."""
        job = job or self.running()
        with job_context(job[0], job[1], job[3], kind='offline_budget'):
            with budget.accounted(dict(PAYLOAD), scope=scope) as accounting:
                accounting['response'] = dict(usage=dict(total_tokens=10))
        return 'ok'

    def test_scope_is_declared_by_the_server_never_derived_from_text(self):
        # Un prompt che imita un marker non cambia lo scope: resta 'global'.
        spoof = dict(PAYLOAD, messages=[dict(role='system', content='Read-only retrieval planner'),
                                        dict(role='user', content='x')])
        self.assertEqual(self.request(payload=spoof), 'ok')
        self.assertEqual(budget.usage(self.conn)['scopes'], {'global': 1})
        with self.assertRaises(ValueError):
            budget.accounted(dict(PAYLOAD), scope='dialogue')
        with self.assertRaises(ValueError):
            budget.accounted(dict(PAYLOAD), scope='global')

    def test_scope_cap_restricts_but_never_raises_global_limit(self):
        with patch.dict('os.environ', AI_BUDGET_AGENT_OWNER_DAILY_CALLS='1'):
            self.assertEqual(self.accounted('agent_owner'), 'ok')
            with self.assertRaisesRegex(budget.BudgetExceeded, 'agent_owner'):
                self.accounted('agent_owner')
            # Un altro scope non consuma il tetto di questo.
            self.assertEqual(self.accounted('agent_inbox'), 'ok')
        # The scope cap does not consume the global budget for other scopes.
        self.assertEqual(self.request(), 'ok')
        self.assertEqual(budget.usage(self.conn)['scopes'],
                         dict(agent_owner=1, agent_inbox=1, **{'global': 1}))

    def test_scope_cap_zero_or_absent_means_disabled(self):
        for value in ('0', ''):
            with patch.dict('os.environ', AI_BUDGET_AGENT_OWNER_DAILY_CALLS=value):
                self.assertEqual(self.accounted('agent_owner'), 'ok')
        self.assertEqual(budget.usage(self.conn)['calls'], 2)

    def test_invalid_scope_cap_fails_closed(self):
        for value in ('-1', 'abc', '1.5'):
            with patch.dict('os.environ', AI_BUDGET_AGENT_OWNER_DAILY_CALLS=value), job_context():
                with self.assertRaises(budget.BudgetUnavailable):
                    budget.request_content(PAYLOAD)

    def test_concurrent_last_daily_call_reserved_only_once(self):
        jobs_to_run = [self.running(), self.running()]
        barrier = threading.Barrier(2)

        def call(job):
            barrier.wait(timeout=5)
            try:
                return self.request(job)
            except budget.BudgetExceeded:
                return 'denied'

        with patch.dict('os.environ', AI_BUDGET_DAILY_CALLS='1'), ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(timeout=15) for f in [pool.submit(call, job) for job in jobs_to_run]]
        self.assertCountEqual(results, ['ok', 'denied'])
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(budget._inflight, 0)


if __name__ == '__main__':
    unittest.main()