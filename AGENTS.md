# AGENTS.md — Baseline di lavoro per p-secretary

Memoria persistente per chi sviluppa (umano o agente). Le fonti di verità sono i
sorgenti e i documenti: `README.md`, `AGENT_FLOWS.md` (flussi con diagrammi),
`CONFIGURATION.md`, `OPERATIONS.md`, `AGENTIC_CORE_PLAN.md` (decisioni e stato di
attuazione). Aggiornare questo file quando l'architettura cambia.

## Cos'è p-secretary
Segretario personale per un solo owner (Raspberry Pi 4, aarch64, Docker). Canali:
Email (IMAP/SMTP), Telegram (bot + account Telethon solo-archivio), WhatsApp
(bridge Node/whatsapp-web.js), Google Calendar. Dashboard Flask+waitress su LAN :8080.

**Un solo agente** (GitHub Copilot SDK, LLM = OpenRouter) interpreta le richieste e
usa strumenti tipizzati. Non esistono più pipeline di classificazione, pratiche,
piani v2, retrieval planner, sub-agent di ricerca o istruzioni permanenti a
vocabolario chiuso: sono stati rimossi (la storia è in git; vedi
`AGENTIC_CORE_PLAN.md`). Non aggiungere flussi fissi: aggiungere uno strumento.

## Principio cardine
**Il modello non è mai autorità — la provenienza nativa lo è.** Il modello sceglie
*cosa guardare e cosa proporre*; il server decide *cosa è permesso*. Ogni effetto è
policy-gated con ricevute; l'attempt è committato PRIMA dell'I/O; «accepted ≠
delivered»; gli effetti `uncertain` non vengono mai rinviati alla cieca. Un prompt
non è mai un confine di sicurezza.

