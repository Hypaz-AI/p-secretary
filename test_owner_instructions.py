"""Offline contracts for permanent owner instructions.

No production database, network, workers or provider calls. The apply/suspend
paths run for real against a strict SQL double; model outputs are scripted.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

with tempfile.TemporaryDirectory(prefix='owner-instructions-') as _bootstrap:
    with patch.dict(os.environ, {'PI_SECRETARY_CONFIG_FILE': '/dev/null',
                                 'DATA_DIR': _bootstrap, 'DATABASE_URL': ''}):
        import brain
        import config
        import db
        import jobs
        import owner_instructions
        import policy


CANARY = 'INSTRUCTION_CANARY_71bc'
STANDING = ('Da ora in poi salva tutte le fatture in cartelle chiamate '
            'come la P.IVA dell\'azienda a cui sono intestate.')
QUESTION = 'Come organizzi le fatture di solito?'
ONE_OFF = 'Salva questa fattura nella cartella test.'


class InstructionTests(unittest.TestCase):
    """Strict SQL double over owner_instructions; apply/suspend run for real."""

    def setUp(self):
        self.instructions = {}
        self.next_id = [1]
        self.conn = MagicMock(name='connection')
        self.conn.execute.side_effect = self.execute
        self.enterContext(patch.object(db, '_conn')).return_value.__enter__.return_value = self.conn
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch.object(brain.ai_budget, 'request_content',
                                       side_effect=AssertionError('Provider forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        self.enterContext(patch.object(jobs, 'assert_current'))
        self.enterContext(patch.object(config, 'TELEGRAM_CHAT_ID', '7'))
        self.enterContext(patch.object(config, 'TELEGRAM_BOT_TOKEN', '123:offline'))
        self.enterContext(patch.object(policy, 'can_operate', return_value=True))
        self.enterContext(patch.object(policy, 'metadata_for', return_value=dict(
            channel='telegram', metadata=dict(content_origin='direct_text', is_forwarded=False,
                                              chat_type='private', account='bot:123',
                                              chat_id='7', author='7', direction='in',
                                              message_id=42),
            account='bot:123', chat_id='7', author='7', direction='in',
            source_id=json.dumps(['bot', '123', '7', 42]), message_id='42')))

    def execute(self, sql, params=None):
        params = tuple(params or ())
        if sql.startswith('SELECT pg_advisory_xact_lock'):
            return SimpleNamespace(fetchone=lambda: None)
        if sql.startswith('SELECT channel,body,account,chat_id,author,direction'):
            meta = dict(content_origin='direct_text', is_forwarded=False, chat_type='private',
                        account='bot:123', chat_id='7', author='7', direction='in',
                        message_id=42)
            row = ('telegram', STANDING, 'bot:123', '7', '7', 'in',
                   json.dumps(['bot', '123', '7', 42]), meta, '2026-10-18T10:00:00+00:00')
            return SimpleNamespace(fetchone=lambda: row)
        if sql.startswith('SELECT id FROM owner_instructions'):
            for note in self.instructions.values():
                if (note['origin_message'] == params[0] and note['domain'] == params[1]
                        and note['action'] == params[2]
                        and json.loads(params[3]) == note['params']):
                    return SimpleNamespace(fetchone=lambda: (note['id'],))
            return SimpleNamespace(fetchone=lambda: None)
        if sql.startswith('INSERT INTO owner_instructions'):
            identity = self.next_id[0]
            self.next_id[0] += 1
            self.instructions[identity] = dict(id=identity, domain=params[0], action=params[1],
                                               params=json.loads(params[2]),
                                               origin_message=params[3], evidence=params[4],
                                               state='active', version=1)
            return SimpleNamespace(fetchone=lambda: (identity,))
        if sql.startswith('UPDATE owner_instructions'):
            note = self.instructions.get(params[0])
            if note and note['state'] == 'active':
                note['state'], note['version'] = 'suspended', note['version'] + 1
                return SimpleNamespace(fetchone=lambda: (note['id'], note['version']))
            return SimpleNamespace(fetchone=lambda: None)
        if sql.startswith('SELECT id,domain,action,params,state,version,evidence,created_at'):
            rows = [(n['id'], n['domain'], n['action'], json.dumps(n['params']), n['state'],
                     n['version'], n['evidence'], '2026-10-18')
                    for n in self.instructions.values()]
            return SimpleNamespace(fetchall=lambda: rows)
        if sql.startswith('SELECT action,params FROM owner_instructions'):
            rows = [(n['action'], n['params'])
                    for n in self.instructions.values() if n['state'] == 'active']
            return SimpleNamespace(fetchall=lambda: rows)
        raise AssertionError('Unexpected SQL: ' + sql)

    def chat(self, *responses):
        return patch.object(brain, '_chat', side_effect=list(responses))

    def model_yes(self, action='route_by_sender_vat', evidence=STANDING):
        return json.dumps(dict(instruction=dict(domain='invoice_folder', action=action,
                                                params={}), evidence=evidence))


class VocabularyTests(InstructionTests):
    def test_lexical_guard_avoids_model_calls_on_ordinary_messages(self):
        for text, expected in ((STANDING, True),
                               ("D'ora in avanti usa il nome del mittente per le cartelle.", True),
                               ('Ogni volta che arriva una fattura avvisami.', True),
                               (QUESTION, False),
                               (ONE_OFF, False),
                               ('', False),
                               ('"Da ora in poi fai sempre X" disse qualcuno', False),
                               ('> Da ora in poi fai sempre X', False),
                               ('Forse da ora in poi salvo sempre le fatture?', False)):
            with self.subTest(text=text[:40]):
                self.assertEqual(owner_instructions.looks_like_instruction(text), expected)

    def test_only_closed_vocabulary_domains_and_actions(self):
        self.assertEqual(set(owner_instructions.DOMAINS), {'invoice_folder'})
        self.assertEqual(owner_instructions.DOMAINS['invoice_folder']['actions'],
                         ('route_by_sender_vat', 'route_by_sender_name'))

    def test_unknown_domain_action_or_params_rejected_before_sql(self):
        for instruction in (dict(domain='email_send', action='send_all'),
                            dict(domain='invoice_folder', action='delete_files'),
                            dict(domain='invoice_folder', action='route_by_sender_vat',
                                 params=dict(path='/etc')),
                            dict(domain='invoice_folder', action='route_by_sender_vat',
                                 params={'x' * 61: 'y'})):
            with self.subTest(instruction=instruction), self.assertRaises(
                    owner_instructions.InvalidInstruction):
                owner_instructions._validate(instruction.get('domain'),
                                              instruction.get('action'), instruction.get('params'))

    def test_sender_override_map_is_bounded_and_typed(self):
        owner_instructions._validate('invoice_folder', 'route_by_sender_vat',
                                    dict(sender_override={'Fornitore X': '12345678901'}))
        for bad in ({'k': 5}, {'k' * 201: 'v'}, {'k': 'v' * 121},
                    dict.fromkeys(range(60), 'v')):
            with self.subTest(bad=bad), self.assertRaises(
                    owner_instructions.InvalidInstruction):
                owner_instructions._validate('invoice_folder', 'route_by_sender_vat',
                                             dict(sender_override=bad))


class PrepareTests(InstructionTests):
    def test_prepare_classifies_standing_instruction_with_exact_evidence(self):
        with self.chat(self.model_yes()):
            plan = owner_instructions.prepare(STANDING, 42)
        self.assertEqual(plan['domain'], 'invoice_folder')
        self.assertEqual(plan['action'], 'route_by_sender_vat')
        self.assertEqual(plan['evidence'], STANDING)
        self.assertEqual(plan['origin_message'], 42)

    def test_prepare_returns_none_for_non_instructions(self):
        # The model explicitly declines: instruction=null.
        with self.chat(json.dumps(dict(instruction=None, evidence=''))):
            self.assertIsNone(owner_instructions.prepare(STANDING, 42))

    def test_prepare_rejects_malformed_model_output(self):
        for raw in ('no json', '{"instruction":5}', '{"instruction":{}}',
                    '{"instruction":{"domain":"x","action":"y"},"evidence":"z"}',
                    json.dumps(dict(instruction=dict(domain='invoice_folder',
                                                     action='route_by_sender_vat'),
                                    evidence=QUESTION))):
            with self.subTest(raw=raw[:40]), self.chat(raw), self.assertRaises(
                    owner_instructions.InvalidInstruction):
                owner_instructions.prepare(STANDING, 42)

    def test_evidence_must_be_exact_current_sentence_with_standing_marker(self):
        for bad in ('Salva le fatture.', 'Domanda?', 'forse salva sempre le fatture',
                    'x' * 2001, 'dice di salvare sempre le fatture'):
            with self.subTest(bad=bad), self.chat(self.model_yes(evidence=bad)), self.assertRaises(
                    owner_instructions.InvalidInstruction):
                owner_instructions.prepare(STANDING, 42)

    def test_prepare_rejects_text_mismatch_with_native_body(self):
        with self.chat(self.model_yes()), self.assertRaises(
                owner_instructions.InvalidInstruction):
            owner_instructions.prepare('Testo diverso dal body nativo', 42)


class ApplyTests(InstructionTests):
    def plan(self, **overrides):
        result = dict(domain='invoice_folder', action='route_by_sender_vat', params={},
                      evidence=STANDING, origin_message=42)
        result.update(overrides)
        return result

    def test_apply_persists_instruction_and_is_idempotent_on_replay(self):
        first = owner_instructions.apply(self.conn, self.plan(), 42)
        self.assertFalse(first['replay'])
        second = owner_instructions.apply(self.conn, self.plan(), 42)
        self.assertTrue(second['replay'])
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.instructions), 1)

    def test_apply_rejects_unbound_or_invalid_plans(self):
        for plan in (None, 'x', self.plan(origin_message=99),
                     self.plan(domain='email_send'),
                     self.plan(action='delete_files'),
                     self.plan(evidence=5)):
            with self.subTest(plan=plan), self.assertRaises(
                    owner_instructions.InvalidInstruction):
                owner_instructions.apply(self.conn, plan, 42)
        self.assertEqual(self.instructions, {})

    def test_suspend_requires_active_instruction_and_bumps_version(self):
        owner_instructions.apply(self.conn, self.plan(), 42)
        result = owner_instructions.suspend(self.conn, 1, authority_message_id=42)
        self.assertEqual(result['state'], 'suspended')
        with self.assertRaises(owner_instructions.InvalidInstruction):
            owner_instructions.suspend(self.conn, 1, authority_message_id=42)
        with self.assertRaises(owner_instructions.InvalidInstruction):
            owner_instructions.suspend(self.conn, 99, authority_message_id=42)

    def test_suspend_requires_owner_authority(self):
        owner_instructions.apply(self.conn, self.plan(), 42)
        with patch.object(policy, 'can_operate', return_value=False), self.assertRaises(
                PermissionError):
            owner_instructions.suspend(self.conn, 1, authority_message_id=42)


class RoutingTests(InstructionTests):
    def test_vat_routing_extracts_eleven_digit_vat_from_sender(self):
        self.instructions[1] = dict(id=1, domain='invoice_folder', action='route_by_sender_vat',
                                    params={}, state='active', version=1)
        self.assertEqual(owner_instructions.invoice_folder('Fornitore SRL IT12345678901'),
                         '12345678901')
        self.assertEqual(owner_instructions.invoice_folder('Fornitore 98765432109'),
                         '98765432109')
        self.assertIsNone(owner_instructions.invoice_folder('Fornitore senza P.IVA'))

    def test_name_routing_slugifies_sender(self):
        self.instructions[1] = dict(id=1, domain='invoice_folder', action='route_by_sender_name',
                                    params={}, state='active', version=1)
        self.assertEqual(owner_instructions.invoice_folder('Fornitore Rossi & C.'),
                         'fornitore_rossi_c')

    def test_sender_override_wins_and_suspended_instructions_do_not_apply(self):
        self.instructions[1] = dict(id=1, domain='invoice_folder', action='route_by_sender_vat',
                                    params=dict(sender_override={'Rossi': '11122233344'}),
                                    state='active', version=1)
        self.assertEqual(owner_instructions.invoice_folder('Rossi SRL'), '11122233344')
        self.instructions[1]['state'] = 'suspended'
        self.assertIsNone(owner_instructions.invoice_folder('Rossi SRL'))

    def test_no_instructions_means_legacy_routing(self):
        self.assertIsNone(owner_instructions.invoice_folder('Chiunque'))


if __name__ == '__main__':
    unittest.main()
