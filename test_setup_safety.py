from test_config_portability import ConfigPortabilityTests


class SetupSafetyTests(ConfigPortabilityTests):
    def test_hidden_json_completion_and_escapes(self):
        self.run_code(r'''
import json
import setup_gcal as setup
from unittest.mock import patch
compact = r'{"private_key":"fake\nline\n","type":"service_account"}'
for lines in ([compact], ['{', '"private_key": "fake\\nline\\n",', '"type": "service_account"', '}']):
    with patch.object(setup.getpass, 'getpass', side_effect=lines) as hidden:
        raw = setup.hidden_json()
        assert json.loads(raw) == json.loads(compact)
        assert r'\n' in raw
        assert hidden.call_count == len(lines)
with patch.object(setup.getpass, 'getpass', return_value='') as hidden:
    assert setup.hidden_json() == ''
    hidden.assert_called_once()
''')

    def test_hidden_json_invalid_bounded_and_redacted(self):
        self.run_code(r'''
import io, contextlib, getpass
import setup_gcal as setup
from unittest.mock import patch
cases = [(['{"fake-secret": bad}'], 1), (['{"fake-secret":', 'FINE'], 2),
         (['[]'], 1), (['{"fake-secret":"' + 'x' * 131072], 1),
         (['{"fake-secret":"oops\\z"}'], 1), (['{"fake-secret":1} trailing'], 1)]
for lines, count in cases:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), patch.object(setup.getpass, 'getpass', side_effect=lines) as hidden:
        try:
            setup.hidden_json()
        except ValueError as exc:
            assert 'fake-secret' not in str(exc)
        else:
            raise AssertionError('invalid input accepted')
        assert hidden.call_count == count
    assert 'fake-secret' not in output.getvalue()
with patch.object(setup.getpass, 'getpass', side_effect=getpass.GetPassWarning):
    try:
        setup.hidden_json()
    except getpass.GetPassWarning:
        pass
    else:
        raise AssertionError('echo fallback accepted')
''')

    def test_calendar_interrupt_does_not_write(self):
        self.run_code('''
import setup_gcal as setup
from unittest.mock import patch
for interruption in (KeyboardInterrupt, EOFError):
    with patch.object(setup, 'guide'), patch.object(setup.gcal, '_load_service_account', return_value=None), \\
            patch.object(setup.getpass, 'getpass', side_effect=interruption), \\
            patch.object(setup.config, 'set_env') as save, patch.object(setup, 'atomic_write') as write:
        assert setup.main() == 130
        save.assert_not_called()
        write.assert_not_called()
''')

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