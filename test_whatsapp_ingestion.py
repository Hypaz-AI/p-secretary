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

    @patch('main.db.communication_seen', return_value=True)
    @patch('main.process_message')
    def test_raw_then_enriched_dedup_still_updates_storage(self, process, seen):
        self.put(enriched=False, version='raw')
        main.check_whatsapp({}, self.bridge)
        self.store.assert_not_called()
        self.put(enriched=True, version='enriched', author_name='Address Book',
                 author_name_rank=3, names_version=2, author_aliases=['person@lid', '123@c.us'])
        main.check_whatsapp({}, self.bridge)
        self.assertEqual(self.store.call_args.kwargs['metadata']['author_name'], 'Address Book')
        self.assertEqual(self.store.call_args.kwargs['metadata']['author_name_rank'], 3)
        process.assert_not_called()
        self.assertEqual(self.bridge.fetch_new_messages(), [])

    def test_ack_raw_or_old_version_cannot_remove_enrichment(self):
        self.put(enriched=False, version='raw')
        self.bridge.ack('a', 'raw')
        self.assertTrue(list(self.bridge.spool_dir.glob('*.json')))
        self.put(version='new')
        self.bridge.ack('a', 'raw')
        self.assertEqual(len(self.bridge.fetch_new_messages()), 1)

    @patch('main.db.communication_seen', return_value=False)
    @patch('main.process_message')
    def test_cached_recovery_queues_analysis(self, process, seen):
        self.put(recovered_cached=True)
        main.check_whatsapp({}, self.bridge)
        self.store.assert_called_once()
        process.assert_called_once()
        self.assertEqual(self.bridge.fetch_new_messages(), [])

    @patch('main.process_message')
    @patch('main.db.log_communication')
    @patch('main.db.communication_seen', return_value=False)
    def test_outgoing_archived_without_any_agent_work(self, seen, log, process):
        self.put(from_me=True)
        main.check_whatsapp({}, self.bridge)
        self.assertEqual(log.call_args.kwargs['source_id'], 'a')
        process.assert_not_called()  # niente job di analisi/dialogo per i propri messaggi in uscita

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
 async getContactLidAndPhone(ids) {return ids.map(id=>({lid:id,pn:'123@c.us'}));}
 async getContactById(id) {
     if (id==='123@c.us') return {name:'Address Book',pushname:'Push'};
     return {pushname:'Push'};
 }
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
 setTimeout:fn=>{if(typeof fn==='function')Promise.resolve().then(fn);return {unref(){}};},setInterval:()=>({unref(){}}),
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
    if(m.author_name!=='Address Book') throw Error('addressbook must beat pushname');
  if(id!=='live' && m.from_name!=='Group')
   throw Error('chat name fallback failed: '+m.from_name);
 }
 const before=Object.keys(files).length;
 await events.message_create({...message('status'),from:'status@broadcast'});
 await events.message_create({...message('old'),timestamp:99});
 if(Object.keys(files).length!==before) throw Error('filter failed');
 const privateMessage = {...message('private'),from:'person@lid',author:undefined,
     hasMedia:false,getChat:async()=>({name:'person@lid'})};
 await events.message_create(privateMessage);
 const read=id=>JSON.parse(files['/spool/'+Buffer.from(id).toString('hex')+'.json']);
 if(read('private').from_name!=='Address Book' ||
        !read('private').chat_aliases.includes('123@c.us')) throw Error('private LID resolution');
 await events.message_create({...privateMessage,id:{id:'out'},from:'self@c.us',
     to:'person@lid',fromMe:true,getContact:async()=>{throw Error('must not resolve self');}});
 if(read('out').from_name!=='Address Book') throw Error('outgoing destination resolution');
 // Durable raw exists while contact lookup is blocked; no enriched flag yet.
 let release;
 vm.runInContext('client.getContactById = async () => {throw Error("unavailable")};'+
     'client.getContactLidAndPhone = async () => {throw Error("unavailable")}',context);
 const blocked=events.message_create({...privateMessage,id:{id:'blocked'},
     getContact:()=>new Promise(resolve=>{release=resolve;})});
 await new Promise(resolve=>setImmediate(resolve));
 if(read('blocked').enriched) throw Error('raw incorrectly enriched');
 release({verifiedName:'Verified'});
 await blocked;
 if(read('blocked').from_name!=='Verified') throw Error('verified fallback');
 await events.message_create({...privateMessage,id:{id:'failed'},
     getContact:async()=>{throw Error('unavailable')}});
 if(!read('failed').enriched || read('failed').from_name!=='person@lid')
     throw Error('contact failure lost raw message');
 console.log('mock Node bridge: PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', harness], input=json.dumps(NODE_BRIDGE),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS', result.stdout)

    def run_node(self, harness):
        result = subprocess.run(['node', '-e', harness], input=json.dumps(NODE_BRIDGE),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_node_media_is_retried_tracked_and_never_silently_lost(self):
        self.run_node(r"""
const vm = require('vm');
let files = {}, events = {}, emitted = [], downloads = {}, failWrite = false;
class Client {
 constructor() { this.info = {wid:{_serialized:'self'}}; }
 on(k, fn) {events[k] = fn;}
 initialize() {return Promise.resolve();}
}
const data = Buffer.from('contenuto').toString('base64');
function message(id, behaviour) {return {id:{_serialized:id, id}, timestamp:200, from:'p@c.us', fromMe:false,
 body:'', hasMedia:true, type:'document', getChat:async()=>({name:'Anna'}), getContact:async()=>({name:'Anna'}),
 downloadMedia: async () => {downloads[id]=(downloads[id]||0)+1; return behaviour(downloads[id]);}};}
const fs = {mkdirSync(){},existsSync:p=>p in files,writeFileSync:(p,v)=>{
   if (failWrite && p.includes('/media/')) throw Error('disk full'); files[p]=v;},
 readFileSync:p=>files[p],renameSync:(a,b)=>{files[b]=files[a];delete files[a];}};
const context = {Buffer, process:{on(){}, env:{WA_SPOOL_DIR:'/spool', WA_MEDIA_DIR:'/media', WA_HISTORY_CUTOFF:'100'},stdin:{}},
 console:{log:v=>emitted.push(JSON.parse(v))},
 setTimeout:fn=>{if(typeof fn==='function')Promise.resolve().then(fn);return {unref(){}};},setInterval:()=>({unref(){}}),
 require:n=> n==='whatsapp-web.js'?{Client,LocalAuth:class{}}:
 n==='fs'?fs:n==='path'?require('path'):n==='crypto'?require('crypto'):
 n==='readline'?{createInterface:()=>({on(){}})}:{} };
vm.createContext(context);
vm.runInContext(JSON.parse(require('fs').readFileSync(0,'utf8')), context);
const read = id => JSON.parse(files['/spool/'+Buffer.from(id).toString('hex')+'.json']);
const ok = {data, mimetype:'application/pdf', filename:'fattura.pdf'};
(async()=>{
 // Il ponte riprova: undefined due volte, poi il file arriva.
 await events.message_create(message('late', n => n < 3 ? undefined : ok));
 let m = read('late');
 if (downloads.late !== 3 || m.attachments.length !== 1 || m.media_error !== false || m.has_media !== true || m.media_type !== 'document')
   throw Error('retry did not recover the media: ' + JSON.stringify(m));
 if (!files['/media/late_fattura.pdf'] || Object.keys(files).some(p => p.endsWith('.tmp'))) throw Error('media not stored atomically');
 // Mai disponibile: nessun allegato, motivo tracciato, messaggio comunque archiviato.
 await events.message_create(message('never', () => undefined));
 m = read('never');
 if (m.attachments.length || m.media_error !== 'unavailable' || !m.enriched || m.has_media !== true) throw Error('unavailable not tracked');
 // Errore di rete, contenuto vuoto, disco pieno: ognuno con il proprio motivo.
 await events.message_create(message('boom', () => {throw Error('private media URL');}));
 if (read('boom').media_error !== 'download_failed') throw Error('download error not tracked');
 await events.message_create(message('empty', () => ({data:'=', mimetype:'x', filename:'e'})));  // base64 senza byte
 if (read('empty').media_error !== 'empty') throw Error('empty not tracked');
 await events.message_create(message('blank', () => ({data:'', mimetype:'x', filename:'e'})));
 if (read('blank').media_error !== 'unavailable' || downloads.blank !== 3) throw Error('blank data must be retried as unavailable');
 failWrite = true;
 await events.message_create(message('disk', () => ok));
 failWrite = false;
 if (read('disk').media_error !== 'write_failed' || read('disk').attachments.length) throw Error('write failure not tracked');
 // Il motivo non contiene mai il testo dell'errore del browser/libreria.
 if (JSON.stringify(read('boom')).includes('private media URL')) throw Error('error text leaked');
 // Un'entry con media fallito non è definitiva: al giro successivo ci riprova e il successo cancella l'errore.
 files['/spool/'+Buffer.from('retry').toString('hex')+'.json'] = JSON.stringify({id:'retry', enriched:true, names_version:2,
   media_error:'unavailable', media_attempts:1, attachments:[]});
 await events.message_create(message('retry', () => ok));
 m = read('retry');
 if (m.attachments.length !== 1 || m.media_error !== false) throw Error('pending media entry was treated as final');
 // Dopo troppi tentativi smette di insistere (il file resta com'è).
 const exhausted = JSON.stringify({id:'tired', enriched:true, names_version:2, media_error:'unavailable', media_attempts:6, attachments:[]});
 files['/spool/'+Buffer.from('tired').toString('hex')+'.json'] = exhausted;
 await events.message_create(message('tired', () => ok));
 if (files['/spool/'+Buffer.from('tired').toString('hex')+'.json'] !== exhausted || downloads.tired) throw Error('retries not bounded');
 // Senza cartella media il fallimento è esplicito, non un messaggio senza traccia.
 console.log('PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_media_state_is_forwarded_as_closed_vocabulary_in_native_metadata(self):
        from connectors.whatsapp import media_metadata
        self.assertEqual(media_metadata({'text': 'solo testo'}), {})
        self.assertEqual(media_metadata({'has_media': True, 'media_error': False, 'media_type': 'image',
                                         'attachments': [{'filename': 'a'}]}),
                         {'has_media': True, 'media_error': False, 'media_type': 'image'})
        for reason in ('download_failed', 'unavailable', 'empty', 'write_failed', 'storage_unavailable'):
            self.assertEqual(media_metadata({'has_media': True, 'media_error': reason, 'attachments': []})['media_error'], reason)
        # Testo arbitrario del ponte non entra mai nei metadati; un allegato presente prevale sul flag.
        self.assertIs(media_metadata({'has_media': True, 'media_error': 'private URL https://x', 'attachments': []})['media_error'], True)
        self.assertIs(media_metadata({'has_media': True, 'media_error': 'unavailable', 'attachments': [{'filename': 'a'}]})['media_error'], False)
        self.assertEqual(media_metadata({'has_media': 'yes', 'media_error': True}), {})
        self.assertNotIn('media_type', media_metadata({'has_media': True, 'media_type': 'x' * 41, 'attachments': []}))

    def test_spooled_media_status_reaches_the_stored_message_and_success_clears_the_error(self):
        import message_store
        self.put(has_media=True, media_error='unavailable', media_type='document', attachments=[])
        self.assertTrue(self.bridge.fetch_new_messages())
        found = self.bridge.fetch_new_messages()[0]
        self.assertEqual((found['metadata']['has_media'], found['metadata']['media_error'],
                          found['metadata']['media_type']), (True, 'unavailable', 'document'))
        # Il merge dei metadati WhatsApp: il successo (False) sovrascrive l'errore precedente.
        merged = message_store.merge_whatsapp_metadata({'has_media': True, 'media_error': 'unavailable'},
                                                       {'has_media': True, 'media_error': False})
        self.assertIs(merged['media_error'], False)


if __name__ == '__main__':
    unittest.main()