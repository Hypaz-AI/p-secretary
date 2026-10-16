"""Real reconciliation SQL against a disposable, Unix-socket-only PostgreSQL.

Like test_reconciliation_provenance.py, this starts its own temporary cluster
when local server binaries and psycopg are available; otherwise it skips.
Neither configured DATABASE_URL nor PI_SECRETARY_TEST_DATABASE_URL is used.
No application, worker, AI, external service, or existing database is started.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

import db
import jobs
import message_store
import task_reconciliation as reconciliation


class ReconciliationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psycopg
        except ImportError:
            raise unittest.SkipTest('psycopg is not installed')
        candidates = sorted(Path('/usr/lib/postgresql').glob('*/bin/initdb'))
        initdb = shutil.which('initdb') or (str(candidates[-1]) if candidates else None)
        if not initdb or os.geteuid() == 0:
            raise unittest.SkipTest('Local PostgreSQL binaries and non-root user required')
        pg_ctl = str(Path(initdb).with_name('pg_ctl'))
        if not Path(pg_ctl).is_file():
            raise unittest.SkipTest('Local pg_ctl is unavailable')

        # Ignore libpq service/host/options overrides, including PGHOSTADDR.
        cls.enterClassContext(patch.dict(os.environ,
            {k: v for k, v in os.environ.items() if not k.startswith('PG')}, clear=True))
        cls.enterClassContext(patch.object(db, '_conn',
            side_effect=AssertionError('Configured database access is forbidden')))
        directory = cls.enterClassContext(tempfile.TemporaryDirectory(prefix='reconcile-pg-'))
        data = str(Path(directory) / 'cluster')
        subprocess.run([initdb, '-D', data, '-A', 'trust', '--no-locale', '-E', 'UTF8'],
                       check=True, capture_output=True)
        # Register before starting so a partially successful start is cleaned up.
        def stop():
            if (Path(data) / 'postmaster.pid').exists():
                subprocess.run([pg_ctl, '-D', data, '-m', 'immediate', '-w', 'stop'],
                               check=True, capture_output=True)
        cls.addClassCleanup(stop)
        subprocess.run([pg_ctl, '-D', data, '-l', directory + '/postgres.log',
                        '-o', f"-F -c listen_addresses='' -c unix_socket_permissions=0700 -k {directory}",
                        '-w', 'start'], check=True, capture_output=True)
        cls.conn = psycopg.connect(host=directory, dbname='postgres',
                                  autocommit=True, connect_timeout=5)
        cls.addClassCleanup(cls.conn.close)
        cls.conn.execute('CREATE DATABASE reconciliation_test')
        test_conn = psycopg.connect(host=directory, dbname='reconciliation_test',
                                   autocommit=True, connect_timeout=5)
        cls.addClassCleanup(test_conn.close)
        cls.conn = test_conn
        if cls.conn.execute('SHOW listen_addresses').fetchone()[0] != '':
            raise AssertionError('Test PostgreSQL must not listen on TCP')

    def setUp(self):
        from psycopg import sql
        self.schema = 'test_reconciliation_' + uuid.uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='5s'")
        self.conn.execute("SET lock_timeout='2s'")
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def task(self, **fields):
        title = fields.pop('title', 'Send report')
        deadline = fields.pop('deadline', datetime(2030, 1, 2, tzinfo=timezone.utc))
        self.assertFalse(fields, 'Unknown task fixture fields')
        return self.conn.execute('INSERT INTO tasks(title,deadline) VALUES (%s,%s) RETURNING id',
                                 (title, deadline)).fetchone()[0]

    def message(self, table='telegram_messages', *, metadata=None, ts=None, acquired=None):
        from psycopg import sql
        self.assertIn(table, message_store.TABLES.values())
        acquired = acquired or datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        return self.conn.execute(sql.SQL('''INSERT INTO {}(channel,body,metadata,ts,acquired_at)
            VALUES (%s,%s,%s,%s,%s) RETURNING id''').format(sql.Identifier(table)),
            (table.split('_')[0], 'Report sent', json.dumps(metadata or {}), ts, acquired)).fetchone()[0]

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
            (reconciliation.calendar_key(task, 0),)).fetchone()[0]
        jobs.enqueue(self.conn, 'action', f'task-reminder:{task}',
                     {'type': 'reminder', 'id': task, 'deadline': '2030-01-02T00:00:00+00:00'})
        self.conn.execute("""UPDATE work_jobs SET status='running',token='old-lease',
            lease_until=now()+interval '1 hour' WHERE key=%s""", (f'task-reminder:{task}',))
        return appointment

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
            reconciliation.migrate(self.conn)
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
        self.assertEqual(self.conn.execute('SELECT count(*) FROM task_reconciliation_runs').fetchone()[0], 0)

    def test_queue_history_guard_and_live_revision_idempotency(self):
        reconciliation.migrate(self.conn)
        for table in message_store.TABLES.values():
            with self.subTest(table=table):
                history = self.message(table, metadata={'historical': True})
                live = self.message(table)
                with self.conn.transaction():
                    self.assertIsNone(reconciliation.queue(self.conn, history))
                    self.assertIsNone(reconciliation.queue(self.conn, history, 'edit'))
                    self.assertIsNotNone(reconciliation.queue(self.conn, live))
                    self.assertIsNone(reconciliation.queue(self.conn, live))
                    self.assertIsNotNone(reconciliation.queue(self.conn, live, 'edit'))
                rows = self.conn.execute('''SELECT key,payload,status FROM work_jobs
                    WHERE payload->>'message_id'=%s ORDER BY key''', (str(live),)).fetchall()
                self.assertEqual({row[0] for row in rows},
                                 {reconciliation._key(live), reconciliation._key(live, 'edit')})
                self.assertEqual({row[1]['revision'] for row in rows}, {'', 'edit'})
                self.assertTrue(all(row[2] == 'pending' for row in rows))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 6)

    def test_complete_versions_audit_and_cleans_derived_work_atomically(self):
        reconciliation.migrate(self.conn)
        task, mid = self.task(), self.message()
        appointment = self.derived_work(task)
        competing = reconciliation._record(self.conn, self.proposal(task, 'competing'))
        data = self.proposal(task, 'complete', message_id=mid)
        self.assertEqual(reconciliation.apply_change(self.conn, data), 'applied')
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
        self.assertEqual(reconciliation.apply_change(self.conn, audit[0]), 'applied')
        self.assertEqual(reconciliation.apply_change(self.conn, data), 'duplicate')
        self.assertEqual(self.snapshot(), before)

    def test_stale_evidence_revision_preserves_task_and_derived_work(self):
        reconciliation.migrate(self.conn)
        task = self.task()
        self.derived_work(task)
        mid = self.message(metadata={'revision_date': '2026-10-16T10:00:00+00:00'})
        proposal = reconciliation._record(self.conn, self.proposal(task, 'old-edit', message_id=mid,
            evidence_revision='2026-10-16T09:00:00+00:00'))
        before = self.snapshot()
        self.assertEqual(reconciliation.apply_change(self.conn, proposal), 'stale')
        after = self.snapshot()
        for table in ('tasks', 'appointments', 'work_jobs'):
            self.assertEqual(after[table], before[table])
        self.assertEqual(self.conn.execute('SELECT status FROM task_changes WHERE id=%s',
                                          (proposal,)).fetchone()[0], 'stale')

    def test_matching_evidence_revision_compares_timestamp_not_offset_text(self):
        reconciliation.migrate(self.conn)
        task = self.task()
        mid = self.message(metadata={'revision_date': '2026-10-16T12:00:00+02:00'})
        self.assertEqual(reconciliation.apply_change(self.conn,
            self.proposal(task, 'same-edit', message_id=mid,
                          evidence_revision='2026-10-16T10:00:00+00:00')), 'applied')

    def test_older_evidence_chronology_is_stale_even_with_current_task_version(self):
        reconciliation.migrate(self.conn)
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
                self.assertEqual(reconciliation.apply_change(self.conn, self.proposal(
                    task, name + ':new', operation='update', changes={'title': 'New title'},
                    message_id=newer)), 'applied')
                before = self.conn.execute('SELECT * FROM tasks WHERE id=%s', (task,)).fetchone()
                self.assertEqual(reconciliation.apply_change(self.conn, self.proposal(
                    task, name + ':old', expected_version=1, message_id=older)), 'stale')
                self.assertEqual(self.conn.execute('SELECT * FROM tasks WHERE id=%s',
                                                  (task,)).fetchone(), before)
                self.assertEqual(self.conn.execute('SELECT status FROM task_changes WHERE evidence_key=%s',
                                                  (name + ':old',)).fetchone()[0], 'stale')

    def test_newer_evidence_can_apply_after_previous_update(self):
        reconciliation.migrate(self.conn)
        task = self.task()
        older = self.message(ts=datetime(2026, 10, 15, tzinfo=timezone.utc))
        newer = self.message(ts=datetime(2026, 10, 16, tzinfo=timezone.utc))
        self.assertEqual(reconciliation.apply_change(self.conn, self.proposal(task, 'first',
            operation='update', changes={'title': 'Updated'}, message_id=older)), 'applied')
        self.assertEqual(reconciliation.apply_change(self.conn, self.proposal(task, 'second',
            expected_version=1, message_id=newer)), 'applied')
        self.assertEqual(self.conn.execute('SELECT status,version,last_evidence_id FROM tasks').fetchone(),
                         ('done', 2, newer))

    def test_stale_task_version_does_not_apply(self):
        reconciliation.migrate(self.conn)
        task = self.task()
        self.conn.execute('UPDATE tasks SET version=3 WHERE id=%s', (task,))
        self.assertEqual(reconciliation.apply_change(self.conn, self.proposal(task, 'old-version')), 'stale')
        self.assertEqual(self.conn.execute('SELECT status,version,completed_at FROM tasks').fetchone(),
                         ('open', 3, None))
        self.assertEqual(self.conn.execute('SELECT status FROM task_changes').fetchone()[0], 'stale')

    def test_database_outbox_failure_rolls_back_task_audit_and_calendar_then_retry_works(self):
        import psycopg
        reconciliation.migrate(self.conn)
        task = self.task()
        self.derived_work(task)
        reconciliation._record(self.conn, self.proposal(task, 'competing'))
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
            reconciliation.apply_change(self.conn, data)
        self.assertEqual(self.snapshot(), before)
        self.conn.execute('DROP TRIGGER fail_calendar_outbox ON work_jobs')
        self.assertEqual(reconciliation.apply_change(self.conn, data), 'applied')

    def test_caller_transaction_rollback_includes_successful_nested_apply_and_queue(self):
        reconciliation.migrate(self.conn)
        task, mid = self.task(), self.message()
        self.derived_work(task)
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'caller abort'), self.conn.transaction():
            self.assertIsNotNone(reconciliation.queue(self.conn, mid))
            self.assertEqual(reconciliation.apply_change(self.conn,
                self.proposal(task, 'caller-rollback', message_id=mid)), 'applied')
            raise RuntimeError('caller abort')
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()