"""Offline singleton recovery; never inspect real Docker or authentication data."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from secretary.setup import installer
from secretary.connectors.storage import FileLock
from secretary.connectors import whatsapp_profile as recovery


class ProfileRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'wa_session'
        self.profile = self.root / 'session'
        self.profile.mkdir(parents=True)
        (self.profile / 'credentials').write_bytes(b'preserve-session')
        (self.root / 'other').write_bytes(b'preserve-other')
        for name, target in [('SingletonLock', 'abcdef123456-504'),
                             ('SingletonSocket', '/tmp/absent-chrome/socket'),
                             ('SingletonCookie', 'cookie')]:
            (self.profile / name).symlink_to(target)

    def repair(self, retired=True):
        with patch.object(recovery, '_retired', return_value=retired):
            return recovery.repair_docker_singletons(self.root)

    def test_preserves_session_and_lock_inode(self):
        with FileLock(self.root / '.owner.lock'):
            inode = (self.root / '.owner.lock').stat().st_ino
        self.assertTrue(self.repair())
        self.assertEqual((self.profile / 'credentials').read_bytes(), b'preserve-session')
        self.assertEqual((self.root / 'other').read_bytes(), b'preserve-other')
        self.assertEqual((self.root / '.owner.lock').stat().st_ino, inode)
        self.assertFalse(any(self.profile.glob('Singleton*')))
        self.assertFalse(self.repair())

    def test_exclusive_lock_required(self):
        with FileLock(self.root / '.owner.lock', blocking=False):
            with patch.object(recovery, '_retired') as inspect:
                self.assertFalse(recovery.repair_docker_singletons(self.root))
                inspect.assert_not_called()
        self.assertTrue((self.profile / 'SingletonLock').is_symlink())

    def test_unknown_owner_and_unexpected_artifact_preserved(self):
        self.assertFalse(self.repair(False))
        cookie = self.profile / 'SingletonCookie'
        cookie.unlink()
        cookie.write_text('not-a-symlink')
        self.assertFalse(self.repair())
        self.assertTrue((self.profile / 'SingletonLock').is_symlink())
        self.assertEqual(cookie.read_text(), 'not-a-symlink')

    def test_symlink_profile_refused(self):
        other = self.root / 'saved'
        self.profile.rename(other)
        self.profile.symlink_to(other, target_is_directory=True)
        self.assertFalse(self.repair())
        self.assertTrue((other / 'SingletonLock').is_symlink())

    def test_visible_orphan_refused(self):
        original = Path.read_bytes
        def command(path):
            if path.name == 'cmdline':
                return b'chromium\0--user-data-dir=' + str(self.profile).encode()
            return original(path)
        with patch.object(Path, 'read_bytes', command):
            self.assertFalse(self.repair())
        self.assertTrue((self.profile / 'SingletonLock').is_symlink())

    def test_docker_evidence(self):
        owner = {'Id': 'abcdef123456' + '0' * 52,
                 'Config': {'Hostname': 'abcdef123456'},
                 'State': {'Running': False, 'Status': 'exited'},
                 'Mounts': [{'Source': str(self.root.parent)}]}
        active = dict(owner, Id='1' * 64, Config={'Hostname': 'new'},
                      State={'Running': True, 'Status': 'running'})
        for containers, expected in [([], True), ([owner], True),
                                     ([active], False), ([owner, active], False),
                                     ([dict(owner, State={'Running': True})], False)]:
            with self.subTest(containers=containers), \
                 patch.object(Path, 'exists', return_value=False), \
                 patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': ''}), \
                 patch.object(recovery, '_docker', side_effect=[' '.join(c['Id'] for c in containers), json.dumps(containers)]):
                self.assertEqual(recovery._retired('abcdef123456', self.root), expected)

    def test_remote_daemon_unknown_hostname_and_container_refused(self):
        with patch.object(Path, 'exists', return_value=False), \
             patch.dict(os.environ, {'DOCKER_HOST': 'tcp://remote:2376', 'DOCKER_CONTEXT': ''}), \
             patch.object(recovery, '_docker') as docker:
            self.assertFalse(recovery._retired('abcdef123456', self.root))
            self.assertFalse(recovery._retired('custom-host', self.root))
            docker.assert_not_called()
        with patch.object(Path, 'exists', return_value=True):
            self.assertFalse(recovery._retired('abcdef123456', self.root))

    def test_docker_failure_leaves_everything_intact(self):
        with patch.object(recovery, '_retired', side_effect=OSError('unavailable')):
            self.assertFalse(recovery.repair_docker_singletons(self.root))
        self.assertTrue((self.profile / 'SingletonLock').is_symlink())

    def test_launcher_stops_then_repairs_before_recreation(self):
        home = Path(self.temp.name) / 'home'
        (home / 'config').mkdir(parents=True)
        installer.private_write(home / 'config/progress.json', '{"complete": true}')
        installer.private_write(home / 'config/secretary.env',
                                'OPENROUTER_API_KEY=test\nDATABASE_URL=postgresql://u:p@external/db\n')
        events = []
        def run(command, **kwargs):
            events.append(command)
            return SimpleNamespace(returncode=0)
        with patch.dict(os.environ, {'P_SECRETARY_HOME': str(home)}), \
             patch.object(installer.subprocess, 'run', side_effect=run), \
             patch.object(recovery, 'repair_docker_singletons', side_effect=lambda root: events.append('repair')):
            self.assertEqual(installer.launch(SimpleNamespace(command='up')), 0)
        repair = events.index('repair')
        self.assertEqual(events[repair - 1][-2:], ['stop', 'app'])
        self.assertIn('--force-recreate', events[repair + 1])


if __name__ == '__main__':
    unittest.main()