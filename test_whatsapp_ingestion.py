"""Offline ingestion regression tests: no WhatsApp sends, DB or AI calls."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import main
from connectors.whatsapp import WhatsAppBridge, NODE_BRIDGE


class IngestionTests(unittest.TestCase):
    def setUp(self):
        switch = patch('main.flows.enabled', return_value=True)
        switch.start()
        self.addCleanup(switch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = patch('config.DATA_DIR', Path(self.tmp.name))
        self.cfg.start()
        self.addCleanup(self.cfg.stop)
        self.bridge = WhatsAppBridge()
        store = patch('main.db.store_message', return_value=42)
        self.store = store.start()
        self.addCleanup(store.stop)

    def put(self, id='a', **extra):
        m = dict(id=id, timestamp=self.bridge.history_cutoff().timestamp() + 60,
                 chat_id='group@g.us', author='contact@c.us',
                 **{'from': 'contact@c.us'}, from_name='Group', text='hello',
                 enriched=True, attachments=[])
        m.update(extra)
        (self.bridge.spool_dir / (id.encode().hex() + '.json')).write_text(json.dumps(m))
        return m

    def test_durable_until_ack_restart_and_cutoff(self):
        self.put()
        self.put('old', timestamp=self.bridge.history_cutoff().timestamp() - 1)
        self.put('status', chat_id='status@broadcast')
        self.assertEqual([m['id'] for m in self.bridge.fetch_new_messages()], ['a'])
        restarted = WhatsAppBridge()
        self.assertEqual(restarted.fetch_new_messages()[0]['id'], 'a')
        self.assertIn('+', restarted.fetch_new_messages()[0]['msg_date'])
        restarted.ack('a')
        self.assertEqual(restarted.fetch_new_messages(), [])

    @patch('main.db.communication_seen', return_value=False)
    @patch('main.process_message')
    def test_failure_does_not_drop_rest_and_retries(self, process, seen):
        self.put('a')
        self.put('b')
        process.side_effect = [RuntimeError('mock'), {}]
        main.check_whatsapp({}, self.bridge)
        self.assertEqual(process.call_count, 2)
        self.assertEqual([m['id'] for m in self.bridge.fetch_new_messages()], ['a'])
        process.side_effect = None
        main.check_whatsapp({}, self.bridge)
        self.assertEqual(self.bridge.fetch_new_messages(), [])
        self.assertEqual(process.call_args.kwargs['source_id'], 'a')

    @patch('main.db.communication_seen', return_value=False)
    @patch('main.process_message')
    def test_sender_label_names_not_ids(self, process, seen):
        # gruppo in entrata: CHI ha scritto e DOVE, non solo il nome del gruppo
        self.put(author_name='Marco')
        main.check_whatsapp({}, self.bridge)
        self.assertEqual(process.call_args.args[1], 'Marco in Group')
        self.assertEqual(process.call_args.args[2], 'WhatsApp · Group')
        # gruppo senza nome autore: fallback con ID ma gruppo sempre presente
        self.put('b', author_name=None)
        main.check_whatsapp({}, self.bridge)
        self.assertEqual(process.call_args.args[1], 'Group · contact@c.us')
        # uscita: destinazione leggibile
        self.put('c', from_me=True)
        with patch('main.db.log_communication') as log:
            main.check_whatsapp({}, self.bridge)
        self.assertEqual(log.call_args.args[1], 'io → Group')
        # privata in entrata: nome e numero
        self.put('d', chat_id='39333@c.us', **{'from': '39333@c.us'}, from_name='Anna')
        main.check_whatsapp({}, self.bridge)
        self.assertEqual(process.call_args.args[1], 'Anna (39333@c.us)')

    def test_unenriched_text_waits_for_attachments(self):
        self.put(enriched=False)
        self.bridge.proc = Mock()
        self.bridge.proc.poll.return_value = None
        self.assertEqual(self.bridge.fetch_new_messages(), [])

    @patch('main.db.communication_seen', return_value=False)
    @patch('main.process_message')
    def test_cached_recovery_queues_analysis(self, process, seen):
        self.put(recovered_cached=True)
        main.check_whatsapp({}, self.bridge)
        self.store.assert_called_once()
        process.assert_called_once()
        self.assertEqual(self.bridge.fetch_new_messages(), [])

    @patch('main.brain.chat_with_user', side_effect=AssertionError('AI dialogue forbidden'))
    @patch('main.db.log_communication')
    @patch('main.db.communication_seen', return_value=False)
    def test_outgoing_archived_no_dialogue(self, seen, log, chat):
        self.put(from_me=True)
        main.check_whatsapp({}, self.bridge)
        self.assertEqual(log.call_args.kwargs['source_id'], 'a')
        chat.assert_not_called()
        self.assertFalse(main._is_from_user('whatsapp', 'owner', True))

    @patch('main.process_message')
    @patch('main.db.communication_seen', return_value=True)
    def test_durable_duplicate_skips_ai(self, seen, process):
        self.put()
        main.check_whatsapp({}, self.bridge)
        process.assert_not_called()
        self.store.assert_called_once()
        self.assertEqual(self.bridge.fetch_new_messages(), [])

    def test_node_error_fallback_events_and_cached_history(self):
        # Execute the actual embedded bridge in a VM with all network/fs mocked.
        harness = r'''
const vm = require('vm');
let files = {}, events = {}, emitted = [];
class Client {
 constructor() { this.info = {wid:{_serialized:'self'}}; }
 on(k, fn) {events[k] = fn;}
 initialize() {return Promise.resolve();}
 async getChats() { return [{id:{_serialized:'g@g.us'}, name:'Group',
   fetchMessages: async options => {
    if (options.limit !== 200) throw Error('unbounded history requested');
     return [message('history')];
   }}]; }
}
function message(id) {return {id:{_serialized:id, id}, timestamp:200,
 from:'g@g.us', author:'person@c.us', body:'text', hasMedia:true,
 getChat:async()=>{throw Error('chat failed');},
 getContact:async()=>({pushname:'Marco', name:null}),
 downloadMedia:async()=>{throw Error('media failed');}};}
const fs = {mkdirSync(){},existsSync:p=>p in files,writeFileSync:(p,v)=>files[p]=v,
 renameSync:(a,b)=>{files[b]=files[a];delete files[a];}};
const context = {Buffer, process:{on(){}, env:{WA_SPOOL_DIR:'/spool',
 WA_MEDIA_DIR:'/media', WA_HISTORY_CUTOFF:'100'},stdin:{}},
 console:{log:v=>emitted.push(JSON.parse(v))},
 setTimeout:()=>({unref(){}}),setInterval:()=>({unref(){}}),
 require:n=> n==='whatsapp-web.js'?{Client,LocalAuth:class{}}:
 n==='fs'?fs:n==='path'?require('path'):n==='crypto'?require('crypto'):
 n==='readline'?{createInterface:()=>({on(){}})}:{} };
vm.createContext(context);
vm.runInContext(JSON.parse(require('fs').readFileSync(0,'utf8')), context);
(async()=>{
 if (!events.message_create || events.message) throw Error('duplicate event');
 await events.message_create(message('live'));
 await events.message_create({...message('native'),id:{$1:'native',id:'native'}});
 const native=JSON.parse(files['/spool/'+Buffer.from('native').toString('hex')+'.json']);
 if(native.id!=='native') throw Error('native ID rejected');
 // nel flusso reale ready→recover() popola la mappa dei nomi PRIMA che
 // arrivino i messaggi live successivi
 await vm.runInContext('ready = true; recover()', context);
 await events.message_create(message('live2'));
 for (const id of ['live','history','live2']) {
  const m=JSON.parse(files['/spool/'+Buffer.from(id).toString('hex')+'.json']);
  if(m.text!=='text'||!m.media_error||!m.enriched||m.author!=='person@c.us')
   throw Error('message dropped');
  if(m.author_name!=='Marco') throw Error('author name not resolved');
  if(id!=='live' && m.from_name!=='Group')
   throw Error('chat name fallback failed: '+m.from_name);
 }
 const before=Object.keys(files).length;
 await events.message_create({...message('status'),from:'status@broadcast'});
 await events.message_create({...message('old'),timestamp:99});
 if(Object.keys(files).length!==before) throw Error('filter failed');
 console.log('mock Node bridge: PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', harness], input=json.dumps(NODE_BRIDGE),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()