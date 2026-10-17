"""Shared task egress: real native gates and captured provider requests.

Offline tests use a strict SQL double. SQL tests opt in ONLY through
PI_SECRETARY_TEST_DATABASE_URL, an explicit *_test DB on an existing Unix socket;
each test bootstraps a private schema. No dotenv, configured DB, server startup,
workers or external providers. Run with PI_SECRETARY_CONFIG_FILE=/dev/null and a
temporary DATA_DIR. Borrow fixture helpers under an alias, never inherited tests.
"""
from contextlib import nullcontext
import json
import unittest
from unittest.mock import patch
import uuid

import brain
import config
import db
import jobs
import knowledge
import owner_dialogue as dialogue
import policy
import secretary
import task_reconciliation as lifecycle
import test_reconciliation_task_privacy as helpers


CANARY = 'SHARED_TASK_RETAINED_EMAIL_TITLE_19ab72'
QUESTION = 'Quali attività sono aperte?'
NO_CHANGES = '{"changes":[],"clarification":""}'


class _SharedCases:
    def guard(self):
        for target in ('socket.socket.connect', 'socket.create_connection',
                       'urllib.request.urlopen'):
            self.enterContext(patch(target, side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(db, '_conn',
            side_effect=AssertionError('Configured database forbidden')))
        if db.psycopg:
            self.enterContext(patch.object(db.psycopg, 'connect',
                side_effect=AssertionError('Unapproved SQL connection forbidden')))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.enterContext(patch.object(config, 'TIMEZONE', 'UTC'))
        self.enterContext(patch.object(jobs, 'execution_context', return_value=None))
        self.provider = self.enterContext(patch.object(brain.ai_budget, 'request_content',
            side_effect=self.answer))

    def ready(self):
        self.consent('telegram')
        self.owner = self.message('telegram', current=True, body=QUESTION)

    def answer(self, payload, **kwargs):
        if 'Read-only retrieval planner' in payload['messages'][0]['content']:
            return '{"tool":"done"}'
        if any(isinstance(m['content'], str) and m['content'].startswith('ARCHIVIO NON FIDATO:')
               for m in payload['messages']):
            return 'Stato verificato.'
        return NO_CHANGES

    def descriptor(self, identity, **fields):
        return dict(source_type='task', source_id=identity, **fields)

    def allowed(self, identity, **fields):
        return dialogue.allows_source(self.conn, self.descriptor(identity, **fields))

    def manifest(self, sources):
        return dialogue.resolve_source_manifest(sources, conn=self.conn)

    def retained_title(self, *, historical=False, attached=False):
        origin = self.message('telegram')
        identity = self.task(owner_origin_message=origin, title='Owner task')
        email = self.message('email', historical=historical)
        if attached:
            self.attach(email, image=True)
        # The title changes once. Later deadline-only edits retain that title
        # while replacing last_evidence_id, just like the real lifecycle does.
        self.applied_update(identity, email, dict(title=CANARY))
        updates = []
        for day in (20, 21, 22):
            mid = self.message('telegram', current=True)
            updates.append(mid)
            self.applied_update(identity, mid, dict(deadline=f'2026-10-{day}T12:00:00+00:00'))
        return identity, origin, email, updates

    def context(self):
        with patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)):
            return dialogue.load_context(self.owner, QUESTION)

    def chat(self, context=None, **kwargs):
        context = self.context() if context is None else context
        with patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)):
            return brain.chat_with_user(QUESTION, history=context['history'], dialogue=dict(
                tasks=context['tasks'], authorized_context={},
                memory_plan=dict(changes=[], known=[]),
                egress_sources=[self.descriptor(row['id']) for row in context['tasks']]), **kwargs)

    def catalogue(self, identity):
        # A controlled catalogue adapter exercises the real typed retrieval
        # filter. Production knowledge currently searches messages/notes/practices,
        # not tasks; no mock permission decision or forged native row is used.
        citation = f'task:{identity}@synthetic'
        return dict(query='attività', citations=[dict(self.descriptor(identity), citation=citation)],
            snippets=[dict(citation=citation, text=CANARY)], untrusted_sources=True)

    def retrieve(self, identity):
        with patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)), \
                patch.object(knowledge, 'context', return_value=self.catalogue(identity)), \
                patch.object(knowledge, 'search', return_value=[]), \
                patch.object(dialogue, '_current_sources', return_value=[]), \
                patch.object(secretary, 'context', return_value=dict(practices=[], responsibilities=[])):
            return dialogue.retrieve(QUESTION, self.owner, [])[0]

    def assert_boundaries(self, identity, expected):
        self.provider.reset_mock()
        context = self.context()
        self.assertEqual(any(row['id'] == identity for row in context['tasks']), expected)
        self.assertEqual(self.chat(context), 'Stato verificato.')
        self.provider.assert_called_once()
        payload = self.provider.call_args.args[0]
        self.assertEqual(CANARY in json.dumps(payload), expected)
        self.assertEqual(payload['messages'][-1], dict(role='user', content=QUESTION))
        self.assertNotIn(helpers.RECEIPT, json.dumps(payload))
        self.provider.reset_mock()
        retrieved = self.retrieve(identity)
        self.provider.assert_called_once()
        self.assertEqual(CANARY in json.dumps(self.provider.call_args.args[0]), expected)
        self.assertEqual(CANARY in json.dumps(retrieved), expected)
        self.assertNotIn(helpers.RECEIPT, json.dumps(self.provider.call_args.args[0]))

    def test_all_retained_applied_origins_flatten_and_email_revoke_filters_both_boundaries(self):
        identity, origin, email, updates = self.retained_title()
        self.assertTrue(self.allowed(identity))
        sources = self.manifest([self.descriptor(identity)])
        self.assertEqual([s['source_id'] for s in sources], sorted({origin, email, *updates}))
        self.assertTrue(all(set(s) == {'source_type', 'source_id', 'requires_media', 'owner_required'}
                            for s in sources))
        self.assert_boundaries(identity, True)
        self.consent('email', allowed=False)
        self.assertFalse(self.allowed(identity))
        with self.assertRaisesRegex(PermissionError, '^Context source egress denied$'):
            self.manifest([self.descriptor(identity)])
        with self.assertRaises(PermissionError):
            dialogue.assert_egress_sources(sources, conn=self.conn)
        self.assert_boundaries(identity, False)
        self.consent('email')
        self.assert_boundaries(identity, True)

    def test_applied_media_needs_text_and_media_even_after_multiple_deadline_edits(self):
        identity, _, email, _ = self.retained_title(attached=True)
        self.assert_boundaries(identity, False)
        self.consent('email', 'ai_media')
        sources = self.manifest([self.descriptor(identity)])
        self.assertTrue(next(s for s in sources if s['source_id'] == email)['requires_media'])
        self.assert_boundaries(identity, True)
        self.consent('email', 'ai_media', allowed=False)
        self.assert_boundaries(identity, False)
        with self.assertRaises(PermissionError):
            dialogue.assert_egress_sources(sources, conn=self.conn)
        self.consent('email', 'ai_media')
        self.consent('email', allowed=False)
        self.assert_boundaries(identity, False)

    def test_applied_history_requires_read_consent_never_operational_replay(self):
        # The fixture represents a formerly live applied source now classified
        # as historical; new operations are not authorized by ai_history.
        origin = self.message('telegram')
        email = self.message('email', historical=True)
        identity = self.task(title=CANARY, owner_origin_message=origin,
                             last_evidence_id=self.owner)
        self.derivation(identity, email)
        self.assert_boundaries(identity, False)
        self.consent('email', 'ai_history')
        self.assert_boundaries(identity, True)
        self.assertFalse(policy.can_operate(self.conn, email, 'task_propose'))
        self.consent('email', 'ai_history', allowed=False)
        self.assertFalse(self.allowed(identity, channel='telegram', historical=False, allowed=True))
        self.assert_boundaries(identity, False)

    def test_pending_rejected_stale_and_other_tasks_origins_do_not_taint(self):
        identity = self.task(title=CANARY, owner_origin_message=self.owner)
        denied = self.message('whatsapp')
        for status in ('pending', 'rejected', 'stale'):
            self.derivation(identity, denied, status=status)
            self.derivation(identity, None, status=status)
        other = self.task(owner_origin_message=self.owner)
        self.derivation(other, denied)
        self.assert_boundaries(identity, True)
        self.assertFalse(self.allowed(other))

    def test_null_and_missing_applied_origins_fail_closed_at_both_boundaries(self):
        for mid in (None, 9223372036854775807):
            with self.subTest(mid=mid):
                identity = self.task(title=CANARY, owner_origin_message=self.owner)
                self.derivation(identity, mid)
                self.assertFalse(self.allowed(identity))
                self.assert_boundaries(identity, False)

    def test_applied_evidence_does_not_replace_missing_or_invalid_base_origin(self):
        for origin in (None, 9223372036854775807):
            with self.subTest(origin=origin):
                identity = self.task(title=CANARY, source_message=origin)
                self.derivation(identity, self.owner)
                self.assertFalse(self.allowed(identity))
                self.assert_boundaries(identity, False)

    def test_each_existing_native_origin_still_requires_its_own_consent(self):
        denied = self.message('whatsapp')
        for field in ('source_message', 'owner_origin_message', 'last_evidence_id'):
            with self.subTest(field=field):
                origins = dict(owner_origin_message=self.owner)
                origins[field] = denied
                identity = self.task(title=CANARY, **origins)
                self.derivation(identity, self.owner)
                self.assertFalse(self.allowed(identity))
                self.assert_boundaries(identity, False)

    def test_cached_task_snapshot_rechecked_before_chat_provider_even_with_vision(self):
        identity, _, _, _ = self.retained_title()
        context = self.context()
        self.assertIn(CANARY, json.dumps(context, default=str))
        self.consent('email', allowed=False)
        self.provider.reset_mock()
        with self.assertRaisesRegex(PermissionError, '^Context source egress denied$'):
            self.chat(context, images=[dict(type='image_url',
                image_url=dict(url='data:image/png;base64,AA=='))])
        self.provider.assert_not_called()
        self.assert_boundaries(identity, False)

    def test_new_applied_origin_after_snapshot_is_rechecked_not_only_latest_evidence(self):
        identity = self.task(title=CANARY, owner_origin_message=self.owner)
        context = self.context()
        self.derivation(identity, self.message('whatsapp'))
        self.provider.reset_mock()
        with self.assertRaises(PermissionError):
            self.chat(context)
        self.provider.assert_not_called()

    def test_retrieval_refilters_cached_task_between_provider_rounds_after_revoke(self):
        identity, _, _, _ = self.retained_title()

        def revoke(payload, **kwargs):
            self.consent('email', allowed=False)
            return '{"tool":"search","query":"attività"}' if self.provider.call_count == 1 else '{"tool":"done"}'

        self.provider.side_effect = revoke
        retrieved = self.retrieve(identity)
        self.assertEqual(self.provider.call_count, 2)
        payloads = [call.args[0] for call in self.provider.call_args_list]
        self.assertIn(CANARY, json.dumps(payloads[0]))
        self.assertNotIn(CANARY, json.dumps(payloads[1]))
        self.assertNotIn(CANARY, json.dumps(retrieved))

    def test_task_backed_appointment_traverses_all_retained_origins(self):
        identity, _, _, _ = self.retained_title()
        appointment = self.appointment(identity)
        source = dict(source_type='appointment', source_id=appointment)
        self.assertEqual(self.manifest([source]), self.manifest([self.descriptor(identity)]))
        self.consent('email', allowed=False)
        self.assertFalse(dialogue.allows_source(self.conn, source))
        with self.assertRaises(PermissionError):
            self.manifest([source])

    def test_flattened_media_requirement_survives_attachment_removal(self):
        identity, _, email, _ = self.retained_title(attached=True)
        self.consent('email', 'ai_media')
        sources = self.manifest([self.descriptor(identity)])
        self.detach(email)
        dialogue.assert_egress_sources(sources, conn=self.conn)
        self.consent('email', 'ai_media', allowed=False)
        with self.assertRaises(PermissionError):
            dialogue.assert_egress_sources(sources, conn=self.conn)


