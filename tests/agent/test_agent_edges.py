"""Confini dell'agente testati offline: web, posta live (IMAP finto), prompt, bozze email.

Nessuna rete e nessun database: le funzioni di rete ricevono un `post`/IMAP finto e
le letture DB passano da una connessione finta. Le garanzie di sicurezza non stanno
nei prompt: qui si verifica che stiano nel codice (validazioni, sola lettura, consenso).
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from secretary.core import config
from secretary.agent import mail, outbox, prompts, web
from secretary.connectors import email as mailbox


class FakeConn:
    """Connessione DB finta: risponde per tabella e registra le istruzioni."""

    def __init__(self, notes=(), rules=(), policy=None):
        self.notes, self.rules, self.policy = list(notes), list(rules), policy
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        cursor = MagicMock()
        if 'FROM notes' in sql:
            cursor.fetchall.return_value = self.notes
        elif 'FROM rules' in sql:
            cursor.fetchall.return_value = self.rules
        elif 'data_policies' in sql:
            cursor.fetchone.return_value = self.policy
        elif 'agent_email_outbox' in sql:
            cursor.fetchone.return_value = (41,)
        return cursor

    def transaction(self):
        return self


def reply(content='Risposta', annotations=(), cost=0.002):
    return {'choices': [{'message': {'content': content, 'annotations': list(annotations)}}],
            'usage': {'cost': cost}}


def citation(url, title='Titolo'):
    return {'type': 'url_citation', 'url_citation': {'url': url, 'title': title}}


class WebSearchTests(unittest.TestCase):
    def search(self, query='tasso BCE oggi', response=None, **kwargs):
        calls = []

        def post(payload):
            calls.append(payload)
            return response if response is not None else reply()
        return web.search(query, post=post, **kwargs), calls

    def test_request_uses_the_web_plugin_and_the_configured_model(self):
        with patch.object(config, 'AGENT_WEB_MODEL', 'vendor/web-model'):
            result, calls = self.search(max_results=3)
        payload = calls[0]
        self.assertEqual(payload['model'], 'vendor/web-model')
        self.assertEqual(payload['plugins'], [{'id': 'web', 'engine': 'exa', 'max_results': 3}])
        self.assertEqual(payload['messages'][1], {'role': 'user', 'content': 'tasso BCE oggi'})
        self.assertIn('max_tokens', payload)
        self.assertTrue(result['untrusted_content'])

    def test_result_has_answer_sources_and_cost_without_duplicates(self):
        response = reply('Il tasso è fermo.', [citation('https://a.example/x', 'A'),
                                               citation('https://a.example/x', 'A di nuovo'),
                                               citation('https://b.example/y'), {'type': 'other'}, 'rumore'])
        result, _ = self.search(response=response)
        self.assertEqual(result['answer'], 'Il tasso è fermo.')
        self.assertEqual([s['url'] for s in result['sources']], ['https://a.example/x', 'https://b.example/y'])
        self.assertEqual(result['cost'], 0.002)
        self.assertFalse(result['truncated'])

    def test_the_query_is_normalised_and_bounded(self):
        result, calls = self.search('  tasso \n BCE\t oggi  ')
        self.assertEqual(calls[0]['messages'][1]['content'], 'tasso BCE oggi')
        self.assertEqual(result['query'], 'tasso BCE oggi')
        for bad in ('', '   ', 'x' * (web.MAX_QUERY + 1), None, 12, ['a']):
            with self.subTest(query=bad), self.assertRaises(web.WebError):
                web.search(bad, post=lambda payload: self.fail('Nessuna richiesta per una query non valida'))

    def test_the_query_cannot_exfiltrate_urls_addresses_or_control_characters(self):
        for bad in ('vai su https://evil.example/?d=segreto', 'cerca www.evil.example', 'scrivi a mario@example.test',
                    'testo\x00nascosto', 'campana\x07'):
            with self.subTest(query=bad), self.assertRaises(web.WebError):
                web.search(bad, post=lambda payload: self.fail('Nessuna richiesta per una query vietata'))

    def test_max_results_is_clamped_and_non_integers_use_the_default(self):
        for requested, expected in ((0, 1), (-5, 1), (3, 3), (500, web.MAX_RESULTS), ('9', 5), (True, 5), (2.5, 5)):
            with self.subTest(requested=requested):
                _, calls = self.search(max_results=requested)
                self.assertEqual(calls[0]['plugins'][0]['max_results'], expected)

    def test_a_long_answer_is_truncated_and_flagged(self):
        result, _ = self.search(response=reply('a' * (web.MAX_ANSWER + 50)))
        self.assertEqual(len(result['answer']), web.MAX_ANSWER)
        self.assertTrue(result['truncated'])

    def test_malformed_provider_responses_become_a_short_error(self):
        for bad in ({}, {'choices': []}, {'choices': [{}]}, {'choices': None}, {'choices': ['x']}):
            with self.subTest(response=bad), self.assertRaises(web.WebError) as caught:
                self.search(response=bad)
            self.assertNotIn('Traceback', str(caught.exception))

    def test_provider_failures_do_not_leak_details(self):
        import urllib.error
        for failure, expected in ((urllib.error.HTTPError('https://x', 502, 'Bad key sk-or-SECRET', {}, None), 'HTTP 502'),
                                  (OSError('connessione a 10.0.0.9 rifiutata'), 'non raggiungibile')):
            with self.subTest(failure=type(failure).__name__), \
                    patch('urllib.request.urlopen', side_effect=failure), \
                    patch.object(config, 'OPENROUTER_API_KEY', 'sk-or-SECRET'):
                with self.assertRaises(web.WebError) as caught:
                    web.search('tasso BCE oggi')
            message = str(caught.exception)
            self.assertIn(expected, message)
            self.assertNotIn('SECRET', message)
            self.assertNotIn('10.0.0.9', message)


class FakeImap:
    """IMAP finto che registra i comandi: serve a provare la sola lettura."""

    def __init__(self, select_status='OK', found=b'4 7 9 12'):
        self.select_status, self.found = select_status, found
        self.selected, self.commands, self.logged_out = [], [], False

    def select(self, folder, readonly=False):
        self.selected.append((folder, readonly))
        return self.select_status, [b'1']

    def uid(self, command, *args):
        self.commands.append((command, args))
        if command == 'search':
            return 'OK', [self.found]
        header = (b'1 (UID 9 RFC822.SIZE 2048 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {80}',
                  b'From: =?utf-8?q?Mar=C3=ACa?= <maria@example.test>\r\nSubject: Preventivo\r\n'
                  b'Date: Mon, 05 Oct 2026 10:00:00 +0000\r\n\r\n')
        return 'OK', [header, b')']

    def logout(self):
        self.logged_out = True


ACCOUNT = dict(address='me@example.test', name='Personale', provider='custom',
               imap_host='imap.example.test', imap_port=993, smtp_host='smtp.example.test',
               smtp_port=587, auth='basic', password='segreto-imap')


class MailValidationTests(unittest.TestCase):
    def test_quote_escapes_and_rejects_unsafe_text(self):
        self.assertEqual(mail._quote(' Mario Rossi ', 'sender'), '"Mario Rossi"')
        self.assertEqual(mail._quote('a"b\\c', 'text'), '"a\\"b\\\\c"')
        for bad in ('', '   ', None, 5, 'x' * 121, 'perché', 'riga\nnuova', 'tab\there', 'nul\x00'):
            with self.subTest(value=bad), self.assertRaises(mail.MailError):
                mail._quote(bad, 'text')

    def test_dates_use_the_imap_format_and_reject_everything_else(self):
        self.assertEqual(mail._day('2026-03-04', 'since'), '04-Mar-2026')
        self.assertEqual(mail._day('2026-12-31', 'before'), '31-Dec-2026')
        for bad in ('04/03/2026', '2026-13-01', '', None, 20260304, 'ieri'):
            with self.subTest(value=bad), self.assertRaises(mail.MailError):
                mail._day(bad, 'since')

    def test_folder_names_are_confined(self):
        self.assertEqual(mail._folder('INBOX'), '"INBOX"')
        self.assertEqual(mail._folder('[Gmail]/Sent Mail'), '"[Gmail]/Sent Mail"')
        for bad in ('../etc', 'a/../b', '', 'x' * 81, 'caffè', 'INBOX"; DELETE', 'a\nb', None, 3):
            with self.subTest(folder=bad), self.assertRaises(mail.MailError):
                mail._folder(bad)

    def test_accounts_never_expose_credentials(self):
        with patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT)]):
            listed = mail.accounts()
            self.assertEqual(listed, [dict(address='me@example.test', name='Personale', provider='custom')])
            self.assertNotIn('segreto-imap', str(listed))
            with self.assertRaises(mail.MailError) as caught:
                mail._account('altro@example.test')
        self.assertIn('me@example.test', str(caught.exception))
        self.assertNotIn('segreto-imap', str(caught.exception))

    def test_email_consent_is_required_on_every_call(self):
        for policy, allowed in ((None, False), ((False,), False), ((None,), False), ((True,), True)):
            with self.subTest(policy=policy), patch.object(mail.db, '_conn', return_value=FakeConn(policy=policy)):
                if allowed:
                    mail._consent()
                else:
                    with self.assertRaises(mail.MailError):
                        mail._consent()

    def test_nothing_touches_the_mailbox_without_consent(self):
        with patch.object(mail.db, '_conn', return_value=FakeConn(policy=None)), \
                patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT)]), \
                patch.object(mail, '_open', side_effect=AssertionError('Connessione senza consenso')):
            with self.assertRaises(mail.MailError):
                mail.search('me@example.test', text='preventivo')
            with self.assertRaises(mail.MailError):
                mail.read('me@example.test', '9')


class MailImapTests(unittest.TestCase):
    def setUp(self):
        self.imap = FakeImap()
        for target in (patch.object(mail, '_consent'),
                       patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT)]),
                       patch.object(mail.mailbox, '_connect', return_value=self.imap)):
            target.start()
            self.addCleanup(target.stop)

    def search_criteria(self):
        return [args for command, args in self.imap.commands if command == 'search'][0]

    def test_the_mailbox_is_always_opened_read_only(self):
        mail.search('me@example.test', text='preventivo', folder='INBOX')
        self.assertEqual(self.imap.selected, [('"INBOX"', True)])
        self.assertTrue(self.imap.logged_out)

    def test_criteria_are_built_from_structured_parameters(self):
        mail.search('me@example.test', sender='Mario', subject='Fattura "urgente"', text='totale',
                    since='2026-09-01', before='2026-10-01')
        self.assertEqual(self.search_criteria(), (None, 'FROM', '"Mario"', 'SUBJECT', '"Fattura \\"urgente\\""',
                                                  'TEXT', '"totale"', 'SINCE', '01-Sep-2026', 'BEFORE', '01-Oct-2026'))

    def test_no_filter_means_all_and_hostile_text_cannot_add_commands(self):
        mail.search('me@example.test')
        self.assertEqual(self.search_criteria(), (None, 'ALL'))
        with self.assertRaises(mail.MailError):
            mail.search('me@example.test', text='x"\r\nA2 DELETE INBOX')
        self.assertEqual([c for c, _ in self.imap.commands].count('search'), 1)

    def test_results_come_from_headers_peeked_never_marking_messages_as_read(self):
        found = mail.search('me@example.test', text='preventivo', limit=2)
        fetches = [args for command, args in self.imap.commands if command == 'fetch']
        self.assertEqual(len(fetches), 2)
        for args in fetches:
            self.assertIn('BODY.PEEK', args[1])
            self.assertNotIn('RFC822 ', args[1] + ' ')
        self.assertEqual(found['total_matches'], 4)
        self.assertTrue(found['untrusted_content'])
        self.assertEqual(found['results'][0]['subject'], 'Preventivo')
        self.assertEqual(found['results'][0]['size'], 2048)
        self.assertIn('maria@example.test', found['results'][0]['sender'])
        # i più recenti per primi, entro il limite richiesto
        self.assertEqual([args[0] for args in fetches], [b'12', b'9'])

    def test_limit_is_clamped(self):
        self.imap.found = b' '.join(str(i).encode() for i in range(1, 60))
        found = mail.search('me@example.test', limit=500)
        self.assertEqual(len(found['results']), mail.MAX_RESULTS)
        found = mail.search('me@example.test', limit=0)
        self.assertEqual(len(found['results']), 1)

    def test_missing_folder_is_reported_and_the_connection_closed(self):
        self.imap.select_status = 'NO'
        with self.assertRaises(mail.MailError):
            mail.search('me@example.test', folder='Inesistente')
        self.assertTrue(self.imap.logged_out)

    def test_uid_must_be_a_plain_number(self):
        for bad in ('9; DELETE', '', '１２', 'abc', 9, None, '1' * 13):
            with self.subTest(uid=bad), self.assertRaises(mail.MailError):
                mail.read('me@example.test', bad)
        self.assertEqual(self.imap.commands, [])

    def test_read_peeks_and_bounds_the_body(self):
        raw = (b'From: Maria <maria@example.test>\r\nTo: me@example.test\r\nSubject: Ciao\r\n'
               b'Date: Mon, 05 Oct 2026 10:00:00 +0000\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
               + b'riga di testo\r\n' * 100)
        self.imap.uid = MagicMock(return_value=('OK', [(b'1 (UID 9 BODY[] {99}', raw), b')']))
        message = mail.read('me@example.test', '9', max_chars=300)
        self.imap.uid.assert_called_once_with('fetch', '9', '(BODY.PEEK[])')
        self.assertEqual(len(message['body']), 300)
        self.assertTrue(message['truncated'])
        self.assertGreater(message['body_chars'], 300)
        self.assertTrue(message['untrusted_content'])
        self.assertEqual(message['subject'], 'Ciao')
        self.assertTrue(self.imap.logged_out)

    def test_read_of_a_missing_message_fails_cleanly(self):
        self.imap.uid = MagicMock(return_value=('NO', [None]))
        with self.assertRaises(mail.MailError):
            mail.read('me@example.test', '9')
        self.assertTrue(self.imap.logged_out)


class FakeDraftImap:
    """IMAP finto per le bozze: registra APPEND/STORE e simula i flag."""

    def __init__(self, folders=None, flags=b'1 (FLAGS (\\Draft))', append_status='OK'):
        self.folders = folders if folders is not None else [
            b'(\\HasNoChildren \\Drafts) "/" "Drafts"', b'(\\HasNoChildren) "/" "INBOX"']
        self.flags, self.append_status = flags, append_status
        self.appended, self.stored, self.selected, self.expunged = [], [], [], False
        self.logged_out = False

    def list(self):
        return 'OK', self.folders

    def select(self, folder, readonly=False):
        self.selected.append((folder, readonly))
        return 'OK', [b'1']

    def append(self, folder, flags, date_time, message):
        self.appended.append((folder, flags, message))
        return self.append_status, [b'APPEND completed']

    def uid(self, command, *args):
        if command == 'search':
            return 'OK', [b'12']
        if command == 'fetch':
            return 'OK', [self.flags]
        if command == 'store':
            self.stored.append(args)
            return 'OK', [b'1']
        return 'NO', [None]

    def expunge(self):
        self.expunged = True
        return 'OK', [b'1']

    def logout(self):
        self.logged_out = True


class DraftMailboxTests(unittest.TestCase):
    """La bozza è una VERA bozza IMAP: APPEND con flag \\Draft, mai un invio."""

    def setUp(self):
        self.imap = FakeDraftImap()
        for target in (patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT)]),
                       patch.object(mailbox, '_connect', return_value=self.imap)):
            target.start()
            self.addCleanup(target.stop)

    def test_the_draft_folder_is_resolved_from_the_special_use_flag(self):
        self.assertEqual(mailbox._draft_folder(self.imap), 'Drafts')

    def test_a_known_folder_name_is_used_when_the_server_declares_no_special_use(self):
        self.imap.folders = [b'(\\HasNoChildren) "/" "INBOX"']
        self.assertEqual(mailbox._draft_folder(self.imap), '[Gmail]/Drafts')

    def test_no_draft_folder_is_an_explicit_error_not_a_silent_success(self):
        self.imap.folders = [b'(\\HasNoChildren) "/" "INBOX"']
        self.imap.select = lambda folder, readonly=False: ('NO', [None])
        with self.assertRaises(mailbox.DraftError):
            mailbox._draft_folder(self.imap)

    def test_saving_appends_a_draft_with_the_draft_flag_and_no_send(self):
        result = mailbox.save_draft(ACCOUNT, 'luca@example.test', 'Preventivo', 'Ciao Luca',
                                    in_reply_to='<orig@example.test>',
                                    references='<a@example.test> <orig@example.test>')
        folder, flags, raw = self.imap.appended[0]
        self.assertEqual((folder, flags), ('"Drafts"', r'(\Draft)'))
        message = __import__('email').message_from_bytes(raw)
        self.assertEqual(message['To'], 'luca@example.test')
        self.assertEqual(message['Subject'], 'Preventivo')
        self.assertEqual(message['In-Reply-To'], '<orig@example.test>')
        self.assertEqual(message['References'], '<a@example.test> <orig@example.test>')
        self.assertEqual(message.get_payload().strip(), 'Ciao Luca')
        self.assertEqual(result['folder'], 'Drafts')
        self.assertEqual(result['uid'], '12')
        self.assertTrue(self.imap.logged_out)

    def test_a_refused_append_is_reported(self):
        self.imap.append_status = 'NO'
        with self.assertRaises(mailbox.DraftError):
            mailbox.save_draft(ACCOUNT, 'luca@example.test', 'Oggetto', 'Corpo')

    def test_draft_exists_requires_the_draft_flag(self):
        self.assertTrue(mailbox.draft_exists(ACCOUNT, 'Drafts', '12'))
        self.imap.flags = b'1 (FLAGS (\\Seen))'
        self.assertFalse(mailbox.draft_exists(ACCOUNT, 'Drafts', '12'))
        self.imap.flags = b'1 (FLAGS ())'
        self.assertFalse(mailbox.draft_exists(ACCOUNT, 'Drafts', '12'))

    def test_deleting_only_touches_a_message_that_is_still_a_draft(self):
        self.assertTrue(mailbox.delete_draft(ACCOUNT, 'Drafts', '12'))
        self.assertEqual(self.imap.stored[0][:2], ('12', '+FLAGS'))
        self.assertTrue(self.imap.expunged)
        self.imap.stored.clear()
        self.imap.flags = b'1 (FLAGS (\\Seen))'
        self.assertFalse(mailbox.delete_draft(ACCOUNT, 'Drafts', '12'))
        self.assertEqual(self.imap.stored, [])

    def test_draft_references_are_validated(self):
        for bad in ('', None, 12, 'a\x00b', 'x' * 201):
            with self.subTest(folder=bad), self.assertRaises(mailbox.DraftError):
                mailbox.draft_exists(ACCOUNT, bad, '12')
        for bad in ('', '12; DELETE', None, 12, '１２'):
            with self.subTest(uid=bad), self.assertRaises(mailbox.DraftError):
                mailbox.draft_exists(ACCOUNT, 'Drafts', bad)


class PromptTests(unittest.TestCase):
    def build(self, mode='owner', **kwargs):
        with patch.object(prompts.db, '_conn', return_value=self.conn), \
                patch.object(prompts.fsbox, 'read_text', **self.instructions_file):
            return prompts.system_prompt(mode, **kwargs)

    def setUp(self):
        self.conn = FakeConn()
        self.instructions_file = dict(side_effect=prompts.fsbox.FsError('assente'))

    def test_now_is_written_in_italian_in_the_configured_zone(self):
        moment = datetime(2026, 10, 21, 22, 30, tzinfo=timezone.utc)  # 00:30 del 22 a Roma (CEST)
        with patch.object(config, 'TIMEZONE', 'Europe/Rome'):
            self.assertEqual(prompts.now_text(moment), 'giovedì 22 ottobre 2026, ore 00:30 (Europe/Rome)')
        with patch.object(config, 'TIMEZONE', 'UTC'):
            self.assertEqual(prompts.now_text(moment.astimezone(ZoneInfo('Europe/Rome'))),
                             'mercoledì 21 ottobre 2026, ore 22:30 (UTC)')

    def test_every_mode_declares_untrusted_content_and_its_own_role(self):
        owner = self.build('owner')
        inbox = self.build('inbox')
        digest = self.build('digest', window=('20/10 21:00', '21/10 21:00', '23/10 21:00'))
        for text in (owner, inbox, digest):
            self.assertIn('CONTENUTO DI TERZI', text)
            self.assertIn('agent.', text)  # primer dello schema
        self.assertIn('DIALOGO DIRETTO', owner)
        self.assertIn('POSTA IN ARRIVO', inbox)
        self.assertIn('RIEPILOGO SERALE', digest)
        self.assertIn('23/10 21:00', digest)

    def test_modes_only_mention_the_tools_they_actually_have(self):
        import re
        from secretary.agent import toolkit
        for mode in ('owner', 'inbox', 'digest'):
            text = self.build(mode, window=('a', 'b', 'c'))
            available = set(toolkit.names(mode)) | {'archive_invoice'}
            for tool in set(re.findall(r'\b([a-z]+(?:_[a-z]+)+)\b', text)) & {s.name for s in toolkit.SPECS}:
                if mode == 'inbox' and tool in ('task_change', 'tasks_close_bulk', 'note_save', 'note_forget'):
                    continue  # citati solo per dire che qui NON si usano
                self.assertIn(tool, available, f'{mode}: il prompt cita {tool}, non disponibile')

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build('admin')

    def test_effects_already_done_are_listed_so_a_retry_does_not_repeat_them(self):
        effects = [dict(tool='task_change', result=dict(receipt='Attività #3 creata')),
                   dict(tool='note_save', result=dict(saved=True))]
        text = self.build('owner', effects=effects)
        self.assertIn('EFFETTI GIÀ ESEGUITI', text)
        self.assertIn('task_change: Attività #3 creata', text)
        self.assertIn('note_save: {"saved": true}', text)
        self.assertNotIn('EFFETTI GIÀ ESEGUITI', self.build('owner'))

    def test_memory_has_instructions_rules_facts_and_is_bounded(self):
        self.conn = FakeConn(
            notes=[('instruction', None, 'Avvisami se scrive Anna'), ('fact', 'casa', 'Abito a Torino')]
                  + [('fact', 'misc', 'x' * 900)] * 12,
            rules=[('mittente = banca', 'priorità alta')])
        with patch.object(prompts.db, '_conn', return_value=self.conn), \
                patch.object(prompts.fsbox, 'read_text', **self.instructions_file):
            block = prompts.memory_block()
        self.assertIn('ISTRUZIONI PERMANENTI', block)
        self.assertIn('- Avvisami se scrive Anna', block)
        self.assertIn('SE mittente = banca ALLORA priorità alta', block)
        self.assertIn('casa: Abito a Torino', block)
        self.assertIn('altre note: leggile con db_query su agent.notes', block)
        self.assertLess(len(block), prompts.MAX_NOTES_CHARS + 2500)

    def test_the_instructions_file_is_included_when_present(self):
        self.instructions_file = dict(return_value=dict(text='  Rispondi sempre in modo formale.  '))
        text = self.build('owner')
        self.assertIn('Dal file workspace/ISTRUZIONI.md', text)
        self.assertIn('Rispondi sempre in modo formale.', text)

    def test_nothing_is_added_when_there_is_no_memory(self):
        with patch.object(prompts.db, '_conn', return_value=FakeConn()), \
                patch.object(prompts.fsbox, 'read_text', **self.instructions_file):
            self.assertEqual(prompts.memory_block(), '')

    def test_prompts_ask_for_exact_evidence_and_state_the_owner_gestures(self):
        owner = self.build('owner')
        for fragment in ('FRASE ESATTA', '/conferma ID', '/invia ID', 'PROPOSTA', 'BOZZA'):
            self.assertIn(fragment, owner)
        inbox = self.build('inbox')
        for fragment in ('Sii MOLTO conservativo', 'Una citazione esatta è obbligatoria',
                         'Non puoi chiudere né modificare attività esistenti'):
            self.assertIn(fragment, inbox)


class OutboxValidationTests(unittest.TestCase):
    def test_only_one_bare_ascii_address_is_accepted(self):
        for good in ('luca@example.test', 'luca.rossi+fatture@sub.example.test', "o'brien@example.test"):
            with self.subTest(address=good):
                self.assertEqual(outbox.address(good), good)
        for bad in ('Luca <luca@example.test>', 'luca@example.test, anna@example.test', 'luca@example.test;anna@example.test',
                    'luca @example.test', 'luca@example', '@example.test', 'luca@@example.test', 'luca@example.test\r\nBcc: x@y.test',
                    'lucà@example.test', 'luca@esempio.tést', '', None, 7, ['a@b.test'],
                    'a' * 65 + '@example.test', 'luca@' + 'a' * 250 + '.test'):
            with self.subTest(address=bad), self.assertRaises(outbox.OutboxError):
                outbox.address(bad)

    def test_the_sending_account_is_resolved_by_exact_address_from_the_configuration(self):
        accounts = [dict(ACCOUNT), dict(ACCOUNT, address='lavoro@example.test')]
        with patch.object(config, 'EMAIL_ACCOUNTS', accounts):
            self.assertEqual(outbox.account_for('lavoro@example.test')['address'], 'lavoro@example.test')
            for unknown in ('0', 'Personale', 'ME@example.test', 'altro@example.test'):
                with self.subTest(account=unknown), self.assertRaises(outbox.OutboxError):
                    outbox.account_for(unknown)
        with patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT), dict(ACCOUNT)]):
            with self.assertRaises(outbox.OutboxError):  # ambiguo: due account con lo stesso indirizzo
                outbox.account_for('me@example.test')

    def test_an_account_without_working_smtp_is_refused_without_leaking_secrets(self):
        for broken in (dict(ACCOUNT, smtp_host=''), dict(ACCOUNT, smtp_port='587'), dict(ACCOUNT, auth='none')):
            with self.subTest(account=sorted(k for k in broken if broken[k] != ACCOUNT[k])), \
                    patch.object(config, 'EMAIL_ACCOUNTS', [broken]):
                with self.assertRaises(outbox.OutboxError) as caught:
                    outbox.account_for('me@example.test')
                self.assertNotIn('segreto-imap', str(caught.exception))

    def thread_conn(self, row):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = row
        return conn

    def test_the_thread_comes_from_the_archived_message_never_from_the_model(self):
        conn = self.thread_conn(('Luca <luca@example.test>', 'in',
                                 dict(message_id='<orig@example.test>', references='<a@example.test>')))
        with patch.object(outbox.policy, 'can_read_context', return_value=True):
            self.assertEqual(outbox._thread(conn, 7, 'luca@example.test'),
                             ('<orig@example.test>', '<a@example.test> <orig@example.test>'))
        # Nessun messaggio di origine: nessun thread inventato.
        self.assertEqual(outbox._thread(conn, None, 'luca@example.test'), (None, None))

    def test_the_thread_is_refused_without_consent_or_for_a_different_recipient(self):
        conn = self.thread_conn(('Luca <luca@example.test>', 'in', dict(message_id='<orig@example.test>')))
        with patch.object(outbox.policy, 'can_read_context', return_value=False):
            with self.assertRaises(outbox.OutboxError):
                outbox._thread(conn, 7, 'luca@example.test')
        with patch.object(outbox.policy, 'can_read_context', return_value=True):
            with self.assertRaises(outbox.OutboxError):
                outbox._thread(conn, 7, 'anna@example.test')
        conn = self.thread_conn(('Luca <luca@example.test>', 'out', dict(message_id='<orig@example.test>')))
        with patch.object(outbox.policy, 'can_read_context', return_value=True):
            with self.assertRaises(outbox.OutboxError):
                outbox._thread(conn, 7, 'luca@example.test')

    def test_a_message_without_a_valid_message_id_gets_no_thread(self):
        with patch.object(outbox.policy, 'can_read_context', return_value=True):
            for metadata in ({}, dict(message_id='non-un-id'), dict(message_id='<a@b>\r\nBcc: x@y')):
                conn = self.thread_conn(('luca@example.test', 'in', metadata))
                with self.subTest(metadata=metadata):
                    self.assertEqual(outbox._thread(conn, 7, 'luca@example.test'), (None, None))

    def test_subject_and_body_are_validated_and_trimmed(self):
        self.assertEqual(outbox._clean('  Preventivo  ', '\n Ciao\n '), ('Preventivo', 'Ciao'))
        for subject in ('', '   ', 'riga\ncon a capo', 'Bcc: x\r\nSubject: y', 'x' * (outbox.MAX_SUBJECT + 1), None, 4):
            with self.subTest(subject=subject), self.assertRaises(outbox.OutboxError):
                outbox._clean(subject, 'corpo')
        for body in ('', '  \n ', 'a\x00b', 'x' * (outbox.MAX_BODY + 1), None, 4):
            with self.subTest(body=body), self.assertRaises(outbox.OutboxError):
                outbox._clean('Oggetto', body)

    def test_sends_are_allowed_only_in_active_mode(self):
        for mode, expected in (('observe', False), ('active', True), ('altro', False)):
            with self.subTest(mode=mode), patch.object(config, 'SECRETARY_MODE', mode):
                self.assertEqual(outbox.sends_allowed(), expected)

    def test_proposing_validates_before_touching_the_database(self):
        conn = FakeConn()
        with patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT)]), \
                patch.object(outbox.jobs, 'enqueue', side_effect=AssertionError('Nessuna notifica')):
            for args in (('sconosciuto@example.test', 'luca@example.test', 'Oggetto', 'Corpo'),
                         ('me@example.test', 'Luca <luca@example.test>', 'Oggetto', 'Corpo'),
                         ('me@example.test', 'luca@example.test', 'Due\nrighe', 'Corpo'),
                         ('me@example.test', 'luca@example.test', 'Oggetto', '')):
                with self.subTest(args=args), self.assertRaises(outbox.OutboxError):
                    outbox.propose(conn, *args, proposed_by_message=5)
        self.assertEqual(conn.statements, [])

    def test_a_proposal_is_stored_as_pending_without_any_io(self):
        conn = FakeConn()
        with patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT)]), \
                patch.object(outbox.jobs, 'enqueue', side_effect=AssertionError('Nessuna notifica qui')):
            result = outbox.propose(conn, 'me@example.test', 'luca@example.test', 'Preventivo', 'x' * 2000, 5)
        self.assertEqual(result['draft_id'], 41)
        self.assertEqual(result['state'], 'proposed')
        self.assertEqual(result['draft_state'], 'pending')
        self.assertEqual(len(conn.statements), 1)
        self.assertIn('INSERT INTO agent_email_outbox', conn.statements[0][0])
        self.assertIn("'pending'", conn.statements[0][0])
        self.assertNotIn('email_send', str(conn.statements))

    def publish_conn(self, row):
        """Connessione finta per publish_draft: una riga in lettura, poi gli UPDATE."""
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        conn.transaction.return_value = conn
        conn.execute.return_value.fetchone.return_value = row
        return conn

    def test_publishing_saves_a_real_draft_in_the_mailbox_and_notifies(self):
        conn = self.publish_conn((
            'me@example.test', 'luca@example.test', 'Preventivo', 'x' * 2000, 'pending', None, None,
            None, None, 5))
        queued = []
        with patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT)]), \
                patch.object(outbox.db, '_conn', return_value=conn), \
                patch('secretary.connectors.email.save_draft',
                      return_value=dict(folder='Drafts', uid='12', message_id='<x@y>')) as save, \
                patch.object(outbox.jobs, 'enqueue', side_effect=lambda *a: queued.append(a)):
            result = outbox.publish_draft(41)
        save.assert_called_once()
        self.assertEqual(result['draft_state'], 'saved')
        self.assertEqual(result['folder'], 'Drafts')
        _, kind, key, payload = queued[0][0], queued[0][1], queued[0][2], queued[0][3]
        self.assertEqual((kind, key, payload['type']), ('action', 'email-draft:41', 'notify'))
        self.assertIn('/invia 41', payload['text'])
        self.assertIn('/scarta 41', payload['text'])
        self.assertIn('A: luca@example.test', payload['text'])
        self.assertIn('cartella Bozze', payload['text'])
        self.assertLess(len(payload['text']), 1800)  # anteprima limitata
        self.assertNotIn('email_send', str(queued))

    def test_a_failed_remote_save_is_reported_and_never_blocks_the_local_draft(self):
        conn = self.publish_conn((
            'me@example.test', 'luca@example.test', 'Preventivo', 'Corpo', 'pending', None, None,
            None, None, 5))
        queued = []
        with patch.object(config, 'EMAIL_ACCOUNTS', [dict(ACCOUNT)]), \
                patch.object(outbox.db, '_conn', return_value=conn), \
                patch('secretary.connectors.email.save_draft',
                      side_effect=mailbox.DraftError('Cartella Bozze non trovata')), \
                patch.object(outbox.jobs, 'enqueue', side_effect=lambda *a: queued.append(a)):
            result = outbox.publish_draft(41)
        self.assertEqual(result['draft_state'], 'failed')
        self.assertIn('Non sono riuscito', queued[0][3]['text'])

    def test_publishing_is_idempotent_and_never_repeats_a_saved_draft(self):
        conn = self.publish_conn((
            'me@example.test', 'luca@example.test', 'Preventivo', 'Corpo', 'saved', 'Drafts', '12',
            None, None, 5))
        with patch.object(outbox.db, '_conn', return_value=conn), \
                patch('secretary.connectors.email.save_draft',
                      side_effect=AssertionError('Nessun secondo salvataggio')):
            result = outbox.publish_draft(41)
        self.assertEqual((result['draft_state'], result['folder'], result['uid']), ('saved', 'Drafts', '12'))


if __name__ == '__main__':
    unittest.main()
