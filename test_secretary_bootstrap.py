"""Offline bootstrap contracts and real SQL in disposable schemas only.

No configured DATABASE_URL is used. PostgreSQL tests start a private, temporary
Unix-socket-only cluster when local binaries are available, otherwise skip.
Providers, sockets and worker startup are never needed by the integration.
"""
from contextlib import contextmanager, ExitStack
import builtins
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import uuid

import capabilities
import ai_budget
import config
import db
import execution
import jobs
import knowledge
import message_store
import owner_dialogue
import owner_instructions
import policy
import research
import secretary
import secretary_runtime
import task_reconciliation
from connectors import media_archive, media_text


class BootstrapOfflineTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch.object(db, '_conn', side_effect=AssertionError('Configured DB forbidden')))

    def test_bootstrap_captures_legacy_before_base_and_orders_all_migrations(self):
        for installed in (False, True):
            with self.subTest(installed=installed), ExitStack() as stack:
                conn = MagicMock()
                trace = []
                def execute(query, params=None):
                    cursor = MagicMock()
                    if 'current_setting' in query:
                        cursor.fetchone.return_value = ('off', 'off', False)
                    elif "to_regclass('schema_migrations')" in query:
                        trace.append('legacy')
                        cursor.fetchone.return_value = ('schema_migrations' if installed else None,)
                    return cursor
                conn.execute.side_effect = execute
                stack.enter_context(patch.object(db, 'available', return_value=True))
                stack.enter_context(patch.object(db, '_conn')).return_value.__enter__.return_value = conn
                modules = (message_store, jobs, task_reconciliation, media_archive,
                           owner_dialogue, capabilities, policy, secretary, knowledge, secretary_runtime)
                for module in modules:
                    def migrate(*args, module=module, **kwargs):
                        trace.append(module.__name__)
                        if module is capabilities:
                            self.assertIs(kwargs['legacy'], installed)
                        return 17 if module is message_store else None
                    stack.enter_context(patch.object(module, 'migrate', side_effect=migrate))
                stack.enter_context(patch.object(db, 'migrate_notes', side_effect=lambda c: trace.append('notes')))
                stack.enter_context(patch.object(db, 'migrate_typed_notes', side_effect=lambda c: trace.append('typed_notes')))
                import research
                stack.enter_context(patch.object(research, 'migrate', side_effect=lambda c: trace.append('research')))
                import owner_instructions
                stack.enter_context(patch.object(owner_instructions, 'migrate', side_effect=lambda c: trace.append('owner_instructions')))
                for module in (execution, ai_budget):
                    stack.enter_context(patch.object(module, 'migrate',
                        side_effect=lambda c, module=module: trace.append(module.__name__)))
                self.assertEqual(db.init(), 17)
                self.assertEqual(trace, ['legacy'] + [m.__name__ for m in modules]
                                 + ['notes', 'typed_notes', 'research', 'owner_instructions',
                                    'execution', 'ai_budget'])
                conn.transaction.assert_called_once_with()

    def test_derived_boundary_never_swallows_schema_or_import_errors(self):
        conn = MagicMock()
        with patch.object(knowledge, 'enqueue', side_effect=RuntimeError('missing schema')):
            with self.assertRaisesRegex(RuntimeError, 'missing schema'):
                message_store.enqueue_derived(conn, 7)
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == 'knowledge':
                raise ImportError('knowledge missing')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=importing):
            with self.assertRaises(ImportError):
                message_store.enqueue_derived(conn, 7)

    def test_history_indexes_without_observing(self):
        with patch.object(knowledge, 'enqueue', return_value={'revision': 'v1'}) as enqueue, \
             patch.object(secretary_runtime, 'observe') as observe:
            message_store.enqueue_derived(MagicMock(), 7, historical=True)
        enqueue.assert_called_once()
        observe.assert_not_called()

    def test_media_text_terminal_states_have_safe_placeholder_pending_still_defers(self):
        conn = MagicMock()
        with patch.object(db, '_conn') as connect, patch.object(media_text, 'convert') as convert:
            connect.return_value.__enter__.return_value = conn
            for status, attempts in (('blocked', 1), ('failed', 8), ('pending', 8), ('done', 1)):
                conn.execute.return_value.fetchone.return_value = (status, attempts, '/secret/provider-error')
                result = media_text.message_text('caption', [dict(id=8, content_type='audio/ogg')])
                self.assertIn('caption', result)
                self.assertIn('Media non trascritto', result)
                self.assertNotIn('/secret', result)
            conn.execute.return_value.fetchone.return_value = ('pending', 7, None)
            with self.assertRaises(jobs.Deferred):
                media_text.message_text('', [dict(id=8, content_type='image/png')])
        convert.assert_not_called()

    def test_minimal_storage_mock_has_explicit_integration_boundary(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (7, False)
        with patch.object(message_store, 'enqueue_derived') as derived, \
             patch.object(media_archive, 'enqueue_message'):
            self.assertEqual(message_store.store(conn, 'email', 's', 't', 'b'), 7)
        derived.assert_called_once_with(conn, 7, historical=False)


class BootstrapPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if db.psycopg is None or os.geteuid() == 0:
            raise unittest.SkipTest('psycopg and a non-root user required')
        candidates = sorted(Path('/usr/lib/postgresql').glob('*/bin/initdb'))
        initdb = shutil.which('initdb') or (str(candidates[-1]) if candidates else None)
        if not initdb or not Path(initdb).with_name('pg_ctl').is_file():
            raise unittest.SkipTest('Local PostgreSQL binaries unavailable')
        cls.enterClassContext(patch.dict(os.environ,
            {k: v for k, v in os.environ.items() if not k.startswith('PG')}, clear=True))
        cls.enterClassContext(patch.object(db, '_conn', side_effect=AssertionError('Configured DB forbidden')))
        directory = cls.enterClassContext(tempfile.TemporaryDirectory(prefix='secretary-bootstrap-pg-'))
        data = Path(directory) / 'cluster'
        ctl = str(Path(initdb).with_name('pg_ctl'))
        subprocess.run([initdb, '-D', str(data), '-A', 'trust', '--no-locale', '-E', 'UTF8'],
                       check=True, capture_output=True)
        def stop():
            if (data / 'postmaster.pid').exists():
                subprocess.run([ctl, '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                               check=True, capture_output=True)
        cls.addClassCleanup(stop)
        subprocess.run([ctl, '-D', str(data), '-l', directory + '/postgres.log', '-o',
            f"-F -c listen_addresses='' -c unix_socket_permissions=0700 -k {directory}", '-w', 'start'],
            check=True, capture_output=True)
        with db.psycopg.connect(host=directory, dbname='postgres', autocommit=True) as admin:
            admin.execute('CREATE DATABASE secretary_bootstrap_test')
        cls.params = dict(host=directory, dbname='secretary_bootstrap_test', autocommit=True,
                          prepare_threshold=None, connect_timeout=5)

    def setUp(self):
        from psycopg import sql
        self.conn = db.psycopg.connect(**self.params)
        self.addCleanup(self.conn.close)
        self.assertEqual(self.conn.execute('SHOW listen_addresses').fetchone(), ('',))
        self.schema = 'bootstrap_' + uuid.uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='15s'")
        self.conn.execute("SET lock_timeout='10s'")
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch.object(db, 'available', return_value=True))
        self.enterContext(patch.object(config, 'OPENROUTER_API_KEY', 'offline-test'))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '777:offline'))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        self.convert = self.enterContext(patch.object(media_text, 'convert', return_value='Voce smeraldo'))
        self.describe = self.enterContext(patch.object(media_text, 'describe_image', return_value='Immagine smeraldo'))

    @contextmanager
    def connection(self):
        yield self.conn

    def drop_schema(self):
        from psycopg import sql
        if self.conn.info.transaction_status != db.psycopg.pq.TransactionStatus.IDLE:
            self.conn.rollback()
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def scalar(self, sql, args=None):
        return self.conn.execute(sql, args).fetchone()[0]

    def store(self, channel='telegram (account)', **kwargs):
        kwargs.setdefault('source_id', uuid.uuid4().hex)
        kwargs.setdefault('metadata', dict(direction='out', chat_id='8', author='7', account='account:7'))
        return message_store.store(self.conn, channel, 'Native sender', 'Preventivo',
                                   kwargs.pop('body', 'Corpo originale'), **kwargs)

    def media(self, channel='telegram (account)', mime='audio/ogg', historical=False):
        mid = self.store(channel, historical=historical, attachments=[
            dict(filename='media', content_type=mime, data=b'original')])
        aid = self.scalar('SELECT id FROM attachments WHERE message_id=%s', (mid,))
        return mid, aid

    def consent(self, channel='telegram', allowed=True, effects=('ai_text', 'ai_media')):
        for effect in effects:
            self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed) VALUES (%s,%s,%s)
                ON CONFLICT(channel,effect) DO UPDATE SET allowed=excluded.allowed''', (channel, effect, allowed))

    def test_fresh_bootstrap_complete_order_idempotence_no_consent(self):
        self.assertEqual(db.init(), 0)
        self.assertEqual(self.scalar('SELECT provenance FROM secretary_profile'), 'fresh')
        self.assertEqual(self.conn.execute('SELECT enabled FROM flow_settings ORDER BY flow').fetchall(),
                         [(False,), (False,)])
        self.assertEqual(self.scalar('SELECT count(*) FROM data_policies'), 0)
        expected = {message_store.VERSION, jobs.VERSION, jobs.RUNTIME_VERSION,
                    media_archive.VERSION, owner_dialogue.VERSION, capabilities.VERSION, policy.VERSION,
                    secretary.VERSION, knowledge.VERSION, secretary_runtime.VERSION,
                    db.NOTES_VERSION, db.TYPED_NOTES_VERSION, research.VERSION,
                    owner_instructions.VERSION, execution.VERSION, ai_budget.VERSION}
        self.assertEqual(set(row[0] for row in self.conn.execute('SELECT version FROM schema_migrations')), expected)
        self.assertIsNotNone(self.scalar("SELECT to_regclass('knowledge_secretary_practices_fts')"))
        receipts = self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        profile = self.conn.execute('SELECT * FROM secretary_profile').fetchall()
        db.init()
        self.assertEqual(self.conn.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall(), receipts)
        self.assertEqual(self.conn.execute('SELECT * FROM secretary_profile').fetchall(), profile)
        self.convert.assert_not_called()
        self.describe.assert_not_called()

    def test_legacy_upgrade_preserves_rows_explicit_switch_and_denies_consent(self):
        message_store.migrate(self.conn, db.SCHEMA)
        self.conn.execute("INSERT INTO notes(text) VALUES ('Vecchia memoria')")
        self.conn.execute(capabilities.FLOW_SCHEMA)
        self.conn.execute("INSERT INTO flow_settings(flow,enabled) VALUES ('invoices',false)")
        self.conn.execute("INSERT INTO email_messages(channel,body,metadata) VALUES ('email','Vecchio testo','{}')")
        before = self.conn.execute('SELECT * FROM email_messages').fetchall()
        db.init()
        self.assertEqual(self.scalar('SELECT provenance FROM secretary_profile'), 'legacy')
        self.assertEqual(self.conn.execute('SELECT flow,enabled FROM flow_settings ORDER BY flow').fetchall(),
                         [('invoices', False), ('monthly_zip', True)])
        self.assertEqual(self.conn.execute('SELECT * FROM email_messages').fetchall(), before)
        self.assertEqual(db.personal_memories(), [dict(id=1, text='Vecchia memoria')])
        self.assertEqual(db.personal_memories(provenance=True)[0]['version'], 1)
        self.assertEqual(self.scalar('SELECT count(*) FROM data_policies'), 0)

    def test_failed_bootstrap_rolls_back_every_migration(self):
        with patch.object(secretary_runtime, 'migrate', side_effect=RuntimeError('broken bootstrap')):
            with self.assertRaisesRegex(RuntimeError, 'broken bootstrap'):
                db.init()
        self.assertIsNone(self.scalar("SELECT to_regclass('schema_migrations')"))
        self.assertIsNone(self.scalar("SELECT to_regclass('email_messages')"))
        db.init()
        self.assertEqual(self.scalar('SELECT provenance FROM secretary_profile'), 'fresh')

    def test_all_native_store_and_duplicate_paths_queue_catalogue_and_live_observation(self):
        db.init()
        for channel in ('email Work', 'whatsapp', 'telegram', 'telegram (account)'):
            with self.subTest(channel=channel):
                source = uuid.uuid4().hex
                mid = self.store(channel, source_id=source)
                self.assertEqual(self.scalar("SELECT count(*) FROM work_jobs WHERE kind='knowledge' AND payload->>'message_id'=%s", (str(mid),)), 1)
                self.assertEqual(self.scalar('SELECT status FROM secretary_runtime_observations WHERE message_id=%s', (mid,)), 'pending')
                # Missing queue rows can be repaired by every duplicate return.
                self.conn.execute("DELETE FROM work_jobs WHERE kind='knowledge' AND payload->>'message_id'=%s", (str(mid),))
                self.assertEqual(self.store(channel, source_id=source), mid)
                self.assertEqual(self.scalar("SELECT count(*) FROM work_jobs WHERE kind='knowledge' AND payload->>'message_id'=%s", (str(mid),)), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM work_jobs WHERE kind='action'"), 0)

    def test_storage_and_queues_rollback_together(self):
        db.init()
        real_observe = secretary_runtime.observe
        def observe_then_fail(conn, mid):
            real_observe(conn, mid)
            raise RuntimeError('abort')
        with patch.object(secretary_runtime, 'observe', side_effect=observe_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'abort'):
                self.store('whatsapp')
        for table in ('whatsapp_messages', 'work_jobs', 'secretary_runtime_observations'):
            self.assertEqual(self.scalar(f'SELECT count(*) FROM {table}'), 0)

    def test_bootstrap_installs_budget_before_first_worker_reservation(self):
        db.init()
        jobs.submit('analysis', 'budget-bootstrap', dict(message_id=1))
        job = jobs.claim('analysis')
        context = jobs.ExecutionContext(job[0], job[3], 'analysis', job[1], float('inf'))
        with patch.object(jobs._local, 'execution', context, create=True):
            identity = ai_budget._reserve(context, ai_budget._limits(), 100,
                                          dict(model='m', max_tokens=1, messages=[]))
            ai_budget._settle(identity, context, dict(usage=dict(total_tokens=120)))
        self.assertEqual(self.conn.execute('SELECT state,charged_tokens FROM secretary_ai_usage WHERE id=%s',
                                          (identity,)).fetchone(), ('completed', 120))
        self.assertIsNotNone(self.scalar("SELECT to_regclass('secretary_email_attempts')"))

    def test_partial_runtime_schema_fails_loudly_not_silently_skipped(self):
        db.init()
        self.conn.execute('DROP TABLE secretary_runtime_observations')
        with self.assertRaises(db.psycopg.errors.UndefinedTable):
            self.store('email')
        self.assertEqual(self.scalar('SELECT count(*) FROM email_messages'), 0)

    def test_whatsapp_enrich_direct_and_revision_early_returns_index(self):
        db.init()
        mid = self.store('whatsapp', source_id='wa')
        previous = self.conn.execute('SELECT id,metadata,sender,subject FROM whatsapp_messages WHERE id=%s', (mid,)).fetchone()
        with self.conn.transaction():
            message_store.enrich_whatsapp(self.conn, previous, dict(chat_name='Anna', chat_name_rank=3), [])
        self.assertEqual(self.scalar("SELECT count(*) FROM work_jobs WHERE kind='knowledge' AND payload->>'message_id'=%s", (str(mid),)), 2)
        options = dict(source_id='edited', update_existing=True)
        revision = dict(direction='out', revision_date='2026-10-16T12:00:00+00:00')
        edited = self.store(metadata=revision, **options)
        self.store(body='Nuovo testo', metadata=dict(revision, revision_date='2026-10-16T13:00:00+00:00'), **options)
        self.conn.execute("DELETE FROM work_jobs WHERE kind='knowledge' AND payload->>'message_id'=%s", (str(edited),))
        self.assertEqual(self.store(body='Vecchio rifiutato', metadata=revision, **options), edited)
        self.assertEqual(self.scalar('SELECT body FROM telegram_messages WHERE id=%s', (edited,)), 'Nuovo testo')
        self.assertEqual(self.scalar("SELECT count(*) FROM work_jobs WHERE kind='knowledge' AND payload->>'message_id'=%s", (str(edited),)), 1)

    def test_history_always_indexes_never_observes_even_replays_and_explicit_ai_grants(self):
        db.init()
        self.consent()
        for channel in ('email', 'whatsapp', 'telegram (account)'):
            source = uuid.uuid4().hex
            mid = self.store(channel, historical=True, source_id=source)
            self.store(channel, source_id=source)
            self.assertEqual(self.scalar("SELECT count(*) FROM work_jobs WHERE kind='knowledge' AND payload->>'message_id'=%s", (str(mid),)), 1)
        mid, aid = self.media(historical=True)
        media_archive.process(dict(attachment_id=aid, archive_only=True))
        self.assertEqual(self.scalar("SELECT status FROM work_jobs WHERE key=%s", (f'media:{aid}',)), 'blocked')
        self.assertEqual(self.scalar('SELECT count(*) FROM secretary_runtime_observations'), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM media_transcripts'), 0)
        self.convert.assert_not_called()

    def test_media_needs_both_grants_of_its_native_channel(self):
        db.init()
        self.consent('telegram')
        self.consent('whatsapp', effects=('ai_media',))
        mid, aid = self.media('whatsapp')
        media_archive.process(dict(attachment_id=aid, channel='telegram', ai_text=True))
        self.assertEqual(self.scalar("SELECT status FROM work_jobs WHERE key=%s", (f'media:{aid}',)), 'blocked')
        self.assertEqual(self.scalar('SELECT count(*) FROM media_transcripts'), 0)
        self.assertIn('consenso', media_text.message_text('caption', [dict(id=aid, content_type='audio/ogg')]))
        self.convert.assert_not_called()

    def test_revocation_or_lost_lease_during_inference_prevents_publication(self):
        db.init()
        self.consent()
        mid, aid = self.media()
        def revoke(attachment):
            self.consent(allowed=False)
            return 'Non pubblicare'
        self.convert.side_effect = revoke
        media_archive.process(dict(attachment_id=aid))
        self.assertEqual(self.scalar('SELECT count(*) FROM media_transcripts'), 0)
        self.assertEqual(self.scalar("SELECT status FROM work_jobs WHERE key=%s", (f'media:{aid}',)), 'blocked')
        self.consent()
        mid, aid = self.media()
        self.convert.side_effect = lambda a: 'Risultato'
        with patch.object(jobs, 'assert_current', side_effect=[True, True, jobs.LeaseLost('lost')]):
            with self.assertRaises(jobs.LeaseLost):
                media_archive.process(dict(attachment_id=aid))
        self.assertEqual(self.scalar('SELECT count(*) FROM media_transcripts'), 0)

    def test_transcript_indexes_atomically_rearms_stale_prepared_observation_and_searches(self):
        db.init()
        self.consent()
        mid, aid = self.media()
        knowledge.index_message(dict(message_id=mid))
        revision = knowledge._snapshot(self.conn, mid)['revision']
        saved = json.dumps(dict(sources=[dict(revision=revision)]))
        self.conn.execute("UPDATE secretary_runtime_observations SET status='stopped',input=%s,plan='{}' WHERE message_id=%s", (saved, mid))
        self.conn.execute("UPDATE work_jobs SET status='done',finished_at=now() WHERE key=%s", (f'secretary-observe:{mid}',))
        self.convert.side_effect = lambda a: self.assertEqual(self.conn.info.transaction_status,
            db.psycopg.pq.TransactionStatus.IDLE) or 'Voce smeraldo'
        media_archive.process(dict(attachment_id=aid))
        self.assertEqual(self.scalar('SELECT text FROM media_transcripts WHERE attachment_id=%s', (aid,)), 'Voce smeraldo')
        status, saved, plan = self.conn.execute('SELECT status,input,plan FROM secretary_runtime_observations WHERE message_id=%s', (mid,)).fetchone()
        self.assertEqual(status, 'pending')
        self.assertIsNone(plan)
        self.assertNotEqual(saved['ingress_revision'], revision)
        self.assertEqual(self.scalar("SELECT status FROM work_jobs WHERE key=%s", (f'secretary-observe:{mid}',)), 'pending')
        knowledge.index_message(dict(message_id=mid))
        self.assertTrue(knowledge.search('smeraldo'))
        self.assertEqual(self.scalar('SELECT body FROM telegram_messages WHERE id=%s', (mid,)), 'Corpo originale')
        self.assertEqual(self.scalar('SELECT data FROM attachments WHERE id=%s', (aid,)), b'original')
        before = self.conn.execute('SELECT * FROM work_jobs ORDER BY id').fetchall()
        media_archive.process(dict(attachment_id=aid))
        self.assertEqual(self.conn.execute('SELECT * FROM work_jobs ORDER BY id').fetchall(), before)

    def test_failed_index_enqueue_rolls_back_transcript_and_observation(self):
        db.init()
        self.consent()
        mid, aid = self.media(mime='image/png')
        before = self.conn.execute('SELECT * FROM secretary_runtime_observations').fetchall()
        with patch.object(knowledge, 'enqueue', side_effect=RuntimeError('index broken')):
            with self.assertRaisesRegex(RuntimeError, 'index broken'):
                media_archive.process(dict(attachment_id=aid))
        self.assertEqual(self.scalar('SELECT count(*) FROM media_transcripts'), 0)
        self.assertEqual(self.conn.execute('SELECT * FROM secretary_runtime_observations').fetchall(), before)

    def test_same_revision_does_not_rearm_terminal_observation(self):
        db.init()
        mid = self.store(source_id='same')
        self.conn.execute("UPDATE secretary_runtime_observations SET status='done' WHERE message_id=%s", (mid,))
        self.conn.execute("UPDATE work_jobs SET status='done',finished_at=now() WHERE key=%s", (f'secretary-observe:{mid}',))
        self.store(source_id='same')
        self.assertEqual(self.scalar('SELECT status FROM secretary_runtime_observations WHERE message_id=%s', (mid,)), 'done')

    def test_live_revision_fences_running_observation_but_never_retries_failed_jobs(self):
        db.init()
        revision = dict(direction='out', revision_date='2026-10-16T12:00:00+00:00')
        mid = self.store(source_id='edits', metadata=revision, update_existing=True)
        key = f'secretary-observe:{mid}'
        self.conn.execute("UPDATE work_jobs SET status='running',token='old',lease_until=now()+interval '1 hour' WHERE key=%s", (key,))
        self.store(source_id='edits', body='Changed', update_existing=True,
                   metadata=dict(revision, revision_date='2026-10-16T13:00:00+00:00'))
        self.assertEqual(self.conn.execute('SELECT status,token,lease_until FROM work_jobs WHERE key=%s', (key,)).fetchone(),
                         ('pending', None, None))
        self.conn.execute("UPDATE work_jobs SET status='failed' WHERE key=%s", (key,))
        self.store(source_id='edits', body='Changed again', update_existing=True,
                   metadata=dict(revision, revision_date='2026-10-16T14:00:00+00:00'))
        self.assertEqual(self.scalar('SELECT status FROM work_jobs WHERE key=%s', (key,)), 'failed')

    def test_blocked_worker_is_terminal_and_storage_replay_does_not_rearm_it(self):
        db.init()
        mid, aid = self.media()
        job = jobs.claim('media')
        self.assertIsNotNone(job)
        context = jobs.ExecutionContext(job[0], job[3], 'media', job[1], float('inf'))
        with patch.object(jobs._local, 'execution', context, create=True):
            media_archive.process(job[1])
            with self.assertRaises(jobs.LeaseLost):
                jobs.assert_current(self.conn)
        self.assertEqual(self.scalar('SELECT status FROM work_jobs WHERE id=%s', (job[0],)), 'blocked')
        self.assertIsNone(jobs.claim('media'))
        with self.conn.transaction():
            media_archive.enqueue_message(self.conn, mid)
        self.assertEqual(self.scalar('SELECT status FROM work_jobs WHERE id=%s', (job[0],)), 'blocked')

    def test_failed_conversion_consumes_existing_retry_budget_then_consumer_can_continue(self):
        db.init()
        self.consent()
        mid, aid = self.media()
        self.convert.side_effect = RuntimeError('private provider detail')
        job = jobs.claim('media')
        self.assertEqual(job[2], 1)
        with self.assertRaises(RuntimeError):
            media_archive.process(job[1])
        jobs.finish(job, RuntimeError('private provider detail'))
        self.assertEqual(self.scalar('SELECT status FROM work_jobs WHERE id=%s', (job[0],)), 'pending')
        with self.assertRaises(jobs.Deferred):
            media_text.message_text('', [dict(id=aid, content_type='audio/ogg')])
        self.conn.execute("UPDATE work_jobs SET attempts=7,available_at=now() WHERE id=%s", (job[0],))
        last = jobs.claim('media')
        self.assertEqual(last[2], 8)
        with self.assertRaises(RuntimeError):
            media_archive.process(last[1])
        jobs.finish(last, RuntimeError('private provider detail'))
        self.assertEqual(self.scalar('SELECT status FROM work_jobs WHERE id=%s', (job[0],)), 'failed')
        self.assertIn('Media non trascritto', media_text.message_text('', [dict(id=aid, content_type='audio/ogg')]))
        self.assertEqual(self.scalar('SELECT count(*) FROM media_transcripts'), 0)

    def test_removing_attachment_during_inference_discards_result(self):
        db.init()
        self.consent()
        mid, aid = self.media()
        def remove(attachment):
            self.conn.execute('DELETE FROM attachments WHERE id=%s', (aid,))
            return 'Discard'
        self.convert.side_effect = remove
        media_archive.process(dict(attachment_id=aid))
        self.assertEqual(self.scalar('SELECT count(*) FROM media_transcripts'), 0)

    def test_note_types_provenance_version_and_legacy_return_contract(self):
        db.init()
        mid = self.store()
        identity = db.add_note('Preferisce il tè', kind='preference', source_message=mid, source_revision='r1')
        self.assertEqual(db.personal_memories(), [dict(id=identity, text='Preferisce il tè')])
        self.assertEqual(db.personal_memories(provenance=True)[0], dict(id=identity, text='Preferisce il tè',
            kind='preference', source_message=mid, source_revision='r1', version=1, topic=None))
        changed = db.apply_personal_memories([dict(operation='update', id=identity, text='Preferisce il caffè')],
            {identity}, expected={identity: 'Preferisce il tè'}, source_message=mid, source_revision='r2')
        self.assertEqual(changed, 1)
        memory = db.personal_memories(provenance=True)[0]
        self.assertEqual((memory['kind'], memory['version'], memory['source_revision']), ('preference', 2, 'r2'))
        with self.assertRaises(ValueError):
            db.add_note('Invalid', source_message=True)


if __name__ == '__main__':
    unittest.main()