"""Comandi slash del proprietario (gesti espliciti) su PostgreSQL di test, senza modello né rete.

Il proprietario conferma con un gesto nativo: testo diretto, non inoltrato, in chat
privata dal bot. Storico, inoltrati, altre chat e testo modificato dopo l'archiviazione
non possono mai eseguire un comando.
"""
import json
import unittest
import uuid
from unittest.mock import patch

from secretary.core import config, db, jobs
from secretary.archive import message_store
from secretary.agent import outbox, owner
from tests.agent.test_agent_postgres import AgentPostgresCase

OWNER = '101'


class OwnerCommandCase(AgentPostgresCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', OWNER))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:offline'))
        self.enterContext(patch.object(config, 'TIMEZONE', 'Europe/Rome'))

    def owner_message(self, text, *, direct=True, forwarded=False, author=OWNER, historical=False):
        number = int(uuid.uuid4().hex[:8], 16) + 1
        metadata = dict(account='bot:777', chat_id=OWNER, author=author, direction='in', chat_type='private',
                        content_origin='direct_text' if direct else 'forwarded', is_forwarded=forwarded,
                        message_id=number)
        return message_store.store(self.conn, 'telegram', 'Mario', '', text, ts='2026-10-20T10:00:00+00:00',
                                   source_id=json.dumps(['bot', '777', author, number]), metadata=metadata,
                                   historical=historical)

    def command(self, mid, text=None):
        """Esegue owner.command dentro un vero job 'owner' (lease e token reali)."""
        body = text if text is not None else self.conn.execute(
            'SELECT body FROM telegram_messages WHERE id=%s', (mid,)).fetchone()[0]
        with db._conn() as conn:
            jobs.enqueue(conn, 'owner', f'owner:{mid}', dict(message_id=mid, text=''))
        job = jobs.claim('owner')
        worker = jobs.Worker('owner', lambda payload: None)
        try:
            with worker._execution(job):
                result = owner.command(body, mid)
        except jobs.Deferred:
            raise  # resta in coda, come nel Worker
        except BaseException as exc:
            jobs.finish(job, exc, permanent=True)  # un rifiuto di autorità è un fallimento visibile
            raise
        jobs.finish(job)  # come il Worker: il comando successivo non deve restare in coda dietro
        return result

    def reply(self, mid):
        rows = self.conn.execute("SELECT payload->>'text' FROM work_jobs WHERE key LIKE %s ORDER BY key",
                                 (f'owner-reply:{mid}:%',)).fetchall()
        return '\n'.join(r[0] for r in rows)

    def keys(self, pattern):
        return [r[0] for r in self.conn.execute('SELECT key FROM work_jobs WHERE key LIKE %s ORDER BY key', (pattern,))]


