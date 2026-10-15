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
from setup_ui import guide, section


def main() -> int:
    guide('microsoft')
    force = "--force" in sys.argv[1:]
    accounts = [a for a in config.EMAIL_ACCOUNTS if a.get("auth") == "oauth2"]
    if not accounts:
        print("Nessun account Microsoft nel .env "
              "(serve EMAIL{n}_PROVIDER=outlook oppure m365).")
        return 2
    print(f"Account Microsoft da autorizzare: {len(accounts)}")
    failed = False
    for acc in accounts:
        section(f"{acc['name']} — {acc['address']}")
        try:
            ms_oauth.authorize(acc, force=force)
        except Exception as exc:  # noqa: BLE001
            failed = True
            print(f"❌ Autorizzazione fallita ({type(exc).__name__}); riprovare il setup.")
    print("\nSetup incompleto: riprovare gli account falliti." if failed else "\nAutorizzazione completata.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
