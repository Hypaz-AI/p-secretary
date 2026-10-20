"""Regressioni offline del riepilogo serale (job durevole) e della pianificazione a 48 ore."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

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

    def test_empty_day_uses_neutral_text_without_calling_the_agent_and_exact_48_hours(self):
        self.conn.execute.return_value.fetchall.return_value = []
        queued = {}
        with patch('flows.enabled', return_value=True), \
             patch('digest.datetime') as clock, \
             patch('db.pending_tasks', return_value=[]) as pending, \
             patch('digest.plan_pending_tasks') as plan, \
             patch('digest.witnesses', return_value=([], [])), \
             patch('db.appointments_between', return_value=[]) as agenda, \
             patch('agent.turns.digest_text') as agent, \
             patch('digest._seal_sources', return_value=([], [])), \
             patch('digest._queue_notification', side_effect=lambda conn, day, text, sources, media:
                   queued.update(day=day, text=text)):
            clock.now.return_value = self.now
            clock.strptime = datetime.strptime
            digest.run({'day': '2099-10-24'})
        end = pending.call_args.args[0]
        self.assertEqual((end.astimezone(timezone.utc) - self.now.astimezone(timezone.utc)).total_seconds(), 48 * 3600)
        self.assertNotEqual(end.utcoffset(), self.now.utcoffset())
        plan.assert_called_once_with([], self.now, end)
        agenda.assert_called_once_with(self.now, end)
        agent.assert_not_called()  # nessuna fonte consentita: niente chiamata al modello
        self.assertIn(digest.NO_SOURCE_TEXT, queued['text'])
        self.assertIn(end.strftime('%d/%m %H:%M'), queued['text'])

    def test_agent_text_is_sealed_with_window_and_read_sources(self):
        self.conn.execute.return_value.fetchall.return_value = []
        captured = {}
        with patch('flows.enabled', return_value=True), \
             patch('db.pending_tasks', return_value=[]), patch('db.appointments_between', return_value=[]), \
             patch('digest.plan_pending_tasks'), \
             patch('digest.witnesses', side_effect=[([1, 2], [2]), ([1, 3], [])]), \
             patch('agent.turns.digest_text', return_value=('Riepilogo del giorno', [9])) as agent, \
             patch('digest._seal_sources', return_value=([1, 2, 3, 9], [2])) as seal, \
             patch('digest._queue_notification', side_effect=lambda conn, day, text, sources, media:
                   captured.update(text=text, sources=sources, media=media)):
            digest.run({'day': '2099-10-24'})
        agent.assert_called_once()
        before, after, touched = seal.call_args.args[1:]
        self.assertEqual((before, after, touched), (([1, 2], [2]), ([1, 3], []), [9]))
        self.assertIn('Riepilogo del giorno', captured['text'])
        self.assertEqual((captured['sources'], captured['media']), ([1, 2, 3, 9], [2]))

    def test_invalid_job_payload_fails_permanently_and_paused_flow_defers(self):
        for payload in (None, {}, {'day': 'oggi'}, {'day': '2099-13-40'}, {'day': 7}):
            with self.subTest(payload=payload), self.assertRaises((jobs.Permanent, ValueError)):
                digest.run(payload)
        with patch('flows.enabled', return_value=False), self.assertRaises(jobs.Deferred):
            digest.run({'day': '2099-10-24'})

    def test_enqueue_is_idempotent_per_day_and_never_sends(self):
        with patch('jobs.submit') as submit:
            digest.enqueue_digest(self.now)
        submit.assert_called_once_with('digest', 'digest:2099-10-24', {'day': '2099-10-24'})

    def test_scheduler_enqueues_once_at_the_configured_time(self):
        state = {}
        with patch('flows.enabled', return_value=True), patch('flows.mark'), \
             patch('jobs.cursor', return_value=None), patch('jobs.set_cursor') as cursor, \
             patch('digest.enqueue_digest', return_value=True) as enqueue, \
             patch('digest._parse_digest_time', return_value=(21, 0)), \
             patch('digest.datetime') as clock:
            clock.now.return_value = self.now.replace(hour=20)
            self.assertFalse(digest.maybe_send_digest(state))
            enqueue.assert_not_called()
            clock.now.return_value = self.now
            self.assertTrue(digest.maybe_send_digest(state))
        enqueue.assert_called_once()
        cursor.assert_called_once_with('last_digest_day', '2099-10-24')
        with patch('flows.enabled', return_value=True), patch('flows.mark'), \
             patch('jobs.cursor', side_effect=lambda key, default=None: '2099-10-24' if key == 'last_digest_day' else None), \
             patch('digest.enqueue_digest') as enqueue, patch('digest.datetime') as clock:
            clock.now.return_value = self.now
            self.assertFalse(digest.maybe_send_digest(state))
        enqueue.assert_not_called()

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


if __name__ == '__main__':
    unittest.main()