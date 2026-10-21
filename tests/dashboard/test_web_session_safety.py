"""Run the real web scripts (charts.js + app.js) in an offline Node VM with a synthetic DOM/fetch.

Only JavaScript source and synthetic fixtures enter the subprocess: no browser,
application imports, deployment configuration, database or network access.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest

from tests.support import REPO_ROOT, STATIC_DIR


ROOT = REPO_ROOT


def node_env():
    """PATH minimale per il subprocess node, con la sua directory reale.

    os.defpath (/bin:/usr/bin) non include /usr/local/bin, dove i runner CI
    (e molti deployment) installano Node: il subprocess erediterebbe un PATH
    senza node. Si antepone SOLO la directory del binario risolto: nessuna
    variabile d'ambiente del deployment viene comunque trasmessa.
    """
    path = os.defpath
    try:
        node_dir = Path(subprocess.run(['which', 'node'], capture_output=True,
                                        text=True).stdout.strip()).parent
        if str(node_dir) not in path.split(os.pathsep):
            path = str(node_dir) + os.pathsep + path
    except (OSError, ValueError):
        pass
    return {'PATH': path}


NODE_HARNESS = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {script, charts, scenario, options} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));

const matchers = {
  button: item => item.tagName === 'BUTTON',
  input: item => item.tagName === 'INPUT',
  '[data-key]': item => item.dataset.key !== undefined
};
const dialogs = [];
function textNode(text) {const item = new Element('#text'); item._text = text; return item;}
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = []; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.disabled = false; this.value = ''; this._text = ''; this._id = '';
    this.style = {};
    this.classList = {toggle() {}, add() {}, remove() {}, contains() {return false;}};
  }
  get id() {return this._id;}
  set id(value) {this._id = String(value); elements.set(this._id, this);}
  get textContent() {return this._text + this.children.map(child => child.textContent).join('');}
  set textContent(value) {this._text = String(value); this.children = [];}
  append(...children) {
    // Il DOM vero trasforma ogni valore non-nodo in testo ("false", "null"): qui diventa un errore.
    children.forEach(child => assert.ok(typeof child === 'string' || child instanceof Element,
      `append() requires nodes or strings, got ${String(child)}`));
    this.children.push(...children.map(child => (typeof child === 'string' ? textNode(child) : child)));
  }
  replaceChildren(...children) {this.textContent = ''; this.append(...children);}
  setAttribute(name, value) {this.attributes[name] = String(value);}
  querySelectorAll(selector) {
    const match = matchers[selector];
    assert.ok(match, 'Unexpected synthetic DOM selector');
    return this.children.flatMap(child => [...(match(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  // Il dialogo modale mette il focus sul pulsante di conferma: la prova risponde al posto dell'utente.
  focus() {
    if (this._id !== 'dialog-ok') return;
    dialogs.push({title: element('dialog-title').textContent, body: element('dialog-body').textContent});
    (options.dialog === 'cancel' ? element('dialog-cancel') : this).onclick();
  }
  reset() {}
  scrollIntoView() {}
}
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {const item = new Element(); item._id = id; elements.set(id, item);}
  return elements.get(id);
}
function walk(item, visit) {visit(item); item.children.forEach(child => walk(child, visit));}
function findAll(root, predicate) {const found = []; walk(root, item => {if (predicate(item)) found.push(item);}); return found;}
// Minimal page navigation state: the real app uses hash-based pages.
const navButtons = [];
let hash = '';
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
const requests = [];
const context = vm.createContext({
  document: {
    getElementById: element,
    createElement: tag => new Element(tag),
    createElementNS: (namespace, tag) => new Element(tag),
    querySelectorAll: selector => {
      assert.equal(selector, '.nav-link', 'Unexpected document selector');
      return navButtons;
    }
  },
  location: {get hash() {return hash;}, set hash(value) {hash = String(value);}},
  history: {replaceState() {}},
  window: {addEventListener() {}, scrollTo() {}},
  URLSearchParams,
  fetch(path, init) {
    const request = {...deferred(), path, init};
    requests.push(request);
    return request.promise;
  },
  // Never run the production timers or expose real browser/network APIs. `confirm` is deliberately absent:
  // every confirmation goes through the in-page dialog, so a stray window.confirm() would throw here.
  setInterval() {}, setTimeout() {return 0;}, clearTimeout() {}
});
const run = code => vm.runInContext(code, context);
const checkpoint = () => new Promise(resolve => setImmediate(resolve));
function take(path) {
  const request = requests.shift();
  assert.ok(request, `Missing request: ${path}`);
  assert.equal(request.path, path);
  return request;
}
function respond(request, body, status = 200) {
  request.resolve({ok: status >= 200 && status < 300, status, json: async () => body});
}
function domState() {
  const describe = item => ({text: item._text, hidden: item.hidden,
    disabled: item.disabled, value: item.value, dataset: {...item.dataset},
    children: item.children.map(describe)});
  return [...elements].map(([id, item]) => [id, describe(item)]);
}
const privateIds = ['agent-runs', 'agent-emails'];
function snapshot(marker) {
  return {model: 'openrouter/auto', readonly_role: true, sends_allowed: false, limits: {},
    runs: [{id: 11, kind: 'owner', state: 'done', tool_calls: 2, error: `${marker} run`,
      model: 'openrouter/auto', started_at: '2099-01-01T00:00:00Z', finished_at: null}],
    emails: [{id: 5, account: `${marker}@example.invalid`, recipient: 'to@example.invalid',
      subject: `${marker} subject`, body: `${marker} body`, state: 'proposed',
      created_at: '2099-01-01T00:00:00Z', decided_at: null, sent_at: null}]};
}
function detail(marker) {
  return {run_id: 11, calls: [{seq: 1, tool: 'db_query', args: `${marker} args`, ok: true,
    summary: `${marker} summary`, duration_ms: 12, at: '2099-01-01T00:00:00Z'}]};
}
function finish(request, outcome, body) {
  if (outcome === 'network') request.reject(Error('Synthetic network failure'));
  else if (outcome === 'json') request.resolve({ok: true, status: 200,
    json: async () => {throw Error('Synthetic JSON failure');}});
  else if (outcome === 'unauthorized') respond(request, {error: 'Sessione scaduta'}, 401);
  else if (outcome === 'unavailable') respond(request, {error: 'Schema non disponibile'}, 503);
  else {assert.equal(outcome, 'success'); respond(request, body);}
}
async function logout() {
  const leaving = element('logout').onclick();
  respond(take('/api/logout'), {});
  await leaving;
  assert.equal(element('dashboard').hidden, true);
  for (const id of [...privateIds, 'agent-calls']) assert.equal(element(id).textContent, '', id);
  assert.equal(element('agent-detail').hidden, true);
}
async function login() {
  // Use the real API/session helpers without unrelated workspace loaders.
  const entering = run(`api('/api/login', {username: 'offline', password: 'synthetic'})
    .then(result => {csrf = result.csrf; show(true);})`);
  respond(take('/api/login'), {csrf: 'new-synthetic-session'});
  await entering;
  assert.equal(element('dashboard').hidden, false);
}
function assertCurrent() {
  for (const id of privateIds) assert.ok(element(id).textContent.includes('CURRENT_PRIVATE'), id);
  assert.equal(element('agent-refresh').disabled, false);
  assert.ok(element('agent-status').textContent.includes('Solo metadati'));
}

async function currentLoad() {
  const loading = run('loadAgent()');
  assert.equal(element('agent-refresh').disabled, true);
  finish(take('/api/agent'), options.outcome, snapshot('CURRENT_PRIVATE'));
  await loading;
  if (options.outcome === 'unauthorized') {
    assert.equal(element('dashboard').hidden, true);
    for (const id of privateIds) assert.equal(element(id).textContent, '', id);
    assert.ok(!element('agent-status').textContent.includes('Solo metadati'));
  } else if (options.outcome === 'success') {
    assertCurrent();
    // Un invio non è mai offerto dalla dashboard: la decisione passa da Telegram.
    assert.ok(element('agent-emails').textContent.includes('/invia 5'));
    assert.equal(element('agent-send').textContent.includes('disabilitato'), true);
  } else {
    for (const id of privateIds) assert.equal(element(id).textContent.includes('CURRENT_PRIVATE'), false, id);
    assert.equal(element('agent-refresh').disabled, false);
    assert.ok(element('agent-status').textContent.includes('non disponibile'));
  }
}
async function staleLoad() {
  const old = {loading: run('loadAgent()'), request: take('/api/agent')};
  await logout();
  let current;
  if (options.session !== 'logged_out') {
    await login();
    current = {loading: run('loadAgent()'), request: take('/api/agent')};
    if (options.session === 'new_rendered') {
      respond(current.request, snapshot('CURRENT_PRIVATE'));
      await current.loading;
      assertCurrent();
    }
  }
  // A stale continuation must not change ANY current DOM/control state,
  // including catches/finally and the refresh button of a newer pending load.
  const before = domState();
  finish(old.request, options.outcome, snapshot('OLD_PRIVATE'));
  await old.loading;
  await checkpoint();
  assert.deepEqual(domState(), before, 'Stale agent continuation changed the DOM');
  if (options.session === 'new_pending') {
    respond(current.request, snapshot('CURRENT_PRIVATE'));
    await current.loading;
    assertCurrent();
  }
}
async function staleDetail() {
  const opening = run('openRun({id: 11, kind: "owner"})');
  const request = take('/api/agent/runs/11');
  await logout();
  if (options.session !== 'logged_out') {
    await login();
    const reload = run('loadAgent()');
    respond(take('/api/agent'), snapshot('CURRENT_PRIVATE'));
    await reload;
  }
  const before = domState();
  finish(request, options.outcome, detail('OLD_PRIVATE'));
  await opening;
  await checkpoint();
  assert.deepEqual(domState(), before, 'Stale run detail changed the DOM');
  assert.equal(element('agent-detail').hidden, true);
}
async function openDetail() {
  const loading = run('loadAgent()');
  respond(take('/api/agent'), snapshot('CURRENT_PRIVATE'));
  await loading;
  const button = element('agent-runs').querySelector('button');
  assert.ok(button, 'Missing tool-usage button');
  const opening = button.onclick();
  respond(take('/api/agent/runs/11'), detail('CURRENT_PRIVATE'));
  await opening;
  assert.equal(element('agent-detail').hidden, false);
  assert.ok(element('agent-calls').textContent.includes('db_query'));
  assert.ok(element('agent-calls').textContent.includes('CURRENT_PRIVATE summary'));
}
async function staleMediaRetry() {
  element('media-retry-id').value = '42';
  element('media-retry-channel').value = 'telegram';
  const form = new Element('form'); form.append(new Element('button'));
  const submitting = element('media-retry').onsubmit({preventDefault() {}, target: form});
  await checkpoint();  // la conferma è un dialogo nella pagina: la richiesta parte dopo la risposta
  const request = take('/api/media/retry-blocked');
  assert.deepEqual(JSON.parse(request.init.body), {message_id: 42, expected_channel: 'telegram'});
  await logout();
  const before = domState();
  finish(request, options.outcome, {queued: 1});
  await submitting;
  await checkpoint();
  assert.deepEqual(domState(), before, 'Stale media retry changed the DOM');
}

async function mediaRecovery() {
  const form = new Element('form'); form.append(new Element('button'));
  element('media-retry-channel').value = 'telegram (account)';
  if (options.invalid) {
    element('media-retry-id').value = '0';
    await element('media-retry').onsubmit({preventDefault() {}, target: form});
    assert.equal(requests.length, 0, 'An invalid message ID must never reach the server');
    assert.equal(dialogs.length, 0, 'An invalid message ID must not even ask for confirmation');
    assert.ok(element('media-retry-status').textContent.includes('ID numerico'));
    return;
  }
  element('media-retry-id').value = '7';
  const pending = element('media-retry').onsubmit({preventDefault() {}, target: form});
  await checkpoint();
  assert.equal(dialogs.length, 1, 'The media retry must be confirmed explicitly');
  assert.ok(dialogs[0].body.includes('#7'));
  if (options.dialog === 'cancel') {
    await pending;
    assert.equal(requests.length, 0, 'A cancelled confirmation must never reach the server');
    assert.equal(form.children[0].disabled, false);
    return;
  }
  assert.equal(form.children[0].disabled, true);
  const request = take('/api/media/retry-blocked');
  assert.equal(request.init.method, 'POST');
  assert.equal(request.init.headers['X-CSRF-Token'], 'synthetic-session');
  assert.deepEqual(JSON.parse(request.init.body), {message_id: 7, expected_channel: 'telegram (account)'});
  finish(request, options.outcome, {queued: options.queued});
  await pending;
  assert.equal(form.children[0].disabled, false);
  assert.ok(element('media-retry-status').textContent.includes(options.outcome === 'success'
    ? options.queued ? 'media accodati' : 'Nessun media recuperabile'
    : 'Verifica entrambi i consensi'));
}

// ---------------------------------------------------------------- budget
function budgetFixture() {
  return {available: true, day_timezone: 'UTC',
    usage: {calls: 240, reserved_tokens: 1950000, charged_tokens: 1900000, reported_tokens: null,
      cost_unreported_calls: 0, inflight: 4, reported_cost: '0.5'},
    limits: {DAILY_CALLS: 400, DAILY_TOKENS: 2000000, MAX_INFLIGHT: 4, MAX_OUTPUT_TOKENS: 65536},
    daily_cost: '1.00', money_cap_enforced: true, scope_caps: {agent_owner: 100}, scope_usage: {agent_owner: 100},
    orphaned: [{job_id: 9, reserved_tokens: 100, created_at: '2099-01-01T00:00:00Z'}]};
}
function budgetLevels() {
  // Soglie: verde <60%, gialla 60–80%, arancione 80–100% (100 escluso), rossa >=100% e non finito.
  const levels = [...run('[0, 0.59, 0.6, 0.79, 0.8, 0.99, 1, 1.5, Infinity, NaN].map(budgetLevel)')];
  assert.deepEqual(levels, ['ok', 'ok', 'warn', 'warn', 'high', 'high', 'full', 'full', 'full', 'full']);
}
async function budgetRender() {
  const loading = run('loadBudget()');
  respond(take('/api/budget'), budgetFixture());
  await loading;
  const box = element('ai-budget');
  const cards = findAll(box, item => item.dataset.level);
  assert.deepEqual(cards.map(card => card.dataset.level), ['warn', 'high', 'full', 'ok']);
  assert.equal(findAll(box, item => item.className === 'flag').length, 2, 'SATURATO: slot pieni e tetto per ambito');
  assert.ok(box.textContent.includes('SATURATO'));
  const edits = findAll(box, item => item.tagName === 'BUTTON' && item.textContent === 'Modifica questo limite');
  assert.equal(edits.length, 4, 'Ogni misura con un limite configurabile offre la modifica');
  const bars = findAll(box, item => item.attributes.role === 'progressbar');
  assert.equal(bars.length, 1);
  assert.equal(bars[0].className, 'meter-fill lvl-full');
  assert.equal(bars[0].attributes['aria-valuenow'], '100');
  assert.equal(bars[0].attributes['aria-valuemax'], '100');
  assert.ok(bars[0].attributes['aria-label'].includes('rosso'));
  assert.ok(box.textContent.includes('non comunicati'));
  // Panoramica: le stesse misure come barre, senza pulsante di modifica.
  const summary = findAll(element('overview-budget'), item => item.attributes.role === 'progressbar');
  assert.deepEqual(summary.map(bar => bar.className), ['meter-fill lvl-warn', 'meter-fill lvl-high', 'meter-fill lvl-full']);
  assert.equal(findAll(element('overview-budget'), item => item.tagName === 'BUTTON').length, 0);
  // Slot bloccati da worker morti: riaccodo la posta, poi libero gli slot e ricarico.
  assert.ok(box.textContent.includes('1 slot bloccati'));
  const button = label => findAll(box, item => item.tagName === 'BUTTON' && item.textContent === label)[0];
  const requeueing = button('Riaccoda posta fallita').onclick();
  const requeue = take('/api/jobs/requeue');
  assert.deepEqual(JSON.parse(requeue.init.body), {kind: 'analysis'});
  respond(requeue, {requeued: [1, 2]});
  await requeueing;
  assert.ok(element('toasts').textContent.includes('Messaggi riaccodati: 2'));
  const freeing = button('Libera slot').onclick();
  const reconcile = take('/api/budget/reconcile');
  assert.deepEqual(JSON.parse(reconcile.init.body), {});
  respond(reconcile, {freed: 1});
  await checkpoint();
  respond(take('/api/budget'), budgetFixture());
  await freeing;
  assert.ok(element('toasts').textContent.includes('Slot liberati: 1'));
}

// ------------------------------------------------------------------ consensi
function policies(allowedFirst) {
  const rows = [];
  for (const channel of ['email', 'telegram', 'whatsapp']) {
    for (const effect of ['ai_text', 'ai_media', 'ai_history']) {
      const first = channel === 'email' && effect === 'ai_text';
      rows.push({channel, effect, allowed: first && allowedFirst, version: first && allowedFirst ? 1 : 0});
    }
  }
  return {policies: rows, scope: 'channel/effect', default_allowed: false};
}
async function consent() {
  const loading = run('loadPolicies()');
  respond(take('/api/data-policies'), policies(false));
  await loading;
  const switches = () => findAll(element('data-policies'), item => item.tagName === 'INPUT');
  assert.equal(switches().length, 9);
  assert.ok(switches().every(item => item.checked !== true), 'Nessun consenso e` preabilitato');
  const toggle = switches()[0];
  toggle.checked = true;
  const changing = toggle.onchange();
  await checkpoint();
  assert.equal(dialogs.length, 1, 'Un consenso va confermato esplicitamente');
  assert.ok(dialogs[0].body.includes('Email'));
  if (options.dialog === 'cancel') {
    await changing;
    assert.equal(requests.length, 0, 'Un dialogo annullato non deve raggiungere il server');
    assert.equal(toggle.checked, false, 'L’interruttore torna allo stato salvato');
    return;
  }
  const request = take('/api/data-policies');
  assert.deepEqual(JSON.parse(request.init.body), {channel: 'email', effect: 'ai_text', allowed: true, expected_version: 0});
  assert.equal(toggle.disabled, true);
  respond(request, {channel: 'email', effect: 'ai_text', allowed: true, version: 1});
  await checkpoint();
  respond(take('/api/data-policies'), policies(true));
  await changing;
  assert.equal(switches()[0].checked, true);
  assert.ok(element('data-policies').textContent.includes('Consentito esplicitamente'));
}

// ---------------------------------------------------------------- impostazioni
function configFixture() {
  return {revision: 'rev1', apply: 'Runtime non Docker: riavvio manuale.', fields: [
    {key: 'AGENT_MODEL', group: 'Agente', type: 'text', value: 'openrouter/auto', secret: false, configured: true, description: 'Modello dell’agente.'},
    {key: 'OPENROUTER_API_KEY', group: 'Agente', type: 'password', value: '', secret: true, configured: true},
    {key: 'SECRETARY_MODE', group: 'Agente', type: 'select', options: ['observe', 'active'], value: 'observe', secret: false, configured: true},
    {key: 'DASHBOARD_PORT', group: 'Agente', type: 'number', value: '8080', secret: false, configured: true, readonly: 'Solo deployment.'},
    {key: 'GCAL_ENABLED', group: 'Calendar', type: 'select', options: ['true', 'false'], value: 'false', secret: false, configured: false},
    {key: 'WHATSAPP_ENABLED', group: 'Calendar', type: 'select', options: ['true', 'false'], value: '', secret: false, configured: false}]};
}
const changes = () => JSON.parse(run('JSON.stringify(pendingChanges())'));
async function configLifecycle() {
  const loading = run('loadConfiguration()');
  respond(take('/api/configuration'), configFixture());
  await loading;
  assert.equal(element('config-groups').children.length, 2);
  const model = element('setting-AGENT_MODEL');
  assert.equal(model.value, 'openrouter/auto');
  assert.equal(model.attributes['aria-describedby'], 'setting-AGENT_MODEL-help');
  assert.ok(element('setting-AGENT_MODEL-help').textContent.includes('Modello'));
  const secret = element('setting-OPENROUTER_API_KEY');
  assert.equal(secret.type, 'password');
  assert.equal(secret.value, '', 'Una credenziale salvata non viene mai mostrata');
  const mode = element('setting-SECRETARY_MODE');
  assert.equal(mode.attributes.role, 'radiogroup');
  assert.equal(mode.querySelectorAll('button').length, 2);
  assert.equal(mode.value, 'observe');
  assert.equal(element('setting-DASHBOARD_PORT').disabled, true);
  // Un valore non impostato resta leggibile ("Predefinito") e non e` scegliibile di nuovo se il vuoto non e` ammesso.
  const unset = element('setting-WHATSAPP_ENABLED').querySelectorAll('button');
  assert.deepEqual(unset.map(button => button.dataset.value), ['', 'true', 'false']);
  assert.equal(unset[0].attributes['aria-checked'], 'true');
  assert.equal(unset[0].disabled, true);
  assert.equal(unset[1].disabled, false);
  // Nessuna modifica: niente riepilogo e niente richiesta.
  await element('config-form').onsubmit({preventDefault() {}});
  assert.equal(element('config-review').hidden, true);
  assert.ok(element('config-status').textContent.includes('Nessuna modifica'));
  // Modifiche: solo i campi cambiati; i campi di sola lettura non contano mai.
  const edit = () => {
    model.value = 'x/y'; model.oninput();
    mode.querySelectorAll('button')[1].onclick();
    secret.value = 'sk-new-secret'; secret.oninput();
    element('setting-DASHBOARD_PORT').value = '9999';
  };
  edit();
  assert.deepEqual(changes(), {AGENT_MODEL: 'x/y', SECRETARY_MODE: 'active', OPENROUTER_API_KEY: 'sk-new-secret'});
  assert.ok(element('config-status').textContent.includes('3 modifiche'));
  element('config-cancel').onclick();
  assert.deepEqual(changes(), {});
  assert.equal(model.value, 'openrouter/auto');
  assert.equal(mode.value, 'observe');
  edit();
  await element('config-form').onsubmit({preventDefault() {}});
  assert.equal(element('config-review').hidden, false);
  const review = element('config-changes').textContent;
  assert.ok(review.includes('Modello dell’agente · valore modificato'));
  assert.ok(review.includes('Chiave API OpenRouter · sostituzione credenziale (valore nascosto)'));
  assert.ok(!review.includes('sk-new-secret') && !review.includes('x/y'), 'Il riepilogo non mostra i valori');
  const saving = element('config-confirm').onclick();
  assert.equal(model.disabled, true, 'Durante il salvataggio i campi sono bloccati');
  assert.equal(mode.querySelectorAll('button')[0].disabled, true);
  const request = take('/api/configuration');
  assert.deepEqual(JSON.parse(request.init.body), {revision: 'rev1',
    changes: {AGENT_MODEL: 'x/y', SECRETARY_MODE: 'active', OPENROUTER_API_KEY: 'sk-new-secret'}});
  if (options.fail) {
    respond(request, {error: 'Verifica fallita'}, 400);
    await saving;
    assert.ok(element('config-status').textContent.includes('Non salvato'));
    assert.equal(secret.value, '', 'Dopo un errore la credenziale digitata viene azzerata');
    assert.equal(model.value, 'x/y', 'Le altre modifiche restano per riprovare');
    assert.equal(model.disabled, false);
    assert.equal(mode.querySelectorAll('button')[0].disabled, false);
    assert.equal(element('config-review').hidden, true);
    return;
  }
  respond(request, {ok: true, restart_scheduled: true});
  await saving;
  assert.equal(element('config-fields').textContent, '');
  assert.ok(element('config-status').textContent.includes('Riavvio automatico'));
  assert.equal(run('configRevision'), '', 'La revisione salvata non si riusa');
  assert.equal(element('config-form').hidden, true);
}

// ----------------------------------------------------------------- panoramica
function dashboardFixture(flowEnabled) {
  const today = '2026-10-21';
  return {agent_name: 'p-secretary', timezone: 'Europe/Rome', today,
    counts: [[today, 'email', 'in', 3, 2, 1], ['2026-10-20', 'email', 'in', 1, 1, 0], [today, 'telegram', 'out', 2, 0, 0]],
    recent: [['email', 'in', '2026-10-21T08:00:00+00:00', 'analyzed'], ['telegram', 'out', null, 'archived']],
    jobs: [{kind: 'analysis', status: 'failed', count: 2, oldest_age_seconds: 30, last_success: null, failure_reason: 'Timeout provider'},
           {kind: 'analysis', status: 'done', count: 10, oldest_age_seconds: 5, last_success: null, failure_reason: null}],
    calendar_pending: [[1, 'Visita', '2026-10-22 10:00', 'pending']], notifications_sent: 3,
    tasks: [['open', 4], ['done', 1]], invoices: [['hypaz', '2026-10', 3]], appointments: 2, zips: [['hypaz', '2026-10', false]],
    flows: [{id: 'email', label: 'Email', enabled: true, configuration: 'configured', health: {status: 'running', observed_at: null}},
            {id: 'invoices', label: 'Fatture', enabled: flowEnabled, configuration: 'configured', health: {status: 'paused'}}]};
}
function trendsFixture() {
  const days = Array.from({length: 14}, (_, index) => `2026-10-${String(8 + index).padStart(2, '0')}`);
  const zeros = () => Array(14).fill(0);
  const email = {in: zeros(), out: zeros(), other: zeros()}; email.in[13] = 3; email.out[12] = 1;
  const telegram = {in: zeros(), out: zeros(), other: zeros()}; telegram.out[13] = 2;
  const tokens = zeros(); tokens[13] = 1500; const calls = zeros(); calls[13] = 4;
  return {available: true, timezone: 'Europe/Rome', ai_day_timezone: 'UTC', days, messages: {email, telegram},
    ai: {calls, tokens, cost: zeros()}, runs: {done: zeros(), failed: zeros(), running: zeros()}};
}
async function overview() {
  const refreshing = run('refresh()');
  respond(take('/api/dashboard'), dashboardFixture(false));
  await refreshing;
  assert.equal(element('status').dataset.state, 'ok');
  const values = findAll(element('kpis'), item => item.className === 'kpi-value').map(item => item.textContent);
  assert.deepEqual(values, ['3', '2', '1', '4', '2']);  // in, out, da gestire, attività aperte, appuntamenti
  assert.ok(element('kpis').textContent.includes('Attività aperte'));
  assert.ok(element('jobs-list').textContent.includes('Timeout provider'));
  assert.ok(element('traffic').textContent.includes('Email') && element('traffic').textContent.includes('+2'));
  assert.ok(element('calendar-pending').textContent.includes('Visita'));
  assert.ok(element('activity').textContent.includes('ricevuto'));
  assert.ok(element('invoices').textContent.includes('hypaz'));
  // Interruttore del flusso sospeso: una richiesta esatta, poi ricarica.
  const flowSwitches = findAll(element('flows'), item => item.tagName === 'INPUT');
  assert.deepEqual(flowSwitches.map(item => item.checked === true), [true, false]);
  flowSwitches[1].checked = true;
  const toggling = flowSwitches[1].onchange();
  const toggle = take('/api/flows/invoices');
  assert.deepEqual(JSON.parse(toggle.init.body), {enabled: true});
  respond(toggle, {ok: true});
  await checkpoint();
  respond(take('/api/dashboard'), dashboardFixture(true));
  await toggling;
  assert.equal(findAll(element('flows'), item => item.tagName === 'INPUT')[1].checked, true);
  // Grafici dalle serie giornaliere.
  const loading = run('loadTrends()');
  respond(take('/api/trends'), trendsFixture());
  await loading;
  const columns = () => findAll(element('messages-chart'), item => item.attributes['aria-label'] && item.onmouseenter);
  assert.equal(columns().length, 14);
  assert.ok(columns()[13].attributes['aria-label'].includes('Email 3'));
  columns()[13].onmouseenter();
  assert.ok(element('messages-readout').textContent.includes('totale 5'));
  element('messages-direction').querySelectorAll('button')[1].onclick();  // solo in entrata
  columns()[13].onmouseenter();
  assert.ok(element('messages-readout').textContent.includes('totale 3'));
  findAll(element('messages-legend'), item => item.tagName === 'BUTTON')[0].onclick();  // nasconde Email
  columns()[13].onmouseenter();
  assert.ok(element('messages-readout').textContent.includes('totale 0'));
  const kpiSparks = findAll(element('kpis'), item => item.attributes.class === 'spark');
  assert.ok(kpiSparks.length >= 2, 'I KPI mostrano l’andamento dei 14 giorni');
}

(async () => {
  vm.runInContext(charts, context, {filename: 'static/charts.js'});
  vm.runInContext(script, context, {filename: 'static/app.js'});
  // Finish the real bootstrap request as an anonymous, offline session.
  respond(take('/api/session'), {error: 'Not authenticated'}, 401);
  await checkpoint();
  run("csrf = 'synthetic-session'; show(true);");
  if (scenario === 'media') await mediaRecovery();
  else if (scenario === 'detail') await openDetail();
  else if (scenario === 'stale') await staleLoad();
  else if (scenario === 'stale_detail') await staleDetail();
  else if (scenario === 'stale_media') await staleMediaRetry();
  else if (scenario === 'budget_levels') budgetLevels();
  else if (scenario === 'budget_render') await budgetRender();
  else if (scenario === 'consent') await consent();
  else if (scenario === 'config') await configLifecycle();
  else if (scenario === 'overview') await overview();
  else {assert.equal(scenario, 'current'); await currentLoad();}
  assert.equal(requests.length, 0, 'Unexpected API request');
  console.log('PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
'''


class WebSessionSafetyTests(unittest.TestCase):
    def run_scenario(self, scenario, **options):
        result = subprocess.run(
            ['node', '--unhandled-rejections=strict', '-e', NODE_HARNESS],
            input=json.dumps(dict(script=(STATIC_DIR / 'app.js').read_text(),
                                 charts=(STATIC_DIR / 'charts.js').read_text(),
                                 scenario=scenario, options=options)),
            cwd=ROOT, env=node_env(), text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_current_session_renders_runs_and_drafts_without_a_send_control(self):
        for outcome in ('success', 'unavailable'):
            with self.subTest(outcome=outcome):
                self.run_scenario('current', outcome=outcome)

    def test_unauthorized_cannot_restore_the_agent_view(self):
        self.run_scenario('current', outcome='unauthorized')

    def test_run_detail_lists_tools_used(self):
        self.run_scenario('detail')

    def test_logout_during_load_cannot_restore_private_dom(self):
        for outcome in ('success', 'unavailable', 'network', 'json', 'unauthorized'):
            with self.subTest(outcome=outcome):
                self.run_scenario('stale', session='logged_out', outcome=outcome)

    def test_relogin_fences_render_errors_and_cleanup(self):
        for session in ('new_pending', 'new_rendered'):
            for outcome in ('success', 'unavailable', 'network', 'json', 'unauthorized'):
                with self.subTest(session=session, outcome=outcome):
                    self.run_scenario('stale', session=session, outcome=outcome)

    def test_stale_run_detail_is_session_fenced(self):
        for session in ('logged_out', 'new_rendered'):
            for outcome in ('success', 'network', 'unauthorized'):
                with self.subTest(session=session, outcome=outcome):
                    self.run_scenario('stale_detail', session=session, outcome=outcome)

    def test_media_retry_is_explicit_per_message_and_reports_the_real_outcome(self):
        for queued in (0, 1):
            self.run_scenario('media', outcome='success', queued=queued)
        for outcome in ('unavailable', 'network'):
            self.run_scenario('media', outcome=outcome)
        self.run_scenario('media', invalid=True)
        # Un dialogo annullato non raggiunge mai il server.
        self.run_scenario('media', outcome='success', queued=1, dialog='cancel')

    def test_stale_media_retry_is_session_fenced(self):
        for outcome in ('success', 'network', 'unavailable'):
            with self.subTest(outcome=outcome):
                self.run_scenario('stale_media', outcome=outcome)

    def test_budget_thresholds_are_exact(self):
        self.run_scenario('budget_levels')

    def test_budget_page_shows_levels_flags_edit_links_and_repair_actions(self):
        self.run_scenario('budget_render')

    def test_consent_switch_needs_explicit_confirmation_and_sends_an_exact_request(self):
        for dialog in ('ok', 'cancel'):
            with self.subTest(dialog=dialog):
                self.run_scenario('consent', dialog=dialog)

    def test_config_form_sends_only_changes_hides_secrets_and_recovers_from_errors(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.run_scenario('config', fail=fail)

    def test_overview_renders_kpis_flows_jobs_and_charts(self):
        self.run_scenario('overview')


if __name__ == '__main__':
    unittest.main()
