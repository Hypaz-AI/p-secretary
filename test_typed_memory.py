"""Contratti offline della memoria personale tipizzata (un solo ricordo vivo per argomento).

Nessun database di produzione, rete, worker o provider. Il percorso di scrittura gira
davvero contro un doppio SQL rigoroso.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

with tempfile.TemporaryDirectory(prefix='typed-memory-') as _bootstrap:
    with patch.dict(os.environ, {'PI_SECRETARY_CONFIG_FILE': '/dev/null',
                                 'DATA_DIR': _bootstrap, 'DATABASE_URL': ''}):
        import db
        import jobs


class TypedMemoryTests(unittest.TestCase):
    """Strict SQL double over the notes table; apply runs for real."""

    def setUp(self):
        self.notes = {}
        self.next_id = [1]
        self.conn = MagicMock(name='connection')
        self.conn.execute.side_effect = self.execute
        self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value = self.conn
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('ai_budget.request_content',
                                side_effect=AssertionError('Provider forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))

    def execute(self, sql, params=None):
        params = tuple(params or ())
        if sql.startswith('SELECT pg_advisory_xact_lock'):
            return SimpleNamespace(fetchone=lambda: None)
        if sql.startswith('SELECT text FROM notes WHERE id=%s FOR UPDATE'):
            note = self.notes.get(params[0])
            return SimpleNamespace(fetchone=lambda: (note['text'],) if note else None)
        if 'UPDATE notes SET text=%s,version=version+1' in sql and 'WHERE id=(SELECT' in sql:
            # typed replace-on-conflict: subselect on lower(topic), AND text<>%s
            topic = params[4]
            text = params[0]
            for note in self.notes.values():
                if note.get('topic') is not None and note['topic'].lower() == topic:
                    if note['text'] != text:
                        note['text'], note['version'] = text, note['version'] + 1
                        return SimpleNamespace(fetchone=lambda: (note['id'],))
                    # AND text<>%s: identical text updates nothing.
                    return SimpleNamespace(fetchone=lambda: None)
            return SimpleNamespace(fetchone=lambda: None)
        if 'UPDATE notes SET text=%s,version=version+1' in sql and 'WHERE id=' in sql:
            # update/delete by id (legacy path)
            note = self.notes.get(params[4])
            if note and note['text'] != params[0]:
                note['text'], note['version'] = params[0], note['version'] + 1
                return SimpleNamespace(rowcount=1)
            return SimpleNamespace(rowcount=0)
        if sql.startswith('INSERT INTO notes(text,kind,source_message,source_revision,topic)'):
            text, kind, source_message, source_revision, topic = params[:5]
            if any(n['text'].strip().lower() == text.strip().lower() for n in self.notes.values()):
                return SimpleNamespace(fetchone=lambda: None)
            identity = self.next_id[0]
            self.next_id[0] += 1
            self.notes[identity] = dict(id=identity, text=text, kind=kind, version=1, topic=topic)
            return SimpleNamespace(fetchone=lambda: (identity,))
        if sql.startswith('INSERT INTO notes(text,kind,source_message,source_revision)'):
            text, kind = params[0], params[1]
            if any(n['text'].strip().lower() == text.strip().lower() for n in self.notes.values()):
                return SimpleNamespace(fetchone=lambda: None)
            identity = self.next_id[0]
            self.next_id[0] += 1
            self.notes[identity] = dict(id=identity, text=text, kind=kind, version=1, topic=None)
            return SimpleNamespace(fetchone=lambda: (identity,))
        if sql.startswith('DELETE FROM notes WHERE id=%s'):
            self.notes.pop(params[0], None)
            return SimpleNamespace(rowcount=1)
        raise AssertionError('Unexpected SQL: ' + sql)

    def add(self, text, topic=None, identity=None):
        note = dict(id=identity or self.next_id[0], text=text, kind='personal', version=1, topic=topic)
        if identity is None:
            self.next_id[0] += 1
        self.notes[note['id']] = note
        return note


class TopicNormalizationTests(TypedMemoryTests):
    def test_normalize_topic_lowercases_and_collapses_whitespace(self):
        self.assertEqual(db.normalize_topic('  Caffè   Latte  '), 'caffè latte')
        self.assertIsNone(db.normalize_topic(None))

    def test_invalid_topics_fail_typed(self):
        for bad in ('', '   ', 'x' * 121, 5, True, [], {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                db.normalize_topic(bad)


class ReplaceOnConflictTests(TypedMemoryTests):
    def test_new_typed_note_replaces_existing_topic_not_duplicates(self):
        self.add('Il caffè lo prendo zuccherato.', topic='caffe')
        changed = db.apply_personal_memories(
            [dict(operation='add', id=None, text='Il caffè lo prendo amaro.', topic='caffe')],
            set(), conn=self.conn, source_message=101)
        self.assertEqual(changed, 1)
        self.assertEqual(len(self.notes), 1)
        note = next(iter(self.notes.values()))
        self.assertEqual(note['text'], 'Il caffè lo prendo amaro.')
        self.assertEqual(note['version'], 2)

    def test_same_topic_same_text_is_idempotent(self):
        self.add('Il caffè lo prendo amaro.', topic='caffe')
        changed = db.apply_personal_memories(
            [dict(operation='add', id=None, text='Il caffè lo prendo amaro.', topic='caffe')],
            set(), conn=self.conn, source_message=101)
        self.assertEqual(changed, 0)
        self.assertEqual(next(iter(self.notes.values()))['version'], 1)

    def test_untyped_notes_stay_additive(self):
        self.add('Sono nato a Roma.')
        self.add('Ho visitato Parigi nel 2019.')
        changed = db.apply_personal_memories(
            [dict(operation='add', id=None, text='Ho visitato Lisbona nel 2021.')],
            set(), conn=self.conn, source_message=101)
        self.assertEqual(changed, 1)
        self.assertEqual(len(self.notes), 3)

    def test_different_topics_do_not_replace_each_other(self):
        self.add('Il caffè lo prendo amaro.', topic='caffe')
        changed = db.apply_personal_memories(
            [dict(operation='add', id=None, text='Il tè lo prendo verde.', topic='te')],
            set(), conn=self.conn, source_message=101)
        self.assertEqual(changed, 1)
        self.assertEqual(len(self.notes), 2)

    def test_topic_matching_is_case_insensitive(self):
        self.add('Il caffè lo prendo amaro.', topic='Caffe')
        changed = db.apply_personal_memories(
            [dict(operation='add', id=None, text='Il caffè lo prendo con latte.', topic='CAFFE')],
            set(), conn=self.conn, source_message=101)
        self.assertEqual(changed, 1)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(next(iter(self.notes.values()))['text'], 'Il caffè lo prendo con latte.')

    def test_invalid_topic_in_change_fails_apply_not_silently_ignores(self):
        with self.assertRaises(ValueError):
            db.apply_personal_memories(
                [dict(operation='add', id=None, text='Nota.', topic='x' * 121)],
                set(), conn=self.conn, source_message=101)


class MemoryApplicationTests(unittest.TestCase):
    """CAS transazionale di apply_personal_memories (usata dallo strumento note_save/note_forget)."""

    def connection(self, rows=None):
        conn = MagicMock()
        rows = rows or {}

        def execute(query, params=None):
            cursor = MagicMock(rowcount=1)
            if query.startswith('SELECT text'):
                cursor.fetchone.return_value = rows.get(params[0])
            elif query.startswith('INSERT'):
                cursor.fetchone.return_value = (99,)
            return cursor

        conn.execute.side_effect = execute
        return conn

    def test_shared_connection_is_not_opened_committed_or_closed(self):
        conn = self.connection({7: ('Tè',), 8: ('Obsoleto',)})
        changes = [dict(operation='add', text='Cammina'),
                   dict(operation='update', id=7, text='Caffè'),
                   dict(operation='delete', id=8)]
        expected = [dict(id=7, text='Tè'), dict(id=8, text='Obsoleto')]
        with patch('db._conn') as connect:
            count = db.apply_personal_memories(changes, {7, 8}, conn=conn, expected=expected)
        self.assertEqual(count, 3)
        connect.assert_not_called()
        for method in (conn.transaction, conn.commit, conn.rollback, conn.close,
                       conn.__enter__, conn.__exit__):
            method.assert_not_called()
        queries = [call.args[0] for call in conn.execute.call_args_list]
        self.assertIn('pg_advisory_xact_lock', queries[0])
        self.assertTrue(all('FOR UPDATE' in query for query in queries[1:3]))
        self.assertTrue(queries[3].startswith('INSERT'))

    def test_changed_missing_or_unsnapshotted_target_fails_before_any_write(self):
        for operation in ('update', 'delete'):
            for row, expected in [(('Changed',), {7: 'Tè'}), (None, {7: 'Tè'}),
                                  (('Tè',), {})]:
                with self.subTest(operation=operation, row=row, expected=expected):
                    conn = self.connection({7: row})
                    changes = [dict(operation='add', text='Non scrivere'),
                               dict(operation=operation, id=7, text='Caffè')]
                    with self.assertRaises(ValueError):
                        db.apply_personal_memories(changes, {7}, conn=conn, expected=expected)
                    queries = [call.args[0] for call in conn.execute.call_args_list]
                    self.assertFalse(any(q.startswith(('INSERT', 'UPDATE', 'DELETE'))
                                         for q in queries))

    def test_cas_validates_original_snapshot_once_for_repeated_target(self):
        conn = self.connection({7: ('Tè',)})
        count = db.apply_personal_memories([
            dict(operation='update', id=7, text='Caffè'),
            dict(operation='delete', id=7)], {7}, conn=conn, expected={7: 'Tè'})
        self.assertEqual(count, 2)
        queries = [call.args[0] for call in conn.execute.call_args_list]
        self.assertEqual(sum(q.startswith('SELECT text') for q in queries), 1)

    def test_legacy_owns_connection_and_transaction_without_recursion(self):
        conn = self.connection()
        with patch('db._conn') as connect:
            connect.return_value.__enter__.return_value = conn
            count = db.apply_personal_memories([dict(operation='add', text='Fatto')], set())
        self.assertEqual(count, 1)
        connect.assert_called_once_with()
        conn.transaction.assert_called_once_with()
        conn.transaction.return_value.__exit__.assert_called_once()
        self.assertEqual(conn.transaction.return_value.__exit__.call_args.args[-3:],
                 (None, None, None))
        connect.return_value.__exit__.assert_called_once()
        self.assertEqual(connect.return_value.__exit__.call_args.args[-3:], (None, None, None))

    def test_owned_transaction_rolls_back_on_stale_snapshot(self):
        conn = self.connection({7: ('Changed',)})
        with patch('db._conn') as connect:
            connect.return_value.__enter__.return_value = conn
            with self.assertRaises(ValueError):
                db.apply_personal_memories([dict(operation='delete', id=7)], {7},
                                           expected={7: 'Tè'})
            self.assertIs(conn.transaction.return_value.__exit__.call_args.args[-3], ValueError)

    def test_legacy_noop_counts_and_unknown_id_are_preserved(self):
        conn = self.connection()
        conn.execute.side_effect = None
        conn.execute.return_value.fetchone.return_value = None
        conn.execute.return_value.rowcount = 0
        self.assertEqual(db.apply_personal_memories([
            dict(operation='add', text='Duplicato'),
            dict(operation='update', id=7, text='Identico'),
            dict(operation='delete', id=99)], {7}, conn=conn), 0)
        self.assertEqual(len(conn.execute.call_args_list), 3)


if __name__ == '__main__':
    unittest.main()
