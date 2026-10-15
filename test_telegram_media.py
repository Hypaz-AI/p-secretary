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

    def test_images_and_documents_never_reach_converter(self):
        for mime in (*media_text.IMAGE_TYPES, 'application/pdf', 'application/xml'):
            with self.subTest(mime=mime), patch.object(media_text, '_encoded_media') as read, patch(
                    'connectors.media_text.urllib.request.urlopen') as network:
                self.assertEqual(media_text.convert({'content_type': mime}), '')
                read.assert_not_called()
                network.assert_not_called()

    def test_image_parts_preserve_data_url_and_ignore_audio_documents(self):
        parts = media_text.image_parts([
            {'content_type': 'image/jpeg; charset=binary', 'data': b'jpg'},
            {'content_type': 'audio/ogg'}, {'content_type': 'application/pdf'}])
        self.assertEqual(parts, [{'type': 'image_url', 'image_url': {
            'url': 'data:image/jpeg;base64,' + base64.b64encode(b'jpg').decode()}}])

    def test_image_size_limit_before_network(self):
        with patch.object(media_text, 'MAX_BYTES', 2), patch(
                'connectors.media_text.urllib.request.urlopen') as network:
            with self.assertRaises(ValueError):
                media_text.image_parts([{'content_type': 'image/png', 'data': b'png'}])
            network.assert_not_called()

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
                media_text.convert({'content_type': 'audio/ogg', 'data': b'ogg'})

    def test_caption_preserved_and_nonmedia_ignored(self):
        with patch.object(media_text, 'convert', return_value='testo parlato') as call:
            result = media_text.message_text('controlla questo', [
                {'content_type': 'image/png'}, {'content_type': 'application/pdf'},
                {'content_type': 'audio/ogg'}])
        self.assertIn('controlla questo', result)
        self.assertIn('[Trascrizione vocale]\ntesto parlato', result)
        call.assert_called_once_with({'content_type': 'audio/ogg'})

    def test_image_only_message_keeps_caption_without_conversion(self):
        with patch.object(media_text, 'convert') as convert:
            self.assertEqual(media_text.message_text('caption', [
                {'content_type': 'image/png'}]), 'caption')
        convert.assert_not_called()


class PipelineMediaTests(unittest.TestCase):
    def test_analysis_transcribes_only_audio_and_preserves_document_attachments(self):
        import pipeline
        attachments = [{'filename': 'photo.png', 'content_type': 'image/png'},
                       {'filename': 'voice.ogg', 'content_type': 'audio/ogg'},
                       {'filename': 'invoice.pdf', 'content_type': 'application/pdf'},
                       {'filename': 'invoice.xml', 'content_type': 'application/xml'}]
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = None
        with patch.object(pipeline, 'load_message', return_value=(
                ('telegram', 'sender', 'subject', 'caption', 'source'), attachments)), patch.object(
                pipeline.flows, 'enabled', return_value=True), patch.object(
                pipeline.db, '_conn', return_value=connection), patch.object(
                media_text, 'convert', return_value='spoken') as convert, patch.object(
                pipeline.brain, 'analyze', return_value={'category': 'errore'}) as analyze:
            with self.assertRaisesRegex(RuntimeError, 'Analysis unavailable'):
                pipeline.analyze({'message_id': 42})
        convert.assert_called_once_with(attachments[1])
        analyze.assert_called_once_with('subject', 'sender',
            'caption\n\n[Trascrizione vocale]\nspoken', 'telegram', attachments)


