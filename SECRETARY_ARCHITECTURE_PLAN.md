# Piano: p-secretary come segretario personale generalista

Stato: **proposta di implementazione, non funzionalità già realizzate**.
Data: 16 ottobre 2026. Base: analisi statica del repository corrente.

## 1. Obiettivo di prodotto

p-secretary deve prendere in carico le questioni affidate dal proprietario:
comprenderle, raccogliere il contesto, organizzare informazioni e documenti,
proporre o compiere il prossimo passo, seguirne l'esito e rendere conto.

La sua unità di lavoro non sarà il messaggio, ma la **pratica**. Il messaggio è
un'osservazione; una responsabilità personale indica che cosa seguire; una
pratica rappresenta una questione concreta; attività e strumenti ne eseguono i passi.

Non significa promettere competenze universali o simulare azioni inesistenti.
Il segretario deve conoscere strumenti disponibili, limiti, deleghe e informazioni
mancanti. Se non può fare qualcosa, deve dirlo e proporre il passo realizzabile.

### Contratto comportamentale

1. Ricorda obiettivi e preferenze senza richiedere che vengano ripetuti.
2. Collega comunicazioni e documenti pertinenti anche fra canali diversi.
3. Distingue fatti, dichiarazioni di terzi, ipotesi, decisioni e istruzioni.
4. Mantiene una prossima azione o un'attesa con data di riesame per ogni pratica attiva.
5. Chiede chiarimenti mirati quando l'incertezza cambia una decisione concreta.
6. Non inventa date, autorizzazioni, risultati, consegne o completezza dello storico.
7. Anticipa scadenze e mancate risposte, senza sommergere il proprietario di notifiche.
8. Agisce entro deleghe esplicite e rende visibile ciò che ha fatto e perché.
9. Chiude una pratica sulla base di criteri ed evidenze, non perché ha inviato un messaggio.
10. Rimane neutro rispetto ai domini: contabilità, casa, lavoro o altro dipendono dall'utente.

### Presa in carico, non generazione indiscriminata di pratiche

Distinguere una risposta puntuale, un incarico una tantum e una responsabilità
continuativa. «A che ora è la riunione?» non apre automaticamente una pratica;
«occupati di organizzarla» richiede invece un incarico con obiettivo e limiti.
Una comunicazione osservata può generare un candidato, non una delega implicita.
Le responsabilità già approvate possono prendere in carico nuovi casi nel loro ambito.

Alla presa in carico il segretario rende chiari, senza moduli superflui: cosa
seguirà, primo passo, eventuale decisione necessaria e quando tornerà a riferire.
«Me ne occupo» deve corrispondere a uno stato persistito e riesaminabile.

Precedenza: vincoli di sicurezza/permessi → istruzione owner corrente specifica →
responsabilità pertinente → preferenze salvate → default generali. Un'istruzione
corrente non amplia automaticamente le deleghe continuative. Due incarichi
incompatibili richiedono priorità o un compromesso esplicito, non una scelta nascosta.
La correzione «non al mattino, per questo progetto preferisco il pomeriggio»
aggiorna l'ambito corretto, senza cancellare indiscriminatamente altre preferenze.

## 2. Punto di partenza e componenti da preservare

| Area | Base disponibile | Evoluzione necessaria |
| --- | --- | --- |
| Ingressi | IMAP, bridge WhatsApp, bot Telegram, Telethon | Contratto comune di osservazione, copertura e autorità |
| Persistenza | Archivio nativo, allegati, PostgreSQL | Identità stabili, documenti e collegamenti a pratiche |
| Lavoro durevole | `jobs.py`, retry, lease, outbox | Dipendenze, rinnovo lease, priorità, risultati verificabili |
| Dialogo | `owner_dialogue.py`, piani persistiti, commit e replay | Strumenti di ricerca/azione e decisioni collegate alle pratiche |
| Memoria | Note, regole, riepilogo e recall del dialogo | Tipi, provenienza, versioni, ambito e responsabilità approvate |
| Attività | `owner_commitments.py`, `task_reconciliation.py` | Più attività per messaggio, attese, dipendenze e criteri di chiusura |
| Documenti | Originali, PDF/XML e conversione audio/immagini | Catalogo e indice generale, non soltanto cartelle contabili |
| Operatività | Promemoria, Calendar, digest, fatture | Ciclo continuativo e specializzazioni opzionali |

Non riscrivere questi componenti da zero. Conservare ID, versioni, ricevute,
provenienza, ordinamento delle risposte e chiavi idempotenti già in uso.

### Prerequisiti di sicurezza emersi dalla lettura

Da confermare con test riproducibili prima di aumentare l'autonomia:

