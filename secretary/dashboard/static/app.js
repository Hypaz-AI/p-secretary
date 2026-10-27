'use strict';
// p-secretary · dashboard. JavaScript senza dipendenze né build: ogni testo passa da textContent o da nodi DOM
// (mai da markup), così nessun contenuto dell'archivio può diventare codice. I grafici sono in charts.js.
let csrf = '';
let sessionGeneration = 0;
const el = id => document.getElementById(id);
const node = (tag, text) => {const n = document.createElement(tag); n.textContent = text; return n;};
const DOM_PROPS = new Set(['hidden', 'disabled', 'checked', 'value', 'type', 'name', 'id', 'min', 'max', 'step',
  'placeholder', 'href', 'htmlFor', 'download']);
// Costruttore di nodi: h('div', {class, text, dataset, onclick, 'aria-label'}, ...figli)
function h(tag, props, ...kids) {
  const n = document.createElement(tag);
  Object.entries(props || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === false) return;
    if (key === 'class') n.className = value;
    else if (key === 'text') n.textContent = value;
    else if (key === 'dataset') Object.assign(n.dataset, value);
    else if (key.startsWith('on') || DOM_PROPS.has(key)) n[key] = value;
    else n.setAttribute(key, value === true ? '' : value);
  });
  kids.flat(2).forEach(kid => {if (kid !== undefined && kid !== null && kid !== false) n.append(kid);});
  return n;
}

// ------------------------------------------------------------------ icone
const ICONS = {
  mail: ['M4 6.5h16v11H4z', 'M4.4 7.2L12 13l7.6-5.8'],
  chat: ['M4 5.5h16v10.5H9.5L5 19.5V16H4z'],
  phone: ['M6 3.8h3l1.5 4-2 1.3a10 10 0 0 0 5.4 5.4l1.3-2 4 1.5v3a2 2 0 0 1-2.2 2A15 15 0 0 1 4 6a2 2 0 0 1 2-2.2z'],
  folder: ['M3.5 7a2 2 0 0 1 2-2H10l2 2.2h6.5a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z'],
  file: ['M7 3.5h7l4.5 4.5v12.5H7z', 'M14 3.5V8h4.5'],
  inbound: ['M17.5 6.5l-11 11', 'M6.5 9v8.5H15'],
  outbound: ['M6.5 17.5l11-11', 'M9 6.5h8.5V15'],
  bolt: ['M13 3L5.5 13.5H11L10 21l7.5-10.5H12z'],
  task: ['M5 6.5l1.6 1.6L9.5 5', 'M12 7h7.5', 'M5 13l1.6 1.6L9.5 11.5', 'M12 13.5h7.5', 'M5 19.5h4.5', 'M12 19.5h7.5'],
  calendar: ['M4.5 6.5h15v13h-15z', 'M4.5 10.5h15', 'M8.5 4v3.5', 'M15.5 4v3.5'],
  alert: ['M12 4l9 16H3z', 'M12 10v4.5', 'M12 17.5v.1'],
  check: ['M5 12.5l4.5 4.5L19 7.5'],
  download: ['M12 4v11', 'M7.5 11l4.5 4.5 4.5-4.5', 'M5 19.5h14'],
  trash: ['M5 7h14', 'M9.5 7V4.5h5V7', 'M7 7l.8 12.5h8.4L17 7'],
  chevron: ['M9 6l6 6-6 6'],
  agent: ['M4 8h16v11H4z', 'M12 8V4.8', 'M9.5 13.5v.1', 'M14.5 13.5v.1']
};
function icon(name, cls) {
  return svg('svg', {viewBox: '0 0 24 24', class: cls || 'ico', 'aria-hidden': 'true'},
    (ICONS[name] || ICONS.file).map(d => svg('path', {d})));
}
const channelName = channel => ({email: 'Email', telegram: 'Telegram', 'telegram (account)': 'Telegram personale',
  whatsapp: 'WhatsApp'}[channel] || channel);
const channelIcon = channel => (channel === 'email' ? 'mail' : channel === 'whatsapp' ? 'phone'
  : String(channel).startsWith('telegram') ? 'chat' : 'file');

// --------------------------------------------------------------- formattatori
const numberFormat = new Intl.NumberFormat('it');
const fmt = value => numberFormat.format(Number(value) || 0);
const relativeTime = new Intl.RelativeTimeFormat('it', {numeric: 'auto'});
function ago(value) {
  if (!value) return '—';
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return 'adesso';
  if (abs < 3600) return relativeTime.format(Math.round(seconds / 60), 'minute');
  if (abs < 86400) return relativeTime.format(Math.round(seconds / 3600), 'hour');
  return relativeTime.format(Math.round(seconds / 86400), 'day');
}
const localDate = value => (value ? new Date(value).toLocaleString('it') : 'Non prevista');
function sizeLabel(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toLocaleString('it', {maximumFractionDigits: 1})} KiB`;
  return `${(size / 1024 / 1024).toLocaleString('it', {maximumFractionDigits: 1})} MiB`;
}
// Larghezza reale del contenitore: i grafici si disegnano 1:1 (testo nitido) e si ridisegnano al resize.
const chartWidth = box => Math.max(320, box.clientWidth || 640);
const monthName = month => new Date(`${month}-01T12:00:00`).toLocaleDateString('it', {month: 'short', year: '2-digit'});

// ------------------------------------------------------------ stato e sessione
const state = {};
function resetState() {
  Object.assign(state, {dashboard: null, trends: null, budget: null, agent: null, hiddenChannels: new Set(),
    direction: 'all', metric: 'tokens', runFilter: 'all', runsShown: 10, activityShown: 8, files: [], filesShown: 25,
    healthAt: 0, configLoaded: false, budgetAt: 0, tick: 0});
}
resetState();

const THEME_KEY = 'theme';
function effectiveTheme() {
  const root = document.documentElement;
  if (root && root.dataset.theme) return root.dataset.theme;
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}
function applyTheme(theme) {
  if (document.documentElement) document.documentElement.dataset.theme = theme;
}
// Solo la preferenza grafica in un cookie: nessun dato privato resta nel browser.
const savedTheme = /(?:^|; )theme=(light|dark)/.exec(document.cookie || '');
if (savedTheme) applyTheme(savedTheme[1]);
el('theme-toggle').onclick = () => {
  const next = effectiveTheme() === 'light' ? 'dark' : 'light';
  applyTheme(next);
  document.cookie = `${THEME_KEY}=${next}; Path=/; Max-Age=31536000; SameSite=Strict`;
};

// Navigazione multi-pagina: una sola pagina visibile alla volta.
const PAGES = ['overview', 'agent', 'ai-permissions', 'budget', 'integration-settings', 'archive'];
const PAGE_INFO = {
  overview: ['Panoramica', 'Cosa sta succedendo adesso, in un colpo d’occhio'],
  agent: ['Agente', 'Esecuzioni, strumenti usati e bozze di email'],
  'ai-permissions': ['Permessi AI', 'Quali dati può leggere l’intelligenza artificiale'],
  budget: ['Budget AI', 'Consumo giornaliero, limiti e saturazione'],
  'integration-settings': ['Impostazioni', 'Servizi collegati e preferenze'],
  archive: ['Archivio e file', 'File autorizzati, fatture e ZIP mensili']
};
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
  el('page-title').textContent = PAGE_INFO[name][0];
  el('page-subtitle').textContent = PAGE_INFO[name][1];
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  window.scrollTo({top: 0});
  if (!el('dashboard').hidden) loadPageData(name);
}
function renderCharts(name) {
  if (name === 'overview') renderMessages();
  if (name === 'agent') renderRunsChart();
  if (name === 'budget') renderBudgetChart();
  if (name === 'archive' && state.dashboard) renderArchive(state.dashboard);
}
function loadPageData(name) {
  renderCharts(name);
  if (name === 'budget' && Date.now() - state.budgetAt > 15000) loadBudget();
  if (name === 'integration-settings' && !state.configLoaded) loadConfiguration();
}
document.querySelectorAll('.nav-link').forEach(button => {
  button.onclick = () => showPage(button.dataset.page);
});
window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));
let resizeTimer = 0;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {if (!el('dashboard').hidden) renderCharts(currentPage);}, 150);
});
// getRandomValues also works on trusted-LAN HTTP; randomUUID requires HTTPS.
const operationNonce = () => [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, '0')).join('');

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
const PRIVATE_IDS = ['kpis', 'messages-chart', 'messages-legend', 'overview-budget', 'overview-agent', 'flows', 'jobs-chart',
  'jobs-list', 'traffic', 'calendar-pending', 'activity', 'health-results', 'agent-runs', 'agent-emails', 'agent-calls',
  'agent-runs-chart', 'agent-run-filters', 'data-policies', 'ai-budget', 'budget-chart', 'invoices', 'invoice-chart',
  'zips', 'file-list', 'config-fields', 'config-groups'];
function show(connected) {
  el('login').hidden = connected; el('dashboard').hidden = !connected; el('logout').hidden = !connected;
  el('refresh-now').hidden = !connected;
  el('app').dataset.auth = connected ? 'in' : 'out';
  if (!connected) {
    sessionGeneration++;
    resetState();
    PRIVATE_IDS.forEach(id => el(id).replaceChildren());
    el('config-form').hidden = true; el('config-save').hidden = true; configRevision = '';
    el('agent-detail').hidden = true; el('media-retry-status').textContent = '';
    ['messages-readout', 'budget-readout', 'agent-draft-count', 'health-status', 'policy-status', 'file-status'].forEach(id => {el(id).textContent = '';});
    el('status').textContent = 'Accesso protetto alla dashboard locale'; el('status').dataset.state = '';
    el('page-title').textContent = 'Control room'; el('page-subtitle').textContent = 'Accesso protetto alla dashboard locale';
  }
}

// ------------------------------------------------------------ componenti comuni
function toast(message, kind) {
  const item = h('div', {class: 'toast', dataset: {kind: kind || 'ok'}, text: message});
  el('toasts').append(item);
  setTimeout(() => item.remove && item.remove(), 5200);
}
// Conferma modale (sostituisce confirm() del browser). Risolve true/false.
function confirmDialog({title, message, ok = 'Conferma', danger = false}) {
  return new Promise(resolve => {
    const dialog = el('dialog');
    const previous = document.activeElement;
    const close = value => {
      dialog.hidden = true; dialog.onkeydown = null;
      if (previous && previous.focus) previous.focus();
      resolve(value);
    };
    el('dialog-title').textContent = title;
    el('dialog-body').textContent = message;
    el('dialog-ok').textContent = ok;
    el('dialog-ok').className = danger ? 'btn btn-danger' : 'btn btn-primary';
    el('dialog-ok').onclick = () => close(true);
    el('dialog-cancel').onclick = () => close(false);
    dialog.onkeydown = event => {if (event.key === 'Escape') close(false);};
    dialog.hidden = false;
    if (el('dialog-ok').focus) el('dialog-ok').focus();
  });
}
// Controllo a scelta singola (al posto delle tendine): il valore corrente vive in container.value.
function setSegmented(container, value) {
  container.value = value;
  container.querySelectorAll('button').forEach(button => button.setAttribute('aria-checked', String(button.dataset.value === value)));
}
function segmented(container, options, value, onchange) {
  container.value = value;
  // Ogni opzione e` [valore, etichetta, bloccata?]: una opzione bloccata resta visibile ma non scegliibile.
  container.replaceChildren(...options.map(([optionValue, label, fixed]) => {
    const button = h('button', {type: 'button', role: 'radio', text: label, dataset: {value: optionValue, fixed: fixed ? 'true' : ''},
      'aria-checked': String(optionValue === value), disabled: !!fixed});
    button.onclick = () => {
      if (container.disabled) return;
      setSegmented(container, optionValue);
      if (onchange) onchange(optionValue);
    };
    return button;
  }));
  container.setDisabled = flag => {
    container.disabled = flag;
    container.querySelectorAll('button').forEach(button => {button.disabled = flag || button.dataset.fixed === 'true';});
  };
  return container;
}
function toggleSwitch({checked, label, onchange}) {
  const input = h('input', {type: 'checkbox', checked, role: 'switch', 'aria-label': label});
  input.onchange = () => onchange(input);
  return h('label', {class: 'switch'}, input, h('span', {class: 'track'}));
}
function bindTabs(tabs, onSelect) {
  const select = index => {
    tabs.forEach(([tabId, panelId], position) => {
      el(tabId).setAttribute('aria-selected', String(position === index));
      el(tabId).tabIndex = position === index ? 0 : -1;
      el(panelId).hidden = position !== index;
    });
    if (onSelect) onSelect(index);
  };
  tabs.forEach(([tabId], index) => {
    el(tabId).onclick = () => select(index);
    el(tabId).onkeydown = event => {
      const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
      if (!step) return;
      const next = (index + step + tabs.length) % tabs.length;
      select(next); el(tabs[next][0]).focus();
    };
  });
}
const badge = (text, kind) => h('span', {class: `badge${kind ? ' badge-' + kind : ''}`, text});
const emptyState = text => h('div', {class: 'empty', text});
function pagedButton(button, total, shown) {
  const rest = total - shown;
  button.hidden = rest <= 0;
  button.textContent = `Mostra altri ${Math.min(rest, 15)}${rest > 15 ? ` · ${rest} nascosti` : ''}`;
}
function skeleton(box) {box.replaceChildren(h('div', {class: 'skeleton'}));}

