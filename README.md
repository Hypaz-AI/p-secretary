# Dashboard locale

Dashboard integrata nel servizio principale tramite Flask/Waitress, senza nuovo
servizio systemd: **http://192.168.1.119/** (porta **80**). Bind predefinito `0.0.0.0`,
configurabile con `DASHBOARD_BIND` e `DASHBOARD_PORT` nel `.env`.
`DASHBOARD_HOSTNAME` aggiunge il nome host autorizzato (default `pi-secretary.local`).

Credenziali generate al primo avvio in
`/home/ale/.config/pi-secretary/dashboard.json` (permessi `600`, fuori dal repository).
Aprire quel file **localmente** per leggere utente e password; non condividerlo.
Sessioni di un'ora in memoria (invalidate al riavvio), cookie HttpOnly/SameSite,
CSRF sui cambiamenti, controllo Origin/Host e limite tentativi di login.
Nessuna chiave `.env`, contenuto dei messaggi, SQL libero, comando shell o accesso
file viene esposto. La pagina pubblica contiene solo l'interfaccia di login.

**HTTP non cifra credenziali e traffico**: usare solo una LAN fidata, mai port
forwarding Internet. Per reti non fidate usare tunnel SSH o reverse proxy HTTPS;
con HTTPS impostare `DASHBOARD_COOKIE_SECURE=true`. Non sono state modificate
regole firewall. Il drop-in `deploy/dashboard-port80.conf`, installato in
`/etc/systemd/system/pi-secretary.service.d/`, concede soltanto
`CAP_NET_BIND_SERVICE`; `deploy/telegram-venv.conf` usa il Python della `.venv`.

I sette interruttori sono persistiti in `flow_settings`, creata in modo additivo
nel solo database configurato. Stop/start effettivo WhatsApp e Telegram personale
al prossimo giro principale; email/digest/ZIP al prossimo ciclo; fatture al prossimo
allegato analizzato. Un lavoro già avviato può completarsi. Nessun reset storico.
Il bot continua a gestire comandi e dialogo proprietario anche se l'analisi bot è
spenta; altre chat e file restano archiviati ma non analizzati. La riattivazione non
esegue invii immediati dalla dashboard: la normale routine può analizzare messaggi
di ieri/oggi e inviare notifiche secondo la soglia già configurata.

I KPI contano righe native, non solo analisi: giorni nel fuso `TIMEZONE`, direzioni
in/out/unknown, stato analisi, azioni richieste, fatture, appuntamenti aperti e ZIP.
Telegram personale è archivio senza AI; le analisi mancanti non sono errori.
Le diverse identità bot/personale restano distinte e possono rappresentare lo
stesso messaggio visto attraverso due integrazioni. Stato worker con timestamp:
`connected` è connessione osservata, non garanzia di consegna; `idle` è ultimo
callback completato, non verifica permanente della rete; `not_observed` non è sano.
Un timestamp vecchio segnala che il ciclo può essere bloccato. Nessun pulsante
manda messaggi, genera digest o avvia import storici.

Una funzione `not_configured` non viene configurata dal suo interruttore:
Telegram personale richiede anche `TELEGRAM_USER_ENABLED=true` e una sessione
autorizzata. Il recupero WhatsApp usa soltanto la cache disponibile e al massimo
100 chat; non garantisce completezza e non richiede cronologia remota illimitata.
I messaggi WhatsApp in uscita non attivano dialogo AI o risposte automatiche.
La verifica iniziale HTTP va eseguita dopo l'inizializzazione del database:
`systemctl` può indicare `active` prima che Waitress apra la porta.
Le connessioni Postgres disabilitano i prepared statement nominati per compatibilità
con endpoint in transaction pooling. Nessuna URI o password viene mostrata dalla UI.

Test offline: discovery `unittest` con `.venv/bin/python`; includono API HTTP,
autenticazione, CSRF, cookie, whitelist, rate limit e regressioni ingestione.

# 🤖 Pi Secretary — Segretario virtuale su Raspberry Pi

Un servizio Python sempre attivo che legge **email, WhatsApp e Telegram**,
analizza ogni comunicazione con un modello AI via **OpenRouter** e agisce:
riassume, rileva appuntamenti, archivia fatture, impara regole e ti notifica su Telegram.

## Le 5 pipeline

