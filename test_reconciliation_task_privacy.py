"""Task-context egress regressions at the real reconciliation/provider boundary.

Run with PI_SECRETARY_CONFIG_FILE=/dev/null and a temporary DATA_DIR. Unit tests
use a strict SQL double, never mocked consent decisions. Optional SQL tests use
ONLY PI_SECRETARY_TEST_DATABASE_URL: an explicit *_test database on an existing
Unix socket, without libpq services or inherited PG settings. Each test gets a
private, fully migrated schema. No configured DB, cluster startup, TCP/providers.
"""
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

import brain
import db
import jobs
import message_store
import owner_dialogue
import policy
import task_reconciliation as reconciliation


CANARY = 'SYNTHETIC_OWNER_TASK_PRIVATE_82a1'
RECEIPT = 'PRIVATE_PROVENANCE_RECEIPT_NOT_FOR_PROVIDER_64bc'
FIELDS = ('channel', 'metadata', 'ts', 'acquired_at', 'source_id', 'account',
          'chat_id', 'author', 'direction', 'message_id')
PAST = datetime(2026, 10, 14, tzinfo=timezone.utc)
CURRENT = datetime(2026, 10, 16, tzinfo=timezone.utc)


def validated_test_params(url):
    """Validate explicit test opt-in before libpq can resolve defaults/services."""
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(url)
    if (not params.get('dbname', '').endswith('_test') or 'service' in params
            or any(char in params['dbname'] for char in '/:= \t\r\n')):
        raise ValueError('An explicit database ending in _test (no service) is required')
    host = params.get('host', '')
    if not Path(host).is_absolute() or ',' in host or 'hostaddr' in params:
        raise ValueError('An explicit Unix socket directory (no TCP/hostaddr) is required')
    return params