- La barriera `historical` è presente nella riconciliazione, ma non è un controllo
  generale in `pipeline.ingest/analyze/action`. Un elemento importato e poi
  reincontrato dal polling non deve generare operazioni retroattive involontarie.
- La certezza dell'evidenza non prova l'autorità del mittente: la riconciliazione
  deve verificare chi può modificare quale impegno, soprattutto per effetti derivati.
- L'autenticazione della chat owner non rende autorevole il contenuto inoltrato.
  Occorre conservare inoltri, citazioni e origine dei media prima del routing.
- Il token del job protegge il suo completamento, non universalmente ogni effetto
  del worker scaduto. Servono fencing applicativo, deadline e gestione degli esiti incerti.
- I comandi legacy non hanno tutti le garanzie transazionali del dialogo durevole.
- Vanno coperti avanzamento dei cursori, riavvio WhatsApp, spool bloccato,
  conversioni media fallite e output AI invalido, senza perdere o ignorare lavoro.

## 3. Architettura logica

**Connettori → osservazioni persistenti → conoscenza e ricerca → pratiche →
decisione → policy → strumenti/outbox → ricevute e verifica → nuovo riesame.**

Il proprietario interagisce attraverso dialogo e dashboard. Entrambi usano gli
stessi servizi di dominio: niente secondo percorso privilegiato di scrittura.

### A. Percezione comune, privilegi separati

Ogni ingresso produce un'osservazione con:

- ID immutabile della sorgente/account, ID esterno, conversazione e revisione;
- identità tecnica del mittente, destinatari, direzione e metadati di inoltro;
- data dell'evento, data di acquisizione, fuso e provenienza live/recupero/import;
- corpo, allegati e testi derivati con riferimenti alle rispettive fonti;
- qualità, limiti e stato dell'acquisizione/estrazione.

Il nome visualizzato non è un'identità autenticata. Rinominare un account non
deve creare un nuovo account logico. Per le email acquisire anche `In-Reply-To`,
`References` e destinatari; il solo `Message-ID` non identifica un thread.

Email, WhatsApp e Telegram personale condividono la comprensione, non i privilegi.
Un messaggio inviato dall'account personale non diventa automaticamente un comando.
Nel bot, testo diretto, inoltro, documento, didascalia e trascrizione devono
mantenere distinta la loro autorità: un file non deve perdere il dialogo associato.

L'import storico può alimentare catalogo e contesto, ma non concedere deleghe o
riattivare azioni scadute. Il recupero dopo un'interruzione deve invece valutare
validità attuale e finestra dell'azione. L'eventuale elaborazione operativa dello
storico richiede una richiesta separata, circoscritta e osservabile.

### B. Conoscenza e ricerca realmente utilizzabili

Costruire un servizio di ricerca trasversale su comunicazioni, documenti,
trascrizioni, persone, pratiche e memoria. Deve consentire al segretario di
cercare, aprire una fonte e approfondire, non ricevere soltanto gli ultimi messaggi.

Prima implementazione: PostgreSQL full-text, filtri strutturati e indici.
Ricerca semantica opzionale solo se i benchmark mostrano lacune effettive; nessun
database vettoriale obbligatorio nella prima versione.

Ogni risultato include fonte, data, revisione, frammento e stato di estrazione.
Recupero del contesto per pratica e responsabilità, con budget complessivo:
non tutte le note e tutto lo storico dentro ogni prompt.

Le identità cross-channel vengono collegate con evidenze forti o conferma owner.
Omonimia e somiglianza del testo generano candidati, non fusioni irreversibili.

### C. Pratiche e responsabilità

**Pratica:** una questione concreta, ad esempio una riparazione, una candidatura
o l'organizzazione di un viaggio. Contiene obiettivo, partecipanti, documenti,
decisioni, attività, prossima azione, attese e criterio di conclusione.

Stati iniziali: `proposed`, `active`, `waiting`, `blocked`, `completed`, `cancelled`.
Le transizioni sono validate; una pratica completata può riaprirsi con nuova
evidenza e motivazione. Una pratica attiva deve avere un prossimo passo, una
condizione di attesa/riesame oppure un blocco esplicito.

**Responsabilità:** una delega continuativa, per esempio «segui le manutenzioni
di casa e avvisami quando manca una risposta». Definisce:

- obiettivo, ambito, sorgenti/interlocutori e dati utilizzabili;
- trigger, eventuale periodicità, condizioni di arresto e risultato atteso;
- capacità richieste, azioni consentite e conferme necessarie;
- destinatari, limiti di frequenza/costo e preferenze di rendicontazione;
- origine owner, versione, stato e autorizzazioni associate.

Le condizioni eseguibili sono strutturate e validate. Il modello può proporle,
ma non installare codice, SQL, shell o workflow arbitrari.

### D. Memoria personale, non prompt universale

Separare quattro concetti, mantenendo una presentazione semplice nella UI:

