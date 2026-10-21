"""Opt-in integration in a disposable schema on a dedicated TEST database.

Never uses config.DATABASE_URL. Set PI_SECRETARY_TEST_DATABASE_URL explicitly;
the database name MUST end in _test. Only this test's UUID schema is removed.
"""
import os
import unittest
import uuid
from unittest.mock import patch

from secretary.core import jobs

URL = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL','')


def _database_name(url):
    """Accetta sia URI postgresql:// che keyword conninfo (host=/sock dbname=...)."""
    from psycopg.conninfo import conninfo_to_dict
    return conninfo_to_dict(url).get('dbname', '')


@unittest.skipUnless(URL, 'Dedicated PostgreSQL test URL not supplied')
class PostgresJobsTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        if not _database_name(URL).endswith('_test'):
            self.fail('Refusing database without _test suffix')
        self.schema = 'test_jobs_'+uuid.uuid4().hex
        self.conn = psycopg.connect(URL, autocommit=True, connect_timeout=5)
        self.conn.execute('CREATE SCHEMA '+self.schema)
        self.conn.execute('SET search_path TO '+self.schema)
        self.conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,reset_rows BIGINT NOT NULL)')
        self.conn.execute('CREATE TABLE appointments(id BIGSERIAL PRIMARY KEY)')
        self.conn.execute('CREATE TABLE invoices(id BIGSERIAL PRIMARY KEY)')
        jobs.migrate(self.conn)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.conn.execute('DROP SCHEMA '+self.schema+' CASCADE')
        self.conn.close()

    def test_atomic_enqueue_retry_and_idempotency(self):
        jobs.migrate(self.conn)
        with self.conn.transaction():
            jobs.enqueue(self.conn,'analysis','a',{'message_id':1})
            jobs.enqueue(self.conn,'analysis','a',{'message_id':1})
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0],1)
        with self.assertRaises(RuntimeError), self.conn.transaction():
            jobs.enqueue(self.conn,'analysis','rolled-back',{})
            raise RuntimeError('crash')
        from contextlib import nullcontext
        with patch('secretary.core.db._conn',side_effect=lambda:nullcontext(self.conn)):
            job = jobs.claim('analysis')
            self.assertIsNotNone(job)
            self.assertIsNone(jobs.claim('analysis'))
            jobs.finish(job,'TimeoutError')
            self.assertIsNone(jobs.claim('analysis'))
            self.conn.execute("UPDATE work_jobs SET available_at=now()-interval '1 second'")
            retried = jobs.claim('analysis')
            self.assertEqual(retried[2],2)
            jobs.finish(job)  # Stale token cannot finish the new attempt.
            self.assertEqual(self.conn.execute('SELECT status FROM work_jobs').fetchone()[0],'running')
            jobs.finish(retried)
        self.assertEqual(self.conn.execute('SELECT status FROM work_jobs').fetchone()[0],'done')

    def test_permanent_failure_is_terminal_with_reason_and_no_backoff(self):
        # A Permanent denial must land in terminal 'failed' immediately, with
        # an explicit failure_reason and no retry backoff: it can never succeed.
        jobs.migrate(self.conn)
        with self.conn.transaction():
            jobs.enqueue(self.conn,'action','zombie:1',{'type':'notify'})
        from contextlib import nullcontext
        with patch('secretary.core.db._conn',side_effect=lambda:nullcontext(self.conn)):
            job = jobs.claim('action')
            self.assertIsNotNone(job)
            jobs.finish(job, jobs.Permanent('Effect policy denied'), permanent=True)
        row = self.conn.execute('SELECT status,error,failure_reason,available_at,finished_at '
                                 'FROM work_jobs WHERE id=%s',(job[0],)).fetchone()
        self.assertEqual(row[0],'failed')
        self.assertEqual(row[1],'Permanent')
        self.assertIn('Effect policy denied',row[2])
        self.assertIsNotNone(row[4])
        # Terminal failure is never re-claimed, even after the backoff window.
        self.conn.execute("UPDATE work_jobs SET available_at=now()-interval '1 hour'")
        with patch('secretary.core.db._conn',side_effect=lambda:nullcontext(self.conn)):
            self.assertIsNone(jobs.claim('action'))

    def test_worker_permanent_exception_fails_job_not_defer(self):
        # Worker.run must route Permanent to finish(permanent=True): the job
        # ends 'failed' with a reason instead of looping on Deferred forever.
        jobs.migrate(self.conn)
        with self.conn.transaction():
            jobs.enqueue(self.conn,'action','loop:1',{'type':'notify'})
        from contextlib import nullcontext
        with patch('secretary.core.db._conn',side_effect=lambda:nullcontext(self.conn)):
            def deny(payload):
                worker.stop_event.set()  # One claim iteration only (set after the claim).
                raise jobs.Permanent('Effect policy denied')
            worker = jobs.Worker('action', deny)
            worker.run()
        row = self.conn.execute('SELECT status,error,failure_reason FROM work_jobs '
                                "WHERE key='loop:1'").fetchone()
        self.assertEqual(row[0],'failed')
        self.assertEqual(row[1],'Permanent')
        self.assertIn('Effect policy denied',row[2])