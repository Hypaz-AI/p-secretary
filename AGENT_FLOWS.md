# AGENT_FLOWS.md — I flussi agentici di p-secretary

**Data**: 2026-10-18 · **Complementa**: `AGENTS.md` (baseline), `SECRETARY_ARCHITECTURE_PLAN.md` (visione), `OWNER_DIALOGUE.md` (dialogo)

> Come lavora l'agente: ogni flusso con **input**, **output**, **guardie** e **invarianti**.
> Il principio cardine vale per tutto: **il modello non è mai autorità — la provenance nativa lo è.**

## Indice

1. Panoramica: worker e cicli
2. Ingestione e routing dei messaggi
3. Worker analysis
4. Dialogo owner — il loop agentico principale
5. Autorizzazione di un'azione
6. Il piano del segretario: prepare → apply
7. Revisioni runtime (bounded review)
7-bis. Worker research — lavoro lungo con sub-job
7-ter. Istruzioni permanenti dell'owner — il capo insegna comportamenti nuovi
8. Gateway email — i tre gesti dell'owner
9. La macchina a stati delle ricevute
10. Riconciliazione task
11. Digest giornaliero
12. Guardie trasversali: budget AI, quiet hours, modalità
13. Esempio end-to-end

---

## 1. Panoramica: worker e cicli

L'agente non è un loop monolitico: è un insieme di **worker indipendenti** che consumano una **coda durevole** PostgreSQL (`work_jobs`). Ogni worker ha una responsabilità stretta; nessuno può agire fuori dal proprio confine di policy.

```mermaid
flowchart TB
    subgraph CANALI["Canali nativi in ingresso"]
        E["Email IMAP"]
        T["Telegram bot"]
        W["WhatsApp bridge"]
        TA["Telethon (solo archivio)"]
    end

    MS["message_store<br/>tabelle native per canale<br/>+ vista communications"]
    Q[("work_jobs<br/>coda durevole<br/>SKIP LOCKED · lease 15 min")]

    subgraph WK["Worker (main.py)"]
        AW["analysis"]
        MW["media"]
        RW["reconcile"]
        AC["action"]
        OW["owner"]
        KW["knowledge"]
        SR["secretary_review"]
        RS["research"]
        EE["secretary_email_execution"]
    end

    B["brain + ai_budget<br/>ledger fail-closed"]
    SEC["secretary<br/>piani v2 · prepare → apply"]
    RT["secretary_runtime<br/>revisioni limitate"]
    OD["owner_dialogue<br/>lease 12 min"]
    POL["policy<br/>consensi + ricevute"]
    KN["knowledge<br/>catalogo v2"]

    subgraph EFF["Effetti in uscita"]
        NT["Notifiche owner<br/>(quiet hours)"]
        GC["Calendar<br/>(grant sigillati v3)"]
        ML["Email<br/>(gateway 3 gesti)"]
        PR["Proposte / approvazioni"]
    end

    CANALI --> MS --> Q --> WK
    AW --> B
    KW --> KN
    OW --> OD --> SEC
    SR --> RT --> SEC
    RS --> KN
    SEC --> POL
    AC --> POL
    EE --> ML
    POL --> EFF
    POL -.->|consensi ai_text / ai_media / ai_history| B
```

**I 9 worker e i loro confini:**

| Worker | Consuma | Input | Output principale |
|---|---|---|---|
| `analysis` | job `analysis` | message_id | verdetto JSON, job task/appuntamento/fattura, notifiche, osservazioni runtime |
| `media` | job `media` | allegato | trascrizioni audio / descrizioni immagini (consenso `ai_media`) |
| `reconcile` | job `reconcile` | ingresso live | cambi task auto-applicati o proposte all'owner |
| `action` | job `action` | payload azione | effetti esterni con ricevute (notify, calendar, invoice, reminder, zip) |
| `owner` | job `owner` | messaggio owner nativo diretto | risposta + piani applicati + task + memoria |
| `knowledge` | job `knowledge` | message_id | estrazioni indicizzate nel catalogo (sha256, revisioni) |
| `secretary_review` | wakeup | revisione dovuta / osservazione | pratiche aggiornate (CAS) + notifiche coalesced |
| `research` | job `research` (decompose/subtask/synthesize) | domanda dell'owner | findings con citazioni + report persistito (nessun effetto esterno) |
| `secretary_email_execution` | job `execute_approved` | dispatch approvato | invio SMTP con ricevta attempt-before-I/O |

