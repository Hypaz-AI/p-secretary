"""Offline runtime contracts and opt-in SQL tests. No production DB or HTTP.

SQL tests require PI_SECRETARY_TEST_DATABASE_URL with a database ending _test.
Every SQL case uses its own disposable schema, mocked brain and blocked HTTP.
"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import time
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
import pipeline
import policy
import secretary
import secretary_runtime as runtime


def source(mid=7, text='Il preventivo per il tetto è disponibile.'):
    return dict(entry_id=11, source_type='message', source_id=mid, message_id=mid,
        revision='a' * 64, channel='email', citation=f'message:{mid}@' + 'a' * 64,
        historical=False, current=True, status='ready', title='Tetto', text=text)


def plan(clock=None, action='review', sources=None):
    clock = clock or datetime.now(timezone.utc)
    evidence = (sources or [source()])[0]
    return dict(summary='Preventivo disponibile, da valutare.', evidence=evidence['text'],
        citations=[evidence['citation']], action=action, next_action='Valutare il preventivo',
        waiting_for='decisione del proprietario', next_review_at=(clock + timedelta(days=2)).isoformat(),
        draft='Bozza locale da verificare.' if action == 'draft' else '',
        question='Vuoi valutare questo preventivo?' if action == 'request_owner_decision' else '')


class OfflineRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.conn = self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_strict_plan_allowlist_grounding_and_future_window(self):
        clock = datetime.now(timezone.utc)
        valid = plan(clock)
        responsibility = dict(allowed_actions=['review', 'draft', 'request_owner_decision'])
        self.assertEqual(runtime._validate_plan(valid, [source()], responsibility, clock), valid)
        invalid = [dict(valid, action='send_email'), dict(valid, evidence='made up'),
                   dict(valid, citations=['message:unknown']), dict(valid, state='completed'),
                   dict(valid, next_review_at=clock.isoformat()), dict(valid, draft='Send now'),
                   dict(valid, question='Unrequested question'), dict(valid, next_review_at='2026-10-17')]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(secretary.InvalidPlan):
                runtime._validate_plan(item, [source()], responsibility, clock)

    def test_draft_and_owner_decision_require_delegation(self):
        clock = datetime.now(timezone.utc)
        for action in ('draft', 'request_owner_decision'):
            with self.subTest(action=action):
                candidate = plan(clock, action)
                runtime._validate_plan(candidate, [source()], dict(allowed_actions=['review', action]), clock)
                with self.assertRaises(secretary.InvalidPlan):
                    runtime._validate_plan(candidate, [source()], dict(allowed_actions=['review']), clock)

    def test_exact_scope_has_word_boundaries_not_sender_authority(self):
        self.assertTrue(runtime._exact_scope('tetto', 'Preventivo TETTO pronto'))
        self.assertFalse(runtime._exact_scope('tetto', 'Il tettoiaio risponde'))
        self.assertFalse(runtime._exact_scope('', 'tutto'))

    def test_quiet_time_defaults_and_environment_override(self):
        with patch.object(config, 'TIMEZONE', 'UTC'):
            late = datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc)
            self.assertEqual(runtime._quiet_until(late), datetime(2026, 10, 17, 8, tzinfo=timezone.utc))
            daytime = late.replace(hour=14)
            self.assertEqual(runtime._quiet_until(daytime), daytime)
            with patch.dict(os.environ, {'SECRETARY_REVIEW_QUIET_START':'0', 'SECRETARY_REVIEW_QUIET_END':'0'}):
                self.assertEqual(runtime._quiet_until(late), late)
            with patch.dict(os.environ, {'SECRETARY_REVIEW_ACK_MINUTES':'bad'}):
                self.assertEqual(runtime._integer('SECRETARY_REVIEW_ACK_MINUTES', 1440), 1440)

    def test_history_stops_before_loading_attachments_or_ai(self):
        with patch.object(policy, 'can_operate', return_value=False), \
             patch.object(pipeline, 'load_message') as load, patch.object(brain, 'analyze') as model:
            pipeline.analyze(dict(message_id=7))
        load.assert_not_called()
        model.assert_not_called()

    def test_ai_default_denied_before_media_or_model(self):
        self.conn.execute.return_value.fetchone.return_value = None
        def gate(conn, mid, effect, **kwargs):
            return effect != 'ai_text'
        with patch.object(policy, 'can_operate', side_effect=gate), \
             patch.object(pipeline, 'load_message', return_value=(('email','s','x','b','src'),[])), \
             patch('flows.enabled', return_value=True), patch.object(brain, 'analyze') as model:
            with self.assertRaises(jobs.Deferred):
                pipeline.analyze(dict(message_id=7))
        model.assert_not_called()

    def test_invoice_opt_in_and_sender_labels_never_auto_calendar(self):
        self.conn.execute.return_value.fetchone.return_value = None
        event = dict(title='X', dt='2099-01-01 10:00')
        verdict = dict(summary='generic result', category='altro', appointments=[event], is_invoice=True)
        with patch.object(policy, 'can_operate', return_value=True), \
             patch.object(pipeline, 'load_message', return_value=(('email','Trusted','x','b','src'),[dict()])), \
             patch('flows.enabled', return_value=True), patch.object(brain, 'analyze', return_value=verdict), \
             patch.object(pipeline.gcal, 'validate_event', return_value=event), \
             patch.object(pipeline, 'save_event') as save, patch.object(capabilities, 'enabled', return_value=False), \
             patch.object(jobs, 'enqueue') as enqueue, patch.object(runtime, 'observe'), \
             patch.dict(os.environ, {'CALENDAR_EXTERNAL_AUTO':'true', 'CALENDAR_TRUSTED_SENDERS':'Trusted'}):
            pipeline.analyze(dict(message_id=7))
        self.assertFalse(save.call_args.args[4])
        self.assertFalse(any(call.args[3].get('type') == 'invoice' for call in enqueue.call_args_list))

    def test_save_event_preserves_native_owner_key_not_source_label(self):
        self.conn.execute.return_value.fetchone.return_value = (4,)
        event = dict(title='X', dt='2099-01-01 10:00')
        with patch.object(policy, 'calendar_event_hash', return_value='sealed') as digest, \
             patch.object(policy, 'grant_calendar') as grant, patch.object(jobs, 'enqueue') as enqueue:
            pipeline.save_event(self.conn, event, 'message:7:0', 'owner label', False)
            enqueue.assert_not_called()
            grant.assert_not_called()
            # Natural-language approval passes the persisted intent flag; the
            # grant itself never calls a model.
            pipeline.save_event(self.conn, event, 'owner:9:event:0', 'dialogo utente',
                                calendar_intent=True)
            digest.assert_called_once_with(self.conn, 4)
            grant.assert_called_once_with(self.conn, 4, 'sealed', 9, calendar_intent=True)
            # Without the classified flag the natural path stays a local proposal.
            grant.reset_mock()
            pipeline.save_event(self.conn, event, 'owner:9:event:0', 'dialogo utente')
            grant.assert_not_called()

    def test_attempt_committed_before_http_and_uncertain_not_resent(self):
        trace = []
        self.conn.transaction.return_value.__exit__.side_effect = lambda *args: trace.append('commit')
        with patch.object(policy, 'prepare_effect', return_value=True), \
             patch.object(policy, 'can_operate', return_value=True), \
             patch.object(policy, 'record_receipt', side_effect=lambda c,k,s: trace.append(s) or True):
            with self.assertRaises(pipeline.EffectUncertain):
                pipeline._effect('notify:7', 7, 'notify', dict(text='x'), lambda: trace.append('http') or False)
        self.assertLess(trace.index('attempted'), trace.index('commit'))
        self.assertLess(trace.index('commit'), trace.index('http'))
        self.assertIn('uncertain', trace)
        self.conn.execute.return_value.fetchone.return_value = ('uncertain',)
        with patch.object(policy, 'prepare_effect', return_value=False), \
             patch.object(policy, 'record_receipt') as receipt:
            callback = MagicMock()
            with self.assertRaises(pipeline.EffectUncertain):
                pipeline._effect('notify:7', 7, 'notify', dict(text='x'), callback)
        callback.assert_not_called()
        receipt.assert_not_called()

    def test_accepted_receipt_replays_without_send(self):
        self.conn.execute.return_value.fetchone.return_value = ('accepted',)
        callback = MagicMock()
        with patch.object(policy, 'prepare_effect', return_value=False):
            self.assertTrue(pipeline._effect('notify:7', 7, 'notify', {}, callback))
        callback.assert_not_called()

    def test_deterministic_calendar_retry_uses_new_attempt_receipt(self):
        self.conn.execute.return_value.fetchone.return_value = ('uncertain',)
        with patch.object(policy, 'prepare_effect', side_effect=[False,True]) as prepare, \
             patch.object(policy, 'can_operate', return_value=True), \
             patch.object(policy, 'record_receipt', return_value=True), \
             patch.object(jobs, 'execution_context', return_value=None):
            callback = MagicMock(return_value=True)
            pipeline._effect('calendar:7', 7, 'calendar', {}, callback, deterministic=True)
        self.assertIn(':retry:', prepare.call_args.args[1])
        callback.assert_called_once()

    def test_policy_revoked_between_attempt_and_http(self):
        # Revocation is monotonic policy: the job fails permanently instead of
        # looping on Deferred; the attempt receipt stays 'uncertain' (no blind resend).
        with patch.object(policy, 'prepare_effect', return_value=True), \
             patch.object(policy, 'record_receipt', return_value=True) as receipt, \
             patch.object(policy, 'can_operate', return_value=False):
            callback = MagicMock()
            with self.assertRaises(jobs.Permanent):
                pipeline._effect('notify:7', 7, 'notify', {}, callback)
        callback.assert_not_called()
        self.assertEqual(receipt.call_args.args[-1], 'uncertain')

    def test_lost_lease_prevents_actions(self):
        with patch.object(jobs, 'assert_current', side_effect=jobs.LeaseLost()), \
             patch.object(pipeline.telegram, 'notify') as notify:
            with self.assertRaises(jobs.LeaseLost):
                pipeline.action(dict(type='notify', message_id=7, text='x'))
        notify.assert_not_called()

    def test_durable_owner_commands_preserve_fenced_coordinator(self):
        context = MagicMock(message_id=42)
        command = MagicMock(return_value=True)
        with patch.object(jobs, 'execution_context', return_value=context), \
             patch.object(jobs, 'assert_current'), \
             patch.dict(sys.modules, {'owner_dialogue': SimpleNamespace(command=command)}), \
             patch.object(pipeline.telegram, 'notify') as notify:
            self.assertTrue(pipeline.owner_commands('/fatto 7'))
        command.assert_called_once_with('/fatto 7', 42)
        notify.assert_not_called()

    def test_scheduler_calls_due_and_runtime_backlog_not_knowledge_fullscan(self):
        self.conn.execute.return_value.fetchall.return_value = []
        with patch.object(capabilities, 'enabled', return_value=False), \
             patch.object(secretary, 'due_reviews') as due, patch.object(runtime, 'backlog') as backlog, \
             patch.object(knowledge, 'backfill') as scan:
            pipeline.schedule()
        due.assert_called_once_with(self.conn)
        backlog.assert_called_once_with(self.conn)
        scan.assert_not_called()

    def test_no_arbitrary_worker_tools_and_bounded_sources(self):
        malicious = source()
        malicious.update(path='/private/key', data=b'secret', metadata={'token':'secret'}, documents=[])
        clean = runtime._bounded_source(malicious)
        self.assertFalse(set(clean) & {'path','data','metadata','documents'})
        with self.assertRaises(secretary.InvalidPlan):
            runtime._citation_list(['message:999'], [source()])


class OneOffRuntimeTests(unittest.TestCase):
    """Native owner provenance is required even without a responsibility row."""

    def setUp(self):
        self.conn = MagicMock()
        self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value = self.conn
        self.enterContext(patch.object(jobs, 'assert_current'))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.clock = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        self.enterContext(patch.object(runtime, '_now', return_value=self.clock))
        self.text = 'Crea una pratica per il tetto e riesamina al 2026-10-16T11:00:00+00:00.'
        self.practice = dict(id=1, version=1, state='active', responsibility_id=None,
            origin_message=7, next_review_at='2026-10-16T11:00:00+00:00')
        self.fetch = self.enterContext(patch.object(secretary, '_fetch', return_value=self.practice))
        self.trusted = self.enterContext(patch.object(secretary, '_trusted_message',
            side_effect=lambda *a: (self.text, self.clock - timedelta(days=1))))
        self.policy = self.enterContext(patch.object(policy, 'can_operate', return_value=True))
        self.change = dict(kind='practice', operation='create', id=None, expected_version=0,
            fields=dict(next_review_at=self.practice['next_review_at'], responsibility_id=None),
            evidence=self.text, operation_key='secretary:7:0')
        self.owner_plan = dict(text_hash=secretary.payload_hash(self.text),
            reference_time=(self.clock - timedelta(days=1)).isoformat(), changes=[self.change])
        self.receipt = self.enterContext(patch.object(secretary, '_receipt', return_value=('receipt',
            dict(kind='practice', id=1, version=1))))
        self.conn.execute.return_value.fetchone.side_effect = self.row
        self.payload = dict(practice_id=1, expected_version=1, reason='due', message_id=None)

    def row(self):
        return (7, self.owner_plan, self.change['operation_key'])

    def authorize(self, text):
        self.text = text
        self.owner_plan['text_hash'] = secretary.payload_hash(text)
        self.change['evidence'] = text
        return runtime._state(self.conn, self.payload)

    def test_due_owner_review_without_standing_responsibility(self):
        state = runtime._state(self.conn, self.payload)
        self.assertIsNone(state['responsibility'])
        self.assertEqual(state['authority']['allowed_actions'], ['review'])
        self.assertTrue(runtime._same_versions(state, deepcopy(state)))
        self.assertEqual(self.fetch.call_count, 1)  # No invented responsibility lookup.

    def test_oneoff_never_runs_initial_observation_candidate_or_future_date(self):
        for reason in ('initial', 'observation', 'candidate', 'unknown'):
            with self.subTest(reason=reason):
                self.assertIsNone(runtime._state(self.conn, dict(self.payload, reason=reason)))
        self.practice['next_review_at'] = (self.clock + timedelta(hours=1)).isoformat()
        self.assertIsNone(runtime._state(self.conn, self.payload))
        self.trusted.assert_not_called()

    def test_inactive_missing_or_revoked_authority_is_not_borrowed(self):
        for state in ('proposed', 'completed', 'cancelled'):
            self.practice['state'] = state
            self.assertIsNone(runtime._state(self.conn, self.payload))
        self.practice['state'] = 'active'
        self.policy.return_value = False
        self.assertIsNone(runtime._state(self.conn, self.payload))
        self.policy.return_value = True
        self.trusted.side_effect = secretary.Unauthorized('Forwarded or revoked')
        self.assertIsNone(runtime._state(self.conn, self.payload))

    def test_exact_applied_owner_plan_and_current_version_are_required(self):
        self.conn.execute.return_value.fetchone.side_effect = None
        self.conn.execute.return_value.fetchone.return_value = None
        self.assertIsNone(runtime._state(self.conn, self.payload))
        self.conn.execute.return_value.fetchone.return_value = self.row()
        self.receipt.return_value = None
        self.assertIsNone(runtime._state(self.conn, self.payload))
        self.receipt.return_value = ('receipt', dict(kind='practice', id=1, version=2))
        self.assertIsNone(runtime._state(self.conn, self.payload))

    def test_updated_authority_keeps_creation_origin_and_requires_both_consents(self):
        self.practice['version'] = self.payload['expected_version'] = 2
        self.change.update(operation='update', id=1, expected_version=1,
                           operation_key='secretary:8:0')
        self.text = self.text.replace('Crea una pratica', 'Aggiorna la pratica')
        self.change['evidence'] = self.text
        self.owner_plan['text_hash'] = secretary.payload_hash(self.text)
        self.conn.execute.return_value.fetchone.side_effect = lambda: (8, self.owner_plan, self.change['operation_key'])
        self.receipt.return_value = ('receipt', dict(kind='practice', id=1, version=2))
        state = runtime._state(self.conn, self.payload)
        self.assertEqual(state['authority']['message_id'], 8)
        self.assertEqual(state['practice']['origin_message'], 7)
        self.assertEqual({call.args[1] for call in self.trusted.call_args_list}, {7, 8})
        with patch.object(runtime, '_sources_current', return_value=True):
            for mid in (7, 8):
                with self.subTest(mid=mid):
                    self.policy.side_effect = lambda c, m, e, **kw: (m, e) != (mid, 'ai_text')
                    with self.assertRaises(jobs.Deferred):
                        runtime._ai_permission(self.conn, state, [])

    def test_quotes_questions_edits_and_ungrounded_times_cannot_authorize(self):
        for text in ('> ' + self.text, self.text.replace('Crea', 'Forse crea'),
                     self.text[:-1] + '?', self.text.replace('11:00', '10:00')):
            with self.subTest(text=text):
                self.assertIsNone(self.authorize(text))
        self.authorize(self.change['evidence'])
        self.owner_plan['text_hash'] = secretary.payload_hash('different native text')
        self.assertIsNone(runtime._state(self.conn, self.payload))

    def test_local_draft_decision_need_explicit_same_sentence_authority(self):
        authority = runtime._state(self.conn, self.payload)['authority']
        for action in ('review', 'draft', 'request_owner_decision', 'send_email'):
            candidate = dict(plan(self.clock, action), next_review_at=None)
            if action == 'review':
                runtime._validate_plan(candidate, [source()], None, self.clock, authority=authority)
            else:
                with self.assertRaises(secretary.InvalidPlan):
                    runtime._validate_plan(candidate, [source()], None, self.clock, authority=authority)
        text = self.text[:-1] + ' e prepara una bozza e chiedimi una decisione.'
        authority = self.authorize(text)['authority']
        self.assertEqual(authority['allowed_actions'], ['review', 'draft', 'request_owner_decision'])
        for action in ('draft', 'request_owner_decision'):
            runtime._validate_plan(dict(plan(self.clock, action), next_review_at=None), [source()],
                                   None, self.clock, authority=authority)
        with self.assertRaises(secretary.InvalidPlan):
            runtime._validate_plan(plan(self.clock), [source()], None, self.clock, authority=authority)
        with self.assertRaises(secretary.InvalidPlan):
            runtime._validate_plan(dict(plan(self.clock), next_review_at=None), [source()], None, self.clock)

    def test_oneoff_origin_and_every_source_require_ai_consent(self):
        state = runtime._state(self.conn, self.payload)
        for mid, effect in ((7, 'ai_text'), (9, 'ai_text'), (9, 'ai_media')):
            with self.subTest(mid=mid, effect=effect):
                self.policy.side_effect = lambda c, m, e, **kw: (m, e) != (mid, effect)
                document = dict(source(9), source_type='document')
                with patch.object(knowledge, '_snapshot', return_value=dict(revision='a' * 64)):
                    self.conn.execute.return_value.fetchone.side_effect = None
                    self.conn.execute.return_value.fetchone.return_value = ('a' * 64,)
                    with self.assertRaises(jobs.Deferred):
                        runtime._ai_permission(self.conn, state, [document])

    def test_oneoff_sources_never_include_candidate_proposals(self):
        self.conn.execute.return_value.fetchall.return_value = [(7,), (9,)]
        self.assertEqual(runtime._source_ids(self.conn, 1, None), [7, 9])
        sql = '\n'.join(c.args[0] for c in self.conn.execute.call_args_list)
        self.assertNotIn('secretary_runtime_link_proposals', sql)


class RuntimeDurabilityTests(unittest.TestCase):
    """Exercise the real observation state machine with a small SQL inbox double."""

    def setUp(self):
        self.conn = self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch.object(jobs, 'assert_current'))
        self.enterContext(patch.object(runtime, '_native_allowed', return_value=True))
        self.state = dict(practice=dict(id=1, version=1, origin_message=7, objective='Riparare il tetto'),
            responsibility=dict(id=2, version=1, origin_message=7, scope=dict(topic='tetto'),
                                allowed_actions=['review']))
        self.current = self.enterContext(patch.object(runtime, '_state', return_value=self.state))
        self.enterContext(patch.object(runtime, '_candidate_rows', return_value=[self.state]))
        self.sources = [source(text='Preventivo per la copertura disponibile.')]
        self.enterContext(patch.object(runtime, '_open_sources', return_value=self.sources))
        self.permission = self.enterContext(patch.object(runtime, '_ai_permission'))
        self.fresh = self.enterContext(patch.object(runtime, '_sources_current', return_value=True))
        self.queue = self.enterContext(patch.object(secretary, '_queue_review'))
        self.raw = dict(links=[dict(practice_id=1, evidence=self.sources[0]['text'])])
        self.chat = self.enterContext(patch.object(brain, '_chat', side_effect=lambda *a, **k: json.dumps(self.raw)))
        self.inbox = dict(status='pending', input=None, plan=None)
        self.insertions = []
        self.insert_result = (1,)
        self.conn.execute.side_effect = self.execute
        self.payload = dict(observation_message_id=7, operation_key='secretary-observe:7')

    def execute(self, sql, args=None):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        if 'SELECT status,input,plan FROM secretary_runtime_observations' in sql:
            cursor.fetchone.return_value = tuple(deepcopy(self.inbox[k]) for k in ('status', 'input', 'plan'))
        elif 'SELECT input,plan FROM secretary_runtime_observations' in sql:
            cursor.fetchone.return_value = (deepcopy(self.inbox['input']), deepcopy(self.inbox['plan']))
        elif 'UPDATE secretary_runtime_observations SET input=' in sql:
            if self.inbox['status'] == 'pending' and self.inbox['plan'] is None:
                self.inbox.update(input=json.loads(args[0]), plan=json.loads(args[1]))
        elif 'UPDATE secretary_runtime_observations SET status=' in sql:
            self.inbox['status'] = 'stopped' if "status='stopped'" in sql else 'done'
        elif 'INSERT INTO secretary_runtime_link_proposals' in sql:
            self.insertions.append(args)
            cursor.fetchone.return_value = self.insert_result
        return cursor

    def test_no_live_sources_persists_terminal_status(self):
        with patch.object(runtime, '_open_sources', return_value=[]):
            self.assertEqual(runtime.review(self.payload)['status'], 'stopped')
        self.assertEqual(self.inbox['status'], 'stopped')
        self.chat.assert_not_called()

    def test_saved_source_change_stops_on_first_application_not_only_retry(self):
        self.fresh.return_value = False
        result = runtime.review(self.payload)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(self.inbox['status'], 'stopped')
        self.assertIsNotNone(self.inbox['plan'])
        self.assertEqual(self.insertions, [])

    def test_concurrent_saved_plan_uses_its_own_source_and_revision(self):
        persisted = source(text='Una diversa revisione approvabile.')
        persisted.update(entry_id=99, revision='b' * 64)
        original = self.execute
        def execute(sql, args=None):
            if 'UPDATE secretary_runtime_observations SET input=' in sql:
                self.inbox.update(input=dict(sources=[persisted], candidates=[self.state]),
                    plan=dict(links=[dict(practice_id=1, evidence=persisted['text'])]))
            return original(sql, args)
        self.conn.execute.side_effect = execute
        self.assertEqual(runtime.review(self.payload)['proposed_links'], 1)
        self.assertEqual(self.insertions[0][5:8], (99, 'b' * 64, persisted['text']))

    def test_terminal_concurrent_observation_is_not_applied_or_overwritten(self):
        self.sources[0]['text'] = 'tetto'
        def changed(conn, source):
            self.inbox['status'] = 'stopped'
            return [self.state]
        with patch.object(runtime, '_candidate_rows', side_effect=changed):
            self.assertEqual(runtime.review(self.payload)['status'], 'stopped')
        self.assertEqual(self.inbox['status'], 'stopped')
        self.assertEqual(self.insertions, [])

    def test_revocation_during_inference_prevents_persisting_plan(self):
        def model(*args, **kwargs):
            self.current.return_value = None
            return json.dumps(self.raw)
        self.chat.side_effect = model
        self.assertEqual(runtime.review(self.payload)['status'], 'stopped')
        self.assertIsNone(self.inbox['plan'])
        self.assertEqual(self.inbox['status'], 'stopped')

    def test_proposed_count_counts_committed_insertions_not_model_candidates(self):
        self.sources[0]['text'] = 'tetto'
        self.insert_result = None
        self.assertEqual(runtime.review(self.payload)['proposed_links'], 0)
        self.queue.assert_not_called()

    def test_reused_empty_model_plan_still_checks_data_policy(self):
        self.inbox.update(input=dict(sources=self.sources, candidates=[self.state]), plan=dict(links=[]))
        self.permission.side_effect = jobs.Deferred('Revoked')
        with self.assertRaises(jobs.Deferred):
            runtime.review(self.payload)
        self.assertEqual(self.inbox['status'], 'pending')
        self.chat.assert_not_called()

    def test_model_plan_replay_does_not_call_model_again(self):
        self.inbox.update(input=dict(sources=self.sources, candidates=[self.state]), plan=self.raw)
        self.assertEqual(runtime.review(self.payload)['proposed_links'], 1)
        self.chat.assert_not_called()


class RuntimeFenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value = self.conn
        self.enterContext(patch.object(jobs, 'assert_current'))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_backlog_total_bound_and_missing_prepared_job_recovery(self):
        self.conn.execute.return_value.fetchall.return_value = []
        runtime.backlog(self.conn, 2)
        statements = [c.args[0] for c in self.conn.execute.call_args_list]
        self.assertTrue(any('secretary_runtime_reviews' in sql and "'prepared'" in sql for sql in statements))
        self.conn.execute.reset_mock()
        self.conn.execute.return_value.fetchall.return_value = [(7,), (8,)]
        with patch.object(runtime, '_queue_observation', return_value=(1,)), patch.object(jobs, 'enqueue') as enqueue:
            self.assertEqual(runtime.backlog(self.conn, 2), 2)
        enqueue.assert_not_called()

    def test_recovery_stop_and_enqueue_roll_back_when_fence_is_lost(self):
        clock = datetime.now(timezone.utc)
        state = dict(practice=dict(id=1, version=1),
                     responsibility=dict(id=2, version=1, allowed_actions=['review']))
        prepared = dict(state=state, sources=[source()], prepared_at=clock.isoformat())
        candidate = plan(clock)
        self.conn.execute.return_value.fetchall.return_value = [
            ('old:due', prepared, candidate, 'prepared', None, 1, 1)]
        with patch.object(runtime, '_state', return_value=state), \
             patch.object(runtime, '_sources_current', return_value=True), \
             patch.object(jobs, 'enqueue', return_value=(1,)) as enqueue, \
             patch.object(jobs, 'assert_current', side_effect=[True, jobs.LeaseLost()]):
            with self.assertRaises(jobs.LeaseLost):
                runtime._recover_expired_reviews(self.conn, 1)
        enqueue.assert_called_once()
        self.assertIs(self.conn.transaction.return_value.__exit__.call_args.args[0], jobs.LeaseLost)

    def test_runtime_upgrade_supports_existing_owner_accept_link_contract(self):
        self.conn.execute.return_value.fetchone.side_effect = [(1,), None]
        self.assertTrue(runtime.migrate(self.conn))
        sql = '\n'.join(c.args[0] for c in self.conn.execute.call_args_list)
        self.assertIn('ADD COLUMN IF NOT EXISTS version', sql)
        self.assertIn("'accepted'", sql)
        self.assertNotIn('CREATE TABLE secretary_runtime_reviews', sql)

    def test_missing_or_revoked_notification_origin_is_terminal(self):
        payload = dict(practice_id=1, practice_version=2, responsibility_id=3, responsibility_version=1,
                       attention_hash='hash')
        p = dict(id=1, version=2, state='active', origin_message=7, responsibility_id=3)
        r = dict(id=3, version=1, state='active', origin_message=8, allowed_actions=['review'])
        for error in (secretary.Conflict('Missing'), secretary.Unauthorized('Revoked')):
            with patch.object(secretary, '_fetch', side_effect=error), self.assertRaises(runtime.ReviewStopped):
                runtime.assert_notification_current(payload)
        with patch.object(secretary, '_fetch', side_effect=[p, r]), \
             patch.object(secretary, '_trusted_message'), patch.object(policy, 'can_operate', return_value=False), \
             self.assertRaises(runtime.ReviewStopped):
            runtime.assert_notification_current(payload)

    def test_unchanged_but_unsent_notification_survives_new_review_version(self):
        proposed = plan(action='draft')
        state = dict(practice=dict(id=1, origin_message=7), responsibility=dict(id=2, version=1))
        digest = secretary.payload_hash({k: proposed[k] for k in ('action', 'question', 'draft', 'next_action')})
        previous = dict(type='notify', secretary_review=True, practice_id=1, message_id=7,
                        attention_hash=digest, review_operation_key='review:old')
        for receipt, expected in ((None, 1), ('authorized', 1), ('blocked', 1),
                                  ('accepted', 0), ('verified', 0), ('uncertain', 0), ('attempted', 0)):
            def execute(sql, args=None):
                cursor = MagicMock()
                if 'FROM secretary_runtime_attention' in sql:
                    cursor.fetchone.return_value = (digest, datetime.now(timezone.utc), None)
                elif 'FROM work_jobs' in sql:
                    cursor.fetchone.return_value = (12, previous)
                else:
                    cursor.fetchone.return_value = (1,) if receipt in (
                        'attempted', 'uncertain', 'accepted', 'verified') else None
                return cursor
            self.conn.execute.side_effect = execute
            with self.subTest(receipt=receipt), patch.object(jobs, 'enqueue', return_value=(1,)) as enqueue:
                runtime._notify_owner(self.conn, state, 3, 'review:new', proposed)
                self.assertEqual(enqueue.call_count, expected)
                lookup = next(c for c in self.conn.execute.call_args_list if 'FROM work_jobs' in c.args[0])
                self.assertEqual(json.loads(lookup.args[1][0]), dict(type='notify', secretary_review=True,
                    practice_id=1, message_id=7, attention_hash=digest))
                effect = next(c for c in self.conn.execute.call_args_list if 'FROM effect_receipts' in c.args[0])
                self.assertEqual(effect.args[1], (7, 'notification-job:12',
                    'notification:' + jobs.event_id(json.dumps(previous, sort_keys=True))))
            self.conn.execute.reset_mock()

    def test_acknowledged_attention_never_reads_receipts_or_requeues(self):
        proposed = plan(action='draft')
        state = dict(practice=dict(id=1, origin_message=7), responsibility=dict(id=2, version=1))
        digest = secretary.payload_hash({k: proposed[k] for k in ('action', 'question', 'draft', 'next_action')})
        clock = datetime.now(timezone.utc)
        self.conn.execute.return_value.fetchone.return_value = (digest, clock, clock)
        with patch.object(jobs, 'enqueue') as enqueue:
            runtime._notify_owner(self.conn, state, 3, 'review:new', proposed)
        enqueue.assert_not_called()
        self.assertEqual(self.conn.execute.call_count, 1)


class RuntimePostgresTests(unittest.TestCase):
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
        self.schema = 'runtime_test_' + uuid.uuid4().hex
        self.conn = db.psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.assertTrue(self.conn.execute('SELECT current_database()').fetchone()[0].endswith('_test'))
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='15s'")
        self.conn.execute("SET lock_timeout='10s'")
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)
        from connectors import media_archive
        import owner_dialogue
        import task_reconciliation
        media_archive.migrate(self.conn)
        task_reconciliation.migrate(self.conn)
        owner_dialogue.migrate(self.conn)
        policy.migrate(self.conn)
        capabilities.migrate(self.conn, legacy=False)
        secretary.migrate(self.conn)
        knowledge.migrate(self.conn)
        runtime.migrate(self.conn)
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:offline'))
        self.enterContext(patch.object(config, 'TIMEZONE', 'UTC'))
        self.enterContext(patch.dict(os.environ, {'SECRETARY_REVIEW_QUIET_START':'0','SECRETARY_REVIEW_QUIET_END':'0'}))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.chat = self.enterContext(patch.object(brain, '_chat', side_effect=AssertionError('Unexpected AI')))

    @contextmanager
    def connection(self):
        yield self.conn

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def archive(self, text='Segui il tetto.', channel='telegram', historical=False):
        message_id = int(uuid.uuid4().hex[:8], 16) + 1
        meta = (dict(account='bot:777', chat_id='101', author=101, direction='in', chat_type='private',
            content_origin='direct_text', is_forwarded=False, message_id=message_id, historical=historical)
            if channel == 'telegram' else dict(historical=historical, direction='in'))
        source_id = json.dumps(['bot','777','101',message_id]) if channel == 'telegram' else 'email-' + uuid.uuid4().hex
        # These tests explicitly index/observe; live ingress is tested elsewhere.
        # Avoid incidental jobs masking the runtime's own queue/count contracts.
        with patch.object(message_store, 'enqueue_derived'):
            mid = message_store.store(self.conn, channel, 'A sender label', 'tetto', text,
                ts=datetime.now(timezone.utc).isoformat(), source_id=source_id,
                metadata=meta, historical=historical)
        knowledge.index_message(dict(message_id=mid))
        return mid

    def delegate(self):
        self.origin = self.archive()
        r = self.conn.execute('''INSERT INTO secretary_responsibilities
            (type,objective,scope,triggers,allowed_actions,stop_condition,state,origin_message,operation_key)
            VALUES ('monitoring','Seguire il tetto',%s,%s,%s,'Smetti alla revoca','active',%s,%s)
            RETURNING id''', (json.dumps(dict(topic='tetto',channels=['email','telegram'])),
                json.dumps(['linked_message','review_due']), json.dumps(['review','draft','request_owner_decision']),
                self.origin, uuid.uuid4().hex)).fetchone()[0]
        self.rid = r
        self.pid = self.conn.execute('''INSERT INTO secretary_practices
            (objective,state,next_action,waiting_for,blocked_reason,next_review_at,completion_criteria,
             responsibility_id,origin_message,operation_key)
            VALUES ('Riparare il tetto','active','Valutare i preventivi','','',%s,'Conferma owner',%s,%s,%s)
            RETURNING id''', (datetime.now(timezone.utc)-timedelta(hours=1), r, self.origin, uuid.uuid4().hex)).fetchone()[0]
        self.conn.execute('''INSERT INTO secretary_links(practice_id,telegram_message_id,evidence,origin_message)
            VALUES (%s,%s,'Segui il tetto.',%s)''', (self.pid,self.origin,self.origin))
        return self.pid

    def owner_plan(self, text, message_id, changes):
        """Prepare real v2 provenance without AI or persistent data consent."""
        # Simulate an already-prepared owner request, then restore the exact
        # policy state so runtime default-deny/revocation tests stay meaningful.
        # Native sources and exported snapshot versions are resolved by prepare,
        # not invented by the fixture or supplied by the deterministic model.
        with self.conn.transaction(force_rollback=True):
            self.consent()
            with patch.object(brain, '_chat', return_value=json.dumps(
                    dict(changes=changes, clarification=''))):
                return secretary.prepare(text, message_id)

    def one_off(self, action='review', state='active'):
        from test_secretary import change, practice_fields
        due = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        extra = {'review': '', 'draft': ' e prepara una bozza',
                 'request_owner_decision': ' e chiedimi una decisione'}[action]
        text = f'Crea una pratica per il tetto e riesamina al {due.isoformat()}{extra}.'
        self.origin = self.archive(text)
        item = change(fields=practice_fields(objective='Riparare il tetto', state=state,
            next_review_at=due.isoformat(), waiting_for='Preventivo' if state == 'waiting' else '',
            blocked_reason='Manca il preventivo' if state == 'blocked' else ''),
            evidence=text)
        owner_plan = self.owner_plan(text, self.origin, [item])
        secretary.apply(self.conn, owner_plan, self.origin)
        self.pid = self.conn.execute('SELECT id FROM secretary_practices WHERE origin_message=%s',
                                     (self.origin,)).fetchone()[0]
        return self.pid

    def consent(self, channel='telegram', effect='ai_text', allowed=True):
        self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed) VALUES (%s,%s,%s)
            ON CONFLICT(channel,effect) DO UPDATE SET allowed=excluded.allowed,version=data_policies.version+1''',
            (channel,effect,allowed))

    def wake(self):
        return secretary.due_reviews(self.conn)[0]

    def respond_with_plan(self, action='review', hook=None, *, draft=None):
        def model(system, user, **kwargs):
            from psycopg.pq import TransactionStatus
            self.assertEqual(self.conn.info.transaction_status, TransactionStatus.IDLE)
            data = json.loads(user)
            if hook:
                hook(data)
            clock = datetime.fromisoformat(data['now'])
            result = plan(clock, action, data['sources'])
            if draft is not None:
                result['draft'] = draft
            if data.get('mode') == 'one_off':
                result['next_review_at'] = None
            return json.dumps(result)
        return patch.object(brain, '_chat', side_effect=model)

    @contextmanager
    def notification_execution(self, identity, payload):
        """Synthetic live lease, with real SQL fencing and worker effect identity."""
        token = uuid.uuid4().hex
        self.conn.execute("""UPDATE work_jobs SET status='running',token=%s,
            lease_until=now()+interval '15 minutes' WHERE id=%s""", (token, identity))
        context = jobs.ExecutionContext(identity, token, 'action', payload, time.monotonic() + 60)
        with patch.object(jobs._local, 'execution', context, create=True):
            yield

    def test_oneoff_due_review_local_notification_and_no_recurring_delegation(self):
        self.one_off()
        self.consent()
        initial = self.conn.execute("SELECT payload FROM work_jobs WHERE key LIKE %s",
            (f'secretary-review:{self.pid}:%:initial',)).fetchone()[0]
        self.assertEqual(runtime.review(initial)['status'], 'stopped')
        self.chat.assert_not_called()
        wake = self.wake()
        with self.respond_with_plan() as model:
            result = runtime.review(wake)
        data = json.loads(model.call_args.args[1])
        self.assertEqual(data['mode'], 'one_off')
        self.assertIsNone(data['responsibility'])
        self.assertEqual(data['authorization']['allowed_actions'], ['review'])
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.conn.execute('SELECT version,state,next_review_at,responsibility_id '
            'FROM secretary_practices').fetchone(), (2, 'active', None, None))
        self.assertEqual(self.conn.execute('SELECT responsibility_id,responsibility_version '
            'FROM secretary_runtime_reviews').fetchone(), (None, None))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_responsibilities').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_approvals').fetchone()[0], 0)
        self.assertEqual(runtime.review(wake), result)
        self.assertEqual(secretary.due_reviews(self.conn, now=datetime.now(timezone.utc) + timedelta(days=365)), [])
        notification = self.conn.execute("SELECT payload FROM work_jobs WHERE kind='action'").fetchone()[0]
        self.assertIsNone(notification['responsibility_id'])
        runtime.assert_notification_current(notification)
        with patch.object(pipeline.telegram, 'notify', return_value=True) as notify:
            pipeline.action(notification)
            pipeline.action(notification)
        notify.assert_called_once()
        self.assertIn('nessuna delega continuativa', notify.call_args.args[0])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action'").fetchone()[0], 1)

    def test_oneoff_second_message_reschedules_current_version_and_executes_once(self):
        from test_secretary import change
        self.one_off(state='waiting')
        creation_origin = self.origin
        old_wake = self.wake()
        due = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
        text = f'Aggiorna la pratica per il tetto e riesamina al {due.isoformat()}.'
        authority_mid = self.archive(text)
        item = change(operation='update', identity=self.pid, version=1,
            fields=dict(next_review_at=due.isoformat()), evidence=text)
        owner_plan = self.owner_plan(text, authority_mid, [item])
        self.assertEqual(owner_plan['schema_version'], 2)
        self.assertEqual(owner_plan['input_versions'], [
            dict(source_type='practice', source_id=self.pid, version=1)])
        self.assertEqual({s['source_id'] for s in owner_plan['input_sources']},
                         {creation_origin, authority_mid})
        secretary.apply(self.conn, owner_plan, authority_mid)
        self.consent()
        self.assertEqual(runtime.review(old_wake)['status'], 'stopped')
        wake = self.wake()
        self.assertEqual(wake['expected_version'], 2)
        with self.respond_with_plan() as model:
            result = runtime.review(wake)
            self.assertEqual(runtime.review(wake), result)
        model.assert_called_once()
        self.assertEqual(result['version'], 3)
        prepared = self.conn.execute('SELECT input FROM secretary_runtime_reviews').fetchone()[0]
        self.assertEqual(prepared['state']['authority']['message_id'], authority_mid)
        self.assertEqual(prepared['state']['authority']['operation_key'],
                         owner_plan['changes'][0]['operation_key'])
        self.assertEqual(prepared['state']['practice']['origin_message'], creation_origin)
        self.assertEqual(self.conn.execute('SELECT origin_message,version,responsibility_id '
            'FROM secretary_practices WHERE id=%s', (self.pid,)).fetchone(), (creation_origin, 3, None))
        consumed_wake = self.wake()  # Waiting retains a timestamp, not permission.
        self.assertEqual(runtime.review(consumed_wake)['status'], 'stopped')
        self.assertEqual(secretary.due_reviews(self.conn), [])
        self.assertEqual(runtime.backlog(self.conn), 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_responsibilities').fetchone()[0], 0)
        notification = self.conn.execute("SELECT payload FROM work_jobs WHERE kind='action'").fetchone()[0]
        self.assertEqual(notification['message_id'], creation_origin)
        runtime.assert_notification_current(notification)
        for mid in (creation_origin, authority_mid):
            with self.subTest(revoked=mid):
                self.conn.execute("UPDATE telegram_messages SET metadata=metadata || "
                    "'{\"is_forwarded\":true}'::jsonb WHERE id=%s", (mid,))
                with self.assertRaises(runtime.ReviewStopped):
                    runtime.assert_notification_current(notification)
                self.conn.execute("UPDATE telegram_messages SET metadata=metadata || "
                    "'{\"is_forwarded\":false}'::jsonb WHERE id=%s", (mid,))
        with patch.object(pipeline.telegram, 'notify', return_value=True) as notify:
            pipeline.action(notification)
            pipeline.action(notification)
        notify.assert_called_once()

    def test_oneoff_prepared_plan_replay_waiting_schema_and_bounded_followup(self):
        self.one_off(state='waiting')
        self.consent()
        wake = self.wake()
        with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                runtime.review(wake)
        result = runtime.review(wake)  # Default mock forbids fresh inference.
        self.assertEqual(runtime.review(wake), result)
        self.assertEqual(self.conn.execute('SELECT version,state FROM secretary_practices').fetchone(), (2, 'waiting'))
        extra = secretary.due_reviews(self.conn)
        self.assertEqual(len(extra), 1)  # Waiting requires its owner timestamp by schema.
        self.assertEqual(runtime.review(extra[0])['status'], 'stopped')
        self.assertEqual(secretary.due_reviews(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action'").fetchone()[0], 1)

    def test_oneoff_source_consent_revocation_before_inference_and_replay(self):
        self.one_off()
        wake = self.wake()
        with self.assertRaises(jobs.Deferred):
            runtime.review(wake)
        self.chat.assert_not_called()
        mid = self.archive('Preventivo per il tetto, con dati privati.', channel='email')
        self.conn.execute('INSERT INTO secretary_links(practice_id,email_message_id,evidence,origin_message) '
            'VALUES (%s,%s,%s,%s)', (self.pid, mid, 'Preventivo', self.origin))
        self.consent()
        with self.assertRaises(jobs.Deferred):
            runtime.review(wake)
        self.chat.assert_not_called()
        self.consent('email')
        with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                runtime.review(wake)
        self.consent('email', allowed=False)
        with self.assertRaises(jobs.Deferred):
            runtime.review(wake)
        self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices').fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action'").fetchone()[0], 0)

    def test_oneoff_prepared_authority_revoked_or_native_source_changed_stops(self):
        for revoke in ('cancelled', 'completed', 'forwarded', 'edited'):
            with self.subTest(revoke=revoke):
                self.one_off()
                self.consent()
                wake = next(w for w in secretary.due_reviews(self.conn) if w['practice_id'] == self.pid)
                with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
                    with self.assertRaises(RuntimeError):
                        runtime.review(wake)
                if revoke in ('cancelled', 'completed'):
                    self.conn.execute('UPDATE secretary_practices SET state=%s,next_review_at=NULL,version=version+1 '
                                      'WHERE id=%s', (revoke, self.pid))
                elif revoke == 'forwarded':
                    self.conn.execute("UPDATE telegram_messages SET metadata=metadata || '{\"is_forwarded\":true}'::jsonb WHERE id=%s",
                                      (self.origin,))
                else:
                    self.conn.execute('UPDATE telegram_messages SET body=body||%s WHERE id=%s', (' Changed.', self.origin))
                self.assertEqual(runtime.review(wake)['status'], 'stopped')

    def test_oneoff_only_explicit_draft_or_decision_no_external_approval(self):
        for action in ('draft', 'request_owner_decision'):
            with self.subTest(action=action):
                self.one_off(action)
                self.consent()
                wake = next(w for w in secretary.due_reviews(self.conn) if w['practice_id'] == self.pid)
                with self.respond_with_plan(action):
                    result = runtime.review(wake)
                self.assertEqual(result['action'], action)
                self.assertFalse(result['external_sends_supported'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_approvals').fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT DISTINCT payload->>'type' FROM work_jobs WHERE kind='action'").fetchall(), [('notify',)])

    def test_oneoff_rejects_escalated_actions_and_model_recurring_dates(self):
        self.one_off()
        self.consent()
        wake = self.wake()
        for action in ('draft', 'request_owner_decision', 'send_email', 'review'):
            with self.subTest(action=action):
                def model(system, user, **kwargs):
                    data = json.loads(user)
                    candidate = plan(datetime.fromisoformat(data['now']), action, data['sources'])
                    if action != 'review':
                        candidate['next_review_at'] = None
                    return json.dumps(candidate)
                with patch.object(brain, '_chat', side_effect=model), self.assertRaises(secretary.InvalidPlan):
                    runtime.review(wake)
                self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices').fetchone()[0], 1)
                self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_runtime_reviews').fetchone()[0], 0)
                self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action'").fetchone()[0], 0)

    def test_oneoff_blocked_review_preserves_state_and_prepared_plan_expires(self):
        self.one_off(state='blocked')
        self.consent()
        wake = self.wake()
        with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                runtime.review(wake)
        with patch.object(runtime, '_now', return_value=datetime.now(timezone.utc) + timedelta(days=31)):
            self.assertEqual(runtime.review(wake), dict(status='stopped', reason='prepared_review_expired'))
        self.assertEqual(self.conn.execute('SELECT version,state FROM secretary_practices').fetchone(), (1, 'blocked'))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action'").fetchone()[0], 0)
        with patch.object(runtime, '_now', return_value=datetime.now(timezone.utc) + timedelta(days=31)):
            self.assertEqual(runtime.backlog(self.conn), 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_wakeups').fetchone()[0], 2)

    def test_oneoff_notification_stops_on_ack_native_revocation_and_expiry(self):
        self.one_off()
        self.consent()
        with self.respond_with_plan():
            runtime.review(self.wake())
        notification = self.conn.execute("SELECT payload FROM work_jobs WHERE kind='action'").fetchone()[0]
        runtime.assert_notification_current(notification)
        with patch.object(runtime, '_now', return_value=datetime.now(timezone.utc) + timedelta(days=31)), \
             self.assertRaises(runtime.ReviewStopped):
            runtime.assert_notification_current(notification)
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata || '{\"is_forwarded\":true}'::jsonb WHERE id=%s", (self.origin,))
        with self.assertRaises(runtime.ReviewStopped):
            runtime.assert_notification_current(notification)
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata || '{\"is_forwarded\":false}'::jsonb WHERE id=%s", (self.origin,))
        runtime.acknowledge(self.conn, self.pid, authority_message_id=self.origin)
        with patch.object(pipeline.telegram, 'notify') as notify:
            pipeline.action(notification)
        notify.assert_not_called()

    def test_additive_migration_idempotent_no_policy_grants(self):
        self.assertFalse(runtime.migrate(self.conn))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT reset_rows FROM schema_migrations WHERE version=%s',
                                           (runtime.VERSION,)).fetchone(), (0,))

    def test_runtime_v1_upgrade_preserves_rows_and_enables_owner_link_acceptance(self):
        self.delegate()
        mid = self.archive('Il preventivo per il tetto è pronto.', channel='email')
        runtime.observe(self.conn, mid)
        runtime.review(dict(observation_message_id=mid, operation_key=f'secretary-observe:{mid}'))
        proposal = self.conn.execute('SELECT id FROM secretary_runtime_link_proposals').fetchone()[0]
        self.conn.execute('DELETE FROM schema_migrations WHERE version=%s', (runtime.VERSION,))
        self.conn.execute('INSERT INTO schema_migrations(version,reset_rows) VALUES (%s,0)', (runtime.BASE_VERSION,))
        self.conn.execute('''ALTER TABLE secretary_runtime_link_proposals DROP COLUMN version;
            ALTER TABLE secretary_runtime_link_proposals DROP CONSTRAINT secretary_runtime_link_proposals_state_check;
            ALTER TABLE secretary_runtime_link_proposals ADD CONSTRAINT secretary_runtime_link_proposals_state_check
            CHECK(state='proposed')''')
        self.assertTrue(runtime.migrate(self.conn))
        self.assertFalse(runtime.migrate(self.conn))
        result = secretary.accept_link(self.conn, proposal_id=proposal, expected_version=1,
            caller=secretary.DashboardOwner(owner_id='101', authenticated=True, csrf_verified=True))
        self.assertEqual(result['message_id'], mid)
        self.assertEqual(self.conn.execute('SELECT state,version FROM secretary_runtime_link_proposals').fetchone(), ('accepted', 2))

    def test_review_ai_default_deny_then_persist_apply_and_replay(self):
        self.delegate()
        wake = self.wake()
        with self.assertRaises(jobs.Deferred):
            runtime.review(wake)
        self.chat.assert_not_called()
        self.consent()
        with self.respond_with_plan():
            result = runtime.review(wake)
        self.assertEqual(result['status'], 'applied')
        self.assertFalse(result['external_sends_supported'])
        self.assertEqual(runtime.review(wake), result)
        self.assertEqual(self.conn.execute('SELECT version,state FROM secretary_practices').fetchone(), (2,'active'))
        self.assertEqual(self.conn.execute('SELECT status FROM secretary_runtime_reviews').fetchone(), ('applied',))
        self.assertEqual(secretary.due_reviews(self.conn), [])

    def test_durable_prepared_plan_reused_after_crash(self):
        self.delegate()
        self.consent()
        wake = self.wake()
        with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash after prepare')):
            with self.assertRaises(RuntimeError):
                runtime.review(wake)
        self.assertEqual(self.conn.execute('SELECT status FROM secretary_runtime_reviews').fetchone(), ('prepared',))
        result = runtime.review(wake)  # unexpected AI is blocked by setUp
        self.assertEqual(result['status'], 'applied')

    def test_expired_recurring_due_plan_recovers_after_outage_without_ingress(self):
        for replay_before_scheduler in (False, True):
            with self.subTest(replay_before_scheduler=replay_before_scheduler):
                self.delegate()
                self.consent()
                wake = next(w for w in secretary.due_reviews(self.conn) if w['practice_id'] == self.pid)
                with self.respond_with_plan('draft'), \
                     patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash before apply')):
                    with self.assertRaises(RuntimeError):
                        runtime.review(wake)
                prior = self.conn.execute('SELECT input,plan FROM secretary_runtime_reviews '
                    'WHERE operation_key=%s', (wake['operation_key'],)).fetchone()
                future = datetime.fromisoformat(prior[1]['next_review_at']) + timedelta(hours=1)
                with patch.object(runtime, '_now', return_value=future), \
                     patch.object(capabilities, 'enabled', return_value=False):
                    if replay_before_scheduler:
                        stopped = runtime.review(wake)
                        self.assertEqual(stopped['reason'], 'prepared_review_expired')
                    pipeline.schedule()
                    pipeline.schedule()
                    recovery_key = runtime._expired_review_result(wake['operation_key'], True)['recovery_operation_key']
                    recovery = self.conn.execute('SELECT payload FROM work_jobs WHERE key=%s',
                                                  (recovery_key,)).fetchone()[0]
                    self.assertEqual(recovery, dict(wake, operation_key=recovery_key))
                    self.assertEqual(self.conn.execute('SELECT reason,message_id FROM secretary_wakeups '
                        'WHERE operation_key=%s', (recovery_key,)).fetchone(), ('due', None))
                    self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices '
                        'WHERE id=%s', (self.pid,)).fetchone()[0], 1)
                    self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action' "
                        "AND payload->>'practice_id'=%s", (str(self.pid),)).fetchone()[0], 0)
                    self.assertEqual(runtime.review(wake)['reason'], 'prepared_review_expired')
                    # Only the fresh operation may infer/apply; the old plan stays stopped.
                    with self.respond_with_plan('draft') as model:
                        result = runtime.review(recovery)
                        self.assertEqual(runtime.review(recovery), result)
                    model.assert_called_once()
                    self.assertEqual(result['version'], 2)
                    pipeline.schedule()
                    self.assertEqual(runtime.backlog(self.conn, 1), 0)
                    self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_wakeups '
                        'WHERE practice_id=%s', (self.pid,)).fetchone()[0], 2)
                    self.assertEqual(self.conn.execute('SELECT input,plan FROM secretary_runtime_reviews '
                        'WHERE operation_key=%s', (wake['operation_key'],)).fetchone(), prior)
                    notification = self.conn.execute("SELECT payload FROM work_jobs WHERE kind='action' "
                        "AND payload->>'practice_id'=%s", (str(self.pid),)).fetchone()[0]
                    with patch.object(pipeline.telegram, 'notify', return_value=True) as notify:
                        pipeline.action(notification)
                        pipeline.action(notification)
                    notify.assert_called_once()
                self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_runtime_observations').fetchone()[0], 0)

    def test_expired_recovery_is_bounded_atomic_and_does_not_rearm_failed_jobs(self):
        self.delegate()
        self.consent()
        wake = self.wake()
        with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                runtime.review(wake)
        prior = self.conn.execute('SELECT input,plan,status,result FROM secretary_runtime_reviews').fetchone()
        future = datetime.fromisoformat(prior[1]['next_review_at']) + timedelta(hours=1)
        with patch.object(runtime, '_now', return_value=future):
            with patch.object(jobs, 'enqueue', side_effect=RuntimeError('outbox crash')):
                with self.assertRaises(RuntimeError):
                    runtime.backlog(self.conn, 1)
            self.assertEqual(self.conn.execute('SELECT input,plan,status,result FROM secretary_runtime_reviews').fetchone(), prior)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_wakeups').fetchone()[0], 1)
            self.conn.execute("UPDATE work_jobs SET status='failed' WHERE key=%s", (wake['operation_key'],))
            self.assertEqual(runtime.backlog(self.conn, 1), 0)
            self.conn.execute("UPDATE work_jobs SET status='done' WHERE key=%s", (wake['operation_key'],))
            self.assertEqual(runtime.backlog(self.conn, 1), 1)
            self.assertEqual(runtime.backlog(self.conn, 1), 0)
            recovery_key = runtime._expired_review_result(wake['operation_key'], True)['recovery_operation_key']
            recovery = self.conn.execute('SELECT payload FROM work_jobs WHERE key=%s', (recovery_key,)).fetchone()[0]
            # A second outage gets a distinct bounded key, never another use of the old plan.
            with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
                with self.assertRaises(RuntimeError):
                    runtime.review(recovery)
        with patch.object(runtime, '_now', return_value=future + timedelta(days=3)):
            self.assertEqual(runtime.backlog(self.conn, 1), 1)
            self.assertEqual(runtime.backlog(self.conn, 1), 0)
        keys = [row[0] for row in self.conn.execute('SELECT operation_key FROM secretary_wakeups').fetchall()]
        self.assertEqual(len(keys), 3)
        self.assertTrue(all(len(key) <= 200 for key in keys))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_decisions WHERE event=%s',
                                           ('runtime_review',)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action'").fetchone()[0], 0)

    def test_expired_recovery_total_limit_with_multiple_due_practices(self):
        for _ in range(3):
            self.delegate()
            self.consent()
            wake = next(w for w in secretary.due_reviews(self.conn) if w['practice_id'] == self.pid)
            with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
                with self.assertRaises(RuntimeError):
                    runtime.review(wake)
        with patch.object(runtime, '_now', return_value=datetime.now(timezone.utc) + timedelta(days=3)):
            for expected in (1, 2, 3):
                self.assertEqual(runtime.backlog(self.conn, 1), 1)
                self.assertEqual(self.conn.execute("SELECT count(*) FROM secretary_wakeups "
                    "WHERE operation_key LIKE 'secretary-review-recovery:%'").fetchone()[0], expected)
            self.assertEqual(runtime.backlog(self.conn, 1), 0)
        self.chat.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_decisions WHERE event=%s',
                                           ('runtime_review',)).fetchone()[0], 0)

    def test_expired_recovery_lost_lease_rolls_back_stop_wake_and_outbox(self):
        self.delegate()
        self.consent()
        wake = self.wake()
        with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                runtime.review(wake)
        original_enqueue = jobs.enqueue
        def lose_lease_after_enqueue(*args, **kwargs):
            result = original_enqueue(*args, **kwargs)
            self.enterContext(patch.object(jobs, 'assert_current', side_effect=jobs.LeaseLost()))
            return result
        with patch.object(runtime, '_now', return_value=datetime.now(timezone.utc) + timedelta(days=3)), \
             patch.object(jobs, 'enqueue', side_effect=lose_lease_after_enqueue):
            with self.assertRaises(jobs.LeaseLost):
                runtime.backlog(self.conn, 1)
        self.assertEqual(self.conn.execute('SELECT status,result FROM secretary_runtime_reviews').fetchone(), ('prepared', None))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_wakeups').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 1)

    def test_expired_recurring_recovery_rechecks_versions_sources_and_native_authority(self):
        for invalid in ('responsibility', 'practice', 'source', 'origin'):
            with self.subTest(invalid=invalid):
                self.delegate()
                self.consent()
                wake = next(w for w in secretary.due_reviews(self.conn) if w['practice_id'] == self.pid)
                with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError('crash')):
                    with self.assertRaises(RuntimeError):
                        runtime.review(wake)
                candidate = self.conn.execute('SELECT plan FROM secretary_runtime_reviews '
                    'WHERE operation_key=%s', (wake['operation_key'],)).fetchone()[0]
                if invalid == 'responsibility':
                    self.conn.execute("UPDATE secretary_responsibilities SET state='revoked',version=version+1 WHERE id=%s", (self.rid,))
                elif invalid == 'practice':
                    self.conn.execute("UPDATE secretary_practices SET state='completed',next_review_at=NULL,version=version+1 WHERE id=%s", (self.pid,))
                elif invalid == 'source':
                    self.conn.execute('UPDATE knowledge_entries SET active=FALSE WHERE message_id=%s', (self.origin,))
                else:
                    self.conn.execute("UPDATE telegram_messages SET metadata=metadata || '{\"is_forwarded\":true}'::jsonb WHERE id=%s", (self.origin,))
                with patch.object(runtime, '_now', return_value=datetime.fromisoformat(candidate['next_review_at']) + timedelta(hours=1)):
                    self.assertEqual(runtime.backlog(self.conn, 1), 0)
                self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_wakeups WHERE practice_id=%s', (self.pid,)).fetchone()[0], 1)

    def test_outbox_failure_rolls_back_review_apply_but_keeps_prepared_plan(self):
        self.delegate()
        self.consent()
        wake = self.wake()
        with self.respond_with_plan('draft'), patch.object(jobs, 'enqueue', side_effect=RuntimeError('outbox unavailable')):
            with self.assertRaises(RuntimeError):
                runtime.review(wake)
        self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT status FROM secretary_runtime_reviews').fetchone(), ('prepared',))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_runtime_attention').fetchone()[0], 0)
        self.assertEqual(runtime.review(wake)['status'], 'applied')

    def test_consent_revoked_during_inference_prevents_plan_and_apply(self):
        self.delegate()
        self.consent()
        wake = self.wake()
        with self.respond_with_plan(hook=lambda data: self.consent(allowed=False)):
            with self.assertRaises(jobs.Deferred):
                runtime.review(wake)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_runtime_reviews').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices').fetchone()[0], 1)

    def test_revoked_completed_and_changed_responsibility_stop(self):
        for stop in ('revoked','completed','version'):
            with self.subTest(stop=stop):
                self.delegate()
                self.consent()
                wake = self.wake()
                with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError()):
                    with self.assertRaises(RuntimeError):
                        runtime.review(wake)
                if stop == 'revoked':
                    self.conn.execute("UPDATE secretary_responsibilities SET state='revoked',version=version+1 WHERE id=%s", (self.rid,))
                elif stop == 'completed':
                    self.conn.execute("UPDATE secretary_practices SET state='completed',next_review_at=NULL,version=version+1 WHERE id=%s", (self.pid,))
                else:
                    self.conn.execute('UPDATE secretary_responsibilities SET version=version+1 WHERE id=%s', (self.rid,))
                self.assertEqual(runtime.review(wake)['status'], 'stopped')

    def test_new_external_exact_scope_proposal_without_ai_or_privilege_mutation(self):
        self.delegate()
        mid = self.archive('Il preventivo per il tetto è pronto.', channel='email')
        with patch.object(secretary, '_queue_review', wraps=secretary._queue_review) as queue:
            runtime.observe(self.conn, mid)
            result = runtime.review(dict(observation_message_id=mid, operation_key=f'secretary-observe:{mid}'))
        self.assertEqual(result['proposed_links'], 1)
        self.chat.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT state,method,message_id FROM secretary_runtime_link_proposals').fetchone(),
                         ('proposed','exact_scope',mid))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_links').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT version,state FROM secretary_practices').fetchone(), (1,'active'))
        self.assertEqual(queue.call_args.args[2], 'candidate')
        self.assertEqual(runtime.review(dict(observation_message_id=mid,operation_key=f'secretary-observe:{mid}'))['status'], 'done')

    def test_historical_observation_no_jobs_no_ai(self):
        self.delegate()
        mid = self.archive('tetto storico', channel='email', historical=True)
        runtime.observe(self.conn, mid)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_runtime_observations').fetchone()[0], 0)
        self.chat.assert_not_called()

    def test_new_external_model_link_persisted_and_reused_after_crash(self):
        self.delegate()
        self.consent()
        self.consent('email')
        mid = self.archive('Il fornitore conferma la copertura e il preventivo.', channel='email')
        runtime.observe(self.conn, mid)
        payload = dict(observation_message_id=mid, operation_key=f'secretary-observe:{mid}')
        def model(system, user, **kwargs):
            from psycopg.pq import TransactionStatus
            self.assertEqual(self.conn.info.transaction_status, TransactionStatus.IDLE)
            data = json.loads(user)
            return json.dumps(dict(links=[dict(practice_id=self.pid, evidence=data['source']['text'])]))
        with patch.object(brain, '_chat', side_effect=model) as model_mock, \
             patch.object(secretary, '_queue_review', side_effect=RuntimeError('crash before proposal commit')):
            with self.assertRaises(RuntimeError):
                runtime.review(payload)
        model_mock.assert_called_once()
        self.assertIsNotNone(self.conn.execute('SELECT plan FROM secretary_runtime_observations WHERE message_id=%s', (mid,)).fetchone()[0])
        self.assertEqual(runtime.review(payload)['proposed_links'], 1)
        self.assertEqual(self.conn.execute('SELECT method FROM secretary_runtime_link_proposals').fetchone(), ('model',))
        self.assertEqual(self.conn.execute('SELECT version,state FROM secretary_practices').fetchone(), (1,'active'))

    def test_document_context_requires_media_consent_and_uses_knowledge(self):
        self.delegate()
        self.conn.execute('''INSERT INTO attachments(message_id,channel,filename,content_type,data)
            VALUES (%s,'telegram','offerta.xml','application/xml',%s)''',
            (self.origin, b'<offerta><oggetto>tetto preventivo 2500 euro</oggetto></offerta>'))
        knowledge.index_message(dict(message_id=self.origin))
        self.consent()
        wake = self.wake()
        with self.assertRaises(jobs.Deferred):
            runtime.review(wake)
        self.consent(effect='ai_media')
        with self.respond_with_plan() as model:
            self.assertEqual(runtime.review(wake)['status'], 'applied')
        data = json.loads(model.call_args.args[1])
        documents = [s for s in data['sources'] if s['source_type'] == 'document']
        self.assertEqual(len(documents), 1)
        self.assertIn('2500', documents[0]['text'])
        self.assertNotIn('path', documents[0])

    def test_candidate_sources_need_each_channel_consent_for_review(self):
        self.delegate()
        mid = self.archive('Preventivo per il tetto.', channel='email')
        runtime.observe(self.conn, mid)
        runtime.review(dict(observation_message_id=mid,operation_key=f'secretary-observe:{mid}'))
        wake = self.conn.execute("SELECT payload FROM work_jobs WHERE key LIKE 'secretary-review:%:candidate'").fetchone()[0]
        self.consent()
        with self.assertRaises(jobs.Deferred):
            runtime.review(wake)
        self.consent('email')
        with self.respond_with_plan('draft'):
            result = runtime.review(wake)
        self.assertEqual(result['action'], 'draft')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_approvals').fetchone()[0], 0)

    def test_owner_notification_receipted_and_stops_after_revocation(self):
        self.delegate()
        self.consent()
        wake = self.wake()
        with self.respond_with_plan('request_owner_decision'):
            runtime.review(wake)
        payload = self.conn.execute("SELECT payload FROM work_jobs WHERE kind='action'").fetchone()[0]
        with patch.object(pipeline.telegram, 'notify', return_value=True) as notify:
            pipeline.action(payload)
            pipeline.action(payload)
        notify.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT status FROM effect_receipts').fetchone(), ('accepted',))
        payload['text'] += 'changed'
        self.conn.execute("UPDATE secretary_responsibilities SET state='revoked',version=version+1")
        with patch.object(pipeline.telegram, 'notify') as notify:
            pipeline.action(payload)
        notify.assert_not_called()

    def test_accepted_owner_reply_does_not_lose_identical_quiet_hours_review(self):
        self.delegate()
        self.consent()
        reply = dict(type='notify', text='Delegazione registrata.', html=False,
                     owner_reply_mid=self.origin, owner_reply_chunk=0)
        reply_key = f'owner-reply:{self.origin}:0'
        jobs.enqueue(self.conn, 'action', reply_key, reply)
        with patch.object(pipeline.telegram, 'notify', return_value=True) as notify:
            pipeline.action(reply)
        notify.assert_called_once()
        self.conn.execute("UPDATE work_jobs SET status='done' WHERE key=%s", (reply_key,))
        self.assertEqual(self.conn.execute('SELECT key,status,message_id FROM effect_receipts').fetchone(),
                         (reply_key, 'accepted', self.origin))
        clock = datetime.now(timezone.utc).replace(hour=23, minute=30, second=0, microsecond=0) + timedelta(days=1)
        ready = (clock + timedelta(days=1)).replace(hour=8, minute=0)
        with patch.dict(os.environ, {'SECRETARY_REVIEW_QUIET_START':'22', 'SECRETARY_REVIEW_QUIET_END':'8'}), \
             patch.object(runtime, '_now', return_value=clock), self.respond_with_plan('draft') as model:
            self.assertEqual(runtime.review(self.wake())['version'], 2)
            old = self.conn.execute("SELECT payload FROM work_jobs WHERE payload->>'secretary_review'='true'").fetchone()[0]
            runtime.assert_notification_current(old)
            wake = secretary.observe(self.conn, self.origin)[0]
            result = runtime.review(wake)
            self.assertEqual(result['version'], 3)
            self.assertEqual(runtime.review(wake), result)
        self.assertEqual(model.call_count, 2)
        rows = self.conn.execute("""SELECT id,payload,available_at FROM work_jobs
            WHERE payload->>'secretary_review'='true' ORDER BY id""").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[2] for row in rows], [ready, ready])
        self.assertTrue(all(row[2] > clock for row in rows))
        self.assertEqual(old['attention_hash'], rows[1][1]['attention_hash'])
        with self.assertRaises(runtime.ReviewStopped):
            runtime.assert_notification_current(old)
        deliverable = []
        for identity, payload, _ in rows:
            try:
                runtime.assert_notification_current(payload)
            except runtime.ReviewStopped:
                continue
            deliverable.append(identity)
        self.assertEqual(deliverable, [rows[1][0]])
        with patch.object(runtime, '_now', return_value=ready), \
             patch.object(pipeline.telegram, 'notify', return_value=True) as notify:
            for identity, payload, _ in rows:
                with self.notification_execution(identity, payload):
                    pipeline.action(payload)
                    pipeline.action(payload)
        notify.assert_called_once_with(rows[1][1]['text'], html=False)
        self.assertEqual(self.conn.execute('SELECT key,status FROM effect_receipts ORDER BY key').fetchall(),
                         [(f'notification-job:{rows[1][0]}', 'accepted'), (reply_key, 'accepted')])

    def test_prior_review_attempt_receipts_prevent_duplicate_replacement(self):
        for worker in (False, True):
            for status in ('attempted', 'uncertain', 'accepted', 'verified'):
                with self.subTest(worker=worker, status=status):
                    self.delegate()
                    self.consent()
                    with self.respond_with_plan('draft'):
                        runtime.review(self.wake())
                    identity, payload = self.conn.execute("""SELECT id,payload FROM work_jobs
                        WHERE payload->>'secretary_review'='true' AND payload->>'practice_id'=%s""",
                        (str(self.pid),)).fetchone()
                    execution = self.notification_execution(identity, payload) if worker else nullcontext()
                    with execution:
                        effect_key = pipeline._notification_key(payload)
                        if status == 'attempted':
                            effect = dict(text=payload['text'], html=False, recipient='101')
                            self.assertTrue(policy.prepare_effect(self.conn, effect_key, self.origin, 'notify', effect))
                            self.assertTrue(policy.record_receipt(self.conn, effect_key, 'attempted'))
                        else:
                            with patch.object(pipeline.telegram, 'notify', return_value=status != 'uncertain') as notify:
                                if status == 'uncertain':
                                    with self.assertRaises(pipeline.EffectUncertain):
                                        pipeline.action(payload)
                                else:
                                    pipeline.action(payload)
                            notify.assert_called_once()
                            if status == 'verified':
                                self.assertTrue(policy.record_receipt(self.conn, effect_key, 'verified'))
                    self.assertEqual(self.conn.execute('SELECT status,job_id FROM effect_receipts WHERE key=%s',
                        (effect_key,)).fetchone(), (status, identity if worker else None))
                    self.conn.execute("UPDATE work_jobs SET status='done' WHERE id=%s", (identity,))
                    wake = secretary.observe(self.conn, self.origin)[0]
                    with self.respond_with_plan('draft'):
                        self.assertEqual(runtime.review(wake)['version'], 3)
                    self.assertEqual(self.conn.execute("""SELECT count(*) FROM work_jobs
                        WHERE payload->>'secretary_review'='true' AND payload->>'practice_id'=%s""",
                        (str(self.pid),)).fetchone()[0], 1)
                    with patch.object(pipeline.telegram, 'notify') as notify:
                        pipeline.action(payload)
                    notify.assert_not_called()

    def test_unrelated_review_job_or_effect_identity_does_not_suppress_replacement(self):
        for unrelated in ('job_key', 'effect_key', 'practice', 'hash', 'not_review'):
            with self.subTest(unrelated=unrelated):
                self.delegate()
                self.consent()
                with self.respond_with_plan('draft'):
                    runtime.review(self.wake())
                identity, payload = self.conn.execute("""SELECT id,payload FROM work_jobs
                    WHERE payload->>'secretary_review'='true' AND payload->>'practice_id'=%s""",
                    (str(self.pid),)).fetchone()
                # Deliberately synthetic receipt: same origin (even same job_id for
                # effect_key), but never the previous attention's notification operation.
                if unrelated != 'effect_key':
                    other = dict(payload, review_operation_key='unrelated:' + uuid.uuid4().hex)
                    if unrelated == 'practice':
                        other['practice_id'] += 100000
                    elif unrelated == 'hash':
                        other['attention_hash'] = 'b' * 64
                    elif unrelated == 'not_review':
                        other['secretary_review'] = False
                    key = 'wrong-notify-key:' + uuid.uuid4().hex if unrelated == 'job_key' else other['review_operation_key'] + ':notify'
                    receipt_job = jobs.enqueue(self.conn, 'action', key, other)[0]
                    effect_key = f'notification-job:{receipt_job}'
                else:
                    receipt_job = identity
                    effect_key = 'unrelated-effect:' + uuid.uuid4().hex
                self.conn.execute("""INSERT INTO effect_receipts(key,message_id,effect,payload_hash,status,job_id)
                    VALUES (%s,%s,'notify',%s,'accepted',%s)""", (effect_key, self.origin, 'a' * 64, receipt_job))
                wake = secretary.observe(self.conn, self.origin)[0]
                with self.respond_with_plan('draft'):
                    self.assertEqual(runtime.review(wake)['version'], 3)
                current = self.conn.execute("""SELECT payload FROM work_jobs
                    WHERE key=%s""", (wake['operation_key'] + ':notify',)).fetchone()
                self.assertIsNotNone(current)
                runtime.assert_notification_current(current[0])
                with self.assertRaises(runtime.ReviewStopped):
                    runtime.assert_notification_current(payload)

    def test_older_same_hash_review_receipt_does_not_suppress_latest_unsent_attention(self):
        self.delegate()
        self.consent()
        for index, draft in enumerate(('Bozza A.', 'Bozza B.', 'Bozza A.', 'Bozza A.')):
            wake = self.wake() if index == 0 else secretary.observe(self.conn, self.origin)[0]
            with self.respond_with_plan('draft', draft=draft):
                self.assertEqual(runtime.review(wake)['version'], index + 2)
            if index == 0:
                identity, payload = self.conn.execute("SELECT id,payload FROM work_jobs WHERE key=%s",
                    (wake['operation_key'] + ':notify',)).fetchone()
                with self.notification_execution(identity, payload), \
                     patch.object(pipeline.telegram, 'notify', return_value=True) as notify:
                    pipeline.action(payload)
                notify.assert_called_once()
                self.conn.execute("UPDATE work_jobs SET status='done' WHERE id=%s", (identity,))
        rows = self.conn.execute("SELECT payload FROM work_jobs WHERE payload->>'secretary_review'='true' ORDER BY id").fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][0]['attention_hash'], rows[-1][0]['attention_hash'])
        runtime.assert_notification_current(rows[-1][0])
        for (payload,) in rows[:-1]:
            with self.assertRaises(runtime.ReviewStopped):
                runtime.assert_notification_current(payload)

    def test_acknowledged_identical_review_stays_silent_after_version_bump(self):
        self.delegate()
        self.consent()
        with self.respond_with_plan('draft'):
            runtime.review(self.wake())
        runtime.acknowledge(self.conn, self.pid, authority_message_id=self.origin)
        with self.respond_with_plan('draft'):
            self.assertEqual(runtime.review(secretary.observe(self.conn, self.origin)[0])['version'], 3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM effect_receipts').fetchone()[0], 0)
        self.assertIsNotNone(self.conn.execute('SELECT acknowledged_at FROM secretary_runtime_attention').fetchone()[0])

    def test_real_notify_uncertain_receipt_no_blind_resend(self):
        mid = self.archive()
        payload = dict(type='notify', message_id=mid, text='offline test')
        with patch.object(pipeline.telegram, 'notify', return_value=False) as notify:
            with self.assertRaises(pipeline.EffectUncertain):
                pipeline.action(payload)
            with self.assertRaises(pipeline.EffectUncertain):
                pipeline.action(payload)
        notify.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT status FROM effect_receipts').fetchone(), ('uncertain',))

    def test_real_owner_reminder_preserves_default_label_and_receipt_replay(self):
        origin = self.archive()
        when = datetime.now(timezone.utc) - timedelta(minutes=2)
        identity, version = self.conn.execute('''INSERT INTO tasks(title,deadline,owner_origin_message,owner_operation_key)
            VALUES ('Valutare preventivo',%s,%s,%s) RETURNING id,version''',
            (when,origin,'owner-test:' + uuid.uuid4().hex)).fetchone()
        payload = dict(type='reminder', id=identity, version=version, owner_reminder=True, remind_at=when.isoformat())
        with patch.object(pipeline.telegram, 'notify', return_value=True) as notify:
            pipeline.action(payload)
            pipeline.action(payload)
        notify.assert_called_once()
        self.assertIn('promemoria predefinito (default alla scadenza)', notify.call_args.args[0])
        self.assertFalse(notify.call_args.kwargs['html'])
        self.assertEqual(self.conn.execute('SELECT status,message_id FROM effect_receipts').fetchone(), ('accepted',origin))
        self.conn.execute("UPDATE tasks SET status='done',version=version+1 WHERE id=%s", (identity,))
        with patch.object(pipeline.telegram, 'notify') as notify:
            pipeline.action(payload)
        notify.assert_not_called()

    def test_lease_lost_after_model_rolls_back_plan_and_apply(self):
        self.delegate()
        self.consent()
        wake = self.wake()
        def revoke_execution(data):
            self.lease = self.enterContext(patch.object(jobs, 'assert_current', side_effect=jobs.LeaseLost()))
        with self.respond_with_plan(hook=revoke_execution):
            with self.assertRaises(jobs.LeaseLost):
                runtime.review(wake)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_runtime_reviews').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices').fetchone()[0], 1)

    def test_calendar_ungranted_and_zip_without_provenance(self):
        self.delegate()
        event = dict(title='Owner event', dt='2099-01-01T10:00:00+00:00')
        identity = pipeline.save_event(self.conn, event, f'owner:{self.origin}:event:0', 'owner', True)
        self.assertEqual(self.conn.execute('SELECT sync_state FROM appointments WHERE id=%s',
                                          (identity,)).fetchone(), ('proposal',))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM calendar_effect_grants '
            'WHERE appointment_id=%s', (identity,)).fetchone()[0], 0)
        with patch.object(pipeline.gcal, 'is_active', return_value=True), \
             patch.object(pipeline.gcal, 'create_event') as create:
            pipeline.action(dict(type='calendar',id=identity))  # Ungranted local proposal stays inert.
        create.assert_not_called()
        # zip_send is delegated to native invoice provenance: a month without
        # any archived invoice message fails permanently instead of looping.
        self.assertTrue(policy.can_operate(self.conn, self.origin, 'zip_send'))
        with patch.object(pipeline.capabilities, 'enabled', return_value=True), \
             patch.object(pipeline.flows, 'enabled', return_value=True), \
             patch.object(pipeline.telegram, 'send_document') as send:
            with self.assertRaises(jobs.Permanent):
                pipeline.action(dict(type='zip', month='2026-09'))
        send.assert_not_called()

    def test_schedule_and_observe_are_coalesced_bounded(self):
        self.delegate()
        with patch.object(capabilities, 'enabled', return_value=False):
            pipeline.schedule()
            pipeline.schedule()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='secretary_review'").fetchone()[0], 1)
        runtime.observe(self.conn, self.origin)
        runtime.observe(self.conn, self.origin)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_runtime_observations').fetchone()[0], 1)
        runtime.backlog(self.conn)

    def test_changed_source_stops_persisted_review(self):
        self.delegate()
        self.consent()
        wake = self.wake()
        with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError()):
            with self.assertRaises(RuntimeError):
                runtime.review(wake)
        self.conn.execute('UPDATE knowledge_entries SET active=FALSE')
        self.assertEqual(runtime.review(wake)['status'], 'stopped')
        self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices').fetchone()[0], 1)

    def test_native_edit_without_reindex_stops_persisted_review(self):
        self.delegate()
        self.consent()
        wake = self.wake()
        with self.respond_with_plan(), patch.object(runtime, '_apply_review', side_effect=RuntimeError()):
            with self.assertRaises(RuntimeError):
                runtime.review(wake)
        self.conn.execute('UPDATE telegram_messages SET body=body||%s WHERE id=%s', (' Fonte modificata.', self.origin))
        self.assertEqual(runtime.review(wake)['status'], 'stopped')

    def test_ack_prevents_queued_notification_send(self):
        self.delegate()
        self.consent()
        with self.respond_with_plan('draft'):
            runtime.review(self.wake())
        payload = self.conn.execute("SELECT payload FROM work_jobs WHERE kind='action'").fetchone()[0]
        runtime.acknowledge(self.conn, self.pid, authority_message_id=self.origin)
        with patch.object(pipeline.telegram, 'notify') as notify:
            pipeline.action(payload)
        notify.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM effect_receipts').fetchone()[0], 0)

    def test_ack_suppresses_unchanged_content_without_permissions(self):
        self.delegate()
        self.consent()
        with self.respond_with_plan('draft'):
            runtime.review(self.wake())
        runtime.acknowledge(self.conn, self.pid, authority_message_id=self.origin)
        self.assertIsNotNone(self.conn.execute('SELECT acknowledged_at FROM secretary_runtime_attention').fetchone()[0])
        with self.assertRaises(secretary.Unauthorized):
            runtime.acknowledge(self.conn, self.pid, authority_message_id=self.archive(channel='email'))


if __name__ == '__main__':
    unittest.main()