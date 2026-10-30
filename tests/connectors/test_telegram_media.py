import base64
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from secretary.core import config
from secretary.connectors import media_text, telegram


def completion(content, finish_reason='stop'):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        'choices': [{'finish_reason': finish_reason,
                     'message': {'content': content}}]
    }).encode()
    return response


class MediaTextTests(unittest.TestCase):
    def test_audio_budget_scales_with_duration_and_is_bounded(self):
        self.assertEqual(media_text.audio_token_budget({'duration': 30}), 4696)
        self.assertEqual(media_text.audio_token_budget({'duration': 300}), 10096)
        self.assertEqual(media_text.audio_token_budget({'duration': 1200}), 28096)
        self.assertEqual(media_text.audio_token_budget({'duration': 100000}), 32768)
        for duration in (None, '', 'bad', True, -1, 0, float('nan'), float('inf')):
            with self.subTest(duration=duration):
                self.assertEqual(media_text.audio_token_budget({'duration': duration}), 16384)

    def test_long_transcription_is_not_silently_cut(self):
        text = 'parlato ' * 3000
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': text}}]}).encode()
        with patch.object(config, 'OPENROUTER_API_KEY', 'test'), patch(
                'secretary.connectors.media_text.urllib.request.urlopen', return_value=response) as request:
            result = media_text.convert({'content_type': 'audio/ogg', 'data': b'audio',
                                         'duration': 300})
        self.assertEqual(result, text.strip())
        self.assertEqual(json.loads(request.call_args.args[0].data)['max_tokens'], 10096)

    def test_audio_truncation_retries_with_larger_budget(self):
        with patch.object(config, 'OPENROUTER_API_KEY', 'test'), patch(
                'secretary.connectors.media_text.urllib.request.urlopen', side_effect=[
                    completion('inizio', 'length'), completion('trascrizione completa')]) as request:
            result = media_text.convert({'content_type': 'audio/ogg', 'data': b'audio',
                                         'duration': 300})
        self.assertEqual(result, 'trascrizione completa')
        self.assertEqual([json.loads(call.args[0].data)['max_tokens']
                          for call in request.call_args_list], [10096, 20192])

    def test_audio_safety_limit_raises_instead_of_cutting(self):
        with patch.object(config, 'OPENROUTER_API_KEY', 'test'), patch.object(
                media_text.ai_budget, 'request_content', return_value='troppo lungo'), patch.object(
                media_text, 'MAX_TEXT', 2):
            with self.assertRaisesRegex(RuntimeError, 'limite di sicurezza'):
                media_text.convert({'content_type': 'audio/ogg', 'data': b'audio'})

    def request(self, mime, data=b'media'):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': 'testo estratto'}}]}).encode()
        with patch.object(config, 'OPENROUTER_API_KEY', 'test-key'), patch(
                'secretary.connectors.media_text.urllib.request.urlopen', return_value=response) as call:
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
                    'secretary.connectors.media_text.urllib.request.urlopen') as network:
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
                'secretary.connectors.media_text.urllib.request.urlopen') as network:
            with self.assertRaises(ValueError):
                media_text.image_parts([{'content_type': 'image/png', 'data': b'png'}])
            network.assert_not_called()

    def test_size_limit_before_network(self):
        with patch.object(media_text, 'MAX_BYTES', 2), patch(
                'secretary.connectors.media_text.urllib.request.urlopen') as call:
            with self.assertRaises(ValueError):
                media_text.convert({'content_type': 'audio/ogg', 'data': b'abc'})
            call.assert_not_called()

    def test_empty_output_raises_for_retry(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":null}}]}'
        with patch.object(config, 'OPENROUTER_API_KEY', 'test'), patch(
                'secretary.connectors.media_text.urllib.request.urlopen', return_value=response):
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
    def test_load_message_restores_archived_audio_duration(self):
        from secretary.workflows import pipeline
        connection = MagicMock()
        conn = connection.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = ('telegram', 'owner', '', 'test', '1')
        conn.execute.return_value.fetchall.return_value = [
            ('voice.ogg', 'audio/ogg', '/tmp/voice.ogg', None,
             {'duration': 123, 'path': 'untrusted path'}),
            ('old.ogg', 'audio/ogg', '/tmp/old.ogg', None, {})]
        with patch.object(pipeline.db, '_conn', return_value=connection):
            _, attachments = pipeline.load_message(42)
        self.assertEqual(attachments[0]['duration'], 123)
        self.assertEqual(attachments[0]['path'], '/tmp/voice.ogg')
        self.assertNotIn('duration', attachments[1])

    def test_inbox_transcribes_only_audio_and_lists_other_attachments(self):
        """L'agente riceve la trascrizione della voce e il solo elenco degli altri allegati."""
        from secretary.workflows import pipeline
        from secretary.agent import turns
        attachments = [{'id': 1, 'filename': 'photo.png', 'content_type': 'image/png', 'transcript': 'foto'},
                       {'filename': 'voice.ogg', 'content_type': 'audio/ogg'},
                       {'id': 3, 'filename': 'invoice.pdf', 'content_type': 'application/pdf'},
                       {'id': 4, 'filename': 'invoice.xml', 'content_type': 'application/xml'}]
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = None
        connection.__enter__.return_value.execute.return_value.fetchall.return_value = []
        seen = {}

        class Fake:
            def run(self, state, system, prompt, scope, timeout):
                seen['prompt'] = prompt
                return ''
        with patch.object(pipeline, 'load_message', return_value=(
                ('telegram', 'sender', 'subject', 'caption', 'source'), attachments)), patch.object(
                turns.flows, 'enabled', return_value=True), patch.object(
                pipeline.policy, 'can_operate', side_effect=lambda conn, mid, effect, **kw:
                    mid == 42 and effect in {'extract', 'ai_text', 'ai_media'}) as grant, patch.object(
                pipeline.db, '_conn', return_value=connection), patch.object(
                media_text, 'convert', return_value='spoken') as convert:
            with self.assertRaisesRegex(RuntimeError, 'Triage incompleto'):
                turns.inbox_job({'message_id': 42}, runtime=Fake())
        convert.assert_called_once_with(attachments[1])
        self.assertIn('caption', seen['prompt'])
        self.assertIn('[Trascrizione vocale]\nspoken', seen['prompt'])
        self.assertIn('Immagine: testo e descrizione]\nfoto', seen['prompt'])
        for name in ('photo.png', 'invoice.pdf', 'invoice.xml'):
            self.assertIn(name, seen['prompt'])
        self.assertTrue({'extract', 'ai_text', 'ai_media'} <= {c.args[2] for c in grant.call_args_list})


class TelegramMediaTests(unittest.TestCase):
    def fetch(self, message, fail=False, owner='7'):
        message.update({'date': int(datetime.now().timestamp()), 'message_id': 42,
                        'from': {'id': 7, 'first_name': 'Utente'}})
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'TELEGRAM_BOT_TOKEN', '123:test'), patch.object(
                config, 'TELEGRAM_CHAT_ID', owner), patch.object(telegram, 'acquire_poll_lock'), patch.object(
                telegram, '_call', return_value={'result': [{'update_id': 5, 'message': message}]}), patch.object(
                telegram, '_download', side_effect=OSError('network') if fail else None) as download:
            result = telegram.fetch_new_messages(4, directory)
        return result, download

    def test_voice_is_downloaded_and_authorized(self):
        (messages, offset), download = self.fetch({'chat': {'id': 7, 'type': 'private'},
            'voice': {'file_id': 'v', 'file_unique_id': 'unique', 'mime_type': 'audio/ogg',
                      'duration': 42}})
        self.assertEqual(offset, 6)
        self.assertEqual(messages[0]['attachments'][0]['content_type'], 'audio/ogg')
        self.assertEqual(messages[0]['attachments'][0]['duration'], 42)
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

    def test_unsupported_owner_content_is_archived_not_silently_dropped(self):
        # Nella chat privata owner↔bot ogni input riceve una risposta: uno
        # sticker/video/posizione non scaricabile resta un messaggio visibile.
        for key, label in (('sticker', 'sticker'), ('video', 'video'),
                           ('location', 'posizione'), ('contact', 'contatto'),
                           ('poll', 'sondaggio')):
            with self.subTest(key=key):
                (messages, offset), download = self.fetch(
                    {'chat': {'id': 7, 'type': 'private'}, key: {'file_id': 'x'}})
                self.assertEqual(offset, 6)
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]['text'], f'[Contenuto non supportato: {label}]')
                self.assertEqual(messages[0]['attachments'], [])
                download.assert_not_called()

    def test_unsupported_content_placeholder_is_never_command_authority(self):
        # Il segnaposto è testo sintetico: non deve diventare `direct_text`,
        # ma il messaggio deve comunque arrivare al worker owner (risposta).
        from secretary import main
        from secretary.core import db, jobs, policy
        message = {'update_id': 8, 'from': 'owner',
                   'text': '[Contenuto non supportato: sticker]',
                   'source_id': json.dumps(['bot', '123', '7', 42]),
                   'chat_id': '7', 'chat_type': 'private',
                   'msg_date': '2026-10-14T11:38:00+02:00',
                   'metadata': {'account': 'bot:123', 'update_id': 8, 'message_id': 42,
                                'chat_id': '7', 'chat_type': 'private', 'chat_name': 'owner',
                                'author': 7, 'direction': 'in'},
                   'attachments': []}
        with patch.object(main, 'running', True), \
             patch.object(config, 'TELEGRAM_CHAT_ID', '7'), \
             patch.object(config, 'TELEGRAM_BOT_TOKEN', '123:test'), \
             patch.object(main.flows, 'enabled', return_value=True), \
             patch.object(telegram, 'fetch_new_messages', return_value=([message], 9)), \
             patch.object(db, '_conn') as connect, \
             patch.object(db.message_store, 'store', return_value=42) as store, \
             patch.object(policy, 'can_operate', return_value=True), \
             patch.object(jobs, 'enqueue') as enqueue:
            main.check_telegram({})
        metadata = store.call_args.kwargs['metadata']
        self.assertEqual(metadata['content_origin'], 'attachment')
        self.assertEqual(enqueue.call_args.args[1], 'owner')


