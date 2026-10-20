"""Policy, ricevute e ciclo di vita delle attività su PostgreSQL di test (mai config.DATABASE_URL).

Opt-in: PI_SECRETARY_TEST_DATABASE_URL con un database il cui nome finisce in _test.
Nessuna applicazione, worker, AI o servizio esterno viene avviato.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch
import uuid

import config
import db
import jobs
import message_store
import policy
import task_reconciliation as lifecycle
from agent import owner as agent_owner


class _PostgresFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
        if not url:
            raise unittest.SkipTest('Set PI_SECRETARY_TEST_DATABASE_URL to opt in')
        if db.psycopg is None:
            raise unittest.SkipTest('psycopg is not installed')
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(url)
        if not params.get('dbname', '').endswith('_test') or params.get('service'):
            raise ValueError('An explicit database ending in _test (no service) is required')
        # Ignore libpq service/host/options overrides, including PGHOSTADDR.
        cls.enterClassContext(patch.dict(os.environ,
            {k: v for k, v in os.environ.items() if not k.startswith('PG')}, clear=True))
        cls.enterClassContext(patch.object(db, '_conn',
            side_effect=AssertionError('Configured database access is forbidden')))
        cls.conn = db.psycopg.connect(**params, autocommit=True, connect_timeout=5, prepare_threshold=None)
        cls.addClassCleanup(cls.conn.close)

    def setUp(self):
        from psycopg import sql
        self.schema = 'test_policy_' + uuid.uuid4().hex
        self.views = 'agent_pol_' + uuid.uuid4().hex[:12]
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='15s'")
        self.conn.execute("SET lock_timeout='10s'")
        self.enterContext(patch.object(config, 'AGENT_DB_SCHEMA', self.views))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden')))

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(self.views)))
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))


class TaskLifecyclePostgresTests(_PostgresFixture):
    def setUp(self):
        super().setUp()
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)
        agent_owner.migrate(self.conn)
        policy.migrate(self.conn)

    def task(self, **fields):
        title = fields.pop('title', 'Send report')
        deadline = fields.pop('deadline', datetime(2030, 1, 2, tzinfo=timezone.utc))
        self.assertFalse(fields, 'Unknown task fixture fields')
        return self.conn.execute('INSERT INTO tasks(title,deadline) VALUES (%s,%s) RETURNING id',
                                 (title, deadline)).fetchone()[0]

    def message(self, table='telegram_messages', *, metadata=None, ts=None, acquired=None, owner=True):
        from psycopg import sql
        self.assertIn(table, message_store.TABLES.values())
        acquired = acquired or datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        native = ('offline', None, None, 'in')
        metadata = dict(metadata or {})
        source_id, message_id = uuid.uuid4().hex, None
        if table == 'telegram_messages':
            # Authority comes from native columns, not sender labels or model flags.
            identity = '7' if owner else '8'
            native = ('bot:123', identity, identity, 'in')
            message_id = int(uuid.uuid4().hex[:8], 16) + 1
            source_id = json.dumps(['bot', '123', identity, message_id])
            metadata = {**dict(chat_type='private', content_origin='direct_text', is_forwarded=False,
                              account=native[0], chat_id=identity, author=identity, direction='in',
                              message_id=message_id), **metadata}
        return self.conn.execute(sql.SQL('''INSERT INTO {}
            (channel,body,metadata,ts,acquired_at,account,chat_id,author,direction,source_id,message_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''').format(sql.Identifier(table)),
            (table.split('_')[0], 'Report sent', json.dumps(metadata), ts, acquired, *native,
             source_id, str(message_id) if message_id is not None else None)).fetchone()[0]

    def grant_ai(self, effect, channel='telegram'):
        self.assertIn(effect, ('ai_text', 'ai_media'))
        self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed) VALUES (%s,%s,TRUE)
            ON CONFLICT(channel,effect) DO UPDATE SET allowed=TRUE''', (channel, effect))

    def proposal(self, task, key, **fields):
        data = dict(task_id=task, expected_version=0, evidence_key=key,
                    operation='complete', changes={}, reason='Explicit confirmation',
                    evidence='Report sent')
        data.update(fields)
        return data

    def snapshot(self):
        # Compare complete persisted rows, not mocks or just return values.
        return {table: self.conn.execute('SELECT * FROM ' + table + ' ORDER BY id').fetchall()
                for table in ('tasks', 'task_changes', 'appointments', 'work_jobs')}

    def derived_work(self, task):
        appointment = self.conn.execute('''INSERT INTO appointments
            (title,dt,sync_key,sync_state,event_id,event_data)
            VALUES ('Report','2030-01-02 00:00',%s,'synced','test-event','{}') RETURNING id''',
            (lifecycle.calendar_key(task, 0),)).fetchone()[0]
        jobs.enqueue(self.conn, 'action', f'task-reminder:{task}',
                     {'type': 'reminder', 'id': task, 'deadline': '2030-01-02T00:00:00+00:00'})
        self.conn.execute("""UPDATE work_jobs SET status='running',token='old-lease',
            lease_until=now()+interval '1 hour' WHERE key=%s""", (f'task-reminder:{task}',))
        return appointment

    def test_policy_receipts_use_actual_columns_and_never_allow_a_repeated_attempt(self):
        mid, payload = self.message(), {'text': 'Local owner notice', 'recipient': '7'}
        with self.conn.transaction():
            self.assertTrue(policy.prepare_effect(self.conn, 'receipt', mid, 'notify', payload,
                                                  owner_required=True))
        row = self.conn.execute('''SELECT message_id,effect,owner_required,status,token,
            "authorization",payload_hash FROM effect_receipts WHERE key='receipt' ''').fetchone()
        self.assertEqual(row, (mid, 'notify', True, 'authorized', None, None, policy.payload_hash(payload)))
        self.assertTrue(policy.record_receipt(self.conn, 'receipt', 'attempted'))
        self.assertFalse(policy.record_receipt(self.conn, 'receipt', 'attempted'))
        self.assertTrue(policy.record_receipt(self.conn, 'receipt', 'accepted'))
        self.assertTrue(policy.record_receipt(self.conn, 'receipt', 'verified'))
        with self.conn.transaction():
            self.assertFalse(policy.prepare_effect(self.conn, 'receipt', mid, 'notify', payload,
                                                   owner_required=True))
        with self.assertRaisesRegex(ValueError, 'identity conflict'), self.conn.transaction():
            policy.prepare_effect(self.conn, 'receipt', mid, 'notify', {'text': 'Changed'},
                                  owner_required=True)
        self.assertEqual(self.conn.execute('SELECT status FROM effect_receipts').fetchall(), [('verified',)])

    def test_policy_receipt_attempt_rechecks_native_attestation_and_cannot_rearm_blocked(self):
        mid, payload = self.message(), {'text': 'Owner-only notice'}
        self.assertTrue(policy.prepare_effect(self.conn, 'revoke', mid, 'notify', payload,
                                              owner_required=True))
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata-'content_origin' WHERE id=%s", (mid,))
        self.assertFalse(policy.record_receipt(self.conn, 'revoke', 'attempted'))
        self.assertEqual(self.conn.execute('SELECT status FROM effect_receipts').fetchone(), ('blocked',))
        self.conn.execute('''UPDATE telegram_messages SET metadata=metadata ||
            '{"content_origin":"direct_text"}'::jsonb WHERE id=%s''', (mid,))
        self.assertFalse(policy.prepare_effect(self.conn, 'revoke', mid, 'notify', payload, owner_required=True))
        self.assertFalse(policy.record_receipt(self.conn, 'revoke', 'attempted'))

    def test_nullable_receipt_source_is_not_a_blanket_system_authorization(self):
        payload = {'text': 'Unsealed notice', 'recipient': '7'}
        for authorization in (None, True, 'digest:unsealed', 'calendar:unsealed'):
            with self.subTest(authorization=authorization):
                self.assertFalse(policy.prepare_effect(self.conn, 'unsealed', None, 'notify', payload,
                                                       authorization=authorization))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM effect_receipts').fetchone(), (0,))

    def test_policy_ai_receipt_requires_explicit_consent_and_rechecks_revocation(self):
        mid, payload = self.message('email_messages'), {'text': 'Local evidence'}
        self.assertFalse(policy.prepare_effect(self.conn, 'ai', mid, 'ai_text', payload))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM effect_receipts').fetchone(), (0,))
        self.grant_ai('ai_text', channel='email')
        self.assertTrue(policy.prepare_effect(self.conn, 'ai', mid, 'ai_text', payload))
        self.conn.execute("UPDATE data_policies SET allowed=FALSE WHERE channel='email' AND effect='ai_text'")
        self.assertFalse(policy.record_receipt(self.conn, 'ai', 'attempted'))
        self.assertEqual(self.conn.execute('SELECT status FROM effect_receipts').fetchone(), ('blocked',))

    def test_policy_native_attestations_cannot_be_substituted_by_owner_flags(self):
        for overrides in ({'content_origin': None}, {'content_origin': 'caption'},
                          {'content_origin': 'transcript'}, {'is_forwarded': None},
                          {'is_forwarded': True}, {'forward_origin': {}},
                          {'quote': {'text': 'Report sent'}}, {'message_id': '42'},
                          {'account': 'bot:999'}, {'author': '8'}):
            with self.subTest(overrides=overrides):
                mid = self.message(metadata={**overrides, 'owner': True, 'trusted': True})
                self.assertTrue(policy.can_operate(self.conn, mid, 'task_propose'))
                self.assertFalse(policy.can_operate(self.conn, mid, 'task_apply'))

    def test_migrate_is_idempotent_and_fences_only_unfinished_legacy_reminders(self):
        task = self.task()
        deadline = self.conn.execute('SELECT deadline FROM tasks WHERE id=%s', (task,)).fetchone()[0]
        for status in ('pending', 'running', 'done'):
            jobs.enqueue(self.conn, 'action', 'legacy:' + status, {'type': 'reminder', 'id': task})
            self.conn.execute('UPDATE work_jobs SET status=%s WHERE key=%s',
                              (status, 'legacy:' + status))
        explicit = {'type': 'reminder', 'id': task, 'deadline': '2029-01-01T00:00:00+00:00'}
        jobs.enqueue(self.conn, 'action', 'explicit', explicit)
        for _ in range(2):
            lifecycle.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT version,last_evidence_id FROM tasks').fetchone(),
                         (0, None))
        for key, payload, status in self.conn.execute(
                'SELECT key,payload,status FROM work_jobs ORDER BY key'):
            if key == 'explicit':
                self.assertEqual(payload, explicit)
            elif status == 'done':
                self.assertNotIn('deadline', payload)
            else:
                self.assertEqual(datetime.fromisoformat(payload['deadline']), deadline)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM task_changes').fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT to_regclass('task_reconciliation_runs')").fetchone()[0])

    def test_complete_versions_audit_and_cleans_derived_work_atomically(self):
        lifecycle.migrate(self.conn)
        task, mid = self.task(), self.message()
        appointment = self.derived_work(task)
        competing = lifecycle._record(self.conn, self.proposal(task, 'competing'))
        data = self.proposal(task, 'complete', message_id=mid)
        self.assertEqual(lifecycle.apply_change(self.conn, data), 'applied')
        self.assertEqual(self.conn.execute('''SELECT status,version,last_evidence_id,
            completed_at IS NOT NULL FROM tasks WHERE id=%s''', (task,)).fetchone(),
            ('done', 1, mid, True))
        audit = self.conn.execute('''SELECT id,operation,changes,expected_version,status,
            message_id,evidence_revision,reason,evidence FROM task_changes WHERE evidence_key='complete' ''').fetchone()
        self.assertEqual(audit[1:], ('complete', {}, 0, 'applied', mid, '',
                                     'Explicit confirmation', 'Report sent'))
        self.assertEqual(self.conn.execute('SELECT status FROM task_changes WHERE id=%s',
                                          (competing,)).fetchone()[0], 'stale')
        self.assertEqual(self.conn.execute('SELECT sync_key,done,sync_state FROM appointments WHERE id=%s',
                                          (appointment,)).fetchone(), (None, True, 'cancel_pending'))
        self.assertEqual(self.conn.execute('''SELECT status,token,lease_until,finished_at IS NOT NULL
            FROM work_jobs WHERE key=%s''', (f'task-reminder:{task}',)).fetchone(),
            ('done', None, None, True))
        self.assertEqual(self.conn.execute('SELECT payload FROM work_jobs WHERE key=%s',
            (f'task-calendar:{task}:1:{appointment}',)).fetchone()[0],
            {'type': 'calendar', 'id': appointment})
        before = self.snapshot()
        self.assertEqual(lifecycle.apply_change(self.conn, audit[0]), 'applied')
        self.assertEqual(lifecycle.apply_change(self.conn, data), 'duplicate')
        self.assertEqual(self.snapshot(), before)

    def test_stale_evidence_revision_preserves_task_and_derived_work(self):
        lifecycle.migrate(self.conn)
        task = self.task()
        self.derived_work(task)
        mid = self.message(metadata={'revision_date': '2026-10-16T10:00:00+00:00'})
        proposal = lifecycle._record(self.conn, self.proposal(task, 'old-edit', message_id=mid,
            evidence_revision='2026-10-16T09:00:00+00:00'))
        before = self.snapshot()
        self.assertEqual(lifecycle.apply_change(self.conn, proposal), 'stale')
        after = self.snapshot()
        for table in ('tasks', 'appointments', 'work_jobs'):
            self.assertEqual(after[table], before[table])
        self.assertEqual(self.conn.execute('SELECT status FROM task_changes WHERE id=%s',
                                          (proposal,)).fetchone()[0], 'stale')

    def test_matching_evidence_revision_compares_timestamp_not_offset_text(self):
        lifecycle.migrate(self.conn)
        task = self.task()
        mid = self.message(metadata={'revision_date': '2026-10-16T12:00:00+02:00'})
        self.assertEqual(lifecycle.apply_change(self.conn,
            self.proposal(task, 'same-edit', message_id=mid,
                          evidence_revision='2026-10-16T10:00:00+00:00')), 'applied')

    def test_older_evidence_chronology_is_stale_even_with_current_task_version(self):
        lifecycle.migrate(self.conn)
        base = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        hour = timedelta(hours=1)
        cases = (
            ('native_time_before_acquisition', base, base + hour, base + hour, base),
            ('acquisition_tiebreak', base, base, base, base + hour),
            ('identity_tiebreak', base, base, base, base),
            ('missing_native_time', None, base, None, base + hour),
        )
        for name, old_ts, old_acquired, new_ts, new_acquired in cases:
            with self.subTest(case=name):
                task = self.task()
                older = self.message(ts=old_ts, acquired=old_acquired)
                newer = self.message(ts=new_ts, acquired=new_acquired)
                self.assertEqual(lifecycle.apply_change(self.conn, self.proposal(
                    task, name + ':new', operation='update', changes={'title': 'New title'},
                    message_id=newer)), 'applied')
                before = self.conn.execute('SELECT * FROM tasks WHERE id=%s', (task,)).fetchone()
                self.assertEqual(lifecycle.apply_change(self.conn, self.proposal(
                    task, name + ':old', expected_version=1, message_id=older)), 'stale')
                self.assertEqual(self.conn.execute('SELECT * FROM tasks WHERE id=%s',
                                                  (task,)).fetchone(), before)
                self.assertEqual(self.conn.execute('SELECT status FROM task_changes WHERE evidence_key=%s',
                                                  (name + ':old',)).fetchone()[0], 'stale')

    def test_newer_evidence_can_apply_after_previous_update(self):
        lifecycle.migrate(self.conn)
        task = self.task()
        older = self.message(ts=datetime(2026, 10, 15, tzinfo=timezone.utc))
        newer = self.message(ts=datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertEqual(lifecycle.apply_change(self.conn, self.proposal(task, 'first',
            operation='update', changes={'title': 'Updated'}, message_id=older)), 'applied')
        self.assertEqual(lifecycle.apply_change(self.conn, self.proposal(task, 'second',
            expected_version=1, message_id=newer)), 'applied')
        self.assertEqual(self.conn.execute('SELECT status,version,last_evidence_id FROM tasks').fetchone(),
                         ('done', 2, newer))

    def test_stale_task_version_does_not_apply(self):
        lifecycle.migrate(self.conn)
        task = self.task()
        self.conn.execute('UPDATE tasks SET version=3 WHERE id=%s', (task,))
        self.assertEqual(lifecycle.apply_change(self.conn, self.proposal(task, 'old-version')), 'stale')
        self.assertEqual(self.conn.execute('SELECT status,version,completed_at FROM tasks').fetchone(),
                         ('open', 3, None))
        self.assertEqual(self.conn.execute('SELECT status FROM task_changes').fetchone()[0], 'stale')

    def test_database_outbox_failure_rolls_back_task_audit_and_calendar_then_retry_works(self):
        import psycopg
        lifecycle.migrate(self.conn)
        task = self.task()
        self.derived_work(task)
        lifecycle._record(self.conn, self.proposal(task, 'competing'))
        # A real SQL failure after task/audit/calendar mutations, not a mocked transaction.
        self.conn.execute('''CREATE FUNCTION fail_calendar_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.key LIKE 'task-calendar:%' THEN
                    RAISE EXCEPTION 'test outbox failure';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER fail_calendar_outbox BEFORE INSERT ON work_jobs
            FOR EACH ROW EXECUTE FUNCTION fail_calendar_outbox()''')
        before = self.snapshot()
        data = self.proposal(task, 'rollback', message_id=self.message())
        with self.assertRaisesRegex(psycopg.errors.RaiseException, 'test outbox failure'):
            lifecycle.apply_change(self.conn, data)
        self.assertEqual(self.snapshot(), before)
        self.conn.execute('DROP TRIGGER fail_calendar_outbox ON work_jobs')
        self.assertEqual(lifecycle.apply_change(self.conn, data), 'applied')

    def test_caller_transaction_rollback_includes_successful_nested_apply(self):
        lifecycle.migrate(self.conn)
        task, mid = self.task(), self.message()
        self.derived_work(task)
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'caller abort'), self.conn.transaction():
            self.assertEqual(lifecycle.apply_change(self.conn,
                self.proposal(task, 'caller-rollback', message_id=mid)), 'applied')
            raise RuntimeError('caller abort')
        self.assertEqual(self.snapshot(), before)

    def test_third_party_evidence_needs_verified_current_owner_confirmation(self):
        lifecycle.migrate(self.conn)
        task = self.task()
        self.derived_work(task)
        mid = self.message(owner=False)
        proposal = lifecycle._record(self.conn, self.proposal(task, 'third-party', message_id=mid))
        before = self.snapshot()
        self.assertEqual(lifecycle.apply_change(self.conn, proposal), 'blocked')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute('''SELECT effect,allowed,reason FROM effect_audit
            WHERE message_id=%s ORDER BY id''', (mid,)).fetchall(),
            [('task_propose', True, 'allowed'), ('task_apply', False, 'owner_required')])

        # Only this explicitly manual confirmation supplies current owner authority.
        authority = self.message()
        self.assertEqual(lifecycle.apply_change(self.conn, proposal,
            authority_message_id=authority), 'applied')
        self.assertEqual(self.conn.execute('SELECT status,version,last_evidence_id FROM tasks').fetchone(),
                         ('done', 1, mid))
        self.assertEqual(self.conn.execute('''SELECT effect,allowed,reason FROM effect_audit
            WHERE message_id=%s ORDER BY id''', (authority,)).fetchall(),
            [('owner_command', True, 'allowed')])

    def test_owner_confirmation_cannot_promote_historical_evidence(self):
        lifecycle.migrate(self.conn)
        task = self.task()
        self.derived_work(task)
        mid = self.message(metadata={'historical': True})
        proposal = lifecycle._record(self.conn, self.proposal(task, 'history', message_id=mid))
        authority = self.message()
        before = self.snapshot()
        self.assertEqual(lifecycle.apply_change(self.conn, proposal,
            authority_message_id=authority), 'blocked')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute('''SELECT effect,allowed,reason FROM effect_audit
            WHERE message_id=%s ORDER BY id''', (mid,)).fetchall(),
            [('task_propose', False, 'historical')])


class PolicyMigrationPostgresTests(_PostgresFixture):
    def test_full_bootstrap_and_replay_preserve_policy_receipts_and_default_deny(self):
        with patch.object(db, 'available', return_value=True), patch.object(
                db, '_conn', side_effect=lambda: nullcontext(self.conn)):
            self.assertEqual(db.init(), 0)
            self.assertEqual(self.conn.execute('SELECT provenance FROM secretary_profile').fetchone(), ('fresh',))
            before = self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
            self.assertEqual(db.init(), 0)
        self.assertEqual(self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall(), before)
        self.assertIn(policy.VERSION, {row[0] for row in before})
        for table in ('data_policies', 'effect_audit', 'effect_receipts',
                      'calendar_effect_grants', 'system_notifications', 'work_jobs'):
            self.assertEqual(self.conn.execute('SELECT count(*) FROM ' + table).fetchone(), (0,), table)

    def test_minimal_installed_schema_upgrades_receipts_without_rewriting_existing_rows(self):
        # Only the durable job key is a dependency; do not synthesize the entire
        # secretary bootstrap just to exercise an installed policy upgrade.
        self.conn.execute('''CREATE TABLE work_jobs (id BIGINT PRIMARY KEY);
            CREATE TABLE effect_receipts (
                key TEXT PRIMARY KEY, message_id BIGINT NOT NULL, effect TEXT NOT NULL,
                payload_hash TEXT NOT NULL, owner_required BOOLEAN NOT NULL DEFAULT FALSE,
                status TEXT NOT NULL CHECK(status IN
                    ('authorized','attempted','accepted','verified','failed','uncertain','blocked')),
                job_id BIGINT, token TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
            INSERT INTO work_jobs(id) VALUES (9);
            INSERT INTO effect_receipts(key,message_id,effect,payload_hash,status)
                VALUES ('legacy',42,'notify','old-hash','uncertain')''')
        before = self.conn.execute('SELECT * FROM effect_receipts').fetchone()
        policy.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM effect_receipts').fetchone(), (*before, None))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone(), (0,))
        self.assertIsNone(self.conn.execute("SELECT to_regclass('telegram_messages')").fetchone()[0])
        self.assertEqual(self.conn.execute('''SELECT is_nullable FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='effect_receipts'
            AND column_name='message_id' ''').fetchone(), ('YES',))
        self.conn.execute("INSERT INTO data_policies(channel,effect,allowed,version) VALUES ('email','ai_text',TRUE,8)")
        self.conn.execute('''INSERT INTO system_notifications
            (token,system,scope,recipient,effect_key,job_id,payload,payload_hash,source_ids,media_source_ids)
            VALUES ('digest:fixture','digest','2026-10-16','7','digest-fixture',9,'{}','hash','{}','{}')''')
        for _ in range(2):
            policy.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM effect_receipts').fetchone(), (*before, None))
        self.assertEqual(self.conn.execute('SELECT channel,effect,allowed,version FROM data_policies').fetchall(),
                         [('email', 'ai_text', True, 8)])
        self.assertEqual(self.conn.execute('SELECT * FROM work_jobs').fetchall(), [(9,)])
        self.assertEqual(self.conn.execute('SELECT job_id FROM system_notifications').fetchall(), [(9,)])
        with self.assertRaises(db.psycopg.errors.ForeignKeyViolation), self.conn.transaction():
            self.conn.execute('UPDATE system_notifications SET job_id=10')
        self.assertEqual(self.conn.execute('SELECT count(*),min(reset_rows) FROM schema_migrations WHERE version=%s',
                                          (policy.VERSION,)).fetchone(), (1, 0))

    def test_policy_migration_preserves_explicit_consent_and_caller_rollback(self):
        self.conn.execute('''CREATE TABLE work_jobs (id BIGINT PRIMARY KEY);
            CREATE TABLE data_policies (
                channel TEXT NOT NULL, effect TEXT NOT NULL, allowed BOOLEAN NOT NULL DEFAULT FALSE,
                version BIGINT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(channel,effect));
            INSERT INTO data_policies(channel,effect,allowed,version)
                VALUES ('email','ai_text',TRUE,8),('telegram','ai_media',FALSE,12)''')
        before = self.conn.execute('SELECT * FROM data_policies ORDER BY channel,effect').fetchall()
        with self.assertRaisesRegex(RuntimeError, 'caller abort'), self.conn.transaction():
            policy.migrate(self.conn)
            self.assertEqual(self.conn.execute('SELECT * FROM data_policies ORDER BY channel,effect').fetchall(), before)
            raise RuntimeError('caller abort')
        self.assertIsNone(self.conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0])
        self.assertIsNone(self.conn.execute("SELECT to_regclass('effect_receipts')").fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT * FROM data_policies ORDER BY channel,effect').fetchall(), before)
        policy.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM data_policies ORDER BY channel,effect').fetchall(), before)

    def test_missing_or_unkeyed_job_dependency_rolls_back_every_policy_relation(self):
        with self.assertRaises(db.psycopg.errors.UndefinedTable):
            policy.migrate(self.conn)
        for table in ('schema_migrations', 'data_policies', 'effect_audit', 'effect_receipts',
                      'calendar_effect_grants', 'system_notifications'):
            self.assertIsNone(self.conn.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0], table)
        self.conn.execute('CREATE TABLE work_jobs (id BIGINT NOT NULL)')
        with self.assertRaises(db.psycopg.errors.InvalidForeignKey):
            policy.migrate(self.conn)
        self.assertIsNone(self.conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0])
        self.assertIsNone(self.conn.execute("SELECT to_regclass('effect_receipts')").fetchone()[0])
        self.conn.execute('ALTER TABLE work_jobs ADD PRIMARY KEY (id)')
        policy.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT version FROM schema_migrations').fetchall(), [(policy.VERSION,)])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone(), (0,))

    def test_legacy_bootstrap_never_backfills_owner_attestation_or_ai_consent(self):
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)
        mid = self.conn.execute('''INSERT INTO telegram_messages
            (channel,body,account,chat_id,author,direction,source_id,message_id,metadata)
            VALUES ('telegram','Report sent','bot:123','7','7','in',
                '["bot","123","7",42]','42',
                '{"account":"bot:123","chat_id":"7","author":"7","direction":"in",
                  "chat_type":"private","message_id":42,"owner":true,"trusted":true}') RETURNING id''').fetchone()[0]
        before = self.conn.execute('SELECT * FROM telegram_messages').fetchall()
        with patch.object(db, 'available', return_value=True), patch.object(
                db, '_conn', side_effect=lambda: nullcontext(self.conn)):
            db.init()
        self.assertEqual(self.conn.execute('SELECT * FROM telegram_messages').fetchall(), before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone(), (0,))
        self.assertTrue(policy.can_operate(self.conn, mid, 'task_propose'))
        self.assertFalse(policy.can_operate(self.conn, mid, 'owner_command'))
        self.assertFalse(policy.can_operate(self.conn, mid, 'ai_text'))


if __name__ == '__main__':
    unittest.main()