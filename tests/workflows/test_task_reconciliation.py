"""Regressioni offline del ciclo di vita delle attività: nessun DB, modello o servizio esterno."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from secretary.workflows import task_reconciliation as tr


def item(**overrides):
    result = dict(task_id=7, operation='complete', changes={}, reason='Pagamento confermato',
                  evidence='Invoice paid', certainty='certain', unique_match=True)
    result.update(overrides)
    return result


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.conn.transaction.return_value.__enter__.return_value = self.conn
        connect = patch('secretary.core.db._conn')
        connect.start().return_value.__enter__.return_value = self.conn
        self.addCleanup(connect.stop)
        network = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def sql(self):
        return '\n'.join(call.args[0] for call in self.conn.execute.call_args_list)


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

    def test_trusted_audit_forbids_invalid_identity_or_fields(self):
        with self.assertRaises(ValueError):
            tr.apply_change(self.conn, dict(task_id=True, expected_version=0, evidence_key='x',
                                           operation='complete', changes={}))


if __name__ == '__main__':
    unittest.main()