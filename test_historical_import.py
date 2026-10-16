"""Offline runner/launcher contracts: no real config, database, sessions or Docker."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import historical_import as history
import installer


END = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.config = ModuleType('config')
        self.config.EMAIL_ACCOUNTS = [{'name': 'first'}, {'name': 'second'}]
        self.config.TELEGRAM_USER_ENABLED = True
        self.config.WHATSAPP_ENABLED = True
        self.config.TELEGRAM_BOT_TOKEN = 'offline-token'
        self.db = Mock(init=Mock(), store_message=Mock())
        self.flows = Mock(init=Mock(), settings=Mock(return_value={}))
        self.email = Mock(iter_history_messages=Mock(return_value=[]))
        self.telegram = Mock(import_history=AsyncMock(return_value=3))
        self.whatsapp = Mock(import_history=Mock(return_value=4))
        connectors = ModuleType('connectors')
        connectors.email = self.email
        connectors.telegram_account = self.telegram
        connectors.whatsapp = self.whatsapp
        self.pipeline = Mock()
        modules = {'config': self.config, 'db': self.db, 'flows': self.flows,
                   'connectors': connectors, 'connectors.email': self.email,
                   'connectors.telegram_account': self.telegram,
                   'connectors.whatsapp': self.whatsapp, 'pipeline': self.pipeline}
        replacement = patch.dict(sys.modules, modules)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_positive_days_inclusive_bounds(self):
        for value in ('1', '3650', 14):
            with self.subTest(value=value):
                self.assertEqual(history.positive_days(value), int(value))
        for value in ('0', '-1', '3651'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                history.positive_days(value)
        with self.assertRaises(ValueError):
            history.positive_days('not-a-number')

    def test_no_enabled_connections_initializes_only(self):
        self.assertEqual(history.run_import(14, now=END), [])
        self.db.init.assert_called_once_with()
        self.flows.init.assert_called_once_with()
        self.flows.settings.assert_called_once_with()
        self.email.iter_history_messages.assert_not_called()
        self.telegram.import_history.assert_not_called()
        self.whatsapp.import_history.assert_not_called()
        self.db.store_message.assert_not_called()
        self.assertIn('Nessuna connessione', self.output.getvalue())

    def test_connections_require_flow_and_configuration_flags(self):
        self.flows.settings.return_value = dict(email=False, telegram_user=True,
                                               whatsapp=True, telegram_bot=True, gcal=True)
        self.config.TELEGRAM_USER_ENABLED = False
        self.config.WHATSAPP_ENABLED = False
        self.config.TELEGRAM_BOT_TOKEN = ''
        self.assertEqual(history.run_import(1, now=END), [])
        self.email.iter_history_messages.assert_not_called()
        self.telegram.import_history.assert_not_called()
        self.whatsapp.import_history.assert_not_called()

    def test_email_stores_exact_native_kwargs_without_pipeline(self):
        self.flows.settings.return_value = {'email': True}
        message = dict(source_id='native-1', ts=END.isoformat(),
                       metadata={'direction': 'out'}, attachments=[{'name': 'a.pdf'}],
                       subject='subject', body='body', **{'from': 'sender'})
        self.email.iter_history_messages.side_effect = [[message], []]
        self.assertEqual(history.run_import(14, now=END), [
            {'source': 'Email 1', 'count': 1, 'status': 'ok'},
            {'source': 'Email 2', 'count': 0, 'status': 'ok'}])
        self.db.store_message.assert_called_once_with(
            'email (first)', 'sender', 'subject', 'body', ts=message['ts'],
            source_id='native-1', metadata=message['metadata'],
            attachments=message['attachments'], historical=True)
        self.assertIs(self.db.store_message.call_args.kwargs['attachments'], message['attachments'])
        self.assertEqual(self.pipeline.mock_calls, [])

    def test_source_failures_continue_and_redact_exception(self):
        self.flows.settings.return_value = dict(email=True, telegram_user=True, whatsapp=True)
        self.email.iter_history_messages.side_effect = [RuntimeError('private secret'), []]
        self.telegram.import_history.side_effect = OSError('private secret')
        results = history.run_import(2, now=END)
        self.assertEqual([r['status'] for r in results], ['failed', 'ok', 'failed', 'limitato'])
        self.assertIsNone(results[0]['count'])
        self.assertEqual(results[-1]['count'], 4)
        self.telegram.import_history.assert_awaited_once_with(END - timedelta(days=2), END)
        self.whatsapp.import_history.assert_called_once_with(END - timedelta(days=2), END)
        self.assertNotIn('private secret', self.output.getvalue())
        self.assertIn('RuntimeError', self.output.getvalue())

    def test_partial_email_failure_preserves_stored_messages_and_continues(self):
        self.flows.settings.return_value = {'email': True}
        def partial():
            yield dict(subject='s', body='b', ts='t', source_id='id',
                       metadata={}, attachments=[], **{'from': 'sender'})
            raise RuntimeError('offline')
        self.email.iter_history_messages.side_effect = [partial(), []]
        results = history.run_import(1, now=END)
        self.assertEqual([r['status'] for r in results], ['failed', 'ok'])
        self.db.store_message.assert_called_once()

    def test_frozen_rolling_window_shared_by_all_sources(self):
        self.flows.settings.return_value = dict(email=True, telegram_user=True,
                                               whatsapp=True, telegram_bot=True)
        with patch.object(history, 'datetime') as clock:
            clock.now.side_effect = [END, END + timedelta(hours=1)]
            results = history.run_import(14)
        clock.now.assert_called_once_with(timezone.utc)
        start = END - timedelta(days=14)
        self.assertEqual(self.email.iter_history_messages.call_args_list, [
            call(account, start, END) for account in self.config.EMAIL_ACCOUNTS])
        self.telegram.import_history.assert_awaited_once_with(start, END)
        self.whatsapp.import_history.assert_called_once_with(start, END)
        self.assertEqual(results[-1], {'source': 'Telegram bot', 'count': 0, 'status': 'unsupported'})

    def test_timezone_offset_window_and_explicit_now_avoid_clock(self):
        end = END.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.flows.settings.return_value = {'whatsapp': True}
        with patch.object(history, 'datetime') as clock:
            history.run_import(1, now=end)
        clock.now.assert_not_called()
        self.whatsapp.import_history.assert_called_once_with(END - timedelta(days=1), end)

    def test_invalid_window_rejected_before_initialization(self):
        for days, end, error in ((0, END, argparse.ArgumentTypeError),
                                 (3651, END, argparse.ArgumentTypeError),
                                 (1, END.replace(tzinfo=None), ValueError)):
            with self.subTest(days=days, end=end), self.assertRaises(error):
                history.run_import(days, now=end)
        self.db.init.assert_not_called()
        self.flows.init.assert_not_called()


class MainTests(unittest.TestCase):
    def test_exit_statuses_and_failure_precedence(self):
        for statuses, expected in (([], 0), (['ok'], 0), (['limited'], 2),
                                   (['limitato'], 2), (['unsupported'], 2),
                                   (['failed'], 1), (['limitato', 'failed'], 1)):
            with self.subTest(statuses=statuses), patch.object(
                    history, 'run_import', return_value=[{'status': s} for s in statuses]) as run:
                self.assertEqual(history.main(['--days', '3650']), expected)
                run.assert_called_once_with(3650)

    def test_interactive_default_and_explicit_days(self):
        for answer, expected in (('', 14), (' 7 ', 7)):
            with self.subTest(answer=answer), patch('builtins.input', return_value=answer), \
                    patch.object(history, 'run_import', return_value=[]) as run:
                self.assertEqual(history.main([]), 0)
                run.assert_called_once_with(expected)
        with patch('builtins.input') as prompt, patch.object(history, 'run_import', return_value=[]):
            self.assertEqual(history.main(['--days', '1']), 0)
            prompt.assert_not_called()

    def test_interactive_invalid_input_and_cancellation(self):
        for answer in ('0', '3651', 'invalid'):
            with self.subTest(answer=answer), patch('builtins.input', return_value=answer), \
                    patch.object(history, 'run_import') as run, redirect_stdout(io.StringIO()):
                self.assertEqual(history.main([]), 1)
                run.assert_not_called()
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error), patch('builtins.input', side_effect=error), \
                    patch.object(history, 'run_import') as run, redirect_stdout(io.StringIO()):
                self.assertEqual(history.main([]), 130)
                run.assert_not_called()

    def test_runner_exception_and_cancellation(self):
        for error, expected in ((RuntimeError('private secret'), 1), (KeyboardInterrupt(), 130), (EOFError(), 130)):
            output = io.StringIO()
            with self.subTest(error=type(error)), patch.object(history, 'run_import', side_effect=error), \
                    redirect_stdout(output):
                self.assertEqual(history.main(['--days', '14']), expected)
            self.assertNotIn('private secret', output.getvalue())

    def test_cli_invalid_days_exit_two_without_import(self):
        for value in ('0', '-1', '3651', 'invalid'):
            with self.subTest(value=value), patch.object(history, 'run_import') as run, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                history.main(['--days', value])
            self.assertEqual(exc.exception.code, 2)
            run.assert_not_called()


class InstallerHistoryTests(unittest.TestCase):
    def setUp(self):
        mask = patch.object(installer.os, 'umask')
        mask.start()
        self.addCleanup(mask.stop)

    def test_parser_days_bounds_and_command_restriction(self):
        for arguments in (['import-history', '--days', '0'], ['import-history', '--days', '3651'],
                          ['status', '--days', '14'], ['import-history', '--days', 'invalid']):
            with self.subTest(arguments=arguments), patch.object(sys, 'argv', ['installer.py'] + arguments), \
                    patch.object(installer, 'launch') as launch, redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as exc:
                installer.main()
            self.assertEqual(exc.exception.code, 2)
            launch.assert_not_called()
        for days in (None, 1, 3650):
            arguments = ['installer.py', 'import-history'] + ([] if days is None else ['--days', str(days)])
            with self.subTest(days=days), patch.object(sys, 'argv', arguments), \
                    patch.object(installer, 'launch', return_value=2) as launch, \
                    patch.object(installer.os, 'umask'):
                self.assertEqual(installer.main(), 2)
                self.assertEqual(vars(launch.call_args.args[0]), {'command': 'import-history', 'days': days})

    def test_launcher_stops_repairs_builds_then_imports_and_propagates_code(self):
        for days, code in ((None, 0), (14, 2), (3650, 1), (1, 130)):
            with self.subTest(days=days, code=code), tempfile.TemporaryDirectory() as temp, \
                    patch.dict(os.environ, {'PI_SECRETARY_HOME': temp}), \
                    patch.object(installer, 'progress', return_value={'complete': True, 'database': 'local'}), \
                    patch.object(installer, 'read_env', return_value={}), \
                    patch.object(installer.subprocess, 'run', return_value=Mock(returncode=code)) as run, \
                    patch('connectors.whatsapp_profile.repair_docker_singletons', return_value=False) as repair, \
                    redirect_stdout(io.StringIO()):
                events = Mock()
                events.attach_mock(run, 'run')
                events.attach_mock(repair, 'repair')
                self.assertEqual(installer.launch(argparse.Namespace(command='import-history', days=days)), code)
                base = ['docker', 'compose', '--project-directory', str(installer.ROOT),
                        '-f', str(installer.ROOT / 'compose.yaml')]
                command = ['run', '--rm', '--no-deps', 'app', 'python', 'historical_import.py']
                if days is not None:
                    command += ['--days', str(days)]
                self.assertEqual(events.mock_calls[2:], [
                    call.run(base + ['stop', 'app'], check=True),
                    call.repair(Path(temp) / 'data/wa_session'),
                    call.run(base + ['build', 'app'], check=True),
                    call.run(base + ['--profile', 'local', 'up', '-d', '--wait', '--wait-timeout', '120', 'db'], check=True),
                    call.run(base + command, check=False)])

    def test_launcher_requires_completed_setup_before_mutating_services(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PI_SECRETARY_HOME': temp}), \
                patch.object(installer, 'progress', return_value={}), \
                patch.object(installer.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'setup'):
                installer.launch(argparse.Namespace(command='import-history', days=14))
            self.assertEqual([c.args[0] for c in run.call_args_list],
                             [['docker', 'compose', 'version'], ['docker', 'info']])


if __name__ == '__main__':
    unittest.main()