// ---------------------------------------------------------------- salute
const healthLabels = {tested: 'Verificato in rete', observed: 'Solo stato osservato', disabled: 'Disabilitato', unconfigured: 'Non configurato', skipped: 'Non eseguito', failed: 'Verifica fallita', timeout: 'Tempo scaduto'};
const healthKind = {tested: 'ok', observed: 'info', failed: 'bad', timeout: 'bad'};
el('health-check').onclick = async () => {
  const generation = sessionGeneration;
  const button = el('health-check');
  button.disabled = true;
  el('health-results').setAttribute('aria-busy', 'true');
  el('health-status').textContent = 'Verifica in corso… massimo circa 12 secondi. I risultati precedenti, se presenti, non sono aggiornati.';
  try {
    const result = await api('/api/platform-health', {});
    el('health-results').replaceChildren(...result.results.map(check => h('article', {class: 'health'},
      h('header', {}, h('span', {class: `dot dot-${healthKind[check.status] || ''}`}), check.label),
      badge(healthLabels[check.status] || 'Stato sconosciuto', healthKind[check.status]),
      h('p', {text: check.explanation}),
      h('small', {text: `${new Date(check.checked_at).toLocaleString('it')} · ${check.latency_ms} ms`}))));
    el('health-status').textContent = `Controllo completato ${new Date(result.completed_at).toLocaleString('it')}. “Verificato” copre solo l’operazione indicata, non l’intero flusso. Attesa minima tra controlli: 30 secondi.`;
  } catch (error) {
    if (generation === sessionGeneration) el('health-status').textContent = `${error.message} I risultati precedenti non sono aggiornati.`;
  } finally {
    button.disabled = false; el('health-results').setAttribute('aria-busy', 'false');
  }
};

// ------------------------------------------------------------- impostazioni
const fieldNames = {AGENT_NAME: 'Nome assistente', OPENROUTER_API_KEY: 'Chiave API OpenRouter', OPENROUTER_MODEL: 'Modello per immagini e media', AGENT_MODEL: 'Modello dell’agente', AGENT_WEB_MODEL: 'Modello per la ricerca web', AGENT_NOTIFY_MAX_PER_DAY: 'Massimo notifiche urgenti al giorno', DATABASE_URL: 'Connessione PostgreSQL', TELEGRAM_BOT_TOKEN: 'Token del bot', TELEGRAM_CHAT_ID: 'Proprietario autorizzato', TELEGRAM_USER_ENABLED: 'Account personale', TELEGRAM_API_ID: 'ID applicazione Telegram', TELEGRAM_API_HASH: 'Segreto applicazione Telegram', TELEGRAM_PHONE: 'Numero di telefono', WHATSAPP_ENABLED: 'Connessione WhatsApp', GCAL_ENABLED: 'Abilita calendario', NOTION_ENABLED: 'Abilita Notion', NOTION_API_KEY: 'Token connessione Notion', NOTION_AGENT_ID: 'ID Custom Agent Notion', GCAL_CALENDAR_ID: 'ID del calendario condiviso', GCAL_EVENT_DURATION: 'Durata appuntamenti (minuti)', GCAL_SERVICE_ACCOUNT_JSON: 'Nuova chiave service account (JSON)', TIMEZONE: 'Fuso orario', LANGUAGE: 'Lingua', DIGEST_TIME: 'Orario riepilogo', NOTIFY_PRIORITY_THRESHOLD: 'Priorità minima notifiche', POLL_INTERVAL: 'Intervallo controllo (secondi)',
  AI_BUDGET_DAILY_CALLS: 'Tentativi AI al giorno', AI_BUDGET_DAILY_TOKENS: 'Token AI al giorno', AI_BUDGET_MAX_INFLIGHT: 'Richieste AI simultanee', AI_BUDGET_MAX_OUTPUT_TOKENS: 'Massimo token di output per tentativo', AI_BUDGET_DAILY_COST: 'Tetto di spesa giornaliero',
  OPENROUTER_AUDIO_MODEL: 'Modello per l’audio', TELEGRAM_USER_DIALOG_LIMIT: 'Conversazioni lette per ciclo', TELEGRAM_USER_MESSAGE_LIMIT: 'Messaggi letti per conversazione', TELEGRAM_USER_RECOVERY_INTERVAL: 'Intervallo di recupero (secondi)', TELEGRAM_EXCLUDED_CHATS: 'Chat Telegram escluse (ID)', WHATSAPP_EXCLUDED_CHATS: 'Chat WhatsApp escluse (ID)',
  SECRETARY_MODE: 'Modalità del segretario', EFFECT_AUDIT_RETENTION_DAYS: 'Conservazione audit degli effetti (giorni)', DASHBOARD_BIND: 'Indirizzo di ascolto', DASHBOARD_COOKIE_SECURE: 'Cookie solo HTTPS',
  DASHBOARD_CREDENTIAL_FILE: 'File credenziali dashboard', DASHBOARD_HOSTNAME: 'Nome host', DASHBOARD_PORT: 'Porta', DATA_DIR: 'Cartella dati', GCAL_SERVICE_ACCOUNT_FILE: 'File service account', INVOICE_DIR: 'Cartella fatture',
  P_SECRETARY_CONFIG_FILE: 'File di configurazione', TELEGRAM_USER_SESSION: 'Sessione Telegram personale', WA_CHROMIUM_PATH: 'Percorso di Chromium'};
