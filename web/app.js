'use strict';
let csrf = '';
const el = id => document.getElementById(id);
const node = (tag, text) => { const n = document.createElement(tag); n.textContent = text; return n; };
function terminalCommand(title, command) {
  const box = node('div', ''); box.className = 'terminal-command';
  const pre = node('pre', ''); pre.append(node('code', command));
  const copy = node('button', 'Copia comando'); copy.type = 'button';
  const status = node('small', ''); status.setAttribute('role', 'status');
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(command);
      status.textContent = 'Comando copiato. Incollalo nel terminale del server.';
    } catch (_) {
      const selection = window.getSelection();
      const range = document.createRange(); range.selectNodeContents(pre);
      selection.removeAllRanges(); selection.addRange(range);
      status.textContent = 'Copia automatica non disponibile: comando selezionato, premi Ctrl+C (⌘C su Mac).';
    }
  };
  box.append(node('h4', title), pre, copy, status);
  return box;
}
async function api(path, data) {
  const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) { if (response.status === 401) show(false); throw Error(result.error || 'Richiesta fallita'); }
  return result;
}
function show(connected) {
  el('login').hidden = connected; el('dashboard').hidden = !connected; el('logout').hidden = !connected;
  if (!connected) {el('config-fields').replaceChildren(); el('config-groups').replaceChildren(); el('config-form').hidden = true; el('config-save').hidden = true; configRevision = '';}
}
let configRevision = '';
const healthLabels = {tested:'Verificato in rete', observed:'Solo stato osservato', disabled:'Disabilitato', unconfigured:'Non configurato', skipped:'Non eseguito', failed:'Verifica fallita', timeout:'Tempo scaduto'};
el('health-check').onclick = async () => {
  const button = el('health-check');
  button.disabled = true;
  el('health-results').setAttribute('aria-busy', 'true');
  el('health-status').textContent = 'Verifica in corso… massimo circa 12 secondi. I risultati precedenti, se presenti, non sono aggiornati.';
  try {
    const result = await api('/api/platform-health', {});
    el('health-results').replaceChildren(...result.results.map(check => {
      const card = node('article', ''); card.className = 'health-card';
      card.append(node('h3', check.label), node('strong', healthLabels[check.status] || 'Stato sconosciuto'),
        node('p', check.explanation), node('small', `${new Date(check.checked_at).toLocaleString('it')} · ${check.latency_ms} ms`));
      return card;
    }));
    el('health-status').textContent = `Controllo completato ${new Date(result.completed_at).toLocaleString('it')}. “Verificato” copre solo l’operazione indicata, non l’intero flusso. Attesa minima tra controlli: 30 secondi.`;
  } catch (error) {el('health-status').textContent = `${error.message} I risultati precedenti non sono aggiornati.`;}
  finally {button.disabled = false; el('health-results').setAttribute('aria-busy', 'false');}
};
const fieldNames = {AGENT_NAME:'Nome assistente', OPENROUTER_API_KEY:'Chiave API OpenRouter', OPENROUTER_MODEL:'Modello AI', DATABASE_URL:'Connessione PostgreSQL', TELEGRAM_BOT_TOKEN:'Token del bot', TELEGRAM_CHAT_ID:'Proprietario autorizzato', TELEGRAM_USER_ENABLED:'Account personale', TELEGRAM_API_ID:'ID applicazione Telegram', TELEGRAM_API_HASH:'Segreto applicazione Telegram', TELEGRAM_PHONE:'Numero di telefono', WHATSAPP_ENABLED:'Connessione WhatsApp', GCAL_ENABLED:'Abilita calendario', GCAL_CALENDAR_ID:'ID del calendario condiviso', GCAL_EVENT_DURATION:'Durata appuntamenti (minuti)', GCAL_SERVICE_ACCOUNT_JSON:'Nuova chiave service account (JSON)', TIMEZONE:'Fuso orario', LANGUAGE:'Lingua', DIGEST_TIME:'Orario riepilogo', NOTIFY_PRIORITY_THRESHOLD:'Priorità minima notifiche', POLL_INTERVAL:'Intervallo controllo (secondi)'};
const suffixNames = {NAME:'Nome', PROVIDER:'Provider', ADDRESS:'Indirizzo email', PASSWORD:'Password per app', AUTH:'Metodo di accesso', CLIENT_ID:'ID applicazione OAuth', IMAP_HOST:'Server posta in arrivo', IMAP_PORT:'Porta IMAP', SMTP_HOST:'Server posta in uscita', SMTP_PORT:'Porta SMTP', VAT:'Partita IVA', DESC:'Descrizione'};
const fieldLabel = key => fieldNames[key] || suffixNames[key.replace(/^(EMAIL\d+|INVOICE_ENTITY\d+)_/, '')] || key.toLowerCase().replaceAll('_', ' ');
let selectedGroup = '';
function filterGroups() {
  const term = el('config-search').value.toLocaleLowerCase('it');
  document.querySelectorAll('#config-groups button').forEach(b => {
    b.hidden = !b.textContent.toLocaleLowerCase('it').includes(term) || (b.dataset.empty === 'true' && !el('config-show-empty').checked && !term);
  });
}
function pendingChanges() {
  return Object.fromEntries([...el('config-fields').querySelectorAll('[data-key]')].filter(i => !i.disabled && i.value !== i.dataset.original && (i.dataset.secret !== 'true' || i.value)).map(i => [i.dataset.key, i.value]));
}
function editAgain() {el('config-review').hidden = true; el('config-save').hidden = false;}
el('config-search').oninput = filterGroups;
el('config-show-empty').onchange = filterGroups;
el('config-back').onclick = editAgain;
el('config-cancel').onclick = () => {el('config-fields').querySelectorAll('[data-key]').forEach(i => {i.value = i.dataset.original;}); editAgain(); el('config-status').textContent = 'Modifiche annullate. Credenziali salvate invariate.';};
el('config-load').onclick = async () => {
  if (Object.keys(pendingChanges()).length && !confirm('Scartare le modifiche non salvate e ricaricare?')) return;
  try {
    const result = await api('/api/configuration');
    configRevision = result.revision;
    el('config-info').textContent = result.apply;
    el('config-fields').replaceChildren();
    el('config-groups').replaceChildren(); el('config-form').hidden = false; editAgain();
    const groups = new Map();
    result.fields.forEach(field => {
      if (!groups.has(field.group)) {
        const details = node('section', ''); details.className = 'settings-detail'; details.hidden = true;
        details.append(node('h3', field.group));
        if (field.group === 'Telegram e WhatsApp') {
          const guide = node('div', ''); guide.className = 'setup-guide';
          guide.append(node('h4', 'Telegram: cosa vuoi fare?'), node('p', 'Rinnovare il token dello stesso bot: inserisci sotto il nuovo token ottenuto da BotFather. Il proprietario resta invariato. La verifica usa solo getMe, senza messaggi.'), node('h4', 'Collegare un nuovo bot o cambiare proprietario'), node('p', 'È necessaria una nuova autorizzazione dal terminale del deployment: esegui setup_telegram.py tramite il launcher. Il web non può verificare la proprietà di un bot diverso.'), node('h4', 'Leggere il tuo account personale'), node('p', 'È un collegamento distinto dal bot. Usa setup_telegram_account.py nel terminale del deployment per login e codice di conferma. Non inserire qui OTP o password Telegram.'), node('p', 'WhatsApp: autorizza il QR con setup_whatsapp.py nel terminale del deployment.'));
          details.append(guide);
          guide.append(node('p', 'Esegui sul server che ospita Docker, nella cartella del progetto (non dentro il container). Il launcher può sospendere temporaneamente l’acquisizione. Scegli “5 · setup opzionali” e il servizio desiderato; non eseguire direttamente gli script Python.'));
        }
        const commands = node('div', ''); commands.className = 'setup-guide';
        commands.append(node('h4', 'Operazioni da terminale · Docker'), node('p', 'Apri il terminale del server nella cartella che contiene pi-secretary. Questi comandi non contengono credenziali e non vengono eseguiti dal browser.'), terminalCommand('Riconfigura o collega un servizio', 'sh ./pi-secretary configure'), node('p', 'Conferma la pausa dell’app e scegli la sezione desiderata. Per Telegram e WhatsApp scegli i setup opzionali; per Microsoft scegli la sezione email.'), terminalCommand('Applica la configurazione e avvia l’app', 'sh ./pi-secretary up'), terminalCommand('Controlla lo stato dei container', 'sh ./pi-secretary status'));
        details.append(commands);
        const fields = result.fields.filter(f => f.group === field.group);
        const configured = fields.some(f => f.configured);
        const identity = fields.find(f => /^EMAIL\d+_ADDRESS$|^INVOICE_ENTITY\d+_NAME$/.test(f.key));
        const displayName = identity && identity.value ? `${field.group} · ${identity.value}` : field.group;
        details.querySelector('h3').textContent = displayName;
        const button = node('button', ''); button.type = 'button'; button.className = 'integration-card';
        button.append(node('strong', displayName), node('small', configured ? 'Impostazioni presenti · apri' : 'Da configurare · apri'));
        button.dataset.empty = String(/^(Email |Entità fatture )/.test(field.group) && !configured);
        button.setAttribute('aria-expanded', 'false');
        button.onclick = () => {selectedGroup = field.group; groups.forEach((section, name) => {section.hidden = name !== selectedGroup;}); document.querySelectorAll('#config-groups button').forEach(b => b.setAttribute('aria-expanded', String(b === button))); details.scrollIntoView({behavior:'smooth', block:'start'});};
        el('config-groups').append(button);
        groups.set(field.group, details); el('config-fields').append(details);
      }
      const label = node('label', fieldLabel(field.key));
      const input = node(field.type === 'select' ? 'select' : field.type === 'textarea' ? 'textarea' : 'input', '');
      if (input.tagName === 'INPUT') input.type = field.secret ? 'password' : (field.type || 'text');
      if (input.tagName === 'SELECT') {
        const current = field.value || '';
        const empty = node('option', 'Non impostato · default applicazione/preset'); empty.value = ''; input.append(empty);
        empty.disabled = !/^EMAIL\d+_AUTH$/.test(field.key);
        (field.options || []).forEach(value => {
          const option = node('option', value === 'true' ? 'Sì (true)' : value === 'false' ? 'No (false)' : value);
          option.value = value; input.append(option);
        });
        // Preserve legacy values on load: never silently rewrite an unknown value.
        if (current && !(field.options || []).includes(current)) {
          const legacy = node('option', `${current} · valore esistente non valido`); legacy.value = current; input.append(legacy);
        }
      }
      ['min', 'max', 'step'].forEach(name => {if (field[name] !== undefined) input.setAttribute(name, field[name]);});
      input.autocomplete = 'off'; input.spellcheck = false;
      input.dataset.key = field.key; input.dataset.original = field.secret ? '' : (field.value || '');
      input.dataset.secret = String(field.secret);
      input.value = field.secret ? '' : (field.value || '');
      input.id = 'setting-' + field.key; label.htmlFor = input.id;
      input.disabled = !!field.readonly;
      input.placeholder = field.secret ? 'Incolla un nuovo valore solo per sostituire quello salvato' : 'Default applicazione';
      label.append(input);
      if (field.description) {
        const help = node('small', field.description); help.id = input.id + '-help';
        input.setAttribute('aria-describedby', help.id); label.append(help);
      }
      if (field.secret) label.append(node('small', field.configured ? '● Credenziale salvata · lascia vuoto per mantenerla' : '○ Nessuna credenziale salvata'));
      input.oninput = () => {editAgain(); el('config-status').textContent = `${Object.keys(pendingChanges()).length} modifiche da controllare · non ancora salvate`;};
      if (field.readonly) label.append(node('small', field.readonly));
      groups.get(field.group).append(label);
    });
    filterGroups(); el('config-load').textContent = 'Ricarica impostazioni';
    el('config-save').hidden = false; el('config-status').textContent = 'Modificare solo i campi necessari. Le verifiche di rete sono senza invii o pairing; OAuth e sessioni richiedono il terminale.';
  } catch(e) {el('config-status').textContent = e.message;}
};
el('config-form').onsubmit = async event => {
  event.preventDefault();
  const changes = pendingChanges();
  if (changes.TELEGRAM_BOT_TOKEN && !/^\d+:[A-Za-z0-9_-]+$/.test(changes.TELEGRAM_BOT_TOKEN)) {
    el('config-status').textContent = 'Token bot non valido: usa il token completo fornito da BotFather (numero:codice). Nessuna richiesta inviata e nessun valore salvato.';
    el('setting-TELEGRAM_BOT_TOKEN').focus(); return;
  }
  el('config-changes').replaceChildren(...Object.keys(changes).map(key => node('li', `${fieldLabel(key)}${el('setting-' + key).dataset.secret === 'true' ? ' · sostituzione credenziale (valore nascosto)' : ' · valore modificato'}`)));
  if (!Object.keys(changes).length) {el('config-status').textContent = 'Nessuna modifica da salvare.'; return;}
  el('config-review').hidden = false; el('config-save').hidden = true;
  el('config-review').scrollIntoView({behavior:'smooth', block:'center'});
};
el('config-confirm').onclick = async () => {
  const changes = pendingChanges();
  const inputs = [...el('config-fields').querySelectorAll('input,textarea,select')];
  if (!Object.keys(changes).length) {el('config-status').textContent = 'Nessuna modifica.'; return;}
  el('config-confirm').disabled = true; el('config-back').disabled = true; el('config-cancel').disabled = true;
  inputs.forEach(i => {i.dataset.locked = String(i.disabled); i.disabled = true;});
  el('config-save').disabled = true; el('config-load').disabled = true;
  el('config-status').textContent = 'Verifica in corso… nessuna modifica live.';
  try {
    await api('/api/configuration', {revision: configRevision, changes});
    // A successful revision is deliberately invalidated: reload before another edit.
    el('config-fields').replaceChildren(); el('config-save').hidden = true; configRevision = '';
    el('config-form').hidden = true; el('config-groups').replaceChildren();
    el('config-status').textContent = 'Batch salvato atomicamente. Runtime invariato: riavviare manualmente quando opportuno. Nessuna migrazione DB eseguita.';
  } catch(e) {el('config-status').textContent = `Non salvato. ${e.message} Le credenziali precedenti sono invariate. Reinserisci le nuove credenziali per riprovare.`; editAgain();}
  finally {
    inputs.filter(input => input.dataset.secret === 'true').forEach(input => {input.value = '';});
    Object.keys(changes).forEach(key => {delete changes[key];});
    el('config-save').disabled = false; el('config-load').disabled = false;
    inputs.forEach(i => {i.disabled = i.dataset.locked === 'true';});
    el('config-confirm').disabled = false; el('config-back').disabled = false; el('config-cancel').disabled = false;
  }
};
let filePath = '';
const fileQuery = path => new URLSearchParams({root: el('file-root').value, path}).toString();
async function browseFiles() {
  try {
    const result = await api(`/api/files?${fileQuery(filePath)}`);
    el('file-path').textContent = `${el('file-root').selectedOptions[0].textContent} / ${filePath}`;
    el('file-up').disabled = !filePath;
    el('file-list').replaceChildren();
    result.entries.forEach(entry => {
      const path = filePath ? `${filePath}/${entry.name}` : entry.name;
      const r = row(el('file-list'), entry.name, entry.directory ? 'Cartella' : `${entry.size} byte`);
      if (entry.directory) {
        const b = node('button', 'Apri'); b.onclick = () => {filePath = path; browseFiles();}; r.append(b);
      } else {
        const a = node('a', 'Scarica'); a.href = `/api/files/download?${fileQuery(path)}`; r.append(a);
        const b = node('button', 'Elimina');
        b.onclick = async () => {
          if (!confirm(`Eliminare ${entry.name}?`)) return;
          try {
            const response = await fetch(`/api/files?${fileQuery(path)}`, {method:'DELETE', headers:{'X-CSRF-Token':csrf}});
            const result = await response.json();
            if (!response.ok) throw Error(result.error || 'Eliminazione fallita');
            await browseFiles();
          } catch(e) {el('file-status').textContent = e.message;}
        }; r.append(b);
      }
    });
    el('file-status').textContent = '';
  } catch(e) {el('file-status').textContent = e.message;}
}
el('file-root').onchange = () => {filePath = ''; browseFiles();};
el('file-up').onclick = () => {filePath = filePath.split('/').slice(0,-1).join('/'); browseFiles();};
el('file-refresh').onclick = browseFiles;
el('file-send').onclick = async () => {
  const file = el('file-upload').files[0];
  if (!file) return;
  if (file.size > 20*1024*1024) {el('file-status').textContent = 'File troppo grande'; return;}
  el('file-send').disabled = true;
  try {
    const response = await fetch(`/api/files/upload?${new URLSearchParams({name:file.name})}`, {
      method:'POST', headers:{'X-CSRF-Token':csrf, 'Content-Type':'application/octet-stream'}, body:file});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Upload fallito');
    el('file-root').value = 'data'; filePath = 'uploads'; el('file-upload').value = ''; await browseFiles();
    el('file-status').textContent = 'File caricato';
  } catch(e) {el('file-status').textContent = e.message;}
  finally {el('file-send').disabled = false;}
};
function row(parent, left, right) { const r = node('div', ''); r.className = 'row'; r.append(node('div', left), node('small', right)); parent.append(r); return r; }
async function refresh() {
  try {
    const d = await api('/api/dashboard'); show(true);
    el('status').textContent = `● ${d.agent_name} · Collegato · ${d.timezone} · ultimo aggiornamento ${new Date().toLocaleTimeString('it')}`;
    const today = d.today;
    const sum = (direction) => d.counts.filter(r => String(r[0]).slice(0,10) === today && r[2] === direction).reduce((s,r)=>s+r[3],0);
    el('kpis').replaceChildren();
    [['In entrata oggi',sum('in')],['In uscita oggi',sum('out')],['Da gestire · ieri/oggi',d.counts.reduce((s,r)=>s+r[5],0)],['Appuntamenti aperti',d.appointments]].forEach(([label,value])=>{
      const c=node('div','');c.className='card';c.append(node('small',label),node('strong',value));el('kpis').append(c);
    });
    el('flows').replaceChildren();
    d.flows.forEach(f=>{const h=f.health;const age=h.observed_at?Math.floor(Date.now()/1000-h.observed_at):null;
      const r=row(el('flows'),f.label,`${h.status} ${f.configuration?`· configurazione: ${f.configuration}`:''} ${age===null?'':`· ${age}s fa`}`);
      const b=node('button',f.enabled?'Flusso abilitato · sospendi':'Flusso sospeso · abilita');b.setAttribute('aria-label',`${f.label}: ${b.textContent}`);
      b.onclick=async()=>{b.disabled=true;try{await api(`/api/flows/${f.id}`,{enabled:!f.enabled});await refresh();}catch(e){el('status').textContent=e.message;}finally{b.disabled=false;}};r.append(b);
    });
    el('traffic').replaceChildren();d.counts.forEach(r=>row(el('traffic'),`${r[0]} · ${r[1]} · ${r[2]}`,`${r[3]} messaggi / ${r[4]} analizzati`));
    el('activity').replaceChildren();d.recent.forEach(r=>row(el('activity'),`${r[0]} · ${r[1]||'direzione ignota'}`,`${r[3]} · ${r[2]||'data ignota'}`));
    (d.jobs||[]).forEach(j=>row(el('activity'),`${j.kind} · ${j.status}: ${j.count}`,`età massima ${Math.round(j.oldest_age_seconds||0)}s · ultimo successo ${j.last_success||'mai'}`));
    row(el('activity'),'Notifiche confermate',String(d.notifications_sent||0));
    (d.calendar_pending||[]).forEach(a=>row(el('activity'),`Calendario #${a[0]} ${a[1]}`,`${a[2]} · ${a[3]}`));
    el('invoices').replaceChildren();d.invoices.forEach(r=>row(el('invoices'),`${r[0]} · ${r[1]}`,`${r[2]} documenti`));
    el('zips').replaceChildren();d.zips.forEach(r=>row(el('zips'),`ZIP ${r[0]} · ${r[1]}`,r[2]?'inviato':'non inviato'));
  } catch(e) {el('status').textContent=e.message;}
}
el('login').onsubmit=async e=>{e.preventDefault();el('error').textContent='';try{const data=Object.fromEntries(new FormData(e.target));const result=await api('/api/login',data);csrf=result.csrf;e.target.reset();show(true);await refresh();await browseFiles();}catch(err){el('error').textContent=err.message;}};
el('logout').onclick=async()=>{await api('/api/logout',{});csrf='';show(false);};
api('/api/session').then(s=>{csrf=s.csrf;show(true);refresh();browseFiles();}).catch(()=>{});
setInterval(()=>{if(!el('dashboard').hidden)refresh();},10000);