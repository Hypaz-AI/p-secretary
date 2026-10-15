"""Italian setup help; stdlib only, no credentials or configuration reads."""
import os
import sys


def section(title, steps=()):
    """Keep redirected output plain and respect NO_COLOR (even when empty)."""
    color = (sys.stdout.isatty() and 'NO_COLOR' not in os.environ
             and os.environ.get('TERM', '') not in ('', 'dumb'))
    heading = f'\n── {title} ──'
    print(f'\033[1;36m{heading}\033[0m' if color else heading)
    for number, step in enumerate(steps, 1):
        print(f'  {number}. {step}')


GUIDES = {
    'welcome': ('Configurazione guidata · PI-SECRETARY', (
        'Prepara un browser sul PC o telefono: i link ufficiali sono indicati in ogni sezione.',
        'Inserisci segreti solo nei prompt nascosti del terminale o sui siti ufficiali, mai in chat o nei log.',
        'INVIO mantiene il valore indicato o salta; [s/N] richiede un sì esplicito. Le risposte vengono salvate subito.',
        'I login sono opzionali e separati dall’avvio. Puoi riprendere con setup o modificare con configure.')),
    'database': ('Database PostgreSQL', (
        'Per iniziare premi INVIO: il launcher gestisce PostgreSQL locale e genera una password privata, senza pubblicare la porta DB.',
        'Per un server esterno crea prima database e utente dedicati nel pannello del fornitore e autorizza la rete del container.',
        'Prepara un URI postgresql://UTENTE:PASSWORD@HOST:5432/DATABASE: codifica i caratteri speciali e usa il TLS richiesto dal fornitore.',
        'Guida ufficiale URI/TLS: https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNSTRING',
        'In Docker localhost indica il container, non il server host. Il cambio di destinazione NON migra dati né ruota password esistenti.')),
    'identity': ('Identità e provider AI · OpenRouter', (
        'Scegli il nome con cui si presenterà l’assistente; non è una credenziale.',
        'Accedi o registrati su https://openrouter.ai/ e controlla credito e limiti su https://openrouter.ai/settings/credits',
        'Apri https://openrouter.ai/settings/keys → Create Key; scegli nome e limite di spesa, copia la chiave nel prompt nascosto.',
        'La chiave è necessaria prima di avviare l’app. Non incollarla in comandi, screenshot o chat; revocala dal sito se esposta.',
        'Le comunicazioni elaborate possono essere inviate al provider AI: verifica privacy e policy su https://openrouter.ai/docs/guides/privacy/logging')),
    'preferences': ('Preferenze e fatture', (
        'Fuso orario: Europe/Rome gestisce anche l’ora legale; lingua: it per italiano.',
        'Cerca il modello su https://openrouter.ai/models e copia l’identificativo provider/modello, non il nome commerciale; verifica costi e disponibilità.',
        'Ora digest: formato 24 ore HH:MM nel fuso scelto (es. 21:00). Il bot Telegram va collegato per riceverlo.',
        'Le entità fatture sono facoltative (massimo 4): nome/cartella, partita IVA e descrizione aiutano la classificazione; non sono credenziali fiscali.')),
    'email': ('Account email', (
        'Scegli il provider della casella, poi un nome riconoscibile e l’indirizzo completo. Sono supportati fino a 50 account.',
        'gmail usa una password per app; outlook è Microsoft personale, m365 è lavoro/scuola e richiedono OAuth, non password.',
        'zimbra_ovh usa il preset OVH; custom richiede gli host IMAP (lettura) e SMTP (invio) indicati dal fornitore.',
        'Rifiutare un nuovo account termina le aggiunte; quelli già salvati restano disponibili. Un account senza credenziali non è ancora operativo.')),
    'gmail': ('Gmail · password per app', (
        'Accedi con la casella corretta su https://myaccount.google.com/security e attiva Verifica in due passaggi.',
        'Apri https://myaccount.google.com/apppasswords, crea una password per questa app e copiala nel prompt nascosto, non usare la password Google.',
        'Se la voce manca, policy Workspace o protezioni dell’account possono impedirla: consulta https://support.google.com/accounts/answer/185833 o l’amministratore.')),
    'outlook': ('Email Microsoft · OAuth', (
        'Non inserire password per app o password della casella: salva l’indirizzo e scegli poi il setup microsoft.',
        'Per outlook usa l’account personale; per m365 l’account lavoro/scuola. Il consenso verrà completato nel browser.')),
    'zimbra_ovh': ('Email OVH · verifica la casella', (
        'Accedi al pannello ufficiale https://www.ovh.com/manager/ → Web Cloud → servizio email e verifica indirizzo, password casella e server.',
        'Il preset usa ssl0.ovh.net: Zimbra e altre offerte possono avere server diversi; configura gli override EMAILn_IMAP_HOST/SMTP_HOST nel file privato.',
        'Consulta la guida della tua offerta su https://help.ovhcloud.com/ e inserisci solo la password della casella, non quella del pannello OVH.')),
    'custom': ('Email personalizzata · parametri del fornitore', (
        'Nel pannello ufficiale del tuo fornitore cerca Configurazione client email: abilita IMAP e annota host IMAP/SMTP, porte e TLS.',
        'Se il fornitore offre password per app, creane una dedicata dopo aver attivato 2FA. Non usare credenziali del pannello amministrativo.',
        'Inserisci host senza https://; per porte e override avanzati consulta .env.example e modifica solo le chiavi necessarie nel file privato.')),
    'telegram': ('Bot Telegram · dialogo e notifiche', (
        'Apri il BotFather ufficiale https://t.me/BotFather (guida: https://core.telegram.org/bots/tutorial#obtain-your-bot-token).',
        'Invia /newbot, scegli nome e username che termina in bot; incolla il token solo nel prompt nascosto. INVIO rinvia il setup.',
        'Apri il nuovo bot, premi Avvia e invia dalla TUA chat privata il codice monouso mostrato dal setup entro 120 secondi; /start da solo non basta.',
        'Attendi il messaggio di prova. Non inviare il codice in gruppi; se il token è esposto revocalo con BotFather. Il bot non legge il tuo account personale.')),
    'telegram_account': ('Telegram personale · API e accesso', (
        'Accedi su https://my.telegram.org con il numero del tuo account; apri API development tools e crea un’app (titolo e nome breve).',
        'Guida ufficiale: https://core.telegram.org/api/obtaining_api_id — copia api_id numerico e api_hash nei rispettivi campi; non sono il token BotFather.',
        'Inserisci il telefono completo di prefisso internazionale (es. +39…). Se avvii lo script da solo, salva prima TELEGRAM_API_ID/API_HASH/PHONE nel file privato.',
        'Apri lo stesso account nell’app Telegram: il codice di login verrà richiesto nel terminale; l’eventuale password 2FA è quella impostata da te.',
        'La sessione concede accesso all’account: proteggila come una password. L’acquisizione si abilita solo con il consenso finale; non risponde ai tuoi contatti.')),
    'whatsapp': ('WhatsApp · collegamento tramite QR', (
        'Tieni il telefono connesso a Internet. Android: menu ⋮ → Dispositivi collegati → Collega un dispositivo; iPhone: Impostazioni → Dispositivi collegati.',
        'Sblocca il telefono se richiesto e inquadra il QR del terminale; non usare la normale fotocamera. Se scade, ripeti con il nuovo QR.',
        'Guida ufficiale: https://faq.whatsapp.com/ — cerca “Come collegare un dispositivo”. Non condividere QR o schermate: autorizzano accesso all’account.',
        'Attendi la conferma di connessione. Una sessione valida viene riutilizzata senza QR; puoi revocarla da Dispositivi collegati.',
        'Il ponte usa whatsapp-web.js, non un’API ufficiale WhatsApp. In Docker Node/Chromium sono già inclusi: non installarli sull’host per correggere il container.')),
    'gcal': ('Google Calendar · preparazione', (
        'Apri https://console.cloud.google.com/ e seleziona o crea un progetto; in API e servizi → Libreria abilita Google Calendar API.',
        'Metodo service account: IAM e amministrazione → Account di servizio → Crea (nessun ruolo di progetto necessario), poi Chiavi → Aggiungi chiave → JSON.',
        'Guida ufficiale: https://cloud.google.com/iam/docs/creating-managing-service-account-keys — rispetta eventuali policy che vietano chiavi.',
        'Copia privatamente il JSON nel percorso mostrato sotto (Docker: PI_SECRETARY_HOME/data/gcal_service_account.json sull’host); permessi 600, mai upload dalla dashboard.',
        'Su https://calendar.google.com → Impostazioni del calendario → Condividi con persone specifiche: aggiungi client_email del JSON con “Apportare modifiche agli eventi”.',
        'Da Integra calendario copia l’ID calendario: per un service account NON usare primary. Poi rilancia il setup; l’evento di prova richiede consenso separato.')),
    'gcal_oauth': ('Google Calendar · alternativa OAuth', (
        'Nel progetto Google Cloud configura Google Auth Platform: branding, pubblico e accesso dati Calendar; in Testing aggiungi il tuo account tra gli utenti di test.',
        'In Client crea un ID OAuth di tipo “TV e dispositivi con input limitato”; copia Client ID e Client secret nei prompt, mai in chat.',
        'Guida ufficiale: https://developers.google.com/identity/protocols/oauth2/limited-input-device',
        'Dopo il consenso il setup mostra un codice temporaneo: apri https://www.google.com/device, inserisci il codice e accedi all’account del calendario.',
        'Verifica nome app e permessi prima di consentire. Mantieni aperto il terminale; se neghi o scade il codice, ripeti il setup. In Testing il token può scadere dopo 7 giorni.')),
    'microsoft': ('Microsoft · accesso nel browser', (
        'Configura prima almeno una email con provider outlook o m365; il setup autorizza ogni account separatamente e riusa i token ancora validi (--force per rifare il login).',
        'Apri https://microsoft.com/devicelogin su un dispositivo fidato, inserisci il codice temporaneo mostrato e accedi con l’indirizzo indicato.',
        'Completa MFA e controlla nome app e permessi IMAP/SMTP prima del consenso. Password e MFA vanno SOLO sul sito Microsoft; lascia aperto il terminale.',
        'Se il tenant blocca il consenso/device flow, contatta l’amministratore: non aggirare le policy e non usare password normali.',
        'Client personalizzato: https://entra.microsoft.com/ → Registrazioni app → Nuova registrazione; scegli i tipi di account appropriati, abilita flussi client pubblici e permessi delegati Exchange Online IMAP.AccessAsUser.All e SMTP.Send.',
        'Salva l’ID applicazione in EMAILn_CLIENT_ID nel file privato, non un client secret. Guida: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code',
        'I token sono salvati privatamente e rinnovati quando possibile; accessi revocati o codice scaduto richiedono un nuovo setup.')),
    'dashboard': ('Dashboard · primo accesso dopo l’avvio', (
        'Questo wizard non avvia l’app: conferma la configurazione e scegli separatamente se verificare il DB e avviare.',
        'Dopo l’avvio apri http://127.0.0.1:8080 sul server; da remoto usa un tunnel SSH come descritto nel README, non pubblicare HTTP su Internet.',
        'Docker: apri privatamente PI_SECRETARY_HOME/config/dashboard.json sull’host con un editor locale e leggi username/password generati al primo avvio, poi inseriscili nella pagina login.',
        'Non stampare né condividere il file. Verifica gli interruttori dei connettori: configurazione completata non significa servizio pronto o login ancora valido.')),
}


def guide(name):
    section(*GUIDES[name])