class TelegramMediaTests(unittest.TestCase):
    def fetch(self, message, fail=False, owner='7'):
        message.update({'date': int(datetime.now().timestamp()), 'message_id': 42,
                        'from': {'first_name': 'Utente'}})
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'TELEGRAM_BOT_TOKEN', '123:test'), patch.object(
                config, 'TELEGRAM_CHAT_ID', owner), patch.object(telegram, 'acquire_poll_lock'), patch.object(
                telegram, '_call', return_value={'result': [{'update_id': 5, 'message': message}]}), patch.object(
                telegram, '_download', side_effect=OSError('network') if fail else None) as download:
            result = telegram.fetch_new_messages(4, directory)
        return result, download

    def test_voice_is_downloaded_and_authorized(self):
        (messages, offset), download = self.fetch({'chat': {'id': 7, 'type': 'private'},
            'voice': {'file_id': 'v', 'file_unique_id': 'unique', 'mime_type': 'audio/ogg'}})
        self.assertEqual(offset, 6)
        self.assertEqual(messages[0]['attachments'][0]['content_type'], 'audio/ogg')
        self.assertNotIn('unauthorized', messages[0])
        self.assertEqual(download.call_args.kwargs['max_bytes'], media_text.MAX_BYTES)

    def test_largest_owner_photo_and_caption(self):
        (messages, _), download = self.fetch({'chat': {'id': 7, 'type': 'private'}, 'caption': 'didascalia',
            'photo': [{'file_id': 'small', 'file_unique_id': 's', 'width': 10, 'height': 10},
                      {'file_id': 'large', 'file_unique_id': 'l', 'width': 100, 'height': 100}]})
        self.assertEqual(download.call_args.args[0], 'large')
        self.assertNotIn('unauthorized', messages[0])
        self.assertEqual(messages[0]['text'], 'didascalia')

    def test_rejected_chats_consume_offset_without_content_handling(self):
        for owner, chat in [('7', {'id': 8, 'type': 'private'}),
                            ('', {'id': 7, 'type': 'private'}),
                            (None, {'id': 7, 'type': 'private'}),
                            ('7', {'id': 7, 'type': 'group'}),
                            ('7', {'id': 7, 'type': 'supergroup'}),
                            ('7', {'id': 7, 'type': 'channel'}),
                            ('7', {'id': 7}), ('7', {}),
                            ('-7', {'id': -7, 'type': 'private'})]:
            for media in ({'text': 'private content'},
                          {'voice': {'file_id': 'v', 'file_unique_id': 'v'}},
                          {'audio': {'file_id': 'a', 'file_unique_id': 'a'}},
                          {'photo': [{'file_id': 'p', 'file_unique_id': 'p'}]},
                          {'document': {'file_id': 'd', 'file_unique_id': 'd',
                                        'file_name': 'private.pdf'}}):
                with self.subTest(owner=owner, chat=chat, media=media), patch.object(
                        telegram, '_media_attachments') as preprocess, patch.object(
                        telegram, '_full_name') as sender, patch.object(
                        telegram, '_chat_name') as name, patch('builtins.print') as log, patch.object(
                        telegram, 'notify') as notify, patch.object(media_text, 'convert') as ai:
                    (messages, offset), download = self.fetch({'chat': chat, **media}, owner=owner)
                self.assertEqual((messages, offset), ([], 6))
                for call in (download, preprocess, sender, name, log, notify, ai):
                    call.assert_not_called()

    def test_unknown_update_does_not_block_following_owner_message(self):
        updates = [{'update_id': 5, 'message': {'chat': {'id': 8, 'type': 'private'},
                    'voice': {'file_id': 'unknown'}}},
                   {'update_id': 6, 'message': {'chat': {'id': 7, 'type': 'private'},
                    'text': 'owner', 'message_id': 42}}]
        with patch.object(config, 'TELEGRAM_BOT_TOKEN', '123:test'), patch.object(
                config, 'TELEGRAM_CHAT_ID', '7'), patch.object(telegram, 'acquire_poll_lock'), patch.object(
                telegram, '_call', return_value={'result': updates}), patch.object(telegram, '_download') as download:
            messages, offset = telegram.fetch_new_messages(4)
        self.assertEqual(offset, 7)
        self.assertEqual([m['text'] for m in messages], ['owner'])
        download.assert_not_called()

    def test_failed_download_does_not_advance_offset(self):
        (messages, offset), _ = self.fetch({'chat': {'id': 7, 'type': 'private'},
            'voice': {'file_id': 'v', 'file_unique_id': 'u'}}, fail=True)
        self.assertEqual(messages, [])
        self.assertEqual(offset, 5)

    def test_oversized_media_is_visible_without_download(self):
        (messages, _), download = self.fetch({'chat': {'id': 7, 'type': 'private'}, 'voice': {
            'file_id': 'v', 'file_unique_id': 'u', 'file_size': media_text.MAX_BYTES + 1}})
        download.assert_not_called()
        self.assertIn('limite', messages[0]['text'])


class OwnerMediaTests(unittest.TestCase):
    def test_owner_photo_is_multimodal_and_skips_privileged_commands(self):
        import main
        with patch('pipeline.load_message', return_value=(('telegram', '', '', 'caption', ''),
                [{'content_type': 'image/jpeg', 'data': b'jpg'}])), patch.object(
                media_text, 'convert', return_value='/stop') as convert, patch.object(main, 'handle_user_chat') as chat, patch.object(
                main, '_handle_user_command') as command:
            main._owner_job({'message_id': 42, 'text': 'caption', 'media': True})
        command.assert_not_called()
        convert.assert_not_called()
        self.assertEqual('caption', chat.call_args.args[0])
        self.assertEqual(chat.call_args.kwargs['images'][0]['type'], 'image_url')
        self.assertEqual(base64.b64decode(chat.call_args.kwargs['images'][0][
            'image_url']['url'].split(',')[1]), b'jpg')

    def test_failed_conversion_does_not_call_main_model(self):
        import main
        with patch('pipeline.load_message', return_value=(('telegram', '', '', '', ''),
                [{'content_type': 'audio/ogg'}])), patch.object(media_text, 'convert', side_effect=RuntimeError('retry')), patch.object(
                main, 'handle_user_chat') as chat:
            with self.assertRaises(RuntimeError):
                main._owner_job({'message_id': 42, 'text': '', 'media': True})
        chat.assert_not_called()