const suffixNames = {NAME: 'Nome', PROVIDER: 'Provider', ADDRESS: 'Indirizzo email', PASSWORD: 'Password per app', AUTH: 'Metodo di accesso', CLIENT_ID: 'ID applicazione OAuth', IMAP_HOST: 'Server posta in arrivo', IMAP_PORT: 'Porta IMAP', SMTP_HOST: 'Server posta in uscita', SMTP_PORT: 'Porta SMTP', VAT: 'Partita IVA', DESC: 'Descrizione'};
const fieldLabel = key => fieldNames[key] || suffixNames[key.replace(/^(EMAIL\d+|INVOICE_ENTITY\d+)_/, '')] || key.toLowerCase().replaceAll('_', ' ');
let configRevision = '';
let selectedGroup = '';
const configSections = new Map();
function terminalCommand(title, command) {
  const pre = h('pre', {}, h('code', {text: command}));
  const status = h('small', {role: 'status'});
  const copy = h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'Copia comando'});
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
  return h('div', {class: 'terminal'}, h('h4', {text: title}), pre, h('div', {class: 'actions'}, copy, status));
}
function setupTools(group) {
  const body = h('div', {}, h('p', {class: 'muted', text: 'Apri il terminale del server nella cartella che contiene p-secretary. Questi comandi non contengono credenziali e non vengono eseguiti dal browser.'}),
    terminalCommand('Riconfigura o collega un servizio', 'sh ./p-secretary configure'),
    h('p', {class: 'muted', text: 'Conferma la pausa dell’app e scegli la sezione desiderata. Per Telegram e WhatsApp scegli i setup opzionali; per Microsoft scegli la sezione email.'}),
    terminalCommand('Applica la configurazione e avvia l’app', 'sh ./p-secretary up'),
    terminalCommand('Controlla lo stato dei container', 'sh ./p-secretary status'));
  body.hidden = true;
  const toggle = h('button', {type: 'button', class: 'tools-toggle', 'aria-expanded': 'false'}, icon('chevron'), 'Operazioni da terminale · Docker');
  toggle.onclick = () => {
    body.hidden = !body.hidden;
    toggle.setAttribute('aria-expanded', String(!body.hidden));
  };
  const tools = h('div', {class: 'tools'}, toggle, body);
  if (group === 'Telegram e WhatsApp') {
    tools.prepend(h('div', {class: 'guide'},
      h('h4', {text: 'Telegram: cosa vuoi fare?'}),
      h('p', {text: 'Rinnovare il token dello stesso bot: inserisci sotto il nuovo token ottenuto da BotFather. Il proprietario resta invariato. La verifica usa solo getMe, senza messaggi.'}),
      h('h4', {text: 'Collegare un nuovo bot o cambiare proprietario'}),
      h('p', {text: 'È necessaria una nuova autorizzazione dal terminale del deployment: esegui il launcher “configure” e scegli i setup opzionali. Il web non può verificare la proprietà di un bot diverso.'}),
      h('h4', {text: 'Leggere il tuo account personale'}),
      h('p', {text: 'È un collegamento distinto dal bot: login e codice di conferma si fanno nel terminale (setup opzionali). Non inserire qui OTP o password Telegram.'}),
      h('p', {text: 'WhatsApp: autorizza il QR dal terminale del deployment, con i setup opzionali del launcher.'}),
      h('p', {text: 'Esegui sul server che ospita Docker, nella cartella del progetto (non dentro il container). Il launcher può sospendere temporaneamente l’acquisizione; non eseguire direttamente gli script Python.'})));
  }
  return tools;
}
const configInputs = () => [...el('config-fields').querySelectorAll('[data-key]')];
function pendingChanges() {
  return Object.fromEntries(configInputs().filter(i => !i.disabled && i.value !== i.dataset.original && (i.dataset.secret !== 'true' || i.value)).map(i => [i.dataset.key, i.value]));
}
function setFieldValue(input, value) {
  if (input.setValue) input.setValue(value); else input.value = value;
}
function lockField(input, locked) {
  input.disabled = locked;
  if (input.setDisabled) input.setDisabled(locked);
}
function refreshChangeState() {
  const count = Object.keys(pendingChanges()).length;
  configInputs().forEach(input => {
    const holder = input.parentNode || input;
    if (holder.classList) holder.classList.toggle('changed', !input.disabled && input.value !== input.dataset.original && (input.dataset.secret !== 'true' || !!input.value));
  });
  el('config-status').textContent = count ? `${count} modifiche da controllare · non ancora salvate` : '';
}
function editAgain() {el('config-review').hidden = true; el('config-save').hidden = false;}
function filterGroups() {
  const term = el('config-search').value.toLocaleLowerCase('it');
  const buttons = [...el('config-groups').querySelectorAll('button')];
  buttons.forEach(b => {
    b.hidden = !b.textContent.toLocaleLowerCase('it').includes(term) || (b.dataset.empty === 'true' && !el('config-show-empty').checked && !term);
  });
  const current = buttons.find(b => b.dataset.group === selectedGroup);
  if (!current || current.hidden) {
    const first = buttons.find(b => !b.hidden);
    if (first) selectGroup(first.dataset.group);
  }
}
function selectGroup(group) {
  selectedGroup = group;
  configSections.forEach((section, name) => {section.hidden = name !== group;});
  el('config-groups').querySelectorAll('button').forEach(b => b.setAttribute('aria-expanded', String(b.dataset.group === group)));
}
el('config-search').oninput = filterGroups;
el('config-show-empty').onchange = filterGroups;
el('config-back').onclick = editAgain;
el('config-cancel').onclick = () => {
  configInputs().forEach(i => setFieldValue(i, i.dataset.original));
  editAgain(); refreshChangeState();
  el('config-status').textContent = 'Modifiche annullate. Credenziali salvate invariate.';
};
el('config-load').onclick = async () => {
  if (Object.keys(pendingChanges()).length && !(await confirmDialog({title: 'Scartare le modifiche?', message: 'Ci sono modifiche non salvate: ricaricando andranno perse.', ok: 'Scarta e ricarica', danger: true}))) return;
  await loadConfiguration();
};
function buildControl(field, id) {
  let input;
  if (field.type === 'select') {
    const current = field.value || '';
    const options = (field.options || []).map(value => [value, value === 'true' ? 'Sì' : value === 'false' ? 'No' : value]);
    const emptyAllowed = /^EMAIL\d+_AUTH$/.test(field.key);
    // Preserve legacy values on load: never silently rewrite an unknown value.
    if (current && !(field.options || []).includes(current)) options.push([current, `${current} · valore esistente non valido`]);
    if (options.length > 0 && options.length <= 3) {
      input = h('div', {class: 'seg', role: 'radiogroup'});
      // Valore non impostato: mostrato come "Predefinito" (scegliibile solo dove il valore vuoto e` ammesso).
      const all = emptyAllowed || current === '' ? [['', 'Predefinito', !emptyAllowed], ...options] : options;
      segmented(input, all, current, () => {editAgain(); refreshChangeState();});
      input.setValue = value => setSegmented(input, value);
      return input;
    }
    input = h('select', {});
    const empty = h('option', {value: '', text: 'Non impostato · default applicazione/preset'});
    empty.disabled = !emptyAllowed;
    input.append(empty);
    options.forEach(([value, label]) => input.append(h('option', {value, text: label})));
    return input;
  }
  input = h(field.type === 'textarea' ? 'textarea' : 'input', {});
  if (input.tagName === 'INPUT') input.type = field.secret ? 'password' : (field.type || 'text');
  ['min', 'max', 'step'].forEach(name => {if (field[name] !== undefined) input.setAttribute(name, field[name]);});
  input.placeholder = field.secret ? 'Nuovo valore (vuoto = invariato)' : 'Default applicazione';
  return input;
}
function buildField(field) {
  const id = 'setting-' + field.key;
  const input = buildControl(field, id);
  input.id = id;
  input.dataset.key = field.key; input.dataset.original = field.secret ? '' : (field.value || '');
  input.dataset.secret = String(field.secret);
  if (!input.setValue) {
    input.autocomplete = 'off'; input.spellcheck = false;
    input.value = field.secret ? '' : (field.value || '');
    input.oninput = () => {editAgain(); refreshChangeState();};
    input.onchange = input.oninput;
  }
  input.disabled = !!field.readonly;
  if (input.setDisabled) input.setDisabled(!!field.readonly);
  const label = h('label', {class: 'field-label', htmlFor: id, id: id + '-label', text: fieldLabel(field.key)});
  if (input.setValue) input.setAttribute('aria-labelledby', label.id);
  const wrap = h('div', {class: `field${field.type === 'textarea' ? ' wide' : ''}`}, label, input);
  if (field.description) {
    const help = h('small', {class: 'help', id: id + '-help', text: field.description});
    input.setAttribute('aria-describedby', help.id); wrap.append(help);
  }
  if (field.secret) wrap.append(h('small', {class: `secret-state${field.configured ? ' saved' : ''}`, text: field.configured ? '● Credenziale salvata · lascia vuoto per mantenerla' : '○ Nessuna credenziale salvata'}));
  if (field.readonly) wrap.append(h('small', {class: 'help', text: field.readonly}));
  return wrap;
}
async function loadConfiguration() {
  const generation = sessionGeneration;
  el('config-load').disabled = true;
  try {
    const result = await api('/api/configuration');
    if (generation !== sessionGeneration) return;
    state.configLoaded = true;
    configRevision = result.revision;
    el('config-info').textContent = result.apply;
    el('config-fields').replaceChildren(); configSections.clear();
    el('config-groups').replaceChildren(); el('config-form').hidden = false; editAgain();
    const holders = new Map();
    result.fields.forEach(field => {
      if (!holders.has(field.group)) {
        const fields = result.fields.filter(f => f.group === field.group);
        const configured = fields.some(f => f.configured);
        const identity = fields.find(f => /^EMAIL\d+_ADDRESS$|^INVOICE_ENTITY\d+_NAME$/.test(f.key));
        const displayName = identity && identity.value ? `${field.group} · ${identity.value}` : field.group;
        const box = h('div', {class: 'fields'});
        const section = h('section', {class: 'settings-detail', hidden: true}, h('h3', {text: displayName}),
          h('p', {class: 'lead', text: 'Le credenziali salvate non vengono mai mostrate: compila solo i campi da cambiare.'}), box, setupTools(field.group));
        const button = h('button', {type: 'button', class: 'group-item', dataset: {group: field.group, empty: String(/^(Email |Entità fatture )/.test(field.group) && !configured)}, 'aria-expanded': 'false'},
          h('span', {class: `dot dot-${configured ? 'ok' : ''}`}),
          h('span', {}, h('strong', {text: displayName}), h('small', {text: configured ? 'Impostazioni presenti' : 'Da configurare'})));
        button.onclick = () => {selectGroup(field.group); section.scrollIntoView({behavior: 'smooth', block: 'start'});};
        el('config-groups').append(button);
        configSections.set(field.group, section); holders.set(field.group, box); el('config-fields').append(section);
      }
      holders.get(field.group).append(buildField(field));
    });
    if (!configSections.has(selectedGroup)) selectedGroup = '';
    filterGroups(); refreshChangeState();
    el('config-load').textContent = 'Ricarica impostazioni';
    el('config-save').hidden = false;
    el('config-status').textContent = 'Modificare solo i campi necessari. Le verifiche di rete sono senza invii o pairing; OAuth e sessioni richiedono il terminale.';
  } catch (e) {
    if (generation === sessionGeneration) el('config-status').textContent = e.message;
  } finally {el('config-load').disabled = false;}
}
// Deep link: apre Impostazioni sul gruppo e sul campo che controlla una misura di budget.
async function openSettingsGroup(group, key) {
  showPage('integration-settings');
  const find = () => [...el('config-groups').querySelectorAll('button')].find(b => b.textContent.startsWith(group));
  if (el('config-form').hidden || !find()) await loadConfiguration();
  const button = find();
  if (button) {button.hidden = false; selectGroup(button.dataset.group);}
  const input = el('setting-' + key);
  if (input) {input.focus(); input.scrollIntoView({behavior: 'smooth', block: 'center'});}
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
  el('config-review').scrollIntoView({behavior: 'smooth', block: 'center'});
};
el('config-confirm').onclick = async () => {
  const changes = pendingChanges();
  const inputs = configInputs();
  if (!Object.keys(changes).length) {el('config-status').textContent = 'Nessuna modifica.'; return;}
  el('config-confirm').disabled = true; el('config-back').disabled = true; el('config-cancel').disabled = true;
  inputs.forEach(i => {i.dataset.locked = String(i.disabled); lockField(i, true);});
  el('config-save').disabled = true; el('config-load').disabled = true;
  el('config-status').textContent = 'Verifica in corso… nessuna modifica live.';
  try {
    const result = await api('/api/configuration', {revision: configRevision, changes});
    // A successful revision is deliberately invalidated: reload before another edit.
    el('config-fields').replaceChildren(); el('config-save').hidden = true; configRevision = '';
    el('config-form').hidden = true; el('config-groups').replaceChildren(); state.configLoaded = false;
    el('config-status').textContent = result.restart_scheduled
      ? 'Configurazione salvata. Riavvio automatico del container in corso: la dashboard sarà temporaneamente non disponibile. Al ritorno, accedi nuovamente. Nessuna migrazione DB eseguita.'
      : 'Configurazione salvata. Runtime non Docker: riavvio manuale necessario. Nessuna migrazione DB eseguita.';
  } catch (e) {
    el('config-status').textContent = `Non salvato. ${e.message} Le credenziali precedenti sono invariate. Reinserisci le nuove credenziali per riprovare.`; editAgain();
  } finally {
    inputs.filter(input => input.dataset.secret === 'true').forEach(input => {input.value = '';});
    Object.keys(changes).forEach(key => {delete changes[key];});
    el('config-save').disabled = false; el('config-load').disabled = false;
    inputs.forEach(i => lockField(i, i.dataset.locked === 'true'));
    el('config-confirm').disabled = false; el('config-back').disabled = false; el('config-cancel').disabled = false;
  }
};

