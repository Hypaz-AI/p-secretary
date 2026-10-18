# Pi Secretary · spazio personale sotto controllo

Documenti, ricerca, memoria, attività e responsabilità sono il centro
dell’interfaccia; **fatture e ZIP sono opzionali**, non il profilo obbligatorio.
La dashboard aggiunge quattro pannelli autenticati:

- **Cosa seguo per te**: pratiche, prossima azione, attese, revisioni,
  responsabilità e proposte. Sospensione/revoca usa la versione letta (CAS),
  registra una ricevuta e revoca le approvazioni collegate. Nessuna riattivazione
  implicita o nuova delega dal browser.
- **Documenti e ricerca**: catalogo e ricerca lessicale italiana con citazioni,
  testo estratto, stato parziale/storico e apertura della fonte DB. Non è una
  ricerca semantica; un file caricato genericamente non viene indicizzato da solo.
- **Permessi AI**: consenso esplicito per email/Telegram/WhatsApp × testo/media,
  più **storico come contesto di sola lettura**, negato in assenza di una scelta.
  Telegram comprende bot e account personale: non esiste granularità per
  chat/account. Lo storico richiede anche testo e, per documenti/media, media;
  non autorizza comandi o azioni successive. Abilitare un flusso o inserire una
  chiave **non dà consenso**. Revocare non ritira dati già trasmessi.
- **Esiti**: ricevute locali, revisioni/bozze e collegamenti suggeriti, distinti
  da consegne esterne. Un’approvazione lega payload esatto, versione e scadenza;
  **non invia nulla**, non prova un successo e non è un permesso generico.

Pratiche/responsabilità sono viste limitate a 24 elementi per raccolta; catalogo
e revisioni a 20. I pannelli segretario si aggiornano manualmente per non cambiare
il payload durante una decisione; i contatori operativi restano automatici.
Fatture/ZIP inattivi restano chiusi in **Consulta storico fatture e ZIP**, sempre
accessibile; i flussi esistenti sono gli unici interruttori persistenti.

