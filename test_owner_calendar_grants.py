"""Offline Calendar adapter regressions, including real sealed grants and SQL.

SQL requires PI_SECRETARY_TEST_DATABASE_URL naming an explicit *_test database,
with no libpq service. No server is started and DATABASE_URL is never used.
Models, HTTP, direct Telegram and Calendar delivery are forbidden throughout.
"""
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import uuid

with tempfile.TemporaryDirectory(prefix='calendar-owner-import-') as _bootstrap:
    with patch.dict(os.environ, {'PI_SECRETARY_CONFIG_FILE': '/dev/null',
                                'DATA_DIR': _bootstrap, 'DATABASE_URL': ''}):
        import brain
        import config
        import db
        import jobs
        import message_store
        import owner_commitments
        import owner_dialogue as od
        import pipeline
        import policy
        import secretary
        import task_reconciliation
        from connectors import gcal
        import test_calendar_destination_binding as calendar_sql


ANCHOR = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
TEXT = 'Crea un appuntamento in agenda per la visita.'
EVENT = dict(title='Visita', dt='2030-01-02 15:00', location='Studio', notes='Controllo',
             duration_min=45, reminder_minutes=15)


def context():
    return dict(history=[], recent_ids=[], summary=od.CONTEXT_OMITTED,
                summarized_through=0, recalled=[], tasks=[])


def plan(text=TEXT):
    return dict(reply='Risposta informativa', events=[dict(EVENT)], commitments=[],
                task_error='', current_text=text)


