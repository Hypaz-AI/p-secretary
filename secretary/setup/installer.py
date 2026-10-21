"""Stdlib orchestration; secrets never appear in command arguments or progress."""
import argparse
import fcntl
import getpass
import json
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
import secrets
import signal
import subprocess
import sys
from urllib.parse import quote, urlsplit
from secretary.setup.ui import guide, section

ROOT = Path(__file__).resolve().parents[2]


def private_write(path, text):
    from secretary.connectors.storage import atomic_write
    atomic_write(path, text)


def read_env(path):
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and not key.startswith('#'):
                try:
                    result[key.strip()] = json.loads(value)
                except ValueError:
                    result[key.strip()] = value.strip().strip("'")
    return result


def confirm(prompt):
    return input(prompt + ' [s/N] ').strip().lower() in ('s', 'si', 'sì', 'y', 'yes')


def progress(path):
    try:
        state = json.loads(path.read_text())
        for key in ('telegram', 'telegram_account', 'whatsapp', 'gcal', 'microsoft'):
            if type(state.get(key)) is bool:
                state[key] = 'ready' if state[key] else 'failed'
        return state
    except FileNotFoundError:
        return {}


def provision_database(config_dir, existing, external=None):
    """Never rotate an existing local password or overwrite an existing URI."""
    pg = config_dir / 'postgres.env'
    if existing:
        return existing, 'local' if urlsplit(existing).hostname == 'db' else 'external'
    if external:
        parsed = urlsplit(external)
        if parsed.scheme not in ('postgres', 'postgresql') or not parsed.hostname:
            raise ValueError('URI PostgreSQL non valido')
        return external, 'external'
    values = read_env(pg)
    password = values.get('POSTGRES_PASSWORD') or secrets.token_urlsafe(36)
    if pg.exists() and not values.get('POSTGRES_PASSWORD'):
        raise ValueError('postgres.env esistente senza password: correggere manualmente')
    if not pg.exists():
        private_write(pg, 'POSTGRES_USER=secretary\nPOSTGRES_DB=secretary\nPOSTGRES_PASSWORD=' + password + '\n')
    return 'postgresql://secretary:' + quote(password, safe='') + '@db:5432/secretary', 'local'


def valid_preference(key, value):
    if key == 'TIMEZONE':
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            return False
    if key == 'DIGEST_TIME':
        return bool(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value))
    if key == 'LANGUAGE':
        return bool(re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z]{2,8})?', value))
    if key == 'AGENT_WEB_MODEL' and value == '':
        return True  # vuoto = usa OPENROUTER_MODEL
    if key in ('OPENROUTER_MODEL', 'AGENT_MODEL', 'AGENT_WEB_MODEL'):
        return bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]*', value))
    return True


def prompt_missing(config, key, label, secret=False, choices=None, edit=False):
    """Save answers immediately, without replacing existing settings."""
    saved = read_env(config.CONFIG_FILE).get(key, '')
    if saved and not edit and valid_preference(key, saved):
        return saved
    defaults = {'TIMEZONE': 'Europe/Rome', 'LANGUAGE': 'it',
                'OPENROUTER_MODEL': 'google/gemini-2.5-flash', 'DIGEST_TIME': '21:00'}
    default = saved or defaults.get(key, '')
    while True:
        hint = 'mantieni salvato' if secret and saved else default if not secret and default else 'salta'
        value = (getpass.getpass if secret else input)(label + f' (INVIO = {hint}): ').strip() or default
        if not value:
            return ''
        if (choices and value not in choices) or not valid_preference(key, value):
            print('Valore non valido' + (': ' + ', '.join(choices) if choices else ''))
            continue
        config.set_env(key, value)
        return value


def configure_email(config, edit=False):
    guide('email')
    print('Email: fino a 50 account; password/app password con input nascosto.')
    add_new = True
    for i in range(1, 51):
        prefix = f'EMAIL{i}_'
        existing = any(key.startswith(prefix) for key in read_env(config.CONFIG_FILE))
        if existing and edit and not confirm(f'Modificare account email {i} (nessun valore segreto mostrato)?'):
            continue
        if not existing:
            if not add_new or not confirm(f'Aggiungere account email {i}?'):
                add_new = False
                continue
        provider = prompt_missing(config, prefix + 'PROVIDER', 'Provider: gmail/outlook/m365/zimbra_ovh/custom',
                                  choices=('gmail', 'outlook', 'm365', 'zimbra_ovh', 'custom'), edit=edit)
        if not existing or edit:
            guide('outlook' if provider == 'm365' else provider or 'custom')
        prompt_missing(config, prefix + 'NAME', f'Nome account {i}', edit=edit)
        prompt_missing(config, prefix + 'ADDRESS', f'Indirizzo email {i}', edit=edit)
        if provider not in ('outlook', 'm365'):
            prompt_missing(config, prefix + 'PASSWORD', 'Password o password per app', secret=True, edit=edit)
        if provider in ('', 'custom'):
            for suffix, label in [('IMAP_HOST', 'Host IMAP'), ('SMTP_HOST', 'Host SMTP')]:
                prompt_missing(config, prefix + suffix, label, edit=edit)


