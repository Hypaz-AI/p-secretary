#!/usr/bin/env python3
"""Autorizza gli account Microsoft (outlook.com / Microsoft 365) via OAuth2.

Microsoft non accetta più password normali su IMAP/SMTP: questo script esegue
il login "device code" per ogni account Microsoft presente nel .env e salva
i token in data/ms_tokens.json (rinnovati poi in automatico).

Uso:
    python3 setup_ms_auth.py            # solo gli account senza token valido
    python3 setup_ms_auth.py --force     # rifà il login per tutti
"""
import sys

import config
from connectors import ms_oauth


def main() -> None:
    force = "--force" in sys.argv[1:]
    accounts = [a for a in config.EMAIL_ACCOUNTS if a.get("auth") == "oauth2"]
    if not accounts:
        print("Nessun account Microsoft nel .env "
              "(serve EMAIL{n}_PROVIDER=outlook oppure m365).")
        return
    print(f"Account Microsoft da autorizzare: {len(accounts)}")
    for acc in accounts:
        print(f"\n{'=' * 55}\n{acc['name']} — {acc['address']}\n{'=' * 55}")
        try:
            ms_oauth.authorize(acc, force=force)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ errore su {acc['address']}: {exc}")
    print("\nFatto. I token sono salvati in data/ms_tokens.json.")
    print("Verifica con:  python3 test_emails.py")


if __name__ == "__main__":
    main()
