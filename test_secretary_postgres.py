"""Opt-in SQL contracts, isolated schemas, NEVER production/default DB URLs.

Only PI_SECRETARY_TEST_DATABASE_URL with an explicit dbname ending _test and no
libpq service is allowed. Caller provides an already running test server. No
server, shell, production workers or HTTP/model calls are started by these tests.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import threading
import unittest
from unittest.mock import patch
import uuid

import brain
import config
import db
import jobs
import message_store
import secretary as sec
from test_secretary import ANCHOR, TEXT, change, practice_fields, responsibility_fields


class SecretaryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
        if not cls.url:
            raise unittest.SkipTest('Set PI_SECRETARY_TEST_DATABASE_URL to opt in')
        if db.psycopg is None:
            raise unittest.SkipTest('psycopg is not installed')
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(cls.url)
        if not params.get('dbname', '').endswith('_test') or params.get('service'):
            raise ValueError('An explicit database ending in _test (no service) is required')

    def setUp(self):
        from psycopg import sql
        self.schema = 'secretary_test_' + uuid.uuid4().hex
        self.conn = db.psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=5)
        self.addCleanup(lambda: self.conn.close())
        self.assertTrue(self.conn.execute('SELECT current_database()').fetchone()[0].endswith('_test'))
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.configure(self.conn)
        self.main_thread = threading.get_ident()
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch.object(db, 'available', return_value=True))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:not-a-secret'))
        self.chat = self.enterContext(patch.object(brain, '_chat', side_effect=AssertionError('Unexpected LLM')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        db.init()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone()[0], 0)
        # Explicit fixture consent, not a bootstrap grant or a mocked policy gate.
        self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed)
            VALUES ('telegram','ai_text',TRUE),('telegram','ai_media',TRUE)''')

    def configure(self, conn):
        from psycopg import sql
        conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        conn.execute("SET timezone='UTC'")
        conn.execute("SET statement_timeout='15s'")
        conn.execute("SET lock_timeout='10s'")

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def fresh_connection(self):
        conn = db.psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=5)
        try:
            self.configure(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def connection(self):
        if threading.get_ident() == self.main_thread:
            yield self.conn
        else:
            with self.fresh_connection() as conn:
                yield conn

    def archive(self, text=TEXT, *, historical=False, **overrides):
        metadata = dict(account='bot:777', chat_id='101', author=101, direction='in',
                        chat_type='private', content_origin='direct_text', is_forwarded=False,
                        message_id=int(uuid.uuid4().hex[:8], 16) + 1)
        metadata.update(overrides)
        return message_store.store(self.conn, 'telegram', 'Display name is not authority', '', text,
            ts=ANCHOR.isoformat(), source_id=json.dumps(['bot', '777', '101', metadata['message_id']]),
            metadata=metadata, historical=historical)

    def plan(self, mid, changes, text=TEXT):
        with patch.object(brain, '_chat', return_value=json.dumps(dict(changes=changes, clarification=''))):
            return sec.prepare(text, mid, reference_time=ANCHOR)

    def create(self, fields=None):
        fields = fields or practice_fields()
        text = 'Occupati di organizzare la riparazione'
        if fields['next_review_at'] is not None:
            text += ' e riesamina al ' + fields['next_review_at']
        text += '.'
        mid = self.archive(text)
        plan = self.plan(mid, [change(fields=fields, evidence=text)], text)
        receipt = sec.apply(self.conn, plan, mid)
        identity = self.conn.execute('SELECT id FROM secretary_practices WHERE origin_message=%s', (mid,)).fetchone()[0]
        return identity, mid, plan, receipt

    def create_responsibility(self):
        text = 'Segui le manutenzioni di casa.'
        mid = self.archive(text)
        plan = self.plan(mid, [change(kind='responsibility', evidence=text)], text)
        sec.apply(self.conn, plan, mid)
        return self.conn.execute('SELECT id FROM secretary_responsibilities WHERE origin_message=%s', (mid,)).fetchone()[0]

    def propose(self, practice_id, version=1, **overrides):
        payload = dict(action='send_message', channel='email', recipient='tecnico@example.test',
                       text='Chiedo un preventivo', documents=[])
        arguments = dict(practice_id=practice_id, expected_version=version, payload=payload,
                         expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                         operation_key='secretary-approval:request:' + uuid.uuid4().hex)
        arguments.update(overrides)
        return sec.request_approval(self.conn, **arguments), arguments

    def resolve(self, identity, payload, **overrides):
        arguments = dict(decision='approve', expected_version=1,
                         operation_key='secretary-approval:resolve:' + uuid.uuid4().hex,
                         caller=sec.DashboardOwner('101', True, True))
        arguments.update(overrides)
        return sec.resolve_approval(self.conn, identity, payload, **arguments)

    def tables(self):
        return {table: self.conn.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()
                for table in ('secretary_practices', 'secretary_responsibilities', 'secretary_links',
                              'secretary_decisions', 'secretary_receipts', 'secretary_plans',
                              'secretary_approvals', 'secretary_wakeups', 'work_jobs')}

    def test_additive_migration_twice_receipt_and_sql_immutability(self):
        self.create()
        before = self.tables()
        migrations = self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        db.init()
        db.init()
        sec.migrate(self.conn)
        sec.migrate(self.conn)
        self.assertEqual(self.tables(), before)
        self.assertEqual(self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall(), migrations)
        self.assertEqual(self.conn.execute('SELECT count(*),min(reset_rows) FROM schema_migrations WHERE version=%s',
                                           (sec.VERSION,)).fetchone(), (1, 0))
        for table in ('secretary_decisions', 'secretary_receipts', 'secretary_plans', 'secretary_links'):
            for command in (f'DELETE FROM {table}', f'TRUNCATE {table}'):
                with self.subTest(command=command), self.assertRaises(db.psycopg.errors.RaiseException):
                    with self.conn.transaction():
                        self.conn.execute(command)
        self.assertEqual(self.tables(), before)

    def test_prepare_llm_outside_transaction_create_links_restart_replay(self):
        mid = self.archive()
        attachment = self.conn.execute('''INSERT INTO attachments(message_id,channel,filename)
            VALUES (%s,'telegram','preventivo.pdf') RETURNING id''', (mid,)).fetchone()[0]
        ingress_jobs = self.tables()['work_jobs']

        def model(*args, **kwargs):
            from psycopg.pq import TransactionStatus
            self.assertEqual(self.conn.info.transaction_status, TransactionStatus.IDLE)
            return json.dumps(dict(changes=[change()], clarification=''))

        with patch.object(brain, '_chat', side_effect=model):
            plan = sec.prepare(TEXT, mid)
        self.assertEqual(self.tables()['work_jobs'], ingress_jobs)
        receipts = sec.apply(self.conn, plan, mid)
        self.assertEqual(len(receipts), 1)
        links = self.conn.execute('SELECT telegram_message_id,document_id,evidence FROM secretary_links ORDER BY id').fetchall()
        self.assertEqual(links, [(mid, None, TEXT), (None, attachment, TEXT)])
        before = self.tables()
        self.assertEqual(before['work_jobs'][:len(ingress_jobs)], ingress_jobs)
        self.assertEqual(len(before['work_jobs']), len(ingress_jobs) + 1)
        self.assertEqual(self.conn.execute('SELECT kind,key,payload FROM work_jobs WHERE key=%s',
            ('secretary-review:1:v1:initial',)).fetchall(), [('secretary_review', 'secretary-review:1:v1:initial',
                dict(practice_id=1, expected_version=1, reason='initial', message_id=mid,
                     operation_key='secretary-review:1:v1:initial'))])
        self.conn.close()
        self.conn = self.fresh_connection()
        self.assertEqual(sec.apply(self.conn, json.loads(json.dumps(plan)), mid), receipts)
        self.assertEqual(self.tables(), before)
        self.chat.assert_not_called()

    def test_prepare_denies_missing_text_or_media_consent_before_llm(self):
        mid = self.archive()
        self.conn.execute("DELETE FROM data_policies WHERE channel='telegram' AND effect='ai_text'")
        with self.assertRaises(PermissionError):
            sec.prepare(TEXT, mid, reference_time=ANCHOR)
        self.conn.execute("INSERT INTO data_policies(channel,effect,allowed) VALUES ('telegram','ai_text',TRUE)")
        self.conn.execute('''INSERT INTO attachments(message_id,channel,filename)
            VALUES (%s,'telegram','preventivo.pdf')''', (mid,))
        self.conn.execute("DELETE FROM data_policies WHERE channel='telegram' AND effect='ai_media'")
        with self.assertRaises(PermissionError):
            sec.prepare(TEXT, mid, reference_time=ANCHOR)
        self.chat.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_plans').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_receipts').fetchone()[0], 0)

    def test_native_provenance_checks_run_again_before_any_write(self):
        for overrides in ({'historical': True}, {'author': 102}, {'account': 'account:777'}, {'chat_type': 'group'},
                          {'direction': 'out'}, {'content_origin': 'caption'}, {'is_forwarded': True},
                          {'forward_origin': {'type': 'hidden_user'}}, {'quote': {'text': TEXT}}):
            mid = self.archive(**overrides)
            with self.subTest(overrides=overrides), self.assertRaises(sec.Unauthorized):
                sec.prepare(TEXT, mid)
        mid = self.archive()
        plan = self.plan(mid, [change()])
        for metadata in ('{"historical":true}', '{"is_forwarded":true}', '{"content_origin":"transcript"}'):
            with self.conn.transaction():
                self.conn.execute('UPDATE telegram_messages SET metadata=metadata || %s::jsonb WHERE id=%s', (metadata, mid))
                before = self.tables()
                with self.assertRaises(sec.Unauthorized):
                    sec.apply(self.conn, plan, mid)
                self.assertEqual(self.tables(), before)
                self.conn.execute('''UPDATE telegram_messages SET metadata=metadata ||
                    '{"historical":false,"is_forwarded":false,"content_origin":"direct_text"}'::jsonb WHERE id=%s''', (mid,))
        self.assertEqual(sec.apply(self.conn, plan, mid)[0].split(':')[0], 'Pratica #1')

    def test_changed_retries_stale_batch_all_or_nothing_and_replay_after_edits(self):
        identity, mid, plan, original = self.create()
        altered = deepcopy(plan)
        altered['changes'][0]['fields']['objective'] = 'Changed'
        before = self.tables()
        with self.assertRaises(sec.Conflict):
            sec.apply(self.conn, altered, mid)
        self.assertEqual(self.tables(), before)
        altered_manifest = deepcopy(plan)
        altered_manifest['input_sources'][0]['requires_media'] = True
        with self.assertRaises(sec.Conflict):
            sec.apply(self.conn, altered_manifest, mid)
        self.assertEqual(self.tables(), before)
        text = 'Aggiorna la pratica.'
        mid2 = self.archive(text)
        update = change(operation='update', identity=identity, version=1,
                        fields={'next_action': 'Confrontare i preventivi'}, evidence=text)
        plan2 = self.plan(mid2, [update], text)
        mid3 = self.archive(text)
        stale = self.plan(mid3, [update], text)
        sec.apply(self.conn, plan2, mid2)
        before = self.tables()
        with self.assertRaises(sec.Conflict):
            sec.apply(self.conn, stale, mid3)
        self.assertEqual(self.tables(), before)
        self.assertEqual(sec.apply(self.conn, plan, mid), original)
        self.assertEqual(self.tables(), before)

    def test_outer_transaction_and_partial_sql_failure_roll_back_every_receipt(self):
        mid = self.archive()
        plan = self.plan(mid, [change(), change(fields=practice_fields(objective='Seconda riparazione'))])
        self.conn.execute('''CREATE FUNCTION secretary_test_fail() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.operation_key LIKE '%:1' THEN RAISE EXCEPTION 'receipt failure'; END IF; RETURN NEW; END $$''')
        self.conn.execute('''CREATE TRIGGER secretary_test_failure BEFORE INSERT ON secretary_receipts
            FOR EACH ROW EXECUTE FUNCTION secretary_test_fail()''')
        before = self.tables()
        with self.assertRaisesRegex(db.psycopg.errors.RaiseException, 'receipt failure'):
            sec.apply(self.conn, plan, mid)
        self.assertEqual(self.tables(), before)
        self.conn.execute('DROP TRIGGER secretary_test_failure ON secretary_receipts')
        self.chat.side_effect = AssertionError('Retry must reuse the original prepared plan')
        with self.assertRaisesRegex(RuntimeError, 'caller rollback'):
            with self.conn.transaction():
                self.assertEqual(len(sec.apply(self.conn, plan, mid)), 2)
                raise RuntimeError('caller rollback')
        self.assertEqual(self.tables(), before)
        receipts = sec.apply(self.conn, plan, mid)
        self.assertEqual(len(receipts), 2)
        committed = self.tables()
        self.assertEqual(sec.apply(self.conn, deepcopy(plan), mid), receipts)
        self.assertEqual(self.tables(), committed)
        saved = self.conn.execute('SELECT payload FROM secretary_plans WHERE message_id=%s', (mid,)).fetchone()[0]
        self.assertEqual(saved['input_sources'], plan['input_sources'])
        self.assertEqual(saved['input_versions'], plan['input_versions'])
        self.chat.assert_not_called()

    def test_concurrent_creates_and_cas_updates(self):
        mid = self.archive()
        plan = self.plan(mid, [change()])
        barrier = threading.Barrier(2)

        def apply_concurrently(batch, message):
            with self.fresh_connection() as conn:
                barrier.wait(timeout=5)
                try:
                    return sec.apply(conn, batch, message)
                except sec.Conflict:
                    return 'conflict'

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(apply_concurrently, plan, mid) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_practices').fetchone()[0], 1)
        identity = self.conn.execute('SELECT id FROM secretary_practices').fetchone()[0]
        text = 'Aggiorna la pratica.'
        messages = [self.archive(text) for _ in range(2)]
        plans = [self.plan(message, [change(operation='update', identity=identity, version=1,
                    fields={'next_action': 'Confrontare il preventivo'}, evidence=text)], text) for message in messages]
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(apply_concurrently, batch, message) for batch, message in zip(plans, messages)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(results.count('conflict'), 1)
        self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices').fetchone()[0], 2)

    def test_approval_payload_expiry_revocation_retries_and_caller_authorization(self):
        identity, _, _, _ = self.create()
        ingress_jobs = self.tables()['work_jobs']
        approval, args = self.propose(identity)
        self.assertEqual(sec.request_approval(self.conn, **args), approval)
        changed = dict(args, payload=dict(args['payload'], text='Testo diverso'))
        with self.assertRaises(sec.Conflict):
            sec.request_approval(self.conn, **changed)
        before = self.tables()
        for caller in (None, sec.DashboardOwner('102', True, True), sec.DashboardOwner('101', True, False)):
            with self.assertRaises(sec.Unauthorized):
                self.resolve(approval, args['payload'], caller=caller)
        with self.assertRaises(sec.Conflict):
            self.resolve(approval, dict(args['payload'], recipient='altro@example.test'))
        with self.assertRaises(sec.Conflict):
            self.resolve(approval, args['payload'], now=datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual(self.tables(), before)
        key = 'secretary-approval:resolve:stable'
        receipt = self.resolve(approval, args['payload'], operation_key=key)
        before = self.tables()
        self.assertEqual(self.resolve(approval, args['payload'], operation_key=key), receipt)
        self.assertEqual(self.tables(), before)
        self.resolve(approval, args['payload'], decision='revoke', expected_version=2)
        self.assertEqual(self.conn.execute('SELECT state,version FROM secretary_approvals').fetchone(), ('revoked', 3))
        with self.assertRaises(sec.Conflict):
            self.resolve(approval, args['payload'], expected_version=3)
        # Replaying an old receipt is not a new execution grant.
        self.assertEqual(self.resolve(approval, args['payload'], operation_key=key), receipt)
        self.assertEqual(self.tables()['work_jobs'], ingress_jobs)

    def test_responsibility_suspend_revokes_approval_and_blocks_dependent_changes(self):
        responsibility = self.create_responsibility()
        identity, _, _, _ = self.create(practice_fields(responsibility_id=responsibility))
        approval, args = self.propose(identity)
        self.resolve(approval, args['payload'])
        text = 'Sospendi la responsabilità.'
        mid = self.archive(text)
        plan = self.plan(mid, [change(kind='responsibility', operation='suspend', identity=responsibility,
                                     version=1, fields={}, evidence=text)], text)
        sec.apply(self.conn, plan, mid)
        self.assertEqual(self.conn.execute('SELECT state,version FROM secretary_approvals').fetchone(), ('revoked', 3))
        self.assertEqual(self.conn.execute('SELECT state,version FROM secretary_responsibilities').fetchone(), ('suspended', 2))
        with self.assertRaises(sec.Unauthorized):
            self.propose(identity)
        text = 'Aggiorna la pratica.'
        mid2 = self.archive(text)
        plan2 = self.plan(mid2, [change(operation='update', identity=identity, version=1,
                                      fields={'next_action': 'Preparare altra bozza'}, evidence=text)], text)
        with self.assertRaises(sec.Unauthorized):
            sec.apply(self.conn, plan2, mid2)
        # Revocation does not trap local work in an unclosable state.
        text = 'Annulla la pratica.'
        mid3 = self.archive(text)
        plan3 = self.plan(mid3, [change(operation='update', identity=identity, version=1,
                                      fields={'state': 'cancelled'}, evidence=text)], text)
        sec.apply(self.conn, plan3, mid3)

    def test_practice_update_invalidates_pending_payload_approval(self):
        identity, _, _, _ = self.create()
        approval, args = self.propose(identity)
        text = 'Aggiorna la pratica.'
        mid = self.archive(text)
        plan = self.plan(mid, [change(operation='update', identity=identity, version=1,
            fields={'next_action': 'Verificare nuovi documenti'}, evidence=text)], text)
        sec.apply(self.conn, plan, mid)
        with self.assertRaises(sec.Conflict):
            self.resolve(approval, args['payload'], expected_version=2)
        self.assertEqual(self.conn.execute('SELECT state FROM secretary_approvals').fetchone()[0], 'revoked')

    def test_responsibility_version_is_a_fenced_dependency_of_prepared_practice(self):
        responsibility = self.create_responsibility()
        identity, _, _, _ = self.create(practice_fields(responsibility_id=responsibility))
        text = 'Aggiorna la pratica.'
        mid = self.archive(text)
        plan = self.plan(mid, [change(operation='update', identity=identity, version=1,
            fields={'next_action': 'Preparare la bozza aggiornata'}, evidence=text)], text)
        self.assertEqual(plan['responsibility_versions'], [{'id': responsibility, 'expected_version': 1}])
        text2 = 'Aggiorna la responsabilità.'
        mid2 = self.archive(text2)
        plan2 = self.plan(mid2, [change(kind='responsibility', operation='update', identity=responsibility,
            version=1, fields={'objective': 'Seguire manutenzioni e preventivi'}, evidence=text2)], text2)
        sec.apply(self.conn, plan2, mid2)
        before = self.tables()
        with self.assertRaises(sec.Conflict):
            sec.apply(self.conn, plan, mid)
        self.assertEqual(self.tables(), before)

    def test_due_review_and_observe_are_bounded_atomic_internal_and_idempotent(self):
        due = ANCHOR - timedelta(hours=1)
        identity, mid, _, _ = self.create(practice_fields(next_review_at=due.isoformat()))
        before = self.conn.execute('SELECT state,version,next_review_at FROM secretary_practices').fetchall()
        reviews = sec.due_reviews(self.conn, ANCHOR)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['practice_id'], identity)
        self.assertEqual(sec.due_reviews(self.conn, ANCHOR), [])
        self.assertEqual(len(sec.observe(self.conn, mid)), 1)
        self.assertEqual(sec.observe(self.conn, mid), [])
        foreign = self.archive('Completata la riparazione.', author=102)
        self.assertEqual(sec.observe(self.conn, foreign), [])
        # An explicitly catalogued third-party link is data only, not owner authority.
        self.conn.execute('''INSERT INTO secretary_links(practice_id,telegram_message_id,evidence,origin_message)
            VALUES (%s,%s,'Dichiarazione da verificare',%s)''', (identity, foreign, mid))
        self.assertEqual(sec.observe(self.conn, foreign), [])  # coalesced same version
        self.assertEqual(self.conn.execute('SELECT state,version,next_review_at FROM secretary_practices').fetchall(), before)
        self.assertEqual(self.conn.execute('SELECT DISTINCT kind FROM work_jobs ORDER BY kind').fetchall(),
                         [('knowledge',), ('secretary_review',)])
        self.chat.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_receipts').fetchone()[0], 1)

    def test_review_limit_fairness_and_outbox_rollback(self):
        for _ in range(3):
            self.create(practice_fields(next_review_at=ANCHOR.isoformat()))
        with patch.object(sec, 'MAX_REVIEWS', 2):
            self.assertEqual(len(sec.due_reviews(self.conn, ANCHOR)), 2)
            self.assertEqual(len(sec.due_reviews(self.conn, ANCHOR)), 1)
            self.assertEqual(sec.due_reviews(self.conn, ANCHOR), [])
        self.create(practice_fields(next_review_at=ANCHOR.isoformat()))
        before = self.tables()
        with patch.object(jobs, 'enqueue', side_effect=RuntimeError('outbox unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'outbox unavailable'):
                sec.due_reviews(self.conn, ANCHOR)
        self.assertEqual(self.tables(), before)
        self.assertEqual(len(sec.due_reviews(self.conn, ANCHOR)), 1)

    def test_empty_plan_receipt_binding_and_bounded_dashboard(self):
        text = 'A che ora è la riunione?'
        mid = self.archive(text)
        plan = self.plan(mid, [], text)
        self.assertEqual(sec.apply(self.conn, plan, mid), [])
        self.assertEqual(sec.apply(self.conn, plan, mid), [])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_practices').fetchone()[0], 0)
        self.create()
        context = sec.context('riparazione', 1)
        self.assertEqual(len(context['practices']), 1)
        self.assertEqual(sec.context('%')['practices'], [])
        data = sec.snapshot()
        self.assertEqual(len(data['practices']), 1)
        self.assertEqual(len(data['plans']), 2)
        json.dumps(data)

    def test_links_enforce_native_referential_integrity(self):
        identity, mid, _, _ = self.create()
        for statement, params in (
            ('DELETE FROM telegram_messages WHERE id=%s', (mid,)),
            ('INSERT INTO secretary_links(practice_id,document_id,evidence,origin_message) VALUES (%s,999999,\'x\',%s)', (identity, mid)),
            ('INSERT INTO secretary_links(practice_id,evidence,origin_message) VALUES (%s,\'x\',%s)', (identity, mid))):
            with self.subTest(statement=statement), self.assertRaises(db.psycopg.IntegrityError):
                with self.conn.transaction():
                    self.conn.execute(statement, params)

    def test_observe_limit_does_not_starve_later_linked_practices(self):
        practices = [self.create() for _ in range(3)]
        foreign = self.archive('Il tecnico ha inviato un nuovo preventivo.', author=102)
        for identity, owner_mid, _, _ in practices:
            self.conn.execute('''INSERT INTO secretary_links(practice_id,telegram_message_id,evidence,origin_message)
                VALUES (%s,%s,'Preventivo da esaminare',%s)''', (identity, foreign, owner_mid))
        before = self.conn.execute('SELECT id,state,version FROM secretary_practices ORDER BY id').fetchall()
        with patch.object(sec, 'MAX_REVIEWS', 2):
            self.assertEqual(len(sec.observe(self.conn, foreign)), 2)
            self.assertEqual(len(sec.observe(self.conn, foreign)), 1)
            self.assertEqual(sec.observe(self.conn, foreign), [])
        self.assertEqual(self.conn.execute('SELECT id,state,version FROM secretary_practices ORDER BY id').fetchall(), before)
        with self.assertRaises(sec.Unauthorized):
            sec.prepare('Il tecnico ha inviato un nuovo preventivo.', foreign)

    def test_revocation_is_terminal_and_new_approval_cannot_be_created(self):
        identity = self.create_responsibility()
        text = 'Revoca la responsabilità.'
        mid = self.archive(text)
        plan = self.plan(mid, [change(kind='responsibility', operation='revoke', identity=identity,
                                     version=1, fields={}, evidence=text)], text)
        receipts = sec.apply(self.conn, plan, mid)
        self.assertEqual(sec.apply(self.conn, plan, mid), receipts)
        self.assertEqual(self.conn.execute('SELECT state,version FROM secretary_responsibilities').fetchone(), ('revoked', 2))
        text = 'Riattiva la responsabilità.'
        mid = self.archive(text)
        with self.assertRaises(sec.Conflict):
            self.plan(mid, [change(kind='responsibility', operation='update', identity=identity,
                                  version=2, fields={'state': 'active'}, evidence=text)], text)

    def test_approval_documents_exact_payload_and_expiration_cannot_be_rewound(self):
        identity, mid, _, _ = self.create()
        attachment = self.conn.execute('''INSERT INTO attachments(message_id,channel,filename)
            VALUES (%s,'telegram','evidenza.pdf') RETURNING id''', (mid,)).fetchone()[0]
        payload = dict(action='send_message', channel='email', recipient='tecnico@example.test',
                       text='Chiedo un preventivo', documents=[dict(id=attachment, sha256='a' * 64)])
        with self.assertRaises(sec.InvalidPlan):
            self.propose(identity, payload=payload)
        self.conn.execute('''INSERT INTO secretary_links(practice_id,document_id,evidence,origin_message)
            VALUES (%s,%s,'Documento owner',%s)''', (identity, attachment, mid))
        approval, args = self.propose(identity, payload=payload)
        wrong_hash = dict(payload, documents=[dict(id=attachment, sha256='b' * 64)])
        with self.assertRaises(sec.Conflict):
            self.resolve(approval, wrong_hash)
        # Simulate time passage in this isolated schema; caller-supplied 'now'
        # must not turn an actually expired proposal back into a valid approval.
        self.conn.execute("UPDATE secretary_approvals SET expires_at=now()-interval '1 hour' WHERE id=%s", (approval,))
        before = self.tables()
        with self.assertRaises(sec.Conflict):
            self.resolve(approval, args['payload'], now=ANCHOR - timedelta(days=3650))
        self.assertEqual(self.tables(), before)
        self.assertEqual(sec.snapshot()['approvals'][0]['effective_state'], 'expired')
        self.resolve(approval, args['payload'], decision='revoke')
        self.assertEqual(self.conn.execute('SELECT state FROM secretary_approvals WHERE id=%s', (approval,)).fetchone()[0], 'revoked')


if __name__ == '__main__':
    unittest.main()