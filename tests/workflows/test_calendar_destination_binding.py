"""Sealed Calendar destination/time regressions; real SQL, no network/secrets.

SQL requires P_SECRETARY_TEST_DATABASE_URL naming an explicit *_test database,
without libpq services. Never starts PostgreSQL or uses DATABASE_URL. Pure
contracts run without a database; all provider boundaries are intercepted.
"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import uuid

with tempfile.TemporaryDirectory(prefix='calendar-import-') as _bootstrap:
    with patch.dict(os.environ, {'P_SECRETARY_CONFIG_FILE': '/dev/null',
                                'DATA_DIR': _bootstrap, 'DATABASE_URL': ''}):
        from secretary.core import config, db, jobs
        from secretary.archive import message_store
        from secretary.workflows import pipeline
        from secretary.core import policy
        from secretary.connectors import gcal


CALENDAR = 'sealed-owner@group.calendar.google.com'
OTHER_CALENDAR = 'differently-shared@group.calendar.google.com'
EVENT = dict(title='Private visit', dt='2030-01-02 15:00', location='Office',
             notes='Synthetic private note', duration_min=45, reminder_minutes=15)


def validated_test_params(url):
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(url)
    if not params.get('dbname', '').endswith('_test') or 'service' in params:
        raise ValueError('An explicit database ending in _test (no service) is required')
    return params


class _CalendarPostgresFixture(unittest.TestCase):
    """Opt-in SQL only; no server bootstrap or configured connection fallback."""

    @classmethod
    def setUpClass(cls):
        url = os.environ.get('P_SECRETARY_TEST_DATABASE_URL')
        if not url:
            raise unittest.SkipTest('Set P_SECRETARY_TEST_DATABASE_URL to opt in')
        if db.psycopg is None:
            raise unittest.SkipTest('psycopg is not installed')
        params = validated_test_params(url)
        cls.enterClassContext(patch.dict(os.environ,
            {k: v for k, v in os.environ.items() if not k.startswith('PG')}, clear=True))
        cls.enterClassContext(patch.object(db, '_conn',
            side_effect=AssertionError('Configured database forbidden')))
        cls.conn = db.psycopg.connect(**params, autocommit=True,
            connect_timeout=5, prepare_threshold=None)
        cls.addClassCleanup(cls.conn.close)
        actual = cls.conn.execute('SELECT current_database()').fetchone()[0]
        if actual != params['dbname'] or not actual.endswith('_test'):
            raise AssertionError('Refusing a non-test database')
        cls.enterClassContext(patch.object(db.psycopg, 'connect',
            side_effect=AssertionError('Additional database connections forbidden')))

    def setUp(self):
        from psycopg import sql
        self.schema = 'test_calendar_destination_' + uuid.uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='15s'")
        self.conn.execute("SET lock_timeout='10s'")
        self.enterContext(patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)))
        self.enterContext(patch.dict(os.environ, {'GCAL_CALENDAR_ID': CALENDAR}))
        self.enterContext(patch.object(config, 'TIMEZONE', 'Europe/Rome'))
        self.enterContext(patch.object(config, 'GCAL_EVENT_DURATION', 60))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))


class CalendarTargetOfflineTests(unittest.TestCase):
    """Pure execution/target contracts, never dependent on SQL opt-in."""

    def setUp(self):
        self.conn = MagicMock()
        self.conn.transaction.side_effect = lambda: nullcontext(self.conn)
        self.connect = self.enterContext(patch.object(db, '_conn',
            side_effect=AssertionError('Database forbidden')))
        if db.psycopg is not None:
            self.enterContext(patch.object(db.psycopg, 'connect', side_effect=AssertionError('SQL forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('Server bootstrap forbidden')))
        self.enterContext(patch.dict(os.environ, {'GCAL_CALENDAR_ID': CALENDAR}))
        self.enterContext(patch.object(config, 'TIMEZONE', 'Europe/Rome'))
        self.enterContext(patch.object(config, 'GCAL_EVENT_DURATION', 60))
        self.enterContext(patch.object(gcal, '_load_service_account', side_effect=AssertionError('Credentials forbidden')))
        self.enterContext(patch.object(gcal, 'get_access_token', side_effect=AssertionError('Credentials forbidden')))
        self.enterContext(patch.object(gcal, 'create_event', side_effect=AssertionError('Unsealed create forbidden')))
        self.enterContext(patch.object(gcal, 'delete_event', side_effect=AssertionError('Unsealed delete forbidden')))
        self.active = self.enterContext(patch.object(gcal, 'is_active', return_value=True))
        self.api = self.enterContext(patch.object(gcal, '_api', side_effect=lambda method, path, **kw:
            {'id': kw['body']['id']} if method == 'POST' else {}))
        self.row = (jobs.event_id('synthetic'), dict(EVENT), 'pending', 'synthetic')

    def effect(self, operation='create'):
        return policy.calendar_payload(self.row, operation)

    def run_action(self, *, allowed=True, operation='create'):
        row = self.row if operation == 'create' else (*self.row[:2], 'cancel_pending', self.row[3])
        self.conn.execute.return_value.fetchone.return_value = row
        with patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)), \
             patch.object(policy, 'effect_allowed', return_value=allowed), \
             patch.object(policy, 'prepare_effect', return_value=True), \
             patch.object(policy, 'record_receipt', return_value=True):
            pipeline.action(dict(type='calendar', id=42, authority_message_id=7,
                                 authorization='calendar:offline'))

    def test_target_covers_trusted_destination_resolved_time_and_defaults(self):
        self.row[1].update(calendar_id=OTHER_CALENDAR, timezone='UTC')
        self.row[1].pop('duration_min')
        target = self.effect()['target']
        self.assertEqual(target, dict(calendar_id=CALENDAR, timezone='Europe/Rome',
            duration_min=60, reminder_minutes=15,
            start='2030-01-02T15:00:00+01:00', end='2030-01-02T16:00:00+01:00'))
        self.active.assert_not_called()
        self.api.assert_not_called()

    def test_save_event_snapshot_database_and_lease_errors_propagate(self):
        self.conn.execute.return_value.fetchone.return_value = (42,)
        errors = [RuntimeError('Snapshot failure'), jobs.LeaseLost('Lease lost')]
        if db.psycopg is not None:
            errors.append(db.psycopg.OperationalError('Database failure'))
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                 patch.object(policy, 'calendar_event_hash', side_effect=error), \
                 patch.object(policy, 'grant_calendar') as grant, \
                 patch.object(jobs, 'enqueue') as enqueue:
                with self.assertRaises(type(error)) as raised:
                    pipeline.save_event(self.conn, dict(EVENT), 'owner:7:event:0', 'owner',
                                        calendar_intent=True)
                self.assertIs(raised.exception, error)
                grant.assert_not_called()
                enqueue.assert_not_called()
        self.active.assert_not_called()
        self.api.assert_not_called()

    def test_save_event_grant_value_error_is_not_an_invalid_snapshot(self):
        self.conn.execute.return_value.fetchone.return_value = (42,)
        with patch.object(policy, 'calendar_event_hash', return_value='sealed'), \
             patch.object(policy, 'grant_calendar', side_effect=ValueError('Grant failure')):
            with self.assertRaisesRegex(ValueError, 'Grant failure'):
                pipeline.save_event(self.conn, dict(EVENT), 'owner:7:event:0', 'owner',
                                    calendar_intent=True)
        self.active.assert_not_called()
        self.api.assert_not_called()

    def test_save_event_insert_and_initial_lease_failures_propagate(self):
        with patch.object(jobs, 'assert_current', side_effect=jobs.LeaseLost()), \
             patch.object(policy, 'calendar_event_hash') as digest:
            with self.assertRaises(jobs.LeaseLost):
                pipeline.save_event(self.conn, dict(EVENT), 'owner:7:event:0', 'owner')
            self.conn.execute.assert_not_called()
            digest.assert_not_called()
        self.conn.execute.side_effect = ValueError('Insert failure')
        with patch.object(policy, 'calendar_event_hash') as digest:
            with self.assertRaisesRegex(ValueError, 'Insert failure'):
                pipeline.save_event(self.conn, dict(EVENT), 'owner:7:event:0', 'owner')
            digest.assert_not_called()

    def test_invalid_targets_fail_before_readiness_or_receipts(self):
        for operation in ('create', 'cancel'):
            for changes in ({'GCAL_CALENDAR_ID': ''}, {'GCAL_CALENDAR_ID': 'primary'},
                            {'GCAL_CALENDAR_ID': 'bad\ncalendar'}):
                with self.subTest(operation=operation, changes=changes), patch.dict(os.environ, changes):
                    with self.assertRaises(jobs.Deferred):
                        self.run_action(operation=operation)
            with patch.object(config, 'TIMEZONE', 'Invalid/Zone'):
                with self.assertRaises(jobs.Deferred):
                    self.run_action(operation=operation)
        self.active.assert_not_called()
        self.api.assert_not_called()

    def test_invalid_create_events_cannot_bypass_connector_validation(self):
        for changes in (dict(title=''), dict(location=7), dict(notes=[]), dict(dt='2000-01-01 12:00'),
                        dict(dt='2030-03-31 02:30'), dict(dt='2030-10-27 02:30'),
                        dict(duration_min=True), dict(reminder_minutes=-1)):
            with self.subTest(changes=changes):
                row = (self.row[0], {**EVENT, **changes}, *self.row[2:])
                with self.assertRaises(ValueError):
                    policy.calendar_payload(row)
        self.active.assert_not_called()
        self.api.assert_not_called()

    def test_denied_authority_never_resolves_readiness_or_credentials(self):
        for operation in ('create', 'cancel'):
            with self.assertRaises(jobs.Deferred):
                self.run_action(allowed=False, operation=operation)
        self.active.assert_not_called()
        self.api.assert_not_called()

    def test_real_policy_requires_exact_sealed_target_and_rejects_legacy_grants(self):
        for operation in ('create', 'cancel'):
            effect = self.effect(operation)
            state = 'pending' if operation == 'create' else 'cancel_pending'
            row = (*self.row[:2], state, self.row[3], operation == 'cancel')
            grant = (42, 7, policy.payload_hash('/conferma 42'), policy.payload_hash(effect),
                     self.row[3], state, operation == 'cancel', effect['target'])
            def allowed(snapshot=grant):
                self.conn.execute.return_value.fetchone.side_effect = [(42,), snapshot]
                with patch.object(policy, '_appointment', return_value=row), \
                     patch.object(policy, '_owner_text', return_value='/conferma 42'):
                    return policy.effect_allowed(self.conn, 7, 'calendar', effect,
                                                 authorization='calendar:offline')
            self.assertTrue(allowed())
            self.assertFalse(allowed((*grant[:-1], None)))
            with patch.dict(os.environ, {'GCAL_CALENDAR_ID': OTHER_CALENDAR}):
                self.assertFalse(allowed())
            with patch.object(config, 'TIMEZONE', 'UTC'):
                self.assertFalse(allowed())
        self.api.assert_not_called()
        self.active.assert_not_called()

    def test_changed_target_during_readiness_cannot_reach_api(self):
        for operation in ('create', 'cancel'):
            for change in ('calendar', 'timezone', 'duration'):
                with self.subTest(operation=operation, change=change), patch.dict(os.environ), \
                     patch.object(config, 'TIMEZONE', 'Europe/Rome'), \
                     patch.object(config, 'GCAL_EVENT_DURATION', 60):
                    self.row[1].pop('duration_min', None)
                    def ready():
                        if change == 'calendar':
                            os.environ['GCAL_CALENDAR_ID'] = OTHER_CALENDAR
                        elif change == 'timezone':
                            config.TIMEZONE = 'UTC'
                        else:
                            config.GCAL_EVENT_DURATION = 120
                        return True
                    self.active.side_effect = ready
                    if operation == 'cancel' and change == 'duration':
                        self.run_action(operation=operation)  # Duration is irrelevant to DELETE.
                        self.api.reset_mock()
                    else:
                        with self.assertRaises(jobs.Deferred):
                            self.run_action(operation=operation)
        self.api.assert_not_called()

    def test_config_mutation_after_final_check_cannot_redirect_wire_request(self):
        real_io = pipeline._calendar_io
        for operation in ('create', 'cancel'):
            self.api.reset_mock()
            expected = self.effect(operation)
            def mutate(payload, io_operation='create'):
                os.environ['GCAL_CALENDAR_ID'] = OTHER_CALENDAR
                config.TIMEZONE = 'UTC'
                config.GCAL_EVENT_DURATION = 120
                return real_io(payload, io_operation)
            with patch.dict(os.environ), patch.object(config, 'TIMEZONE', 'Europe/Rome'), \
                 patch.object(config, 'GCAL_EVENT_DURATION', 60), \
                 patch.object(pipeline, '_calendar_io', side_effect=mutate):
                self.run_action(operation=operation)
            call = self.api.call_args
            self.assertIn('/calendars/' + CALENDAR.replace('@', '%40') + '/events', call.args[1])
            self.assertNotIn(OTHER_CALENDAR.replace('@', '%40'), call.args[1])
            if operation == 'create':
                self.assertEqual(call.kwargs['body']['start'], dict(
                    dateTime=expected['target']['start'], timeZone='Europe/Rome'))
                self.assertEqual(call.kwargs['body']['end']['dateTime'], expected['target']['end'])

    def test_wire_body_matches_existing_connector_serializer(self):
        for dt in ('2030-01-02 15:00', '2030-07-02T15:00:59+00:00', '2030-03-31 01:30'):
            with self.subTest(dt=dt):
                event = {**EVENT, 'dt': dt, 'duration_min': 120}
                effect = policy.calendar_payload((self.row[0], event, *self.row[2:]))
                self.assertTrue(REAL_CREATE(event['title'], dt, location=event['location'],
                    notes=event['notes'], duration_min=120, reminder_minutes=15, event_id=self.row[0])['ok'])
                legacy_args = deepcopy(self.api.call_args.args)
                legacy_kwargs = deepcopy(self.api.call_args.kwargs)
                self.assertTrue(pipeline._calendar_io(effect))
                self.assertEqual(self.api.call_args.args, legacy_args)
                self.assertEqual(self.api.call_args.kwargs, legacy_kwargs)

    def test_conflict_reconciles_only_exact_content_on_original_target(self):
        effect = self.effect()
        self.assertTrue(pipeline._calendar_io(effect))
        expected = deepcopy(self.api.call_args.kwargs['body'])
        conflict = RuntimeError('Google Calendar API 409: conflict')
        for change in ({}, dict(id='other'), dict(status='cancelled'), dict(summary='Other'),
                       dict(start={'dateTime': '2031-01-01T00:00:00Z'}),
                       dict(reminders={'useDefault': True}), dict(description='Changed')):
            with self.subTest(change=change), patch.dict(os.environ):
                self.api.reset_mock()
                def respond(method, path, **kwargs):
                    if method == 'POST':
                        os.environ['GCAL_CALENDAR_ID'] = OTHER_CALENDAR
                        raise conflict
                    return {**expected, **change}
                self.api.side_effect = respond
                self.assertEqual(pipeline._calendar_io(effect), not bool(change))
                self.assertEqual([c.args[0] for c in self.api.call_args_list], ['POST', 'GET'])
                self.assertTrue(all(CALENDAR.replace('@', '%40') in c.args[1]
                                    for c in self.api.call_args_list))

    def test_create_requires_acknowledged_id_delete_only_accepts_absence(self):
        effect = self.effect()
        for response in ({}, {'id': 'other'}):
            self.api.side_effect = None
            self.api.return_value = response
            self.assertFalse(pipeline._calendar_io(effect))
        effect = self.effect('cancel')
        for code in (404, 410):
            self.api.side_effect = RuntimeError(f'Google Calendar API {code}: absent')
            self.assertTrue(pipeline._calendar_io(effect))
        for code in (403, 500):
            self.api.side_effect = RuntimeError(f'Google Calendar API {code}: denied')
            with self.assertRaises(RuntimeError):
                pipeline._calendar_io(effect)

    def test_inactive_connector_stops_without_provider_io(self):
        self.active.return_value = False
        for operation in ('create', 'cancel'):
            with self.assertRaisesRegex(jobs.Deferred, 'paused or unavailable'):
                self.run_action(operation=operation)
        self.api.assert_not_called()

    def test_final_lease_fence_config_change_denies_before_api(self):
        for operation in ('create', 'cancel'):
            self.active.reset_mock()
            def fence(conn=None):
                if conn is None and self.active.called:
                    os.environ['GCAL_CALENDAR_ID'] = OTHER_CALENDAR
                return True
            with patch.dict(os.environ), patch.object(jobs, 'assert_current', side_effect=fence):
                with self.assertRaises(jobs.Deferred):
                    self.run_action(operation=operation)
        self.api.assert_not_called()

    def test_revoked_inflight_grant_cannot_publish_synced_state(self):
        self.conn.execute.return_value.fetchone.return_value = self.row
        with patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)), \
             patch.object(pipeline, '_effect', return_value=True), \
             patch.object(policy, 'effect_allowed', side_effect=[True, False]):
            with self.assertRaisesRegex(jobs.Deferred, 'after I/O'):
                pipeline.action(dict(type='calendar', id=42, authority_message_id=7,
                                     authorization='calendar:old'))
        self.assertFalse(any('UPDATE appointments' in call.args[0] for call in self.conn.execute.call_args_list))

    def test_sql_requires_explicit_opt_in_even_with_configured_database(self):
        with patch.dict(os.environ, {'P_SECRETARY_TEST_DATABASE_URL': '',
                                    'DATABASE_URL': 'postgresql://forbidden/production',
                                    'PGDATABASE': 'production', 'PGSERVICE': 'deployment'}):
            with self.assertRaises(unittest.SkipTest):
                _CalendarPostgresFixture.setUpClass()
        self.connect.assert_not_called()

    def test_v3_migration_adds_target_without_backfilling_or_regranting(self):
        self.conn.execute.return_value.fetchone.return_value = None
        policy.migrate(self.conn)
        statements = '\n'.join(call.args[0] for call in self.conn.execute.call_args_list)
        self.assertEqual(policy.VERSION, 'secretary_effect_grants_v3')
        self.assertIn('ADD COLUMN IF NOT EXISTS connector_target JSONB', statements)
        self.assertNotIn('UPDATE calendar_effect_grants', statements)
        self.assertNotIn('INSERT INTO calendar_effect_grants', statements)
        self.assertNotIn('INSERT INTO data_policies', statements)
        self.assertEqual(self.conn.execute.call_args.args[1], (policy.VERSION,))
        self.conn.reset_mock()
        self.conn.execute.return_value.fetchone.return_value = (1,)
        policy.migrate(self.conn)
        self.assertEqual(self.conn.execute.call_count, 3)

    @unittest.skipIf(db.psycopg is None, 'psycopg is not installed')
    def test_sql_rejects_missing_or_production_database_and_libpq_services(self):
        for url in ('', 'host=/unused', 'dbname=production',
                    'service=deployment dbname=calendar_test', 'service= dbname=calendar_test'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validated_test_params(url)
        self.assertEqual(validated_test_params('host=/unused dbname=calendar_test'),
                         dict(host='/unused', dbname='calendar_test'))
        self.connect.assert_not_called()

    @unittest.skipIf(db.psycopg is None, 'psycopg is not installed')
    def test_sql_clears_pg_overrides_and_uses_only_explicit_test_url(self):
        class Fixture(_CalendarPostgresFixture):
            pass
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ('calendar_test',)
        def connect(**params):
            self.assertEqual(params, dict(host='/unused', dbname='calendar_test',
                autocommit=True, connect_timeout=5, prepare_threshold=None))
            self.assertFalse(any(key.startswith('PG') for key in os.environ))
            return conn
        with patch.dict(os.environ, {'P_SECRETARY_TEST_DATABASE_URL': 'host=/unused dbname=calendar_test',
                                    'PGSERVICE': 'deployment', 'PGDATABASE': 'production',
                                    'PGHOSTADDR': '192.0.2.1', 'PGOPTIONS': '-c search_path=public'}), \
             patch.object(db.psycopg, 'connect', side_effect=connect) as connection:
            try:
                Fixture.setUpClass()
            finally:
                Fixture.doClassCleanups()
            connection.assert_called_once()
        conn.close.assert_called_once()

    def test_accepted_receipt_replay_does_not_resend_calendar_request(self):
        effect = self.effect()
        self.conn.execute.return_value.fetchone.return_value = ('accepted',)
        with patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)), \
             patch.object(policy, 'prepare_effect', return_value=False), \
             patch.object(policy, 'record_receipt') as receipt:
            self.assertTrue(pipeline._effect('calendar:offline:synced', 7, 'calendar', effect,
                lambda: pipeline._calendar_io(effect), deterministic=True, authorization='calendar:offline'))
        self.api.assert_not_called()
        receipt.assert_not_called()


class CalendarDestinationBindingTests(_CalendarPostgresFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('secretary.core.ai_budget.request_content', side_effect=AssertionError('Model forbidden')))
        self.enterContext(patch.object(gcal, '_load_service_account',
            side_effect=AssertionError('Credentials forbidden')))
        self.enterContext(patch.object(gcal, 'get_access_token',
            side_effect=AssertionError('Credentials forbidden')))
        self.api = self.enterContext(patch.object(gcal, '_api', side_effect=lambda method, path, **kw:
            {'id': kw['body']['id']} if method == 'POST' else {}))
        self.active = self.enterContext(patch.object(gcal, 'is_active', return_value=True))
        self.create = self.enterContext(patch.object(gcal, 'create_event', side_effect=AssertionError('Unsealed create forbidden')))
        self.delete = self.enterContext(patch.object(gcal, 'delete_event', side_effect=AssertionError('Unsealed delete forbidden')))
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)
        policy.migrate(self.conn)

    def appointment(self, event=None):
        return pipeline.save_event(self.conn, dict(EVENT if event is None else event),
            'synthetic:' + uuid.uuid4().hex, 'Synthetic external proposal', approved=False)

    def archive(self, identity, operation):
        text = ('/annulla' if operation == 'cancel' else '/conferma') + f' {identity}'
        native_id = int(uuid.uuid4().hex[:12], 16)
        metadata = dict(account='bot:123', chat_id='7', author='7', direction='in',
            chat_type='private', content_origin='direct_text', is_forwarded=False,
            message_id=native_id)
        return self.conn.execute('''INSERT INTO telegram_messages
            (ts,channel,body,source_id,metadata,account,chat_id,author,direction,message_id)
            VALUES (%s,'telegram',%s,%s,%s,'bot:123','7','7','in',%s) RETURNING id''',
            (datetime(2026, 10, 16, 12, tzinfo=timezone.utc), text,
             json.dumps(['bot', '123', '7', native_id]), json.dumps(metadata),
             str(native_id))).fetchone()[0]

    def grant(self, identity=None, operation='create'):
        identity = identity or self.appointment()
        mid = self.archive(identity, operation)
        with self.conn.transaction():
            digest = policy.calendar_event_hash(self.conn, identity, operation=operation)
            token = policy.grant_calendar(self.conn, identity, digest, mid, operation=operation)
        self.assertIsInstance(token, str)
        payload = self.conn.execute('SELECT payload FROM work_jobs WHERE key=%s', (token,)).fetchone()[0]
        effect = policy.calendar_payload(self.row(identity), operation)
        return identity, mid, token, payload, effect

    def row(self, identity):
        return self.conn.execute('''SELECT event_id,event_data,sync_state,sync_key,done
            FROM appointments WHERE id=%s''', (identity,)).fetchone()

    def run_action(self, payload):
        # A real claimed lease and real assert_current, without heartbeat threads.
        self.conn.execute('''UPDATE work_jobs SET status='running',token='synthetic-lease',
            lease_until=clock_timestamp()+interval '15 minutes' WHERE key=%s''',
            (payload['authorization'],))
        identity = self.conn.execute('SELECT id FROM work_jobs WHERE key=%s',
                                     (payload['authorization'],)).fetchone()[0]
        context = jobs.ExecutionContext(identity, 'synthetic-lease', 'action', payload, float('inf'))
        with patch.object(jobs, 'execution_context', return_value=context):
            pipeline.action(payload)

    def assert_no_connector(self):
        self.active.assert_not_called()
        self.api.assert_not_called()
        self.create.assert_not_called()
        self.delete.assert_not_called()

    def test_owner_save_without_calendar_configuration_retains_local_proposal(self):
        event = gcal.validate_event(dict(EVENT))
        mid = self.archive(0, 'create')
        self.conn.execute('UPDATE telegram_messages SET body=%s WHERE id=%s',
                          ('Crea un appuntamento in agenda per la visita.', mid))
        key = f'owner:{mid}:event:0'
        for configured in (None, '', 'primary'):
            with self.subTest(calendar_id=configured), patch.dict(os.environ), \
                 patch.object(policy, 'grant_calendar', wraps=policy.grant_calendar) as grant, \
                 patch.object(jobs, 'enqueue', wraps=jobs.enqueue) as enqueue:
                if configured is None:
                    os.environ.pop('GCAL_CALENDAR_ID', None)
                else:
                    os.environ['GCAL_CALENDAR_ID'] = configured
                with self.conn.transaction():
                    identity = pipeline.save_event(self.conn, event, key, 'owner', approved=True)
                self.assertEqual(self.row(identity),
                                 (jobs.event_id(key), event, 'proposal', key, False))
                with self.assertRaises(ValueError):
                    policy.calendar_event_hash(self.conn, identity)
                pipeline.action(dict(type='calendar', id=identity))
                grant.assert_not_called()
                enqueue.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM appointments').fetchone(), (1,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM calendar_effect_grants').fetchone(), (0,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone(), (0,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM effect_receipts').fetchone(), (0,))
        self.assert_no_connector()

    def test_owner_save_invalid_stored_date_retains_local_proposal(self):
        event = gcal.validate_event(dict(EVENT))
        key = 'owner:7:event:0'
        identity = pipeline.save_event(self.conn, event, key, 'owner', approved=False)
        expired = {**event, 'dt': '2000-01-01 12:00'}
        with self.assertRaises(ValueError):
            gcal.validate_event(expired)  # Adapters still reject invalid input before saving.
        self.conn.execute('UPDATE appointments SET dt=%s,event_data=%s::jsonb WHERE id=%s',
                          (expired['dt'], json.dumps(expired), identity))
        before = self.row(identity)
        with patch.object(policy, 'grant_calendar', wraps=policy.grant_calendar) as grant, \
             patch.object(jobs, 'enqueue', wraps=jobs.enqueue) as enqueue:
            with self.conn.transaction():
                # A validated retry must hash the stored snapshot, not overwrite it.
                self.assertEqual(pipeline.save_event(self.conn, event, key, 'owner', approved=True), identity)
            grant.assert_not_called()
            enqueue.assert_not_called()
        self.assertEqual(self.row(identity), before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM calendar_effect_grants').fetchone(), (0,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone(), (0,))
        self.assert_no_connector()

    def test_target_and_hash_use_dynamic_trusted_config_not_event_or_static_constant(self):
        event = {**EVENT, 'calendar_id': OTHER_CALENDAR, 'timezone': 'UTC',
                 'target': dict(calendar_id=OTHER_CALENDAR, timezone='UTC')}
        identity = self.appointment(event)
        with patch.object(config, 'GCAL_CALENDAR_ID', 'stale-static-calendar'):
            identity, mid, token, payload, effect = self.grant(identity)
        expected = dict(calendar_id=CALENDAR, timezone='Europe/Rome', duration_min=45,
            reminder_minutes=15, start='2030-01-02T15:00:00+01:00', end='2030-01-02T15:45:00+01:00')
        target, digest = self.conn.execute('''SELECT connector_target,payload_hash
            FROM calendar_effect_grants WHERE token=%s''', (token,)).fetchone()
        self.assertEqual(target, expected)
        self.assertEqual(effect['target'], expected)
        self.assertEqual(digest, policy.payload_hash(effect))
        for change in ('destination', 'timezone'):
            with self.subTest(change=change):
                guard = (patch.dict(os.environ, {'GCAL_CALENDAR_ID': OTHER_CALENDAR})
                         if change == 'destination' else patch.object(config, 'TIMEZONE', 'UTC'))
                with guard:
                    self.assertNotEqual(policy.calendar_event_hash(self.conn, identity), digest)
                    self.assertFalse(policy.effect_allowed(self.conn, mid, 'calendar', effect, authorization=token))
        self.assert_no_connector()

    def test_changed_destination_or_timezone_before_worker_denies_create_and_cancel(self):
        for operation in ('create', 'cancel'):
            for change in ('destination', 'timezone'):
                with self.subTest(operation=operation, change=change):
                    identity, mid, token, payload, effect = self.grant(operation=operation)
                    before = self.row(identity)
                    guard = (patch.dict(os.environ, {'GCAL_CALENDAR_ID': OTHER_CALENDAR})
                             if change == 'destination' else patch.object(config, 'TIMEZONE', 'UTC'))
                    with guard:
                        self.assertFalse(policy.effect_allowed(self.conn, mid, 'calendar', effect, authorization=token))
                        with self.assertRaises(jobs.Deferred):
                            self.run_action(payload)
                    self.assertEqual(self.row(identity), before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM effect_receipts').fetchone(), (0,))
        self.assert_no_connector()

    def test_invalid_or_missing_target_configuration_fails_closed_before_worker(self):
        for operation in ('create', 'cancel'):
            identity, mid, token, payload, effect = self.grant(operation=operation)
            for guard in (patch.dict(os.environ, {'GCAL_CALENDAR_ID': ''}),
                          patch.dict(os.environ, {'GCAL_CALENDAR_ID': 'primary'}),
                          patch.object(config, 'TIMEZONE', 'No/Such_Zone')):
                with guard, self.subTest(operation=operation):
                    self.assertFalse(policy.effect_allowed(self.conn, mid, 'calendar', effect, authorization=token))
                    with self.assertRaises(jobs.Deferred):
                        self.run_action(payload)
        self.assert_no_connector()

    def test_unchanged_grants_execute_create_and_cancel_with_real_receipts(self):
        for operation, state in (('create', 'synced'), ('cancel', 'cancelled')):
            with self.subTest(operation=operation):
                identity, mid, token, payload, effect = self.grant(operation=operation)
                self.run_action(payload)
                self.assertEqual(self.row(identity)[2], state)
                self.assertEqual(self.conn.execute('''SELECT status,payload_hash,"authorization"
                    FROM effect_receipts WHERE key=%s''', (token + ':' + state,)).fetchone(),
                    ('accepted', policy.payload_hash(effect), token))
        self.assertEqual([c.args[0] for c in self.api.call_args_list], ['POST', 'DELETE'])
        self.assertEqual(self.api.call_args_list[0].kwargs['body']['summary'], EVENT['title'])
        self.create.assert_not_called()
        self.delete.assert_not_called()

    def test_retry_after_uncertain_create_reconciles_exact_event_without_duplicate(self):
        identity, mid, token, payload, effect = self.grant()
        sent = {}
        def timeout(method, path, **kwargs):
            sent.update(deepcopy(kwargs['body']))
            raise TimeoutError('Synthetic lost acknowledgement')
        self.api.side_effect = timeout
        with self.assertRaises(TimeoutError):
            self.run_action(payload)
        self.assertEqual(self.row(identity)[2], 'pending')
        self.assertEqual(self.conn.execute('SELECT status FROM effect_receipts').fetchall(), [('uncertain',)])
        self.api.reset_mock()
        def retry(method, path, **kwargs):
            if method == 'POST':
                self.assertEqual(kwargs['body'], sent)
                raise RuntimeError('Google Calendar API 409: conflict')
            return sent
        self.api.side_effect = retry
        self.run_action(payload)
        self.assertEqual(self.row(identity)[2], 'synced')
        self.assertEqual([c.args[0] for c in self.api.call_args_list], ['POST', 'GET'])
        self.assertEqual(self.conn.execute('SELECT status FROM effect_receipts ORDER BY key').fetchall(),
                         [('uncertain',), ('accepted',)])
        self.api.reset_mock()
        self.run_action(payload)
        self.api.assert_not_called()

    def test_new_target_grant_during_io_cannot_be_marked_done_by_old_attempt(self):
        for operation in ('create', 'cancel'):
            with self.subTest(operation=operation):
                identity, mid, token, payload, effect = self.grant(operation=operation)
                replacement = []
                def io(method, path, **kwargs):
                    self.assertIn(CALENDAR.replace('@', '%40'), path)
                    os.environ['GCAL_CALENDAR_ID'] = OTHER_CALENDAR
                    with patch.object(jobs, 'execution_context', return_value=None):
                        replacement.append(self.grant(identity, operation))
                    return {'id': kwargs['body']['id']} if method == 'POST' else {}
                self.api.side_effect = io
                with patch.dict(os.environ):
                    with self.assertRaisesRegex(jobs.Deferred, 'after I/O'):
                        self.run_action(payload)
                    self.assertEqual(self.row(identity)[2], 'pending' if operation == 'create' else 'cancel_pending')
                    self.assertTrue(self.conn.execute('SELECT revoked FROM calendar_effect_grants WHERE token=%s',
                                                     (token,)).fetchone()[0])
                    self.assertEqual(self.conn.execute('SELECT status FROM effect_receipts WHERE key=%s',
                        (token + ':' + effect['state'],)).fetchone(), ('accepted',))
                    self.api.side_effect = lambda method, path, **kw: {'id': kw['body']['id']} if method == 'POST' else {}
                    self.run_action(replacement[0][3])
                    self.assertEqual(self.row(identity)[2], effect['state'])
                    self.assertIn(OTHER_CALENDAR.replace('@', '%40'), self.api.call_args.args[1])

    def test_target_change_during_readiness_cannot_reach_connector(self):
        for operation in ('create', 'cancel'):
            for change in ('destination', 'timezone', 'expiry', 'native'):
                with self.subTest(operation=operation, change=change):
                    identity, mid, token, payload, effect = self.grant(operation=operation)
                    def readiness():
                        if change == 'destination':
                            os.environ['GCAL_CALENDAR_ID'] = OTHER_CALENDAR
                        elif change == 'timezone':
                            config.TIMEZONE = 'UTC'
                        elif change == 'expiry':
                            self.conn.execute('''UPDATE calendar_effect_grants
                                SET expires_at=clock_timestamp()-interval '1 second' WHERE token=%s''', (token,))
                        else:
                            self.conn.execute("UPDATE telegram_messages SET author='8' WHERE id=%s", (mid,))
                        return True
                    with patch.dict(os.environ), patch.object(config, 'TIMEZONE', 'Europe/Rome'):
                        self.active.side_effect = readiness
                        with self.assertRaises(jobs.Deferred):
                            self.run_action(payload)
                    self.assertEqual(self.row(identity)[2], 'cancel_pending' if operation == 'cancel' else 'pending')
        self.create.assert_not_called()
        self.delete.assert_not_called()
        self.api.assert_not_called()

    def test_final_lease_check_target_change_is_revalidated_directly_before_connector(self):
        for operation in ('create', 'cancel'):
            identity, mid, token, payload, effect = self.grant(operation=operation)
            real_assert = jobs.assert_current
            def fence(conn=None):
                result = real_assert(conn)
                if conn is None and self.active.called:
                    os.environ['GCAL_CALENDAR_ID'] = OTHER_CALENDAR
                return result
            self.active.reset_mock()
            with patch.dict(os.environ), patch.object(jobs, 'assert_current', side_effect=fence):
                with self.assertRaises(jobs.Deferred):
                    self.run_action(payload)
        self.create.assert_not_called()
        self.delete.assert_not_called()
        self.api.assert_not_called()

    def test_expiry_revocation_and_native_checks_still_deny_before_connector(self):
        mutations = (
            "UPDATE calendar_effect_grants SET expires_at=clock_timestamp()-interval '1 second' WHERE token=%s",
            'UPDATE calendar_effect_grants SET revoked=TRUE WHERE token=%s',
            "UPDATE telegram_messages SET metadata=metadata || '{\"historical\":true}'::jsonb WHERE id=%s",
            "UPDATE telegram_messages SET metadata=metadata || '{\"quote\":{}}'::jsonb WHERE id=%s",
            "UPDATE telegram_messages SET body='Different current instruction' WHERE id=%s",
            "UPDATE telegram_messages SET author='8' WHERE id=%s")
        for operation in ('create', 'cancel'):
            for mutation in mutations:
                with self.subTest(operation=operation, mutation=mutation):
                    identity, mid, token, payload, effect = self.grant(operation=operation)
                    self.conn.execute(mutation, (token if 'calendar_effect_grants' in mutation else mid,))
                    self.assertFalse(policy.effect_allowed(self.conn, mid, 'calendar', effect, authorization=token))
                    with self.assertRaises(jobs.Deferred):
                        self.run_action(payload)
        self.assert_no_connector()

    def test_prepared_receipt_attempt_rechecks_destination_timezone_expiry_and_native(self):
        for operation in ('create', 'cancel'):
            for change in ('destination', 'timezone', 'expiry', 'native'):
                with self.subTest(operation=operation, change=change):
                    identity, mid, token, payload, effect = self.grant(operation=operation)
                    key = token + ':prepared'
                    self.assertTrue(policy.prepare_effect(self.conn, key, mid, 'calendar',
                        effect, authorization=token))
                    with patch.dict(os.environ), patch.object(config, 'TIMEZONE', 'Europe/Rome'):
                        if change == 'destination':
                            os.environ['GCAL_CALENDAR_ID'] = OTHER_CALENDAR
                        elif change == 'timezone':
                            config.TIMEZONE = 'UTC'
                        elif change == 'expiry':
                            self.conn.execute('''UPDATE calendar_effect_grants
                                SET expires_at=clock_timestamp()-interval '1 second' WHERE token=%s''', (token,))
                        else:
                            self.conn.execute("UPDATE telegram_messages SET author='8' WHERE id=%s", (mid,))
                        self.assertFalse(policy.record_receipt(self.conn, key, 'attempted'))
                    self.assertEqual(self.conn.execute('SELECT status FROM effect_receipts WHERE key=%s',
                                                       (key,)).fetchone(), ('blocked',))
        self.assert_no_connector()

    def test_replay_cannot_rebind_changed_config_or_renew_expired_grant(self):
        identity, mid, token, payload, effect = self.grant()
        before = self.conn.execute('SELECT * FROM calendar_effect_grants').fetchall()
        for change in ('destination', 'timezone'):
            guard = (patch.dict(os.environ, {'GCAL_CALENDAR_ID': OTHER_CALENDAR})
                     if change == 'destination' else patch.object(config, 'TIMEZONE', 'UTC'))
            with guard:
                digest = policy.calendar_event_hash(self.conn, identity)
                self.assertIsNone(policy.grant_calendar(self.conn, identity, digest, mid))
        self.assertEqual(self.conn.execute('SELECT * FROM calendar_effect_grants').fetchall(), before)
        self.conn.execute("UPDATE calendar_effect_grants SET expires_at=clock_timestamp()-interval '1 second'")
        digest = policy.calendar_event_hash(self.conn, identity)
        self.assertIsNone(policy.grant_calendar(self.conn, identity, digest, mid))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone(), (1,))
        # A different, current native command may explicitly approve the new target.
        with patch.dict(os.environ, {'GCAL_CALENDAR_ID': OTHER_CALENDAR}):
            identity, new_mid, new_token, payload, effect = self.grant(identity)
            self.assertNotEqual(new_mid, mid)
            self.assertNotEqual(new_token, token)
            self.run_action(payload)
        self.api.assert_called_once()
        self.assertIn(OTHER_CALENDAR.replace('@', '%40'), self.api.call_args.args[1])

    def test_target_change_between_hash_and_grant_denies_without_promoting(self):
        identity = self.appointment()
        mid = self.archive(identity, 'create')
        digest = policy.calendar_event_hash(self.conn, identity)
        with patch.dict(os.environ, {'GCAL_CALENDAR_ID': OTHER_CALENDAR}):
            self.assertIsNone(policy.grant_calendar(self.conn, identity, digest, mid))
        self.assertEqual(self.row(identity)[2], 'proposal')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM calendar_effect_grants').fetchone(), (0,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone(), (0,))

    def test_default_duration_and_resolved_time_are_bound_not_just_event_json(self):
        event = {k: v for k, v in EVENT.items() if k != 'duration_min'}
        identity, mid, token, payload, effect = self.grant(self.appointment(event))
        self.assertEqual(effect['target']['end'], '2030-01-02T16:00:00+01:00')
        with patch.object(config, 'GCAL_EVENT_DURATION', 120):
            self.assertFalse(policy.effect_allowed(self.conn, mid, 'calendar', effect, authorization=token))
            with self.assertRaises(jobs.Deferred):
                self.run_action(payload)
        self.assert_no_connector()

    def test_create_target_times_match_actual_connector_body_without_remote_calls(self):
        for dt in ('2030-01-02 15:00', '2030-07-02T15:00:59+00:00', '2030-03-31 01:30'):
            event = {**EVENT, 'dt': dt, 'duration_min': 120}
            identity = self.appointment(event)
            effect = policy.calendar_payload(self.row(identity))
            # Execute the real connector serializer, intercepting its API boundary.
            with patch.object(gcal, 'create_event', wraps=REAL_CREATE), patch.object(
                    gcal, '_api', return_value={'id': effect['event_id']}) as api:
                self.assertTrue(gcal.create_event(event['title'], event['dt'],
                    duration_min=120, reminder_minutes=15, event_id=effect['event_id'])['ok'])
            body = api.call_args.kwargs['body']
            self.assertEqual(effect['target']['start'], body['start']['dateTime'])
            self.assertEqual(effect['target']['end'], body['end']['dateTime'])
            self.assertEqual(effect['target']['timezone'], body['start']['timeZone'])
            self.assertIn('/calendars/' + CALENDAR.replace('@', '%40') + '/events', api.call_args.args[1])

    def test_v3_migration_preserves_v2_rows_and_denies_legacy_unbound_grants(self):
        grants = [self.grant(operation=operation) for operation in ('create', 'cancel')]
        for identity, mid, token, payload, effect in grants:
            legacy = {k: v for k, v in effect.items() if k != 'target'}
            self.conn.execute('UPDATE calendar_effect_grants SET payload_hash=%s WHERE token=%s',
                              (policy.payload_hash(legacy), token))
        self.conn.execute("INSERT INTO data_policies(channel,effect,allowed,version) VALUES ('email','ai_text',TRUE,8)")
        self.conn.execute('''INSERT INTO effect_receipts(key,message_id,effect,payload_hash,status)
            VALUES ('legacy-receipt',42,'calendar_write','legacy-hash','uncertain')''')
        self.conn.execute('ALTER TABLE calendar_effect_grants DROP COLUMN connector_target')
        self.conn.execute('UPDATE schema_migrations SET version=%s WHERE version=%s',
                          ('secretary_effect_grants_v2', policy.VERSION))
        tables = ('calendar_effect_grants', 'data_policies', 'effect_receipts', 'work_jobs')
        before = {table: self.conn.execute('SELECT * FROM ' + table).fetchall() for table in tables}
        policy.migrate(self.conn)
        for table in tables:
            after = self.conn.execute('SELECT * FROM ' + table).fetchall()
            self.assertEqual(after, [(*row, None) for row in before[table]]
                             if table == 'calendar_effect_grants' else before[table])
        self.assertEqual(self.conn.execute('''SELECT version FROM schema_migrations
            WHERE version LIKE 'secretary_effect_grants_%%' ORDER BY version''').fetchall(),
            [('secretary_effect_grants_v2',), ('secretary_effect_grants_v3',)])
        for identity, mid, token, payload, effect in grants:
            legacy = {k: v for k, v in effect.items() if k != 'target'}
            for candidate in (legacy, effect):
                self.assertFalse(policy.effect_allowed(self.conn, mid, 'calendar', candidate, authorization=token))
            self.assertIsNone(policy.grant_calendar(self.conn, identity,
                policy.calendar_event_hash(self.conn, identity, operation='cancel' if effect['state'] == 'cancelled' else 'create'),
                mid, operation='cancel' if effect['state'] == 'cancelled' else 'create'))
            with self.assertRaises(jobs.Deferred):
                self.run_action(payload)
        self.assert_no_connector()
        # Fresh native approvals can recover both pending legacy operations;
        # the old commands themselves cannot upgrade their missing target.
        for identity, mid, token, payload, effect in grants:
            operation = 'cancel' if effect['state'] == 'cancelled' else 'create'
            new = self.grant(identity, operation)
            self.assertNotEqual(new[2], token)
            self.run_action(new[3])
        self.assertEqual([c.args[0] for c in self.api.call_args_list], ['POST', 'DELETE'])

    def test_fresh_v3_migration_is_idempotent_and_caller_atomic(self):
        before = self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        policy.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall(), before)
        self.conn.execute('DELETE FROM schema_migrations WHERE version=%s', (policy.VERSION,))
        self.conn.execute('ALTER TABLE calendar_effect_grants DROP COLUMN connector_target')
        with self.assertRaisesRegex(RuntimeError, 'caller abort'), self.conn.transaction():
            policy.migrate(self.conn)
            raise RuntimeError('caller abort')
        self.assertEqual(self.conn.execute('''SELECT count(*) FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='calendar_effect_grants'
            AND column_name='connector_target' ''').fetchone(), (0,))
        self.assertIsNone(self.conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s',
                                           (policy.VERSION,)).fetchone())
        policy.migrate(self.conn)


REAL_CREATE = gcal.create_event


if __name__ == '__main__':
    unittest.main()