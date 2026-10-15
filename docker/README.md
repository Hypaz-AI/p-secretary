# Readiness e riconfigurazione

Usare Compose v2 con supporto `up --wait --wait-timeout`. L'healthcheck app usa
solo urllib della standard library contro `/healthz` con Host `localhost`.
Segnala il processo HTTP avviato, non disponibilità dei connettori.
Il launcher `up` forza la ricreazione app e attende al massimo 120 secondi;
prima dell'avvio richiede OpenRouter e verifica il DB con SELECT 1.
`configure` non migra dati quando cambia la destinazione DB. I setup eseguiti
nel container indicano di ricaricare con launcher up, non tramite systemctl.
La configurazione confermata non implica che tutti i setup opzionali siano ready.

# Docker: configurazione e risoluzione dei problemi

Per il primo avvio parti dal [README principale](../README.md).
Esegui il launcher `sh ./pi-secretary` dalla directory del repository.
Gli script di autenticazione sono eseguiti dal wizard nel container:
non serve un virtualenv host né installare Node/Chromium sull'host.

## Prerequisiti, percorsi e permessi

Linux, Git, Python 3 host, Docker e plugin Compose recente con `up --wait`.
Verifica `python3 --version`, `docker compose version` e `docker info`.
L'accesso al daemon deve essere già predisposto per il tuo utente: il launcher
non usa sudo, non modifica gruppi e non cambia proprietari. L'accesso al daemon
Docker è un privilegio elevato.

App e tmpfs usano UID/GID correnti. Le directory host devono appartenere a
quell'utente ed essere scrivibili; il launcher imposta 700 sulle directory
principali. Non alternare root e utente normale. Non usare `chmod 777`.

`PI_SECRETARY_HOME` seleziona la directory persistente, default
`~/.local/share/pi-secretary`. Se la cambi, esportala nella sessione e usala
coerentemente per tutti i comandi:

```sh
export PI_SECRETARY_HOME="$HOME/.local/share/pi-secretary"
sh ./pi-secretary setup
```

| Host, sotto `PI_SECRETARY_HOME` | Container |
|---|---|
| `config/` | `/config` |
| `config/secretary.env` | `/config/secretary.env` |
| `config/dashboard.json` | `/config/dashboard.json` |
| `data/` | `/var/lib/pi-secretary` |
| `data/invoices/` | `/var/lib/pi-secretary/invoices` |
| `data/telegram_account/personal.session` | `/var/lib/pi-secretary/telegram_account/personal.session` |
| `data/home/` | HOME del container |

La cache bridge è rigenerata in `/tmp/cache`. Compose impone percorsi dati,
fatture, sessione personale Telegram, credenziali dashboard e bind/porta
interni: prevalgono su `secretary.env`.

**Una sola istanza:** progetto `pi-secretary`, volume DB `pi-secretary_postgres`.
Cambiare home non isola DB o container. Le invocazioni dirette di Compose
usano 1000:1000 se UID/GID non sono esportati: preferisci il launcher.

## Wizard, ripresa e modifica dei valori

`setup` ferma l'app, costruisce l'immagine e apre il wizard. Le risposte sono
salvate subito in `config/secretary.env`, lo stato non segreto in
`config/progress.json`. I login opzionali possono fare chiamate di rete e
richiedono consenso. Inserisci segreti solo nel terminale o nelle pagine
ufficiali, mai in chat.

Ogni sezione mostra istruzioni italiane numerate e link ufficiali per creare
credenziali e completare l'accesso. I titoli usano colori ANSI solo su TTY con
TERM compatibile; `NO_COLOR` (anche vuoto), TERM=dumb e output rediretto li
disattivano. Nessuna dipendenza aggiuntiva. Le guide non aggiungono richieste
di input né eseguono login autonomamente.

