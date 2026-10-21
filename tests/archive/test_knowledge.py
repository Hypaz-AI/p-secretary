"""Offline contracts; imports cannot load the deployment dotenv in isolation."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

with tempfile.TemporaryDirectory(prefix='knowledge-import-') as _bootstrap:
    with patch.dict(os.environ, {'P_SECRETARY_CONFIG_FILE': '/dev/null',
                                'DATA_DIR': _bootstrap, 'DATABASE_URL': ''}):
        from secretary.archive import knowledge as k


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix='knowledge-unit-'))
        self.base = Path(self.tmp)
        self.data = self.base / 'data'
        self.invoices = self.base / 'invoices'
        (self.data / 'uploads').mkdir(parents=True)
        self.invoices.mkdir()
        self.enterContext(patch.object(k.config, 'BASE_DIR', self.base))
        self.enterContext(patch.object(k.config, 'DATA_DIR', self.data))
        self.enterContext(patch.object(k.config, 'INVOICE_DIR', self.invoices))
        self.enterContext(patch.object(k.db, '_conn', side_effect=AssertionError('Configured DB forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))

    def attachment(self, **fields):
        result = dict(id=8, filename='accordo.xml', content_type='application/xml',
                      path=None, byte_size=27, data=b'<a>Riparazione caldaia</a>',
                      duration=None, transcript=None, transcript_hash=None, transcript_kind=None)
        result.update(fields)
        return result

    def message(self):
        return dict(id=42, channel='email', sender='Mittente', subject='Accordo',
                    body='corpo', ts=None, acquired_at=None, source_id='native:42',
                    conversation_id=None, direction='in', native_revision='', historical=True)

    def snapshot(self):
        return dict(message=self.message(), attachments=[], attachment_count=0, revision='r1')

    @contextmanager
    def connection(self, conn):
        with patch.object(k.db, '_conn') as connect:
            connect.return_value.__enter__.return_value = conn
            yield connect

    def test_migrate_additive_receipt_and_no_jobs_or_resets(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        receipt = k.migrate(conn)
        self.assertEqual(receipt, dict(version=k.VERSION, applied=True, reset_rows=0))
        sql = '\n'.join(c.args[0] for c in conn.execute.call_args_list)
        self.assertIn('knowledge_occurrences', sql)
        self.assertIn("to_tsvector('italian'", sql)
        self.assertNotRegex(sql, r'(?i)\b(DROP|TRUNCATE|DELETE)\b|INSERT INTO work_jobs')

    def test_migrate_existing_receipt_skips_ddl(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)
        receipt = k.migrate(conn)
        self.assertFalse(receipt['applied'])
        self.assertFalse(any(c.args[0] == k.SCHEMA for c in conn.execute.call_args_list))

    def test_enqueue_key_is_revision_scoped_and_archive_only(self):
        conn = MagicMock()
        with patch.object(k, '_snapshot', return_value=self.snapshot()), patch.object(
                k.jobs, 'enqueue', return_value=(7,)) as enqueue:
            result = k.enqueue(conn, 42)
        enqueue.assert_called_once_with(conn, 'knowledge', 'knowledge:42:r1',
                                       dict(message_id=42, revision='r1', archive_only=True))
        self.assertTrue(result['queued'])
        self.assertEqual(result['job_id'], 7)

    def test_enqueue_missing_or_duplicate(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch.object(k, '_snapshot', return_value=None), patch.object(k.jobs, 'enqueue') as enqueue:
            self.assertEqual(k.enqueue(conn, 42)['status'], 'missing')
            enqueue.assert_not_called()
        with patch.object(k, '_snapshot', return_value=self.snapshot()), patch.object(
                k.jobs, 'enqueue', return_value=None):
            self.assertFalse(k.enqueue(conn, 42)['queued'])

    def test_safe_xml_bytes_extract_and_no_arbitrary_metadata(self):
        result = k._extract(self.attachment(secret='provider-secret'), {}, [0])
        self.assertEqual(result['text'], 'Riparazione caldaia')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['sha256'], hashlib.sha256(self.attachment()['data']).hexdigest())

    def test_path_and_bytea_content_hash_match_and_original_is_unchanged(self):
        path = self.data / 'uploads' / 'accordo.xml'
        content = self.attachment()['data']
        path.write_bytes(content)
        before = path.stat()
        result = k._extract(self.attachment(path=str(path), byte_size=None, data=None), {}, [0])
        self.assertEqual(result['text'], 'Riparazione caldaia')
        self.assertEqual(result['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_legacy_relative_path_and_invoice_path_are_selected_only(self):
        for path in (self.data / 'uploads' / 'old.xml', self.invoices / 'document.xml'):
            path.write_bytes(b'<a>Documento storico</a>')
            relative = str(path.relative_to(self.base))
            self.assertIn('storico', k._extract(self.attachment(
                path=relative, byte_size=None, data=None), {}, [0])['text'])

    def test_outside_traversal_secret_and_arbitrary_data_dir_blocked(self):
        for relative in ('../outside.xml', 'data/uploads/../../outside.xml', 'data/ms_tokens.json',
                         'data/uploads/.env', 'data/wa_session/keys.xml', 'data/uploads/secret.xml'):
            result = k._extract(self.attachment(path=relative, byte_size=None, data=None), {}, [0])
            self.assertEqual(result['status'], 'blocked', relative)

    def test_symlink_leaf_intermediate_root_and_hardlink_blocked(self):
        original = self.invoices / 'original.xml'
        original.write_bytes(b'<a>non leggere</a>')
        leaf = self.data / 'uploads' / 'leaf.xml'
        leaf.symlink_to(original)
        intermediate = self.data / 'uploads' / 'linked'
        intermediate.symlink_to(self.invoices, target_is_directory=True)
        hardlink = self.data / 'uploads' / 'hard.xml'
        os.link(original, hardlink)
        for path in (leaf, intermediate / 'original.xml', hardlink):
            result = k._extract(self.attachment(path=str(path), byte_size=None, data=None), {}, [0])
            self.assertEqual(result['status'], 'blocked')
        root_link = self.base / 'linked-root'
        root_link.symlink_to(self.data, target_is_directory=True)
        with patch.object(k.config, 'DATA_DIR', root_link):
            self.assertEqual(k._extract(self.attachment(path=str(root_link / 'uploads' / 'leaf.xml'),
                byte_size=None, data=None), {}, [0])['status'], 'blocked')

    def test_missing_oversize_fifo_and_budget_are_explicit(self):
        attachments = [self.attachment(byte_size=None, data=None),
                       self.attachment(byte_size=k.MAX_BYTES + 1, data=None),
                       self.attachment(budget_exceeded=True)]
        for attachment, expected in zip(attachments, ('missing', 'too_large', 'budget_exceeded')):
            self.assertEqual(k._extract(attachment, {}, [0])['status'], expected)
        fifo = self.data / 'uploads' / 'pipe.xml'
        os.mkfifo(fifo)
        self.assertEqual(k._extract(self.attachment(path=str(fifo), byte_size=None, data=None),
                                    {}, [0])['status'], 'blocked')
        with patch.object(k, 'MAX_MESSAGE_BYTES', 1):
            self.assertEqual(k._extract(self.attachment(), {}, [0])['status'], 'budget_exceeded')

    def test_path_signature_detects_repair_and_same_identity_edit(self):
        path = self.data / 'uploads' / 'file.xml'
        self.assertEqual(k._path_revision(str(path)), 'missing')
        path.write_bytes(b'<a>prima</a>')
        old = k._path_revision(str(path))
        path.write_bytes(b'<a>dopo molto diverso</a>')
        self.assertNotEqual(k._path_revision(str(path)), old)

    def test_document_dedup_does_not_dedup_occurrence_transcript(self):
        cache = {}
        with patch.object(k.documents, 'extract', return_value='testo comune') as extract:
            a = k._extract(self.attachment(transcript='voce prima'), cache, [0])
            b = k._extract(self.attachment(id=9, transcript='voce seconda'), cache, [0])
        extract.assert_called_once()
        self.assertEqual(a['sha256'], b['sha256'])
        self.assertIn('prima', a['entry_text'])
        self.assertNotIn('prima', b['entry_text'])
        self.assertIn('seconda', b['entry_text'])

    def test_persisted_extraction_cache_skips_parser(self):
        saved = dict(text='estratto salvato', status='ready', error_code=None, truncated=False)
        with patch.object(k.documents, 'extract') as extract:
            result = k._extract(self.attachment(), {}, [0], lambda digest, suffix: saved)
        extract.assert_not_called()
        self.assertEqual(result['text'], 'estratto salvato')

    def test_failed_extraction_sanitized_and_native_transcript_retained(self):
        with patch.object(k.documents, 'extract', side_effect=RuntimeError('API_TOKEN=do-not-export')):
            result = k._extract(self.attachment(transcript='trascrizione già acquisita'), {}, [0])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['entry_status'], 'partial')
        self.assertEqual(result['error_code'], 'extraction_failed')
        self.assertNotIn('API_TOKEN', str(result))
        self.assertIn('acquisita', result['entry_text'])

    def test_empty_unsupported_pending_transcript_and_text_cap(self):
        image = self.attachment(filename='image.jpg', content_type='image/jpeg')
        self.assertEqual(k._extract(image, {}, [0])['entry_status'], 'pending_transcript')
        image['transcript'] = 'x' * (k.MAX_TEXT + 1)
        result = k._extract(image, {}, [0])
        self.assertEqual(len(result['entry_text']), k.MAX_TEXT)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['entry_status'], 'ready')
        self.assertFalse(result['extraction_truncated'])
        with patch.object(k.documents, 'extract', return_value=''):
            self.assertEqual(k._extract(self.attachment(), {}, [0])['status'], 'empty')

    def test_xml_entities_fail_closed(self):
        result = k._extract(self.attachment(data=b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>'), {}, [0])
        self.assertEqual(result['status'], 'failed')

    def test_pdf_uses_real_local_parser_when_available(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest('Optional pypdf dependency is not installed')
        from io import BytesIO
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(200, 200)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 10 100 Td (Preventivo smeraldo) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        output = BytesIO()
        writer.write(output)
        result = k._extract(self.attachment(filename='preventivo.pdf', data=output.getvalue(),
                                            byte_size=len(output.getvalue())), {}, [0])
        self.assertEqual(result['status'], 'ready')
        self.assertIn('smeraldo', result['text'])

    def test_byte_budget_skips_remaining_sources_before_read(self):
        with patch.object(k, '_bytes') as read:
            result = k._extract(self.attachment(), {}, [k.MAX_MESSAGE_BYTES])
        read.assert_not_called()
        self.assertEqual(result['status'], 'budget_exceeded')

    def test_snapshot_fingerprint_tracks_full_body_and_transcript_revisions(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [
            (42, 'email', 'sender', 'subject', 'caption', None, None, 'native', None,
             'in', '', False, 'body-hash-1'), (1,)]
        conn.execute.return_value.fetchall.return_value = [
            (8, 'voice.ogg', 'audio/ogg', None, 5, 'bytes-hash', None, None,
             'voice', 'transcript-hash-1', 'audio')]
        with patch.object(k, '_exists', return_value=True):
            first = k._snapshot(conn, 42)
        conn.execute.return_value.fetchone.side_effect = [
            (42, 'email', 'sender', 'subject', 'caption', None, None, 'native', None,
             'in', '', False, 'body-hash-2'), (1,)]
        with patch.object(k, '_exists', return_value=True):
            second = k._snapshot(conn, 42)
        self.assertNotEqual(first['revision'], second['revision'])

    def test_worker_ignores_payload_text_paths_and_authority(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        snapshot = self.snapshot()
        snapshot['attachments'] = [self.attachment()]
        with self.connection(conn), patch.object(k, '_snapshot', return_value=snapshot), patch.object(
                k, '_cached_extraction', return_value=None), patch.object(k, '_publish', return_value={'status': 'indexed'}) as publish:
            result = k.index_message(dict(message_id=42, text='FAKE', path='/etc/passwd', send=True))
        self.assertEqual(result['status'], 'indexed')
        self.assertEqual(publish.call_args.args[1]['message']['body'], 'corpo')
        self.assertEqual(publish.call_args.args[2][0]['text'], 'Riparazione caldaia')

    def test_stale_worker_only_enqueues_current_revision(self):
        conn = MagicMock()
        with self.connection(conn), patch.object(k, '_snapshot', return_value=self.snapshot()), patch.object(
                k, '_enqueue_snapshot') as enqueue, patch.object(k.documents, 'extract') as extract:
            result = k.index_message(dict(message_id=42, revision='old'))
        self.assertEqual(result['status'], 'stale')
        enqueue.assert_called_once()
        extract.assert_not_called()

    def test_publication_rechecks_revision_after_parser_and_does_not_write_stale_text(self):
        conn = MagicMock()
        newer = self.snapshot()
        newer['revision'] = 'r2'
        with patch.object(k, '_snapshot', return_value=newer), patch.object(k, '_enqueue_snapshot') as enqueue:
            result = k._publish(conn, self.snapshot(), [])
        self.assertEqual(result['status'], 'stale')
        enqueue.assert_called_once_with(conn, newer)
        self.assertFalse(any('INSERT INTO knowledge_' in c.args[0] for c in conn.execute.call_args_list))

    def test_source_reference_is_selected_and_not_raw_metadata(self):
        message = self.message()
        message['metadata'] = {'api_key': 'private'}
        self.assertNotIn('private', str(k._source_ref(message)))
        self.assertEqual(k._source_ref(message)['message_id'], 42)

    def test_backfill_cursor_bounded_and_no_actions(self):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [(2,), (3,), (4,)]
        with self.connection(conn), patch.object(k, 'enqueue', side_effect=[{'queued': True}, {'queued': False}]) as enqueue:
            result = k.backfill(batch=2, after_id=1)
        self.assertEqual(result, dict(scanned=2, queued=1, after_id=3, has_more=True, archive_only=True))
        self.assertEqual([c.args[1] for c in enqueue.call_args_list], [2, 3])
        self.assertEqual(conn.execute.call_args.args[1], (1, 3))


if __name__ == '__main__':
    unittest.main()