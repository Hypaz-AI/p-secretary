"""Offline lifecycle regressions: no database, model or external service calls."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import task_reconciliation as tr


def item(**overrides):
    result = dict(task_id=7, operation='complete', changes={}, reason='Pagamento confermato',
                  evidence='Invoice paid', certainty='certain', unique_match=True)
    result.update(overrides)
    return result


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.conn.transaction.return_value.__enter__.return_value = self.conn
        connect = patch.object(tr.db, '_conn')
        connect.start().return_value.__enter__.return_value = self.conn
        self.addCleanup(connect.stop)
        network = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def parse(self, change=None, body='Invoice paid', **kwargs):
        return tr.parse_changes({'changes': [change or item()]},
                                [dict(id=7, version=3)], body, **kwargs)[0]

    def sql(self):
        return '\n'.join(call.args[0] for call in self.conn.execute.call_args_list)


class ParsingTests(OfflineTests):
    def test_explicit_unique_certain_quote_auto_applies(self):
        change = self.parse()
        self.assertTrue(change['auto'])
        self.assertEqual(change['expected_version'], 3)

    def test_uncertainty_nonunique_cancel_and_missing_evidence_are_proposals(self):
        for change in (item(certainty='uncertain'), item(unique_match=False),
                       item(operation='cancel'), item(evidence='')):
            with self.subTest(change=change):
                self.assertFalse(self.parse(change)['auto'])

    def test_substrings_negation_questions_and_conditionals_are_not_auto(self):
        for evidence, body in [('paid', 'not paid'), ('paid', 'unpaid'),
                               ('Invoice paid', 'Invoice paid?'),
                               ('Invoice not paid', 'Invoice not paid'),
                               ('If paid', 'If paid'), ('non pagato', 'non pagato')]:
            with self.subTest(body=body):
                self.assertFalse(self.parse(item(evidence=evidence), body)['auto'])

    def test_extracted_document_evidence_is_supported(self):
        self.assertTrue(self.parse(body='', extracted='Invoice paid')['auto'])

    def test_unquoted_evidence_rejected_except_visual_proposal(self):
        with self.assertRaises(ValueError):
            self.parse(body='unrelated')
        self.assertFalse(self.parse(body='', allow_visual_evidence=True)['auto'])

    def test_malformed_batches_reject_atomically(self):
        bad = [item(task_id=True), item(task_id=8), item(unique_match='yes'),
               item(operation='delete'), item(changes={'status': 'done'}),
               item(operation='update', changes={}),
               item(operation='update', changes={'deadline': '2030-01-01T10:00:00'}),
               item(operation='update', changes={'title': ' '}), item(evidence=42)]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.parse(change)
        with self.assertRaises(ValueError):
            tr.parse_changes({'changes': [item(), item()]}, [dict(id=7, version=0)], 'Invoice paid')
        with self.assertRaises(ValueError):
            tr.parse_changes({'changes': {}}, [], '')

    def test_global_candidate_limit_disables_auto(self):
        tasks = [dict(id=i, version=0) for i in range(1, 42)]
        self.assertFalse(tr.parse_changes({'changes': [item()]}, tasks, 'Invoice paid')[0]['auto'])

    def test_deadline_update_requires_aware_iso_but_allows_removal(self):
        for deadline in ('2099-01-02T12:00:00+02:00', None):
            change = self.parse(item(operation='update', changes={'deadline': deadline}))
            self.assertEqual(change['changes']['deadline'], deadline)


class DerivedTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.deadline = datetime.now(timezone.utc) + timedelta(days=3)
        self.event = dict(title='Original', dt='2099-01-01T10:00:00+00:00', duration_min=15)

    def derived(self, operation='update', changes=None, deadline='default', rows=None):
        self.conn.execute.return_value.fetchall.return_value = rows if rows is not None else []
        with patch.object(tr.jobs, 'enqueue') as enqueue, \
             patch.object(tr.pipeline, 'save_event') as save, \
             patch.object(tr.pipeline.gcal, 'validate_event', side_effect=lambda event: event):
            tr._derived(self.conn, 7, 4, operation, changes if changes is not None else {'deadline': 'x'},
                        self.deadline if deadline == 'default' else deadline, 'Updated')
        return enqueue, save

    def test_replacement_uses_new_calendar_and_job_identity(self):
        enqueue, save = self.derived(rows=[(91, 'cancel_pending', self.event)])
        event, key = save.call_args.args[1:3]
        self.assertEqual(key, 'task:7:v4')
        self.assertNotEqual(tr.jobs.event_id(key), tr.jobs.event_id('task:7:v3'))
        self.assertEqual(event['title'], 'Updated')
        self.assertEqual(event['dt'], self.deadline.isoformat())
        self.assertTrue(save.call_args.kwargs['approved'])
        self.assertEqual(enqueue.call_args_list[0].args[2], 'task-calendar:7:4:91')

    def test_calendar_cancellation_is_scoped_to_task_not_source_message(self):
        self.derived(operation='complete', changes={}, rows=[(91, 'cancel_pending', self.event)])
        sql, params = self.conn.execute.call_args_list[0].args
        self.assertIn('sync_key=%s OR sync_key LIKE %s', sql)
        self.assertEqual(params, ('task:7', 'task:7:v%'))
        self.assertNotIn('message:', sql)

    def test_unapproved_calendar_replacement_remains_proposal(self):
        enqueue, save = self.derived(rows=[(91, 'cancelled', self.event)])
        self.assertFalse(save.call_args.kwargs['approved'])
        self.assertFalse(any(c.args[3]['type'] == 'calendar' for c in enqueue.call_args_list))

    def test_no_calendar_is_invented_without_existing_derived_event(self):
        _, save = self.derived()
        save.assert_not_called()

    def test_closing_or_removing_deadline_cancels_reminder_without_replacement(self):
        for operation, changes, deadline in [('complete', {}, self.deadline),
                                               ('cancel', {}, self.deadline),
                                               ('update', {'deadline': None}, None)]:
            with self.subTest(operation=operation):
                self.conn.reset_mock()
                _, save = self.derived(operation, changes, deadline, [(91, 'cancel_pending', self.event)])
                save.assert_not_called()
                self.assertIn("status='done'", self.sql())
                self.assertEqual(self.conn.execute.call_args.args[1], ('task-reminder:7',))

    def test_reminder_replacement_resets_lease_and_carries_deadline(self):
        enqueue, _ = self.derived()
        payload = enqueue.call_args.args[3]
        self.assertEqual(payload['deadline'], self.deadline.isoformat())
        sql, params = self.conn.execute.call_args.args
        self.assertIn('token=NULL', sql)
        self.assertIn('attempts=0', sql)
        self.assertIn("interval '24 hours'", sql)
        self.assertEqual(json.loads(params[0]), payload)

    def test_title_only_update_does_not_rearm_reminder(self):
        enqueue, _ = self.derived(changes={'title': 'Updated'})
        enqueue.assert_not_called()
        self.assertNotIn('UPDATE work_jobs', self.sql())

    def test_delivery_gate_checks_latest_deadline_open_state_and_due_window(self):
        due = datetime.now(timezone.utc) + timedelta(hours=2)
        self.conn.execute.return_value.fetchone.return_value = (due,)
        self.assertTrue(tr.reminder_is_current(self.conn, dict(id=7, deadline=due.isoformat())))
        for payload in (dict(id=7), dict(id=7, deadline=None), dict(id=7, deadline='bad'),
                        dict(id=7, deadline='2099-01-01T10:00:00'),
                        dict(id=7, deadline=self.deadline.isoformat())):
            self.assertFalse(tr.reminder_is_current(self.conn, payload))
        self.conn.execute.return_value.fetchone.return_value = None
        self.assertFalse(tr.reminder_is_current(self.conn, dict(id=7, deadline=due.isoformat())))
        self.conn.execute.return_value.fetchone.return_value = (self.deadline,)
        self.assertFalse(tr.reminder_is_current(self.conn, dict(id=7, deadline=self.deadline.isoformat())))


class LifecycleTests(OfflineTests):
    def apply(self, task=('open', 3, 'Invoice', None), status='pending', operation='complete'):
        self.conn.execute.return_value.fetchone.side_effect = [
            (7,), task, (operation, {}, 3, status), None]
        with patch.object(tr, '_derived') as derived:
            result = tr.apply_change(self.conn, 12)
        return result, derived

    def test_apply_versions_audit_and_invalidates_other_proposals(self):
        result, derived = self.apply()
        self.assertEqual(result, 'applied')
        self.assertIn('version=version+1', self.sql())
        self.assertIn("status='stale'", self.sql())
        derived.assert_called_once_with(self.conn, 7, 4, 'complete', {}, None, 'Invoice')

    def test_stale_version_closed_or_missing_task_has_no_derived_effects(self):
        for task in (('open', 4, 'Invoice', None), ('done', 3, 'Invoice', None), None):
            with self.subTest(task=task):
                result, derived = self.apply(task)
                self.assertEqual(result, 'stale')
                derived.assert_not_called()

    def test_already_applied_or_rejected_proposal_is_idempotent(self):
        for status in ('applied', 'rejected', 'stale'):
            result, derived = self.apply(status=status)
            self.assertEqual(result, status)
            derived.assert_not_called()

    def test_missing_proposal(self):
        self.conn.execute.return_value.fetchone.return_value = None
        self.assertEqual(tr.apply_change(self.conn, 12), 'missing')

    def test_manual_close_uses_audited_versioned_path(self):
        self.conn.execute.return_value.fetchone.return_value = ('open', 3)
        with patch.object(tr, 'apply_change', return_value='applied') as apply:
            self.assertTrue(tr.close_task(self.conn, 7))
        data = apply.call_args.args[1]
        self.assertEqual((data['task_id'], data['expected_version'], data['operation']), (7, 3, 'complete'))
        self.assertTrue(data['evidence_key'].startswith('manual:'))

    def test_manual_close_closed_or_missing_is_not_success(self):
        for row in (None, ('done', 3)):
            self.conn.execute.return_value.fetchone.return_value = row
            self.assertFalse(tr.close_task(self.conn, 7))

    def test_trusted_audit_forbids_invalid_identity_or_fields(self):
        with self.assertRaises(ValueError):
            tr.apply_change(self.conn, dict(task_id=True, expected_version=0, evidence_key='x',
                                           operation='complete', changes={}))


class ReconcileTests(OfflineTests):
    """Lifecycle only; real egress gates live in test_reconciliation_task_privacy.py."""

    def setUp(self):
        super().setUp()
        # Deliberately isolate lifecycle snapshots from native provenance/data-policy SQL.
        # This fixture does not claim that email/model certainty grants task authority.
        self.enterContext(patch.object(tr.policy, 'can_operate', return_value=True))
        self.enterContext(patch.object(tr, '_allows_candidate', return_value=True))

    def run_reconcile(self, count=1, result=None, body='Invoice paid', attachments=None,
                      apply_status='applied', task_state=('open', 3), record=12):
        rows = [(i, 'Invoice', None, 3, 1, 'sender', 'subject', 'original') for i in range(7, 7 + count)]
        self.conn.execute.return_value.fetchone.side_effect = [None, ('run',)] + [task_state] * count
        self.conn.execute.return_value.fetchall.return_value = rows
        with patch.object(tr.pipeline, 'load_message', return_value=(
                ('email', 'sender', 'subject', body, 'source'), attachments or [])), \
             patch.object(tr.flows, 'enabled', return_value=True), \
             patch.object(tr.media_text, 'message_text', return_value=body) as media, \
             patch.object(tr.documents, 'attachment_text', return_value=''), \
             patch.object(tr.brain, 'reconcile_tasks', create=True,
                          return_value=result or {'changes': [item()]}) as ai, \
             patch.object(tr, '_record', return_value=record), \
             patch.object(tr, 'apply_change', return_value=apply_status) as apply, \
             patch.object(tr, '_notify') as notify:
            tr.reconcile({'message_id': 100})
        return ai, apply, notify, media

    def test_auto_notification_only_after_actual_apply(self):
        for status in ('applied', 'stale'):
            with self.subTest(status=status):
                _, apply, notify, _ = self.run_reconcile(apply_status=status)
                apply.assert_called_once()
                self.assertEqual(notify.call_count, int(status == 'applied'))
                if status == 'applied':
                    self.assertIn('Invoice', notify.call_args.args[2])
                    self.assertIn('applicata', notify.call_args.args[2])

    def test_proposal_human_readable_title_status_and_commands(self):
        _, apply, notify, _ = self.run_reconcile(result={'changes': [item(unique_match=False)]})
        apply.assert_not_called()
        text = notify.call_args.args[2]
        for expected in ('Invoice', 'in attesa di conferma', 'Completa', '/conferma_attivita 12'):
            self.assertIn(expected, text)

    def test_stale_or_duplicate_proposal_not_advertised(self):
        for kwargs in (dict(task_state=('done', 3)), dict(record=None)):
            _, apply, notify, _ = self.run_reconcile(result={'changes': [item(unique_match=False)]}, **kwargs)
            apply.assert_not_called()
            notify.assert_not_called()

    def test_multiple_batches_never_autoapply_even_if_model_claims_unique(self):
        # A different task in the second batch avoids a deliberately invalid ID.
        results = [{'changes': [item()]}, {'changes': [item(task_id=47)]}]
        self.conn.execute.return_value.fetchone.side_effect = [None, ('run',), ('open', 3), ('open', 3)]
        self.conn.execute.return_value.fetchall.return_value = [
            (i, 'Invoice', None, 3, 1, 'sender', 'subject', 'original') for i in range(7, 48)]
        with patch.object(tr.pipeline, 'load_message', return_value=(('email', 's', 's', 'Invoice paid', 'x'), [])), \
             patch.object(tr.flows, 'enabled', return_value=True), \
             patch.object(tr.documents, 'attachment_text', return_value=''), \
             patch.object(tr.brain, 'reconcile_tasks', create=True, side_effect=results) as ai, \
             patch.object(tr, '_record', side_effect=[12, 13]), \
             patch.object(tr, 'apply_change') as apply, patch.object(tr, '_notify') as notify:
            tr.reconcile({'message_id': 100})
        self.assertEqual(ai.call_count, 2)
        apply.assert_not_called()
        self.assertEqual(notify.call_count, 2)

    def test_source_timestamp_not_task_creation_used_for_derived_tasks(self):
        self.run_reconcile()
        self.assertIn('ELSE c.acquired_at END', self.sql())
        self.assertNotIn('t.created_at <=', self.sql())
        self.assertIn('WHEN t.source_message IS NULL THEN t.created_at', self.sql())
        self.assertIn('ELSE c.ts END', self.sql())
        self.assertIn('COALESCE(ts,acquired_at)', self.sql())

    def test_normalized_audio_body_and_original_images_pass_to_brain(self):
        attachments = [dict(content_type='audio/ogg'), dict(content_type='image/png')]
        ai, _, _, media = self.run_reconcile(attachments=attachments)
        media.assert_called_once_with('Invoice paid', attachments)
        self.assertEqual(ai.call_args.args[2], 'Invoice paid')
        self.assertEqual(ai.call_args.args[4], attachments)

    def test_image_only_evidence_is_a_manual_proposal(self):
        _, apply, notify, _ = self.run_reconcile(body='', attachments=[dict(content_type='image/png')])
        apply.assert_not_called()
        notify.assert_called_once()

    def test_generated_image_description_never_counts_as_native_quote(self):
        self.conn.execute.return_value.fetchone.side_effect = [None, ('run',), ('open', 3)]
        self.conn.execute.return_value.fetchall.return_value = [
            (7, 'Invoice', None, 3, 1, 's', 's', 'original')]
        with patch.object(tr.pipeline, 'load_message', return_value=(
                ('telegram', 'owner', '', '', 'x'), [{'content_type': 'image/png'}])), \
             patch.object(tr.flows, 'enabled', return_value=True), \
             patch.object(tr.media_text, 'message_text', return_value='Invoice paid'), \
             patch.object(tr.documents, 'attachment_text', return_value=''), \
             patch.object(tr.brain, 'reconcile_tasks', return_value={'changes': [item()]}), \
             patch.object(tr, '_record', return_value=12), \
             patch.object(tr, 'apply_change') as apply, patch.object(tr, '_notify') as notify:
            tr.reconcile({'message_id': 100})
        apply.assert_not_called()
        self.assertIn('in attesa di conferma', notify.call_args.args[2])

    def test_deadline_changes_always_require_owner_confirmation(self):
        _, apply, notify, _ = self.run_reconcile(result={'changes': [item(
            operation='update', changes={'deadline': '2099-01-01T10:00:00+01:00'})]})
        apply.assert_not_called()
        self.assertIn('in attesa di conferma', notify.call_args.args[2])

    def test_completed_run_returns_without_ai(self):
        self.conn.execute.return_value.fetchone.return_value = (1,)
        with patch.object(tr.pipeline, 'load_message', return_value=(('email', 's', 's', 'b', 'x'), [])), \
             patch.object(tr.flows, 'enabled', return_value=True), \
             patch.object(tr.brain, 'reconcile_tasks', create=True) as ai:
            tr.reconcile({'message_id': 100})
        ai.assert_not_called()

    def test_paused_flow_defers(self):
        with patch.object(tr.pipeline, 'load_message', return_value=(('email', 's', 's', 'b', 'x'), [])), \
             patch.object(tr.flows, 'enabled', return_value=False):
            with self.assertRaises(tr.jobs.Deferred):
                tr.reconcile({'message_id': 100})
        self.conn.execute.assert_not_called()

    def test_edit_accepted_during_ai_invalidates_old_evidence(self):
        self.conn.execute.return_value.fetchone.side_effect = [None, None]
        self.conn.execute.return_value.fetchall.return_value = [
            (7, 'Invoice', None, 3, 1, 'sender', 'subject', 'original')]
        with patch.object(tr.pipeline, 'load_message', return_value=(
                ('telegram (account)', 's', 's', 'Invoice paid', 'x'), [])), \
             patch.object(tr.flows, 'enabled', return_value=True), \
             patch.object(tr.documents, 'attachment_text', return_value=''), \
             patch.object(tr.brain, 'reconcile_tasks', return_value={'changes': [item()]}), \
             patch.object(tr, '_record') as record, patch.object(tr, '_notify') as notify:
            tr.reconcile({'message_id': 100, 'revision': '2026-10-16T10:00:00+00:00'})
        record.assert_not_called()
        notify.assert_not_called()
        self.assertIn('FOR SHARE', self.sql())
        self.assertNotIn('INSERT INTO task_reconciliation_runs', self.sql())

    def test_listing_includes_title_and_filters_stale_snapshots(self):
        self.conn.execute.return_value.fetchall.return_value = [(12, 7, 'complete', {}, 'reason', 'quote', 'Invoice')]
        with patch.object(tr, '_notify') as notify:
            self.assertTrue(tr.owner_commands('/proposte'))
        self.assertIn('Invoice', notify.call_args.args[2])
        self.assertIn('t.version=p.expected_version', self.sql())

    def test_rejection_and_confirmation_report_actual_result(self):
        with patch.object(tr, 'apply_change', return_value='stale'), patch.object(tr, '_notify') as notify:
            self.assertTrue(tr.owner_commands('/conferma_attivita 12'))
            self.assertIn('stale', notify.call_args.args[2])
        self.conn.execute.return_value.fetchone.return_value = None
        with patch.object(tr, '_notify') as notify:
            self.assertTrue(tr.owner_commands('/rifiuta_attivita 12'))
            self.assertIn('non pendente', notify.call_args.args[2])

    def test_notification_chunks_are_plain_text_and_untruncated(self):
        text = 'a' * 8001
        with patch.object(tr.jobs, 'enqueue') as enqueue:
            tr._notify(self.conn, 'test', text)
        payloads = [call.args[3] for call in enqueue.call_args_list]
        self.assertEqual(''.join(payload['text'] for payload in payloads), text)
        self.assertTrue(all(not payload['html'] for payload in payloads))


if __name__ == '__main__':
    unittest.main()