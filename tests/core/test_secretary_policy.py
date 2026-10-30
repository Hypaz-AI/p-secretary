"""Pure mocked F1 policy/receipt tests; no database, AI, HTTP or secrets."""
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch

from secretary.core import jobs, policy
from secretary.workflows import task_reconciliation as tr


def native(channel='telegram', metadata=None, **overrides):
    attestation = dict(chat_type='private', content_origin='direct_text', is_forwarded=False,
                       account='bot:123', chat_id='7', author='7', direction='in', message_id=42)
    values = dict(channel=channel, metadata={**attestation, **(metadata or {})},
                  ts=datetime.now(timezone.utc), acquired_at=datetime.now(timezone.utc),
                  source_id=json.dumps(['bot', '123', '7', 42]),
                  account='bot:123', chat_id='7', author='7',
                  direction='in', message_id='42')
    values.update(overrides)
    return values


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('secretary.core.db._conn', side_effect=AssertionError('Database forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(policy.config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.conn = MagicMock()
        self.conn.transaction.side_effect = lambda: nullcontext(self.conn)


class MetadataTests(IsolatedTests):
    def test_native_fields_override_metadata_and_view_is_not_used(self):
        values = native(metadata={'historical': True, 'author': 'forged', 'channel': 'email'})
        self.conn.execute.return_value.fetchone.side_effect = [None, None, tuple(values.values())]
        result = policy.metadata_for(self.conn, 42)
        self.assertEqual(result['author'], '7')
        self.assertEqual(result['channel'], 'telegram')
        self.assertTrue(result['historical'])
        self.assertEqual(result['id'], 42)
        sql = '\n'.join(c.args[0] for c in self.conn.execute.call_args_list)
        for table in policy.NATIVE_TABLES:
            self.assertIn(table, sql)
        self.assertNotIn('communications', sql)
        self.assertIn('FOR SHARE', sql)

    def test_missing_invalid_and_boolean_message_ids_fail_closed(self):
        for identity in (None, True, False, '42', 0, -1):
            self.assertEqual(policy.metadata_for(self.conn, identity), {})
        self.conn.execute.assert_not_called()
        self.conn.execute.return_value.fetchone.return_value = None
        self.assertEqual(policy.metadata_for(self.conn, 42), {})

    def test_missing_or_malformed_native_metadata_is_not_authority(self):
        self.conn.execute.return_value.fetchone.return_value = tuple(native(metadata={}).values())
        self.assertEqual(policy.metadata_for(self.conn, 42)['channel'], 'telegram')
        values = native()
        values['metadata'] = 'untrusted JSON string'
        self.conn.execute.return_value.fetchone.return_value = tuple(values.values())
        self.assertEqual(policy.metadata_for(self.conn, 42), {})

    def test_migrate_is_versioned_additive_and_no_data_consent_is_seeded(self):
        self.conn.execute.return_value.fetchone.return_value = None
        policy.migrate(self.conn)
        sql = '\n'.join(c.args[0] for c in self.conn.execute.call_args_list)
        for table in ('data_policies', 'effect_audit', 'effect_receipts'):
            self.assertIn('CREATE TABLE IF NOT EXISTS ' + table, sql)
        for forbidden in ('DROP TABLE', 'DROP COLUMN', 'DROP CONSTRAINT', 'TRUNCATE ',
                          'INSERT INTO data_policies', 'UPDATE telegram_messages'):
            self.assertNotIn(forbidden, sql)
        self.assertIn('ALTER COLUMN message_id DROP NOT NULL', sql)
        self.assertIn('ADD COLUMN IF NOT EXISTS "authorization" TEXT', sql)
        self.assertEqual(self.conn.execute.call_args.args[1], (policy.VERSION,))
        self.conn.reset_mock()
        self.conn.execute.return_value.fetchone.return_value = (1,)
        policy.migrate(self.conn)
        self.assertEqual(self.conn.execute.call_count, 3)


class AuthorityTests(IsolatedTests):
    def allowed(self, data=None, effect='owner_command', **kwargs):
        with patch.object(policy, 'metadata_for', return_value=data if data is not None else native()):
            return policy.can_operate(self.conn, 42, effect, **kwargs)

    def test_only_native_private_direct_matching_bot_owner_can_command(self):
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed(effect='task_apply'))
        self.assertTrue(self.allowed(effect='notify', owner_required=True))
        for change in ({'channel': 'telegram (account)'}, {'channel': 'email'},
                       {'channel': 'whatsapp'}, {'account': 'bot:999'}, {'account': None},
                       {'author': None}, {'author': '8'}, {'chat_id': '8'},
                       {'direction': 'out'}, {'metadata': {'chat_type': 'group'}},
                       {'metadata': {'owner': True, 'trusted': True}}):
            with self.subTest(change=change):
                values = native()
                values.update(change)
                self.assertFalse(self.allowed(values))

    def test_all_forward_provenance_denies_owner_even_hidden_origin(self):
        for field, value in (('forward_origin', {'type': 'hidden_user', 'sender_user_name': 'Owner'}),
                             ('forward_origin', {}), ('forward_date', 1),
                             ('forward_from', {'id': 7}), ('forward_from_chat', {'id': 7}),
                             ('forward_sender_name', 'owner'), ('is_automatic_forward', True),
                             ('is_forwarded', True)):
            with self.subTest(field=field):
                self.assertFalse(self.allowed(native(metadata={field: value})))
                self.assertTrue(self.allowed(native(metadata={field: value}), 'task_propose'))

    def test_missing_or_non_direct_transport_attestation_is_never_owner_authority(self):
        for field in native()['metadata']:
            values = native()
            del values['metadata'][field]
            with self.subTest(missing=field):
                self.assertFalse(self.allowed(values))
                self.assertTrue(self.allowed(values, 'task_propose'))
        for origin in ('caption', 'transcript', 'document', None):
            with self.subTest(origin=origin):
                self.assertFalse(self.allowed(native(metadata={'content_origin': origin})))

    def test_voice_image_and_document_get_dialogue_but_never_command_authority(self):
        # Nella chat privata owner↔bot ogni input va valutato e riceve risposta;
        # solo il testo scritto può però eseguire comandi o autorizzare scritture.
        for origin in ('direct_audio', 'attachment'):
            with self.subTest(origin=origin):
                values = native(metadata={'content_origin': origin})
                self.assertTrue(self.allowed(values, 'owner_dialogue', owner_required=True))
                self.assertFalse(self.allowed(values, 'owner_command', owner_required=True))
                for effect in ('task_apply', 'task_create', 'memory_write', 'email_send',
                               'reminder_create'):
                    self.assertFalse(self.allowed(values, effect))
                self.assertTrue(self.allowed(values, 'task_propose'))
        # Il testo scritto resta l'unica autorità di comando.
        self.assertTrue(self.allowed(native(), 'owner_command', owner_required=True))
        self.assertTrue(self.allowed(native(), 'owner_dialogue', owner_required=True))

    def test_dialogue_authority_still_requires_the_private_owner_chat(self):
        for change in ({'channel': 'telegram (account)'}, {'channel': 'whatsapp'},
                       {'account': 'bot:999'}, {'author': '8'}, {'chat_id': '8'},
                       {'direction': 'out'}, {'metadata': {'chat_type': 'group'}},
                       {'metadata': {'is_forwarded': True}},
                       {'metadata': {'historical': True}}):
            with self.subTest(change=change):
                values = native(metadata={'content_origin': 'direct_audio'})
                values.update(change)
                self.assertFalse(self.allowed(values, 'owner_dialogue', owner_required=True))

    def test_metadata_identity_and_native_source_must_agree(self):
        for field, value in (('account', 'bot:999'), ('chat_id', '8'), ('author', '8'),
                             ('direction', 'out'), ('message_id', '42'), ('message_id', True),
                             ('message_id', 43)):
            with self.subTest(field=field, value=value):
                self.assertFalse(self.allowed(native(metadata={field: value})))
        for source in ('native-id', json.dumps(['bot', '999', '7', 42]),
                       json.dumps(['bot', '123', '8', 42]),
                       json.dumps(['bot', '123', '7', '42']),
                       json.dumps(['bot', '123', '7', True]),
                       json.dumps(['bot', '123', '7', 43])):
            with self.subTest(source=source):
                self.assertFalse(self.allowed(native(source_id=source)))
        self.assertFalse(self.allowed(native(message_id='43')))

    def test_configuration_and_native_ids_cannot_be_replaced_by_model_booleans(self):
        for owner in ('', None, '-7', 'not-an-id'):
            with patch.object(policy.config, 'TELEGRAM_CHAT_ID', owner):
                self.assertFalse(self.allowed(native(metadata={'owner': True})))
        for identity in (True, False, None, '42', 0):
            self.assertFalse(policy.can_operate(self.conn, identity, 'owner_command'))

    def test_historical_denied_for_every_effect_and_cannot_be_overridden(self):
        for effect in policy.LOCAL_EFFECTS | policy.OWNER_EFFECTS | policy.DATA_EFFECTS:
            with self.subTest(effect=effect):
                self.assertFalse(self.allowed(native(metadata={'historical': True}), effect))
        self.assertEqual(self.conn.execute.call_args.args[1][-1], 'historical')

    def test_missing_owner_required_false_is_not_authorized(self):
        self.assertFalse(self.allowed({}, 'task_propose'))
        self.assertFalse(self.allowed({}, 'owner_command', owner_required=False))

    def test_external_and_own_outgoing_are_evidence_not_commands(self):
        for channel, direction in (('email', 'in'), ('whatsapp', 'out'),
                                   ('telegram (account)', 'out')):
            values = native(channel=channel, direction=direction,
                            metadata={'owner': True, 'trusted_owner': True})
            self.assertTrue(self.allowed(values, 'task_propose'))
            self.assertFalse(self.allowed(values, 'task_apply'))
            self.assertFalse(self.allowed(values, 'notify', owner_required=True))

    def test_ai_data_requires_exact_explicit_channel_effect_policy(self):
        for row, expected in ((None, False), ((False,), False), (('true',), False), ((True,), True)):
            self.conn.execute.return_value.fetchone.return_value = row
            with self.subTest(row=row):
                self.assertEqual(self.allowed(native(channel='email (personal)'), 'ai_text'), expected)
                call = self.conn.execute.call_args_list[-2]
                self.assertEqual(call.args[1], ('email', 'ai_text'))

    def test_no_blanket_remote_send_calendar_zip_or_unknown_grant(self):
        # 'zip' is now the delegated alias of zip_send (LOCAL_EFFECTS): it is
        # tested separately below. Everything else here stays non-delegated.
        for effect in ('send', 'whatsapp_send', 'calendar', 'pay',
                       'delete', 'model-secret@example.invalid'):
            self.assertFalse(self.allowed(effect=effect))
        self.assertNotIn('model-secret', str(self.conn.execute.call_args_list))

    def test_email_send_is_a_native_owner_effect_never_third_party(self):
        # L'invio di una bozza è autorizzato SOLO dal gesto nativo del proprietario (/invia).
        self.assertIn('email_send', policy.OWNER_EFFECTS)
        self.assertTrue(self.allowed(effect='email_send'))
        for channel, direction in (('email', 'in'), ('whatsapp', 'in'), ('telegram (account)', 'out')):
            self.assertFalse(self.allowed(native(channel=channel, direction=direction), 'email_send'))
        self.assertFalse(self.allowed(native(metadata={'is_forwarded': True}), 'email_send'))
        self.assertFalse(self.allowed(native(metadata={'historical': True}), 'email_send'))

    def test_zip_send_is_delegated_local_effect(self):
        # zip_send is delegated: an archived native message may authorize the
        # monthly archive notification, with its own provenance.
        self.assertIn('zip_send', policy.LOCAL_EFFECTS)
        self.assertTrue(self.allowed(native(), 'zip_send'))


class ReceiptTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(policy, 'can_operate', return_value=True))
        self.payload = {'text': 'private body', 'to': 'owner'}
        self.digest = hashlib.sha256(json.dumps(self.payload, sort_keys=True,
                                               separators=(',', ':')).encode()).hexdigest()

    def test_concrete_payload_hash_and_stable_key_not_private_body_stored(self):
        self.conn.execute.return_value.fetchone.return_value = (42, 'notify', self.digest, False, 'authorized', None)
        self.assertTrue(policy.prepare_effect(self.conn, 'notify:42', 42, 'notify', self.payload))
        sql = str(self.conn.execute.call_args_list)
        self.assertIn(self.digest, sql)
        self.assertNotIn('private body', sql)

    def test_changed_payload_or_authority_is_never_new_approval(self):
        for row in ((42, 'notify', 'different', False, 'authorized', None),
                    (42, 'notify', self.digest, True, 'authorized', None),
                    (42, 'notify', self.digest, False, 'authorized', 'digest:other')):
            self.conn.execute.return_value.fetchone.return_value = row
            with self.assertRaisesRegex(ValueError, 'identity conflict'):
                policy.prepare_effect(self.conn, 'notify:42', 42, 'notify', self.payload)

    def test_attempted_uncertain_or_delivered_effect_is_not_prepared_again(self):
        for status in ('attempted', 'uncertain', 'accepted', 'verified', 'failed', 'blocked'):
            self.conn.execute.return_value.fetchone.return_value = (42, 'notify', self.digest, False, status, None)
            self.assertFalse(policy.prepare_effect(self.conn, 'notify:42', 42, 'notify', self.payload))

    def test_revocation_at_attempt_blocks_without_remote_action(self):
        self.conn.execute.return_value.fetchone.return_value = (42, 'notify', False, 'authorized', None, None, self.digest)
        policy.can_operate.return_value = False
        self.assertFalse(policy.record_receipt(self.conn, 'notify:42', 'attempted'))
        self.assertEqual(self.conn.execute.call_args.args[1][0], 'blocked')

    def test_repeated_attempt_is_not_send_permission(self):
        self.conn.execute.return_value.fetchone.return_value = (42, 'notify', False, 'attempted', None, None, self.digest)
        self.assertFalse(policy.record_receipt(self.conn, 'notify:42', 'attempted'))
        self.assertEqual(self.conn.execute.call_count, 1)

    def test_receipt_transition_and_uncertain_never_retries(self):
        for old, new, expected in (('authorized', 'attempted', True),
                                    ('attempted', 'accepted', True),
                                    ('accepted', 'verified', True),
                                    ('attempted', 'attempted', False),
                                    ('uncertain', 'attempted', False),
                                    ('verified', 'attempted', False)):
            self.conn.execute.return_value.fetchone.return_value = (42, 'notify', False, old, None, None, self.digest)
            with self.subTest(old=old, new=new):
                self.assertEqual(policy.record_receipt(self.conn, 'notify:42', new), expected)

    def test_superseded_attempt_cannot_publish_result(self):
        self.conn.execute.return_value.fetchone.return_value = (42, 'notify', False, 'attempted', 'old', None, self.digest)
        with patch.object(jobs, 'assert_current'), patch.object(jobs, 'execution_context',
                return_value=jobs.ExecutionContext(8, 'new', 'action', {}, float('inf'))):
            self.assertFalse(policy.record_receipt(self.conn, 'notify:42', 'accepted'))
        self.assertEqual(self.conn.execute.call_count, 1)

    def test_attempt_rechecks_saved_payload_hash_and_sealed_authorization(self):
        self.conn.execute.return_value.fetchone.return_value = (
            42, 'calendar_write', True, 'authorized', None, 'calendar:sealed', self.digest)
        with patch.object(policy, '_calendar_allowed', return_value=False) as allowed:
            self.assertFalse(policy.record_receipt(self.conn, 'calendar:42', 'attempted'))
        allowed.assert_called_once_with(self.conn, 42, self.digest, 'calendar:sealed')
        policy.can_operate.assert_not_called()
        self.assertEqual(self.conn.execute.call_args.args[1][0], 'blocked')


