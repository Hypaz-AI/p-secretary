# p-secretary · un agente per le tue comunicazioni

Segretario personale containerizzato (Docker, multi-piattaforma): raccoglie email, WhatsApp e
Telegram, archivia tutto su PostgreSQL e **lascia che sia un agente a ragionare**.
Non esistono pipeline di classificazione, schemi di piano o flussi fissi: quando
scrivi al bot, o quando arriva un messaggio, un agente ([GitHub Copilot SDK](https://github.com/github/copilot-sdk),
LLM via **OpenRouter**, `openrouter/auto` di default) decide cosa guardare e cosa
fare usando strumenti tipizzati:

| Strumento | Cosa permette |
|---|---|
| `db_schema`, `db_query` | query SQL **libere in sola lettura** sulle viste `agent.*` (messaggi, allegati, documenti, attività, appuntamenti, note, regole, fatture, dialoghi) |
| `read_message`, `attachment_text` | testo completo di un messaggio, testo estratto/trascritto di un allegato |
| `fs_list/find/read/write/mkdir` | file in `/data` (lettura) e nella cartella **`workspace/`** (scrittura, visibile nella pagina «Archivio e file» della dashboard) |
| `mail_accounts/search/read` | ricerca e lettura **dal vivo** delle caselle email (IMAP, sola lettura) |
| `web_search` | ricerca sul web (plugin web di OpenRouter), senza budget dedicato |
| `task_change`, `tasks_close_bulk`, `note_save`, `note_forget` | scritture sulle tue attività e note, solo da una **tua frase esatta** e con ricevuta |
| `calendar_propose`, `email_propose` | **proposte**: l'evento nasce locale (`/conferma ID` per sincronizzarlo), la bozza email aspetta il tuo `/invia ID` |
| `triage`, `task_from_message`, `notify_urgent`, `archive_invoice` | solo nella posta in arrivo: registrano l'esito, avvisano **solo per vere emergenze** (tetto giornaliero), archiviano fatture |

**Tre modi di lavorare**, stesso agente, strumenti diversi:

- **Dialogo** — scrivi al bot in linguaggio naturale («quante fatture ho ricevuto a
  settembre?», «chiudi tutto tranne la call con Omar», «cerca nelle mail il
  preventivo del fornitore e scrivimi un riepilogo in `workspace/`»). I comandi
  slash restano come gesti espliciti: `/conferma`, `/annulla`, `/invia`, `/scarta`,
  `/fatto`, `/task`, `/attivita`, `/appuntamenti`, `/note`, `/regole`, `/fatture`.
- **Posta in arrivo** — per ogni messaggio (anche a lotti) l'agente decide se è
  rilevante, lo riassume, estrae al più attività con citazione esatta e **non
  disturba** salvo emergenze reali (`AGENT_NOTIFY_MAX_PER_DAY`, default 5).
- **Riepilogo serale** — a `DIGEST_TIME` l'agente scrive il recap delle ultime 24 ore
  e il piano delle successive 48; il server sigilla testo e fonti prima dell'invio.

## Cosa NON decide mai il modello

Il modello **non è autorità**: lo è la provenienza nativa verificata dal server.

- **Chi sei**: solo il testo nativo, diretto, non inoltrato, in chat privata dal bot
  vale come tuo ordine. Email, inoltri, storico, altre chat e contenuti degli
  allegati sono **dati non fidati**: possono informare una risposta, mai comandare.
- **Cosa legge**: i consensi per canale (testo / media / storico) sono incorporati
  nelle viste `agent.*` e rivalutati **a ogni query**; revocarne uno nasconde subito
  i dati, anche a una sessione già aperta. Default: tutto negato.
- **Come scrive**: strumenti tipizzati, argomenti validati, ricevuta idempotente
  nella stessa transazione dell'effetto; ogni scrittura sul tuo conto richiede una tua
  frase esatta (citazione verificata dal server sul messaggio archiviato).
- **Effetti verso l'esterno**: un tentativo è registrato **prima** dell'I/O; un esito
  incerto non viene mai rinviato alla cieca; «accettato» non significa «consegnato».
  Calendar = grant sigillato su payload e calendario di destinazione; email = bozza +
  tuo `/invia` + modalità `SECRETARY_MODE=active`.
- **Sicurezza SQL**: una sola `SELECT` per volta, solo relazioni `agent.*`, funzioni
  consentite, transazione READ ONLY con timeout, cursore con tetto di righe e
  `SET LOCAL ROLE` a un ruolo `agent_ro` che vede **solo** quelle viste. L'app lo crea da
  sola al primo avvio (DB locale); con un DB esterno senza `CREATEROLE` il DBA esegue due
  istruzioni una tantum ([`deploy/sql/agent_ro.sql`](deploy/sql/agent_ro.sql)).
- **La chiave OpenRouter non entra mai nel runtime dell'agente**: ogni richiesta passa
  da un gateway locale (loopback, bearer monouso per turno) che applica il ledger
  di budget (`AI_BUDGET_*`), limita l'output e legge costo/token reali.

L'AI può sbagliare e i contenuti analizzati vengono inviati al provider: valuta
privacy e costi. Non è un sistema di emergenza.

## La dashboard

Rete locale, login dedicato, CSRF e same-origin. Sei pagine, con menu laterale (barra in
basso su telefono), tema scuro/chiaro e nessuna risorsa esterna:

- **Panoramica**: KPI con andamento a 14 giorni, grafico dei messaggi per canale, budget AI di
  oggi, flussi con interruttore, code di lavoro, traffico, attività recente, salute dei connettori.
- **Agente**: modello e protezione del database, grafico delle esecuzioni, tabella filtrabile con il
  dettaglio degli strumenti usati, bozze email (nessun prompt né risposta del modello).
- **Permessi AI**: matrice dei consensi con interruttori (ogni cambio chiede conferma) e recupero
  dei media bloccati.
- **Budget AI**: anelli di saturazione, soglie colorate, tetti per ambito, andamento giornaliero,
  riparazione degli slot bloccati.
- **Impostazioni**: elenco dei servizi a sinistra, un servizio alla volta a destra, riepilogo prima
  di salvare.
- **Archivio e file**: file (con filtro e percorso), fatture e ZIP mensili.

La dashboard non decide né invia nulla per conto dell'agente: bozze e
sincronizzazioni passano dal tuo gesto su Telegram. Per sviluppare l'interfaccia con dati
fittizi su un database usa-e-getta: `python -m tests.demo_dashboard` (vedi `docs/OPERATIONS.md`).

## Stato e limiti

- `main()` avvia sei worker durevoli: `analysis` (posta in arrivo), `owner`
  (dialogo), `digest`, `action` (unico punto con effetti esterni), `media`,
  `knowledge` (estrazione documenti). Lease di 15 minuti, heartbeat di 30 secondi,
  massimo otto tentativi con backoff.
- L'agente lavora a **sessioni effimere**: ogni turno ricostruisce il contesto dal DB
  e la sessione viene cancellata, così nessun testo di terzi resta nella cronologia
  del runtime. Nel dialogo le risposte precedenti dell'assistente rientrano solo
  per `AGENT_HISTORY_RESPONSE_MINUTES` (default 20): oltre quel tempo l'agente
  rilegge i dati con gli strumenti.
- Non c'è esecuzione di workflow arbitrari, shell, né accesso ad altro filesystem
  che `/data`. Il runtime parte in modalità `empty` e rifiuta ogni permesso che non
  sia uno strumento dell'agente.
- Restano aperti: aggiornamento/disponibilità Calendar e drift remoto, email in
  thread e con allegati, erase nei backup, prova di ripristino completa e
  valutazione qualitativa dell'agente sul tuo archivio.

Dettaglio dei flussi: [AGENT_FLOWS.md](docs/AGENT_FLOWS.md). Baseline per chi sviluppa:
[AGENTS.md](AGENTS.md). Configurazione: [CONFIGURATION.md](docs/CONFIGURATION.md).
Operatività: [OPERATIONS.md](docs/OPERATIONS.md).

### Struttura del repository

```
secretary/        pacchetto Python (`python -m secretary`): core, archive, workflows,
                  agent, connectors, dashboard (API + `static/`), setup, maintenance
tests/            suite unittest, una cartella per pacchetto (`python -m unittest discover`)
docs/             flussi, configurazione, operatività, guida Docker, note storiche
deploy/           entrypoint Docker, manifest del ponte WhatsApp, SQL del DBA, unità systemd
Dockerfile, compose*.yaml, p-secretary (launcher), requirements.txt, .env.example
```

## Nuove installazioni e aggiornamenti

Fatture e ZIP sono opzionali e partono disattivati. Un aggiornamento da una versione
precedente conserva i flag espliciti e **non** concede consensi AI. Le tabelle del vecchio
segretario (pratiche, responsabilità, revisioni, esecuzione email) non vengono più
usate: restano nel DB finché non le rimuovi tu con
[`deploy/sql/drop_legacy_tables.sql`](deploy/sql/drop_legacy_tables.sql), da eseguire solo dopo un backup.

# Importazione storica una tantum

Il comando `sh ./p-secretary import-history --days 30` importa nell'archivio DB
le comunicazioni degli ultimi 30 giorni dalle connessioni abilitate. Omettendo
`--days`, lo script chiede il numero di giorni (default 14; ammessi 1–3650).
L'intervallo è calcolato al momento dell'avvio, include l'inizio ed esclude la fine.

Il comando ferma l'app per liberare le sessioni Telegram/WhatsApp, ricostruisce
l'immagine e avvia un container temporaneo. **Al termine l'app resta ferma**:
riavviarla con `sh ./p-secretary up`. Non eseguire altri runtime sulle stesse
sessioni durante l'importazione. In installazioni Python dirette è disponibile
`python -m secretary.archive.historical_import --days 30`, dopo aver fermato il runtime.

- Email: INBOX e cartella Inviate riconosciuta, allegati inclusi, senza segnare
  i messaggi come letti. Cartella Inviate assente o errori producono un esito
  incompleto; i dati già acquisiti restano salvati.
- Telegram personale: tutte le conversazioni accessibili e paginazione fino
  all'inizio dell'intervallo; vocali e immagini supportati vengono scaricati
  entro il limite di 10 MiB; eventuali job media restano soggetti ai gate sotto,
  non una promessa di conversione storica.
- WhatsApp: solo storico sincronizzato disponibile sul dispositivo collegato,
  non garanzia di recupero completo dal server.
- Telegram bot: lo storico passato non è disponibile tramite Bot API.
- Calendar: escluso perché contiene appuntamenti, non comunicazioni.

Nessuna richiesta AI sincrona, comando, risposta o scrittura Calendar viene avviata
dallo script. I media possono essere accodati, ma **la coda non aggira il consenso
né autorizza azioni al riavvio**: le fonti storiche restano negate alla conversione
operativa e al replay di analisi/task/Calendar/invii, anche con consenso storico.
`ai_history` abilita solo letture AI di contesto supportate, insieme a testo e,
per documenti/media, media; non riattiva il digest operativo sullo storico.
Il vincolo vale per import marcati storici dall'aggiornamento, non certifica la
provenienza di vecchie righe prive di tale marcatura.
Le identità native evitano duplicati in archivio: i conteggi includono anche
messaggi già presenti. Nessun watermark di recupero ordinario viene avanzato.
Rieseguire dopo un errore è sicuro. Codici di uscita: 0 completato, 1 errore
(anche parziale), 2 copertura limitata/non supportata, 130 annullato.

Robustezza del recupero: le email con Date assente/non valida usano INTERNALDATE;
messaggi malformati vengono segnalati per UID senza bloccare i successivi (esito
finale comunque incompleto). La scansione IMAP può leggere l'intera casella per
gestire correttamente le date malformate, quindi caselle grandi richiedono tempo.
Telegram rispetta i FloodWait con un budget cumulativo di 300 secondi: attese
superiori richiedono di riprovare più tardi, non vengono aggirate.
WhatsApp continua sulle altre chat in caso di errore, salva i messaggi disponibili
anche prima di segnalare un errore parziale e usa recuperi progressivi fino a
12.800 messaggi per chat; il raggiungimento del limite è segnalato come parziale.

# Contratto onboarding Docker

I setup opzionali restituiscono `0` (ready), `2` (skipped/consenso rifiutato),
`1` (failed). Ready indica il risultato del setup, non una garanzia permanente
di connettività. `progress.json` contiene questi stati; i booleani legacy vengono
normalizzati. `complete` significa soltanto configurazione confermata.

`configure` permette modifiche preservando i valori con INVIO; i segreti non
vengono mostrati. I campi vuoti vengono riproposti. Timezone, lingua, modello e
orario digest sono validati. Cambiare URI DB non migra i dati.
OpenRouter è obbligatoria prima dell'avvio app: se manca, usare `configure`.
Telegram personale viene abilitato soltanto dopo autorizzazione e consenso.

Il launcher rilegge la modalità DB dopo il wizard e ricrea app con la nuova
configurazione. Attende fino a 120 secondi con Compose `--wait`.
`/healthz` è pubblico, soggetto al controllo Host, e restituisce solo
`{"status":"started"}`: nessun dato personale, DB o chiamata esterna.
Non certifica i connettori: dashboard distingue configurazione, interruttore
enabled e salute osservata dai worker. `configured` non significa ready.

## Riepilogo serale e promemoria

A `DIGEST_TIME`, nel `TIMEZONE` configurato, l'agente scrive il recap delle ultime
24 ore e il piano delle successive 48, anche senza nuovi messaggi (un job durevole
`digest:AAAA-MM-GG`: ripeterlo o riavviare non crea copie). Le scadenze personali
(`/task`, date-only → promemoria alle 09:00 locali; orario preciso → alla scadenza,
oppure l'orario richiesto) sono aggiunte in una sezione **deterministica**, quindi
compaiono anche se il modello le omette.

- Il recap usa solo i dati che i tuoi consensi rendono leggibili; senza fonti
  consentite il testo è neutro e **non si chiama il modello**.
- Creare un'attività, anche con `/task`, **non autorizza Calendar**: gli eventi
  derivati restano proposte locali (`/conferma ID`).
- Il testo è sigillato con i messaggi che il modello ha potuto leggere: se prima
  dell'invio un consenso viene revocato, l'invio si ferma e il lavoro si ripete.
- Accodato/accettato non significa consegnato; un invio incerto richiede
  riconciliazione, non un reinvio automatico.

## Attività e promemoria

`/task titolo | 2026-11-03T10:00+01:00` (scadenza facoltativa), `/attivita`, `/fatto ID`
funzionano senza modello. In dialogo l'agente può creare, modificare, completare o
annullare attività, anche in blocco («chiudi tutto tranne…»), **solo** se la richiesta
è una tua frase esatta nel messaggio corrente; le email non possono modificare le tue
attività (solo proporne di nuove con citazione esatta, senza notifiche). Ogni modifica
è versionata, con evidenza e ripulitura di promemoria/eventi derivati. Una cancellazione
Calendar remota richiede un nuovo grant esplicito `/annulla ID`.

## AI: immagini native e audio separato

`OPENROUTER_MODEL` **DEVE supportare nativamente input testo e immagini**
(`image_url`); il default è `google/gemini-2.5-flash`. Serve a descrivere le immagini
e a trascrivere i media archiviati; **non** è il modello dell'agente (vedi `AGENT_MODEL`).
Vocali e immagini supportati acquisiti
da Telegram (bot/account) e WhatsApp possono essere convertiti in testo tramite
la coda persistente `media` se la fonte nativa corrente consente testo e media.
I vocali usano `OPENROUTER_AUDIO_MODEL`; le immagini usano il modello principale
per estrarre testo visibile e una descrizione visiva strutturata, non semplici
stringhe base64 né i token visivi interni del provider.
Il risultato è salvato in PostgreSQL, tabella `media_transcripts`, collegata
all'allegato, e l'agente lo legge dalla vista `agent.documents` (sempre dopo il consenso media);
il digest non carica trascrizioni salvo che l'agente le apra con `attachment_text`.
Originali e didascalie restano invariati. Sono inclusi messaggi inviati/ricevuti,
recupero e import storico **nell'archivio/coda, non nel permesso a convertire**.
Anche i media archiviati con byte/file possono essere accodati dalla migrazione:
lo storico resta bloccato operativamente, e il consenso negato produce un blocco
terminale. Per i soli blocchi correnti idonei esiste il recupero esplicito sopra.
Le conversioni ammesse hanno costi AI; import/riavvio non concedono consenso.
Non vengono contattati mittenti o eseguiti comandi dalla sola trascrizione.
L'estrazione documentale PDF/XML resta separata e invariata.
Il bot accetta soltanto la chat privata del proprietario configurato.
Per limiti, fallback e trattamento dati vedere `CONFIGURATION.md`.
Le installazioni esistenti con un modello solo testuale devono scegliere un
modello multimodale; i file di configurazione privati non vengono migrati.

## Google Calendar: solo service account IAM

Calendar non supporta login OAuth utente. In Google Cloud abilita Calendar API,
crea un account in IAM → Account di servizio (nessun ruolo di progetto richiesto),
poi Chiavi → Aggiungi chiave → JSON. Rispetta le policy dell’organizzazione.
Il setup Calendar nel wizard permette **incolla nascosto** (JSON compatto o
multilinea, termina con una riga `FINE`) oppure importazione da file. Serve un
terminale con input nascosto; se non disponibile, l’incolla viene bloccato.
Non inserire chiavi in chat, argomenti di shell o upload generici. La dashboard
protetta dispone di un campo JSON Calendar dedicato con validazione separata,
senza evento di prova: vedere [CONFIGURATION.md](docs/CONFIGURATION.md#setup-calendar).

La chiave viene validata localmente e salvata atomicamente con permessi `0600`;
ogni sostituzione richiede consenso. Il default Docker è
`/var/lib/p-secretary/gcal_service_account.json`, corrispondente a
`P_SECRETARY_HOME/data/gcal_service_account.json` sull’host. Il file da importare
deve essere visibile **dentro il container**: un percorso del PC non basta.
L’incolla evita di dover trasferire il file. `GCAL_SERVICE_ACCOUNT_FILE` indica
un percorso visibile sia al setup sia all’app; montare separatamente eventuali
percorsi personalizzati fuori dalla directory dati.

Condividi il calendario con `client_email` del JSON, autorizzando “Apportare
modifiche agli eventi”. Da Impostazioni → Integra calendario copia l’ID esplicito:
**non usare `primary`**. Il setup richiede consenso per la verifica di rete e,
separatamente, per creare/cancellare l’evento di prova prima di abilitare Calendar.
Le vecchie variabili `GCAL_CLIENT_ID` / `GCAL_CLIENT_SECRET` e il file
`gcal_tokens.json` sono ignorati: revocare eventuali vecchie autorizzazioni
Google e rimuovere privatamente quei residui se non più necessari.
L’OAuth delle **email Microsoft** rimane invariato.

Segretario personale containerizzato: raccoglie email, WhatsApp e Telegram, analizza
con OpenRouter, prepara riepiloghi, archivia fatture e gestisce attività e
proposte di appuntamento. Le integrazioni sono opzionali: l'AI richiede una
chiave OpenRouter; dialogo e notifiche richiedono un bot Telegram.
I contenuti analizzati vengono inviati al servizio AI: valuta privacy e costi.

## Primo avvio con Docker

Servono **Git, Python 3 sull'host, Docker e il plugin Docker Compose
recente** con supporto a `up --wait`, più una shell POSIX (`sh`). L'immagine è
multi-piattaforma: viene costruita per l'architettura dell'host (amd64 o arm64, per
esempio un Raspberry Pi 4). Il tuo utente deve già avere accesso al daemon Docker:
il launcher non usa sudo e non installa Docker. Serve Internet;
Node e Chromium vengono installati nell'immagine, non sull'host.

Sul computer che ospiterà il servizio:

```sh
git clone https://github.com/Hypaz-AI/p-secretary.git
cd p-secretary
sh ./p-secretary setup
```

Se hai già il repository, entra nella sua directory invece di clonarlo.
`setup` **costruisce già l'immagine**: non serve una build manuale né copiare
`.env.example`. Il wizard chiede:

1. URI PostgreSQL: **premi Invio lasciandolo vuoto per il DB locale**.
   Le credenziali vengono generate e salvate privatamente. Non devi installare
   PostgreSQL sull'host. L'URI esterno serve solo se hai un DB dedicato.
2. Nome agente, chiave OpenRouter, fuso orario, lingua, modello e ora digest.
3. Se aggiungere email (fino a 50). La configurazione di entità fatture (fino a
  quattro) è opzionale e non abilita automaticamente fatture o ZIP.
4. Se eseguire login per bot Telegram, Telegram personale, WhatsApp, Calendar
   e Microsoft: puoi rispondere **no** e completarli dopo.

Le domande con «INVIO = salta» sono saltabili; dove previsti si usano i default.
Saltare credenziali non configura l'integrazione. Alla fine conferma il wizard,
poi accetta separatamente la verifica DB e l'avvio app, oppure rimandali.
Per avviare dopo la conferma e controllare:

```sh
sh ./p-secretary up
sh ./p-secretary status
sh ./p-secretary logs
```

L'app inizializza/migra il DB configurato e può iniziare raccolta e invii:
non usare per prova un database di produzione esistente.

## Aprire la dashboard

Sull'host apri **http://127.0.0.1:8080**: la porta deve essere libera.
Compose pubblica solo sul loopback host, non sulla LAN. Per un server remoto,
esegui **dal tuo PC**:

```sh
ssh -N -L 18080:127.0.0.1:8080 UTENTE@SERVER
```

Sostituisci `UTENTE` con l'utente SSH e `SERVER` con nome/IP del server;
apri poi **http://127.0.0.1:18080** sul tuo PC. Non esporre HTTP su Internet.

Al primo avvio della dashboard vengono generate credenziali dedicate in
`~/.local/share/p-secretary/config/dashboard.json`. Apri il file **privatamente
sull'host con un editor locale** e leggi `username` e `password`. Non sono
le credenziali Linux; non condividere il file. Con `P_SECRETARY_HOME` diverso,
cerca nella sua sottocartella `config/`.

La pagina **File** permette navigazione/download di cartelle dati consentite e
fatture. Upload solo in `uploads/`, massimo **20 MiB**, senza sovrascrittura e
**senza analisi AI automatica**. Non espone l'intero server: chiavi, configurazioni,
token, sessioni e spool sono esclusi. File referenziati dal DB non sono eliminabili;
la cancellazione è bloccata anche se il DB non è disponibile.

## Dati e gestione quotidiana

Il default è `~/.local/share/p-secretary/`, fuori dal repository:

| Percorso host | Contenuto |
|---|---|
| `config/secretary.env` | Configurazione e credenziali integrazioni |
| `config/dashboard.json` | Login dashboard |
| `config/postgres.env` | Credenziali DB locale |
| `data/` | Allegati, token, sessioni, stato e archivio `invoices/` |

Il DB locale usa il volume Docker `p-secretary_postgres`. `P_SECRETARY_HOME`
cambia i percorsi host, **non crea una seconda istanza**: progetto e volume
restano gli stessi. Backup di DB e filesystem sono a tuo carico.

- `sh ./p-secretary configure`: ferma l'app dopo conferma, ripropone login e
  campi mancanti; **non sostituisce valori già presenti**.
- Per cambiarli, modifica privatamente `config/secretary.env`, poi esegui
  `sh ./p-secretary down` e `sh ./p-secretary up`: il file è letto all'avvio.
  Il solo `up` non forza il riavvio di un container già attivo.
- `sh ./p-secretary doctor`: prova connessione DB, senza avviarlo o migrare.
- `sh ./p-secretary down`: rimuove container/rete, **conserva il volume DB**.

Per account, troubleshooting e migrazione leggi la [guida Docker](docs/DOCKER.md).
[OPERATIONS.md](docs/OPERATIONS.md) è il riferimento tecnico per operatori, in inglese.

**Limiti:** nessun import storico illimitato, backup o aggiornamento automatico,
né consegna esattamente una volta. Gli appuntamenti rilevati sono proposte da
confermare; gli ZIP dei mesi chiusi sono accodati dal mese successivo. L'AI può
sbagliare: non è un sistema di emergenza. Packaging sperimentale, senza promessa
di immagini prebuilt, build riproducibili o compatibilità con ogni hardware.