// -------------------------------------------------------------------- file
let filePath = '';
segmented(el('file-root'), [['data', 'Dati'], ['invoices', 'Fatture']], 'data', () => {filePath = ''; browseFiles();});
const fileQuery = path => new URLSearchParams({root: el('file-root').value, path}).toString();
function renderCrumbs() {
  const parts = filePath ? filePath.split('/') : [];
  const rootLabel = el('file-root').value === 'invoices' ? 'Fatture' : 'Dati';
  const crumbs = [h('button', {type: 'button', class: 'crumb', text: rootLabel, 'aria-current': parts.length ? false : 'page',
    onclick: () => {filePath = ''; browseFiles();}})];
  parts.forEach((part, index) => {
    crumbs.push(h('span', {text: '/'}));
    crumbs.push(h('button', {type: 'button', class: 'crumb', text: part, 'aria-current': index === parts.length - 1 ? 'page' : false,
      onclick: () => {filePath = parts.slice(0, index + 1).join('/'); browseFiles();}}));
  });
  el('file-path').replaceChildren(...crumbs);
}
async function deleteFile(entry, path) {
  if (!(await confirmDialog({title: 'Eliminare il file?', message: `“${entry.name}” verrà eliminato definitivamente.`, ok: 'Elimina', danger: true}))) return;
  try {
    const response = await fetch(`/api/files?${fileQuery(path)}`, {method: 'DELETE', headers: {'X-CSRF-Token': csrf}});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Eliminazione fallita');
    toast('File eliminato.'); await browseFiles();
  } catch (e) {el('file-status').textContent = e.message; toast(e.message, 'bad');}
}
function renderFiles() {
  const term = el('file-search').value.toLocaleLowerCase('it');
  const entries = state.files.filter(entry => entry.name.toLocaleLowerCase('it').includes(term))
    .sort((a, b) => Number(b.directory) - Number(a.directory) || a.name.localeCompare(b.name, 'it'));
  const shown = entries.slice(0, state.filesShown);
  if (!entries.length) {el('file-list').replaceChildren(emptyState(term ? 'Nessun file corrisponde al filtro.' : 'Cartella vuota.')); el('file-more').hidden = true; return;}
  const body = h('tbody');
  shown.forEach(entry => {
    const path = filePath ? `${filePath}/${entry.name}` : entry.name;
    const actions = h('td', {class: 'actions-cell'});
    if (entry.directory) {
      actions.append(h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'Apri', onclick: () => {filePath = path; browseFiles();}}));
    } else {
      actions.append(h('a', {class: 'btn btn-ghost btn-sm', href: `/api/files/download?${fileQuery(path)}`, text: 'Scarica'}), ' ',
        h('button', {type: 'button', class: 'btn btn-danger btn-sm', text: 'Elimina', onclick: () => deleteFile(entry, path)}));
    }
    body.append(h('tr', {}, h('td', {}, h('div', {class: `name${entry.directory ? ' folder' : ''}`}, icon(entry.directory ? 'folder' : 'file'), entry.name)),
      h('td', {class: 'num', text: entry.directory ? 'Cartella' : sizeLabel(entry.size)}), actions));
  });
  el('file-list').replaceChildren(h('table', {class: 'tbl'}, h('thead', {}, h('tr', {}, h('th', {text: 'Nome'}), h('th', {class: 'num', text: 'Dimensione'}), h('th'))), body));
  pagedButton(el('file-more'), entries.length, shown.length);
}
async function browseFiles() {
  const generation = sessionGeneration;
  try {
    const result = await api(`/api/files?${fileQuery(filePath)}`);
    if (generation !== sessionGeneration) return;
    state.files = result.entries; state.filesShown = 25;
    el('file-up').disabled = !filePath;
    renderCrumbs(); renderFiles();
    el('file-status').textContent = '';
  } catch (e) {if (generation === sessionGeneration) el('file-status').textContent = e.message;}
}
el('file-search').oninput = () => {state.filesShown = 25; renderFiles();};
el('file-more').onclick = () => {state.filesShown += 15; renderFiles();};
el('file-up').onclick = () => {filePath = filePath.split('/').slice(0, -1).join('/'); browseFiles();};
el('file-refresh').onclick = browseFiles;
el('file-upload').onchange = () => {
  const file = el('file-upload').files && el('file-upload').files[0];
  el('file-picked').textContent = file ? `${file.name} · ${sizeLabel(file.size)}` : 'Scegli file…';
};
el('file-send').onclick = async () => {
  const file = el('file-upload').files[0];
  if (!file) {el('file-status').textContent = 'Scegli prima un file da caricare.'; return;}
  if (file.size > 20 * 1024 * 1024) {el('file-status').textContent = 'File troppo grande'; return;}
  el('file-send').disabled = true;
  try {
    const response = await fetch(`/api/files/upload?${new URLSearchParams({name: file.name})}`, {
      method: 'POST', headers: {'X-CSRF-Token': csrf, 'Content-Type': 'application/octet-stream'}, body: file});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Upload fallito');
    setSegmented(el('file-root'), 'data'); filePath = 'uploads'; el('file-upload').value = ''; el('file-picked').textContent = 'Scegli file…';
    await browseFiles();
    el('file-status').textContent = 'File caricato'; toast('File caricato in uploads.');
  } catch (e) {el('file-status').textContent = e.message; toast(e.message, 'bad');}
  finally {el('file-send').disabled = false;}
};
// I grafici si disegnano alla larghezza reale: ridisegno quando il pannello diventa visibile.
bindTabs([['archive-tab-files', 'archive-panel-files'], ['archive-tab-invoices', 'archive-panel-invoices'], ['archive-tab-zips', 'archive-panel-zips']],
  () => {if (state.dashboard) renderArchive(state.dashboard);});
