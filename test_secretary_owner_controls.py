"""Owner controls: offline HTTP/service/Node tests and explicit opt-in SQL.

SQL requires PI_SECRETARY_TEST_DATABASE_URL naming a *_test database, with no
service indirection. Only disposable schemas; no cluster startup or providers.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import dashboard
import jobs
import secretary
import secretary_runtime as runtime
import test_secretary_dashboard as dashboard_fixtures
import test_secretary_runtime as runtime_fixtures
import test_web_session_safety as web_fixtures


HASH = 'a' * 64
LINK_PATH = '/api/secretary/runtime/links/9/accept'
ACK_PATH = '/api/secretary/runtime/practices/3/acknowledge'


class OwnerControlRoutesTests(unittest.TestCase):
    # Reuse only helpers, not TestCase inheritance/discovery of unrelated tests.
    setUp = dashboard_fixtures.SecretaryDashboardTests.setUp
    post = dashboard_fixtures.SecretaryDashboardTests.post

    def test_mutations_require_session_csrf_same_origin_and_host(self):
        anonymous = dashboard.create_app({'username': 'owner', 'password': 'offline'}).test_client()
        for path in (LINK_PATH, ACK_PATH):
            with self.subTest(path=path):
                self.assertEqual(anonymous.post(path, json={}).status_code, 401)
                self.assertEqual(self.client.post(path, json={}).status_code, 403)
                for extra, status in (({'Origin': 'https://evil.invalid'}, 403),
                                      ({'Sec-Fetch-Site': 'cross-site'}, 403),
                                      ({'Host': 'evil.invalid'}, 400)):
                    self.assertEqual(self.client.post(path, json={},
                        headers={**self.headers, **extra}).status_code, status)
        self.conn.execute.assert_not_called()

    def test_accept_forwards_only_proposal_version_and_server_owner(self):
        with patch.object(secretary, 'accept_link', return_value={'proposal_id': 9}) as accept:
            response = self.post(LINK_PATH, {'expected_version': 4})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['external_execution'])
        self.assertEqual(response.json['link'], {'proposal_id': 9})
        args = accept.call_args.kwargs
        self.assertEqual(accept.call_args.args, (self.conn,))
        self.assertEqual(args['proposal_id'], 9)
        self.assertEqual(args['expected_version'], 4)
        secretary._caller(args['caller'])
        self.assertIs(type(args['caller']), secretary.DashboardOwner)
        self.conn.transaction.assert_called()

    def test_ack_forwards_exact_practice_version_hash_and_server_owner(self):
        result = dict(receipt='Presa in carico', acknowledged=True)
        with patch.object(runtime, 'acknowledge_dashboard', return_value=result) as ack:
            response = self.post(ACK_PATH, dict(expected_version=7, attention_hash=HASH))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['acknowledgement'], result)
        self.assertFalse(response.json['external_execution'])
        self.assertEqual(ack.call_args.args, (self.conn, 3))
        args = ack.call_args.kwargs
        self.assertEqual(args['expected_version'], 7)
        self.assertEqual(args['attention_hash'], HASH)
        secretary._caller(args['caller'])

    def test_exact_schemas_reject_bulk_metadata_permissions_and_fake_authority(self):
        for path, data, module, method in (
                (LINK_PATH, dict(expected_version=1), secretary, 'accept_link'),
                (ACK_PATH, dict(expected_version=1, attention_hash=HASH), runtime, 'acknowledge_dashboard')):
            with patch.object(module, method) as service:
                invalid = [None, [], {}, [data], dict(data, expected_version=True),
                           dict(data, expected_version=0), dict(data, expected_version=2**63)]
                for field in ('caller', 'owner_id', 'authenticated', 'csrf_verified', 'metadata',
                              'permissions', 'evidence', 'proposal_ids', 'authority_message_id', 'operation_key'):
                    invalid.append(dict(data, **{field: 'untrusted'}))
                for value in invalid:
                    with self.subTest(path=path, value=value):
                        self.assertEqual(self.post(path, value).status_code, 400)
                self.assertEqual(self.post(path + '?caller=owner', data).status_code, 400)
                self.assertEqual(self.post(path.replace('/9/', '/0/').replace('/3/', '/0/'), data).status_code, 400)
                service.assert_not_called()

    def test_duplicate_keys_nonfinite_malformed_and_non_json_are_rejected(self):
        for path, body, module, method in (
                (LINK_PATH, '{"expected_version":1,"expected_version":2}', secretary, 'accept_link'),
                (ACK_PATH, '{"expected_version":1,"attention_hash":"a","attention_hash":"b"}', runtime, 'acknowledge_dashboard')):
            with patch.object(module, method) as service:
                for raw in (body, '{"expected_version":NaN}', '{"expected_version":Infinity}', '{'):
                    self.assertEqual(self.client.post(path, data=raw, content_type='application/json',
                        headers=self.headers).status_code, 400)
                self.assertEqual(self.client.post(path, data='{}', content_type='text/plain',
                    headers=self.headers).status_code, 400)
                self.assertEqual(self.client.post(path, data='x' * 5000, content_type='application/json',
                    headers=self.headers).status_code, 413)
                service.assert_not_called()

    def test_conflict_auth_failure_and_database_errors_are_sanitized(self):
        for path, data, module, method in (
                (LINK_PATH, dict(expected_version=1), secretary, 'accept_link'),
                (ACK_PATH, dict(expected_version=1, attention_hash=HASH), runtime, 'acknowledge_dashboard')):
            for error, status in ((secretary.Conflict('PRIVATE'), 409),
                                  (secretary.Unauthorized('PRIVATE'), 403),
                                  (RuntimeError('PRIVATE'), 503)):
                with patch.object(module, method, side_effect=error):
                    response = self.post(path, data)
                self.assertEqual(response.status_code, status)
                self.assertNotIn('PRIVATE', response.get_data(as_text=True))
            with patch.object(dashboard.config, 'TELEGRAM_CHAT_ID', ''):
                self.assertEqual(self.post(path, data).status_code, 403)

    def test_runtime_projection_exposes_exact_tokens_and_literal_evidence_only(self):
        proposal = dict(action='draft', evidence='<script>not authority</script>',
                        summary='Summary', draft='Literal draft', citations=['message:8@revision'], prompt='PRIVATE')
        link = dict(id=9, version=4, practice_id=3, practice_version=7, responsibility_id=2,
                    responsibility_version=6, message_id=8, entry_id=11, revision=HASH,
                    evidence='Literal evidence', state='proposed', metadata='PRIVATE')
        attention = dict(practice_id=3, practice_version=7, last_hash=HASH,
                         acknowledged_at=None, proposal=proposal, input='PRIVATE')
        with patch.object(runtime, 'snapshot', return_value=dict(link_proposals=[link] * 25,
                reviews=[dict(plan=proposal, input='PRIVATE')], attention=[attention] * 25)):
            response = self.client.get('/api/secretary/runtime')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        self.assertEqual(len(response.json['attention']), 20)
        self.assertEqual(len(response.json['link_proposals']), 20)
        for key in ('version', 'practice_version', 'responsibility_version', 'revision', 'evidence'):
            self.assertEqual(response.json['link_proposals'][0][key], link[key])
        self.assertEqual(response.json['attention'][0]['last_hash'], HASH)
        self.assertEqual(response.json['attention'][0]['proposal']['evidence'], proposal['evidence'])


class DashboardAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.owner = secretary.DashboardOwner('101', True, True)
        self.enterContext(patch.object(secretary.config, 'TELEGRAM_CHAT_ID', '101'))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('No network')))
        self.conn.execute.return_value.fetchone.side_effect = [None, ({'id': 3, 'version': 7},), (HASH, None)]

    def ack(self, **changes):
        args = dict(expected_version=7, attention_hash=HASH, caller=self.owner)
        args.update(changes)
        return runtime.acknowledge_dashboard(self.conn, 3, **args)

    def test_strict_authority_rejected_before_database(self):
        for owner in (None, dict(owner_id='101', authenticated=True, csrf_verified=True),
                      secretary.DashboardOwner('102', True, True), secretary.DashboardOwner('101', 1, True),
                      secretary.DashboardOwner('101', True, False), secretary.DashboardOwner('101', False, True)):
            with self.subTest(owner=owner), self.assertRaises(secretary.Unauthorized):
                self.ack(caller=owner)
        self.conn.execute.assert_not_called()

    def test_bad_versions_and_hashes_rejected_before_database(self):
        for version in (True, 0, -1, 2**63, '7'):
            with self.assertRaises(secretary.InvalidPlan):
                self.ack(expected_version=version)
        for digest in (None, True, '', 'A' * 64, HASH + '\n', 'g' * 64, []):
            with self.assertRaises(secretary.InvalidPlan):
                self.ack(attention_hash=digest)
        self.conn.execute.assert_not_called()

    def test_ack_atomic_receipt_audit_and_no_permission_expansion(self):
        result = self.ack()
        calls = self.conn.execute.call_args_list
        self.assertIn('pg_advisory_xact_lock', calls[0].args[0])
        self.assertIn('FOR UPDATE', calls[2].args[0])
        self.assertIn('FOR UPDATE', calls[3].args[0])
        self.assertIn('COALESCE(acknowledged_at,now())', calls[4].args[0])
        self.assertIn('INSERT INTO secretary_receipts', calls[5].args[0])
        self.assertIn('INSERT INTO secretary_decisions', calls[6].args[0])
        self.assertEqual(calls[5].args[1][0], result['operation_key'])
        request = json.loads(calls[6].args[1][2])['request']
        self.assertEqual(request, dict(operation='acknowledge', practice_id=3, expected_version=7,
                                      attention_hash=HASH, owner_id='101'))
        self.assertTrue(result['acknowledged'])
        sql = '\n'.join(call.args[0] for call in calls)
        for forbidden in ('UPDATE secretary_practices', 'secretary_approvals', 'data_policies',
                          'INSERT INTO work_jobs', 'INSERT INTO telegram_messages'):
            self.assertNotIn(forbidden, sql)
        self.conn.transaction.assert_called_once()

    def test_matching_receipt_replay_does_not_ack_new_attention(self):
        request = dict(operation='acknowledge', practice_id=3, expected_version=7,
                       attention_hash=HASH, owner_id='101')
        result = dict(acknowledged=True, receipt='Original receipt')
        self.conn.execute.return_value.fetchone.side_effect = [(secretary.payload_hash(request), 'Original receipt', result)]
        self.assertEqual(self.ack(), result)
        self.assertEqual(self.conn.execute.call_count, 2)

    def test_changed_receipt_request_rejected(self):
        self.conn.execute.return_value.fetchone.side_effect = [('wrong-hash', 'Old', {})]
        with self.assertRaises(secretary.Conflict):
            self.ack()
        self.assertEqual(self.conn.execute.call_count, 2)

    def test_stale_practice_attention_or_missing_attention_never_write(self):
        for practice, attention in (({'version': 8}, (HASH, None)),
                                     ({'version': 7}, ('b' * 64, None)), ({'version': 7}, None)):
            self.conn.reset_mock()
            self.conn.execute.return_value.fetchone.side_effect = [None, (practice,), attention]
            with self.subTest(practice=practice, attention=attention), self.assertRaises(secretary.Conflict):
                self.ack()
            self.assertTrue(all(not c.args[0].lstrip().startswith(('INSERT', 'UPDATE'))
                                for c in self.conn.execute.call_args_list))

    def test_lost_fence_rolls_back_ack_receipt_and_audit(self):
        with patch.object(jobs, 'assert_current', side_effect=[None, jobs.LeaseLost()]):
            with self.assertRaises(jobs.LeaseLost):
                self.ack()
        self.assertIs(self.conn.transaction.return_value.__exit__.call_args.args[0], jobs.LeaseLost)


class NotificationReceiptBindingTests(unittest.TestCase):
    def test_only_current_review_operation_receipts_suppress_replacement_in_quiet_hours(self):
        clock = datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc)
        plan = runtime_fixtures.plan(clock, 'draft')
        digest = secretary.payload_hash({key: plan[key] for key in ('action', 'question', 'draft', 'next_action')})
        state = dict(practice=dict(id=3, origin_message=7), responsibility=dict(id=2, version=1))
        notification = dict(type='notify', secretary_review=True, practice_id=3, message_id=7,
                            attention_hash=digest, review_operation_key='old:review')
        legacy_key = 'notification:' + jobs.event_id(json.dumps(notification, sort_keys=True))
        for key, status, suppress in ((None, None, False), ('owner-reply:7:0', 'accepted', False),
                ('notification-job:99', 'accepted', False), (legacy_key, 'blocked', False),
                ('notification-job:12', 'authorized', False), (legacy_key, 'accepted', True),
                ('notification-job:12', 'attempted', True), ('notification-job:12', 'uncertain', True),
                ('notification-job:12', 'accepted', True), ('notification-job:12', 'verified', True)):
            conn = MagicMock()
            def execute(sql, args=None):
                cursor = MagicMock()
                if 'FROM secretary_runtime_attention' in sql:
                    cursor.fetchone.return_value = (digest, clock - timedelta(minutes=5), None)
                elif 'FROM work_jobs' in sql:
                    self.assertIn('ORDER BY id DESC LIMIT 1', sql)
                    self.assertIn("key=(payload->>'review_operation_key')||':notify'", sql)
                    self.assertEqual(json.loads(args[0]), dict(type='notify', secretary_review=True,
                        practice_id=3, message_id=7, attention_hash=digest))
                    cursor.fetchone.return_value = (12, notification)
                elif 'FROM effect_receipts' in sql:
                    self.assertEqual(args, (7, 'notification-job:12', legacy_key))
                    cursor.fetchone.return_value = ((1,) if key in args[1:] and status in
                        ('attempted', 'uncertain', 'accepted', 'verified') else None)
                return cursor
            conn.execute.side_effect = execute
            with self.subTest(key=key, status=status), patch.object(runtime, '_now', return_value=clock), \
                 patch.object(runtime.config, 'TIMEZONE', 'UTC'), patch.dict(os.environ, {}, clear=True), \
                 patch.object(jobs, 'enqueue', return_value=(13,)) as enqueue:
                runtime._notify_owner(conn, state, 4, 'new:review', plan)
            self.assertEqual(enqueue.call_count, 0 if suppress else 1)
            if not suppress:
                self.assertEqual(enqueue.call_args.args[2], 'new:review:notify')
                quiet = next(c for c in conn.execute.call_args_list if 'GREATEST' in c.args[0])
                self.assertEqual(quiet.args[1], (datetime(2026, 10, 17, 8, tzinfo=timezone.utc), 'new:review:notify'))


OWNER_WEB_HARNESS = web_fixtures.NODE_HARNESS.split('(async () => {', 1)[0] + r'''
const confirmations = [];
context.confirm = text => {confirmations.push(text); return true;};
function runtimeSnapshot(marker) {
    return {reviews: [], attention: [{practice_id: 3, practice_version: 7,
        last_hash: 'a'.repeat(64), acknowledged_at: options.acknowledged ? '2026-10-16' : null,
        proposal: {summary: marker, evidence: '<script>literal evidence</script>', draft: marker,
            citations: ['message:8@revision']}}], link_proposals: [{id: 9, version: 4,
        practice_id: 3, practice_version: 7, responsibility_id: 2, responsibility_version: 6,
        entry_id: 11, message_id: 8, revision: 'b'.repeat(64), evidence: '<script>literal link</script>',
        state: options.accepted ? 'accepted' : 'proposed'}]};
}
async function renderRuntime(marker) {
    const loading = run('loadRuntime()');
    respond(take('/api/secretary/runtime'), runtimeSnapshot(marker));
    await loading;
}
function control() {
    const identity = options.kind === 'ack' ? 'runtime-reviews' : 'runtime-links';
    const text = options.kind === 'ack' ? 'Presa in carico' : 'Accetta questo';
    return element(identity).querySelectorAll('button').find(button => button.textContent.startsWith(text));
}
async function completeMutation(request) {
    respond(request, options.kind === 'ack' ? {acknowledgement: {receipt: 'Exact ack receipt'}}
        : {link: {proposal_id: 9}});
    await checkpoint();
    respond(take('/api/secretary'), snapshot('CURRENT_PRIVATE'));
    await checkpoint();
    respond(take('/api/secretary/execution'), execution('CURRENT_PRIVATE'));
    await checkpoint();
    respond(take('/api/secretary/runtime'), runtimeSnapshot('CURRENT_PRIVATE'));
}
async function exactControl() {
    await renderRuntime('CURRENT_PRIVATE');
    assert.ok(element('runtime-links').textContent.includes('<script>literal link</script>'));
    assert.ok(element('runtime-links').textContent.includes('responsabilità #2 · v6'));
    assert.ok(element('runtime-links').textContent.includes('Collegamento #9 · v4'));
    assert.ok(element('runtime-reviews').textContent.includes('<script>literal evidence</script>'));
    const button = control(); assert.ok(button);
    const clicking = button.onclick();
    const path = options.kind === 'ack' ? '/api/secretary/runtime/practices/3/acknowledge'
        : '/api/secretary/runtime/links/9/accept';
    const request = take(path);
    assert.deepEqual(JSON.parse(request.init.body), options.kind === 'ack'
        ? {expected_version: 7, attention_hash: 'a'.repeat(64)} : {expected_version: 4});
    assert.equal(request.init.headers['X-CSRF-Token'], 'synthetic-session');
    assert.ok(confirmations[0].includes(options.kind === 'ack' ? 'v7' : 'v4'));
    assert.equal(button.disabled, true);
    if (options.outcome === 'success') await completeMutation(request);
    else respond(request, {error: 'Reload exact snapshot'}, 409);
    await clicking;
    assert.ok(element('runtime-status').textContent.includes(options.outcome === 'success'
        ? options.kind === 'ack' ? 'Exact ack receipt' : 'accettato' : 'Reload exact snapshot'));
}
async function staleRuntime() {
    const loading = run('loadRuntime()');
    const request = take('/api/secretary/runtime');
    await logout();
    if (options.session === 'new_session') {await login(); await renderRuntime('CURRENT_PRIVATE');}
    const before = domState();
    finish(request, options.outcome, runtimeSnapshot('OLD_PRIVATE'));
    await loading; await checkpoint();
    assert.deepEqual(domState(), before, 'Stale runtime load changed current DOM');
}
async function staleControl() {
    await renderRuntime('OLD_PRIVATE');
    const button = control(); assert.ok(button);
    let clicking, request;
    if (options.stage !== 'before_click') {
        clicking = button.onclick();
        request = take(options.kind === 'ack' ? '/api/secretary/runtime/practices/3/acknowledge'
            : '/api/secretary/runtime/links/9/accept');
        if (options.stage !== 'post') {
            respond(request, {link: {proposal_id: 9}, acknowledgement: {receipt: 'OLD_PRIVATE'}});
            await checkpoint(); request = take('/api/secretary');
            if (options.stage !== 'secretary') {
                respond(request, snapshot('OLD_PRIVATE')); await checkpoint();
                respond(take('/api/secretary/execution'), execution('OLD_PRIVATE')); await checkpoint();
                request = take('/api/secretary/runtime');
            }
        }
    }
    await logout();
    if (options.session === 'new_session') {await login(); await renderRuntime('CURRENT_PRIVATE');}
    const before = domState();
    if (options.stage === 'before_click') await button.onclick();
    else {
        const body = options.stage === 'post' ? {link: {}, acknowledgement: {receipt: 'OLD_PRIVATE'}}
            : options.stage === 'secretary' ? snapshot('OLD_PRIVATE') : runtimeSnapshot('OLD_PRIVATE');
        finish(request, options.outcome, body);
        await clicking;
    }
    await checkpoint();
    assert.deepEqual(domState(), before, 'Stale owner control changed current DOM');
}
(async () => {
    vm.runInContext(script, context, {filename: 'web/app.js'});
    respond(take('/api/session'), {error: 'Anonymous'}, 401); await checkpoint();
    run("csrf = 'synthetic-session'; show(true);");
    if (scenario === 'exact') await exactControl();
    else if (scenario === 'stale_runtime') await staleRuntime();
    else if (scenario === 'resolved') {
        await renderRuntime('CURRENT_PRIVATE');
        assert.equal(control(), undefined, 'Resolved proposal/attention remained actionable');
    } else await staleControl();
    assert.equal(requests.length, 0, 'Stale control issued another request');
    console.log('PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
'''


class OwnerControlsWebTests(unittest.TestCase):
    def run_scenario(self, scenario, **options):
        result = subprocess.run(['node', '--unhandled-rejections=strict', '-e', OWNER_WEB_HARNESS],
            input=json.dumps(dict(script=(Path(__file__).parent / 'web/app.js').read_text(),
                                  scenario=scenario, options=options)),
            cwd=Path(__file__).parent, env={'PATH': os.defpath}, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_literal_evidence_exact_versions_hashes_csrf_and_explicit_buttons(self):
        for kind in ('ack', 'link'):
            for outcome in ('success', 'conflict'):
                with self.subTest(kind=kind, outcome=outcome):
                    self.run_scenario('exact', kind=kind, outcome=outcome)

    def test_resolved_attention_and_link_have_no_mutation_button(self):
        self.run_scenario('resolved', kind='ack', acknowledged=True)
        self.run_scenario('resolved', kind='link', accepted=True)

    def test_runtime_snapshot_await_catch_are_fenced_on_logout_and_relogin(self):
        for session in ('logged_out', 'new_session'):
            for outcome in ('success', 'network', 'json', 'unauthorized', 'unavailable'):
                with self.subTest(session=session, outcome=outcome):
                    self.run_scenario('stale_runtime', session=session, outcome=outcome)

    def test_owner_actions_fenced_before_click_and_after_every_await(self):
        for kind in ('ack', 'link'):
            for session in ('logged_out', 'new_session'):
                for stage in ('before_click', 'post', 'secretary', 'runtime'):
                    for outcome in ('success', 'network', 'unauthorized'):
                        with self.subTest(kind=kind, session=session, stage=stage, outcome=outcome):
                            self.run_scenario('stale_control', kind=kind, session=session,
                                              stage=stage, outcome=outcome)


class OwnerControlsPostgresTests(unittest.TestCase):
    """Only explicitly supplied *_test database; no implicit local cluster."""
    setUpClass = classmethod(runtime_fixtures.RuntimePostgresTests.setUpClass.__func__)
    connection = runtime_fixtures.RuntimePostgresTests.connection
    drop_schema = runtime_fixtures.RuntimePostgresTests.drop_schema
    archive = runtime_fixtures.RuntimePostgresTests.archive
    delegate = runtime_fixtures.RuntimePostgresTests.delegate
    consent = runtime_fixtures.RuntimePostgresTests.consent
    wake = runtime_fixtures.RuntimePostgresTests.wake
    respond_with_plan = runtime_fixtures.RuntimePostgresTests.respond_with_plan
    notification_execution = runtime_fixtures.RuntimePostgresTests.notification_execution

    def setUp(self):
        runtime_fixtures.RuntimePostgresTests.setUp(self)
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('No network')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('No workers')))
        self.client = dashboard.create_app({'username': 'owner', 'password': 'offline'}).test_client()
        csrf = self.client.post('/api/login', json={'username': 'owner', 'password': 'offline'}).json['csrf']
        self.headers = {'X-CSRF-Token': csrf}
        self.delegate()

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def link(self):
        mid = self.archive('Il preventivo per il tetto è pronto.', channel='email')
        runtime.observe(self.conn, mid)
        runtime.review(dict(observation_message_id=mid, operation_key=f'secretary-observe:{mid}'))
        proposal = self.conn.execute('SELECT to_jsonb(t) FROM secretary_runtime_link_proposals t').fetchone()[0]
        return f"/api/secretary/runtime/links/{proposal['id']}/accept", dict(expected_version=proposal['version']), proposal

    def attention(self):
        self.consent()
        with self.respond_with_plan('draft'):
            runtime.review(self.wake())
        item = self.client.get('/api/secretary/runtime').json['attention'][0]
        path = f'/api/secretary/runtime/practices/{self.pid}/acknowledge'
        return path, dict(expected_version=item['practice_version'], attention_hash=item['last_hash'])

    def test_accept_exact_displayed_link_replay_receipt_and_no_new_permissions(self):
        path, data, proposal = self.link()
        view = self.client.get('/api/secretary/runtime').json['link_proposals'][0]
        for key in ('version', 'evidence', 'revision', 'practice_version', 'responsibility_version'):
            self.assertEqual(view[key], proposal[key])
        first = self.post(path, data)
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(self.post(path, data).json, first.json)
        self.assertEqual(self.conn.execute('SELECT state,version FROM secretary_runtime_link_proposals').fetchone(), ('accepted', 2))
        self.assertEqual(self.conn.execute('SELECT evidence FROM secretary_links WHERE email_message_id=%s',
            (proposal['message_id'],)).fetchone(), (proposal['evidence'],))
        self.assertEqual(self.conn.execute('SELECT version FROM secretary_practices').fetchone(), (2,))
        self.assertEqual(self.conn.execute('SELECT version,state FROM secretary_responsibilities').fetchone(), (1, 'active'))
        for table in ('data_policies', 'secretary_approvals', 'effect_receipts'):
            self.assertEqual(self.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM secretary_receipts WHERE operation_key LIKE 'secretary-link:%'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM secretary_decisions WHERE event='receipt'").fetchone()[0], 1)
        self.chat.assert_not_called()

    def test_link_stale_proposal_and_scope_versions_fail_without_receipt(self):
        path, data, proposal = self.link()
        self.assertEqual(self.post(path, dict(expected_version=proposal['version'] + 1)).status_code, 409)
        self.conn.execute('UPDATE secretary_responsibilities SET version=version+1')
        self.assertEqual(self.post(path, data).status_code, 409)
        self.conn.execute('UPDATE secretary_responsibilities SET version=version-1')
        self.conn.execute('UPDATE secretary_practices SET version=version+1')
        self.assertEqual(self.post(path, data).status_code, 409)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_receipts').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_links WHERE email_message_id IS NOT NULL').fetchone()[0], 0)

    def test_link_evidence_revision_and_native_authority_rechecked(self):
        path, data, proposal = self.link()
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata || '{\"is_forwarded\":true}'::jsonb WHERE id=%s", (self.origin,))
        self.assertEqual(self.post(path, data).status_code, 403)
        self.conn.execute("UPDATE telegram_messages SET metadata=metadata || '{\"is_forwarded\":false}'::jsonb WHERE id=%s", (self.origin,))
        self.conn.execute('UPDATE knowledge_entries SET active=false WHERE id=%s', (proposal['entry_id'],))
        self.assertEqual(self.post(path, data).status_code, 409)
        self.conn.execute('UPDATE knowledge_entries SET active=true WHERE id=%s', (proposal['entry_id'],))
        self.conn.execute("UPDATE email_messages SET body=body || ' changed' WHERE id=%s", (proposal['message_id'],))
        self.assertEqual(self.post(path, data).status_code, 409)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_receipts').fetchone()[0], 0)

    def test_ack_receipt_replay_audit_and_suppression_without_expanding_permissions(self):
        path, data = self.attention()
        before = self.conn.execute('SELECT to_jsonb(p) FROM secretary_practices p').fetchone()[0]
        first = self.post(path, data)
        self.assertEqual(first.status_code, 200, first.json)
        stamp = self.conn.execute('SELECT acknowledged_at FROM secretary_runtime_attention').fetchone()[0]
        self.assertIsNotNone(stamp)
        self.assertEqual(self.post(path, data).json, first.json)
        self.assertEqual(self.conn.execute('SELECT acknowledged_at FROM secretary_runtime_attention').fetchone()[0], stamp)
        self.assertEqual(self.conn.execute('SELECT to_jsonb(p) FROM secretary_practices p').fetchone()[0], before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_receipts').fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM secretary_decisions WHERE event='receipt'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT version,state FROM secretary_responsibilities').fetchone(), (1, 'active'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_approvals').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone()[0], 1)
        notification = self.conn.execute("SELECT payload FROM work_jobs WHERE kind='action'").fetchone()[0]
        with self.assertRaises(runtime.ReviewStopped):
            runtime.assert_notification_current(notification)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM effect_receipts').fetchone()[0], 0)

    def test_stale_ack_version_hash_and_missing_attention_fail_atomically(self):
        path, data = self.attention()
        for changed in (dict(data, expected_version=data['expected_version'] + 1),
                        dict(data, attention_hash='b' * 64)):
            self.assertEqual(self.post(path, changed).status_code, 409)
        self.conn.execute('DELETE FROM secretary_runtime_attention')
        self.assertEqual(self.post(path, data).status_code, 409)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_receipts').fetchone()[0], 0)

    def test_ack_replay_does_not_ack_later_content(self):
        path, data = self.attention()
        first = self.post(path, data)
        self.conn.execute("UPDATE secretary_runtime_attention SET last_hash=%s,acknowledged_at=NULL", ('b' * 64,))
        self.conn.execute('UPDATE secretary_practices SET version=version+1')
        self.assertEqual(self.post(path, data).json, first.json)
        self.assertEqual(self.conn.execute('SELECT last_hash,acknowledged_at FROM secretary_runtime_attention').fetchone(), ('b' * 64, None))

    def test_ack_audit_failure_rolls_back_update_and_receipt(self):
        path, data = self.attention()
        with patch.object(secretary, '_decision', side_effect=RuntimeError('audit failed')):
            self.assertEqual(self.post(path, data).status_code, 503)
        self.assertIsNone(self.conn.execute('SELECT acknowledged_at FROM secretary_runtime_attention').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM secretary_receipts').fetchone()[0], 0)

    def test_unsent_quiet_hour_attention_replaced_despite_unrelated_owner_receipt(self):
        path, data = self.attention()
        self.conn.execute("INSERT INTO effect_receipts(key,message_id,effect,payload_hash,status) VALUES (%s,%s,'notify',%s,'accepted')",
                          (f'owner-reply:{self.origin}:0', self.origin, HASH))
        wake = secretary.observe(self.conn, self.origin)[0]
        clock = datetime.now(timezone.utc).replace(hour=23, minute=30)
        with patch.dict(os.environ, {'SECRETARY_REVIEW_QUIET_START': '22', 'SECRETARY_REVIEW_QUIET_END': '8'}), \
             patch.object(runtime, '_now', return_value=clock), self.respond_with_plan('draft'):
            runtime.review(wake)
            quiet_until = runtime._quiet_until(clock)
        rows = self.conn.execute("SELECT id,payload,available_at FROM work_jobs WHERE kind='action' ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1][2], quiet_until)
        self.assertEqual(rows[-1][2].astimezone(timezone.utc).hour, 8)
        self.assertGreater(rows[-1][2], clock)
        runtime.assert_notification_current(rows[-1][1])
        with self.assertRaises(runtime.ReviewStopped):
            runtime.assert_notification_current(rows[0][1])
        self.assertEqual(self.post(path, data).status_code, 409)

    def test_current_review_attempt_receipt_suppresses_identical_new_notification(self):
        self.attention()
        identity = self.conn.execute("SELECT id FROM work_jobs WHERE kind='action'").fetchone()[0]
        self.conn.execute("INSERT INTO effect_receipts(key,message_id,effect,payload_hash,status) VALUES (%s,%s,'notify',%s,'attempted')",
                          (f'notification-job:{identity}', self.origin, HASH))
        with self.respond_with_plan('draft'):
            runtime.review(secretary.observe(self.conn, self.origin)[0])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM work_jobs WHERE kind='action'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()