class ReconciliationDatabaseOptInTests(unittest.TestCase):
    def test_explicit_test_database_accepts_caller_socket_without_hardcoded_identity(self):
        self.assertEqual(validated_test_params(
            'host=/var/tmp/synthetic-parent/socket port=5439 dbname=isolated_test user=fixture'),
            dict(host='/var/tmp/synthetic-parent/socket', port='5439',
                 dbname='isolated_test', user='fixture'))

    def test_default_production_service_and_network_targets_are_rejected_before_connect(self):
        with patch('psycopg.connect', side_effect=AssertionError('SQL forbidden')) as connect:
            for url in ('', 'host=/tmp/synthetic', 'host=/tmp/synthetic dbname=production',
                        'host=/tmp/synthetic service=deployment dbname=isolated_test',
                        'host=/tmp/synthetic service= dbname=isolated_test',
                        'dbname=isolated_test', 'host=localhost dbname=isolated_test',
                        'host=/tmp/synthetic,localhost dbname=isolated_test',
                        'host=/tmp/synthetic hostaddr=127.0.0.1 dbname=isolated_test',
                        'host=/tmp/synthetic dbname=postgresql://production/isolated_test'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    validated_test_params(url)
        connect.assert_not_called()

    def test_sql_fixture_without_opt_in_skips_without_configured_database_fallback(self):
        class Probe(unittest.TestCase):
            setUpClass = classmethod(ReconciliationTaskPrivacyPostgresTests.setUpClass.__func__)

        environment = dict(DATABASE_URL='host=/tmp/forbidden dbname=production',
                           PGDATABASE='production', PGSERVICE='production')
        with patch.dict(os.environ, environment, clear=True), patch('psycopg.connect') as connect:
            for value in (None, ''):
                with self.subTest(value=value):
                    if value is None:
                        os.environ.pop('PI_SECRETARY_TEST_DATABASE_URL', None)
                    else:
                        os.environ['PI_SECRETARY_TEST_DATABASE_URL'] = value
                    with self.assertRaisesRegex(unittest.SkipTest, 'PI_SECRETARY_TEST_DATABASE_URL'):
                        Probe.setUpClass()
            connect.assert_not_called()

    def test_sql_fixture_invalid_opt_in_fails_without_default_or_service_fallback(self):
        class Probe(unittest.TestCase):
            setUpClass = classmethod(ReconciliationTaskPrivacyPostgresTests.setUpClass.__func__)

        environment = dict(DATABASE_URL='host=/tmp/forbidden dbname=production',
                           PGDATABASE='other_test', PGSERVICE='production')
        with patch.dict(os.environ, environment, clear=True), patch('psycopg.connect') as connect:
            for value in ('host=/tmp/synthetic dbname=production',
                          'dbname=isolated_test service=production',
                          'host=/tmp/synthetic'):
                with self.subTest(value=value):
                    os.environ['PI_SECRETARY_TEST_DATABASE_URL'] = value
                    with self.assertRaises(ValueError):
                        Probe.setUpClass()
            connect.assert_not_called()

    def test_sql_fixture_clears_all_pg_overrides_without_opening_a_connection(self):
        class Probe(unittest.TestCase):
            setUpClass = classmethod(ReconciliationTaskPrivacyPostgresTests.setUpClass.__func__)

        environment = dict(PI_SECRETARY_TEST_DATABASE_URL=
            'host=/var/tmp/synthetic-parent/socket dbname=isolated_test',
            PGHOSTADDR='127.0.0.1', PGSERVICE='production', PGOPTIONS='-c search_path=public',
            PGPASSWORD='synthetic-not-a-secret')
        with patch.dict(os.environ, environment, clear=True), patch('psycopg.connect') as connect:
            try:
                Probe.setUpClass()
                self.assertFalse(any(key.startswith('PG') for key in os.environ))
                self.assertEqual(Probe.params['dbname'], 'isolated_test')
                connect.assert_not_called()
            finally:
                Probe.doClassCleanups()
            self.assertEqual(os.environ['PGSERVICE'], 'production')


def native(channel, historical=False):
    identity = int(uuid.uuid4().hex[:12], 16) + 1
    metadata = dict(chat_type='private', content_origin='direct_text',
                    is_forwarded=False, account='bot:123', chat_id='7', author='7',
                    direction='in', message_id=identity, historical=historical,
                    private_receipt=RECEIPT)
    return dict(channel=channel, metadata=metadata, ts=PAST, acquired_at=PAST,
                source_id=json.dumps(['bot', '123', '7', identity]),
                account='bot:123', chat_id='7', author='7', direction='in',
                message_id=str(identity), sender='Synthetic sender',
                subject='Synthetic subject', body=RECEIPT)


class _PrivacyCases:
    """Identical contracts against a SQL double and the explicit test socket."""

    def guard(self):
        for target in ('socket.socket.connect', 'socket.create_connection',
                       'urllib.request.urlopen'):
            self.enterContext(patch(target, side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(db, '_conn',
            side_effect=AssertionError('Configured database forbidden')))
        if db.psycopg:
            self.enterContext(patch.object(db.psycopg, 'connect',
                side_effect=AssertionError('Unapproved SQL connection forbidden')))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.enterContext(patch.object(reconciliation.flows, 'enabled', return_value=True))
        self.enterContext(patch.object(jobs, 'execution_context', return_value=None))
        self.enterContext(patch.object(reconciliation.documents, 'attachment_text', return_value=''))
        self.provider = self.enterContext(patch.object(brain.ai_budget, 'request_content',
            return_value='{"changes": []}'))

    def reconcile(self, *, channel='email', historical=False, attached=False, mid=None):
        if mid is None:
            mid = self.message(channel, historical=historical, current=True)
        if attached:
            self.attach(mid)
        self.provider.reset_mock()
        # This is the only application connection override; all native consent,
        # task provenance, brain serialization and provider payloads remain real.
        with patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)):
            reconciliation.reconcile(dict(message_id=mid))

    def exported(self):
        tasks = []
        for call in self.provider.call_args_list:
            content = call.args[0]['messages'][1]['content']
            if isinstance(content, list):
                content = content[0]['text']
            tasks.extend(json.loads(content)['tasks'])
        return tasks

    def allowed_peer(self):
        return self.task(source_message=self.message('email'), title='Allowed email task')

    def assert_private_omitted(self, identity):
        self.assertNotIn(identity, [task['id'] for task in self.exported()])
        self.assertNotIn(CANARY, json.dumps(self.provider.call_args_list, default=str))

    def test_denied_telegram_owner_task_cannot_ride_allowed_email_trigger(self):
        private = self.task(owner_origin_message=self.message('telegram'), title=CANARY)
        allowed = self.allowed_peer()
        self.reconcile()
        self.provider.assert_called_once()
        self.assert_private_omitted(private)
        self.assertEqual([task['id'] for task in self.exported()], [allowed])

    def test_authorized_owner_task_is_intact_without_private_provenance_fields(self):
        self.consent('telegram')
        origin = self.message('telegram')
        identity = self.task(owner_origin_message=origin, title=CANARY)
        self.reconcile()
        self.provider.assert_called_once()
        self.assertEqual(self.exported(), [dict(id=identity, title=CANARY,
            deadline=None, version=0, source_message=None, sender=None, subject=None, body=None)])
        self.assertNotIn(RECEIPT, json.dumps(self.provider.call_args_list, default=str))

    def test_unprovenanced_null_task_is_omitted_and_not_an_ai_request(self):
        private = self.task(title=CANARY)
        self.reconcile()
        self.provider.assert_not_called()
        self.assert_private_omitted(private)

    def test_missing_native_origin_is_not_consent(self):
        private = self.task(owner_origin_message=9223372036854775807, title=CANARY)
        self.allowed_peer()
        self.reconcile()
        self.provider.assert_called_once()
        self.assert_private_omitted(private)

    def test_every_native_task_origin_requires_its_own_consent(self):
        denied = self.message('telegram')
        private = [self.task(title=CANARY, **fields) for fields in (
            dict(source_message=denied),
            dict(source_message=self.message('email'), owner_origin_message=denied),
            dict(source_message=self.message('email'), last_evidence_id=denied),
            dict(last_evidence_id=denied))]
        allowed = self.allowed_peer()
        self.reconcile()
        self.assertEqual([task['id'] for task in self.exported()], [allowed])
        for identity in private:
            self.assert_private_omitted(identity)

    def test_telegram_history_and_media_grants_do_not_replace_text_consent(self):
        self.consent('telegram', 'ai_history')
        self.consent('telegram', 'ai_media')
        origin = self.message('telegram', historical=True)
        self.attach(origin, image=True)
        identity = self.task(owner_origin_message=origin, title=CANARY)
        self.allowed_peer()
        self.reconcile()
        self.assert_private_omitted(identity)
        self.consent('telegram')
        self.reconcile()
        self.assertIn(identity, [task['id'] for task in self.exported()])

    def test_all_native_origins_require_media_consent_when_attached(self):
        self.consent('telegram')
        origin = self.message('telegram')
        self.attach(origin, image=True)
        private = [self.task(title=CANARY, **fields) for fields in (
            dict(source_message=origin), dict(owner_origin_message=origin),
            dict(source_message=self.message('email'), last_evidence_id=origin))]
        self.allowed_peer()
        self.reconcile()
        for identity in private:
            self.assert_private_omitted(identity)
        self.consent('telegram', 'ai_media')
        self.reconcile()
        self.assertTrue(set(private).issubset(task['id'] for task in self.exported()))

    def test_historical_candidate_needs_read_consent_not_operational_replay(self):
        self.consent('telegram')
        identity = self.task(owner_origin_message=self.message('telegram', historical=True), title=CANARY)
        self.allowed_peer()
        self.reconcile()
        self.assert_private_omitted(identity)
        self.consent('telegram', 'ai_history')
        self.reconcile()
        self.assertIn(identity, [task['id'] for task in self.exported()])

    def test_historical_flag_must_be_native_boolean_not_a_catalogue_claim(self):
        self.consent('telegram')
        self.consent('telegram', 'ai_history')
        private = []
        for historical in (None, 'false', 0, 1):
            origin = self.message('telegram', historical=historical)
            private.append(self.task(owner_origin_message=origin, title=CANARY))
        allowed = self.allowed_peer()
        self.reconcile()
        self.assertEqual([task['id'] for task in self.exported()], [allowed])
        for identity in private:
            self.assert_private_omitted(identity)

    def test_older_applied_input_still_requires_consent_after_latest_evidence(self):
        identity = self.task(source_message=self.message('email'),
                             last_evidence_id=self.message('email'), title=CANARY)
        self.derivation(identity, self.message('telegram'))
        self.allowed_peer()
        self.reconcile()
        self.assert_private_omitted(identity)
        self.consent('telegram')
        self.reconcile()
        self.assertIn(identity, [task['id'] for task in self.exported()])

    def test_prior_applied_media_and_history_are_not_laundered_by_latest_input(self):
        self.consent('telegram')
        origin = self.message('telegram', historical=True)
        self.attach(origin, image=True)
        identity = self.task(source_message=self.message('email'),
                             last_evidence_id=self.message('email'), title=CANARY)
        self.derivation(identity, origin)
        self.allowed_peer()
        self.consent('telegram', 'ai_history')
        self.reconcile()
        self.assert_private_omitted(identity)
        self.consent('telegram', 'ai_media')
        self.reconcile()
        self.assertIn(identity, [task['id'] for task in self.exported()])
        self.consent('telegram', 'ai_history', allowed=False)
        self.reconcile()
        self.assert_private_omitted(identity)

    def test_unapplied_proposals_do_not_taint_but_missing_applied_origin_fails_closed(self):
        identity = self.task(owner_origin_message=self.message('email'), title=CANARY)
        for status in ('pending', 'rejected', 'stale'):
            self.derivation(identity, self.message('telegram'), status=status)
        self.reconcile()
        self.assertEqual([task['id'] for task in self.exported()], [identity])
        self.assertNotIn(RECEIPT, json.dumps(self.provider.call_args_list, default=str))
        self.derivation(identity, None)
        self.reconcile()
        self.provider.assert_not_called()

    def test_rechecks_owner_consent_after_snapshot_before_first_provider(self):
        self.consent('telegram')
        self.task(owner_origin_message=self.message('telegram'), title=CANARY)

        def revoke(body, attachments):
            self.consent('telegram', allowed=False)
            return body

        with patch.object(reconciliation.media_text, 'message_text', side_effect=revoke):
            with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
                self.reconcile()
        self.provider.assert_not_called()

    def test_rechecks_all_native_task_origins_after_snapshot(self):
        identity = self.task(owner_origin_message=self.message('email'), title=CANARY)
        denied = self.message('telegram')

        def replace_origin(body, attachments):
            self.mutate_task(identity, owner_origin_message=denied)
            return body

        with patch.object(reconciliation.media_text, 'message_text', side_effect=replace_origin):
            with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
                self.reconcile()
        self.provider.assert_not_called()

    def test_rechecks_exact_version_and_open_status_after_snapshot(self):
        identity = self.task(owner_origin_message=self.message('email'), title=CANARY)
        for fields in (dict(version=1), dict(status='done')):
            with self.subTest(fields=fields):
                self.mutate_task(identity, version=0, status='open')

                def mutate(body, attachments):
                    self.mutate_task(identity, **fields)
                    return body

                with patch.object(reconciliation.media_text, 'message_text', side_effect=mutate):
                    with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
                        self.reconcile()
                self.provider.assert_not_called()

    def test_lease_loss_at_provider_boundary_never_exports_candidate(self):
        self.task(owner_origin_message=self.message('email'), title=CANARY)

        def lose_at_boundary(conn=None):
            if conn is None:
                raise jobs.LeaseLost('Synthetic lease lost')

        with patch.object(jobs, 'assert_current', side_effect=lose_at_boundary):
            with self.assertRaises(jobs.LeaseLost):
                self.reconcile()
        self.provider.assert_not_called()

    def test_rechecks_each_batch_and_never_exports_revoked_second_batch(self):
        self.consent('telegram')
        origin = self.message('telegram')
        for index in range(41):
            self.task(owner_origin_message=origin, title=CANARY if index == 40 else f'Allowed {index}')

        def revoke(payload, **kwargs):
            self.consent('telegram', allowed=False)
            return '{"changes": []}'

        self.provider.side_effect = revoke
        with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
            self.reconcile()
        self.provider.assert_called_once()
        self.assertEqual(len(self.exported()), 40)
        self.assertNotIn(CANARY, json.dumps(self.provider.call_args_list, default=str))

    def test_rechecks_prior_applied_inputs_and_native_media_after_snapshot(self):
        self.consent('telegram')
        origin = self.message('telegram')
        self.task(owner_origin_message=origin, title=CANARY)

        def attach_after_snapshot(body, attachments):
            self.attach(origin, image=True)
            return body

        with patch.object(reconciliation.media_text, 'message_text', side_effect=attach_after_snapshot):
            with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
                self.reconcile()
        self.provider.assert_not_called()
        self.consent('telegram', 'ai_media')

        def derive_after_snapshot(body, attachments):
            self.derivation(identity, self.message('whatsapp'))
            return body

        identity = self.task(owner_origin_message=self.message('email'), title=CANARY)
        with patch.object(reconciliation.media_text, 'message_text', side_effect=derive_after_snapshot):
            with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
                self.reconcile()
        self.provider.assert_not_called()

    def test_trigger_still_requires_operational_text_media_and_nonhistorical_source(self):
        self.allowed_peer()
        self.consent('email', allowed=False)
        with self.assertRaisesRegex(jobs.Deferred, '^Reconciliation data policy blocked$'):
            self.reconcile()
        self.provider.assert_not_called()
        self.consent('email')
        with self.assertRaisesRegex(jobs.Deferred, '^Reconciliation media policy blocked$'):
            self.reconcile(attached=True)
        self.provider.assert_not_called()
        self.consent('email', 'ai_media')
        self.reconcile(attached=True)
        self.provider.assert_called_once()
        self.consent('email', 'ai_history')
        self.reconcile(historical=True)
        self.provider.assert_not_called()

    def test_trigger_consent_is_rechecked_between_batches(self):
        self.consent('telegram')
        origin = self.message('telegram')
        for index in range(41):
            self.task(owner_origin_message=origin, title=f'Allowed {index}')

        def revoke(payload, **kwargs):
            self.consent('email', allowed=False)
            return '{"changes": []}'

        self.provider.side_effect = revoke
        with self.assertRaisesRegex(jobs.Deferred, '^Reconciliation data policy blocked$'):
            self.reconcile()
        self.provider.assert_called_once()

    def test_later_batch_snapshot_version_is_rechecked_after_first_provider(self):
        origin = self.message('email')
        identities = [self.task(owner_origin_message=origin,
            title=CANARY if index == 40 else f'Allowed {index}') for index in range(41)]

        def mutate(payload, **kwargs):
            self.mutate_task(identities[-1], version=1)
            return '{"changes": []}'

        self.provider.side_effect = mutate
        with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
            self.reconcile()
        self.provider.assert_called_once()
        self.assertEqual(len(self.exported()), 40)
        self.assertNotIn(CANARY, json.dumps(self.provider.call_args_list, default=str))

    def test_temporal_candidates_use_native_acquisition_and_message_time_not_analysis_time(self):
        # A task analyzed late still refers to old evidence and remains eligible.
        old = self.task(source_message=self.message('email'), created_at=CURRENT,
                        title='Late analysis of old source')
        # Either future native time or future acquisition excludes a task.
        future_native = self.task(source_message=self.message('email', ts=CURRENT,
            acquired_at=PAST), title=CANARY)
        future_acquisition = self.task(source_message=self.message('email', ts=PAST,
            acquired_at=CURRENT), title=CANARY)
        future_owner = self.task(owner_origin_message=self.message('email'),
                                 created_at=CURRENT, title=CANARY)
        undated = self.task(source_message=self.message('email', ts=None), title='Undated source')
        evidence = self.message('email', ts=PAST, acquired_at=PAST)
        self.reconcile(mid=evidence)
        self.assertEqual([task['id'] for task in self.exported()], [old, undated])
        for identity in (future_native, future_acquisition, future_owner):
            self.assert_private_omitted(identity)

    def test_trigger_task_is_excluded_and_equal_candidate_times_are_allowed(self):
        evidence = self.message('email')
        private = self.task(source_message=evidence, title=CANARY)
        allowed = self.allowed_peer()
        self.reconcile(mid=evidence)
        self.assertEqual([task['id'] for task in self.exported()], [allowed])
        self.assert_private_omitted(private)

    def test_candidate_lookup_is_bounded_and_authorized_batches_are_unchanged(self):
        origin = self.message('email')
        for index in range(201):
            self.task(owner_origin_message=origin, title=f'Allowed {index}')
        self.reconcile()
        self.assertEqual(self.provider.call_count, 5)
        self.assertEqual(len(self.exported()), 200)
        self.assertEqual(self.exported()[-1]['title'], 'Allowed 199')


