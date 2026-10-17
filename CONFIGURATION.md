# Integrazione dashboard del segretario

Il servizio attuale integra un **segretario di revisione limitato**, non un motore
di workflow arbitrari. `db.init()` applica anche le migrazioni execution/ai_budget
prima dei flussi; `main()` avvia otto worker persistenti: analysis, media,
reconcile, action, owner, knowledge, secretary_review e secretary_email_execution.
Indicizzazione, osservazioni, revisioni dovute e backlog sono collegati al runtime.
La dashboard non migra schemi e non avvia worker alla richiesta. Le sezioni sotto
conservano le procedure di setup e i dettagli dei percorsi preesistenti, con i
limiti attuali esplicitati. Il consenso salvato non certifica ogni chiamata legacy;
le chiamate dirette fuori dai job non hanno il budget persistente Worker.
Non eseguire prove contro account, sessioni o DB di produzione.

## API HTTP locali

Tutte richiedono login dashboard, controllo Host e `Cache-Control: no-store`.
Le POST richiedono CSRF e stessa origine; nessuna riceve SQL, URL, percorsi,
comandi, credenziali del proprietario o flag di autorità dal client.

| Rotta | Contratto |
| --- | --- |
| `GET /api/secretary` | Proiezione di `secretary.snapshot()`: pratiche, responsabilità, approvazioni, link, ricevute, eventi e wakeup; max 24/raccolta e indicatori truncated |
| `GET /api/secretary/runtime` | Proiezione di `secretary_runtime.snapshot(limit=20)`: stati, bozze/proposte e attenzione con versione/hash; senza input preparati, prompt o configurazioni |
| `POST /api/secretary/runtime/links/<id>/accept` | Solo expected_version della proposta mostrata; evidenza/revisione e versioni pratica/delega rilette dal server; ricevuta e nuova revisione, nessun effetto esterno |
| `POST /api/secretary/runtime/practices/<id>/acknowledge` | Solo expected_version della pratica e attention_hash mostrato; presa in carico dell'attenzione invariata, non completamento o autorizzazione |
| `GET /api/secretary/support` | Supportato vs pianificato; nessun segreto o valore env privato |
| `GET /api/knowledge` | `knowledge.snapshot(limit=20)`, documenti e stati di indicizzazione |
| `GET /api/knowledge/search` | `q` non vuoto ≤512 caratteri, `limit` 1–50, `channel` esatto opzionale ≤80; parametri sconosciuti rifiutati |
| `GET /api/knowledge/open/<type>/<id>` | Tipi message/document/entry/note/practice, ID positivo; nessun path/URL. not_found resta uno stato della fonte |
| `GET /api/data-policies` | Nove coppie email/telegram/whatsapp × ai_text/ai_media/ai_history; riga assente = false, versione 0, nessuna scrittura alla lettura |
| `POST /api/data-policies` | Esattamente channel/effect/allowed/expected_version; booleano JSON, CAS sulla versione; 0 inserisce solo se assente |
| `POST /api/media/retry-blocked` | Solo message_id/expected_channel nativi; recupero esplicito di media correnti bloccati per consenso, non modifica i consensi |
| `GET /api/capabilities` | Stato dei soli opzionali, letto da flows; nessun secondo archivio di flag |
| `POST /api/flows/<id>` | Interruttore esistente, booleano enabled; non concede consenso dati né deleghe |
| `POST /api/secretary/responsibilities/<id>` | Esattamente action (`suspend`/`revoke`) ed expected_version positivo; nessun resume/create/scope arbitrario |
| `POST /api/secretary/approvals/<id>` | Esattamente payload/decision/expected_version/operation_key; decision approve/revoke; chiave stabile `secretary-approval:…` |
| `POST /api/secretary/emails` | practice_id/expected_version/payload/account_address/expires_at/operation_key espliciti; prepara proposta email sigillata, non invia |
| `GET /api/secretary/emails/<id>` | Snapshot di mittente, oggetto fisso, payload, versione, scadenza e supporto documenti |
| `POST /api/secretary/emails/<id>/dispatch` | expected_version/account_address/subject/payload/approval_operation_key/operation_key; secondo gesto esplicito, accoda solo identificatori |
| `GET /api/secretary/execution` | Max 24 proposte/esiti e 50 indirizzi mittente, nessun segreto SMTP/token job; accepted non significa consegnato |
| `GET /api/secretary/budget` | Contatori e limiti aggregati UTC dei soli contesti Worker; costi mancanti ignoti, nessun tetto monetario |

