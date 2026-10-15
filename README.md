# Pi Secretary

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
