#!/usr/bin/env python3
"""Configura il bot Telegram del segretario, in modo guidato.

1) Crea il bot con @BotFather e incolla qui il token
2) Scrivi un messaggio al tuo bot
3) Token e chat id vengono scritti automaticamente nel .env

Uso:
    python3 setup_telegram.py
"""
import json
import os
import getpass
import secrets
import subprocess
import sys
import time
import urllib.request

import config
from connectors.storage import FileLock, bot_lock_path
from setup_ui import guide


def tg_call(token: str, method: str, params: dict = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def warn_if_service_running() -> None:
    """Il bot può leggere i messaggi da un solo processo alla volta."""
    if os.environ.get('PI_SECRETARY_CONTAINER'):
        print('Fermare app con il launcher prima del setup concorrente.')
        return
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "pi-secretary"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() == "active":
            print("⚠️  Il servizio pi-secretary è ATTIVO.")
            print("    Fermalo prima di continuare, altrimenti i due processi")
            print("    si contendono i messaggi del bot:")
            print("    sudo systemctl stop pi-secretary\n")
            input("Premi INVIO se l'hai fermato (o per continuare comunque) ")
    except Exception:  # noqa: BLE001
        pass  # systemctl non disponibile o servizio inesistente: si prosegue


def main() -> None:
    guide('telegram')

    print("\n1. Apri Telegram, cerca @BotFather e scrivi /newbot")
    print("2. Scegli un nome (es. 'Il mio segretario')")
    print("3. Scegli uno username che finisce con 'bot'")
    print("4. BotFather ti darà un TOKEN tipo '123456789:AAE...'")
    token = getpass.getpass("\nIncolla qui il TOKEN: ").strip()
    if not token:
        return 2
    try:
        with FileLock(bot_lock_path(token), blocking=False):
            if not token:
                return 2
            return pair(token)
    except (Exception, KeyboardInterrupt):
        print('❌ Polling occupato o lock non accessibile: fermare runtime/setup concorrente.')
        return 1


def owner_matches(msg, nonce):
    return bool(msg and msg.get('text', '').strip() == nonce
                and msg.get('chat', {}).get('type') == 'private'
                and not msg.get('from', {}).get('is_bot', False)
                and msg.get('from', {}).get('id') == msg.get('chat', {}).get('id'))


def pair(token):

    try:
        me = tg_call(token, "getMe")
        bot_username = me["result"]["username"]
        print(f"\n✅ Token valido! Il tuo bot è @{bot_username}")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Token non valido ({type(exc).__name__})")
        return 1

    nonce = 'pair-' + secrets.token_urlsafe(24)
    print(f'\nInvia dalla tua chat privata a @{bot_username} questo codice monouso:')
    print(nonce)
    print('Aspetto fino a 120 secondi...')

    chat_id = None
    offset = 0
    deadline = time.time() + 120
    while time.time() < deadline and chat_id is None:
        try:
            updates = tg_call(token, "getUpdates", {"offset": offset, "timeout": 0})
            for u in updates.get("result", []):
                offset = max(offset, u["update_id"] + 1)
                msg = u.get("message") or u.get("edited_message")
                # solo chat private: in un gruppo il chat id sarebbe sbagliato
                if owner_matches(msg, nonce):
                    chat_id = msg["chat"]["id"]
                    name = msg["chat"].get("first_name", "")
                    print(f"\n✅ Messaggio ricevuto da {name}! chat_id = {chat_id}")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)

    if chat_id is None:
        print("\n⚠️  Nessun messaggio ricevuto entro 120 secondi.")
        print("    Verifica la chat privata e il codice monouso, poi ripeti il setup.")
        print("    Non inserire il token in URL del browser: potrebbe restare nella cronologia.")
        return 1

    config.set_env("TELEGRAM_BOT_TOKEN", token)
    config.set_env("TELEGRAM_CHAT_ID", str(chat_id))
    print("   chat_id salvato nel .env")

    try:
        tg_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "🤖 Segretario virtuale: Telegram configurato correttamente!",
        })
        print("\n🎉 Fatto! Ti ho appena mandato un messaggio di test.")
        print("   Ricaricare la configurazione con il launcher up (Docker) o riavviare il servizio host.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n⚠️  Invio del test fallito ({type(exc).__name__})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
