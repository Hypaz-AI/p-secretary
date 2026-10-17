"""Run the real web script in an offline Node VM with a synthetic DOM/fetch.

Only JavaScript source and synthetic fixtures enter the subprocess: no browser,
application imports, deployment configuration, database or network access.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent


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
  setInterval() {}
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
const privateIds = ['practices', 'responsibilities', 'approvals',
  'secretary-receipts', 'secretary-decisions', 'email-practice', 'email-executions',
  'email-account'];
function snapshot(marker) {
  return {
    practices: [{id: 11, version: 2, state: 'active', objective: `${marker} practice`}],
    responsibilities: [{id: 22, version: 1, state: 'active', objective: `${marker} duty`,
      scope: {topic: marker, channels: ['email']}, allowed_actions: [], stop_condition: marker}],
    approvals: [{id: 33, practice_id: 11, version: 1, state: 'pending',
      effective_state: 'pending', payload_hash: marker, expires_at: '2099-01-01T00:00:00Z',
      payload: {channel: 'email', text: `${marker} approval`}}],
    receipts: [{receipt: `${marker} receipt`}], decisions: [{event: `${marker} decision`}],
    wakeups: [{practice_id: 11, reason: `${marker} wakeup`}], links: [], truncated: {}
  };
}
function execution(marker) {
  const address = `${marker}@example.invalid`;
  return {subject: `${marker} subject`, accounts: [{account_address: address}],
    proposals: [{approval_id: 33, expected_version: 1, state: 'pending',
      dispatch_state: 'not_requested', account_address: address, subject: `${marker} subject`,
      expires_at: '2099-01-01T00:00:00Z', payload: {recipient: address,
        text: `${marker} email`, channel: 'email', documents: []}}]};
}
function finish(request, outcome, body) {
  if (outcome === 'network') request.reject(Error('Synthetic network failure'));
  else if (outcome === 'json') request.resolve({ok: true, status: 200,
    json: async () => {throw Error('Synthetic JSON failure');}});
  else if (outcome === 'unauthorized') respond(request, {error: 'Sessione scaduta'}, 401);
  else if (outcome === 'unavailable') respond(request, {error: 'Schema non disponibile'}, 503);
  else {assert.equal(outcome, 'success'); respond(request, body);}
}
async function beginLoad(marker) {
  const loading = run('loadSecretary()');
  respond(take('/api/secretary'), snapshot(marker));
  await checkpoint();
  return {loading, request: take('/api/secretary/execution')};
}
async function logout() {
  const leaving = element('logout').onclick();
  respond(take('/api/logout'), {});
  await leaving;
  assert.equal(element('dashboard').hidden, true);
  for (const id of privateIds) assert.equal(element(id).textContent, '', id);
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
  assert.equal(element('email-prepare').disabled, false);
  assert.equal(element('secretary-refresh').disabled, false);
}

async function searchedSource(hit, expectedPath, source) {
  element('knowledge-query').value = 'revision';
  const form = new Element('form'); form.append(new Element('button'));
  const searching = element('knowledge-search').onsubmit({preventDefault() {}, target: form});
  respond(take('/api/knowledge/search?q=revision&limit=20'), {results: [hit]});
  await searching;
  const button = element('knowledge-results').querySelector('button');
  const opening = button.onclick();
  assert.equal(button.disabled, true);
  respond(take(expectedPath), source);
  await opening;
  assert.equal(element('source-text').textContent, source.text);
  assert.ok(element('source-meta').textContent.includes(source.citation));
  assert.equal(element('knowledge-source').hidden, false);
  assert.equal(button.disabled, false);
}
async function sources() {
  const document = {source_type: 'document', source_id: 404, entry_id: 904,
    citation: 'document:404@old-revision', title: 'Old attachment occurrence'};
  if (options.kind === 'message') {
    // The native message ID now refers to a newer revision; only entry 901
    // still identifies the text/citation that this search returned.
    const hit = {source_type: 'message', source_id: 7, entry_id: 901,
      citation: 'message:7@old-revision', snippet: 'old searched text'};
    await searchedSource(hit, '/api/knowledge/open/entry/901', {...hit,
      status: 'indexed', text: 'old searched text', documents: [document]});
    // Attachments returned by that exact message retain their occurrence IDs.
    const opening = element('source-documents').querySelector('button').onclick();
    respond(take('/api/knowledge/open/document/404'), {...document,
      status: 'indexed', text: 'old attachment text'});
    await opening;
    assert.equal(element('source-text').textContent, 'old attachment text');
    assert.ok(element('source-meta').textContent.includes(document.citation));
  } else if (options.kind === 'document') {
    await searchedSource(document, '/api/knowledge/open/document/404', {...document,
      status: 'indexed', text: 'exact occurrence, not an attachment/blob ID'});
  } else {
    // Unversioned secretary message links, runtime entries, and live optional
    // sources must retain the existing routing when no searched revision exists.
    for (const source_type of ['message', 'entry', 'note', 'practice']) {
      const hit = {source_type, source_id: 17, citation: `${source_type}:17@live`};
      await searchedSource(hit, `/api/knowledge/open/${source_type}/17`, {...hit,
        status: 'indexed', text: `${source_type} source`});
    }
  }
}
async function staleLoad() {
  let old;
  if (options.stage === 'execution') old = await beginLoad('OLD_PRIVATE');
  else {
    old = {loading: run('loadSecretary()'), request: take('/api/secretary')};
  }
  await logout();
  let currentLoad, currentRequest;
  if (options.session !== 'logged_out') {
    await login();
    currentLoad = run('loadSecretary()');
    currentRequest = take('/api/secretary');
    if (options.session === 'new_rendered') {
      respond(currentRequest, snapshot('CURRENT_PRIVATE'));
      await checkpoint();
      respond(take('/api/secretary/execution'), execution('CURRENT_PRIVATE'));
      await currentLoad;
      assertCurrent();
    }
  }
  // A stale continuation must not change ANY current DOM/control state,
  // including catches/finally and the refresh button of a newer pending load.
  const before = domState();
  finish(old.request, options.outcome, options.stage === 'execution'
    ? execution('OLD_PRIVATE') : snapshot('OLD_PRIVATE'));
  await old.loading;
  await checkpoint();
  assert.deepEqual(domState(), before, 'Stale secretary continuation changed the DOM');
  if (options.session === 'new_pending') {
    respond(currentRequest, snapshot('CURRENT_PRIVATE'));
    await checkpoint();
    respond(take('/api/secretary/execution'), execution('CURRENT_PRIVATE'));
    await currentLoad;
    assertCurrent();
  }
}
async function currentLoad() {
  const {loading, request} = await beginLoad('CURRENT_PRIVATE');
  finish(request, options.outcome, execution('CURRENT_PRIVATE'));
  await loading;
  if (options.outcome === 'unauthorized') {
    assert.equal(element('dashboard').hidden, true);
    for (const id of privateIds) assert.equal(element(id).textContent, '', id);
    assert.equal(element('email-prepare').disabled, true);
    assert.ok(!element('secretary-status').textContent.includes('Snapshot manuale'));
  } else if (options.outcome === 'success') assertCurrent();
  else {
    for (const id of privateIds.slice(0, 6)) {
      assert.ok(element(id).textContent.includes('CURRENT_PRIVATE'), id);
    }
    assert.ok(element('email-executions').textContent.includes('Schema non disponibile'));
    assert.equal(element('email-prepare').disabled, true);
    assert.equal(element('secretary-refresh').disabled, false);
    assert.ok(element('secretary-status').textContent.includes('Snapshot manuale'));
  }
}

(async () => {
  vm.runInContext(script, context, {filename: 'web/app.js'});
  // Finish the real bootstrap request as an anonymous, offline session.
  respond(take('/api/session'), {error: 'Not authenticated'}, 401);
  await checkpoint();
  run("csrf = 'synthetic-session'; show(true);");
  if (scenario === 'sources') await sources();
  else if (scenario === 'stale') await staleLoad();
  else {assert.equal(scenario, 'current'); await currentLoad();}
  assert.equal(requests.length, 0, 'Unexpected API request');
  console.log('PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
'''


class WebSessionSafetyTests(unittest.TestCase):
    def run_scenario(self, scenario, **options):
        result = subprocess.run(
            ['node', '--unhandled-rejections=strict', '-e', NODE_HARNESS],
            input=json.dumps(dict(script=(ROOT / 'web/app.js').read_text(),
                                 scenario=scenario, options=options)),
            cwd=ROOT, env=node_env(), text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_searched_message_opens_immutable_revision_and_its_documents(self):
        self.run_scenario('sources', kind='message')

    def test_searched_document_keeps_exact_occurrence_route(self):
        self.run_scenario('sources', kind='document')

    def test_unversioned_and_optional_sources_keep_existing_routes(self):
        self.run_scenario('sources', kind='fallback')

    def test_current_session_renders_with_optional_execution_fallback(self):
        for outcome in ('success', 'unavailable'):
            with self.subTest(outcome=outcome):
                self.run_scenario('current', outcome=outcome)

    def test_execution_unauthorized_cannot_restore_secretary_snapshot(self):
        self.run_scenario('current', outcome='unauthorized')

    def test_logout_during_execution_cannot_restore_private_dom(self):
        for outcome in ('success', 'unavailable', 'network', 'json', 'unauthorized'):
            with self.subTest(outcome=outcome):
                self.run_scenario('stale', stage='execution', session='logged_out', outcome=outcome)

    def test_relogin_fences_execution_render_errors_and_cleanup(self):
        for session in ('new_pending', 'new_rendered'):
            for outcome in ('success', 'unavailable', 'network', 'json', 'unauthorized'):
                with self.subTest(session=session, outcome=outcome):
                    self.run_scenario('stale', stage='execution', session=session, outcome=outcome)

    def test_snapshot_await_errors_and_cleanup_are_also_session_fenced(self):
        for session in ('logged_out', 'new_pending', 'new_rendered'):
            for outcome in ('success', 'unavailable'):
                with self.subTest(session=session, outcome=outcome):
                    self.run_scenario('stale', stage='snapshot', session=session, outcome=outcome)


if __name__ == '__main__':
    unittest.main()