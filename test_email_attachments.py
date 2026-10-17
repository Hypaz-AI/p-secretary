"""Offline file storage and migration tests: no real database connections."""
from contextlib import nullcontext
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import config
import email_attachments
import message_store
import migrate_email_attachments
import pipeline


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'email_attachments'
        patched = patch.object(config, 'EMAIL_ATTACHMENTS_DIR', self.root)
        patched.start()
        self.addCleanup(patched.stop)
        self.conn = MagicMock()
        self.conn.transaction.side_effect = lambda: nullcontext()
        # Catalogue/runtime integration is covered by test_secretary_bootstrap.
        self.derived = self.enterContext(patch.object(message_store, 'enqueue_derived'))

    def test_persist_deduplicates_pdf_and_empty_attachment(self):
        for data in (b'%PDF-test', b'', b'<xml/>'):
            path = email_attachments.persist(data)
            self.assertEqual(Path(path).name, hashlib.sha256(data).hexdigest())
            self.assertEqual(Path(path).read_bytes(), data)
            self.assertEqual(email_attachments.persist(memoryview(data)), path)
        self.assertEqual(len(list(self.root.iterdir())), 3)

    def test_corrupt_existing_file_is_not_overwritten(self):
        path = Path(email_attachments.persist(b'original'))
        path.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            email_attachments.persist(b'original')
        self.assertEqual(path.read_bytes(), b'corrupt')

    def test_atomic_write_failure_cleans_temporary_and_never_inserts(self):
        self.conn.execute.return_value.fetchone.return_value = (42,)
        with patch.object(email_attachments.os, 'link', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                message_store.store(self.conn, 'email', 's', 't', 'b', attachments=[
                    {'filename': 'x.pdf', 'data': b'%PDF-test'}])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(any('INSERT INTO attachments' in c.args[0]
                             for c in self.conn.execute.call_args_list))

    def test_store_email_path_not_blob_and_keeps_input(self):
        self.conn.execute.return_value.fetchone.return_value = (42,)
        att = {'filename': '../invoice.pdf', 'content_type': 'application/pdf', 'data': b'%PDF-test'}
        message_store.store(self.conn, 'email (work)', 's', 't', 'b', attachments=[att])
        args = self.conn.execute.call_args.args[1]
        self.assertEqual(args[:4], (42, 'email', '../invoice.pdf', 'application/pdf'))
        self.assertEqual(Path(args[4]).read_bytes(), att['data'])
        self.assertIsNone(args[5])
        self.assertEqual(att['data'], b'%PDF-test')
        self.derived.assert_called_once_with(self.conn, 42, historical=False)

    def test_duplicate_message_does_not_write_attachments(self):
        self.conn.execute.return_value.fetchone.side_effect = [None, (42,)]
        message_store.store(self.conn, 'email', 's', 't', 'b', source_id='id',
                            attachments=[{'data': b'pdf'}])
        self.assertFalse(self.root.exists())
        self.derived.assert_called_once_with(self.conn, 42, historical=False)

    def test_pipeline_load_preserves_legacy_blob_and_new_path(self):
        path = email_attachments.persist(b'new')
        with patch.object(pipeline.db, '_conn') as connect:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = ('email','s','t','b','id')
            connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
                ('old.pdf','application/pdf',None,b'old'), ('new.pdf','application/pdf',path,None)]
            _, attachments = pipeline.load_message(42)
        self.assertEqual(attachments[0]['data'], b'old')
        self.assertEqual(Path(attachments[1]['path']).read_bytes(), b'new')

    def test_migration_verified_before_update_and_resumable(self):
        self.conn.execute.return_value.fetchone.side_effect = [(1, memoryview(b'pdf')), None]
        self.assertEqual(migrate_email_attachments.migrate(self.conn), 1)
        updates = [c for c in self.conn.execute.call_args_list if c.args[0].startswith('UPDATE')]
        self.assertEqual(len(updates), 1)
        path, row_id = updates[0].args[1]
        self.assertEqual(row_id, 1)
        self.assertEqual(Path(path).read_bytes(), b'pdf')
        self.assertIn('data=NULL', updates[0].args[0])
        self.conn.execute.return_value.fetchone.side_effect = [None]
        self.assertEqual(migrate_email_attachments.migrate(self.conn), 0)

    def test_migration_verification_failure_keeps_blob(self):
        self.conn.execute.return_value.fetchone.return_value = (1, b'pdf')
        with patch.object(email_attachments, 'verify', side_effect=ValueError('corrupt')):
            with self.assertRaises(ValueError):
                migrate_email_attachments.migrate(self.conn)
        self.assertFalse(any(c.args[0].startswith('UPDATE') for c in self.conn.execute.call_args_list))

    def test_migration_limit(self):
        self.conn.execute.return_value.fetchone.return_value = (1, b'pdf')
        self.assertEqual(migrate_email_attachments.migrate(self.conn, limit=1), 1)


if __name__ == '__main__':
    unittest.main()