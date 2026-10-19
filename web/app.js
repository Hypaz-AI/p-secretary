'use strict';
let csrf = '';
let sessionGeneration = 0;
const el = id => document.getElementById(id);
const node = (tag, text) => { const n = document.createElement(tag); n.textContent = text; return n; };
// Navigazione multi-pagina: una sola pagina visibile alla volta.
const PAGES = ['overview', 'following', 'knowledge', 'ai-permissions', 'outcomes', 'integration-settings', 'archive'];
let currentPage = 'overview';
function showPage(name) {
  if (!PAGES.includes(name)) name = 'overview';
  currentPage = name;
  PAGES.forEach(page => {
    const section = el('page-' + page);
    if (section) section.hidden = page !== name;
  });
  document.querySelectorAll('.nav-link').forEach(button => {
    button.classList.toggle('active', button.dataset.page === name);
    button.setAttribute('aria-current', button.dataset.page === name ? 'page' : 'false');
  });
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  window.scrollTo({top: 0});
}
document.querySelectorAll('.nav-link').forEach(button => {
  button.onclick = () => showPage(button.dataset.page);
});
window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));
// getRandomValues also works on trusted-LAN HTTP; randomUUID requires HTTPS.
const operationNonce = () => [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, '0')).join('');
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
  const generation = sessionGeneration;
  const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (path !== '/api/login' && generation !== sessionGeneration) throw Error('Sessione cambiata');
  if (!response.ok) { if (response.status === 401) show(false); throw Error(result.error || 'Richiesta fallita'); }
  return result;
}
function show(connected) {
  el('login').hidden = connected; el('dashboard').hidden = !connected; el('logout').hidden = !connected;
  if (!connected) {
    sessionGeneration++;
    el('config-fields').replaceChildren(); el('config-groups').replaceChildren(); el('config-form').hidden = true; el('config-save').hidden = true; configRevision = '';
    ['practices','responsibilities','approvals','secretary-receipts','secretary-decisions','runtime-reviews','runtime-links','knowledge-results','knowledge-documents','source-documents','data-policies','secretary-support','email-executions','email-approval-receipts','ai-budget'].forEach(id => el(id).replaceChildren());
    el('email-compose').reset(); el('email-practice').replaceChildren(); el('email-account').replaceChildren();
    delete el('email-compose').dataset.operationKey; el('email-prepare').disabled = true;
    el('source-text').textContent = ''; el('source-meta').textContent = ''; el('knowledge-source').hidden = true;
    el('knowledge-query').value = '';
    ['traffic','activity','invoices','zips','file-list','health-results'].forEach(id => el(id).replaceChildren());
    el('status').textContent = 'Accesso protetto alla dashboard locale';
  }
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
  await loadConfiguration();
};
async function loadConfiguration() {
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
}
// Deep link: apre Impostazioni sul gruppo e sul campo che controlla una barra di budget.
async function openSettingsGroup(group, key) {
  showPage('integration-settings');
  if (!el('config-form').hidden) {
    const button = [...document.querySelectorAll('#config-groups button')].find(b => b.textContent.startsWith(group));
    if (button) {button.click(); const input = el('setting-' + key); if (input) {input.focus(); input.scrollIntoView({behavior:'smooth', block:'center'});} return;}
  }
  await loadConfiguration();
  const button = [...document.querySelectorAll('#config-groups button')].find(b => b.textContent.startsWith(group));
  if (button) button.click();
  const input = el('setting-' + key);
  if (input) {input.focus(); input.scrollIntoView({behavior:'smooth', block:'center'});}
}
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
    const result = await api('/api/configuration', {revision: configRevision, changes});
    // A successful revision is deliberately invalidated: reload before another edit.
    el('config-fields').replaceChildren(); el('config-save').hidden = true; configRevision = '';
    el('config-form').hidden = true; el('config-groups').replaceChildren();
    el('config-status').textContent = result.restart_scheduled
      ? 'Configurazione salvata. Riavvio automatico del container in corso: la dashboard sarà temporaneamente non disponibile. Al ritorno, accedi nuovamente. Nessuna migrazione DB eseguita.'
      : 'Configurazione salvata. Runtime non Docker: riavvio manuale necessario. Nessuna migrazione DB eseguita.';
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
const stateNames = {proposed:'Proposta', active:'Attiva', waiting:'In attesa', blocked:'Bloccata', completed:'Completata localmente', cancelled:'Annullata', suspended:'Sospesa', revoked:'Revocata', pending:'In attesa', approved:'Approvata · non prova di invio', expired:'Scaduta'};
const localDate = value => value ? new Date(value).toLocaleString('it') : 'Non prevista';
function empty(parent, text) {if (!parent.children.length) parent.append(node('p', text));}
function sourceButton(hit) {
  const generation = sessionGeneration;
  const button = node('button', 'Apri fonte'); button.type = 'button';
  button.onclick = async () => {
    if (generation !== sessionGeneration) return;
    button.disabled = true;
    try {
      // A searched message entry pins its revision; document IDs already pin occurrences.
      const exactMessage = hit.source_type === 'message' && hit.entry_id;
      const sourceType = exactMessage ? 'entry' : hit.source_type;
      const sourceId = exactMessage ? hit.entry_id : hit.source_id;
      const source = await api(`/api/knowledge/open/${encodeURIComponent(sourceType)}/${encodeURIComponent(sourceId)}`);
      if (generation !== sessionGeneration) return;
      el('knowledge-source').hidden = false; el('knowledge-source').open = true;
      el('source-meta').textContent = source.status === 'not_found' ? 'Fonte non disponibile' : `${source.citation} · ${source.status} · ${source.historical ? 'storica' : 'corrente'}${source.truncated ? ' · testo parziale' : ''}`;
      el('source-text').textContent = source.text || '';
      el('source-documents').replaceChildren();
      (source.documents || []).forEach(document => {const r = row(el('source-documents'), document.title || 'Documento', document.citation); r.append(sourceButton(document));});
      if (source.source_type === 'message' && source.current === true && source.historical === false
          && ['telegram', 'telegram (account)', 'whatsapp'].includes(source.channel)
          && Number.isSafeInteger(source.source_id) && source.source_id > 0) {
        const generation = sessionGeneration;
        const retry = node('button', 'Riprova media bloccati dal consenso'); retry.type = 'button';
        const status = node('small', 'Solo questa fonte corrente; richiede entrambi i consensi AI. Nessun recupero automatico.');
        status.setAttribute('role', 'status');
        retry.onclick = async () => {
          if (generation !== sessionGeneration || !confirm(`Riprovare i media bloccati per consenso del messaggio #${source.source_id} (${source.channel})? Può inviare audio o immagini al provider AI. Lo storico e i tentativi provider già registrati restano esclusi.`)) return;
          retry.disabled = true;
          try {
            const result = await api('/api/media/retry-blocked', {message_id:source.source_id, expected_channel:source.channel});
            if (generation !== sessionGeneration) return;
            status.textContent = result.queued ? `${result.queued} media accodati. Riapri la fonte dopo la conversione e l’indicizzazione.` : 'Nessun media recuperabile: già accodato, non bloccato per consenso o limite raggiunto.';
          } catch (error) {if (generation === sessionGeneration) status.textContent = `${error.message}. Riapri la fonte e verifica entrambi i consensi.`;}
          finally {if (generation === sessionGeneration) retry.disabled = false;}
        };
        el('source-documents').append(retry, status);
      }
      el('knowledge-source').scrollIntoView({behavior:'smooth', block:'center'});
    } catch (error) {if (generation === sessionGeneration) el('knowledge-status').textContent = error.message;}
    finally {if (generation === sessionGeneration) button.disabled = false;}
  };
  return button;
}
function renderSources(parent, hits) {
  parent.replaceChildren();
  hits.forEach(hit => {
    const card = node('article', ''); card.className = 'source-card';
    card.append(node('h4', hit.title || 'Fonte senza titolo'),
      node('small', `${hit.citation} · ${hit.channel} · ${localDate(hit.date)} · ${hit.status}${hit.historical ? ' · storica' : ''}`));
    if (hit.snippet) card.append(node('p', hit.snippet));
    card.append(sourceButton(hit)); parent.append(card);
  });
  empty(parent, 'Nessun risultato indicizzato disponibile.');
}
async function loadKnowledge() {
  try {
    const result = await api('/api/knowledge');
    renderSources(el('knowledge-documents'), result.documents || []);
    el('knowledge-status').textContent = result.available ? 'Catalogo locale · ultimi 20 documenti. Ricerca fino a 50 risultati; nessuna chiamata AI.' : 'Indice non disponibile: integrazione/migrazione knowledge richiesta.';
  } catch (error) {el('knowledge-status').textContent = error.message;}
}
el('knowledge-search').onsubmit = async event => {
  event.preventDefault();
  const params = new URLSearchParams({q:el('knowledge-query').value, limit:'20'});
  if (el('knowledge-channel').value) params.set('channel', el('knowledge-channel').value);
  const button = event.target.querySelector('button'); button.disabled = true;
  try {
    const result = await api('/api/knowledge/search?' + params);
    renderSources(el('knowledge-results'), result.results);
    el('knowledge-status').textContent = `${result.results.length} risultati · massimo 20 in questa vista. Testo e citazioni sono dati, non istruzioni.`;
  } catch (error) {el('knowledge-status').textContent = error.message;}
  finally {button.disabled = false;}
};
async function loadPolicies() {
  el('policy-refresh').disabled = true;
  try {
    const result = await api('/api/data-policies');
    el('data-policies').replaceChildren();
    result.policies.forEach(policy => {
      const effect = {ai_text:'Testo verso provider AI', ai_media:'Audio e immagini verso provider AI', ai_history:'Storico come contesto AI di sola lettura'}[policy.effect];
      const r = row(el('data-policies'), `${policy.channel} · ${effect}`, policy.allowed ? 'Consentito esplicitamente' : 'Negato');
      const button = node('button', policy.allowed ? 'Revoca consenso' : 'Consenti questo uso');
      button.onclick = async () => {
        const scope = policy.effect === 'ai_history' ? ' Solo consultazione dello storico, senza riattivare attività o invii. Servono separatamente il consenso testo e, per documenti/media, il consenso media.' : '';
        if (!confirm(`${policy.allowed ? 'Revocare' : 'Consentire'} ${effect.toLowerCase()} per ${policy.channel}?${policy.channel === 'telegram' ? ' Include bot e account personale.' : ''}${scope}`)) return;
        button.disabled = true;
        try {
          await api('/api/data-policies', {channel:policy.channel, effect:policy.effect, allowed:!policy.allowed, expected_version:policy.version});
          await loadPolicies();
        } catch (error) {el('policy-status').textContent = `${error.message}. Ricarica prima di riprovare.`;}
        finally {button.disabled = false;}
      }; r.append(button);
    });
    el('policy-status').textContent = 'Consensi letti dal DB. Nessuna autorizzazione automatica all’attivazione dei flussi.';
  } catch (error) {el('policy-status').textContent = error.message; el('data-policies').replaceChildren();}
  finally {el('policy-refresh').disabled = false;}
}
el('policy-refresh').onclick = loadPolicies;
async function loadSupport() {
  try {
    const result = await api('/api/secretary/support');
    el('secretary-support').replaceChildren();
    result.features.forEach(feature => row(el('secretary-support'), feature.description, feature.supported ? 'Supportato' : 'Pianificato · non attivo'));
  } catch (error) {el('secretary-support').textContent = error.message;}
}
function approvalReceipt(identity) {
  return [...el('email-approval-receipts').children].find(item => item.dataset.approvalId === String(identity));
}
function rememberApproval(approval, saved) {
  const previous = approvalReceipt(approval.id); if (previous) previous.remove();
  const receipt = node('span', saved.receipt);
  receipt.dataset.approvalId = String(approval.id);
  receipt.dataset.version = String(approval.version + 1);
  receipt.dataset.approvalOperationKey = saved.operation_key;
  receipt.dataset.payload = JSON.stringify(approval.payload);
  el('email-approval-receipts').append(receipt);
}
function dispatchControl(card, email) {
  const receipt = approvalReceipt(email.approval_id);
  const button = node('button', 'Invio esplicito'); button.type = 'button';
  const validReceipt = receipt && receipt.dataset.version === String(email.expected_version)
    && receipt.dataset.payload === JSON.stringify(email.payload);
  button.disabled = !validReceipt || email.state !== 'approved' || email.payload.documents.length > 0
    || new Date(email.expires_at).getTime() <= Date.now() || email.dispatch_state !== 'not_requested';
  if (!validReceipt) card.append(node('small', 'Invio bloccato senza ricevuta di Approva ottenuta in questa sessione della pagina.'));
  button.onclick = async () => {
    if (!confirm(`Invio esplicito da ${email.account_address} a ${email.payload.recipient}, oggetto “${email.subject}”? Questa operazione può trasmettere l’email.`)) return;
    button.disabled = true;
    const operationKey = `secretary-email-dispatch:dashboard:${email.approval_id}:v${email.expected_version}`;
    try {
      await api(`/api/secretary/emails/${email.approval_id}/dispatch`, {
        expected_version:email.expected_version, account_address:email.account_address,
        subject:email.subject, payload:email.payload,
        approval_operation_key:receipt.dataset.approvalOperationKey, operation_key:operationKey});
      await loadSecretary(); el('email-status').textContent = 'Invio esplicito accodato. Non è una conferma di accettazione, consegna o lettura.';
    } catch (error) {el('email-status').textContent = `${error.message}. Ricarica gli esiti; non reinviare alla cieca.`;}
  };
  card.append(button);
}
function renderExecution(result) {
  el('email-executions').replaceChildren();
  const states = {not_requested:'Non richiesto', queued:'Accodato · non ancora accettato',
    attempted:'Tentativo registrato · esito non confermato', accepted:'Accettato dal connettore · consegna non verificata',
    uncertain:'Esito incerto · nessun reinvio automatico'};
  result.proposals.forEach(email => {
    const card = node('article', ''); card.className = 'approval-card';
    card.append(node('h4', `Email #${email.approval_id} · ${states[email.dispatch_state] || 'Stato sconosciuto'}`),
      node('p', `Mittente: ${email.account_address}`), node('p', `Destinatario: ${email.payload.recipient}`),
      node('p', `Oggetto fisso: ${email.subject}`),
      node('p', `Versione ${email.expected_version} · scadenza ${localDate(email.expires_at)} · ${stateNames[email.state] || email.state}`),
      node('pre', JSON.stringify(email.payload, null, 2)),
      node('p', email.payload.documents.length ? 'Documenti presenti: invio NON supportato. Nessun allegato verrà omesso silenziosamente.' : 'Solo testo · allegati non supportati.'));
    dispatchControl(card, email); el('email-executions').append(card);
  });
  empty(el('email-executions'), 'Nessuna proposta email sigillata in questa vista (massimo 24).');
  const account = el('email-account'); const selected = account.value;
  const placeholder = node('option', 'Scegli esplicitamente il mittente'); placeholder.value = '';
  account.replaceChildren(placeholder);
  result.accounts.forEach(item => {const option = node('option', item.account_address); option.value = item.account_address; account.append(option);});
  account.value = selected;
  el('email-subject').textContent = result.subject;
  el('email-prepare').disabled = false;
}
el('email-compose').oninput = () => {delete el('email-compose').dataset.operationKey;};
el('email-compose').onsubmit = async event => {
  event.preventDefault();
  const form = event.target; const choice = el('email-practice').selectedOptions[0];
  if (!choice || !choice.value || !el('email-account').value) {el('email-status').textContent = 'Seleziona pratica/versione e mittente espliciti.'; return;}
  const operationKey = form.dataset.operationKey || `secretary-approval:email:dashboard:${operationNonce()}`;
  form.dataset.operationKey = operationKey; el('email-prepare').disabled = true;
  try {
    const saved = await api('/api/secretary/emails', {practice_id:Number(choice.value),
      expected_version:Number(choice.dataset.version), account_address:el('email-account').value,
      expires_at:new Date(el('email-expires').value).toISOString(), operation_key:operationKey,
      payload:{action:'send_message', channel:'email', recipient:el('email-recipient').value,
        text:el('email-text').value, documents:[]}});
    form.reset(); delete form.dataset.operationKey;
    await loadSecretary(); el('email-status').textContent = `Email #${saved.email.approval_id} preparata. Nessun invio: controlla il payload, poi Approva e solo dopo Invio esplicito.`;
  } catch (error) {el('email-status').textContent = `${error.message}. Nessun invio richiesto.`;}
  finally {el('email-prepare').disabled = false;}
};
const BUDGET_LEVEL_NAMES = {ok: 'verde', warn: 'giallo', high: 'arancione', full: 'rosso'};
const BUDGET_SCOPE_NAMES = {dialogue: 'dialogo', review: 'revisione', research: 'ricerca', analysis: 'analisi'};
// Soglie richieste: verde <60%, gialla 60–80%, arancione 80–100% (100 escluso), rossa >=100%.
function budgetLevel(ratio) {
  if (!Number.isFinite(ratio)) return 'full';
  if (ratio < 0.6) return 'ok';
  if (ratio < 0.8) return 'warn';
  if (ratio < 1) return 'high';
  return 'full';
}
function budgetMeter(parent, label, used, limit, target) {
  const ratio = limit > 0 ? used / limit : Infinity;
  const level = budgetLevel(ratio);
  const percent = Number.isFinite(ratio) ? Math.round(ratio * 100) : '∞';
  const detail = `${used} / ${limit} · ${percent}%`;
  const meter = node('div', ''); meter.className = 'budget-meter';
  const head = node('div', ''); head.className = 'budget-head';
  head.append(node('span', label), node('small', detail));
  if (level === 'full') {const flag = node('span', 'SATURATO'); flag.className = 'budget-flag'; head.append(flag);}
  if (target) {
    const edit = node('button', 'Modifica questo limite'); edit.type = 'button'; edit.className = 'budget-edit';
    edit.onclick = () => openSettingsGroup(target.group, target.key);
    head.append(edit);
  }
  const track = node('div', ''); track.className = 'budget-track';
  const fill = node('div', ''); fill.className = `budget-fill ${level}`;
  fill.style.width = `${Number.isFinite(ratio) ? Math.min(100, Math.max(0, ratio * 100)) : 100}%`;
  fill.setAttribute('role', 'progressbar');
  fill.setAttribute('aria-valuemin', '0');
  fill.setAttribute('aria-valuemax', String(limit));
  fill.setAttribute('aria-valuenow', String(used));
  fill.setAttribute('aria-label', `${label}: ${detail} · livello ${BUDGET_LEVEL_NAMES[level]}`);
  track.append(fill); meter.append(head, track); parent.append(meter);
  return meter;
}
const AI_LIMITS_GROUP = 'AI · limiti dei Worker (chiamate, token, concorrenza)';
const AI_COST_GROUP = 'AI · tetto di spesa in denaro (opzionale)';
async function loadBudget() {
  el('ai-budget').replaceChildren();
  try {
    const result = await api('/api/secretary/budget'); const usage = result.usage; const limits = result.limits;
    budgetMeter(el('ai-budget'), 'Tentativi HTTP oggi (UTC)', usage.calls, limits.DAILY_CALLS,
      {group: AI_LIMITS_GROUP, key: 'AI_BUDGET_DAILY_CALLS'});
    budgetMeter(el('ai-budget'), 'Token addebitati conservativamente', usage.charged_tokens, limits.DAILY_TOKENS,
      {group: AI_LIMITS_GROUP, key: 'AI_BUDGET_DAILY_TOKENS'});
    budgetMeter(el('ai-budget'), 'Slot prenotati / massimo simultanei', usage.inflight, limits.MAX_INFLIGHT,
      {group: AI_LIMITS_GROUP, key: 'AI_BUDGET_MAX_INFLIGHT'});
    if (result.money_cap_enforced && Number(result.daily_cost) > 0) {
      budgetMeter(el('ai-budget'), 'Costo comunicato / tetto di spesa', Number(usage.reported_cost || 0), Number(result.daily_cost),
        {group: AI_COST_GROUP, key: 'AI_BUDGET_DAILY_COST'});
    }
    Object.keys(result.scope_caps || {}).forEach(scope => {
      budgetMeter(el('ai-budget'), `Budget ${BUDGET_SCOPE_NAMES[scope] || scope}`, (result.scope_usage || {})[scope] || 0, result.scope_caps[scope]);
    });
    // Due metriche distinte, mai un rapporto fuorviante: la prenotazione è una stima per eccesso.
    row(el('ai-budget'), 'Token prenotati in anticipo (stima per eccesso)', String(usage.reserved_tokens));
    row(el('ai-budget'), 'Token comunicati dal provider (consumo reale)', usage.reported_tokens ?? 'non comunicati');
    row(el('ai-budget'), 'Massimo output per tentativo', String(limits.MAX_OUTPUT_TOKENS));
    if (!result.money_cap_enforced) row(el('ai-budget'), 'Costo comunicato (non tetto di spesa)', `${usage.reported_cost ?? 'non comunicato'} · ${usage.cost_unreported_calls} tentativi con costo non comunicato`);
  } catch (error) {el('ai-budget').textContent = `${error.message}. Contabilità non disponibile: nessun consumo nullo presunto.`;}
}
async function loadSecretary() {
  const generation = sessionGeneration;
  el('secretary-refresh').disabled = true;
  try {
    const result = await api('/api/secretary');
    if (generation !== sessionGeneration) return;
    const selectedPractice = el('email-practice').value;
    const placeholder = node('option', 'Scegli una pratica esistente e la sua versione'); placeholder.value = '';
    el('email-practice').replaceChildren(placeholder);
    result.practices.filter(practice => ['active','waiting'].includes(practice.state)).forEach(practice => {
      const option = node('option', `#${practice.id} · v${practice.version} · ${practice.objective}`);
      option.value = String(practice.id); option.dataset.version = String(practice.version); el('email-practice').append(option);
    });
    el('email-practice').value = selectedPractice;
    let execution = null;
    try {
      execution = await api('/api/secretary/execution');
      if (generation !== sessionGeneration) return;
      renderExecution(execution);
    }
    catch (error) {
      // Optional execution failures must not revive a snapshot from another session.
      if (generation !== sessionGeneration) return;
      el('email-executions').textContent = `${error.message}. Esecuzione email/schema opzionale non disponibile.`;
      el('email-prepare').disabled = true;
    }
    ['practices','responsibilities','approvals','secretary-receipts','secretary-decisions'].forEach(id => el(id).replaceChildren());
    result.practices.forEach(practice => {
      const card = node('article', ''); card.className = 'work-card';
      card.append(node('span', `#${practice.id} · ${stateNames[practice.state] || practice.state} · v${practice.version}`), node('h4', practice.objective));
      [['Prossimo passo',practice.next_action],['In attesa di',practice.waiting_for],['Blocco',practice.blocked_reason],['Completamento',practice.completion_criteria],['Revisione',localDate(practice.next_review_at)]].forEach(([label,value]) => {if (value) card.append(node('p', `${label}: ${value}`));});
      if (practice.responsibility_id) card.append(node('small', `Responsabilità #${practice.responsibility_id}`));
      const links = result.links.filter(link => link.practice_id === practice.id);
      links.forEach(link => {
        const id = link.document_id || link.email_message_id || link.telegram_message_id || link.whatsapp_message_id;
        const r = row(card, link.evidence || 'Fonte collegata', `Fonte #${id}`);
        // secretary document IDs are attachments, not knowledge occurrence IDs.
        // Message IDs are shared native identities; documents use the catalog.
        if (!link.document_id && id) r.append(sourceButton({source_type:'message', source_id:id}));
      });
      el('practices').append(card);
    });
    empty(el('practices'), 'Nessuna pratica in questa vista limitata. Le nuove assegnazioni richiedono il percorso proprietario del servizio.');
    result.responsibilities.forEach(responsibility => {
      const card = node('article', ''); card.className = 'work-card';
      card.append(node('span', `#${responsibility.id} · ${stateNames[responsibility.state] || responsibility.state} · v${responsibility.version}`), node('h4', responsibility.objective), node('p', `Ambito: ${responsibility.scope.topic} · ${responsibility.scope.channels.join(', ')}`), node('p', `Azioni ammesse: ${responsibility.allowed_actions.join(', ')}`), node('p', `Stop: ${responsibility.stop_condition}`));
      const controls = node('div', ''); controls.className = 'action-bar';
      (responsibility.state === 'active' ? ['suspend','revoke'] : responsibility.state === 'suspended' ? ['revoke'] : []).forEach(action => {
        const button = node('button', action === 'suspend' ? 'Sospendi' : 'Revoca');
        button.onclick = async () => {
          if (!confirm(`${button.textContent} responsabilità #${responsibility.id}? Le approvazioni collegate saranno revocate.${action === 'revoke' ? ' La revoca è definitiva.' : ''}`)) return;
          controls.querySelectorAll('button').forEach(b => {b.disabled = true;});
          try {
            const saved = await api(`/api/secretary/responsibilities/${responsibility.id}`, {action, expected_version:responsibility.version});
            await loadSecretary(); el('secretary-status').textContent = saved.receipt;
          } catch (error) {el('secretary-status').textContent = `${error.message}. Aggiorna la vista prima di riprovare.`;}
        }; controls.append(button);
      }); card.append(controls); el('responsibilities').append(card);
    });
    empty(el('responsibilities'), 'Nessuna responsabilità in questa vista.');
    result.approvals.forEach(approval => {
      const card = node('article', ''); card.className = 'approval-card';
      const email = execution && execution.proposals.find(item => item.approval_id === approval.id);
      card.append(node('h4', `Approvazione #${approval.id} · pratica #${approval.practice_id} · ${stateNames[approval.effective_state] || approval.state}`), node('p', `Versione ${approval.version} · scadenza ${localDate(approval.expires_at)}`), node('small', `SHA-256 payload: ${approval.payload_hash}`));
      card.append(node('pre', JSON.stringify(approval.payload, null, 2)));
      if (email) card.append(node('p', `Mittente: ${email.account_address} · oggetto fisso: ${email.subject} · solo testo; documenti non supportati.`));
      const controls = node('div', ''); controls.className = 'action-bar';
      (approval.effective_state === 'pending' ? ['approve','revoke'] : approval.state !== 'revoked' ? ['revoke'] : []).forEach(decision => {
        const button = node('button', decision === 'approve' ? 'Approva payload esatto' : 'Revoca approvazione');
        if (decision === 'approve' && approval.payload.channel === 'email' && !email) button.disabled = true;
        button.onclick = async () => {
          if (!confirm(`${button.textContent} #${approval.id}, versione ${approval.version}? Nessun invio verrà eseguito.`)) return;
          controls.querySelectorAll('button').forEach(b => {b.disabled = true;});
          try {
            const saved = await api(`/api/secretary/approvals/${approval.id}`, {payload:approval.payload, decision, expected_version:approval.version, operation_key:`secretary-approval:dashboard:${approval.id}:v${approval.version}:${decision}`});
            if (decision === 'approve') rememberApproval(approval, saved);
            else {const receipt = approvalReceipt(approval.id); if (receipt) receipt.remove();}
            await loadSecretary(); el('secretary-status').textContent = saved.receipt;
          } catch (error) {el('secretary-status').textContent = `${error.message}. Aggiorna la vista; nessun esito esterno verificato.`;}
        }; controls.append(button);
      }); card.append(controls); el('approvals').append(card);
    });
    empty(el('approvals'), 'Nessuna proposta in questa vista.');
    result.receipts.forEach(receipt => row(el('secretary-receipts'), receipt.receipt, localDate(receipt.created_at)));
    empty(el('secretary-receipts'), 'Nessuna ricevuta locale disponibile.');
    result.decisions.forEach(decision => row(el('secretary-decisions'), decision.event, localDate(decision.created_at)));
    result.wakeups.forEach(wakeup => row(el('secretary-decisions'), `Pratica #${wakeup.practice_id} · revisione accodata: ${wakeup.reason}`, 'Non eseguita da questa pagina'));
    el('secretary-status').textContent = `Snapshot manuale · massimo 24 elementi per raccolta.${Object.values(result.truncated).some(Boolean) ? ' Altri elementi non mostrati.' : ''} La lettura non avvia invii. Controlla gli esiti email separati.`;
  } catch (error) {
    if (generation !== sessionGeneration) return;
    el('secretary-status').textContent = `${error.message}. Snapshot non aggiornato; verificare integrazione e migrazioni del servizio.`;
    ['responsibilities','approvals','email-executions'].forEach(id => el(id).querySelectorAll('button').forEach(button => {button.disabled = true;}));
    el('email-prepare').disabled = true;
  }
  finally {if (generation === sessionGeneration) el('secretary-refresh').disabled = false;}
}
function runtimeOwnerControl(card, label, path, data, confirmation, success) {
  const generation = sessionGeneration;
  const button = node('button', label); button.type = 'button';
  button.onclick = async () => {
    if (generation !== sessionGeneration || !confirm(confirmation)) return;
    button.disabled = true;
    try {
      const saved = await api(path, data);
      if (generation !== sessionGeneration) return;
      await loadSecretary();
      if (generation !== sessionGeneration) return;
      await loadRuntime();
      if (generation !== sessionGeneration) return;
      el('runtime-status').textContent = saved.acknowledgement ? saved.acknowledgement.receipt : success;
    } catch (error) {
      if (generation === sessionGeneration) el('runtime-status').textContent = `${error.message}. Ricarica prima di riprovare; nessuna autorizzazione aggiunta.`;
    }
  };
  card.append(button);
}
async function loadRuntime() {
  const generation = sessionGeneration;
  try {
    const result = await api('/api/secretary/runtime');
    if (generation !== sessionGeneration) return;
    el('runtime-reviews').replaceChildren(); el('runtime-links').replaceChildren();
    (result.attention || []).forEach(attention => {
      const card = node('article', ''); card.className = 'source-card';
      card.append(node('h4', `Attenzione · pratica #${attention.practice_id} · v${attention.practice_version}`),
        node('small', `SHA-256 attenzione: ${attention.last_hash} · ${attention.acknowledged_at ? 'Già presa in carico' : 'Da prendere in carico'}`));
      const proposal = attention.proposal || {};
      ['summary','evidence','question','draft','next_action'].forEach(field => {if (proposal[field]) card.append(node('pre', proposal[field]));});
      card.append(node('small', (proposal.citations || []).join(', ')));
      if (!attention.acknowledged_at && Number.isSafeInteger(attention.practice_id)
          && Number.isSafeInteger(attention.practice_version) && /^[0-9a-f]{64}$/.test(attention.last_hash)
          && proposal.evidence) {
        runtimeOwnerControl(card, 'Presa in carico · contenuto esatto',
          `/api/secretary/runtime/practices/${attention.practice_id}/acknowledge`,
          {expected_version:attention.practice_version, attention_hash:attention.last_hash},
          `Prendere in carico l’attenzione mostrata per la pratica #${attention.practice_id}, v${attention.practice_version}, SHA-256 ${attention.last_hash}? Silenzia questo contenuto, senza completare la pratica o ampliare permessi.`, '');
      }
      el('runtime-reviews').append(card);
    });
    result.reviews.forEach(review => {
      const card = node('article', ''); card.className = 'source-card';
      card.append(node('h4', `Pratica #${review.practice_id} · revisione ${review.status}`));
      const proposal = review.proposal || {};
      ['summary','evidence','question'].forEach(field => {if (proposal[field]) card.append(node('p', proposal[field]));});
      if (proposal.draft) {
        const draft = node('pre', proposal.draft); draft.className = 'source-text'; card.append(draft);
        const prepare = node('button', 'Prepara invio'); prepare.type = 'button';
        prepare.onclick = () => {
          if (generation !== sessionGeneration) return;
          el('email-practice').value = String(review.practice_id);
          el('email-text').value = draft.textContent;
          el('email-recipient').value = ''; el('email-account').value = ''; el('email-expires').value = '';
          delete el('email-compose').dataset.operationKey;
          el('email-status').textContent = 'Bozza copiata, non inviata. Scegli pratica/versione corrente, destinatario, mittente e scadenza espliciti.';
          el('email-compose').scrollIntoView({behavior:'smooth', block:'center'}); el('email-recipient').focus();
        }; card.append(prepare);
      }
      card.append(node('small', `${(proposal.citations || []).join(', ')} · Bozza/proposta locale, nessun invio a terzi.`));
      el('runtime-reviews').append(card);
    });
    result.link_proposals.forEach(link => {
      const card = node('article', ''); card.className = 'source-card';
      card.append(node('h4', `Collegamento #${link.id} · v${link.version} · ${link.state}`),
        node('p', `Pratica #${link.practice_id} · v${link.practice_version} · responsabilità #${link.responsibility_id} · v${link.responsibility_version}`),
        node('p', `Messaggio #${link.message_id} · voce #${link.entry_id} · revisione ${link.revision}`),
        node('pre', link.evidence), sourceButton({source_type:'entry', source_id:link.entry_id}));
      if (link.state === 'proposed' && Number.isSafeInteger(link.id) && Number.isSafeInteger(link.version)) {
        runtimeOwnerControl(card, 'Accetta questo collegamento',
          `/api/secretary/runtime/links/${link.id}/accept`, {expected_version:link.version},
          `Accettare il collegamento #${link.id}, v${link.version}, alla pratica #${link.practice_id} v${link.practice_version}, responsabilità #${link.responsibility_id} v${link.responsibility_version}? Solo l’evidenza mostrata della voce #${link.entry_id}, revisione ${link.revision}. Nessuna delega o permesso aggiunto.`,
          `Collegamento #${link.id} accettato. Nessun invio esterno o ampliamento dei permessi.`);
      }
      el('runtime-links').append(card);
    });
    empty(el('runtime-reviews'), 'Nessuna revisione in questa vista (massimo 20).');
    empty(el('runtime-links'), 'Nessun collegamento proposto.');
    el('runtime-status').textContent = 'Runtime limitato a revisioni e proposte. Nessun tool arbitrario o invio a terzi.';
  } catch (error) {
    if (generation !== sessionGeneration) return;
    el('runtime-status').textContent = `${error.message}. Runtime opzionale o schema non disponibile.`;
    ['runtime-reviews','runtime-links'].forEach(id => el(id).querySelectorAll('button').forEach(button => {button.disabled = true;}));
  }
}
el('secretary-refresh').onclick = () => {loadSecretary(); loadKnowledge(); loadRuntime(); loadBudget();};
el('budget-settings').onclick = () => openSettingsGroup(AI_LIMITS_GROUP, 'AI_BUDGET_MAX_INFLIGHT');
function loadWorkspace() {return Promise.all([loadSecretary(), loadKnowledge(), loadPolicies(), loadSupport(), loadRuntime(), loadBudget()]);}
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
    (d.jobs||[]).forEach(j=>row(el('activity'),`${j.kind} · ${j.status}: ${j.count}`,`età massima ${Math.round(j.oldest_age_seconds||0)}s · ultimo successo ${j.last_success||'mai'}${j.failure_reason?` · motivo: ${j.failure_reason}`:''}`));
    row(el('activity'),'Notifiche confermate',String(d.notifications_sent||0));
    (d.calendar_pending||[]).forEach(a=>row(el('activity'),`Calendario #${a[0]} ${a[1]}`,`${a[2]} · ${a[3]}`));
    const invoiceActive = d.flows.some(f => ['invoices','monthly_zip'].includes(f.id) && f.enabled);
    el('invoice-inactive').hidden = invoiceActive;
    if (el('invoice-history').dataset.active !== String(invoiceActive)) {
      el('invoice-history').open = invoiceActive; el('invoice-history').dataset.active = String(invoiceActive);
    }
    [['invoice-records','invoices'],['zip-records','monthly_zip']].forEach(([id,flow]) => {
      const active = d.flows.some(f => f.id === flow && f.enabled);
      if (el(id).dataset.active !== String(active)) {el(id).open = active; el(id).dataset.active = String(active);}
    });
    el('invoices').replaceChildren();d.invoices.forEach(r=>row(el('invoices'),`${r[0]} · ${r[1]}`,`${r[2]} documenti`));
    el('zips').replaceChildren();d.zips.forEach(r=>row(el('zips'),`ZIP ${r[0]} · ${r[1]}`,r[2]?'inviato':'non inviato'));
  } catch(e) {el('status').textContent=e.message;}
}
el('login').onsubmit=async e=>{e.preventDefault();el('error').textContent='';try{const data=Object.fromEntries(new FormData(e.target));const result=await api('/api/login',data);csrf=result.csrf;e.target.reset();show(true);showPage(location.hash.slice(1)||'overview');await refresh();await loadWorkspace();await browseFiles();}catch(err){el('error').textContent=err.message;}};
el('logout').onclick=async()=>{await api('/api/logout',{});csrf='';show(false);};
api('/api/session').then(s=>{csrf=s.csrf;show(true);showPage(location.hash.slice(1)||'overview');refresh();loadWorkspace();browseFiles();}).catch(()=>{});
setInterval(()=>{if(!el('dashboard').hidden)refresh();},10000);