## Mappa dei moduli
### Agente (`agent/`)
- `runtime.py` — un solo `CopilotClient` in un thread asyncio dedicato, `mode='empty'`
  (nessuno strumento d'ambiente: shell/file/rete/git), permessi diversi da quelli
  dei tool custom respinti; **sessioni effimere** (contesto ricostruito dal DB a ogni
  turno, sessione cancellata a fine turno). `stop()` a livello modulo.
- `gateway.py` — HTTP su loopback nello stesso processo: l'unico LLM è OpenRouter
  e la **chiave non entra mai nel runtime**. Bearer monouso per turno → job
  `ExecutionContext`; ogni richiesta passa dal ledger (`ai_budget.accounted`),
  `max_tokens` esplicito, costo/token reali; lease perso/budget/ledger KO = rifiuto
  PRIMA del provider.
- `toolkit.py` — catalogo `SPECS` (nome, parametri pydantic `extra=forbid`,
  handler, modalità ammesse, `write`). `call()` applica lease, tetto
  `AGENT_MAX_TOOL_CALLS`, audit. Le scritture passano da `_journaled` →
  `journal.once` (effetto + riga di journal nella stessa transazione).
- `sqlguard.py` + `schema.py` + `data.py` — SQL libero in sola lettura: AST pglast
  (una SELECT, solo `agent.*`/CTE locali, funzioni in allowlist, nessun cast `reg*`),
  transazione READ ONLY con timeout, `SET LOCAL ROLE agent_ro` se il ruolo esiste
  (lo predispone `schema.ensure_readonly_role` da `db.init`; se l'utente DB non può crearlo
  serve il DBA: `deploy/agent_ro.sql`; su PG16+ conta il privilegio SET, non MEMBER). Le viste `agent.*` incorporano i consensi
  (`data_policies`: ai_text / ai_media / ai_history) e li rivalutano **a ogni query**.
  `messages.media_missing` segnala allegati multimediali non persistiti.
- `owner.py` — autorità nativa (`authorize`, `current_text`), scambi durevoli
  (`begin`/`deliver`/`complete`), storico recente (`recent_history`) e comandi slash
  deterministici (`command`: /task /attivita /fatto /conferma /annulla /invia /scarta
  /appuntamenti /note /regole /fatture /start /stop).
- `outbox.py` — email in uscita: `propose` (agente) → `decide` (`/invia` o `/scarta`,
  solo testo nativo dell'owner, rifiutato se `SECRETARY_MODE='observe'`) →
  `pipeline.action` (kind `email`, SMTP, tentativo committato prima dell'I/O).
  Un destinatario, indirizzo nudo, nessun allegato né intestazione.
- `mail.py` — IMAP **sola lettura** (`select(readonly=True)`, `BODY.PEEK`); criteri
  da parametri strutturati; consenso `email/ai_text` verificato a ogni chiamata.
- `web.py` — ricerca web via plugin `web` di OpenRouter; query ≤200 car., niente
  URL/indirizzi, nessun fetch di pagine; nessun budget dedicato (decisione esplicita).
- `fsbox.py` — filesystem: lettura di `/data` (allegati, fatture, upload,
  workspace); scrittura SOLO in `workspace/` (dir_fd + `O_NOFOLLOW`, no hardlink,
  no sovrascrittura, quota `AGENT_WORKSPACE_QUOTA_MB`).
- `journal.py` — `agent_runs`, `agent_tool_calls` (solo metadati bounded: mai
  prompt né risposte), `agent_effects` (idempotenza per `(turn_key, tool, hash args)`).
- `prompts.py` — prompt di sistema per modalità (`owner`, `inbox`, `digest`).
- `turns.py` — i tre modi di lavorare: `owner_turn`, `inbox_job` (lotti fino a
  `AGENT_INBOX_BATCH`), `digest_text`.

### Resto dell'applicazione
- `main.py` — entrypoint: `db.init()` prima dei worker; sei worker durevoli
  (`analysis`, `media`, `action`, `owner`, `digest`, `knowledge`); attività
  periodiche con un solo thread vivo per nome; `check_telegram` archivia +
  checkpointa il cursore nella stessa transazione.
- `jobs.py` — coda durevole `work_jobs`: `SKIP LOCKED`, lease 15 min, heartbeat 30 s,
  deadline 1 h, backoff 15 s→1 h, max 8 tentativi; `Deferred` non consuma tentativi;
  `assert_current` ricontrolla il lease prima di ogni effetto; `bind(context)`.
- `policy.py` — `data_policies` default-deny; `effect_audit`; `effect_receipts`
  (authorized→attempted→accepted/failed/uncertain); grant Calendar v3 sigillati
  (hash testo owner + hash payload + destinazione; il replay non rinnova); `_owner()`.
- `message_store.py` — tabelle native per canale + vista `communications`; lo storico
  importato è contesto read-only, mai replay operativo.
- `pipeline.py` — `ingest` (un solo job `analysis` per messaggio), `load_message`,
  `action` (notify, calendar, invoice, email), promemoria, `schedule` (prune
  retention giornaliero, audit agente incluso).
- `digest.py` — `run(payload)` per `digest:AAAA-MM-GG`: testimoni dei consensi,
  sezione deterministica delle scadenze personali, sigillo delle fonti, notifica
  durevole. Senza fonti **non chiama il modello**.
- `dashboard.py`, `dashboard_owner.py`, `web/` — unica superficie fidata (sessione/
  CSRF/origin, no-store, CSP); `DashboardOwner` costruito server-side.
- `knowledge.py` — indice lessicale dei documenti (estrazione allegati).
- `owner_commitments.py` — grammatica date deterministica, `build_change/build_bulk`.
- `ai_budget.py` — ledger `secretary_ai_usage` con `scope` (`agent_owner`,
  `agent_inbox`, …) derivato dal server, mai dal modello; tetti opzionali per scope.
- `capabilities.py`, `flows.py`, `privacy.py`, `recovery.py`,
  `historical_import.py`, `installer.py`, `admin_config.py`, `platform_health.py`.
- `connectors/` — identità native verificate in ingresso (UIDVALIDITY email, forward
  metadata Telegram, version-ACK WhatsApp); tutti gli effetti in uscita.
  `whatsapp.py` ritenta il download dei media e traccia gli esiti (`MEDIA_ERRORS`).

## Invarianti da non rompere
1. **Autorità owner** = solo testo nativo vivo, diretto, non inoltrato, in chat privata
   dal bot (`content_origin='direct_text'`, `is_forwarded=False`). Storico, inoltrati,
   email, trascrizioni e allegati sono evidenza, mai comando.
2. **Consensi default-deny**, rivalutati a ogni confine reale: ogni query SQL
   dell'agente, ogni lettura live, ogni invio al provider, il digest prima del sigillo.
3. **Scritture dell'agente**: solo strumenti tipizzati; sul conto dell'owner serve
   una sua frase esatta (citazione verificata sul messaggio archiviato); i messaggi di
   terzi non possono modificare attività esistenti né note.
4. **Calendar** = sempre proposta locale + `/conferma` (grant sigillato); **email** =
   bozza + `/invia` + `SECRETARY_MODE=active`.
5. **Attempt-before-I/O**; `uncertain` mai rinviato; effetto + journal nella stessa
   transazione; `jobs.assert_current` prima di ogni effetto.
6. **La chiave OpenRouter non lascia il processo dell'app**; nessun prompt/risposta nei log.
7. **Notifiche autonome**: solo vere emergenze, tetto giornaliero serializzato con
   advisory lock (`AGENT_NOTIFY_MAX_PER_DAY`).
8. Il `Dockerfile` copia i moduli **per nome**: ogni nuovo `*.py` di primo livello va
   aggiunto (lo verifica `test_dashboard_api.test_packaging_and_ci_contracts`).

## Convenzioni dei test (~55 file)
Offline-first: rete e DB vietati via patch. SQL opt-in con
`PI_SECRETARY_TEST_DATABASE_URL` (dbname che finisce in `_test`; alcuni moduli
richiedono un **socket Unix**, es. `host=/tmp/pg-test-sock`, senza service/hostaddr).
`support.py` fornisce `completion`, `job_context`, `postgres_fixture()` (schema unico per
test: patch di `config.AGENT_DB_SCHEMA` e drop finale). Il runtime Copilot reale si
abilita con `PI_SECRETARY_TEST_AGENT_RUNTIME=/percorso/copilot-runtime`; i test di
`test_agent_runtime` usano un LLM scriptato dietro il vero gateway.

```sh
# offline (come la CI)
env -i PATH="$PATH" HOME=/tmp PI_SECRETARY_CONFIG_FILE=/dev/null DATA_DIR="$(mktemp -d)" \
  python -m unittest discover
# con PostgreSQL usa-e-getta e runtime reale
env -i PATH="$PATH" HOME=/tmp PI_SECRETARY_CONFIG_FILE=/dev/null DATA_DIR="$(mktemp -d)" \
  PI_SECRETARY_TEST_AGENT_RUNTIME=... PI_SECRETARY_TEST_DATABASE_URL="host=/tmp/pg-test-sock dbname=secretary_test user=secretary_test password=..." \
  python -m unittest discover
```

Lezioni: non rimuovere import «inutilizzati» che i test patchano (es.
`connectors/media_text.py` deve importare `json` e `urllib.request`); i test di
concorrenza vanno ripetuti più volte (la race sul tetto notifiche falliva 1 volta su 4);
i moduli `*_postgres` con cluster proprio vanno lanciati per classe.
Aree coperte: provenienza e consensi (viste `agent.*`), SQL guard, fsbox, gateway, bordi di web/posta/prompt/bozze
(`test_agent_edges`),
turni dell'owner (comandi, idempotenza, autorità), runtime reale (12 scenari, inclusa
iniezione da email ostile), budget/ledger per scope, dashboard API e sessione web (VM
Node), ricevute/calendar, privacy erase, recupero media, import storico, connettori.

## Roadmap aperta
Calendar update/disponibilità/drift; email in thread e con allegati; erase nei backup;
prova di ripristino completa; valutazione qualitativa dell'agente sul vero archivio;
test di `agent/mail.py` contro un vero server IMAP (oggi solo IMAP finto) e di `agent/web.py` contro la rete;
drop delle tabelle legacy (`deploy/drop_legacy_tables.sql`, a cura dell'owner dopo backup).

## Vocabolario
Turno, strumento, ricevuta, consenso, testimone, sigillo (sealed), gesto dell'owner
(`/conferma`, `/invia`), manifest di fonti, lease, journal degli effetti.
