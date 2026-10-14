"""Opt-in integration in a disposable schema on a dedicated TEST database.

Never uses config.DATABASE_URL. Set PI_SECRETARY_TEST_DATABASE_URL explicitly;
the database name MUST end in _test. Only this test's UUID schema is removed.
"""
import os
import unittest
import uuid
from urllib.parse import urlsplit
from unittest.mock import patch

import jobs

URL = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL','')


@unittest.skipUnless(URL, 'Dedicated PostgreSQL test URL not supplied')
class PostgresJobsTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        if not urlsplit(URL).path.rstrip('/').endswith('_test'):
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
        with patch('db._conn',side_effect=lambda:nullcontext(self.conn)):
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