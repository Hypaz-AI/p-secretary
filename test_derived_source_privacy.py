"""Opt-in PostgreSQL regressions for revoked, transitively derived practice data.

Reuses only RuntimePostgresTests' isolated-schema helpers, not its test methods.
PI_SECRETARY_TEST_DATABASE_URL must explicitly name an isolated *_test database.
All model/provider calls are intercepted and Python network access is forbidden.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch
import uuid

import brain
import db
import jobs
import knowledge
import owner_dialogue as dialogue
import secretary
import secretary_runtime as runtime
import test_secretary_runtime as helpers


CANARY = 'PRIVATE_EMAIL_DERIVED_CANARY_7af812'


class DerivedSourcePrivacyTests(unittest.TestCase):
    setUpClass = classmethod(helpers.RuntimePostgresTests.setUpClass.__func__)
    connection = helpers.RuntimePostgresTests.connection
    drop_schema = helpers.RuntimePostgresTests.drop_schema
    archive = helpers.RuntimePostgresTests.archive
    delegate = helpers.RuntimePostgresTests.delegate
    consent = helpers.RuntimePostgresTests.consent
    wake = helpers.RuntimePostgresTests.wake

    def setUp(self):
        helpers.RuntimePostgresTests.setUp(self)
        db.migrate_notes(self.conn)
        self.provider = self.enterContext(patch.object(brain.ai_budget, 'request_content',
            side_effect=AssertionError('Unexpected provider call')))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.consent()
        self.consent('email')

    def descriptor(self, kind='practice', identity=None):
        return dict(source_type=kind, source_id=self.pid if identity is None else identity)

    def allowed(self, kind='practice', identity=None):
        return dialogue.allows_source(self.conn, self.descriptor(kind, identity))

    def derived_practice(self):
        self.delegate()
        self.email = self.archive('Il preventivo tetto contiene ' + CANARY, channel='email')
        evidence = knowledge.open_source('message', self.email)
        self.conn.execute('''INSERT INTO secretary_runtime_link_proposals
            (practice_id,practice_version,responsibility_id,responsibility_version,message_id,
             entry_id,revision,evidence,method) VALUES (%s,1,%s,1,%s,%s,%s,%s,'exact_scope')''',
            (self.pid, self.rid, self.email, evidence['entry_id'], evidence['revision'], CANARY))

        def infer(system, user, **kwargs):
            data = json.loads(user)
            evidence = next(s for s in data['sources'] if s['message_id'] == self.email)
            result = helpers.plan(datetime.fromisoformat(data['now']), sources=[evidence])
            result.update(next_action=CANARY + ' next step', waiting_for=CANARY + ' waiting')
            return json.dumps(result)

        wake = self.wake()
        with patch.object(brain, '_chat', side_effect=infer) as model:
            self.assertEqual(runtime.review(wake)['status'], 'applied')
        model.assert_called_once()
        self.assertTrue(self.allowed())
        # A proposal need not remain in the current evidence set forever.
        # Privileged secretary_links are append-only and are not modified.
        self.conn.execute('DELETE FROM secretary_runtime_link_proposals WHERE message_id=%s', (self.email,))
        return wake['operation_key']

    def next_wake(self):
        self.conn.execute('UPDATE secretary_practices SET next_review_at=%s WHERE id=%s',
            (datetime.now(timezone.utc) - timedelta(hours=1), self.pid))
        return self.wake()

    def saved_input(self, key):
        return self.conn.execute('SELECT input FROM secretary_runtime_reviews WHERE operation_key=%s',
                                 (key,)).fetchone()[0]

    def save_input(self, key, value):
        self.conn.execute('UPDATE secretary_runtime_reviews SET input=%s WHERE operation_key=%s',
                          (json.dumps(value), key))

    def assert_prepare_and_dialogue(self, expected):
        text = 'Qual è lo stato del tetto?'
        mid = self.archive(text)
        with patch.object(brain, '_chat', return_value='{"changes":[],"clarification":""}') as model:
            secretary.prepare(text, mid)
        prompt = json.loads(model.call_args.args[1])
        self.assertEqual(CANARY in json.dumps(prompt), expected)
        self.assertEqual(bool(prompt['SNAPSHOT']['practices']), expected)
        # Real retrieval filtering and the final dialogue's actual provider path.
        with patch.object(brain, '_chat', return_value='{"tool":"done"}') as retrieval, \
                patch.object(brain.ai_budget, 'request_content', return_value='Stato verificato.') as provider, \
                patch.object(dialogue, 'compact'):
            dialogue.respond(text, mid)
        self.assertEqual(CANARY in json.dumps(retrieval.call_args_list, default=str), expected)
        self.assertEqual(CANARY in json.dumps(provider.call_args.args[0]), expected)
        self.assertEqual(CANARY in json.dumps(dialogue.filtered_knowledge(
            knowledge.open_source('practice', self.pid))), expected)
        # knowledge.context adds entry_id=null for non-indexed optional sources.
        citations = dialogue.filtered_knowledge(knowledge.context('Riparare il tetto', limit=12))['citations']
        self.assertEqual(any(c['source_type'] == 'practice' and c['source_id'] == self.pid
                             for c in citations), expected)

    def test_revoked_email_not_exported_by_prepare_retrieval_or_final_dialogue(self):
        self.derived_practice()
        self.consent('email', allowed=False)
        self.assertFalse(self.allowed())
        self.assert_prepare_and_dialogue(False)
        self.chat.assert_not_called()
        self.provider.assert_not_called()

    def test_authorized_derived_practice_still_reaches_prepare_and_dialogue(self):
        self.derived_practice()
        self.assert_prepare_and_dialogue(True)

    def test_runtime_own_practice_prompt_rechecks_revoked_prior_inputs(self):
        self.derived_practice()
        self.consent('email', allowed=False)
        with self.assertRaises(jobs.Deferred):
            runtime.review(self.next_wake())
        self.chat.assert_not_called()
        self.provider.assert_not_called()

    def test_authorized_runtime_prompt_retains_derived_fields(self):
        self.derived_practice()
        with patch.object(brain, '_chat', side_effect=lambda system, user, **kw:
                json.dumps(helpers.plan(datetime.fromisoformat(json.loads(user)['now']),
                                       sources=json.loads(user)['sources']))) as model:
            self.assertEqual(runtime.review(self.next_wake())['status'], 'applied')
        data = json.loads(model.call_args.args[1])
        self.assertIn(CANARY, data['practice']['next_action'])
        self.assertIn(CANARY, data['practice']['waiting_for'])

    def test_all_older_applied_manifests_count_not_only_current_version(self):
        key = self.derived_practice()
        # A later review has only the owner source, but its input practice
        # contains the earlier private fields. Keep all older dependencies.
        with helpers.RuntimePostgresTests.respond_with_plan(self):
            runtime.review(self.next_wake())
        current = self.conn.execute('SELECT max(practice_version) FROM secretary_runtime_reviews').fetchone()[0]
        self.assertGreater(current, self.conn.execute('SELECT practice_version FROM secretary_runtime_reviews '
            'WHERE operation_key=%s', (key,)).fetchone()[0])
        self.consent('email', allowed=False)
        self.assertFalse(self.allowed())
        with self.assertRaises(jobs.Deferred):
            runtime.review(self.next_wake())
        self.chat.assert_not_called()

    def test_candidate_model_prompt_denies_derived_practice_after_revoke(self):
        self.derived_practice()
        mid = self.archive('Il fornitore conferma la copertura e il preventivo.')
        runtime.observe(self.conn, mid)
        self.consent('email', allowed=False)
        with self.assertRaises(jobs.Deferred):
            runtime.review(dict(observation_message_id=mid, operation_key=f'secretary-observe:{mid}'))
        self.chat.assert_not_called()

    def test_all_candidates_checked_even_when_other_candidate_is_authorized(self):
        self.derived_practice()
        private_pid = self.pid
        self.delegate()
        mid = self.archive('Il fornitore conferma la copertura e il preventivo.')
        runtime.observe(self.conn, mid)
        self.consent('email', allowed=False)
        self.assertTrue(self.allowed())
        self.assertFalse(self.allowed(identity=private_pid))
        with self.assertRaises(jobs.Deferred):
            runtime.review(dict(observation_message_id=mid, operation_key=f'secretary-observe:{mid}'))
        self.chat.assert_not_called()

    def test_transitive_practice_manifest_rechecks_its_applied_sources(self):
        key = self.derived_practice()
        private_pid = self.pid
        self.delegate()
        other = knowledge.open_source('practice', private_pid)
        value = self.saved_input(key)
        value['state'] = runtime._state(self.conn, dict(practice_id=self.pid, expected_version=1, reason='due'))
        value = json.loads(json.dumps(value, default=str))
        value['sources'] = [other]
        with self.conn.transaction():
            self.conn.execute('''INSERT INTO secretary_runtime_reviews(operation_key,practice_id,
                practice_version,responsibility_id,responsibility_version,input,plan,status)
                VALUES (%s,%s,1,%s,1,%s,'{}','applied')''',
                (uuid.uuid4().hex, self.pid, self.rid, json.dumps(value)))
            # Applied reviews consume their input version; a version-1 input
            # cannot influence a practice that is still at version 1.
            self.assertEqual(self.conn.execute('''UPDATE secretary_practices
                SET version=version+1 WHERE id=%s AND version=1 RETURNING version''',
                (self.pid,)).fetchone(), (2,))
        self.assertTrue(self.allowed())
        self.consent('email', allowed=False)
        self.assertFalse(self.allowed())

    def test_media_rechecked_natively_in_old_and_current_sources(self):
        key = self.derived_practice()
        self.conn.execute('''INSERT INTO attachments(message_id,channel,filename,content_type,data)
            VALUES (%s,'email','offer.xml','application/xml',%s)''',
            (self.email, b'<offer>tetto private media</offer>'))
        self.assertFalse(self.allowed())
        self.consent('email', effect='ai_media')
        self.assertTrue(self.allowed())
        knowledge.index_message(dict(message_id=self.email))
        document = next(s for s in runtime._open_sources([self.email]) if s['source_type'] == 'document')
        self.assertTrue(runtime._sources_current(self.conn, [document], ai=True))
        self.consent('email', effect='ai_media', allowed=False)
        self.assertFalse(self.allowed())
        disguised = dict(document, source_type='message', source_id=self.email,
                         channel='telegram', historical=False, current=True)
        self.assertFalse(runtime._sources_current(self.conn, [disguised], ai=True))
        self.assertFalse(dialogue.allows_source(self.conn, disguised))
        with self.assertRaises(jobs.Deferred):
            runtime._sources_current(self.conn, [document], ai=True)
        self.assertIsNotNone(self.saved_input(key))

    def test_forged_flags_ids_and_historical_sources_cannot_authorize(self):
        key = self.derived_practice()
        value = self.saved_input(key)
        email_source = next(s for s in value['sources'] if s['message_id'] == self.email)
        self.consent('email', allowed=False)
        email_source.update(channel='telegram', historical=False, current=True, allowed=True, ai_text=True)
        self.save_input(key, value)
        self.assertFalse(self.allowed())
        self.consent('email')
        historical = self.archive(CANARY, channel='email', historical=True)
        for identity in (historical, self.origin, -1, True, '1', 2 ** 63):
            broken = deepcopy(value)
            item = next(s for s in broken['sources'] if s['message_id'] == self.email)
            item['source_id'] = identity
            self.save_input(key, broken)
            with self.subTest(identity=identity):
                self.assertFalse(self.allowed())
        # A valid native historical source remains denied despite forged flags.
        value['sources'] = [dict(runtime._bounded_source(knowledge.open_source('message', historical)),
                                 historical=False, current=True, channel='telegram')]
        self.save_input(key, value)
        self.assertFalse(self.allowed())

    def test_missing_malformed_and_cyclic_legacy_manifests_fail_closed(self):
        key = self.derived_practice()
        original = self.saved_input(key)
        for manifest in (None, [], {}, [None], [{}], [self.descriptor()],
                         [dict(source_type=[], source_id=self.origin)]):
            value = dict(original, sources=manifest)
            self.save_input(key, value)
            with self.subTest(manifest=manifest):
                self.assertFalse(self.allowed())
        value = dict(original)
        value.pop('sources')
        self.save_input(key, value)
        self.assertFalse(self.allowed())
        self.save_input(key, original)
        self.assertTrue(self.allowed())

    def test_missing_runtime_provenance_table_fails_closed_without_schema_repair(self):
        self.derived_practice()
        self.conn.execute('DROP TABLE secretary_runtime_reviews')
        self.assertFalse(self.allowed())
        self.assertIsNone(self.conn.execute("SELECT to_regclass('secretary_runtime_reviews')").fetchone()[0])

    def test_incomplete_applied_state_and_uncited_sources_fail_closed(self):
        key = self.derived_practice()
        original = self.saved_input(key)
        for part, field in (('state', 'responsibility'), ('practice', 'responsibility_id'),
                            ('practice', 'origin_message')):
            value = deepcopy(original)
            target = value['state'] if part == 'state' else value['state'][part]
            target.pop(field)
            self.save_input(key, value)
            with self.subTest(part=part, field=field):
                self.assertFalse(self.allowed())
        self.save_input(key, original)
        self.assertTrue(self.allowed())
        # An uncited source still entered the model prompt and can influence any
        # derived field: plan.citations is not a complete dependency manifest.
        other = self.archive('An unrelated private source.', channel='email')
        original['sources'].append(runtime._bounded_source(knowledge.open_source('message', other)))
        self.save_input(key, original)
        self.conn.execute("UPDATE email_messages SET metadata=metadata||'{\"historical\":true}'::jsonb WHERE id=%s",
                          (other,))
        self.assertFalse(self.allowed())

    def test_native_origin_and_current_responsibility_are_filtered(self):
        self.derived_practice()
        original_pid = self.pid
        self.delegate()
        self.conn.execute('UPDATE secretary_practices SET responsibility_id=%s WHERE id=%s',
                          (self.rid, original_pid))
        self.assertTrue(self.allowed(identity=original_pid))
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata||'{\"is_forwarded\":true}'::jsonb WHERE id=%s",
                          (self.origin,))
        self.assertFalse(self.allowed())
        self.assertFalse(self.allowed('responsibility', self.rid))
        self.assertFalse(self.allowed(identity=original_pid))

    def test_prepared_and_stopped_reviews_do_not_taint_owner_only_practice(self):
        key = self.derived_practice()
        for status in ('prepared', 'stopped'):
            self.conn.execute('UPDATE secretary_runtime_reviews SET status=%s WHERE operation_key=%s',
                              (status, key))
            self.consent('email', allowed=False)
            # Simulate an unapplied fixture: no runtime fields were committed.
            self.conn.execute("UPDATE secretary_practices SET next_action='Owner action',waiting_for='' WHERE id=%s",
                              (self.pid,))
            with self.subTest(status=status):
                self.assertTrue(self.allowed())

    def test_prior_and_current_responsibility_origins_remain_required(self):
        key = self.derived_practice()
        original_pid, original_rid = self.pid, self.rid
        self.delegate()
        self.conn.execute('UPDATE secretary_practices SET responsibility_id=%s WHERE id=%s',
                          (self.rid, original_pid))
        self.assertTrue(self.allowed(identity=original_pid))
        old_origin = self.conn.execute('SELECT origin_message FROM secretary_responsibilities WHERE id=%s',
                                       (original_rid,)).fetchone()[0]
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata||'{\"historical\":true}'::jsonb WHERE id=%s",
                          (old_origin,))
        self.assertFalse(self.allowed(identity=original_pid))
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed('responsibility', self.rid))
        self.assertIsNotNone(self.saved_input(key))


if __name__ == '__main__':
    unittest.main()