**Lanciatori periodici** (un thread vivo ciascuno): fetch email, digest, schedule (revisioni dovute, promemoria), secretary_reviews, whatsapp.

**Invarianti della coda** (`jobs.py`): claim con `SKIP LOCKED`; lease 15 min con heartbeat 30 s; deadline 1 h; backoff 15 s → 1 h; max 8 tentativi; `Deferred` non consuma tentativi; `assert_current` ricontrolla il lease **prima di ogni effetto**.

---

## 2. Ingestione e routing dei messaggi

Ogni canale porta identità native verificate: **UIDVALIDITY** per email, **metadata di forward** per Telegram, **version-ACK** per WhatsApp. Lo storico importato (Telethon, import email) è flaggato `historical` e resta contesto di sola lettura.

```mermaid
flowchart TD
    POLL["Polling / fetch<br/>FileLock · UIDVALIDITY · version-ACK"]
    STORE["message_store.store<br/>(transazione atomica)"]
    DER["enqueue_derived:<br/>knowledge.enqueue +<br/>secretary_runtime.observe"]
    AUTH{"Testo owner nativo?<br/>direct_text · non inoltrato · chat privata bot"}
    OJ["job 'owner'"]
    AJ["job 'analysis'"]
    CKPT["Checkpoint cursore<br/>(stessa transazione dell'archiviazione)"]

    POLL --> STORE --> DER
    STORE --> AUTH
    AUTH -->|"sì (solo Telegram bot)"| OJ --> CKPT
    AUTH -->|"no: email, WhatsApp, inoltrati, storico"| AJ --> CKPT
```

| | |
|---|---|
| **Input** | Messaggi nativi dai 4 canali (live o storico) |
| **Output** | Righe tabelle native; job `owner`/`analysis`; enqueue derivato (catalogo + osservazioni); checkpoint cursore |
| **Guardie** | Identità native verificate in ingresso; flag `historical`; dedup per canale |
| **Invarianti** | Store + enqueue atomici; cursore checkpointato **solo dopo** l'archiviazione, nella stessa transazione; contenuto indiretto/inoltrato dell'owner = solo evidenza, mai comando |

---

## 3. Worker analysis

Il primo sguardo dell'agente su ogni messaggio. Il modello produce un **verdetto JSON** (con `severity` e `urgent`), mai testo libero eseguibile.

```mermaid
flowchart TD
    CLAIM["Claim job analysis<br/>(lease 15 min · heartbeat 30 s)"]
    GATE{"can_read_context?<br/>consenso ai_text / ai_media"}
    AL["Advisory lock per message_id<br/>(un'analisi alla volta)"]
    VER["brain.analyze<br/>→ verdetto JSON (severity, urgent)"]
    INSTR["evaluate_notify (§7-ter):<br/>istruzioni permanenti attive?<br/>giudizio semantico —<br/>match ⇒ urgent=True<br/>(fail-closed: errore ⇒ skip)"]
    D1{"needs_action?"}
    D2{"appuntamento?"}
    D3{"fattura?"}
    D4{"urgente?"}
    TQ["job task"]
    AQ["job appointment"]
    IQ["job invoice"]
    NQ["notify via quiet_hours<br/>(slitta se non urgente)"]
    OBS["secretary_runtime.observe<br/>(osservazione per le revisioni)"]
    FIN["Attempt + ricevuta<br/>committati PRIMA di ogni I/O"]

    CLAIM --> GATE
    GATE -->|"negato"| SKIP["Salta (audit context_denied)"]
    GATE -->|"ok"| AL --> VER --> INSTR --> D1
    D1 -->|"sì"| TQ
    D1 -->|"no"| D2
    D2 -->|"sì"| AQ
    D2 -->|"no"| D3
    D3 -->|"sì"| IQ
    D3 -->|"no"| D4
    D4 -->|"sì"| NQ
    D4 -->|"no"| OBS
    TQ --> OBS
    AQ --> OBS
    NQ --> FIN
    OBS --> FIN
```

