#!/usr/bin/env python3
"""User-run interactive authorization; never imported/called by the service."""
import asyncio
import getpass
import os
import sys

import config
from connectors.telegram_account import api_credentials, secure_session, session_path
from connectors.storage import FileLock


def login_code():
    print('\nDove trovare il codice di accesso:')
    print('  Apri Telegram sul telefono o su un altro dispositivo già collegato.')
    print('  Controlla la chat di servizio ufficiale "Telegram", anche tra le chat archiviate.')
    print('  Verifica che l’account aperto sia quello del numero inserito nel wizard.')
    print('  Non usare token del bot, API_ID, API_HASH o codici di my.telegram.org.')
    print('  La consegna via SMS non è garantita: dipende da Telegram.')
    print('Se non arriva nulla, non tentare codici a caso: digita q per uscire.')
    print('Prima di riprovare controlla il numero con prefisso internazionale;')
    print('evita richieste ripetute ravvicinate, che possono causare limiti temporanei.')
    print('Il codice non viene mostrato mentre lo digiti. Non condividerlo in chat.')
    code = getpass.getpass('Codice di accesso Telegram (q = esci): ').strip()
    if code.lower() == 'q':
        raise KeyboardInterrupt()
    return code


async def authorize():
    from telethon import TelegramClient
    api_id, api_hash = api_credentials()
    path = session_path()
    secure_session(path)
    client = TelegramClient(str(path), api_id, api_hash)
    try:
        print('\nAccesso all’account Telegram PERSONALE (non al bot).')
        print('Controlla il numero configurato nel wizard e apri il relativo account Telegram.')
        print('Se questa sessione è già autorizzata, non sarà richiesto alcun codice.')
        await client.start(
            phone=lambda: config.TELEGRAM_PHONE or getpass.getpass('Phone (+country code): '),
            code_callback=login_code,
            password=lambda: getpass.getpass('Password verifica in due passaggi Telegram (impostata da te, non un codice): '))
        me = await client.get_me()
        if me.bot:
            raise ValueError('Personal account required, not a bot')
        print('Session authorized. Enable ingestion only with explicit consent.')
    finally:
        await client.disconnect()
        secure_session(path)


def main():
    if not sys.stdin.isatty():
        print('Run this setup manually in an interactive terminal, never through systemd.')
        return 1
    # CLI-only umask also protects SQLite journal files on initial creation.
    os.umask(0o077)
    try:
        with FileLock(str(session_path()) + '.owner.lock', blocking=False):
            asyncio.run(authorize())
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Authorization failed ({type(exc).__name__}). Check credentials/dependencies; no secrets logged.')
        return 1
    if input('Abilitare acquisizione Telegram personale? [s/N] ').strip().lower() not in ('s', 'si', 'sì', 'y', 'yes'):
        return 2
    config.set_env('TELEGRAM_USER_ENABLED', 'true')
    return 0


if __name__ == '__main__':
    sys.exit(main())