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
function rememberChat(chat) {
  if (chat && chat.id && (chat.name || chat.formattedTitle))
    CHAT_NAMES[chat.id._serialized || chat.id] = chat.name || chat.formattedTitle;
}
function cutoff() {
  return Math.max(Number(process.env.WA_HISTORY_CUTOFF || 0), 0);
}
async function archive(msg, recovered_cached=false) {
  const id = msg.id && (msg.id._serialized || msg.id.id);
  if (!id || msg.from==='status@broadcast' || msg.timestamp < cutoff()) return;
  const entry = {id, timestamp:msg.timestamp, chat_id:msg.fromMe?msg.to:msg.from,
    author:msg.author || msg.from, from:msg.from, from_name:msg.from,
    author_name:null, from_me:!!msg.fromMe, text:msg.body || '', attachments:[], recovered_cached,
    enriched:false};
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
    } catch(e) {entry.media_error=true;}
  }
  entry.enriched=true;persist();
}
async function recover() {
  if(!ready) return;
  try {
    const chats=await client.getChats();
    // prima riempi la mappa dei nomi (rapida), poi il fetch lento
    for(const chat of chats.slice(0,100)) rememberChat(chat);
    for(const chat of chats.slice(0,100)) {
      try {for(const msg of await chat.fetchMessages({limit:0})) await archive(msg,true);} catch(e) {}
    }
  } catch(e) {}
}
client.on('message_create', msg => archive(msg));
setInterval(recover,900000).unref();

client.on('qr', (qr) => {
  console.log(JSON.stringify({ type: 'qr', qr }));
  require('qrcode-terminal').generate(qr, { small: true });
});

client.on('ready', () => {
  ready = true;
  console.log(JSON.stringify({ type: 'ready', self: client.info && client.info.wid ? client.info.wid._serialized : null }));
  recover();
});

client.on('message', async (msg) => {
  return archive(msg);
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
  } catch (e) { /* ignora */ }
});

client.on('disconnected', () => {
  ready = false;
  console.log(JSON.stringify({ type: 'disconnected' }));
});

client.initialize();

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  try {
    const cmd = JSON.parse(line);
    if (cmd.type === 'send' && ready) {
      await client.sendMessage(cmd.to, cmd.text);
      console.log(JSON.stringify({ type: 'sent', to: cmd.to }));
    }
  } catch (e) { /* ignora */ }
});
"""


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
        return datetime.now(ZoneInfo(config.TIMEZONE)).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)

    def ack(self, identity):
        (self.spool_dir / (identity.encode().hex() + '.json')).unlink(missing_ok=True)

    def start(self) -> bool:
        if not config.WHATSAPP_ENABLED:
            return False
        import os
        from pathlib import Path

        session_dir = config.DATA_DIR / "wa_session"
        session_dir.mkdir(parents=True, exist_ok=True)
        media_dir = config.DATA_DIR / "wa_media"
        media_dir.mkdir(parents=True, exist_ok=True)
        bridge_path = config.DATA_DIR / "wa_bridge.js"
        bridge_path.write_text(NODE_BRIDGE, encoding="utf-8")

        env = dict(os.environ,
                   WA_SESSION_DIR=str(session_dir),
                   WA_SPOOL_DIR=str(self.spool_dir),
                   WA_HISTORY_CUTOFF=str(self.history_cutoff().timestamp()),
                   WA_MEDIA_DIR=str(media_dir))
        # Su ARM64 Puppeteer scariva binari x86: punta al Chromium di sistema
        chromium = _find_system_chromium()
        if chromium:
            env["WA_CHROMIUM_PATH"] = chromium
        try:
            self.proc = subprocess.Popen(
                ["node", str(bridge_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, env=env,
                cwd=str(config.DATA_DIR),
            )
        except FileNotFoundError:
            print("[whatsapp] node non trovato: installalo con `sudo apt install nodejs npm`")
            return False

        threading.Thread(target=self._reader, daemon=True).start()
        print("[whatsapp] ponte avviato — se è la prima volta, scansiona il QR qui sopra")
        return True

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None
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
      end = cutoff + timedelta(days=2)
      msgs = []
      for path in sorted(self.spool_dir.glob('*.json')):
        try:
          m = json.loads(path.read_text())
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
