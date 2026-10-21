-- Ruolo di SOLA LETTURA per le query libere dell'agente.
--
-- Quando serve questo script
--   * DB locale del compose (utente `secretary`, superuser): NON serve. L'app crea da
--     sola il ruolo al primo avvio (db.init -> agent.schema.ensure_readonly_role).
--   * DB esterno con un utente applicativo senza CREATEROLE (es. svc_pi_secretary): il
--     DBA esegue UNA volta le due istruzioni della PARTE 1. Il resto (USAGE sullo
--     schema e SELECT sulle viste) lo concede l'app stessa al prossimo avvio, perché
--     è proprietaria delle viste: l'ordine delle operazioni non conta.
--
-- Senza il ruolo l'app funziona comunque: restano la guardia AST (una sola SELECT, solo
-- viste dello schema agent) e la transazione READ ONLY; all'avvio stampa un avviso e la
-- pagina «Agente» della dashboard mostra «Protezione database».
--
-- Il ruolo è NOLOGIN, senza alcun attributo, e vede SOLO le viste agent.*: le viste
-- appartengono all'utente applicativo e leggono le tabelle con i SUOI privilegi, quindi il
-- ruolo non ha alcun accesso diretto alle tabelle.
--
-- Cambiare svc_pi_secretary se l'utente applicativo ha un altro nome; per un altro nome
-- di ruolo impostare AGENT_DB_ROLE (vuoto = nessun ruolo, solo guardia AST).

-- ===== PARTE 1 · obbligatoria per il DBA (idempotente) =====
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
        CREATE ROLE agent_ro NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END
$$;

GRANT agent_ro TO svc_pi_secretary;

-- ===== PARTE 2 · facoltativa: la esegue già l'app (utile solo se vuoi farla tu) =====
-- CREATE SCHEMA IF NOT EXISTS agent AUTHORIZATION svc_pi_secretary;
-- GRANT USAGE ON SCHEMA agent TO agent_ro;
-- GRANT SELECT ON ALL TABLES IN SCHEMA agent TO agent_ro;
-- ALTER DEFAULT PRIVILEGES FOR ROLE svc_pi_secretary IN SCHEMA agent GRANT SELECT ON TABLES TO agent_ro;

-- Verifica (come svc_pi_secretary, dopo aver avviato l'app almeno una volta): la prima deve
-- funzionare e la seconda FALLIRE.
--   BEGIN; SET LOCAL ROLE agent_ro; SELECT count(*) FROM agent.messages; ROLLBACK;
--   BEGIN; SET LOCAL ROLE agent_ro; SELECT count(*) FROM public.email_messages; ROLLBACK;
