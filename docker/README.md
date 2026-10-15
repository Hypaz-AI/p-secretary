# Packaging sperimentale

Prerequisiti: Linux, Python 3 sul host, Docker con plugin Compose recente
(supporto `up --wait`), accesso al daemon già predisposto.
Il launcher non usa sudo e non cambia proprietario dei file.
Esporta UID/GID del chiamante per user e tmpfs Compose (default 1000:1000
solo per invocazioni dirette). Le directory host devono appartenere al
chiamante ed essere scrivibili; HOME viene creata con questa identità.

Eseguire `sh ./pi-secretary setup`; poi `sh ./pi-secretary up`.
Comandi disponibili: `setup`, `configure`, `status`, `up`, `down`, `logs`,
`doctor`. `configure` ferma l'app solo dopo conferma. Un lock host serializza
tutti i comandi del launcher; i setup riutilizzano i lock delle sessioni esistenti.
Non eseguire contemporaneamente un servizio systemd o setup manuali sugli stessi
account: i lock proteggono solo sessioni che condividono lo stesso DATA_DIR.

`PI_SECRETARY_HOME` seleziona la directory host (default
`~/.local/share/pi-secretary`). `config/secretary.env`, `config/dashboard.json`,
`config/postgres.env` e `data/` sono persistenti. HOME del container è
`data/home`, mentre la cache bridge viene rigenerata in `/tmp/cache`.
Proteggere e fare backup di configurazione, dati e volume PostgreSQL;
`down` non cancella i volumi. Non cambiare postgres.env dopo l'inizializzazione
del volume: PostgreSQL non applica automaticamente nuove password.
Il nome progetto è fisso: `pi-secretary`, volume `pi-secretary_postgres`.
È supportata una sola istanza: cambiare PI_SECRETARY_HOME non isola il DB
né i container. La migrazione dei dati attuali non è automatica: trasferire
manualmente configurazione, token/sessioni e database.

Configurazione esistente: copiare privatamente il proprio `.env` in
`config/secretary.env` prima del wizard; i valori presenti non vengono sostituiti.
I percorsi dati/sessione/dashboard e bind sono imposti dal Compose e prevalgono
sul file. I token esistenti vanno trasferiti separatamente in `data/` preservando
proprietà e permessi. Non viene importata la configurazione host implicitamente.

Senza URI viene scelto PostgreSQL 16 locale, senza porte pubblicate. L'URI esterno
è richiesto con input nascosto; l'override esclude il profilo DB locale. Prima
di avviare app il launcher attende la readiness locale e verifica `SELECT 1`.
`doctor` non avvia il database, non importa `db.py` e non applica migrazioni.
Le migrazioni normali dell'app avvengono solo al suo avvio esplicito.
La progressione del wizard non contiene segreti. Le autenticazioni opzionali
richiedono consenso; i relativi script possono eseguire chiamate di rete.
Il wizard salva subito ogni risposta e riprende i campi mancanti senza
sovrascrivere chiavi esistenti. Include timezone, lingua, modello, ora digest,
fino a 50 email (provider/nome/indirizzo, password basic nascosta e host
IMAP/SMTP custom) e fino a quattro entità fatture. Porte e override avanzati
restano nel file privato. Microsoft usa il setup OAuth opzionale. Prima del
setup Telegram personale richiede API_ID, API_HASH nascosto e telefono;
senza tutti e tre il setup viene rinviato.

Non usare `docker compose config` per condividere diagnostica: può espandere
credenziali. Anche log e output degli script di autenticazione vanno trattati
come sensibili. Dashboard solo loopback host, senza TLS: accesso remoto tramite
tunnel o reverse proxy configurato separatamente. Nessuna promessa di hardening
production-ready, backup automatici, upgrade DB o compatibilità Chromium ARM
senza test reali. Dipendenze Python non bloccate a versioni esatte; l'immagine
base è fissata a versione/distribuzione, non digest.