Errori: 400 validazione, 401 login, 403 autorità/CSRF/origine, 409 versione o
payload cambiato, 413 corpo eccessivo, 501 modulo opzionale assente, 503
schema/servizio indisponibile. Nessun dettaglio di errore DB/provider viene
restituito. Il corpo normale è ≤4096 byte; approvazioni e preparazione/dispatch
email ≤49152 byte per testo UTF-8 fino a 8000 caratteri e documenti vincolati.
Link/ack/retry-media ed email usano il parser rigoroso che rifiuta chiavi duplicate
e JSON non finito; non estendere questa garanzia a tutte le rotte legacy.
Upload/config mantengono i propri limiti. La ricerca è locale e non chiama il
provider AI.

## Approvazioni e controlli restrittivi

La dashboard costruisce `secretary.DashboardOwner` **solo nel backend** dopo il
guard di autenticazione/CSRF, usando `TELEGRAM_CHAT_ID` configurato. Chi dispone
delle credenziali dashboard è quindi amministratore/proprietario fidato, non un
utente con permessi ridotti. Senza owner numerico valido approve/revoke/stop sono
rifiutati. Nessun caller client viene deserializzato.

`secretary.resolve_approval` verifica payload esatto (canale, destinatario, testo,
documenti e relativi hash), versione, scadenza e versioni delle dipendenze. La UI
mostra l’intero payload e il digest e rimanda lo stesso oggetto, senza rigenerarlo
da un modello. Matching retry restituisce la ricevuta. La revoca resta possibile
dopo scadenza; approvare non esegue né accoda alcuna azione esterna. Il runtime
revisioni non consuma queste approvazioni per inviare a terzi. Il gateway email
separato richiede il secondo gesto di dispatch: mittente configurato esplicito,
oggetto **Messaggio approvato**, payload e ricevuta di approvazione esatti.
La UI conserva `approval_operation_key` solo nel DOM, non nello storage browser.
Il worker email è già avviato da `main()`, ma una coda non prova la consegna.
Supportato solo testo: allegati non vuoti rifiutati, non omessi; thread/Message-ID
non supportati. Tentativo persistito prima di SMTP; timeout/crash/esito incerto
non autorizzano un reinvio automatico. Revocare non richiama un invio in volo.

L'accettazione di un link usa esclusivamente la proposta persistita e la sua
versione: rilegge fonte/revisione e ambito, avanza la pratica, invalida approvazioni
dipendenti e accoda una revisione. Non è un comando per collegare qualsiasi
risultato di ricerca. L'ack confronta versione pratica e hash dell'attenzione:
silenzia il contenuto invariato e impedisce notifiche di revisione già superate o
riconosciute ai controlli d'invio. Nessuno dei due concede deleghe o invii esterni.

Poiché `policy` non espone gestione consensi e `secretary` non espone uno stop
dashboard, gli helper `_server_data_policy` e `_server_stop_responsibility`
vivono **solo in dashboard.py**. SQL parametrizzato, identificatori fissi e
transazioni; lo stop usa il medesimo advisory lock secretary, CAS, ricevuta e
audit append-only, invalidando approvazioni pending/approved anche attraverso le
pratiche collegate. La revoca è terminale; le pratiche non sono dichiarate
completate né cancellate. Nessun tentativo di fermare un HTTP già in corso.
Questi helper sono un confine temporaneo da sostituire con API pubbliche core,
non un motore generico di mutazioni.

## Consenso, supporto e migrazione

- Nessun auto-allow nel wizard, alla prima lettura o attivando capabilities.
  `ai_text`, `ai_media` e `ai_history` sono separati; la granularità è **canale**, non
  indirizzo email/account/chat. Un deny non elimina file né revoca il provider.
- `policy.can_read_context` distingue il contesto storico di sola lettura:
  servono testo + storico del canale, e media per documenti/media. `ai_history`
  da solo non basta e non è un effetto operativo. `policy.can_operate` continua
  a negare operazioni storiche; consenso storico non abilita comandi, attività,
  invii o conversioni media storiche. Non ogni consumer usa lo storico (il digest
  operativo lo esclude). I contenuti terzi restano evidenza, mai autorità.
- I nuovi piani proprietario di pratiche/responsabilità sono v2: `input_sources`
  risolti dal server a tutte le fonti native esportate, inclusi snapshot/storia,
  e `input_versions` dei record letti. Piani applicati/ricevute e revisioni runtime
  conservano dipendenze precedenti; derivazioni mancanti/cicliche non autorizzano
  egress AI. Un replay v1 non prova il consenso alla derivazione.
