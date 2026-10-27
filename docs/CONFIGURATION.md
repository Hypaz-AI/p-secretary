# Configurazione e contratti

Il servizio è **un agente con strumenti tipizzati** (vedi [AGENT_FLOWS.md](AGENT_FLOWS.md)),
non un motore di workflow. `db.init()` applica le migrazioni additive prima dei flussi;
`main()` avvia sette worker durevoli: `analysis`, `media`, `action`, `owner`, `digest`,
`knowledge`, `notion`. La dashboard non migra schemi e non avvia worker alla richiesta.
Non eseguire prove contro account, sessioni o DB di produzione.

## Variabili dell'agente

Tutte hanno un default e sono validate all'avvio (fuori intervallo = default). Le prime tre
e `SECRETARY_MODE` / `EFFECT_AUDIT_RETENTION_DAYS` si modificano anche dalla dashboard
(Impostazioni); le altre solo da `config/secretary.env`.

| Variabile | Default | Significato |
| --- | --- | --- |
| `AGENT_MODEL` | `openrouter/auto` | Modello dell'agente; deve supportare il tool calling. `openrouter/auto` lascia scegliere a OpenRouter |
| `AGENT_WEB_MODEL` | `OPENROUTER_MODEL` | Modello che esegue la ricerca web (plugin `web` di OpenRouter) |
| `AGENT_NOTIFY_MAX_PER_DAY` | 5 (0–100) | Tetto di avvisi autonomi urgenti per giorno locale; serializzato con advisory lock |
| `AGENT_MAX_TOOL_CALLS` | 30 (1–200) | Chiamate agli strumenti per turno |
| `AGENT_OWNER_TIMEOUT` | 240 s (30–1800) | Durata massima di un turno richiesto dall'owner |
| `AGENT_BACKGROUND_TIMEOUT` | 900 s (60–3300) | Durata massima di triage e riepilogo |
| `AGENT_LLM_TIMEOUT` | 180 s (20–600) | Timeout di ogni richiesta al provider |
| `AGENT_MAX_OUTPUT_TOKENS` | 16384 (1024–65536) | Token massimi di output per richiesta (esplicito: il ledger lo richiede) |
| `AGENT_INBOX_BATCH` | 8 (1–25) | Messaggi in arrivo valutati in un solo turno |
| `AGENT_HISTORY_RESPONSE_MINUTES` | 20 (0–240) | Le risposte passate dell'assistente rientrano nel contesto solo entro questa finestra |
| `AGENT_WORKSPACE_QUOTA_MB` | 200 | Quota della cartella `workspace/` |
| `AGENT_DB_SCHEMA` | `agent` | Schema delle viste interrogabili |
| `AGENT_DB_ROLE` | `agent_ro` | Ruolo PostgreSQL di sola lettura (vedi sotto) |
| `SECRETARY_MODE` | `observe` | `observe`: nessuna email a terzi, le bozze restano salvate; `active`: `/invia` può inviare |
| `EFFECT_AUDIT_RETENTION_DAYS` | 30 | Retention del registro di audit delle policy (0 la disattiva; le ricevute restano) |
| `RETENTION_MESSAGES_DAYS` | 730 (0–36500) | Messaggi scaduti per data di acquisizione, con allegati, derivati e file non più referenziati; 0 disattiva. Vedi [OPERATIONS.md](OPERATIONS.md#retention) |
| `RETENTION_OPERATIONAL_DAYS` | 90 (0–3650) | Payload dei job conclusi (chiavi conservate), outbox email inviata/rifiutata; 0 disattiva |
| `RETENTION_AI_USAGE_DAYS` | 365 (0–3650) | Ledger AI dei consumi completati oltre la finestra (prenotazioni incerte escluse); 0 disattiva |

`OPENROUTER_MODEL` e `OPENROUTER_AUDIO_MODEL` **non** sono il modello dell'agente: servono
solo a descrivere le immagini e trascrivere i vocali archiviati (sezione sotto).
`NOTIFY_PRIORITY_THRESHOLD` e `QUIET_HOURS_*` non esistono più: l'agente avvisa solo per vere
emergenze (tetto sopra) e il resto va nel riepilogo serale.

### Ruolo di sola lettura `agent_ro`

L'agente interroga solo lo schema `agent` (viste che incorporano i consensi). La barriera
principale è la validazione AST + transazione READ ONLY; il ruolo è la seconda barriera
indipendente: `NOLOGIN`, nessun attributo, `SELECT` solo sulle viste `agent.*`.

**Lo predispone l'app a ogni avvio** (`db.init`), per quanto i privilegi dell'utente del database lo consentono:

| Situazione | Cosa succede |
| --- | --- |
| **DB locale del compose** (utente `secretary`, superuser) | Il ruolo viene creato al primo avvio: nessun passo manuale |
| Utente con `CREATEROLE` | Come sopra |
| **DB esterno, utente senza `CREATEROLE`** (es. `svc_pi_secretary`) | Il DBA esegue **una volta** la PARTE 1 di [`deploy/sql/agent_ro.sql`](../deploy/sql/agent_ro.sql) (`CREATE ROLE` + `GRANT` all'utente applicativo); `USAGE` e `SELECT` sulle viste li concede l'app, che ne è proprietaria |
| Ruolo mancante e nessun privilegio | L'app parte lo stesso e stampa un avviso con le due istruzioni; la dashboard mostra «Protezione database: assente» |
| `AGENT_DB_ROLE=` (vuoto) | Nessun ruolo voluto: restano guardia AST e transazione READ ONLY |

Da PostgreSQL 16 il ruolo va **assunto** con il privilegio `SET`, che chi lo crea con `CREATEROLE`
non ha automaticamente: l'app lo concede con un `GRANT` esplicito e lo verifica a ogni avvio.

## Notion (Custom Agent, sola lettura)

Jarvis può delegare ricerche sul workspace Notion a un **Custom Agent** creato e configurato
prima nella UI di Notion (piano Business/Enterprise), tramite le Agent APIs (public beta,
`Notion-Version: 2026-03-11`). Un solo strumento, `notion_ask`, disponibile **solo nel modo
owner** e solo con messaggio diretto nativo; nessuna scrittura su Notion passa da qui: se la
sessione entra in `requires_action` la delega viene rifiutata, mai approvata.

| Variabile | Default | Significato |
| --- | --- | --- |
| `NOTION_ENABLED` | `false` | Abilita lo strumento `notion_ask` |
| `NOTION_API_KEY` | vuoto | Token della connessione interna con capacità «View sessions and interact with agents» e il solo Custom Agent in *Agent access* |
| `NOTION_AGENT_ID` | vuoto | ID del Custom Agent (`agent_type=custom_agent`, `status=active`, da `POST /v1/agents/query`) |
| `NOTION_FAST_WAIT_SECONDS` | 25 (0–60) | Attesa breve nel turno prima di rispondere «ricerca in corso» |
| `NOTION_HTTP_TIMEOUT_SECONDS` | 15 (5–60) | Timeout di ogni chiamata HTTP a `api.notion.com` |
| `NOTION_MAX_RUN_SECONDS` | 900 (60–7200) | Durata massima di una delega; oltre, il monitor la chiude come fallita |

`NOTION_ENABLED`, `NOTION_API_KEY` e `NOTION_AGENT_ID` si modificano anche dalla dashboard
(Impostazioni → gruppo *Notion (sola lettura)*). Al salvataggio con Notion attivo, token e ID
vengono verificati su `api.notion.com`: l'agente deve comparire tra i Custom Agent attivi della
connessione, altrimenti il salvataggio è rifiutato e il file resta invariato. Il token non viene
mai mostrato di nuovo. I tre timeout restano modificabili solo in `config/secretary.env`.

Ogni delega è una riga di `notion_delegations` committata **prima** della POST: un esito
ambiguo (timeout, 5xx) marca `uncertain` e non viene mai rigiocato, così una ricerca non può
duplicarsi. Le ricerche lente sono seguite dal worker durevole `notion` (polling ogni 60 s
senza consumare tentativi) e il risultato arriva su Telegram con una sola notifica
(`notion-result:<id>`). Il token non finisce mai in log o nel journal dell'agente.
Se il proprietario insiste sulla stessa ricerca, Jarvis può proseguire la stessa sessione
Notion (`continue_previous`): la sessione la sceglie il server (l'ultima conclusa), mai il
modello, e c'è al massimo un turno in volo per sessione.

Setup: creare il Custom Agent in Notion, condividere in *Tools and access* **solo** le pagine
e i database da leggere (permesso «Can view», verificato con un test negativo), creare il
token con l'accesso al solo agente e testarlo prima in Notion. Dettagli in `.env.example`.

## API HTTP locali

Tutte richiedono login dashboard, controllo Host e `Cache-Control: no-store`.
Le POST richiedono CSRF e stessa origine; nessuna riceve SQL, URL, percorsi, comandi,
credenziali del proprietario o flag di autorità dal client. `DashboardOwner` è costruito
**solo nel backend** dopo i controlli di sessione, usando `TELEGRAM_CHAT_ID`: chi ha le
credenziali dashboard è l'amministratore fidato, non un utente con permessi ridotti.

| Rotta | Contratto |
| --- | --- |
| `GET /api/dashboard` | Flussi, contatori, traffico, attività recente (solo metadati: nessun testo o contatto) |
| `GET /api/trends` | Serie giornaliere (14 giorni) per i grafici: messaggi per canale e direzione, chiamate/token/costo AI, esecuzioni dell'agente per stato. Solo conteggi aggregati, mai testo, contatti o prompt |
| `POST /api/flows/<flow>` | Interruttore esistente, booleano `enabled`; non concede consenso dati |
| `POST /api/platform-health` | Verifica manuale e di sola lettura di DB, email, bot, Calendar, chiave AI |
| `GET /api/agent` | Modello, protezione DB (`readonly_role`), `sends_allowed`, limiti, ultime 30 esecuzioni e 20 bozze email |
| `GET /api/agent/runs/<id>` | Strumenti chiamati in un'esecuzione: nome, argomenti limitati, esito, durata (max 60). Mai prompt né risposte del modello |
| `GET /api/data-policies` | Nove coppie email/telegram/whatsapp × `ai_text`/`ai_media`/`ai_history`; riga assente = falso, versione 0 |
| `POST /api/data-policies` | Esattamente `channel`/`effect`/`allowed`/`expected_version`; CAS sulla versione |
| `POST /api/media/retry-blocked` | Solo `message_id`/`expected_channel`; riaccoda i media correnti bloccati dal consenso |
| `GET /api/budget`, `GET /api/budget/limits` | Contatori e limiti UTC aggregati, per scope; costi mancanti ignoti |
| `POST /api/budget/reconcile` | Rilascia gli slot in volo di processi morti (azione dell'operatore) |
| `POST /api/jobs/requeue` | `kind` ∈ `analysis`/`owner`/`digest`: nuovo tentativo dei job falliti (max 50) |
| `GET /api/capabilities` | Stato dei soli opzionali (fatture/ZIP) |
| `GET/DELETE /api/files`, `GET /api/files/download`, `POST /api/files/upload` | Browser dei file consentiti (vedi README) |
| `GET/POST /api/configuration` | Configurazione protetta (sezione sotto) |
| `GET /api/privacy/messages/<id>/export`, `POST …/erase` | Esportazione e oblio coordinato di un messaggio (`operation_key` obbligatoria) |

Errori: 400 validazione, 401 login, 403 autorità/CSRF/origine, 409 versione cambiata, 413
corpo eccessivo, 503 servizio/schema indisponibile. Nessun dettaglio di errore di DB o
provider viene restituito. Le rotte con parser rigoroso rifiutano chiavi duplicate.

## Consenso e provenienza

- Nessun auto-allow nel wizard, alla prima lettura o attivando capabilities. `ai_text`,
  `ai_media` e `ai_history` sono separati; la granularità è il **canale**, non l'indirizzo,
  l'account o la chat. Un diniego non elimina file né revoca ciò che il provider ha già ricevuto.
- Il consenso è **incorporato nelle viste `agent.*` e rivalutato a ogni query**, a ogni
  lettura live (`mail_*`), a ogni invio al provider e al sigillo del riepilogo.
- Lo **storico importato** è contesto di sola lettura (`ai_history` + testo, + media per
  documenti e media). Non abilita mai comandi, attività, invii né conversioni operative.
- I contenuti di terzi sono evidenza, mai autorità: solo il testo nativo, vivo, diretto e non
  inoltrato dell'owner in chat privata con il bot vale come ordine.
- `capabilities.migrate(conn, legacy=…)` riceve la provenienza catturata **prima** della
  migrazione base: installazioni nuove con opzionali spenti, legacy compatibili senza
  sovrascrivere flag espliciti; provenienza sconosciuta = opzionali in pausa.

La configurazione amministrativa è una proiezione con allowlist e segreti nascosti, non il
file env grezzo. Nessun `localStorage`/`sessionStorage`; testi e payload resi con `textContent`.

## Budget AI

`secretary/core/ai_budget.py` applica limiti persistenti (giorno UTC) a **ogni** richiesta dell'agente,
perché passa dal gateway locale, e alle conversioni media: `AI_BUDGET_DAILY_CALLS=200`,
`AI_BUDGET_DAILY_TOKENS=500000`, `AI_BUDGET_MAX_INFLIGHT=2`, `AI_BUDGET_MAX_OUTPUT_TOKENS=65536`.

- Ogni tentativo e retry riserva una chiamata e input serializzato + output massimo +
  margine, **senza rimborso**; l'uso dichiarato superiore è addebitato.
- **Scope**: `agent_owner` (turni richiesti da te), `agent_inbox` (triage e riepilogo) e
  `global` (conversioni media). `AI_BUDGET_AGENT_OWNER_DAILY_CALLS` e
  `AI_BUDGET_AGENT_INBOX_DAILY_CALLS` aggiungono un tetto per scope **dentro** quello globale:
  possono restringere, mai alzare. Lo scope lo dichiara il server, mai il modello.
- DB, ledger o lease non disponibili = richiesta rifiutata prima del provider. Il budget
  esaurito **differisce** il job senza consumare tentativi.
- Gli slot di processi morti richiedono riconciliazione manuale (`POST /api/budget/reconcile`)
  dopo aver escluso richieste vive. Otto worker non significano otto richieste simultanee.
- `AI_BUDGET_DAILY_COST` (opzionale) è un tetto monetario sui costi **riportati** dal
  provider; le richieste senza costo riportato non contano: imposta anche un limite di spesa
  sulla chiave presso OpenRouter. La **ricerca web non ha un budget dedicato** (decisione esplicita).
- Valori web: interi positivi fino a 2147483647 (output fino a 65536), applicati dopo il
  riavvio; i valori ereditati dall'ambiente del processo restano in sola lettura.

## Calendar sigillato

I grant `secretary_effect_grants_v3` richiedono il testo nativo corrente dell'owner (`/conferma`)
e sigillano evento/stato, ID Calendar esplicito configurato, `TIMEZONE` e tempi risolti
(inizio, fine, durata, promemoria). Il worker rilegge target e scadenza prima dell'effetto
e usa il target sigillato. Grant scaduti, o legacy senza target, sono negati; un replay non
rinnova né ricollega un vecchio consenso a una nuova configurazione.

- L'agente **non** scrive su Calendar: `calendar_propose` crea solo una proposta locale.
- Creare un'attività, anche con `/task`, non concede Calendar.
- Notifiche, ZIP ed email registrano il tentativo **prima** dell'I/O: gli esiti
  `attempted`/`uncertain` richiedono riconciliazione, non un reinvio cieco.

Restano fuori: aggiornamento e disponibilità Calendar e drift remoto, allegati e thread nelle
email in uscita, erase nei backup, prova completa di un backup ripristinato.

# Vocali e immagini Telegram e WhatsApp

## Archivio testuale persistente

Per i media supportati acquisiti da Telegram bot, account personale MTProto e
WhatsApp la coda `media` può produrre testo salvato in PostgreSQL
(`media_transcripts`, chiave `attachment_id`) solo se la fonte nativa corrente
passa entrambi i consensi testo/media. Anche inviati, recuperati e importati
possono essere archiviati/accodati: la presenza del job non autorizza il provider.
Il vocale è trascritto fedelmente; l'immagine è inviata come `image_url` al modello
principale e convertita in testo visibile, descrizione visiva, dati operativi e
incertezze. I token visivi interni non sono testo esportabile: si conserva una
rappresentazione testuale analizzabile e non la codifica base64 dell'immagine.

Originali e didascalie non vengono modificati. I risultati riusciti sono riusati
fra riavvii/retry. Gli errori provider seguono i retry limitati; il consenso negato
produce stato terminale `blocked/media_consent_required`, non retry automatico.
L'agente non usa testo parziale; fonti bloccate o non disponibili hanno
segnalazioni esplicite (`messages.media_missing` per gli allegati non persistiti). Il testo
derivato si legge dalle viste `agent.documents`/`attachment_text`, sempre dopo il consenso media. La sola conversione non esegue comandi, non invia risposte
ai contatti e non crea appuntamenti.

Al primo avvio la migrazione additiva accoda anche audio/immagini già archiviati
con byte o file disponibili: i soli job ammessi dai gate possono generare
richieste e costi OpenRouter. Le fonti storiche restano negate alla conversione
operativa anche con `ai_history`: l'import non chiama l'AI inline, ma nemmeno
autorizza conversioni o azioni successive riavviando il servizio.
Non recupera automaticamente i vecchi media MTProto archiviati solo come metadati:
devono essere riacquisiti dal recupero o dall'importazione storica.
I risultati vision possono contenere errori: controllare dati critici.

Per recuperare una fonte **corrente** dopo aver concesso testo e media, usare
la pagina **Permessi AI** → **Recupero media bloccati dal consenso**. Il POST
`/api/media/retry-blocked` accetta solo `message_id` e `expected_channel`
(`telegram`, `telegram (account)`, `whatsapp`); non concede consenso. Esclude
storico, trascrizioni presenti e tentativi provider già registrati; massimo
20 allegati per richiesta e tre reset per allegato. Non recupera automaticamente
alla modifica dei permessi. Il worker ricontrolla consenso e fonte prima della
richiesta/pubblicazione; non aggirare il controllo con reset SQL di massa.

## Budget di output dei media

Il budget di output dell'agente è `AGENT_MAX_OUTPUT_TOKENS`. Le conversioni media usano un
budget proprio, non la finestra di contesto né una spesa fissa:

- **Immagini**: 16.384 token.
- **Vocali**: **4.096 + 20 token per secondo**, fino a 32.768. Esempi: 30 s = 4.696;
  5 min = 10.096; 20 min = 28.096. Senza durata valida (audio storico o canale senza durata)
  si usano 16.384. La durata Telegram è conservata nell'archivio.
- `ai_budget.request_content` scarta le risposte troncate e rigenera **una volta** dai messaggi
  originali, con budget raddoppiato e tetto assoluto di 65.536. Nessun testo parziale o
  ragionamento viene usato come trascrizione, e nessuna data è ricostruita da testo parziale.
- Le trascrizioni non vengono tagliate in silenzio: oltre 262.144 caratteri si segnala un errore.
- Questi margini non garantiscono una trascrizione completa per audio arbitrariamente lunghi:
  al limite vanno divisi. Il modello scelto deve supportare l'output richiesto.

- `OPENROUTER_AUDIO_MODEL`: `google/gemini-2.5-flash` (input audio, `input_audio` base64;
  scegli un provider compatibile con OGG/Opus).
- `OPENROUTER_MODEL`: `google/gemini-2.5-flash`; deve supportare **nativamente** testo e
  immagini (`image_url`): requisito obbligatorio, non sostituibile con OCR. Genera anche il
  testo persistente delle immagini; non esiste un modello immagini separato. Se la richiesta
  visiva fallisce, il fallback non usa le immagini e non crea nulla.
- Entrambi usano `OPENROUTER_API_KEY` e sono modificabili dalla configurazione amministrativa.
- Limite per media: 10 MiB. Audio e immagini sono trasmessi ai provider OpenRouter: valgono
  costi e condizioni di trattamento dati dei provider selezionati.
- Gli allegati sono archiviati **prima** della conversione. Solo la chat privata del proprietario
  in `TELEGRAM_CHAT_ID` è accettata dal bot; senza proprietario configurato nessun messaggio lo è.
  Chat estranee, gruppi e canali sono ignorati prima di download, archiviazione o analisi,
  senza risposta (gli update sono comunque consumati).
- Il testo di un'immagine o di un vocale non è mai interpretato come comando.
- L'estrazione generica PDF/XML resta separata. L'account personale Telegram resta in sola
  lettura (niente invii né ricevute di lettura), ma scarica vocali e immagini.

Per installazioni Docker occorre ricostruire e ricreare il container per caricare il codice
aggiornato. Il runtime dell'agente è incorporato nell'immagine (non si scarica all'avvio).

# Configurazione dashboard

La sezione **Configurazione protetta** usa la stessa sessione, CSRF, controlli
Host/Origin e intestazioni no-store della dashboard. Non esiste API shell/exec.
Aprire i gruppi, modificare i campi necessari e scegliere **Verifica e salva**.
I segreti non sono restituiti: il campo vuoto mantiene il valore precedente.
Non usare l'upload generico per chiavi private. Il JSON Calendar si incolla nel
campo dedicato, mantenuto solo nella richiesta e cancellato dalla UI dopo l'esito.
Nessun localStorage/sessionStorage viene utilizzato.

## Transazione e applicazione

- Allowlist: identità/AI, database, 50 email (tutti i campi supportati), 4 entità,
  preferenze, Calendar e parametri di recupero Telegram personale.
- Revisione opaca HMAC, lock condiviso con `config.set_env`, validazione prima
  della pubblicazione, singola sostituzione atomica privata 0600 del file env.
- Un errore in un campo annulla l'intero batch senza riavvio.
  Dopo un salvataggio riuscito in Docker, il ciclo principale attende almeno
  2 secondi per la risposta HTTP, chiude ordinatamente worker e connettori,
  quindi esce. La policy Compose `restart: unless-stopped` riavvia il container
  e il nuovo processo rilegge il file env persistente. Nessun socket Docker
  o shell API. La dashboard sarà temporaneamente indisponibile.
  Fuori da Docker occorre un riavvio manuale. `os.environ` non viene mutato.
  Per installazioni esistenti ricostruire l'immagine e ricreare il servizio
  tramite il launcher per applicare il nuovo codice e la policy.
  Nessuna migrazione o copia dei dati viene effettuata cambiando database.
- Il JSON Calendar è verificato isolatamente in memoria, senza cache live;
  dopo verifica viene scritto in `.calendar-keys` privata presso il file env,
  quindi pubblicato tramite il puntatore env nello stesso commit. Le chiavi
  precedenti sono conservate per il processo ancora attivo e recupero manuale.
  Un crash prima del commit può lasciare un file privato non referenziato,
  ma non pubblicare una configurazione parziale. Non cancellarlo durante uso.
- I valori ereditati dall'ambiente del processo sono **sola lettura**: scrivere
  il file non può prevalere su Docker. Paths, bind, porte, file credenziali,
  eseguibile Chromium e percorso sessione sono sempre deployment-managed.
  Modificarli esternamente e ricreare il container consapevolmente.

## Verifiche e limiti intenzionali

- PostgreSQL: connessione candidata reale, TLS verify-full, IP pubblico fissato
  dopo controllo DNS, transazione read-only e solo `SELECT 1`, timeout limitati.
  DB locali/privati (incluso `db` Compose), URI con query e TLS avanzato devono
  essere gestiti dal terminale. La dashboard non testa né cambia il DB locale
  tramite sonde SSRF. Nessuna migrazione viene avviata.
- OpenRouter: endpoint autenticato `/api/v1/key`, nessuna generazione a pagamento.
- Telegram bot: solo `getMe`, niente pairing, aggiornamenti o invii. Rotazione
  consentita solo per la medesima identità bot già configurata, mantenendo
  `TELEGRAM_CHAT_ID`. Nuovo bot/proprietario richiede `python -m secretary.setup.telegram`.
- Email basic: login IMAPS e SMTP TLS, nessun SELECT cartella né invio. Solo
  endpoint dei preset noti; custom host e OAuth Microsoft richiedono il terminale
  (`configure`, `python -m secretary.setup.ms_auth`). Anche modifiche email nominali richiedono
  credenziali valide: non si dichiara successo senza verifica del batch candidato.
- Calendar: validazione RSA locale, scambio JWT sull'endpoint Google fisso,
  lettura metadati e CalendarList con ruolo writer/owner. Nessun evento viene
  creato. Il calendario deve essere anche presente in CalendarList; un calendario
  condiviso ma non elencato viene rifiutato conservativamente.
- WhatsApp e Telegram personale: i flag NON costituiscono un'autorizzazione.
  Campi di autorizzazione mostrati ma in sola lettura con istruzioni per
  `python -m secretary.setup.whatsapp` (QR) / `python -m secretary.setup.telegram_account` (OTP/2FA) nel deployment.
  La dashboard non raccoglie OTP, non avvia script e non promette readiness.
- Sonde HTTP: solo HTTPS vendor fisso, niente redirect né proxy ambientali.
  Errori remoti e segreti non sono restituiti né loggati dall'API.

Questa sezione non sostituisce un secret manager: i valori necessari al prossimo
avvio restano sul server in file privati. Usare solo su rete fidata o dietro HTTPS
correttamente configurato. Un amministratore locale del filesystem resta fidato.

## Setup Calendar

`python -m secretary.setup.gcal` apre direttamente l'input JSON nascosto, compatto o multilinea;
una riga `FINE` conclude, INVIO iniziale annulla. Non esiste più la scelta `[i]`.
Il percorso CLI mantiene il proprio flusso con consenso esplicito per l'evento
di prova; la nuova transazione dashboard è separata e non crea eventi.

## Chat escluse (WhatsApp e Telegram personale)

- `WHATSAPP_EXCLUDED_CHATS` e `TELEGRAM_EXCLUDED_CHATS`: elenchi di ID separati da virgole
  (massimo 200). Modificabili dalla dashboard (Impostazioni → Telegram e WhatsApp), effetto dopo il riavvio.
- Il blocco è **a monte**, nel connettore, prima di qualsiasi lettura del contenuto, download di media,
  scrittura nello spool o nell'archivio, e prima di ogni analisi o job AI:
  - WhatsApp: il ponte Node scarta i messaggi di chat escluse in `archive()` (live, recupero,
    import storico) e non interroga le chat escluse; Python elimina dallo spool le voci residue.
  - Telegram personale: `archive()` esce prima di ogni RPC o download (live, recupero, import
    storico); i dialog esclusi non vengono neppure letti.
  - Un ID malformato viene ignorato con un avviso che non ne riporta il contenuto.
- Il bot Telegram accetta solo la chat privata del proprietario (`TELEGRAM_CHAT_ID`): non ha una
  lista di esclusione.
- Non cancella i messaggi già archiviati. Per trovare gli ID:
  `SELECT DISTINCT channel, chat_id, sender FROM communications WHERE channel IN ('whatsapp','telegram (account)');`