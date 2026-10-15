"""Connettore WhatsApp: ponte via whatsapp-web.js (Node) controllato da Python.

Alla prima connessione viene mostrato un QR nel terminale da scansionare
con WhatsApp -> Dispositivi collegati. La sessione viene salvata in
`data/wa_session` e riutilizzata.
"""
import json
import subprocess
import threading
from pathlib import Path

import config

# Script Node.js che fa da ponte: legge comandi JSON da stdin,
# scrive eventi JSON su stdout.
NODE_BRIDGE = r"""
const { Client, LocalAuth } = require('whatsapp-web.js');
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const AUTH_ONLY = process.env.WA_AUTH_ONLY === '1';
const HISTORY_IMPORT = process.env.WA_HISTORY_IMPORT === '1';
function historyError(error) {
  console.log(JSON.stringify({type:'history_error', error:String(error)}));
}

const MEDIA_DIR = process.env.WA_MEDIA_DIR;
if (MEDIA_DIR) fs.mkdirSync(MEDIA_DIR, { recursive: true });

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: process.env.WA_SESSION_DIR }),
  puppeteer: {
    // Su Raspberry/ARM Chrome for Testing non esiste: usa Chromium di sistema
    executablePath: process.env.WA_CHROMIUM_PATH || undefined,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
  },
});

let ready = false;
const SPOOL = process.env.WA_SPOOL_DIR;
if (SPOOL) fs.mkdirSync(SPOOL, {recursive:true});
// Mappa chat_id -> nome conversazione: getChats() ha i titoli anche quando
// getChatById non li ha ancora risolti (chat mai aperte nella UI web).
const CHAT_NAMES = {};
const INFLIGHT = new Set();
function rememberChat(chat) {
  if (chat && chat.id && (chat.name || chat.formattedTitle))
    CHAT_NAMES[chat.id._serialized || chat.id] = chat.name || chat.formattedTitle;
}
function cutoff() {
  return Math.max(Number(process.env.WA_HISTORY_CUTOFF || 0), 0);
}
async function archive(msg, recovered_cached=false) {
  if (AUTH_ONLY || !SPOOL) return;
  const id = msg.id && (msg.id._serialized || msg.id.id);
  if (!id || msg.from==='status@broadcast' || msg.timestamp < cutoff() ||
      (HISTORY_IMPORT && msg.timestamp >= Number(process.env.WA_HISTORY_END))) return;
  if (INFLIGHT.has(id)) return;
  const finalPath = path.join(SPOOL, Buffer.from(id).toString('hex')+'.json');
  if (fs.existsSync(finalPath)) {
    if (!HISTORY_IMPORT || JSON.parse(fs.readFileSync(finalPath,'utf8')).enriched) return;
  }
  INFLIGHT.add(id);
  try {
  const entry = {id, timestamp:msg.timestamp, chat_id:msg.fromMe?msg.to:msg.from,
    author:msg.author || msg.from, from:msg.from, from_name:msg.from,
    author_name:null, from_me:!!msg.fromMe, text:msg.body || '', attachments:[], recovered_cached,
    enriched:false, version:require('crypto').randomUUID()};
  if (HISTORY_IMPORT) entry.self = client.info.wid._serialized;
  const dest = path.join(SPOOL, Buffer.from(id).toString('hex')+'.json');
  const persist = () => {fs.writeFileSync(dest+'.tmp', JSON.stringify(entry));fs.renameSync(dest+'.tmp', dest);};
  persist();
  try {const chat=await msg.getChat();rememberChat(chat);
    entry.from_name=(chat && (chat.name || chat.formattedTitle)) || CHAT_NAMES[entry.chat_id] || msg.from;
  } catch(e) {entry.chat_error=true;
    entry.from_name=CHAT_NAMES[entry.chat_id] || msg.from;}
  if(!msg.fromMe && msg.author && msg.author !== msg.from) {
    // nei gruppi chat.name è il nome del GRUPPO: risolvi chi ha scritto
    // il singolo messaggio dal suo contatto (pushname o nome rubrica)
    try {const contact=await msg.getContact();
      entry.author_name=(contact && (contact.pushname || contact.name)) || null;
    } catch(e) {entry.contact_error=true;}
  }
  if(msg.hasMedia && MEDIA_DIR) {
    try {
      const media=await msg.downloadMedia();
      const safe=(id+'_'+(media.filename||'file')).replace(/[^\w.-]/g,'_');
      const target=path.join(MEDIA_DIR,safe);
      fs.writeFileSync(target,Buffer.from(media.data,'base64'));
      entry.attachments.push({filename:media.filename||safe,content_type:media.mimetype,path:target});
    } catch(e) {entry.media_error=true;if(HISTORY_IMPORT) throw e;}
  }
  entry.enriched=true;persist();
  } finally { INFLIGHT.delete(id); }
}
async function recover() {
  if(!ready || AUTH_ONLY) return;
  try {
    const chats=await client.getChats();
    // prima riempi la mappa dei nomi (rapida), poi il fetch lento
    for(const chat of chats.slice(0,100)) rememberChat(chat);
    for(const chat of chats.slice(0,100)) {
      try {for(const msg of await chat.fetchMessages({limit:200})) await archive(msg,true);} catch(e) {}
    }
  } catch(e) {}
}
if (!HISTORY_IMPORT) {
  client.on('message_create', msg => archive(msg));
  setInterval(recover,900000).unref();
}

client.on('qr', (qr) => {
  if (HISTORY_IMPORT) {
    historyError('Existing WhatsApp session requires authentication');
    return;
  }
  if (process.env.WA_SHOW_QR !== '1') {
    console.log(JSON.stringify({type:'reauth_required'}));
    return;
  }
  console.log(JSON.stringify({ type: 'qr', qr }));
  require('qrcode-terminal').generate(qr, { small: true });
});

client.on('ready', async () => {
  ready = true;
  console.log(JSON.stringify({ type: 'ready', self: client.info && client.info.wid ? client.info.wid._serialized : null }));
  if (!HISTORY_IMPORT) { recover(); return; }
  try {
    const chats = await client.getChats();
    for (const chat of chats) rememberChat(chat);
    for (const chat of chats) {
      // Only history already available to this linked device; no server backfill.
      for (const msg of await chat.fetchMessages({limit:Infinity})) await archive(msg,true);
    }
    console.log(JSON.stringify({type:'history_complete'}));
  } catch (e) { historyError(e); }
});
if (HISTORY_IMPORT) client.on('auth_failure', historyError);

/* Single message_create handler covers incoming and outgoing messages.
client.on('message', async (msg) => {
  try {
    const chat = await msg.getChat();
    const selfWid = client.info && client.info.wid ? client.info.wid._serialized : '';
    // from del mittente: per chat private è il numero;
    // msg.fromMe = inviata da un dispositivo collegato a questo account
    const entry = {
      type: 'message',
      from: msg.from,
      from_name: chat.name || msg.from,
      from_me: !!msg.fromMe,
      self: selfWid,
      text: msg.body || '',
      attachments: [],
    };
    // media in arrivo (pdf, immagine, zip, ...): scaricalo e segnala il percorso
    if (msg.hasMedia && MEDIA_DIR) {
      const media = await msg.downloadMedia();
      const safe = (msg.id.id + '_' + (media.filename || 'file')).replace(/[^\w.-]/g, '_');
      const dest = path.join(MEDIA_DIR, safe);
      fs.writeFileSync(dest, Buffer.from(media.data, 'base64'));
      entry.attachments.push({
        filename: media.filename || safe,
        content_type: media.mimetype,
        path: dest,
      });
    }
    console.log(JSON.stringify(entry));
  } catch (e) { }
});
*/

client.on('disconnected', () => {
  ready = false;
  console.log(JSON.stringify({ type: 'disconnected' }));
});

const initialization = client.initialize();
if (HISTORY_IMPORT) initialization.catch(historyError);
let stopping = false;
async function shutdown() {
  if (stopping) return;
  stopping = true;
  ready = false;
  const deadline = setTimeout(() => process.exit(1), 15000);
  try { await client.destroy(); clearTimeout(deadline); process.exit(0); }
  catch (e) { clearTimeout(deadline); process.exit(1); }
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  try {
    const cmd = JSON.parse(line);
    if (!HISTORY_IMPORT && cmd.type === 'send' && ready) {
      await client.sendMessage(cmd.to, cmd.text);
      console.log(JSON.stringify({ type: 'sent', to: cmd.to }));
    }
  } catch (e) { /* ignora */ }
});
"""