| # | Pipeline | Come funziona |
|---|---|---|
| 1 | **Riassunto giornaliero** | All'ora `DIGEST_TIME` (default 21:00) raccoglie dal DB tutte le comunicazioni della giornata, l'AI ne fa un riassunto e te lo manda via bot |
| 2 | **Filtro intelligente** | Ogni input viene classificato: se importante → notifica immediata; se spam/pubblicità → registrato e **segnato come letto** in mailbox |
| 3 | **Contesto su Postgres** | Ogni comunicazione, appuntamento, nota e regola finisce nel DB (`DATABASE_URL`). Puoi insegnare regole: *"quando X allora Y"* |
| 4 | **Archivio fatture** | Ogni fattura in arrivo (allegato email, file Telegram, media WhatsApp) viene salvata in `INVOICE_DIR/<entità>/<aaaa-mm>/` — l'AI decide se è per **hypaz** o **alessio** |
| 5 | **Zip mensili** | L'ultimo giorno del mese crea uno zip per entità con le fatture del mese appena concluso e te lo invia via bot |

## Cosa fa

### Notifiche gravi/urgenti

L'analisi produce `severity` (`bassa|media|alta`), `urgent` (booleano) e
`notification_reason` (motivazione concreta). **Gravità alta O urgenza** inviano
subito un avviso al proprietario tramite il bot Telegram già configurato, anche
con `NOTIFY_PRIORITY_THRESHOLD=mai`, prima delle operazioni Calendar/fatture.
L'avviso indica urgenza, gravità e motivo; tutti i campi dinamici sono escapati HTML.
Per le comunicazioni ordinarie resta invariata la soglia di priorità: `mai` le
lascia al digest. La sola priorità alta non costituisce un'emergenza.

Il prompt richiede rischi concreti gravi o interventi entro poche ore per evitare
danni importanti; fatture usuali, newsletter, rinnovi e diciture «urgente» non
bastano. Campi mancanti/non validi ed errori AI non attivano l'override.
La valutazione AI può sbagliare: non è un sistema di emergenza garantito.
«Subito» significa dopo ricezione/analisi nel normale polling: interruttori,
deduplicazione e fonti solo archivio restano invariati. Richiede Telegram
configurato e raggiungibile; non introduce retry o garanzie di consegna.

- 📧 **Email** — controlla fino a 50 caselle via IMAP ogni `POLL_INTERVAL` secondi
- 💬 **WhatsApp** — ponte via `whatsapp-web.js` (QR al primo avvio)
- ✈️ **Telegram** — bot per notifiche e comandi rapidi
- 🧠 **AI (OpenRouter)** — per ogni messaggio produce: riassunto, categoria,
  priorità, azione da fare, bozza di risposta e appuntamenti rilevati
