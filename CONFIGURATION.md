# Vocali e immagini Telegram

Nella chat del bot sono supportati messaggi vocali, audio e foto (anche con
didascalia). OpenRouter trascrive l'audio prima del dialogo. Le foto del proprietario
sono invece inviate come parti `image_url` (data URL base64) al modello principale multimodale,
insieme a didascalia, identità dell'assistente, archivio, memoria e cronologia.
Anche le domande successive hanno accesso alla foto finché rimane
nella cronologia in RAM (ultimi 10 messaggi; persa al riavvio). Nessun tool remoto:
i marker calendario continuano a essere validati ed eseguiti dal codice principale.

- `OPENROUTER_AUDIO_MODEL`: `google/gemini-2.5-flash` (input audio).
- `OPENROUTER_MODEL`: `google/gemini-2.5-flash` (testo e immagini nativi).
- Entrambi usano `OPENROUTER_API_KEY`; `OPENROUTER_MODEL` è il modello principale.
  Viene usato per tutte le conversazioni, con o senza immagini, e deve supportare
  **nativamente input testo e immagini** (`image_url`): è un requisito obbligatorio,
  non sostituibile con OCR. Nessun modello o preprocessore immagini separato.
  Il prompt visivo è agnostico rispetto al soggetto della foto.
  Se la richiesta visiva fallisce o non restituisce testo, il fallback elimina le
  immagini, dichiara che non sono disponibili e non può creare eventi calendario.
  Non viene mostrato il reasoning interno del provider come risposta.
- I due modelli sono modificabili anche nella configurazione amministrativa AI.
- Limite per media: 10 MiB. I vocali Telegram OGG/Opus vengono inviati come
  `input_audio` base64; scegliere un provider compatibile con tale formato.
- Audio e immagini vengono trasmessi ai provider OpenRouter: valgono costi e
  condizioni di trattamento dati dei provider selezionati.
- Gli allegati sono archiviati prima della trascrizione audio; errori di trascrizione
  vengono ritentati dalla coda lavori, senza inviare contenuti parziali al modello.
  Solo la chat privata del proprietario configurato in `TELEGRAM_CHAT_ID` è
  accettata dal bot. Senza proprietario configurato nessun messaggio è accettato.
  Chat estranee, gruppi e canali sono ignorati prima di download, log dei contenuti,
  archiviazione o analisi AI, senza risposte; gli update vengono comunque consumati.
- Il testo delle immagini non viene interpretato come comando rapido o nuova regola.
- La pipeline testuale non converte immagini né le invia al trascrittore audio.
  L'estrazione generica PDF/XML resta separata e invariata, senza OCR immagini.
- Questa funzionalità riguarda il **bot**, non l'archiviazione dell'account personale
  Telegram (MTProto), separata e di sola lettura, che non scarica media.

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