URI vuoto significa PostgreSQL 16 locale, senza porta DB pubblicata, con
password generata in `config/postgres.env`. L'URI esterno è richiesto con
input nascosto: usa un DB dedicato raggiungibile dal container. `localhost`
nell'URI indica il container, non l'host. Codifica percentualmente i caratteri
speciali delle credenziali e configura TLS dove necessario. La modalità
esterna non avvia il profilo DB locale.

Dopo la conferma del wizard seguono consensi separati per avvio DB locale/
verifica `SELECT 1` e avvio app. `doctor` non applica migrazioni; l'app sì.

Riprendi un'interruzione con `setup`, oppure usa `configure` per riconfigurare.
`configure` chiede conferma prima di fermare l'app; anche `setup` lo fa se lo
stato era completo. `configure` permette di scegliere una sezione e modificare
i valori: INVIO conserva quelli salvati. `setup` riprende i valori validi già
salvati e richiede quelli mancanti. I login già eseguiti sono riproposti e possono
essere accettati di nuovo; lo stato salvato non prova la validità attuale dei token.

Per correggere valori esistenti apri privatamente `config/secretary.env` con un
editor locale (file 600). Sintassi `KEY=value`, una chiave per riga, commenti
solo su righe separate. La [.env.example](../.env.example) è un riferimento
per le chiavi, **non copiarla integralmente**: contiene percorsi e istruzioni
del vecchio deployment host. Gli script possono stampare «.env» o suggerire
systemd: in Docker scrivono in `secretary.env`, gestisci l'app col launcher.

Il file è montato, ma viene letto solo all'avvio del processo. `up` forza
la ricreazione dell'app e verifica prima il DB. Dopo modifiche usa:

```sh
sh ./pi-secretary up
```

Non cambiare
`postgres.env` dopo l'inizializzazione: PostgreSQL non applica automaticamente
nuove password. Il launcher rileva la modalità locale/esterna dall'URI salvato;
il cambio di destinazione non migra i dati: va pianificato separatamente.

## Collegare gli account

Puoi saltare i login e tornarci con `configure`. Non eseguire altri runtime
sugli stessi account durante setup: i lock proteggono solo processi che
condividono lo stesso `DATA_DIR`.

### Email

Accetta «Aggiungere account email?» e inserisci provider, nome e indirizzo.
Rifiutare un nuovo account termina l'aggiunta dei successivi. Fino a 50 account;
host custom sono chiesti nel wizard, porte/override avanzati vanno nel file.

| Provider | Autenticazione |
|---|---|
| `gmail` | Password per app |
| `outlook` | OAuth Microsoft personale |
| `m365` | OAuth Microsoft lavoro/scuola |
| `zimbra_ovh` | Password casella |
| `custom` | Password e host IMAP/SMTP del fornitore |

- Gmail: verifica accesso IMAP, abilita verifica in due passaggi e crea una
	password per app su <https://myaccount.google.com/apppasswords>. Se l'account
	non le consente, consulta l'amministratore; non usare la password normale.
- Microsoft: salva prima provider/indirizzo, poi accetta il setup `microsoft`.
	Apri <https://microsoft.com/devicelogin>, inserisci il codice e accedi per ogni
	account. Il tenant M365 deve consentire device flow; un client alternativo
	si configura con `EMAILn_CLIENT_ID`. Token in `data/ms_tokens.json`, rinnovati
	quando possibile. Non inserire password normali per Microsoft.
- Zimbra OVH: preset `ssl0.ovh.net`; verifica il server del tuo servizio nel
	pannello OVH e configura override se diverso.

Un indirizzo salvato senza credenziali non è un account funzionante. Controlla
i log dopo avvio senza pubblicare indirizzi, contenuti o errori sensibili.

### Bot Telegram

Accetta setup `telegram`. Crea un bot con **@BotFather** (`/newbot`), inserisci
il token nel prompt nascosto e invia al bot dalla **tua chat privata** il codice
monouso stampato entro 120 secondi. Salva token/chat ID e invia un messaggio di
prova. Il bot serve per dialogo, notifiche, digest e ZIP, non legge il tuo account.

