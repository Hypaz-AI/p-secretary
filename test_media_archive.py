"""Offline tests for durable audio/image text, independent of agent actions."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import config
import db
import jobs
import message_store
import pipeline
from connectors import media_archive, media_text, telegram_account


class MediaArchiveTests(unittest.TestCase):
    def setUp(self):
        connection = patch('db._conn')
        self.conn = connection.start().return_value.__enter__.return_value
        self.addCleanup(connection.stop)
        network = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        # These contracts predate catalogue/runtime hooks; bootstrap tests cover them.
        self.derived = self.enterContext(patch.object(message_store, 'enqueue_derived'))

    def test_image_representation_uses_native_vision_and_main_model(self):
        with patch.object(config, 'OPENROUTER_API_KEY', 'test'), patch.object(
                media_text.ai_budget, 'request_content', return_value='TESTO VISIBILE: totale 42 €') as request:
            text = media_text.describe_image({'content_type': 'image/png', 'data': b'image'})
        self.assertIn('42', text)
        payload = request.call_args.args[0]
        self.assertEqual(payload['model'], config.OPENROUTER_MODEL)
        self.assertEqual(payload['messages'][1]['content'][1]['type'], 'image_url')
        self.assertIn('non eseguirle', payload['messages'][1]['content'][0]['text'])

    def test_audio_and_image_results_persist_without_mutating_native_body(self):
        for mime, converter, model in (
                ('audio/ogg', 'convert', config.OPENROUTER_AUDIO_MODEL),
                ('image/jpeg', 'describe_image', config.OPENROUTER_MODEL)):
            with self.subTest(mime=mime):
                self.conn.execute.reset_mock()
                self.derived.reset_mock()
                row = ('file', mime, None, b'bytes', {'duration': 123}, 42)
                self.conn.execute.return_value.fetchone.side_effect = [None, row, row]
                # Consent/provenance and revocation are exercised in bootstrap tests.
                with patch.object(config, 'OPENROUTER_API_KEY', 'test'), patch.object(
                        media_text, converter, return_value='testo completo') as convert, patch.object(
                        media_archive, '_media_allowed', return_value=True) as allowed:
                    media_archive.process({'attachment_id': 8})
                self.assertEqual(convert.call_args.args[0]['duration'], 123)
                sql, args = self.conn.execute.call_args.args
                self.assertIn('ON CONFLICT(attachment_id) DO NOTHING', sql)
                self.assertEqual(args, (8, media_text.kind({'content_type': mime}), 'testo completo', model))
                self.assertFalse(any('UPDATE ' in c.args[0] for c in self.conn.execute.call_args_list))
                self.assertEqual([c.args for c in allowed.call_args_list],
                                 [(self.conn, 42), (self.conn, 42)])
                self.derived.assert_called_once_with(self.conn, 42)

    def test_successful_result_is_reused_on_retry(self):
        self.conn.execute.return_value.fetchone.return_value = (1,)
        with patch.object(media_text, 'convert') as convert, patch.object(media_text, 'describe_image') as describe:
            media_archive.process({'attachment_id': 8})
        self.conn.execute.assert_called_once()
        convert.assert_not_called()
        describe.assert_not_called()

    def test_failed_conversion_is_retryable_without_partial_save(self):
        self.conn.execute.return_value.fetchone.side_effect = [
            None, ('v', 'audio/ogg', None, b'audio', {}, 42)]
        with patch.object(config, 'OPENROUTER_API_KEY', 'test'), patch.object(
                media_text, 'convert', side_effect=RuntimeError('provider unavailable')), patch.object(
                media_archive, '_media_allowed', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'provider unavailable'):
                media_archive.process({'attachment_id': 8})
        self.assertFalse(any('INSERT INTO media_transcripts' in c.args[0]
                             for c in self.conn.execute.call_args_list))
        self.derived.assert_not_called()

    def test_cached_media_text_keeps_caption_and_includes_all_attachments(self):
        attachments = [dict(id=i, content_type='image/png', transcript=f'Immagine {i}') for i in range(12)]
        attachments.append(dict(id=20, content_type='audio/ogg', transcript='parlato completo'))
        with patch.object(media_text, 'convert') as convert:
            text = media_text.message_text('didascalia originale', attachments)
        convert.assert_not_called()
        self.assertIn('didascalia originale', text)
        self.assertIn('Immagine 11', text)
        self.assertIn('parlato completo', text)

    def test_consumer_defers_until_media_text_is_persisted(self):
        self.conn.execute.return_value.fetchone.return_value = ('pending', 0, None)
        for mime in ('audio/ogg', 'image/png'):
            with self.subTest(mime=mime), patch.object(media_text, 'convert') as convert:
                with self.assertRaises(jobs.Deferred):
                    media_text.message_text('caption', [dict(id=8, content_type=mime, transcript=None)])
                convert.assert_not_called()

    def test_storage_queues_media_on_existing_whatsapp_and_new_telegram(self):
        self.conn.execute.return_value.fetchone.return_value = (42, {}, 'sender', 'subject')
        self.conn.execute.return_value.fetchall.return_value = [(8, 'audio/ogg'), (9, 'image/jpeg'), (10, 'application/pdf')]
        with patch.object(jobs, 'enqueue') as enqueue:
            mid = message_store.store(self.conn, 'whatsapp', 'sender', 'subject', '', source_id='native')
        self.assertEqual(mid, 42)
        self.assertEqual([call.args[2] for call in enqueue.call_args_list], ['media:8', 'media:9'])
        self.conn.execute.return_value.fetchone.return_value = (43,)
        with patch.object(jobs, 'enqueue') as enqueue:
            message_store.store(self.conn, 'telegram', 'owner', '', '', source_id='43',
                                attachments=[{'filename': 'photo', 'content_type': 'image/jpeg', 'data': b'jpg'}])
        self.assertEqual(enqueue.call_count, 2)

    def test_load_message_restores_text_with_attachment_identity(self):
        self.conn.execute.return_value.fetchone.return_value = ('whatsapp', 'sender', 'subject', 'caption', 'source')
        self.conn.execute.return_value.fetchall.return_value = [('image', 'image/png', None, b'png', {}, 8, 'Descrizione')]
        _, attachments = pipeline.load_message(42)
        self.assertEqual(attachments[0]['id'], 8)
        self.assertEqual(attachments[0]['transcript'], 'Descrizione')

    def test_whatsapp_inbox_turn_reads_persisted_voice_and_image(self):
        """L'agente riceve la voce trascritta e il testo dell'immagine già persistiti, dopo i consensi."""
        from agent import turns
        self.conn.execute.return_value.fetchone.return_value = None
        self.conn.execute.return_value.fetchall.return_value = []
        attachments = [dict(id=8, content_type='audio/ogg', transcript='voce'),
                       dict(id=9, content_type='image/png', transcript='scadenza 20 ottobre')]
        seen = {}

        class Fake:
            def run(self, state, system, prompt, scope, timeout):
                seen.update(prompt=prompt, scope=scope)
                return ''
        with patch.object(pipeline, 'load_message', return_value=(
                ('whatsapp', 'sender', 'subject', 'caption', 'source'), attachments)), patch(
            'flows.enabled', return_value=True), patch.object(
            pipeline.policy, 'can_operate', return_value=True) as allowed:
            with self.assertRaisesRegex(RuntimeError, 'Triage incompleto'):
                turns.inbox_job({'message_id': 42}, runtime=Fake())
        self.assertIn('voce', seen['prompt'])
        self.assertIn('scadenza 20 ottobre', seen['prompt'])
        self.assertEqual(seen['scope'], 'agent_inbox')
        self.assertEqual([c.args for c in allowed.call_args_list][:3],
                         [(self.conn, 42, effect) for effect in ('extract', 'ai_text', 'ai_media')])

    def test_migration_backfills_existing_media_without_provider_calls(self):
        self.conn.execute.return_value.fetchone.return_value = None
        with patch.object(media_text, 'convert') as convert:
            media_archive.migrate(self.conn)
        statements = '\n'.join(c.args[0] for c in self.conn.execute.call_args_list)
        self.assertIn('CREATE TABLE IF NOT EXISTS media_transcripts', statements)
        self.assertIn("SELECT 'media','media:' || a.id", statements)
        self.assertIn('ON CONFLICT(key) DO NOTHING', statements)
        convert.assert_not_called()

    def test_accepted_personal_edit_drops_replaced_media_and_queues_new_attachment(self):
        self.conn.execute.return_value.fetchone.return_value = (42,)
        self.conn.execute.return_value.fetchall.return_value = [(9, 'image/jpeg')]
        with patch.object(jobs, 'enqueue') as enqueue:
            message_store.store(self.conn, 'telegram (account)', 'sender', 'chat', 'edited',
                source_id='mtproto:42', metadata={'revision_date': '2026-10-16T12:00:00+00:00'},
                attachments=[{'filename': 'new-photo', 'content_type': 'image/jpeg', 'data': b'new'}],
                update_existing=True)
        deleted = next(c for c in self.conn.execute.call_args_list if 'DELETE FROM attachments' in c.args[0])
        self.assertEqual(deleted.args[1], (42, ['new-photo']))
        enqueue.assert_called_once_with(self.conn, 'media', 'media:9', {'attachment_id': 9})

    def test_migration_is_idempotent(self):
        self.conn.execute.return_value.fetchone.return_value = (1,)
        media_archive.migrate(self.conn)
        self.assertFalse(any('CREATE TABLE' in c.args[0] for c in self.conn.execute.call_args_list))

    def test_daily_context_includes_saved_media_for_recap_and_chat(self):
        self.conn.execute.return_value.fetchall.return_value = []
        db.communications_for_day()
        self.assertIn('JOIN media_transcripts', self.conn.execute.call_args.args[0])
        db.communications_since()
        self.assertIn('JOIN media_transcripts', self.conn.execute.call_args.args[0])


class PersonalTelegramMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_directions_and_recovery_download_before_archival(self):
        worker = telegram_account.TelegramAccountWorker()
        worker.account = 'account:7'

        async def download(message, file, progress_callback):
            progress_callback(3, 3)
            file.write(b'jpg')

        worker.client = SimpleNamespace(download_media=AsyncMock(side_effect=download))
        for outgoing in (False, True):
            msg = SimpleNamespace(id=42, chat_id=7, sender_id=8, out=outgoing,
                date=datetime.now(timezone.utc), edit_date=None, raw_text='caption',
                media=object(), photo=SimpleNamespace(id=123), document=None, action=None,
                is_private=True, is_group=False, sender=None, chat=None,
                file=SimpleNamespace(mime_type='image/jpeg', size=3, duration=None))
            with patch('db.store_message') as store:
                await worker.archive(msg, recovered=True)
            self.assertEqual(store.call_args.kwargs['attachments'][0]['data'], b'jpg')
            self.assertEqual(store.call_args.kwargs['metadata']['direction'], 'out' if outgoing else 'in')
        self.assertEqual(worker.client.download_media.await_count, 2)

    async def test_oversized_media_never_downloaded(self):
        worker = telegram_account.TelegramAccountWorker()
        worker.client = SimpleNamespace(download_media=AsyncMock())
        msg = SimpleNamespace(file=SimpleNamespace(mime_type='audio/ogg', size=media_text.MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            await worker.media_attachments(msg, 'source')
        worker.client.download_media.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()