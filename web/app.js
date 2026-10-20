'use strict';
let csrf = '';
let sessionGeneration = 0;
const el = id => document.getElementById(id);
const node = (tag, text) => { const n = document.createElement(tag); n.textContent = text; return n; };
// Navigazione multi-pagina: una sola pagina visibile alla volta.
const PAGES = ['overview', 'agent', 'ai-permissions', 'integration-settings', 'archive'];
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
    ['agent-runs','agent-emails','agent-calls','data-policies','ai-budget'].forEach(id => el(id).replaceChildren());
    el('agent-detail').hidden = true; el('media-retry-status').textContent = '';
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
const fieldNames = {AGENT_NAME:'Nome assistente', OPENROUTER_API_KEY:'Chiave API OpenRouter', OPENROUTER_MODEL:'Modello per immagini e media', AGENT_MODEL:'Modello dell’agente', AGENT_WEB_MODEL:'Modello per la ricerca web', AGENT_NOTIFY_MAX_PER_DAY:'Massimo notifiche urgenti al giorno', DATABASE_URL:'Connessione PostgreSQL', TELEGRAM_BOT_TOKEN:'Token del bot', TELEGRAM_CHAT_ID:'Proprietario autorizzato', TELEGRAM_USER_ENABLED:'Account personale', TELEGRAM_API_ID:'ID applicazione Telegram', TELEGRAM_API_HASH:'Segreto applicazione Telegram', TELEGRAM_PHONE:'Numero di telefono', WHATSAPP_ENABLED:'Connessione WhatsApp', GCAL_ENABLED:'Abilita calendario', GCAL_CALENDAR_ID:'ID del calendario condiviso', GCAL_EVENT_DURATION:'Durata appuntamenti (minuti)', GCAL_SERVICE_ACCOUNT_JSON:'Nuova chiave service account (JSON)', TIMEZONE:'Fuso orario', LANGUAGE:'Lingua', DIGEST_TIME:'Orario riepilogo', NOTIFY_PRIORITY_THRESHOLD:'Priorità minima notifiche', POLL_INTERVAL:'Intervallo controllo (secondi)'};
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
const localDate = value => value ? new Date(value).toLocaleString('it') : 'Non prevista';
function empty(parent, text) {if (!parent.children.length) parent.append(node('p', text));}
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
const BUDGET_LEVEL_NAMES = {ok: 'verde', warn: 'giallo', high: 'arancione', full: 'rosso'};
const BUDGET_SCOPE_NAMES = {agent_owner: 'agente · tue richieste', agent_inbox: 'agente · posta in arrivo'};
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
    const result = await api('/api/budget'); const usage = result.usage; const limits = result.limits;
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
    // Slot bloccati da worker morti: azione di riparazione immediata.
    const orphaned = result.orphaned || [];
    if (orphaned.length) {
      const panel = node('div', ''); panel.className = 'budget-repair';
      panel.append(node('strong', `⚠ ${orphaned.length} slot bloccati da worker morti (job non più in esecuzione)`));
      const actions = node('div', ''); actions.className = 'budget-repair-actions';
      const free = node('button', 'Libera slot'); free.type = 'button';
      free.onclick = async () => {
        free.disabled = true;
        try {const r = await api('/api/budget/reconcile', {});
             panel.append(node('small', `Slot liberati: ${r.freed}`)); await loadBudget();}
        catch (error) {free.disabled = false; panel.append(node('small', error.message));}
      };
      const requeue = node('button', 'Riaccoda posta fallita'); requeue.type = 'button';
      requeue.onclick = async () => {
        requeue.disabled = true;
        try {const r = await api('/api/jobs/requeue', {kind: 'analysis'});
             panel.append(node('small', `Messaggi riaccodati: ${r.requeued.length}`));}
        catch (error) {requeue.disabled = false; panel.append(node('small', error.message));}
      };
      actions.append(free, requeue); panel.append(actions);
      el('ai-budget').append(panel);
    }
  } catch (error) {el('ai-budget').textContent = `${error.message}. Contabilità non disponibile: nessun consumo nullo presunto.`;}
}
const RUN_KINDS = {owner: 'Dialogo con te', inbox: 'Posta in arrivo', digest: 'Riepilogo serale'};
const RUN_STATES = {running: 'in corso', done: 'completata', failed: 'fallita'};
const DRAFT_STATES = {proposed: 'In attesa di tua decisione', approved: 'Approvata · invio in coda', rejected: 'Scartata',
  sent: 'Inviata · consegna non verificata', failed: 'Fallita', uncertain: 'Esito incerto · nessun reinvio automatico'};