class TaskLifecyclePolicyTests(IsolatedTests):
    """Il ciclo di vita ricontrolla provenienza e autorità al momento dell'effetto."""

    def setUp(self):
        super().setUp()
        self.gate = self.enterContext(patch.object(policy, 'can_operate',
                                    side_effect=lambda conn, mid, effect, **kw: effect != 'task_apply'))
        self.enterContext(patch.object(jobs, 'assert_current'))
        self.enterContext(patch.object(jobs, 'execution_context', return_value=None))

    def test_execution_rechecks_history_and_authority_on_queued_proposal(self):
        with patch.object(tr, '_derived') as derived:
            for effect in ('task_propose', 'task_apply'):
                self.conn.execute.return_value.fetchone.side_effect = [
                    (7,), ('open', 3, 'Invoice', None), ('complete', {}, 3, 'pending'), (42, '')]
                self.gate.side_effect = lambda conn, mid, requested, **kw: requested != effect
                self.assertEqual(tr.apply_change(self.conn, 12), 'blocked')
            derived.assert_not_called()

    def test_owner_confirmation_checked_by_native_id_not_boolean(self):
        self.gate.side_effect = lambda *args, **kwargs: False
        self.assertEqual(tr.apply_change(self.conn, 12, authority_message_id=True), 'blocked')
        self.assertEqual(self.gate.call_args.args[1], True)
        self.assertTrue(self.gate.call_args.kwargs['owner_required'])

    def test_background_job_cannot_use_the_owner_id_of_an_old_payload(self):
        context = jobs.ExecutionContext(1, 'tok', 'analysis', {'message_id': 42}, float('inf'))
        with patch.object(jobs, 'execution_context', return_value=context):
            self.gate.side_effect = lambda *args, **kwargs: True
            self.assertEqual(tr.apply_change(self.conn, 12, authority_message_id=42), 'blocked')


if __name__ == '__main__':
    unittest.main()