def bridge_path():
  """Generated executable outside DATA_DIR; supports read-only installs via cache."""
  import os
  import hashlib
  from connectors.storage import FileLock, atomic_write
  cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'pi-secretary'
  target = cache / ('wa_bridge-' + hashlib.sha256(NODE_BRIDGE.encode()).hexdigest()[:16] + '.js')
  with FileLock(str(target) + '.lock'):
    if not target.exists():
      atomic_write(target, NODE_BRIDGE)
  return target


def bridge_env(auth_only=False):
  import os
  env = dict(os.environ, WA_SESSION_DIR=str(config.DATA_DIR / 'wa_session'),
         WA_SPOOL_DIR=str(config.DATA_DIR / 'wa_pending'),
         WA_MEDIA_DIR=str(config.DATA_DIR / 'wa_media'),
         WA_AUTH_ONLY='1' if auth_only else '0',
         WA_SHOW_QR='1' if auth_only else '0')
  # Preserve Docker-provided NODE_PATH; keep legacy data/node_modules working.
  env['NODE_PATH'] = os.pathsep.join(filter(None, [os.environ.get('NODE_PATH'),
    config.get('WA_NODE_MODULES'), str(config.BASE_DIR / 'data/node_modules')]))
  chromium = config.get('WA_CHROMIUM_PATH') or _find_system_chromium()
  if chromium:
    env['WA_CHROMIUM_PATH'] = chromium
  return env


