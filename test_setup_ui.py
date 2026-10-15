"""Offline presentation/help regressions; never authorize real accounts."""
import contextlib
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import setup_ui


class SetupUITests(unittest.TestCase):
    def render(self, tty, env):
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output), \
                patch.object(output, 'isatty', return_value=tty):
            setup_ui.section('Titolo', ('Primo passo', 'Secondo passo'))
        return output.getvalue()

    def test_plain_redirect_and_dumb_terminal(self):
        for tty, env in [(False, {'TERM': 'xterm'}), (True, {'TERM': 'dumb'}),
                         (True, {}), (True, {'TERM': 'xterm', 'NO_COLOR': ''}),
                         (True, {'TERM': 'xterm', 'NO_COLOR': '1'})]:
            with self.subTest(tty=tty, env=env):
                text = self.render(tty, env)
                self.assertNotIn('\033', text)
                self.assertIn('1. Primo passo', text)
                self.assertIn('2. Secondo passo', text)

    def test_tty_color_only_wraps_heading(self):
        text = self.render(True, {'TERM': 'xterm-256color'})
        self.assertIn('\033[1;36m', text)
        self.assertIn('\033[0m\n  1.', text)
        self.assertEqual(text.count('\033'), 2)

    def test_all_guides_are_print_only_and_have_steps(self):
        with patch('builtins.input', side_effect=AssertionError('Unexpected input')), \
                patch('getpass.getpass', side_effect=AssertionError('Unexpected secret input')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            for name, (_, steps) in setup_ui.GUIDES.items():
                self.assertGreaterEqual(len(steps), 2, name)
                setup_ui.guide(name)
        self.assertNotIn('\033', output.getvalue())

    def test_credential_and_consent_guidance(self):
        for name, required in {
            'database': ('postgresql.org', 'NON migra', 'localhost'),
            'identity': ('openrouter.ai/settings/keys', 'limite di spesa'),
            'gmail': ('apppasswords', 'due passaggi'),
            'telegram': ('t.me/BotFather', 'chat privata', '120'),
            'telegram_account': ('my.telegram.org', 'api_hash', 'consenso'),
            'whatsapp': ('faq.whatsapp.com', 'QR', 'revocarla'),
            'gcal': ('console.cloud.google.com', 'client_email', 'NON usare primary'),
            'microsoft': ('devicelogin', 'MFA', 'EMAILn_CLIENT_ID'),
            'dashboard': ('dashboard.json', 'primo avvio', 'tunnel SSH'),
        }.items():
            text = ' '.join(setup_ui.GUIDES[name][1])
            for term in required:
                self.assertIn(term, text, (name, term))

    def test_image_includes_shared_module(self):
        root = Path(__file__).resolve().parent
        self.assertIn('COPY setup_ui.py ', (root / 'Dockerfile').read_text())


if __name__ == '__main__':
    unittest.main()