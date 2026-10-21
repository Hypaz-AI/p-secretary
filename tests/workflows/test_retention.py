"""Retention offline: chiavi dei file, grazia, passata giornaliera, configurazione.

Nessun database e nessuna rete: il DB è sostituito da un finto connection context,
i file vivono in una cartella temporanea. La potatura reale sul DB è in
test_retention_postgres.py (opt-in, P_SECRETARY_TEST_DATABASE_URL).
"""
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from secretary.core import config, db
from secretary.workflows import retention

OLD = time.time() - 60 * 86400  # oltre la grazia di 30 giorni
RECENT = time.time()


def fake_db(stamp=None):
    """Context manager di db._conn con una riga di stamp (o nessuna)."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None if stamp is None else (stamp,)
    context = MagicMock()
    context.__enter__.return_value = conn
    context.__exit__.return_value = False
    return context, conn


class KeyTests(unittest.TestCase):
    def test_key_is_relative_so_old_host_paths_match_current_files(self):
        self.assertEqual(retention._key('/old/host/data/email_attachments/abc'), 'email_attachments/abc')
        self.assertEqual(retention._key('/srv/data/tg_files/x.pdf'), 'tg_files/x.pdf')
        self.assertEqual(retention._key('wa_media/w1.jpg'), 'wa_media/w1.jpg')

    def test_paths_outside_media_or_nested_are_not_keyed(self):
        self.assertIsNone(retention._key('/data/invoices/hypaz/2026-01/a.pdf'))
        self.assertIsNone(retention._key('/data/tg_files/sub/x.pdf'))
        self.assertIsNone(retention._key(None))

    def test_media_file_rejects_traversal_and_foreign_folders(self):
        with tempfile.TemporaryDirectory() as data, patch.object(config, 'DATA_DIR', Path(data)):
            self.assertIsNone(retention._media_file('email_attachments/..'))
            self.assertIsNone(retention._media_file('email_attachments/.'))
            self.assertIsNone(retention._media_file('invoices/a.pdf'))
            self.assertEqual(retention._media_file('tg_files/a.pdf'), Path(data) / 'tg_files' / 'a.pdf')


class FileReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.enterContext(patch.object(config, 'DATA_DIR', self.data))
        for folder in ('email_attachments', 'tg_files', 'wa_media', 'invoices'):
            (self.data / folder).mkdir()

    def file(self, folder, name, mtime):
        path = self.data / folder / name
        path.write_bytes(b'x')
        os.utime(path, (mtime, mtime))
        return path

    def test_released_file_removed_only_when_unreferenced_and_old(self):
        gone = self.file('email_attachments', 'shaA', OLD)
        kept = self.file('email_attachments', 'shaS', OLD)
        fresh = self.file('wa_media', 'w1.jpg', RECENT)
        with patch.object(retention, '_referenced', return_value={'email_attachments/shaS'}) as refs, \
                patch.object(db, '_conn', return_value=fake_db()[0]):
            removed = retention._release({'email_attachments/shaA', 'email_attachments/shaS',
                                          'wa_media/w1.jpg'}, time.time() - retention.FILE_GRACE_SECONDS)
        self.assertEqual(removed, 1)  # solo shaA: non referenziato e più vecchio della grazia
        self.assertFalse(gone.exists())
        self.assertTrue(kept.exists())  # ancora referenziato
        self.assertTrue(fresh.exists())  # recente: la grazia sulla data di modifica lo protegge
        refs.assert_called_once()

    def test_orphan_sweep_only_touches_old_unreferenced_email_attachments(self):
        orphan = self.file('email_attachments', 'orphan', OLD)
        recent = self.file('email_attachments', 'recent', RECENT)
        referenced = self.file('email_attachments', 'used', OLD)
        spool = self.file('tg_files', 'old-download.pdf', OLD)
        with patch.object(retention, '_referenced', return_value={'email_attachments/used'}), \
                patch.object(db, '_conn', return_value=fake_db()[0]):
            removed = retention._sweep_orphans(time.time() - retention.FILE_GRACE_SECONDS)
        self.assertEqual(removed, 1)
        self.assertFalse(orphan.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(referenced.exists())
        self.assertTrue(spool.exists())  # tg_files non è swept: può essere in attesa di archiviazione

    def test_symlinks_and_directories_are_never_deleted(self):
        target = self.file('invoices', 'fattura.pdf', OLD)
        link = self.data / 'email_attachments' / 'link'
        os.symlink(target, link)
        (self.data / 'email_attachments' / 'subdir').mkdir()
        with patch.object(retention, '_referenced', return_value=set()), \
                patch.object(db, '_conn', return_value=fake_db()[0]):
            retention._sweep_orphans(time.time() - retention.FILE_GRACE_SECONDS)
        self.assertTrue(target.exists())
        self.assertTrue(os.path.islink(link))
        self.assertTrue((self.data / 'email_attachments' / 'subdir').is_dir())

    def test_reusing_a_deduplicated_attachment_refreshes_its_mtime(self):
        from secretary.archive import email_attachments
        with patch.object(config, 'EMAIL_ATTACHMENTS_DIR', self.data / 'email_attachments'):
            path = Path(email_attachments.persist(b'stesso contenuto'))
            os.utime(path, (OLD, OLD))
            self.assertEqual(email_attachments.persist(b'stesso contenuto'), str(path))
        self.assertGreater(path.stat().st_mtime, time.time() - 60)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(db, 'available', return_value=True))
        self.today = datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
        self.complete = {'messages': 0, 'files': 0, 'job_payloads': 0, 'outbox': 0,
                         'ai_usage': 0, 'complete': True, 'skipped': []}

    def test_one_pass_per_day_when_complete(self):
        context, _ = fake_db({'day': self.today, 'complete': True})
        with patch.object(db, '_conn', return_value=context), patch.object(retention, 'run') as run:
            self.assertIsNone(retention.run_scheduled())
        run.assert_not_called()

    def test_incomplete_pass_resumes_on_next_tick(self):
        context, _ = fake_db({'day': self.today, 'complete': False})
        with patch.object(db, '_conn', return_value=context), \
                patch.object(retention, 'run', return_value=dict(self.complete, complete=False)) as run:
            report = retention.run_scheduled()
        run.assert_called_once()
        self.assertFalse(report['complete'])

    def test_disabled_classes_never_touch_the_database(self):
        with patch.object(config, 'RETENTION_MESSAGES_DAYS', 0), \
                patch.object(config, 'RETENTION_OPERATIONAL_DAYS', 0), \
                patch.object(config, 'RETENTION_AI_USAGE_DAYS', 0), \
                patch.object(db, '_conn', side_effect=AssertionError('DB forbidden')):
            self.assertIsNone(retention.run_scheduled())

    def test_run_skips_message_pruning_while_legacy_tables_exist(self):
        context, conn = fake_db()
        conn.execute.return_value.fetchone.return_value = ('secretary_links',)
        with patch.object(config, 'RETENTION_MESSAGES_DAYS', 730), \
                patch.object(config, 'RETENTION_OPERATIONAL_DAYS', 0), \
                patch.object(config, 'RETENTION_AI_USAGE_DAYS', 0), \
                patch.object(db, '_conn', return_value=context), \
                patch.object(retention, '_batch', side_effect=AssertionError('no deletes')):
            report = retention.run()
        self.assertEqual(report['skipped'], ['legacy_tables'])
        self.assertEqual(report['messages'], 0)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('RETENTION_MESSAGES_DAYS', None)
            self.assertEqual(config._retention_days('RETENTION_MESSAGES_DAYS', 730, 36500), 730)
        with patch.dict(os.environ, {'RETENTION_MESSAGES_DAYS': 'abc'}):
            self.assertEqual(config._retention_days('RETENTION_MESSAGES_DAYS', 730, 36500), 730)
        with patch.dict(os.environ, {'RETENTION_MESSAGES_DAYS': '-5'}):
            self.assertEqual(config._retention_days('RETENTION_MESSAGES_DAYS', 730, 36500), 730)
        with patch.dict(os.environ, {'RETENTION_MESSAGES_DAYS': '0'}):
            self.assertEqual(config._retention_days('RETENTION_MESSAGES_DAYS', 730, 36500), 0)
        with patch.dict(os.environ, {'RETENTION_MESSAGES_DAYS': '99999'}):
            self.assertEqual(config._retention_days('RETENTION_MESSAGES_DAYS', 730, 36500), 730)


class ProtectionContractTests(unittest.TestCase):
    """Il predicato di protezione copre ogni tabella che tiene vivo un messaggio."""

    def test_every_live_reference_is_protected(self):
        clause = retention._protected('m')
        for needle in ('tasks t', 'task_changes', 'notes n', 'effect_receipts',
                       "'authorized', 'attempted', 'uncertain'", 'agent_email_outbox',
                       "'proposed', 'approved', 'uncertain'", 'calendar_effect_grants',
                       'system_notifications', 'source_ids', 'media_source_ids',
                       "'pending', 'running', 'blocked'", "j.payload->>'message_id'",
                       "j.payload->>'attachment_id'"):
            with self.subTest(reference=needle):
                self.assertIn(needle, clause)

    def test_job_keys_and_identity_tables_are_never_deleted(self):
        import inspect
        source = inspect.getsource(retention)
        for forbidden in ('DELETE FROM work_jobs', 'DELETE FROM message_dedup', 'DELETE FROM email_seen',
                          'DELETE FROM effect_receipts', 'DELETE FROM notes', 'DELETE FROM tasks',
                          'DELETE FROM privacy_receipts', 'DELETE FROM invoices'):
            with self.subTest(statement=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