- Il resolver condiviso owner/task-reconciliation segue origini delle attività e
  **tutte** le evidenze `task_changes` applicate, non solo `last_evidence_id`.
  Proposte pending/rejected escluse; fonti applicate mancanti/negate bloccano quel
  contesto. Non è la provenienza completa degli input modello dei vecchi piani
  task/memoria, né un modello completo di memoria tipizzata e conflitti.
- `SECRETARY_MODE=observe|active`, `SECRETARY_QUIET_HOURS` globale e
  `SECRETARY_AI_DAILY_BUDGET` sono **riservati/pianificati**, commentati in
  `.env.example`, in sola lettura nella UI. Non usarli come interruttori di
  sicurezza; oggi non hanno consumer runtime.
- Sono supportati `SECRETARY_REVIEW_QUIET_START`/`END`: interi 0–23, default
  22 e 8 nel TIMEZONE. Inizio=fine disattiva la fascia. Ritardano solo le
  notifiche di revisione, non digest, emergenze o tutte le chiamate AI.
  `SECRETARY_REVIEW_MIN_MINUTES` (5–10080, default 60) limita il prossimo
  riesame; `SECRETARY_REVIEW_ACK_MINUTES` (1–10080, default 1440) limita
  riproposizioni dello stesso contenuto non riconosciuto. Richiedono runtime
  migrato (già incluso in `db.init()`) e riavvio; il solo modulo non prova readiness.
- `ai_budget.py` applica nei soli contesti job Worker limiti persistenti:
  `AI_BUDGET_DAILY_CALLS=200`, `AI_BUDGET_DAILY_TOKENS=500000` (giorno UTC),
  `AI_BUDGET_MAX_INFLIGHT=2`, `AI_BUDGET_MAX_OUTPUT_TOKENS=65536` per default.
  Ogni tentativo/retry riserva chiamata e byte input serializzato + output massimo
  + margine, senza rimborso; uso dichiarato superiore è addebitato. DB/ledger/lease
  indisponibili bloccano le richieste Worker; slot di processi morti richiedono
  riconciliazione manuale dopo aver escluso richieste vive. Otto worker non
  significano otto richieste AI simultanee. Chiamate dirette/legacy e generazione
  digest periodica fuori dai job non hanno questi cap persistenti. Non è un
  tetto monetario globale: impostare limiti sulla chiave OpenRouter. Valori web
  positivi fino a 2147483647 (output fino a 65536), applicati dopo riavvio;
  valori ereditati dall'ambiente restano read-only.
- `capabilities.migrate(conn, legacy=…)` deve ricevere la provenienza catturata
  **prima** della migrazione base: fresh opzionali false, legacy compatibili
  senza sovrascrivere flag espliciti. Provenienza assente/sconosciuta: opzionali
  in pausa, non dedurre consenso o nuove responsabilità dai vecchi dati.

La dashboard non importa file di configurazione privati nelle viste segretario e
non restituisce piani o input modello. La configurazione amministrativa rimane
una proiezione allowlist con segreti nascosti, non il file env grezzo. Nessun
localStorage/sessionStorage; testo/snippet/payload resi con textContent, non HTML.
Per i requisiti bootstrap vedere [OPERATIONS.md](OPERATIONS.md).

## Calendar sigillato e limiti del piano

I grant `secretary_effect_grants_v3` richiedono testo proprietario nativo corrente
e sigillano evento/stato, ID Calendar esplicito configurato, TIMEZONE e tempi di
creazione risolti (inizio/fine/durata/promemoria). Il worker rilegge target e
scadenza prima dell'effetto e usa il target sigillato. Grant scaduti o legacy senza
target sono negati; replay non rinnova o ricollega un vecchio consenso alla nuova
configurazione. Serve una nuova autorizzazione proprietario, non un backfill.
Le scadenze task non concedono Calendar: eventuali eventi restano proposte.
`CALENDAR_EXTERNAL_AUTO`/`CALENDAR_TRUSTED_SENDERS` non attivano più auto-creazione
per etichetta mittente nel percorso integrato. Segnare spam/newsletter come letti
è solo comportamento del classificatore privato legacy, non dell'ingest attuale.
Anche notifiche/ZIP del pipeline registrano il tentativo prima dell'I/O: esiti
attempted/uncertain non idempotenti richiedono riconciliazione, non reinvio cieco.