class TaskSharedProvenanceTests(_SharedCases, unittest.TestCase):
    message = helpers.ReconciliationTaskPrivacyTests.message
    task = helpers.ReconciliationTaskPrivacyTests.task
    attach = helpers.ReconciliationTaskPrivacyTests.attach
    consent = helpers.ReconciliationTaskPrivacyTests.consent
    derivation = helpers.ReconciliationTaskPrivacyTests.derivation

    def setUp(self):
        self.appointments = {}
        helpers.ReconciliationTaskPrivacyTests.setUp(self)
        self.ready()

    def applied_update(self, identity, mid, changes):
        self.derivation(identity, mid)
        task = self.tasks[identity]
        task.update(changes, last_evidence_id=mid, version=task['version'] + 1)

    def detach(self, mid):
        self.attachments.pop(mid, None)

    def appointment(self, identity):
        aid = len(self.appointments) + 1
        self.appointments[aid] = f'task:{identity}:v4'
        return aid

    def execute(self, sql, params=None):
        query = ' '.join(sql.split())
        row, rows = None, []
        if query == 'SELECT summary,summarized_through FROM owner_dialogue_state WHERE id=1':
            row = ('', 0)
        elif query.startswith('SELECT message_id,message_time,user_text,response'):
            pass
        elif query.startswith('SELECT id,title,deadline,status,owner_deadline_date,owner_remind_at,version FROM tasks '):
            rows = [(t['id'], t['title'], t['deadline'], t['status'], None, None, t['version'])
                    for t in self.tasks.values() if t['status'] == 'open']
        elif query.startswith('SELECT sync_key FROM appointments WHERE id='):
            value = self.appointments.get(params[0])
            row = (value,) if value else None
        else:
            return helpers.ReconciliationTaskPrivacyTests.execute(self, sql, params)
        from types import SimpleNamespace
        return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: rows)

    def test_malformed_applied_ids_are_not_coerced_or_treated_as_absent(self):
        identity = self.task(owner_origin_message=self.owner)
        for mid in (True, False, 0, -1, '1', 2 ** 63):
            with self.subTest(mid=mid):
                self.derivations = [(identity, mid, 'applied')]
                self.assertFalse(self.allowed(identity))


