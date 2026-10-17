"""Owner-plan provenance: isolated PostgreSQL and offline strict-schema tests.

Opt in with PI_SECRETARY_TEST_DATABASE_URL, an explicit isolated *_test database.
No TestCase imports/inheritance. Providers, HTTP and Python sockets are forbidden
except explicit synthetic provider captures and the libpq Unix test socket.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

import brain
import capabilities
import config
import db
import jobs
import knowledge
import message_store
import owner_dialogue as dialogue
import policy
import secretary
import secretary_runtime as runtime
import test_history_context_policy as policy_helpers
import test_secretary as pure_helpers


CANARY = 'PRIVATE_OWNER_PLAN_CANARY_9ca3'
REAL_CHAT = brain._chat


class OwnerPlanSchemaTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(db, '_conn', side_effect=AssertionError('Unexpected DB')))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))

    def plan(self, version=2):
        result = dict(schema_version=version, message_id=42,
            text_hash=secretary.payload_hash(pure_helpers.TEXT),
            reference_time=pure_helpers.ANCHOR.isoformat(), changes=[], clarification='',
            responsibility_versions=[])
        if version == 2:
            result.update(input_sources=[dict(source_type='message', source_id=42,
                requires_media=False, owner_required=True)], input_versions=[])
        return result

    def test_v1_remains_exact_and_v2_requires_native_manifest(self):
        secretary._plan_schema(self.plan(1))
        secretary._plan_schema(self.plan())
        for bad in (dict(self.plan(1), input_sources=[]), dict(self.plan(1), schema_version=True),
                    dict(self.plan(), schema_version=3), dict(self.plan(), input_sources=[]),
                    dict(self.plan(), input_versions={})):
            with self.subTest(plan=bad), self.assertRaises(secretary.InvalidPlan):
                secretary._plan_schema(bad)

    def test_v2_malformed_manifests_and_refs_never_reach_sql(self):
        cases = []
        for field, values in (
                ('source_type', ('practice', [], None)),
                ('source_id', (True, '42', -1, 2 ** 63)),
                ('requires_media', (None, 1, 'true')),
                ('owner_required', (False, None, 1))):
            for value in values:
                plan = self.plan()
                plan['input_sources'][0][field] = value
                cases.append(plan)
        cases.extend((dict(self.plan(), input_sources=[None]),
                      dict(self.plan(), input_sources=self.plan()['input_sources'] * 2)))
        for ref in ({}, dict(source_type='task', source_id=1, version=1),
                    dict(source_type='practice', source_id=True, version=1),
                    dict(source_type='responsibility', source_id=1, version=False)):
            cases.append(dict(self.plan(), input_versions=[ref]))
        for plan in cases:
            conn = MagicMock()
            with self.subTest(plan=plan), self.assertRaises(secretary.InvalidPlan):
                secretary.apply(conn, plan, 42)
            conn.execute.assert_not_called()

    def test_prepare_manifest_is_not_accepted_from_model(self):
        source = self.plan()['input_sources']
        with patch.object(secretary, '_read_prepare', return_value=(pure_helpers.ANCHOR,
                dict(practices=[], responsibilities=[]))), \
                patch.object(dialogue, 'filtered_practices', side_effect=deepcopy), \
                patch.object(dialogue, 'filter_history', return_value=[]), \
                patch.object(dialogue, 'resolve_source_manifest', return_value=source), \
                patch.object(dialogue, 'assert_egress_sources'), \
                patch.object(brain, '_chat', return_value=json.dumps(dict(changes=[],
                    clarification='', input_sources=source))):
            with self.assertRaises(secretary.InvalidPlan):
                secretary.prepare(pure_helpers.TEXT, 42)

    def test_applied_manifest_requires_matching_immutable_plan_and_receipt(self):
        plan = self.plan()
        change = dict(pure_helpers.change(evidence=pure_helpers.TEXT), operation_key='secretary:42:0')
        plan['changes'] = [change]
        result = dict(kind='practice', id=7, version=1, origin_message=42, operation_index=0)
        row = (42, secretary.payload_hash(plan), plan, change['operation_key'],
               secretary.payload_hash(change), result)
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [row]
        self.assertIsNotNone(secretary.applied_input_sources(conn, 'practice', 7, 1, 'secretary:42:0'))
        variants = [[], [tuple(None if i == 2 else value for i, value in enumerate(row))],
            [tuple('wrong' if i == 1 else value for i, value in enumerate(row))],
            [tuple('wrong' if i == 4 else value for i, value in enumerate(row))],
            [(*row[:-1], dict(result, operation_index=True))],
            [(*row[:-1], dict(result, version=2))]]
        for rows in variants:
            conn.execute.return_value.fetchall.return_value = rows
            with self.subTest(rows=rows):
                self.assertIsNone(secretary.applied_input_sources(conn, 'practice', 7, 1, 'secretary:42:0'))

    def test_receipt_identity_types_and_stable_operation_binding_fail_closed(self):
        plan = self.plan()
        change = dict(pure_helpers.change(evidence=pure_helpers.TEXT), operation_key='secretary:42:0')
        plan['changes'] = [change]
        result = dict(kind='practice', id=1, version=1, origin_message=42, operation_index=0)
        cases = []
        for field, value in (('id', True), ('version', True)):
            cases.append((deepcopy(plan), dict(result, **{field: value}), change['operation_key']))
        for fields, key in ((dict(expected_version=False), change['operation_key']),
                            (dict(operation_key='secretary:42:1'), 'secretary:42:1')):
            bad = deepcopy(plan)
            bad['changes'][0].update(fields)
            cases.append((bad, dict(result), key))
        for bad, bound_result, key in cases:
            conn = MagicMock()
            conn.execute.return_value.fetchall.return_value = [(42, secretary.payload_hash(bad), bad,
                key, secretary.payload_hash(bad['changes'][0]), bound_result)]
            with self.subTest(plan=bad, result=bound_result):
                self.assertIsNone(secretary.applied_input_sources(conn, 'practice', 1, 1, key))

    def test_partial_update_keeps_prior_receipt_sources_and_v1_cannot_be_laundered(self):
        original = self.plan()
        original['changes'] = [dict(pure_helpers.change(), operation_key='secretary:42:0')]
        updated = self.plan()
        updated.update(message_id=43, input_sources=[dict(source_type='message', source_id=43,
            requires_media=False, owner_required=True)])
        updated['changes'] = [dict(pure_helpers.change(operation='update', identity=7, version=1,
            fields=dict(next_action='Updated'), evidence='Aggiorna la pratica.'),
            operation_key='secretary:43:0')]

        def row(plan, version):
            change = plan['changes'][0]
            return (plan['message_id'], secretary.payload_hash(plan), plan, change['operation_key'],
                secretary.payload_hash(change), dict(kind='practice', id=7, version=version,
                    origin_message=plan['message_id'], operation_index=0))

        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [row(updated, 2), row(original, 1)]
        sources = secretary.applied_input_sources(conn, 'practice', 7, 2, 'secretary:42:0')
        self.assertEqual({s['source_id'] for s in sources}, {42, 43})
        legacy = {k: v for k, v in original.items() if k not in ('input_sources', 'input_versions')}
        legacy['schema_version'] = 1
        conn.execute.return_value.fetchall.return_value = [row(updated, 2), row(legacy, 1)]
        self.assertIsNone(secretary.applied_input_sources(conn, 'practice', 7, 2, 'secretary:42:0'))

    def test_history_filter_ignores_malformed_entries_without_borrowing_ids(self):
        conn = MagicMock()
        history = [None, 'untracked', dict(role='assistant', content=CANARY, message_id=42),
            dict(role='user', content='Authorized', message_id=42)]
        with patch.object(policy, 'can_operate', return_value=True), \
                patch.object(dialogue, 'allows_source', return_value=True):
            self.assertEqual(dialogue.filter_history(history, conn=conn), [history[-1]])
            self.assertEqual(dialogue.filter_history(dict(message_id=42), conn=conn), [])


class OwnerPlanReadEgressTests(unittest.TestCase):
    """Real native read gates and provider serialization; no PostgreSQL needed."""
    message = policy_helpers.HistoryContextPolicyTests.message
    consent = policy_helpers.HistoryContextPolicyTests.consent

    def setUp(self):
        policy_helpers.HistoryContextPolicyTests.setUp(self)
        self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value = self.conn
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.provider = self.enterContext(patch.object(brain.ai_budget, 'request_content',
            return_value='{"changes":[],"clarification":""}'))
        self.current = self.message('telegram', mid=101)
        self.origin = self.message('telegram', mid=43)
        self.email = self.message('email', mid=42)
        self.consent('telegram', 'ai_text')
        self.consent('email', 'ai_text')
        item = dict(pure_helpers.change(), operation_key='secretary:43:0')
        self.plan = dict(schema_version=2, message_id=self.origin,
            text_hash=secretary.payload_hash(pure_helpers.TEXT), reference_time=pure_helpers.ANCHOR.isoformat(),
            changes=[item], clarification='', responsibility_versions=[], input_versions=[], input_sources=[
                dict(source_type='message', source_id=self.origin, owner_required=True, requires_media=False),
                dict(source_type='message', source_id=self.email, owner_required=False, requires_media=False)])
        self.practice = dict(pure_helpers.practice_fields(objective=CANARY), id=7, version=1,
                             origin_message=self.origin, operation_key=item['operation_key'])
        self.snapshot = dict(practices=[self.practice], responsibilities=[])

    def execute(self, sql, params=None):
        if sql.startswith('SELECT origin_message,version,operation_key,responsibility_id'):
            return SimpleNamespace(fetchone=lambda: (self.origin, self.practice['version'], 'secretary:43:0', None))
        if 'FROM secretary_receipts r LEFT JOIN secretary_plans p' in sql:
            self.assertEqual(params, ('practice', '7'))
            item = self.plan['changes'][0]
            row = (self.origin, secretary.payload_hash(self.plan), self.plan, 'secretary:43:0',
                secretary.payload_hash(item), dict(kind='practice', id=7, version=1,
                    origin_message=self.origin, operation_index=0))
            return SimpleNamespace(fetchall=lambda: [row])
        if sql.startswith("SELECT to_regclass('secretary_runtime_reviews')"):
            return SimpleNamespace(fetchone=lambda: ('secretary_runtime_reviews',))
        if 'FROM secretary_runtime_reviews' in sql:
            return SimpleNamespace(fetchall=lambda: [])
        if sql.startswith('SELECT 1 FROM attachments'):
            return SimpleNamespace(fetchone=lambda: None)
        return policy_helpers.HistoryContextPolicyTests.execute(self, sql, params)

    def prepare(self):
        with patch.object(secretary, '_read_prepare', return_value=(pure_helpers.ANCHOR, self.snapshot)):
            return secretary.prepare(pure_helpers.TEXT, self.current)

    def test_all_native_dependencies_rechecked_at_real_prepare_provider_boundary(self):
        plan = self.prepare()
        self.assertIn(CANARY, json.dumps(self.provider.call_args.args[0]))
        self.assertEqual({s['source_id'] for s in plan['input_sources']}, {self.current, self.origin, self.email})
        self.consent('email', 'ai_text', allowed=False)
        self.prepare()
        self.assertNotIn(CANARY, json.dumps(self.provider.call_args.args[0]))
        with self.assertRaises(PermissionError):
            dialogue.assert_egress_sources(plan['input_sources'], conn=self.conn)
        self.consent('email', 'ai_text')
        self.assertTrue(dialogue.allows_source(self.conn, dict(source_type='practice', source_id=7)))
        self.message('telegram', mid=self.origin, historical=True)
        self.consent('telegram', 'ai_history')
        self.assertFalse(dialogue.allows_source(self.conn, dict(source_type='practice', source_id=7)))
        self.prepare()
        self.assertNotIn(CANARY, json.dumps(self.provider.call_args.args[0]))

    def test_historical_email_revocation_rechecks_an_already_flattened_manifest(self):
        self.message('email', mid=self.email, historical=True)
        self.consent('email', 'ai_history')
        plan = self.prepare()
        self.assertIn(CANARY, json.dumps(self.provider.call_args.args[0]))
        self.consent('email', 'ai_history', allowed=False)
        self.prepare()
        self.assertNotIn(CANARY, json.dumps(self.provider.call_args.args[0]))
        with self.assertRaises(PermissionError):
            dialogue.assert_egress_sources(plan['input_sources'], conn=self.conn)
        self.assertFalse(policy.can_operate(self.conn, self.email, 'ai_text'))

    def test_v1_applied_context_never_reaches_provider_despite_current_native_grants(self):
        self.plan['schema_version'] = 1
        self.plan.pop('input_sources')
        self.plan.pop('input_versions')
        self.assertFalse(dialogue.allows_source(self.conn, dict(source_type='practice', source_id=7)))
        plan = self.prepare()
        self.assertNotIn(CANARY, json.dumps(self.provider.call_args.args[0]))
        self.assertEqual(plan['input_versions'], [])
        self.assertEqual({s['source_id'] for s in plan['input_sources']}, {self.current})


class OwnerPlanPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
        if not cls.url or db.psycopg is None:
            raise unittest.SkipTest('Explicit isolated PI_SECRETARY_TEST_DATABASE_URL required')
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(cls.url)
        if not params.get('dbname', '').endswith('_test') or params.get('service'):
            raise ValueError('Explicit _test database without service required')

    def setUp(self):
        from psycopg import sql
        self.schema = 'owner_plan_test_' + uuid.uuid4().hex
        self.conn = db.psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.assertTrue(self.conn.execute('SELECT current_database()').fetchone()[0].endswith('_test'))
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='5s'")
        self.conn.execute("SET lock_timeout='3s'")
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)
        from connectors import media_archive
        media_archive.migrate(self.conn)
        dialogue.migrate(self.conn)
        self.conn.execute('ALTER TABLE tasks ADD COLUMN version BIGINT NOT NULL DEFAULT 0; '
                          'ALTER TABLE tasks ADD COLUMN last_evidence_id BIGINT')
        db.migrate_notes(self.conn)
        policy.migrate(self.conn)
        capabilities.migrate(self.conn, legacy=False)
        secretary.migrate(self.conn)
        knowledge.migrate(self.conn)
        runtime.migrate(self.conn)
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:offline'))
        self.enterContext(patch.object(config, 'TIMEZONE', 'UTC'))
        self.enterContext(patch.dict(os.environ, {'SECRETARY_REVIEW_QUIET_START': '0',
                                                'SECRETARY_REVIEW_QUIET_END': '0'}))
        self.chat = self.enterContext(patch.object(brain, '_chat', side_effect=AssertionError('Unexpected AI')))
        self.provider = self.enterContext(patch.object(brain.ai_budget, 'request_content',
            side_effect=AssertionError('Unexpected provider')))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.consent()
        self.consent('email')

    @contextmanager
    def connection(self):
        yield self.conn

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def archive(self, text='Segui il tetto.', channel='telegram', historical=False):
        native_id = int(uuid.uuid4().hex[:8], 16) + 1
        meta = (dict(account='bot:777', chat_id='101', author=101, direction='in', chat_type='private',
            content_origin='direct_text', is_forwarded=False, message_id=native_id, historical=historical)
            if channel == 'telegram' else dict(historical=historical, direction='in'))
        source_id = (json.dumps(['bot', '777', '101', native_id]) if channel == 'telegram'
                     else 'synthetic-email-' + uuid.uuid4().hex)
        with patch.object(message_store, 'enqueue_derived'):
            mid = message_store.store(self.conn, channel, 'Synthetic sender', 'tetto', text,
                ts=datetime(2026, 10, 16, 10, tzinfo=timezone.utc).isoformat(), source_id=source_id,
                metadata=meta, historical=historical)
        knowledge.index_message(dict(message_id=mid))
        return mid

    def delegate(self):
        self.origin = self.archive()
        self.rid = self.conn.execute('''INSERT INTO secretary_responsibilities
            (type,objective,scope,triggers,allowed_actions,stop_condition,state,origin_message,operation_key)
            VALUES ('monitoring','Seguire il tetto',%s,%s,%s,'Revoca owner','active',%s,%s) RETURNING id''',
            (json.dumps(dict(topic='tetto', channels=['email', 'telegram'])),
             json.dumps(['linked_message', 'review_due']), json.dumps(['review', 'draft']),
             self.origin, uuid.uuid4().hex)).fetchone()[0]
        self.pid = self.conn.execute('''INSERT INTO secretary_practices
            (objective,state,next_action,waiting_for,blocked_reason,next_review_at,completion_criteria,
             responsibility_id,origin_message,operation_key)
            VALUES ('Riparare il tetto','active','Valutare preventivi','','',%s,'Conferma owner',%s,%s,%s)
            RETURNING id''', (datetime.now(timezone.utc) - timedelta(hours=1), self.rid,
                             self.origin, uuid.uuid4().hex)).fetchone()[0]
        self.conn.execute('''INSERT INTO secretary_links(practice_id,telegram_message_id,evidence,origin_message)
            VALUES (%s,%s,'Segui il tetto.',%s)''', (self.pid, self.origin, self.origin))

    def consent(self, channel='telegram', effect='ai_text', allowed=True):
        self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed) VALUES (%s,%s,%s)
            ON CONFLICT(channel,effect) DO UPDATE SET allowed=excluded.allowed,version=data_policies.version+1''',
            (channel, effect, allowed))

    def descriptor(self, kind='practice', identity=None):
        return dict(source_type=kind, source_id=self.pid if identity is None else identity)

    def allowed(self, kind='practice', identity=None):
        return dialogue.allows_source(self.conn, self.descriptor(kind, identity))

    def derived_practice(self):
        self.delegate()
        self.email = self.archive('Il preventivo contiene ' + CANARY, channel='email')
        evidence = knowledge.open_source('message', self.email)
        self.conn.execute('''INSERT INTO secretary_runtime_link_proposals
            (practice_id,practice_version,responsibility_id,responsibility_version,message_id,
             entry_id,revision,evidence,method) VALUES (%s,1,%s,1,%s,%s,%s,%s,'exact_scope')''',
            (self.pid, self.rid, self.email, evidence['entry_id'], evidence['revision'], CANARY))

        def infer(system, user, **kwargs):
            data = json.loads(user)
            source = next(s for s in data['sources'] if s['message_id'] == self.email)
            return json.dumps(dict(summary='Preventivo da valutare.', evidence=source['text'],
                citations=[source['citation']], action='review', next_action=CANARY,
                waiting_for=CANARY, next_review_at=(datetime.fromisoformat(data['now'])
                    + timedelta(days=2)).isoformat(), draft='', question=''))

        with patch.object(brain, '_chat', side_effect=infer):
            self.assertEqual(runtime.review(secretary.due_reviews(self.conn)[0])['status'], 'applied')
        self.assertTrue(self.allowed())
        self.conn.execute('DELETE FROM secretary_runtime_link_proposals WHERE message_id=%s', (self.email,))

    def prepare(self, text, changes, history=None, hook=None):
        mid = self.archive(text)

        def infer(system, user, **kwargs):
            from psycopg.pq import TransactionStatus
            self.assertEqual(self.conn.info.transaction_status, TransactionStatus.IDLE)
            if hook:
                hook(json.loads(user))
            return json.dumps(dict(changes=changes, clarification=''))

        with patch.object(brain.ai_budget, 'request_content', side_effect=lambda request: infer(
                request['messages'][0]['content'], request['messages'][1]['content'])) as provider:
            # Exercise the real serializer and provider boundary, not just _chat.
            with patch.object(brain, '_chat', new=REAL_CHAT):
                plan = secretary.prepare(text, mid, history=history)
        self.assertEqual(provider.call_count, 1)
        return mid, plan, provider.call_args.args[0]

    def create_from_context(self, kind='practice', history=None):
        text = 'Crea una pratica successiva.' if kind == 'practice' else 'Crea una responsabilità successiva.'
        fields = (pure_helpers.practice_fields(objective='Successiva ' + CANARY,
                    next_action='Valutare ' + CANARY) if kind == 'practice'
                  else pure_helpers.responsibility_fields(objective='Monitorare ' + CANARY))
        item = pure_helpers.change(kind=kind, evidence=text, fields=fields)
        mid, plan, request = self.prepare(text, [item], history)
        receipts = secretary.apply(self.conn, plan, mid)
        identity = self.conn.execute(f'SELECT id FROM {secretary.TABLES[kind]} WHERE origin_message=%s',
                                     (mid,)).fetchone()[0]
        return identity, mid, plan, receipts, request

    def assert_native_manifest(self, plan, required):
        self.assertEqual(plan['schema_version'], 2)
        self.assertEqual({s['source_id'] for s in plan['input_sources']}, set(required))
        self.assertEqual([s['source_id'] for s in plan['input_sources']], sorted(required))
        for source in plan['input_sources']:
            self.assertEqual(set(source), {'source_type', 'source_id', 'requires_media', 'owner_required'})
            self.assertEqual(source['source_type'], 'message')

    def test_all_exported_uncited_inputs_persist_and_revoke_both_record_kinds(self):
        self.derived_practice()
        private_pid = self.pid
        extra = self.archive('Uncited authorized owner history.')
        history = [dict(role='user', content='Uncited authorized owner history.', message_id=extra)]
        required = {self.origin, self.email, extra}
        for kind in ('practice', 'responsibility'):
            identity, mid, plan, _, request = self.create_from_context(kind, history)
            self.assertIn(CANARY, json.dumps(request))
            required.add(mid)
            self.assert_native_manifest(plan, required)
            self.assertIn(dict(source_type='practice', source_id=private_pid, version=2), plan['input_versions'])
            saved = self.conn.execute('SELECT payload FROM secretary_plans WHERE message_id=%s', (mid,)).fetchone()[0]
            self.assertEqual(saved, plan)
            self.assertTrue(self.allowed(kind, identity))
            self.consent('email', allowed=False)
            self.assertFalse(self.allowed(kind, identity))
            self.consent('email')
        self.chat.assert_not_called()
        self.provider.assert_not_called()

    def test_revocation_excludes_a_and_b_at_real_prepare_retrieval_final_boundaries(self):
        self.derived_practice()
        identity, _, _, _, _ = self.create_from_context()
        self.consent('email', allowed=False)
        self.assertFalse(self.allowed('practice', identity))
        text = 'Qual è lo stato delle pratiche?'
        mid, _, request = self.prepare(text, [])
        self.assertNotIn(CANARY, json.dumps(request))
        with patch.object(brain, '_chat', return_value='{"tool":"done"}') as retrieval:
            data, snapshot = dialogue.retrieve(text, mid, [])
        self.assertNotIn(CANARY, json.dumps((retrieval.call_args_list, data, snapshot), default=str))
        with patch.object(brain, '_chat', return_value='{"tool":"done"}'), \
                patch.object(brain.ai_budget, 'request_content', return_value='Stato verificato.') as provider, \
                patch.object(dialogue, 'compact'):
            dialogue.respond(text, mid)
        self.assertNotIn(CANARY, json.dumps(provider.call_args.args[0]))
        for kind, identity in (('practice', self.pid), ('practice', identity)):
            self.assertNotIn(CANARY, json.dumps(dialogue.filtered_knowledge(
                knowledge.open_source(kind, identity))))

    def test_self_update_is_preupdate_snapshot_not_cycle_and_preserves_all_inputs(self):
        text = 'Crea una pratica diretta.'
        mid, plan, _ = self.prepare(text, [pure_helpers.change(evidence=text)])
        secretary.apply(self.conn, plan, mid)
        pid = self.conn.execute('SELECT id FROM secretary_practices').fetchone()[0]
        for version in (1, 2):
            text = 'Aggiorna la pratica diretta.'
            item = pure_helpers.change(operation='update', identity=pid, version=version,
                fields=dict(next_action='Nuovo passo'), evidence=text)
            update_mid, update, _ = self.prepare(text, [item])
            self.assertIn(dict(source_type='practice', source_id=pid, version=version), update['input_versions'])
            secretary.apply(self.conn, update, update_mid)
            self.assertTrue(self.allowed('practice', pid))
        self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices').fetchone()[0], 3)
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata||'{\"is_forwarded\":true}'::jsonb WHERE id=%s",
                          (update_mid,))
        self.assertFalse(self.allowed('practice', pid))

    def test_runtime_then_owner_self_update_retains_email_and_replays_after_revoke(self):
        self.derived_practice()
        text = 'Aggiorna la pratica del tetto.'
        item = pure_helpers.change(operation='update', identity=self.pid, version=2,
            fields=dict(next_action='Un nuovo passo owner'), evidence=text)
        mid, plan, _ = self.prepare(text, [item])
        self.assertIn(dict(source_type='practice', source_id=self.pid, version=2), plan['input_versions'])
        receipt = secretary.apply(self.conn, plan, mid)
        self.assertTrue(self.allowed())
        self.consent('email', allowed=False)
        self.assertFalse(self.allowed())
        self.assertEqual(secretary.apply(self.conn, deepcopy(plan), mid), receipt)
        self.chat.assert_not_called()
        self.provider.assert_not_called()

    def test_historical_dependency_needs_read_consent_without_promoting_owner_history(self):
        self.derived_practice()
        pid, _, _, _, _ = self.create_from_context()
        self.conn.execute("UPDATE email_messages SET metadata=metadata||'{\"historical\":true}'::jsonb WHERE id=%s",
                          (self.email,))
        self.assertFalse(self.allowed('practice', pid))
        self.consent('email', effect='ai_history')
        self.assertTrue(self.allowed('practice', pid))
        self.consent('email', effect='ai_history', allowed=False)
        self.assertFalse(self.allowed('practice', pid))
        self.consent('email', effect='ai_history')
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata||'{\"historical\":true}'::jsonb WHERE id=%s",
                          (self.origin,))
        self.consent('telegram', effect='ai_history')
        self.assertFalse(self.allowed('practice', pid))
        self.assertFalse(policy.can_operate(self.conn, self.origin, 'owner_command', owner_required=True))
        self.chat.assert_not_called()
        self.provider.assert_not_called()

    def test_responsibility_partial_update_and_detach_preserve_old_private_inputs(self):
        self.derived_practice()
        private_pid = self.pid
        rid, _, _, _, _ = self.create_from_context('responsibility')
        text = 'Aggiorna la responsabilità successiva.'
        mid, plan, _ = self.prepare(text, [pure_helpers.change(kind='responsibility', operation='update',
            identity=rid, version=1, fields=dict(objective='Obiettivo aggiornato'), evidence=text)])
        secretary.apply(self.conn, plan, mid)
        self.assertTrue(self.allowed('responsibility', rid))
        text = 'Aggiorna la pratica del tetto.'
        mid, plan, _ = self.prepare(text, [pure_helpers.change(operation='update', identity=private_pid,
            version=2, fields=dict(responsibility_id=None, next_action='Passo owner'), evidence=text)])
        secretary.apply(self.conn, plan, mid)
        self.assertTrue(self.allowed('practice', private_pid))
        self.consent('email', allowed=False)
        self.assertFalse(self.allowed('practice', private_pid))
        self.assertFalse(self.allowed('responsibility', rid))
        self.assertTrue(self.allowed('responsibility', self.rid))

    def test_flattened_b_ignores_future_a_and_includes_original_a_version(self):
        self.delegate()
        pid_a = self.pid
        pid_b, _, plan, _, _ = self.create_from_context()
        self.assertIn(dict(source_type='practice', source_id=pid_a, version=1), plan['input_versions'])
        self.assertTrue(self.allowed('practice', pid_b))
        email = self.archive('Later private source.', channel='email')
        state = runtime._state(self.conn, dict(practice_id=pid_a, expected_version=1, reason='due'))
        self.conn.execute('''INSERT INTO secretary_runtime_reviews(operation_key,practice_id,
            practice_version,responsibility_id,responsibility_version,input,plan,status)
            VALUES ('later-a',%s,1,%s,1,%s,'{}','applied')''',
            (pid_a, self.rid, json.dumps(dict(state=state, sources=[dict(source_type='message', source_id=email)]),
                                       default=str)))
        self.conn.execute('UPDATE secretary_practices SET version=2 WHERE id=%s', (pid_a,))
        self.consent('email', allowed=False)
        self.assertFalse(self.allowed('practice', pid_a))
        self.assertTrue(self.allowed('practice', pid_b))

    def test_version_change_during_snapshot_resolution_stops_before_provider(self):
        self.delegate()
        original = dialogue.filtered_practices

        def changed(snapshot):
            result = original(snapshot)
            self.conn.execute('UPDATE secretary_practices SET version=version+1 WHERE id=%s', (self.pid,))
            return result

        mid = self.archive('Crea una pratica.')
        with patch.object(dialogue, 'filtered_practices', side_effect=changed):
            with self.assertRaises(PermissionError):
                secretary.prepare('Crea una pratica.', mid)
        self.chat.assert_not_called()
        self.provider.assert_not_called()

    def test_media_requirement_survives_attachment_removal_and_revoke(self):
        self.derived_practice()
        self.consent('email', effect='ai_media')
        attachment = self.conn.execute('''INSERT INTO attachments(message_id,channel,filename)
            VALUES (%s,'email','synthetic.pdf') RETURNING id''', (self.email,)).fetchone()[0]
        pid, _, plan, _, _ = self.create_from_context()
        source = next(s for s in plan['input_sources'] if s['source_id'] == self.email)
        self.assertTrue(source['requires_media'])
        self.conn.execute('DELETE FROM attachments WHERE id=%s', (attachment,))
        self.assertTrue(self.allowed('practice', pid))
        self.consent('email', effect='ai_media', allowed=False)
        self.assertFalse(self.allowed('practice', pid))
        self.assertFalse(dialogue.allows_source(self.conn, dict(source, allowed=True, requires_media=True)))

    def test_every_uncited_native_message_is_rechecked_not_just_email(self):
        self.derived_practice()
        extra = self.archive('Unrelated owner history.')
        pid, _, plan, _, _ = self.create_from_context(history=[dict(role='user',
            content='Unrelated owner history.', message_id=extra)])
        self.assertIn(extra, {s['source_id'] for s in plan['input_sources']})
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata||'{\"historical\":true}'::jsonb WHERE id=%s",
                          (extra,))
        self.assertFalse(self.allowed('practice', pid))
        self.consent('telegram', effect='ai_history')
        # Historical read consent never restores native owner authority.
        self.assertFalse(self.allowed('practice', pid))
        self.assertFalse(policy.can_operate(self.conn, extra, 'owner_command', owner_required=True))

    def test_v1_replay_is_compatible_but_untracked_context_is_not_read_authority(self):
        self.derived_practice()
        text = 'Crea una pratica legacy.'
        mid = self.archive(text)
        _, ts = secretary._trusted_message(self.conn, mid)
        item = dict(pure_helpers.change(evidence=text, fields=pure_helpers.practice_fields(
            objective=CANARY)), operation_key=f'secretary:{mid}:0')
        plan = dict(schema_version=1, message_id=mid, text_hash=secretary.payload_hash(text),
            reference_time=ts.isoformat(), changes=[item], clarification='', responsibility_versions=[])
        receipt = secretary.apply(self.conn, plan, mid)
        pid = self.conn.execute('SELECT id FROM secretary_practices WHERE origin_message=%s', (mid,)).fetchone()[0]
        self.assertFalse(self.allowed('practice', pid))
        self.assertEqual(secretary.apply(self.conn, deepcopy(plan), mid), receipt)
        with self.assertRaises(secretary.InvalidPlan):
            secretary.apply(self.conn, dict(plan, input_sources=[]), mid)

    def test_fresh_and_direct_records_work_without_runtime_table_but_unknown_versions_close(self):
        self.conn.execute('DROP TABLE secretary_runtime_reviews')
        text = 'Crea una pratica nuova.'
        mid, plan, _ = self.prepare(text, [pure_helpers.change(evidence=text)])
        secretary.apply(self.conn, plan, mid)
        pid = self.conn.execute('SELECT id FROM secretary_practices').fetchone()[0]
        self.assertTrue(self.allowed('practice', pid))
        text = 'Aggiorna la pratica nuova.'
        update_mid, update, _ = self.prepare(text, [pure_helpers.change(operation='update',
            identity=pid, version=1, fields=dict(next_action='Passo owner tracciato'), evidence=text)])
        secretary.apply(self.conn, update, update_mid)
        self.assertTrue(self.allowed('practice', pid))
        self.delegate()
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed('responsibility', self.rid))
        self.conn.execute('UPDATE secretary_practices SET version=2 WHERE id=%s', (self.pid,))
        self.assertFalse(self.allowed())

    def test_runtime_cycle_still_fails_closed(self):
        self.derived_practice()
        self.conn.execute('''UPDATE secretary_runtime_reviews SET input=jsonb_set(input,'{sources}',%s::jsonb)
            WHERE practice_id=%s''', (json.dumps([self.descriptor()]), self.pid))
        self.assertFalse(self.allowed())

    def test_normalized_manifest_is_deterministic_and_ors_requirements(self):
        mid = self.archive('Unico input.')
        a = dict(source_type='message', source_id=mid)
        b = dict(a, owner_required=True)
        self.assertEqual(dialogue.resolve_source_manifest([a, b, a], conn=self.conn),
                         dialogue.resolve_source_manifest([b, a], conn=self.conn))
        self.assertEqual(dialogue.resolve_source_manifest([a, b], conn=self.conn),
            [dict(a, owner_required=True, requires_media=False)])
        for source in (dict(a, source_id=True), dict(a, requires_media='yes'),
                       dict(a, source_id=2 ** 63), dict(a, source_type=[])):
            with self.subTest(source=source), self.assertRaises(PermissionError):
                dialogue.resolve_source_manifest([source], conn=self.conn)


if __name__ == '__main__':
    unittest.main()