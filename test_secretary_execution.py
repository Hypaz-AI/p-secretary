"""Offline F7 contracts; SQL tests opt in to an isolated *_test DB/schema only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import uuid

import brain
import config
import db
import execution as gateway
import jobs
import secretary as sec
from connectors import email
import test_secretary_postgres as contracts


ACCOUNT = dict(address='owner@example.test', smtp_host='smtp.example.test', smtp_port=587,
               auth='basic', password='offline-only', name='Not an identity', provider='custom')
PAYLOAD = dict(action='send_message', channel='email', recipient='tech@example.test',
               text='Chiedo un preventivo.', documents=[])


@contextmanager
def job_context(job_id=1, payload=None, token='offline-token', kind=gateway.KIND):
    previous = jobs.execution_context()
    context = jobs.ExecutionContext(job_id, token, kind, payload or {}, time.monotonic() + 60)
    jobs._local.execution = context
    try:
        yield context
    finally:
        jobs._local.execution = previous


class PostgreSQLFixture(unittest.TestCase):
    # Reuse the existing explicitly guarded DB fixture WITHOUT inheriting/rerunning
    # its unrelated tests. Connections are search_path-isolated and autocommit.
    setUpClass = classmethod(contracts.SecretaryPostgresTests.setUpClass.__func__)
    configure = contracts.SecretaryPostgresTests.configure
    drop_schema = contracts.SecretaryPostgresTests.drop_schema
    fresh_connection = contracts.SecretaryPostgresTests.fresh_connection
    connection = contracts.SecretaryPostgresTests.connection
    archive = contracts.SecretaryPostgresTests.archive
    plan = contracts.SecretaryPostgresTests.plan
    create = contracts.SecretaryPostgresTests.create
    create_responsibility = contracts.SecretaryPostgresTests.create_responsibility

    def setUp(self):
        from psycopg import sql
        self.schema = 'secretary_execution_test_' + uuid.uuid4().hex
        self.conn = db.psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=5)
        self.addCleanup(lambda: self.conn.close())
        self.assertTrue(self.conn.execute('SELECT current_database()').fetchone()[0].endswith('_test'))
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.configure(self.conn)
        self.main_thread = threading.get_ident()
        # Bootstrap the complete production schema, but never use its configured
        # URL. Every connection (including concurrent workers) stays in our
        # explicitly guarded test database and per-test search_path.
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch.object(db, 'available', return_value=True))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:offline'))
        self.chat = self.enterContext(patch.object(brain, '_chat', side_effect=AssertionError('Unexpected LLM')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        db.init()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone()[0], 0)
        # Explicit test consent is needed for prepare(), not for email dispatch.
        # It does not authorize media, another channel, or historical sources.
        self.conn.execute("INSERT INTO data_policies(channel,effect,allowed) VALUES ('telegram','ai_text',true)")


class EmailPureTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(config, 'EMAIL_ACCOUNTS', [deepcopy(ACCOUNT)]))
        self.conn = MagicMock()
        self.send = self.enterContext(patch.object(email, 'send', side_effect=AssertionError('No SMTP')))
        self.enterContext(patch.object(db, '_conn', side_effect=AssertionError('No DB')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('No HTTP')))

    def test_single_exact_recipient_rejects_lists_headers_and_display_names(self):
        for address in ('a@example.test,b@example.test', 'A <a@example.test>',
                        'a@example.test\r\nBcc: b@example.test', 'a@example.test b@example.test',
                        'a@example.test; b@example.test', ' a@example.test', 'a..b@example.test',
                        'a@localhost', 'gruppo:a@example.test;', 'a@éxample.test', None, True):
            with self.subTest(address=address), self.assertRaises(sec.InvalidPlan):
                gateway._payload(dict(PAYLOAD, recipient=address))
        self.assertEqual(gateway._address('a+b@example.test'), 'a+b@example.test')

    def test_no_account_fallback_even_with_one_and_no_duplicate_identity(self):
        for address in (None, '', 'missing@example.test', 'OWNER@example.test'):
            with self.assertRaises(sec.InvalidPlan):
                gateway.request_email(self.conn, practice_id=1, expected_version=1,
                    payload=PAYLOAD, account_address=address,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    operation_key='secretary-approval:test')
        with patch.object(config, 'EMAIL_ACCOUNTS', [ACCOUNT, dict(ACCOUNT)]):
            with self.assertRaises(sec.Conflict):
                gateway._account(ACCOUNT['address'])
        self.send.assert_not_called()

    def test_account_binding_ignores_order_label_and_secret_but_not_endpoint(self):
        _, original = gateway._account(ACCOUNT['address'])
        changed = dict(ACCOUNT, name='New label', password='rotated')
        with patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT, address='other@example.test'), changed]):
            self.assertEqual(gateway._account(ACCOUNT['address'])[1], original)
        with patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT, smtp_host='changed.example.test')]):
            self.assertNotEqual(gateway._account(ACCOUNT['address'])[1], original)

    def test_worker_and_finite_schema_required(self):
        for payload in ({}, dict(approval_id=True, dispatch_key='x'),
                        dict(approval_id=1, dispatch_key='x', text='do this')):
            with self.assertRaises(sec.InvalidPlan):
                gateway.execute_approved(payload)
        with self.assertRaises(sec.Unauthorized):
            gateway.execute_approved(dict(approval_id=1, dispatch_key='x'))
        with job_context(kind='action'), self.assertRaises(sec.Unauthorized):
            gateway.execute_approved(dict(approval_id=1, dispatch_key='x'))

    def test_dispatch_requires_real_server_owner_not_deserialized_flags(self):
        with patch.object(config, 'TELEGRAM_CHAT_ID', '101'):
            for caller in ({'owner_id': '101', 'authenticated': True, 'csrf_verified': True},
                           sec.DashboardOwner('101', True, False), sec.DashboardOwner('102', True, True)):
                with self.assertRaises(sec.Unauthorized):
                    gateway.dispatch_email(self.conn, approval_id=1, expected_version=2,
                        account_address=ACCOUNT['address'], payload=PAYLOAD,
                        approval_operation_key='secretary-approval:approve',
                        operation_key='secretary-email-dispatch:test', caller=caller)

    def test_guarded_documents_block_paths_symlinks_hardlinks_and_changed_bytes(self):
        with tempfile.TemporaryDirectory(prefix='f7-files-') as root:
            root = Path(root)
            uploads = root / 'uploads'
            uploads.mkdir()
            source = uploads / 'invoice.txt'
            source.write_bytes(b'approved bytes')
            link = uploads / 'linked.txt'
            link.symlink_to(source)
            hardlink = uploads / 'hard.txt'
            os.link(source, hardlink)
            digest = hashlib.sha256(b'approved bytes').hexdigest()
            with patch.object(config, 'DATA_DIR', root), patch.object(config, 'INVOICE_DIR', root / 'invoices'):
                for path in (link, hardlink, root / '.env', '/etc/passwd', uploads / '../outside'):
                    self.conn.execute.return_value.fetchone.return_value = (str(path), None, None)
                    with self.subTest(path=str(path)), self.assertRaises(sec.Conflict):
                        gateway._documents(self.conn, 1, [dict(id=1, sha256=digest)])
                hardlink.unlink()
                self.conn.execute.return_value.fetchone.return_value = (str(source), None, None)
                gateway._documents(self.conn, 1, [dict(id=1, sha256=digest)])
                source.write_bytes(b'changed')
                with self.assertRaises(sec.Conflict):
                    gateway._documents(self.conn, 1, [dict(id=1, sha256=digest)])

    def test_documents_reject_oversize_db_bytes_and_wrong_hash(self):
        document = dict(id=1, sha256='a' * 64)
        self.conn.execute.return_value.fetchone.return_value = (None, gateway.MAX_DOCUMENT_BYTES + 1, None)
        with self.assertRaises(sec.InvalidPlan):
            gateway._documents(self.conn, 1, [document])
        self.conn.execute.return_value.fetchone.return_value = (None, 3, b'abc')
        with self.assertRaises(sec.Conflict):
            gateway._documents(self.conn, 1, [document])
        self.send.assert_not_called()


class EmailWorkerOfflineTests(unittest.TestCase):
    """Real handler, mocked SQL/SMTP: no test database is necessary."""

    def setUp(self):
        self.conn = MagicMock()
        self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value = self.conn
        self.enterContext(patch.object(config, 'EMAIL_ACCOUNTS', [deepcopy(ACCOUNT)]))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('No HTTP')))
        self.fence = self.enterContext(patch.object(jobs, 'assert_current'))
        self.send = self.enterContext(patch.object(email, 'send', return_value=True))
        self.payload = dict(approval_id=1, dispatch_key='secretary-email-dispatch:offline')
        self.context = self.enterContext(job_context(payload=self.payload))
        self.approval = dict(payload=deepcopy(PAYLOAD), payload_hash=sec.payload_hash(PAYLOAD),
            state='approved', version=2, expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            practice_id=2, practice_version=1, responsibility_id=None)
        self.practice = dict(id=2, version=1, state='active', responsibility_id=None)
        self.enterContext(patch.object(sec, '_fetch', side_effect=lambda conn, table, identity, lock=False:
            deepcopy(self.approval if table == 'secretary_approvals' else self.practice)))
        self.enterContext(patch.object(gateway, '_proposal', return_value=dict(
            account_address=ACCOUNT['address'], account_hash=gateway._account(ACCOUNT['address'])[1],
            payload_hash=sec.payload_hash(PAYLOAD), subject=gateway.SUBJECT)))
        self.enterContext(patch.object(gateway, '_native'))
        self.enterContext(patch.object(gateway, '_documents'))
        self.validate = self.enterContext(patch.object(gateway, '_validate', wraps=gateway._validate))
        self.receipt = self.enterContext(patch.object(gateway, '_approval_receipt'))
        request = dict(approval_id=1, expected_version=2, account_address=ACCOUNT['address'],
            payload=PAYLOAD, subject=gateway.SUBJECT, approval_operation_key='secretary-approval:offline', owner_id='101')
        self.grant = (self.payload['dispatch_key'], 2, 'secretary-approval:offline', '101', sec.payload_hash(request))
        self.attempt = None
        self.persist_result = True
        self.trace = []
        self.commits = 0
        self.after_commit = None
        self.transaction_depth = 0
        def begin(*args):
            self.transaction_depth += 1
            self.saved_attempt = self.attempt
        def commit(exc_type, exc, traceback):
            self.transaction_depth -= 1
            if exc_type is not None:
                self.attempt = self.saved_attempt
                self.trace.append('rollback')
                return False
            self.commits += 1
            self.trace.append('commit')
            if self.after_commit:
                self.after_commit(self.commits)
        self.conn.transaction.return_value.__enter__.side_effect = begin
        self.conn.transaction.return_value.__exit__.side_effect = commit
        self.conn.execute.side_effect = self.execute

    def execute(self, sql, args=None):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        if 'FROM secretary_email_dispatches' in sql:
            cursor.fetchone.return_value = self.grant
        elif 'SELECT state FROM secretary_email_attempts' in sql:
            cursor.fetchone.return_value = (self.attempt,) if self.attempt else None
        elif 'INSERT INTO secretary_email_attempts' in sql:
            self.attempt = 'attempted'
            self.trace.append('attempted')
        elif 'UPDATE secretary_email_attempts' in sql and self.persist_result:
            self.attempt = args[0]
            cursor.fetchone.return_value = (self.attempt,)
        return cursor

    def test_commit_before_smtp_and_accepted_replay_sends_once(self):
        def send(*args):
            self.assertEqual(self.attempt, 'attempted')
            self.assertGreaterEqual(self.commits, 2)
            self.assertEqual(self.transaction_depth, 0)
            self.assertEqual(self.validate.call_count, 2)
            self.trace.append('smtp')
            return True
        self.send.side_effect = send
        result = gateway.execute_approved(self.payload)
        self.assertEqual(result['state'], 'accepted')
        self.assertFalse(result['delivered'])
        self.assertLess(self.trace.index('attempted'), self.trace.index('commit'))
        self.assertLess(self.trace.index('commit'), self.trace.index('smtp'))
        self.assertEqual(gateway.execute_approved(self.payload), result)
        self.send.assert_called_once()

    def test_revocation_after_attempt_commit_blocks_smtp_and_replay(self):
        def revoke(commits):
            if commits == 1:
                self.validate.side_effect = sec.Conflict('Revoked after reservation')
        self.after_commit = revoke
        with self.assertRaises(sec.Conflict):
            gateway.execute_approved(self.payload)
        self.send.assert_not_called()
        self.assertEqual(self.attempt, 'uncertain')
        self.assertEqual(gateway.execute_approved(self.payload)['state'], 'uncertain')

    def test_actual_approval_and_configuration_changes_after_attempt_commit_block_smtp(self):
        mutations = (
            ('revoked', lambda: self.approval.update(state='revoked')),
            ('version', lambda: self.approval.update(version=3)),
            ('payload', lambda: self.approval['payload'].update(text='Not approved')),
            ('payload_hash', lambda: self.approval.update(payload_hash='0' * 64)),
            ('expiry', lambda: self.approval.update(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))),
            ('practice', lambda: self.practice.update(version=2)),
            ('account', lambda: config.EMAIL_ACCOUNTS[0].update(smtp_host='changed.example.test')),
        )
        approval, practice = deepcopy(self.approval), deepcopy(self.practice)
        for name, mutate in mutations:
            with self.subTest(change=name):
                self.approval, self.practice = deepcopy(approval), deepcopy(practice)
                self.attempt, self.commits = None, 0
                with patch.object(config, 'EMAIL_ACCOUNTS', [deepcopy(ACCOUNT)]):
                    self.after_commit = lambda count: mutate() if count == 1 else None
                    with self.assertRaises(sec.Conflict):
                        gateway.execute_approved(self.payload)
                    self.assertEqual(self.attempt, 'uncertain')
                    self.assertEqual(gateway.execute_approved(self.payload)['state'], 'uncertain')
        self.send.assert_not_called()

    def test_shortened_current_expiry_rechecked_after_final_authorization_commit(self):
        now = datetime.now(timezone.utc)
        def advance(commits):
            if commits == 1:
                self.approval['expires_at'] = now + timedelta(seconds=1)
            elif commits == 2:
                clock.now.return_value = now + timedelta(seconds=2)
        self.after_commit = advance
        with patch.object(gateway, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            with self.assertRaises(sec.Conflict):
                gateway.execute_approved(self.payload)
        self.assertEqual(self.attempt, 'uncertain')
        self.assertEqual(gateway.execute_approved(self.payload)['state'], 'uncertain')
        self.send.assert_not_called()

    def test_refreshed_account_is_used_after_final_authorization(self):
        self.after_commit = lambda count: config.EMAIL_ACCOUNTS[0].update(
            password='offline-rotated') if count == 1 else None
        self.assertEqual(gateway.execute_approved(self.payload)['state'], 'accepted')
        self.assertEqual(self.send.call_args.args[0]['password'], 'offline-rotated')

    def test_lost_lease_after_commit_blocks_smtp(self):
        self.after_commit = lambda n: setattr(self.fence, 'side_effect', jobs.LeaseLost()) if n == 1 else None
        with self.assertRaises(jobs.LeaseLost):
            gateway.execute_approved(self.payload)
        self.send.assert_not_called()
        self.assertEqual(self.attempt, 'uncertain')

    def test_lost_lease_after_final_authorization_commit_blocks_smtp_and_replay(self):
        self.after_commit = lambda n: setattr(self.fence, 'side_effect', jobs.LeaseLost()) if n == 2 else None
        with self.assertRaises(jobs.LeaseLost):
            gateway.execute_approved(self.payload)
        self.send.assert_not_called()
        self.assertEqual(self.attempt, 'uncertain')
        self.fence.side_effect = None  # A later current worker may read, not resend.
        self.assertEqual(gateway.execute_approved(self.payload)['state'], 'uncertain')

    def test_missing_result_write_must_not_report_accepted_or_resend(self):
        self.persist_result = False
        with self.assertRaises(RuntimeError):
            gateway.execute_approved(self.payload)
        self.assertEqual(self.attempt, 'attempted')
        self.assertEqual(gateway.execute_approved(self.payload)['state'], 'uncertain')
        self.send.assert_called_once()

    def test_wrong_result_state_must_not_report_accepted_or_resend(self):
        def wrong_result(sql, args=None):
            cursor = self.execute(sql, args)
            if 'UPDATE secretary_email_attempts' in sql:
                cursor.fetchone.return_value = ('uncertain',)
            return cursor
        self.conn.execute.side_effect = wrong_result
        with self.assertRaises(RuntimeError):
            gateway.execute_approved(self.payload)
        self.assertEqual(self.attempt, 'attempted')  # Failed verification rolls back.
        self.assertEqual(gateway.execute_approved(self.payload)['state'], 'uncertain')
        self.send.assert_called_once()

    def test_connector_truthiness_is_not_acceptance(self):
        for outcome in (False, None, 1, 'ok', {'accepted': True}):
            with self.subTest(outcome=outcome):
                self.attempt = None
                self.send.return_value = outcome
                self.assertEqual(gateway.execute_approved(self.payload)['state'], 'uncertain')

    def test_timeout_is_uncertain_and_never_resent(self):
        self.send.side_effect = TimeoutError('private error')
        with self.assertRaises(TimeoutError):
            gateway.execute_approved(self.payload)
        self.assertEqual(gateway.execute_approved(self.payload)['state'], 'uncertain')
        self.send.assert_called_once()


class EmailPostgresTests(PostgreSQLFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(config, 'EMAIL_ACCOUNTS', [deepcopy(ACCOUNT)]))
        self.send = self.enterContext(patch.object(email, 'send', return_value=True))
        self.enterContext(patch('smtplib.SMTP', side_effect=AssertionError('No real SMTP')))
        self.owner = sec.DashboardOwner('101', True, True)

    def propose(self, payload=None, fields=None):
        practice_id, mid, _, _ = self.create(fields)
        arguments = dict(practice_id=practice_id, expected_version=1, payload=deepcopy(payload or PAYLOAD),
            account_address=ACCOUNT['address'], expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            operation_key='secretary-approval:email:' + uuid.uuid4().hex)
        snapshot = gateway.request_email(self.conn, **arguments)
        return snapshot['approval_id'], arguments, mid

    def approve(self, identity, arguments):
        key = 'secretary-approval:approve:' + uuid.uuid4().hex
        sec.resolve_approval(self.conn, identity, arguments['payload'], decision='approve',
            expected_version=1, operation_key=key, caller=self.owner)
        return key

    def dispatch(self, identity, arguments, approval_key):
        kwargs = dict(approval_id=identity, expected_version=2, account_address=arguments['account_address'],
            payload=arguments['payload'], approval_operation_key=approval_key,
            operation_key='secretary-email-dispatch:' + uuid.uuid4().hex, caller=self.owner)
        return gateway.dispatch_email(self.conn, **kwargs), kwargs

    def ready(self):
        identity, args, mid = self.propose()
        key = self.approve(identity, args)
        payload, kwargs = self.dispatch(identity, args, key)
        job = jobs.claim(gateway.KIND)
        return identity, args, mid, payload, kwargs, job

    def execute(self, payload, job):
        with job_context(job[0], payload, job[3]):
            return gateway.execute_approved(payload)

    def work_jobs(self):
        return self.conn.execute('SELECT * FROM work_jobs ORDER BY id').fetchall()

    def email_job_count(self):
        return self.conn.execute('SELECT count(*) FROM work_jobs WHERE kind=%s', (gateway.KIND,)).fetchone()[0]

    def test_migration_and_pending_approval_never_enqueue_send_or_ai(self):
        identity, arguments, _ = self.propose()
        # Native ingress/cataloguing and the practice's initial review are real;
        # inert proposal/approval/bootstrap replay must not change their jobs.
        pending = self.work_jobs()
        migrations = self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        db.init()
        gateway.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall(), migrations)
        self.assertEqual(gateway.request_email(self.conn, **arguments)['approval_id'], identity)
        self.assertEqual(self.work_jobs(), pending)
        self.assertEqual(self.email_job_count(), 0)
        self.assertEqual(self.conn.execute('SELECT count(*),min(reset_rows) FROM schema_migrations WHERE version=%s',
                                           (gateway.VERSION,)).fetchone(), (1, 0))
        self.approve(identity, arguments)
        self.assertEqual(self.work_jobs(), pending)
        self.assertEqual(self.email_job_count(), 0)
        self.send.assert_not_called()
        self.chat.assert_not_called()
        for table in ('secretary_email_proposals', 'secretary_email_dispatches'):
            with self.assertRaises(db.psycopg.errors.RaiseException):
                with self.conn.transaction():
                    self.conn.execute('TRUNCATE ' + table + ' CASCADE')

    def test_draft_preparation_requires_exact_text_consent_and_native_source(self):
        from test_secretary import TEXT, change
        mid = self.archive()
        self.conn.execute('DELETE FROM data_policies')
        for channel, effect, allowed in (('email', 'ai_text', True), ('telegram', 'ai_media', True),
                                         ('telegram', 'ai_text', False)):
            with self.subTest(channel=channel, effect=effect, allowed=allowed):
                self.conn.execute('INSERT INTO data_policies(channel,effect,allowed) VALUES (%s,%s,%s)',
                                  (channel, effect, allowed))
                with self.assertRaises(PermissionError):
                    sec.prepare(TEXT, mid, reference_time=contracts.ANCHOR)
                self.conn.execute('DELETE FROM data_policies')
        self.chat.assert_not_called()
        self.conn.execute("INSERT INTO data_policies(channel,effect,allowed) VALUES ('telegram','ai_text',true)")
        self.assertEqual(len(self.plan(mid, [change()])['changes']), 1)
        for metadata in ('{"historical":true}', '{"is_forwarded":true}', '{"author":102}'):
            with self.subTest(metadata=metadata):
                untrusted = self.archive()
                self.conn.execute('UPDATE telegram_messages SET metadata=metadata || %s::jsonb WHERE id=%s',
                                  (metadata, untrusted))
                with self.assertRaises(sec.Unauthorized):
                    sec.prepare(TEXT, untrusted, reference_time=contracts.ANCHOR)
        self.assertEqual(self.email_job_count(), 0)
        self.chat.assert_not_called()
        self.send.assert_not_called()

    def test_email_dispatch_neither_requires_nor_grants_ai_consent(self):
        identity, arguments, _ = self.propose()
        self.conn.execute('DELETE FROM data_policies')
        key = self.approve(identity, arguments)
        payload, _ = self.dispatch(identity, arguments, key)
        job = jobs.claim(gateway.KIND)
        self.assertEqual(self.execute(payload, job)['state'], 'accepted')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone()[0], 0)
        self.send.assert_called_once()
        self.chat.assert_not_called()

    def test_changed_sender_retry_and_outer_rollback_are_atomic(self):
        identity, arguments, _ = self.propose()
        pending = self.work_jobs()
        with patch.object(config, 'EMAIL_ACCOUNTS', [ACCOUNT, dict(ACCOUNT, address='other@example.test')]):
            with self.assertRaises(sec.Conflict):
                gateway.request_email(self.conn, **dict(arguments, account_address='other@example.test'))
        self.assertEqual(gateway.email_snapshot(self.conn, identity)['account_address'], ACCOUNT['address'])
        key = self.approve(identity, arguments)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.conn.transaction():
                self.dispatch(identity, arguments, key)
                raise RuntimeError('rollback')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_email_dispatches').fetchone()[0], 0)
        self.assertEqual(self.work_jobs(), pending)
        self.assertEqual(self.email_job_count(), 0)

    def test_dispatch_requires_exact_approval_receipt_and_payload(self):
        identity, arguments, _ = self.propose()
        with self.assertRaises(sec.Conflict):
            self.dispatch(identity, arguments, 'secretary-approval:missing')
        key = self.approve(identity, arguments)
        with self.assertRaises(sec.Unauthorized):
            self.dispatch(identity, arguments, 'secretary-approval:missing')
        for changed in (dict(arguments, payload=dict(PAYLOAD, text='changed')),
                        dict(arguments, payload=dict(PAYLOAD, recipient='other@example.test'))):
            with self.assertRaises(sec.Conflict):
                self.dispatch(identity, changed, key)
        self.send.assert_not_called()

    def test_accepted_means_not_delivered_and_retry_never_sends_again(self):
        identity, args, _, payload, kwargs, job = self.ready()
        pending = self.work_jobs()

        def accepted(account, recipient, subject, body):
            from psycopg.pq import TransactionStatus
            self.assertEqual(self.conn.info.transaction_status, TransactionStatus.IDLE)
            with self.fresh_connection() as check:
                self.assertEqual(check.execute('SELECT state FROM secretary_email_attempts WHERE approval_id=%s',
                                               (identity,)).fetchone()[0], 'attempted')
            self.assertEqual((account['address'], recipient, subject, body),
                             (ACCOUNT['address'], PAYLOAD['recipient'], gateway.SUBJECT, PAYLOAD['text']))
            return True

        self.send.side_effect = accepted
        result = self.execute(payload, job)
        self.assertEqual(result, dict(approval_id=identity, state='accepted', delivered=False, automatic_retry=False))
        self.assertEqual(self.execute(payload, job), result)
        self.assertEqual(gateway.dispatch_email(self.conn, **kwargs), payload)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.work_jobs(), pending)
        self.assertEqual(self.email_job_count(), 1)
        self.chat.assert_not_called()

    def test_false_and_timeout_are_uncertain_and_never_resent(self):
        for outcome in (False, TimeoutError('do not persist private exception')):
            _, _, _, payload, _, job = self.ready()
            if isinstance(outcome, Exception):
                self.send.side_effect = outcome
                with self.assertRaises(TimeoutError):
                    self.execute(payload, job)
                self.send.side_effect = None
            else:
                self.send.return_value = outcome
                self.assertEqual(self.execute(payload, job)['state'], 'uncertain')
            before = self.send.call_count
            self.assertEqual(self.execute(payload, job)['state'], 'uncertain')
            self.assertEqual(self.send.call_count, before)
            jobs.finish(job)

    def test_crashed_attempt_and_failed_result_persistence_cannot_resend(self):
        identity, _, _, payload, _, job = self.ready()
        with patch.object(gateway, '_finish', side_effect=OSError('offline DB')):
            with self.assertRaises(OSError):
                self.execute(payload, job)
        self.assertEqual(self.execute(payload, job)['state'], 'uncertain')
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.conn.execute('SELECT state FROM secretary_email_attempts WHERE approval_id=%s',
                                           (identity,)).fetchone()[0], 'attempted')

    def test_expiry_rechecked_after_reservation_before_smtp(self):
        _, _, _, payload, _, job = self.ready()
        original = jobs.assert_current
        call_count = 0

        def fence(conn=None):
            nonlocal call_count
            call_count += 1
            ordinal = call_count
            result = original(conn)
            if ordinal == 3:
                # Authorization and attempt commit succeeded; clock advances
                # before SMTP. The committed attempt still forbids any retry.
                clock.now.return_value = datetime.now(timezone.utc) + timedelta(days=1)
            return result

        with patch.object(gateway, 'datetime', wraps=datetime) as clock:
            clock.now.side_effect = None
            clock.now.return_value = datetime.now(timezone.utc)
            with patch.object(jobs, 'assert_current', side_effect=fence), self.assertRaises(sec.Conflict):
                self.execute(payload, job)
        self.assertEqual(self.execute(payload, job)['state'], 'uncertain')
        self.send.assert_not_called()

    def test_changed_versions_expiry_revocation_native_origin_and_account_deny(self):
        mutations = (
            "UPDATE secretary_approvals SET state='revoked' WHERE id=%s",
            "UPDATE secretary_approvals SET expires_at=now()-interval '1 second' WHERE id=%s",
            "UPDATE secretary_approvals SET version=version+1 WHERE id=%s",
            "UPDATE secretary_practices SET version=version+1 WHERE id=(SELECT practice_id FROM secretary_approvals WHERE id=%s)",
            "UPDATE secretary_practices SET state='completed' WHERE id=(SELECT practice_id FROM secretary_approvals WHERE id=%s)",
            "UPDATE telegram_messages SET metadata=metadata || '{\"historical\":true}'::jsonb WHERE id=(SELECT p.origin_message FROM secretary_practices p JOIN secretary_approvals a ON a.practice_id=p.id WHERE a.id=%s)",
        )
        for sql in mutations:
            identity, _, _, payload, _, job = self.ready()
            self.conn.execute(sql, (identity,))
            with self.subTest(sql=sql), self.assertRaises((sec.Conflict, sec.Unauthorized)):
                self.execute(payload, job)
            jobs.finish(job)
        _, _, _, payload, _, job = self.ready()
        for accounts in ([], [dict(ACCOUNT, smtp_host='other.example.test')]):
            with patch.object(config, 'EMAIL_ACCOUNTS', accounts), self.assertRaises(sec.Conflict):
                self.execute(payload, job)
        self.send.assert_not_called()

    def test_responsibility_version_suspension_revocation_fenced(self):
        from test_secretary import practice_fields
        for field, value in (('version', 2), ('state', 'suspended'), ('state', 'revoked')):
            rid = self.create_responsibility()
            identity, arguments, _ = self.propose(fields=practice_fields(responsibility_id=rid))
            key = self.approve(identity, arguments)
            payload, _ = self.dispatch(identity, arguments, key)
            job = jobs.claim(gateway.KIND)
            self.conn.execute('UPDATE secretary_responsibilities SET ' + field + '=%s WHERE id=%s', (value, rid))
            with self.assertRaises((sec.Conflict, sec.Unauthorized)):
                self.execute(payload, job)
            jobs.finish(job)
        self.send.assert_not_called()

    def test_current_lease_and_dispatch_grant_required(self):
        identity, _, _, payload, _, job = self.ready()
        self.conn.execute("UPDATE work_jobs SET token='superseded' WHERE id=%s", (job[0],))
        with self.assertRaises(jobs.LeaseLost):
            self.execute(payload, job)
        with job_context(job[0], dict(approval_id=identity + 1, dispatch_key='invented'), 'superseded'):
            with self.assertRaises(sec.Unauthorized):
                gateway.execute_approved(dict(approval_id=identity + 1, dispatch_key='invented'))
        self.send.assert_not_called()

    def test_documents_verified_but_connector_incapability_explicitly_blocks_dispatch(self):
        identity, arguments, mid = self.propose()
        pending = self.work_jobs()
        data = b'approved document'
        aid = self.conn.execute('''INSERT INTO attachments(message_id,channel,filename,data)
            VALUES (%s,'telegram','estimate.pdf',%s) RETURNING id''', (mid, data)).fetchone()[0]
        self.conn.execute('''INSERT INTO secretary_links(practice_id,document_id,evidence,origin_message)
            VALUES (%s,%s,'Documento',%s)''', (arguments['practice_id'], aid, mid))
        documents = [dict(id=aid, sha256=hashlib.sha256(data).hexdigest())]
        arguments = dict(arguments, payload=dict(PAYLOAD, documents=documents),
                         operation_key='secretary-approval:document:' + uuid.uuid4().hex)
        identity = gateway.request_email(self.conn, **arguments)['approval_id']
        key = self.approve(identity, arguments)
        with self.assertRaises(gateway.UnsupportedEmail):
            self.dispatch(identity, arguments, key)
        self.conn.execute('UPDATE attachments SET data=%s WHERE id=%s', (b'changed', aid))
        with self.assertRaises(sec.Conflict):
            self.dispatch(identity, arguments, key)
        self.assertEqual(self.work_jobs(), pending)
        self.assertEqual(self.email_job_count(), 0)
        self.send.assert_not_called()

    def test_concurrent_attempts_send_once(self):
        _, _, _, payload, _, job = self.ready()
        barrier = threading.Barrier(2)

        def execute():
            barrier.wait(timeout=5)
            return self.execute(payload, job)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(timeout=15) for f in [pool.submit(execute), pool.submit(execute)]]
        self.assertEqual(self.send.call_count, 1)
        self.assertTrue(all(row['state'] in ('accepted', 'uncertain') for row in results))


if __name__ == '__main__':
    unittest.main()