def stop_process(proc):
  proc.terminate()
  try:
    proc.wait(timeout=20)
  except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait(timeout=5)


def import_history(start, end, timeout=600):
  """Synchronously archive synced WhatsApp history in [start, end).

  Requires an existing linked-device session and exclusive ownership. This
  never authenticates, analyzes, sends, or marks messages read. A private
  persistent spool survives failures independently of the runtime spool.
  Returns the number of messages successfully stored (including DB upserts).
  """
  import math
  import queue
  import time
  from datetime import datetime
  import db
  from connectors.storage import FileLock

  if (not isinstance(start, datetime) or not isinstance(end, datetime)
      or start.utcoffset() is None or end.utcoffset() is None or start >= end):
    raise ValueError('History requires timezone-aware start < end')
  if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
    raise ValueError('timeout must be positive and finite')
  deadline = time.monotonic() + timeout

  def remaining():
    value = deadline - time.monotonic()
    if value <= 0:
      raise TimeoutError('WhatsApp historical import timed out')
    return value

  session = config.DATA_DIR / 'wa_session'
  if not (session / 'session').is_dir():
    raise FileNotFoundError('Existing WhatsApp session required; run setup separately')
  with FileLock(session / '.owner.lock', blocking=False):
    spool = config.DATA_DIR / 'wa_history_pending'
    spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = bridge_env()
    env.update(WA_HISTORY_IMPORT='1', WA_AUTH_ONLY='0', WA_SHOW_QR='0',
           WA_SPOOL_DIR=str(spool), WA_HISTORY_CUTOFF=str(start.timestamp()),
           WA_HISTORY_END=str(end.timestamp()))
    proc = None
    reader = None
    events = queue.Queue()

    def read_events():
      try:
        for line in proc.stdout:
          events.put(line)
      except Exception as exc:
        events.put(exc)
      finally:
        events.put(None)

    try:
      remaining()
      proc = subprocess.Popen(
        ['node', str(bridge_path())], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=env, cwd=str(config.DATA_DIR))
      reader = threading.Thread(target=read_events, daemon=True)
      reader.start()
      self_wid = None
      while True:
        try:
          line = events.get(timeout=remaining())
        except queue.Empty as exc:
          raise TimeoutError('WhatsApp historical import timed out') from exc
        if isinstance(line, Exception):
          raise line
        if line is None:
          raise RuntimeError('WhatsApp bridge exited before history_complete')
        try:
          event = json.loads(line)
        except ValueError:
          continue  # Chromium and dependency diagnostic output.
        if not isinstance(event, dict):
          continue
        kind = event.get('type')
        if kind in ('history_error', 'error', 'auth_failure', 'disconnected'):
          raise RuntimeError(event.get('error') or f'WhatsApp {kind}')
        if kind in ('qr', 'reauth_required'):
          raise PermissionError('WhatsApp session requires authentication')
        if kind == 'ready':
          self_wid = event.get('self')
        if kind == 'history_complete':
          if not self_wid:
            raise RuntimeError('WhatsApp history completed without account identity')
          break

      count = 0
      for path in sorted(spool.glob('*.json')):
        remaining()
        m = json.loads(path.read_text())
        if not m.get('enriched'):
          continue
        if (not start.timestamp() <= m['timestamp'] < end.timestamp()
            or m.get('chat_id') == 'status@broadcast'
            or m.get('self', self_wid) != self_wid):
          continue
        identity = m['id']
        if path.name != identity.encode().hex() + '.json':
          raise ValueError('WhatsApp spool identity mismatch')
        chat_name = m.get('from_name') or m.get('from') or '?'
        if m.get('from_me'):
          sender = f'io → {chat_name}'
        elif '@g.us' in str(m.get('chat_id') or m.get('from') or ''):
          sender = (f"{m['author_name']} in {chat_name}" if m.get('author_name')
                else f"{chat_name} · {m.get('author') or m.get('from')}")
        else:
          sender = f"{chat_name} ({m.get('from')})"
        db.store_message('whatsapp', sender, f'WhatsApp · {chat_name}',
          m.get('text', ''),
          ts=datetime.fromtimestamp(m['timestamp'], start.tzinfo).isoformat(),
          source_id=identity, attachments=m.get('attachments', []),
          metadata={'account': self_wid, 'self': self_wid,
                'chat_id': m.get('chat_id'), 'author': m.get('author'),
                'message_id': identity, 'chat_name': chat_name,
                'author_name': m.get('author_name'),
                'direction': 'out' if m.get('from_me') else 'in',
                'recovered': True})
        count += 1
        # ACK only the exact enriched version that was durably stored.
        current = json.loads(path.read_text())
        if (m.get('version') is not None and current.get('enriched')
            and current.get('id') == identity
            and current.get('version') == m['version']):
          path.unlink()
      remaining()
      return count
    finally:
      try:
        if proc is not None:
          stop_process(proc)
      finally:
        if reader is not None:
          reader.join(timeout=1)
        if proc is not None:
          for stream in (proc.stdin, proc.stdout):
            if stream is not None:
              stream.close()


