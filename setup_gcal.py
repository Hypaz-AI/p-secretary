#!/usr/bin/env python3
"""Collega il Google Calendar dell'utente al segretario.

DUE modalità, in ordine di preferenza:

1. SERVICE ACCOUNT (consigliata: nessun login, nessun consenso, nessuna
   scadenza — l'ideale per un server sempre acceso):
   a. Su https://console.cloud.google.com (stesso progetto dell'API
      Calendar): IAM e amministrazione → Account di servizio → Crea.
      Nessun ruolo necessario. Poi sulla riga dell'account: Chiavi →
      Aggiungi chiave → JSON: scarica il file.
   b. Copia il file qui come  data/gcal_service_account.json
      (oppure imposta GCAL_SERVICE_ACCOUNT_FILE nel .env).
   c. Nella chiave trovi "client_email": qualcosa come
      secretary@<progetto>.iam.gserviceaccount.com — copialo.
   d. Apri https://calendar.google.com, impostazioni del TUO calendario
      → Condividi con persone specifiche: incolla quell'email con permesso
      «Apportare modifiche agli eventi». È come condividere il calendario
      con una persona: il segretario vede e scrive SOLO i calendari che
      gli condividi, nient'altro.
   e. Rilancia questo script: elenca i calendari condivisi, ti fa
      scegliere il tuo e verifica lettura e scrittura.

2. OAUTH2 DEVICE FLOW (fallback): login su https://www.google.com/device.
   Con l'app in modalità Testing il consenso scade dopo 7 giorni.

Uso:
    python3 setup_gcal.py           # rileva da solo la modalità
    python3 setup_gcal.py --force   # rifà il login device flow
"""
import sys
import getpass
from datetime import datetime, timedelta

import config
from connectors import gcal


def verify_write():
    if input('Creare e cancellare un evento di prova? [s/N] ').strip().lower() not in ('s', 'si', 'sì', 'y', 'yes'):
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


def _setup() -> int:
    force = "--force" in sys.argv[1:]
    print("=" * 55)
    print("  Setup Google Calendar — PI-SECRETARY")
    print("=" * 55)

    # --- modalità 1: service account -------------------------------------
    sa = gcal._load_service_account()
    if sa:
        email = sa.get("client_email", "?")
        print(f"\n🔑 Trovata chiave service account: {email}")
        print("   Verifico che il calendario sia condiviso con questo indirizzo...")
        # CalendarList contiene sottoscrizioni, non tutti i calendari
        # accessibili tramite ACL: verifica direttamente l'ID condiviso.
        default = config.get("GCAL_CALENDAR_ID")
        if default == "primary":
            default = ""
        chosen_id = input(f"ID calendario [INVIO={default}]: ").strip() or default
        if not chosen_id:
            print("❌ Inserisci l'ID da Impostazioni → Integra calendario.")
            return 1
        try:
            from urllib.parse import quote
            info = gcal._api("GET", f"/calendars/{quote(chosen_id, safe='')}")
        except Exception as exc:
            print(f"❌ Calendario non accessibile ({type(exc).__name__}); verificare ID e condivisione.")
            return 1
        config.set_env("GCAL_CALENDAR_ID", chosen_id)
        print(f"✅ Calendario scelto: {info.get('summary', chosen_id)}")

        verified = verify_write()
        if not verified:
            return 2 if verified is None else 1
        config.set_env("GCAL_ENABLED", "true")
        print("\n✅ GCAL_ENABLED=true scritto nel .env. Nessun token da")
        print("   rinnovare: la chiave vale finché non la revochi tu.")
        print("   Ricaricare con launcher up (Docker) o riavviare il servizio host.")
        return 0

    # --- modalità 2: OAuth2 device flow ----------------------------------
    print("\nNessuna chiave service account trovata in")
    print(f"   {gcal.service_account_file()}")
    print("\nLa modalità CONSIGLIATA è il service account (nessun login,")
    print("nessuna scadenza): vedi i punti 1a-1d in testa a questo script.")
    print("\nAltrimenti prosegui con il login OAuth2 (scade ogni 7 giorni")
    print("finché l'app non è pubblicata in produzione).")
    proceed = input("\nProcedo con il login OAuth2? [s/N] ").strip().lower()
    if proceed not in ("s", "si", "sì", "y", "yes"):
        print("\nPer il service account: copia la chiave JSON in")
        print(f"   {gcal.service_account_file()}")
        print("   e rilancia questo script.")
        return 2

    client_id = config.GCAL_CLIENT_ID
    if client_id:
        client_id = input(
            f"\nClient ID (INVIO per riusare {client_id[:18]}…): ").strip() or client_id
    else:
        print("\nServe l'ID client OAuth: vedi i prerequisiti in testa a questo script.")
        client_id = input("Client ID: ").strip()
    client_secret = config.GCAL_CLIENT_SECRET
    if client_secret:
        client_secret = getpass.getpass(
            "Client secret (INVIO per riusare quello salvato): ").strip() or client_secret
    else:
        client_secret = getpass.getpass("Client secret: ").strip()
    if not client_id or not client_secret:
        print("❌ ID client o secret mancanti.")
        return 1
    config.set_env("GCAL_CLIENT_ID", client_id)
    config.set_env("GCAL_CLIENT_SECRET", client_secret)

    print("\nOra colleghiamo l'account Google del calendario.")
    try:
        gcal.authorize(force=force)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ autorizzazione fallita ({type(exc).__name__}); riprovare il setup.")
        return 1

    try:
        info = gcal.whoami()
        account = info.get("id", "")
        print(f"✅ Collegato all'account: {account} — "
              f"calendario «{info.get('summary', '?')}»")
        gcal.remember_account(account)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ non riesco a leggere il calendario primario ({type(exc).__name__})")

    cal = input(
        "\nCalendario da usare (INVIO = primary, il principale): ").strip() or "primary"
    config.set_env("GCAL_CALENDAR_ID", cal)

    # verifica di lettura
    try:
        events = gcal.upcoming_events(days=7)
        if events:
            print(f"📖 Lettura OK: prossimo evento «{events[0]['title']}» "
                  f"il {events[0]['dt']}")
        else:
            print("📖 Lettura OK: nessun evento nei prossimi 7 giorni.")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ lettura fallita ({type(exc).__name__})")
        return 1

    verified = verify_write()
    if not verified:
        return 2 if verified is None else 1

    config.set_env("GCAL_ENABLED", "true")
    print("\n✅ GCAL_ENABLED=true scritto nel .env.")
    print("   Ricaricare con launcher up (Docker) o riavviare il servizio host.")
    print("   Poi verifica l'interruttore «Google Calendar» nella dashboard.")
    print("\nNOTA: con la schermata di consenso in modalità «Testing» il token")
    print("scade dopo 7 giorni. Pubblica l'app in produzione (anche senza")
    print("verifica Google) per evitarlo, oppure rifai questo setup quando la")
    print("dashboard segnala token_missing.")
    return 0


def main():
    try:
        return _setup()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Setup Calendar fallito ({type(exc).__name__}); verificare configurazione e riprovare.')
        return 1


if __name__ == "__main__":
    sys.exit(main())
