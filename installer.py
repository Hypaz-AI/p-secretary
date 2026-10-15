#!/usr/bin/env python3
"""Stdlib orchestration; secrets never appear in command arguments or progress."""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parent


def private_write(path, text):
    from connectors.storage import atomic_write
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
        return json.loads(path.read_text())
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


def prompt_missing(config, key, label, secret=False, choices=None):
    """Save answers immediately, without replacing existing settings."""
    if key in read_env(config.CONFIG_FILE) or config.get(key):
        return config.get(key)
    while True:
        value = (getpass.getpass if secret else input)(label + ' (INVIO = salta): ').strip()
        if not value:
            return ''
        if choices and value not in choices:
            print('Valori ammessi: ' + ', '.join(choices))
            continue
        config.set_env(key, value)
        return value


def configure_email(config):
    print('Email: fino a 50 account; password/app password con input nascosto.')
    add_new = True
    for i in range(1, 51):
        prefix = f'EMAIL{i}_'
        existing = any(key.startswith(prefix) for key in read_env(config.CONFIG_FILE))
        if not existing:
            if not add_new or not confirm(f'Aggiungere account email {i}?'):
                add_new = False
                continue
        provider = prompt_missing(config, prefix + 'PROVIDER', 'Provider: gmail/outlook/m365/zimbra_ovh/custom',
                                  choices=('gmail', 'outlook', 'm365', 'zimbra_ovh', 'custom'))
        prompt_missing(config, prefix + 'NAME', f'Nome account {i}')
        prompt_missing(config, prefix + 'ADDRESS', f'Indirizzo email {i}')
        if provider not in ('outlook', 'm365'):
            prompt_missing(config, prefix + 'PASSWORD', 'Password o password per app', secret=True)
        if provider in ('', 'custom'):
            for suffix, label in [('IMAP_HOST', 'Host IMAP'), ('SMTP_HOST', 'Host SMTP')]:
                prompt_missing(config, prefix + suffix, label)


def configure_preferences(config):
    for key, label in [('TIMEZONE', 'Timezone (es. Europe/Rome)'), ('LANGUAGE', 'Lingua (es. it)'),
                       ('OPENROUTER_MODEL', 'Modello OpenRouter'), ('DIGEST_TIME', 'Ora digest (HH:MM)')]:
        prompt_missing(config, key, label)
    add_new = True
    for i in range(1, 5):
        prefix = f'INVOICE_ENTITY{i}_'
        existing = any(key.startswith(prefix) for key in read_env(config.CONFIG_FILE))
        if not existing:
            if not add_new or not confirm(f'Aggiungere entità fatture {i}?'):
                add_new = False
                continue
        for suffix, label in [('NAME', 'Nome entità/cartella'), ('VAT', 'Partita IVA'), ('DESC', 'Descrizione entità')]:
            prompt_missing(config, prefix + suffix, label)


