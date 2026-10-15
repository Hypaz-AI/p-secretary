#!/usr/bin/env python3
"""User-run interactive authorization; never imported/called by the service."""
import asyncio
import getpass
import os
import sys
import re
import time

import config
from connectors.telegram_account import api_credentials, secure_session, session_path
from connectors.storage import FileLock
from setup_ui import guide


class LoginCancelled(Exception):
    """Expected interactive cancellation, not a credential failure."""


def delivery_unavailable():
    print('Telegram rifiuta ulteriori metodi di consegna per questa richiesta.')
    print('Non è possibile forzare SMS o garantire l’arrivo del codice.')
    print('Nessun altro reinvio automatico: attendi prima di riprovare; verifica numero e prefisso.')
    print('Usa un numero verificato di cui controlli l’account, oppure QR con Telegram già collegato.')


async def password_login(client):
    from telethon import errors
    for _ in range(3):
        try:
            return await client.sign_in(password=getpass.getpass(
                'Password verifica in due passaggi (Ctrl-C annulla): '))
        except errors.PasswordHashInvalidError:
            print('Password 2FA non valida; riprovare o Ctrl-C.')
    raise LoginCancelled('Tentativi password esauriti; riprovare più tardi.')


async def qr_login(client):
    """Display bearer credentials only as an explicitly requested terminal QR."""
    from telethon import errors
    try:
        import qrcode
    except ImportError:
        print('QR non disponibile: installare le dipendenze aggiornate / ricostruire l’immagine Docker.')
        return None
    print('Il QR autorizza questa sessione: non condividerlo e non registrare il terminale.')
    print('Sul telefono già collegato: Telegram → Impostazioni → Dispositivi → Collega dispositivo desktop.')
    print('Scansiona con l’account personale configurato; il QR non aggira la password 2FA.')
    if input('Mostrare il QR segreto in questo terminale? [s/N] ').strip().lower() not in ('s', 'si', 'sì', 'y', 'yes'):
        return None
    qr = await client.qr_login()
    for attempt in range(3):
        # Telethon requires wait() to register its update handler before scanning.
        waiter = asyncio.create_task(qr.wait())
        try:
            await asyncio.sleep(0)
            image = qrcode.QRCode(border=2)
            image.add_data(qr.url)
            image.print_ascii(invert=True)
            return await waiter
        except errors.SessionPasswordNeededError:
            return await password_login(client)
        except asyncio.TimeoutError:
            print('QR scaduto; nessun codice telefonico richiesto.')
        finally:
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if attempt == 2 or input('Creare un nuovo QR? [s/N] ').strip().lower() not in ('s', 'si', 'sì', 'y', 'yes'):
            return None
        await qr.recreate()
    return None


def login_code():
    print('\nDove trovare il codice di accesso:')
    print('  Apri Telegram sul telefono o su un altro dispositivo già collegato.')
    print('  Controlla la chat di servizio ufficiale "Telegram", anche tra le chat archiviate.')
    print('  Verifica che l’account aperto sia quello del numero inserito nel wizard.')
    print('  Non usare token del bot, API_ID, API_HASH o codici di my.telegram.org.')
    print('  La consegna via SMS non è garantita: dipende da Telegram.')
    print('Se non arriva nulla: r tenta il reinvio consentito, qr propone il QR, q esce.')
    print('Prima di riprovare controlla il numero con prefisso internazionale;')
    print('evita richieste ripetute ravvicinate, che possono causare limiti temporanei.')
    print('Il codice non viene mostrato mentre lo digiti. Non condividerlo in chat.')
    code = getpass.getpass('Codice Telegram (r = reinvio, qr = QR, q = esci): ').strip()
    if code.lower() == 'q':
        raise KeyboardInterrupt()
    return code


