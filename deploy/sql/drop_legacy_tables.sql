-- Rimuove le tabelle del VECCHIO segretario (pratiche, responsabilità, revisioni,
-- esecuzione email, ricerca, dialogo/istruzioni del proprietario, riconciliazione).
--
-- NON viene eseguito dall'applicazione: sono dati di una versione precedente, non
-- più letti né scritti. Il nuovo agente usa agent_runs, agent_tool_calls,
-- agent_effects, agent_email_outbox e privacy_receipts, più le tabelle native dei
-- messaggi, attività, appuntamenti, note, regole e fatture, che restano.
--
-- Prima di eseguirlo:
--   1. fai un backup (pg_dump) e verifica di poterlo ripristinare;
--   2. fermare l'app non è obbligatorio, ma dopo il DROP non c'è ritorno;
--   3. eseguilo come proprietario delle tabelle (utente applicativo):
--        psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f deploy/sql/drop_legacy_tables.sql
--
-- Lo script si ferma senza modificare nulla se una tabella NON legacy ha una
-- chiave esterna verso una tabella legacy.

BEGIN;

CREATE TEMP TABLE legacy_tables(name text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO legacy_tables VALUES
    ('owner_dialogue_state'), ('owner_instructions'),
    ('research_findings'), ('research_requests'),
    ('secretary_approvals'), ('secretary_decisions'),
    ('secretary_email_attempts'), ('secretary_email_dispatches'), ('secretary_email_proposals'),
    ('secretary_links'), ('secretary_plans'), ('secretary_practices'), ('secretary_receipts'),
    ('secretary_responsibilities'), ('secretary_runtime_attention'),
    ('secretary_runtime_link_proposals'), ('secretary_runtime_observations'),
    ('secretary_runtime_reviews'), ('secretary_wakeups'),
    ('task_reconciliation_runs');

DO $$
DECLARE
    offender text;
BEGIN
    SELECT string_agg(DISTINCT c.conrelid::regclass::text || ' -> ' || c.confrelid::regclass::text, ', ')
      INTO offender
      FROM pg_constraint c
      JOIN pg_class referenced ON referenced.oid = c.confrelid
      JOIN pg_class referencing ON referencing.oid = c.conrelid
     WHERE c.contype = 'f'
       AND referenced.relname IN (SELECT name FROM legacy_tables)
       AND referencing.relname NOT IN (SELECT name FROM legacy_tables);
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION 'Una tabella non legacy dipende da una tabella legacy: %', offender;
    END IF;
END
$$;

DO $$
DECLARE
    item record;
BEGIN
    FOR item IN SELECT name FROM legacy_tables ORDER BY name LOOP
        EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', item.name);
    END LOOP;
END
$$;

DROP FUNCTION IF EXISTS secretary_immutable() CASCADE;
DROP FUNCTION IF EXISTS secretary_email_immutable() CASCADE;

COMMIT;
