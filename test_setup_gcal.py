"""Offline setup security tests: temporary files, no Google requests."""
import contextlib
import getpass
import io
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import setup_gcal as setup


class CalendarSetupTests(unittest.TestCase):
    def test_file_import_removed_and_saved_key_reused(self):
        saved = {'client_email': 'saved@example.invalid'}
        with patch.object(setup.gcal, '_load_service_account', return_value=saved), \
             patch('builtins.input', return_value='n'), patch.object(setup, 'hidden_json') as hidden:
            self.assertEqual(setup.supply_service_account(), saved)
            hidden.assert_not_called()
        with patch.object(setup.gcal, '_load_service_account', return_value={}), \
             patch('builtins.input', return_value='f'), patch.object(setup, 'hidden_json') as hidden:
            self.assertIsNone(setup.supply_service_account())
            hidden.assert_not_called()

    def test_hidden_compact_and_multiline(self):
        for lines in (['{"private":"SECRET"}', 'FINE'], ['{', '"private":"SECRET"', '}', 'FINE']):
            output = io.StringIO()
            with patch('getpass.getpass', side_effect=lines), contextlib.redirect_stdout(output):
                self.assertEqual(json.loads(setup.hidden_json()), {'private': 'SECRET'})
            self.assertNotIn('SECRET', output.getvalue())

    def test_no_echo_fallback(self):
        def fallback(*args):
            warnings.warn('no tty', getpass.GetPassWarning)
            self.fail('must not read echoed input')
        with patch('getpass.getpass', side_effect=fallback):
            with self.assertRaises(getpass.GetPassWarning):
                setup.hidden_json()

    def test_secure_import_and_overwrite_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'nested/key.json'
            source = Path(tmp) / 'source.json'
            source.write_text('{"private_key":"SECRET"}')
            with patch.object(setup.config, 'DATA_DIR', target.parent), patch.object(setup.config, 'set_env'), \
                 patch.object(setup.gcal, 'service_account_file', return_value=target), \
                 patch.object(setup.gcal, '_load_service_account', return_value={}), \
                 patch.object(setup.gcal, 'validate_service_account', side_effect=lambda data: data):
                target = target.parent / 'gcal_service_account.json'
                with patch('builtins.input', side_effect=['i']), patch.object(setup, 'hidden_json', return_value=source.read_text()):
                    setup.supply_service_account()
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                before = target.read_bytes()
                with patch('builtins.input', side_effect=['i', 'n']), \
                     patch.object(setup, 'hidden_json', return_value='{"private_key":"NEW"}'):
                    self.assertIsNone(setup.supply_service_account())
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(list(target.parent.glob('.key.json.*')), [])

    def test_invalid_json_and_cancel_never_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'key.json'
            with patch.object(setup.gcal, 'service_account_file', return_value=target), \
                 patch.object(setup.gcal, '_load_service_account', return_value={}):
                with patch('builtins.input', return_value='i'), \
                     patch.object(setup, 'hidden_json', return_value='{"private_key":"SECRET"}'):
                    with self.assertRaises(ValueError) as error:
                        setup.supply_service_account()
                    self.assertNotIn('SECRET', str(error.exception))
                with patch('builtins.input', return_value=''):
                    self.assertIsNone(setup.supply_service_account())
                self.assertFalse(target.exists())

    def test_primary_rejected_without_network(self):
        with patch.object(setup, 'supply_service_account', return_value={'client_email': 'test@example.com'}), \
             patch('builtins.input', return_value='primary'), patch.object(setup.gcal, '_api') as api:
            self.assertEqual(setup._setup(), 1)
            api.assert_not_called()

    def test_errors_never_show_secrets(self):
        output = io.StringIO()
        with patch.object(setup, '_setup', side_effect=ValueError('SECRET')), contextlib.redirect_stdout(output):
            self.assertEqual(setup.main(), 1)
        self.assertNotIn('SECRET', output.getvalue())

    def test_event_decline_does_not_enable(self):
        with patch.object(setup, 'supply_service_account', return_value={'client_email': 'test@example.com'}), \
             patch('builtins.input', side_effect=['shared@example.com', 's', 'n']), \
             patch.object(setup.gcal, '_api', return_value={}), \
             patch.object(setup.config, 'set_env') as save, \
             patch.object(setup.gcal, 'create_event') as create:
            self.assertEqual(setup._setup(), 2)
            create.assert_not_called()
            self.assertNotIn(unittest.mock.call('GCAL_ENABLED', 'true'), save.call_args_list)