class CalendarAdapterTests(unittest.TestCase):
    """Pure adapter contracts remain runnable without PostgreSQL binaries."""

    def setUp(self):
        self.mid, self.text = 101, TEXT
        self.conn = MagicMock()
        self.conn.transaction.side_effect = lambda: nullcontext(self.conn)
        self.conn.execute.side_effect = self.execute
        self.state = 'proposal'
        self.enterContext(patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)))
        if db.psycopg is not None:
            self.enterContext(patch.object(db.psycopg, 'connect',
                side_effect=AssertionError('Configured database forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('ai_budget.request_content', side_effect=AssertionError('Model forbidden')))
        self.enterContext(patch.object(gcal, 'create_event', side_effect=AssertionError('Calendar forbidden')))
        self.enterContext(patch.object(gcal, 'delete_event', side_effect=AssertionError('Calendar forbidden')))
        self.enterContext(patch('connectors.telegram.notify', side_effect=AssertionError('Direct send forbidden')))
        self.gate = self.enterContext(patch.object(policy, 'can_operate',
            side_effect=lambda conn, mid, effect, **kw: effect == 'owner_command'))
        self.metadata = self.enterContext(patch.object(policy, 'metadata_for', return_value=dict(
            metadata=dict(content_origin='direct_text', is_forwarded=False))))
        self.saved = plan()
        self.acquire = self.enterContext(patch.object(od, 'acquire',
            side_effect=lambda *args: ('owner-lease', (ANCHOR, self.saved, None))))
        self.enterContext(patch.object(od, 'load_context', side_effect=lambda *args: context()))
        self.enterContext(patch.object(od, 'compact'))
        self.apply = self.enterContext(patch.object(owner_commitments, 'apply',
            return_value=dict(confirmations=[])))
        self.save = self.enterContext(patch.object(pipeline, 'save_event', return_value=42))
        self.enqueue = self.enterContext(patch.object(jobs, 'enqueue'))
        self.event_hash = self.enterContext(patch.object(policy, 'calendar_event_hash', return_value='event-hash'))
        self.grant = self.enterContext(patch.object(policy, 'grant_calendar', return_value='calendar:sealed'))

    def execute(self, sql, params=()):
        row = None
        if sql.startswith('SELECT body FROM telegram_messages'):
            row = (self.text,)
        elif 'FROM owner_dialogue_state' in sql and 'FOR UPDATE' in sql:
            row = (1,)
        elif sql.startswith('SELECT sync_state FROM appointments'):
            row = (self.state,) if self.state is not None else None
        return MagicMock(**{'fetchone.return_value': row})

    def test_natural_create_requests_specific_approval_and_reports_persisted_state(self):
        for state, receipt in (
                ('pending', 'sincronizzazione Calendar in coda, non ancora confermata'),
                ('synced', 'sincronizzato con Calendar'),
                ('proposal', 'proposta locale; Calendar non autorizzato'),
                ('local', 'proposta locale; Calendar non autorizzato'),
                ('cancel_pending', 'annullamento Calendar in coda, non ancora confermato'),
                ('cancelled', 'annullato su Calendar'),
                (None, 'stato Calendar non confermato')):
            with self.subTest(state=state):
                self.state = state
                self.save.reset_mock()
                response = od.respond(TEXT, self.mid)
                self.save.assert_called_once_with(self.conn, EVENT,
                    f'owner:{self.mid}:event:0', 'dialogo utente', approved=True)
                self.assertIn(receipt, response)
                self.assertNotIn(self.saved['reply'], response)
        self.assertFalse(any(c.args[2] == 'calendar_write' for c in self.gate.call_args_list))

    def test_commands_hash_and_grant_same_operation_without_unsealed_enqueue(self):
        for command, operation in (('/conferma', 'create'), ('/annulla', 'cancel')):
            with self.subTest(command=command):
                self.text = f'{command} 42'
                self.event_hash.reset_mock()
                self.grant.reset_mock()
                self.enqueue.reset_mock()
                self.assertTrue(od.command(self.text, self.mid))
                self.event_hash.assert_called_once_with(self.conn, 42, operation=operation)
                self.grant.assert_called_once_with(self.conn, 42, 'event-hash', self.mid,
                                                    operation=operation)
                self.enqueue.assert_called_once()
                self.assertEqual(self.enqueue.call_args.args[2], f'owner-reply:{self.mid}:0')
                self.assertIn('in coda, non ancora confermata', self.enqueue.call_args.args[3]['text'])
        self.assertFalse(any('UPDATE appointments' in c.args[0] for c in self.conn.execute.call_args_list))
        self.assertFalse(any(c.args[2] == 'calendar_write' for c in self.gate.call_args_list))

    def test_command_denial_is_not_a_queued_receipt(self):
        self.grant.return_value = None
        for command in ('/conferma', '/annulla'):
            with self.subTest(command=command):
                response = od._command_effect(self.conn, self.mid, command, '42', None)
                self.assertIn('non autorizzata', response)
                self.assertNotIn('in coda', response)
        self.enqueue.assert_not_called()

    def test_missing_or_legacy_invalid_event_does_not_mint_grant(self):
        self.event_hash.side_effect = ValueError('Invalid calendar snapshot')
        for command in ('/conferma', '/annulla'):
            response = od._command_effect(self.conn, self.mid, command, '42', None)
            self.assertIn('non trovato o non valido', response)
        self.grant.assert_not_called()
        self.enqueue.assert_not_called()

    def test_invalid_and_out_of_range_command_ids_never_reach_policy(self):
        for argument in ('', '-1', '42 OR 1=1', '４２', '0', str(2 ** 63)):
            with self.subTest(argument=argument):
                response = od._command_effect(self.conn, self.mid, '/conferma', argument, None)
                self.assertNotIn('in coda', response)
        self.event_hash.assert_not_called()
        self.grant.assert_not_called()

    def test_saved_calendar_plan_still_requires_current_unquoted_intent(self):
        for text in ('Grazie', 'Non creare un appuntamento in agenda.',
                     'Forse crea un appuntamento in agenda.',
                     '> Crea un appuntamento in agenda.',
                     'Assistente: Crea un appuntamento in agenda.',
                     '```\nCrea un appuntamento in agenda.\n```',
                     'Messaggio inoltrato\nCrea un appuntamento in agenda.'):
            with self.subTest(text=text):
                self.text, self.saved = text, plan(text)
                self.assertEqual(od.respond(text, self.mid), self.saved['reply'])
        self.save.assert_not_called()

    def test_saved_plan_native_text_or_provenance_mismatch_blocks_calendar(self):
        self.text = 'Un messaggio diverso'
        self.assertEqual(od.respond(TEXT, self.mid), self.saved['reply'])
        self.text = TEXT
        self.metadata.return_value['metadata']['quote'] = dict(text=TEXT)
        self.assertEqual(od.respond(TEXT, self.mid), self.saved['reply'])
        self.save.assert_not_called()

    def test_command_native_text_mismatch_fails_before_grant(self):
        with self.assertRaises(PermissionError):
            od.command('/conferma 42', self.mid)
        self.event_hash.assert_not_called()
        self.grant.assert_not_called()
        self.enqueue.assert_not_called()

    def test_completed_command_replay_never_regrants(self):
        receipt = 'Operazione Calendar in coda, non ancora confermata.'
        self.acquire.side_effect = None
        self.acquire.return_value = ('owner-lease', (ANCHOR, None, receipt))
        self.assertTrue(od.command('/conferma 42', self.mid))
        self.event_hash.assert_not_called()
        self.grant.assert_not_called()
        self.assertEqual(self.enqueue.call_args.args[3]['text'], receipt)


class CalendarGrantPostgresTests(calendar_sql._CalendarPostgresFixture):
    """Real policy, native provenance, grants, jobs, dialogue and transactions."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(db, '_conn', side_effect=lambda: nullcontext(self.conn)))
        # Only the schema needed by these adapters; no concurrent runtime/UI
        # migrations or derived ingestion workers are part of this fixture.
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)
        task_reconciliation.migrate(self.conn)
        od.migrate(self.conn)
        policy.migrate(self.conn)
        self.enterContext(patch.object(config, 'TIMEZONE', 'Europe/Rome'))
        self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed)
            VALUES ('telegram','ai_text',TRUE),('telegram','ai_media',TRUE)''')
        self.enterContext(patch.object(od, 'load_context', side_effect=lambda *args: context()))
        self.enterContext(patch.object(od, 'authorized_chat_context', return_value=(
            dict(communications=[], appointments=[], memories=[], rules=[]), [])))
        self.enterContext(patch.object(od, 'retrieve', return_value=({}, {})))
        self.enterContext(patch.object(od, 'compact'))
        self.enterContext(patch.object(secretary, 'prepare', return_value=None))
        self.enterContext(patch.object(owner_commitments, 'prepare', return_value=[]))
        marker = ('Ho già sincronizzato tutto!\n@@EVENTO@@ Visita | 2030-01-02 15:00'
                  ' | Studio | Controllo | 45 | 15')
        self.chat = self.enterContext(patch.object(brain, 'chat_with_user', return_value=marker))
        self.enterContext(patch.object(brain, '_chat', side_effect=AssertionError('Model forbidden')))
        self.enterContext(patch('ai_budget.request_content', side_effect=AssertionError('Model forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(gcal, '_api', side_effect=AssertionError('Calendar I/O forbidden')))
        self.enterContext(patch.object(gcal, '_load_service_account', side_effect=AssertionError('Credentials forbidden')))
        self.enterContext(patch.object(gcal, 'create_event', side_effect=AssertionError('Calendar forbidden')))
        self.enterContext(patch.object(gcal, 'delete_event', side_effect=AssertionError('Calendar forbidden')))
        self.enterContext(patch('connectors.telegram.notify', side_effect=AssertionError('Direct send forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))

    def archive(self, text=TEXT, *, historical=False, **overrides):
        # Insert native evidence directly, without invoking unrelated knowledge
        # and runtime derivation. Policy reads the real native columns/metadata.
        native_id = int(uuid.uuid4().hex[:12], 16)
        metadata = dict(account='bot:123', chat_id='7', author='7', direction='in',
            chat_type='private', content_origin='direct_text', is_forwarded=False,
            message_id=native_id)
        metadata.update(overrides)
        if historical:
            metadata['historical'] = True
        mid = self.conn.execute('''INSERT INTO telegram_messages
            (ts,channel,sender,subject,body,source_id,metadata,account,chat_id,author,direction,message_id)
            VALUES (%s,'telegram','Display name is not authority','',%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id''', (ANCHOR, text, json.dumps(['bot', '123', '7', native_id]),
                json.dumps(metadata), metadata['account'], metadata['chat_id'], metadata['author'],
                metadata['direction'], str(native_id))).fetchone()[0]
        jobs.enqueue(self.conn, 'owner', f'owner:{mid}', dict(message_id=mid, text=text))
        return mid

    def done(self, mid):
        self.conn.execute("UPDATE work_jobs SET status='done' WHERE key=%s", (f'owner:{mid}',))

    def appointment(self, key=None, **overrides):
        event = gcal.validate_event({**EVENT, **overrides})
        return pipeline.save_event(self.conn, event, key or 'message:999:0', 'untrusted source', approved=False)

    def state(self, identity):
        return self.conn.execute('SELECT sync_state,done FROM appointments WHERE id=%s',
                                 (identity,)).fetchone()

    def calendar_jobs(self):
        return self.conn.execute("SELECT key,payload FROM work_jobs WHERE payload->>'type'='calendar' ORDER BY id").fetchall()

    def grants(self):
        return self.conn.execute('SELECT * FROM calendar_effect_grants ORDER BY token').fetchall()

    def reply(self, mid):
        row = self.conn.execute('SELECT response FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()
        return row[0] if row else None

    def attach(self, mid):
        self.conn.execute("INSERT INTO attachments(message_id,channel,filename) VALUES (%s,'telegram','evidence.txt')", (mid,))

    def assert_sealed(self, identity, mid, operation='create'):
        rows = self.calendar_jobs()
        key, payload = rows[-1]
        self.assertTrue(key.startswith('calendar:'))
        self.assertEqual(payload, dict(type='calendar', id=identity,
            authority_message_id=mid, authorization=key))
        row = self.conn.execute('SELECT event_id,event_data,sync_state,sync_key,done FROM appointments WHERE id=%s',
                                (identity,)).fetchone()
        effect = policy.calendar_payload(row, operation)
        self.assertTrue(policy.effect_allowed(self.conn, mid, 'calendar_write', effect, authorization=key))
        self.assertFalse(policy.can_operate(self.conn, mid, 'calendar_write', owner_required=True))
        self.assertFalse(policy.effect_allowed(self.conn, mid, 'calendar_write', effect))
        self.assertEqual(self.conn.execute('SELECT authority_hash,payload_hash FROM calendar_effect_grants WHERE token=%s',
            (key,)).fetchone(), (policy.payload_hash(self.conn.execute(
                'SELECT body FROM telegram_messages WHERE id=%s', (mid,)).fetchone()[0]), policy.payload_hash(effect)))
        return key

    def fail_outbox(self, prefix):
        from psycopg import sql
        self.conn.execute('''CREATE FUNCTION reject_calendar_test_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF starts_with(NEW.key, TG_ARGV[0]) THEN
                    RAISE EXCEPTION 'calendar test outbox failure';
                END IF;
                RETURN NEW;
            END $$''')
        self.conn.execute(sql.SQL('''CREATE TRIGGER reject_calendar_test_outbox BEFORE INSERT ON work_jobs
            FOR EACH ROW EXECUTE FUNCTION reject_calendar_test_outbox({})''').format(sql.Literal(prefix)))

    def allow_outbox(self):
        self.conn.execute('DROP TRIGGER reject_calendar_test_outbox ON work_jobs')
        self.conn.execute('DROP FUNCTION reject_calendar_test_outbox()')

    def test_natural_creation_seals_only_its_event_and_replays_without_duplicate(self):
        mid = self.archive()
        job = jobs.claim('owner')
        current = jobs.ExecutionContext(job[0], job[3], 'owner', job[1], float('inf'))
        with patch.object(jobs, 'execution_context', return_value=current):
            response = od.respond(TEXT, mid)
        identity = self.conn.execute('SELECT id FROM appointments').fetchone()[0]
        self.assertEqual(self.state(identity), ('pending', False))
        self.assertIn('sincronizzazione Calendar in coda, non ancora confermata', response)
        self.assertNotIn('Ho già sincronizzato', response)
        self.assertEqual(self.reply(mid), response)
        self.assert_sealed(identity, mid)
        self.assertEqual(len(self.calendar_jobs()), 1)
        self.assertEqual(len(self.grants()), 1)
        rows = self.conn.execute("SELECT payload FROM work_jobs WHERE key=%s", (f'owner-reply:{mid}:0',)).fetchall()
        self.assertEqual(rows[0][0]['text'], response)
        before = (self.grants(), self.calendar_jobs())
        self.chat.side_effect = AssertionError('Replay must not infer')
        self.assertEqual(od.respond('Different retry payload', mid), response)
        self.assertEqual((self.grants(), self.calendar_jobs()), before)

    def test_grant_denial_preserves_local_proposal_and_truthful_receipt(self):
        mid = self.archive()
        self.attach(mid)  # Native text is current, but cannot grant attached instructions.
        response = od.respond(TEXT, mid)
        identity = self.conn.execute('SELECT id FROM appointments').fetchone()[0]
        self.assertEqual(self.state(identity), ('proposal', False))
        self.assertIn('proposta locale; Calendar non autorizzato', response)
        self.assertNotIn('in coda', response)
        self.assertEqual(self.grants(), [])
        self.assertEqual(self.calendar_jobs(), [])
        self.assertEqual(self.reply(mid), response)

    def test_confirm_existing_external_proposal_uses_explicit_native_grant(self):
        identity = self.appointment()
        text = f'/conferma {identity}'
        mid = self.archive(text)
        self.assertTrue(od.command(text, mid))
        self.assertEqual(self.state(identity), ('pending', False))
        self.assertIn('in coda, non ancora confermata', self.reply(mid))
        self.assert_sealed(identity, mid)
        before = (self.grants(), self.calendar_jobs())
        self.assertTrue(od.command(text, mid))
        self.assertEqual((self.grants(), self.calendar_jobs()), before)
        self.chat.assert_not_called()

    def test_cancel_seals_separate_operation_for_each_supported_state(self):
        for state in ('proposal', 'local', 'pending', 'synced'):
            with self.subTest(state=state):
                identity = self.appointment(f'message:999:{state}')
                self.conn.execute('UPDATE appointments SET sync_state=%s WHERE id=%s', (state, identity))
                text = f'/annulla {identity}'
                mid = self.archive(text)
                self.assertTrue(od.command(text, mid))
                self.assertEqual(self.state(identity), ('cancel_pending', True))
                self.assert_sealed(identity, mid, 'cancel')
                self.assertIn('in coda, non ancora confermata', self.reply(mid))
                self.done(mid)
        self.assertEqual(len(self.grants()), 4)
        self.assertEqual(len(self.calendar_jobs()), 4)

    def test_cancel_revokes_create_grant_and_payload_tampering_fails_closed(self):
        mid = self.archive()
        od.respond(TEXT, mid)
        identity = self.conn.execute('SELECT id FROM appointments').fetchone()[0]
        create_token = self.assert_sealed(identity, mid)
        self.done(mid)
        text = f'/annulla {identity}'
        cancel_mid = self.archive(text)
        od.command(text, cancel_mid)
        cancel_token = self.assert_sealed(identity, cancel_mid, 'cancel')
        self.assertNotEqual(create_token, cancel_token)
        self.assertTrue(self.conn.execute('SELECT revoked FROM calendar_effect_grants WHERE token=%s', (create_token,)).fetchone()[0])
        row = self.conn.execute('SELECT event_id,event_data,sync_state,sync_key,done FROM appointments WHERE id=%s', (identity,)).fetchone()
        self.assertFalse(policy.effect_allowed(self.conn, mid, 'calendar', policy.calendar_payload(row), authorization=create_token))
        effect = policy.calendar_payload(row, 'cancel')
        self.conn.execute("UPDATE appointments SET event_data=jsonb_set(event_data,'{location}','\"Changed\"') WHERE id=%s", (identity,))
        self.assertFalse(policy.effect_allowed(self.conn, cancel_mid, 'calendar', effect, authorization=cancel_token))

    def test_denied_commands_leave_appointment_and_calendar_outbox_unchanged(self):
        identity = self.appointment()
        for command in ('/conferma', '/annulla'):
            with self.subTest(command=command):
                text = f'{command} {identity}'
                mid = self.archive(text)
                self.attach(mid)
                od.command(text, mid)
                self.assertIn('non autorizzata', self.reply(mid))
                self.assertNotIn('in coda', self.reply(mid))
                self.assertEqual(self.state(identity), ('proposal', False))
                self.assertEqual(self.grants(), [])
                self.assertEqual(self.calendar_jobs(), [])
                self.done(mid)

    def test_natural_intent_cannot_approve_an_unrelated_proposal(self):
        identity = self.appointment()
        mid = self.archive()
        response = od._command_effect(self.conn, mid, '/conferma', str(identity), None)
        self.assertIn('non autorizzata', response)
        self.assertEqual(self.state(identity), ('proposal', False))
        self.assertEqual(self.grants(), [])
        self.assertEqual(self.calendar_jobs(), [])

    def test_forward_history_caption_and_nonowner_never_authorize_adapters(self):
        cases = [dict(historical=True), dict(is_forwarded=True),
                 dict(forward_origin={'type': 'hidden_user'}), dict(quote={'text': TEXT}),
                 dict(content_origin='caption'), dict(content_origin='transcript'),
                 dict(author='8'), dict(direction='out')]
        for overrides in cases:
            for text in (TEXT, '/conferma 1', '/annulla 1'):
                with self.subTest(overrides=overrides, text=text):
                    mid = self.archive(text, **overrides)
                    with self.assertRaises(PermissionError):
                        od.respond(text, mid) if text == TEXT else od.command(text, mid)
                    self.done(mid)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM appointments').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM owner_exchanges').fetchone()[0], 0)
        self.assertEqual(self.grants(), [])
        self.chat.assert_not_called()

    def test_fresh_quoted_negated_or_mismatched_text_cannot_create_events(self):
        for native_text, supplied in (
                ('> ' + TEXT, '> ' + TEXT), ('Non creare un appuntamento in agenda.', 'Non creare un appuntamento in agenda.'),
                ('Grazie', 'Grazie'), ('Grazie', TEXT)):
            with self.subTest(native_text=native_text, supplied=supplied):
                mid = self.archive(native_text)
                od.respond(supplied, mid)
                self.assertEqual(self.conn.execute('SELECT plan FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()[0]['events'], [])
                self.done(mid)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM appointments').fetchone()[0], 0)
        self.assertEqual(self.grants(), [])

    def test_saved_plan_rechecks_native_body_before_requesting_grant(self):
        mid = self.archive()
        token, _ = od.acquire(mid, TEXT, False)
        self.conn.execute('UPDATE owner_exchanges SET plan=%s WHERE message_id=%s', (json.dumps(plan()), mid))
        self.conn.execute('UPDATE owner_dialogue_state SET token=NULL,lease_until=NULL WHERE token=%s', (token,))
        self.conn.execute("UPDATE telegram_messages SET body='Grazie' WHERE id=%s", (mid,))
        self.assertEqual(od.respond(TEXT, mid), plan()['reply'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM appointments').fetchone()[0], 0)
        self.assertEqual(self.grants(), [])
        self.chat.assert_not_called()

    def test_existing_synced_event_receipt_does_not_claim_new_queue(self):
        mid = self.archive()
        identity = self.appointment(f'owner:{mid}:event:0')
        self.conn.execute("UPDATE appointments SET sync_state='synced' WHERE id=%s", (identity,))
        response = od.respond(TEXT, mid)
        self.assertIn('sincronizzato con Calendar', response)
        self.assertNotIn('in coda', response)
        self.assertEqual(self.calendar_jobs(), [])
        self.assertEqual(self.grants(), [])

    def test_missing_and_legacy_invalid_events_produce_durable_failure_receipts(self):
        identity = self.conn.execute("INSERT INTO appointments(title,dt) VALUES ('Legacy','2030-01-02 15:00') RETURNING id").fetchone()[0]
        for target in (identity, identity + 1, 2 ** 63):
            for command in ('/conferma', '/annulla'):
                text = f'{command} {target}'
                mid = self.archive(text)
                self.assertTrue(od.command(text, mid))
                self.assertIn('non trovato', self.reply(mid))
                self.assertNotIn('in coda', self.reply(mid))
                self.done(mid)
        self.assertEqual(self.state(identity), ('local', False))
        self.assertEqual(self.grants(), [])
        self.assertEqual(self.calendar_jobs(), [])

    def test_natural_effect_reply_and_both_outboxes_rollback_then_reuse_plan(self):
        mid = self.archive()
        self.fail_outbox('owner-reply:')
        with self.assertRaisesRegex(db.psycopg.errors.RaiseException, 'calendar test outbox failure'):
            od.respond(TEXT, mid)
        saved, response = self.conn.execute('SELECT plan,response FROM owner_exchanges WHERE message_id=%s', (mid,)).fetchone()
        self.assertTrue(saved['events'])
        self.assertIsNone(response)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM appointments').fetchone()[0], 0)
        self.assertEqual(self.grants(), [])
        self.assertEqual(self.calendar_jobs(), [])
        self.allow_outbox()
        self.chat.side_effect = AssertionError('Saved plan must not infer')
        response = od.respond(TEXT, mid)
        identity = self.conn.execute('SELECT id FROM appointments').fetchone()[0]
        self.assert_sealed(identity, mid)
        self.assertIn('in coda, non ancora confermata', response)
        self.assertEqual(self.reply(mid), response)

    def test_command_grant_state_reply_and_outbox_rollback_together(self):
        identity = self.appointment()
        for command in ('/conferma', '/annulla'):
            with self.subTest(command=command):
                text = f'{command} {identity}'
                mid = self.archive(text)
                self.fail_outbox('owner-reply:')
                with self.assertRaisesRegex(db.psycopg.errors.RaiseException, 'calendar test outbox failure'):
                    od.command(text, mid)
                self.assertEqual(self.state(identity), ('proposal', False))
                self.assertIsNone(self.reply(mid))
                self.assertEqual(self.grants(), [])
                self.assertEqual(self.calendar_jobs(), [])
                self.allow_outbox()
                self.done(mid)
        self.assertTrue(od.command(text, mid))
        self.assert_sealed(identity, mid, 'cancel')

    def test_sealed_calendar_outbox_failure_cannot_leave_promoted_state(self):
        mid = self.archive()
        self.fail_outbox('calendar:')
        with self.assertRaisesRegex(db.psycopg.errors.RaiseException, 'calendar test outbox failure'):
            od.respond(TEXT, mid)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM appointments').fetchone()[0], 0)
        self.assertEqual(self.grants(), [])
        self.assertEqual(self.calendar_jobs(), [])
        self.assertIsNone(self.reply(mid))


if __name__ == '__main__':
    unittest.main()