"""Offline: chat escluse (WHATSAPP_EXCLUDED_CHATS / TELEGRAM_EXCLUDED_CHATS) bloccate a monte.

Nessuna rete, nessun Telegram/WhatsApp reale, nessun DB: il bridge Node gira in una VM con
client, fs e spool finti; i messaggi di chat escluse non devono mai toccare getChat, download,
spool, archivio o log.
"""
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from secretary.core import config, db
from secretary.connectors import telegram_account as tg
from secretary.connectors import whatsapp as wa
from secretary.dashboard import admin_config


class ChatListParsingTests(unittest.TestCase):
    def test_parser_keeps_only_wellformed_ids_and_logs_only_a_count(self):
        with patch('builtins.print') as log:
            parsed = config.parse_chat_list('WHATSAPP_EXCLUDED_CHATS',
                                            ' 120363@g.us , 39333@c.us,bad value,@x,')
            self.assertEqual(parsed, frozenset({'120363@g.us', '39333@c.us'}))
            log.assert_called_once_with('[config] WHATSAPP_EXCLUDED_CHATS: 2 voci non valide ignorate')
            self.assertEqual(config.parse_chat_list('TELEGRAM_EXCLUDED_CHATS', '-1001234567890,123,abc'),
                             frozenset({'-1001234567890', '123'}))
            self.assertEqual(config.parse_chat_list('TELEGRAM_EXCLUDED_CHATS', ''), frozenset())

    def test_admin_schema_and_validation_share_the_same_rules(self):
        fields = admin_config.schema()
        for key in ('WHATSAPP_EXCLUDED_CHATS', 'TELEGRAM_EXCLUDED_CHATS'):
            self.assertEqual(fields[key]['group'], 'Telegram e WhatsApp')
            self.assertFalse(fields[key]['secret'])
        admin_config.validate({'WHATSAPP_EXCLUDED_CHATS': '120363@g.us, 39333@c.us'}, {})
        admin_config.validate({'TELEGRAM_EXCLUDED_CHATS': '-1001234567890,123'}, {})
        admin_config.validate({'TELEGRAM_EXCLUDED_CHATS': ''}, {})
        rejected = [
            ('WHATSAPP_EXCLUDED_CHATS', 'bad value'),
            ('WHATSAPP_EXCLUDED_CHATS', '1@evil.com'),
            ('TELEGRAM_EXCLUDED_CHATS', '@user'),
            ('TELEGRAM_EXCLUDED_CHATS', ','.join(['1'] * 201)),
        ]
        for key, value in rejected:
            with self.subTest(key=key, value=value[:20]), self.assertRaises(ValueError):
                admin_config.validate({key: value}, {})


class WhatsAppSpoolExclusionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for p in (patch.object(config, 'DATA_DIR', Path(directory.name)),
                  patch.object(config, 'WHATSAPP_EXCLUDED_CHATS', frozenset({'120363@g.us'}))):
            p.start()
            self.addCleanup(p.stop)
        self.bridge = wa.WhatsAppBridge()

    def spool(self, identity, chat_id):
        entry = {'id': identity, 'timestamp': self.bridge.history_cutoff().timestamp() + 60,
                 'chat_id': chat_id, 'from': chat_id, 'author': 'p@c.us', 'from_name': 'Gruppo',
                 'text': 'segreto ' + identity, 'enriched': True, 'attachments': []}
        (self.bridge.spool_dir / (identity.encode().hex() + '.json')).write_text(json.dumps(entry))

    def test_live_spool_of_excluded_chat_is_deleted_and_never_returned(self):
        self.spool('ex', '120363@g.us')
        self.spool('ok', '777@g.us')
        self.assertEqual([m['id'] for m in self.bridge.fetch_new_messages()], ['ok'])
        self.assertFalse((self.bridge.spool_dir / ('ex'.encode().hex() + '.json')).exists())

    def test_exclusions_reach_the_node_bridge_environment(self):
        self.assertEqual(wa.bridge_env()['WA_EXCLUDED_CHATS'], '120363@g.us')