| Tipo | Esempio | Effetto |
| --- | --- | --- |
| Fatto/preferenza | «Preferisco appuntamenti al mattino» | Orienta proposte e ricerca |
| Nota di riferimento | «Questi sono i dati per la spedizione» | Informazione consultabile |
| Procedura/responsabilità | «Raccogli questi documenti ogni mese» | Proposta operativa versionata |
| Delega | «Puoi inviare a questo destinatario entro questi limiti» | Autorizza effetti specifici |

Il dialogo è il punto di ingresso naturale. Una richiesta chiara può attivare
capacità interne a basso rischio; non occorre chiedere due volte la stessa cosa.
Ambiguità, destinatari mancanti o effetti sensibili richiedono chiarimenti mirati.
Una nota ricordata non equivale a una procedura attiva o a un permesso di invio.

Memorie con fonte, ambito, versione e validità temporale. Contraddizioni da
risolvere esplicitamente; i riepiloghi non sovrascrivono i fatti strutturati.
Le vecchie note/regole restano consultabili e possono essere proposte come
responsabilità, mai promosse in blocco ad automazioni autorizzate.

### E. Runtime decisionale con strumenti limitati

Estendere il coordinamento durevole di `owner_dialogue`, non sostituirlo con
una conversazione infinita fra agenti. Un singolo coordinatore iniziale usa
strumenti tipizzati; eventuali estrattori specializzati non hanno deleghe proprie.

Per ogni risveglio:

1. Caricare pratica, responsabilità, versioni, deleghe ed evidenze pertinenti.
2. Cercare/aprire fonti mancanti con un numero limitato di chiamate.
3. Preparare un piano strutturato: motivazione sintetica, passi, dipendenze,
   evidenze, effetti attesi, criteri di verifica e condizioni di arresto.
4. Validare il piano e valutare le autorizzazioni fuori dal modello.
5. Persistire piano e versioni; committare modifiche locali e outbox atomicamente.
6. Eseguire gli effetti esterni fuori dalla transazione.
7. Registrare ricevute, verificare l'esito e aggiornare pratica/prossimo riesame.

Ogni passo ha identità stabile persistita. Retry e riavvii riusano la decisione
salvata; modifiche a documenti, pratica o delega invalidano i passi dipendenti.
Nessuna chiamata AI o HTTP dentro transazioni DB lunghe.

Limiti per esecuzione: passi, tempo, token, costo e azioni. Un'operazione dubbia
si ferma con una domanda utile o uno stato di blocco, non con un loop di retry.
Salvare motivazioni operative e fonti, non richiedere ragionamenti interni estesi.

## 4. Autonomia e autorizzazioni

Disponibilità tecnica, abilitazione e autorizzazione sono stati diversi.
Una connessione SMTP funzionante non autorizza a inviare qualsiasi email.

| Classe di effetto | Default per nuova installazione |
| --- | --- |
| Cercare nei dati autorizzati, estrarre, catalogare senza distruggere originali | Automatico entro policy dati e budget |
| Preparare bozze, proporre pratiche e collegamenti incerti | Automatico, dichiarato come proposta |
| Creare attività/promemoria richiesti direttamente dal proprietario | Automatico con ricevuta |
| Mutare impegni sulla base di dichiarazioni di terzi | Solo se criteri e autorità sono delegati; altrimenti proposta |
| Inviare messaggi/documenti, accettare appuntamenti, assumere impegni | Conferma specifica o delega circoscritta |
| Pagare, comprare, cancellare originali o divulgare dati sensibili | Non autonomo nel primo rilascio; eventuale funzione futura esplicita |

Il motore policy verifica prima della preparazione e immediatamente prima
dell'effetto: identità, ambito, provenienza, versione, destinatario, allegati,
limiti, scadenza della delega e stato corrente della pratica.

L'approvazione riguarda un payload concreto: testo/bozza, destinatari, documenti
e loro versioni. Cambiarli invalida la conferma. Anche un «sì» deve riferirsi a una
proposta identificabile e ancora valida; non approva tutte le proposte pendenti.

Revocare una responsabilità impedisce nuovi effetti e invalida il lavoro non
ancora eseguito. Un invio già accettato dal provider non è annullabile per magia:
il sistema deve distinguere ciò che può fermare da ciò che è già avvenuto.

## 5. Catalogazione generale e filesystem

Separare **contenuto originale**, **occorrenza**, **metadati**, **testo derivato**
e **collocazione logica**. Lo stesso PDF può arrivare due volte o appartenere a
più pratiche: deduplicare i bytes non deve cancellare le diverse provenienze.

Pipeline documentale:

1. Acquisire l'originale, verificare dimensioni/MIME e calcolare l'hash.
2. Registrare documento, occorrenza e stato di disponibilità.
3. Estrarre testo/metadati con strumenti supportati e versionati.
4. Classificare genericamente e collegare alla pratica con fonti e incertezza.
5. Indicizzare e pubblicare una vista leggibile, ricercabile e scaricabile.
6. Applicare eventuali convenzioni personali autorizzate di cartella/esportazione.

Conservare gli originali; non spostarli o rinominarli distruttivamente per una
classificazione AI. Usare cartelle/esportazioni leggibili accanto a un archivio
immutabile. Path e nomi derivano da template validati, non da stringhe arbitrarie
del modello; impedire traversal, accessi fuori radice e attraversamento di symlink.

Estendere progressivamente il supporto: PDF/XML già disponibili, immagini/audio,
file caricati e documenti degli account personali. OCR PDF, archivi compressi,
formati Office e navigazione web sono capacità dichiarate e opzionali, non
presunte. Parsing isolato e limiti di risorse; segnalare esplicitamente estrazioni
parziali o fallite. Nessun parser di allegati riceve autorità di esecuzione.

Filesystem e DB non condividono una transazione: usare pubblicazione atomica,
stati intermedi, verifiche hash e recupero degli orfani. Migrare senza imporre
spostamenti iniziali, mantenendo leggibili sia riferimenti `path` sia `BYTEA`.
Backup e ripristino devono verificare insieme database, file e associazioni.

## 6. Rimuovere il bias fatture senza perdere funzionalità

Il nucleo può riconoscere genericamente un tipo documentale. Non deve richiedere
campi fiscali in ogni risposta né assegnare una priorità speciale alla contabilità.

### Capacità opzionale iniziale: gestione fatture

Separare riconoscimento specializzato, estrazione dei dati, associazione all'entità,
archiviazione personale, raccolta periodica e consegna. Sono azioni distinte.
Una fattura riconosciuta non autorizza né il suo pagamento né l'invio a un terzo.

Il registro delle capacità dichiara schema, strumenti, permessi, requisiti,
versione e verifiche. Iniziare con moduli interni, senza marketplace di plugin.
ZIP, estrazione PDF e salvataggio file rimangono primitive generali.

Punti da disaccoppiare: prompt/normalizzazione in `brain.py`, rami e scheduler
in `pipeline.py`, default in `flows.py`, schema contabile nei job, configurazione,
onboarding, dashboard e frontend. Rimuovere anche identità personali incorporate
nei testi di prodotto, mantenendo i dati personali soltanto nel deployment.

### Default e migrazione

- **Nuova installazione:** nucleo neutro; nessuna entità contabile, raccolta
  mensile o invio contabile attivato implicitamente.
- **Installazione esistente:** conservare dati e stati effettivi dei flussi,
  distinguendo impostazioni esplicite da comportamento legacy ereditato.
  Renderli visibili e revisionabili, senza presentarli come nuove approvazioni.
- Non dedurre l'utilizzo da `INVOICE_DIR`, che esiste anche per default, né
  soltanto dalle entità: può esserci un archivio storico sotto `sconosciuto`.
- Preservare temporaneamente `/fatture`, chiavi config, campi API, vecchi verdetti,
  chiavi `invoice:*`/`zip:*` e handler attraverso adattatori di compatibilità.
- Una capacità sospesa lascia consultabile l'archivio e non riattiva consegne.
  Non rigenerare lavori dai vecchi verdetti né riarmare ZIP già inviati.
- I controlli di sicurezza nuovi prevalgono su automatismi non sufficientemente
  autorizzati: mettere in attesa e spiegare, senza perdere dati o inventare consenso.

La personalizzazione vive nel DB/configurazione privata. La repo contiene solo
capacità generiche e procedure di esempio inattive, nessuna preferenza reale owner.

## 7. Modello dati incrementale

Evolvere PostgreSQL con migrazioni additive versionate, non sostituire l'archivio.
Nomi indicativi da finalizzare durante le fasi; evitare una tabella per ogni
astrazione prima di averne un caso d'uso verificato.

| Oggetto | Dati/relazioni essenziali |
| --- | --- |
| Sorgente e identità | ID immutabile, account, identità tecniche, alias confermati |
| Osservazione | Messaggio/revisione esistenti, provenienza, tempi, qualità, autorità |
| Documento | Hash/contenuto, occorrenze, testo derivato, stato, metadati versionati |
| Pratica | Obiettivo, stato/versione, responsabilità, partecipanti, criterio di chiusura |
| Collegamenti | Pratica ↔ messaggi/documenti/attività con motivazione e provenienza |
| Attività/attesa | Task esistente, assegnatario, dipendenze, atteso da, prossimo riesame |
| Memoria/responsabilità | Tipo, fonte owner, ambito, versione, stato e validità |
| Piano e passi | Risveglio, snapshot/versioni, dipendenze, strumenti, stato |
| Delega/approvazione | Effetti consentiti, payload/ambito, limiti, versione, scadenza |
| Ricevuta | Azione, provider ID, risultato, verifica, errore/esito incerto |