Restano fuori dal completamento: workflow/dipendenze arbitrari; memoria tipizzata,
conflitti e provenienza completa degli input dei vecchi piani task/memoria;
collegamento generale di evidenze esistenti da retrieval alle pratiche; condizioni
di stop eseguibili (non basta il testo salvato); Calendar update/disponibilità/
drift remoto; allegati e thread di invio email; modalità/tetto monetario globali;
erase/export privacy coordinati; prova completa di backup ripristinato,
validazione in produzione e valutazione semantica. Il piano architetturale rimane
una roadmap. Questa modifica è solo documentale: nessun dato utente letto,
test/comando terminale eseguito o risultato di suite dichiarato; la validazione
completa corrente sarà aggiunta dal run principale.

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
Analisi/dialogo non usano testo parziale; fonti bloccate/non disponibili hanno
segnalazioni esplicite. Il testo derivato è riutilizzabile solo nei percorsi che
ne verificano le fonti; non tutti i consumer lo esportano (il digest attuale non
carica trascrizioni). La sola conversione non esegue comandi, non invia risposte
ai contatti e non crea appuntamenti.

Al primo avvio la migrazione additiva accoda anche audio/immagini già archiviati
con byte o file disponibili: i soli job ammessi dai gate possono generare
richieste e costi OpenRouter. Le fonti storiche restano negate alla conversione
operativa anche con `ai_history`: l'import non chiama l'AI inline, ma nemmeno
autorizza conversioni o azioni successive riavviando il servizio.
Non recupera automaticamente i vecchi media MTProto archiviati solo come metadati:
devono essere riacquisiti dal recupero o dall'importazione storica.
I risultati vision possono contenere errori: controllare dati critici.

Per recuperare una fonte **corrente** dopo aver concesso testo e media, aprirla
in Documenti e ricerca e usare **Riprova media bloccati dal consenso**. Il POST
`/api/media/retry-blocked` accetta solo `message_id` e `expected_channel`
(`telegram`, `telegram (account)`, `whatsapp`); non concede consenso. Esclude
storico, trascrizioni presenti e tentativi provider già registrati; massimo
20 allegati per richiesta e tre reset per allegato. Non recupera automaticamente
alla modifica dei permessi. Il worker ricontrolla consenso e fonte prima della
richiesta/pubblicazione; non aggirare il controllo con reset SQL di massa.

## Budget AI e protezione dai troncamenti

Questi sono i budget di output per richiesta (ragionamento incluso, dove previsto),
non la finestra di contesto né una spesa fissa. Nei Worker si aggiungono i cap
persistenti sopra; la tabella non estende tali cap alle chiamate legacy dirette:

| Funzione | Primo tentativo | Retry se troncato |
| --- | ---: | ---: |
| Dialogo e agenda | 16.384 | 32.768 |
| Analisi comunicazioni | 8.192 | 16.384 |
| Memoria personale | 4.096 | 8.192 |
| Digest | 8.192 | 16.384 |

Per gli audio: **4.096 + 20 token per secondo**, fino a 32.768 al primo
tentativo. Esempi: 30 s = 4.696; 5 min = 10.096; 20 min = 28.096.
Senza durata valida si usano 16.384 token. La durata Telegram viene conservata
anche nell'archivio; audio storici o altri canali senza durata usano il fallback.
Il retry raddoppia il budget, con tetto assoluto di 65.536 token.
Questi margini non garantiscono una trascrizione completa per audio arbitrariamente
lunghi: al limite occorre dividerli. Il modello/provider scelto deve supportare
i limiti di output richiesti (indipendentemente dalla sua finestra di contesto).

Le richieste tramite `ai_budget.request_content` scartano le risposte troncate e
rigenerano al massimo una volta dai messaggi originali, se il cap output lo
consente. Nessuna data viene ricostruita da testo parziale,
nessun reasoning viene usato come risposta finale. Le trascrizioni non vengono
più tagliate silenziosamente a 16.000 caratteri: oltre 262.144 caratteri si segnala
un errore. Ragionamento `low`; le istruzioni di risposta concisa restano attive.

Nella chat del bot sono supportati messaggi vocali, audio e foto (anche con
didascalia). Con fonte/consensi ammessi, OpenRouter trascrive l'audio prima del
dialogo. Le foto del proprietario possono essere inviate come parti `image_url`
(data URL base64) al modello principale multimodale, insieme a didascalia,
identità dell'assistente e sottoinsieme consentito di archivio/memoria/storia.
Il dialogo persistente corrente recupera contesto con controlli di fonte/consenso;
non promette di reinviare la foto a ogni turno. Il limite degli ultimi 10 messaggi
in RAM appartiene solo al vecchio percorso diretto di compatibilità. Nessun tool
remoto arbitrario: marker Calendar validati localmente non sostituiscono intenti
proprietario nativi e grant sigillati.

