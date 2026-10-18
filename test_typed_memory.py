"""Offline contracts for typed personal memory (topic replace-on-conflict).

No production database, network, workers or provider calls. The apply path runs
for real against a strict SQL double; model outputs are scripted JSON.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

with tempfile.TemporaryDirectory(prefix='typed-memory-') as _bootstrap:
    with patch.dict(os.environ, {'PI_SECRETARY_CONFIG_FILE': '/dev/null',
                                 'DATA_DIR': _bootstrap, 'DATABASE_URL': ''}):
        import brain
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
        self.enterContext(patch.object(brain.ai_budget, 'request_content',
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


class PrepareTopicTests(TypedMemoryTests):
    def prepare(self, raw):
        with patch.object(brain, '_chat', return_value=raw):
            return brain.prepare_user_memories('Ricorda che il caffè lo prendo amaro.', [])

    def test_prepare_passes_valid_topic_through(self):
        plan = self.prepare(json.dumps(dict(changes=[
            dict(operation='add', id=None, text='Il caffè amaro.', topic='caffe')])))
        self.assertEqual(plan['changes'][0]['topic'], 'caffe')

    def test_prepare_drops_invalid_or_oversized_topics(self):
        for bad in ('', '   ', 'x' * 121, 5, True, [], {}):
            with self.subTest(bad=bad):
                plan = self.prepare(json.dumps(dict(changes=[
                    dict(operation='add', id=None, text='Nota.', topic=bad)])))
                self.assertNotIn('topic', plan['changes'][0])

    def test_prepare_keeps_topic_optional(self):
        plan = self.prepare(json.dumps(dict(changes=[
            dict(operation='add', id=None, text='Fatto indipendente.')])))
        self.assertNotIn('topic', plan['changes'][0])


if __name__ == '__main__':
    unittest.main()
