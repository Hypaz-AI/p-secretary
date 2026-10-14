"""Offline emergency notification tests: AI, DB, Telegram and side effects mocked."""
import json
import unittest
from unittest.mock import patch

import brain
import config
import main


class UrgentNotificationTests(unittest.TestCase):
    def setUp(self):
        self.mocks = {}
        for name, kwargs in (
            ('brain._chat', {}),
            ('brain._rules_block', {'return_value': ''}),
            ('brain._entities_block', {'return_value': ''}),
            ('main.db.store_message', {'return_value': 42}),
            ('main.db.communication_seen', {'return_value': False}),
            ('main.db.log_communication', {'return_value': 7}),
            ('main.telegram.notify', {'return_value': True}),
            ('main.db.add_appointment', {}),
            ('main._sync_appointment_to_gcal', {'return_value': {'ok': True}}),
            ('main._save_invoice_attachments', {}),
            ('main.flows.enabled', {'return_value': False}),
            ('main.email_conn.mark_as_read', {}),
        ):
            p = patch(name, **kwargs)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        p = patch.object(config, 'NOTIFY_PRIORITY_THRESHOLD', 'mai')
        p.start()
        self.addCleanup(p.stop)

    def process(self, data):
        self.mocks['brain._chat'].return_value = json.dumps(data)
        return main.process_message('email', 'Marco', 'Oggetto', 'Testo', {}, source_id='id')

    def test_high_severity_overrides_mai_once_before_log(self):
        order = []
        self.mocks['main.telegram.notify'].side_effect = lambda text: order.append('notify')
        self.mocks['main.db.log_communication'].side_effect = lambda *a, **k: order.append('log')
        verdict = self.process({'severity': ' ALTA ', 'urgent': 'false',
                                'notification_reason': 'Compromissione account confermata'})
        self.assertEqual(verdict['severity'], 'alta')
        self.assertFalse(verdict['urgent'])
        self.assertEqual(order, ['notify', 'log'])
        self.assertTrue(self.mocks['main.db.log_communication'].call_args.kwargs['notified'])
        text = self.mocks['main.telegram.notify'].call_args.args[0]
        self.assertIn('<b>Gravità:</b> alta', text)
        self.assertIn('Compromissione account confermata', text)

    def test_urgent_overrides_mai(self):
        for urgent in (True, ' TRUE ', 'sì'):
            with self.subTest(urgent=urgent):
                self.mocks['main.telegram.notify'].reset_mock()
                self.process({'urgent': urgent, 'severity': 'media'})
                self.mocks['main.telegram.notify'].assert_called_once()
                self.assertIn('<b>Urgenza:</b> sì', self.mocks['main.telegram.notify'].call_args.args[0])

    def test_ordinary_high_priority_does_not_override_mai(self):
        self.process({'priority': 'alta', 'severity': 'media', 'urgent': 'false'})
        self.mocks['main.telegram.notify'].assert_not_called()
        self.assertFalse(self.mocks['main.db.log_communication'].call_args.kwargs['notified'])

    def test_ordinary_threshold_preserved(self):
        for threshold, priority, expected in (('alta', 'alta', True), ('alta', 'media', False),
                                               ('media', 'media', True), ('bassa', 'bassa', True)):
            with self.subTest(threshold=threshold, priority=priority):
                self.mocks['main.telegram.notify'].reset_mock()
                with patch.object(config, 'NOTIFY_PRIORITY_THRESHOLD', threshold):
                    self.process({'priority': priority})
                self.assertEqual(self.mocks['main.telegram.notify'].call_count, int(expected))

    def test_malformed_fields_do_not_trigger(self):
        for value in (None, [], 1, {}, 'false', '0', 'no', 'urgent', 'unknown'):
            with self.subTest(value=value):
                verdict = self.process({'urgent': value, 'severity': value, 'priority': value,
                                        'appointments': value, 'notification_reason': value})
                self.assertFalse(verdict['urgent'])
                self.assertEqual(verdict['severity'], 'bassa')
        self.mocks['main.telegram.notify'].assert_not_called()

    def test_invalid_json_and_ai_failure_do_not_trigger(self):
        for raw in ('not json', '{}', '[]', '{broken}', None):
            self.mocks['brain._chat'].return_value = raw
            main.process_message('email', 'Marco', '', 'Testo', {})
        self.mocks['brain._chat'].side_effect = RuntimeError('offline error')
        verdict = main.process_message('email', 'Marco', '', 'Testo', {})
        self.assertEqual(verdict['category'], 'errore')
        self.assertFalse(verdict['urgent'])
        self.assertEqual(verdict['severity'], 'bassa')
        self.mocks['main.telegram.notify'].assert_not_called()

    def test_error_category_cannot_override(self):
        self.process({'category': 'errore', 'severity': 'alta', 'urgent': True})
        self.mocks['main.telegram.notify'].assert_not_called()

    def test_all_dynamic_notification_fields_escaped(self):
        payload = '<i>untrusted & "quoted"</i>'
        verdict = brain.normalize_verdict(dict(priority='alta', severity='alta', urgent=True,
            notification_reason=payload, summary=payload, needs_action=True, action=payload,
            needs_reply=True, reply_hint=payload,
            appointments=[dict(title=payload, datetime=payload, location=payload)]))
        verdict['appointment_results'] = [{'ok': False, 'error': payload}]
        text = main.format_verdict(payload, payload, payload, verdict)
        self.assertNotIn(payload, text)
        self.assertEqual(text.count(main.telegram.escape_html(payload)), 11)
        self.assertIn('<b>Nuova comunicazione</b>', text)

    def test_duplicate_preserves_ingestion_without_analysis(self):
        self.mocks['main.db.communication_seen'].return_value = True
        self.assertEqual(main.process_message('email', 'Marco', '', 'Testo', {}, source_id='id'), {})
        self.mocks['main.db.store_message'].assert_called_once()
        self.mocks['brain._chat'].assert_not_called()
        self.mocks['main.telegram.notify'].assert_not_called()


if __name__ == '__main__':
    unittest.main()