**Stato di integrazione:** il servizio è un **segretario di revisione limitato**,
già collegato al bootstrap/runtime, non un esecutore di workflow arbitrari.
`db.init()` applica le migrazioni additive, incluse execution e ai_budget, prima
dei flussi; `main()` avvia nove worker persistenti: analysis, media, reconcile,
action, owner, knowledge, research, secretary_review e secretary_email_execution.
Indicizzazione, osservazioni, revisioni dovute e backlog sono integrati. Ogni
worker tratta un job alla volta; le attività periodiche hanno un solo thread vivo
per nome. Lease di 15 minuti, heartbeat di 30 secondi, limite esecuzione di un'ora
e massimo otto tentativi per errori ordinari non significano resend illimitati.
La dashboard non migra il DB né avvia worker alla richiesta: schema/modulo assente
è segnalato, non trasformato in una falsa vista vuota o in un consenso.
Vedere [contratti e limiti](CONFIGURATION.md#integrazione-dashboard-del-segretario)
e il [piano architetturale](SECRETARY_ARCHITECTURE_PLAN.md): **il piano non è una
checklist di funzionalità tutte completate**.

Nuovo profilo capabilities: fatture/ZIP disattivati. Upgrade legacy: flag
espliciti preservati, mancanti compatibili col profilo legacy; provenienza
sconosciuta trattata in pausa per le funzionalità opzionali. Verificare i flussi
prima di riattivare: nessuna riscrittura delle deleghe o inferenza dallo storico.
Quiet hours supportate solo per notifiche delle revisioni (`22–08`, configurabili),
non globali. Modalità globale observe/active e tetto monetario AI sono pianificati;
i cap Worker sono reali ma non limiti monetari: default **200 chiamate/giorno,
500000 token riservati/addebitati al giorno UTC, due richieste in volo, 65536 token
massimi di output per richiesta**. Ogni retry consuma una nuova riserva senza
rimborso; ledger/DB/lease non disponibili bloccano le richieste Worker. Le chiamate
dirette/legacy e il digest periodico fuori dai job non hanno questo budget
persistente. Non è una garanzia che ogni percorso AI legacy sia protetto.

## Controlli proprietario e provenienza

I nuovi piani proprietario di pratiche/responsabilità sono v2: il server conserva
le fonti native di **tutti gli input esportati**, inclusi snapshot e storia
(`input_sources`, `input_versions`), con piani applicati e ricevute immutabili.
Le letture derivate seguono anche revisioni runtime precedenti; fonti
mancanti/cicliche/non verificabili non autorizzano invio al modello. I vecchi
piani v1 non diventano prova di consenso. Il resolver condiviso owner/riconciliazione
task segue origini e tutte le evidenze applicate delle attività, non solo
l'ultima: revocare una fonte applicata blocca quel contesto; proposte non applicate
non contribuiscono. Questo **non completa** la provenienza degli input modello
dei vecchi piani attività/memoria o il modello di conflitti della memoria.

- **Accetta collegamento**: POST `/api/secretary/runtime/links/<id>/accept`, solo
  `expected_version` della proposta mostrata. Fonte, revisione e ambito sono
  ricontrollati; avanza la pratica, invalida approvazioni dipendenti e accoda una
  revisione. Non concede una delega né collega qualsiasi risultato di ricerca.
- **Presa in carico**: POST
  `/api/secretary/runtime/practices/<id>/acknowledge`, `expected_version` della
  pratica e `attention_hash` mostrato. Silenzia l'attenzione invariata, non chiude
  la pratica né autorizza effetti; notifiche superate/riconosciute sono fermate
  dai controlli prima dell'invio. Entrambi i controlli richiedono login/CSRF/origine
  e identità proprietario costruita dal backend, mai autorità dal JSON.
- **Riprova media bloccati dal consenso**, dalla fonte corrente aperta: POST
  `/api/media/retry-blocked`, `message_id,expected_channel`. Occorrono entrambi
  i consensi testo/media; solo fonti correnti idonee, non storico o tentativi
  provider già registrati. Massimo 20 allegati/richiesta e tre reset/allegato.
  Concedere il consenso non recupera automaticamente i job bloccati.
- **Email di solo testo**: prepara con POST `/api/secretary/emails`, controlla
  GET `/api/secretary/emails/<id>`, approva il payload esatto e poi scegli
  separatamente POST `/api/secretary/emails/<id>/dispatch`. Mittente configurato
  esplicito, oggetto fisso **Messaggio approvato**, destinatario/testo, versione,
  scadenza e ricevuta sono vincolati. Approvare non accoda/invia; allegati e thread
  non supportati. GET `/api/secretary/execution` mostra esiti limitati, non prova
  consegna/lettura. La ricevuta UI resta solo nel DOM. Dopo un tentativo persistito,
  timeout/crash/esito incerto non vengono reinviati alla cieca: serve riconciliare.

Calendar usa grant **v3**: testo proprietario nativo corrente, evento/stato,
ID Calendar esplicito configurato, fuso e tempi di creazione risolti
(inizio/fine/durata/promemoria) sono sigillati e ricontrollati prima dell'I/O.
Grant scaduti o legacy senza target sono negati; replay non rinnova il consenso
né lo ricollega a un nuovo calendario/fuso. Serve nuova autorizzazione corrente.
`CALENDAR_EXTERNAL_AUTO`/`CALENDAR_TRUSTED_SENDERS` non abilitano più auto-creazione
per etichetta mittente. Anche notifiche/risposte/ZIP del pipeline persistono il
tentativo prima dell'invio: attempted/uncertain richiedono riconciliazione, non
resend cieco. Segnare spam/newsletter come letti è solo comportamento del
classificatore privato legacy, non dell'ingest attuale.

## Roadmap ancora aperta

Restano: workflow/dipendenze arbitrari (parzialmente coperti dal worker research,
read-only); provenienza completa degli input dei vecchi piani attività/memoria;
collegamento generale di evidenze esistenti alle pratiche dal retrieval;
condizioni di stop eseguibili (il testo salvato non è un motore di regole);
Calendar update/disponibilità/drift; allegati e invii email in thread;
tetto monetario/modalità e quiet hours globali; erase/export privacy coordinati;
prova completa di backup ripristinato, validazione in produzione e valutazione
semantica/qualitativa. La memoria tipizzata (replace-on-conflict per topic) è
integrata.
L'integrazione limitata non dichiara il piano completato. Risultati correnti della
suite completa saranno aggiunti dal run principale: questa modifica documentale
non esegue test o terminale e non legge dati utente.

# Dialogo continuo con la segretaria

La chat naturale Telegram del proprietario usa uno storico persistente, un
riepilogo progressivo e il recupero mirato dei vecchi dialoghi. Note personali,
decisioni e procedure restano distinte da attività, scadenze e appuntamenti.
I nuovi schemi vengono creati con migrazioni additive al normale avvio.
Architettura, promemoria, garanzie di retry e limiti: [OWNER_DIALOGUE.md](OWNER_DIALOGUE.md).

# Importazione storica una tantum

Il comando `sh ./pi-secretary import-history --days 30` importa nell'archivio DB
le comunicazioni degli ultimi 30 giorni dalle connessioni abilitate. Omettendo
`--days`, lo script chiede il numero di giorni (default 14; ammessi 1–3650).
L'intervallo è calcolato al momento dell'avvio, include l'inizio ed esclude la fine.

Il comando ferma l'app per liberare le sessioni Telegram/WhatsApp, ricostruisce
l'immagine e avvia un container temporaneo. **Al termine l'app resta ferma**:
riavviarla con `sh ./pi-secretary up`. Non eseguire altri runtime sulle stesse
sessioni durante l'importazione. In installazioni Python dirette è disponibile
`python historical_import.py --days 30`, dopo aver fermato il runtime.

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

# Pi Secretary

## Digest giornaliero e piano delle prossime 48 ore

A `DIGEST_TIME`, nel `TIMEZONE` configurato, il bot invia il recap delle ultime
24 ore e un piano delle successive 48 ore, anche in assenza di nuovi messaggi.
Il planner locale consulta agenda interna, Google Calendar quando disponibile e
attività aperte pertinenti: arretrate, in scadenza entro 48 ore e senza scadenza,
incluse quelle nate da comunicazioni più vecchie. Il testo AI usa solo il
sottoinsieme ammesso dai controlli di fonte: non promette copertura completa né
una verifica generale di disponibilità/conflitti Calendar.

- Il planner delle attività esterne preesistenti può proporre eventi locali di
  15 minuti con popup 30 minuti prima e promemoria 24 ore prima (subito se già
  entro le 24 ore). Gli arretrati non cambiano scadenza. Le attività proprietario
  usano invece promemoria espliciti o, per default, alle 09:00 locali per sole
  date e alla scadenza per orari precisi: non inventano un orario di deadline.
- Eventi e promemoria hanno chiavi persistenti: ripetere il digest o riavviare
  non crea nuove copie. Gli appuntamenti già associati al messaggio sono mantenuti.
- Creare un task, anche con `/task`, **non autorizza Calendar**. Gli eventuali
  eventi derivati restano proposte locali, da autorizzare separatamente con
  `/conferma ID`; le attività proprietario non entrano nel vecchio planner
  automatico delle scadenze. Nessuna auto-creazione per etichetta mittente.
- Per attività senza scadenza l'AI può suggerire prossimi passi, senza inventare
  date o dichiarare appuntamenti salvati; non certifica assenza di conflitti.
- `/attivita` elenca le attività aperte; `/fatto ID` le chiude. I promemoria ancora
  in coda verificano che l'attività sia aperta prima di inviarla.

Il flusso dashboard `digest` deve essere attivo. I dati inviati al modello sono
filtrati secondo le fonti ammesse: non tutto l'archivio/Calendar remoto viene
esportato all'AI. Il recap o fallback è accodato come notifica persistente;
accodato/accettato non significa consegnato. Un invio attempted/uncertain richiede
riconciliazione, non un reinvio automatico. La generazione periodica del digest
fuori dai job non ha il cap AI persistente Worker.

## Aggiornamento delle attività dalle nuove comunicazioni

I nuovi input correnti acquisiti dai flussi attivi possono essere confrontati con
le attività aperte, inclusi messaggi Telegram personali e WhatsApp inviati, solo
con fonti/consensi ammessi. La valutazione avviene in una coda persistente
separata, con tentativi limitati in caso di errore.

- Completamenti e modifiche del titolo automatici richiedono testo proprietario
  nativo corrente, evidenza esplicita/certa e collegamento univoco all'attività.
  Comunicazioni esterne e messaggi personali/WhatsApp inviati restano evidenza
  per proposte, non autorità per applicare la modifica.
- Dubbi, corrispondenze multiple, annullamenti, cambi di scadenza e deduzioni
  esclusivamente visive richiedono conferma via bot, anche con soglia notifiche `mai`.
- `/proposte` elenca le richieste pendenti; `/conferma_attivita ID` applica una
  proposta e `/rifiuta_attivita ID` la respinge. Gli ID sono quelli delle proposte,
  distinti dagli ID delle attività e degli appuntamenti.
- Ogni modifica conserva evidenza e versione. Una proposta superata da modifiche
  all'attività o al messaggio sorgente non viene applicata.
- `/fatto ID` usa lo stesso percorso: chiude l'attività e ritira i promemoria e
  aggiorna gli eventi derivati localmente. Una cancellazione Calendar remota
  richiede un nuovo grant esplicito `/annulla ID`: chiudere il task non basta.
  Gli appuntamenti indipendenti del messaggio originale non sono cancellati.
- Gli import storici espliciti restano archivio, senza riconciliazione automatica
  anche se recuperati nuovamente dai connettori. Questo vincolo di provenienza
  vale per gli import effettuati dopo l'aggiornamento, non per vecchie righe già importate.

Le migrazioni additive sono eseguite al prossimo avvio del servizio. Nessuna
nuova credenziale o variabile `.env` è necessaria. La valutazione usa OpenRouter
e può aumentare il consumo AI in proporzione ai messaggi e alle attività aperte.

## AI: immagini native e audio separato

`OPENROUTER_MODEL` **DEVE supportare nativamente input testo e immagini**
(`image_url`); il default è `google/gemini-2.5-flash`. Le foto del proprietario
possono essere inviate al modello principale, con didascalia e contesto ammesso,
solo dopo i controlli di fonte/consenso. Vocali e immagini supportati acquisiti
da Telegram (bot/account) e WhatsApp possono essere convertiti in testo tramite
la coda persistente `media` se la fonte nativa corrente consente testo e media.
I vocali usano `OPENROUTER_AUDIO_MODEL`; le immagini usano il modello principale
per estrarre testo visibile e una descrizione visiva strutturata, non semplici
stringhe base64 né i token visivi interni del provider.
Il risultato è salvato in PostgreSQL, tabella `media_transcripts`, collegata
all'allegato, e riutilizzato da analisi/contesto del dialogo con i relativi gate;
il digest attuale non carica trascrizioni.
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
senza evento di prova: vedere [CONFIGURATION.md](CONFIGURATION.md#setup-calendar).

La chiave viene validata localmente e salvata atomicamente con permessi `0600`;
ogni sostituzione richiede consenso. Il default Docker è
`/var/lib/pi-secretary/gcal_service_account.json`, corrispondente a
`PI_SECRETARY_HOME/data/gcal_service_account.json` sull’host. Il file da importare
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

Segretario personale per Linux: raccoglie email, WhatsApp e Telegram, analizza
con OpenRouter, prepara riepiloghi, archivia fatture e gestisce attività e
proposte di appuntamento. Le integrazioni sono opzionali: l'AI richiede una
chiave OpenRouter; dialogo e notifiche richiedono un bot Telegram.
I contenuti analizzati vengono inviati al servizio AI: valuta privacy e costi.

## Primo avvio con Docker

Servono **Linux, Git, Python 3 sull'host, Docker e il plugin Docker Compose
recente** con supporto a `up --wait`. Il tuo utente deve già avere accesso al
daemon Docker: il launcher non usa sudo e non installa Docker. Serve Internet;
Node e Chromium vengono installati nell'immagine, non sull'host.

Sul computer che ospiterà il servizio:

```sh
git clone https://github.com/Hypaz-AI/p-secretary.git
cd p-secretary
sh ./pi-secretary setup
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
sh ./pi-secretary up
sh ./pi-secretary status
sh ./pi-secretary logs
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
`~/.local/share/pi-secretary/config/dashboard.json`. Apri il file **privatamente
sull'host con un editor locale** e leggi `username` e `password`. Non sono
le credenziali Linux; non condividere il file. Con `PI_SECRETARY_HOME` diverso,
cerca nella sua sottocartella `config/`.

La pagina **File** permette navigazione/download di cartelle dati consentite e
fatture. Upload solo in `uploads/`, massimo **20 MiB**, senza sovrascrittura e
**senza analisi AI automatica**. Non espone l'intero server: chiavi, configurazioni,
token, sessioni e spool sono esclusi. File referenziati dal DB non sono eliminabili;
la cancellazione è bloccata anche se il DB non è disponibile.

## Dati e gestione quotidiana

Il default è `~/.local/share/pi-secretary/`, fuori dal repository:

| Percorso host | Contenuto |
|---|---|
| `config/secretary.env` | Configurazione e credenziali integrazioni |
| `config/dashboard.json` | Login dashboard |
| `config/postgres.env` | Credenziali DB locale |
| `data/` | Allegati, token, sessioni, stato e archivio `invoices/` |

Il DB locale usa il volume Docker `pi-secretary_postgres`. `PI_SECRETARY_HOME`
cambia i percorsi host, **non crea una seconda istanza**: progetto e volume
restano gli stessi. Backup di DB e filesystem sono a tuo carico.

- `sh ./pi-secretary configure`: ferma l'app dopo conferma, ripropone login e
  campi mancanti; **non sostituisce valori già presenti**.
- Per cambiarli, modifica privatamente `config/secretary.env`, poi esegui
  `sh ./pi-secretary down` e `sh ./pi-secretary up`: il file è letto all'avvio.
  Il solo `up` non forza il riavvio di un container già attivo.
- `sh ./pi-secretary doctor`: prova connessione DB, senza avviarlo o migrare.
- `sh ./pi-secretary down`: rimuove container/rete, **conserva il volume DB**.

Per account, troubleshooting e migrazione leggi la [guida Docker](docker/README.md).
[OPERATIONS.md](OPERATIONS.md) è il riferimento tecnico per operatori, in inglese.

**Limiti:** nessun import storico illimitato, backup o aggiornamento automatico,
né consegna esattamente una volta. Gli appuntamenti rilevati sono proposte da
confermare; gli ZIP dei mesi chiusi sono accodati dal mese successivo. L'AI può
sbagliare: non è un sistema di emergenza. Packaging sperimentale, senza promessa
di immagini prebuilt, build riproducibili o compatibilità con ogni hardware.