### Telegram personale

Ottieni API ID/hash da <https://my.telegram.org>, «API development tools».
Il wizard richiede anche telefono internazionale; senza tutti e tre rinvia il
login. Inserisci codice e password 2FA solo nel terminale. La sessione concede
tecnicamente accesso all'account: proteggila come una password.

Il setup chiede un consenso finale prima di impostare `TELEGRAM_USER_ENABLED=true`;
INVIO rifiuta l'acquisizione (codice di uscita 2). Il codice di accesso arriva
normalmente nella chat di servizio ufficiale «Telegram» su un dispositivo già
collegato, anche tra le chat archiviate; gli SMS non sono garantiti. Non usare
codici di my.telegram.org, API hash o token bot. Con `q` esci senza tentare codici
casuali; controlla numero e account prima di riprovare, evitando richieste ravvicinate.
Ricarica successivamente con `up` e verifica l'interruttore dashboard.
Archivia testo/didascalie in entrata/uscita, non risponde ai contatti, non esegue
i loro comandi, non segna letto né scarica allegati. Nessuna analisi AI per
messaggio, ma il digest può usare il testo. «Sola lettura» è una scelta del codice,
non un permesso Telegram limitato.

### WhatsApp

Accetta setup `whatsapp`, scansiona QR da WhatsApp → Dispositivi collegati.
Node/Chromium sono nell'immagine; sessione in `data/wa_session/`, riusata se
valida. Verifica che sia stato salvato `WHATSAPP_ENABLED=true` prima dell'avvio.
I messaggi in uscita non attivano dialogo AI. Compatibilità ARM non garantita
senza test reali; non seguire suggerimenti snap host come rimedio al container.

### Google Calendar

**Service account:** abilita API Calendar nel progetto su
<https://console.cloud.google.com>, crea account di servizio da IAM e
amministrazione, poi chiave JSON. Importala privatamente via filesystem host,
**non dalla pagina File**:

1. Copia in `PI_SECRETARY_HOME/data/gcal_service_account.json`, sostituendo
	 `PI_SECRETARY_HOME` col percorso reale. È il default nel container:
	 `/var/lib/pi-secretary/gcal_service_account.json`.
2. In alternativa copia in `PI_SECRETARY_HOME/config/gcal_service_account.json`
	 e imposta `GCAL_SERVICE_ACCOUNT_FILE=/config/gcal_service_account.json`
	 in `secretary.env`. **La cartella config è montata ed è utilizzabile**.
	 Non impostare il percorso host come percorso nel container.
3. File dell'utente launcher, leggibile da lui, permessi 600. Leggi privatamente
	 `client_email`, condividi con quell'indirizzo il calendario su
	 <https://calendar.google.com> con «Apportare modifiche agli eventi».
4. Copia l'ID da Impostazioni → Integra calendario. Usa `configure`, accetta
	 setup `gcal` e rifiuta gli altri login non necessari. Inserisci l'ID condiviso,
	 **non `primary` per il service account**.
5. Il setup verifica accesso e chiede consenso per creare/cancellare un evento
	 di prova. Abilita Calendar solo se la prova riesce; se non riesce a cancellare
	 l'evento di prova, eliminalo manualmente.

Percorsi fuori da `config/` e `data/` richiedono un mount aggiuntivo esplicito.
La chiave vale fino a revoca; condivisioni e policy possono comunque cambiare.

**Solo service account IAM:** nessuna alternativa OAuth utente. Nel setup puoi
incollare il JSON con input nascosto (compatto o multilinea, riga `FINE` per
terminare) oppure importare un file visibile al container. Il salvataggio è
atomico con permessi `0600`; sovrascrivere un file esistente richiede consenso.
Senza terminale con input nascosto l’incolla è bloccato. Vecchi token e client
OAuth Calendar sono ignorati; revocarli/rimuoverli privatamente se inutilizzati.
Il login OAuth per email Microsoft resta supportato e invariato.

