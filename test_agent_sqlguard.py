"""La guardia SQL dell'agente è l'ultima difesa prima del database: casi ostili."""
import datetime
import unittest
from decimal import Decimal

from agent import sqlguard


def rejects(case, sql, schema='agent'):
    with case.assertRaises(sqlguard.SqlRejected, msg=sql):
        sqlguard.validate(sql, schema)


class SqlGuardTests(unittest.TestCase):
    def test_accepts_ordinary_read_queries(self):
        for sql in (
            "SELECT count(*) FROM agent.messages WHERE day >= current_date - 3",
            "SELECT channel, count(*) AS n FROM agent.messages GROUP BY 1 ORDER BY 2 DESC LIMIT 10;",
            "SELECT m.id, a.filename FROM agent.messages m JOIN agent.attachments a ON a.message_id = m.id",
            "WITH recent AS (SELECT * FROM agent.messages WHERE ts > now() - interval '2 days') "
            "SELECT source, count(*) FROM recent GROUP BY 1",
            "SELECT id FROM agent.messages WHERE to_tsvector('italian', coalesce(subject,'')||' '||coalesce(body,'')) "
            "@@ websearch_to_tsquery('italian', 'fattura')",
            "SELECT extract(epoch FROM now()), substring(body from 1 for 5), lower(sender), "
            "date_trunc('day', ts) FROM agent.messages",
            "SELECT id, row_number() OVER (PARTITION BY source ORDER BY ts DESC) FROM agent.messages",
            "SELECT * FROM generate_series(1, 5)",
            "SELECT jsonb_build_object('a', 1), current_date, a::text FROM (SELECT 1 AS a) s",
        ):
            self.assertEqual(sqlguard.validate(sql), sql.strip().rstrip(';').strip())

    def test_rejects_multiple_statements_and_non_selects(self):
        for sql in ("SELECT 1; SELECT 2", "SELECT 1; DROP TABLE tasks",
                    "INSERT INTO agent.tasks(title) VALUES ('x')",
                    "UPDATE agent.tasks SET title='x'", "DELETE FROM agent.tasks",
                    "DROP TABLE tasks", "CREATE TABLE x(a int)", "ALTER ROLE a SUPERUSER",
                    "COPY agent.messages TO '/tmp/x'", "SET ROLE postgres", "SHOW all",
                    "EXPLAIN SELECT 1", "VACUUM", "CALL x()", "DO $$ BEGIN END $$",
                    "LISTEN x", "NOTIFY x", "TRUNCATE tasks", "GRANT ALL ON tasks TO a",
                    "BEGIN", "COMMIT", "SELECT 1 FROM agent.messages; SELECT 2 --", ""):
            rejects(self, sql)

    def test_only_the_validated_statement_text_is_returned(self):
        self.assertEqual(sqlguard.validate('SELECT 1 FROM agent.messages; --'),
                         'SELECT 1 FROM agent.messages')
        self.assertEqual(sqlguard.validate('  SELECT 1 FROM agent.messages ;  '),
                         'SELECT 1 FROM agent.messages')

    def test_rejects_writes_hidden_inside_cte(self):
        rejects(self, "WITH d AS (DELETE FROM agent.tasks RETURNING *) SELECT * FROM d")
        rejects(self, "WITH i AS (INSERT INTO agent.tasks(title) VALUES ('x') RETURNING id) SELECT * FROM i")
        rejects(self, "WITH u AS (UPDATE agent.tasks SET title='x' RETURNING id) SELECT * FROM u")

    def test_rejects_into_and_locking(self):
        rejects(self, "SELECT * INTO newtable FROM agent.messages")
        rejects(self, "SELECT * FROM agent.messages FOR UPDATE")
        rejects(self, "SELECT * FROM agent.messages FOR SHARE")

    def test_only_agent_views_are_readable(self):
        for sql in ("SELECT * FROM messages", "SELECT * FROM public.tasks",
                    "SELECT * FROM pg_catalog.pg_class", "SELECT * FROM pg_class",
                    "SELECT * FROM information_schema.tables",
                    "SELECT * FROM email_messages", "SELECT * FROM work_jobs",
                    "SELECT * FROM agent.messages m, work_jobs j",
                    "SELECT * FROM other.messages",
                    "SELECT * FROM (SELECT * FROM public.attachments) s",
                    "SELECT (SELECT count(*) FROM pg_authid)",
                    "SELECT * FROM agent.messages WHERE id IN (SELECT message_id FROM public.attachments)"):
            rejects(self, sql)

    def test_cte_name_cannot_shadow_a_real_table_outside_its_scope(self):
        # Una CTE chiamata come una tabella reale è lecita SOLO se definita qui.
        rejects(self, "SELECT * FROM work_jobs")
        sqlguard.validate("WITH work_jobs AS (SELECT 1 AS a) SELECT * FROM work_jobs")

    def test_rejects_dangerous_and_unlisted_functions(self):
        for sql in ("SELECT pg_sleep(100)", "SELECT pg_read_file('/etc/passwd')",
                    "SELECT pg_ls_dir('/')", "SELECT lo_import('/etc/passwd')",
                    "SELECT set_config('role','postgres',false)", "SELECT current_setting('x')",
                    "SELECT pg_advisory_lock(1)", "SELECT query_to_xml('select 1', true, false, '')",
                    "SELECT dblink('x','y')", "SELECT pg_terminate_backend(1)",
                    "SELECT version()", "SELECT pg_catalog.pg_sleep(1)",
                    "SELECT public.some_udf(1)", "SELECT nextval('message_ids')",
                    "SELECT pg_stat_file('/x')", "SELECT txid_current()",
                    "SELECT * FROM pg_logical_slot_get_changes('a',null,null)"):
            rejects(self, sql)

    def test_rejects_catalog_probing_casts_and_params(self):
        for sql in ("SELECT 'pg_authid'::regclass", "SELECT 'x'::regproc",
                    "SELECT CAST('pg_class' AS regclass)", "SELECT 1::oid",
                    "SELECT $1", "SELECT * FROM agent.messages WHERE id = $1"):
            rejects(self, sql)

    def test_size_and_garbage_limits(self):
        rejects(self, 'SELECT ' + '1,' * 5000 + '1')
        rejects(self, 'SELEC * FRM x')
        rejects(self, 'SELECT 1\x00')
        rejects(self, None)
        rejects(self, 123)

    def test_schema_name_is_configurable(self):
        sqlguard.validate('SELECT * FROM agent_t1.messages', 'agent_t1')
        rejects(self, 'SELECT * FROM agent.messages', 'agent_t1')

    def test_result_shaping_bounds_rows_cells_and_types(self):
        rows = [(i, 'x' * 1000, b'\x00\x01', Decimal('1.5'), datetime.datetime(2026, 10, 20, 9, 0),
                 None, True) for i in range(300)]
        shaped = sqlguard.shape(['a', 'b', 'c', 'd', 'e', 'f', 'g'], rows)
        self.assertTrue(shaped['truncated'])
        self.assertLessEqual(shaped['row_count'], sqlguard.MAX_ROWS)
        first = shaped['rows'][0]
        self.assertLessEqual(len(first[1]), sqlguard.MAX_CELL_CHARS + 80)
        self.assertIn('read_message', first[1])
        self.assertEqual(first[2], '<binario 2 byte>')
        self.assertEqual(first[3], 1.5)
        self.assertEqual(first[4], '2026-10-20T09:00:00')
        self.assertIsNone(first[5])
        self.assertIs(first[6], True)

    def test_result_shaping_caps_total_size(self):
        rows = [('y' * 390,) * 5 for _ in range(150)]
        shaped = sqlguard.shape(['a'] * 5, rows)
        self.assertTrue(shaped['truncated'])
        self.assertLess(sum(len(str(c)) for r in shaped['rows'] for c in r), sqlguard.MAX_RESULT_CHARS + 2000)

    def test_control_characters_are_neutralized(self):
        shaped = sqlguard.shape(['a'], [('ciao\x00\x07\x1b[31mrosso',)])
        self.assertNotIn('\x00', shaped['rows'][0][0])
        self.assertNotIn('\x1b', shaped['rows'][0][0])


if __name__ == '__main__':
    unittest.main()
