"""Offline tests; subprocesses never load the installed .env."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO_ROOT


class ConfigPortabilityTests(unittest.TestCase):
    def run_code(self, code, extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'PATH': os.environ.get('PATH', ''), 'P_SECRETARY_CONFIG_FILE': tmp + '/config.env',
                   'DATA_DIR': tmp + '/data', 'HOME': tmp}
            env.update(extra or {})
            result = subprocess.run([sys.executable, '-c', code], cwd=REPO_ROOT,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_roundtrip_and_permissions(self):
        self.run_code(r'''
from secretary.core import config
import os, stat
value = r'C:\new\folder\1' + ' "quoted" # ='
config.set_env('EXAMPLE', value)
del os.environ['EXAMPLE']
config._load_dotenv(config.CONFIG_FILE)
assert os.environ['EXAMPLE'] == value
assert stat.S_IMODE(config.CONFIG_FILE.stat().st_mode) == 0o600
for bad in ['a\nb', 'a\rb', 'a\x00b']:
    try: config.set_env('EXAMPLE', bad)
    except ValueError: pass
    else: raise AssertionError('multiline accepted')
assert config.INVOICE_DIR == config.DATA_DIR / 'invoices'
assert config.TELEGRAM_USER_SESSION == str(config.DATA_DIR / 'telegram_account/personal.session')
''')

    def test_explicit_paths_preserved(self):
        self.run_code("from secretary.core import config; assert str(config.INVOICE_DIR) == '/custom/invoices'; assert config.TELEGRAM_USER_SESSION == 'legacy/personal.session'",
                      {'INVOICE_DIR': '/custom/invoices', 'TELEGRAM_USER_SESSION': 'legacy/personal.session'})

    def test_concurrent_updates(self):
        self.run_code('''
from secretary.core import config
import subprocess, sys, os
workers = [subprocess.Popen([sys.executable, '-c', "from secretary.core import config; config.set_env('KEY%d', 'value')" % n]) for n in range(8)]
assert all(p.wait() == 0 for p in workers)
config._load_dotenv(config.CONFIG_FILE)
assert all(os.environ.get('KEY%d' % n) == 'value' for n in range(8))
''')

    def test_lock_exclusion_and_atomic_failure(self):
        self.run_code('''
from secretary.core import config
from secretary.connectors.storage import FileLock, atomic_write
from unittest.mock import patch
path = config.DATA_DIR / 'private'
atomic_write(path, 'old')
with patch('secretary.connectors.storage.os.replace', side_effect=OSError):
    try: atomic_write(path, 'new')
    except OSError: pass
assert path.read_text() == 'old'
with FileLock(str(path) + '.lock'):
    try:
        with FileLock(str(path) + '.lock', blocking=False): pass
    except OSError: pass
    else: raise AssertionError('lock not exclusive')
''')