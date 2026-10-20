"""Opt-in SQL tests: explicit *_test database, unique schema, no public fallback.

Only PI_SECRETARY_TEST_DATABASE_URL is used; no servers/workers are started.
Fresh-interpreter imports disable dotenv and use temporary application data.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

with tempfile.TemporaryDirectory(prefix='knowledge-pg-import-') as _bootstrap:
    with patch.dict(os.environ, {'PI_SECRETARY_CONFIG_FILE': '/dev/null',
                                'DATA_DIR': _bootstrap, 'DATABASE_URL': ''}):
        import db
        import jobs
        import knowledge as k
        import message_store


def validated_test_params(url):
    """Validate before connecting, including libpq environment overrides."""
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(url)
    if not params.get('dbname', '').endswith('_test') or params.get('service'):
        raise ValueError('An explicit database ending in _test (no service) is required')
    return params


class KnowledgePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
        if not url:
            raise unittest.SkipTest('Set PI_SECRETARY_TEST_DATABASE_URL to opt in')
        if db.psycopg is None:
            raise unittest.SkipTest('psycopg is not installed')
        cls.params = validated_test_params(url)
        # Never inherit libpq services/options/passwords from the deployment.
        cls.enterClassContext(patch.dict(os.environ,
            {key: value for key, value in os.environ.items() if not key.startswith('PG')}, clear=True))
        cls.enterClassContext(patch.object(db, '_conn', side_effect=AssertionError('Configured DB forbidden')))

    def setUp(self):
        from psycopg import sql
        self.schema = 'knowledge_test_' + uuid.uuid4().hex
        self.conn = db.psycopg.connect(**self.params, autocommit=True,
                                      prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.assertTrue(self.conn.execute('SELECT current_database()').fetchone()[0].endswith('_test'))
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='15s'")
        self.conn.execute("SET lock_timeout='10s'")
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)
        self.conn.execute('''CREATE TABLE media_transcripts (
            attachment_id BIGINT PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,text TEXT NOT NULL,model TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix='knowledge-sql-'))
        self.data = Path(self.tmp) / 'data'
        (self.data / 'uploads').mkdir(parents=True)
        self.enterContext(patch.object(k.config, 'DATA_DIR', self.data))
        self.enterContext(patch.object(k.config, 'INVOICE_DIR', Path(self.tmp) / 'invoices'))
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        k.migrate(self.conn)

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    @contextmanager
    def connection(self):
        yield self.conn

    def message(self, channel='email', body='Accordi riparazione caldaia', historical=False):
        table = {'email': 'email_messages', 'whatsapp': 'whatsapp_messages',
                 'telegram (account)': 'telegram_messages'}[channel]
        return self.conn.execute(f'''INSERT INTO {table}
            (channel,sender,subject,body,metadata,source_id,ts)
            VALUES (%s,'Mittente','Preventivo',%s,%s,%s,'2020-01-02T12:00:00Z') RETURNING id''',
            (channel, body, json.dumps(dict(historical=historical, api_token='never-export')),
             uuid.uuid4().hex)).fetchone()[0]

    def attachment(self, mid, content=b'<a>Preventivo manutenzione termosifoni</a>', **fields):
        return self.conn.execute('''INSERT INTO attachments
            (message_id,channel,filename,content_type,path,data,metadata)
            VALUES (%s,'email',%s,%s,%s,%s,%s) RETURNING id''',
            (mid, fields.get('filename', 'preventivo.xml'), fields.get('mime', 'application/xml'),
             fields.get('path'), content, json.dumps({'api_key': 'never-export', 'duration': 3}))).fetchone()[0]

    ENTRY = """e.id,e.source_type,CASE WHEN e.source_type='document' THEN e.occurrence_id ELSE e.message_id END,
        r.revision,e.channel,e.title,e.ts,e.acquired_at,e.historical,e.status,e.source_ref,e.active"""

    def hit(self, row):
        entry, kind, source, revision, channel, title, date, acquired, historical, status, ref, active = row[:12]
        result = dict(entry_id=entry, source_type=kind, source_id=source, revision=revision, channel=channel,
                      title=title, date=date.isoformat() if date else None,
                      acquired_at=acquired.isoformat() if acquired else None,
                      historical=historical, status=status, source=ref, current=active,
                      citation=f'{kind}:{source}@{revision}')
        if len(row) > 12:
            result['text'] = row[12]
        return result

    def search(self, query, channel=None, limit=8):
        """Lettura diretta dell'indice (la stessa che l'agente fa con SQL su agent.documents)."""
        rows = self.conn.execute(f"""SELECT {self.ENTRY} FROM knowledge_entries e
            JOIN knowledge_runs r ON r.id=e.run_id
            WHERE e.active AND e.search_vector @@ websearch_to_tsquery('italian',%s)
              AND (%s::text IS NULL OR e.channel=%s)
            ORDER BY e.ts DESC NULLS LAST,e.id DESC LIMIT %s""", (query, channel, channel, limit)).fetchall()
        return [self.hit(row) for row in rows]

    def opened(self, source_type, identity):
        where = {'message': "e.source_type='message' AND e.message_id=%s AND e.active",
                 'document': "e.source_type='document' AND e.occurrence_id=%s", 'entry': 'e.id=%s'}[source_type]
        row = self.conn.execute(f"""SELECT {self.ENTRY},e.text FROM knowledge_entries e
            JOIN knowledge_runs r ON r.id=e.run_id WHERE {where} ORDER BY e.id DESC LIMIT 1""",
            (identity,)).fetchone()
        result = self.hit(row)
        if source_type == 'message':
            result['documents'] = [self.hit(r) for r in self.conn.execute(f"""SELECT {self.ENTRY}
                FROM knowledge_entries e JOIN knowledge_runs r ON r.id=e.run_id
                WHERE e.message_id=%s AND r.revision=%s AND e.source_type='document' ORDER BY e.id""",
                (identity, result['revision'])).fetchall()]
        return result

    def documents(self):
        return self.conn.execute("SELECT count(*) FROM knowledge_entries WHERE active AND source_type='document'").fetchone()[0]

    def index(self, mid):
        receipt = k.enqueue(self.conn, mid)
        return k.index_message(dict(message_id=mid, revision=receipt['revision']))

    def test_migration_receipt_is_additive_and_repeat_preserves_every_native_row(self):
        mid = self.message(historical=True)
        aid = self.attachment(mid)
        before = self.conn.execute('SELECT * FROM email_messages').fetchall()
        self.assertFalse(k.migrate(self.conn)['applied'])
        self.assertEqual(self.conn.execute('SELECT * FROM email_messages').fetchall(), before)
        self.assertEqual(self.conn.execute('SELECT id FROM attachments').fetchall(), [(aid,)])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT reset_rows FROM schema_migrations WHERE version=%s',
                                          (k.VERSION,)).fetchone(), (0,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0], 0)

    def test_cross_channel_italian_search_citations_dates_history_and_filters(self):
        ids = [self.message(c, historical=True) for c in ('email', 'whatsapp', 'telegram (account)')]
        for mid in ids:
            self.assertEqual(self.index(mid)['status'], 'indexed')
        hits = self.search('riparazioni')  # Italian stemming, not substring matching.
        self.assertEqual({r['source_id'] for r in hits}, set(ids))
        self.assertTrue(all(r['historical'] for r in hits))
        self.assertTrue(all(r['date'].startswith('2020-01-02') for r in hits))
        self.assertTrue(all(r['citation'] and r['revision'] and r['acquired_at'] for r in hits))
        self.assertEqual(len(self.search('caldaia', channel='whatsapp')), 1)
        self.assertEqual(self.search('caldaia', channel="email' OR true --"), [])
        self.assertNotIn('never-export', str(hits))

    def test_hash_content_dedup_keeps_distinct_occurrences_paths_and_selected_metadata(self):
        content = b'<a>Preventivo manutenzione termosifoni</a>'
        path = self.data / 'uploads' / 'old.xml'
        path.write_bytes(content)
        a, b = self.message(), self.message('whatsapp')
        self.attachment(a, content)
        self.attachment(b, None, path=str(path))
        with patch.object(k.documents, 'extract', wraps=k.documents.extract) as extract:
            self.index(a)
            self.index(b)
        extract.assert_called_once()  # Cross-message persistent extraction reuse.
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_documents').fetchone(), (1,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_occurrences').fetchone(), (2,))
        hits = self.search('termosifoni')
        self.assertEqual(len(hits), 2)
        self.assertEqual(len({h['source_id'] for h in hits}), 2)
        self.assertEqual(len({h['source']['sha256'] for h in hits}), 1)
        self.assertEqual(path.read_bytes(), content)
        self.assertNotIn(str(path), str(hits))
        self.assertEqual(self.conn.execute('SELECT metadata FROM knowledge_occurrences ORDER BY id').fetchall(),
                         [({'duration': 3},), ({'duration': 3},)])

    def test_retry_edit_and_old_entry_provenance(self):
        mid = self.message(body='Zirconio vecchio')
        self.index(mid)
        old = self.search('zirconio')[0]
        before = self.conn.execute('SELECT count(*) FROM knowledge_entries').fetchone()
        self.assertEqual(self.index(mid)['status'], 'duplicate')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_entries').fetchone(), before)
        self.conn.execute("UPDATE email_messages SET body='Titanio nuovo' WHERE id=%s", (mid,))
        self.index(mid)
        self.assertEqual(self.search('zirconio'), [])
        self.assertEqual(len(self.search('titanio')), 1)
        opened = self.opened('entry', old['entry_id'])
        self.assertIn('vecchio', opened['text'])
        self.assertFalse(opened['current'])
        self.assertEqual(opened['citation'], old['citation'])
        self.assertEqual(self.opened('message', mid)['text'], 'Titanio nuovo')

    def test_body_reversion_reactivates_correct_revision(self):
        mid = self.message(body='Zirconio prima')
        self.index(mid)
        self.conn.execute("UPDATE work_jobs SET status='done',finished_at=now() WHERE kind='knowledge'")
        self.conn.execute("UPDATE email_messages SET body='Titanio dopo' WHERE id=%s", (mid,))
        self.index(mid)
        self.conn.execute("UPDATE email_messages SET body='Zirconio prima' WHERE id=%s", (mid,))
        self.assertTrue(k.enqueue(self.conn, mid)['queued'])
        k.index_message({'message_id': mid})
        self.assertEqual(len(self.search('zirconio')), 1)
        self.assertEqual(self.search('titanio'), [])

    def test_same_attachment_identity_new_transcript_creates_new_revision(self):
        mid = self.message('telegram (account)')
        aid = self.attachment(mid, b'audio', filename='voce.ogg', mime='audio/ogg')
        first = self.index(mid)
        self.assertEqual(first['status'], 'partial')
        self.conn.execute("INSERT INTO media_transcripts VALUES (%s,'audio','Accordo smeraldo','fixture',now())", (aid,))
        second = self.index(mid)
        self.assertNotEqual(first['revision'], second['revision'])
        old = self.search('smeraldo')[0]
        self.conn.execute("UPDATE media_transcripts SET text='Accordo rubino' WHERE attachment_id=%s", (aid,))
        third = self.index(mid)
        self.assertNotEqual(second['revision'], third['revision'])
        self.assertEqual(self.search('smeraldo'), [])
        self.assertEqual(len(self.search('rubino')), 1)
        self.assertIn('smeraldo', self.opened('document', old['source_id'])['text'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_documents').fetchone(), (1,))

    def test_same_attachment_identity_new_bytea_changes_revision_and_content(self):
        mid = self.message()
        aid = self.attachment(mid, b'<a>Zirconio</a>')
        first = self.index(mid)
        self.conn.execute("UPDATE attachments SET data=%s WHERE id=%s", (b'<a>Titanio</a>', aid))
        second = self.index(mid)
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual(self.search('zirconio'), [])
        self.assertEqual(len(self.search('titanio')), 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_documents').fetchone(), (2,))

    def test_path_repair_changes_enqueue_key_without_moving_original(self):
        mid = self.message()
        path = self.data / 'uploads' / 'missing.xml'
        self.attachment(mid, None, path=str(path))
        first = self.index(mid)
        self.assertEqual(first['status'], 'partial')
        path.write_bytes(b'<a>Preventivo smeraldo</a>')
        second = self.index(mid)
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual(len(self.search('smeraldo')), 1)
        self.assertTrue(path.is_file())

    def test_failed_empty_blocked_and_unsupported_states_are_explicit(self):
        mid = self.message()
        self.attachment(mid, b'<invalid')
        self.attachment(mid, b'office', filename='file.docx')
        self.attachment(mid, None, path='/etc/passwd')
        self.attachment(mid, b'<a/>', filename='empty.xml')
        self.assertEqual(self.index(mid)['status'], 'partial')
        opened = self.opened('message', mid)
        statuses = {d['status'] for d in opened['documents']}
        self.assertEqual(statuses, {'failed', 'unsupported', 'blocked', 'empty'})
        self.assertIn('extraction_failed', str(opened))
        self.assertNotIn('/etc/passwd', str(opened))
        self.assertEqual(self.documents(), 4)

    def test_database_url_guard_rejects_implicit_or_non_test_database(self):
        for url in ('dbname=production', 'host=/tmp', 'service=deployment dbname=example_test'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validated_test_params(url)

    def test_same_revision_partial_failure_retries_extraction_without_duplicate_occurrence(self):
        mid = self.message()
        self.attachment(mid)
        with patch.object(k.documents, 'extract', side_effect=RuntimeError('sensitive token')):
            self.assertEqual(self.index(mid)['status'], 'partial')
        self.assertEqual(self.index(mid)['status'], 'indexed')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_occurrences').fetchone(), (1,))
        self.assertEqual(self.conn.execute('SELECT status,attempts FROM knowledge_extractions').fetchone(), ('ready', 2))

    def test_backfill_is_idempotent_paginated_historical_archive_only(self):
        ids = [self.message(historical=True) for _ in range(3)]
        first = k.backfill(batch=2)
        self.assertEqual(first['after_id'], ids[1])
        self.assertTrue(first['has_more'])
        self.assertEqual(k.backfill(batch=2)['queued'], 0)
        second = k.backfill(batch=2, after_id=first['after_id'])
        self.assertFalse(second['has_more'])
        self.assertEqual(second['queued'], 1)
        for kind, payload in self.conn.execute('SELECT kind,payload FROM work_jobs'):
            self.assertEqual(kind, 'knowledge')
            self.assertTrue(payload['archive_only'])
            k.index_message(payload)
        self.assertTrue(all(h['historical'] for h in self.search('caldaia')))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM tasks').fetchone(), (0,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM message_analysis').fetchone(), (0,))

    def test_caller_rollback_includes_queue_and_publication_rolls_back_on_sql_failure(self):
        mid = self.message()
        with self.assertRaisesRegex(RuntimeError, 'abort'), self.conn.transaction():
            k.enqueue(self.conn, mid)
            raise RuntimeError('abort')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_jobs').fetchone(), (0,))
        self.attachment(mid)
        self.conn.execute('''CREATE FUNCTION fail_knowledge() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'fixture failure'; END $$;
            CREATE TRIGGER reject_knowledge BEFORE INSERT ON knowledge_entries
            FOR EACH ROW EXECUTE FUNCTION fail_knowledge()''')
        with self.assertRaises(db.psycopg.errors.RaiseException):
            self.index(mid)
        for table in ('knowledge_entries', 'knowledge_documents', 'knowledge_occurrences', 'knowledge_runs'):
            self.assertEqual(self.conn.execute('SELECT count(*) FROM ' + table).fetchone(), (0,))
        self.conn.execute('DROP TRIGGER reject_knowledge ON knowledge_entries')
        self.assertEqual(k.index_message({'message_id': mid})['status'], 'indexed')

    def test_stale_job_and_inflight_edit_do_not_publish_old_revision(self):
        mid = self.message(body='Zirconio')
        receipt = k.enqueue(self.conn, mid)
        self.conn.execute("UPDATE email_messages SET body='Titanio' WHERE id=%s", (mid,))
        self.assertEqual(k.index_message(receipt)['status'], 'stale')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_entries').fetchone(), (0,))
        self.attachment(mid)
        extract = k.documents.extract
        def edit(data, filename):
            self.conn.execute("UPDATE email_messages SET body='Rubino' WHERE id=%s", (mid,))
            return extract(data, filename)
        with patch.object(k.documents, 'extract', side_effect=edit):
            self.assertEqual(k.index_message({'message_id': mid})['status'], 'stale')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_entries').fetchone(), (0,))
        self.assertEqual(k.index_message({'message_id': mid})['status'], 'indexed')
        self.assertEqual(len(self.search('rubino')), 1)

    def test_optional_transcript_table_missing_attachment_caps_and_context_budget(self):
        self.conn.execute('DROP TABLE media_transcripts')
        mid = self.message(body='smeraldo ' * 4000)
        for i in range(k.MAX_ATTACHMENTS + 2):
            self.attachment(mid, b'<a>smeraldo</a>', filename=f'file{i}.xml')
        result = self.index(mid)
        self.assertEqual(result['omitted_attachments'], 2)
        self.assertEqual(result['status'], 'partial')
        self.assertLessEqual(len(self.opened('message', mid)['text']), k.MAX_TEXT)
        self.assertEqual(len(self.search('smeraldo', limit=1)), 1)
        self.assertEqual(self.documents(), k.MAX_ATTACHMENTS)

    def test_extractor_version_and_removed_attachment_are_reindexed(self):
        mid = self.message()
        aid = self.attachment(mid, b'<a>smeraldo</a>')
        first = self.index(mid)
        with patch.object(k, 'EXTRACTOR_VERSION', 'documents_fixture_v2'):
            second = self.index(mid)
            self.assertNotEqual(first['revision'], second['revision'])
            self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_extractions').fetchone(), (2,))
            self.conn.execute('DELETE FROM attachments WHERE id=%s', (aid,))
            self.index(mid)
        self.assertEqual(self.search('smeraldo'), [])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM knowledge_occurrences').fetchone(), (2,))


if __name__ == '__main__':
    unittest.main()