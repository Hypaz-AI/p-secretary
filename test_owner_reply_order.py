"""Offline delivery ordering contracts using an in-memory SQL outbox."""
import os
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import config
import db
import jobs
import pipeline
import policy


class OwnerReplyOrderTests(unittest.TestCase):
    def setUp(self):
        self.sql = sqlite3.connect(':memory:')
        self.addCleanup(self.sql.close)
        self.sql.execute('''CREATE TABLE work_jobs (
            id INTEGER PRIMARY KEY, kind TEXT, key TEXT UNIQUE, status TEXT,
            available_at TEXT, attempts INTEGER DEFAULT 0)''')
        self.conn = MagicMock()
        self.conn.execute.side_effect = lambda sql, params=(): self.sql.execute(
            sql.replace('%s', '?'), params)
        connect = self.enterContext(patch.object(db, '_conn'))
        connect.return_value.__enter__.return_value = self.conn
        self.connect = connect
        self.notify = self.enterContext(patch.object(pipeline.telegram, 'notify', return_value=True))
        # Keep the real ordering query and delivery path, but isolate PostgreSQL
        # policy/receipt persistence from this SQLite outbox-only fixture.
        self.gate = self.enterContext(patch('policy.can_operate', side_effect=lambda conn, mid, effect, **kw:
            effect in {'notify', 'owner_command'}))
        self.effect = self.enterContext(patch('policy.prepare_effect', return_value=True))
        self.receipt = self.enterContext(patch('policy.record_receipt', return_value=True))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        if db.psycopg is not None:
            self.enterContext(patch.object(db.psycopg, 'connect', side_effect=AssertionError('PostgreSQL forbidden')))

    def reply(self, identity, mid, chunk, status='pending'):
        self.insert(identity, f'owner-reply:{mid}:{chunk}', status)
        return dict(type='notify', text=f'{mid}/{chunk}', html=False,
                    owner_reply_mid=mid, owner_reply_chunk=chunk)

    def insert(self, identity, key, status='pending', kind='action'):
        self.sql.execute('INSERT INTO work_jobs(id,kind,key,status) VALUES (?,?,?,?)',
                         (identity, kind, key, status))

    def status(self, identity, status):
        self.sql.execute('UPDATE work_jobs SET status=? WHERE id=?', (status, identity))

    def test_pending_backoff_and_running_earlier_chunk_block_without_sending(self):
        self.reply(1, 10, 0)
        later = self.reply(2, 10, 1, 'running')
        self.sql.execute("UPDATE work_jobs SET available_at='2099-01-01',attempts=7 WHERE id=1")
        for status in ('pending', 'running'):
            with self.subTest(status=status):
                self.status(1, status)
                with self.assertRaisesRegex(jobs.Deferred, 'Earlier owner reply'):
                    pipeline.action(later)
        self.notify.assert_not_called()
        self.assertEqual(self.sql.execute('SELECT attempts FROM work_jobs WHERE id=1').fetchone(), (7,))

    def test_failed_send_holds_later_chunks_and_turns_until_success(self):
        first = self.reply(1, 90, 0, 'running')
        chunk = self.reply(2, 90, 1)
        turn = self.reply(3, 4, 0)
        self.notify.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'not acknowledged'):
            pipeline.action(first)
        self.status(1, 'pending')  # jobs.finish schedules a retry after send failure.
        self.notify.reset_mock()
        for later in (chunk, turn):
            with self.assertRaises(jobs.Deferred):
                pipeline.action(later)
        self.notify.assert_not_called()
        self.notify.return_value = True
        self.status(1, 'running')
        pipeline.action(first)
        self.status(1, 'done')
        pipeline.action(chunk)
        self.status(2, 'done')
        pipeline.action(turn)
        self.assertEqual([call.args[0] for call in self.notify.call_args_list], ['90/0', '90/1', '4/0'])

    def test_terminal_earlier_jobs_do_not_block(self):
        self.reply(1, 10, 0, 'done')
        self.reply(2, 10, 1, 'failed')
        current = self.reply(3, 20, 0, 'running')
        pipeline.action(current)
        self.notify.assert_called_once_with('20/0', html=False)

    def test_self_and_later_jobs_do_not_block_and_query_is_parameterized(self):
        current = self.reply(10, 99, 12, 'running')
        self.reply(11, 1, 0, 'running')
        pipeline.action(current)
        self.notify.assert_called_once_with('99/12', html=False)
        sql, params = self.conn.execute.call_args.args
        self.assertEqual(params, ('owner-reply:%', 'owner-reply:99:12'))
        self.assertNotIn('owner-reply:99:12', sql)
        self.assertIn('id < (SELECT id FROM work_jobs WHERE key=%s)', sql)

    def test_earlier_unrelated_pending_jobs_do_not_block_owner_reply(self):
        self.insert(1, 'notify:1')
        self.insert(2, 'calendar:owner:1', 'running')
        self.insert(3, 'owner:1', kind='owner')
        pipeline.action(self.reply(4, 10, 0, 'running'))
        self.notify.assert_called_once_with('10/0', html=False)

    def test_unrelated_notify_never_checks_owner_queue(self):
        self.reply(1, 10, 0, 'running')
        pipeline.action(dict(type='notify', text='urgent'))
        self.notify.assert_called_once_with('urgent', html=True)
        # Receipts need DB contexts, but unrelated sends never query the outbox.
        self.conn.execute.assert_not_called()
        self.effect.assert_called_once()
        self.assertEqual([c.args[2] for c in self.receipt.call_args_list], ['attempted', 'accepted'])

    def test_calendar_is_not_held_by_owner_reply(self):
        self.reply(1, 10, 0, 'running')
        self.conn.execute.side_effect = None
        key = 'owner:42:event:0'
        event = dict(title='Visit', dt='2030-01-01 10:00')
        row = (jobs.event_id(key), event, 'pending', key)
        self.conn.execute.return_value.fetchone.return_value = row
        authorization = 'calendar:offline-owner-grant'
        with patch.dict(os.environ, {'GCAL_CALENDAR_ID': 'owner-reply-order@group.calendar.google.com'}), \
             patch.object(config, 'TIMEZONE', 'Europe/Rome'), \
             patch.object(config, 'GCAL_EVENT_DURATION', 60), \
             patch.object(pipeline.gcal, 'is_active', return_value=True) as ready, \
             patch.object(pipeline, '_calendar_io', return_value=True) as sync:
            effect_payload = policy.calendar_payload(row)
            self.assertEqual(effect_payload['target'], dict(
                calendar_id='owner-reply-order@group.calendar.google.com', timezone='Europe/Rome',
                duration_min=60, reminder_minutes=30,
                start='2030-01-01T10:00:00+01:00', end='2030-01-01T11:00:00+01:00'))
            digest = policy.payload_hash(effect_payload)
            # Isolate grant persistence, not effect_allowed: only the explicit
            # owner/token and the exact sealed target/payload may authorize I/O.
            with patch('policy._calendar_allowed', side_effect=lambda conn, mid, hashed, token:
                       mid == 42 and hashed == digest and token == authorization) as grant:
                pipeline.action(dict(type='calendar', id=2, authority_message_id=42,
                                     authorization=authorization))
        ready.assert_called_once_with()
        sync.assert_called_once_with(effect_payload, 'create')
        self.effect.assert_called_once_with(self.conn, f'{authorization}:synced',
                                            42, 'calendar', effect_payload,
                                            authorization=authorization)
        self.assertEqual([c.args[1:] for c in grant.call_args_list],
                         [(42, digest, authorization)] * 4)
        self.assertEqual([c.args for c in self.receipt.call_args_list],
                         [(self.conn, f'{authorization}:synced', 'attempted'),
                          (self.conn, f'{authorization}:synced', 'accepted')])
        self.assertFalse(any('work_jobs' in call.args[0] for call in self.conn.execute.call_args_list))
        self.notify.assert_not_called()

    def test_calendar_owner_sync_key_does_not_replace_explicit_grant(self):
        self.reply(1, 10, 0, 'running')
        self.conn.execute.side_effect = None
        key = 'owner:42:event:0'
        event = dict(title='Visit', dt='2030-01-01 10:00')
        row = (jobs.event_id(key), event, 'pending', key)
        self.conn.execute.return_value.fetchone.return_value = row
        authorization = 'calendar:offline-owner-grant'
        with patch.dict(os.environ, {'GCAL_CALENDAR_ID': 'owner-reply-order@group.calendar.google.com'}), \
             patch.object(config, 'TIMEZONE', 'Europe/Rome'), \
             patch.object(config, 'GCAL_EVENT_DURATION', 60), \
             patch.object(pipeline.gcal, 'is_active', return_value=True) as ready, \
             patch.object(pipeline, '_calendar_io', return_value=True) as sync, \
             patch('policy._calendar_allowed', return_value=False) as grant:
            digest = policy.payload_hash(policy.calendar_payload(row))
            for authority in ({}, dict(authority_message_id=42),
                              dict(authorization=authorization),
                              dict(authority_message_id=42, authorization=authorization)):
                with self.subTest(authority=authority), \
                     self.assertRaisesRegex(jobs.Deferred, '^Calendar authorization changed$'):
                    pipeline.action(dict(type='calendar', id=2, **authority))
            # Il digest atteso è quello dell'operazione create (stato pending
            # su evento non synced): il worker la sigilla come create.
            grant.assert_called_once_with(self.conn, 42, digest, authorization)
        ready.assert_not_called()
        sync.assert_not_called()
        self.effect.assert_not_called()
        self.receipt.assert_not_called()
        self.assertFalse(any('work_jobs' in call.args[0] for call in self.conn.execute.call_args_list))
        self.notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()