Le associazioni devono avere integrità referenziale verificabile. Una pratica
contiene più messaggi, e un messaggio può contribuire a più pratiche. Consentire
più attività per messaggio sostituendo gradualmente `UNIQUE(source_message)` con
identità operative persistite; preservare tutti gli ID e le vecchie ricevute.

Per i fatti distinguere tempo di validità e tempo di registrazione. Registrare
revisioni e audit delle decisioni importanti; non serve convertire l'intero
sistema a event sourcing o introdurre un database a grafo.

## 8. Ciclo proattivo e controllo dell'attenzione

Il segretario si risveglia per nuovi dati, richieste owner, scadenze, attese
scadute, esiti degli strumenti o controlli periodici delle pratiche.
Non interroga continuamente il modello quando non cambia nulla.

Scheduler persistente con `next_review_at`, motivazione e chiavi idempotenti.
Accorpare eventi ravvicinati della stessa pratica e serializzarne le mutazioni
per versione, evitando che analisi e riconciliazione creino due attività equivalenti.

Una mancata risposta produce il passo appropriato alla delega: avviso, bozza,
sollecito autorizzato o escalation. Mai un sollecito infinito: limiti e stop condition.
Separare gravità, urgenza, scadenza, importanza personale e costo dell'interruzione.
Quiet hours, riepiloghi raggruppati e preferenze owner per la frequenza.

Il briefing riporta ciò che richiede una decisione, ciò che è in attesa e ciò
che è cambiato. Gli esiti vengono dalle ricevute, non dalle intenzioni del modello.
Un giorno saltato o un servizio indisponibile non deve far sparire il lavoro:
recupero esplicito e indicazione della copertura effettiva.

## 9. Affidabilità, privacy e limiti operativi

- Contratto strumenti: input/output tipizzati, permessi, idempotenza, timeout,
  classificazione del rischio, verifica e comportamento in caso di esito incerto.
- Ricevute esterne distinguono: preparato, autorizzato, accodato, tentato,
  accettato dal provider, verificato, fallito, incerto. Accettato non significa letto.
- Dove il provider supporta identità idempotenti, utilizzarle; altrimenti non
  promettere exactly-once. Dopo timeout di un invio, riconciliare prima di ritentare;
  se non è verificabile, fermarsi o chiedere invece di duplicare alla cieca.
- Rinnovo lease con fencing e deadline; nessuna promessa che il solo DB possa
  fermare un effetto remoto già in corso. I job ricevono contesto di esecuzione.
- Code con priorità e quote per non bloccare risposte owner dietro backfill/media.
  Dipendenze fallite terminano in stato spiegabile, non in `Deferred` eterno.
- Budget AI per esecuzione/giorno, contabilità usage, rate limit, backpressure,
  circuit breaker e modalità degradata senza perdita dell'acquisizione.
- Policy di dati per sorgente/responsabilità: cosa può andare a un provider AI,
  inclusi media e import storici. Minimizzazione, segreti esclusi e log sanitizzati.
- Retention, esportazione e oblio coordinati fra note, indice, riepiloghi, piani,
  originali e backup secondo una policy esplicita. Non basta cancellare una nota.
- Traccia consultabile: fonte → pratica → decisione → autorizzazione → azione → esito.
  Metriche su freschezza e copertura ingressi, code, blocchi, costo e consegne.

## 10. Roadmap implementativa

Ogni fase consegna codice, migrazioni, test, UI/dialogo minimo e documentazione.
Non aspettare l'ultima fase per rendere controllabile ciò che viene introdotto.

### F0 — Baseline e contratto di comportamento

- Inventario capacità realmente eseguibili, limiti e contratti attuali.
- Ripristinare una baseline riproducibile dei test, distinguendo difetti preesistenti.
- Estendere CI oltre installer/build: suite offline e PostgreSQL isolato.
- Costruire dataset sintetico multicanale e una tabella feature/supporto/delega.

**Uscita:** baseline registrata, scenari d'accettazione e nessun test sulla produzione.

### F1 — Confini di fiducia e runtime affidabile

- Contratto di osservazione/provenienza e barriera storico verificata anche nei worker.
- Conservazione inoltri/autorità, policy centralizzata degli effetti e audit minimo.
- Matrice di tutti gli effetti esistenti, inclusi inferenza/media verso provider AI,
  Calendar, notifiche owner e consegne ZIP: autorizzazione, controllo al momento
  dell'esecuzione, revoca, ricevuta e default bloccante quando il permesso manca.
  Nessuna attesa di F7 per applicare questi confini ai percorsi già operativi.