- `OPENROUTER_AUDIO_MODEL`: `google/gemini-2.5-flash` (input audio).
- `OPENROUTER_MODEL`: `google/gemini-2.5-flash` (testo e immagini nativi).
- Entrambi usano `OPENROUTER_API_KEY`; `OPENROUTER_MODEL` è il modello principale.
  Viene usato per tutte le conversazioni, con o senza immagini, e deve supportare
  **nativamente input testo e immagini** (`image_url`): è un requisito obbligatorio,
  non sostituibile con OCR. Lo stesso modello genera anche il testo persistente;
  nessun modello immagini separato.
  Il prompt visivo è agnostico rispetto al soggetto della foto.
  Se la richiesta visiva fallisce o non restituisce testo, il fallback elimina le
  immagini, dichiara che non sono disponibili e non può creare eventi calendario.
  Non viene mostrato il reasoning interno del provider come risposta.
- I due modelli sono modificabili anche nella configurazione amministrativa AI.
- Limite per media: 10 MiB. I vocali Telegram OGG/Opus vengono inviati come
  `input_audio` base64; scegliere un provider compatibile con tale formato.
- Audio e immagini vengono trasmessi ai provider OpenRouter: valgono costi e
  condizioni di trattamento dati dei provider selezionati.
- Gli allegati sono archiviati prima della conversione media; errori provider
  seguono retry limitati, blocchi di consenso restano terminali senza recupero
  esplicito idoneo. Nessun contenuto parziale viene usato come trascrizione.
  Solo la chat privata del proprietario configurato in `TELEGRAM_CHAT_ID` è
  accettata dal bot. Senza proprietario configurato nessun messaggio è accettato.
  Chat estranee, gruppi e canali sono ignorati prima di download, log dei contenuti,
  archiviazione o analisi AI, senza risposte; gli update vengono comunque consumati.
- Il testo delle immagini non viene interpretato come comando rapido o nuova regola.
- Le immagini non vengono inviate al trascrittore audio: il worker vision usa
  `OPENROUTER_MODEL`. L'estrazione generica PDF/XML resta separata e invariata.
- L'account personale Telegram resta in sola lettura verso Telegram (niente invii
  o ricevute di lettura), ma ora scarica vocali e immagini per archivio e conversione.

Per installazioni Docker è necessario ricostruire e ricreare il container per
caricare il codice aggiornato. Non sono necessarie nuove dipendenze Python.

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
  `TELEGRAM_CHAT_ID`. Nuovo bot/proprietario richiede `setup_telegram.py`.
- Email basic: login IMAPS e SMTP TLS, nessun SELECT cartella né invio. Solo
  endpoint dei preset noti; custom host e OAuth Microsoft richiedono il terminale
  (`configure`, `setup_ms_auth.py`). Anche modifiche email nominali richiedono
  credenziali valide: non si dichiara successo senza verifica del batch candidato.
- Calendar: validazione RSA locale, scambio JWT sull'endpoint Google fisso,
  lettura metadati e CalendarList con ruolo writer/owner. Nessun evento viene
  creato. Il calendario deve essere anche presente in CalendarList; un calendario
  condiviso ma non elencato viene rifiutato conservativamente.
- WhatsApp e Telegram personale: i flag NON costituiscono un'autorizzazione.
  Campi di autorizzazione mostrati ma in sola lettura con istruzioni per
  `setup_whatsapp.py` (QR) / `setup_telegram_account.py` (OTP/2FA) nel deployment.
  La dashboard non raccoglie OTP, non avvia script e non promette readiness.
- Sonde HTTP: solo HTTPS vendor fisso, niente redirect né proxy ambientali.
  Errori remoti e segreti non sono restituiti né loggati dall'API.

Questa sezione non sostituisce un secret manager: i valori necessari al prossimo
avvio restano sul server in file privati. Usare solo su rete fidata o dietro HTTPS
correttamente configurato. Un amministratore locale del filesystem resta fidato.

## Setup Calendar

`setup_gcal.py` apre direttamente l'input JSON nascosto, compatto o multilinea;
una riga `FINE` conclude, INVIO iniziale annulla. Non esiste più la scelta `[i]`.
Il percorso CLI mantiene il proprio flusso con consenso esplicito per l'evento
di prova; la nuova transazione dashboard è separata e non crea eventi.