def _find_system_chromium() -> str:
    """Trova un Chromium eseguibile sul sistema (ARM-friendly).

    Puppeteer su ARM64 scarica binari x86 che non girano: preferiamo
    sempre il Chromium di sistema quando c'è.
    """
    import shutil
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    # snap di Ubuntu (non sempre nel PATH dell'utente)
    for snap_path in ("/snap/bin/chromium", "/usr/lib/chromium/chromium"):
        if Path(snap_path).exists():
            return snap_path
    return ""


class WhatsAppBridge:
    """Gestisce il processo Node del ponte WhatsApp."""

    def __init__(self):
        self.proc = None
        self.session_lock = None
        self.ready = False
        self.self_wid = ""          # numero del dispositivo collegato
        self.incoming = []          # messaggi ricevuti in attesa di essere letti
        self._lock = threading.Lock()
        self.spool_dir = config.DATA_DIR / 'wa_pending'
        self.spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def history_cutoff():
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(config.TIMEZONE)).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(1,min(90,int(config.get('RECOVERY_DAYS','14')))))

    def ack(self, identity, version=None):
      path = self.spool_dir / (identity.encode().hex() + '.json')
      try:
        entry = json.loads(path.read_text())
        if entry.get('enriched') and entry.get('version') == version:
          path.unlink(missing_ok=True)
      except (OSError, ValueError):
        pass

    def start(self) -> bool:
        if not config.WHATSAPP_ENABLED:
            return False
        import os
        from pathlib import Path

        session_dir = config.DATA_DIR / "wa_session"
        session_dir.mkdir(parents=True, exist_ok=True)
        media_dir = config.DATA_DIR / "wa_media"
        media_dir.mkdir(parents=True, exist_ok=True)
        from connectors.storage import FileLock
        if self.proc:
          return True
        try:
          self.session_lock = FileLock(session_dir / '.owner.lock', blocking=False)
          self.session_lock.__enter__()
        except OSError:
          self.session_lock = None
          print('[whatsapp] sessione occupata; setup/runtime già attivo')
          return False

        env = bridge_env()
        env['WA_HISTORY_CUTOFF'] = str(self.history_cutoff().timestamp())
        # Su ARM64 Puppeteer scariva binari x86: punta al Chromium di sistema
        chromium = _find_system_chromium()
        if chromium:
            env["WA_CHROMIUM_PATH"] = chromium
        try:
            self.proc = subprocess.Popen(
                ["node", str(bridge_path())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, env=env,
                cwd=str(config.DATA_DIR),
            )
        except (OSError, ValueError):
            self.session_lock.__exit__()
            self.session_lock = None
            print('[whatsapp] avvio fallito; controllare Node, cache e dipendenze')
            return False

        threading.Thread(target=self._reader, daemon=True).start()
        print('[whatsapp] ponte avviato; per autenticare usare setup_whatsapp.py')
        return True

    def stop(self) -> None:
        if self.proc:
            stop_process(self.proc)
            self.proc = None
        if self.session_lock:
          self.session_lock.__exit__()
          self.session_lock = None
        self.ready = False

    # ---- lettura eventi --------------------------------------
    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "qr":
                pass  # il QR è già stampato dal bridge
            elif etype == 'reauth_required':
              self.ready = False
              print('[whatsapp] riautenticazione necessaria: eseguire setup_whatsapp.py')
            elif etype == "ready":
                self.ready = True
                self.self_wid = event.get("self") or ""
                print("[whatsapp] connesso!")
            elif etype == "message":
                with self._lock:
                    self.incoming.append({
                        "from": event["from"],
                        "from_name": event.get("from_name", ""),
                        "from_me": event.get("from_me", False),
                        "text": event.get("text", ""),
                        "attachments": event.get("attachments", []),
                    })
            elif etype == 'disconnected':
                self.ready = False

    # ---- API usate dal segretario ----------------------------
    def fetch_new_messages(self) -> list:
      from datetime import datetime, timedelta
      from zoneinfo import ZoneInfo
      cutoff = self.history_cutoff()
      end = datetime.now(ZoneInfo(config.TIMEZONE)) + timedelta(minutes=5)
      msgs = []
      for path in sorted(self.spool_dir.glob('*.json'))[:200]:
        try:
          m = json.loads(path.read_text())
          if not m.get('enriched'):
            continue
          dt = datetime.fromtimestamp(m['timestamp'], ZoneInfo(config.TIMEZONE))
          if not cutoff <= dt < end or m.get('chat_id') == 'status@broadcast':
            continue
          m['msg_date'] = dt.isoformat()
          m['metadata'] = {'account': self.self_wid, 'chat_id': m.get('chat_id'),
            'author': m.get('author'), 'message_id': m['id'],
            'chat_name': m.get('from_name'), 'author_name': m.get('author_name'),
            'direction': 'out' if m.get('from_me') else 'in'}
          msgs.append(m)
        except (ValueError, KeyError, OSError, TypeError):
          continue
      return msgs

    def send(self, to: str, text: str) -> bool:
        if not self.proc or not self.ready:
            return False
        try:
            self.proc.stdin.write(json.dumps({"type": "send", "to": to, "text": text}) + "\n")
            self.proc.stdin.flush()
            return True
        except Exception:  # noqa: BLE001
            return False
