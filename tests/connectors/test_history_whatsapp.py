"""Offline one-shot WhatsApp history contracts. Never opens a real session."""
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from secretary.core import db
from secretary.connectors import whatsapp as wa
from secretary.connectors.storage import FileLock


START = datetime(2020, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(days=1)
REAL_POPEN = subprocess.Popen


class WhatsAppHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'wa_session' / 'session').mkdir(parents=True)
        self.spool = self.root / 'wa_history_pending'
        self.spool.mkdir()
        self.proc = Mock(stdin=io.StringIO(), stdout=io.StringIO(
            'diagnostic\n{"type":"ready","self":"me"}\n'
            '{"type":"history_complete"}\n'))
        for p in (patch.object(wa.config, 'DATA_DIR', self.root),
                  patch.object(wa, 'bridge_path', return_value=Path('/offline.js')),
                  patch.object(wa, 'bridge_env', return_value={}),
                  patch.object(wa.subprocess, 'Popen', return_value=self.proc),
                  patch.object(wa, 'stop_process'),
                  patch.object(db, 'store_message', return_value=1)):
            mock = p.start()
            self.addCleanup(p.stop)
            if p.attribute == 'Popen':
                self.spawn = mock
            elif p.attribute == 'stop_process':
                self.stop = mock
            elif p.attribute == 'store_message':
                self.store = mock

    def put(self, identity='a', **changes):
        entry = dict(id=identity, timestamp=START.timestamp(), enriched=True,
                     version='v1', self='me', chat_id='group@g.us',
                     author='person@c.us', from_name='Group', author_name='Marco',
                     from_me=False, text='history', attachments=[{'path': '/offline.pdf'}])
        entry['from'] = 'group@g.us'
        entry.update(changes)
        path = self.spool / (identity.encode().hex() + '.json')
        path.write_text(json.dumps(entry))
        return path

    def test_return_count_exact_bounds_metadata_labels_and_separate_spool(self):
        self.put()
        self.put('out', from_me=True)
        self.put('private', chat_id='person@c.us', from_name='Anna',
                 **{'from': 'person@c.us'})
        self.put('fallback', author_name=None)
        for i, changes in enumerate((dict(timestamp=END.timestamp()),
                dict(timestamp=START.timestamp()-1), dict(enriched=False),
                dict(chat_id='status@broadcast'))):
            self.put(str(i), **changes)
            foreign = self.put('foreign')
            entry = json.loads(foreign.read_text())
            entry['self'] = 'another'
            foreign.write_text(json.dumps(entry))
        normal = self.root / 'wa_pending'
        normal.mkdir()
        unrelated = normal / 'unrelated.json'
        unrelated.write_text('not consumed')
        self.assertEqual(wa.import_history(START, END), 4)
        self.assertEqual(unrelated.read_text(), 'not consumed')
        self.assertEqual(len(list(self.spool.glob('*.json'))), 5)
        calls = {c.kwargs['source_id']: c for c in self.store.call_args_list}
        self.assertEqual(calls['a'].args, ('whatsapp', 'Marco in Group', 'WhatsApp · Group', 'history'))
        self.assertEqual(calls['out'].args[1], 'io → Group')
        self.assertEqual(calls['private'].args[1], 'Anna (person@c.us)')
        self.assertEqual(calls['fallback'].args[1], 'Group · person@c.us')
        self.assertEqual(calls['a'].kwargs['metadata']['account'], 'me')
        self.assertEqual(calls['out'].kwargs['metadata']['direction'], 'out')
        self.assertEqual(calls['a'].kwargs['attachments'], [{'path': '/offline.pdf'}])
        self.assertEqual(calls['a'].kwargs['ts'], START.isoformat())
        env = self.spawn.call_args.kwargs['env']
        self.assertEqual(env['WA_HISTORY_IMPORT'], '1')
        self.assertEqual(env['WA_SHOW_QR'], '0')
        self.assertEqual(env['WA_SPOOL_DIR'], str(self.spool))
        self.stop.assert_called_once_with(self.proc)

    def test_no_message_cap(self):
        for i in range(205):
            self.put(str(i))
        self.assertEqual(wa.import_history(START, END), 205)

    def test_store_failure_retains_spool_releases_ownership(self):
        path = self.put()
        self.store.side_effect = RuntimeError('DB offline')
        with self.assertRaisesRegex(RuntimeError, 'DB offline'):
            wa.import_history(START, END)
        self.assertTrue(path.exists())
        self.stop.assert_called_once()
        with FileLock(self.root / 'wa_session' / '.owner.lock', blocking=False):
            pass

    def test_ack_does_not_delete_replaced_or_unversioned_entry(self):
        path = self.put()
        def replace(*args, **kwargs):
            self.put(version='v2')
        self.store.side_effect = replace
        self.assertEqual(wa.import_history(START, END), 1)
        self.assertEqual(json.loads(path.read_text())['version'], 'v2')

    def test_missing_busy_and_invalid_never_spawn(self):
        for start, end, timeout in ((START.replace(tzinfo=None), END, 600),
                                   (END, START, 600), (START, END, 0),
                                   (START, END, float('inf'))):
            with self.assertRaises(ValueError):
                wa.import_history(start, end, timeout)
        with FileLock(self.root / 'wa_session' / '.owner.lock', blocking=False):
            with self.assertRaises(OSError):
                wa.import_history(START, END)
        (self.root / 'wa_session' / 'session').rmdir()
        with self.assertRaises(FileNotFoundError):
            wa.import_history(START, END)
        self.spawn.assert_not_called()

    def test_error_events_eof_and_authentication_propagate(self):
        for event, error in (({'type':'history_error', 'error':'fetch failed'}, RuntimeError),
                             ({'type':'reauth_required'}, PermissionError),
                             ({'type':'disconnected'}, RuntimeError),
                             ({'type':'history_complete'}, RuntimeError),
                             ({}, RuntimeError)):
            self.proc.stdout = io.StringIO(json.dumps(event) + '\n')
            with self.subTest(event=event), self.assertRaises(error):
                wa.import_history(START, END)
        self.store.assert_not_called()
        self.assertEqual(self.stop.call_count, 5)

    def test_silent_child_timeout_and_cleanup(self):
        release = threading.Event()
        class Silent:
            def __iter__(self):
                release.wait(2)
                return iter(())
            def close(self):
                pass
        self.proc.stdout = Silent()
        self.stop.side_effect = lambda proc: release.set()
        with self.assertRaises(TimeoutError):
            wa.import_history(START, END, timeout=0.03)
        self.stop.assert_called_once()
        with FileLock(self.root / 'wa_session' / '.owner.lock', blocking=False):
            pass

    def test_partial_completion_drains_and_sanitizes(self):
        path = self.put()
        self.proc.stdout = io.StringIO(
            '{"type":"ready","self":"me"}\n' + json.dumps({
                'type': 'history_complete', 'partial': True,
                'errors': {'fetch_timeout': 2, 'secret phone/token': 1}}) + '\n')
        with self.assertRaises(wa.WhatsAppHistoryPartialError) as caught:
            wa.import_history(START, END)
        self.assertEqual(caught.exception.stored_count, 1)
        self.assertEqual(caught.exception.reasons, ('fetch_timeout', 'fetch_failed'))
        self.assertNotIn('secret', str(caught.exception))
        self.assertFalse(path.exists())
        self.stop.assert_called_once()

    def test_fatal_and_eof_after_ready_still_drain(self):
        for suffix in ('', '{"type":"history_error","error":"private token"}\n'):
            with self.subTest(suffix=suffix):
                path = self.put()
                self.proc.stdout = io.StringIO('{"type":"ready","self":"me"}\n' + suffix)
                with self.assertRaises(wa.WhatsAppHistoryPartialError) as caught:
                    wa.import_history(START, END)
                self.assertEqual(caught.exception.stored_count, 1)
                self.assertNotIn('private', str(caught.exception))
                self.assertFalse(path.exists())

    def test_timeout_after_ready_drains_before_reporting(self):
        path = self.put()
        release = threading.Event()
        class SilentAfterReady:
            def __iter__(self):
                yield '{"type":"ready","self":"me"}\n'
                release.wait(2)
            def close(self):
                pass
        self.proc.stdout = SilentAfterReady()
        self.stop.side_effect = lambda proc: release.set()
        with self.assertRaises(TimeoutError) as caught:
            wa.import_history(START, END, timeout=0.03)
        self.assertEqual(caught.exception.stored_count, 1)
        self.assertFalse(path.exists())

    def test_spawn_error_releases_lock(self):
        self.spawn.side_effect = OSError('node unavailable')
        with self.assertRaises(OSError):
            wa.import_history(START, END)
        with FileLock(self.root / 'wa_session' / '.owner.lock', blocking=False):
            pass

    def test_real_node_bridge_in_offline_vm(self):
        harness = r'''
const vm=require('vm'), events={}, files={}, output=[];
let fetched=0, mode='normal', requests=[];
function message(id, timestamp=100) {return {id:{_serialized:id},timestamp,
 from:'g@g.us',author:'p@c.us',body:'text',hasMedia:false,
 getChat:async()=>({name:'Group'}),getContact:async()=>({pushname:'Marco'})};}
class Client {
 constructor(){this.info={wid:{_serialized:'me'}};}
 on(k,v){events[k]=v;}
 initialize(){return Promise.resolve();}
 async getChats(){return Array.from({length:105},(_,i)=>({
  id:{_serialized:'g@g.us'},name:'Group',fetchMessages:async options=>{
    if(!Number.isFinite(options.limit)||options.limit>12800) throw Error('unbounded fetch');
    requests.push(options.limit);
    if(mode==='fail' && i===0) throw Error('private phone/token');
    if(mode==='browser' && i===0) throw Error('Protocol error secret');
    if(mode==='cap' && i===0) return Array.from({length:options.limit},(_,j)=>message('cap'+j,150));
    if(mode==='growth' && i===0) return Array.from({length:options.limit},(_,j)=>message('grow'+j,j===399?99:150));
    if(mode==='stalled' && i===0) return Array.from({length:options.limit},(_,j)=>message('same'+(j%200),150));
    if(mode==='archive' && i===0) return [{...message('badmedia',150),hasMedia:true,
     downloadMedia:async()=>{throw Error('private media URL');}}];
   fetched++;return [message('m'+i),message('end'+i,200),message('old'+i,99)];
  }}));}
 async sendMessage(){throw Error('send forbidden');}
}
const context={Buffer,process:{env:{WA_HISTORY_IMPORT:'1',WA_SPOOL_DIR:'/history',
 WA_HISTORY_CUTOFF:'100',WA_HISTORY_END:'200',WA_MEDIA_DIR:'/media'},on(){},stdin:{}},
 console:{log:s=>output.push(JSON.parse(s))},setInterval(){throw Error('live timer');},
 require:n=>n==='whatsapp-web.js'?{Client,LocalAuth:class{}}:n==='fs'?{
 mkdirSync(){},existsSync:p=>p in files,readFileSync:p=>files[p],
 writeFileSync:(p,v)=>files[p]=v,renameSync:(a,b)=>{files[b]=files[a];delete files[a];}
 }:n==='readline'?{createInterface:()=>({on(){}})}:require(n)};
vm.createContext(context);
vm.runInContext(JSON.parse(require('fs').readFileSync(0,'utf8')),context);
(async()=>{
 if(events.message_create||events.message) throw Error('live listener');
 await events.ready();
 if(fetched!==105||Object.keys(files).length!==105) throw Error('history cap/bounds');
 if(output.at(-1).type!=='history_complete') throw Error('missing completion');
 const entry=JSON.parse(Object.values(files)[0]);
 if(!entry.enriched||entry.self!=='me') throw Error('bad enrichment/account');
 for(const scenario of ['fail','browser','cap','growth','stalled','archive']) {
  mode=scenario;requests=[];fetched=0;await events.ready();
  const last=output.at(-1);
  if(last.type!=='history_complete') throw Error('missing partial completion');
  const partial=['fail','browser','cap','archive'].includes(mode);
  if(last.partial!==partial) throw Error('wrong partial flag '+mode);
  if(fetched!==104) throw Error('later chats skipped '+mode);
  if(mode==='cap' && !last.errors.history_cap_reached) throw Error('missing cap reason');
  if(mode==='browser' && !last.errors.browser_unavailable) throw Error('missing browser reason');
  if(mode==='archive' && !last.errors.archive_failed) throw Error('missing media reason');
  if(mode==='growth' && requests.slice(0,2).join()!=='200,400') throw Error('no progressive fetch');
  if(mode==='stalled' && requests.length!==106) throw Error('no-growth did not stop');
 }
 if(JSON.stringify(output).includes('private')) throw Error('sensitive error leaked');
 events.qr('secret');
 if(output.at(-1).type!=='history_error') throw Error('login allowed');
 console.log('PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        with patch.object(subprocess, 'Popen', REAL_POPEN):
            result = subprocess.run(['node', '-e', harness], input=json.dumps(wa.NODE_BRIDGE),
                                    text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()