class VisionChatTests(unittest.TestCase):
    def test_visual_prompt_is_subject_agnostic(self):
        import brain
        visual = brain.CHAT_PROMPT.split('IMMAGINI:', 1)[1]
        for term in ('volto', 'emozioni', 'espressioni visibili'):
            self.assertNotIn(term, visual)
        self.assertIn('senza presumere il soggetto', visual)
        self.assertIn('senza inventare dettagli', visual)

    def chat_payload(self, images=None, history=None):
        import brain
        with patch('db.communications_for_day', return_value=[]), patch(
                'db.upcoming_appointments', return_value=[]), patch(
                'db.personal_memories', return_value=[]), patch(
                'db.all_rules', return_value=[]), patch(
                'connectors.gcal.is_active', return_value=False), patch(
                'brain.remember_user', return_value='nessuna modifica necessaria') as memory, patch(
                'brain._owner_chat_request', return_value='risposta') as request:
            brain.chat_with_user('felice o triste?', history=history, images=images)
        memory.assert_called_once_with('felice o triste?', [])
        return request.call_args.args[0]

    def test_current_photo_receives_caption_identity_context_history_and_markers(self):
        parts = media_text.image_parts([{'content_type': 'image/png', 'data': b'png'}])
        payload = self.chat_payload(parts, [{'role': 'assistant', 'content': 'Ciao'}])
        self.assertEqual(payload['model'], config.OPENROUTER_MODEL)
        self.assertIn('@@EVENTO@@', payload['messages'][0]['content'])
        self.assertIn('Il tuo nome', payload['messages'][0]['content'])
        self.assertIn('ARCHIVIO NON FIDATO', payload['messages'][1]['content'])
        self.assertEqual(payload['messages'][-2]['content'], 'Ciao')
        self.assertEqual(payload['messages'][-1]['content'][0]['text'], 'felice o triste?')
        self.assertEqual(payload['messages'][-1]['content'][1:], parts)

    def test_followup_and_plain_text_both_use_main_model(self):
        history = [{'role': 'user', 'content': media_text.image_parts([
            {'content_type': 'image/jpeg', 'data': b'jpg'}])}]
        self.assertEqual(self.chat_payload(history=history)['model'], config.OPENROUTER_MODEL)
        self.assertEqual(self.chat_payload()['model'], config.OPENROUTER_MODEL)

    def test_failed_vision_falls_back_without_images_reasoning_or_events(self):
        import brain
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'choices': [
            {'message': {'content': 'Dal testo posso aiutarti.\n@@EVENTO@@ inventato',
                         'reasoning': 'private reasoning'}}]}).encode()
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'caption'}] +
                    media_text.image_parts([{'content_type': 'image/png', 'data': b'png'}])}]
        with patch('brain.urllib.request.urlopen', side_effect=[OSError('unavailable'), response]) as call:
            reply = brain._owner_chat_request({'model': config.OPENROUTER_MODEL,
                                              'messages': messages}, True)
        fallback = json.loads(call.call_args.args[0].data)
        self.assertEqual(fallback['model'], config.OPENROUTER_MODEL)
        self.assertNotIn('image_url', json.dumps(fallback))
        self.assertIn('caption', fallback['messages'][0]['content'])
        self.assertIn('Non posso vedere', reply)
        self.assertNotIn('@@EVENTO@@', reply)
        self.assertNotIn('private reasoning', reply)

    def test_history_preserves_photo_and_main_handles_calendar(self):
        import main
        parts = media_text.image_parts([{'content_type': 'image/png', 'data': b'png'}])
        with patch.object(main, '_chat_history', []), patch(
                'brain.chat_with_user', return_value='risposta'), patch(
                'main.gcal.parse_markers', return_value=('risposta', [{'title': 'evento'}])), patch(
                'main._create_chat_appointments', return_value='salvato') as create, patch(
                'main.telegram.notify', return_value=True):
            main.handle_user_chat('caption', images=parts)
            self.assertEqual(main._chat_history[0]['content'][1:], parts)
            self.assertEqual(main._chat_history[1]['content'], 'salvato')
            create.assert_called_once()


if __name__ == '__main__':
    unittest.main()