| | |
|---|---|
| **Input** | Job `analysis` con message_id |
| **Output** | Verdetto persistito; job task/appuntamento/fattura; notifiche (incl. urgent da istruzioni permanenti); osservazioni runtime |
| **Guardie** | `can_read_context` (default-deny); advisory lock per mid; budget AI; `evaluate_notify` fail-closed (un errore non rompe mai l'analisi) |
| **Invarianti** | Contenuto **indiretto** dell'owner non genera mai `needs_action`/appuntamenti; il contenuto del messaggio è untrusted data per il valutatore di istruzioni (non può crearle/modificarle); ogni I/O preceduto da attempt committato |

---

## 4. Dialogo owner — il loop agentico principale

Il cuore agentico: quando l'owner scrive direttamente al bot, `owner_dialogue.respond` orchestra inferenza, piano, effetti e risposta in un'unica transazione logica con lease e replay idempotente. Il retrieval è guidato da un **registry di tool read-only** (`agent_tools.py`): il modello può solo *nominare* un tool dichiarato e fornire argomenti tipati; il server valida, ricontrolla lease e consensi **per chiamata**, e filtra i risultati dentro il tool stesso. Il planner ha **round configurabili** (`RETRIEVAL_MAX_ROUNDS`, default 4, ceiling 8) e uno **scratchpad bounded** (note ≤200 char, max 8, inert data).

```mermaid
sequenceDiagram
    autonumber
    participant O as Owner (bot Telegram)
    participant Q as work_jobs
    participant D as owner_dialogue
    participant S as secretary
    participant K as knowledge
    participant B as brain + ai_budget
    participant DB as PostgreSQL

    O->>Q: testo nativo diretto (job 'owner')
    Q->>D: claim (lease 12 min)
    opt riprova dopo crash
        D->>DB: ricevuta di replay già presente?
        D->>O: risposta salvata (idempotente, nessun doppio effetto)
    end
    D->>DB: load_context (100 scambi · 12k token recenti · richiamo lessicale)
    D->>S: prepare(testo, mid, history filtrata)
    Note over S,DB: §6 — manifest server-side + assert_egress_sources PRIMA dell'inferenza
    S-->>D: piano v2 validato (changes, clarification)
    D->>S: owner_commitments.prepare (task owner, grammatica date)
    D->>B: prepare_user_memories (piano memoria,<br/>topic opzionale: replace-on-conflict)
    D->>K: retrieve via agent_tools registry
    Note over K: search · open · tasks · appointments · practices
    Note over K: gate di consenso PER CHIAMATA, risultati filtrati DENTRO il tool
    D->>B: chat_with_user (dialogo + egress_sources)
    B-->>D: risposta per l'owner
    D->>DB: persist piano (PRIMA degli effetti)
    rect rgb(245,245,220)
        D->>DB: transazione atomica effetti
        Note over D,DB: secretary.apply (savepoint) · task (CAS) · memoria (CAS) · save_event
    end
    D->>O: risposta + outbox (ordinata da owner_reply_mid)
    D->>DB: compact (checkpoint riassunto del dialogo)
```

| | |
|---|---|
| **Input** | Messaggio owner nativo, diretto, non inoltrato, chat privata (job `owner`) |
| **Output** | Risposta + chunks in outbox; pratiche/responsabilità applicate; task owner; memoria; eventi calendario locali; ricevute |
| **Guardie** | Lease 12 min; identità owner stretta (`policy._owner`); consensi egress ricontrollati a ogni confine; budget AI; tool registry read-only con gate per chiamata (`open` limitato agli ID emersi dal server) |
| **Invarianti** | Piano **persistito prima** degli effetti; effetti in transazione atomica con savepoint e CAS; replay idempotente sulle riprove; la history è filtrata — i comandi storici non diventano mai istruzioni operative; nessun tool di scrittura/invio esiste nel registry |

---

## 5. Autorizzazione di un'azione

Come qualsiasi effetto — dal piano del segretario, dal worker action o dal dialogo — attraversa i cancelli prima di toccare il mondo esterno.

```mermaid
flowchart TD
    CMD["Azione proposta<br/>(piano · worker · dialogo)"]
    OWN{"Provenance owner nativa?<br/>direct_text · non inoltrato · chat privata"}
    NOPE["Solo evidenza:<br/>nessuna autorità operativa"]
    POL{"policy.can_operate<br/>consenso canale + effetto?"}
    SEAL{"Grant sigillato?<br/>Calendar v3: hash testo owner<br/>+ hash payload + destinazione"}
    PREP["prepare_effect<br/>→ ricevuta 'authorized'"]
    ATT["COMMIT attempt nel DB<br/>(PRIMA dell'I/O)"]
    IO["I/O esterno<br/>SMTP · Telegram · Calendar"]
    ACC["Ricevuta finale:<br/>accepted / failed / uncertain"]

    CMD --> OWN
    OWN -->|"no"| NOPE
    OWN -->|"sì"| POL
    POL -->|"negato"| DENY["Deny (effect_audit)"]
    POL -->|"ok"| SEAL
    SEAL -->|"mancante / non corrisponde"| DENY
    SEAL -->|"ok"| PREP --> ATT --> IO --> ACC
```

| | |
|---|---|
| **Input** | Azione con provenance (message_id) + payload |
| **Output** | Ricevute `authorized → attempted → accepted/failed/uncertain` |
| **Guardie** | Identità owner; consenso per canale+effetto; grant sigillati per Calendar; lease del job (`assert_current`) |
| **Invarianti** | La revoca di un consenso è ricontrollata a **ogni confine reale** (prepare, retrieval, provider finale) anche su manifest già materializzati; il replay di un grant non lo rinnova mai; "accepted ≠ delivered" |

---

## 6. Il piano del segretario: prepare → apply

Il modello **propone**, il server **valida e dispone**. Il manifest delle sorgenti non arriva mai dal modello: è risolto server-side e verificato prima dell'inferenza.

```mermaid
flowchart TD
    subgraph PREP["prepare() — inferenza vincolata"]
        SNAP["Snapshot pratiche / responsabilità<br/>(filtered_practices) + anchor temporale"]
        MAN["resolve_source_manifest<br/>SERVER-SIDE (DAG, cycle detection)"]
        EG{"assert_egress_sources:<br/>tutti i consensi OK?"}
        PROMPT["Prompt al modello<br/>(brain._chat → ai_budget.request_content)"]
        PARSE{"_parse_model:<br/>JSON rigoroso?<br/>(chiavi duplicate / NaN rifiutati)"}
        SCHEMA{"_plan_schema v2 + validazione:<br/>evidenze = citazioni esatte ·<br/>azioni entro allowed_actions"}
    end

    subgraph APP["apply() — esecuzione atomica"]
        WL["WRITE_LOCK"]
        CAS{"CAS expected_version<br/>+ versioni responsabilità dipendenti"}
        WRITE["Scritture pratiche/responsabilità<br/>+ ricevute immutabili"]
        INV["_invalidate approvazioni superate"]
        QR["_queue_review (prossima revisione)"]
    end

    SNAP --> MAN --> EG
    EG -->|"negato"| STOP["PermissionError — nessun dato esce"]
    EG -->|"ok"| PROMPT --> PARSE
    PARSE -->|"non valido"| INVALID["InvalidPlan — scartato"]
    PARSE -->|"ok"| SCHEMA
    SCHEMA -->|"non valido"| INVALID
    SCHEMA -->|"ok"| PERSIST["Persist piano (append-only)"]
    PERSIST --> WL --> CAS
    CAS -->|"conflitto"| CONFLICT["Conflict — ricarica e ripropa con versioni fresche"]
    CAS -->|"ok"| WRITE --> INV --> QR
```

| | |
|---|---|
| **Input** | Testo owner + history filtrata + snapshot pratiche/responsabilità |
| **Output** | Piano v2 persistito (con `input_sources`/`input_versions`); ricevute di apply; revisione in coda |
| **Guardie** | Manifest server-side; `assert_egress_sources` prima dell'inferenza; parsing JSON rigoroso; schema del piano; CAS in apply |
| **Invarianti** | Il modello non può fornire il manifest né auto-concedersi consensi; ogni evidenza deve essere una citazione esatta di una sorgente reale; le azioni stanno dentro `allowed_actions`; apply atomico sotto lock |

---

## 7. Revisioni runtime (bounded review)

L'agente **segue** le pratiche nel tempo, ma in modo limitato: finestre di revisione obbligate, fonti riaperte e ricontrollate, stop condition solo strutturali.

```mermaid
flowchart TD
    WAKE["Wakeup: revisione dovuta ·<br/>osservazione · recovery di revisioni scadute"]
    MATCH{"operation_key<br/>corrisponde al wakeup?"}
    STATE["Ri-risoluzione dello stato<br/>(versioni correnti, non cache)"]
    SRC["_open_sources<br/>(limitato, campi whitelist)"]
    CUR{"_sources_current?<br/>consensi ricontrollati native"}
    PLAN2["Piano di revisione persistito<br/>PRIMA dell'apply"]
    VAL{"_validate_plan:<br/>citazioni esatte · azioni consentite ·<br/>finestra 60 min – 30 giorni"}
    APP["_apply_review:<br/>CAS version bump · _invalidate ·<br/>_reissue_link_proposals"]
    NOT["_notify_owner:<br/>coalescing per hash contenuto ·<br/>quiet hours · attesa ack"]
    STOPC{"stop_condition_met?<br/>(SOLO strutturale:<br/>max_reviews · deadline · practice_state)"}

    WAKE --> MATCH
    MATCH -->|"no"| DROP["Scarta (wakeup stale)"]
    MATCH -->|"sì"| STATE --> SRC --> CUR
    CUR -->|"revocato"| DEF["Deferred — nessuna inferenza, nessun dato esce"]
    CUR -->|"ok"| PLAN2 --> VAL
    VAL -->|"no"| REJ["Piano rifiutato"]
    VAL -->|"sì"| APP --> NOT --> STOPC
    STOPC -->|"sì"| FINE["Pratica sospesa / completata"]
    STOPC -->|"no"| NEXT["Prossima revisione programmata"]
```

| | |
|---|---|
| **Input** | Wakeup (revisione dovuta, osservazione da messaggio live, recovery) |
| **Output** | Pratica aggiornata (CAS); notifica owner coalesced; link proposals riemesse; valutazione stop |
| **Guardie** | Finestra 60 min – 30 gg; citazioni esatte; consensi ricontrollati su ogni sorgente (anche vecchie versioni applicate); quiet hours |
| **Invarianti** | Piano persistito prima dell'apply; la **prosa** del modello non si esegue mai — solo stop condition strutturali; notifiche coalesced per hash per non tempestare l'owner |

---

## 7-bis. Worker research — lavoro lungo con sub-job

Il lease del dialogo (12 min) non può ospitare lavoro lungo. Il worker `research` (`research.py`) lo sposta su sub-job durevoli, ciascuno con lease proprio, e sintetizza i risultati. Tutto read-only verso il mondo esterno.

```mermaid
flowchart TD
    REQ["research.request<br/>(da coordinatore fidato:<br/>autorità owner già verificata)"]
    DEC["Job decompose:<br/>domanda → 1-6 sub-task<br/>(JSON rigoroso, validato)"]
    SUB["Job subtask ×N:<br/>lease proprio ciascuno ·<br/>tool read-only via registry ·<br/>max 3 round · open allowlistato"]
    FIND["Finding persistito:<br/>testo + citazioni REALI<br/>(forged citations droppate)"]
    SYN["Job synthesize:<br/>solo quando tutti i findings<br/>sono arrivati"]
    REP["Report persistito<br/>(≤8000 char, status done)"]

    REQ --> DEC --> SUB --> FIND --> SYN --> REP
    SUB -.->|"scope 'research'<br/>nel budget AI"| BUD["Ledger con scope"]
```

| | |
|---|---|
| **Input** | Domanda dell'owner + origin_message (autorità già verificata dal chiamante) |
| **Output** | Findings con citazioni reali; report finale persistito; nessun effetto esterno |
| **Guardie** | Decomposizione validata (1-6 sub-task, JSON rigoroso); tool solo dal registry read-only; `open` limitato agli ID emersi; citazioni forged droppate; budget scope 'research' |
| **Invarianti** | Ogni sub-job ha lease proprio e ricontrolla lo stato della request; la sintesi parte solo a findings completi; il testo del modello è DATA mai autorità; il report non viene mai inviato — il chiamante lo legge come contesto non fidato |

---

## 7-ter. Istruzioni permanenti dell'owner — il capo insegna comportamenti nuovi

Quando l'owner dice "da ora in poi…", il segretario impara una procedura stabile. Il design separa **condizione** e **azione**: la condizione è il linguaggio libero dell'owner, giudicato **semanticamente dal modello** a ogni messaggio in arrivo; l'azione è un vocabolario chiuso agganciato ai gateway di effetto esistenti. Il modello giudica QUANDO; il server decide COSA.

```mermaid
flowchart TD
    CMD["Owner (testo diretto bot):<br/>'da ora in poi, ogni volta che mi<br/>scrive il signor De Angelis,<br/>avvisami immediatamente'"]
    GUARD{"looks_like_instruction?<br/>guardia lessicale:<br/>marker di permanenza in frase<br/>non quotata, non inoltrata"}
    PREP["prepare: classificazione nel<br/>vocabolario CHIUSO (dominio+azione)<br/>+ condition = parole dell'owner<br/>+ evidenza = frase esatta"]
    PERSIST["Piano nel owner_exchanges.plan<br/>(PRIMA degli effetti)"]
    APPLY["apply nella transazione effetti:<br/>idempotente su replay · ricevuta ·<br/>conferma all'owner"]
    ACTIVE["Istruzione attiva<br/>(condition in linguaggio naturale,<br/>version, provenance, revocabile)"]
    ARR["Arriva un messaggio qualunque<br/>(email, WhatsApp, Telegram)"]
    EVAL["evaluate_notify (pipeline.analyze):<br/>il modello giudica SEMANTICAMENTE<br/>se la condizione scatta"]
    MATCH{"match?"}
    NOTIFY["Notifica urgente all'owner<br/>(gateway notify esistente:<br/>ricevute, quiet hours bypass)"]
    SKIP["Nessuna azione<br/>(fail-closed sul dubbio)"]
    REV["Owner: '/ferma_istruzione N'<br/>→ sospesa (version bump)"]

    CMD --> GUARD
    GUARD -->|"no"| CHAT["Dialogo normale"]
    GUARD -->|"sì"| PREP --> PERSIST --> APPLY --> ACTIVE
    ARR --> EVAL --> MATCH
    MATCH -->|"sì"| NOTIFY
    MATCH -->|"no / incerto"| SKIP
    REV -.-> ACTIVE
```

**I due domini del vocabolario chiuso**:

| Dominio | Azioni | Come scatta |
|---|---|---|
| `notify_on` | `notify_owner` | **Semantica per messaggio**: `evaluate_notify` in `pipeline.analyze` giudica la condizione (parole dell'owner) su ogni messaggio in arrivo; match ⇒ notifica urgente |
| `invoice_folder` | `route_by_sender_vat`, `route_by_sender_name` | **Deterministica all'archiviazione**: `invoices.save_invoice` consulta `invoice_folder(sender)` (P.IVA o nome del mittente, slug difensivo, fallback legacy) |

| | |
|---|---|
| **Input** | Testo diretto owner con marker di permanenza ('da ora in poi', 'sempre', 'ogni volta'…) |
| **Output** | Istruzione attiva (condizione = parole dell'owner, azione allowlistata, provenance, ricevuta); conferma all'owner; per ogni messaggio in arrivo: valutazione semantica → notifica urgente se la condizione scatta |
| **Guardie** | Azione = vocabolario CHIUSO server-declared (il modello può solo scegliere, mai inventare); condizione = stringa bounded, DATA mai autorità; evidenza = frase esatta non quotata; testo nativo verificato; apply idempotente; ID forged droppati dalla valutazione |
| **Invarianti** | Solo testo owner vivo/diretto/non inoltrato può creare istruzioni; il contenuto dei messaggi in arrivo non può mai creare/modificare/revocare condizioni (è untrusted data per il valutatore); il dubbio non matcha (fail-closed); la notifica passa dal gateway esistente con ricevute e quiet hours; revocabile con ricevuta |

---

## 8. Gateway email — i tre gesti dell'owner

L'invio email è l'effetto più pericoloso: richiede **tre gesti distinti dell'owner**, con la proposta sigillata e il dispatch esplicito separati nel tempo.

```mermaid
sequenceDiagram
    autonumber
    participant O as Owner
    participant D as Dashboard (CSRF + sessione)
    participant EX as execution.py
    participant DB as PostgreSQL
    participant W as Worker execute_approved
    participant S as SMTP

    rect rgb(230,240,255)
        Note over O,DB: Gesto 1 — proposta
        O->>D: compone email (destinatario, testo, allegati)
        D->>EX: request_email
        EX->>DB: INSERT proposta sigillata (hash testo + allegati + destinatario)
        EX-->>D: proposta in attesa (nessun job, nessun invio)
    end

    rect rgb(230,255,230)
        Note over O,DB: Gesto 2 — approvazione
        O->>D: approva (ricevuta; chiave solo nel DOM)
        D->>DB: ricevuta di approvazione owner
    end

    rect rgb(255,245,230)
        Note over O,DB: Gesto 3 — dispatch esplicito
        O->>D: "Invia ora"
        D->>EX: dispatch_email (richiede la chiave della ricevuta di approvazione)
        EX->>DB: INSERT dispatch + job (solo se SECRETARY_MODE = active)
    end

    W->>DB: claim job → grant recheck → COMMIT attempt (PRIMA di SMTP)
    W->>DB: documenti ricaricati + hash verificati nella transazione attempt
    W->>S: invio MIME (byte in memoria, mai percorsi file)
    S-->>W: esito
    W->>DB: ricevuta: accepted / failed / uncertain
    Note over W: uncertain → MAI rinviato alla cieca
```

| | |
|---|---|
| **Input** | Bozza owner dalla dashboard; approvazione; dispatch esplicito |
| **Output** | Proposta sigillata; dispatch; email inviata; ricevute complete |
| **Guardie** | CSRF/origin/sessione dashboard; `SECRETARY_MODE=active` (`quiet_hours.sends_allowed`); grant recheck nel worker; hash allegati verificati |
| **Invarianti** | Tre gesti distinti e non aggirabili; attempt committato prima dell'SMTP; gli allegati sono ricostruiti da byte verificati in memoria; un invio "uncertain" non viene mai ritentato automaticamente |

---

## 9. La macchina a stati delle ricevute

Ogni effetto esterno lascia una traccia immutabile con transizioni a senso unico.

```mermaid
stateDiagram-v2
    [*] --> authorized: prepare_effect (grant + policy verificati)
    authorized --> attempted: COMMIT attempt nel DB (prima dell'I/O)
    attempted --> accepted: I/O confermato dal canale
    attempted --> failed: I/O fallito in modo deterministico
    attempted --> uncertain: esito sconosciuto (crash / timeout)
    failed --> [*]: retry deterministico con backoff (solo se idempotente)
    uncertain --> [*]: intervento owner richiesto, mai resend automatico
    accepted --> [*]
```

**Perché conta**: "accepted ≠ delivered" — la ricevuta dice cosa l'agente *sa*, non cosa *spera*. Un effetto `uncertain` (crash tra attempt e I/O) resta visibile e richiede una decisione dell'owner: il sistema non indovina.

---

## 10. Riconciliazione task

I task vivono o muoiono per **evidenza quotata**: il modello può proporre cambi solo citando testualmente la fonte, e l'auto-applicazione ha condizioni strettissime.

```mermaid
flowchart TD
    ING["Ingresso live (non storico)<br/>→ job reconcile"]
    CAND["Candidati ≤ 200<br/>batch da 40"]
    CONSENT{"Consensi ricontrollati<br/>TRA i batch"}
    MODEL["brain.reconcile_tasks<br/>(RECONCILIATION_PROMPT)"]
    CHANGES["parse_changes<br/>(richiede _explicit_quote)"]
    AUTO{"certain + unique + quoted<br/>+ non-cancel?"}
    APPLY["apply_change:<br/>lock task → lock proposta ·<br/>provenance recheck · cleanup derivati"]
    PROP["Proposta all'owner:<br/>/conferma_attivita · /rifiuta_attivita"]

    ING --> CAND --> CONSENT
    CONSENT -->|"revocato"| STOP3["Stop — nessun dato al modello"]
    CONSENT -->|"ok"| MODEL --> CHANGES --> AUTO
    AUTO -->|"sì"| APPLY
    AUTO -->|"no"| PROP
```

| | |
|---|---|
| **Input** | Job `reconcile` da ingresso live |
| **Output** | Cambi task auto-applicati (solo strettissimi) o proposte con comandi owner |
| **Guardie** | Candidati limitati; consensi ricontrollati tra i batch; citazione esplicita obbligatoria |
| **Invarianti** | Auto-apply solo per `certain + unique + quoted + non-cancel`; i task derivati (calendar/reminder) vengono ripuliti quando il task cambia; lo storico non riconcilia mai nulla |

---

## 11. Digest giornaliero

Il riassunto quotidiano è un effetto **sigillato dal sistema**, non testo libero del modello: ogni sorgente nativa passa per il filtro dei permessi.

```mermaid
flowchart TD
    TIMER["maybe_send_digest<br/>(cursore del giorno)"]
    WIN["Finestra 24h · piano 48h"]
    PLAN["plan_pending_tasks:<br/>task owner → solo promemoria ·<br/>task legacy → promemoria + proposta calendario locale"]
    PERM["_permitted_sources:<br/>OGNI sorgente nativa filtrata per consenso"]
    SUM["brain.summarize_day (DIGEST_PROMPT)"]
    SEAL["_queue_notification:<br/>system_notifications sigillata + job<br/>(transazione atomica)"]
    QH{"quiet_hours ·<br/>ordinamento owner-reply"}
    SEND["Notifica digest all'owner"]

    TIMER --> WIN --> PLAN --> PERM --> SUM --> SEAL --> QH
    QH -->|"quiet"| LATER["Slitta e riprova"]
    QH -->|"ok"| SEND
```

| | |
|---|---|
| **Input** | Cursore giornaliero del digest |
| **Output** | Notifica sigillata `system_notifications` + job di invio |
| **Guardie** | `_permitted_sources` su ogni sorgente; quiet hours; cursore idempotente |
| **Invarianti** | Notifica e job sigillati nella stessa transazione; il digest non può mai contenere dati di un canale revocato |

---

## 12. Guardie trasversali: budget AI, quiet hours, modalità

### Budget AI (`ai_budget.py`) — fail-closed, con scope

```mermaid
flowchart TD
    REQ["Richiesta modello<br/>(brain → ai_budget.request_content)"]
    SCOPE{"scope derivato server-side<br/>dal system prompt:<br/>global / dialogue / review /<br/>research / analysis"}
    LIM{"_reserve (advisory lock):<br/>globali: chiamate < 200/gg · token < 500k/gg ·<br/>in-flight < 2<br/>+ tetto per-scope opzionale<br/>(AI_BUDGET_*_DAILY_CALLS)"}
    CALL["Chiamata provider"]
    TRUNC{"Risposta troncata?"}
    RETRY["Retry 1× con budget raddoppiato"]
    SETTLE["_settle: usage + costo + scope<br/>nel ledger (secretary_ai_usage)"]

    REQ --> SCOPE --> LIM
    LIM -->|"superato"| FC["Rifiuta — fail-closed, nessun bypass"]
    LIM -->|"ok"| CALL --> TRUNC
    TRUNC -->|"sì"| RETRY
    RETRY -->|"ancora troncata"| TC["TruncatedCompletion (visibile)"]
    TRUNC -->|"no"| SETTLE
    RETRY -->|"ok"| SETTLE
```

Il tetto per-scope **restringe mai supera** quello globale: un cap `dialogue=50`
limita le chiamate del dialogo ma non libera spazio sul tetto globale.

### Quiet hours + modalità (`quiet_hours.py`)

```mermaid
flowchart LR
    NOT["Notifica in coda"]
    URG{"Urgente / emergenza?"}
    QH{"is_quiet?<br/>(fasce notturne)"}
    MODE{"sends_allowed?<br/>SECRETARY_MODE == 'active'"}
    SEND["Invio con owner_reply_mid<br/>+ currency check"]

    NOT --> URG
    URG -->|"sì"| MODE
    URG -->|"no"| QH
    QH -->|"sì"| DEFER["defer_until → riprova più tardi"]
    QH -->|"no"| MODE
    MODE -->|"no"| HOLD["Trattenuta — nessun invio"]
    MODE -->|"sì"| SEND
```

**Invarianti**: il budget fallisce **chiuso** (mai "un ultimo sforzo"); le emergenze bypassano le quiet hours ma mai la modalità; `SECRETARY_MODE` è l'interruttore generale di tutti gli invii in uscita.

---

## 13. Esempio end-to-end

Una pratica che nasce, viene osservata, revisionata e notificata — il ciclo completo dell'agente.

```mermaid
sequenceDiagram
    autonumber
    participant O as Owner
    participant TG as Telegram bot
    participant PL as pipeline
    participant DL as owner_dialogue
    participant SE as secretary
    participant RT as secretary_runtime
    participant EM as Email (IMAP)

    O->>TG: "Occupati della riparazione del tetto."
    TG->>PL: store + job 'owner' (testo diretto, non inoltrato)
    PL->>DL: respond (lease 12 min)
    DL->>SE: prepare → apply (CAS)
    SE-->>RT: pratica v1 + ricevuta + revisione programmata
    DL-->>O: "Pratica creata. Ti tengo aggiornato."
    EM->>PL: email del fornitore (arrivo live)
    PL->>RT: analysis → observe (osservazione per scope)
    RT->>SE: review — fonti riaperte, consensi ricontrollati
    SE-->>RT: piano di revisione validato (citazioni esatte)
    RT->>RT: apply (CAS v2) + notifica coalesced
    RT-->>O: "Novità sul tetto: è arrivato il preventivo."
    O->>TG: conferma link / revoca (stop condition strutturale)
```

**Fil rouge dell'esempio**: a ogni passo, l'autorità viene dalla provenance nativa (il testo diretto dell'owner, l'email reale del fornitore), mai dall'output del modello; ogni effetto lascia ricevute immutabili; ogni consenso è ricontrollato al confine, non fidato a memoria.

---

*Documento generato il 2026-10-18 dopo un giro completo di sorgenti e test (~78 file). Le fonti di verità restano i sorgenti: aggiornare questo file quando l'architettura cambia.*