class OwnerMediaTests(unittest.TestCase):
    """Il worker del proprietario: comando slash solo da testo nativo diretto, altrimenti l'agente."""

    def setUp(self):
        from secretary.core import db
        self.body = 'caption'
        self.conn = MagicMock(name='owner-worker-connection')
        def execute(sql, *args):
            if sql.startswith('SELECT body FROM telegram_messages'):
                row = (self.body,)
            else:
                raise AssertionError('Unexpected worker SQL: ' + sql)
            return MagicMock(**{'fetchone.return_value': row})
        self.conn.execute.side_effect = execute
        connect = self.enterContext(patch('secretary.core.db._conn'))
        connect.return_value.__enter__.return_value = self.conn
        self.enterContext(patch('secretary.core.policy.can_operate', side_effect=lambda conn, mid, effect, **kw:
            mid == 42 and effect in {'owner_command', 'owner_dialogue', 'ai_text', 'ai_media'}))
        self.enterContext(patch('secretary.core.policy.metadata_for', return_value=dict(metadata=dict(
            content_origin='direct_text', is_forwarded=False))))
        self.enterContext(patch('secretary.core.jobs.assert_current'))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('secretary.core.ai_budget.request_content', side_effect=AssertionError('Model forbidden')))
        if db.psycopg is not None:
            self.enterContext(patch.object(db.psycopg, 'connect', side_effect=AssertionError('PostgreSQL forbidden')))

    def test_media_message_never_runs_a_slash_command_and_goes_to_the_agent(self):
        from secretary import main
        from secretary.agent import owner, turns
        with patch('secretary.workflows.pipeline.load_message', return_value=(('telegram', '', '', '/stop', ''),
                [{'content_type': 'image/jpeg', 'data': b'jpg'}])), \
             patch.object(owner, 'command') as command, patch.object(turns, 'owner_turn') as turn:
            self.body = '/stop'
            main._owner_job({'message_id': 42, 'text': '/stop', 'media': True})
        command.assert_not_called()  # una didascalia o un contenuto media non diventa mai un comando
        turn.assert_called_once_with(42)

    def test_native_text_command_is_handled_without_the_agent(self):
        from secretary import main
        from secretary.agent import owner, turns
        self.body = '/attivita'
        with patch('secretary.workflows.pipeline.load_message', return_value=(('telegram', 'owner', '', self.body, ''), [])), \
             patch.object(owner, 'command', return_value=True) as command, patch.object(turns, 'owner_turn') as turn:
            main._owner_job(dict(message_id=42, text='Payload non autorevole', media=False))
        command.assert_called_once_with(self.body, 42)
        turn.assert_not_called()

    def test_plain_text_that_is_not_a_command_goes_to_the_agent(self):
        from secretary import main
        from secretary.agent import owner, turns
        self.body = 'Domanda archiviata'
        with patch('secretary.workflows.pipeline.load_message', return_value=(('telegram', 'owner', '', self.body, ''), [])), \
             patch.object(owner, 'command', return_value=False) as command, patch.object(turns, 'owner_turn') as turn:
            main._owner_job(dict(message_id=42, text='Payload non autorevole', media=False))
        command.assert_called_once_with(self.body, 42)
        turn.assert_called_once_with(42)

    def test_loss_of_native_authority_is_a_permanent_visible_failure(self):
        from secretary.core import jobs
        from secretary import main
        from secretary.agent import owner, turns
        with patch.object(owner, 'authorize', side_effect=PermissionError('gone')), \
             patch.object(turns, 'owner_turn') as turn:
            with self.assertRaises(jobs.Permanent):
                main._owner_job(dict(message_id=42))
        turn.assert_not_called()


if __name__ == '__main__':
    unittest.main()