class TaskSharedProvenancePostgresTests(_SharedCases, unittest.TestCase):
    setUpClass = classmethod(helpers.ReconciliationTaskPrivacyPostgresTests.setUpClass.__func__)
    drop_schema = helpers.ReconciliationTaskPrivacyPostgresTests.drop_schema
    message = helpers.ReconciliationTaskPrivacyPostgresTests.message
    task = helpers.ReconciliationTaskPrivacyPostgresTests.task
    attach = helpers.ReconciliationTaskPrivacyPostgresTests.attach
    consent = helpers.ReconciliationTaskPrivacyPostgresTests.consent
    derivation = helpers.ReconciliationTaskPrivacyPostgresTests.derivation

    def setUp(self):
        helpers.ReconciliationTaskPrivacyPostgresTests.setUp(self)
        self.ready()

    def applied_update(self, identity, mid, changes):
        version = self.conn.execute('SELECT version FROM tasks WHERE id=%s', (identity,)).fetchone()[0]
        self.assertEqual(lifecycle.apply_change(self.conn, dict(task_id=identity,
            expected_version=version, evidence_key=uuid.uuid4().hex, message_id=mid,
            operation='update', changes=changes, reason=helpers.RECEIPT, evidence='Synthetic evidence'),
            authority_message_id=self.owner), 'applied')

    def detach(self, mid):
        self.conn.execute('DELETE FROM attachments WHERE message_id=%s', (mid,))

    def appointment(self, identity):
        return self.conn.execute('''INSERT INTO appointments(dt,title,sync_key)
            VALUES ('2026-10-22 12:00',%s,%s) RETURNING id''',
            (CANARY, f'task:{identity}:v4')).fetchone()[0]

    def test_full_owner_dialogue_real_lifecycle_chat_retains_title_then_omits_after_revoke(self):
        identity, _, email, updates = self.retained_title()
        self.assertEqual(self.conn.execute('SELECT title,version,last_evidence_id FROM tasks WHERE id=%s',
            (identity,)).fetchone(), (CANARY, 4, updates[-1]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM task_changes WHERE task_id=%s AND status='applied'",
            (identity,)).fetchone(), (4,))
        for expected in (True, False):
            if not expected:
                self.consent('email', allowed=False)
            mid = self.message('telegram', current=True, body=QUESTION)
            self.provider.reset_mock()
            with patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)):
                self.assertEqual(dialogue.respond(QUESTION, mid), 'Stato verificato.')
            payloads = [call.args[0] for call in self.provider.call_args_list]
            final = next(p for p in payloads if any(isinstance(m['content'], str)
                and m['content'].startswith('ARCHIVIO NON FIDATO:') for m in p['messages']))
            self.assertEqual(CANARY in json.dumps(final), expected)
            extraction = next(p for p in payloads if 'Extract ONLY explicit personal commitments'
                              in p['messages'][0]['content'])
            self.assertEqual(CANARY in json.dumps(extraction), expected)
            if not expected:
                self.assertNotIn(CANARY, json.dumps(payloads))
            self.assertNotIn(helpers.RECEIPT, json.dumps(payloads))
        self.assertEqual(self.conn.execute('SELECT message_id FROM task_changes WHERE task_id=%s '
            "AND changes ? 'title'", (identity,)).fetchone(), (email,))


if __name__ == '__main__':
    unittest.main()