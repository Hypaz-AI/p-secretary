"""Opt-in SQL regressions for runtime review -> owner link acceptance.

Only an explicit *_test database, disposable schemas and mocked providers; no
cluster lifecycle, production configuration, workers or network operations.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import dashboard
import jobs
import knowledge
import secretary
import secretary_runtime as runtime
import test_secretary_runtime as fixtures


class LinkProposalLifecyclePostgresTests(unittest.TestCase):
    # Copy only fixture helpers: inheriting/importing the TestCase would collect
    # its unrelated tests again. It never creates or stops a PostgreSQL cluster.
    setUpClass = classmethod(fixtures.RuntimePostgresTests.setUpClass.__func__)
    connection = fixtures.RuntimePostgresTests.connection
    drop_schema = fixtures.RuntimePostgresTests.drop_schema
    archive = fixtures.RuntimePostgresTests.archive
    delegate = fixtures.RuntimePostgresTests.delegate
    consent = fixtures.RuntimePostgresTests.consent
    owner_plan = fixtures.RuntimePostgresTests.owner_plan
    respond_with_plan = fixtures.RuntimePostgresTests.respond_with_plan

    def setUp(self):
        fixtures.RuntimePostgresTests.setUp(self)
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('No network')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('No workers')))
        self.delegate()
        # Deliberately distinguish practice.version from proposal.version.
        self.conn.execute('UPDATE secretary_practices SET version=7 WHERE id=%s', (self.pid,))
        self.consent()
        self.consent('email')
        self.client = dashboard.create_app({'username': 'owner', 'password': 'offline'}).test_client()
        csrf = self.client.post('/api/login', json={'username': 'owner', 'password': 'offline'}).json['csrf']
        self.headers = {'X-CSRF-Token': csrf}

    def proposal(self, identity):
        return secretary._fetch(self.conn, 'secretary_runtime_link_proposals', identity)

    def observe_proposal(self, text='Il preventivo per il tetto è pronto.'):
        mid = self.archive(text, channel='email')
        runtime.observe(self.conn, mid)
        observation = dict(observation_message_id=mid, operation_key=f'secretary-observe:{mid}')
        self.assertEqual(runtime.review(observation)['proposed_links'], 1)
        proposal = self.conn.execute('''SELECT to_jsonb(q) FROM secretary_runtime_link_proposals q
            WHERE practice_id=%s AND message_id=%s''', (self.pid, mid)).fetchone()[0]
        wake = self.conn.execute('''SELECT payload FROM work_jobs
            WHERE key=%s''', (f"secretary-review:{self.pid}:v{proposal['practice_version']}:candidate",)).fetchone()[0]
        return proposal, wake, observation

    def apply_review(self, wake):
        with self.respond_with_plan():
            result = runtime.review(wake)
        self.assertEqual(result['status'], 'applied')
        return result

    def prepare_review(self, wake):
        with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                runtime.review(wake)
        self.assertEqual(self.conn.execute('''SELECT status FROM secretary_runtime_reviews
            WHERE operation_key=%s''', (wake['operation_key'],)).fetchone(), ('prepared',))

    def refresh(self, identity):
        response = self.client.get('/api/secretary/runtime')
        self.assertEqual(response.status_code, 200, response.json)
        return next(p for p in response.json['link_proposals'] if p['id'] == identity)

    def accept(self, proposal, version=None):
        return self.client.post(f"/api/secretary/runtime/links/{proposal['id']}/accept",
            json=dict(expected_version=proposal['version'] if version is None else version), headers=self.headers)

    def tables(self):
        return {table: self.conn.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                for table in ('secretary_practices', 'secretary_responsibilities', 'secretary_links',
                    'secretary_runtime_link_proposals', 'secretary_runtime_observations',
                    'secretary_runtime_reviews', 'secretary_runtime_attention', 'secretary_approvals',
                    'secretary_decisions', 'secretary_receipts', 'secretary_wakeups', 'work_jobs',
                    'data_policies', 'effect_receipts')}

    def assert_denied_unchanged(self, proposal, status=409):
        before = self.tables()
        response = self.accept(proposal)
        self.assertEqual(response.status_code, status, response.json)
        self.assertEqual(self.tables(), before)

    def owner_change(self, kind='practice', revoke=False):
        from test_secretary import change
        if kind == 'practice':
            text = 'Aggiorna la pratica per il tetto: chiedi un nuovo preventivo.'
            fields = dict(next_action='Chiedere un nuovo preventivo')
            identity = self.pid
        else:
            text = 'Revoca la responsabilità di seguire il tetto.' if revoke else 'Aggiorna la responsabilità di seguire il tetto.'
            fields = {} if revoke else dict(objective='Seguire un diverso preventivo per il tetto')
            identity = self.rid
        version = secretary._fetch(self.conn, secretary.TABLES[kind], identity)['version']
        mid = self.archive(text)
        item = change(kind=kind, operation='revoke' if revoke else 'update', identity=identity,
            version=version, fields=fields, evidence=text)
        secretary.apply(self.conn, self.owner_plan(text, mid, [item]), mid)

    def test_observe_candidate_review_refresh_accept_and_stale_click(self):
        old, wake, _ = self.observe_proposal()
        self.assertEqual((old['version'], old['practice_version']), (1, 7))
        before_links = self.conn.execute('SELECT * FROM secretary_links ORDER BY id').fetchall()
        responsibility = secretary._fetch(self.conn, 'secretary_responsibilities', self.rid)
        policies = self.conn.execute('SELECT * FROM data_policies ORDER BY channel,effect').fetchall()
        result = self.apply_review(wake)
        current = self.refresh(old['id'])
        self.assertEqual((current['version'], current['practice_version'], current['state']), (2, 8, 'proposed'))
        for field in ('id', 'responsibility_id', 'responsibility_version', 'message_id',
                      'entry_id', 'revision', 'evidence', 'method'):
            self.assertEqual(current[field], old[field])
        self.assertEqual(self.conn.execute('SELECT * FROM secretary_links ORDER BY id').fetchall(), before_links)
        self.assert_denied_unchanged(old)
        # Production API token is proposal.version, NOT the practice version.
        self.assertEqual(self.accept(current, current['practice_version']).status_code, 409)
        response = self.accept(current)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['external_execution'])
        accepted = self.proposal(old['id'])
        self.assertEqual((accepted['state'], accepted['version'], accepted['practice_version']), ('accepted', 3, 8))
        self.assertEqual(response.json['link']['practice_version'], 9)
        self.assertEqual(response.json['link']['version'], 3)
        self.assertEqual(self.conn.execute('''SELECT evidence,origin_message FROM secretary_links
            WHERE practice_id=%s AND email_message_id=%s''', (self.pid, old['message_id'])).fetchone(),
            (old['evidence'], self.origin))
        audit = self.conn.execute("SELECT data FROM secretary_decisions WHERE event='link_proposal_reissued'").fetchone()[0]
        self.assertEqual((audit['proposal_id'], audit['previous_version'], audit['version']), (old['id'], 1, 2))
        self.assertEqual((audit['previous_practice_version'], audit['practice_version']), (7, 8))
        self.assertEqual(audit['review_operation_key'], wake['operation_key'])
        self.assertEqual(secretary._fetch(self.conn, 'secretary_responsibilities', self.rid), responsibility)
        self.assertEqual(self.conn.execute('SELECT * FROM data_policies ORDER BY channel,effect').fetchall(), policies)
        for table in ('secretary_approvals', 'effect_receipts'):
            self.assertEqual(self.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        before = self.tables()
        self.assertEqual(runtime.review(wake), result)
        self.assertEqual(self.accept(current).json, response.json)
        self.assertEqual(self.tables(), before)
        self.chat.assert_not_called()

    def test_repeated_runtime_only_reviews_reissue_but_accepted_rows_and_links_stay_immutable(self):
        first, wake, _ = self.observe_proposal()
        self.apply_review(wake)
        refreshed = self.refresh(first['id'])
        self.assertEqual(self.accept(refreshed).status_code, 200)
        accepted = self.proposal(first['id'])
        links = self.conn.execute('SELECT * FROM secretary_links ORDER BY id').fetchall()
        pending, next_wake, _ = self.observe_proposal('Il secondo preventivo per il tetto è pronto.')
        self.apply_review(next_wake)
        current = self.refresh(pending['id'])
        for _ in range(2):
            self.assert_denied_unchanged(pending)
            version = secretary._fetch(self.conn, 'secretary_practices', self.pid)['version']
            wake = secretary._queue_review(self.conn, (self.pid, version), 'observation', pending['message_id'])
            self.apply_review(wake)
        current = self.refresh(pending['id'])
        self.assertEqual(current['version'], pending['version'] + 3)
        self.assertEqual(self.proposal(first['id']), accepted)
        self.assertEqual(self.conn.execute('SELECT * FROM secretary_links ORDER BY id').fetchall(), links)
        self.assertEqual(self.accept(current).status_code, 200)
        # Native links retain SQL-level append-only protections.
        with self.assertRaises(secretary.db.psycopg.errors.RaiseException):
            with self.conn.transaction():
                self.conn.execute('UPDATE secretary_links SET evidence=evidence WHERE practice_id=%s', (self.pid,))

    def test_owner_practice_edit_is_not_rebased_by_a_later_runtime_review(self):
        old, _, _ = self.observe_proposal()
        self.owner_change()
        version = secretary._fetch(self.conn, 'secretary_practices', self.pid)['version']
        wake = secretary._queue_review(self.conn, (self.pid, version), 'due')
        self.apply_review(wake)
        self.assertEqual(self.proposal(old['id']), old)
        self.assert_denied_unchanged(self.refresh(old['id']))

    def test_delegation_edit_is_not_rebased_by_a_later_runtime_review(self):
        old, _, _ = self.observe_proposal()
        self.owner_change('responsibility')
        # Candidate wakes deliberately stop when their delegation scope changes;
        # a valid later due review must not revive that older proposal either.
        wake = secretary._queue_review(self.conn, (self.pid, old['practice_version']), 'due')
        self.apply_review(wake)
        self.assertEqual(self.proposal(old['id']), old)
        self.assert_denied_unchanged(self.refresh(old['id']))

    def test_prepared_review_stops_after_owner_change_or_delegation_revocation(self):
        old, wake, _ = self.observe_proposal()
        self.prepare_review(wake)
        self.owner_change()
        self.assertEqual(runtime.review(wake)['status'], 'stopped')
        self.assertEqual(self.proposal(old['id']), old)
        self.assert_denied_unchanged(old)
        # The refreshed successor also cannot survive a revoked delegation.
        successor, wake, _ = self.observe_proposal('Un nuovo preventivo per il tetto.')
        self.prepare_review(wake)
        self.owner_change('responsibility', revoke=True)
        self.assertEqual(runtime.review(wake)['status'], 'stopped')
        self.assertEqual(self.proposal(successor['id']), successor)
        self.assert_denied_unchanged(successor, 403)

    def test_refreshed_proposal_denies_later_owner_delegation_and_native_source_changes(self):
        old, wake, _ = self.observe_proposal()
        self.apply_review(wake)
        current = self.refresh(old['id'])
        mutations = [
            ('UPDATE secretary_practices SET next_action=%s,version=version+1 WHERE id=%s',
             ('Nuova istruzione owner', self.pid), 409),
            ('UPDATE secretary_practices SET responsibility_id=NULL,version=version+1 WHERE id=%s', (self.pid,), 409),
            ('UPDATE secretary_responsibilities SET version=version+1 WHERE id=%s', (self.rid,), 409),
            ("UPDATE secretary_responsibilities SET state='revoked',version=version+1 WHERE id=%s", (self.rid,), 403),
            ("UPDATE telegram_messages SET metadata=metadata || '{\"is_forwarded\":true}'::jsonb WHERE id=%s", (self.origin,), 403),
            ("UPDATE email_messages SET body=body || ' edited' WHERE id=%s", (old['message_id'],), 409),
            ("UPDATE email_messages SET metadata=metadata || '{\"historical\":true}'::jsonb WHERE id=%s", (old['message_id'],), 403),
            ('UPDATE knowledge_entries SET active=false WHERE id=%s', (old['entry_id'],), 409),
            ('UPDATE knowledge_entries SET text=%s WHERE id=%s', ('Replaced evidence', old['entry_id']), 409),
        ]
        for sql, params, status in mutations:
            with self.subTest(sql=sql), self.conn.transaction(force_rollback=True):
                self.conn.execute(sql, params)
                self.assert_denied_unchanged(current, status)

    def test_source_manifest_rechecked_before_runtime_bump_or_reissue(self):
        old, wake, _ = self.observe_proposal()
        self.prepare_review(wake)
        for mutation in (
                "UPDATE email_messages SET body=body || ' edited' WHERE id=%s",
                "UPDATE email_messages SET metadata=metadata || '{\"historical\":true}'::jsonb WHERE id=%s"):
            with self.subTest(mutation=mutation), self.conn.transaction(force_rollback=True):
                self.conn.execute(mutation, (old['message_id'],))
                self.assertEqual(runtime.review(wake)['status'], 'stopped')
                self.assertEqual(self.proposal(old['id']), old)
                self.assertEqual(secretary._fetch(self.conn, 'secretary_practices', self.pid)['version'], 7)
                self.assertEqual(self.conn.execute("SELECT count(*) FROM secretary_decisions WHERE event='link_proposal_reissued'").fetchone()[0], 0)
        self.assertEqual(runtime.review(wake)['status'], 'applied')
        self.assertEqual(self.refresh(old['id'])['version'], 2)

    def test_source_reindex_does_not_rebind_old_revision_to_a_fresh_manifest(self):
        old, wake, _ = self.observe_proposal()
        self.conn.execute("UPDATE email_messages SET body=body || ' revised' WHERE id=%s", (old['message_id'],))
        knowledge.index_message(dict(message_id=old['message_id']))
        self.assertNotEqual(knowledge._snapshot(self.conn, old['message_id'])['revision'], old['revision'])
        self.apply_review(wake)
        self.assertEqual(self.proposal(old['id']), old)
        self.assert_denied_unchanged(old)

    def test_only_reviewed_manifest_proposals_are_reissued(self):
        old, wake, _ = self.observe_proposal()
        second, _, _ = self.observe_proposal('Un altro preventivo per il tetto.')
        self.prepare_review(wake)
        prepared = self.conn.execute('SELECT input FROM secretary_runtime_reviews WHERE operation_key=%s',
            (wake['operation_key'],)).fetchone()[0]
        prepared = deepcopy(prepared)
        prepared['sources'] = [s for s in prepared['sources'] if s['message_id'] != second['message_id']]
        self.conn.execute('UPDATE secretary_runtime_reviews SET input=%s WHERE operation_key=%s',
            (json.dumps(prepared), wake['operation_key']))
        self.assertEqual(runtime.review(wake)['status'], 'applied')
        self.assertEqual(self.proposal(old['id'])['version'], 2)
        self.assertEqual(self.proposal(second['id']), second)
        self.assert_denied_unchanged(second)

    def test_runtime_reissue_and_review_audit_failures_roll_back_every_effect_and_retry(self):
        old, wake, _ = self.observe_proposal()
        self.prepare_review(wake)
        before = self.tables()
        decision = secretary._decision
        for event in ('link_proposal_reissued', 'runtime_review'):
            def fail(conn, key, name, data):
                if name == event:
                    raise RuntimeError('audit failed')
                return decision(conn, key, name, data)
            with self.subTest(event=event), patch.object(secretary, '_decision', side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, 'audit failed'):
                    runtime.review(wake)
            self.assertEqual(self.tables(), before)
        self.assertEqual(runtime.review(wake)['status'], 'applied')
        self.assertEqual(self.refresh(old['id'])['version'], 2)

    def test_owner_accept_audit_failure_rolls_back_link_proposal_practice_receipt_and_queue(self):
        old, wake, _ = self.observe_proposal()
        self.apply_review(wake)
        current = self.refresh(old['id'])
        before = self.tables()
        with patch.object(secretary, '_decision', side_effect=RuntimeError('audit failed')):
            self.assertEqual(self.accept(current).status_code, 503)
        self.assertEqual(self.tables(), before)
        response = self.accept(current)
        self.assertEqual(response.status_code, 200, response.json)

    def test_lost_runtime_fence_rolls_back_reissue_review_and_audit(self):
        old, wake, _ = self.observe_proposal()
        self.prepare_review(wake)
        before = self.tables()
        def fence(conn=None):
            if conn is not None and self.proposal(old['id'])['version'] != old['version']:
                raise jobs.LeaseLost()
        with patch.object(jobs, 'assert_current', side_effect=fence), self.assertRaises(jobs.LeaseLost):
            runtime.review(wake)
        self.assertEqual(self.tables(), before)

    def test_receipt_replay_after_owner_change_is_historical_not_new_authorization(self):
        old, wake, observation = self.observe_proposal()
        result = self.apply_review(wake)
        current = self.refresh(old['id'])
        response = self.accept(current)
        self.assertEqual(response.status_code, 200, response.json)
        self.owner_change('responsibility', revoke=True)
        before = self.tables()
        self.assertEqual(self.accept(current).json, response.json)
        self.assertEqual(runtime.review(wake), result)
        self.assertEqual(runtime.review(observation), dict(status='done'))
        self.assertEqual(self.tables(), before)
        self.assert_denied_unchanged(self.proposal(old['id']))


if __name__ == '__main__':
    unittest.main()