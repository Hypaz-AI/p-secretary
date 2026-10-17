"""Memoria personale: test offline senza rete né database."""
import json
import unittest
from unittest.mock import MagicMock, patch

import brain


class PersonalMemoryTests(unittest.TestCase):
    def test_automatic_preference_saved(self):
        changes = [{'operation': 'add', 'text': 'L’utente preferisce il tè.'}]
        with patch('brain._chat', return_value=json.dumps({'changes': changes})) as model, patch(
            'db.apply_personal_memories', return_value=1
        ) as save:
            status = brain.remember_user('Preferisco il tè.', [])
        self.assertTrue(status.startswith('successo:'))
        self.assertIn('Preferisco il tè.', model.call_args.args[1])
        self.assertEqual(save.call_args.args[0][0]['text'], changes[0]['text'])

    def test_update_and_explicit_forget(self):
        for operation in ('update', 'delete'):
            with self.subTest(operation=operation), patch('brain._chat', return_value=json.dumps({
                'changes': [{'operation': operation, 'id': 7, 'text': 'Preferisce il caffè.'}]
            })), patch('db.apply_personal_memories', return_value=1) as save:
                brain.remember_user('Correggi oppure dimentica questa preferenza.',
                                    [{'id': 7, 'text': 'Preferisce il tè.'}])
                self.assertEqual(save.call_args.args[1], {7})

    def test_invalid_operations_cannot_write(self):
        invalid = [None, {'operation': 'drop'}, {'operation': 'delete', 'id': 9},
                   {'operation': 'update', 'id': True, 'text': 'x'},
                   {'operation': 'add', 'text': ''}, {'operation': 'add', 'text': 'x' * 1001}]
        with patch('brain._chat', return_value=json.dumps({'changes': invalid})), patch(
            'db.apply_personal_memories'
        ) as save:
            brain.remember_user('ciao', [{'id': 1, 'text': 'Fatto'}])
        save.assert_not_called()

    def test_database_failure_not_confirmed(self):
        with patch('brain._chat', return_value='{"changes":[{"operation":"add","text":"Fatto"}]}'), patch(
            'db.apply_personal_memories', side_effect=RuntimeError('offline')
        ):
            self.assertTrue(brain.remember_user('Fatto', []).startswith('errore:'))

    def test_authorized_old_memories_and_new_memory_visible_in_durable_projection(self):
        old = [{'id': n, 'text': f'Preferenza {n}'} for n in range(1, 16)]
        dialogue = {'memory_plan': {'known': old, 'changes': [
            {'operation': 'add', 'text': 'Preferisce il tè'}]}}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': 'Risposta'}}]
        }).encode()
        with patch('db.personal_memories') as read, patch(
            'brain.remember_user'
        ) as remember, patch('db._conn') as connect, patch(
            'brain.urllib.request.urlopen', return_value=response
        ) as request:
            brain.chat_with_user('Preferisco il tè', dialogue=dialogue)
        read.assert_not_called()
        remember.assert_not_called()
        connect.assert_not_called()
        messages = json.loads(request.call_args.args[0].data)['messages']
        self.assertIn('Preferenza 1', messages[1]['content'])
        self.assertIn('Preferisce il tè', messages[1]['content'])
        self.assertIn('ESITO MEMORIA: piano preparato, non ancora salvato', messages[0]['content'])
        self.assertNotIn('ESITO MEMORIA: successo:', messages[0]['content'])


if __name__ == '__main__':
    unittest.main()