- Rendere durevoli i comandi owner; correggere cursori/ACK, starvation e retry bloccati.
- Lease, deadline, gestione dipendenze e ricevute iniziali.

**File principali:** `main.py`, `message_store.py`, `pipeline.py`, `jobs.py`,
connettori, `task_reconciliation.py`, `owner_dialogue.py`.

**Uscita:** replay, import e messaggi non autorevoli non provocano effetti non consentiti;
nessun testo/media viene inviato all'AI senza la policy dati pertinente.

### Percorso minimo fino alla prima dimostrazione

Le fasi F2–F5 indicano dipendenze logiche, non quattro piattaforme da completare
integralmente prima di provarne l'utilità. Dopo il gate F1 costruire una sola
sezione verticale, con questi sottoinsiemi:

1. F2: profilo neutro e adattatore fatture, senza un framework generico di plugin.
2. F3: ricerca sulle fonti email/WhatsApp e PDF già acquisiti del caso pilota,
  senza aspettare tutti i formati, identità o backfill dell'intero archivio.
3. F4: pratica minima, collegamenti, prossima azione/attesa, una preferenza con
  fonte e una delega esplicita; non l'intera ontologia di memoria e responsabilità.
4. F5: richiesta → ricerca → pratica → bozza/attività → ricevuta → riesame.

Eseguire subito anche casi equivalenti in altri domini. Estendere poi gli stessi
contratti e completare gli altri elementi F2–F5; non rimandare il feedback utente
al completamento di ogni astrazione elencata nel piano.

### F2 — Nucleo neutro e registro capacità

- Separare schema/prompt generici dai contributi delle capacità attive.
- Estrarre fatture e raccolta mensile dal contratto universale, mantenendo adattatori.
- Versionare il profilo operativo: nuova installazione neutra e legacy esplicito.
- Onboarding per obiettivi; UI distingue disponibile, attivo, autorizzato e operativo.

**File principali:** `brain.py`, `pipeline.py`, `invoices.py`, `flows.py`,
`config.py`, `admin_config.py`, `installer.py`, `setup_ui.py`, `dashboard.py`, `web/`.

**Uscita:** nessun workflow contabile implicito su installazione nuova; upgrade senza
perdita archivio, duplicazione ZIP o silenziosa espansione dei permessi.

### F3 — Fonti, documenti e ricerca comune

- ID sorgenti stabili, relazioni identità confermate e metadati thread email.
- Catalogo originale/occorrenza/testo estratto; indicizzazione versionata.
- Strumenti `search`, `open_source`, `get_document` con fonti e limiti.
- Backfill esplicito, riprendibile e privo di effetti operativi.

**File principali:** `message_store.py`, `documents.py`, `email_attachments.py`,
`connectors/media_archive.py`, `file_browser.py`; nuovi servizi di catalogo/ricerca.

**Uscita:** recuperare un documento vecchio dal dialogo, citarne la fonte e distinguere
«non trovato» da «non acquisito/non estraibile», senza rileggere tutto nel prompt.

### F4 — Pratiche, memoria tipizzata e responsabilità

- Schema minimo pratiche/collegamenti/attese e transizioni versionate.
- Evolvere task e note senza ricrearli; più attività per messaggio.
- Dialogo per creare, correggere, sospendere e revocare responsabilità/deleghe.
- Registro decisioni e recupero del contesto comune anche agli estrattori.

**File principali:** `owner_dialogue.py`, `owner_commitments.py`,
`task_reconciliation.py`, `db.py`; nuovi servizi di pratiche, memoria e policy.

**Uscita:** una questione riunisce fonti di canali diversi e ha obiettivo, prossima
azione/attesa e criterio di chiusura; una nota non diventa un invio autorizzato.

### F5 — Coordinatore durevole e prima sezione verticale

- Tool loop limitato: ricerca, lettura, collegamenti, task, bozze e catalogazione.
- Piani persistiti, passi identificati, controllo versioni e ricevute reali.
- Percezione comune dei canali senza unificare autorità; deduplica operativa.
- Riesame temporale minimo di una pratica, usando lo scheduler esistente.

**Prima dimostrazione, non contabile:** «Segui la riparazione di casa: recupera
preventivo e accordi, segnala differenze e ricordami la conferma; non scrivere
al fornitore senza chiedermelo». Email e WhatsApp alimentano una sola pratica;
documento trovato, confronto con fonti, bozza e scadenza, nessun invio implicito.

**Uscita:** percorso completo richiesta → contesto → azione locale → ricevuta →
riesame, recuperabile dopo crash senza duplicazioni degli effetti locali.

### F6 — Proattività e catalogazione estese

