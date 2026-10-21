#!/usr/bin/env python3
"""Verifica rapida che tutti gli account email del .env funzionino.

Uso:
    python3 test_emails.py
"""
import imaplib
import ssl

from secretary.core import config
from secretary.connectors import ms_oauth


def main() -> None:
    ctx = ssl.create_default_context()
    if not config.EMAIL_ACCOUNTS:
        print("Nessun account email configurato nel .env")
        return
    ok = 0
    for acc in config.EMAIL_ACCOUNTS:
        try:
            imap = imaplib.IMAP4_SSL(acc["imap_host"], acc["imap_port"], ssl_context=ctx)
            if acc.get("auth") == "oauth2":
                token = ms_oauth.get_access_token(acc)
                xoauth = f"user={acc['address']}\x01auth=Bearer {token}\x01\x01".encode()
                imap.authenticate("XOAUTH2", lambda _: xoauth)
            else:
                imap.login(acc["address"], acc["password"])
            status, data = imap.select("INBOX", readonly=True)
            n = data[0].decode() if status == "OK" else "?"
            print(f"✅ {acc['name']:20} {acc['address']:38} INBOX: {n} messaggi")
            imap.logout()
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"❌ {acc['name']:20} {acc['address']:38} {exc}")
    print(f"\n{ok}/{len(config.EMAIL_ACCOUNTS)} account OK")


if __name__ == "__main__":
    main()
