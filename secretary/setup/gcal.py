"""Google Calendar esclusivamente tramite service account IAM, senza login utente."""
import sys
import getpass
import json
import warnings
from pathlib import Path
from datetime import datetime, timedelta

from secretary.core import config
from secretary.connectors import gcal
from secretary.connectors.storage import FileLock, atomic_write
from secretary.setup.ui import guide


def confirm(prompt):
    return input(prompt + ' [s/N] ').strip().lower() in ('s', 'si', 'sì', 'y', 'yes')


def hidden_json():
    print('Incolla JSON compatto o multilinea (input nascosto).')
    print('Un oggetto JSON completo termina automaticamente; FINE è opzionale per terminare manualmente.')
    print('INVIO iniziale salta Calendar; Ctrl-C annulla il wizard. Nessun contenuto viene mostrato.')
    lines = []
    size = 0
    # getpass altrimenti ripiega su stdin con echo: vietato per chiavi private.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        while True:
            line = getpass.getpass('JSON> ')
            if not lines and not line.strip():
                return ''
            if line.strip() == 'FINE':
                raw = '\n'.join(lines)
                if not raw.strip():
                    return ''
                return _json_object(raw, final=True)
            size += len(line) + 1
            if size > 131072:
                raise ValueError('JSON troppo grande')
            lines.append(line)
            raw = '\n'.join(lines)
            if _json_object(raw) is not None:
                return raw


def _json_object(raw, final=False):
    """Recognize JSON prefixes without decoding escapes or exposing parser details."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        tail = raw[exc.pos:].strip()
        incomplete = (not tail or exc.msg.startswith('Unterminated string') or
                      (exc.msg == 'Expecting value' and
                       any(word.startswith(tail) for word in ('true', 'false', 'null'))))
        if incomplete and not final:
            return None
        raise ValueError('JSON malformato; nessun file modificato.') from None
    except (ValueError, RecursionError):
        raise ValueError('JSON non valido; nessun file modificato.') from None
    if not isinstance(value, dict):
        raise ValueError('Serve un oggetto JSON; nessun file modificato.')
    return raw


def supply_service_account():
    path = gcal.service_account_file()
    print(f'Percorso JSON nel processo corrente: {path}')
    print('Docker: il default /var/lib/pi-secretary/gcal_service_account.json corrisponde a')
    print('PI_SECRETARY_HOME/data/gcal_service_account.json sull’host.')
    print('Incolla il contenuto JSON, non il percorso del file sul PC.')
    existing = gcal._load_service_account()
    if existing and not confirm('Una chiave valida è già presente. Sostituirla?'):
        return existing
    raw = hidden_json()
    if not raw.strip():
        return None
    path = config.DATA_DIR / 'gcal_service_account.json'
    try:
        data = gcal.validate_service_account(json.loads(raw))
    except (ValueError, TypeError):
        raise ValueError('JSON service account IAM non valido; nessun file modificato.') from None
    with FileLock(str(path) + '.lock'):
        if path.is_symlink():
            raise ValueError('Destinazione symlink non consentita')
        if path.exists() and not confirm('Confermi la sovrascrittura del file chiave esistente?'):
            return None
        atomic_write(path, json.dumps(data, indent=2) + '\n')
    config.set_env('GCAL_SERVICE_ACCOUNT_FILE', str(path))
    gcal._sa_token_cache.update(token='', expires_at=0.0)
    print('Chiave validata e salvata privatamente (0600).')
    return data


def verify_write():
    if not confirm('Creare e cancellare un evento di prova?'):
        return None
    probe = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d %H:%M')
    res = gcal.create_event(title='Test pi-secretary', start=probe, source='setup di prova')
    if not res.get('ok') or not res.get('event_id'):
        print('❌ Scrittura fallita; calendario non abilitato.')
        return False
    try:
        cleanup = gcal.delete_event(res['event_id'])
    except Exception:
        cleanup = {}
    if not cleanup.get('ok'):
        print('❌ Evento di prova non rimosso: eliminarlo manualmente. Calendario non abilitato.')
        return False
    print('✍️ Scrittura e cancellazione OK.')
    return True


def _setup():
    guide('gcal')
    sa = supply_service_account()
    if not sa:
        return 2
    print(f"Condividi il calendario con {sa['client_email']} (Apportare modifiche agli eventi).")
    default = config.get('GCAL_CALENDAR_ID').strip()
    if default.lower() == 'primary':
        default = ''
    chosen_id = input(f'ID da Impostazioni → Integra calendario [INVIO={default}]: ').strip() or default
    if chosen_id.lower() in ('', 'primary'):
        print('❌ Serve l’ID esplicito del calendario condiviso, non primary.')
        return 1
    if not confirm('Verificare ora l’accesso al calendario tramite rete Google?'):
        return 2
    from urllib.parse import quote
    gcal._api('GET', f"/calendars/{quote(chosen_id, safe='')}")
    config.set_env('GCAL_CALENDAR_ID', chosen_id)
    # config.set_env aggiorna l’ambiente, non gli attributi già importati.
    config.GCAL_CALENDAR_ID = chosen_id
    verified = verify_write()
    if not verified:
        return 2 if verified is None else 1
    config.set_env('GCAL_ENABLED', 'true')
    print('✅ Calendar configurato. Prosegui il wizard; nessun avvio automatico.')
    return 0


def main():
    try:
        return _setup()
    except KeyboardInterrupt:
        print('Configurazione Calendar annullata; wizard interrotto. Impostazioni già salvate preservate.')
        return 130
    except EOFError:
        print('Input terminato; configurazione Calendar annullata e wizard interrotto.')
        return 130
    except Exception as exc:
        print(f'Setup Calendar fallito ({type(exc).__name__}); nessun dettaglio privato mostrato.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
