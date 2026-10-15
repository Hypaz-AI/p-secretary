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
  all'inizio dell'intervallo; allegati rappresentati come metadati, non scaricati.
- WhatsApp: solo storico sincronizzato disponibile sul dispositivo collegato,
  non garanzia di recupero completo dal server.
- Telegram bot: lo storico passato non è disponibile tramite Bot API.
- Calendar: escluso perché contiene appuntamenti, non comunicazioni.

Nessuna analisi AI, comando, risposta o scrittura Calendar viene avviata dallo
script. I normali flussi, una volta riattivati, possono analizzare nuovamente
messaggi recuperati; i dati importati possono comparire nei digest successivi.
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

## AI: immagini native e audio separato

`OPENROUTER_MODEL` **DEVE supportare nativamente input testo e immagini**
(`image_url`); il default è `google/gemini-2.5-flash`. Le foto del proprietario
sono inviate direttamente al modello principale, con didascalia e cronologia,
senza modello immagini separato né preprocessing OCR. Solo l'audio viene
trascritto con `OPENROUTER_AUDIO_MODEL` prima del dialogo. La pipeline testuale
ignora le immagini; l'estrazione documentale PDF/XML resta separata e invariata.
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
Non inserire chiavi in chat, argomenti di shell o dashboard.

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
3. Se aggiungere email (fino a 50) ed entità fatture (fino a quattro).
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