- Attese, follow-up, dipendenze, riapertura, quiet hours e briefing orientato alle decisioni.
- Parità di estrazione documentale fra ingressi, inclusi upload e documenti personali.
- Convenzioni personali di archivio/esportazione e responsabilità fatture opzionale.
- Limiti e costi visibili per OCR o formati aggiuntivi; nessuna falsa promessa di supporto.

**Uscita:** il segretario interviene anche senza nuovi messaggi quando occorre,
ma si arresta correttamente quando la pratica è risolta, sospesa o revocata.

### F7 — Esecuzione esterna delegata e verifica

- Integrare uno strumento per volta: Calendar completo, poi bozze/invio email;
  altri canali di invio soltanto con ricevute e gestione errori adeguate.
- Conferme vincolate al payload, revoca, destinatari stabili e limiti per responsabilità.
- Le garanzie di autorizzazione esistono già da F1: qui vengono applicate ai nuovi
  strumenti e alle nuove deleghe, non introdotte per la prima volta.
- Riconciliazione esiti remoti, gestione ambiguità e drift Calendar.
- Gestione quotidiana di appuntamenti, disponibilità, conflitti e ripianificazione
  con strumenti effettivi; non dedurre disponibilità da dati incompleti.

**Uscita:** «preparato», «accodato» e «accettato/verificato» sono distinguibili;
nessun invio duplicato intenzionalmente per un timeout non riconciliato.

### F8 — Consolidamento e rollout

- Vista «cosa stai seguendo per me», pratiche, documenti, deleghe ed esiti.
- Esportazione/oblio, budget, avvisi di degrado e restore DB+filesystem verificato.
- Benchmark semantici e di carico su hardware target; ottimizzare solo i colli reali.
- Rollout osservativo, pilota opt-in, autonomia graduata e rollback provato.

**Uscita:** utilità misurata su casi reali autorizzati, confini verificati e gestione
operativa comprensibile senza consultare i log tecnici.

## 11. Scenari e criteri d'accettazione

| Scenario | Comportamento richiesto |
| --- | --- |
| Nuovo utente senza interesse contabile | Nessun workflow fatture/ZIP o priorità contabile ereditata |
| Preferenza espressa in chat | Ricordata e applicata nei contesti pertinenti, non soltanto nella risposta corrente |
| Preventivo email + accordo WhatsApp | Una pratica, confronto documentato; associazioni ambigue chiarite |
| Più documenti nello stesso messaggio | Classificazione per documento, non tutti trattati come fattura |
| Stesso file ricevuto da due persone | Un contenuto, due provenienze consultabili |
| Ricerca di un documento vecchio | Recupero dalla ricerca con originale/fonte, non invenzione dal riepilogo |
| Proprietario attende una risposta | Riesame alla data prevista, bozza/sollecito secondo delega |
| Nuova evidenza dopo chiusura | Valutazione di riapertura, senza rieseguire vecchi invii |
| Inoltro o PDF con istruzioni ostili | Contenuto come dato, nessuna modifica a policy o deleghe |
| Import storico e successivo polling | Contesto consultabile, nessun replay operativo non autorizzato |
| Cambio destinatario dopo approvazione | Conferma invalidata prima dell'invio |
| Revoca con job pendenti | Lavoro non eseguito fermato; esiti già avvenuti dichiarati |
| Crash dopo invio / risposta provider persa | Stato incerto riconciliato o esposto; niente successo inventato |
| Servizio AI/provider indisponibile | Acquisizione durevole e degrado visibile, nessun silenzioso abbandono |
| Restore backup | Documenti e pratiche nuovamente accessibili e associati correttamente |

Verifiche a livelli diversi:

1. Test deterministici di schema, policy, date, path e transizioni.
2. Test PostgreSQL di atomicità, concorrenza, replay, revoca e migrazione.
3. Fault injection su crash, timeout, file assenti e provider ambigui.
4. Valutazione semantica del modello su corpus sintetico bilanciato fra domini:
   associazione pratiche, copertura impegni, qualità ricerca, domande e falsi allarmi.
5. Pilota supervisionato con fonti reali solo dietro autorizzazione e budget.

Le simulazioni del modello non provano la qualità semantica reale. I gate di
sicurezza richiedono zero violazioni nei casi protetti della suite, non una
promessa di infallibilità. Soglie di precisione/recall e latenza vanno fissate sul
dataset e sull'hardware in F0, prima di sviluppare i relativi incrementi,
poi mantenute come regressioni.

### Gate minimi assegnati alle fasi