function table(headers, rows) {
  return h('table', {class: 'tbl'}, h('thead', {}, h('tr', {}, headers.map(([text, numeric]) => h('th', {class: numeric ? 'num' : false, text})))), h('tbody', {}, rows));
}
function renderArchive(d) {
  const invoiceActive = d.flows.some(f => ['invoices', 'monthly_zip'].includes(f.id) && f.enabled);
  el('invoice-inactive').hidden = invoiceActive;
  const months = [...new Set(d.invoices.map(r => r[1]))].sort().slice(-12);
  const entities = [...new Set(d.invoices.map(r => r[0]))];
  if (months.length) {
    const series = entities.map((entity, index) => ({key: entity, label: entity, cls: 'c' + ((index % 4) + 1),
      values: months.map(month => (d.invoices.find(r => r[0] === entity && r[1] === month) || [0, 0, 0])[2])}));
    el('invoice-chart').replaceChildren(stackedBars(months.map(month => month + '-01'), series, {width: chartWidth(el('invoice-chart')), height: 170, label: 'Documenti per mese',
      tick: day => monthName(day.slice(0, 7)), describe: day => monthName(day.slice(0, 7))}),
      h('div', {class: 'legend'}, series.map(item => h('span', {class: 'legend-item', dataset: {cls: item.cls}}, h('span', {class: 'swatch'}), item.label))));
  } else el('invoice-chart').replaceChildren();
  el('invoices').replaceChildren(d.invoices.length ? table([['Entità'], ['Mese'], ['Documenti', true]], d.invoices.map(r => h('tr', {}, h('td', {text: r[0]}), h('td', {text: r[1]}), h('td', {class: 'num', text: fmt(r[2])})))) : emptyState('Nessuna fattura archiviata.'));
  el('zips').replaceChildren(d.zips.length ? table([['Entità'], ['Mese'], ['Stato']], d.zips.map(r => h('tr', {}, h('td', {text: r[0]}), h('td', {text: r[1]}), h('td', {}, badge(r[2] ? 'Inviato' : 'Non inviato', r[2] ? 'ok' : 'warn'))))) : emptyState('Nessun ZIP mensile.'));
}

// ----------------------------------------------------------------- consensi
const EFFECTS = [['ai_text', 'Testo', 'Testo verso provider AI'], ['ai_media', 'Audio e immagini', 'Audio e immagini verso provider AI'],
  ['ai_history', 'Storico', 'Storico come contesto AI di sola lettura']];
const POLICY_CHANNELS = {email: 'Email', telegram: 'Telegram (bot e account personale)', whatsapp: 'WhatsApp'};
async function loadPolicies() {
  const generation = sessionGeneration;
  el('policy-refresh').disabled = true;
  try {
    const result = await api('/api/data-policies');
    if (generation !== sessionGeneration) return;
    const grid = [h('div', {class: 'mx-head'}, 'Sorgente'), ...EFFECTS.map(([, title, text]) => h('div', {class: 'mx-head'}, title, h('small', {text})))];
    Object.keys(POLICY_CHANNELS).forEach(channel => {
      grid.push(h('div', {class: 'mx-channel'}, icon(channelIcon(channel)), POLICY_CHANNELS[channel]));
      EFFECTS.forEach(([effect, title, text]) => {
        const policy = result.policies.find(p => p.channel === channel && p.effect === effect);
        if (!policy) {grid.push(h('div', {class: 'mx-cell'}, '—')); return;}
        const status = h('small', {text: policy.allowed ? 'Consentito esplicitamente' : 'Negato'});
        const control = toggleSwitch({checked: policy.allowed, label: `${POLICY_CHANNELS[channel]} · ${text}: ${policy.allowed ? 'consentito' : 'negato'}`,
          onchange: async input => {
            const scope = effect === 'ai_history' ? ' Solo consultazione dello storico, senza riattivare attività o invii. Servono separatamente il consenso testo e, per documenti/media, il consenso media.' : '';
            const ok = await confirmDialog({title: `${policy.allowed ? 'Revocare' : 'Consentire'} questo uso?`,
              message: `${policy.allowed ? 'Revocare' : 'Consentire'} «${text}» per ${POLICY_CHANNELS[channel]}?${channel === 'telegram' ? ' Include bot e account personale.' : ''}${scope}`,
              ok: policy.allowed ? 'Revoca consenso' : 'Consenti questo uso', danger: policy.allowed});
            if (!ok) {input.checked = policy.allowed; return;}
            input.disabled = true;
            try {
              await api('/api/data-policies', {channel, effect, allowed: !policy.allowed, expected_version: policy.version});
              toast(`${policy.allowed ? 'Consenso revocato' : 'Consenso concesso'} · ${POLICY_CHANNELS[channel]}`);
              await loadPolicies();
            } catch (error) {
              input.checked = policy.allowed; input.disabled = false;
              if (generation === sessionGeneration) el('policy-status').textContent = `${error.message}. Ricarica prima di riprovare.`;
            }
          }});
        grid.push(h('div', {class: 'mx-cell', dataset: {allowed: String(policy.allowed), label: title}}, control, status));
      });
    });
    el('data-policies').replaceChildren(...grid);
    el('policy-status').textContent = 'Consensi letti dal DB. Nessuna autorizzazione automatica all’attivazione dei flussi.';
  } catch (error) {
    if (generation === sessionGeneration) {el('policy-status').textContent = error.message; el('data-policies').replaceChildren();}
  } finally {if (generation === sessionGeneration) el('policy-refresh').disabled = false;}
}
el('policy-refresh').onclick = loadPolicies;
segmented(el('media-retry-channel'), [['telegram', 'Telegram bot'], ['telegram (account)', 'Telegram personale'], ['whatsapp', 'WhatsApp']], 'telegram');
el('media-retry').onsubmit = async event => {
  event.preventDefault();
  const generation = sessionGeneration;
  const id = Number(el('media-retry-id').value); const channel = el('media-retry-channel').value;
  if (!Number.isSafeInteger(id) || id <= 0) {el('media-retry-status').textContent = 'Indica l’ID numerico del messaggio.'; return;}
  if (!(await confirmDialog({title: 'Riprovare i media?', message: `Riprovare i media bloccati per consenso del messaggio #${id} (${channel})? Può inviare audio o immagini al provider AI. Lo storico e i tentativi già registrati restano esclusi.`, ok: 'Riprova'}))) return;
  const button = event.target.querySelector('button'); button.disabled = true;
  try {
    const result = await api('/api/media/retry-blocked', {message_id: id, expected_channel: channel});
    if (generation !== sessionGeneration) return;
    el('media-retry-status').textContent = result.queued ? `${result.queued} media accodati.` : 'Nessun media recuperabile: già accodato, non bloccato per consenso o limite raggiunto.';
  } catch (error) {if (generation === sessionGeneration) el('media-retry-status').textContent = `${error.message}. Verifica entrambi i consensi AI.`;}
  finally {button.disabled = false;}
};

