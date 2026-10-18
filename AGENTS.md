# AGENTS.md — Baseline di lavoro per p-secretary (2026-10-18)

Nota operativa: questo file serve da memoria persistente dell'agente (il memory tool
era malfunzionante alla data sopra). Aggiornarlo quando l'architettura cambia.
Fonti di verità rimangono i sorgenti e i documenti: README.md, SECRETARY_ARCHITECTURE_PLAN.md,
IMPLEMENTATION.md, CONFIGURATION.md, OPERATIONS.md, OWNER_DIALOGUE.md.

Documento di dettaglio dei flussi agentici (input/output, guardie, invarianti,
diagrammi Mermaid): AGENT_FLOWS.md.

## Cos'è p-secretary
Segretario personale per un solo owner (Raspberry Pi, Docker). NON un esecutore
arbitrario di workflow: "integrated bounded review secretary". Canali: Email
(IMAP/SMTP), Telegram (bot + account Telethon solo-archivio), WhatsApp (bridge
Node/whatsapp-web.js), Google Calendar. Dashboard Flask+waitress su LAN :8080.

## Principio cardine
**Il modello non è mai autorità — la provenance nativa lo è.** Ogni effetto è
policy-gated con ricevute immutabili; l'attempt è committato nel DB PRIMA
dell'I/O; "accepted ≠ delivered"; gli effetti "uncertain" non vengono mai
rinviati alla cieca.

## Mappa dei moduli
- `main.py` — entrypoint: db.init() prima dei worker; 8 worker (analysis, media,
  reconcile, action, owner, knowledge, secretary_review, secretary_email_execution);
  `check_telegram` archivia+checkpointer il cursore nella stessa transazione.
- `jobs.py` — coda durevole `work_jobs`: SKIP LOCKED, lease 15 min, heartbeat 30s,
  deadline 1h, backoff 15s→1h, max 8 tentativi; `Deferred` non consuma tentativi;
  `assert_current` ricontrolla il lease prima di ogni effetto.
- `policy.py` — `data_policies` default-deny (email/telegram/whatsapp ×
  ai_text/ai_media/ai_history); `effect_audit`; `effect_receipts`
  authorized→attempted→accepted/failed/uncertain; grant Calendar v3 sigillati
  (hash testo owner + hash payload + destinazione; il replay non rinnova);
  `_owner()` identità stretta; `metadata_for` con FOR SHARE.
- `message_store.py` — 3 tabelle native per canale + vista `communications`;
  lo storico importato è contesto read-only, mai replay operativo.
- `secretary.py` — pratiche/responsabilità con CHECK; piani v2 con
  `input_sources`/`input_versions` risolti SERVER-SIDE (mai accettati dal
  modello) + `assert_egress_sources` PRIMA dell'inferenza; `_parse_model`
  JSON rigoroso (chiavi duplicate/NaN rifiutati); evidenze = citazioni esatte;
  azioni entro `allowed_actions`; apply atomico sotto WRITE_LOCK con CAS.
- `secretary_runtime.py` — revisioni finestra 60min–30gg, osservazioni,
  link_proposals (evidenza exact_scope), attention/ack, stop condition SOLO
  strutturali (max_reviews/deadline/practice_state; la prosa non si esegue mai).
- `owner_dialogue.py` — lease 12 min; piano persistito PRIMA degli effetti;
  replay idempotente sulle riprove; DAG sorgenti con cycle detection;
  `resolve_source_manifest`/`assert_egress_sources`/`filter_history`/
  `filtered_knowledge`/`filtered_practices`. Retrieval planner con round
  configurabili (RETRIEVAL_MAX_ROUNDS, default 4, hard ceiling 8) e
  scratchpad bounded (note ≤200 char, max 8, inert data mai autorità).
- `agent_tools.py` — registry di tool read-only per il planner di retrieval
  (search, open, tasks, appointments, practices): argomenti validati
  strettamente, lease ricontrollato per chiamata, risultati filtrati DENTRO
  il tool con i gate di consenso nativi; nessun tool di scrittura/invio;
  `open` limitato agli ID già emersi dal server (citations/current/roundi
  precedenti). `owner_dialogue.retrieve` orchestre due round via registry.
- `owner_instructions.py` — istruzioni permanenti dell'owner: il capo insegna
  un nuovo comportamento stabile in linguaggio naturale ('da ora in poi…').
  Vocabolario CHIUSO server-declared (dominio invoice_folder: azioni
  route_by_sender_vat/route_by_sender_name); guardia lessicale prima del
  modello; evidenza = frase esatta con marker di permanenza; apply idempotente
  con ricevuta; revoca via /ferma_istruzione o dashboard; consultate da
  invoices.save_invoice. Email/inoltrati/storico NON possono crearle.