class ReconciliationTaskPrivacyTests(_PrivacyCases, unittest.TestCase):
    def setUp(self):
        self.messages, self.tasks, self.attachments, self.derivations = {}, {}, {}, []
        self.grants, self.runs = {}, set()
        self.conn = MagicMock()
        self.conn.transaction.side_effect = lambda: nullcontext(self.conn)
        self.conn.execute.side_effect = self.execute
        self.guard()
        self.consent('email')

    def message(self, channel='email', *, historical=False, current=False, **times):
        mid = len(self.messages) + 1
        value = native(channel, historical)
        if current:
            value.update(ts=CURRENT, acquired_at=CURRENT, body='No task update.')
        value.update(times)
        self.messages[mid] = value
        return mid

    def task(self, *, title='Allowed', source_message=None, owner_origin_message=None,
             last_evidence_id=None, created_at=PAST):
        identity = len(self.tasks) + 1
        self.tasks[identity] = dict(id=identity, title=title, deadline=None, version=0,
            source_message=source_message, owner_origin_message=owner_origin_message,
            last_evidence_id=last_evidence_id, created_at=created_at, status='open')
        return identity

    def mutate_task(self, identity, **fields):
        self.tasks[identity].update(fields)

    def attach(self, mid, image=False):
        self.attachments.setdefault(mid, []).append(('synthetic.png' if image else 'synthetic.bin',
            'image/png' if image else 'application/octet-stream', None, b'offline', {}, 1, None))

    def consent(self, channel, effect='ai_text', allowed=True):
        self.grants[channel, effect] = allowed

    def derivation(self, identity, mid, status='applied'):
        self.derivations.append((identity, mid, status))

    def execute(self, sql, params=None):
        query = ' '.join(sql.split())
        row, rows = None, []
        if query.startswith('SELECT channel,sender,subject,body,source_id FROM communications '):
            value = self.messages.get(params[0])
            row = tuple(value[key] for key in ('channel', 'sender', 'subject', 'body', 'source_id')) if value else None
        elif query.startswith('SELECT a.filename,a.content_type,a.path,a.data,a.metadata,'):
            rows = self.attachments.get(params[0], [])
        elif query.startswith('SELECT ' + ','.join(FIELDS) + ' FROM '):
            self.assertIn('FOR SHARE', query)
            table = query.split(' FROM ', 1)[1].split()[0]
            value = self.messages.get(params[0])
            if value and table == value['channel'].split(' ', 1)[0] + '_messages':
                row = tuple(value[field] for field in FIELDS)
        elif query.startswith('SELECT allowed FROM data_policies '):
            self.assertIn('FOR SHARE', query)
            row = (self.grants[params],) if params in self.grants else None
        elif query.startswith('INSERT INTO effect_audit('):
            self.assertNotIn(CANARY, str(params))
            self.assertNotIn(RECEIPT, str(params))
        elif query.startswith('SELECT 1 FROM task_reconciliation_runs '):
            row = (1,) if params[0] in self.runs else None
        elif query.startswith('SELECT t.id,t.title,t.deadline,t.version,t.source_message,'):
            self.assertTrue(query.endswith('ORDER BY t.id LIMIT 200'))
            current = self.messages[params[0]]
            for task in self.tasks.values():
                source = self.messages.get(task['source_message'])
                acquired = source['acquired_at'] if source else task['created_at']
                ts = source['ts'] if source else task['created_at']
                if (task['status'] != 'open' or task['source_message'] == params[0]
                        or (task['source_message'] is not None and source is None)
                        or acquired > current['acquired_at']
                        or (ts is not None and ts > (current['ts'] or current['acquired_at']))):
                    continue
                rows.append(tuple(task[field] for field in ('id', 'title', 'deadline', 'version', 'source_message'))
                    + tuple(source[field] if source else None for field in ('sender', 'subject', 'body')))
                if len(rows) == 200:
                    break
        elif query.startswith('SELECT status,version FROM tasks WHERE id='):
            task = self.tasks.get(params[0])
            row = (task['status'], task['version']) if task else None
        elif query.startswith('SELECT source_message,owner_origin_message,last_evidence_id FROM tasks '):
            task = self.tasks.get(params[0])
            row = tuple(task[field] for field in ('source_message', 'owner_origin_message', 'last_evidence_id')) if task else None
        elif query.startswith('SELECT 1 FROM attachments WHERE message_id='):
            row = (1,) if self.attachments.get(params[0]) else None
        elif query.startswith('SELECT DISTINCT message_id FROM task_changes '):
            self.assertIn("status='applied'", query)
            rows = list({(mid,) for identity, mid, status in self.derivations
                         if identity == params[0] and status == 'applied'})
        elif query.startswith('SELECT pg_advisory_xact_lock('):
            pass
        elif query.startswith('INSERT INTO task_reconciliation_runs(key) '):
            if params[0] not in self.runs:
                self.runs.add(params[0])
                row = params
        else:
            raise AssertionError('Unexpected SQL: ' + query)
        return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: rows)