| Gate | Prova e soglia iniziale |
| --- | --- |
| F0–F1 | Tutti i test deterministici dei confini protetti e di replay passano; nessun nuovo errore rispetto alla baseline documentata |
| F2 | Installazione nuova senza workflow contabile implicito; ogni fixture legacy conserva archivio/stati senza duplicare effetti |
| F3 | Tutti i documenti campione supportati e indicizzati sono apribili con provenienza; quelli mancanti/parziali sono dichiarati come tali |
| Prima sezione verticale | Almeno tre domini non contabili: casa, viaggio e lavoro; stessi strumenti di base, nessun ramo specifico per il caso pilota |
| F4–F6 | Scenari di presa in carico, non-presa-in-carico, correzione preferenze e arresto follow-up passano in tre esecuzioni con modello/prompt fissati |
| F7 | Suite fault injection e revoca interamente verde; nessun successo o autorizzazione inventati nei casi protetti |

Il corpus in F0 deve includere casi senza azione e ambigui, non solo casi positivi.
I fallimenti semantici non vengono mascherati da retry finché il caso passa:
si registra l'esito di ciascuna esecuzione. Per un pilota supervisionato si possono
limitare le capacità, non allentare i gate di autorizzazione.

Metriche di prodotto: pratiche senza prossimo passo, scadenze perse, conferme
superflue, notifiche inutili, collegamenti errati, documenti recuperabili, azioni
con ricevuta verificabile e costo per pratica seguita. Non usare il numero di
chiamate LLM come indicatore di qualità agentica.

## 12. Migrazione, distribuzione e priorità

Migrazioni nuove richiamate da `db.init`, additive e riprendibili. Non modificare
soltanto uno schema già receipted: sulle installazioni esistenti non verrebbe
rieseguito. L'eventuale vecchia tabella `communications` richiede un percorso
offline separato, non reset o conversione implicita durante l'avvio.

### Protocollo di passaggio fra vecchi e nuovi percorsi

1. Inventariare versioni, configurazione effettiva, job pendenti e ricevute.
2. Costruire mapping verificabili di ID, capacità legacy e autorizzazioni; ciò che
  è ereditato resta etichettato come tale, non come consenso appena espresso.
3. Applicare prima schema e letture compatibili; fare backfill idempotente con
  checkpoint e confronto dei conteggi/hash, senza effetti operativi.
4. Sospendere brevemente i soli dispatcher interessati, censire i tentativi già
  in corso e scegliere esplicitamente se drenare, mantenere compatibili o
  invalidare ogni classe di job. Non far partire contemporaneamente due scheduler
  sullo stesso workflow con chiavi differenti.
5. Spostare i writer al nuovo contratto. In particolare, non rimuovere
  `UNIQUE(source_message)` mentre un vecchio writer usa ancora quel conflitto:
  mantenere la compatibilità o dichiarare e verificare la versione minima.
6. Verificare invarianti finali: nessun ID/documento perso, nessuna consegna
  riarmata, deleghe revocate ancora revocate, pratiche e task riconciliati.
7. Provare il rollback di letture e writer compatibili senza riattivare effetti
  sospesi. Le migrazioni irreversibili non devono dipendere dal downgrade cieco
  dell'immagine precedente.

Feature flag e profilo versione per attivare le capacità gradualmente. Modalità
osservativa: genera piani e confronti, ma non esegue nuovi effetti. Eseguire
backfill su copie isolate prima della produzione; non renderlo obbligatorio per
avviare il servizio. Un rollback di funzionalità non elimina tabelle o dati;
la compatibilità con immagini precedenti va verificata, non presunta.

Aggiornare Dockerfile, packaging e documentazione con ogni nuovo modulo.
Il codice nel deployment Docker è nell'immagine: file presenti nella working
copy non implicano rollout del servizio. Nessun riavvio/invio/migrazione di
produzione fa parte della sola preparazione di questo piano.

### Scelte di semplicità

- Un'applicazione modulare Python, PostgreSQL e filesystem; conservare il bridge
  Node necessario a WhatsApp. Niente microservizi, Redis o framework multi-agent
  obbligatori nella prima iterazione.
- Autorità e stato restano in servizi deterministici; il modello interpreta e
  propone decisioni tramite contratti validati, non scrive liberamente su DB/FS.
- Prima una sezione verticale utile e sicura, poi nuove capacità. Estensioni come
  browser, prenotazioni o acquisti si aggiungono soltanto con strumenti, verifiche
  e deleghe specifiche; non sono abilità dichiarate per effetto di un prompt.
- Non fissare tempi di consegna prima di F0 e del dimensionamento dei dati.
  La sequenza segue dipendenze e gate, non l'aggiunta indiscriminata di feature.

**Ordine di investimento:** affidabilità e fiducia → nucleo neutro → ricerca e
pratiche → ciclo operativo → deleghe ed esiti. Più autonomia soltanto quando è
possibile dimostrare che cosa il segretario ha capito, chi lo ha autorizzato,
che cosa ha fatto e che cosa resta da fare.