class WhatsAppHistoryExclusionTests(unittest.TestCase):
    """import_history: stesso contratto di test_history_whatsapp, con una chat esclusa."""

    START = datetime(2020, 1, 1, tzinfo=timezone.utc)
    END = START + timedelta(days=1)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'wa_session' / 'session').mkdir(parents=True)
        self.spool = self.root / 'wa_history_pending'
        self.spool.mkdir()
        self.proc = Mock(stdin=io.StringIO(), stdout=io.StringIO(
            '{"type":"ready","self":"me"}\n{"type":"history_complete"}\n'))
        for p in (patch.object(wa.config, 'DATA_DIR', self.root),
                  patch.object(wa.config, 'WHATSAPP_EXCLUDED_CHATS', frozenset({'ex@g.us'})),
                  patch.object(wa, 'bridge_path', return_value=Path('/offline.js')),
                  patch.object(wa, 'bridge_env', return_value={}),
                  patch.object(wa.subprocess, 'Popen', return_value=self.proc),
                  patch.object(wa, 'stop_process'),
                  patch.object(db, 'store_message', return_value=1)):
            mock = p.start()
            self.addCleanup(p.stop)
            if p.attribute == 'store_message':
                self.store = mock

    def put(self, identity, chat_id):
        entry = {'id': identity, 'timestamp': self.START.timestamp() + 3600, 'enriched': True,
                 'version': 'v1', 'self': 'me', 'chat_id': chat_id, 'from': chat_id,
                 'from_name': 'Chat', 'from_me': False, 'text': 'segreto ' + identity,
                 'attachments': []}
        path = self.spool / (identity.encode().hex() + '.json')
        path.write_text(json.dumps(entry))
        return path

    def test_excluded_history_is_removed_and_not_archived(self):
        excluded = self.put('ex', 'ex@g.us')
        self.put('ok', 'ok@g.us')
        self.assertEqual(wa.import_history(self.START, self.END), 1)
        self.assertEqual([c.kwargs['source_id'] for c in self.store.call_args_list], ['ok'])
        self.assertFalse(excluded.exists())