Gli appuntamenti rilevati nelle comunicazioni sono proposte di default:
collegare Calendar non significa abilitare creazione automatica.

## Comandi e dashboard

| Suffisso di `sh ./pi-secretary` | Effetto |
|---|---|
| `setup` | Build/wizard, consensi separati per verifica e avvio |
| `configure` | Conferma arresto app, build/wizard per campi mancanti e login |
| `up` | Richiede wizard confermato; avvia DB locale, verifica DB, avvia app |
| `down` | Rimuove container/rete, conserva volumi |
| `status` | Stato container |
| `logs` | Ultime 100 righe app, non streaming |
| `doctor` | Solo `SELECT 1`, non avvia DB né applica migrazioni |

Un lock host serializza i comandi. Non ci sono comandi launcher `restart`,
`backup` o `update`. Build manuale facoltativa: per `docker compose build app`
esporta prima `PI_SECRETARY_HOME`, `PI_SECRETARY_UID`, `PI_SECRETARY_GID` corretti;
`config/postgres.env` deve già esistere. Setup fa già la build. Nessuna promessa
di immagine prebuilt o build riproducibile: base non fissata a digest, Python
non bloccato a versioni esatte.

Dashboard: loopback host `127.0.0.1:8080`, HTTP senza TLS. Usa il
[tunnel SSH](../README.md#aprire-la-dashboard) per accesso remoto.
Apri privatamente `config/dashboard.json` con editor locale e leggi
`username`/`password`, generati al primo avvio. Sessioni di un'ora in memoria,
invalidate al riavvio. Un proxy HTTPS richiede configurazione separata,
hostname/header rivisti e `DASHBOARD_COOKIE_SECURE=true`.

Interruttori persistenti nel DB, applicati nei cicli successivi; un lavoro già
avviato può completarsi. Non configurano integrazioni mancanti, non fanno import
storici o invii immediati dal pulsante; la routine riattivata può riprendere
elaborazioni/notifiche. `connected`/`idle` con timestamp non garantiscono
consegna o salute permanente; `not_observed` non significa sano. Archivio
Telegram personale senza analisi AI non è un errore.

File manager: cartelle dati consentite e fatture, niente symlink o file sensibili;
upload in `uploads/`, 20 MiB, senza overwrite/analisi AI. Cancellazione solo se
non referenziato dal DB, bloccata quando DB indisponibile. Non importare chiavi qui.

### Comandi nel bot del proprietario

| Comando | Uso |
|---|---|
| `/appuntamenti` | Agenda e stato sync |
| `/conferma ID`, `/annulla ID` | Conferma proposta/cancellazione Calendar |
| `/task titolo \| YYYY-MM-DDTHH:MM+02:00` | Attività con scadenza opzionale |
| `/attivita`, `/fatto ID` | Attività aperte/completamento |
| `/note`, `/regole`, `/fatture` | Note, regole, fatture |
| `quando <condizione> allora <istruzione>` | Insegna una regola |

Modifica eventi in linguaggio naturale non implementata: cancella e crea nuovo.
Policy dettagliate in [OPERATIONS.md](../OPERATIONS.md).

## Troubleshooting

- Docker non accessibile: controlla daemon/plugin/accesso utente. L'errore finale
	del launcher è generico: leggi privatamente anche l'errore precedente.
- Permessi/HOME: controlla proprietà di home, `config/`, `data/`, `data/home/`
	e file importati. Il launcher non ripara ricorsivamente proprietà; fai correggere
	dall'amministratore per l'UID corrente, senza app root o permessi 777.
- Porta 8080 occupata: individua l'altro servizio e scegli quale mantenere.
	`DASHBOARD_PORT` nel file non cambia il mapping Compose; un'altra porta host
	richiede modifica esplicita al mapping e al tunnel, non un'opzione wizard.
- DB: `doctor` non lo avvia; `up` lo fa in modalità locale dopo wizard confermato.
	Per DB esterno verifica rete/DNS/TLS/credenziali nel contesto container.
- Dashboard assente: `status` non garantisce readiness app; il DB viene
	inizializzato prima dell'apertura HTTP. Controlla `logs`.
- Configurazione incompleta: completa/conferma wizard, non aggirare entrypoint
	modificando `progress.json`.
- Login fallito: ripeti `configure`, accetta solo il setup interessato.
- Chromium/WhatsApp: controlla errori bridge; non scaricare x86 su ARM.

Non condividere `docker compose config`: può espandere credenziali.
Log e output dei login possono contenere dati personali o segreti.

## Migrazione da installazione precedente

**Non serve a una nuova installazione.** Solo durante una migrazione pianificata,
fai fermare/disabilitare il vecchio servizio dall'amministratore, per esempio
`sudo systemctl disable --now pi-secretary.service`. Non eseguire systemd e
Docker sugli stessi account: il launcher non ferma systemd, lock su directory
diverse non bastano.

Prima backup verificati di DB, configurazione, token/sessioni, allegati e fatture.
Puoi copiare privatamente il vecchio `.env` in `config/secretary.env` prima del
wizard, ma rivedilo prima dell'avvio: valori esistenti non vengono sostituiti,
percorsi host non sono necessariamente visibili nel container. Trasferisci
token/sessioni separatamente in `data/`, mantenendo proprietà e permessi.

**Non riutilizzare casualmente il vecchio DB:** allegati/fatture possono avere
percorsi assoluti nel database. Copiare file o cambiare `DATA_DIR` non aggiorna
i riferimenti. Le migrazioni schema non trasferiscono automaticamente i percorsi;
l'avvio può elaborare job e inviare messaggi. Prova su DB clone isolato con invii
disabilitati, riconcilia percorsi/job e pianifica il passaggio. Non presumere
compatibilità di un vecchio volume PostgreSQL con una nuova versione.

## Backup e riferimenti operativi

`down` conserva il volume, **non è un backup**. Durante manutenzione salva
coerentemente dump PostgreSQL, `config/`, `data/` e archivi esterni. Proteggi/cifra
copie e prova restore su DB nuovo isolato. Non eliminare direttamente file
referenziati. Nessun backup o aggiornamento automatico: prima di aggiornare
sorgenti/immagini rivedi compatibilità, migrazioni e rollback; setup/configure
ricostruiscono l'immagine.

Informazioni operative in [OPERATIONS.md](../OPERATIONS.md):

- Coda durevole, lease/retry/outbox: Telegram può duplicare invii dopo crash o
	risposta persa; nessuna consegna esattamente una volta.
- Recupero limitato da `RECOVERY_DAYS` (default 14, 1–90) e limiti connettori;
	WhatsApp dipende da cronologia disponibile, Telegram da quote/retention.
- Gravità alta/urgenza AI possono superare soglia `mai`; polling, errori e rete
	restano rilevanti. Non è un sistema d'emergenza garantito.
- PDF/XML con limiti difensivi, niente OCR, verifica P7M, estrazione ZIP o
	validazione fiscale. ZIP mesi chiusi dal mese successivo; fatture tardive
	non causano reinvio automatico di ZIP già consegnati.
- Tabelle native `email_messages`, `whatsapp_messages`, `telegram_messages`;
	`communications` è una vista. DB/filesystem devono restare coerenti.
- Allegati email in `data/email_attachments/<sha256>`; vecchi BYTEA leggibili.
	Migrazione blob opzionale e restore sono procedure per operatori.

[IMPLEMENTATION.md](../IMPLEMENTATION.md) è un riepilogo tecnico, non onboarding.
Nessuna promessa di hardening production-ready, upgrade DB automatici o
compatibilità hardware senza verifica.