- 📅 **Agenda** — appuntamenti salvati in Postgres (`appointments`) e,
  se collegato, **creati su Google Calendar** (su richiesta nel dialogo o
  quando l'AI ne rileva uno nelle comunicazioni)
- 📝 **Diario** — righe native Postgres e vista `communications`

## Installazione

```bash
cd ~/pi-secretary
cp .env.example .env
nano .env          # inserisci chiavi e account
```

### Email (fino a 50 account)
Ogni account è un blocco `EMAIL{n}_*` nel `.env`; `EMAIL{n}_PROVIDER` imposta
automaticamente i server corretti:

| `PROVIDER` | Per | Autenticazione |
|---|---|---|
| `gmail` | Google / Gmail | password per app |
| `outlook` | Microsoft personale (outlook.com, hotmail, live) | OAuth2 |
| `m365` | Microsoft 365 lavoro/scuola | OAuth2 |
| `zimbra_ovh` | Zimbra hostato su OVH | password della casella |
| `custom` | qualsiasi altro server | password + host a mano |

**Google** — per ogni account:
1. Gmail → ⚙️ Impostazioni → *Inoltro e POP/IMAP* → verifica che IMAP sia
   attivo (nei Gmail personali recenti lo è sempre)
2. Crea una **password per app** su <https://myaccount.google.com/apppasswords>
   (serve la verifica in due passaggi) e incollala in `EMAIL{n}_PASSWORD`

**Microsoft (personale e M365)** — Microsoft non accetta più password normali
su IMAP/SMTP, quindi serve OAuth2 (nessuna password nel `.env`):

```bash
python3 setup_ms_auth.py     # aggiungi --force per rifare il login
```

Segui le istruzioni: apri <https://microsoft.com/devicelogin>, inserisci il
codice mostrato e accedi con l'account Microsoft. I token finiscono in
`data/ms_tokens.json` e vengono rinnovati automaticamente. Se l'account M365
è aziendale, l'amministratore deve consentire il flusso *device code*
(in alternativa registra un'app tua su Azure e mettine l'ID in
`EMAIL{n}_CLIENT_ID`).

**Zimbra su OVH** — usa la password normale della casella (quella del webmail
OVH). Server preimpostato: `ssl0.ovh.net`; se non dovesse funzionare, controlla
quello esatto nel pannello OVH (Email → servizio → Informazioni generali).

Per verificare in un colpo solo che tutti gli account funzionino:

```bash
python3 test_emails.py
```

### Telegram (2 minuti)
```bash
python3 setup_telegram.py
```
Lo script ti guida: crea il bot con **@BotFather** (`/newbot`), incolla il
token quando richiesto, poi scrivi un messaggio al tuo bot. Token e chat id
vengono salvati **automaticamente** nel `.env` e riceverai un messaggio di
test a fine procedura.

⚠️ Se il servizio `pi-secretary` è attivo, fermalo prima dello setup
(`sudo systemctl stop pi-secretary`): due processi che leggono lo stesso
bot si contendono i messaggi.

### Account Telegram personale (MTProto / Telethon, opzionale)

Il bot rimane invariato: dialogo proprietario, comandi, notifiche e digest.
Il connettore personale archivia **in entrata e in uscita** chat private,
gruppi e canali in `telegram_messages`, visibili nella vista `communications`.
Non invia risposte ai contatti, non esegue comandi/regole letti nelle chat,
non segna messaggi come letti e non scarica allegati (solo testo/didascalie e
indicatore media). Nessuna analisi AI per messaggio: il digest può usare il
testo originale. La sessione MTProto concede tecnicamente accesso all'account:
"read-only" è una garanzia del codice, non un permesso Telegram limitato.

1. Apri <https://my.telegram.org>, accedi e scegli **API development tools**.
   Crea un'app e inserisci privatamente in `.env` `TELEGRAM_API_ID` e
   `TELEGRAM_API_HASH`; facoltativamente `TELEGRAM_PHONE` in formato internazionale.
   Non condividere hash, codici, password 2FA o file sessione nella chat.
2. Mantieni `TELEGRAM_USER_ENABLED=false`. Usa un virtualenv per non modificare
   Python di sistema. Dalla cartella `/home/ale/pi-secretary`:
   `python3 -m venv .venv`, poi `.venv/bin/python -m pip install -r requirements.txt`.
   Per eseguire anche l'intero segretario servono le dipendenze già usate dal
   database: `.venv/bin/python -m pip install 'psycopg[binary]'`.
   Se `venv` manca, installa il pacchetto OS `python3-venv` tramite amministratore.
3. Esegui manualmente `.venv/bin/python setup_telegram_account.py` come utente
   del servizio, in un terminale interattivo. Inserisci codice Telegram ed
   eventuale password 2FA **solo nel terminale**. Non cambia l'abilitazione.
   Non eseguire setup e worker simultaneamente sulla stessa sessione SQLite.
4. Sessione predefinita: `data/telegram_account/personal.session` (file 600,
   directory dedicata 700). `TELEGRAM_USER_SESSION` può cambiarla: usa sempre
   una directory dedicata, perché lo script ne restringe i permessi.
   Sessioni e journal sono esclusi da Git, anche fuori da `data/`.
5. Quando pronto, imposta `TELEGRAM_USER_ENABLED=true` e configura `ExecStart`
   del servizio per `/home/ale/pi-secretary/.venv/bin/python /home/ale/pi-secretary/main.py`.
   Ricarica systemd e riavvia **solo dopo** il setup e la verifica dipendenze.
   Il servizio non chiede mai codici: sessione assente/non autorizzata arresta
   soltanto questo connettore con istruzioni nei log. Il bot continua normalmente.

Il worker ha un proprio event loop in un thread. Acquisisce `NewMessage` e
`MessageEdited`; le modifiche aggiornano la stessa riga senza duplicarla,
preservando data originale e revisioni più nuove. Identità:
`["mtproto", "user:<account-id>", "<peer-id>", <message-id>]`, canale
`telegram (account)`, account e conversazione separati dal bot.

Ogni messaggio archiviato risolve i **nomi leggibili** di mittente e chat
(prima dalla cache locale di Telethon, senza RPC; solo se l'entità manca
viene fatta una risoluzione limitata). `sender` non è mai un ID numerico:
è `Nome Cognome (@user)` per le persone, `Nome in NomeGruppo` nei gruppi,
`io → NomeChat` per i messaggi in uscita, il titolo per i canali.
`subject` contiene il nome della conversazione e `metadata` conserva
`sender_name` e `chat_name` per l'analisi AI e il dialogo.

Ogni messaggio archiviato risolve i **nomi leggibili** di mittente e chat
(prima dalla cache locale di Telethon, senza RPC; solo se l'entità manca
viene fatta una risoluzione limitata). `sender` non è mai un ID numerico:
è `Nome Cognome (@user)` per le persone, `Nome in NomeGruppo` nei gruppi,
`io → NomeChat` per i messaggi in uscita, il titolo per i canali.
`subject` contiene il nome della conversazione e `metadata` conserva
`sender_name` e `chat_name` per l'analisi AI e il dialogo.

All'avvio e ogni `TELEGRAM_USER_RECOVERY_INTERVAL` (900 secondi) recupera
**ieri dalle 00:00 e oggi**, secondo `TIMEZONE`. Limiti predefiniti:
`TELEGRAM_USER_DIALOG_LIMIT=100` dialoghi e `TELEGRAM_USER_MESSAGE_LIMIT=200`
ultimi messaggi per dialogo. Nessuna paginazione storica illimitata; dialoghi
oltre il limite e chat molto attive possono essere incompleti. I log indicano
limiti, contatori, errori e saturazioni. Eventi vecchi, incluse modifiche a
messaggi anteriori a ieri, sono ignorati. Errori DB live vengono ritentati dal
recupero limitato: non è una coda durevole né una garanzia di raccolta completa.
Le eliminazioni non vengono sincronizzate e non c'è storico delle revisioni.

Verifica offline: `.venv/bin/python -m unittest discover -v` (nessun login o
invio). Dopo l'abilitazione, verifica nei log `read-only archive active` e
`bounded yesterday/today recovery`; nel DB controlla `telegram_messages`
con `channel='telegram (account)'`, `account`, `direction`, timestamp e testo.
Usa messaggi di prova nelle tue chat e una modifica per verificare l'upsert;
non attenderti risposte automatiche. La verifica offline non prova la connessione
reale, autorizzazioni account, schema remoto o visibilità di tutti i dialoghi.

### Google Calendar (opzionale — l'agente fissa appuntamenti)
```bash
python3 setup_gcal.py
```

**Modalità consigliata — service account** (nessun login, nessun consenso,
**nessuna scadenza**: l'ideale per un server sempre acceso):
1. Su <https://console.cloud.google.com> (progetto con API *Google
   Calendar* abilitata): **IAM e amministrazione → Account di servizio →
   Crea** (nessun ruolo necessario). Sulla riga dell'account: **Chiavi →
   Aggiungi chiave → JSON** e scarica il file.
2. Copia la chiave come `data/gcal_service_account.json` (o imposta
   `GCAL_SERVICE_ACCOUNT_FILE` nel `.env`).
3. Nella chiave trovi `"client_email"`: copia quell'indirizzo e
   **condividi con esso il tuo calendario** da <https://calendar.google.com>
   (impostazioni del calendario → *Aggiungi utenti* → permesso
   **«Apportare modifiche agli eventi»**), come faresti con una persona.
4. Rilancia `setup_gcal.py`: verifica lettura e scrittura (evento di prova
   creato e rimosso) e imposta `GCAL_ENABLED=true`.

La chiave vale **finché non la revochi tu**: niente token da rinnovare.
Il segretario vede e scrive **solo** i calendari che gli condividi.

*Alternativa — OAuth2 device flow* (login su
<https://www.google.com/device>, token in `data/gcal_tokens.json` rinnovati
in automatico): serve un ID client OAuth tipo «TV e dispositivi con input
limitato»; con l'app in modalità *Testing* il consenso scade ogni 7 giorni
— pubblicala in produzione per evitarlo. `setup_gcal.py` la propone solo
se manca la chiave service account.

Una volta collegato puoi scrivere al bot: *«mettimi un appuntamento domani
alle 15 dal dentista»* — l'evento finisce dritto nel tuo Google Calendar.
Gli appuntamenti che l'AI rileva nelle email/WhatsApp/Telegram vengono
sincronizzati automaticamente, e `/appuntamenti` legge l'agenda vera di Google.
L'interruttore **Google Calendar** nella dashboard sospende ogni scrittura.

### WhatsApp (opzionale)
```bash
sudo apt install nodejs npm
python3 setup_whatsapp.py
```
Lo script avvia il ponte, mostra il **QR code** da scansionare (WhatsApp →
Impostazioni → Dispositivi collegati) e, una volta connesso, imposta
`WHATSAPP_ENABLED=true` nel `.env` da solo. La sessione resta salvata in
`data/wa_session` e viene riusata a ogni riavvio.

Per ogni messaggio il ponte risolve il **nome della conversazione** (chat
privata o gruppo) e, nei gruppi, anche il **nome di chi ha scritto** il
singolo messaggio dal suo contatto (pushname o nome in rubrica). Il mittente
archiviato è sempre leggibile: `Nome in NomeGruppo` nei gruppi,
`Nome (numero)` nelle private, `io → NomeChat` in uscita — mai un ID nudo.
L'ID tecnico resta nei `metadata`.

Se le dipendenze Node mancano: `cd data && npm install`.

### Avvio manuale
```bash
python3 main.py
```

### Servizio systemd (avvio automatico)
```bash
sudo tee /etc/systemd/system/pi-secretary.service <<'EOF'
[Unit]
Description=Pi Secretary - segretario virtuale
After=network-online.target

[Service]
Type=simple
User=ale
WorkingDirectory=/home/ale/pi-secretary
ExecStart=/usr/bin/python3 /home/ale/pi-secretary/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pi-secretary
journalctl -u pi-secretary -f
```

## Comandi Telegram

| Comando | Effetto |
|---|---|
| `/appuntamenti` | Prossimi appuntamenti (Google Calendar se collegato, altrimenti agenda interna) |
| `/note` | Ultime 5 note |
| `/regole` | Regole imparate finora |
| `/fatture` | Fatture in archivio per mese/entità |
| `quando <condizione> allora <istruzione>` | **Insegna una regola** al segretario (es. *"quando il mittente è amazon allora non avvisarmi"*) |

Qualsiasi altro messaggio o file scritto al bot viene interpretato dall'AI:
mandagli una fattura in PDF e finisce dritta nell'archivio giusto.

## Struttura del database (Postgres)

La migrazione `native_messages_v1` sostituisce il diario con la vista compatibile
`communications`. I dati originali sono in `email_messages`, `whatsapp_messages`
e `telegram_messages`; `conversations`, `attachments` e `message_analysis`
separano chat, allegati e risultati AI. `ts` è il timestamp originale (NULL se
sconosciuto), `acquired_at` è l'acquisizione. Gli ID mancanti non vengono inventati.
`message_dedup` ed `email_seen` non vengono eliminati né potati.

Il reset iniziale richiede `db.init(approved_reset=True)` esplicito; gli avvii
ordinari usano `db.init()` e non cancellano dati. La migrazione è transazionale,
versionata e rifiuta dipendenze esterne anziché usare CASCADE. Fatture, agenda,
note, regole, zip e cursori restano intatti. Solo `config.DATABASE_URL` è usato.
La raccolta ordinaria è limitata a ieri/oggi; `fetch_day_messages` richiede
`historical=True` per giorni precedenti. WhatsApp recupera solo cache recente.

| Tabella | Contenuto |
|---|---|
| `communications` | Diario di ogni comunicazione analizzata (con verdetto AI in JSONB) |
| `appointments` | Agenda |
| `notes` | Note |
| `rules` | Regole insegnate dall'utente, passate all'AI a ogni analisi |
| `invoices` | Fatture archiviate (entità, mese, percorso) |
| `monthly_zips` | Stato degli zip mensili già inviati |

## File di dati

| File | Contenuto |
|---|---|
| `data/state.json` | UID email / offset Telegram / giorno ultimo digest |
| `data/zips/` | Zip mensili delle fatture prima dell'invio |
| `data/tg_files/`, `data/wa_media/` | Allegati scaricati da Telegram/WhatsApp |
| `/home/ale/invoices/<entità>/<aaaa-mm>/` | Archivio fatture organizzato |

## Note

- Il nome dell'agente si imposta con `AGENT_NAME` nell'ambiente o nel `.env`
   (esempio: `AGENT_NAME=Ada`; predefinito: `Pi Secretary`, anche se vuoto).
   Il nome viene comunicato all'AI nel dialogo, nell'analisi e nei riepiloghi:
   se gli chiedi come si chiama, usa il nome configurato. Dopo una modifica
   riavvia il servizio; le variabili dell'ambiente hanno precedenza sul `.env`.
- Per Gmail serve una **password per app** (account Google → Sicurezza →
  Verifica in due passaggi → Password per le app).
- Il modello AI si imposta con `OPENROUTER_MODEL` (slug OpenRouter, es.
  `meta-llama/llama-3.3-70b-instruct`, `openai/gpt-4o-mini`, ...).