class TelegramExclusionTests(unittest.IsolatedAsyncioTestCase):
    def message(self, **changes):
        m = dict(id=42, chat_id=-100123, sender_id=8, out=False,
                 date=datetime.now(timezone.utc), edit_date=None, raw_text='segreto',
                 media=None, action=None, is_private=False, is_group=False,
                 sender=None, chat=None)
        m.update(changes)
        return SimpleNamespace(**m)

    def setUp(self):
        for name, kwargs in (('secretary.core.recovery.scan_start',
                              {'return_value': datetime(2000, 1, 1, tzinfo=timezone.utc)}),
                             ('secretary.core.recovery.completed', {})):
            p = patch(name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(config, 'TELEGRAM_EXCLUDED_CHATS', frozenset({'-100123'}))
        p.start()
        self.addCleanup(p.stop)
        self.worker = tg.TelegramAccountWorker()
        self.worker.account = 'user:7'
        self.worker.client = Mock(download_media=AsyncMock())

    async def test_excluded_message_never_reaches_lookup_download_or_store(self):
        media = self.message(media=object(), file=SimpleNamespace(mime_type='image/jpeg', size=1))
        media.get_sender = AsyncMock()
        media.get_chat = AsyncMock()
        with patch.object(tg.db, 'store_message') as store:
            self.assertFalse(await self.worker.archive(media))
            self.assertFalse(await self.worker.archive(self.message(out=True)))
            await self.worker._on_message(SimpleNamespace(message=self.message()))
        store.assert_not_called()
        media.get_sender.assert_not_awaited()
        media.get_chat.assert_not_awaited()
        self.worker.client.download_media.assert_not_awaited()

    async def test_other_chats_are_still_archived(self):
        with patch.object(tg.db, 'store_message', return_value=1) as store:
            self.assertTrue(await self.worker.archive(self.message(chat_id=-100999)))
        store.assert_called_once()
        self.assertEqual(store.call_args.kwargs['metadata']['chat_id'], '-100999')

    async def test_recovery_never_requests_messages_of_excluded_dialog(self):
        requested = []

        async def dialogs(**kwargs):
            yield SimpleNamespace(id=-100123, entity='excluded')
            yield SimpleNamespace(id=-100999, entity='allowed')

        async def messages(entity, **kwargs):
            requested.append(entity)
            for _ in ():
                yield None

        self.worker.client = SimpleNamespace(iter_dialogs=dialogs, iter_messages=messages)
        # _recover_pass importa FloodWaitError: un modulo finto evita di dipendere da Telethon.
        errors = SimpleNamespace(FloodWaitError=type('FloodWaitError', (Exception,), {}))
        fake = {'telethon': SimpleNamespace(errors=errors), 'telethon.errors': errors}
        with patch.dict('sys.modules', fake):
            await self.worker._recover_pass()
        self.assertEqual(requested, ['allowed'])


class NodeBridgeExclusionTests(unittest.TestCase):
    def test_node_bridge_drops_excluded_chats_before_any_read_spool_or_log(self):
        harness = r'''
const vm = require('vm');
const outer = console;
let files = {}, events = {}, emitted = [], chatCalls = [], mediaCalls = 0, fetched = [];
class Client {
 constructor() { this.info = {wid:{_serialized:'self'}}; }
 on(k, fn) { events[k] = fn; }
 initialize() { return Promise.resolve(); }
 async getChats() {
   const chat = (id, name) => ({id:{_serialized:id}, name,
     fetchMessages: async () => { fetched.push(id); return []; }});
   return [chat('ex@g.us', 'Escluso'), chat('ok@g.us', 'Ok')];
 }
}
function message(id, chat, extra) {
 return Object.assign({id:{_serialized:id, id}, timestamp:200, from:chat, fromMe:false,
   author:'p@c.us', body:'segreto '+id, hasMedia:true,
   getChat: async () => { chatCalls.push(id); return {name:'Chat'}; },
   getContact: async () => { chatCalls.push('contact ' + id); return {name:'Persona'}; },
   downloadMedia: async () => { mediaCalls++;
     return {data: Buffer.from('doc').toString('base64'), mimetype:'application/pdf', filename:'a.pdf'}; }
 }, extra);
}
const fs = {mkdirSync(){}, existsSync: p => p in files, readFileSync: p => files[p],
 writeFileSync: (p, v) => { files[p] = v; },
 renameSync: (a, b) => { files[b] = files[a]; delete files[a]; }};
const context = {Buffer, process: {on(){}, stdin: {}, env: {WA_SPOOL_DIR:'/spool', WA_MEDIA_DIR:'/media',
   WA_HISTORY_CUTOFF:'100', WA_EXCLUDED_CHATS:'ex@g.us'}},
 console: {log: v => emitted.push(String(v)), error: v => emitted.push(String(v))},
 setTimeout: fn => { if (typeof fn === 'function') Promise.resolve().then(fn); return {unref(){}}; },
 setInterval: () => ({unref(){}}),
 require: n => n === 'whatsapp-web.js' ? {Client, LocalAuth: class {}} :
   n === 'fs' ? fs : n === 'path' ? require('path') : n === 'crypto' ? require('crypto') :
   n === 'readline' ? {createInterface: () => ({on(){}})} : {}};
vm.createContext(context);
vm.runInContext(JSON.parse(require('fs').readFileSync(0, 'utf8')), context);
const key = id => '/spool/' + Buffer.from(id).toString('hex') + '.json';
(async () => {
 // in ingresso e in uscita dalla chat esclusa: nessuna lettura, nessun download, nessuno spool
 await events.message_create(message('ex-in', 'ex@g.us'));
 await events.message_create(message('ex-out', 'ex@g.us', {fromMe:true, from:'self@c.us', to:'ex@g.us'}));
 if (Object.keys(files).length) throw Error('excluded message spooled');
 if (chatCalls.length || mediaCalls) throw Error('excluded message was read or downloaded');
 // chat non esclusa: archiviata normalmente
 await events.message_create(message('ok-in', 'ok@g.us'));
 if (!files[key('ok-in')]) throw Error('allowed message dropped');
 if (mediaCalls !== 1) throw Error('allowed media not downloaded');
 // recupero: la chat esclusa non viene nemmeno interrogata
 await vm.runInContext('ready = true; recover()', context);
 if (fetched.join() !== 'ok@g.us') throw Error('excluded chat fetched during recovery: ' + fetched.join());
 if (emitted.some(line => line.includes('segreto ex'))) throw Error('excluded text logged');
 outer.log('excluded chats: PASS');
})().catch(e => { outer.error(e); process.exitCode = 1; });
'''
        result = subprocess.run(['node', '-e', harness], input=json.dumps(wa.NODE_BRIDGE),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()