class ReconciliationTaskPrivacyPostgresTests(_PrivacyCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
        if not url:
            raise unittest.SkipTest('Set PI_SECRETARY_TEST_DATABASE_URL to an existing *_test Unix socket DB')
        import psycopg
        cls.params = validated_test_params(url)
        cls.psycopg = psycopg
        cls.enterClassContext(patch.dict(os.environ,
            {key: value for key, value in os.environ.items() if not key.startswith('PG')}, clear=True))

    def setUp(self):
        from psycopg import sql
        # Retain only the explicitly validated test connector for a second
        # private-schema connection; guard() forbids every implicit connection.
        self.test_connect = self.psycopg.connect
        self.conn = self.psycopg.connect(**self.params, autocommit=True,
                                        prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.guard()
        self.assertEqual(self.conn.execute('SELECT current_database()').fetchone(),
                         (self.params['dbname'],))
        self.schema = 'test_reconciliation_privacy_' + uuid.uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='5s'")
        self.conn.execute("SET lock_timeout='2s'")
        # Run the actual complete bootstrap, not a drifting partial list of
        # migrations. Only this private-schema connection is available to it.
        with patch.object(db, 'available', return_value=True), patch.object(
                db, '_conn', side_effect=lambda: nullcontext(self.conn)):
            self.assertEqual(db.init(), 0)
        # Most contracts roll back data before dropping their private schema.
        # The explicit two-connection races need committed rows visible to the
        # competing writer, so only those tests use ordinary autocommit.
        if not self._testMethodName.startswith('test_concurrent_'):
            self.enterContext(self.conn.transaction(force_rollback=True))
        self.consent('email')

    def competing_writer(self):
        from psycopg import sql
        conn = self.test_connect(**self.params, autocommit=True,
                                 prepare_threshold=None, connect_timeout=5)
        self.addCleanup(conn.close)
        conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        conn.execute("SET statement_timeout='5s'")
        conn.execute("SET lock_timeout='2s'")
        return conn

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def message(self, channel='email', *, historical=False, current=False, **times):
        from psycopg import sql
        value = native(channel, historical)
        if current:
            value.update(ts=CURRENT, acquired_at=CURRENT, body='No task update.')
        value.update(times)
        fields = FIELDS + ('sender', 'subject', 'body')
        return self.conn.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({}) RETURNING id').format(
            sql.Identifier(channel.split(' ', 1)[0] + '_messages'),
            sql.SQL(',').join(map(sql.Identifier, fields)),
            sql.SQL(',').join(sql.Placeholder() for _ in fields)),
            tuple(json.dumps(value[field]) if field == 'metadata' else value[field]
                  for field in fields)).fetchone()[0]

    def task(self, *, title='Allowed', source_message=None, owner_origin_message=None,
             last_evidence_id=None, created_at=PAST):
        return self.conn.execute('''INSERT INTO tasks(title,source_message,owner_origin_message,
            last_evidence_id,created_at,owner_operation_key) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''',
            (title, source_message, owner_origin_message, last_evidence_id, created_at,
             RECEIPT + uuid.uuid4().hex)).fetchone()[0]

    def mutate_task(self, identity, **fields):
        from psycopg import sql
        self.assertTrue(fields)
        self.assertFalse(set(fields) - {'version', 'status', 'owner_origin_message'})
        self.conn.execute(sql.SQL('UPDATE tasks SET {} WHERE id=%s').format(
            sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(field)) for field in fields)),
            (*fields.values(), identity))

    def attach(self, mid, image=False):
        self.conn.execute('''INSERT INTO attachments(message_id,channel,filename,content_type,data)
            VALUES (%s,'telegram',%s,%s,%s)''',
            (mid, 'synthetic.png' if image else 'synthetic.bin',
             'image/png' if image else 'application/octet-stream', b'offline'))

    def consent(self, channel, effect='ai_text', allowed=True):
        self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed) VALUES (%s,%s,%s)
            ON CONFLICT(channel,effect) DO UPDATE SET allowed=excluded.allowed,
            version=data_policies.version+1,updated_at=now()''', (channel, effect, allowed))

    def derivation(self, identity, mid, status='applied'):
        self.conn.execute('''INSERT INTO task_changes(task_id,message_id,evidence_key,operation,
            changes,reason,evidence,expected_version,status)
            VALUES (%s,%s,%s,'update','{}',%s,%s,0,%s)''',
            (identity, mid, uuid.uuid4().hex, RECEIPT, RECEIPT, status))

    def test_full_bootstrap_is_private_complete_idempotent_and_does_not_add_consent(self):
        before = self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        with patch.object(db, 'available', return_value=True), patch.object(
                db, '_conn', side_effect=lambda: nullcontext(self.conn)):
            self.assertEqual(db.init(), 0)
        self.assertEqual(self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall(), before)
        self.assertEqual(self.conn.execute('SELECT channel,effect,allowed FROM data_policies').fetchall(),
                         [('email', 'ai_text', True)])
        for table in ('tasks', 'task_changes', 'owner_exchanges', 'media_transcripts',
                      'secretary_practices', 'knowledge_entries', 'secretary_runtime_observations',
                      'secretary_email_proposals', 'secretary_ai_usage'):
            # Resolve through exactly the same search_path used by the handler.
            row = self.conn.execute('''SELECT n.nspname FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=to_regclass(%s)''',
                (table,)).fetchone()
            self.assertEqual(row, (self.schema,), table)

    def test_concurrent_native_consent_revocation_is_seen_before_provider(self):
        self.consent('telegram')
        self.task(owner_origin_message=self.message('telegram'), title=CANARY)
        writer = self.competing_writer()

        def revoke(body, attachments):
            writer.execute("UPDATE data_policies SET allowed=FALSE,version=version+1 "
                           "WHERE channel='telegram' AND effect='ai_text'")
            return body

        with patch.object(reconciliation.media_text, 'message_text', side_effect=revoke):
            with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
                self.reconcile()
        self.provider.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM task_reconciliation_runs').fetchone(), (0,))

    def test_concurrent_prior_applied_input_is_seen_before_provider(self):
        identity = self.task(owner_origin_message=self.message('email'), title=CANARY)
        denied = self.message('telegram')
        writer = self.competing_writer()

        def derive(body, attachments):
            writer.execute('''INSERT INTO task_changes(task_id,message_id,evidence_key,operation,
                changes,reason,evidence,expected_version,status)
                VALUES (%s,%s,'synthetic-race','update','{}',%s,%s,0,'applied')''',
                (identity, denied, RECEIPT, RECEIPT))
            return body

        with patch.object(reconciliation.media_text, 'message_text', side_effect=derive):
            with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
                self.reconcile()
        self.provider.assert_not_called()

    def test_concurrent_task_version_change_blocks_unsent_second_batch(self):
        origin = self.message('email')
        with self.conn.transaction():
            identities = [self.task(owner_origin_message=origin,
                title=CANARY if index == 40 else f'Allowed {index}') for index in range(41)]
        writer = self.competing_writer()

        def update(payload, **kwargs):
            writer.execute('UPDATE tasks SET version=version+1 WHERE id=%s', (identities[-1],))
            return '{"changes": []}'

        self.provider.side_effect = update
        with self.assertRaisesRegex(jobs.Deferred, '^Candidate data policy blocked$'):
            self.reconcile()
        self.provider.assert_called_once()
        self.assertEqual(len(self.exported()), 40)
        self.assertNotIn(CANARY, json.dumps(self.provider.call_args_list, default=str))


if __name__ == '__main__':
    unittest.main()