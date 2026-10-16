"""Offline owner scheduling regressions; owner_dialogue supplies the migration."""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import db
import digest
import pipeline
import task_reconciliation as tr


class OwnerReminderTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        connection = patch('db._conn')
        connection.start().return_value.__enter__.return_value = self.conn
        self.addCleanup(connection.stop)
        tz = patch('config.TIMEZONE', 'Europe/Rome')
        tz.start()
        self.addCleanup(tz.stop)
        network = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.now = datetime(2030, 3, 30, 12, tzinfo=ZoneInfo('Europe/Rome'))
        self.task = dict(id=7, version=3, title='Pagare fattura', deadline=None,
                         source_message=None, owner_deadline_date=date(2030, 3, 31),
                         owner_remind_at=None, owner_origin_message=42,
                         owner_operation_key='owner-commitment:42:0')

    def row(self, task=None):
        task = task or self.task
        return tuple(task[k] for k in ('deadline', 'version', 'title', 'source_message',
                                      'owner_deadline_date', 'owner_remind_at',
                                      'owner_origin_message', 'owner_operation_key'))

    def payload(self, when):
        return dict(type='reminder', id=7, version=3, owner_reminder=True,
                    remind_at=when.isoformat())

    def test_date_only_defaults_to_local_nine_with_destination_dst(self):
        for day, offset in ((date(2030, 3, 31), 2), (date(2030, 10, 27), 1)):
            with self.subTest(day=day):
                task = dict(self.task, owner_deadline_date=day)
                when = pipeline.owner_reminder_time(task)
                self.assertEqual(when.hour, 9)
                self.assertEqual(when.date(), day)
                self.assertEqual(when.utcoffset(), timedelta(hours=offset))
                self.assertIn('default 09:00 locale', pipeline.owner_task_schedule_label(task, self.now))
                self.assertIsNone(task['deadline'])

    def test_explicit_time_wins_and_is_not_scheduled_24_hours_early(self):
        when = self.now + timedelta(hours=40)
        task = dict(self.task, owner_remind_at=when, deadline=when + timedelta(hours=2))
        with patch('jobs.enqueue', return_value=(1,)) as enqueue:
            pipeline.enqueue_owner_reminder(self.conn, task)
        self.assertEqual(enqueue.call_args.args[2], 'owner-reminder:7:v3')
        self.assertEqual(enqueue.call_args.args[3], self.payload(when))
        sql, params = self.conn.execute.call_args.args
        self.assertEqual(params, (when, 'owner-reminder:7:v3'))
        self.assertNotIn('24 hours', sql)
        self.assertIn('promemoria richiesto', pipeline.owner_task_schedule_label(task))

    def test_timed_deadline_defaults_to_deadline_and_is_labelled_as_default(self):
        when = self.now + timedelta(hours=40)
        task = dict(self.task, owner_deadline_date=None, deadline=when)
        self.assertEqual(pipeline.owner_reminder_time(task), when)
        label = pipeline.owner_task_schedule_label(task, self.now)
        self.assertIn('promemoria predefinito (default alla scadenza)', label)
        self.assertIn(when.isoformat(), label)
        self.assertNotIn('promemoria richiesto', label)
        self.assertNotIn('09:00 locale', label)
        with patch('jobs.enqueue', return_value=(1,)) as enqueue:
            pipeline.enqueue_owner_reminder(self.conn, task)
        self.assertEqual(enqueue.call_args.args[3], self.payload(when))
        self.assertEqual(self.conn.execute.call_args.args[1], (when, 'owner-reminder:7:v3'))

    def test_date_only_default_takes_precedence_over_timed_deadline(self):
        task = dict(self.task, deadline=self.now + timedelta(hours=40))
        expected = datetime(2030, 3, 31, 9, tzinfo=ZoneInfo('Europe/Rome'))
        self.assertEqual(pipeline.owner_reminder_time(task), expected)
        self.assertIn('default 09:00 locale', pipeline.owner_task_schedule_label(task, self.now))

    def test_explicit_reminder_overrides_timed_deadline_default(self):
        when = self.now + timedelta(hours=2)
        task = dict(self.task, owner_deadline_date=None, owner_remind_at=when,
                    deadline=when + timedelta(hours=4))
        self.assertEqual(pipeline.owner_reminder_time(task), when)
        label = pipeline.owner_task_schedule_label(task, self.now)
        self.assertIn('promemoria richiesto', label)
        self.assertNotIn('default', label)

    def test_duplicate_version_does_not_rearm_and_new_version_uses_new_key(self):
        with patch('jobs.enqueue', return_value=None) as enqueue:
            pipeline.enqueue_owner_reminder(self.conn, self.task)
            self.conn.execute.assert_not_called()
            pipeline.enqueue_owner_reminder(self.conn, dict(self.task, version=4))
        self.assertEqual([c.args[2] for c in enqueue.call_args_list],
                         ['owner-reminder:7:v3', 'owner-reminder:7:v4'])

    def test_undated_owner_task_without_requested_reminder_is_not_scheduled(self):
        with patch('jobs.enqueue') as enqueue:
            pipeline.enqueue_owner_reminder(self.conn, dict(self.task, owner_deadline_date=None))
        enqueue.assert_not_called()

    def test_digest_routes_owner_rows_to_same_key_and_never_auto_calendars(self):
        for task in (self.task, dict(self.task, owner_remind_at=self.now + timedelta(hours=3))):
            with self.subTest(task=task):
                self.conn.execute.return_value.fetchone.return_value = self.row(task)
                with patch('jobs.enqueue', return_value=(1,)) as enqueue, \
                     patch('pipeline.save_event') as save:
                    digest.plan_pending_tasks([task], self.now, self.now + timedelta(hours=48))
                self.assertEqual(enqueue.call_args.args[2], 'owner-reminder:7:v3')
                save.assert_not_called()

    def test_digest_rereads_closed_and_updated_owner_tasks(self):
        self.conn.execute.return_value.fetchone.return_value = None
        with patch('jobs.enqueue') as enqueue:
            digest.plan_pending_tasks([self.task], self.now, self.now + timedelta(days=2))
        enqueue.assert_not_called()
        self.conn.execute.return_value.fetchone.return_value = self.row(dict(self.task, version=4))
        with patch('jobs.enqueue', return_value=None) as enqueue:
            digest.plan_pending_tasks([self.task], self.now, self.now + timedelta(days=2))
        self.assertEqual(enqueue.call_args.args[2], 'owner-reminder:7:v4')

    def test_scheduler_keeps_legacy_and_owner_jobs_separate(self):
        due = self.now + timedelta(hours=3)
        self.conn.execute.return_value.fetchall.side_effect = [
            [(7, 3, self.task['owner_deadline_date'], due, None),
             (9, 2, None, None, due)], [(8, due)]]
        with patch('flows.enabled', return_value=False), patch('jobs.enqueue', return_value=None) as enqueue:
            pipeline.schedule()
        self.assertEqual([c.args[2] for c in enqueue.call_args_list],
                         ['owner-reminder:7:v3', 'owner-reminder:9:v2', 'task-reminder:8'])
        self.assertEqual(enqueue.call_args_list[1].args[3],
                         dict(type='reminder', id=9, version=2, owner_reminder=True,
                              remind_at=due.isoformat()))
        self.assertEqual(enqueue.call_args_list[2].args[3],
                         dict(type='reminder', id=8, deadline=due.isoformat()))
        owner_sql = self.conn.execute.call_args_list[0].args[0]
        self.assertIn('owner_remind_at,deadline FROM tasks', owner_sql)
        self.assertIn("status='open'", owner_sql)
        self.assertIn('deadline IS NOT NULL', owner_sql)
        self.assertIn('owner_origin_message IS NOT NULL OR owner_operation_key IS NOT NULL', owner_sql)
        self.assertIn('FOR UPDATE', owner_sql)
        sql = self.conn.execute.call_args.args[0]
        self.assertIn('owner_origin_message IS NULL AND owner_operation_key IS NULL', sql)
        self.assertIn('owner_remind_at IS NULL', sql)
        self.assertIn("interval '24 hours'", sql)

    def test_delivery_checks_exact_due_time_version_and_open_state(self):
        now = datetime.now(timezone.utc)
        past, future = now - timedelta(minutes=1), now + timedelta(minutes=1)
        self.conn.execute.return_value.fetchone.return_value = (3, None, future, None)
        self.assertFalse(tr.reminder_is_current(self.conn, self.payload(future)))
        self.conn.execute.return_value.fetchone.return_value = (3, None, past, None)
        self.assertTrue(tr.reminder_is_current(self.conn, self.payload(past)))
        self.assertIn("status='open'", self.conn.execute.call_args.args[0])
        for row in (None, (4, None, past, None), (3, None, past - timedelta(hours=1), None)):
            self.conn.execute.return_value.fetchone.return_value = row
            self.assertFalse(tr.reminder_is_current(self.conn, self.payload(past)))

    def test_default_delivery_gate_recomputes_local_date_time(self):
        day = date(2000, 3, 26)
        when = pipeline.owner_reminder_time(dict(owner_deadline_date=day))
        self.conn.execute.return_value.fetchone.return_value = (3, day, None, None)
        self.assertTrue(tr.reminder_is_current(self.conn, self.payload(when)))
        self.assertFalse(tr.reminder_is_current(self.conn, self.payload(when - timedelta(hours=24))))

    def test_timed_default_delivery_requires_current_deadline_version_and_due_time(self):
        now = datetime.now(timezone.utc)
        past, future = now - timedelta(minutes=1), now + timedelta(minutes=1)
        self.conn.execute.return_value.fetchone.return_value = (3, None, None, past)
        self.assertTrue(tr.reminder_is_current(self.conn, self.payload(past)))
        self.assertIn('owner_remind_at,deadline FROM tasks', self.conn.execute.call_args.args[0])
        for row, when in ((None, past), ((4, None, None, past), past),
                          ((3, None, None, future), future),
                          ((3, None, None, future), past),
                          ((3, None, None, None), past),
                          ((3, None, past - timedelta(hours=1), past), past)):
            with self.subTest(row=row, when=when):
                self.conn.execute.return_value.fetchone.return_value = row
                self.assertFalse(tr.reminder_is_current(self.conn, self.payload(when)))

    def test_malformed_payloads_fail_closed(self):
        for change in (dict(version=None), dict(version=True), dict(remind_at=None),
                       dict(remind_at='bad'), dict(remind_at='2030-01-01T09:00:00')):
            self.assertFalse(tr.reminder_is_current(self.conn, dict(self.payload(self.now), **change)))
        self.conn.execute.assert_not_called()

    def test_legacy_gate_excludes_owner_rows(self):
        when = datetime.now(timezone.utc)
        self.conn.execute.return_value.fetchone.return_value = None
        self.assertFalse(tr.reminder_is_current(self.conn, dict(id=7, deadline=when.isoformat())))
        sql = self.conn.execute.call_args.args[0]
        self.assertIn('owner_origin_message IS NULL', sql)
        self.assertIn('owner_remind_at IS NULL', sql)

    def test_existing_manual_completion_path_fences_owner_delivery(self):
        self.conn.execute.return_value.fetchone.return_value = ('open', 3)
        with patch('task_reconciliation.apply_change', return_value='applied') as apply:
            self.assertTrue(tr.close_task(self.conn, 7))
        change = apply.call_args.args[1]
        self.assertEqual(change['expected_version'], 3)
        self.assertEqual(change['operation'], 'complete')
        # A worker may already have claimed v3 before completion committed.
        self.conn.execute.return_value.fetchone.return_value = None
        with patch('connectors.telegram.notify') as notify:
            pipeline.action(self.payload(datetime.now(timezone.utc) - timedelta(minutes=1)))
        notify.assert_not_called()

    def test_timed_owner_deadline_without_requested_reminder_uses_default_not_legacy(self):
        task = dict(self.task, deadline=self.now + timedelta(hours=4), owner_deadline_date=None)
        self.conn.execute.return_value.fetchone.return_value = self.row(task)
        with patch('jobs.enqueue', return_value=(1,)) as enqueue, patch('pipeline.save_event') as save:
            digest.plan_pending_tasks([task], self.now, self.now + timedelta(days=2))
        self.assertEqual(enqueue.call_args.args[2], 'owner-reminder:7:v3')
        self.assertEqual(enqueue.call_args.args[3], self.payload(task['deadline']))
        self.assertEqual(self.conn.execute.call_args.args[1],
                         (task['deadline'], 'owner-reminder:7:v3'))
        save.assert_not_called()

    def test_action_labels_timed_default_as_default_not_requested(self):
        when = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.conn.execute.return_value.fetchone.side_effect = [
            (3, None, None, when), ('Pagare fattura', when, None, None)]
        with patch('connectors.telegram.notify', return_value=True) as notify:
            pipeline.action(self.payload(when))
        text = notify.call_args.args[0]
        self.assertIn('default alla scadenza', text)
        self.assertNotIn('promemoria richiesto', text)
        self.assertFalse(notify.call_args.kwargs['html'])

    def test_action_suppresses_stale_and_labels_current_default(self):
        with patch('task_reconciliation.reminder_is_current', return_value=False), \
             patch('connectors.telegram.notify') as notify:
            pipeline.action(self.payload(self.now))
        notify.assert_not_called()
        self.conn.execute.return_value.fetchone.return_value = ('Pagare fattura', None, date(2000, 1, 1), None)
        with patch('task_reconciliation.reminder_is_current', return_value=True), \
             patch('connectors.telegram.notify', return_value=True) as notify:
            pipeline.action(self.payload(self.now))
        self.assertIn('default 09:00 locale', notify.call_args.args[0])
        self.assertIn('arretrata', notify.call_args.args[0])

    def test_pending_tasks_filters_date_horizon_in_local_timezone(self):
        until = datetime(2030, 3, 30, 23, 30, tzinfo=timezone.utc)  # March 31 in Rome
        def row(identity, day, reminder=None):
            return (identity, 'Pagare', None, None, day, reminder, 42, 'key', 3)
        self.conn.execute.return_value.fetchall.return_value = [
            row(1, date(2000, 1, 1)), row(2, date(2030, 3, 31)),
            row(3, date(2030, 4, 1)), row(4, None),
            row(5, date(2030, 4, 2), until - timedelta(hours=1))]
        tasks = db.pending_tasks(until)
        self.assertEqual([t['id'] for t in tasks], [1, 2, 5, 4])
        self.assertEqual(tasks[1]['owner_deadline_date'], date(2030, 3, 31))
        self.assertIsNone(tasks[1]['deadline'])
        sql = self.conn.execute.call_args.args[0]
        self.assertIn("status='open'", sql)
        self.assertIn('owner_remind_at <= horizon.until', sql)

    def test_attivita_labels_date_only_default_and_past_date(self):
        self.conn.execute.return_value.fetchall.return_value = [
            (7, 'Pagare fattura', None, None, date(2000, 1, 1), None, 42)]
        with patch('task_reconciliation.owner_commands', return_value=False), \
             patch('connectors.telegram.notify', return_value=True) as notify:
            self.assertTrue(pipeline.owner_commands('/attivita'))
        text = notify.call_args.args[0]
        for label in ('2000-01-01', 'solo data', 'default 09:00 locale', 'arretrata', 'fonte #42'):
            self.assertIn(label, text)

    def test_digest_surfaces_date_default_even_if_ai_omits_task(self):
        task = dict(self.task, owner_deadline_date=date(2000, 1, 1))
        with patch('digest.datetime') as clock, \
             patch('db.communications_since', return_value=[]), \
             patch('db.pending_tasks', return_value=[task]), \
             patch('digest.plan_pending_tasks'), patch('digest.planning_agenda', return_value=[]), \
             patch('brain.summarize_day', return_value='Recap') as summary, \
             patch('jobs.submit') as submit:
            clock.now.return_value = self.now
            digest.send_daily_digest()
        text = submit.call_args.args[2]['text']
        for label in ('#7', '2000-01-01', 'solo data', 'default 09:00 locale', 'arretrata'):
            self.assertIn(label, text)
        self.assertIn('solo data', summary.call_args.kwargs['tasks'][0]['title'])
        self.assertIsNone(task['deadline'])


if __name__ == '__main__':
    unittest.main()