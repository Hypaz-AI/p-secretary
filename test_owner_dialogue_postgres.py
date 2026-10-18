"""Opt-in real SQL contracts for durable owner dialogue.

Only PI_SECRETARY_TEST_DATABASE_URL is used. Its explicit database name must
end in _test; every test owns a fresh schema (never public). The caller starts
PostgreSQL; this module never starts servers, workers, or external services.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import os
import threading
import unittest
from unittest.mock import patch
import uuid

import ai_budget
import brain
import capabilities
import config
import db
import execution
import jobs
import knowledge
import message_store
import owner_commitments
import owner_dialogue as od
import owner_instructions
import pipeline
import policy
import research
import secretary
import secretary_runtime
from connectors import media_archive


ANCHOR = datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc)


class OwnerDialoguePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
        if not cls.url:
            raise unittest.SkipTest('Set PI_SECRETARY_TEST_DATABASE_URL to opt in')
        if db.psycopg is None:
            raise unittest.SkipTest('psycopg is not installed')
        from psycopg.conninfo import conninfo_to_dict
        # Validate before connecting: never fall back to DATABASE_URL, PGDATABASE,
        # a libpq service, or a default database derived from the current user.
        params = conninfo_to_dict(cls.url)
        if not params.get('dbname', '').endswith('_test') or params.get('service'):
            raise ValueError('An explicit database ending in _test (no service) is required')

    def setUp(self):
        from psycopg import sql
        self.schema = 'owner_dialogue_test_' + uuid.uuid4().hex
        self.conn = db.psycopg.connect(self.url, autocommit=True,
                                      prepare_threshold=None, connect_timeout=5)
        self.addCleanup(lambda: self.conn.close())
        actual = self.conn.execute('SELECT current_database()').fetchone()[0]
        self.assertTrue(actual.endswith('_test'), 'Refusing a non-test database')
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.configure(self.conn)
        self.main_thread = threading.get_ident()
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch.object(config, 'TIMEZONE', 'Europe/Rome'))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:not-a-secret'))
        self.enterContext(patch.dict(os.environ, {'GCAL_CALENDAR_ID': 'owner-synthetic@group.calendar.google.com'}))
        self.migrate()
        self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed)
            VALUES ('telegram','ai_text',TRUE),('telegram','ai_media',TRUE)''')
        # SQL lifecycle tests do not exercise retrieval or practice inference.
        self.retrieve = self.enterContext(patch.object(od, 'retrieve', return_value=({}, {})))
        self.secretary_prepare = self.enterContext(patch.object(secretary, 'prepare', return_value=None))
        self.chat = self.enterContext(patch.object(brain, 'chat_with_user', return_value='Risposta salvata'))
        self.prepare = self.enterContext(patch.object(owner_commitments, 'prepare', return_value=[]))
        self.memory_prepare = self.enterContext(patch.object(brain, 'prepare_user_memories', return_value=dict(
            changes=[], known=[], status='nessuna modifica necessaria')))
        # Semantic intent classifier: scripted per test, never the real model.
        self.classify = self.enterContext(patch.object(brain, 'classify_intent', return_value=dict(
            memory=False, calendar=False, standing_instruction=False)))
        self.compact = self.enterContext(patch.object(od, 'compact'))
        self.enterContext(patch.object(brain, '_chat', side_effect=AssertionError('Unexpected model call')))
        self.enterContext(patch('ai_budget.request_content', side_effect=AssertionError('Real model forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.notify = self.enterContext(patch('connectors.telegram.notify',
                                              side_effect=AssertionError('Direct delivery forbidden')))

    def configure(self, conn):
        from psycopg import sql
        # No public fallback: missing migrations must fail rather than find
        # another installation's tables. pg_catalog remains implicitly visible.
        conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        conn.execute("SET timezone='UTC'")
        conn.execute("SET statement_timeout='15s'")
        conn.execute("SET lock_timeout='10s'")

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def migrate(self):
        # Storage's derived jobs and authorized chat context need full bootstrap.
        # _conn is already bound to this test schema, never the configured URL.
        with patch.object(db, 'available', return_value=True):
            db.init()

    @contextmanager
    def connection(self):
        # Main calls reuse an autocommit connection without its closing __exit__.
        # Concurrent workers must never share a psycopg connection/transaction.
        if threading.get_ident() == self.main_thread:
            yield self.conn
        else:
            with self.fresh_connection() as conn:
                yield conn

    def fresh_connection(self):
        conn = db.psycopg.connect(self.url, autocommit=True,
                                  prepare_threshold=None, connect_timeout=5)
        try:
            self.configure(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def restart_connection(self):
        self.conn.close()
        self.conn = self.fresh_connection()

    def archive(self, text='Domanda del proprietario', timestamp=ANCHOR):
        native_id = int(uuid.uuid4().hex[:12], 16)
        metadata = dict(account='bot:777', chat_id='101', author=101, direction='in',
                        chat_type='private', content_origin='direct_text',
                        is_forwarded=False, message_id=native_id)
        with self.conn.transaction():
            mid = message_store.store(
                self.conn, 'telegram', 'owner', '', text,
                ts=timestamp.isoformat() if timestamp else None,
                source_id=json.dumps(['bot', '777', '101', native_id]),
                metadata=metadata)
            jobs.enqueue(self.conn, 'owner', f'owner:{mid}', {'message_id': mid, 'text': text})
        return mid

    def completed(self, text='Messaggio ordinario', response='Risposta ordinaria'):
        mid = self.archive(text)
        self.conn.execute('''INSERT INTO owner_exchanges
            (message_id,message_time,user_text,response,completed_at)
            VALUES (%s,%s,%s,%s,now())''', (mid, ANCHOR, text, response))
        self.done(mid)
        return mid

    def done(self, mid):
        self.conn.execute("UPDATE work_jobs SET status='done',finished_at=now() WHERE key=%s",
                          (f'owner:{mid}',))

    def released(self):
        self.assertEqual(self.conn.execute('SELECT token,lease_until FROM owner_dialogue_state').fetchone(),
                         (None, None))

    def snapshot(self):
        return {table: self.conn.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()
                for table in ('tasks', 'task_changes', 'appointments', 'work_jobs', 'owner_exchanges')}

    def prepared(self, text, fields, *, evidence=None):
        # Build a genuinely validated batch, not a hand-crafted bypass of apply.
        extraction = {'clarification': '', 'changes': [dict(
            operation='create', task_id=None, changes=fields, evidence=evidence or text,
            kind='task', owner_request=True, certainty='certain', unique_match=True)]}
        with patch.object(owner_commitments, 'extract', return_value=extraction):
            return REAL_PREPARE(text, [], ANCHOR, tasks=[])

    def fail_outbox(self, prefix):
        self.conn.execute('''CREATE FUNCTION reject_test_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF starts_with(NEW.key, TG_ARGV[0]) THEN
                    RAISE EXCEPTION 'owner integration outbox failure';
                END IF;
                RETURN NEW;
            END $$''')
        from psycopg import sql
        self.conn.execute(sql.SQL('''CREATE TRIGGER reject_test_outbox BEFORE INSERT ON work_jobs
            FOR EACH ROW EXECUTE FUNCTION reject_test_outbox({})''').format(sql.Literal(prefix)))

    def test_migrations_twice_preserve_schema_data_and_single_version(self):
        mid = self.archive()
        od.respond('Domanda del proprietario', mid)
        before = self.snapshot()
        versions = self.conn.execute('SELECT version,reset_rows FROM schema_migrations ORDER BY version').fetchall()
        self.migrate()
        self.migrate()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute('SELECT version,reset_rows FROM schema_migrations ORDER BY version').fetchall(),
                         versions)
        self.assertEqual({row[0] for row in versions}, {
            message_store.VERSION, jobs.VERSION, jobs.RUNTIME_VERSION, media_archive.VERSION,
            od.VERSION, capabilities.VERSION, policy.VERSION, secretary.VERSION,
            knowledge.VERSION, secretary_runtime.VERSION, db.NOTES_VERSION,
            db.TYPED_NOTES_VERSION, research.VERSION, owner_instructions.VERSION,
            execution.VERSION, ai_budget.VERSION})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM pg_indexes WHERE schemaname=%s AND indexname='owner_exchange_search'",
                                          (self.schema,)).fetchone()[0], 1)
        self.released()

    def test_real_full_text_recall_finds_old_completed_turn_not_recent_or_future(self):
        old = self.completed('Il contratto zirconio è nel cassetto blu', 'Codice originale Z-42')
        for _ in range(101):
            self.completed()
        recent = self.completed('Contratto zirconio recente')
        incomplete = self.archive('Contratto zirconio incompleto')
        self.conn.execute('''INSERT INTO owner_exchanges(message_id,message_time,user_text)
            VALUES (%s,%s,'Contratto zirconio incompleto')''', (incomplete, ANCHOR))
        self.done(incomplete)
        current = self.archive('Dove trovo il contratto zirconio?')
        future = self.completed('Contratto zirconio futuro')
        ctx = od.load_context(current, 'contratto zirconio')
        self.assertNotIn(old, ctx['recent_ids'])
        self.assertIn(recent, ctx['recent_ids'])
        self.assertEqual([r['message_id'] for r in ctx['recalled']], [old])
        self.assertEqual(ctx['recalled'][0]['date'], str(ANCHOR))
        self.assertEqual(ctx['recalled'][0]['owner'], 'Il contratto zirconio è nel cassetto blu')
        self.assertEqual(ctx['recalled'][0]['assistant'], od.ASSISTANT_OMITTED)
        self.assertTrue(all(entry['role'] == 'user' for entry in ctx['history']))
        self.assertNotIn(future, ctx['recent_ids'])
        od.respond('Dove trovo il contratto zirconio?', current)
        dialogue = self.chat.call_args.kwargs['dialogue']
        self.assertEqual(dialogue['recalled'], ctx['recalled'])
        self.assertEqual(set(dialogue['authorized_context']),
                 {'communications', 'appointments', 'memories', 'rules'})
        sources = {(s['source_type'], s['source_id']) for s in dialogue['egress_sources']}
        self.assertTrue({('message', old), ('message', recent), ('message', current)} <= sources)
        self.assertNotIn(('message', future), sources)
        od.assert_egress_sources(dialogue['egress_sources'], conn=self.conn)
        self.assertNotIn('Codice originale Z-42', json.dumps(dialogue, default=str))
        # Masking affects model context, not the immutable archived exchange.
        self.assertEqual(self.conn.execute('SELECT response FROM owner_exchanges WHERE message_id=%s',
                          (old,)).fetchone()[0], 'Codice originale Z-42')

    def test_completed_restart_replay_has_no_ai_and_identical_chunked_outbox(self):
        text = 'Domanda originale'
        mid = self.archive(text)
        self.chat.return_value = 'à😀' * 4000
        response = od.respond(text, mid)
        self.assertEqual(response, self.chat.return_value)
        rows = self.conn.execute("SELECT key,payload FROM work_jobs WHERE kind='action' ORDER BY key").fetchall()
        self.assertEqual([row[0] for row in rows], [f'owner-reply:{mid}:{i}' for i in range(3)])
        self.assertEqual(''.join(row[1]['text'] for row in rows), response)
        self.assertEqual([row[1] for row in rows], [
            dict(type='notify', text=response[start:start + 3500], html=False,
                  message_id=mid, owner_reply_mid=mid, owner_reply_chunk=i)
            for i, start in enumerate(range(0, len(response), 3500))])
        self.conn.execute("UPDATE work_jobs SET status='done',finished_at=now() WHERE kind='action'")
        before = self.snapshot()
        self.restart_connection()
        self.chat.reset_mock()
        self.prepare.reset_mock()
        self.compact.reset_mock()
        self.chat.side_effect = AssertionError('Replay must not call AI')
        self.prepare.side_effect = AssertionError('Replay must not extract')
        for _ in range(2):
            self.assertEqual(od.respond('Changed retry text must not replace original', mid), response)
        self.assertEqual(self.snapshot(), before)
        self.chat.assert_not_called()
        self.prepare.assert_not_called()
        self.compact.assert_not_called()
        self.notify.assert_not_called()
        self.released()

    def test_owner_reply_retry_blocks_later_chunks_and_turns_not_other_actions(self):
        # Test outbox ordering, not uncertain-receipt reconciliation/retry rules.
        self.enterContext(patch.object(policy, 'prepare_effect', return_value=True))
        self.enterContext(patch.object(policy, 'record_receipt', return_value=True))
        first_mid = self.archive('Prima risposta')
        later_mid = self.archive('Risposta successiva')
        od._deliver(self.conn, first_mid, 'a' * 3501)
        od._deliver(self.conn, later_mid, 'Later turn')
        rows = self.conn.execute("SELECT id,payload FROM work_jobs WHERE kind='action' ORDER BY id").fetchall()
        self.notify.side_effect = None
        self.notify.return_value = False
        first = jobs.claim('action')
        self.assertEqual(first[0], rows[0][0])
        with self.assertRaisesRegex(RuntimeError, 'not acknowledged'):
            pipeline.action(first[1])
        jobs.finish(first, 'Notification not acknowledged')
        for status in ('pending', 'running'):
            self.conn.execute('UPDATE work_jobs SET status=%s WHERE id=%s', (status, first[0]))
            for _, payload in rows[1:]:
                with self.subTest(status=status, chunk=payload['owner_reply_chunk']), \
                     self.assertRaisesRegex(jobs.Deferred, 'Earlier owner reply'):
                    pipeline.action(payload)
        self.notify.assert_called_once()
        self.notify.return_value = True
        pipeline.action(dict(type='notify', text='Unrelated urgent notification', message_id=first_mid))
        # The retry excludes itself even while running; later replies wait for finish.
        self.conn.execute("UPDATE work_jobs SET status='pending',available_at=now()-interval '1 minute' WHERE id=%s", (first[0],))
        retry = jobs.claim('action')
        self.assertEqual(retry[0], first[0])
        pipeline.action(retry[1])
        jobs.finish(retry)
        pipeline.action(rows[1][1])
        self.conn.execute("UPDATE work_jobs SET status='failed' WHERE id=%s", (rows[1][0],))
        pipeline.action(rows[2][1])
        self.assertEqual([call.args[0] for call in self.notify.call_args_list],
                         ['a' * 3500, 'Unrelated urgent notification', 'a' * 3500, 'a', 'Later turn'])

    def test_note_and_reply_commit_together_and_replay_does_not_duplicate(self):
        text = 'Ricorda la nota: per le fatture usa la cartella mensile.'
        mid = self.archive(text)
        self.classify.return_value = dict(memory=True, calendar=False, standing_instruction=False)
        self.memory_prepare.return_value = dict(known=[], status='piano preparato', changes=[
            dict(operation='add', id=None, text='Archiviare le fatture nella cartella mensile.')])
        self.fail_outbox('owner-reply:')
        with self.assertRaises(db.psycopg.errors.RaiseException):
            od.respond(text, mid)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM notes').fetchone()[0], 0)
        self.assertIsNone(self.conn.execute('SELECT response FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()[0])
        self.conn.execute('DROP TRIGGER reject_test_outbox ON work_jobs')
        response = od.respond(text, mid)
        self.assertIn('Memoria personale aggiornata', response)
        self.restart_connection()
        self.assertEqual(od.respond(text, mid), response)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM notes').fetchone()[0], 1)

    def test_sql_failure_rolls_back_effects_and_reply_but_restart_reuses_plan(self):
        # Calendar has no blanket grant. This explicit native intent seals one
        # event; an outbox SQL failure rolls back the task, event and grant.
        task_text = 'Devo pagare il contratto domani'
        text = task_text + '. Crea un appuntamento in agenda.'
        mid = self.archive(text)
        self.assertFalse(policy.can_operate(self.conn, mid, 'calendar_write', owner_required=True))
        commitments = self.prepared(text, {'title': task_text, 'owner_deadline_date': '2026-01-02'},
                                    evidence=task_text)
        event = pipeline.gcal.validate_event(dict(title='Controllo', dt='2030-01-02 15:00',
                                                 location='Ufficio', notes='Test'))
        # A saved, previously validated combined plan exercises atomic
        # task/event rollback; fresh task extraction now suppresses events.
        token, _ = od.acquire(mid, text, False)
        self.conn.execute('UPDATE owner_exchanges SET plan=%s WHERE message_id=%s', (
            json.dumps(dict(reply='Piano salvato', commitments=commitments, events=[event],
                            task_error='', current_text=text,
                            intent=dict(memory=False, calendar=True, standing_instruction=False))), mid))
        self.conn.execute('UPDATE owner_dialogue_state SET token=NULL,lease_until=NULL WHERE token=%s', (token,))
        self.fail_outbox('owner-reply:')
        before = self.snapshot()
        with self.assertRaisesRegex(db.psycopg.errors.RaiseException, 'owner integration outbox failure'):
            od.respond(text, mid)
        after = self.snapshot()
        for table in ('tasks', 'task_changes', 'appointments', 'work_jobs'):
            self.assertEqual(after[table], before[table], table)
        saved = self.conn.execute('SELECT plan,response,completed_at FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()
        self.assertIsNotNone(saved[0])
        self.assertEqual(saved[0]['events'], [event])
        self.assertEqual(saved[1:], (None, None))
        self.released()
        self.conn.execute('DROP TRIGGER reject_test_outbox ON work_jobs')
        self.conn.execute('DROP FUNCTION reject_test_outbox()')
        self.restart_connection()
        with patch.object(brain, 'chat_with_user', side_effect=AssertionError('Persisted plan calls AI')), \
             patch.object(owner_commitments, 'prepare', side_effect=AssertionError('Persisted plan extracts')):
            response = od.respond(text, mid)
        self.assertIn('Attività #', response)
        self.assertIn('sincronizzazione Calendar in coda, non ancora confermata', response)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks WHERE owner_origin_message=%s', (mid,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT sync_state FROM appointments WHERE sync_key=%s',
                          (f'owner:{mid}:event:0',)).fetchall(), [('pending',)])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM calendar_effect_grants').fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT key,payload->>'text' FROM work_jobs WHERE kind='action' AND key LIKE 'owner-reply:%%'").fetchall(),
                         [(f'owner-reply:{mid}:0', response)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action' AND payload->>'type'='calendar'").fetchone()[0], 1)
        self.done(mid)
        before_replay = self.snapshot()
        self.assertEqual(od.respond(text, mid), response)
        self.assertEqual(self.snapshot(), before_replay)

    def test_saved_plan_restart_after_consent_revoked_uses_no_new_model(self):
        text = 'Devo pagare il contratto domani'
        mid = self.archive(text)
        self.prepare.return_value = self.prepared(text, {
            'title': 'Pagare contratto', 'owner_deadline_date': '2026-01-02'})
        self.fail_outbox('owner-reply:')
        with self.assertRaises(db.psycopg.errors.RaiseException):
            od.respond(text, mid)
        saved = self.conn.execute('SELECT plan,response FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()
        self.assertIsNotNone(saved[0])
        self.assertIsNone(saved[1])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        self.conn.execute('DROP TRIGGER reject_test_outbox ON work_jobs')
        self.conn.execute("UPDATE data_policies SET allowed=FALSE WHERE channel='telegram'")
        self.restart_connection()
        for mocked in (self.chat, self.prepare, self.memory_prepare, self.retrieve, self.secretary_prepare):
            mocked.reset_mock()
            mocked.side_effect = AssertionError('Saved plan must not infer after consent revocation')
        response = od.respond(text, mid)
        self.assertIn('Attività #', response)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks WHERE owner_origin_message=%s', (mid,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT plan FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()[0], saved[0])
        for mocked in (self.chat, self.prepare, self.memory_prepare, self.retrieve, self.secretary_prepare):
            mocked.assert_not_called()
        self.released()

    def test_concurrent_connections_only_one_lease_and_expired_token_is_fenced(self):
        mid = self.archive()
        barrier = threading.Barrier(2)

        def acquire():
            barrier.wait(timeout=5)
            try:
                return od.acquire(mid, 'Domanda del proprietario', False)
            except jobs.Deferred as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(acquire) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        winners = [r for r in results if isinstance(r, tuple)]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual([r for r in results if isinstance(r, str)], ['Owner dialogue busy'])
        stale = winners[0][0]
        self.assertEqual(self.conn.execute('SELECT count(*) FROM owner_exchanges').fetchone()[0], 1)
        self.conn.execute("UPDATE owner_dialogue_state SET lease_until=now()-interval '1 second'")
        token, _ = od.acquire(mid, 'Retry', False)
        self.assertNotEqual(stale, token)
        with self.assertRaises(jobs.Deferred), self.conn.transaction():
            od._fence(self.conn, stale)
        # A stale worker's finally/release cannot unlock its replacement.
        self.conn.execute('UPDATE owner_dialogue_state SET token=NULL,lease_until=NULL WHERE token=%s', (stale,))
        self.assertEqual(self.conn.execute('SELECT token FROM owner_dialogue_state').fetchone()[0], token)
        with self.conn.transaction():
            od._fence(self.conn, token)

    def test_earlier_owner_job_blocks_later_turn_until_finished(self):
        first = self.archive('Prima domanda')
        second = self.archive('Seconda domanda')
        for status in ('pending', 'running'):
            with self.subTest(status=status):
                self.conn.execute('UPDATE work_jobs SET status=%s WHERE key=%s', (status, f'owner:{first}'))
                with self.assertRaisesRegex(jobs.Deferred, 'Earlier'):
                    od.respond('Seconda domanda', second)
                self.assertEqual(self.conn.execute('SELECT count(*) FROM owner_exchanges').fetchone()[0], 0)
                self.released()
        self.chat.assert_not_called()
        self.done(first)
        self.assertEqual(od.respond('Seconda domanda', second), 'Risposta salvata')

    def test_native_timestamp_and_missing_timestamp_fallback_are_persisted(self):
        for timestamp in (ANCHOR, None):
            with self.subTest(timestamp=timestamp):
                mid = self.archive(timestamp=timestamp)
                expected = self.conn.execute('SELECT COALESCE(ts,acquired_at) FROM communications WHERE id=%s', (mid,)).fetchone()[0]
                od.respond('Domanda del proprietario', mid)
                self.assertEqual(self.prepare.call_args.args[2], expected)
                self.assertEqual(self.chat.call_args.kwargs['dialogue']['message_time'], str(expected))
                self.assertEqual(self.conn.execute('SELECT message_time FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()[0], expected)
                self.done(mid)

    def test_date_only_and_explicit_reminders_schedule_and_fence_real_delivery(self):
        # January 1 locally, despite December 31 UTC: no processing-time anchor.
        cases = [
            ('Devo pagare la fattura domani', {'title': 'Pagare fattura', 'owner_deadline_date': '2026-01-02'}),
            ('Devo consegnare il rapporto e ricordami alle 10:00',
             {'title': 'Consegnare rapporto', 'owner_deadline_date': '2099-03-29',
              'owner_remind_at': '2099-03-29T10:00:00+02:00'}),
        ]
        ids = []
        for text, fields in cases:
            mid = self.archive(text)
            self.prepare.return_value = self.prepared(text, fields)
            response = od.respond(text, mid)
            self.assertEqual(self.prepare.call_args.args[2], ANCHOR)
            self.assertIn('solo data', response)
            row = self.conn.execute('''SELECT id,deadline,owner_deadline_date,owner_remind_at,
                source_message,owner_origin_message,owner_operation_key FROM tasks
                WHERE owner_origin_message=%s''', (mid,)).fetchone()
            ids.append(row[0])
            self.assertIsNone(row[1])
            self.assertEqual(row[2], date.fromisoformat(fields['owner_deadline_date']))
            self.assertEqual(row[3], datetime.fromisoformat(fields['owner_remind_at']) if 'owner_remind_at' in fields else None)
            self.assertEqual(row[4:], (None, mid, f'owner-commitment:{mid}:0'))
            self.done(mid)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM appointments').fetchone()[0], 0)
        with patch('flows.enabled', return_value=False):
            pipeline.schedule()
            pipeline.schedule()
        rows = self.conn.execute("SELECT key,payload,available_at FROM work_jobs WHERE key LIKE 'owner-reminder:%' ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        for identity, (key, payload, available) in zip(ids, rows):
            self.assertEqual(key, f'owner-reminder:{identity}:v0')
            self.assertEqual(payload['id'], identity)
            self.assertEqual(payload['version'], 0)
            self.assertTrue(payload['owner_reminder'])
            self.assertGreaterEqual(available, datetime.fromisoformat(payload['remind_at']))
        self.assertTrue(rows[0][1]['remind_at'].endswith('09:00:00+01:00'))
        self.assertEqual(rows[1][2], datetime.fromisoformat('2099-03-29T10:00:00+02:00'))
        # Old date is due; future explicit time must not send early.
        with patch('connectors.telegram.notify', return_value=True) as notify:
            pipeline.action(rows[1][1])
            notify.assert_not_called()
            pipeline.action(rows[0][1])
            notify.assert_called_once()
            self.assertIn('default 09:00 locale', notify.call_args.args[0])
            self.conn.execute('UPDATE tasks SET version=version+1 WHERE id=%s', (ids[0],))
            pipeline.action(rows[0][1])
            notify.assert_called_once()
            self.conn.execute("UPDATE tasks SET status='done' WHERE id=%s", (ids[0],))
            pipeline.action(dict(rows[0][1], version=1))
            notify.assert_called_once()
        self.conn.execute("UPDATE work_jobs SET status='done' WHERE key=%s", (rows[1][0],))
        before = self.conn.execute('SELECT * FROM work_jobs WHERE key=%s', (rows[1][0],)).fetchone()
        with patch('flows.enabled', return_value=False):
            pipeline.schedule()
        self.assertEqual(self.conn.execute('SELECT * FROM work_jobs WHERE key=%s', (rows[1][0],)).fetchone(), before)

    def test_real_compaction_sql_failure_preserves_checkpoint_then_retry_and_fence(self):
        old = self.completed('Contratto zirconio originale', 'Conservare integralmente')
        for _ in range(104):
            self.completed()
        mid = self.archive()
        token, _ = od.acquire(mid, 'Domanda del proprietario', False)
        ctx = od.load_context(mid, 'contratto zirconio')
        self.assertNotIn(old, ctx['recent_ids'])
        self.conn.execute('''CREATE FUNCTION reject_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.summarized_through <> OLD.summarized_through THEN
                    RAISE EXCEPTION 'checkpoint SQL failure';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON owner_dialogue_state
            FOR EACH ROW EXECUTE FUNCTION reject_checkpoint()''')
        with patch.object(brain, '_chat', return_value='Riepilogo durevole') as summary:
            with self.assertLogs(od.logger, level='WARNING') as logs:
                REAL_COMPACT(mid, ctx, token)
            self.assertIn('checkpoint SQL failure', '\n'.join(logs.output))
            self.assertEqual(self.conn.execute('SELECT summary,summarized_through FROM owner_dialogue_state').fetchone(), ('', 0))
            self.conn.execute('DROP TRIGGER reject_checkpoint ON owner_dialogue_state')
            REAL_COMPACT(mid, ctx, 'stale-token')
            self.assertEqual(self.conn.execute('SELECT summarized_through FROM owner_dialogue_state').fetchone()[0], 0)
            REAL_COMPACT(mid, ctx, token)
            entries = json.loads(summary.call_args.args[1])['exchanges']
            self.assertEqual(int(entries[0][0]), old)
            self.assertTrue(all(int(entry[0]) < min(ctx['recent_ids']) for entry in entries))
            checkpoint = int(entries[-1][0])
            self.assertEqual(self.conn.execute('SELECT summary,summarized_through FROM owner_dialogue_state').fetchone(), ('Riepilogo durevole', checkpoint))
            updated = od.load_context(mid, 'contratto zirconio')
            REAL_COMPACT(mid, updated, token)
            self.assertEqual(summary.call_count, 3)
        self.assertEqual(self.conn.execute('SELECT user_text,response FROM owner_exchanges WHERE message_id=%s', (old,)).fetchone(),
                         ('Contratto zirconio originale', 'Conservare integralmente'))
        self.assertEqual(updated['recalled'][0]['message_id'], old)


REAL_PREPARE = owner_commitments.prepare
REAL_COMPACT = od.compact


if __name__ == '__main__':
    unittest.main()