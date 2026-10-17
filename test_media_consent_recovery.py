"""Explicit consent recovery: offline HTTP/UI and opt-in isolated PostgreSQL.

Only PI_SECRETARY_TEST_DATABASE_URL, naming an explicit *_test database, is
used. Each SQL case creates/drops its own schema; no cluster is started/stopped.
Providers, Python network access and background worker startup are forbidden.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import uuid

import config
import dashboard
import db
import jobs
import knowledge
import message_store
import policy
from connectors import media_archive, media_text


PATH = '/api/media/retry-blocked'
REQUEST = dict(message_id=7, expected_channel='telegram (account)')
ROOT = Path(__file__).resolve().parent


class MediaConsentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.connect = self.enterContext(patch.object(db, '_conn'))
        self.conn = self.connect.return_value.__enter__.return_value
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.retry = self.enterContext(patch.object(media_archive, 'retry_blocked',
            return_value=dict(message_id=7, queued=1, automatic_retry=False)))
        self.app = dashboard.create_app({'username': 'offline', 'password': 'synthetic'})
        self.client = self.app.test_client()
        self.headers = {'X-CSRF-Token': self.client.post('/api/login',
            json=dict(username='offline', password='synthetic')).json['csrf']}

    def test_authenticated_csrf_route_forwards_only_native_source(self):
        response = self.client.post(PATH, json=REQUEST, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['queued'], 1)
        self.assertFalse(response.json['automatic_retry'])
        self.retry.assert_called_once_with(self.conn, 7, expected_channel='telegram (account)')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_anonymous_csrf_origin_host_and_get_cannot_retry(self):
        self.assertEqual(self.app.test_client().post(PATH, json=REQUEST).status_code, 401)
        self.assertEqual(self.client.post(PATH, json=REQUEST).status_code, 403)
        for headers, status in (({'X-CSRF-Token': 'wrong'}, 403),
                ({**self.headers, 'Origin': 'https://evil.invalid'}, 403),
                ({**self.headers, 'Sec-Fetch-Site': 'cross-site'}, 403),
                ({**self.headers, 'Host': 'evil.invalid'}, 400)):
            self.assertEqual(self.client.post(PATH, json=REQUEST, headers=headers).status_code, status)
        self.assertEqual(self.client.get(PATH).status_code, 405)
        self.retry.assert_not_called()
        self.connect.assert_not_called()

    def test_strict_schema_types_query_duplicate_and_nonfinite_rejected(self):
        invalid = [None, [], {}, {**REQUEST, 'message_id': True}, {**REQUEST, 'message_id': 0},
            {**REQUEST, 'message_id': -1}, {**REQUEST, 'message_id': 2 ** 63},
            {**REQUEST, 'message_id': '7'}, {**REQUEST, 'expected_channel': []},
            {**REQUEST, 'expected_channel': 'email'}, {**REQUEST, 'expected_channel': '*'},
            {**REQUEST, 'expected_channel': "telegram'; DROP TABLE work_jobs;--"},
            {**REQUEST, 'attachment_id': 1}, {**REQUEST, 'status': 'failed'},
            {**REQUEST, 'sql': 'UPDATE work_jobs SET status=\'pending\''},
            {**REQUEST, 'historical': False}, {**REQUEST, 'allowed': True}]
        for data in invalid:
            with self.subTest(data=data):
                self.assertEqual(self.client.post(PATH, json=data, headers=self.headers).status_code, 400)
        for raw in ('{"message_id":7,"message_id":8,"expected_channel":"telegram"}',
                    '{"message_id":NaN,"expected_channel":"telegram"}',
                    '{"message_id":Infinity,"expected_channel":"telegram"}', '{'):
            self.assertEqual(self.client.post(PATH, data=raw, content_type='application/json',
                headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(PATH, data=json.dumps(REQUEST),
            content_type='text/plain', headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(PATH + '?all=true', json=REQUEST,
            headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(PATH, data='x' * 4097, content_type='application/json',
            headers=self.headers).status_code, 413)
        self.retry.assert_not_called()
        self.connect.assert_not_called()

    def test_conflicts_and_failures_are_sanitized(self):
        for error, status in ((media_archive.Conflict('PRIVATE_PATH'), 409),
                              (RuntimeError('PRIVATE_PROVIDER_URL'), 503)):
            self.retry.side_effect = error
            response = self.client.post(PATH, json=REQUEST, headers=self.headers)
            self.assertEqual(response.status_code, status)
            self.assertNotIn('PRIVATE', response.get_data(as_text=True))

    def test_service_validates_before_sql(self):
        # Exercise the actual function, not the route's mock.
        original = self.retry_function
        for mid, channel in ((True, 'telegram'), ('7', 'telegram'), (0, 'telegram'),
                             (2 ** 63, 'telegram'), (7, []), (7, 'email'), (7, '*')):
            with self.subTest(mid=mid, channel=channel), self.assertRaises(ValueError):
                original(self.conn, mid, expected_channel=channel)
        self.conn.execute.assert_not_called()

    retry_function = staticmethod(media_archive.retry_blocked)


class MediaConsentPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
        if not cls.url or db.psycopg is None:
            raise unittest.SkipTest('Explicit isolated PI_SECRETARY_TEST_DATABASE_URL required')
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(cls.url)
        if not params.get('dbname', '').endswith('_test') or params.get('service'):
            raise ValueError('Explicit _test database without service required')

    def setUp(self):
        from psycopg import sql
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        self.enterContext(patch.object(db, '_conn', side_effect=AssertionError('Configured DB forbidden')))
        self.conn = db.psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.assertTrue(self.scalar('SELECT current_database()').endswith('_test'))
        self.schema = 'media_consent_' + uuid.uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='5s'")
        self.conn.execute("SET lock_timeout='2s'")
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)
        media_archive.migrate(self.conn)
        policy.migrate(self.conn)
        knowledge.migrate(self.conn)
        import secretary
        import secretary_runtime
        secretary.migrate(self.conn)
        secretary_runtime.migrate(self.conn)
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch.object(config, 'OPENROUTER_API_KEY', 'offline-test'))
        self.convert = self.enterContext(patch.object(media_text, 'convert', return_value='Voce smeraldo recuperata'))
        self.describe = self.enterContext(patch.object(media_text, 'describe_image', return_value='Immagine smeraldo recuperata'))
        self.provider = self.enterContext(patch.object(media_text.ai_budget, 'request_content',
            side_effect=AssertionError('Real provider forbidden')))
        self.client = dashboard.create_app({'username': 'offline', 'password': 'synthetic'}).test_client()
        self.headers = {'X-CSRF-Token': self.client.post('/api/login',
            json=dict(username='offline', password='synthetic')).json['csrf']}

    @contextmanager
    def connection(self):
        yield self.conn

    def drop_schema(self):
        from psycopg import sql
        self.conn.rollback()
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def scalar(self, statement, args=None):
        return self.conn.execute(statement, args).fetchone()[0]

    def archive(self, channel='telegram (account)', *, historical=False, count=1, mime='audio/ogg'):
        mid = message_store.store(self.conn, channel, 'Native sender', 'Media consent', 'Caption originale',
            source_id=uuid.uuid4().hex, ts=datetime.now(timezone.utc).isoformat(),
            metadata=dict(account='account:7', direction='in', chat_id='8', author='8'),
            historical=historical, attachments=[dict(filename=f'media-{i}', content_type=mime,
                data=b'offline media') for i in range(count)])
        ids = [r[0] for r in self.conn.execute('SELECT id FROM attachments WHERE message_id=%s ORDER BY id', (mid,))]
        return mid, ids

    def run_media(self):
        job = jobs.claim('media')
        self.assertIsNotNone(job)
        context = jobs.ExecutionContext(job[0], job[3], 'media', job[1], float('inf'))
        with patch.object(jobs._local, 'execution', context, create=True):
            media_archive.process(job[1])
            # Worker finalization cannot overwrite a terminal consent block.
            jobs.finish(job)
        return job

    def state(self, aid):
        return self.conn.execute('''SELECT status,error,attempts,payload,token,lease_until,finished_at
            FROM work_jobs WHERE key=%s''', (f'media:{aid}',)).fetchone()

    def grant(self, channel='telegram', effects=('ai_text', 'ai_media'), allowed=True):
        for effect in effects:
            version = self.scalar('SELECT coalesce((SELECT version FROM data_policies WHERE channel=%s AND effect=%s),0)',
                                  (channel, effect))
            response = self.client.post('/api/data-policies', headers=self.headers,
                json=dict(channel=channel, effect=effect, allowed=allowed, expected_version=version))
            self.assertEqual(response.status_code, 200)

    def retry(self, mid, channel='telegram (account)'):
        return self.client.post(PATH, json=dict(message_id=mid, expected_channel=channel), headers=self.headers)

    def test_receive_block_grant_explicit_retry_transcript_and_index(self):
        mid, (aid,) = self.archive()
        self.run_media()
        blocked = self.state(aid)
        self.assertEqual(blocked[:3], ('blocked', 'media_consent_required', 1))
        self.convert.assert_not_called()
        knowledge.index_message(dict(message_id=mid))
        self.assertEqual(knowledge.search('smeraldo'), [])
        before = self.conn.execute('SELECT * FROM work_jobs ORDER BY id').fetchall()
        self.grant()
        self.assertEqual(self.conn.execute('SELECT * FROM work_jobs ORDER BY id').fetchall(), before)
        media_archive.enqueue_message(self.conn, mid)
        self.assertEqual(self.state(aid), blocked)
        response = self.retry(mid)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['queued'], 1)
        self.assertFalse(response.json['automatic_retry'])
        pending = self.state(aid)
        self.assertEqual(pending[:3], ('pending', None, 0))
        self.assertEqual(pending[3], dict(attachment_id=aid, media_consent_retries=1))
        self.assertEqual(pending[4:], (None, None, None))
        self.assertEqual(self.retry(mid).json['queued'], 0)
        self.convert.assert_not_called()
        self.run_media()
        self.assertEqual(self.state(aid)[0], 'done')
        self.assertTrue(self.state(aid)[3]['media_provider_attempted'])
        self.assertEqual(self.scalar('SELECT text FROM media_transcripts WHERE attachment_id=%s', (aid,)),
                         'Voce smeraldo recuperata')
        self.assertEqual(self.scalar('SELECT body FROM telegram_messages WHERE id=%s', (mid,)), 'Caption originale')
        knowledge.index_message(dict(message_id=mid))
        self.assertTrue(knowledge.search('smeraldo'))
        self.assertEqual(self.scalar("SELECT count(*) FROM effect_audit WHERE message_id=%s AND reason='media_consent_retry'", (mid,)), 1)
        audits = self.conn.execute('SELECT effect,reason FROM effect_audit').fetchall()
        self.assertNotIn('Caption', str(audits))
        self.assertNotIn('smeraldo', str(audits))
        self.assertIsNone(jobs.claim('media'))
        self.convert.assert_called_once()
        self.provider.assert_not_called()

    def test_whatsapp_image_uses_its_own_both_grants(self):
        mid, (aid,) = self.archive('whatsapp', mime='image/png')
        self.run_media()
        self.grant('telegram')
        self.assertEqual(self.retry(mid, 'whatsapp').status_code, 409)
        for effect, other in (('ai_media', 'ai_text'), ('ai_text', 'ai_media')):
            self.grant('whatsapp', effects=(effect,))
            self.grant('whatsapp', effects=(other,), allowed=False)
            self.assertEqual(self.retry(mid, 'whatsapp').status_code, 409)
            self.assertEqual(self.state(aid)[0], 'blocked')
        self.grant('whatsapp')
        self.assertEqual(self.retry(mid, 'whatsapp').json['queued'], 1)
        self.run_media()
        self.describe.assert_called_once()
        self.convert.assert_not_called()

    def test_history_stays_denied_after_both_grants_and_replay(self):
        mid, (aid,) = self.archive(historical=True)
        self.run_media()
        before = self.state(aid)
        self.grant()
        media_archive.enqueue_message(self.conn, mid)
        self.assertEqual(self.retry(mid).status_code, 409)
        self.assertEqual(self.state(aid), before)
        self.assertEqual(self.scalar('SELECT count(*) FROM media_transcripts'), 0)
        self.convert.assert_not_called()

    def test_current_native_channel_and_provenance_override_request(self):
        mid, (aid,) = self.archive()
        self.run_media()
        self.grant()
        for channel in ('telegram', 'whatsapp'):
            self.assertEqual(self.retry(mid, channel).status_code, 409)
        self.conn.execute('''UPDATE telegram_messages SET metadata=metadata || '{"historical":true}'::jsonb WHERE id=%s''', (mid,))
        self.assertEqual(self.retry(mid).status_code, 409)
        self.assertEqual(self.retry(2 ** 62).status_code, 409)
        self.assertEqual(self.state(aid)[0], 'blocked')
        self.convert.assert_not_called()

    def test_only_exact_blocked_media_jobs_and_source_are_changed(self):
        mid, ids = self.archive(count=11)
        for _ in ids:
            self.run_media()
        self.grant()
        for aid, status in zip(ids, ('failed', 'uncertain', 'attempted', 'pending', 'running', 'done')):
            self.conn.execute('UPDATE work_jobs SET status=%s WHERE key=%s', (status, f'media:{aid}'))
        self.conn.execute("UPDATE work_jobs SET error='another_reason' WHERE key=%s", (f'media:{ids[6]}',))
        self.conn.execute("UPDATE work_jobs SET kind='action' WHERE key=%s", (f'media:{ids[7]}',))
        self.conn.execute("UPDATE work_jobs SET token='stale-token' WHERE key=%s", (f'media:{ids[8]}',))
        self.conn.execute("UPDATE work_jobs SET lease_until=now() WHERE key=%s", (f'media:{ids[9]}',))
        _, (other_aid,) = self.archive()
        self.conn.execute("UPDATE work_jobs SET status='blocked',error='media_consent_required' WHERE key=%s",
                  (f'media:{other_aid}',))
        untouched = {aid: self.state(aid) for aid in ids[:-1] + [other_aid]}
        self.assertEqual(self.retry(mid).json['queued'], 1)
        for aid, row in untouched.items():
            self.assertEqual(self.state(aid), row)
        self.assertEqual(self.state(ids[-1])[0], 'pending')

    def test_attachment_membership_channel_kind_bytes_transcript_and_payload_are_checked(self):
        mid, ids = self.archive(count=7)
        for _ in ids:
            self.run_media()
        self.grant()
        self.conn.execute("UPDATE attachments SET channel='whatsapp' WHERE id=%s", (ids[0],))
        self.conn.execute("UPDATE attachments SET content_type='image/svg+xml' WHERE id=%s", (ids[1],))
        self.conn.execute('UPDATE attachments SET data=NULL,path=NULL WHERE id=%s', (ids[2],))
        self.conn.execute("INSERT INTO media_transcripts(attachment_id,kind,text,model) VALUES (%s,'audio','existing','offline')", (ids[3],))
        self.conn.execute('UPDATE work_jobs SET payload=%s WHERE key=%s',
            (json.dumps(dict(attachment_id=ids[5])), f'media:{ids[4]}'))
        self.conn.execute('UPDATE attachments SET message_id=%s WHERE id=%s', (mid + 100000, ids[5]))
        before = {aid: self.state(aid) for aid in ids[:-1]}
        self.assertEqual(self.retry(mid).json['queued'], 1)
        for aid, row in before.items():
            self.assertEqual(self.state(aid), row)
        self.assertEqual(self.state(ids[-1])[0], 'pending')

    def test_recorded_provider_attempt_cannot_recover_a_later_consent_block(self):
        mid, (aid,) = self.archive()
        self.grant()
        def revoke_after_call(attachment):
            self.grant(allowed=False)
            return 'Never published'
        self.convert.side_effect = revoke_after_call
        self.run_media()
        self.assertTrue(self.state(aid)[3]['media_provider_attempted'])
        self.assertEqual(self.state(aid)[0], 'blocked')
        self.grant()
        before = self.state(aid)
        self.assertEqual(self.retry(mid).json['queued'], 0)
        self.assertEqual(self.state(aid), before)
        self.assertEqual(self.scalar('SELECT count(*) FROM media_transcripts'), 0)
        self.convert.assert_called_once()

    def test_provider_failure_then_consent_block_does_not_erase_attempt(self):
        mid, (aid,) = self.archive()
        self.grant()
        job = jobs.claim('media')
        context = jobs.ExecutionContext(job[0], job[3], 'media', job[1], float('inf'))
        with patch.object(jobs._local, 'execution', context, create=True), \
                patch.object(media_text, 'convert', side_effect=RuntimeError('ambiguous provider')):
            with self.assertRaises(RuntimeError):
                media_archive.process(job[1])
            jobs.finish(job, RuntimeError('ambiguous provider'))
        self.grant(allowed=False)
        self.conn.execute('UPDATE work_jobs SET available_at=now() WHERE id=%s', (job[0],))
        self.run_media()
        self.grant()
        self.assertEqual(self.retry(mid).json['queued'], 0)
        self.assertEqual(self.state(aid)[0], 'blocked')

    def test_lifetime_reset_bound_and_malformed_retry_counts(self):
        mid, (aid,) = self.archive()
        self.run_media()
        for count in range(media_archive.MAX_CONSENT_RETRIES):
            self.grant()
            self.assertEqual(self.retry(mid).json['queued'], 1)
            self.assertEqual(self.state(aid)[3]['media_consent_retries'], count + 1)
            self.grant(allowed=False)
            self.run_media()  # No provider attempt: consent was revoked before claim.
        self.grant()
        self.assertEqual(self.retry(mid).json['queued'], 0)
        for count in (True, -1, '0', None, 100):
            self.conn.execute('UPDATE work_jobs SET payload=%s WHERE key=%s',
                (json.dumps(dict(attachment_id=aid, media_consent_retries=count)), f'media:{aid}'))
            self.assertEqual(self.retry(mid).json['queued'], 0)
        self.convert.assert_not_called()

    def test_batch_bound_fails_without_partial_resets(self):
        mid, ids = self.archive(count=media_archive.MAX_RETRY_ATTACHMENTS + 1)
        self.conn.execute("UPDATE work_jobs SET status='blocked',error='media_consent_required',finished_at=now() WHERE kind='media'")
        self.grant()
        before = [self.state(aid) for aid in ids]
        self.assertEqual(self.retry(mid).status_code, 409)
        self.assertEqual([self.state(aid) for aid in ids], before)
        self.assertEqual(self.scalar("SELECT count(*) FROM effect_audit WHERE reason='media_consent_retry'"), 0)

    def test_recovery_and_audit_rollback_on_caller_abort_or_lost_fence(self):
        mid, (aid,) = self.archive()
        self.run_media()
        self.grant()
        before = self.state(aid)
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            with self.conn.transaction():
                self.assertEqual(media_archive.retry_blocked(self.conn, mid, expected_channel='telegram (account)')['queued'], 1)
                raise RuntimeError('abort')
        self.assertEqual(self.state(aid), before)
        with patch.object(jobs, 'assert_current', side_effect=[True, jobs.LeaseLost()]):
            with self.assertRaises(jobs.LeaseLost):
                media_archive.retry_blocked(self.conn, mid, expected_channel='telegram (account)')
        self.assertEqual(self.state(aid), before)
        self.assertEqual(self.scalar("SELECT count(*) FROM effect_audit WHERE reason='media_consent_retry'"), 0)

    def test_concurrent_retry_and_stale_worker_only_one_reset(self):
        from psycopg import sql
        mid, (aid,) = self.archive()
        old_job = self.run_media()
        self.grant()
        def retry():
            with db.psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=5) as conn:
                conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
                conn.execute("SET statement_timeout='5s'")
                return media_archive.retry_blocked(conn, mid, expected_channel='telegram (account)')['queued']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: retry(), range(2)))
        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(self.state(aid)[3]['media_consent_retries'], 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM effect_audit WHERE reason='media_consent_retry'"), 1)
        jobs.finish(old_job, RuntimeError('stale'))
        context = jobs.ExecutionContext(old_job[0], old_job[3], 'media', old_job[1], float('inf'))
        with patch.object(jobs._local, 'execution', context, create=True):
            with self.assertRaises(jobs.LeaseLost):
                media_archive.process(old_job[1])
        self.assertEqual(self.state(aid)[0], 'pending')
        self.convert.assert_not_called()

    def test_consent_provenance_and_attachment_edits_are_locked_until_commit(self):
        from psycopg import sql
        mid, (aid,) = self.archive()
        self.run_media()
        self.grant()
        with db.psycopg.connect(self.url, autocommit=True, prepare_threshold=None, connect_timeout=5) as other:
            other.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            other.execute("SET lock_timeout='100ms'")
            with self.conn.transaction():
                self.assertEqual(media_archive.retry_blocked(self.conn, mid,
                    expected_channel='telegram (account)')['queued'], 1)
                changes = [
                    ("UPDATE data_policies SET allowed=false WHERE channel='telegram' AND effect=%s", (effect,))
                    for effect in ('ai_text', 'ai_media')]
                changes += [
                    ('''UPDATE telegram_messages SET metadata=metadata || '{"historical":true}'::jsonb WHERE id=%s''', (mid,)),
                    ("UPDATE attachments SET content_type='application/pdf' WHERE id=%s", (aid,)),
                    ("UPDATE work_jobs SET status='failed' WHERE key=%s", (f'media:{aid}',))]
                for statement, args in changes:
                    with self.subTest(statement=statement, args=args), self.assertRaises(db.psycopg.errors.LockNotAvailable):
                        other.execute(statement, args)
            other.execute("UPDATE data_policies SET allowed=false WHERE channel='telegram'")
        # Revocation after the recovery commit is still checked by the worker.
        self.run_media()
        self.assertEqual(self.state(aid)[0], 'blocked')
        self.convert.assert_not_called()


MEDIA_UI_SCENARIO = r'''
async function mediaRecovery() {
  context.confirm = () => true;
  const hit = {source_type: 'message', source_id: 7, entry_id: 901,
    citation: 'message:7@revision', channel: 'telegram (account)'};
  const source = {...hit, status: 'indexed', text: 'Native caption',
    current: true, historical: false, ...options.source};
  await searchedSource(hit, '/api/knowledge/open/entry/901', source);
  const retry = element('source-documents').querySelector('button');
  if (options.denied) {assert.equal(retry, null); return;}
  assert.ok(retry, 'Missing explicit media recovery control');
  assert.equal(requests.length, 0, 'Opening a source must never retry');
  const status = element('source-documents').children.at(-1);
  const pending = retry.onclick();
  assert.equal(retry.disabled, true);
  const request = take('/api/media/retry-blocked');
  assert.equal(request.init.method, 'POST');
  assert.equal(request.init.headers['X-CSRF-Token'], 'synthetic-session');
  assert.deepEqual(JSON.parse(request.init.body), {message_id:7, expected_channel:'telegram (account)'});
  if (options.stale) {
    await logout();
    await login();
    const before = domState(), oldText = status.textContent;
    finish(request, options.outcome, {queued:1});
    await pending;
    assert.deepEqual(domState(), before);
    assert.equal(status.textContent, oldText);
  } else {
    finish(request, options.outcome, {queued:options.queued});
    await pending;
    assert.equal(retry.disabled, false);
    assert.ok(status.textContent.includes(options.outcome === 'success'
      ? options.queued ? 'media accodati' : 'Nessun media recuperabile'
      : 'verifica entrambi i consensi'));
  }
}
'''


class MediaConsentWebTests(unittest.TestCase):
    def scenario(self, **options):
        from test_web_session_safety import NODE_HARNESS
        harness = NODE_HARNESS.replace('(async () => {', MEDIA_UI_SCENARIO + '\n(async () => {', 1)
        harness = harness.replace("if (scenario === 'sources') await sources();",
            "if (scenario === 'media') await mediaRecovery(); else if (scenario === 'sources') await sources();")
        result = subprocess.run(['node', '--unhandled-rejections=strict', '-e', harness],
            input=json.dumps(dict(script=(ROOT / 'web/app.js').read_text(), scenario='media', options=options)),
            cwd=ROOT, env={'PATH': os.defpath}, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_per_native_source_action_and_no_source_open_replay(self):
        for queued in (0, 1):
            self.scenario(outcome='success', queued=queued)
        self.scenario(outcome='unavailable')

    def test_history_stale_revision_and_unsupported_sources_have_no_button(self):
        for source in (dict(historical=True), dict(current=False), dict(channel='email'),
                       dict(source_type='document'), dict(source_id=2 ** 53)):
            self.scenario(source=source, denied=True)

    def test_retry_continuations_are_session_fenced(self):
        for outcome in ('success', 'unavailable', 'network', 'unauthorized'):
            self.scenario(outcome=outcome, queued=1, stale=True)


if __name__ == '__main__':
    unittest.main()