// ------------------------------------------------------------------- budget
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
  const detail = `${fmt(used)} / ${fmt(limit)} · ${percent}%`;
  const head = h('div', {class: 'meter-head'}, h('span', {text: label}), h('small', {text: detail}));
  if (level === 'full') head.append(h('span', {class: 'flag', text: 'SATURATO'}));
  if (target) head.append(h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'Modifica questo limite', onclick: () => openSettingsGroup(target.group, target.key)}));
  const fill = h('div', {class: `meter-fill lvl-${level}`, role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(limit), 'aria-valuenow': String(used),
    'aria-label': `${label}: ${detail} · livello ${BUDGET_LEVEL_NAMES[level]}`});
  fill.style.width = `${Number.isFinite(ratio) ? Math.min(100, Math.max(0, ratio * 100)) : 100}%`;
  const meter = h('div', {class: 'meter'}, head, h('div', {class: 'meter-track'}, fill));
  parent.append(meter);
  return meter;
}
function budgetRing(label, used, limit, target, caption) {
  const ratio = limit > 0 ? used / limit : Infinity;
  const level = budgetLevel(ratio);
  const percent = Number.isFinite(ratio) ? `${Math.round(ratio * 100)}%` : '∞';
  const body = h('div', {}, h('h3', {text: label}), h('strong', {class: 'big', text: `${fmt(used)} / ${fmt(limit)}`}));
  if (caption) body.append(h('small', {class: 'muted', text: caption}));
  if (level === 'full') body.append(' ', h('span', {class: 'flag', text: 'SATURATO'}));
  if (target) body.append(h('div', {}, h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'Modifica questo limite', onclick: () => openSettingsGroup(target.group, target.key)})));
  return h('div', {class: 'ring-card', dataset: {level}}, ring(ratio, level, {text: percent, caption: `${label}: ${fmt(used)} su ${fmt(limit)} · livello ${BUDGET_LEVEL_NAMES[level]}`}), body);
}
const AI_LIMITS_GROUP = 'AI · limiti dei Worker (chiamate, token, concorrenza)';
const AI_COST_GROUP = 'AI · tetto di spesa in denaro (opzionale)';
function renderBudget() {
  const result = state.budget;
  if (!result) return;
  const usage = result.usage; const limits = result.limits;
  const box = el('ai-budget'); box.replaceChildren();
  const rings = h('div', {class: 'rings'}, budgetRing('Tentativi HTTP oggi (UTC)', usage.calls, limits.DAILY_CALLS, {group: AI_LIMITS_GROUP, key: 'AI_BUDGET_DAILY_CALLS'}),
    budgetRing('Token addebitati conservativamente', usage.charged_tokens, limits.DAILY_TOKENS, {group: AI_LIMITS_GROUP, key: 'AI_BUDGET_DAILY_TOKENS'}),
    budgetRing('Slot prenotati / massimo simultanei', usage.inflight, limits.MAX_INFLIGHT, {group: AI_LIMITS_GROUP, key: 'AI_BUDGET_MAX_INFLIGHT'}));
  if (result.money_cap_enforced && Number(result.daily_cost) > 0) {
    rings.append(budgetRing('Costo comunicato / tetto di spesa', Number(usage.reported_cost || 0), Number(result.daily_cost), {group: AI_COST_GROUP, key: 'AI_BUDGET_DAILY_COST'}));
  }
  box.append(rings);
  const scopes = Object.keys(result.scope_caps || {});
  if (scopes.length) {
    const stack = h('div', {class: 'stack'});
    scopes.forEach(scope => budgetMeter(stack, `Budget ${BUDGET_SCOPE_NAMES[scope] || scope}`, (result.scope_usage || {})[scope] || 0, result.scope_caps[scope]));
    box.append(h('h3', {text: 'Tetti per ambito'}), stack);
  }
  // Due metriche distinte, mai un rapporto fuorviante: la prenotazione è una stima per eccesso.
  const kv = h('div', {class: 'kv'},
    h('div', {}, h('span', {text: 'Token prenotati in anticipo (stima per eccesso)'}), h('b', {text: fmt(usage.reserved_tokens)})),
    h('div', {}, h('span', {text: 'Token comunicati dal provider (consumo reale)'}), h('b', {text: usage.reported_tokens == null ? 'non comunicati' : fmt(usage.reported_tokens)})),
    h('div', {}, h('span', {text: 'Massimo output per tentativo'}), h('b', {text: fmt(limits.MAX_OUTPUT_TOKENS)})));
  if (!result.money_cap_enforced) kv.append(h('div', {}, h('span', {text: 'Costo comunicato (non tetto di spesa)'}), h('b', {text: `${usage.reported_cost == null ? 'non comunicato' : Number(usage.reported_cost).toLocaleString('it', {maximumFractionDigits: 4})} · ${usage.cost_unreported_calls} tentativi con costo non comunicato`})));
  box.append(h('h3', {text: 'Dettagli'}), kv);
  // Slot bloccati da worker morti: azione di riparazione immediata.
  const orphaned = result.orphaned || [];
  if (orphaned.length) {
    const panel = h('div', {class: 'repair'}, h('strong', {text: `⚠ ${orphaned.length} slot bloccati da worker morti (job non più in esecuzione)`}));
    const free = h('button', {type: 'button', class: 'btn btn-danger btn-sm', text: 'Libera slot'});
    free.onclick = async () => {
      free.disabled = true;
      try {const r = await api('/api/budget/reconcile', {}); toast(`Slot liberati: ${r.freed}`); await loadBudget();}
      catch (error) {free.disabled = false; toast(error.message, 'bad');}
    };
    const requeue = h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'Riaccoda posta fallita'});
    requeue.onclick = async () => {
      requeue.disabled = true;
      try {const r = await api('/api/jobs/requeue', {kind: 'analysis'}); toast(`Messaggi riaccodati: ${r.requeued.length}`);}
      catch (error) {requeue.disabled = false; toast(error.message, 'bad');}
    };
    panel.append(h('div', {class: 'actions'}, free, requeue));
    box.append(panel);
  }
  renderOverviewBudget(); renderBudgetChart(); renderKpis();
}
async function loadBudget() {
  const generation = sessionGeneration;
  state.budgetAt = Date.now();
  try {
    const result = await api('/api/budget');
    if (generation !== sessionGeneration) return;
    state.budget = result; renderBudget();
  } catch (error) {
    if (generation === sessionGeneration) el('ai-budget').textContent = `${error.message}. Contabilità non disponibile: nessun consumo nullo presunto.`;
  }
}
el('budget-settings').onclick = () => openSettingsGroup(AI_LIMITS_GROUP, 'AI_BUDGET_MAX_INFLIGHT');
el('overview-budget-open').onclick = () => showPage('budget');
function renderOverviewBudget() {
  const result = state.budget; const box = el('overview-budget');
  if (!result) {skeleton(box); return;}
  box.replaceChildren();
  budgetMeter(box, 'Tentativi HTTP', result.usage.calls, result.limits.DAILY_CALLS);
  budgetMeter(box, 'Token addebitati', result.usage.charged_tokens, result.limits.DAILY_TOKENS);
  budgetMeter(box, 'Slot simultanei', result.usage.inflight, result.limits.MAX_INFLIGHT);
}
const METRICS = {tokens: ['Token', 'token addebitati'], calls: ['Chiamate', 'tentativi HTTP'], cost: ['Costo', 'costo comunicato']};
function renderBudgetChart() {
  const trends = state.trends; const box = el('budget-chart');
  if (!trends) {skeleton(box); return;}
  const metrics = Object.keys(METRICS).filter(key => key !== 'cost' || trends.ai.cost.some(value => value > 0));
  if (!metrics.includes(state.metric)) state.metric = 'tokens';
  segmented(el('budget-metric'), metrics.map(key => [key, METRICS[key][0]]), state.metric, value => {state.metric = value; renderBudgetChart();});
  const values = trends.ai[state.metric === 'tokens' ? 'tokens' : state.metric];
  const readout = index => {
    const at = index < 0 ? values.length - 1 : index;
    const day = new Date(`${trends.days[at]}T12:00:00`).toLocaleDateString('it', {weekday: 'long', day: 'numeric', month: 'long'});
    el('budget-readout').textContent = `${index < 0 ? 'Oggi' : day} · ${METRICS[state.metric][1]}: ${state.metric === 'cost' ? values[at].toLocaleString('it', {maximumFractionDigits: 4}) : fmt(values[at])} (giorno UTC)`;
  };
  box.replaceChildren(stackedBars(trends.days, [{key: state.metric, label: METRICS[state.metric][1], values, cls: 'c2'}],
    {width: chartWidth(box), height: 230, label: `Consumo AI per giorno: ${METRICS[state.metric][1]}`, onhover: readout, format: state.metric === 'cost' ? value => value.toLocaleString('it', {maximumFractionDigits: 2}) : compactNumber}));
  box.setAttribute('aria-busy', 'false'); readout(-1);
}

// -------------------------------------------------------------------- agente
const RUN_KINDS = {owner: 'Dialogo con te', inbox: 'Posta in arrivo', digest: 'Riepilogo serale'};
const RUN_STATES = {running: 'in corso', done: 'completata', failed: 'fallita'};
const RUN_BADGE = {running: 'info', done: 'ok', failed: 'bad'};
const DRAFT_STATES = {proposed: 'In attesa di tua decisione', approved: 'Approvata · invio in coda', rejected: 'Scartata',
  sent: 'Inviata · consegna non verificata', failed: 'Fallita', uncertain: 'Esito incerto · nessun reinvio automatico'};
const DRAFT_BADGE = {proposed: 'warn', approved: 'info', rejected: '', sent: 'ok', failed: 'bad', uncertain: 'high'};
function closeRun() {el('agent-detail').hidden = true;}
el('agent-detail-close').onclick = closeRun;
window.addEventListener('keydown', event => {if (event.key === 'Escape' && !el('agent-detail').hidden && el('dialog').hidden) closeRun();});
async function openRun(run) {
  const generation = sessionGeneration;
  try {
    const result = await api(`/api/agent/runs/${run.id}`);
    if (generation !== sessionGeneration) return;
    el('agent-calls').replaceChildren(...result.calls.map(call => {
      const detail = h('pre', {text: `${call.args || ''}\n→ ${call.summary || ''}`, hidden: true});
      const head = h('button', {type: 'button', class: 'call-head', 'aria-expanded': 'false'},
        h('span', {class: 'seq', text: String(call.seq)}), h('b', {text: call.tool}), badge(call.ok ? 'ok' : 'errore', call.ok ? 'ok' : 'bad'),
        h('small', {text: `${call.duration_ms ?? '?'} ms`}));
      head.onclick = () => {detail.hidden = !detail.hidden; head.setAttribute('aria-expanded', String(!detail.hidden));};
      return h('div', {class: 'call'}, head, detail);
    }));
    if (!result.calls.length) el('agent-calls').append(emptyState('Nessuno strumento usato in questa esecuzione.'));
    el('agent-detail-title').textContent = `Esecuzione #${run.id} · ${RUN_KINDS[run.kind] || run.kind}`;
    el('agent-detail').hidden = false;
    el('agent-detail-close').focus();
  } catch (error) {if (generation === sessionGeneration) el('agent-status').textContent = error.message;}
}
function renderRuns() {
  const runs = state.agent ? state.agent.runs : [];
  const filters = [['all', 'Tutte', runs.length], ['owner', 'Dialogo', runs.filter(r => r.kind === 'owner').length], ['inbox', 'Posta', runs.filter(r => r.kind === 'inbox').length],
    ['digest', 'Riepilogo', runs.filter(r => r.kind === 'digest').length], ['failed', 'Fallite', runs.filter(r => r.state === 'failed').length]];
  el('agent-run-filters').replaceChildren(...filters.map(([key, label, count]) => h('button', {type: 'button', class: 'chip', text: `${label} · ${count}`,
    'aria-pressed': String(state.runFilter === key), onclick: () => {state.runFilter = key; state.runsShown = 10; renderRuns();}})));
  const matching = runs.filter(r => state.runFilter === 'all' || r.kind === state.runFilter || (state.runFilter === 'failed' && r.state === 'failed'));
  const shown = matching.slice(0, state.runsShown);
  if (!matching.length) {el('agent-runs').replaceChildren(emptyState('Nessuna esecuzione registrata.')); el('agent-runs-more').hidden = true; return;}
  const rows = shown.map(run => {
    const open = h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'Strumenti usati'});
    open.onclick = () => openRun(run);
    return h('tr', {}, h('td', {text: `#${run.id}`}),
      h('td', {}, RUN_KINDS[run.kind] || run.kind, run.error ? h('span', {class: 'run-error', text: run.error}) : false),
      h('td', {}, badge(RUN_STATES[run.state] || run.state, RUN_BADGE[run.state])),
      h('td', {class: 'num', text: String(run.tool_calls)}), h('td', {title: localDate(run.started_at), text: ago(run.started_at)}),
      h('td', {class: 'actions-cell'}, open));
  });
  el('agent-runs').replaceChildren(table([['Esecuzione'], ['Tipo'], ['Stato'], ['Strumenti', true], ['Avvio'], ['']], rows));
  pagedButton(el('agent-runs-more'), matching.length, shown.length);
}
el('agent-runs-more').onclick = () => {state.runsShown += 15; renderRuns();};
bindTabs([['agent-tab-runs', 'agent-panel-runs'], ['agent-tab-drafts', 'agent-panel-drafts']]);
function renderRunsChart() {
  const trends = state.trends; const box = el('agent-runs-chart');
  if (!trends) {skeleton(box); return;}
  box.replaceChildren(stackedBars(trends.days, [{key: 'done', label: 'completate', values: trends.runs.done, cls: 'c-ok'},
    {key: 'failed', label: 'fallite', values: trends.runs.failed, cls: 'c-bad'}, {key: 'running', label: 'in corso', values: trends.runs.running, cls: 'c-info'}],
    {width: chartWidth(box), height: 150, label: 'Esecuzioni dell’agente per giorno'}));
}
async function loadAgent() {
  const generation = sessionGeneration;
  el('agent-refresh').disabled = true;
  try {
    const result = await api('/api/agent');
    if (generation !== sessionGeneration) return;
    state.agent = result;
    el('agent-emails').replaceChildren();
    renderRuns();
    result.emails.forEach(email => {
      const card = h('article', {class: 'draft'}, h('div', {class: 'row'}, h('h4', {text: `Bozza #${email.id}`}), badge(DRAFT_STATES[email.state] || email.state, DRAFT_BADGE[email.state])),
        h('p', {text: `Da ${email.account} a ${email.recipient}`}), h('p', {text: `Oggetto: ${email.subject}`}), h('pre', {text: email.body}));
      if (email.state === 'proposed') card.append(h('small', {class: 'muted', text: `Per decidere rispondi al bot: /invia ${email.id} oppure /scarta ${email.id}.`}));
      el('agent-emails').append(card);
    });
    if (!result.emails.length) el('agent-emails').append(emptyState('Nessuna bozza di email.'));
    el('agent-draft-count').textContent = result.emails.length ? String(result.emails.length) : '';
    el('agent-model').textContent = `${result.model}`;
    el('agent-role').textContent = result.readonly_role ? 'attivo (ruolo di sola lettura del database)' : 'NON attivo: le query libere usano solo la guardia SQL (vedi deploy/sql/agent_ro.sql)';
    el('agent-role').dataset.state = result.readonly_role ? 'ok' : 'warn';
    el('agent-send').textContent = result.sends_allowed ? 'abilitato (modalità active)' : 'disabilitato (modalità observe): le bozze non vengono inviate';
    el('agent-send').dataset.state = result.sends_allowed ? 'ok' : '';
    el('agent-status').textContent = 'Solo metadati delle esecuzioni (30 più recenti): nessun prompt né risposta del modello.';
    renderOverviewAgent();
  } catch (error) {
    if (generation === sessionGeneration) el('agent-status').textContent = `${error.message}. Vista dell’agente non disponibile.`;
  } finally {if (generation === sessionGeneration) el('agent-refresh').disabled = false;}
}
el('agent-refresh').onclick = loadAgent;
function renderOverviewAgent() {
  const runs = state.agent ? state.agent.runs : [];
  const box = el('overview-agent');
  if (!runs.length) {box.replaceChildren(h('span', {text: 'Agente: nessuna esecuzione registrata.'})); return;}
  const last = runs[0]; const failed = runs.filter(r => r.state === 'failed').length;
  box.replaceChildren(icon('agent'), ' ', `Ultima esecuzione: ${RUN_KINDS[last.kind] || last.kind} · ${RUN_STATES[last.state] || last.state} · ${ago(last.started_at)}`);
  if (failed) box.append(h('div', {}, badge(`${failed} fallite tra le ultime ${runs.length}`, 'bad')));
}

