"""Importazione una tantum nell'archivio nativo, senza analisi o invii."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone


def positive_days(value):
    days = int(value)
    if not 1 <= days <= 3650:
        raise argparse.ArgumentTypeError('I giorni devono essere compresi tra 1 e 3650')
    return days


def run_import(days, now=None):
    from secretary.core import config, db, flows
    from secretary.connectors import email, telegram_account, whatsapp

    positive_days(days)
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError('Timestamp privo di timezone')
    start = end.astimezone(timezone.utc) - timedelta(days=days)
    db.init()
    flows.init()
    enabled = flows.settings()
    print(f'Intervallo: {start.isoformat()} → {end.isoformat()} (estremo finale escluso)')
    print('Solo archivio: nessuna richiesta AI sincrona o invio. Media accodati per trascrizione AI al riavvio; possibili costi OpenRouter. I duplicati mantengono la stessa identità.')
    results = []

    def execute(label, callback, limited=False):
        try:
            count = callback()
            status = 'limitato' if limited else 'ok'
            print(f'{label}: {count} messaggi acquisiti/verificati ({status}).')
            results.append({'source': label, 'count': count, 'status': status})
        except Exception as exc:
            # Non stampare eccezioni che potrebbero contenere credenziali o messaggi.
            print(f'{label}: importazione incompleta ({type(exc).__name__}).')
            count = None
            if (label == 'WhatsApp' and type(exc).__name__ == 'WhatsAppHistoryPartialError'
                    and isinstance(getattr(exc, 'stored_count', None), int)):
                count = exc.stored_count
                print(f'WhatsApp: {count} messaggi già archiviati; controllare sessione/browser '
                      'e disponibilità dello storico. I dati acquisiti sono preservati.')
            elif label == 'Telegram personale':
                print('Telegram: le attese richieste dal server sono rispettate fino a 300 secondi '
                      'complessivi; se il limite è superato, riprovare più tardi. '
                      'Verificare anche autorizzazione e disponibilità della sessione.')
            elif label.startswith('Email '):
                print('Email: verificare i diagnostici folder_index/UID per messaggi malformati, '
                      'oltre ad accesso IMAP e cartella Inviate. I messaggi validi già letti restano salvati.')
            results.append({'source': label, 'count': count, 'status': 'failed'})

    def import_email(account):
        count = 0
        for message in email.iter_history_messages(account, start, end):
            db.store_message(
                f"email ({account['name']})", message['from'], message['subject'],
                message['body'], ts=message['ts'], source_id=message['source_id'],
                metadata=message['metadata'], attachments=message['attachments'],
                historical=True)
            count += 1
        return count

    if enabled.get('email'):
        for index, account in enumerate(config.EMAIL_ACCOUNTS, 1):
            execute(f'Email {index}', lambda account=account: import_email(account))
    if enabled.get('telegram_user') and config.TELEGRAM_USER_ENABLED:
        execute('Telegram personale', lambda: asyncio.run(telegram_account.import_history(start, end)))
    if enabled.get('whatsapp') and config.WHATSAPP_ENABLED:
        print('WhatsApp: recupero limitato allo storico sincronizzato e disponibile sul dispositivo collegato.')
        execute('WhatsApp', lambda: whatsapp.import_history(start, end), limited=True)
    if enabled.get('telegram_bot') and config.TELEGRAM_BOT_TOKEN:
        print('Telegram bot: storico passato non disponibile tramite Bot API; non importato.')
        results.append({'source': 'Telegram bot', 'count': 0, 'status': 'unsupported'})
    print('Google Calendar: escluso; questo script importa comunicazioni, non appuntamenti.')
    if not results:
        print('Nessuna connessione di messaggistica attiva da importare.')
    print('I conteggi includono messaggi già presenti; eventuali errori possono lasciare importazioni parziali. Rieseguire è sicuro.')
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=positive_days, help='Ultimi N giorni (1–3650); richiesti interattivamente se omessi')
    args = parser.parse_args(argv)
    try:
        days = args.days
        if days is None:
            days = positive_days(input('Quanti giorni vuoi importare? [14]: ').strip() or '14')
        results = run_import(days)
        return 1 if any(r['status'] == 'failed' for r in results) else 2 if any(
            r['status'] in ('limited', 'limitato', 'unsupported') for r in results) else 0
    except (KeyboardInterrupt, EOFError):
        print('Importazione annullata; dati già archiviati preservati.')
        return 130
    except Exception as exc:
        print(f'Importazione fallita ({type(exc).__name__}); verificare configurazione e database.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())