def configure_preferences(config, edit=False):
    guide('preferences')
    for key, label in [('TIMEZONE', 'Timezone (es. Europe/Rome)'), ('LANGUAGE', 'Lingua (es. it)'),
                       ('OPENROUTER_MODEL', 'Modello OpenRouter (input nativi testo e immagini obbligatori)'), ('DIGEST_TIME', 'Ora digest (HH:MM)')]:
        prompt_missing(config, key, label, edit=edit)
    print('Documenti, ricerca, memoria e attività non richiedono una configurazione contabile.')
    print('Fatture/ZIP sono facoltativi: dopo l’avvio scegli i flussi nella dashboard; nessun consenso AI viene dato qui.')
    add_new = True
    for i in range(1, 5):
        prefix = f'INVOICE_ENTITY{i}_'
        existing = any(key.startswith(prefix) for key in read_env(config.CONFIG_FILE))
        if not existing:
            if not add_new or not confirm(f'Opzionale · configurare entità fatture {i} (non abilita il flusso)?'):
                add_new = False
                continue
        for suffix, label in [('NAME', 'Nome entità/cartella'), ('VAT', 'Partita IVA'), ('DESC', 'Descrizione entità')]:
            prompt_missing(config, prefix + suffix, label, edit=edit)


def run_setup(name):
    """Esegue `secretary.setup.<name>` in un processo figlio (ms_auth, telegram, ...)."""
    from secretary.core import config
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(ROOT), env.get('PYTHONPATH')]))
    for key in read_env(config.CONFIG_FILE):
        if key not in ('P_SECRETARY_CONFIG_FILE', 'DATA_DIR', 'INVOICE_DIR',
                       'DASHBOARD_BIND', 'DASHBOARD_PORT', 'DASHBOARD_CREDENTIAL_FILE',
                       'TELEGRAM_USER_SESSION'):
            env.pop(key, None)
    # Keep the foreground process group: terminal SIGINT reaches both processes.
    with subprocess.Popen([sys.executable, '-m', 'secretary.setup.' + name], env=env) as child:
        try:
            code = child.wait()
        except KeyboardInterrupt:
            # Also handle SIGINT delivered only to the parent. Reap before returning
            # control to the shell; never resume prompts after cancellation.
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            finally:
                signal.signal(signal.SIGINT, previous)
            raise
    if code in (130, -signal.SIGINT):
        raise KeyboardInterrupt
    return subprocess.CompletedProcess(child.args, code)


def wizard(edit=False):
    try:
        return _wizard(edit)
    except (KeyboardInterrupt, EOFError):
        print('Wizard annullato; impostazioni già salvate preservate. Nessuna prosecuzione o avvio automatico.')
        return 130


