"""Offline Google Calendar tests: HTTP mocked, nessuna rete o scrittura DB."""
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import config
import brain
import main
from connectors import gcal


def setUpModule():
    # Fixed aware clock: fixtures remain future dates regardless of run date.
    global _clock
    _clock = patch.object(gcal, 'datetime', wraps=datetime)
    clock = _clock.start()
    clock.now.side_effect = lambda tz: datetime(2026, 10, 14, 8, tzinfo=tz)


def tearDownModule():
    _clock.stop()


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(value)) + value


def _pkcs8_pem_from_pkcs1_der(pkcs1_der: bytes) -> str:
    """Converte RSAPrivateKey DER (PKCS#1) in PEM PKCS#8, il formato Google."""
    alg_id = bytes.fromhex('300d06092a864886f70d0101010500')  # rsaEncryption
    inner = _tlv(0x04, pkcs1_der)
    pkcs8 = _tlv(0x02, b'\x00') + alg_id + inner
    der = _tlv(0x30, pkcs8)
    return ('-----BEGIN PRIVATE KEY-----\n'
            + base64.b64encode(der).decode()
            + '\n-----END PRIVATE KEY-----\n')


def _pkcs1_der_from_pkcs8_pem(pkcs8_pem: str) -> bytes:
    """Estrae il DER PKCS#1 da un PEM PKCS#8 (per verificare col pacchetto rsa)."""
    body = ''.join(l for l in pkcs8_pem.splitlines() if '-----' not in l)
    der = base64.b64decode(body)

    def read_tlv(data, off):
        tag = data[off]
        off += 1
        length = data[off]
        off += 1
        if length & 0x80:
            n = length & 0x7F
            length = int.from_bytes(data[off:off + n], 'big')
            off += n
        return tag, data[off:off + length], off + length

    # il contenuto del SEQUENCE esterno va parsato DENTRO il suo valore
    _, outer, _ = read_tlv(der, 0)
    _, _, off = read_tlv(outer, 0)   # INTEGER 0
    _, _, off = read_tlv(outer, off)  # AlgorithmIdentifier
    _, inner, _ = read_tlv(outer, off)  # OCTET STRING
    _, rsa_der, _ = read_tlv(inner, 0)  # SEQUENCE RSAPrivateKey
    return rsa_der