async function openRun(run) {
  const generation = sessionGeneration;
  try {
    const result = await api(`/api/agent/runs/${run.id}`);
    if (generation !== sessionGeneration) return;
    el('agent-calls').replaceChildren();
    result.calls.forEach(call => {
      const r = row(el('agent-calls'), `${call.seq}. ${call.tool} · ${call.ok ? 'ok' : 'errore'}`, `${call.duration_ms ?? '?'} ms`);
      const detail = node('pre', `${call.args || ''}\n→ ${call.summary || ''}`); detail.className = 'source-text'; r.append(detail);
    });
    empty(el('agent-calls'), 'Nessuno strumento usato in questa esecuzione.');
    el('agent-detail-title').textContent = `Esecuzione #${run.id} · ${RUN_KINDS[run.kind] || run.kind}`;
    el('agent-detail').hidden = false;
  } catch (error) {if (generation === sessionGeneration) el('agent-status').textContent = error.message;}
}
async function loadAgent() {
  const generation = sessionGeneration;
  el('agent-refresh').disabled = true;
  try {
    const result = await api('/api/agent');
    if (generation !== sessionGeneration) return;
    el('agent-runs').replaceChildren(); el('agent-emails').replaceChildren();
    result.runs.forEach(run => {
      const r = row(el('agent-runs'), `#${run.id} · ${RUN_KINDS[run.kind] || run.kind} · ${RUN_STATES[run.state] || run.state}`,
        `${run.tool_calls} strumenti · ${localDate(run.started_at)}${run.error ? ` · ${run.error}` : ''}`);
      const open = node('button', 'Strumenti usati'); open.type = 'button'; open.onclick = () => openRun(run); r.append(open);
    });
    empty(el('agent-runs'), 'Nessuna esecuzione registrata.');
    result.emails.forEach(email => {
      const card = node('article', ''); card.className = 'approval-card';
      card.append(node('h4', `Bozza #${email.id} · ${DRAFT_STATES[email.state] || email.state}`),
        node('p', `Da ${email.account} a ${email.recipient}`), node('p', `Oggetto: ${email.subject}`),
        node('pre', email.body));
      if (email.state === 'proposed') card.append(node('small', `Per decidere rispondi al bot: /invia ${email.id} oppure /scarta ${email.id}.`));
      el('agent-emails').append(card);
    });
    empty(el('agent-emails'), 'Nessuna bozza di email.');
    el('agent-model').textContent = `${result.model}`;
    el('agent-role').textContent = result.readonly_role ? 'attivo (ruolo di sola lettura del database)' : 'NON attivo: le query libere usano solo la guardia SQL (vedi deploy/agent_ro.sql)';
    el('agent-send').textContent = result.sends_allowed ? 'abilitato (modalità active)' : 'disabilitato (modalità observe): le bozze non vengono inviate';
    el('agent-status').textContent = 'Solo metadati delle esecuzioni (30 più recenti): nessun prompt né risposta del modello.';
  } catch (error) {
    if (generation === sessionGeneration) el('agent-status').textContent = `${error.message}. Vista dell’agente non disponibile.`;
  } finally {if (generation === sessionGeneration) el('agent-refresh').disabled = false;}
}
el('agent-refresh').onclick = loadAgent;
el('media-retry').onsubmit = async event => {
  event.preventDefault();
  const generation = sessionGeneration;
  const id = Number(el('media-retry-id').value); const channel = el('media-retry-channel').value;
  if (!Number.isSafeInteger(id) || id <= 0) {el('media-retry-status').textContent = 'Indica l’ID numerico del messaggio.'; return;}
  if (!confirm(`Riprovare i media bloccati per consenso del messaggio #${id} (${channel})? Può inviare audio o immagini al provider AI. Lo storico e i tentativi già registrati restano esclusi.`)) return;
  const button = event.target.querySelector('button'); button.disabled = true;
  try {
    const result = await api('/api/media/retry-blocked', {message_id: id, expected_channel: channel});
    if (generation !== sessionGeneration) return;
    el('media-retry-status').textContent = result.queued ? `${result.queued} media accodati.` : 'Nessun media recuperabile: già accodato, non bloccato per consenso o limite raggiunto.';
  } catch (error) {if (generation === sessionGeneration) el('media-retry-status').textContent = `${error.message}. Verifica entrambi i consensi AI.`;}
  finally {button.disabled = false;}
};
el('budget-settings').onclick = () => openSettingsGroup(AI_LIMITS_GROUP, 'AI_BUDGET_MAX_INFLIGHT');
function loadWorkspace() {return Promise.all([loadAgent(), loadPolicies(), loadBudget()]);}
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