- `research.py` — worker `research` per lavoro lungo fuori dal lease dialogo:
  decomposizione → sub-job (lease proprio ciascuno, tool read-only via
  registry, citazioni reali allowlistate) → sintesi. Findings persistiti con
  provenance; nessun effetto esterno; budget scope 'research'.
- `ai_budget.py` — ledger `secretary_ai_usage` con colonna `scope`
  (global/dialogue/review/research/analysis derivata server-side dal system
  prompt); tetti per-scope opzionali (AI_BUDGET_*_DAILY_CALLS) che
  restringono mai superano il tetto globale; `usage()` espone conteggi per
  scope.
- `brain.py` + `ai_budget.py` — ledger `secretary_ai_usage`; DAILY_CALLS 200,
  DAILY_TOKENS 500k, MAX_INFLIGHT 2; `_reserve` advisory-lock fail-closed.
  Memoria tipizzata: prompt produce `topic` opzionale per nota;
  `db.apply_personal_memories` con replace-on-conflict (una nota viva per
  topic, indice unico parziale SQL, version bump); note senza topic restano
  additive come prima.
- `execution.py` — gateway email in 3 gesti: request_email (proposta sigillata)
  → approvazione owner (ricevuta) → dispatch_email esplicito (secondo gesto)
  → worker execute_approved (grant recheck + hash allegati) → SMTP.
  Richiede SECRETARY_MODE='active'.
- `task_reconciliation.py` — evidence-gated; auto-apply solo per
  certain+unique+quoted non-cancel, altrimenti proposta con /conferma_attivita.
- `owner_commitments.py` — grammatica date deterministica (ISO/oggi/domani/
  tra N ore), rifiuto ambiguità DST, anchor ORIGINAL_MESSAGE_TIME.
- `quiet_hours.py` — notifiche non urgenti slittano; emergenze bypass;
  `sends_allowed()` = SECRETARY_MODE=='active'.
- `dashboard.py` + `web/` — unica superficie fidata: auth session/CSRF/origin,
  no-store, CSP; `DashboardOwner` costruito server-side; ricevute di approvazione
  SOLO nel DOM (dataset.approvalOperationKey).
- `connectors/` — identità native verificate in ingresso (UIDVALIDITY email,
  forward metadata Telegram, version-ACK WhatsApp); tutti gli effetti in uscita.

## Gestione azioni (sintesi)
1. Autorità owner: SOLO testo nativo vivo, diretto, non inoltrato, in chat
   privata dal bot (content_origin='direct_text', is_forwarded=False).
   Storico/inoltrati/indiretti = solo evidenza; il consenso ai_history NON
   promuove un comando storico a istruzione operativa.
2. Consensi default-deny; la revoca è ricontrollata a OGNI confine reale
   (prepare, retrieval, provider finale) — anche su manifest già materializzati.
3. Flusso: prepare() → persist piano → apply() atomico (CAS) → ricevute.
4. Attempt-before-I/O per ogni effetto esterno; uncertain mai rinviato.
5. Notifiche: ordinamento owner-reply (owner_reply_mid) + quiet hours.

## Convenzioni dei test (~78 file)
Offline-first: rete e DB vietati via patch; SQL opt-in via
PI_SECRETARY_TEST_DATABASE_URL (dbname che finisce in _test, socket Unix,
no service). Coprono: provenance, egress canary, CAS/ricevute, quiet hours,
stop conditions, gateway email, guardie dashboard, web session via Node VM
harness, media consent recovery, dialogue durability/replay, runtime review
lifecycle, link proposal lifecycle, calendar destination binding, privacy
erase, budget ledger (con scope), tool registry read-only (test_agent_tools),
worker research (test_research), memoria tipizzata (test_typed_memory),
istruzioni permanenti owner (test_owner_instructions).

## Roadmap aperta (da IMPLEMENTATION.md)
Workflow arbitrari (parzialmente: worker research); conflitti memoria tipizzata
(risolta: replace-on-conflict per topic); drift Calendar; email threaded;
erase nei backup retention.

## Vocabolario del progetto
Pratiche, responsabilità, ricevute, consensi, revisioni, evidenze, manifest
egress, sigillato (sealed), owner gesture, quiet hours, stop condition.
