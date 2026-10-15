#!/usr/bin/env python3
"""User-run interactive authorization; never imported/called by the service."""
import asyncio
import getpass
import os
import sys

import config
from connectors.telegram_account import api_credentials, secure_session, session_path
from connectors.storage import FileLock


async def authorize():
    from telethon import TelegramClient
    api_id, api_hash = api_credentials()
    path = session_path()
    secure_session(path)
    client = TelegramClient(str(path), api_id, api_hash)
    try:
        await client.start(
            phone=lambda: config.TELEGRAM_PHONE or getpass.getpass('Phone (+country code): '),
            code_callback=lambda: getpass.getpass('Telegram login code: '),
            password=lambda: getpass.getpass('Telegram two-step verification password: '))
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