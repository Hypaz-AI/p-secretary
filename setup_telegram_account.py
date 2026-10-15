#!/usr/bin/env python3
"""User-run interactive authorization; never imported/called by the service."""
import asyncio
import getpass
import os
import sys

import config
from connectors.telegram_account import api_credentials, secure_session, session_path
from connectors.storage import FileLock
from setup_ui import guide


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
            phone=lambda: config.TELEGRAM_PHONE or getpass.getpass('Telefono con prefisso internazionale: '),
            code_callback=login_code,
            password=lambda: getpass.getpass('Password verifica in due passaggi Telegram (impostata da te, non un codice): '))
        me = await client.get_me()
        if me.bot:
            raise ValueError('Serve un account personale, non un bot')
        print('Sessione autorizzata. L’acquisizione richiede il consenso esplicito finale.')
    finally:
        await client.disconnect()
        secure_session(path)


def main():
    if not sys.stdin.isatty():
        print('Esegui questo setup manualmente in un terminale interattivo, mai tramite un servizio.')
        return 1
    guide('telegram_account')
    # CLI-only umask also protects SQLite journal files on initial creation.
    os.umask(0o077)
    try:
        with FileLock(str(session_path()) + '.owner.lock', blocking=False):
            asyncio.run(authorize())
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Autorizzazione fallita ({type(exc).__name__}). Verifica credenziali e dipendenze; nessun segreto registrato.')
        return 1
    if input('Abilitare acquisizione Telegram personale? [s/N] ').strip().lower() not in ('s', 'si', 'sì', 'y', 'yes'):
        return 2
    config.set_env('TELEGRAM_USER_ENABLED', 'true')
    return 0


if __name__ == '__main__':
    sys.exit(main())