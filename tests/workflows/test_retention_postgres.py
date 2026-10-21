"""Retention su PostgreSQL reale (opt-in): potatura, protezioni, file, job, ledger.

Opt-in: P_SECRETARY_TEST_DATABASE_URL con dbname che finisce in _test (mai produzione).
Schema isolato per test, db.init() reale con AGENT_DB_ROLE vuoto (nessun ruolo globale),
DATA_DIR in una cartella temporanea, nessuna rete, nessun worker.
"""
import json
import os
import shutil
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from secretary.core import config, db, jobs
from secretary.workflows import retention

URL = os.environ.get('P_SECRETARY_TEST_DATABASE_URL', '')
OLD = 800  # giorni: oltre RETENTION_MESSAGES_DAYS=730
OLD_JOB = 200  # giorni: oltre RETENTION_OPERATIONAL_DAYS=90


@unittest.skipUnless(URL, 'Set P_SECRETARY_TEST_DATABASE_URL to opt in')
class RetentionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(URL)
        if not params.get('dbname', '').endswith('_test') or params.get('service'):
            raise ValueError('An explicit database ending in _test (no service) is required')

    def setUp(self):
        from psycopg import sql
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('HTTP forbidden')))
        self.enterContext(patch.object(jobs.Worker, 'start', side_effect=AssertionError('Workers forbidden')))
        self.schema = 'retention_' + uuid.uuid4().hex[:16]
        self.views = 'retention_v_' + uuid.uuid4().hex[:12]
        self.conn = db.psycopg.connect(URL, autocommit=True, prepare_threshold=None, connect_timeout=5)
        self.addCleanup(self.conn.close)
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop)
        self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        self.conn.execute("SET timezone='UTC'")
        self.main = None
        self.enterContext(patch.object(db, '_conn', self.connection))
        self.enterContext(patch.object(db, 'available', return_value=True))
        self.enterContext(patch.object(config, 'AGENT_DB_SCHEMA', self.views))
        self.enterContext(patch.object(config, 'AGENT_DB_ROLE', ''))
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data, True)
        for folder in ('email_attachments', 'tg_files', 'wa_media'):
            (self.data / folder).mkdir()
        self.enterContext(patch.object(config, 'DATA_DIR', self.data))
        self.enterContext(patch.object(config, 'RETENTION_MESSAGES_DAYS', 730))
        self.enterContext(patch.object(config, 'RETENTION_OPERATIONAL_DAYS', 90))
        self.enterContext(patch.object(config, 'RETENTION_AI_USAGE_DAYS', 365))
        db.init()

    def drop(self):
        from psycopg import sql
        self.conn.execute('SET search_path TO pg_catalog')
        self.conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(self.views)))
        self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    @contextmanager
    def connection(self):
        """Stessa connessione autocommit del test: nessuna chiusura tra i lotti."""
        yield self.conn

    def scalar(self, query, params=()):
        return self.conn.execute(query, params).fetchone()[0]

    # ----------------------------------------------------------- dati
    def native(self, table, channel, *, days=OLD, body='corpo'):
        mid = self.conn.execute(f"""INSERT INTO {table}(ts,day,channel,sender,subject,body,source_id,account,metadata)
            VALUES (now() - (%s * interval '1 day'), (now() - (%s * interval '1 day'))::date,
                    %s, 'mittente', 'oggetto', %s, %s, 'acc', '{{}}'::jsonb) RETURNING id""",
            (days, days, channel, body, 'src-' + uuid.uuid4().hex)).fetchone()[0]
        self.conn.execute(f"UPDATE {table} SET acquired_at = now() - (%s * interval '1 day') WHERE id=%s",
                          (days, mid))
        return mid

    def attachment(self, mid, channel, path):
        return self.conn.execute("""INSERT INTO attachments(message_id,channel,filename,content_type,path,metadata)
            VALUES (%s,%s,'f.pdf','application/pdf',%s,'{}'::jsonb) RETURNING id""",
            (mid, channel, str(path))).fetchone()[0]

    def knowledge(self, mid, attachment_id, path):
        run = self.conn.execute("""INSERT INTO knowledge_runs(message_id,revision,state,attachment_count)
            VALUES (%s,'r1','indexed',1) RETURNING id""", (mid,)).fetchone()[0]
        doc = self.conn.execute("INSERT INTO knowledge_documents(sha256,byte_size) VALUES (%s,10) RETURNING id",
                                (uuid.uuid4().hex,)).fetchone()[0]
        occurrence = self.conn.execute("""INSERT INTO knowledge_occurrences(run_id,attachment_id,document_id,
            filename,content_type,storage,original_path,availability)
            VALUES (%s,%s,%s,'f.pdf','application/pdf','email_attachments',%s,'available') RETURNING id""",
            (run, attachment_id, doc, str(path))).fetchone()[0]
        extraction = self.conn.execute("""INSERT INTO knowledge_extractions(document_id,extractor_version,format,status)
            VALUES (%s,'v1','pdf','ok') RETURNING id""", (doc,)).fetchone()[0]
        self.conn.execute("""INSERT INTO knowledge_entries(run_id,occurrence_id,extraction_id,source_type,source_key,
            message_id,channel,title,text,ts,acquired_at,historical,status,source_ref)
            VALUES (%s,%s,%s,'document',1,%s,'email (test)','t','testo estratto',now(),now(),false,'ok','{}'::jsonb)""",
            (run, occurrence, extraction, mid))
        return doc

    def job(self, kind, key, payload, status='done', days=OLD_JOB):
        return self.conn.execute("""INSERT INTO work_jobs(kind,key,payload,status,created_at,finished_at)
            VALUES (%s,%s,%s::jsonb,%s,now() - (%s * interval '1 day'),
                    CASE WHEN %s='done' THEN now() - (%s * interval '1 day') END) RETURNING id""",
            (kind, key, json.dumps(payload), status, days, status, days)).fetchone()[0]

    def file(self, folder, name, days=OLD):
        path = self.data / folder / name
        path.write_bytes(b'x')
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))
        return path

    # ----------------------------------------------------------- test
    def test_prunes_only_expired_unprotected_history_and_its_files(self):
        # File: referenziato da A (potato) e F (recente) -> resta.
        sha_a = self.file('email_attachments', 'shaA')
        sha_s = self.file('email_attachments', 'shaS')
        sha_old_host = self.file('email_attachments', 'shaOLD')
        orphan = self.file('email_attachments', 'orphan')
        orphan_new = self.file('email_attachments', 'orphan_new', days=0)
        wa_file = self.file('wa_media', 'w1.jpg')
        tg_kept = self.file('tg_files', 'keep.pdf')

        # A: scaduto, nulla lo protegge -> potato con allegati, analisi e derivati.
        a = self.native('email_messages', 'email (test)')
        att_a = self.attachment(a, 'email (test)', sha_a)
        self.attachment(a, 'email (test)', sha_s)
        self.conn.execute("INSERT INTO message_analysis(message_id,channel,summary) VALUES (%s,'email','s')", (a,))
        doc_a = self.knowledge(a, att_a, sha_a)
        self.job('analysis', f'analysis:{a}', {'message_id': a})

        # B: attività aperta che nasce dal messaggio -> protetto.
        b = self.native('email_messages', 'email (test)')
        self.conn.execute("INSERT INTO tasks(source_message,title,status) VALUES (%s,'attività','open')", (b,))

        # C: job pendente sul messaggio -> protetto; il suo file resta.
        c = self.native('telegram_messages', 'telegram (account)')
        self.attachment(c, 'telegram (account)', tg_kept)
        self.job('analysis', f'analysis:{c}', {'message_id': c}, status='pending')

        # D: scaduto, WhatsApp con media nella cartella wa_media -> potato, file rimosso.
        d = self.native('whatsapp_messages', 'whatsapp')
        self.attachment(d, 'whatsapp', wa_file)

        # F: recente, usa ancora shaS -> il file resta.
        f = self.native('email_messages', 'email (test)', days=1)
        self.attachment(f, 'email (test)', sha_s)

        # G: nota con provenienza, allegato con percorso di un vecchio host -> protetto, file vivo.
        g = self.native('email_messages', 'email (test)')
        self.conn.execute("INSERT INTO notes(text,source_message) VALUES ('nota', %s)", (g,))
        self.attachment(g, 'email (test)', f'/old/host/data/email_attachments/{sha_old_host.name}')

        # H: ricevuta di effetto incerta -> protetto (non si pota una riconciliazione aperta).
        h = self.native('email_messages', 'email (test)')
        self.conn.execute("""INSERT INTO effect_receipts(key,message_id,effect,payload_hash,status)
            VALUES (%s,%s,'notify','h','uncertain')""", ('r-' + uuid.uuid4().hex, h))

        # Operativi: job concluso vecchio (payload compattato), recente (intatto).
        done_old = self.job('analysis', f'analysis:{a}-done', {'message_id': a, 'text': 'segreto'})
        done_recent = self.job('analysis', f'analysis:{f}', {'message_id': f}, days=1)
        self.conn.execute("""INSERT INTO agent_email_outbox(account,recipient,subject,body,state,created_at,sent_at)
            VALUES ('a','r','s','b','sent', now() - interval '200 days', now() - interval '200 days')""")
        self.conn.execute("""INSERT INTO agent_email_outbox(account,recipient,subject,body,state,created_at)
            VALUES ('a','r','s','b','proposed', now() - interval '200 days')""")
        self.conn.execute("""INSERT INTO secretary_ai_usage(id,usage_day,job_id,job_token,reserved_tokens,charged_tokens,state)
            VALUES ('old-done', (now() AT TIME ZONE 'UTC')::date - 400, 1, 't', 10, 10, 'completed')""")
        self.conn.execute("""INSERT INTO secretary_ai_usage(id,usage_day,job_id,job_token,reserved_tokens,charged_tokens,state)
            VALUES ('old-reserved', (now() AT TIME ZONE 'UTC')::date - 400, 2, 't', 10, 10, 'reserved')""")
        jobs_before = self.scalar('SELECT count(*) FROM work_jobs')

        report = retention.run()

        self.assertTrue(report['complete'])
        self.assertEqual(report['messages'], 2)  # A e D
        self.assertEqual(self.scalar('SELECT count(*) FROM email_messages WHERE id=%s', (a,)), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM whatsapp_messages WHERE id=%s', (d,)), 0)
        for kept in (b, c, f, g, h):
            table = 'telegram_messages' if kept == c else 'whatsapp_messages' if kept == d else 'email_messages'
            with self.subTest(kept=kept):
                self.assertEqual(self.scalar(f'SELECT count(*) FROM {table} WHERE id=%s', (kept,)), 1)
        # Derivati e allegati del messaggio potato.
        self.assertEqual(self.scalar('SELECT count(*) FROM attachments WHERE message_id=%s', (a,)), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM message_analysis WHERE message_id=%s', (a,)), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM knowledge_runs WHERE message_id=%s', (a,)), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM knowledge_entries WHERE message_id=%s', (a,)), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM knowledge_documents WHERE id=%s', (doc_a,)), 0)
        # File: rimossi solo se nessuna riga li usa ancora.
        self.assertFalse(sha_a.exists())  # solo A lo usava
        self.assertTrue(sha_s.exists())  # ancora referenziato da F
        self.assertTrue(sha_old_host.exists())  # referenziato da G con percorso di un altro host
        self.assertFalse(orphan.exists())  # orfano vecchio
        self.assertTrue(orphan_new.exists())  # orfano recente: dentro la grazia
        self.assertFalse(wa_file.exists())  # media di D
        self.assertTrue(tg_kept.exists())  # ancora referenziato da C
        self.assertEqual(report['files'], 3)  # shaA, orphan, w1.jpg
        # Job: la riga e la chiave restano, il payload del concluso vecchio è vuoto.
        self.assertEqual(self.scalar('SELECT payload FROM work_jobs WHERE id=%s', (done_old,)), {})
        self.assertEqual(self.scalar('SELECT key FROM work_jobs WHERE id=%s', (done_old,)),
                         f'analysis:{a}-done')
        self.assertEqual(self.scalar('SELECT payload FROM work_jobs WHERE id=%s', (done_recent,)),
                         {'message_id': f})
        self.assertEqual(self.scalar('SELECT count(*) FROM work_jobs'), jobs_before)
        self.assertEqual(report['job_payloads'], 2)  # done_old e il job di A
        # Operativi: outbox terminale e ledger completato spariscono; proposta e in attesa restano.
        self.assertEqual(self.scalar("SELECT count(*) FROM agent_email_outbox WHERE state='sent'"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM agent_email_outbox WHERE state='proposed'"), 1)
        self.assertEqual(report['outbox'], 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM secretary_ai_usage WHERE id='old-done'"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM secretary_ai_usage WHERE id='old-reserved'"), 1)
        self.assertEqual(report['ai_usage'], 1)
        # Identità di deduplica e ricevute non vengono mai potate.
        self.assertEqual(self.scalar('SELECT count(*) FROM effect_receipts'), 1)

        # Seconda passata: nulla da fare.
        again = retention.run()
        self.assertEqual((again['messages'], again['files'], again['complete']), (0, 0, True))

    def test_disabled_classes_change_nothing(self):
        old = self.native('email_messages', 'email (test)')
        sha = self.file('email_attachments', 'x')
        self.attachment(old, 'email (test)', sha)
        with patch.object(config, 'RETENTION_MESSAGES_DAYS', 0), \
                patch.object(config, 'RETENTION_OPERATIONAL_DAYS', 0), \
                patch.object(config, 'RETENTION_AI_USAGE_DAYS', 0):
            report = retention.run()
        self.assertEqual((report['messages'], report['files'], report['job_payloads']), (0, 0, 0))
        self.assertEqual(self.scalar('SELECT count(*) FROM email_messages WHERE id=%s', (old,)), 1)
        self.assertTrue(sha.exists())

    def test_legacy_relations_stop_message_pruning_but_not_housekeeping(self):
        old = self.native('email_messages', 'email (test)')
        self.conn.execute('CREATE TABLE secretary_links (id BIGINT)')
        report = retention.run()
        self.assertEqual(report['skipped'], ['legacy_tables'])
        self.assertEqual(self.scalar('SELECT count(*) FROM email_messages WHERE id=%s', (old,)), 1)

    def test_recent_messages_and_their_fresh_files_survive(self):
        recent = self.native('email_messages', 'email (test)', days=1)
        recent_file = self.file('email_attachments', 'fresh', days=0)
        self.attachment(recent, 'email (test)', recent_file)
        report = retention.run()
        self.assertEqual(report['messages'], 0)  # non è ancora scaduto
        self.assertEqual(report['files'], 0)
        self.assertTrue(recent_file.exists())

    def test_scheduled_pass_runs_once_per_day_with_a_persisted_stamp(self):
        old = self.native('email_messages', 'email (test)')
        first = retention.run_scheduled()
        self.assertEqual(first['messages'], 1)
        self.assertEqual(self.scalar('SELECT count(*) FROM email_messages WHERE id=%s', (old,)), 0)
        stamp = self.scalar("SELECT value FROM source_cursors WHERE key='retention_pass'")
        self.assertTrue(stamp['complete'])
        self.assertIsNone(retention.run_scheduled())  # stesso giorno e completata: nessuna nuova passata

    def test_retention_indexes_are_created_once(self):
        index = "SELECT count(*) FROM pg_indexes WHERE schemaname=%s AND indexname=%s"
        self.assertEqual(self.scalar(index, (self.schema, 'email_messages_acquired')), 1)
        retention.migrate(self.conn)  # idempotente
        self.assertEqual(self.scalar(index, (self.schema, 'effect_receipts_message')), 1)


if __name__ == '__main__':
    unittest.main()
