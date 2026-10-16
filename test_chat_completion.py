"""Offline regressions: truncated owner replies must never execute events."""
import json
import unittest
from unittest.mock import MagicMock, patch

import brain
import ai_budget
from connectors import gcal


def completion(content, finish_reason='stop'):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        'choices': [{'finish_reason': finish_reason,
                     'message': {'content': content}}]
    }).encode()
    return response


class ChatCompletionTests(unittest.TestCase):
    def test_analysis_retries_without_accepting_partial_json(self):
        with patch('brain.urllib.request.urlopen', side_effect=[
                completion('{"appointments": [', 'length'),
                completion('{"appointments": []}')]) as request:
            self.assertEqual(brain._chat('Regole', 'Email'), '{"appointments": []}')
        budgets = [json.loads(call.args[0].data)['max_tokens']
                   for call in request.call_args_list]
        self.assertEqual(budgets, [8192, 16384])

    def test_analysis_rejects_repeated_truncation(self):
        with patch('brain.urllib.request.urlopen', side_effect=[
                completion('{}', 'length'), completion('{}', 'length')]):
            with self.assertRaises(ai_budget.TruncatedCompletion):
                brain._chat('Regole', 'Email')

    def test_reasoning_is_never_used_as_final_content(self):
        response = completion(None)
        response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': None, 'reasoning': 'Testo interno'}}]
        }).encode()
        with patch('brain.urllib.request.urlopen', return_value=response):
            with self.assertRaises(RuntimeError):
                brain._chat('Regole', 'Email')

    def test_native_truncation_and_retry_ceiling(self):
        response = completion('parziale')
        response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'native_finish_reason': 'MAX_TOKENS',
                         'message': {'content': 'parziale'}}]
        }).encode()
        payload = dict(self.payload, max_tokens=40000)
        with patch('brain.urllib.request.urlopen', side_effect=[
                response, completion('Completa')]) as request:
            self.assertEqual(ai_budget.request_content(payload), 'Completa')
        self.assertEqual(json.loads(request.call_args.args[0].data)['max_tokens'], 65536)

    def setUp(self):
        self.payload = {
            'model': 'test-model', 'max_tokens': 4000,
            'messages': [{'role': 'user', 'content': 'Creami tutta l’agenda'}],
            'reasoning': {'effort': 'low'},
        }

    def test_truncated_year_is_discarded_and_full_agenda_retried(self):
        partial = ('Ecco l’agenda\n@@EVENTO@@ Rientro Riccardo Torchio '
                   '- aspettalo al bar | 2026')
        complete = ('@@EVENTO@@ Rientro Riccardo | 2026-10-16 09:15 | | | 15 | 0\n'
                    '@@EVENTO@@ Cena | 2026-10-16 19:30 | | | 60 | 30\n'
                    '@@EVENTO@@ Cinema | 2026-10-16 20:30 | | | 120 | 30')
        with patch('brain.urllib.request.urlopen', side_effect=[
                completion(partial, 'length'), completion(complete)]) as request:
            reply = brain._owner_chat_request(self.payload)
        self.assertEqual(reply, complete)
        _, events = gcal.parse_markers(reply)
        self.assertEqual(len(events), 3)
        self.assertTrue(all('error' not in event for event in events))
        requests = [json.loads(call.args[0].data) for call in request.call_args_list]
        self.assertEqual([p['max_tokens'] for p in requests], [4000, 8000])
        self.assertEqual(requests[0]['messages'], requests[1]['messages'])
        self.assertEqual(self.payload['max_tokens'], 4000)

    def test_repeated_truncation_discards_even_complete_markers(self):
        partial = ('@@EVENTO@@ Cena | 2026-10-16 19:30\n'
                   '@@EVENTO@@ Cinema | 2026')
        with patch('brain.urllib.request.urlopen', side_effect=[
                completion(partial, 'length'), completion(partial, 'length')]) as request:
            reply = brain._owner_chat_request(self.payload)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(gcal.parse_markers(reply)[1], [])
        self.assertIn('non ho creato alcun appuntamento', reply)

    def test_reasoning_exhaustion_with_empty_content_also_retries(self):
        with patch('brain.urllib.request.urlopen', side_effect=[
                completion(None, 'length'), completion('Risposta completa')]) as request:
            self.assertEqual(brain._owner_chat_request(self.payload), 'Risposta completa')
        self.assertEqual(request.call_count, 2)

    def test_complete_reply_does_not_retry(self):
        with patch('brain.urllib.request.urlopen', return_value=completion(' Ciao ')) as request:
            self.assertEqual(brain._owner_chat_request(self.payload), 'Ciao')
        request.assert_called_once()

    def test_retry_failure_never_returns_partial_events(self):
        with patch('brain.urllib.request.urlopen', side_effect=[
                completion('@@EVENTO@@ Cena | 2026', 'length'), OSError('offline')]):
            reply = brain._owner_chat_request(self.payload)
        self.assertEqual(gcal.parse_markers(reply)[1], [])
        self.assertIn('temporaneamente non disponibile', reply)

    def test_truncated_vision_reply_does_not_trigger_text_fallback(self):
        with patch('brain.urllib.request.urlopen', side_effect=[
                completion('@@EVENTO@@ Cena | 2026', 'length'),
                completion('@@EVENTO@@ Cena | 2026', 'length')]) as request:
            reply = brain._owner_chat_request(self.payload, vision=True)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn('Non posso vedere', reply)
        self.assertEqual(gcal.parse_markers(reply)[1], [])


if __name__ == '__main__':
    unittest.main()