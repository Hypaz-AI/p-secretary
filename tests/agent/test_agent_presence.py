import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from secretary.agent import presence, toolkit
from secretary.connectors import telegram


def fast_timing():
    return patch.multiple(presence, TICK=0.01, STATUS_AFTER=0.05, STATUS_EVERY=60, MIN_EDIT=0)


class PresenceTests(unittest.TestCase):
    def test_quick_turn_shows_typing_only_and_no_status(self):
        with patch.object(presence.telegram, 'chat_action', return_value=True) as typing, \
                patch.object(presence.telegram, 'send_status') as send, \
                patch.object(presence.telegram, 'delete_message') as delete, \
                patch.multiple(presence, TICK=0.01, STATUS_AFTER=60):
            p = presence.Presence().start()
            time.sleep(0.05)
            p.close()
        self.assertGreaterEqual(typing.call_count, 1)
        send.assert_not_called()
        delete.assert_not_called()

    def test_long_turn_sends_one_status_updates_on_step_and_deletes_on_close(self):
        with patch.object(presence.telegram, 'chat_action', return_value=True), \
                patch.object(presence.telegram, 'send_status', return_value=77) as send, \
                patch.object(presence.telegram, 'edit_status') as edit, \
                patch.object(presence.telegram, 'delete_message') as delete, \
                fast_timing():
            p = presence.Presence().start()
            time.sleep(0.12)
            p.step('mail_search')
            time.sleep(0.12)
            p.close()
        send.assert_called_once()
        self.assertIn(presence.IDLE, send.call_args.args[0])
        self.assertTrue(any(presence.STEPS['mail_search'] in c.args[1] for c in edit.call_args_list))
        self.assertTrue(all(c.args[0] == 77 for c in edit.call_args_list))
        delete.assert_called_once_with(77)

    def test_no_calls_after_close(self):
        with patch.object(presence.telegram, 'chat_action', return_value=True) as typing, \
                patch.object(presence.telegram, 'send_status', return_value=1), \
                patch.object(presence.telegram, 'edit_status'), \
                patch.object(presence.telegram, 'delete_message'), \
                fast_timing():
            p = presence.Presence().start()
            time.sleep(0.12)
            p.close()
            calls = typing.call_count
            time.sleep(0.1)
        self.assertEqual(typing.call_count, calls)

    def test_telegram_failures_never_reach_the_turn(self):
        with patch.object(presence.telegram, 'chat_action', side_effect=OSError('down')), \
                patch.object(presence.telegram, 'send_status', side_effect=OSError('down')), \
                patch.object(presence.telegram, 'delete_message', side_effect=OSError('down')), \
                fast_timing():
            p = presence.Presence().start()
            p.step('web_search')
            time.sleep(0.08)
            p.close()

    def test_unknown_tool_uses_generic_label(self):
        p = presence.Presence()
        p.step('tool_inesistente')
        self.assertEqual(p._step, presence.IDLE)


class TelegramPresenceCallsTests(unittest.TestCase):
    def test_helpers_build_silent_status_messages(self):
        with patch.object(telegram.config, 'TELEGRAM_BOT_TOKEN', 'token'), \
                patch.object(telegram.config, 'TELEGRAM_CHAT_ID', '42'), \
                patch.object(telegram, '_call', return_value={'result': {'message_id': 9}}) as call:
            self.assertTrue(telegram.chat_action('typing'))
            self.assertEqual(telegram.send_status('ciao'), 9)
            telegram.edit_status(9, 'nuovo')
            telegram.delete_message(9)
        self.assertEqual(call.call_args_list[0].args, ('sendChatAction', {'chat_id': '42', 'action': 'typing'}))
        self.assertEqual(call.call_args_list[1].args,
                         ('sendMessage', {'chat_id': '42', 'text': 'ciao', 'disable_notification': True}))
        self.assertEqual(call.call_args_list[2].args,
                         ('editMessageText', {'chat_id': '42', 'message_id': 9, 'text': 'nuovo'}))
        self.assertEqual(call.call_args_list[3].args, ('deleteMessage', {'chat_id': '42', 'message_id': 9}))

    def test_helpers_do_nothing_when_not_configured(self):
        with patch.object(telegram.config, 'TELEGRAM_BOT_TOKEN', ''), \
                patch.object(telegram, '_call') as call:
            self.assertFalse(telegram.chat_action())
        call.assert_not_called()


class ToolkitProgressTests(unittest.TestCase):
    def test_tool_call_reports_its_name_before_running(self):
        seen = []
        state = toolkit.TurnState('owner', None, 'owner:1')
        state.progress = seen.append
        spec = SimpleNamespace(name='mail_search', handler=lambda s, p: {'ok': True})
        params = MagicMock()
        with patch.object(toolkit.jobs, 'assert_current'), \
                patch.object(toolkit.jobs, 'bind', return_value=nullcontext()), \
                patch.object(toolkit, 'journal'):
            result = toolkit.call(spec, state, params)
        self.assertEqual(result, {'ok': True})
        self.assertEqual(seen, ['mail_search'])


if __name__ == '__main__':
    unittest.main()