class CommandAuthorityTests(OwnerCommandCase):
    def test_plain_text_is_not_a_command_and_creates_no_exchange(self):
        mid = self.owner_message('Quante email ho ricevuto oggi?')
        self.assertFalse(self.command(mid))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM owner_exchanges').fetchone()[0], 0)

    def test_task_command_creates_owner_task_reminder_and_reply_atomically(self):
        mid = self.owner_message('/task Pagare affitto | 2099-01-02T10:00:00+01:00')
        self.assertTrue(self.command(mid))
        row = self.conn.execute('''SELECT title,owner_origin_message,owner_operation_key,deadline
            FROM tasks''').fetchone()
        self.assertEqual(row[:3], ('Pagare affitto', mid, f'owner-command:{mid}'))
        self.assertIn('Attività #', self.reply(mid))
        self.assertTrue(self.keys('owner-reminder:%'))
        self.assertEqual(self.conn.execute('SELECT response IS NOT NULL FROM owner_exchanges').fetchone()[0], True)

    def test_malformed_task_command_writes_nothing_and_explains_syntax(self):
        for text in ('/task', '/task | 2099-01-01', '/task Titolo | non-una-data'):
            with self.subTest(text=text):
                mid = self.owner_message(text)
                self.assertTrue(self.command(mid))
                self.assertIn('Usare /task', self.reply(mid))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_done_command_closes_through_the_versioned_lifecycle(self):
        mid = self.owner_message('/task Chiamare il notaio')
        self.command(mid)
        task = self.conn.execute('SELECT id,version FROM tasks').fetchone()
        done = self.owner_message(f'/fatto {task[0]}')
        self.assertTrue(self.command(done))
        self.assertEqual(self.conn.execute('SELECT status,version FROM tasks').fetchone(), ('done', task[1] + 1))
        self.assertIn('completata', self.reply(done))
        self.assertEqual(self.conn.execute('SELECT status FROM task_changes').fetchone()[0], 'applied')
        # Un secondo /fatto non fa nulla e non finge un successo.
        again = self.owner_message(f'/fatto {task[0]}')
        self.command(again)
        self.assertIn('non aperta', self.reply(again))

    def test_ids_are_strict_numbers(self):
        for argument in ('abc', '', '-1', '1.5', '０', '99999999999999999999'):
            with self.subTest(argument=argument):
                mid = self.owner_message(f'/fatto {argument}'.strip())
                self.assertTrue(self.command(mid))
                self.assertRegex(self.reply(mid), 'ID numerico|non trovato')

    def test_rule_sentence_is_taught_by_the_owner_only(self):
        mid = self.owner_message('quando scrive il commercialista allora avvisami subito')
        self.assertTrue(self.command(mid))
        self.assertEqual(self.conn.execute('SELECT pattern,instruction,taught_by FROM rules').fetchone(),
                         ('scrive il commercialista', 'avvisami subito', 'utente'))

    def test_forwarded_historical_other_author_and_edited_text_never_run_commands(self):
        cases = {
            'inoltrato': self.owner_message('/task Da inoltro', direct=False, forwarded=True),
            'altro autore': self.owner_message('/task Altro autore', author='202'),
            'storico': self.owner_message('/task Storico', historical=True),
        }
        for name, mid in cases.items():
            with self.subTest(name=name), self.assertRaises(PermissionError):
                self.command(mid)
        edited = self.owner_message('/task Originale')
        self.conn.execute("UPDATE telegram_messages SET body='/task Modificato' WHERE id=%s", (edited,))
        with self.assertRaises(PermissionError):
            self.command(edited, text='/task Originale')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs WHERE key LIKE %s',
                                           ('owner-reply:%',)).fetchone()[0], 0)

    def test_replay_after_commit_redelivers_the_receipt_without_a_second_effect(self):
        mid = self.owner_message('/task Una sola volta')
        self.command(mid)
        with db._conn() as conn:
            conn.execute("UPDATE work_jobs SET status='pending',token=NULL,lease_until=NULL WHERE key=%s", (f'owner:{mid}',))
        self.assertTrue(self.command(mid))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
        self.assertEqual(len(self.keys(f'owner-reply:{mid}:%')), 1)

    def test_replies_are_delivered_in_request_order(self):
        first = self.owner_message('/task Primo')
        second = self.owner_message('/task Secondo')
        with db._conn() as conn:
            jobs.enqueue(conn, 'owner', f'owner:{first}', dict(message_id=first, text=''))
            jobs.enqueue(conn, 'owner', f'owner:{second}', dict(message_id=second, text=''))
        job = jobs.claim('owner')  # prende il più vecchio; il secondo non può sorpassarlo
        self.assertEqual(job[1]['message_id'], first)
        with db._conn() as conn:
            conn.execute("UPDATE work_jobs SET status='pending',token=NULL,lease_until=NULL WHERE key=%s", (f'owner:{first}',))
        worker = jobs.Worker('owner', lambda payload: None)
        late = self.conn.execute("SELECT id,payload,attempts,token FROM work_jobs WHERE key=%s", (f'owner:{second}',)).fetchone()
        self.conn.execute("UPDATE work_jobs SET status='running',token='t2',lease_until=now()+interval '5 minutes' WHERE key=%s",
                          (f'owner:{second}',))
        with worker._execution((late[0], late[1], late[2], 't2')):
            with self.assertRaises(jobs.Deferred):
                owner.command('/task Secondo', second)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)