def _wizard(edit=False):
    # Import only inside the container: each setup gets a fresh config module.
    from secretary.core import config
    guide('welcome')
    directory = config.CONFIG_FILE.parent
    state_file = directory / 'progress.json'
    state = progress(state_file)
    sections = {'identity', 'preferences', 'email', 'database', 'setups'}
    if edit:
        print('Configure: 1 identità/OpenRouter, 2 preferenze e funzionalità opzionali, 3 email, 4 database, 5 setup opzionali, INVIO tutto')
        while True:
            choice = input('Sezione: ').strip()
            if choice in ('', '1', '2', '3', '4', '5'):
                break
            print('Scegliere 1-5 oppure INVIO.')
        if choice:
            sections = [{ '1': 'identity', '2': 'preferences', '3': 'email', '4': 'database', '5': 'setups'}[choice]]
    state['complete'] = False
    private_write(state_file, json.dumps(state))
    if not config.DATABASE_URL or 'database' in sections:
        guide('database')
    if not config.DATABASE_URL:
        external = getpass.getpass('URI PostgreSQL esterno (INVIO = DB locale): ').strip()
    else:
        external = None
    uri, mode = provision_database(directory, config.DATABASE_URL, external)
    if not config.DATABASE_URL:
        config.set_env('DATABASE_URL', uri)
    state['database'] = mode
    private_write(state_file, json.dumps(state))
    if 'identity' in sections:
        guide('identity')
    for key, label, secret in [('AGENT_NAME', 'Nome agente', False),
                               ('OPENROUTER_API_KEY', 'Chiave OpenRouter', True)]:
        if 'identity' in sections:
            prompt_missing(config, key, label, secret, edit=edit)
    if edit and 'database' in sections:
        print('Modifica sicura: INVIO preserva i valori salvati; nessuna migrazione DB.')
        if confirm('Modificare destinazione database (i dati NON vengono migrati)?'):
            candidate = getpass.getpass('URI PostgreSQL (INVIO = mantieni): ').strip()
            if candidate:
                uri, mode = provision_database(directory, '', candidate)
                config.set_env('DATABASE_URL', uri)
                state['database'] = mode
    if 'preferences' in sections:
        configure_preferences(config, edit=edit)
    if 'email' in sections:
        configure_email(config, edit=edit)
        values = read_env(config.CONFIG_FILE)
        if any(values.get(f'EMAIL{i}_PROVIDER') in ('outlook', 'm365') for i in range(1, 51)):
            print('Email Microsoft: autorizzazione degli account configurati; riuso token validi.')
            result = run_setup('ms_auth')
            state['microsoft'] = {0: 'ready', 2: 'skipped'}.get(result.returncode, 'failed')
            private_write(state_file, json.dumps(state))
    setups = [('telegram', 'telegram'), ('telegram_account', 'telegram_account'),
              ('whatsapp', 'whatsapp'), ('gcal', 'gcal')]
    print('Gli script opzionali possono effettuare login o chiamate di rete; seguire le loro richieste di consenso.')
    print('Calendar: solo service account IAM, JSON con incolla nascosto; nessun login utente.')
    for name, script in setups:
        if 'setups' in sections and confirm('Eseguire setup ' + name + (f' ({state[name]})' if state.get(name) else '') + '?'):
            if name == 'telegram_account':
                for key, label, secret in [('TELEGRAM_API_ID', 'Telegram personale API_ID', False),
                                           ('TELEGRAM_API_HASH', 'Telegram personale API_HASH', True),
                                           ('TELEGRAM_PHONE', 'Telefono con prefisso internazionale', False)]:
                    prompt_missing(config, key, label, secret)
                if not all(config.get(key) for key in ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_PHONE')):
                    print('Setup Telegram personale rinviato: completare API_ID, API_HASH e telefono.')
                    state[name] = 'skipped'
                    private_write(state_file, json.dumps(state))
                    continue
            # Remove loaded config keys so setdefault sees newly written values.
            result = run_setup(script)
            state[name] = {0: 'ready', 2: 'skipped'}.get(result.returncode, 'failed')
            private_write(state_file, json.dumps(state))
        else:
            state.setdefault(name, 'skipped')
    guide('dashboard')
    section('Riepilogo · configurazione, non readiness runtime')
    for name, _ in setups:
        print(f'  {name}: {state[name]}')
    if 'microsoft' in state:
        print(f"  microsoft: {state['microsoft']}")
    if not confirm('Confermare la configurazione (nessun avvio automatico)?'):
        return 1
    state['complete'] = True
    private_write(state_file, json.dumps(state))
    return 0


def probe():
    """Only SELECT 1, no db module import/migrations; redact connection errors."""
    from secretary.core import config
    import psycopg
    try:
        with psycopg.connect(config.DATABASE_URL, connect_timeout=8) as conn:
            with conn.cursor() as cursor:
                cursor.execute('SELECT 1')
                assert cursor.fetchone() == (1,)
        print('Database: connessione OK (nessuna migrazione).')
        return 0
    except Exception:
        print('Database: verifica fallita; controllare credenziali/rete senza pubblicare i segreti.')
        return 1


def launch(args):
    home = Path(os.environ.get('P_SECRETARY_HOME', '~/.local/share/p-secretary')).expanduser().resolve()
    uid, gid = os.getuid(), os.getgid()
    for directory in (home, home / 'config', home / 'data'):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.stat().st_uid != uid or not os.access(directory, os.W_OK | os.X_OK):
            raise ValueError('Directory non appartenente al chiamante o non scrivibile')
        directory.chmod(0o700)
    os.environ['P_SECRETARY_HOME'] = str(home)
    os.environ['P_SECRETARY_UID'] = str(uid)
    os.environ['P_SECRETARY_GID'] = str(gid)
    with (home / 'launcher.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run(['docker', 'compose', 'version'], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state_file = home / 'config/progress.json'
        state = progress(state_file)
        base = ['docker', 'compose', '--project-directory', str(ROOT), '-f', str(ROOT / 'compose.yaml')]
        def compose(*items, check=True):
            current = progress(state_file)
            uri = read_env(home / 'config/secretary.env').get('DATABASE_URL', '')
            if uri:
                _, current['database'] = provision_database(home / 'config', uri)
                private_write(state_file, json.dumps(current))
            selected = base + (['-f', str(ROOT / 'compose.external.yaml')] if current.get('database') == 'external' else [])
            return subprocess.run(selected + list(items), check=check)
        def database():
            uri = read_env(home / 'config/secretary.env').get('DATABASE_URL', '')
            mode = provision_database(home / 'config', uri)[1] if uri else progress(state_file).get('database')
            if mode == 'local':
                compose('--profile', 'local', 'up', '-d', '--wait', '--wait-timeout', '120', 'db')
        def start():
            if not read_env(home / 'config/secretary.env').get('OPENROUTER_API_KEY', '').strip():
                raise ValueError('OPENROUTER_API_KEY obbligatoria: usare configure e inserire la chiave OpenRouter prima di up.')
            # Inspect/repair only after the previous app has actually stopped,
            # before --force-recreate removes its inspectable identity.
            compose('stop', 'app')
            repair_whatsapp()
            compose('up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '120', 'app')
        def repair_whatsapp():
            from secretary.connectors.whatsapp_profile import repair_docker_singletons
            if repair_docker_singletons(home / 'data/wa_session'):
                print('WhatsApp: singleton Chromium obsoleti rimossi; profilo e sessione preservati.')
        def doctor():
            return compose('run', '--rm', '--no-deps', '-T', 'app', 'python', '-m', 'secretary.setup.installer', '_probe', check=False).returncode
        if args.command in ('setup', 'configure'):
            if (args.command == 'configure' or state.get('complete')) and not confirm('Fermare app e riconfigurare?'):
                return 1
            # Missing env_file must not block the first Compose invocation.
            pg = home / 'config/postgres.env'
            if not pg.exists():
                provision_database(home / 'config', '')
            compose('stop', 'app')
            repair_whatsapp()
            compose('build', 'app')
            compose('run', '--rm', '--no-deps', 'app', 'python', '-m', 'secretary.setup.installer',
                    '_configure' if args.command == 'configure' else '_wizard')
            database()
            if doctor():
                return 1
            if confirm('Avviare ora app?'):
                start()
        elif args.command == 'import-history':
            if not state.get('complete'):
                raise ValueError('Completare setup prima di importare lo storico')
            print('Importazione una tantum: app fermata per liberare le sessioni. Riavviarla con up al termine.')
            compose('stop', 'app')
            repair_whatsapp()
            compose('build', 'app')
            database()
            command = ['run', '--rm', '--no-deps', 'app', 'python', '-m', 'secretary.archive.historical_import']
            if args.days is not None:
                command += ['--days', str(args.days)]
            return compose(*command, check=False).returncode
        elif args.command == 'up':
            if not state.get('complete'):
                raise ValueError('Eseguire setup e confermare prima di avviare app')
            if not read_env(home / 'config/secretary.env').get('OPENROUTER_API_KEY', '').strip():
                raise ValueError('OPENROUTER_API_KEY obbligatoria: usare configure prima di up.')
            # Ricostruisce sempre l'immagine: il codice dell'app e' copiato dentro di essa.
            compose('build', 'app')
            database()
            if doctor():
                return 1
            start()
        elif args.command == 'doctor':
            return doctor()
        elif args.command == 'status':
            compose('--profile', 'local', 'ps')
        elif args.command == 'down':
            compose('--profile', 'local', 'down')
        elif args.command == 'logs':
            print('Attenzione: i log applicativi possono contenere dati personali. Non condividerli senza redazione.')
            compose('logs', '--tail', '100', 'app')
    return 0


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'configure', 'import-history', 'status', 'up', 'down', 'logs', 'doctor', '_wizard', '_configure', '_probe'])
    parser.add_argument('--days', type=int, help='Giorni da importare con import-history (1–3650)')
    args = parser.parse_args()
    if args.days is not None and (args.command != 'import-history' or not 1 <= args.days <= 3650):
        parser.error('--days richiede import-history e un valore tra 1 e 3650')
    try:
        return wizard(edit=args.command == '_configure') if args.command in ('_wizard', '_configure') else probe() if args.command == '_probe' else launch(args)
    except (KeyboardInterrupt, EOFError):
        print('Operazione annullata; nessuna prosecuzione o avvio automatico.', file=sys.stderr)
        return 130
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        if exc.returncode in (130, -signal.SIGINT):
            print('Wizard annullato; nessuna prosecuzione o avvio automatico.', file=sys.stderr)
            return 130
        print('Operazione Docker fallita; nessuna prosecuzione automatica.', file=sys.stderr)
        return 1
    except OSError:
        print('Operazione fallita: verificare Docker Compose, permessi, lock e configurazione. Nessun sudo automatico.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())