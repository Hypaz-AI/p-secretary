"""Regressioni offline per recap e pianificazione persistente a 48 ore."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import brain
import db
import digest
import jobs


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2099, 10, 24, 21, tzinfo=ZoneInfo('Europe/Rome'))
        self.until = self.now + timedelta(hours=48)
        self.connection = patch('db._conn')
        self.conn = self.connection.start().return_value.__enter__.return_value
        self.addCleanup(self.connection.stop)
        network = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def task(self, deadline=None, source=None):
        return dict(id=7, title='Inviare documenti', deadline=deadline, source_message=source)

    def test_empty_day_still_queues_plan_and_uses_exact_48_hours_at_dst(self):
        self.conn.execute.return_value.fetchall.return_value = []
        # Enabled digest flow; no previously sealed notification for this day.
        self.conn.execute.return_value.fetchone.side_effect = [(True,), None]
        with patch('digest.datetime') as clock, \
             patch('config.TELEGRAM_CHAT_ID', '7'), \
             patch('db.pending_tasks', return_value=[]) as pending, \
             patch('digest.plan_pending_tasks') as plan, \
             patch('db.appointments_between', return_value=[]) as agenda, \
             patch('brain.summarize_day') as summarize, \
             patch('jobs.enqueue', return_value=(1,)) as enqueue:
            clock.now.return_value = self.now
            self.assertTrue(digest.send_daily_digest())
        end = pending.call_args.args[0]
        self.assertEqual((end.astimezone(timezone.utc) - self.now.astimezone(timezone.utc)).total_seconds(), 48 * 3600)
        self.assertNotEqual(end.utcoffset(), self.now.utcoffset())
        plan.assert_called_once_with([], self.now, end)
        agenda.assert_called_once_with(self.now, end)
        summarize.assert_not_called()  # No consented native input: local recap only.
        enqueue.assert_called_once()
        payload = enqueue.call_args.args[3]
        self.assertIn('Nessuna fonte autorizzata per il riepilogo AI.', payload['text'])
        self.assertIn(end.strftime('%d/%m %H:%M'), payload['text'])
        self.assertFalse(payload['html'])
        self.assertEqual((payload['type'], payload['system'], payload['flow']),
                         ('notify', 'digest', 'digest'))
        self.assertEqual(payload['digest_day'], self.now.strftime('%Y-%m-%d'))
        seals = [c.args[1] for c in self.conn.execute.call_args_list
                 if 'INSERT INTO system_notifications' in c.args[0]]
        self.assertEqual(len(seals), 1)
        self.assertEqual(seals[0][:5], (payload['system_authorization'], payload['digest_day'],
                                      '7', enqueue.call_args.args[2], 1))
        self.assertEqual(json.loads(seals[0][5]), dict(text=payload['text'], html=False, recipient='7'))
        self.assertEqual(seals[0][-2:], ([], []))

    def test_digest_without_valid_recipient_never_queues_notification(self):
        for recipient in (None, '', '-7', 'invalid'):
            with self.subTest(recipient=recipient), patch('config.TELEGRAM_CHAT_ID', recipient), \
                 patch('jobs.enqueue') as enqueue:
                with self.assertRaisesRegex(jobs.Deferred, 'Digest recipient unavailable'):
                    digest._queue_notification(self.conn, '2099-10-24', 'Piano', [])
                enqueue.assert_not_called()
        self.conn.execute.assert_not_called()

    def test_pending_query_includes_old_and_undated_but_not_completed(self):
        self.conn.execute.return_value.fetchall.return_value = [(7, 'Old task', None, 3)]
        self.assertEqual(db.pending_tasks(self.until)[0]['id'], 7)
        sql, params = self.conn.execute.call_args.args
        self.assertIn("status='open'", sql)
        self.assertIn('deadline IS NULL', sql)
        self.assertNotIn('created_at', sql)
        self.assertEqual(params, (self.until,))

    def test_explicit_task_creates_stable_proposal_and_delayed_reminder(self):
        deadline = self.now + timedelta(hours=40)
        self.conn.execute.return_value.fetchone.side_effect = [(deadline, 0, 'Inviare documenti', None), None]
        with patch('jobs.enqueue', return_value=(1,)) as enqueue, \
             patch('connectors.gcal.validate_event', side_effect=lambda event: event), \
             patch('pipeline.save_event') as save:
            digest.plan_pending_tasks([self.task(deadline)], self.now, self.until)
        self.assertEqual(enqueue.call_args.args[2], 'task-reminder:7')
        self.assertEqual(save.call_args.args[2], 'task:7:v0')
        self.assertEqual(enqueue.call_args.args[3]['deadline'], deadline.isoformat())
        self.assertEqual(save.call_args.args[1]['dt'], deadline.isoformat())
        self.assertFalse(save.call_args.kwargs['approved'])  # A deadline is not Calendar intent.
        updates = [c for c in self.conn.execute.call_args_list if 'available_at=' in c.args[0]]
        self.assertEqual(updates[0].args[1][0], deadline - timedelta(hours=24))

    def test_external_task_remains_proposal(self):
        deadline = self.now + timedelta(hours=2)
        self.conn.execute.return_value.fetchone.side_effect = [(deadline, 2, 'Titolo corrente', 42), None]
        with patch('jobs.enqueue'), patch('connectors.gcal.validate_event', side_effect=lambda e: e), \
             patch('pipeline.save_event') as save:
            digest.plan_pending_tasks([self.task(deadline, source=42)], self.now, self.until)
        self.assertFalse(save.call_args.kwargs['approved'])
        self.assertEqual(save.call_args.args[2], 'task:7:v2')
        self.assertEqual(save.call_args.args[1]['title'], 'Titolo corrente')

    def test_existing_event_and_reminder_are_not_duplicated(self):
        deadline = self.now + timedelta(hours=2)
        self.conn.execute.return_value.fetchone.side_effect = [(deadline, 0, 'Inviare documenti', None), (99,)]
        with patch('jobs.enqueue', return_value=None), patch('pipeline.save_event') as save:
            digest.plan_pending_tasks([self.task(deadline)], self.now, self.until)
        save.assert_not_called()
        self.assertFalse(any('available_at=' in c.args[0] for c in self.conn.execute.call_args_list))

    def test_undated_closed_and_later_tasks_do_not_create_events(self):
        for row in (None, (None, 0, 'Titolo', None), (self.until + timedelta(hours=1), 0, 'Titolo', None)):
            with self.subTest(row=row):
                self.conn.execute.return_value.fetchone.return_value = row
                with patch('jobs.enqueue') as enqueue, patch('pipeline.save_event') as save:
                    digest.plan_pending_tasks([self.task()], self.now, self.until)
                enqueue.assert_not_called()
                save.assert_not_called()

    def test_overdue_task_gets_immediate_reminder_not_past_event(self):
        self.conn.execute.return_value.fetchone.return_value = (self.now - timedelta(days=3), 0, 'Titolo', None)
        with patch('jobs.enqueue', return_value=(1,)), patch('pipeline.save_event') as save:
            digest.plan_pending_tasks([self.task()], self.now, self.until)
        save.assert_not_called()
        self.assertEqual(self.conn.execute.call_args.args[1][0], self.now)

    def test_remote_agenda_filtered_and_local_proposals_preserved(self):
        local = [dict(dt=(self.now + timedelta(hours=1)).isoformat(), title='Proposta', sync_state='proposal')]
        remote = [dict(dt=(self.now + timedelta(hours=h)).isoformat(), title=str(h)) for h in (2, 60)]
        with patch('db.appointments_between', return_value=local), \
             patch('connectors.gcal.is_active', return_value=True), \
             patch('connectors.gcal.upcoming_events', return_value=remote):
            agenda = digest.planning_agenda(self.now, self.until)
        self.assertEqual([a['title'] for a in agenda], ['Proposta', '2'])

    def test_ai_receives_old_pending_tasks_and_fallback_keeps_plan(self):
        task = self.task(self.now - timedelta(days=5))
        appointment = dict(id=8, dt='2099-10-25 10:00', title='Visita', sync_state='proposal')
        for failure in (False, True):
            with self.subTest(failure=failure), \
                 patch('brain._chat', side_effect=RuntimeError('offline') if failure else None,
                       return_value='Recap') as chat:
                text = brain.summarize_day([], [appointment], tasks=[task],
                                           planning_start=self.now, planning_end=self.until)
            self.assertIn('#7 Inviare documenti', chat.call_args.args[1])
            self.assertIn('proposal', chat.call_args.args[1])
            if failure:
                self.assertIn('Visita', text)
                self.assertIn('#7 Inviare documenti', text)


if __name__ == '__main__':
    unittest.main()