// ------------------------------------------------------------------ panoramica
async function loadTrends() {
  const generation = sessionGeneration;
  try {
    const result = await api('/api/trends');
    if (generation !== sessionGeneration) return;
    state.trends = result;
    renderMessages(); renderKpis(); renderBudgetChart(); renderRunsChart();
  } catch (error) {
    if (generation !== sessionGeneration) return;
    ['messages-chart', 'budget-chart', 'agent-runs-chart'].forEach(id => el(id).replaceChildren(emptyState(`Grafici non disponibili: ${error.message}`)));
  }
}
const CHANNEL_CLASSES = ['c1', 'c2', 'c3', 'c4'];
function trendSeries(channel) {
  const messages = state.trends.messages[channel];
  return state.direction === 'in' ? messages.in : state.direction === 'out' ? messages.out
    : messages.in.map((value, index) => value + messages.out[index] + messages.other[index]);
}
function renderMessages() {
  const trends = state.trends; const box = el('messages-chart');
  if (!trends) {skeleton(box); return;}
  segmented(el('messages-direction'), [['all', 'Tutti'], ['in', 'In entrata'], ['out', 'In uscita']], state.direction, value => {state.direction = value; renderMessages();});
  const channels = Object.keys(trends.messages);
  if (!channels.length) {box.replaceChildren(emptyState('Nessun messaggio negli ultimi 14 giorni.')); el('messages-legend').replaceChildren(); el('messages-readout').textContent = ''; return;}
  const classOf = channel => CHANNEL_CLASSES[channels.indexOf(channel) % CHANNEL_CLASSES.length];
  const visible = channels.filter(channel => !state.hiddenChannels.has(channel));
  const series = visible.map(channel => ({key: channel, label: channelName(channel), cls: classOf(channel), values: trendSeries(channel)}));
  const readout = index => {
    const at = index < 0 ? trends.days.length - 1 : index;
    const parts = series.map(item => `${item.label} ${item.values[at]}`);
    const total = series.reduce((sum, item) => sum + item.values[at], 0);
    const day = index < 0 ? 'Oggi' : new Date(`${trends.days[at]}T12:00:00`).toLocaleDateString('it', {weekday: 'long', day: 'numeric', month: 'long'});
    el('messages-readout').textContent = `${day} · ${parts.join(' · ') || 'nessun canale selezionato'} · totale ${total}`;
  };
  box.replaceChildren(stackedBars(trends.days, series, {width: chartWidth(box), height: 230, label: 'Messaggi per giorno e canale', onhover: readout}));
  box.setAttribute('aria-busy', 'false');
  el('messages-legend').replaceChildren(...channels.map(channel => {
    const total = trendSeries(channel).reduce((sum, value) => sum + value, 0);
    const item = h('button', {type: 'button', class: 'legend-item', dataset: {cls: classOf(channel)}, 'aria-pressed': String(!state.hiddenChannels.has(channel))},
      h('span', {class: 'swatch'}), `${channelName(channel)} · ${fmt(total)}`);
    item.onclick = () => {
      if (state.hiddenChannels.has(channel)) state.hiddenChannels.delete(channel); else state.hiddenChannels.add(channel);
      renderMessages();
    };
    return item;
  }));
  readout(-1);
}
function dayShift(day, offset) {
  const date = new Date(`${day}T12:00:00`); date.setDate(date.getDate() + offset);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}
function kpiTile({iconName, label, value, unit, delta, deltaKind, spark, level}) {
  const tile = h('div', {class: 'kpi', dataset: {level: level || ''}},
    h('div', {class: 'kpi-top'}, h('span', {class: 'kpi-icon'}, icon(iconName)), label),
    h('div', {class: 'kpi-value'}, value, unit ? h('small', {text: unit}) : false));
  if (delta) tile.append(h('div', {class: `kpi-delta ${deltaKind || ''}`, text: delta}));
  if (spark) tile.append(sparkline(spark, {label: `Andamento di ${label} negli ultimi 14 giorni`}));
  return tile;
}
function renderKpis() {
  const d = state.dashboard;
  if (!d) {el('kpis').replaceChildren(...[1, 2, 3, 4, 5, 6].map(() => h('div', {class: 'skeleton', style: false}))); return;}
  const yesterday = dayShift(d.today, -1);
  const sum = (day, direction) => d.counts.filter(r => String(r[0]).slice(0, 10) === day && r[2] === direction).reduce((s, r) => s + r[3], 0);
  const trendTotals = direction => (state.trends ? state.trends.days.map((_, index) => Object.values(state.trends.messages).reduce((s, c) => s + c[direction][index], 0)) : null);
  const compare = (now, before, goodUp) => {
    const diff = now - before;
    if (!diff) return ['come ieri', ''];
    return [`${diff > 0 ? '+' : '−'}${Math.abs(diff)} rispetto a ieri`, (diff > 0) === goodUp ? 'up' : 'down'];
  };
  const [inDelta, inKind] = compare(sum(d.today, 'in'), sum(yesterday, 'in'), true);
  const [outDelta, outKind] = compare(sum(d.today, 'out'), sum(yesterday, 'out'), true);
  const openTasks = (d.tasks || []).filter(row => row[0] === 'open').reduce((s, row) => s + row[1], 0);
  const todo = d.counts.reduce((s, r) => s + r[5], 0);
  const waiting = (d.calendar_pending || []).length;
  const tiles = [
    kpiTile({iconName: 'inbound', label: 'In entrata oggi', value: fmt(sum(d.today, 'in')), delta: inDelta, deltaKind: inKind, spark: trendTotals('in')}),
    kpiTile({iconName: 'outbound', label: 'In uscita oggi', value: fmt(sum(d.today, 'out')), delta: outDelta, deltaKind: outKind, spark: trendTotals('out')}),
    kpiTile({iconName: 'alert', label: 'Da gestire · ieri/oggi', value: fmt(todo), delta: todo ? 'messaggi con azione richiesta' : 'nulla in sospeso', deltaKind: todo ? 'down' : 'up'}),
    kpiTile({iconName: 'task', label: 'Attività aperte', value: fmt(openTasks), delta: 'create dall’agente o da te'}),
    kpiTile({iconName: 'calendar', label: 'Appuntamenti aperti', value: fmt(d.appointments), delta: waiting ? `${waiting} in attesa del calendario` : 'sincronizzati', deltaKind: waiting ? 'down' : ''})];
  if (state.budget) {
    const used = state.budget.usage.charged_tokens; const limit = state.budget.limits.DAILY_TOKENS;
    const ratio = limit > 0 ? used / limit : Infinity;
    tiles.push(kpiTile({iconName: 'bolt', label: 'Token AI oggi', value: `${Number.isFinite(ratio) ? Math.round(ratio * 100) : '∞'}`, unit: '%',
      delta: `${compactNumber(used)} su ${compactNumber(limit)}`, deltaKind: ratio >= 1 ? 'bad' : ratio >= 0.8 ? 'down' : '', spark: state.trends ? state.trends.ai.tokens : null, level: budgetLevel(ratio)}));
  }
  el('kpis').replaceChildren(...tiles);
}
// Stato osservato di un flusso → [etichetta, livello]. Livelli: ok, info, warn, bad, '' (neutro).
const FLOW_STATUS = {running: ['in esecuzione', 'ok'], idle: ['attivo, in attesa', 'ok'], ready: ['pronto', 'ok'], connected: ['collegato', 'ok'],
  connecting: ['connessione in corso', 'info'], paused: ['in pausa', ''], not_observed: ['in attesa del primo ciclo', ''],
  not_configured: ['non configurato', ''], config_disabled: ['disattivato nelle impostazioni', ''], credentials_missing: ['credenziali mancanti', 'warn'],
  session_missing: ['sessione mancante', 'warn'], calendar_missing: ['calendario non indicato', 'warn'], session_busy: ['sessione occupata', 'warn'],
  dependency_missing: ['dipendenza mancante', 'bad'], unauthorized: ['non autorizzato', 'bad'], bot_session_refused: ['sessione rifiutata', 'bad'], failed: ['errore', 'bad']};