def wizard():
    # Import only inside the container: each setup gets a fresh config module.
    import config
    directory = config.CONFIG_FILE.parent
    state_file = directory / 'progress.json'
    state = progress(state_file)
    state['complete'] = False
    private_write(state_file, json.dumps(state))
    if not config.DATABASE_URL:
        external = getpass.getpass('URI PostgreSQL esterno (INVIO = DB locale): ').strip()
    else:
        external = None
    uri, mode = provision_database(directory, config.DATABASE_URL, external)
    if not config.DATABASE_URL:
        config.set_env('DATABASE_URL', uri)
    state['database'] = mode
    private_write(state_file, json.dumps(state))
    for key, label, secret in [('AGENT_NAME', 'Nome agente', False),
                               ('OPENROUTER_API_KEY', 'Chiave OpenRouter', True)]:
        prompt_missing(config, key, label, secret)
    configure_preferences(config)
    configure_email(config)
    setups = [('telegram', 'setup_telegram.py'), ('telegram_account', 'setup_telegram_account.py'),
              ('whatsapp', 'setup_whatsapp.py'), ('gcal', 'setup_gcal.py'),
              ('microsoft', 'setup_ms_auth.py')]
    print('Gli script opzionali possono effettuare login o chiamate di rete; seguire le loro richieste di consenso.')
    for name, script in setups:
        if confirm('Eseguire setup ' + name + (' (già eseguito)' if state.get(name) else '') + '?'):
            if name == 'telegram_account':
                for key, label, secret in [('TELEGRAM_API_ID', 'Telegram personale API_ID', False),
                                           ('TELEGRAM_API_HASH', 'Telegram personale API_HASH', True),
                                           ('TELEGRAM_PHONE', 'Telefono con prefisso internazionale', False)]:
                    prompt_missing(config, key, label, secret)
                if not all(config.get(key) for key in ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_PHONE')):
                    print('Setup Telegram personale rinviato: completare API_ID, API_HASH e telefono.')
                    continue
            # Remove loaded config keys so setdefault sees newly written values.
            env = os.environ.copy()
            for key in read_env(config.CONFIG_FILE):
                if key not in ('PI_SECRETARY_CONFIG_FILE', 'DATA_DIR', 'INVOICE_DIR',
                               'DASHBOARD_BIND', 'DASHBOARD_PORT', 'DASHBOARD_CREDENTIAL_FILE',
                               'TELEGRAM_USER_SESSION'):
                    env.pop(key, None)
            result = subprocess.run([sys.executable, str(ROOT / script)], env=env)
            state[name] = result.returncode == 0
            private_write(state_file, json.dumps(state))
    if not confirm('Confermare la configurazione (nessun avvio automatico)?'):
        return 1
    state['complete'] = True
    private_write(state_file, json.dumps(state))
    return 0


def probe():
    """Only SELECT 1, no db module import/migrations; redact connection errors."""
    import config
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
    home = Path(os.environ.get('PI_SECRETARY_HOME', '~/.local/share/pi-secretary')).expanduser().resolve()
    uid, gid = os.getuid(), os.getgid()
    for directory in (home, home / 'config', home / 'data'):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.stat().st_uid != uid or not os.access(directory, os.W_OK | os.X_OK):
            raise ValueError('Directory non appartenente al chiamante o non scrivibile')
        directory.chmod(0o700)
    os.environ['PI_SECRETARY_HOME'] = str(home)
    os.environ['PI_SECRETARY_UID'] = str(uid)
    os.environ['PI_SECRETARY_GID'] = str(gid)
    with (home / 'launcher.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        subprocess.run(['docker', 'compose', 'version'], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state_file = home / 'config/progress.json'
        state = progress(state_file)
        base = ['docker', 'compose', '--project-directory', str(ROOT), '-f', str(ROOT / 'compose.yaml')]
        if state.get('database') == 'external':
            base += ['-f', str(ROOT / 'compose.external.yaml')]
        def compose(*items, check=True):
            return subprocess.run(base + list(items), check=check)
        def database():
            if progress(state_file).get('database') == 'local':
                compose('--profile', 'local', 'up', '-d', '--wait', '--wait-timeout', '120', 'db')
        def doctor():
            return compose('run', '--rm', '--no-deps', '-T', 'app', 'python', 'installer.py', '_probe', check=False).returncode
        if args.command in ('setup', 'configure'):
            if (args.command == 'configure' or state.get('complete')) and not confirm('Fermare app e riconfigurare?'):
                return 1
            # Missing env_file must not block the first Compose invocation.
            pg = home / 'config/postgres.env'
            if not pg.exists():
                provision_database(home / 'config', '')
            compose('stop', 'app')
            compose('build', 'app')
            compose('run', '--rm', '--no-deps', 'app', 'python', 'installer.py', '_wizard')
            if not confirm('Avviare il DB locale se scelto e verificare la connessione con SELECT 1?'):
                return 0
            database()
            if doctor():
                return 1
            if confirm('Avviare ora app?'):
                compose('up', '-d', '--no-deps', 'app')
        elif args.command == 'up':
            if not state.get('complete'):
                raise ValueError('Eseguire setup e confermare prima di avviare app')
            database()
            if doctor():
                return 1
            compose('up', '-d', '--no-deps', 'app')
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
    parser.add_argument('command', choices=['setup', 'configure', 'status', 'up', 'down', 'logs', 'doctor', '_wizard', '_probe'])
    args = parser.parse_args()
    try:
        return wizard() if args.command == '_wizard' else probe() if args.command == '_probe' else launch(args)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print('Operazione fallita: verificare Docker Compose, permessi, lock e configurazione. Nessun sudo automatico.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())