async def authorize():
    from telethon import TelegramClient
    from telethon import errors
    api_id, api_hash = api_credentials()
    path = session_path()
    secure_session(path)
    client = TelegramClient(str(path), api_id, api_hash, flood_sleep_threshold=0)
    try:
        print('\nAccesso all’account Telegram PERSONALE (non al bot).')
        print('Controlla il numero configurato nel wizard e apri il relativo account Telegram.')
        print('Se questa sessione è già autorizzata, non sarà richiesto alcun codice.')
        await client.connect()
        me = await client.get_me()
        if me is None:
            phone = config.TELEGRAM_PHONE
            while True:
                phone = input(f'Telefono [{phone or "non configurato"}] (INVIO conferma, q annulla): ').strip() or phone
                if phone.lower() == 'q':
                    raise KeyboardInterrupt()
                if re.fullmatch(r'\+[1-9]\d{6,14}', phone):
                    break
                print('Inserire + e numero completo, senza spazi.')
            config.set_env('TELEGRAM_PHONE', phone)
            sent = None
            resend_allowed = False
            method = input('Accesso: INVIO = codice, qr = QR senza codice, q = annulla: ').strip().lower()
            if method == 'q':
                raise LoginCancelled('Accesso annullato.')
            if method == 'qr':
                me = await qr_login(client)
                if me is None:
                    raise LoginCancelled('Accesso QR non completato.')
            else:
                try:
                    sent = await client.send_code_request(phone)
                    deadline = delivery_feedback(sent)
                    resend_allowed = getattr(sent, 'next_type', None) is not None
                except errors.SendCodeUnavailableError:
                    delivery_unavailable()
            for attempt in range(10):
                if me is not None:
                    break
                code = login_code()
                if code.lower() == 'qr':
                    me = await qr_login(client)
                    continue
                if code.lower() == 'r':
                    if not resend_allowed:
                        print('Reinvio non disponibile. Usa il codice già ricevuto, qr oppure q; attendi prima di un nuovo setup.')
                    elif time.monotonic() < deadline:
                        print('Attendere il timeout indicato prima di richiedere un altro codice.')
                    else:
                        # Disable until Telegram explicitly offers another method.
                        resend_allowed = False
                        try:
                            sent = await client.send_code_request(phone)
                            deadline = delivery_feedback(sent)
                            resend_allowed = getattr(sent, 'next_type', None) is not None
                        except errors.SendCodeUnavailableError:
                            delivery_unavailable()
                    continue
                if not code or sent is None:
                    continue
                try:
                    me = await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                    break
                except errors.SessionPasswordNeededError:
                    me = await password_login(client)
                    break
                except errors.PhoneCodeExpiredError:
                    print('Codice scaduto. Digita r per richiederne uno nuovo, oppure q per annullare.')
                except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                    print('Codice non valido; riprova oppure q per annullare.')
            if me is None:
                raise LoginCancelled('Tentativi esauriti; attendi prima di riprovare.')
        else:
            print('Sessione autorizzata esistente riutilizzata; nessun codice richiesto.')
            expected = re.sub(r'\D', '', config.TELEGRAM_PHONE or '')
            if expected and expected != getattr(me, 'phone', None):
                raise ValueError('Sessione di un altro account: usare un percorso sessione separato')
        expected = re.sub(r'\D', '', config.TELEGRAM_PHONE or '')
        if expected and expected != getattr(me, 'phone', None):
            raise ValueError('Sessione di un altro account: revocarla da Dispositivi e usare un percorso sessione separato')
        if me.bot:
            raise ValueError('Serve un account personale, non un bot')
        print('Sessione autorizzata. L’acquisizione richiede il consenso esplicito finale.')
    finally:
        await client.disconnect()
        secure_session(path)


def delivery_feedback(sent):
    """Only report protocol metadata, never hashes, addresses or code values."""
    labels = {'SentCodeTypeApp': 'app Telegram (chat di servizio Telegram)',
              'SentCodeTypeSms': 'SMS', 'SentCodeTypeCall': 'chiamata',
              'SentCodeTypeFlashCall': 'chiamata flash', 'SentCodeTypeMissedCall': 'chiamata persa',
              'SentCodeTypeEmailCode': 'email', 'SentCodeTypeSetUpEmailRequired': 'configurazione email richiesta'}
    print('Metodo selezionato da Telegram (non conferma di ricezione): ' + labels.get(type(sent.type).__name__, 'altro metodo Telegram'))
    next_type = getattr(sent, 'next_type', None)
    if next_type is not None:
        next_labels = {'CodeTypeSms': 'SMS', 'CodeTypeCall': 'chiamata',
                       'CodeTypeFlashCall': 'chiamata flash', 'CodeTypeMissedCall': 'chiamata persa'}
        print('Prossimo metodo indicato: ' + next_labels.get(type(next_type).__name__, 'altro metodo Telegram'))
    else:
        print('Telegram non indica un metodo successivo: reinvio disabilitato; qr oppure q.')
    timeout = max(0, getattr(sent, 'timeout', None) or 0)
    if next_type is not None:
        print(f'Reinvio: r (non prima di {timeout} secondi, disponibilità non garantita); q annulla. SMS non forzabile.')
    return time.monotonic() + timeout


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
    except (LoginCancelled, KeyboardInterrupt) as exc:
        print(str(exc) if isinstance(exc, LoginCancelled) else 'Accesso annullato; nessun reinvio automatico.')
        return 2
    except (Exception, KeyboardInterrupt) as exc:
        if type(exc).__name__ in ('FloodWaitError', 'PhoneNumberFloodError', 'PhonePasswordFloodError'):
            print('Limite Telegram: interrompere i tentativi e attendere prima di riprovare.')
        print(f'Autorizzazione fallita ({type(exc).__name__}). Verifica credenziali e dipendenze; nessun segreto registrato.')
        return 1
    if input('Abilitare acquisizione Telegram personale? [s/N] ').strip().lower() not in ('s', 'si', 'sì', 'y', 'yes'):
        return 2
    config.set_env('TELEGRAM_USER_ENABLED', 'true')
    return 0


if __name__ == '__main__':
    sys.exit(main())