class CalendarCommandTests(OwnerCommandCase):
    EVENT = {'title': 'Visita', 'dt': '2099-05-05 10:00', 'duration_min': 30, 'reminder_minutes': 15}

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict('os.environ', {'GCAL_CALENDAR_ID': 'cal@example.invalid'}))

    def proposal(self):
        from secretary.workflows import pipeline
        source = self.owner_message('Ho una visita il 5 maggio')
        with db._conn() as conn, conn.transaction():
            with patch('secretary.core.jobs.assert_current'):
                return pipeline.save_event(conn, dict(self.EVENT), f'owner:{source}:event:0', 'dialogo', False)

    def test_confirm_grants_exactly_that_proposal_and_never_claims_it_was_synced(self):
        identity = self.proposal()
        mid = self.owner_message(f'/conferma {identity}')
        self.assertTrue(self.command(mid))
        reply = self.reply(mid)
        self.assertIn('in coda', reply)
        self.assertIn('non ancora confermata', reply)
        self.assertEqual(self.conn.execute('SELECT sync_state FROM appointments WHERE id=%s', (identity,)).fetchone()[0], 'pending')
        grants = self.conn.execute('SELECT appointment_id,expected_state,expected_done,owner_message_id FROM calendar_effect_grants').fetchall()
        self.assertEqual(grants, [(identity, 'pending', False, mid)])
        queued = self.conn.execute("SELECT payload FROM work_jobs WHERE kind='action' AND payload->>'type'='calendar'").fetchall()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0]['authority_message_id'], mid)

    def test_unknown_or_invalid_event_never_mints_a_grant(self):
        for argument in ('9999', '0'):
            mid = self.owner_message(f'/conferma {argument}')
            self.assertTrue(self.command(mid))
            self.assertRegex(self.reply(mid), 'non trovato|non valido')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM calendar_effect_grants').fetchone()[0], 0)

    def test_cancel_seals_its_own_operation(self):
        identity = self.proposal()
        self.command(self.owner_message(f'/conferma {identity}'))
        self.conn.execute("UPDATE appointments SET sync_state='synced' WHERE id=%s", (identity,))
        mid = self.owner_message(f'/annulla {identity}')
        self.assertTrue(self.command(mid))
        grants = [r for r in self.conn.execute(
            'SELECT expected_state,expected_done,revoked FROM calendar_effect_grants ORDER BY created_at,token')]
        # Il grant di creazione viene revocato e ne nasce uno SEPARATO per l'annullamento.
        self.assertEqual(grants, [('pending', False, True), ('cancel_pending', True, False)])


class EmailDraftCommandTests(OwnerCommandCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(config, 'EMAIL_ACCOUNTS', [dict(
            name='main', address='me@example.test', provider='custom', smtp_host='smtp.example.test',
            smtp_port=587, password='x', auth='basic')]))

    def draft(self):
        source = self.owner_message('Scrivi a Mario')
        with db._conn() as conn, conn.transaction():
            with patch('secretary.core.jobs.assert_current'):
                return outbox.propose(conn, 'me@example.test', 'mario@example.test', 'Ciao', 'Testo', source)['draft_id']

    def test_send_requires_active_mode_and_never_sends_in_observe(self):
        identity = self.draft()
        mid = self.owner_message(f'/invia {identity}')
        with patch.object(config, 'SECRETARY_MODE', 'observe'):
            self.assertTrue(self.command(mid))
        self.assertIn('observe', self.reply(mid).lower())
        self.assertEqual(self.conn.execute('SELECT state FROM agent_email_outbox').fetchone()[0], 'proposed')
        self.assertEqual(self.keys('email-send:%'), [])

    def test_send_in_active_mode_approves_and_queues_one_action_job(self):
        identity = self.draft()
        mid = self.owner_message(f'/invia {identity}')
        with patch.object(config, 'SECRETARY_MODE', 'active'):
            self.assertTrue(self.command(mid))
        self.assertEqual(self.conn.execute('SELECT state,approved_by_message FROM agent_email_outbox').fetchone(), ('approved', mid))
        self.assertEqual(self.keys('email-send:%'), [f'email-send:{identity}'])
        # Un secondo /invia non crea un secondo job.
        again = self.owner_message(f'/invia {identity}')
        with patch.object(config, 'SECRETARY_MODE', 'active'):
            self.command(again)
        self.assertEqual(self.keys('email-send:%'), [f'email-send:{identity}'])

    def test_discard_rejects_and_blocks_a_later_send(self):
        identity = self.draft()
        self.command(self.owner_message(f'/scarta {identity}'))
        self.assertEqual(self.conn.execute('SELECT state FROM agent_email_outbox').fetchone()[0], 'rejected')
        with patch.object(config, 'SECRETARY_MODE', 'active'):
            mid = self.owner_message(f'/invia {identity}')
            self.command(mid)
        self.assertEqual(self.keys('email-send:%'), [])
        self.assertEqual(self.conn.execute('SELECT state FROM agent_email_outbox').fetchone()[0], 'rejected')


class OfflineCommandTests(unittest.TestCase):
    def test_command_vocabulary_is_closed_and_the_old_ones_are_gone(self):
        for name in ('/start', '/stop', '/appuntamenti', '/note', '/regole', '/fatture', '/task', '/attivita',
                     '/fatto', '/conferma', '/annulla', '/invia', '/scarta'):
            self.assertIn(name, owner.COMMANDS)
        for removed in ('/proposte', '/conferma_attivita', '/rifiuta_attivita', '/ferma_istruzione'):
            self.assertNotIn(removed, owner.COMMANDS)

    def test_non_commands_return_false_before_any_database_access(self):
        with patch('secretary.core.db._conn', side_effect=AssertionError('No DB')), patch('secretary.core.jobs.assert_current'):
            for text in ('ciao', '/sconosciuto 1', 'fammi un riepilogo', '', '   '):
                self.assertFalse(owner.command(text, 1), text)


if __name__ == '__main__':
    unittest.main()