const FLOW_CONFIG = {missing_configuration: 'configurazione mancante', disabled_in_config: 'disattivato nelle impostazioni'};
function renderFlows(d) {
  el('flows').replaceChildren(...d.flows.map(f => {
    const health = f.health;
    const age = health.observed_at ? Math.floor(Date.now() / 1000 - health.observed_at) : null;
    const [statusLabel, level] = FLOW_STATUS[health.status] || [health.status, 'warn'];
    const kind = f.enabled ? level : '';
    const detail = !f.enabled ? 'Sospeso' : [statusLabel, FLOW_CONFIG[f.configuration], age === null ? '' : `osservato ${age < 90 ? `${age}s` : `${Math.round(age / 60)} min`} fa`].filter(Boolean).join(' · ');
    const control = toggleSwitch({checked: f.enabled, label: `${f.label}: ${f.enabled ? 'abilitato, sospendi' : 'sospeso, abilita'}`, onchange: async input => {
      input.disabled = true;
      try {await api(`/api/flows/${f.id}`, {enabled: !f.enabled}); toast(`${f.label}: ${f.enabled ? 'sospeso' : 'abilitato'}`); await refresh();}
      catch (e) {input.checked = f.enabled; el('status').textContent = e.message; toast(e.message, 'bad');}
      finally {input.disabled = false;}
    }});
    return h('div', {class: 'flow'}, h('span', {class: `dot ${kind ? 'dot-' + kind : ''}`}), h('div', {}, h('b', {text: f.label}), h('small', {text: detail})), control);
  }));
}
const JOB_STATUS = {done: ['completati', 'j-done', 'sw-done'], pending: ['in attesa', 'j-pending', 'sw-pending'], running: ['in esecuzione', 'j-running', 'sw-running'], failed: ['falliti', 'j-failed', 'sw-failed']};
function renderJobs(d) {
  const jobs = d.jobs || [];
  const totals = {done: 0, pending: 0, running: 0, failed: 0, other: 0};
  jobs.forEach(job => {totals[job.status in totals ? job.status : 'other'] += job.count;});
  const parts = Object.keys(totals).map(key => ({label: (JOB_STATUS[key] || ['altri'])[0], value: totals[key], cls: (JOB_STATUS[key] || [0, 'j-other'])[1]}));
  const sum = parts.reduce((s, p) => s + p.value, 0);
  el('jobs-chart').replaceChildren(sum ? donut(parts, {center: fmt(sum), label: 'Lavori per stato'}) : h('span'),
    h('div', {class: 'donut-legend'}, Object.keys(totals).map(key => h('div', {}, h('span', {class: `swatch ${(JOB_STATUS[key] || [0, 0, 'sw-other'])[2]}`}), (JOB_STATUS[key] || ['altri'])[0], h('b', {text: fmt(totals[key])})))));
  const rank = {failed: 0, running: 1, pending: 2};
  const notable = jobs.filter(job => job.status !== 'done').sort((a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3));
  el('jobs-list').replaceChildren(...(notable.length ? notable.map(job => h('div', {class: 'row'},
    h('div', {}, h('b', {text: `${job.kind}`}), ' ', badge((JOB_STATUS[job.status] || [job.status])[0], job.status === 'failed' ? 'bad' : job.status === 'running' ? 'info' : ''),
      job.failure_reason ? h('small', {class: 'run-error', text: job.failure_reason}) : false),
    h('small', {text: `${fmt(job.count)} · età max ${Math.round(job.oldest_age_seconds || 0)}s`}))) : [emptyState(sum ? 'Nessun lavoro in coda o fallito.' : 'Nessun lavoro registrato.')]));
}
function renderTraffic(d) {
  const yesterday = dayShift(d.today, -1);
  const channels = [...new Set(d.counts.map(r => r[1]))];
  if (!channels.length) {el('traffic').replaceChildren(emptyState('Nessun messaggio ieri o oggi.')); return;}
  const pick = (day, channel, direction) => d.counts.filter(r => String(r[0]).slice(0, 10) === day && r[1] === channel && (!direction || r[2] === direction)).reduce((s, r) => s + r[3], 0);
  el('traffic').replaceChildren(...channels.map(channel => {
    const today = pick(d.today, channel); const before = pick(yesterday, channel);
    const analyzed = d.counts.filter(r => String(r[0]).slice(0, 10) === d.today && r[1] === channel).reduce((s, r) => s + r[4], 0);
    const diff = today - before;
    return h('div', {class: 'feed-item'}, h('span', {class: 'feed-icon'}, icon(channelIcon(channel))),
      h('div', {}, h('span', {class: 'feed-title', text: channelName(channel)}), h('small', {text: `↓ ${pick(d.today, channel, 'in')} · ↑ ${pick(d.today, channel, 'out')} · ${analyzed} analizzati`})),
      h('div', {}, h('b', {text: fmt(today)}), ' ', badge(diff ? `${diff > 0 ? '+' : '−'}${Math.abs(diff)}` : '=', diff > 0 ? 'ok' : diff < 0 ? 'warn' : '')));
  }));
  const pending = d.calendar_pending || [];
  el('calendar-pending').hidden = !pending.length;
  el('calendar-pending').replaceChildren(...(pending.length ? [h('h3', {text: 'Calendario in attesa'})] : []),
    ...pending.map(a => h('div', {class: 'row'}, h('div', {text: `#${a[0]} ${a[1]}`}), h('small', {text: `${a[2]} · ${a[3]}`}))));
}
function renderActivity(d) {
  const recent = d.recent || [];
  const shown = recent.slice(0, state.activityShown);
  el('activity').replaceChildren(...(shown.length ? shown.map(r => h('div', {class: 'feed-item'}, h('span', {class: 'feed-icon'}, icon(channelIcon(r[0]))),
    h('div', {}, h('span', {class: 'feed-title', text: `${channelName(r[0])} · ${r[1] === 'in' ? 'ricevuto' : r[1] === 'out' ? 'inviato' : (r[1] || 'direzione ignota')}`}),
      h('small', {title: localDate(r[2]), text: r[2] ? ago(r[2]) : 'data ignota'})),
    badge(r[3] === 'analyzed' ? 'analizzato' : 'archiviato', r[3] === 'analyzed' ? 'ok' : '')))
    : [emptyState('Nessuna attività recente.')]));
  el('activity-more').hidden = recent.length <= shown.length;
  el('activity-more').textContent = `Mostra altre ${recent.length - shown.length}`;
  // Le notifiche confermate restano una riga di riepilogo.
  if (shown.length) el('activity').append(h('div', {class: 'row'}, h('div', {text: 'Notifiche confermate'}), h('small', {text: String(d.notifications_sent || 0)})));
}
el('activity-more').onclick = () => {state.activityShown += 12; if (state.dashboard) renderActivity(state.dashboard);};
async function refresh() {
  const generation = sessionGeneration;
  try {
    const d = await api('/api/dashboard');
    if (generation !== sessionGeneration) return;
    show(true);
    state.dashboard = d;
    el('status').textContent = `${d.agent_name} · collegato · aggiornato ${new Date().toLocaleTimeString('it')}`;
    el('status').title = `Fuso orario: ${d.timezone}`;
    el('status').dataset.state = 'ok';
    renderKpis(); renderFlows(d); renderJobs(d); renderTraffic(d); renderActivity(d); renderArchive(d);
  } catch (e) {
    if (generation !== sessionGeneration) return;
    el('status').textContent = e.message; el('status').dataset.state = 'bad';
  }
}
el('refresh-now').onclick = () => {refresh(); loadTrends(); loadBudget(); loadAgent();};

// ------------------------------------------------------------ avvio e sessione
function loadWorkspace() {
  return Promise.all([loadTrends(), loadAgent(), loadPolicies(), loadBudget(), browseFiles()]);
}
el('login').onsubmit = async e => {
  e.preventDefault(); el('error').textContent = '';
  try {
    const data = Object.fromEntries(new FormData(e.target));
    const result = await api('/api/login', data);
    csrf = result.csrf; e.target.reset(); show(true); showPage(location.hash.slice(1) || 'overview');
    await Promise.all([refresh(), loadWorkspace()]);
  } catch (err) {el('error').textContent = err.message;}
};
el('logout').onclick = async () => {await api('/api/logout', {}); csrf = ''; show(false);};
api('/api/session').then(s => {
  csrf = s.csrf; show(true); showPage(location.hash.slice(1) || 'overview'); refresh(); loadWorkspace();
}).catch(() => {});
// Aggiornamento automatico: istantanea ogni 10 s; grafici e dati secondari ogni 30 s, solo se la scheda è visibile.
setInterval(() => {
  if (el('dashboard').hidden || document.hidden) return;
  refresh();
  state.tick += 1;
  if (state.tick % 3 === 0) {loadTrends(); loadBudget(); loadAgent();}
}, 10000);
