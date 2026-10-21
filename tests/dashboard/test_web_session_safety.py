"""Run the real web script in an offline Node VM with a synthetic DOM/fetch.

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
const {script, scenario, options} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = []; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.disabled = false; this.value = ''; this._text = '';
    this.style = {};
    this.classList = {toggle() {}, add() {}, remove() {}, contains() {return false;}};
  }
  get textContent() {return this._text + this.children.map(child => child.textContent).join('');}
  set textContent(value) {this._text = String(value); this.children = [];}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.textContent = ''; this.append(...children);}
  setAttribute(name, value) {this.attributes[name] = String(value);}
  querySelectorAll(selector) {
    assert.equal(selector, 'button', 'Unexpected synthetic DOM selector');
    return this.children.flatMap(child => [
      ...(child.tagName === 'BUTTON' ? [child] : []), ...child.querySelectorAll(selector)
    ]);
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  reset() {}
  scrollIntoView() {}
}
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
}
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
  // Never run the production refresh timer or expose real browser/network APIs.
  setInterval() {},
  confirm: () => true
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
    assert.ok(element('media-retry-status').textContent.includes('ID numerico'));
    return;
  }
  element('media-retry-id').value = '7';
  const pending = element('media-retry').onsubmit({preventDefault() {}, target: form});
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

(async () => {
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
                                 scenario=scenario, options=options)),
            cwd=ROOT, env=node_env(), text=True, capture_output=True, timeout=10,
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

    def test_stale_media_retry_is_session_fenced(self):
        for outcome in ('success', 'network', 'unavailable'):
            with self.subTest(outcome=outcome):
                self.run_scenario('stale_media', outcome=outcome)


if __name__ == '__main__':
    unittest.main()