class ServiceAccountTests(unittest.TestCase):
    """La modalità service account: chiave JSON, JWT, nessuna scadenza."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # chiave RSA generata al volo e serializzata in PEM PKCS#8, come le
        # chiavi JSON di Google. 1024 bit: veloci sul Raspberry, la logica
        # di firma è identica a 2048.
        import rsa as rsa_mod
        _, priv = rsa_mod.newkeys(1024)
        pkcs1_der = base64.b64decode(''.join(
            l for l in priv.save_pkcs1(format='PEM').decode().splitlines()
            if '-----' not in l))
        self.priv_pem = _pkcs8_pem_from_pkcs1_der(pkcs1_der)
        self.sa = {'type': 'service_account', 'project_id': 'proj',
                   'private_key': self.priv_pem,
                   'client_email': 'bot@proj.iam.gserviceaccount.com'}
        self.sa_path = Path(self.tmp.name) / 'gcal_service_account.json'
        self.sa_path.write_text(json.dumps(self.sa))
        # reset della cache del token tra i test
        gcal._sa_token_cache.update(token='', expires_at=0.0)

    def test_load_service_account_validates_type_and_key(self):
        with patch.object(gcal, 'service_account_file', return_value=self.sa_path):
            self.assertEqual(gcal._load_service_account(), self.sa)
        bad = dict(self.sa, type='authorized_user')
        self.sa_path.write_text(json.dumps(bad))
        with patch.object(gcal, 'service_account_file', return_value=self.sa_path):
            self.assertEqual(gcal._load_service_account(), {})

    def test_service_account_token_signs_jwt_rs256(self):
        with patch.object(gcal, 'service_account_file', return_value=self.sa_path), \
             patch.object(gcal, '_post', return_value={'access_token': 'tok',
                                                      'expires_in': 3600}) as post:
            token = gcal.get_access_token()
        self.assertEqual(token, 'tok')
        data = post.call_args.args[1]
        self.assertEqual(data['grant_type'],
                         'urn:ietf:params:oauth:grant-type:jwt-bearer')
        # decodifica il JWT e verifica header/claims/firma
        import rsa as rsa_mod
        from rsa import pkcs1

        def decode(segment: str) -> dict:
            padded = segment + '=' * (-len(segment) % 4)
            return json.loads(base64.urlsafe_b64decode(padded))

        header_b64, claims_b64, sig_b64 = data['assertion'].split('.')
        self.assertEqual(decode(header_b64), {'alg': 'RS256', 'typ': 'JWT'})
        claims = decode(claims_b64)
        self.assertEqual(claims['iss'], self.sa['client_email'])
        self.assertEqual(claims['scope'], gcal.SCOPES)
        self.assertEqual(claims['aud'], gcal.TOKEN_URL)
        self.assertEqual(claims['exp'] - claims['iat'], 3600)
        # verifica crittografica della firma: ricostruisci n ed e con il
        # parser DER di gcal (già testato) e verifica con pow(), senza
        # dipendere dal formato interno del pacchetto rsa
        sig = base64.urlsafe_b64decode(sig_b64 + '=' * (-len(sig_b64) % 4))
        n, d = gcal._parse_rsa_private_key(self.priv_pem)
        sig_int = int.from_bytes(sig, 'big')
        # firma^e mod n deve dare il padding PKCS#1 v1.5 atteso
        clear = pow(sig_int, 65537, n)  # e standard
        clear_bytes = clear.to_bytes((n.bit_length() + 7) // 8, 'big')
        digest_info = gcal._SHA256_DIGESTINFO + __import__('hashlib').sha256(
            f"{header_b64}.{claims_b64}".encode()).digest()
        expected = (b'\x00\x01' + b'\xff' * (len(clear_bytes) - len(digest_info) - 3)
                    + b'\x00' + digest_info)
        self.assertEqual(clear_bytes, expected)

    def test_service_account_token_is_cached(self):
        calls = []

        def fake_post(url, data):
            calls.append(data)
            return {'access_token': f'tok{len(calls)}', 'expires_in': 3600}

        with patch.object(gcal, 'service_account_file', return_value=self.sa_path), \
             patch.object(gcal, '_post', side_effect=fake_post):
            self.assertEqual(gcal.get_access_token(), 'tok1')
            self.assertEqual(gcal.get_access_token(), 'tok1')  # cachato
            self.assertEqual(gcal.get_access_token(force_refresh=True), 'tok2')
        self.assertEqual(len(calls), 2)

    def test_readiness_prefers_service_account(self):
        with patch.object(config, 'GCAL_ENABLED', True), \
             patch.object(gcal, 'service_account_file', return_value=self.sa_path), \
             patch.dict(os.environ, {'GCAL_CLIENT_ID': '',
                                     'GCAL_CLIENT_SECRET': ''}):
            # anche senza credenziali OAuth: la chiave basta
            self.assertEqual(gcal.readiness(), 'ready')

    def test_service_account_skips_device_flow_tokens(self):
        # con la chiave presente il vecchio file token non viene mai letto
        with patch.object(gcal, 'service_account_file', return_value=self.sa_path), \
             patch.object(gcal, '_load_tokens',
                          side_effect=AssertionError('device flow usato')), \
             patch.object(gcal, '_post', return_value={'access_token': 'tok'}):
            self.assertEqual(gcal.get_access_token(), 'tok')

    def test_list_calendars_returns_shared(self):
        data = {'items': [{'id': 'a@group.calendar.google.com',
                           'summary': 'Personale', 'accessRole': 'writer'},
                          {'id': 'b@group.calendar.google.com',
                           'summary': 'Lavoro', 'accessRole': 'reader'}]}
        with patch.object(gcal, '_api', return_value=data) as api:
            calendars = gcal.list_calendars()
        api.assert_called_once_with('GET', '/users/me/calendarList')
        self.assertEqual([c['id'] for c in calendars],
                         ['a@group.calendar.google.com',
                          'b@group.calendar.google.com'])
        self.assertEqual(calendars[0]['access_role'], 'writer')

    def test_parse_rsa_key_rejects_garbage(self):
        for bad in ('', 'not a pem',
                    '-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----'):
            with self.subTest(bad=bad[:20]), self.assertRaises(ValueError):
                gcal._parse_rsa_private_key(bad)


class MarkerTests(unittest.TestCase):
    def test_extended_reminder_marker(self):
        _, events = gcal.parse_markers(
            '@@EVENTO@@ Paga F24 | 2026-10-20 12:00 | | scadenza | 15 | 0')
        ev = gcal.validate_event(events[0])
        self.assertEqual(ev['duration_min'], 15)
        self.assertEqual(ev['reminder_minutes'], 0)

    def test_boundary_rejects_invalid_values(self):
        for changes in ({'dt': '2026-10-20'}, {'dt': '2026-02-30 10:00'},
                        {'dt': '2020-01-01 10:00'}, {'duration_min': -2},
                        {'reminder_minutes': 40321}, {'reminder_minutes': True},
                        {'title': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                gcal.validate_event(dict(title='X', dt='2026-10-20 12:00') | changes)

    def test_text_without_markers_is_untouched(self):
        text = "Ciao! Come posso aiutarti?"
        clean, events = gcal.parse_markers(text)
        self.assertEqual(clean, text)
        self.assertEqual(events, [])

    def test_single_marker(self):
        text = "Fatto!\n@@EVENTO@@ Dentista | 2026-10-15 15:00 | Studio Rossi | radiografia"
        clean, events = gcal.parse_markers(text)
        self.assertEqual(clean, "Fatto!")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], {"title": "Dentista", "dt": "2026-10-15 15:00",
                                     "location": "Studio Rossi", "notes": "radiografia"})

    def test_multiple_markers_and_optional_fields(self):
        text = "@@EVENTO@@ A | 2026-10-15 10:00\n@@EVENTO@@ B | 2026-10-16 11:00 | Roma"
        clean, events = gcal.parse_markers(text)
        self.assertEqual(clean, "")
        self.assertEqual([(e["title"], e["location"]) for e in events],
                         [("A", ""), ("B", "Roma")])

    def test_invalid_or_missing_datetime(self):
        for payload in ("Cena | domani sera", "Solo titolo", ""):
            with self.subTest(payload=payload):
                _, events = gcal.parse_markers(f"@@EVENTO@@ {payload}")
                self.assertEqual(len(events), 1)
                self.assertIn("error", events[0])


class ApiTests(unittest.TestCase):
    def test_create_event_builds_rfc3339_body(self):
        with patch.object(gcal, '_api',
                          return_value={'id': 'evt1', 'htmlLink': 'L'}) as api:
            res = gcal.create_event('Dentista', '2026-10-15 15:00',
                                    location='Studio', notes='porta la ricevuta',
                                    duration_min=30, source='test')
        self.assertTrue(res['ok'])
        self.assertEqual(res['event_id'], 'evt1')
        body = api.call_args.kwargs['body']
        self.assertEqual(body['summary'], 'Dentista')
        self.assertEqual(body['location'], 'Studio')
        self.assertTrue(body['start']['dateTime'].startswith('2026-10-15T15:00:00'))
        self.assertTrue(body['end']['dateTime'].startswith('2026-10-15T15:30:00'))
        self.assertEqual(body['start']['timeZone'], config.TIMEZONE)
        self.assertIn('Registrato da: test', body['description'])
        self.assertEqual(body['reminders'], {'useDefault': False,
                         'overrides': [{'method': 'popup', 'minutes': 30}]})

    def test_explicit_reminder_and_api_failure(self):
        with patch.object(gcal, '_api', side_effect=RuntimeError('offline')) as api:
            res = gcal.create_event('Ricorda', '2026-10-20 12:00', reminder_minutes=0)
        self.assertFalse(res['ok'])
        self.assertEqual(api.call_args.kwargs['body']['reminders']['overrides'][0]['minutes'], 0)

    def test_create_event_rejects_bad_date(self):
        res = gcal.create_event('X', 'domani')
        self.assertFalse(res['ok'])
        self.assertIn('data non valida', res['error'])

    def test_upcoming_events_parses_timed_and_all_day(self):
        data = {'items': [
            {'id': '1', 'summary': 'Visita',
             'start': {'dateTime': '2026-10-15T15:00:00+02:00'}, 'location': 'Roma'},
            {'id': '2', 'summary': 'Compleanno', 'start': {'date': '2026-10-16'}},
        ]}
        with patch.object(config, 'TIMEZONE', 'Europe/Rome'), \
             patch.object(gcal, '_api', return_value=data):
            events = gcal.upcoming_events(days=7)
        self.assertEqual(events[0]['dt'], '2026-10-15 15:00')
        self.assertFalse(events[0]['all_day'])
        self.assertEqual(events[1]['dt'], '2026-10-16')
        self.assertTrue(events[1]['all_day'])

    def test_expired_token_is_refreshed(self):
        # device flow: isola dalla chiave service account reale in data/
        with patch.object(gcal, '_load_service_account', return_value={}), \
             patch.object(gcal, '_load_tokens',
                          return_value={'access_token': 'old', 'refresh_token': 'r',
                                        'expires_at': 100.0}), \
             patch.object(gcal, '_save_tokens') as save, \
             patch.object(gcal, '_post',
                          return_value={'access_token': 'new', 'expires_in': 3600}) as post:
            self.assertEqual(gcal.get_access_token(), 'new')
        post.assert_called_once()
        save.assert_called_once()

    def test_readiness_states(self):
        # isola dalla chiave service account reale in data/
        with patch.object(gcal, '_load_service_account', return_value={}):
            with patch.object(config, 'GCAL_ENABLED', False):
                self.assertEqual(gcal.readiness(), 'config_disabled')
            with patch.object(config, 'GCAL_ENABLED', True), \
                 patch.dict(os.environ, {'GCAL_CLIENT_ID': 'id',
                                         'GCAL_CLIENT_SECRET': 'secret'}), \
                 patch.object(gcal, 'token_file') as tf:
                tf.return_value.is_file.return_value = False
                self.assertEqual(gcal.readiness(), 'token_missing')
                tf.return_value.is_file.return_value = True
                self.assertEqual(gcal.readiness(), 'ready')
            with patch.object(config, 'GCAL_ENABLED', True), \
                 patch.dict(os.environ, {'GCAL_CLIENT_ID': '',
                                         'GCAL_CLIENT_SECRET': ''}):
                self.assertEqual(gcal.readiness(), 'credentials_missing')

    def test_authorize_reads_credentials_written_by_setenv(self):
        # regressione: config.set_env aggiorna os.environ ma non gli
        # attributi del modulo config; authorize deve leggere le
        # credenziali al volo per vedere quelle appena scritte dal setup
        stored = {'access_token': 'tok', 'refresh_token': 'r',
                  'expires_at': 4102444800.0}
        with patch.object(config, 'GCAL_CLIENT_ID', ''), \
             patch.object(config, 'GCAL_CLIENT_SECRET', ''), \
             patch.dict(os.environ, {'GCAL_CLIENT_ID': 'cid',
                                     'GCAL_CLIENT_SECRET': 'csec'}), \
             patch.object(gcal, '_store', return_value=stored), \
             patch.object(gcal.time, 'sleep'), \
             patch.object(gcal, '_post') as post:
            post.side_effect = [
                {'device_code': 'dev', 'user_code': 'ABC-DEF',
                 'verification_url': 'https://www.google.com/device',
                 'interval': 0, 'expires_in': 60},
                {'access_token': 'tok', 'expires_in': 3600},
            ]
            entry = gcal.authorize(force=True)
        self.assertEqual(entry, stored)
        first, second = post.call_args_list
        self.assertEqual(first.args[1], {'client_id': 'cid', 'scope': gcal.SCOPES})
        self.assertEqual(second.args[1], {
            'client_id': 'cid', 'client_secret': 'csec',
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'device_code': 'dev'})

    def test_is_active_requires_flow_switch(self):
        with patch.object(config, 'GCAL_ENABLED', True), \
             patch.object(gcal, 'readiness', return_value='ready'), \
             patch.object(gcal.flows, 'enabled', return_value=False):
            self.assertFalse(gcal.is_active())
        with patch.object(config, 'GCAL_ENABLED', True), \
             patch.object(gcal, 'readiness', return_value='ready'), \
             patch.object(gcal.flows, 'enabled', return_value=True):
            self.assertTrue(gcal.is_active())


class ChatAppointmentTests(unittest.TestCase):
    def test_invalid_incoming_deadline_never_writes(self):
        verdict = {'summary': 's', 'category': 'lavoro', 'priority': 'bassa',
                   'appointments': [{'title': 'Scadenza', 'datetime': '2026-10-20'}]}
        with patch.object(main.db, 'store_message', return_value=1), \
             patch.object(main.db, 'log_communication'), \
             patch.object(main.brain, 'analyze', return_value=verdict), \
             patch.object(main.db, 'add_appointment') as add, \
             patch.object(main.gcal, 'create_event') as create, \
             patch.object(main.telegram, 'notify'):
            main._legacy_process_message('email', 'Marco', '', 'scade il 20 ottobre', {})
        add.assert_not_called()
        create.assert_not_called()
        self.assertFalse(verdict['appointment_results'][0]['ok'])

    def test_analysis_prompt_has_aware_reference_and_deadline_policy(self):
        with patch.object(brain, '_chat', return_value='{"appointments": []}') as chat, \
             patch.object(brain, '_rules_block', return_value=''), \
             patch.object(brain, '_entities_block', return_value=''):
            brain.analyze('Pagamento', 'Marco', 'Scade domani', 'email')
        system, context = chat.call_args.args
        self.assertIn('scadenze', system)
        self.assertIn('NON inventare data o ora', system)
        self.assertIn('newsletter', system)
        self.assertIn('ADESSO:', context)
        self.assertIn(config.TIMEZONE, context)
        self.assertRegex(context, r'\d{2}:\d{2}:\d{2}.*[+-]\d{2}:\d{2}')

    def test_calendar_failure_replaces_model_success_and_history(self):
        reply = 'Fatto, salvato!\n@@EVENTO@@ Pagamento | 2026-10-20 12:00 | | | 15 | 0'
        with patch.object(main.brain, 'chat_with_user', return_value=reply), \
             patch.object(main.gcal, 'is_active', return_value=True), \
             patch.object(main.gcal, 'create_event', side_effect=RuntimeError('offline')), \
             patch.object(main.db, 'add_appointment'), \
             patch.object(main.telegram, 'notify') as send:
            main.handle_user_chat('ricordami il pagamento')
        text = send.call_args.args[0]
        self.assertNotIn('Fatto', text)
        self.assertIn('non ancora confermata', text)
        self.assertEqual(main._chat_history[-1]['content'], text)

    def setUp(self):
        from datetime import datetime as RealDateTime
        class FrozenDateTime(RealDateTime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 10, 14, 12, tzinfo=ZoneInfo(config.TIMEZONE))
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)
        from zoneinfo import ZoneInfo
        for name, kwargs in (('connectors.gcal.datetime', {'new':FrozenDateTime}),
                     ('main.db._conn', {}), ('pipeline.save_event', {'return_value':1})):
            p = patch(name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        switch = patch.object(main.flows, 'enabled', return_value=True)
        switch.start()
        self.addCleanup(switch.stop)

    def test_marker_creates_db_and_calendar_event(self):
        reply = "Fatto!\n@@EVENTO@@ Dentista | 2026-10-15 15:00 | Studio Rossi"
        with patch.object(main.brain, 'chat_with_user', return_value=reply), \
             patch.object(main.gcal, 'is_active', return_value=True), \
             patch.object(main.gcal, 'create_event',
                          return_value={'ok': True, 'event_id': 'e1'}) as create, \
             patch.object(main.db, 'add_appointment', return_value=1) as add, \
             patch.object(main.telegram, 'notify') as send:
            main.handle_user_chat('mettimi un appuntamento domani dal dentista')
        add.assert_not_called()
        create.assert_not_called()
        text = send.call_args[0][0]
        self.assertNotIn('@@EVENTO@@', text)
        self.assertIn('sincronizzazione Calendar in coda', text)

    def test_marker_without_calendar_saves_local_agenda(self):
        reply = "Ok\n@@EVENTO@@ Cena | 2026-10-15 20:00"
        with patch.object(main.brain, 'chat_with_user', return_value=reply), \
             patch.object(main.gcal, 'is_active', return_value=False), \
             patch.object(main.gcal, 'create_event') as create, \
             patch.object(main.db, 'add_appointment', return_value=1) as add, \
             patch.object(main.telegram, 'notify') as send:
            main.handle_user_chat('ricordimi la cena')
        create.assert_not_called()
        add.assert_not_called()
        self.assertIn('salvato localmente', send.call_args[0][0])

    def test_invalid_marker_warns_user(self):
        reply = "Fatto!\n@@EVENTO@@ Cena | domani sera"
        with patch.object(main.brain, 'chat_with_user', return_value=reply), \
             patch.object(main.db, 'add_appointment') as add, \
             patch.object(main.telegram, 'notify') as send:
            main.handle_user_chat('mettimi la cena')
        add.assert_not_called()
        self.assertIn('Non sono riuscito', send.call_args[0][0])

    def test_detected_appointments_sync_to_calendar(self):
        verdict = {'summary': 's', 'category': 'lavoro', 'priority': 'bassa',
                   'needs_action': False, 'action': '', 'needs_reply': False,
                   'reply_hint': '',
                   'appointments': [{'title': 'Scadenza pagamento', 'datetime': '2026-10-16 10:00',
                                     'location': '', 'notes': '', 'duration_min': 15,
                                     'reminder_minutes': 0}],
                   'is_invoice': False, 'invoice_info': {}}
        with patch.object(main.db, 'store_message', return_value=42), \
             patch.object(main.db, 'communication_seen', return_value=False), \
             patch.object(main.db, 'log_communication', return_value=1), \
             patch.object(main.db, 'add_appointment', return_value=1), \
             patch.object(main.brain, 'analyze', return_value=verdict), \
             patch.object(main.gcal, 'is_active', return_value=True), \
             patch.object(main.gcal, 'create_event', return_value={'ok': True}) as create, \
             patch.object(main.telegram, 'notify'):
            main._legacy_process_message('email (test)', 'mittente', 'oggetto', 'testo', {})
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs['reminder_minutes'], 0)
        self.assertTrue(verdict['appointment_results'][0]['ok'])

    def test_appointments_command_prefers_calendar(self):
        events = [{'dt': '2026-10-15 15:00', 'title': 'Dentista',
                   'location': 'Studio', 'all_day': False, 'event_id': '1'}]
        with patch.object(main.gcal, 'is_active', return_value=True), \
               patch.object(main.db, 'upcoming_appointments', return_value=[]), \
             patch.object(main.gcal, 'upcoming_events', return_value=events), \
             patch.object(main.telegram, 'notify') as send:
            self.assertTrue(main._handle_user_command('/appuntamenti'))
        text = send.call_args[0][0]
        self.assertIn('Google Calendar', text)
        self.assertIn('Dentista', text)

    def test_appointments_command_falls_back_to_db(self):
        appts = [{'id': 1, 'dt': '2026-10-15 09:00', 'title': 'X',
                  'location': '', 'notes': ''}]
        with patch.object(main.gcal, 'is_active', return_value=False), \
             patch.object(main.db, 'upcoming_appointments', return_value=appts) as up, \
             patch.object(main.telegram, 'notify') as send:
            self.assertTrue(main._handle_user_command('/appuntamenti'))
        up.assert_called_once()
        self.assertIn('X', send.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
