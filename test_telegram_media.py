import base64
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import config
from connectors import media_text, telegram


class MediaTextTests(unittest.TestCase):
    def request(self, mime, data=b'media'):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': 'testo estratto'}}]}).encode()
        with patch.object(config, 'OPENROUTER_API_KEY', 'test-key'), patch(
                'connectors.media_text.urllib.request.urlopen', return_value=response) as call:
            result = media_text.convert({'content_type': mime, 'data': data})
        self.assertEqual(result, 'testo estratto')
        return json.loads(call.call_args.args[0].data)

    def test_voice_uses_separate_model_and_base64(self):
        payload = self.request('audio/ogg')
        self.assertEqual(payload['model'], config.OPENROUTER_AUDIO_MODEL)
        part = payload['messages'][1]['content'][1]
        self.assertEqual(part['input_audio']['format'], 'ogg')
        self.assertEqual(base64.b64decode(part['input_audio']['data']), b'media')

    def test_image_uses_vision_model_and_data_url(self):
        payload = self.request('image/jpeg')
        self.assertEqual(payload['model'], config.OPENROUTER_IMAGE_MODEL)
        self.assertTrue(payload['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,'))

    def test_size_limit_before_network(self):
        with patch.object(media_text, 'MAX_BYTES', 2), patch(
                'connectors.media_text.urllib.request.urlopen') as call:
            with self.assertRaises(ValueError):
                media_text.convert({'content_type': 'audio/ogg', 'data': b'abc'})
            call.assert_not_called()

    def test_empty_output_raises_for_retry(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":null}}]}'
        with patch.object(config, 'OPENROUTER_API_KEY', 'test'), patch(
                'connectors.media_text.urllib.request.urlopen', return_value=response):
            with self.assertRaises(RuntimeError):
                media_text.convert({'content_type': 'image/png', 'data': b'png'})

    def test_caption_preserved_and_nonmedia_ignored(self):
        with patch.object(media_text, 'convert', return_value='testo OCR') as call:
            result = media_text.message_text('controlla questo', [
                {'content_type': 'image/png'}, {'content_type': 'application/pdf'}])
        self.assertIn('controlla questo', result)
        self.assertIn('testo OCR', result)
        self.assertEqual(call.call_count, 1)


class TelegramMediaTests(unittest.TestCase):
    def fetch(self, message, fail=False):
        message.update({'date': int(datetime.now().timestamp()), 'message_id': 42,
                        'from': {'first_name': 'Utente'}})
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'TELEGRAM_BOT_TOKEN', '123:test'), patch.object(
                config, 'TELEGRAM_CHAT_ID', '7'), patch.object(telegram, 'acquire_poll_lock'), patch.object(
                telegram, '_call', return_value={'result': [{'update_id': 5, 'message': message}]}), patch.object(
                telegram, '_download', side_effect=OSError('network') if fail else None) as download:
            result = telegram.fetch_new_messages(4, directory)
        return result, download

    def test_voice_is_downloaded_and_authorized(self):
        (messages, offset), download = self.fetch({'chat': {'id': 7},
            'voice': {'file_id': 'v', 'file_unique_id': 'unique', 'mime_type': 'audio/ogg'}})
        self.assertEqual(offset, 6)
        self.assertEqual(messages[0]['attachments'][0]['content_type'], 'audio/ogg')
        self.assertNotIn('unauthorized', messages[0])
        self.assertEqual(download.call_args.kwargs['max_bytes'], media_text.MAX_BYTES)

    def test_largest_photo_caption_and_untrusted_chat(self):
        (messages, _), download = self.fetch({'chat': {'id': 8}, 'caption': 'didascalia',
            'photo': [{'file_id': 'small', 'file_unique_id': 's', 'width': 10, 'height': 10},
                      {'file_id': 'large', 'file_unique_id': 'l', 'width': 100, 'height': 100}]})
        self.assertEqual(download.call_args.args[0], 'large')
        self.assertTrue(messages[0]['unauthorized'])
        self.assertEqual(messages[0]['text'], 'didascalia')

    def test_failed_download_does_not_advance_offset(self):
        (messages, offset), _ = self.fetch({'chat': {'id': 7},
            'voice': {'file_id': 'v', 'file_unique_id': 'u'}}, fail=True)
        self.assertEqual(messages, [])
        self.assertEqual(offset, 5)

    def test_oversized_media_is_visible_without_download(self):
        (messages, _), download = self.fetch({'chat': {'id': 7}, 'voice': {
            'file_id': 'v', 'file_unique_id': 'u', 'file_size': media_text.MAX_BYTES + 1}})
        download.assert_not_called()
        self.assertIn('limite', messages[0]['text'])


class OwnerMediaTests(unittest.TestCase):
    def test_conversion_precedes_main_model_and_skips_privileged_commands(self):
        import main
        with patch('pipeline.load_message', return_value=(('telegram', '', '', 'caption', ''),
                [{'content_type': 'image/jpeg', 'data': b'jpg'}])), patch.object(
                media_text, 'convert', return_value='/stop'), patch.object(main, 'handle_user_chat') as chat, patch.object(
                main, '_handle_user_command') as command:
            main._owner_job({'message_id': 42, 'text': 'caption', 'media': True})
        command.assert_not_called()
        self.assertIn('/stop', chat.call_args.args[0])
        self.assertIn('caption', chat.call_args.args[0])

    def test_failed_conversion_does_not_call_main_model(self):
        import main
        with patch('pipeline.load_message', return_value=(('telegram', '', '', '', ''),
                [{'content_type': 'audio/ogg'}])), patch.object(media_text, 'convert', side_effect=RuntimeError('retry')), patch.object(
                main, 'handle_user_chat') as chat:
            with self.assertRaises(RuntimeError):
                main._owner_job({'message_id': 42, 'text': '', 'media': True})
        chat.assert_not_called()


if __name__ == '__main__':
    unittest.main()