from test_config_portability import ConfigPortabilityTests


class SetupSafetyTests(ConfigPortabilityTests):
    def test_calendar_consent_and_delete(self):
        self.run_code('''
import setup_gcal as setup
from unittest.mock import patch
with patch('builtins.input', return_value='n'), patch.object(setup.gcal, 'create_event') as create:
    assert not setup.verify_write()
    create.assert_not_called()
with patch('builtins.input', return_value='s'), patch.object(setup.gcal, 'create_event', return_value={'ok': True, 'event_id': 'test'}), patch.object(setup.gcal, 'delete_event', return_value={'ok': False}):
    assert not setup.verify_write()
with patch('builtins.input', return_value='s'), patch.object(setup.gcal, 'create_event', return_value={'ok': True, 'event_id': 'test'}), patch.object(setup.gcal, 'delete_event', return_value={'ok': True}):
    assert setup.verify_write()
''')

    def test_nonce_pairing(self):
        self.run_code('''
from setup_telegram import owner_matches
msg = {'text': 'nonce', 'chat': {'type': 'private', 'id': 42}, 'from': {'id': 42}}
assert owner_matches(msg, 'nonce')
assert not owner_matches(msg, 'different')
msg['chat']['type'] = 'group'
assert not owner_matches(msg, 'nonce')
msg['chat']['type'] = 'private'
msg['from']['id'] = 99
assert not owner_matches(msg, 'nonce')
''')

    def test_whatsapp_env_and_code(self):
        self.run_code('''
import config, os
from connectors.whatsapp import bridge_env, bridge_path, NODE_BRIDGE
os.environ['NODE_PATH'] = '/opt/node_modules'
env = bridge_env(True)
assert env['WA_AUTH_ONLY'] == '1' and env['WA_SHOW_QR'] == '1'
assert env['WA_SPOOL_DIR'] == str(config.DATA_DIR / 'wa_pending')
assert env['NODE_PATH'].startswith('/opt/node_modules')
assert bridge_env()['WA_SHOW_QR'] == '0'
assert not bridge_path().is_relative_to(config.DATA_DIR)
assert 'if (AUTH_ONLY || !SPOOL) return' in NODE_BRIDGE
assert "process.on('SIGTERM', shutdown)" in NODE_BRIDGE
''')