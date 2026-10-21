"""Read-only history consent contracts; no production config, providers or I/O.

Unit tests exercise real policy/provenance against a strict SQL double. Optional
PostgreSQL tests use only an explicit PI_SECRETARY_TEST_DATABASE_URL naming a
Unix-socket *_test database, with a fresh private schema and no public fallback.
Run with PI_SECRETARY_CONFIG_FILE=/dev/null and a temporary DATA_DIR.
"""
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

from secretary.core import policy


FIELDS = ('channel', 'metadata', 'ts', 'acquired_at', 'source_id', 'account',
          'chat_id', 'author', 'direction', 'message_id')
CANARY = 'PRIVATE_HISTORY_CONTEXT_CANARY_572ab'


def native(channel='telegram', *, historical=False, metadata=None, **overrides):
    attestation = dict(chat_type='private', content_origin='direct_text', is_forwarded=False,
                       account='bot:123', chat_id='7', author='7', direction='in', message_id=42)
    values = dict(channel=channel, metadata=dict(attestation, historical=historical),
                  ts=datetime(2001, 1, 1, tzinfo=timezone.utc),
                  acquired_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
                  source_id=json.dumps(['bot', '123', '7', 42]),
                  account='bot:123', chat_id='7', author='7', direction='in', message_id='42')
    values['metadata'].update(metadata or {})
    values.update(overrides)
    return values


class HistoryContextPolicyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(policy.config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.messages = {table: {} for table in policy.NATIVE_TABLES}
        self.grants = {}
        self.audits = []
        self.conn = MagicMock()
        self.conn.execute.side_effect = self.execute

    def execute(self, sql, params):
        if sql.startswith('SELECT ' + ','.join(FIELDS) + ' FROM '):
            self.assertIn('FOR SHARE', sql)
            table = sql.split(' FROM ', 1)[1].split()[0]
            row = self.messages[table].get(params[0])
        elif sql.startswith('SELECT allowed FROM data_policies '):
            self.assertIn('FOR SHARE', sql)
            row = (self.grants[params],) if params in self.grants else None
        elif sql.startswith('INSERT INTO effect_audit('):
            self.audits.append(params)
            row = None
        else:
            raise AssertionError('Unexpected SQL: ' + sql)
        return SimpleNamespace(fetchone=lambda: row)

    def message(self, channel='email', *, mid=42, **kwargs):
        table = channel.split(' ', 1)[0] + '_messages'
        values = native(channel, **kwargs)
        self.messages[table][mid] = tuple(values[field] for field in FIELDS)
        return mid

    def consent(self, channel='email', *effects, allowed=True):
        for effect in effects or ('ai_text',):
            self.grants[channel, effect] = allowed

    def test_historical_text_requires_both_explicit_grants_on_every_native_table(self):
        for index, channel in enumerate(('email', 'telegram', 'whatsapp'), 1):
            mid = self.message(channel, mid=index, historical=True)
            with self.subTest(channel=channel):
                self.assertFalse(policy.can_read_context(self.conn, mid))
                self.consent(channel, 'ai_history')
                self.assertFalse(policy.can_read_context(self.conn, mid))
                self.consent(channel, 'ai_text')
                self.assertTrue(policy.can_read_context(self.conn, mid))
                self.assertEqual(self.audits[-1], (mid, 'ai_text', True, 'context_allowed'))
                self.consent(channel, 'ai_history', allowed=False)
                self.assertFalse(policy.can_read_context(self.conn, mid))
                self.assertEqual(self.audits[-1][-1], 'context_history_denied')
                self.consent(channel, 'ai_history')
                self.consent(channel, 'ai_text', allowed=False)
                self.assertFalse(policy.can_read_context(self.conn, mid))

    def test_live_context_needs_text_not_history_even_if_timestamp_is_old(self):
        mid = self.message()
        self.assertFalse(policy.can_read_context(self.conn, mid))
        self.consent('email', 'ai_history')
        self.assertFalse(policy.can_read_context(self.conn, mid))
        self.consent()
        self.assertTrue(policy.can_read_context(self.conn, mid))
        self.consent('email', 'ai_history', allowed=False)
        self.assertTrue(policy.can_read_context(self.conn, mid))
        values = native('email')
        del values['metadata']['historical']
        self.messages['email_messages'][mid] = tuple(values[field] for field in FIELDS)
        self.assertTrue(policy.can_read_context(self.conn, mid))

    def test_media_needs_text_and_media_and_history_only_if_native_history(self):
        for historical in (False, True):
            with self.subTest(historical=historical):
                self.grants.clear()
                mid = self.message(historical=historical)
                self.consent('email', 'ai_media', 'ai_history')
                self.assertFalse(policy.can_read_context(self.conn, mid, 'ai_media'))
                self.consent()
                self.assertTrue(policy.can_read_context(self.conn, mid, 'ai_media'))
                self.consent('email', 'ai_media', allowed=False)
                self.assertTrue(policy.can_read_context(self.conn, mid))
                self.assertFalse(policy.can_read_context(self.conn, mid, 'ai_media'))
                self.consent('email', 'ai_media')
                self.consent('email', 'ai_history', allowed=False)
                self.assertEqual(policy.can_read_context(self.conn, mid, 'ai_media'), not historical)

    def test_missing_and_invalid_identity_fail_closed_without_native_query(self):
        self.message(historical=True)
        self.consent('email', 'ai_text', 'ai_history', 'ai_media')
        for identity in (None, True, False, '42', 0, -1, 2 ** 63, CANARY):
            self.conn.reset_mock()
            with self.subTest(identity=identity):
                self.assertFalse(policy.can_read_context(self.conn, identity))
                self.assertEqual(self.conn.execute.call_count, 1)
                self.assertEqual(self.audits[-1], (None, 'ai_text', False, 'context_missing_message'))
        self.assertFalse(policy.can_read_context(self.conn, 999))
        self.assertEqual(self.audits[-1], (999, 'ai_text', False, 'context_missing_message'))
        self.assertNotIn(CANARY, str(self.conn.execute.call_args_list))

    def test_native_channel_overrides_forged_flags_and_channel_grants_do_not_cross(self):
        mid = self.message('telegram (account)', historical=True,
            metadata=dict(channel='email', owner=True, allowed=True, ai_history=True))
        self.consent('email', 'ai_text', 'ai_history')
        self.assertFalse(policy.can_read_context(self.conn, mid))
        self.consent('telegram', 'ai_text')
        self.assertFalse(policy.can_read_context(self.conn, mid))
        self.consent('telegram', 'ai_history')
        self.assertTrue(policy.can_read_context(self.conn, mid))
        self.assertIn(('telegram', 'ai_history'), [call.args[1]
            for call in self.conn.execute.call_args_list if 'FROM data_policies ' in call.args[0]])
        self.consent('telegram', 'ai_history', allowed=False)
        self.consent('*', 'ai_history')
        self.consent('telegram (account)', 'ai_history')
        self.assertFalse(policy.can_read_context(self.conn, mid))

    def test_exact_boolean_consent_only_and_no_implicit_policy_writes(self):
        mid = self.message(historical=True)
        self.consent()
        for value in (None, False, 0, 1, 'true', 'false', {}, []):
            with self.subTest(value=value):
                self.consent('email', 'ai_history', allowed=value)
                self.assertFalse(policy.can_read_context(self.conn, mid))
        for call in self.conn.execute.call_args_list:
            self.assertTrue(call.args[0].startswith(('SELECT ', 'INSERT INTO effect_audit(')))

    def test_owner_required_never_promotes_history_or_external_evidence(self):
        self.consent('telegram', 'ai_text', 'ai_history', 'ai_media')
        mid = self.message('telegram', historical=True, metadata=dict(owner=True, trusted=True))
        self.assertTrue(policy.can_read_context(self.conn, mid))
        self.assertFalse(policy.can_read_context(self.conn, mid, owner_required=True))
        self.assertEqual(self.audits[-1][-1], 'context_owner_required')
        self.message('telegram', historical=False)
        self.assertTrue(policy.can_read_context(self.conn, mid, owner_required=True))
        self.message('telegram', metadata=dict(is_forwarded=True))
        self.assertTrue(policy.can_read_context(self.conn, mid))
        self.assertFalse(policy.can_read_context(self.conn, mid, owner_required=True))
        self.message('email')
        self.consent()
        del self.messages['telegram_messages'][mid]
        self.assertFalse(policy.can_read_context(self.conn, mid, owner_required=True))

    def test_malformed_native_history_and_metadata_fail_closed(self):
        self.consent('email', 'ai_text', 'ai_history', 'ai_media')
        for flag in (None, 0, 1, 'false', 'true', {}, []):
            with self.subTest(flag=flag):
                mid = self.message(historical=flag)
                self.assertFalse(policy.can_read_context(self.conn, mid))
                self.assertEqual(self.audits[-1][-1], 'context_historical_invalid')
        for metadata in (None, 'json-string', [], True):
            values = native('email')
            values['metadata'] = metadata
            self.messages['email_messages'][42] = tuple(values[field] for field in FIELDS)
            with self.subTest(metadata=metadata):
                self.assertFalse(policy.can_read_context(self.conn, 42))
                self.assertEqual(self.audits[-1][-1], 'context_missing_message')

    def test_missing_or_unknown_native_channel_cannot_use_forged_metadata_channel(self):
        self.consent('email', *policy.DATA_EFFECTS)
        for channel in (None, '', 'unknown', 42, ['email']):
            values = native(channel, historical=True, metadata=dict(channel='email'))
            self.messages['email_messages'][42] = tuple(values[field] for field in FIELDS)
            with self.subTest(channel=channel):
                self.assertFalse(policy.can_read_context(self.conn, 42))
                self.assertEqual(self.audits[-1][-1], 'context_channel_invalid')

    def test_history_context_consent_never_enables_operational_replay_or_receipts(self):
        mid = self.message(historical=True)
        self.consent('email', *policy.DATA_EFFECTS)
        self.assertTrue(policy.can_read_context(self.conn, mid))
        for effect in policy.LOCAL_EFFECTS | policy.OWNER_EFFECTS | policy.DATA_EFFECTS | {
                'calendar_write', 'zip_send', 'email_send'}:
            with self.subTest(effect=effect):
                self.assertFalse(policy.can_operate(self.conn, mid, effect))
                self.assertFalse(policy.can_operate(self.conn, mid, effect, owner_required=True))
                self.assertFalse(policy.effect_allowed(self.conn, mid, effect, {'text': CANARY}))
                self.assertFalse(policy.prepare_effect(self.conn, 'history:' + effect,
                                                        mid, effect, {'text': CANARY}))
        self.assertNotIn(CANARY, str(self.conn.execute.call_args_list))
        self.message(historical=False)
        self.assertFalse(policy.can_operate(self.conn, mid, 'ai_history'))
        self.assertEqual(self.audits[-1][-1], 'effect_not_delegated')

    def test_context_only_accepts_text_media_and_their_existing_aliases_with_safe_audits(self):
        mid = self.message(historical=True, metadata=dict(secret=CANARY))
        self.consent('email', *policy.DATA_EFFECTS)
        for effect in ('ai_text', 'ai', 'analysis', 'ai_media', 'media'):
            with self.subTest(effect=effect):
                self.assertTrue(policy.can_read_context(self.conn, mid, effect))
        for effect in policy.LOCAL_EFFECTS | policy.OWNER_EFFECTS | {
                'ai_history', 'calendar_write', 'zip_send', CANARY, 'x' * 81,
                None, False, 42, ''}:
            with self.subTest(effect=effect):
                self.assertFalse(policy.can_read_context(self.conn, mid, effect))
                self.assertEqual(self.audits[-1][-1], 'context_effect_not_delegated')
        for effect in ([], {}, {'effect': CANARY}):
            self.assertFalse(policy.can_read_context(self.conn, mid, effect))
        self.assertNotIn(CANARY, str(self.conn.execute.call_args_list))
        self.assertNotIn(CANARY, str(self.audits))


class HistoryContextPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get('PI_SECRETARY_TEST_DATABASE_URL')
        if not cls.url:
            raise unittest.SkipTest('Set PI_SECRETARY_TEST_DATABASE_URL to opt in')
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            raise unittest.SkipTest('psycopg is not installed')
        params = conninfo_to_dict(cls.url)
        if (not params.get('dbname', '').endswith('_test') or params.get('service')
                or params.get('hostaddr') or not params.get('host', '').startswith('/')
                or ',' in params['host']):
            raise ValueError('An explicit Unix-socket *_test database (no service/hostaddr) is required')
        cls.psycopg = psycopg
        cls.enterClassContext(patch.dict(os.environ,
            {key: value for key, value in os.environ.items() if not key.startswith('PG')}, clear=True))

    def setUp(self):
        from psycopg import sql
        from secretary.core import db, jobs
        from secretary.archive import message_store

        self.enterContext(patch.object(db, '_conn', side_effect=AssertionError('Configured DB forbidden')))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.conn = self.psycopg.connect(self.url, autocommit=True,
                                        prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.assertTrue(self.conn.execute('SELECT current_database()').fetchone()[0].endswith('_test'))
        self.schema = 'history_context_test_' + uuid.uuid4().hex
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET statement_timeout='15s'")
        self.conn.execute("SET lock_timeout='10s'")
        message_store.migrate(self.conn, db.SCHEMA)
        jobs.migrate(self.conn)
        policy.migrate(self.conn)

    def drop_schema(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def message(self, channel='email', *, historical=True, metadata=None):
        from psycopg import sql
        native_id = int(uuid.uuid4().hex[:12], 16) + 1
        values = native(channel, historical=historical, metadata=metadata,
                        source_id=json.dumps(['bot', '123', '7', native_id]),
                        message_id=str(native_id))
        values['metadata']['message_id'] = native_id
        return self.conn.execute(sql.SQL('''INSERT INTO {}
            (channel,metadata,ts,acquired_at,source_id,account,chat_id,author,direction,message_id,body)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''').format(
                sql.Identifier(channel.split(' ', 1)[0] + '_messages')),
            tuple(json.dumps(values[field]) if field == 'metadata' else values[field]
                  for field in FIELDS) + (CANARY,)).fetchone()[0]

    def consent(self, channel='email', *effects, allowed=True):
        for effect in effects or ('ai_text',):
            self.conn.execute('''INSERT INTO data_policies(channel,effect,allowed) VALUES (%s,%s,%s)
                ON CONFLICT(channel,effect) DO UPDATE SET allowed=excluded.allowed,
                version=data_policies.version+1,updated_at=now()''', (channel, effect, allowed))

    def test_native_history_is_read_only_and_every_effect_stays_fenced(self):
        for channel in ('email', 'telegram', 'whatsapp'):
            mid = self.message(channel)
            with self.subTest(channel=channel), self.conn.transaction():
                self.assertFalse(policy.can_read_context(self.conn, mid))
                self.consent(channel)
                self.assertFalse(policy.can_read_context(self.conn, mid))
                self.consent(channel, 'ai_history')
                self.assertTrue(policy.can_read_context(self.conn, mid))
                for effect in policy.LOCAL_EFFECTS | policy.OWNER_EFFECTS | policy.DATA_EFFECTS | {
                        'calendar_write', 'zip_send', 'email_send'}:
                    self.assertFalse(policy.can_operate(self.conn, mid, effect))
                    self.assertFalse(policy.effect_allowed(self.conn, mid, effect, {'text': CANARY}))
                    self.assertFalse(policy.prepare_effect(self.conn, f'history:{mid}:{effect}',
                                                            mid, effect, {'text': CANARY}))
        for table in ('effect_receipts', 'work_jobs', 'tasks', 'appointments', 'message_analysis'):
            self.assertEqual(self.conn.execute('SELECT count(*) FROM ' + table).fetchone(), (0,))
        audits = self.conn.execute('SELECT message_id,effect,allowed,reason FROM effect_audit').fetchall()
        self.assertNotIn(CANARY, str(audits))
        self.assertIn('ai_history', policy.DATA_EFFECTS)

    def test_real_channel_media_and_revocation_require_each_grant(self):
        mid = self.message('telegram (account)', metadata=dict(channel='email', allowed=True))
        self.consent('email', *policy.DATA_EFFECTS)
        self.assertFalse(policy.can_read_context(self.conn, mid))
        self.consent('telegram', 'ai_text', 'ai_history')
        self.assertTrue(policy.can_read_context(self.conn, mid))
        self.assertFalse(policy.can_read_context(self.conn, mid, 'ai_media'))
        self.consent('telegram', 'ai_media')
        self.assertTrue(policy.can_read_context(self.conn, mid, 'ai_media'))
        for effect in ('ai_text', 'ai_history', 'ai_media'):
            with self.subTest(effect=effect):
                self.consent('telegram', effect, allowed=False)
                self.assertFalse(policy.can_read_context(self.conn, mid, 'ai_media'))
                self.consent('telegram', effect)
        self.assertTrue(policy.can_read_context(self.conn, mid, 'ai_media'))
        self.assertFalse(policy.can_read_context(self.conn, mid, owner_required=True))
        self.assertFalse(policy.can_read_context(self.conn, None))
        self.assertFalse(policy.can_read_context(self.conn, mid + 1000))
        self.assertFalse(policy.can_read_context(self.conn, 2 ** 63))

    def test_existing_schema_needs_no_migration_or_seeded_history_consent(self):
        versions = self.conn.execute('SELECT version,reset_rows FROM schema_migrations ORDER BY version').fetchall()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM data_policies').fetchone(), (0,))
        mid = self.message()
        self.consent('email', 'ai_text', 'ai_history')
        before = self.conn.execute('SELECT * FROM data_policies ORDER BY channel,effect').fetchall()
        policy.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT * FROM data_policies ORDER BY channel,effect').fetchall(), before)
        self.assertEqual(self.conn.execute('SELECT version,reset_rows FROM schema_migrations ORDER BY version').fetchall(),
                         versions)
        self.assertTrue(policy.can_read_context(self.conn, mid))
        self.assertFalse(policy.can_operate(self.conn, mid, 'ai_history'))
        current = self.message(historical=False)
        self.assertTrue(policy.can_read_context(self.conn, current))
        self.assertFalse(policy.can_operate(self.conn, current, 'ai_history'))
        self.assertFalse(policy.can_read_context(self.conn, current, 'ai_history'))


if __name__ == '__main__':
    unittest.main()